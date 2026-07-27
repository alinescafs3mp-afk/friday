"""Local vLLM router with Qwen-safe prompts and bounded request context."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from jericho.config import JerichoSettings, detect_repeated_token_degeneration

LOGGER = logging.getLogger(__name__)

# This is deliberately approximate.  The hard server-side context limit remains
# authoritative, while this guard prevents obviously oversized requests.
CHARS_PER_TOKEN = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 30.0
_CONTEXT_SAFETY_TOKENS = 256
_TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_TRUNCATION_MARKER = "\n… [контекст сокращён Jericho] …\n"


def _system_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse all system prompts into one leading message.

    The Qwen chat template used by the pinned runtime accepts one system message
    and requires it to be the first item.  Jericho assembles several independent
    system blocks (policy, small-KB guidance, retrieved knowledge), so the router
    normalizes them at the single boundary shared by chat and streaming calls.
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
    return result


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


class LLMRouter:
    """Routes foreground/background requests to an OpenAI-compatible vLLM API."""

    def __init__(self, settings: JerichoSettings) -> None:
        self.settings = settings
        self._foreground_sem = asyncio.Semaphore(4)
        self._background_sem = asyncio.Semaphore(1)

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
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("LLM is disabled")

        sem = self._foreground_sem if priority == "foreground" else self._background_sem
        async with sem:
            return await self._chat_impl(messages, temperature, max_tokens, tools)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        priority: str = "foreground",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.enabled:
            raise RuntimeError("LLM is disabled")

        sem = self._foreground_sem if priority == "foreground" else self._background_sem
        async with sem:
            async for chunk in self._stream_impl(messages, temperature, max_tokens, tools):
                yield chunk

    def _prepare_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        requested_output = max_tokens if max_tokens is not None else self.max_tokens
        requested_output = max(64, min(int(requested_output), self.settings.profile.max_model_len - 512))
        tool_chars = len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
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
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
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
    ) -> dict[str, Any]:
        payload = self._prepare_payload(messages, temperature, max_tokens, tools, stream=False)
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                timeout = httpx.Timeout(self.timeout_sec, connect=15.0)
                async with httpx.AsyncClient(
                    timeout=timeout, trust_env=False, headers=self._auth_headers()
                ) as client:
                    response = await client.post(f"{self.base_url}/chat/completions", json=payload)
                    response.raise_for_status()
                    data = response.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError("LLM response has no choices")
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
                    content = self._strip_thinking(content, finish_reason)
                if detect_repeated_token_degeneration(content):
                    raise RuntimeError("LLM response rejected: repeated-token degeneration detected")
                return {
                    "content": content,
                    "tool_calls": tool_calls,
                    "finish_reason": finish_reason,
                    "usage": data.get("usage", {}),
                }

            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if status not in _TRANSIENT_HTTP_STATUSES or attempt >= MAX_RETRIES - 1:
                    raise
                retry_after = _retry_after_seconds(exc.response)
                delay = retry_after if retry_after is not None else RETRY_BASE_DELAY * (2**attempt)
                delay = min(max(0.0, delay), RETRY_MAX_DELAY)
                LOGGER.warning("LLM HTTP %d, retrying in %.1fs (attempt %d)", status, delay, attempt + 1)
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt >= MAX_RETRIES - 1:
                    raise
                delay = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
                LOGGER.warning("LLM transport error, retrying in %.1fs", delay)
                await asyncio.sleep(delay)

        raise last_error or RuntimeError("LLM request failed after all retries")

    async def _stream_impl(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._prepare_payload(messages, temperature, max_tokens, tools, stream=True)
        try:
            timeout = httpx.Timeout(self.timeout_sec, connect=15.0)
            async with (
                httpx.AsyncClient(timeout=timeout, trust_env=False, headers=self._auth_headers()) as client,
                client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as response,
            ):
                response.raise_for_status()
                buffer = ""
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[len("data: ") :]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        continue
                    choice = choices[0]
                    delta_value = choice.get("delta")
                    delta: dict[str, Any] = delta_value if isinstance(delta_value, dict) else {}
                    content = delta.get("content") or ""
                    finish = choice.get("finish_reason")
                    if content:
                        content = str(content)
                        buffer += content
                        yield {"type": "delta", "content": content}
                    if finish:
                        if self.settings.profile.suppress_model_thinking:
                            buffer = self._strip_thinking(buffer, str(finish))
                        if detect_repeated_token_degeneration(buffer):
                            yield {
                                "type": "error",
                                "error": "LLM response rejected: repeated-token degeneration detected",
                            }
                            return
                        yield {"type": "done", "content": buffer, "finish_reason": finish}
        except Exception as exc:
            yield {"type": "error", "error": str(exc)}

    @staticmethod
    def _strip_thinking(content: str, finish_reason: str = "stop") -> str:
        """Remove a reasoning model's visible chain-of-thought from its answer.

        Runtimes that ignore ``enable_thinking=False`` emit the monologue in ``content``
        itself, and vLLM leaves ``message.reasoning`` empty rather than separating it.
        Measured against the LAN endpoint: the reasoning is CLOSED by a literal
        ``</think>`` with no opening tag, and the answer follows it. So the tag is the
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
        if finish_reason == "length":
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
