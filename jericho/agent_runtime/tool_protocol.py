"""Fail-closed normalization for model-emitted tool-call protocols.

OpenAI-compatible runtimes do not always return native ``tool_calls``.  Some
models emit the same control message as an exact JSON object in assistant
content.  This module accepts a deliberately small set of known dialects and
never lets malformed control payloads become user-visible answers.
"""

from __future__ import annotations

import json
import math
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

_MAX_CALLS_PER_TURN = 8
# What fraction of a reply an embedded tool envelope must account for before the
# reply counts as a control payload rather than prose that quotes one.
_ENVELOPE_DOMINANCE = 0.6
_MAX_ARGUMENT_BYTES = 64_000
_MAX_TOOL_NAME_LENGTH = 128
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_FULL_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
# A whole LINE that is nothing but «Call: something» — the shape a model emits
# when it tries to invoke a tool in prose. Anchored end to end because the loose
# version matched «Call: +7 495…» inside a sentence and threw the answer away.
_CALL_MARKER_RE = re.compile(r"(?im)^\s*call\s*:\s*[A-Za-z_][A-Za-z0-9_.:-]*\s*$")
_TOOL_JSON_KEY_RE = re.compile(
    r"""["'](?:tool|name|arguments|args|parameters|input|tool_calls|function_call|function)["']\s*:"""
)
_TOOL_ENVELOPE_TEXT_RE = re.compile(
    r"""(?is)[{[][^}\]]*(?:
        ["'](?:tool|function|tool_calls|function_call)["']\s*:|
        ["']name["']\s*:\s*["'][^"']+["'][^}\]]*["'](?:arguments|args|parameters|input)["']\s*:
    )""",
    re.VERBOSE,
)
_TOOL_NAME_KEYS = ("tool", "name", "function", "action")
_TOOL_ARGUMENT_KEYS = ("arguments", "args", "parameters", "input")
_TOOL_ENVELOPE_KEYS = frozenset(
    {*_TOOL_NAME_KEYS, *_TOOL_ARGUMENT_KEYS, "tool_calls", "function_call", "type", "id"}
)


@dataclass(frozen=True)
class NormalizedToolCall:
    """One validated function invocation in an OpenAI-compatible shape."""

    name: str
    arguments: dict[str, Any]
    call_id: str = ""

    def to_openai(self, fallback_id: str) -> dict[str, Any]:
        return {
            "id": self.call_id or fallback_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(
                    self.arguments,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ),
            },
        }


@dataclass(frozen=True)
class ToolTurn:
    """Classification of a complete assistant turn."""

    kind: Literal["answer", "tool", "protocol_error"]
    text: str = ""
    calls: tuple[NormalizedToolCall, ...] = ()


def _strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} is not allowed")

    return json.loads(text, parse_constant=reject_constant)


def _is_json_value(value: Any) -> bool:
    if isinstance(value, float) and not math.isfinite(value):
        return False
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return False
    return len(encoded.encode("utf-8")) <= _MAX_ARGUMENT_BYTES


