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
    contains_private_text: bool,
    image_bearing: bool,
) -> RoutedInboxAdvice:
    """Route one advisory extraction without wrapping or replacing the primary."""

    if secondary is None:
        return RoutedInboxAdvice(await primary_call(), primary_model_name, "primary")

    def request_factory() -> ModelRequest:
        return ModelRequest(
            workload=ModelWorkload.EXTRACT,
            messages=tuple(messages),
            max_output_tokens=max(64, int(max_output_tokens)),
            absolute_deadline_monotonic=secondary.new_advisory_deadline(),
            priority=ModelPriority.BACKGROUND,
            effect_class=EffectClass.NONE,
            modality=ModelModality.IMAGE if image_bearing else ModelModality.TEXT,
            require_structured_output=True,
            require_independent_model=True,
            contains_private_text=contains_private_text,
        )

    if secondary.mode is SecondaryMode.SHADOW:
        primary_response = await secondary.run_shadow(
            request_factory,
            primary_call,
            validator=lambda result: valid_inbox_advice_shape(result.structured_output),
        )
        return RoutedInboxAdvice(primary_response, primary_model_name, "primary")

    selected = await secondary.secondary_preferred_required_result(
        request_factory(),
        primary_call,
        validator=lambda result: valid_inbox_advice_shape(result.structured_output),
    )
    if not isinstance(selected, SecondaryResult):
        # The scheduler returns the exact primary object on disabled, admission,
        # transport, deadline, policy or protocol failure.
        return RoutedInboxAdvice(selected, primary_model_name, "primary")
    return RoutedInboxAdvice(
        _secondary_response(selected),
        selected.served_model_alias,
        "secondary",
    )
