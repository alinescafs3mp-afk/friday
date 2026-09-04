"""Restart-safe status/execution facts for mixed journeys."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_RESTART_SCHEMA = "friday.mixed-journey-restart.v1"
MAX_OWNER_CLAIMS = 8
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_STATUS = frozenset(
    {"idle", "pending", "running", "completed", "failed", "cancelled", "continuing", "restarted", "unknown"}
)
_EXECUTION = frozenset(
    {
        "not_started",
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "continuing",
        "restarted",
        "unknown",
    }
)


class MixedJourneyRestartError(ValueError):
    """A restart status or execution fact is malformed."""


class MixedJourneyRestartState(StrEnum):
    EMPTY = "empty"
    CONTINUING = "continuing"
    RESTARTED = "restarted"
    BLOCKED = "blocked"


class MixedJourneyRestartReason(StrEnum):
    NO_FACTS = "no_facts"
    CONTINUING = "continuing"
    RESTARTED = "restarted"
    INVALID_FACTS = "invalid_facts"
    MULTIPLE_EFFECT_OWNERS = "multiple_effect_owners"
    UNSAFE_RECENCY = "unsafe_recency"


@dataclass(frozen=True, slots=True)
class MixedJourneyRestartFactsV1:
    status: str | None = None
    execution: str | None = None
    restarted: bool | None = None
    effect_owners: tuple[object, ...] = ()
    recency_selector: str | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyRestartError(f"{field}_{detail}")


def _id(value: object, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _phase(value: object, *, allowed: frozenset[str], field: str) -> str:
    if type(value) is not str or value.casefold() not in allowed:
        _fail(field, "closed")
    return cast(str, value.casefold())


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        _fail(field, "boolean")
    return cast(bool, value)


def _claims(value: object) -> int:
    if value is None:
        return 0
    if type(value) is int:
        if not 0 <= value <= MAX_OWNER_CLAIMS:
            _fail("effect_owners", "count")
        return cast(int, value)
    if isinstance(value, (str, bytes, bytearray)):
        values: tuple[object, ...] = (value,)
    elif isinstance(value, Sequence):
        values = tuple(value)
    else:
        _fail("effect_owners", "sequence")
    if len(values) > MAX_OWNER_CLAIMS:
        _fail("effect_owners", "count")
    for item in values:
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or "://" in item
            or "/" in item
            or "\\" in item
        ):
            _fail("effect_owners", "private")
    return len(values)


def _selector(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _fail("recency_selector")
    selector = cast(str, value).strip().casefold()
    if selector in {"latest", "head"}:
        _fail("recency_selector", "unsafe")
    if not re.fullmatch(r"revision:[1-9][0-9]{0,9}", selector):
        _fail("recency_selector", "closed")
    return selector


@dataclass(frozen=True, slots=True)
class MixedJourneyRestartV1:
    journey_id: str
    authenticated_turn_id: str
    state: MixedJourneyRestartState
    status: str | None
    execution: str | None
    restarted: bool
    effect_owner_count: int
    recency_selector: str | None
    reason: MixedJourneyRestartReason

    def __post_init__(self) -> None:
        _id(self.journey_id, "journey_id")
        _id(self.authenticated_turn_id, "authenticated_turn_id")
        try:
            state = MixedJourneyRestartState(self.state)
            reason = MixedJourneyRestartReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyRestartError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if self.status is not None:
            _phase(self.status, allowed=_STATUS, field="status")
        if self.execution is not None:
            _phase(self.execution, allowed=_EXECUTION, field="execution")
        if (
            type(self.restarted) is not bool
            or type(self.effect_owner_count) is not int
            or not 0 <= self.effect_owner_count <= MAX_OWNER_CLAIMS
        ):
            _fail("facts")
        if self.recency_selector is not None:
            _selector(self.recency_selector)
        if state in {MixedJourneyRestartState.CONTINUING, MixedJourneyRestartState.RESTARTED} and (
            self.status is None or self.execution is None
        ):
            _fail("active", "facts")
        if state in {MixedJourneyRestartState.EMPTY, MixedJourneyRestartState.BLOCKED} and (
            self.status is not None
            or self.execution is not None
            or self.effect_owner_count
            or self.recency_selector
        ):
            _fail("non_active", "leak")

    @property
    def restart_state(self) -> MixedJourneyRestartState:
        return self.state

    @property
    def decision(self) -> MixedJourneyRestartState:
        return self.state

    @property
    def continuing(self) -> bool:
        return self.state is MixedJourneyRestartState.CONTINUING

    @property
    def restarted_state(self) -> bool:
        return self.state is MixedJourneyRestartState.RESTARTED

    @property
    def owner_count(self) -> int:
        return self.effect_owner_count

    @property
    def selector(self) -> str | None:
        return self.recency_selector

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_RESTART_SCHEMA,
            "journey_id": self.journey_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "status": self.status,
            "execution": self.execution,
            "restarted": self.restarted,
            "effect_owner_count": self.effect_owner_count,
            "recency_selector": self.recency_selector,
            "reason": self.reason.value,
        }


MixedJourneyRestart = MixedJourneyRestartV1
MixedJourneyRestartFacts = MixedJourneyRestartFactsV1
RestartState = MixedJourneyRestartState
RestartReason = MixedJourneyRestartReason


def _empty(key: str, turn: str, reason: MixedJourneyRestartReason) -> MixedJourneyRestartV1:
    return MixedJourneyRestartV1(
        key, turn, MixedJourneyRestartState.EMPTY, None, None, False, 0, None, reason
    )


def _blocked(key: str, turn: str, reason: MixedJourneyRestartReason) -> MixedJourneyRestartV1:
    return MixedJourneyRestartV1(
        key, turn, MixedJourneyRestartState.BLOCKED, None, None, False, 0, None, reason
    )


def build_mixed_journey_restart(
    journey_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    *,
    facts: MixedJourneyRestartFactsV1 | Mapping[str, Any] | None = None,
    status: str | None = None,
    execution: str | None = None,
    restarted: bool | None = None,
    effect_owners: object = None,
    recency_selector: str | None = None,
) -> MixedJourneyRestartV1:
    """Admit restart facts without selecting a live/latest generation."""

    if isinstance(journey_id, Mapping):
        raw = journey_id
        key = cast(str, raw.get("journey_id", "journey"))
        turn = cast(str, raw.get("authenticated_turn_id", raw.get("turn_id", "turn")))
        try:
            key, turn = _id(key, "journey_id"), _id(turn, "authenticated_turn_id")
            if raw.get("schema", MIXED_JOURNEY_RESTART_SCHEMA) != MIXED_JOURNEY_RESTART_SCHEMA:
                _fail("schema")
            if raw.get("state") in {"empty", "blocked"}:
                state = MixedJourneyRestartState(raw["state"])
                reason = MixedJourneyRestartReason(raw.get("reason", "invalid_facts"))
                return (
                    _blocked(key, turn, reason)
                    if state is MixedJourneyRestartState.BLOCKED
                    else _empty(key, turn, reason)
                )
            status = raw.get("status")
            execution = raw.get("execution")
            restarted = raw.get("restarted")
            effect_owners = raw.get("effect_owners", raw.get("effect_owner_count"))
            recency_selector = raw.get("recency_selector")
            journey_id, authenticated_turn_id = key, turn
        except (TypeError, ValueError, MixedJourneyRestartError):
            try:
                return _blocked(
                    _id(key, "journey_id"),
                    _id(turn, "authenticated_turn_id"),
                    MixedJourneyRestartReason.INVALID_FACTS,
                )
            except MixedJourneyRestartError:
                return _blocked("journey", "turn", MixedJourneyRestartReason.INVALID_FACTS)
    key = _id(journey_id, "journey_id")
    turn = _id(authenticated_turn_id, "authenticated_turn_id")
    if facts is not None:
        try:
            if any(
                value is not None for value in (status, execution, restarted, effect_owners, recency_selector)
            ):
                _fail("facts", "duplicate")
            if isinstance(facts, MixedJourneyRestartFactsV1):
                status, execution, restarted, effect_owners, recency_selector = (
                    facts.status,
                    facts.execution,
                    facts.restarted,
                    facts.effect_owners,
                    facts.recency_selector,
                )
            elif isinstance(facts, Mapping):
                allowed = {
                    "schema",
                    "status",
                    "execution",
                    "restarted",
                    "restart",
                    "effect_owners",
                    "effect_owner_count",
                    "recency_selector",
                    "selector",
                    "live_effect_owner_count",
                    "status_phase",
                    "execution_phase",
                }
                if facts.get("schema", MIXED_JOURNEY_RESTART_SCHEMA) != MIXED_JOURNEY_RESTART_SCHEMA:
                    _fail("schema")
                if set(facts) - allowed:
                    _fail("facts", "unknown")
                status, execution = (
                    facts.get("status", facts.get("status_phase")),
                    facts.get("execution", facts.get("execution_phase")),
                )
                restarted = facts.get("restarted", facts.get("restart"))
                effect_owners = facts.get(
                    "effect_owners",
                    facts.get("effect_owner_count", facts.get("live_effect_owner_count")),
                )
                recency_selector = facts.get("recency_selector", facts.get("selector"))
            else:
                _fail("facts", "type")
        except (TypeError, ValueError, MixedJourneyRestartError):
            return _blocked(key, turn, MixedJourneyRestartReason.INVALID_FACTS)
    if (
        status is None
        and execution is None
        and restarted is None
        and effect_owners is None
        and recency_selector is None
    ):
        return _empty(key, turn, MixedJourneyRestartReason.NO_FACTS)
    try:
        if status is None or execution is None:
            _fail("active", "facts")
        status_value = _phase(status, allowed=_STATUS, field="status")
        execution_value = _phase(execution, allowed=_EXECUTION, field="execution")
        restarted_value = False if restarted is None else _bool(restarted, "restarted")
        owner_count = _claims(effect_owners)
        selector = _selector(recency_selector)
    except MixedJourneyRestartError as exc:
        return _blocked(
            key,
            turn,
            MixedJourneyRestartReason.UNSAFE_RECENCY
            if "recency" in str(exc)
            else MixedJourneyRestartReason.INVALID_FACTS,
        )
    if owner_count > 1:
        return _blocked(key, turn, MixedJourneyRestartReason.MULTIPLE_EFFECT_OWNERS)
    explicit_restart = (
        restarted_value
        or status_value in {"restarted", "continuing"}
        or execution_value in {"restarted", "continuing"}
    )
    return MixedJourneyRestartV1(
        key,
        turn,
        MixedJourneyRestartState.RESTARTED if explicit_restart else MixedJourneyRestartState.CONTINUING,
        status_value,
        execution_value,
        restarted_value,
        owner_count,
        selector,
        MixedJourneyRestartReason.RESTARTED if explicit_restart else MixedJourneyRestartReason.CONTINUING,
    )


def validate_mixed_journey_restart(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyRestartV1)
            else build_mixed_journey_restart(cast(Mapping[str, Any], value))
        )
        return (
            isinstance(result, MixedJourneyRestartV1) and result.state is not MixedJourneyRestartState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_journey_restart = build_mixed_journey_restart
validate_journey_restart = validate_mixed_journey_restart

__all__ = [
    "MIXED_JOURNEY_RESTART_SCHEMA",
    "MixedJourneyRestart",
    "MixedJourneyRestartError",
    "MixedJourneyRestartFacts",
    "MixedJourneyRestartFactsV1",
    "MixedJourneyRestartReason",
    "MixedJourneyRestartState",
    "MixedJourneyRestartV1",
    "RestartReason",
    "RestartState",
    "build_journey_restart",
    "build_mixed_journey_restart",
    "validate_journey_restart",
    "validate_mixed_journey_restart",
]
