"""Narrow chat and tool-call smoke for one staged secondary profile epoch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from endpoint_common import (
    EndpointError,
    chat_completion,
    configure_expected_model,
    evidence_identity,
    load_api_key,
    normalize_base_url,
    request_json,
    verify_remote_profile_epoch,
    write_new_json,
)

_TOOL_NAME = "lookup_temperature"
_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


def _bounded_count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 10_000_000 else 0


def _verify_models(body: dict[str, Any], expected_model: str) -> None:
    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) > 128:
        raise EndpointError("/v1/models has an invalid bounded collection")
    model_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise EndpointError("/v1/models contains an invalid row")
        model_ids.append(row["id"])
    if model_ids.count(expected_model) != 1:
        raise EndpointError("/v1/models does not contain exactly one expected alias")


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


def _verify_tool_call(body: dict[str, Any], expected_model: str) -> tuple[str, int, int, str]:
    if body.get("model") != expected_model:
        raise EndpointError("tool smoke returned the wrong served-model alias")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise EndpointError("tool smoke returned an invalid choice collection")
    choice = choices[0]
    if choice.get("finish_reason") != "tool_calls":
        raise EndpointError("tool smoke did not terminate with a tool call")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise EndpointError("tool smoke has no message object")
    if message.get("content") not in {None, ""}:
        raise EndpointError("tool smoke mixed final content with its tool call")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise EndpointError("tool smoke returned an invalid tool-call collection")
    call = calls[0]
    required_keys = {"id", "type", "function"}
    if set(call) not in (required_keys, required_keys | {"index"}):
        raise EndpointError("tool smoke returned an invalid tool-call envelope")
    if call.get("type") != "function":
        raise EndpointError("tool smoke returned an invalid tool-call type")
    if "index" in call and call["index"] != 0:
        raise EndpointError("tool smoke returned an invalid tool-call index")
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
        raise EndpointError("tool smoke returned an invalid call identifier")
    if (
        not isinstance(function, dict)
        or set(function) != {"name", "arguments"}
        or function.get("name") != _TOOL_NAME
    ):
        raise EndpointError("tool smoke selected an unexpected function")
    arguments_text = function.get("arguments")
    if not isinstance(arguments_text, str) or len(arguments_text.encode("utf-8")) > 4096:
        raise EndpointError("tool smoke returned invalid bounded arguments")
    try:
        arguments = json.loads(arguments_text)
    except json.JSONDecodeError as exc:
        raise EndpointError("tool smoke returned malformed JSON arguments") from exc
    if arguments != {"city": "Moscow"}:
        raise EndpointError("tool smoke arguments did not match the closed schema")
    canonical_arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    usage_value = body.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    return (
        canonical_arguments,
        _bounded_count(usage.get("prompt_tokens")),
        _bounded_count(usage.get("completion_tokens")),
        str(choice["finish_reason"]),
    )


def run_smoke(
    base_url: str,
    api_key: str,
    timeout_sec: float,
    ca_file: Path,
    expected_model: str,
) -> dict[str, Any]:
    normalized = normalize_base_url(base_url)
    models, models_latency = request_json(
        "GET",
        f"{normalized}/models",
        api_key=api_key,
        payload=None,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    _verify_models(models, expected_model)

    try:
        completion = chat_completion(
            normalized,
            api_key=api_key,
            messages=[
                {
                    "role": "system",
                    "content": "Отвечай только финальным каналом; не раскрывай внутренние рассуждения.",
                },
                {"role": "user", "content": "Ответь одним коротким русским предложением: узел готов?"},
            ],
            timeout_sec=timeout_sec,
            max_tokens=96,
            temperature=1.0,
            extra={"reasoning_effort": "low", "top_p": 1.0, "seed": 0},
            ca_file=ca_file,
        )
    except EndpointError as exc:
        raise EndpointError(f"chat phase failed: {exc}") from exc
    if completion.finish_reason != "stop" or not any(
        "а" <= character.casefold() <= "я" or character.casefold() == "ё" for character in completion.content
    ):
        raise EndpointError("bounded Russian chat smoke returned an invalid final response")

    tool_payload = {
        "model": expected_model,
        "messages": [
            {"role": "system", "content": "Use the supplied function when explicitly required."},
            {
                "role": "user",
                "content": "Call lookup_temperature once for Moscow. Do not answer from memory.",
            },
        ],
        "max_tokens": 256,
        "reasoning_effort": "low",
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 0,
        "stream": False,
        "tools": [_tool_spec()],
        "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
    }
    try:
        tool_body, tool_latency = request_json(
            "POST",
            f"{normalized}/chat/completions",
            api_key=api_key,
            payload=tool_payload,
            timeout_sec=timeout_sec,
            ca_file=ca_file,
        )
    except EndpointError as exc:
        raise EndpointError(f"tool phase failed: {exc}") from exc
    tool_arguments, tool_prompt_tokens, tool_completion_tokens, tool_finish_reason = _verify_tool_call(
        tool_body, expected_model
    )
    return {
        "schema": "friday.secondary-candidate-smoke.v1",
        "status": "passed",
        **evidence_identity(),
        "observed_at": datetime.now(UTC).isoformat(),
        "models_latency_ms": round(models_latency * 1000, 3),
        "chat_latency_ms": round(completion.latency_sec * 1000, 3),
        "chat_prompt_tokens": completion.prompt_tokens,
        "chat_completion_tokens": completion.completion_tokens,
        "chat_finish_reason": completion.finish_reason,
        "chat_final_sha256": hashlib.sha256(completion.content.encode("utf-8")).hexdigest(),
        "tool_latency_ms": round(tool_latency * 1000, 3),
        "tool_prompt_tokens": tool_prompt_tokens,
        "tool_completion_tokens": tool_completion_tokens,
        "tool_finish_reason": tool_finish_reason,
        "tool_call_sha256": hashlib.sha256(f"{_TOOL_NAME}\n{tool_arguments}".encode()).hexdigest(),
        "raw_content_retained": False,
        "api_key_retained": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--profile-manifest", required=True, type=Path)
    parser.add_argument("--timeout-sec", default=120.0, type=float)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        expected_model = configure_expected_model(args.profile_manifest, args.ca_file)
        api_key = load_api_key(args.api_key_file)
        verify_remote_profile_epoch(
            args.base_url,
            api_key=api_key,
            timeout_sec=args.timeout_sec,
            ca_file=args.ca_file,
        )
        report = run_smoke(
            args.base_url,
            api_key,
            args.timeout_sec,
            args.ca_file,
            expected_model,
        )
        if args.output:
            write_new_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except EndpointError as exc:
        print(
            json.dumps(
                {
                    "schema": "friday.secondary-candidate-smoke-failure.v1",
                    "status": "failed",
                    "error": str(exc),
                    "raw_content_retained": False,
                    "api_key_retained": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