def _coerce_arguments(value: Any) -> dict[str, Any] | None:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value if _is_json_value(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if len(text.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            return None
        try:
            parsed = _strict_json_loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) and _is_json_value(parsed) else None
    return None


def _valid_tool_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _MAX_TOOL_NAME_LENGTH or not _TOOL_NAME_RE.fullmatch(name):
        return None
    return name


def _coerce_single_call(data: Any, *, inherited_id: str = "") -> NormalizedToolCall | None:
    if not isinstance(data, dict):
        return None

    call_id = data.get("id") if isinstance(data.get("id"), str) else inherited_id
    call_id = call_id.strip()[:128] if isinstance(call_id, str) else ""

    # Provider wrappers: {"function_call": {...}} and
    # {"type":"function","function": {...}}.
    for wrapper in ("function_call", "function"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            nested = _coerce_single_call(inner, inherited_id=call_id)
            if nested is not None:
                return nested

    name: str | None = None
    for key in _TOOL_NAME_KEYS:
        candidate = _valid_tool_name(data.get(key))
        if candidate is not None:
            name = candidate
            break
    if name is None:
        return None

    arguments: dict[str, Any] | None = None
    for key in _TOOL_ARGUMENT_KEYS:
        if key in data:
            arguments = _coerce_arguments(data.get(key))
            if arguments is None:
                return None
            break
    if arguments is None:
        # A bare explicit ``tool`` is a valid no-argument call.  A generic
        # ``name`` object mixed with unrelated data is ordinary JSON, not
        # control-plane traffic.
        if data.get("tool") is None and set(data) - _TOOL_ENVELOPE_KEYS:
            return None
        arguments = {}

    return NormalizedToolCall(name=name, arguments=arguments, call_id=call_id)


def normalize_tool_payload(data: Any) -> tuple[NormalizedToolCall, ...] | None:
    """Normalize one tool envelope or an OpenAI ``tool_calls`` array.

    ``None`` means the payload cannot be safely executed.  Callers distinguish
    malformed tool-shaped objects from ordinary answer JSON with
    :func:`is_tool_envelope`.
    """

    if not isinstance(data, dict):
        return None

    if "tool_calls" in data:
        raw_calls = data.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls or len(raw_calls) > _MAX_CALLS_PER_TURN:
            return None
        calls: list[NormalizedToolCall] = []
        for raw_call in raw_calls:
            call = _coerce_single_call(raw_call)
            if call is None:
                return None
            calls.append(call)
        return tuple(calls)

    call = _coerce_single_call(data)
    return (call,) if call is not None else None


def normalize_native_tool_calls(data: Any) -> tuple[NormalizedToolCall, ...] | None:
    """Normalize the native ``message.tool_calls`` value from an API response."""

    if not isinstance(data, list) or not data or len(data) > _MAX_CALLS_PER_TURN:
        return None
    return normalize_tool_payload({"tool_calls": data})


def is_tool_envelope(data: dict[str, Any]) -> bool:
    """Return whether JSON was intended as a tool call rather than answer data."""

    has_named_arguments = any(key in data for key in _TOOL_ARGUMENT_KEYS) and isinstance(
        data.get("name"), str
    )
    return (
        "tool_calls" in data
        or "function_call" in data
        or isinstance(data.get("function"), dict)
        or isinstance(data.get("tool"), str)
        or has_named_arguments
    )


def _embedded_objects(text: str, *, limit: int = 6) -> list[tuple[dict[str, Any], int]]:
    """Parse JSON objects embedded in ``text`` as ``(object, length)`` pairs.

    Scans for balanced brace spans and tries each one. Bounded on purpose: the
    question is «is a control payload hiding in here», and a control payload is
    near the start of whatever the model produced, not on page three.
    """
    found: list[tuple[dict[str, Any], int]] = []
    index = 0
    while len(found) < limit:
        start = text.find("{", index)
        if start < 0:
            return found
        depth = 0
        end = -1
        for position in range(start, min(len(text), start + 20_000)):
            character = text[position]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = position + 1
                    break
        if end < 0:
            return found
        with suppress(json.JSONDecodeError, ValueError):
            decoded = _strict_json_loads(text[start:end])
            if isinstance(decoded, dict):
                found.append((decoded, end - start))
        index = end
    return found


def contains_internal_tool_output(content: str) -> bool:
    """Detect control-plane markers that must never be shown to the user.

    Asks whether an actual tool ENVELOPE is present, not whether the text happens
    to mention one of its key names. The old check fired on any `{` followed
    anywhere by `"name":` — so an answer explaining a JSON config, a snippet from
    the owner's own notes, or a reply quoting an API response was classified as a
    protocol violation, discarded whole, and replaced with «Не удалось безопасно
    завершить вызов инструмента». The user lost the answer and the rounds that
    produced it, for writing about JSON.
    """

    text = (content or "").strip()
    if not text:
        return False
    if _CALL_MARKER_RE.search(text):
        return True
    # The payload has to BE the message, not appear in it. `{"name": …,
    # "parameters": …}` is exactly the shape of a tool call AND exactly the shape
    # of a configuration someone asks about, and nothing in the object itself
    # tells the two apart — only how much of the reply it accounts for. A model
    # that is calling a tool emits the envelope and nothing else; a model that is
    # explaining one wraps it in sentences.
    envelope_chars = max(
        (length for candidate, length in _embedded_objects(text) if is_tool_envelope(candidate)),
        default=0,
    )
    return envelope_chars >= len(text) * _ENVELOPE_DOMINANCE


def classify_tool_turn(content: str) -> ToolTurn:
    """Classify a complete assistant response without leaking control payloads."""

    text = (content or "").strip()
    fenced = _FULL_JSON_FENCE_RE.fullmatch(text)
    candidate = fenced.group(1).strip() if fenced is not None else text
    try:
        decoded = _strict_json_loads(candidate)
    except (json.JSONDecodeError, ValueError):
        decoded = None

    if isinstance(decoded, dict):
        calls = normalize_tool_payload(decoded)
        if calls is not None:
            return ToolTurn(kind="tool", calls=calls)
        if is_tool_envelope(decoded):
            return ToolTurn(kind="protocol_error")

    if contains_internal_tool_output(text):
        return ToolTurn(kind="protocol_error")
    return ToolTurn(kind="answer", text=text)
