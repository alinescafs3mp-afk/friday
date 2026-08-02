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
    """Отказ не отменяет пользы: что нашлось — показать, но причину сказать первой."""
    hits = [{"title": "Поверка", "summary": "порядок и сроки"}]
    text = AgentRuntime._offline_response(
        _context(kb_size=5, knowledge_hits=hits, answer_mode="personal_knowledge"),
        unreachable=True,
    )
    assert text.splitlines()[0].casefold().startswith("⚠️ не могу связаться")
    assert "Поверка" in text
