"""One narrow secondary-model seam for structured Inbox advice."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from friday.secondary_brain import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryBrainScheduler,
    SecondaryMode,
    SecondaryResult,
)
from friday.secondary_brain.contracts import SECONDARY_CONTEXT_TOKEN_RESERVE

_ALLOWED_KINDS = frozenset(
    {
        "note",
        "fact",
        "decision",
        "preference",
        "task",
        "event",
        "project",
        "procedure",
        "contact",
        "reference",
        "idea",
        "technical_note",
        "document",
    }
)
_ALLOWED_ACTIONS = frozenset({"promote", "review", "transient"})
_ALLOWED_ENTITY_TYPES = frozenset(
    {"person", "project", "concept", "event", "organization", "location", "document", "other"}
)
_EXPECTED_KEYS = frozenset(
    {
        "title",
        "summary",
        "knowledge_kind",
        "importance",
        "tags",
        "entities",
        "recommended_action",
        "confidence",
        "rationale",
    }
)


@dataclass(frozen=True, slots=True)
class RoutedInboxAdvice:
    response: dict[str, Any] = field(repr=False)
    model_name: str
    source: str
    diagnostics_before: dict[str, object] | None = field(default=None, repr=False)
    diagnostics_after: dict[str, object] | None = field(default=None, repr=False)


_SECONDARY_CLIP_MARKER = "\n[...bounded secondary input...]\n"
_MIN_SECONDARY_USER_BYTES = 128


def _clip_utf8_middle(value: str, maximum_bytes: int) -> str:
    """Keep deterministic head/tail evidence without splitting UTF-8."""

    raw = value.encode("utf-8", errors="strict")
    if len(raw) <= maximum_bytes:
        return value
    marker = _SECONDARY_CLIP_MARKER.encode("ascii")
    if maximum_bytes <= len(marker):
        return raw[:maximum_bytes].decode("utf-8", errors="ignore")
    body_bytes = maximum_bytes - len(marker)
    # The Inbox carrier puts its deterministic baseline first and private source
    # last. Preserve a bounded baseline prefix while giving most room to source.
    head_bytes = body_bytes // 3
    tail_bytes = body_bytes - head_bytes
    head = raw[:head_bytes].decode("utf-8", errors="ignore")
    tail = raw[-tail_bytes:].decode("utf-8", errors="ignore")
    return f"{head}{_SECONDARY_CLIP_MARKER}{tail}"


def _fit_secondary_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    maximum_input_bytes: int,
) -> tuple[Mapping[str, Any], ...] | None:
    """Fit the one private Inbox carrier to the adapter's conservative 4K gate."""

    if maximum_input_bytes < 1:
        return None
    projected: list[dict[str, Any]] = []
    sizes: list[int] = []
    try:
        for source in messages:
            content = source.get("content")
            if not isinstance(content, str):
                return None
            copied = dict(source)
            projected.append(copied)
            sizes.append(len(content.encode("utf-8", errors="strict")))
    except (UnicodeError, ValueError):
        return None
    if sum(sizes) <= maximum_input_bytes:
        return tuple(projected)

    # Product advice has one terminal user carrier. Never clip system policy or
    # silently discard an earlier conversational message to make private data fit.
    mutable_index = len(projected) - 1
    if mutable_index < 0 or str(projected[mutable_index].get("role") or "").casefold() != "user":
        return None
    fixed_bytes = sum(sizes[:mutable_index])
    available = maximum_input_bytes - fixed_bytes
    if available < _MIN_SECONDARY_USER_BYTES:
        return None
    try:
        projected[mutable_index]["content"] = _clip_utf8_middle(
            str(projected[mutable_index]["content"]),
            available,
        )
    except UnicodeError:
        return None
    fitted_bytes = sum(
        len(str(message.get("content") or "").encode("utf-8", errors="strict")) for message in projected
    )
    return tuple(projected) if fitted_bytes <= maximum_input_bytes else None


def _bounded_score(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(normalized) and 0.0 <= normalized <= 1.0


def valid_inbox_advice_shape(value: object) -> bool:
    """Validate the secondary schema before its output may influence storage."""

    if not isinstance(value, Mapping) or set(value) != _EXPECTED_KEYS:
        return False
    if not isinstance(value.get("title"), str) or len(value["title"]) > 200:
        return False
    if not isinstance(value.get("summary"), str) or len(value["summary"]) > 2_000:
        return False
    if value.get("knowledge_kind") not in _ALLOWED_KINDS:
        return False
    if value.get("recommended_action") not in _ALLOWED_ACTIONS:
        return False
    if not _bounded_score(value.get("importance")) or not _bounded_score(value.get("confidence")):
        return False
    if not isinstance(value.get("rationale"), str) or len(value["rationale"]) > 600:
        return False

    tags = value.get("tags")
    if not isinstance(tags, list) or len(tags) > 16:
        return False
    if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 48 for tag in tags):
        return False

    entities = value.get("entities")
    if not isinstance(entities, list) or len(entities) > 20:
        return False
    for entity in entities:
        if not isinstance(entity, Mapping):
            return False
        if set(entity) != {"name", "entity_type", "confidence", "evidence"}:
            return False
        if not isinstance(entity.get("name"), str) or not str(entity["name"]).strip():
            return False
        if len(str(entity["name"])) > 100 or entity.get("entity_type") not in _ALLOWED_ENTITY_TYPES:
            return False
        if not _bounded_score(entity.get("confidence")):
            return False
        if not isinstance(entity.get("evidence"), str) or len(str(entity["evidence"])) > 240:
            return False
    return True


