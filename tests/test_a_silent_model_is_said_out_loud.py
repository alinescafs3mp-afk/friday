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

import asyncio
from dataclasses import replace

import httpx
import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.agent_runtime.llm import MAX_RETRIES, LLMRouter, LLMUnavailableError


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


class _HalfOpenSilent:
    """A recovery probe that remains in flight while a sibling arrives."""

    def __init__(self) -> None:
        self.attempts = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, *args, **kwargs):
        self.attempts += 1
        self.started.set()
        await self.release.wait()
        raise httpx.ReadTimeout("still silent")


class _InterleavedFailures:
    """One call backs off while a sibling proves that the endpoint is silent."""

    def __init__(self) -> None:
        self.first_attempts = 0
        self.silent_attempts = 0

    async def post(self, *args, **kwargs):
        content = kwargs["json"]["messages"][-1]["content"]
        if content == "fast failure":
            self.first_attempts += 1
            raise httpx.ConnectError("connection refused")
        self.silent_attempts += 1
        raise httpx.ReadTimeout("silent sibling")


class _DelayedToolRefusal:
    """Hold a tool-schema rejection until a sibling opens the breaker."""

    def __init__(self) -> None:
        self.tool_attempts = 0
        self.silent_attempts = 0
        self.tool_started = asyncio.Event()
        self.release_tool_refusal = asyncio.Event()

    async def post(self, url, **kwargs):
        content = kwargs["json"]["messages"][-1]["content"]
        if content == "tool refusal":
            self.tool_attempts += 1
            self.tool_started.set()
            await self.release_tool_refusal.wait()
            return httpx.Response(
                400,
                request=httpx.Request("POST", url),
                text=(
                    '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
                ),
            )
        self.silent_attempts += 1
        raise httpx.ReadTimeout("silent sibling")


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
async def test_one_silent_timeout_blocks_later_calls_until_cooldown_expires(
    settings,
    monkeypatch,
) -> None:
    silent = _Silent()
    client = _client(settings, silent, monkeypatch)

    with pytest.raises(httpx.ReadTimeout):
        await client.chat([{"role": "user", "content": "первый ход"}])
    with pytest.raises(LLMUnavailableError, match="silent cooldown"):
        await client.chat([{"role": "user", "content": "следующий внутренний вызов"}])

    assert silent.attempts == 1, "cooldown снова отправил запрос в доказанно молчащий endpoint"

    # Expiry does not require a health/model probe: the next ordinary call is
    # allowed to test recovery once.  The synthetic endpoint remains silent.
    client._silent_until = 0.0  # noqa: SLF001
    with pytest.raises(httpx.ReadTimeout):
        await client.chat([{"role": "user", "content": "ход после cooldown"}])
    assert silent.attempts == 2


@pytest.mark.asyncio
async def test_expired_cooldown_admits_only_one_half_open_probe(settings, monkeypatch) -> None:
    endpoint = _HalfOpenSilent()
    client = _client(settings, endpoint, monkeypatch)
    client._silent_until = 0.1  # noqa: SLF001 - expired monotonic deadline

    probe = asyncio.create_task(client.chat([{"role": "user", "content": "recovery probe"}]))
    await endpoint.started.wait()
    with pytest.raises(LLMUnavailableError, match="silent cooldown"):
        await client.chat([{"role": "user", "content": "concurrent sibling"}])
    endpoint.release.set()
    with pytest.raises(httpx.ReadTimeout):
        await probe

    assert endpoint.attempts == 1


@pytest.mark.asyncio
async def test_silent_sibling_stops_a_call_already_waiting_to_retry(settings, monkeypatch) -> None:
    endpoint = _InterleavedFailures()
    client = _client(settings, endpoint, monkeypatch)
    retry_waiting = asyncio.Event()
    sibling_timed_out = asyncio.Event()

    async def _coordinated_sleep(_seconds: float) -> None:
        retry_waiting.set()
        await sibling_timed_out.wait()

    monkeypatch.setattr("asyncio.sleep", _coordinated_sleep)
    first = asyncio.create_task(client.chat([{"role": "user", "content": "fast failure"}]))
    await retry_waiting.wait()

    with pytest.raises(httpx.ReadTimeout):
        await client.chat([{"role": "user", "content": "silent sibling"}])
    sibling_timed_out.set()

    with pytest.raises(LLMUnavailableError, match="silent cooldown"):
        await first
    assert endpoint.first_attempts == 1
    assert endpoint.silent_attempts == 1


@pytest.mark.asyncio
async def test_silent_sibling_stops_immediate_schema_less_fallback(settings, monkeypatch) -> None:
    endpoint = _DelayedToolRefusal()
    client = _client(settings, endpoint, monkeypatch)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "synthetic_tool",
                "description": "local deterministic fixture",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    first = asyncio.create_task(
        client.chat(
            [{"role": "user", "content": "tool refusal"}],
            tools=tools,
        )
    )
    await endpoint.tool_started.wait()
    with pytest.raises(httpx.ReadTimeout):
        await client.chat([{"role": "user", "content": "silent sibling"}])
    endpoint.release_tool_refusal.set()

    with pytest.raises(LLMUnavailableError, match="silent cooldown"):
        await first
    assert endpoint.tool_attempts == 1
    assert endpoint.silent_attempts == 1


@pytest.mark.asyncio
async def test_a_refused_connection_is_still_retried(settings, monkeypatch):
    """Другая половина: мгновенный отказ повторять стоит — сервер мог подняться."""
    refusing = _Refusing()
    client = _client(settings, refusing, monkeypatch)

    with pytest.raises(httpx.ConnectError):
        await client.chat([{"role": "user", "content": "привет"}])

    assert refusing.attempts == MAX_RETRIES, "перестали повторять быстрый отказ соединения"
    assert client._silent_until == 0.0  # noqa: SLF001


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


def test_a_timed_out_web_synthesis_never_turns_into_an_archive_only_answer() -> None:
    text = AgentRuntime._offline_response(  # noqa: SLF001
        _context(
            kb_size=1537,
            knowledge_hits=[],
            web_evidence_status="partial",
            web_sources=[
                {"title": "ASUS Ascent GX10: обзор", "url": "https://example.test/review"},
                {"title": "ASUS Ascent GX10: энергопотребление", "url": "https://example.test/power"},
            ],
        ),
        unreachable=True,
        message="Как эта модель по шуму и энергопотреблению?",
    )

    assert text.splitlines()[0].casefold().startswith("⚠️ не могу связаться")
    assert "интернет-поиск завершён" in text.casefold()
    assert "источников найдено: 2" in text.casefold()
    assert "ASUS Ascent GX10: обзор" in text
    assert "в архиве 1537" not in text.casefold()
    assert "подходящего среди них не нашлось" not in text.casefold()


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
