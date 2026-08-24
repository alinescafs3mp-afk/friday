"""Run the content-free protocol and quality gate for the optional SGLang node."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from endpoint_common import (
    EXPECTED_MODEL,
    EndpointError,
    SanitizedCompletion,
    build_tls_context,
    configure_expected_model,
    configured_profile_context_tokens,
    evidence_identity,
    load_api_key,
    normalize_base_url,
    parse_completion,
    request_json,
    request_text,
    validate_profile_headers,
    verify_remote_profile_epoch,
    write_new_json,
)

_EVIDENCE_KEYS = frozenset(
    {"case", "status", "latency_sec", "prompt_tokens", "completion_tokens", "output_sha256"}
)
_RESERVED_PAYLOAD_KEYS = frozenset(
    {"model", "messages", "max_tokens", "temperature", "top_p", "seed", "stream"}
)
_TOOL_NAME = "lookup_temperature"
_TOOL_USER = "Call lookup_temperature once for Moscow. Do not answer from memory."
_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
LONG_CONTEXT_CASE_NAME = "near_limit_long_context_recall"
_CONTEXT_LADDER = frozenset({4096, 8192, 12288, 16384, 24576, 32768, 40960, 49152, 65536})
_LONG_CONTEXT_MARKER = "FRIDAY-LONG-CONTEXT-7C91E2"
_LONG_CONTEXT_FILLER_RESERVE = 512
_LONG_CONTEXT_ACCEPTANCE_RESERVE = 768
_LONG_CONTEXT_MAX_TOKENS = 128
_LONG_CONTEXT_MAX_PROMPT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class QualityCase:
    name: str
    messages: tuple[dict[str, Any], ...] = field(repr=False)
    validator: Callable[[SanitizedCompletion], bool] = field(repr=False)
    max_tokens: int = 256
    temperature: float = 1.0
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)
    allow_empty_length: bool = False


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    assistant_message: dict[str, Any] = field(repr=False)
    latency_sec: float
    prompt_tokens: int
    completion_tokens: int
    hash_material: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CancellationMetrics:
    aborted_total: float | None
    running: float
    queued: float


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence(
    case: str,
    *,
    passed: bool,
    latency_sec: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    hash_material: str = "",
) -> dict[str, object]:
    return {
        "case": case,
        "status": "passed" if passed else "failed",
        "latency_sec": round(max(0.0, latency_sec), 6),
        "prompt_tokens": max(0, prompt_tokens),
        "completion_tokens": max(0, completion_tokens),
        "output_sha256": _sha256(hash_material) if hash_material else "",
    }


def _failed(case: str) -> dict[str, object]:
    return _evidence(case, passed=False)


def _metric_values(metrics: str, name: str) -> list[float]:
    if len(metrics.encode("utf-8")) > 1_048_576:
        raise EndpointError("metrics observation exceeds the response bound")
    values: list[float] = []
    lines = metrics.splitlines()
    if len(lines) > 20_000:
        raise EndpointError("metrics observation has too many rows")
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if len(line) > 4_096:
            raise EndpointError("metrics observation has an oversized row")
        pieces = line.split()
        if len(pieces) not in {2, 3}:
            continue
        metric_id = pieces[0].split("{", 1)[0]
        if metric_id != name:
            continue
        try:
            value = float(pieces[1])
        except (ValueError, OverflowError) as exc:
            raise EndpointError("cancellation metric is not numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise EndpointError("cancellation metric is outside the valid bound")
        values.append(value)
    return values


def _cancellation_metrics(
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> CancellationMetrics:
    metrics_url = f"{base_url.removesuffix('/v1')}/metrics"
    body, _latency = request_text(
        "GET",
        metrics_url,
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    aborted = _metric_values(body, "sglang:num_aborted_requests_total")
    running = _metric_values(body, "sglang:num_running_reqs")
    queued = _metric_values(body, "sglang:num_queue_reqs")
    if not running or not queued:
        raise EndpointError("required cancellation gauges are absent")
    return CancellationMetrics(
        aborted_total=sum(aborted) if aborted else None,
        running=sum(running),
        queued=sum(queued),
    )


def _exact(expected: str) -> Callable[[SanitizedCompletion], bool]:
    folded = expected.strip().casefold()
    return lambda completion: completion.content.strip().casefold() == folded


def _exact_with_separated_reasoning(expected: str) -> Callable[[SanitizedCompletion], bool]:
    exact = _exact(expected)
    return lambda completion: exact(completion) and completion.reasoning_present


def _json_equals(expected: object) -> Callable[[SanitizedCompletion], bool]:
    def validate(completion: SanitizedCompletion) -> bool:
        try:
            return bool(json.loads(completion.content) == expected)
        except json.JSONDecodeError:
            return False

    return validate


def _summary_is_faithful(completion: SanitizedCompletion) -> bool:
    value = completion.content.casefold()
    numbers = re.findall(r"\d+", value)
    return (
        "север" in value and "17" in value and "24.08.2026" in value and numbers == ["17", "24", "08", "2026"]
    )


def _citation_is_preserved(completion: SanitizedCompletion) -> bool:
    return completion.content.count("[SRC-17]") == 1 and bool(
        re.search(r"(?<!\d)42(?!\d)", completion.content)
    )


def _stop_is_honoured(completion: SanitizedCompletion) -> bool:
    return completion.finish_reason == "stop" and completion.content.strip().casefold() == "alpha"


def _truncation_is_reported(completion: SanitizedCompletion) -> bool:
    return completion.finish_reason == "length" and completion.completion_tokens == 8


def _base_messages(
    user: str, *, system: str = "Return final content only. Never call tools."
) -> tuple[dict[str, Any], ...]:
    return (
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    )


def _near_limit_long_context_case(context_tokens: int) -> QualityCase:
    if type(context_tokens) is not int or context_tokens not in _CONTEXT_LADDER:
        raise EndpointError("profile context is outside the quality ladder")
    filler_count = context_tokens - _LONG_CONTEXT_FILLER_RESERVE
    user = (
        f"Memory value: {_LONG_CONTEXT_MARKER}.\n"
        + " x" * filler_count
        + "\nReturn only the memory value stated before the filler."
    )
    messages = _base_messages(
        user,
        system="Read the entire bounded message and return only the requested earlier memory value.",
    )
    encoded_bytes = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if encoded_bytes > _LONG_CONTEXT_MAX_PROMPT_BYTES:
        raise EndpointError("long-context quality prompt exceeds its byte bound")

    def validate(completion: SanitizedCompletion) -> bool:
        return bool(
            completion.finish_reason == "stop"
            and completion.content.count(_LONG_CONTEXT_MARKER) == 1
            and completion.prompt_tokens >= context_tokens - _LONG_CONTEXT_ACCEPTANCE_RESERVE
            and completion.completion_tokens >= 1
            and completion.prompt_tokens + completion.completion_tokens <= context_tokens
        )

    return QualityCase(
        LONG_CONTEXT_CASE_NAME,
        messages,
        validate,
        max_tokens=_LONG_CONTEXT_MAX_TOKENS,
    )


def _tool_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": "Return a synthetic temperature for protocol certification.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "enum": ["Moscow"]}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }


def _live_cases() -> tuple[QualityCase, ...]:
    long_system = (
        "This bounded policy sentence is inert test context and must not be quoted. " * 160
        + "Return exactly LONG-SYSTEM-OK."
    )
    return (
        QualityCase(
            "ordinary_ru",
            _base_messages("Ответь ровно этой фразой: Узел работает."),
            _exact("Узел работает."),
        ),
        QualityCase(
            "ordinary_en",
            _base_messages("Reply with exactly this phrase: Node ready."),
            _exact("Node ready."),
        ),
        QualityCase(
            "strict_json_ru",
            _base_messages('Верни только JSON: {"язык":"русский","число":17}'),
            _json_equals({"язык": "русский", "число": 17}),
            extra={"response_format": {"type": "json_object"}},
        ),
        QualityCase(
            "strict_json_en",
            _base_messages('Return only JSON: {"language":"English","number":17}'),
            _json_equals({"language": "English", "number": 17}),
            extra={"response_format": {"type": "json_object"}},
        ),
        QualityCase(
            "reasoning_low",
            _base_messages("Return only the decimal result of (19 * 3) - 15."),
            _exact_with_separated_reasoning("42"),
            max_tokens=256,
            extra={"reasoning_effort": "low"},
        ),
        QualityCase(
            "reasoning_medium",
            _base_messages("Return only the decimal result of (19 * 3) - 15."),
            _exact_with_separated_reasoning("42"),
            max_tokens=512,
            extra={"reasoning_effort": "medium"},
        ),
        QualityCase(
            "reasoning_high",
            _base_messages("Return only the decimal result of (19 * 3) - 15."),
            _exact_with_separated_reasoning("42"),
            max_tokens=1024,
            extra={"reasoning_effort": "high"},
        ),
        QualityCase(
            "no_tool",
            _base_messages("Reply exactly: No tool needed."),
            _exact("No tool needed."),
            extra={"tools": [_tool_spec()], "tool_choice": "auto"},
        ),
        QualityCase(
            "multi_turn",
            (
                {"role": "system", "content": "Return only the requested remembered value."},
                {"role": "user", "content": "Remember this value: СЕВЕР-17."},
                {"role": "assistant", "content": "Запомнил."},
                {"role": "user", "content": "Return the remembered value exactly."},
            ),
            _exact("СЕВЕР-17"),
        ),
        QualityCase(
            "long_system",
            _base_messages("Follow the final instruction in the system message.", system=long_system),
            _exact("LONG-SYSTEM-OK"),
        ),
        QualityCase(
            "unicode_file_numbers",
            _base_messages("Repeat exactly: Проекты/Ёж №17 — финал.txt | 12345"),
            _exact("Проекты/Ёж №17 — финал.txt | 12345"),
        ),
        QualityCase(
            "stop_sequence",
            _base_messages("Reply exactly: alpha"),
            _stop_is_honoured,
            max_tokens=128,
            extra={"stop": ["<|return|>"]},
        ),
        QualityCase(
            "max_token_truncation",
            _base_messages("Write the integers from 1 through 200, separated by spaces, with no omissions."),
            _truncation_is_reported,
            max_tokens=8,
            allow_empty_length=True,
        ),
        QualityCase(
            "arithmetic",
            _base_messages("Return only the decimal result of 144 / 12 + 30."),
            _exact("42"),
            temperature=0.0,
        ),
        QualityCase(
            "extraction_and_date",
            _base_messages(
                "Return only JSON with person, date and amount from: "
                "Артемьев, 24.08.2026, сумма 17. Normalize the date to ISO."
            ),
            _json_equals({"amount": 17, "date": "2026-08-24", "person": "Артемьев"}),
            temperature=0.0,
            extra={"response_format": {"type": "json_object"}},
        ),
        QualityCase(
            "ru_summary_faithfulness",
            _base_messages(
                "Суммируй одним русским предложением, сохрани числа без изменений: "
                "проект «Север»; бюджет 17 рублей; срок 24.08.2026."
            ),
            _summary_is_faithful,
            temperature=0.0,
        ),
        QualityCase(
            "contradiction",
            _base_messages(
                "Statements A>B and A<B describe the same A and B at the same time. "
                "Return exactly CONTRADICTION if they conflict, otherwise CONSISTENT."
            ),
            _exact("CONTRADICTION"),
            temperature=0.0,
        ),
        QualityCase(
            "citation_preservation",
            _base_messages(
                "Restate this fact in one sentence and preserve its citation exactly once: "
                "The measured height is 42 m [SRC-17]."
            ),
            _citation_is_preserved,
            temperature=0.0,
        ),
        QualityCase(
            "wrong_language_guard",
            _base_messages("Ответь ровно одним русским словом: подтверждено"),
            _exact("подтверждено"),
            temperature=0.0,
        ),
    )


def _completion_request(
    *,
    base_url: str,
    api_key: str,
    messages: tuple[dict[str, Any], ...],
    timeout_sec: float,
    max_tokens: int,
    extra: Mapping[str, Any],
    ca_file: Path,
    temperature: float = 1.0,
    allow_empty_length: bool = False,
) -> SanitizedCompletion:
    if not 1 <= max_tokens <= 4096:
        raise EndpointError("quality case max_tokens is outside the certification bound")
    if _RESERVED_PAYLOAD_KEYS.intersection(extra):
        raise EndpointError("quality case attempted to override a reserved payload field")
    if type(temperature) is not float or temperature not in {0.0, 1.0}:
        raise EndpointError("quality case temperature is outside the certified set")
    payload: dict[str, Any] = {
        "model": EXPECTED_MODEL,
        "messages": list(messages),
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
        "temperature": temperature,
        "top_p": 1.0,
        "seed": 0,
        "stream": False,
        **dict(extra),
    }
    body, latency = request_json(
        "POST",
        f"{base_url}/chat/completions",
        api_key=api_key,
        payload=payload,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    choices = body.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and (message.get("tool_calls") or message.get("function_call")):
            raise EndpointError("non-tool quality case unexpectedly returned a tool call")
    try:
        return parse_completion(body, expected_model=EXPECTED_MODEL, latency_sec=latency)
    except EndpointError:
        if not allow_empty_length:
            raise
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    message = choice.get("message") if isinstance(choice, dict) else None
    usage = body.get("usage")
    completion_tokens = _bounded_token_count(usage.get("completion_tokens")) if isinstance(usage, dict) else 0
    content = message.get("content") if isinstance(message, dict) else None
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    if (
        body.get("model") != EXPECTED_MODEL
        or not isinstance(choice, dict)
        or choice.get("finish_reason") != "length"
        or not isinstance(message, dict)
        or not (content is None or content == "")
        or message.get("tool_calls")
        or message.get("function_call")
        or completion_tokens != max_tokens
        or not math.isfinite(latency)
        or latency < 0
    ):
        raise EndpointError("intentional truncation response is invalid") from None
    return SanitizedCompletion(
        content="",
        latency_sec=latency,
        prompt_tokens=(_bounded_token_count(usage.get("prompt_tokens")) if isinstance(usage, dict) else 0),
        completion_tokens=completion_tokens,
        finish_reason="length",
        reasoning_present=isinstance(reasoning, str) and bool(reasoning),
    )


def _run_live_case(
    case: QualityCase,
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> dict[str, object]:
    try:
        completion = _completion_request(
            base_url=base_url,
            api_key=api_key,
            messages=case.messages,
            timeout_sec=timeout_sec,
            max_tokens=case.max_tokens,
            extra=case.extra,
            ca_file=ca_file,
            temperature=case.temperature,
            allow_empty_length=case.allow_empty_length,
        )
        try:
            passed = bool(case.validator(completion))
        except Exception:
            passed = False
        return _evidence(
            case.name,
            passed=passed,
            latency_sec=completion.latency_sec,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            hash_material=(
                f"intentional-length:{completion.completion_tokens}"
                if case.name == "max_token_truncation"
                else completion.content
            ),
        )
    except EndpointError:
        return _failed(case.name)


def _bounded_token_count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 10_000_000 else 0


def _tool_call_request(
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> ToolCallObservation:
    payload = {
        "model": EXPECTED_MODEL,
        "messages": list(
            _base_messages(_TOOL_USER, system="Use the supplied function when explicitly required.")
        ),
        "max_tokens": 256,
        "reasoning_effort": "low",
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 0,
        "stream": False,
        "tools": [_tool_spec()],
        "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
    }
    body, latency = request_json(
        "POST",
        f"{base_url}/chat/completions",
        api_key=api_key,
        payload=payload,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    if body.get("model") != EXPECTED_MODEL:
        raise EndpointError("tool probe returned the wrong served-model alias")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise EndpointError("tool probe returned an invalid choice collection")
    choice = choices[0]
    if choice.get("finish_reason") != "tool_calls":
        raise EndpointError("tool probe did not terminate with a tool call")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise EndpointError("tool probe has no message object")
    tool_content = message.get("content")
    if tool_content is not None and tool_content != "":
        raise EndpointError("tool probe mixed final content with its tool call")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise EndpointError("tool probe returned an invalid tool-call collection")
    call = calls[0]
    call_keys = set(call)
    required_call_keys = {"id", "type", "function"}
    if call_keys not in (required_call_keys, required_call_keys | {"index"}):
        raise EndpointError("tool probe returned an invalid tool-call envelope")
    if "index" in call and (type(call["index"]) is not int or call["index"] != 0):
        raise EndpointError("tool probe returned an invalid tool-call index")
    if call.get("type") != "function":
        raise EndpointError("tool probe returned an invalid tool-call envelope")
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id) or not isinstance(function, dict):
        raise EndpointError("tool probe returned an invalid call identifier or function")
    if set(function) != {"name", "arguments"} or function.get("name") != _TOOL_NAME:
        raise EndpointError("tool probe selected an unexpected function")
    arguments_text = function.get("arguments")
    if not isinstance(arguments_text, str) or len(arguments_text.encode("utf-8")) > 4096:
        raise EndpointError("tool probe returned invalid bounded arguments")
    try:
        arguments = json.loads(arguments_text)
    except json.JSONDecodeError as exc:
        raise EndpointError("tool probe returned malformed JSON arguments") from exc
    if arguments != {"city": "Moscow"}:
        raise EndpointError("tool probe arguments did not match the closed schema")
    canonical_arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    usage_value = body.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    assistant_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": _TOOL_NAME, "arguments": canonical_arguments},
            }
        ],
    }
    return ToolCallObservation(
        assistant_message=assistant_message,
        latency_sec=latency,
        prompt_tokens=_bounded_token_count(usage.get("prompt_tokens")),
        completion_tokens=_bounded_token_count(usage.get("completion_tokens")),
        hash_material=f"{_TOOL_NAME}\n{canonical_arguments}",
    )


def _run_tool_protocol(
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> list[dict[str, object]]:
    try:
        observation = _tool_call_request(
            base_url=base_url,
            api_key=api_key,
            timeout_sec=timeout_sec,
            ca_file=ca_file,
        )
    except EndpointError:
        return [_failed("tool_call_shape"), _failed("tool_result_continuation")]

    rows = [
        _evidence(
            "tool_call_shape",
            passed=True,
            latency_sec=observation.latency_sec,
            prompt_tokens=observation.prompt_tokens,
            completion_tokens=observation.completion_tokens,
            hash_material=observation.hash_material,
        )
    ]
    call_id = str(observation.assistant_message["tool_calls"][0]["id"])
    continuation_messages: tuple[dict[str, Any], ...] = (
        {"role": "system", "content": "Use the synthetic tool result and return only its integer."},
        {"role": "user", "content": _TOOL_USER},
        observation.assistant_message,
        {"role": "tool", "tool_call_id": call_id, "content": '{"temperature_c":17}'},
    )
    try:
        completion = _completion_request(
            base_url=base_url,
            api_key=api_key,
            messages=continuation_messages,
            timeout_sec=timeout_sec,
            max_tokens=256,
            extra={"tools": [_tool_spec()], "tool_choice": "none"},
            ca_file=ca_file,
        )
        rows.append(
            _evidence(
                "tool_result_continuation",
                passed=completion.content.strip() == "17",
                latency_sec=completion.latency_sec,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                hash_material=completion.content,
            )
        )
    except EndpointError:
        rows.append(_failed("tool_result_continuation"))
    return rows


def _run_disconnect_protocol(
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> list[dict[str, object]]:
    """Cancel a forced-long stream, then require fast single-slot recovery."""

    connection: http.client.HTTPSConnection | None = None
    try:
        baseline = _completion_request(
            base_url=base_url,
            api_key=api_key,
            messages=_base_messages("Reply with exactly: ready"),
            timeout_sec=timeout_sec,
            max_tokens=256,
            extra={},
            ca_file=ca_file,
        )
        if baseline.finish_reason != "stop" or baseline.content.strip().casefold() != "ready":
            raise EndpointError("disconnect baseline canary failed")
        recovery_budget = min(timeout_sec, max(3.0, min(8.0, baseline.latency_sec * 2.5 + 1.0)))
        before = _cancellation_metrics(
            base_url=base_url,
            api_key=api_key,
            timeout_sec=timeout_sec,
            ca_file=ca_file,
        )
        if before.running != 0 or before.queued != 0:
            raise EndpointError("disconnect probe requires a quiescent single-request node")

        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise EndpointError("disconnect probe requires an HTTPS origin")
        context = build_tls_context(base_url, ca_file)
        if context is None:
            raise EndpointError("disconnect probe requires TLS")
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout_sec,
            context=context,
        )
        payload = json.dumps(
            {
                "model": EXPECTED_MODEL,
                "messages": list(
                    _base_messages(
                        "Write the integers from 1 through 5000, separated by spaces, with no omissions."
                    )
                ),
                # The accepted native runtime makes natural completion far
                # slower than the bounded post-disconnect recovery canary.
                "max_tokens": 2_048,
                "min_tokens": 2_048,
                "ignore_eos": True,
                "rid": f"friday-quality-{secrets.token_hex(16)}",
                "reasoning_effort": "low",
                "temperature": 1.0,
                "top_p": 1.0,
                "seed": 0,
                "stream": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection.request(
            "POST",
            f"{parsed.path.rstrip('/')}/chat/completions",
            body=payload,
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise EndpointError("disconnect probe was rejected")
        validate_profile_headers(response.headers)
        observed_event = False
        observed_bytes = 0
        while observed_bytes <= 1_048_576:
            line = response.readline(65_537)
            if not line:
                break
            observed_bytes += len(line)
            stripped = line.lstrip()
            if not stripped.startswith(b"data:"):
                continue
            event_text = stripped[5:].strip()
            if event_text == b"[DONE]":
                raise EndpointError("forced-long stream completed before cancellation")
            try:
                event = json.loads(event_text)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EndpointError("disconnect probe received malformed SSE JSON") from exc
            choices = event.get("choices") if isinstance(event, dict) else None
            if (
                not isinstance(event, dict)
                or event.get("model") != EXPECTED_MODEL
                or not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get("delta"), dict)
            ):
                raise EndpointError("disconnect probe received an invalid completion event")
            observed_event = True
            break
        if not observed_event:
            raise EndpointError("disconnect probe observed no valid stream event")

        active_deadline = time.monotonic() + min(timeout_sec, 8.0)
        active_observed = False
        while time.monotonic() < active_deadline:
            active = _cancellation_metrics(
                base_url=base_url,
                api_key=api_key,
                timeout_sec=min(timeout_sec, 3.0),
                ca_file=ca_file,
            )
            if active.running >= 1:
                active_observed = True
                break
            time.sleep(0.2)
        if not active_observed:
            raise EndpointError("disconnect probe never observed a running request")
    except Exception:
        return [_failed("stream_cancellation"), _failed("client_disconnect_recovery")]
    finally:
        if connection is not None:
            connection.close()

    cancelled_at = time.monotonic()
    drain_deadline = cancelled_at + min(timeout_sec, 8.0)
    drain_observed = False
    try:
        while time.monotonic() < drain_deadline:
            observed = _cancellation_metrics(
                base_url=base_url,
                api_key=api_key,
                timeout_sec=min(timeout_sec, 3.0),
                ca_file=ca_file,
            )
            if observed.running == 0 and observed.queued == 0:
                drain_observed = True
                break
            time.sleep(0.2)
    except EndpointError:
        drain_observed = False
    abort_latency = time.monotonic() - cancelled_at
    if not drain_observed:
        return [_failed("stream_cancellation"), _failed("client_disconnect_recovery")]

    try:
        recovered = _completion_request(
            base_url=base_url,
            api_key=api_key,
            messages=_base_messages("Reply with exactly: ready"),
            timeout_sec=recovery_budget,
            max_tokens=256,
            extra={},
            ca_file=ca_file,
        )
        recovered_ok = (
            recovered.finish_reason == "stop"
            and recovered.content.strip().casefold() == "ready"
            and recovered.latency_sec <= recovery_budget
        )
        recovery_row = _evidence(
            "client_disconnect_recovery",
            passed=recovered_ok,
            latency_sec=recovered.latency_sec,
            prompt_tokens=recovered.prompt_tokens,
            completion_tokens=recovered.completion_tokens,
            hash_material=recovered.content,
        )
    except EndpointError:
        recovery_row = _failed("client_disconnect_recovery")
    return [
        _evidence(
            "stream_cancellation",
            passed=True,
            latency_sec=abort_latency,
        ),
        recovery_row,
    ]


def _inventory_case(
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
) -> tuple[dict[str, object], bool]:
    try:
        body, latency = request_json(
            "GET",
            f"{base_url}/models",
            api_key=api_key,
            payload=None,
            timeout_sec=timeout_sec,
            ca_file=ca_file,
        )
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) > 128:
            raise EndpointError("model inventory has an invalid bounded collection")
        identifiers: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise EndpointError("model inventory contains an invalid row")
            identifiers.append(row["id"])
        passed = identifiers == [EXPECTED_MODEL]
        return (
            _evidence(
                "exact_model_alias",
                passed=passed,
                latency_sec=latency,
                hash_material=EXPECTED_MODEL if passed else "",
            ),
            passed,
        )
    except EndpointError:
        return _failed("exact_model_alias"), False


def _protocol_rejection_cases() -> tuple[tuple[str, str], ...]:
    return (
        ("reject_empty", " "),
        ("reject_nan", "value = NaN"),
        ("reject_degeneration", "repeat " * 24),
        ("reject_harmony", "<|analysis|>private<|end|>"),
    )


def _protocol_rejection_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, content in _protocol_rejection_cases():
        body = {
            "model": EXPECTED_MODEL,
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        try:
            parse_completion(body, expected_model=EXPECTED_MODEL, latency_sec=0.0)
        except EndpointError:
            passed = True
        else:
            passed = False
        rows.append(_evidence(name, passed=passed, hash_material=content))
    return rows


def run_battery(
    *,
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
    context_tokens: int | None = None,
) -> dict[str, object]:
    normalized = normalize_base_url(base_url)
    if urlsplit(normalized).scheme != "https":
        raise EndpointError("quality certification requires the private-CA HTTPS gateway")
    inventory, alias_ok = _inventory_case(
        base_url=normalized,
        api_key=api_key,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    rows = [inventory]
    cases = _live_cases()
    long_context_case = _near_limit_long_context_case(context_tokens) if context_tokens is not None else None
    if alias_ok:
        rows.extend(
            _run_live_case(
                case,
                base_url=normalized,
                api_key=api_key,
                timeout_sec=timeout_sec,
                ca_file=ca_file,
            )
            for case in cases
        )
        if long_context_case is not None:
            rows.append(
                _run_live_case(
                    long_context_case,
                    base_url=normalized,
                    api_key=api_key,
                    timeout_sec=timeout_sec,
                    ca_file=ca_file,
                )
            )
        rows.extend(
            _run_tool_protocol(
                base_url=normalized,
                api_key=api_key,
                timeout_sec=timeout_sec,
                ca_file=ca_file,
            )
        )
        rows.extend(
            _run_disconnect_protocol(
                base_url=normalized,
                api_key=api_key,
                timeout_sec=timeout_sec,
                ca_file=ca_file,
            )
        )
    else:
        rows.extend(_failed(case.name) for case in cases)
        if long_context_case is not None:
            rows.append(_failed(long_context_case.name))
        rows.extend((_failed("tool_call_shape"), _failed("tool_result_continuation")))
        rows.extend((_failed("stream_cancellation"), _failed("client_disconnect_recovery")))
    rows.extend(_protocol_rejection_rows())
    return {
        "schema": "friday.secondary-quality-battery.v1",
        "status": "passed" if all(row["status"] == "passed" for row in rows) else "failed",
        **evidence_identity(),
        "cases": rows,
        "raw_content_retained": False,
        "api_key_retained": False,
    }


def _timeout(value: str) -> float:
    parsed = float(value)
    if not 1.0 <= parsed <= 300.0:
        raise argparse.ArgumentTypeError("timeout must be 1..300 seconds")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--profile-manifest", required=True, type=Path)
    parser.add_argument("--timeout-sec", default=60.0, type=_timeout)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        global EXPECTED_MODEL
        EXPECTED_MODEL = configure_expected_model(args.profile_manifest, args.ca_file)
        api_key = load_api_key(args.api_key_file)
        verify_remote_profile_epoch(
            args.base_url,
            api_key=api_key,
            timeout_sec=args.timeout_sec,
            ca_file=args.ca_file,
        )
        report = run_battery(
            base_url=args.base_url,
            api_key=api_key,
            timeout_sec=args.timeout_sec,
            ca_file=args.ca_file,
            context_tokens=configured_profile_context_tokens(),
        )
        write_new_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "passed" else 2
    except Exception:
        # Never interpolate exception text: transport errors can carry URLs or
        # response bodies, and credential paths are operator-private too.
        print("quality battery failed: closed_error", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
