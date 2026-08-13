"""Local vLLM router with Qwen-safe prompts and bounded request context."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from friday.config import FridaySettings, detect_repeated_token_degeneration

LOGGER = logging.getLogger(__name__)

# This is deliberately approximate.  The hard server-side context limit remains
# authoritative, while this guard prevents obviously oversized requests.
CHARS_PER_TOKEN = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 30.0
#: Short semantic classifiers return a label or one bounded JSON object, not a
#: user-facing answer.  Letting them inherit the normal 2048-token answer budget
#: turns one malformed verdict into a full generation and, with non-streaming
#: HTTP, into a caller that sees no bytes until the global read timeout.  The
#: verifier deliberately keeps its own larger explicit budget; this ceiling is
#: only for classifier call sites.
CLASSIFIER_MAX_TOKENS = 256
#: A full read timeout proves more than a fast connection failure: the server
#: accepted a generation request and then produced no response for the entire
#: configured deadline.  One Friday turn can make several classifier/main/
#: verifier calls, so immediately asking the same silent endpoint again merely
#: multiplies the person's wait.  Keep the breaker local to this router process;
#: expiry performs the next real probe naturally, without a separate health
#: request or background task.
SILENT_ENDPOINT_COOLDOWN_SEC = 300.0
_CONTEXT_SAFETY_TOKENS = 256
_TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_TRUNCATION_MARKER = "\n… [контекст сокращён Friday] …\n"
_MAX_REPORTED_TOOL_NAMES = 64
_MAX_REPORTED_TOOL_NAME_CHARS = 128
_TOOL_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


async def _await_http_request(request: Any) -> httpx.Response:
    """Await one HTTP request and drain its cancellation before returning.

    Cancelling the caller already propagates into ``httpx``.  Keeping the
    request in an explicit task adds the other half of the contract: cancellation
    is awaited to completion before the surrounding ``AsyncClient`` closes, so
    no local request coroutine survives the failed chat call.  A remote server
    may still ignore a disconnected client; only that server can guarantee its
    own generation is aborted.
    """

    request_task = asyncio.create_task(request)
    try:
        return await request_task
    except asyncio.CancelledError:
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        raise


def _system_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse all system prompts into one leading message.

    The Qwen chat template used by the pinned runtime accepts one system message
    and requires it to be the first item.  Friday assembles several independent
    system blocks (policy, small-KB guidance, retrieved knowledge), so the router
    normalizes them at the single boundary every chat call goes through.
    """

    system = [message for message in messages if message.get("role") == "system"]
    if not system:
        return messages
    if len(system) == 1 and messages and messages[0].get("role") == "system":
        return messages
    others = [message for message in messages if message.get("role") != "system"]
    merged = {
        "role": "system",
        "content": "\n\n".join(
            str(message.get("content", "")) for message in system if str(message.get("content", "")).strip()
        ),
    }
    return [merged, *others]


def _message_chars(message: dict[str, Any]) -> int:
    """Estimate prompt size while treating images as model tokens, not base64 text.

    OpenAI-compatible multimodal payloads carry image bytes in data URLs. Their
    serialized length is not a useful approximation of context usage and would
    make the router reject every normal scan before vLLM can apply its own
    vision-token accounting. Text and metadata remain conservatively counted.
    """

    content = message.get("content")
    if isinstance(content, list):
        estimate = 128
        for part in content:
            if not isinstance(part, dict):
                estimate += len(str(part))
                continue
            if part.get("type") == "image_url":
                # Deliberately conservative fixed budget per bounded image.
                estimate += 4_096
            else:
                try:
                    estimate += len(json.dumps(part, ensure_ascii=False, separators=(",", ":"), default=str))
                except (TypeError, ValueError):
                    estimate += len(str(part))
        clone = {key: value for key, value in message.items() if key != "content"}
        try:
            estimate += len(json.dumps(clone, ensure_ascii=False, separators=(",", ":"), default=str))
        except (TypeError, ValueError):
            estimate += len(str(clone))
        return estimate
    try:
        return len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return len(str(message))


