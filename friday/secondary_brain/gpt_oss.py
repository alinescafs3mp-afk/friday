"""Protocol projection for the GPT-OSS SGLang advisory endpoint."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from friday.config import detect_repeated_token_degeneration

from .contracts import (
    EffectClass,
    JsonValue,
    ModelModality,
    ModelRequest,
    ModelUsage,
    SecondaryEndpointConfig,
    SecondaryFailure,
    SecondaryResult,
)

_MAX_RESPONSE_BYTES = 1_048_576
_HARMONY_MARKERS = ("<|channel|>", "<|start|>", "<|end|>", "<|message|>")
_REASONING_MARKERS = ("<think>", "</think>")
_IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})


@dataclass(frozen=True, slots=True)
class ProtocolRejection(Exception):
    """Content-free protocol error safe to retain in local control flow."""

    failure: SecondaryFailure

    def __str__(self) -> str:
        return self.failure.value


def _contains_image(value: Any) -> bool:
    if isinstance(value, Mapping):
        kind = str(value.get("type", "")).strip().casefold()
        if kind in _IMAGE_TYPES or "image_url" in value:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_image(item) for item in value)
    return False


def _contains_tool_material(message: Mapping[str, Any]) -> bool:
    role = str(message.get("role", "")).strip().casefold()
    return bool(
        role == "tool"
        or message.get("tool_calls")
        or message.get("tool_call_id")
        or message.get("function_call")
    )


def _message_chars(message: Mapping[str, Any]) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content)
    return len(str(content))


def _bounded_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(number, 0), 2_147_483_647)


def _immutable_json(value: Any) -> JsonValue:
    """Copy only JSON values, rejecting NaN/Inf and exotic objects."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        return value
    if isinstance(value, list):
        return [_immutable_json(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {str(key): _immutable_json(item) for key, item in value.items()}
    raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)


class GptOssProtocolAdapter:
    """Strict adapter: text in, sanitized final content out, no authority."""

    def build_payload(
        self,
        config: SecondaryEndpointConfig,
        request: ModelRequest,
    ) -> dict[str, Any]:
        if request.modality is not ModelModality.TEXT or any(
            _contains_image(message) for message in request.messages
        ):
            raise ProtocolRejection(SecondaryFailure.UNSUPPORTED_MODALITY)
        if request.effect_class not in {EffectClass.NONE, EffectClass.READ_ONLY}:
            raise ProtocolRejection(SecondaryFailure.EFFECT_DENIED)
        if any(_contains_tool_material(message) for message in request.messages):
            raise ProtocolRejection(SecondaryFailure.TOOL_CALL_REJECTED)

        # A conservative, tokenizer-independent ceiling.  The measured endpoint
        # cap remains authoritative; output and a small protocol margin are kept.
        input_tokens_upper_bound = max(1, sum(_message_chars(item) for item in request.messages))
        context_budget = config.max_context_tokens - request.max_output_tokens - 256
        if context_budget < 1 or input_tokens_upper_bound > context_budget:
            raise ProtocolRejection(SecondaryFailure.CONTEXT_EXCEEDED)

        messages: list[dict[str, Any]] = []
        for source in request.messages:
            role = str(source.get("role", "")).strip().casefold()
            content = source.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
            messages.append({"role": role, "content": content})

        # No tools, images, reasoning body or endpoint-selected model ever enter
        # this payload.  SGLang applies the checkpoint's Harmony chat template.
        return {
            "model": config.served_model_alias,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "temperature": 0.0,
            "stream": False,
        }

    def parse_response(
        self,
        config: SecondaryEndpointConfig,
        request: ModelRequest,
        body: Any,
        *,
        latency_sec: float,
    ) -> SecondaryResult:
        if not isinstance(body, dict):
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        if body.get("model") != config.served_model_alias:
            raise ProtocolRejection(SecondaryFailure.WRONG_MODEL)
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        if message.get("tool_calls") or message.get("function_call"):
            raise ProtocolRejection(SecondaryFailure.TOOL_CALL_REJECTED)

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        if len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE)
        folded = content.casefold()
        if any(marker in content for marker in _HARMONY_MARKERS) or any(
            marker in folded for marker in _REASONING_MARKERS
        ):
            raise ProtocolRejection(SecondaryFailure.REASONING_LEAK)
        if detect_repeated_token_degeneration(content):
            raise ProtocolRejection(SecondaryFailure.DEGENERATION)

        reasoning_was_separated = any(
            bool(message.get(field)) for field in ("reasoning_content", "reasoning", "analysis")
        )
        structured: JsonValue = None
        if request.require_structured_output:
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ProtocolRejection(SecondaryFailure.MALFORMED_RESPONSE) from None
            structured = _immutable_json(parsed)

        usage_raw = body.get("usage")
        usage_map = cast(dict[str, Any], usage_raw) if isinstance(usage_raw, dict) else {}
        usage = ModelUsage(
            prompt_tokens=_bounded_nonnegative_int(usage_map.get("prompt_tokens")),
            completion_tokens=_bounded_nonnegative_int(usage_map.get("completion_tokens")),
            total_tokens=_bounded_nonnegative_int(usage_map.get("total_tokens")),
        )
        return SecondaryResult(
            visible_content=content.strip(),
            structured_output=structured,
            served_model_alias=config.served_model_alias,
            usage=usage,
            latency_sec=max(0.0, latency_sec),
            reasoning_was_separated=reasoning_was_separated,
        )
