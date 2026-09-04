"""A body-free mixed-journey adapter for public-web consumption outcomes.

This seam accepts a previously built :class:`WebResearchConsumptionV1` (or
its closed mapping).  It does not accept URLs, private hosts, evidence bodies,
or provider execution facts, and it performs no network or store I/O.
"""

from __future__ import annotations

import hashlib
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

MIXED_JOURNEY_WEB_FACTS_SCHEMA = "friday.mixed-journey-web-facts.v1"
MAX_CONSUMPTION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_ADMITTED_SOURCE_COUNT = 11
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER_IDS = frozenset({"yandex", "brave", "tavily", "serper", "brave-html", "duckduckgo", "wikipedia"})
_MISSING = object()


class MixedJourneyWebFactsError(ValueError):
    """A public-web consumption fact or result is malformed."""


class MixedJourneyWebFactsState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    BLOCKED = "blocked"


class MixedJourneyWebFactsReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    BLOCKED_PRIVATE = "blocked_private"
    EMPTY_AFTER_OUTBOUND = "empty_after_outbound"
    PROVIDER_FACTS_INVALID = "provider_facts_invalid"
    CONSUMPTION_UNAVAILABLE = "consumption_unavailable"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class MixedJourneyWebFactsInputV1:
    """A previously admitted, body-free consumption outcome."""

    consumption: WebResearchConsumptionV1 | Mapping[str, object] | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyWebFactsError(f"{field}_{detail}")


