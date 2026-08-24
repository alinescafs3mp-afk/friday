"""Run the content-free protocol and quality gate for the optional SGLang node."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import ssl
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
    atomic_write_json,
    load_api_key,
    normalize_base_url,
    parse_completion,
    request_json,
)

_EVIDENCE_KEYS = frozenset(
    {"case", "status", "latency_sec", "prompt_tokens", "completion_tokens", "output_sha256"}
)
_RESERVED_PAYLOAD_KEYS = frozenset({"model", "messages", "max_tokens", "temperature", "stream"})
_TOOL_NAME = "lookup_temperature"
_TOOL_USER = "Call lookup_temperature once for Moscow. Do not answer from memory."
_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


@dataclass(frozen=True, slots=True)
class QualityCase:
    name: str
    messages: tuple[dict[str, Any], ...] = field(repr=False)
    validator: Callable[[SanitizedCompletion], bool] = field(repr=False)
    max_tokens: int = 128
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    assistant_message: dict[str, Any] = field(repr=False)
    latency_sec: float
    prompt_tokens: int
    completion_tokens: int
    hash_material: str = field(repr=False)


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


def _exact(expected: str) -> Callable[[SanitizedCompletion], bool]:
    folded = expected.strip().casefold()
    return lambda completion: completion.content.strip().casefold() == folded


def _json_equals(expected: object) -> Callable[[SanitizedCompletion], bool]:
    def validate(completion: SanitizedCompletion) -> bool:
        try:
            return json.loads(completion.content) == expected
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
    return (
        completion.finish_reason == "length"
        and 1 <= completion.completion_tokens <= 8
        and bool(completion.content.strip())
    )


def _base_messages(
    user: str, *, system: str = "Return final content only. Never call tools."
) -> tuple[dict[str, Any], ...]:
    return (
        {"role": "system", "content": system},
        {"role": "user", "content": user},
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
            _exact("42"),
            extra={"reasoning_effort": "low"},
        ),
        QualityCase(
            "reasoning_medium",
            _base_messages("Return only the decimal result of (19 * 3) - 15."),
            _exact("42"),
            extra={"reasoning_effort": "medium"},
        ),
        QualityCase(
            "reasoning_high",
            _base_messages("Return only the decimal result of (19 * 3) - 15."),
            _exact("42"),
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
            _base_messages("Return exactly: alpha<STOP>omega"),
            _stop_is_honoured,
            max_tokens=32,
            extra={"stop": ["<STOP>"]},
        ),
        QualityCase(
            "max_token_truncation",
            _base_messages("Write the integers from 1 through 200, separated by spaces, with no omissions."),
            _truncation_is_reported,
            max_tokens=8,
        ),
        QualityCase(
            "arithmetic",
            _base_messages("Return only the decimal result of 144 / 12 + 30."),
            _exact("42"),
        ),
        QualityCase(
            "extraction_and_date",
            _base_messages(
                "Return only JSON with person, date and amount from: "
                "Артемьев, 24.08.2026, сумма 17. Normalize the date to ISO."
            ),
            _json_equals({"amount": 17, "date": "2026-08-24", "person": "Артемьев"}),
            extra={"response_format": {"type": "json_object"}},
        ),
        QualityCase(
            "ru_summary_faithfulness",
            _base_messages(
                "Суммируй одним русским предложением, сохрани числа без изменений: "
                "проект «Север»; бюджет 17 рублей; срок 24.08.2026."
            ),
            _summary_is_faithful,
        ),
        QualityCase(
            "contradiction",
            _base_messages(
                "Statements A>B and A<B describe the same A and B at the same time. "
                "Return exactly CONTRADICTION if they conflict, otherwise CONSISTENT."
            ),
            _exact("CONTRADICTION"),
        ),
        QualityCase(
            "citation_preservation",
            _base_messages(
                "Restate this fact in one sentence and preserve its citation exactly once: "
                "The measured height is 42 m [SRC-17]."
            ),
            _citation_is_preserved,
        ),
        QualityCase(
            "wrong_language_guard",
            _base_messages("Ответь ровно одним русским словом: подтверждено"),
            _exact("подтверждено"),
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
) -> SanitizedCompletion:
    if not 1 <= max_tokens <= 4096:
        raise EndpointError("quality case max_tokens is outside the certification bound")
    if _RESERVED_PAYLOAD_KEYS.intersection(extra):
        raise EndpointError("quality case attempted to override a reserved payload field")
    payload: dict[str, Any] = {
        "model": EXPECTED_MODEL,
        "messages": list(messages),
        "max_tokens": max_tokens,
        "temperature": 0.0,
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
    return parse_completion(body, expected_model=EXPECTED_MODEL, latency_sec=latency)


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
            hash_material=completion.content,
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
        "max_tokens": 128,
        "temperature": 0.0,
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
    if set(call) != {"id", "type", "function"} or call.get("type") != "function":
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
            max_tokens=32,
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
    """Close one live stream after its first event, then prove clean recovery."""

    connection: http.client.HTTPSConnection | None = None
    started = time.monotonic()
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise EndpointError("disconnect probe requires an HTTPS origin")
        context = ssl.create_default_context(cafile=str(ca_file))
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
                        "Write the integers from 1 through 500, separated by spaces, with no omissions."
                    )
                ),
                "max_tokens": 512,
                "temperature": 0.0,
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
        observed_event = False
        observed_bytes = 0
        while observed_bytes <= 1_048_576:
            line = response.readline(65_537)
            if not line:
                break
            observed_bytes += len(line)
            if line.lstrip().startswith(b"data:") and b"[DONE]" not in line:
                observed_event = True
                break
        if not observed_event:
            raise EndpointError("disconnect probe observed no stream event")
    except Exception:
        return [_failed("stream_cancellation"), _failed("client_disconnect_recovery")]
    finally:
        if connection is not None:
            connection.close()

    cancelled_latency = time.monotonic() - started
    time.sleep(min(0.25, timeout_sec / 10.0))
    try:
        recovered = _completion_request(
            base_url=base_url,
            api_key=api_key,
            messages=_base_messages("Reply with exactly: ready"),
            timeout_sec=timeout_sec,
            max_tokens=16,
            extra={},
            ca_file=ca_file,
        )
        recovered_ok = recovered.finish_reason == "stop" and recovered.content.strip().casefold() == "ready"
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
            latency_sec=cancelled_latency,
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
        rows.extend((_failed("tool_call_shape"), _failed("tool_result_continuation")))
        rows.extend((_failed("stream_cancellation"), _failed("client_disconnect_recovery")))
    rows.extend(_protocol_rejection_rows())
    return {
        "status": "passed" if all(row["status"] == "passed" for row in rows) else "failed",
        "cases": rows,
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
    parser.add_argument("--timeout-sec", default=60.0, type=_timeout)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_battery(
            base_url=args.base_url,
            api_key=load_api_key(args.api_key_file),
            timeout_sec=args.timeout_sec,
            ca_file=args.ca_file,
        )
        atomic_write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "passed" else 2
    except Exception:
        # Never interpolate exception text: transport errors can carry URLs or
        # response bodies, and credential paths are operator-private too.
        print("quality battery failed: closed_error", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
