"""Pure composition gate for public-web research readiness.

Readiness is derived only from already-built mission, source-diversity, and
consumption facts.  This module never fetches, reads, stores, invents, or
wires retrieval results.  Any missing, invalid, or mismatched input fails
closed to ``NOT_READY``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
)
from friday.orchestration.web_research_mission import (
    WebResearchMissionV1,
    build_web_research_mission,
)
from friday.orchestration.web_source_diversity import (
    WebSourceDiversityNote,
    WebSourceDiversityV1,
    build_web_source_diversity,
)

WEB_RESEARCH_READINESS_SCHEMA = "friday.web-research-readiness.v1"
MAX_READINESS_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class WebResearchReadinessError(ValueError):
    """A readiness value is outside the closed composition contract."""


class WebResearchReadinessState(StrEnum):
    """Closed outcomes exposed to the requesting workflow."""

    READY = "ready"
    READY_DEGRADED = "ready_degraded"
    NOT_READY = "not_ready"


class WebResearchReadinessReason(StrEnum):
    """Closed explanation for one readiness outcome."""

    READY_DIVERSE = "ready_diverse"
    READY_DEGRADED_DIVERSITY = "ready_degraded_diversity"
    READY_DEGRADED_CONSUMPTION = "ready_degraded_consumption"
    INPUTS_INVALID = "inputs_invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    CONSUMPTION_BLOCKED_PRIVATE = "consumption_blocked_private"
    CONSUMPTION_UNAVAILABLE = "consumption_unavailable"
    DIVERSITY_EMPTY = "diversity_empty"
    DIVERSITY_INSUFFICIENT = "diversity_insufficient"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebResearchReadinessError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> WebResearchReadinessState:
    try:
        return WebResearchReadinessState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebResearchReadinessError("readiness_closed") from exc


def _reason(value: object) -> WebResearchReadinessReason:
    try:
        return WebResearchReadinessReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebResearchReadinessError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class WebResearchReadinessV1:
    """Immutable readiness plus the three input identities, or none."""

    readiness_id: str
    authenticated_turn_id: str | None
    mission_id: str | None
    diversity_id: str | None
    consumption_id: str | None
    readiness: WebResearchReadinessState
    reason: WebResearchReadinessReason

    def __post_init__(self) -> None:
        _identifier(self.readiness_id, field="readiness_id")
        ids = (self.mission_id, self.diversity_id, self.consumption_id)
        if any(value is not None for value in ids):
            if any(value is None for value in ids) or self.authenticated_turn_id is None:
                _fail("input_identities", "all_or_none")
            for value, field in zip(ids, ("mission_id", "diversity_id", "consumption_id"), strict=True):
                _identifier(value, field=field)
            _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        elif self.authenticated_turn_id is not None:
            _fail("input_identities", "all_or_none")
        readiness = _state(self.readiness)
        reason = _reason(self.reason)
        object.__setattr__(self, "readiness", readiness)
        object.__setattr__(self, "reason", reason)
        if (
            readiness is WebResearchReadinessState.READY
            and reason is not WebResearchReadinessReason.READY_DIVERSE
        ):
            _fail("readiness_reason", "inconsistent")
        if readiness is WebResearchReadinessState.READY_DEGRADED and reason not in {
            WebResearchReadinessReason.READY_DEGRADED_DIVERSITY,
            WebResearchReadinessReason.READY_DEGRADED_CONSUMPTION,
        }:
            _fail("readiness_reason", "inconsistent")
        if readiness in {
            WebResearchReadinessState.READY,
            WebResearchReadinessState.READY_DEGRADED,
        } and any(value is None for value in ids):
            _fail("readiness", "missing_identities")

    @property
    def state(self) -> WebResearchReadinessState:
        return self.readiness

    @property
    def closed_readiness(self) -> WebResearchReadinessState:
        return self.readiness

    @property
    def decision(self) -> WebResearchReadinessState:
        return self.readiness

    @property
    def closed_reason(self) -> WebResearchReadinessReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_RESEARCH_READINESS_SCHEMA,
            "readiness_id": self.readiness_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "mission_id": self.mission_id,
            "diversity_id": self.diversity_id,
            "consumption_id": self.consumption_id,
            "readiness": self.readiness.value,
            "reason": self.reason.value,
        }


ReadinessState = WebResearchReadinessState
ReadinessReason = WebResearchReadinessReason
WebResearchReadiness = WebResearchReadinessV1


def _coerce_mission(value: object) -> WebResearchMissionV1 | None:
    try:
        result = value if isinstance(value, WebResearchMissionV1) else build_web_research_mission(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def _coerce_diversity(value: object) -> WebSourceDiversityV1 | None:
    try:
        result = value if isinstance(value, WebSourceDiversityV1) else build_web_source_diversity(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def _coerce_consumption(value: object) -> WebResearchConsumptionV1 | None:
    if isinstance(value, WebResearchConsumptionV1):
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        allowed = {
            "consumption_id",
            "authenticated_turn_id",
            "usability",
            "state",
            "decision",
            "selected_provider_id",
            "provider_id",
            "admitted_source_count",
            "reason",
        }
        if set(value) - allowed:
            return None
        usability = value.get("usability", value.get("state", value.get("decision", _MISSING)))
        reason = value.get("reason", _MISSING)
        selected = value.get("selected_provider_id", value.get("provider_id"))
        admitted = value.get("admitted_source_count", _MISSING)
        if usability is _MISSING or reason is _MISSING or admitted is _MISSING:
            return None
        return WebResearchConsumptionV1(
            consumption_id=cast(str, value.get("consumption_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            usability=cast(WebResearchConsumptionState, usability),
            selected_provider_id=cast(str | None, selected),
            admitted_source_count=cast(int, admitted),
            reason=cast(WebResearchConsumptionReason, reason),
        )
    except (TypeError, ValueError):
        return None


def _not_ready(
    readiness_id: str,
    reason: WebResearchReadinessReason,
) -> WebResearchReadinessV1:
    return WebResearchReadinessV1(
        readiness_id=readiness_id,
        authenticated_turn_id=None,
        mission_id=None,
        diversity_id=None,
        consumption_id=None,
        readiness=WebResearchReadinessState.NOT_READY,
        reason=reason,
    )


def _ready(
    readiness_id: str,
    mission: WebResearchMissionV1,
    diversity: WebSourceDiversityV1,
    consumption: WebResearchConsumptionV1,
    state: WebResearchReadinessState,
    reason: WebResearchReadinessReason,
) -> WebResearchReadinessV1:
    return WebResearchReadinessV1(
        readiness_id=readiness_id,
        authenticated_turn_id=mission.authenticated_turn_id,
        mission_id=mission.mission_id,
        diversity_id=diversity.diversity_id,
        consumption_id=consumption.consumption_id,
        readiness=state,
        reason=reason,
    )


def _mapping_value(raw: Mapping[str, Any], *names: str, default: object = None) -> object:
    for name in names:
        if name in raw:
            return raw[name]
    return default


def _known_mapping_keys(raw: Mapping[str, Any]) -> bool:
    return not (
        set(raw)
        - {
            "schema",
            "readiness_id",
            "authenticated_turn_id",
            "mission_id",
            "diversity_id",
            "consumption_id",
            "readiness",
            "state",
            "reason",
            "mission",
            "research_mission",
            "mission_facts",
            "diversity",
            "source_diversity",
            "diversity_facts",
            "consumption",
            "research_consumption",
            "consumption_facts",
        }
    )


def build_web_research_readiness(
    readiness_id: str | Mapping[str, Any],
    mission: object = None,
    diversity: object = None,
    consumption: object = None,
    *,
    authenticated_turn_id: object = None,
) -> WebResearchReadinessV1:
    """Compose already-built facts into READY, READY_DEGRADED, or NOT_READY."""

    if isinstance(readiness_id, Mapping):
        raw = readiness_id
        if not _known_mapping_keys(raw):
            _fail("readiness", "unknown_fields")
        if raw.get("schema", WEB_RESEARCH_READINESS_SCHEMA) != WEB_RESEARCH_READINESS_SCHEMA:
            _fail("schema")
        output_keys = {"readiness", "state", "mission_id", "diversity_id", "consumption_id", "reason"}
        fact_keys = {
            "mission",
            "research_mission",
            "mission_facts",
            "diversity",
            "source_diversity",
            "diversity_facts",
            "consumption",
            "research_consumption",
            "consumption_facts",
        }
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("readiness", "duplicate_representations")
        if output_keys.intersection(raw):
            return WebResearchReadinessV1(
                readiness_id=cast(str, raw.get("readiness_id")),
                authenticated_turn_id=cast(str | None, raw.get("authenticated_turn_id")),
                mission_id=cast(str | None, raw.get("mission_id")),
                diversity_id=cast(str | None, raw.get("diversity_id")),
                consumption_id=cast(str | None, raw.get("consumption_id")),
                readiness=cast(WebResearchReadinessState, raw.get("readiness", raw.get("state"))),
                reason=cast(WebResearchReadinessReason, raw.get("reason")),
            )
        readiness_id = cast(str, raw.get("readiness_id"))
        authenticated_turn_id = _mapping_value(raw, "authenticated_turn_id", default=authenticated_turn_id)
        mission = _mapping_value(raw, "mission", "research_mission", "mission_facts", default=mission)
        diversity = _mapping_value(raw, "diversity", "source_diversity", "diversity_facts", default=diversity)
        consumption = _mapping_value(
            raw,
            "consumption",
            "research_consumption",
            "consumption_facts",
            default=consumption,
        )
    readiness_key = _identifier(readiness_id, field="readiness_id")
    mission_value = _coerce_mission(mission)
    diversity_value = _coerce_diversity(diversity)
    consumption_value = _coerce_consumption(consumption)
    if mission_value is None or diversity_value is None or consumption_value is None:
        return _not_ready(readiness_key, WebResearchReadinessReason.INPUTS_INVALID)

    expected_turn = mission_value.authenticated_turn_id
    if (
        diversity_value.authenticated_turn_id != expected_turn
        or consumption_value.authenticated_turn_id != expected_turn
        or authenticated_turn_id is not None
        and authenticated_turn_id != expected_turn
    ):
        return _not_ready(readiness_key, WebResearchReadinessReason.IDENTITY_MISMATCH)

    consumption_state = consumption_value.usability
    if consumption_state is WebResearchConsumptionState.BLOCKED_PRIVATE:
        return _not_ready(readiness_key, WebResearchReadinessReason.CONSUMPTION_BLOCKED_PRIVATE)
    if consumption_state is WebResearchConsumptionState.UNAVAILABLE:
        return _not_ready(readiness_key, WebResearchReadinessReason.CONSUMPTION_UNAVAILABLE)
    if diversity_value.diversity_note is WebSourceDiversityNote.EMPTY:
        return _not_ready(readiness_key, WebResearchReadinessReason.DIVERSITY_EMPTY)

    if (
        consumption_state is WebResearchConsumptionState.CONSUMABLE
        and diversity_value.diversity_note is WebSourceDiversityNote.DIVERSE
    ):
        return _ready(
            readiness_key,
            mission_value,
            diversity_value,
            consumption_value,
            WebResearchReadinessState.READY,
            WebResearchReadinessReason.READY_DIVERSE,
        )
    if consumption_state is WebResearchConsumptionState.CONSUMABLE_DEGRADED:
        if diversity_value.diversity_note is not WebSourceDiversityNote.DIVERSE:
            return _not_ready(readiness_key, WebResearchReadinessReason.DIVERSITY_INSUFFICIENT)
        reason = WebResearchReadinessReason.READY_DEGRADED_CONSUMPTION
    else:
        reason = WebResearchReadinessReason.READY_DEGRADED_DIVERSITY
    return _ready(
        readiness_key,
        mission_value,
        diversity_value,
        consumption_value,
        WebResearchReadinessState.READY_DEGRADED,
        reason,
    )


def validate_web_research_readiness(value: object) -> bool:
    """Return whether a value is a valid frozen readiness mapping/object."""

    try:
        if isinstance(value, WebResearchReadinessV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        if not _known_mapping_keys(value):
            return False
        if value.get("schema", WEB_RESEARCH_READINESS_SCHEMA) != WEB_RESEARCH_READINESS_SCHEMA:
            return False
        _identifier(value.get("readiness_id"), field="readiness_id")
        ids = tuple(value.get(field) for field in ("mission_id", "diversity_id", "consumption_id"))
        return (
            WebResearchReadinessV1(
                readiness_id=cast(str, value.get("readiness_id")),
                authenticated_turn_id=cast(str | None, value.get("authenticated_turn_id")),
                mission_id=cast(str | None, ids[0]),
                diversity_id=cast(str | None, ids[1]),
                consumption_id=cast(str | None, ids[2]),
                readiness=cast(WebResearchReadinessState, value.get("readiness", value.get("state"))),
                reason=cast(WebResearchReadinessReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


assess_web_research_readiness = build_web_research_readiness
decide_web_research_readiness = build_web_research_readiness
validate_research_readiness = validate_web_research_readiness


__all__ = [
    "MAX_READINESS_ID_CHARS",
    "WEB_RESEARCH_READINESS_SCHEMA",
    "ReadinessReason",
    "ReadinessState",
    "WebResearchReadiness",
    "WebResearchReadinessError",
    "WebResearchReadinessReason",
    "WebResearchReadinessState",
    "WebResearchReadinessV1",
    "assess_web_research_readiness",
    "build_web_research_readiness",
    "decide_web_research_readiness",
    "validate_research_readiness",
    "validate_web_research_readiness",
]