def _id(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _digest(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("summary_digest", "hex")
    return cast(str, value)


def _provider(value: object) -> str:
    if type(value) is not str or value.casefold() not in _PROVIDER_IDS:
        _fail("selected_provider_id", "invalid")
    return value.casefold()


def _reason(value: object) -> MixedJourneyWebFactsReason:
    if isinstance(value, MixedJourneyWebFactsReason):
        return value
    try:
        return MixedJourneyWebFactsReason(str(value).strip().casefold())
    except (TypeError, ValueError) as exc:
        raise MixedJourneyWebFactsError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class MixedJourneyWebFactsV1:
    """One immutable result containing only public-web outcome metadata."""

    consumption_id: str | None
    authenticated_turn_id: str | None
    state: MixedJourneyWebFactsState
    selected_provider_id: str | None
    admitted_source_count: int
    summary_digest: str | None
    reason: MixedJourneyWebFactsReason

    def __post_init__(self) -> None:
        if self.consumption_id is not None:
            _id(self.consumption_id, field="consumption_id")
        if self.authenticated_turn_id is not None:
            _id(self.authenticated_turn_id, field="authenticated_turn_id")
        try:
            state = MixedJourneyWebFactsState(self.state)
            reason = _reason(self.reason)
        except (TypeError, ValueError, MixedJourneyWebFactsError) as exc:
            raise MixedJourneyWebFactsError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if (
            type(self.admitted_source_count) is not int
            or not 0 <= self.admitted_source_count <= MAX_ADMITTED_SOURCE_COUNT
        ):
            _fail("admitted_source_count", "bound")
        if state is MixedJourneyWebFactsState.PRESENT:
            if self.consumption_id is None or self.authenticated_turn_id is None:
                _fail("present", "identity")
            if self.selected_provider_id is None:
                _fail("selected_provider_id")
            _provider(self.selected_provider_id)
            if self.admitted_source_count < 1:
                _fail("admitted_source_count", "empty")
            _digest(self.summary_digest)
        elif (
            self.selected_provider_id is not None
            or self.admitted_source_count != 0
            or self.summary_digest is not None
        ):
            _fail("non_present", "leak")
        if state is MixedJourneyWebFactsState.BLOCKED and (
            self.consumption_id is not None or self.authenticated_turn_id is not None
        ):
            _fail("blocked", "private_leak")

    @property
    def fact_state(self) -> MixedJourneyWebFactsState:
        return self.state

    @property
    def web_state(self) -> MixedJourneyWebFactsState:
        return self.state

    @property
    def decision(self) -> MixedJourneyWebFactsState:
        return self.state

    @property
    def provider_id(self) -> str | None:
        return self.selected_provider_id

    @property
    def digest(self) -> str | None:
        return self.summary_digest

    @property
    def closed_reason(self) -> MixedJourneyWebFactsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_WEB_FACTS_SCHEMA,
            "consumption_id": self.consumption_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "selected_provider_id": self.selected_provider_id,
            "admitted_source_count": self.admitted_source_count,
            "summary_digest": self.summary_digest,
            "reason": self.reason.value,
        }


WebFactsInput = MixedJourneyWebFactsInputV1
WebFactsState = MixedJourneyWebFactsState
WebFactsReason = MixedJourneyWebFactsReason
MixedJourneyWebFacts = MixedJourneyWebFactsV1


def _blocked(reason: MixedJourneyWebFactsReason) -> MixedJourneyWebFactsV1:
    return MixedJourneyWebFactsV1(None, None, MixedJourneyWebFactsState.BLOCKED, None, 0, None, reason)


def _empty() -> MixedJourneyWebFactsV1:
    return MixedJourneyWebFactsV1(
        None, None, MixedJourneyWebFactsState.EMPTY, None, 0, None, MixedJourneyWebFactsReason.NO_FACTS
    )


def _summary(consumption: WebResearchConsumptionV1) -> str:
    value = "|".join(
        (
            consumption.consumption_id,
            consumption.authenticated_turn_id,
            consumption.selected_provider_id or "none",
            str(consumption.admitted_source_count),
            consumption.reason.value,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _consumption(value: object) -> WebResearchConsumptionV1:
    if isinstance(value, WebResearchConsumptionV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        _fail("consumption", "type")
    allowed = {
        "schema",
        "consumption_id",
        "authenticated_turn_id",
        "usability",
        "state",
        "selected_provider_id",
        "provider_id",
        "admitted_source_count",
        "source_count",
        "reason",
    }
    if set(value) - allowed:
        _fail("consumption", "private_or_unknown")
    if value.get("schema") not in (None, "friday.web-research-consumption.v1"):
        _fail("consumption", "schema")
    usability = value.get("usability", value.get("state"))
    if usability in (None, "empty"):
        _fail("consumption", "empty")
    selected = value.get("selected_provider_id", value.get("provider_id"))
    count = value.get("admitted_source_count", value.get("source_count", 0))
    return WebResearchConsumptionV1(
        consumption_id=cast(str, value.get("consumption_id")),
        authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
        usability=cast(WebResearchConsumptionState, usability),
        selected_provider_id=cast(str | None, selected),
        admitted_source_count=cast(int, count),
        reason=cast(WebResearchConsumptionReason, value.get("reason")),
    )


def build_mixed_journey_web_facts(
    consumption: WebResearchConsumptionV1 | Mapping[str, object] | None = None,
    *,
    summary_digest: object = _MISSING,
    web_consumption: WebResearchConsumptionV1 | Mapping[str, object] | None = None,
    facts: MixedJourneyWebFactsInputV1 | Mapping[str, object] | None = None,
) -> MixedJourneyWebFactsV1:
    """Project an already-supplied public-web consumption outcome."""

    if facts is not None:
        if consumption is not None or web_consumption is not None:
            return _blocked(MixedJourneyWebFactsReason.INVALID_FACTS)
        if isinstance(facts, MixedJourneyWebFactsInputV1):
            consumption = facts.consumption
        elif isinstance(facts, Mapping):
            consumption = facts
        else:
            return _blocked(MixedJourneyWebFactsReason.INVALID_FACTS)
    if web_consumption is not None:
        if consumption is not None:
            return _blocked(MixedJourneyWebFactsReason.INVALID_FACTS)
        consumption = web_consumption
    if consumption is None:
        return _empty()
    if isinstance(consumption, Mapping):
        raw = consumption
        if raw.get("state") in {"empty", "blocked"} and "usability" not in raw:
            state = raw.get("state")
            if state == "empty":
                return _empty()
            try:
                reason = _reason(raw.get("reason", MixedJourneyWebFactsReason.INVALID_FACTS.value))
            except MixedJourneyWebFactsError:
                reason = MixedJourneyWebFactsReason.INVALID_FACTS
            return _blocked(reason)
        if raw.get("state") == "present" and "usability" not in raw:
            allowed = {
                "schema",
                "consumption_id",
                "authenticated_turn_id",
                "state",
                "selected_provider_id",
                "admitted_source_count",
                "summary_digest",
                "reason",
            }
            if set(raw) - allowed:
                return _blocked(MixedJourneyWebFactsReason.INVALID_FACTS)
            try:
                value = _consumption(
                    {
                        "consumption_id": raw.get("consumption_id"),
                        "authenticated_turn_id": raw.get("authenticated_turn_id"),
                        "usability": "consumable",
                        "selected_provider_id": raw.get("selected_provider_id"),
                        "admitted_source_count": raw.get("admitted_source_count"),
                        "reason": "primary_sources",
                    }
                )
                if summary_digest is _MISSING:
                    summary_digest = raw.get("summary_digest", _MISSING)
            except (TypeError, ValueError, MixedJourneyWebFactsError):
                return _blocked(MixedJourneyWebFactsReason.PROVIDER_FACTS_INVALID)
        else:
            try:
                value = _consumption(raw.get("consumption", raw))
            except (TypeError, ValueError, MixedJourneyWebFactsError):
                return _blocked(MixedJourneyWebFactsReason.PROVIDER_FACTS_INVALID)
    else:
        try:
            value = _consumption(consumption)
        except (TypeError, ValueError, MixedJourneyWebFactsError):
            return _blocked(MixedJourneyWebFactsReason.PROVIDER_FACTS_INVALID)
    if value.usability is WebResearchConsumptionState.BLOCKED_PRIVATE:
        return _blocked(MixedJourneyWebFactsReason.BLOCKED_PRIVATE)
    if value.usability is WebResearchConsumptionState.UNAVAILABLE:
        reason = (
            MixedJourneyWebFactsReason.EMPTY_AFTER_OUTBOUND
            if value.reason is WebResearchConsumptionReason.NO_ADMITTED_SOURCES
            else MixedJourneyWebFactsReason.PROVIDER_FACTS_INVALID
            if value.reason is WebResearchConsumptionReason.PROVIDER_FACTS_INVALID
            else MixedJourneyWebFactsReason.CONSUMPTION_UNAVAILABLE
        )
        return _blocked(reason)
    try:
        digest = _summary(value) if summary_digest is _MISSING else _digest(summary_digest)
    except MixedJourneyWebFactsError:
        return _blocked(MixedJourneyWebFactsReason.INVALID_FACTS)
    return MixedJourneyWebFactsV1(
        value.consumption_id,
        value.authenticated_turn_id,
        MixedJourneyWebFactsState.PRESENT,
        value.selected_provider_id,
        value.admitted_source_count,
        digest,
        MixedJourneyWebFactsReason.PRESENT,
    )


def validate_mixed_journey_web_facts(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyWebFactsV1)
            else build_mixed_journey_web_facts(cast(Mapping[str, object], value))
        )
        return (
            isinstance(result, MixedJourneyWebFactsV1)
            and result.state is not MixedJourneyWebFactsState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_web_facts = build_mixed_journey_web_facts
validate_web_facts = validate_mixed_journey_web_facts

__all__ = [
    "MIXED_JOURNEY_WEB_FACTS_SCHEMA",
    "WebFactsInput",
    "WebFactsReason",
    "WebFactsState",
    "MixedJourneyWebFacts",
    "MixedJourneyWebFactsError",
    "MixedJourneyWebFactsInputV1",
    "MixedJourneyWebFactsReason",
    "MixedJourneyWebFactsState",
    "MixedJourneyWebFactsV1",
    "build_mixed_journey_web_facts",
    "build_web_facts",
    "validate_mixed_journey_web_facts",
    "validate_web_facts",
]
