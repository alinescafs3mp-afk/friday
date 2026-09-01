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
_CODE_FENCE_PREFIX = "```"
_PAIRED_PROSE_QUOTES = {
    "\u00ab": "\u00bb",
    "\u201c": "\u201d",
}
# A whole LINE that is nothing but «Call: something» — the shape a model emits
# when it tries to invoke a tool in prose. Anchored end to end because the loose
# version matched «Call: +7 495…» inside a sentence and threw the answer away.
_CALL_MARKER_RE = re.compile(r"(?im)^\s*call\s*:\s*[A-Za-z_][A-Za-z0-9_.:-]*\s*$")
_TOOL_CALL_OPEN_RE = re.compile(r"<\s*tool_call\s*>", re.I)
_TOOL_CALL_CLOSE_RE = re.compile(r"<\s*/\s*tool_call\s*>", re.I)
_TAGGED_TOOL_BODY_CONTROL_PREFIX_RE = re.compile(
    r"""(?ix)^(?:
        (?:call|tool|name|function|action)\s*[:=]
        |(?:tool|name|function|action)\s+[A-Za-z_][A-Za-z0-9_.:-]{0,127}
            \s+(?:arguments|args|parameters|input)\b
        |[A-Za-z_][A-Za-z0-9_.:-]{0,127}[ \t]*(?:\r?\n)+[ \t]*[\[{]
        |[A-Za-z_][A-Za-z0-9_.:-]{0,127}\s*\(
        |[A-Za-z_][A-Za-z0-9_.:-]{0,127}\s*$
    )"""
)
_MAX_TOOL_CALL_MARKERS = 64
_MAX_TAGGED_TOOL_BODY_CHARS = 16_000
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
_MALFORMED_TOOL_ROOT_RE = re.compile(
    r"""(?ix)
    \{\s*
    ["']?(?:tool|name|function|action)["']?
    (?:
        [ \t\r\n]*(?P<authority_separator>[:=])[ \t\r\n]*
        |(?P<authority_missing>[ \t\r\n]+)
    )
    ["']?[A-Za-z0-9_.:-]{1,128}["']?
    [ \t\r\n,]*
    ["']?(?:arguments|args|parameters|input)["']?
    (?:
        [ \t\r\n]*(?P<arguments_separator>[:=])
        |(?P<arguments_missing>(?=[ \t\r\n]|[\[{]))
    )
    """
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
    except (TypeError, ValueError, OverflowError, RecursionError):
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
        except (json.JSONDecodeError, ValueError, RecursionError):
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
        with suppress(json.JSONDecodeError, ValueError, RecursionError):
            decoded = _strict_json_loads(text[start:end])
            if isinstance(decoded, dict):
                found.append((decoded, end - start))
        index = end
    return found


def _has_dominant_malformed_tool_root(text: str) -> bool:
    """Find one bounded identity-plus-arguments carrier missed by JSON decode.

    Some runtimes omit both colons while emitting a tool envelope.  Anchoring
    directly at the authority slot keeps earlier ``{x}`` decoys from consuming
    the only candidate budget.  An unclosed root owns the suffix, but it is
    control only when that suffix dominates the complete reply; short examples
    inside ordinary prose therefore remain answers.
    """

    quoted_spans: list[tuple[int, int]] = []
    quote_close = ""
    quote_start = -1
    escaped = False
    for index, character in enumerate(text):
        word_apostrophe = bool(
            character == "'"
            and index > 0
            and index + 1 < len(text)
            and text[index - 1].isalnum()
            and text[index + 1].isalnum()
        )
        if quote_close:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote_close and not word_apostrophe:
                quoted_spans.append((quote_start, index + 1))
                quote_close = ""
                quote_start = -1
            continue
        if character == '"' or (character == "'" and (index == 0 or not text[index - 1].isalnum())):
            quote_close = character
            quote_start = index
        elif character in _PAIRED_PROSE_QUOTES:
            quote_close = _PAIRED_PROSE_QUOTES[character]
            quote_start = index

    scan_stop = len(text)
    position = 0
    quoted_index = 0
    while position < scan_stop:
        while quoted_index < len(quoted_spans) and quoted_spans[quoted_index][1] <= position:
            quoted_index += 1
        if (
            quoted_index < len(quoted_spans)
            and quoted_spans[quoted_index][0] <= position < quoted_spans[quoted_index][1]
        ):
            position = quoted_spans[quoted_index][1]
            continue
        character = text[position]
        if character != "{":
            position += 1
            continue
        match = _MALFORMED_TOOL_ROOT_RE.match(text, position, scan_stop)
        if match is None:
            position += 1
            continue
        if match.group("authority_separator") == ":" and match.group("arguments_separator") == ":":
            position += 1
            continue
        root_start = position
        root_stop = len(text)
        depth = 0
        root_quote = ""
        root_escaped = False
        carrier_stop = root_stop
        root_closed = False
        for position in range(root_start, root_stop):
            character = text[position]
            word_apostrophe = bool(
                character == "'"
                and position > root_start
                and position + 1 < root_stop
                and text[position - 1].isalnum()
                and text[position + 1].isalnum()
            )
            if root_quote:
                if root_escaped:
                    root_escaped = False
                elif character == "\\":
                    root_escaped = True
                elif character == root_quote and not word_apostrophe:
                    root_quote = ""
                continue
            if character == '"' or (
                character == "'" and (position == root_start or not text[position - 1].isalnum())
            ):
                root_quote = character
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    carrier_stop = position + 1
                    root_closed = True
                    break
        if carrier_stop - root_start >= len(text) * _ENVELOPE_DOMINANCE:
            return True
        if not root_closed:
            return False
        position = carrier_stop
    return False


def unclosed_tool_call_markup_has_control(content: str) -> bool:
    """Detect bounded unclosed tag bodies that are control rather than prose."""

    text = content or ""
    opens: list[re.Match[str]] = []
    for match in _TOOL_CALL_OPEN_RE.finditer(text):
        if len(opens) >= _MAX_TOOL_CALL_MARKERS:
            return True
        opens.append(match)
    for index, opened in enumerate(opens):
        next_open = opens[index + 1].start() if index + 1 < len(opens) else len(text)
        closed = _TOOL_CALL_CLOSE_RE.search(text, opened.end(), next_open)
        if closed is not None:
            continue
        body = text[opened.end() : next_open]
        stripped = body.strip()
        if not stripped:
            continue
        if len(body) > _MAX_TAGGED_TOOL_BODY_CHARS:
            return True
        if stripped.startswith(("{", "[", "`", "~")):
            return True
        if _TAGGED_TOOL_BODY_CONTROL_PREFIX_RE.match(stripped) is not None:
            return True
    return False


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
    try:
        complete = _strict_json_loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        pass
    else:
        if isinstance(complete, dict):
            return is_tool_envelope(complete)
        pending = [complete]
        envelope_chars = 0
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if is_tool_envelope(value):
                    try:
                        encoded = json.dumps(
                            value,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                    except (TypeError, ValueError, OverflowError, RecursionError):
                        return True
                    envelope_chars = max(envelope_chars, len(encoded))
            elif isinstance(value, list):
                pending.extend(value)
        return envelope_chars >= len(text) * _ENVELOPE_DOMINANCE
    if _CALL_MARKER_RE.search(text):
        return True
    if unclosed_tool_call_markup_has_control(text):
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
    return envelope_chars >= len(text) * _ENVELOPE_DOMINANCE or _has_dominant_malformed_tool_root(text)


#: Вызов инструмента, записанный как код: `memory_search.search(query="…")`.
#:
#: Замерено на недельном прогоне 2026-08-02: на вопрос «Стоит ли брать 5090 под
#: локальные модели?» человек получил ответом ровно строку
#: `memory_search.search(query="Стоит ли брать 5090 под локальные модели?")`.
#: Распознавались только JSON-конверт и `<tool_call>`, а эта форма — ни то, ни
#: другое, и она уходила человеку как готовый ответ.
#:
#: Требуется полное совпадение со всем текстом: объяснение, В КОТОРОМ упомянут
#: вызов, — законный ответ, и терять его нельзя (ровно эту цену платил прежний
#: детектор конвертов, забраковывавший любой рассказ про JSON).
_CODE_STYLE_CALL_RE = re.compile(
    r"^[a-z_][a-z0-9_]{2,}(?:\.[a-z_][a-z0-9_]*)?"  # имя инструмента, при желании с методом
    r"\(\s*[a-z_][a-z0-9_]*\s*=.*\)$",  # и хотя бы один именованный аргумент
    re.IGNORECASE | re.DOTALL,
)


def looks_like_a_code_style_call(content: str) -> bool:
    """Является ли ответ целиком вызовом инструмента, записанным как код."""
    text = (content or "").strip()
    if not text or "\n" in text.strip().strip("\n"):
        # Многострочный текст — это уже не одно выражение, а рассказ.
        return False
    return bool(_CODE_STYLE_CALL_RE.fullmatch(text))


def _unwrap_full_json_fence(text: str) -> str | None:
    """Return one released unlabelled or exact JSON-owned fence body."""

    if len(text) < len(_CODE_FENCE_PREFIX) * 2 or not text.startswith(_CODE_FENCE_PREFIX):
        return None
    if not text.endswith(_CODE_FENCE_PREFIX):
        return None
    raw_inner = text[len(_CODE_FENCE_PREFIX) : -len(_CODE_FENCE_PREFIX)]
    if raw_inner[:4].casefold() == "json" and (
        len(raw_inner) == 4 or raw_inner[4].isspace() or raw_inner[4] in "[{"
    ):
        inner = raw_inner[4:].strip()
    else:
        inner = raw_inner.strip()
    if not inner.startswith(("{", "[")):
        return None
    return inner


def classify_tool_turn(content: str) -> ToolTurn:
    """Classify a complete assistant response without leaking control payloads."""

    text = (content or "").strip()
    fenced = _unwrap_full_json_fence(text)
    candidate = fenced if fenced is not None else text
    try:
        decoded = _strict_json_loads(candidate)
    except (json.JSONDecodeError, ValueError, RecursionError):
        decoded = None

    if isinstance(decoded, dict):
        calls = normalize_tool_payload(decoded)
        if calls is not None:
            return ToolTurn(kind="tool", calls=calls)
        if is_tool_envelope(decoded):
            return ToolTurn(kind="protocol_error")

    if contains_internal_tool_output(text) or looks_like_a_code_style_call(text):
        return ToolTurn(kind="protocol_error")
    return ToolTurn(kind="answer", text=text)