def _truncate_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= len(_TRUNCATION_MARKER) + 32:
        return value[:limit]
    remaining = limit - len(_TRUNCATION_MARKER)
    head = max(16, remaining * 2 // 3)
    tail = max(0, remaining - head)
    return value[:head] + _TRUNCATION_MARKER + (value[-tail:] if tail else "")


def _truncate_message(message: dict[str, Any], limit: int) -> dict[str, Any]:
    """Return a structurally valid, smaller message.

    Tool-call assistant messages and their following tool responses are grouped
    before this function is used, so request protocol pairing is preserved.
    """

    clone = dict(message)
    content = clone.get("content")
    if isinstance(content, str):
        clone["content"] = _truncate_text(content, max(0, limit - 128))
    elif content is not None and not isinstance(content, (list, dict)):
        clone["content"] = _truncate_text(str(content), max(0, limit - 128))

    tool_calls = clone.get("tool_calls")
    if isinstance(tool_calls, list) and _message_chars(clone) > limit:
        compact_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            compact = dict(call)
            function = compact.get("function")
            if isinstance(function, dict):
                function = dict(function)
                arguments = function.get("arguments")
                if isinstance(arguments, str) and len(arguments) > 2_000:
                    function["arguments"] = json.dumps(
                        {"_truncated": True, "sha256_unavailable_in_prompt": True},
                        separators=(",", ":"),
                    )
                compact["function"] = function
            compact_calls.append(compact)
        clone["tool_calls"] = compact_calls

    if _message_chars(clone) > limit and isinstance(clone.get("content"), str):
        clone["content"] = _truncate_text(clone["content"], max(0, limit // 2))
    return clone


def _message_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group assistant tool calls with their tool responses."""

    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
            groups.append(group)
            continue
        groups.append(group)
        index += 1
    return groups


def _fit_messages_to_context(
    messages: list[dict[str, Any]],
    *,
    max_model_len: int,
    max_output_tokens: int,
    extra_prompt_chars: int = 0,
) -> list[dict[str, Any]]:
    """Keep the newest coherent prompt groups inside the model context budget."""

    normalized = _system_first(messages)
    max_input_tokens = max(512, max_model_len - max_output_tokens - _CONTEXT_SAFETY_TOKENS)
    char_budget = max(2_048, max_input_tokens * CHARS_PER_TOKEN - max(0, extra_prompt_chars))
    if sum(_message_chars(item) for item in normalized) <= char_budget:
        return normalized

    leading_system: dict[str, Any] | None = None
    remainder = normalized
    if normalized and normalized[0].get("role") == "system":
        # Keep policy/retrieval instructions, but never let them consume the
        # entire request.  In normal operation this block is only a few KiB.
        system_budget = min(max(2_048, char_budget // 3), 24_000)
        leading_system = _truncate_message(normalized[0], system_budget)
        remainder = normalized[1:]
        char_budget -= _message_chars(leading_system)

    groups = _message_groups(remainder)
    selected_reversed: list[list[dict[str, Any]]] = []
    remaining = max(512, char_budget)
    for group_index, group in reversed(list(enumerate(groups))):
        group_size = sum(_message_chars(item) for item in group)
        is_latest = group_index == len(groups) - 1
        if group_size <= remaining:
            selected_reversed.append([dict(item) for item in group])
            remaining -= group_size
            continue
        if is_latest or not selected_reversed:
            per_message = max(256, remaining // max(1, len(group)))
            selected_reversed.append([_truncate_message(item, per_message) for item in group])
            remaining = 0
        # Older groups are intentionally dropped rather than producing an
        # invalid over-context request.
        if remaining <= 0:
            break

    selected = [item for group in reversed(selected_reversed) for item in group]
    result = ([leading_system] if leading_system is not None else []) + selected
    if not result and normalized:
        return [_truncate_message(normalized[-1], max(512, char_budget))]
    # Обрез больше не молчит.
    #
    # Окно модели — 32 768 токенов, и в тяжёлом ходе (описания инструментов ~4650,
    # результат инструмента до 4000, выдача поиска до 4000, история, найденные
    # документы) запрос подходит к пределу вплотную. Тогда старые группы
    # выбрасываются — и до сегодняшнего дня об этом не узнавал никто: ни человек,
    # который видел, что Пятница «забыла» начало разговора, ни журнал.
    #
    # Молчаливый обрез — известный класс: владелец сам нашёл его сегодня в блоке
    # автопроверки. Предел законен, молчание о нём — нет.
    dropped_groups = len(groups) - len(selected_reversed)
    if dropped_groups > 0 or len(selected) < len(remainder):
        LOGGER.info(
            "context: промпт не поместился в %d ток. — выброшено групп: %d, сообщений: %d из %d",
            max_model_len,
            max(0, dropped_groups),
            max(0, len(remainder) - len(selected)),
            len(remainder),
        )
        # И модель тоже должна знать, что видит не весь разговор. Иначе она
        # отвечает так, будто помнит всё, и человек получает уверенное «мы этого
        # не обсуждали» про то, что обсуждали десять минут назад.
        result.insert(
            1 if leading_system is not None else 0,
            {
                "role": "system",
                "content": (
                    "Начало этого разговора не поместилось в твою память и сюда не попало. "
                    "Если человек ссылается на сказанное раньше, а ты этого не видишь — так и "
                    "скажи и попроси напомнить, а не утверждай, что такого не было."
                ),
            },
        )
    return result


class LLMUnavailableError(RuntimeError):
    """Отказал КАНАЛ до модели, а не конкретный запрос.

    Различие нужно фоновым рабочим: советчик Inbox останавливает весь разбор
    арендатора, решив, что модель недоступна, и в живом журнале он делал это на
    ошибках разбора ответа — то есть при отвечающей модели. Считать «модель не
    вернула JSON» и «соединение оборвалось» одним и тем же значит ставить ложный
    диагноз; из 1188 неудач советчика 1023 были первого рода и 164 — второго.

    Наследуется от RuntimeError, чтобы прежние обработчики продолжали ловить его
    как раньше: тип уточняет причину, а не меняет контракт.
    """


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            now = parsed.now(parsed.tzinfo)
            return max(0.0, (parsed - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _tools_unsupported(body: str) -> bool:
    """Отвергает ли сервер именно ИНСТРУМЕНТЫ, а не запрос вообще.

    Проверяется по тексту ошибки, а не по одному коду 400: тем же кодом отвечают на
    слишком длинный контекст, неизвестную модель и кривые сообщения, и молча
    выбрасывать инструменты в этих случаях значило бы лечить симптом чужой болезни.
    Формулировка vLLM: «"auto" tool choice requires --enable-auto-tool-choice and
    --tool-call-parser to be set».
    """
    text = (body or "").casefold()
    return "tool" in text and (
        "tool choice" in text or "tool_call_parser" in text or "tool-call-parser" in text
    )


def _bounded_tool_schema_names(tools: Any) -> list[str]:
    """Capability names present in one actual model payload, bounded and content-free.

    This is transport evidence for the caller, not a copy of schemas.  Descriptions,
    parameters and any values embedded in them never leave the request payload through
    this signal.  Invalid and excess entries are omitted, which is fail-closed when the
    caller later checks a model-emitted name against the returned list.
    """

    if not isinstance(tools, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in tools:
        if len(names) >= _MAX_REPORTED_TOOL_NAMES:
            break
        if not isinstance(item, Mapping) or item.get("type") != "function":
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        raw_name = function.get("name")
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if (
            not name
            or len(name) > _MAX_REPORTED_TOOL_NAME_CHARS
            or _TOOL_SCHEMA_NAME_RE.fullmatch(name) is None
            or name in seen
        ):
            continue
        seen.add(name)
        names.append(name)
    return names


_TOOL_CALL_MARKUP = re.compile(r"<\s*tool_call\s*>.*?(?:<\s*/\s*tool_call\s*>|$)", re.S | re.I)


def _strip_tool_call_markup(content: str) -> str:
    """Разметка вызова инструмента — не текст для человека.

    Модель обязана просить инструмент отдельным полем протокола, но иногда пишет
    его в ОТВЕТ как текст. Замерено на живом экземпляре: вопрос «сколько всего
    знаний в базе? посчитай точно» вернул пользователю буквально

        <tool_call>
        {"name":"kg_stats"}
        </tool_call>

    и больше ничего. Снаружи это неотличимо от поломки, а на демо — хуже того.

    Вызывается на ФИНАЛЬНОМ тексте — том, что уходит человеку, — и только там.
    Раньше эта очистка стояла в `_strip_thinking`, то есть ДО разбора протокола, и
    ломала работающий механизм: `tool_protocol` умеет распознать такой текстовый
    вызов и ИСПОЛНИТЬ его, а вырезанный блок превращался в пустоту, пустота — в
    «нарушение протокола», и пользователь получал «не удалось безопасно завершить
    вызов инструмента» там, где раньше получал ответ. Поймано сравнением ответов
    до и после правки, а не тестом.
    """
    if "tool_call" not in content.casefold():
        return content
    return _TOOL_CALL_MARKUP.sub("", content).strip()


#: Блок рассуждения модели: в переписке это служебный текст, а не ответ.
_THINKING_MARKUP = re.compile(r"<think>.*?</think>|</?think>", re.IGNORECASE | re.DOTALL)


def strip_service_markup(content: str) -> str:
    """Убрать из сохранённого текста всё служебное перед показом человеку.

    Нужна отдельно от `_strip_tool_call_markup`, потому что в базе уже лежат
    сообщения, записанные ДО появления очистки на выходе модели: 21 штука с
    `<tool_call>` или `</think>`. Сообщения чата неудаляемы, переписать их
    нельзя — значит чистить надо на выводе, каждый раз.
    """
    return _THINKING_MARKUP.sub("", _strip_tool_call_markup(content or "")).strip()


class LLMRouter:
    """Routes foreground/background requests to an OpenAI-compatible vLLM API."""

    def __init__(self, settings: FridaySettings) -> None:
        self.settings = settings
        self._foreground_sem = asyncio.Semaphore(max(1, int(settings.llm_foreground_slots)))
        self._background_sem = asyncio.Semaphore(1)
        # Помним отказ эндпоинта от инструментов, чтобы не платить за него каждый раз.
        self._tools_refused = False
        # Monotonic deadline after a proven full read timeout.  Fast transport
        # failures do not open it: a restarted endpoint is worth retrying.
        self._silent_until = 0.0
        # Видели ли мы у этого профиля рассуждение вслух. Пока не видели, обрыв
        # по длине означает обрезанный ОТВЕТ, а не незакрытый монолог, и стирать
        # его нельзя — см. `_strip_thinking`.
        self._thinking_seen = False

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    @property
    def base_url(self) -> str:
        return self.settings.llm_base_url

    def _auth_headers(self) -> dict[str, str]:
        # Bearer auth for an OpenAI-compatible endpoint (e.g. a vLLM on the LAN
        # started with --api-key). Empty key = no header (local unauthenticated).
        key = self.settings.llm_api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    @property
    def model(self) -> str:
        return self.settings.llm_model

    @property
    def timeout_sec(self) -> float:
        return self.settings.llm_timeout_sec

    @property
    def total_budget_sec(self) -> float:
        """Ceiling on ONE call, retries included — 1.5x a single attempt.

        Not `MAX_RETRIES * timeout_sec`: that is the number this exists to cut. One
        full attempt plus half of another leaves room for a real retry after a fast
        failure (connection refused, HTTP 503) while refusing to spend three whole
        timeouts on an endpoint that accepts the connection and then says nothing.

        Число живёт в настройках, а не здесь: из него же выводится потолок хода и
        таймаут моста, и три копии одной формулы уже расходились однажды.
        """
        return self.settings.llm_call_budget_sec

    @property
    def max_tokens(self) -> int:
        return self.settings.llm_max_tokens

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // CHARS_PER_TOKEN)

    def estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        return max(1, sum(_message_chars(message) for message in messages) // CHARS_PER_TOKEN)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        priority: str = "foreground",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reject_repeated_token_degeneration: bool = True,
        allow_retries: bool = True,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise LLMUnavailableError("LLM is disabled")

        sem = self._foreground_sem if priority == "foreground" else self._background_sem
        queue_started = time.monotonic()
        async with sem:
            # How long this call waited for one of the shared slots, before it
            # ever reached the model. `total_budget_sec` (below) starts only
            # once the slot is held, so it already excludes this — a caller
            # measuring its OWN elapsed budget across several calls (the
            # agentic tool loop) needs the same exclusion or queueing under
            # real concurrent load eats a budget meant for the model being
            # slow, and cuts a healthy, busy deployment short for the wrong
            # reason.
            queue_wait_sec = time.monotonic() - queue_started
            remaining_silence = self._silent_until - time.monotonic()
            if remaining_silence > 0:
                LOGGER.warning(
                    "LLM endpoint is in silent cooldown for another %.0fs; skipping request",
                    remaining_silence,
                )
                raise LLMUnavailableError("LLM endpoint is in silent cooldown")
            # Once the cooldown expires, admit exactly one half-open recovery
            # probe.  Other foreground slots must not fan out four identical
            # long requests before that probe has established recovery.
            half_open_probe = self._silent_until > 0
            if half_open_probe:
                self._silent_until = float("inf")
            try:
                result = await self._chat_impl(
                    messages,
                    temperature,
                    max_tokens,
                    tools,
                    tool_choice,
                    half_open_probe=half_open_probe,
                    reject_repeated_token_degeneration=reject_repeated_token_degeneration,
                    allow_retries=allow_retries,
                )
            except BaseException:
                # A full ReadTimeout replaces the sentinel with a fresh finite
                # deadline inside `_chat_impl`.  Fast failures and cancellation
                # prove no continued silence, so allow a later ordinary call.
                if half_open_probe and self._silent_until == float("inf"):
                    self._silent_until = 0.0
                raise
        result["_queue_wait_sec"] = queue_wait_sec
        return result

    def _prepare_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        requested_output = max_tokens if max_tokens is not None else self.max_tokens
        requested_output = max(64, min(int(requested_output), self.settings.profile.max_model_len - 512))
        # A 400 which specifically identified unsupported tool calling is an
        # endpoint capability result, not a one-request accident.  Keep the
        # successful schema-less mode for later chats on this router instance;
        # otherwise every user message pays for the same doomed request first.
        effective_tools = None if self._tools_refused else tools
        tool_chars = (
            len(json.dumps(effective_tools, ensure_ascii=False, default=str)) if effective_tools else 0
        )
        fitted = _fit_messages_to_context(
            messages,
            max_model_len=self.settings.profile.max_model_len,
            max_output_tokens=requested_output,
            extra_prompt_chars=tool_chars,
        )
        input_tokens = self.estimate_messages_tokens(fitted) + tool_chars // CHARS_PER_TOKEN
        available_output = self.settings.profile.max_model_len - input_tokens - _CONTEXT_SAFETY_TOKENS
        if available_output < 64:
            raise ValueError("Prompt is too large for the configured model context")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": fitted,
            "temperature": temperature if temperature is not None else self.settings.profile.temperature,
            "max_tokens": min(requested_output, available_output),
            # Потоковой выдачи у этого клиента нет: она была написана целиком и
            # никем не звалась — ни кодом, ни тестом, — и удалена 2026-08-05.
            # История в git; при возвращении её придётся написать заново вместе
            # с потребителем, потому что непроверяемый сетевой код доверия не
            # заслуживает.
            "stream": False,
        }
        if effective_tools:
            payload["tools"] = effective_tools
            offered_names = set(_bounded_tool_schema_names(effective_tools))
            if isinstance(tool_choice, str) and tool_choice in offered_names:
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice},
                }
        if self.settings.profile.suppress_model_thinking:
            # This is the supported Qwen path; post-processing below is only a
            # defense-in-depth fallback for non-conforming model responses.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    async def _chat_impl(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        *,
        half_open_probe: bool = False,
        reject_repeated_token_degeneration: bool = True,
        allow_retries: bool = True,
    ) -> dict[str, Any]:
        payload = self._prepare_payload(messages, temperature, max_tokens, tools, tool_choice)
        last_error: Exception | None = None

        # One budget for the whole series, not per attempt. Each attempt is allowed
        # `timeout_sec`, so three of them plus backoff cost 3*240 + 6 = 726 seconds —
        # and that is per CALL, while one user message makes several: measured, a
        # slow-but-alive endpoint in research mode costs seven calls, 85 minutes, all
        # of it holding one of four foreground slots. Nothing outside cuts it short:
        # there is no request timeout on the server, and the Telegram bridge giving up
        # after 270 seconds does not cancel the handler.
        #
        # The deadline stops a NEW attempt from starting, rather than interrupting one
        # in flight — a request that is answering must not be killed mid-stream.
        deadline = time.monotonic() + self.total_budget_sec
        # Начало отсчёта для строки замера ниже. Без неё вопрос «почему ответ
        # идёт полторы минуты» не имеет ответа: один ход человека делает
        # несколько вызовов модели, и до этого ни один из них не был измерен —
        # чинить приходилось бы вслепую.
        call_started = time.monotonic()

        def _ensure_retry_allowed() -> None:
            remaining_silence = self._silent_until - time.monotonic()
            owns_half_open_sentinel = half_open_probe and self._silent_until == float("inf")
            if remaining_silence > 0 and not owns_half_open_sentinel:
                LOGGER.warning("LLM endpoint became silent during retry; skipping request")
                raise LLMUnavailableError("LLM endpoint is in silent cooldown")

        # Some routes have already completed an expensive or externally visible
        # stage and own a deterministic fallback.  They can opt out of transport
        # retries so one failed synthesis never starts a second generation for
        # the same accepted evidence.
        max_attempts = MAX_RETRIES if allow_retries else 1
        for attempt in range(max_attempts):
            if attempt and time.monotonic() >= deadline:
                LOGGER.warning("LLM budget of %.0fs is spent; not retrying", self.total_budget_sec)
                break
            if attempt:
                _ensure_retry_allowed()
            try:
                timeout = httpx.Timeout(self.timeout_sec, connect=15.0)
                async with httpx.AsyncClient(
                    timeout=timeout, trust_env=False, headers=self._auth_headers()
                ) as client:
                    response = await _await_http_request(
                        client.post(f"{self.base_url}/chat/completions", json=payload)
                    )
                    if (
                        allow_retries
                        and response.status_code == 400
                        and payload.get("tools")
                        and _tools_unsupported(response.text)
                    ):
                        # Сервер не умеет вызов инструментов — это НЕ повод потерять ответ.
                        # vLLM, запущенный без `--enable-auto-tool-choice` и
                        # `--tool-call-parser`, отвергает ЛЮБОЙ запрос с `tools` четырёхсотым,
                        # а агент шлёт их всегда. На этой установке из-за этого не работал
                        # ни один вызов инструмента с самого начала, и человек видел
                        # «LLM сейчас недоступна» вместо ответа: отказ в одной способности
                        # выглядел как отказ модели целиком.
                        #
                        # Запоминаем на экземпляре, чтобы не платить отвергнутым запросом
                        # за каждое сообщение, и говорим один раз вслух.
                        if not self._tools_refused:
                            self._tools_refused = True
                            LOGGER.warning(
                                "LLM endpoint refuses tool calls (start vLLM with "
                                "--enable-auto-tool-choice and --tool-call-parser); "
                                "continuing without tools"
                            )
                        payload = {
                            key: value
                            for key, value in payload.items()
                            if key not in {"tools", "tool_choice"}
                        }
                        # This fallback is a second HTTP request.  A sibling may
                        # have proved the endpoint silent while the rejected
                        # schema request was in flight, so consult the shared
                        # breaker before sending the schema-less retry.
                        _ensure_retry_allowed()
                        response = await _await_http_request(
                            client.post(f"{self.base_url}/chat/completions", json=payload)
                        )
                    response.raise_for_status()
                    data = response.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                if not isinstance(choices, list) or not choices:
                    raise LLMUnavailableError("LLM response has no choices")
                choice = choices[0] if isinstance(choices[0], dict) else {}
                message_value = choice.get("message")
                message: dict[str, Any] = message_value if isinstance(message_value, dict) else {}
                content = message.get("content") or ""
                if not isinstance(content, str):
                    content = str(content)
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list):
                    tool_calls = []

                finish_reason = str(choice.get("finish_reason") or "stop")
                if self.settings.profile.suppress_model_thinking and content:
                    content = self._strip_thinking(content, finish_reason, thinking_seen=self._thinking_seen)
                if "</think>" in content or "<think>" in content:
                    # Профиль всё-таки рассуждает вслух — значит обрыв по длине у
                    # него действительно может оставить один монолог.
                    self._thinking_seen = True
                if reject_repeated_token_degeneration and detect_repeated_token_degeneration(content):
                    LOGGER.warning("LLM response rejected: repeated_token_degeneration")
                    raise RuntimeError("LLM response rejected: repeated-token degeneration detected")
                usage_value = data.get("usage")
                usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
                try:
                    prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
                except (TypeError, ValueError):
                    prompt_tokens = 0
                try:
                    completion_tokens = max(0, int(usage.get("completion_tokens") or 0))
                except (TypeError, ValueError):
                    completion_tokens = 0
                safe_finish_reason = (
                    finish_reason
                    if finish_reason in {"stop", "length", "tool_calls", "content_filter", "function_call"}
                    else "other"
                )
                LOGGER.info(
                    "LLM call: %.1fs, промпт %d ток., ответ %d ток., инструментов %d, повод %s",
                    time.monotonic() - call_started,
                    prompt_tokens,
                    completion_tokens,
                    len(payload.get("tools") or []),
                    safe_finish_reason,
                )
                self._silent_until = 0.0
                return {
                    "content": content,
                    "tool_calls": tool_calls,
                    "finish_reason": finish_reason,
                    "usage": data.get("usage", {}),
                    # The first request may have been rejected specifically for
                    # carrying tool schemas and retried without them.  Report the
                    # capabilities in the payload that ACTUALLY produced this
                    # response, never the nominal list passed into ``chat``.
                    "_offered_tool_names": _bounded_tool_schema_names(payload.get("tools")),
                }

            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if status not in _TRANSIENT_HTTP_STATUSES or attempt >= max_attempts - 1:
                    raise
                retry_after = _retry_after_seconds(exc.response)
                delay = retry_after if retry_after is not None else RETRY_BASE_DELAY * (2**attempt)
                delay = min(max(0.0, delay), RETRY_MAX_DELAY)
                LOGGER.warning("LLM HTTP %d, retrying in %.1fs (attempt %d)", status, delay, attempt + 1)
                await asyncio.sleep(delay)
            except httpx.ReadTimeout as exc:
                # Сервер ПРИНЯЛ запрос и молчал весь таймаут — повтор не поможет.
                #
                # Замерено на живом отказе 2026-08-02: сервер модели отвечал на
                # служебные запросы за 30 мс, а генерацию не начинал вовсе.
                # Пятница отработала два полных таймаута по 240 с подряд, и
                # человек ждал ответа 8 минут 40 секунд вместо четырёх — второй
                # заход был обречён ровно так же, как первый.
                #
                # Отказ соединения — другое дело: он мгновенный, и повтор через
                # две секунды нередко попадает в поднявшийся сервер. Поэтому
                # разделены именно эти два случая, а не «сеть» целиком.
                last_error = exc
                self._silent_until = time.monotonic() + SILENT_ENDPOINT_COOLDOWN_SEC
                LOGGER.warning(
                    "LLM read timeout after %.0fs; not retrying a silent endpoint", self.timeout_sec
                )
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt >= max_attempts - 1:
                    raise
                delay = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
                LOGGER.warning("LLM transport error, retrying in %.1fs", delay)
                await asyncio.sleep(delay)

        raise last_error or LLMUnavailableError("LLM request failed after all retries")

    @staticmethod
    def _strip_thinking(content: str, finish_reason: str = "stop", thinking_seen: bool = True) -> str:
        """Remove a reasoning model's visible chain-of-thought from its answer.

        Runtimes that ignore ``enable_thinking=False`` emit the monologue in ``content``
        itself, and vLLM leaves ``message.reasoning`` empty rather than separating it.
        Measured against the LAN endpoint: the reasoning is CLOSED by a literal
        ``</think>`` with no opening tag, and the answer follows it.

        ⚠️ Перемерено 2026-07-30: НЫНЕШНИЙ рантайм этот флаг СОБЛЮДАЕТ. Через роутер
        модель отвечает без монолога (совету по Inbox хватает 78–125 токенов), а тот же
        запрос прямым обращением, без флага, тратит 2500–3600. Разбор ниже остаётся
        защитой на случай рантайма без поддержки флага, но описывать сегодняшнее
        поведение как «игнорирует» — неверно. So the tag is the
        only reliable boundary — the prose that precedes it is unstable, differing
        between two calls with identical parameters and temperature 0.

        The earlier marker heuristic assumed the opposite layout and cut everything
        *before* a phrase like "Here's a thinking process:". Against this model that
        returned either the empty string (marker at index 0, so the caller fell back to
        "Не удалось сформировать ответ.") or the entire monologue, tag included. It is
        kept below only as a fallback for runtimes that really do lead with the marker.

        ``finish_reason`` matters because reasoning consumes the output budget: a
        request that runs out of tokens mid-thought has NO answer to extract, and the
        monologue must not be handed back as though it were one.
        """

        if "</think>" in content:
            return content.rsplit("</think>", 1)[-1].strip()
        if finish_reason == "length" and thinking_seen:
            # Truncated before the model ever reached its answer. Verified on this
            # endpoint: an entity-extraction prompt spends 2000 tokens and still never
            # closes the tag. Empty lets the caller's own fallback speak.
            #
            # An audit read this as "any long answer is erased" and proposed
            # returning the partial text. Not taken, and the reason is the branch
            # above: on a reasoning model a long ANSWER that hits the limit still
            # carries its `</think>`, so it survives there with everything after
            # the tag intact. Reaching this line means the monologue never closed,
            # and there is no answer inside to rescue — only the model's notes,
            # which the enrichment paths would parse as content.
            #
            # ⚠️ Перемерено 2026-08-02: у профиля `qwen36-vl` рассуждение ОТКЛЮЧЕНО
            # флагом (`enable_thinking: False`), и рантайм его соблюдает — в 15+
            # живых ответах `</think>` не встретился ни разу. Значит для него
            # ветка выше сработать не может, а «монолога» здесь не бывает: обрыв
            # по длине означает обрезанный ОТВЕТ, и стирать его — терять то
            # единственное, что модель успела сказать. Поэтому стирание теперь
            # применяется, только если рассуждение в ответах этого профиля вообще
            # встречается (`thinking_seen`).
            return ""
        markers = (
            "Here's a thinking process:",
            "Here is the thinking process:",
            "Thinking process:",
            "Let me think",
        )
        for marker in markers:
            index = content.casefold().find(marker.casefold())
            if index >= 0:
                return content[:index].strip()
        return content.strip()
