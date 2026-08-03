"""Молчащая модель: сказать об этом сразу и не ждать второй раз впустую.

Замерено на живом отказе 2026-08-02. Сервер модели отвечал на служебные запросы
за 30 мс, а генерацию не начинал вовсе: короткий вопрос висел 120 с без единого
токена. Пятница отработала два полных таймаута по 240 с подряд и записала ответ
через 8 минут 40 секунд — а ответ оказался таким:

    «В базе 1533 объектов, но надёжного совпадения нет. Попробуйте уточнить
     формулировку. LLM сейчас недоступна.»

То есть человеку предложили поправить ФОРМУЛИРОВКУ в ответ на поломку СВЯЗИ, а
настоящая причина стояла последней строкой после точки. Он ждал этого почти
девять минут и всё равно не понял, что случилось.

Здесь обе половины: причина идёт первой и своими словами, а молчащий сервер не
получает второго полного таймаута — повтор был обречён ровно так же, как первый.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.agent_runtime.llm import MAX_RETRIES, LLMRouter


class _Silent:
    """Сервер, который принимает запрос и молчит — ровно живой случай."""

    def __init__(self) -> None:
        self.attempts = 0

    async def post(self, *args, **kwargs):
        self.attempts += 1
        raise httpx.ReadTimeout("timed out")


class _Refusing:
    """Сервер, который не поднялся: отказ мгновенный, повтор осмыслен."""

    def __init__(self) -> None:
        self.attempts = 0

    async def post(self, *args, **kwargs):
        self.attempts += 1
        raise httpx.ConnectError("connection refused")


def _client(settings, transport, monkeypatch) -> LLMRouter:
    client = LLMRouter(replace(settings, llm_enabled=True, llm_timeout_sec=240.0))

    class _Ctx:
        async def __aenter__(self):
            return transport

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Ctx())
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    return client


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_a_silent_endpoint_is_not_asked_twice(settings, monkeypatch):
    """Мутация: убрать ветку ReadTimeout — попыток станет три.

    Цена мутации в секундах живого таймаута: 240 → 720. Замеренное ожидание
    человека при отказе было 520 с; после правки один таймаут, ~240 с.
    """
    silent = _Silent()
    client = _client(settings, silent, monkeypatch)

    with pytest.raises(httpx.ReadTimeout):
        await client.chat([{"role": "user", "content": "привет"}])

    assert silent.attempts == 1, (
        f"молчащий сервер опрошен {silent.attempts} раз(а) — это "
        f"{silent.attempts * 240:.0f} с ожидания вместо 240"
    )


@pytest.mark.asyncio
async def test_a_refused_connection_is_still_retried(settings, monkeypatch):
    """Другая половина: мгновенный отказ повторять стоит — сервер мог подняться."""
    refusing = _Refusing()
    client = _client(settings, refusing, monkeypatch)

    with pytest.raises(httpx.ConnectError):
        await client.chat([{"role": "user", "content": "привет"}])

    assert refusing.attempts == MAX_RETRIES, "перестали повторять быстрый отказ соединения"


def _context(**kwargs) -> AgentContext:
    context = AgentContext(conversation_id="conv_test", user_id="boss")
    for key, value in kwargs.items():
        setattr(context, key, value)
    return context


def test_the_reason_comes_first_not_after_a_full_stop() -> None:
    """Мутация: убрать заголовок — тест краснеет.

    Проверяется не наличие слова где-нибудь в тексте, а ПЕРВАЯ строка: именно её
    человек читает в уведомлении Telegram, и именно она была не о том.
    """
    text = AgentRuntime._offline_response(_context(kb_size=1533), unreachable=True)
    first_line = text.splitlines()[0]
    assert "не отвеча" in first_line.casefold(), f"первая строка не о причине: {first_line!r}"
    assert "уточнить формулировку" not in text, "поломку связи всё ещё выдают за плохую формулировку"


def test_the_disabled_model_is_not_called_a_failure() -> None:
    """Выключенная модель — настройка человека, а не поломка."""
    text = AgentRuntime._offline_response(_context(kb_size=10), unreachable=False)
    assert "не отвеча" not in text.casefold()
    assert "недоступна" in text


def test_found_material_is_still_offered_with_the_reason_on_top() -> None:
    """Отказ не отменяет пользы: что нашлось — показать, но причину сказать первой.

    Уточнено 2026-08-03 по живому отказу: показывается не «что нашлось вообще», а
    что нашлось УВЕРЕННО по вопросу, который сам указывает на свои материалы.
    Ядро проверки осталось прежним — польза не отменяется и причина стоит первой.
    """
    hits = [{"title": "Поверка", "summary": "порядок и сроки", "_rerank_score": 0.81}]
    text = AgentRuntime._offline_response(
        _context(kb_size=5, knowledge_hits=hits, answer_mode="personal_knowledge"),
        unreachable=True,
        message="что там по поверке в моей базе",
    )
    assert text.splitlines()[0].casefold().startswith("⚠️ не могу связаться")
    assert "Поверка" in text


def test_an_outage_does_not_dump_the_archive_at_a_stray_remark() -> None:
    """Найдено на живом отказе 2026-08-03.

    За двадцать минут человек получил восемь отказов подряд, пять из них — с
    выдержками из документов на 700–1200 знаков, включая таблицы. Одно из его
    сообщений было длиной в ЧЕТЫРЕ знака.

    Причина не в пороге совпадения, а в том, что при отказе модели не остаётся
    ничего, что понимает вопрос: намерение распознаёт арбитр, а он работает через
    ту же модель. Поиск при этом отрабатывает всегда, и у короткой реплики счёт
    совпадения ВЫШЕ, чем у настоящего вопроса. Чем меньше человек написал, тем
    увереннее ему показывали чужой документ.
    """
    hits = [{"title": "Ведомость", "summary": "таблица со сроками", "_rerank_score": 0.83}]
    text = AgentRuntime._offline_response(
        _context(kb_size=1533, knowledge_hits=hits, answer_mode="personal_knowledge"),
        unreachable=True,
        message="Э-э",
    )

    assert "Ведомость" not in text, "документ ушёл человеку в ответ на междометие"
    assert "таблица со сроками" not in text
    assert "не отвеча" in text.splitlines()[0].casefold()


def test_the_promise_in_the_header_matches_what_follows() -> None:
    """«Пробую обойтись тем, что есть в архиве» — обещание, а не украшение.

    Если архив не показывается, эта фраза обещает человеку то, чего дальше нет:
    он дочитывает до конца в поисках обещанного. Служебная строка обязана читаться
    как правда — класс, чинившийся на этом проекте дважды.
    """
    silent = AgentRuntime._offline_response(
        _context(kb_size=1533, knowledge_hits=[]), unreachable=True, message="привет"
    )
    assert "в архиве" not in silent.splitlines()[0].casefold()

    hits = [{"title": "Поверка", "summary": "сроки", "_rerank_score": 0.7}]
    shown = AgentRuntime._offline_response(
        _context(kb_size=5, knowledge_hits=hits, answer_mode="personal_knowledge"),
        unreachable=True,
        message="что у меня по поверке",
    )
    assert "в архиве" in shown.splitlines()[0].casefold()


def test_a_weak_match_stays_hidden_even_on_a_proper_question() -> None:
    """Два признака нужны ОБА: замеренный порог 0.5 не отменяется словом «мои»."""
    hits = [{"title": "Ведомость", "summary": "таблица", "_rerank_score": 0.2}]
    text = AgentRuntime._offline_response(
        _context(kb_size=1533, knowledge_hits=hits, answer_mode="personal_knowledge"),
        unreachable=True,
        message="что там по моим приборам",
    )
    assert "Ведомость" not in text