def _secondary_response(result: SecondaryResult) -> dict[str, Any]:
    return {
        # Round-trip only the validated typed object. Raw response padding or
        # alternative serialization must never bypass the downstream 64 KiB gate.
        "content": json.dumps(
            result.structured_output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
        },
    }


async def route_inbox_advice(
    *,
    secondary: SecondaryBrainScheduler | None,
    messages: Sequence[Mapping[str, Any]],
    max_output_tokens: int,
    primary_model_name: str,
    primary_call: Callable[[], Awaitable[dict[str, Any]]],
    image_bearing: bool,
    observe_diagnostics: bool = False,
) -> RoutedInboxAdvice:
    """Route one advisory extraction without wrapping or replacing the primary."""

    if secondary is None:
        return RoutedInboxAdvice(await primary_call(), primary_model_name, "primary")

    secondary_messages: tuple[Mapping[str, Any], ...] = tuple(messages)
    secondary_max_output_tokens = max(64, int(max_output_tokens))
    profile_limits = secondary.advisory_profile_limits
    if profile_limits is not None:
        context_tokens, profile_max_output_tokens = profile_limits
        secondary_max_output_tokens = min(
            secondary_max_output_tokens,
            profile_max_output_tokens,
        )
        maximum_input_bytes = context_tokens - secondary_max_output_tokens - SECONDARY_CONTEXT_TOKEN_RESERVE
        fitted = _fit_secondary_messages(
            messages,
            maximum_input_bytes=maximum_input_bytes,
        )
        if fitted is None:
            return RoutedInboxAdvice(await primary_call(), primary_model_name, "primary")
        secondary_messages = fitted

    def request_factory() -> ModelRequest:
        return ModelRequest(
            workload=ModelWorkload.EXTRACT,
            messages=secondary_messages,
            max_output_tokens=secondary_max_output_tokens,
            absolute_deadline_monotonic=secondary.new_advisory_deadline(),
            priority=ModelPriority.BACKGROUND,
            effect_class=EffectClass.NONE,
            modality=ModelModality.IMAGE if image_bearing else ModelModality.TEXT,
            require_structured_output=True,
            require_independent_model=True,
            # Inbox input is always tenant-private. Keep this classification
            # code-owned so neither assist nor shadow can accidentally relabel it.
            contains_private_text=True,
        )

    def validator(result: SecondaryResult) -> bool:
        return valid_inbox_advice_shape(result.structured_output)

    diagnostics_before: dict[str, object] | None = None
    diagnostics_after: dict[str, object] | None = None
    if secondary.mode is SecondaryMode.SHADOW:
        if observe_diagnostics:
            primary_response, diagnostics_before, diagnostics_after = await secondary.run_shadow_observed(
                request_factory,
                primary_call,
                validator=validator,
            )
            return RoutedInboxAdvice(
                primary_response,
                primary_model_name,
                "primary",
                diagnostics_before,
                diagnostics_after,
            )
        primary_response = await secondary.run_shadow(
            request_factory,
            primary_call,
            validator=validator,
        )
        return RoutedInboxAdvice(primary_response, primary_model_name, "primary")

    if observe_diagnostics:
        (
            selected,
            diagnostics_before,
            diagnostics_after,
        ) = await secondary.secondary_preferred_required_result_observed(
            request_factory(),
            primary_call,
            validator=validator,
        )
    else:
        selected = await secondary.secondary_preferred_required_result(
            request_factory(),
            primary_call,
            validator=validator,
        )
    if not isinstance(selected, SecondaryResult):
        # The scheduler returns the exact primary object on disabled, admission,
        # transport, deadline, policy or protocol failure.
        return RoutedInboxAdvice(
            selected,
            primary_model_name,
            "primary",
            diagnostics_before,
            diagnostics_after,
        )
    return RoutedInboxAdvice(
        _secondary_response(selected),
        selected.served_model_alias,
        "secondary",
        diagnostics_before,
        diagnostics_after,
    )
