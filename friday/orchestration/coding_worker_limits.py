"""Closed resource limits for a future isolated Coding worker.

Only positive, bounded integers are admitted.  The builder validates supplied
facts and never starts a timer, allocates memory, or changes process limits.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

CODING_WORKER_LIMITS_SCHEMA = "friday.coding-worker-limits.v1"
MAX_LIMITS_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_WALL_CLOCK_SEC = 900
MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
MAX_CPU_SEC = 900
# Descriptive aliases make the closed limits easy for callers and tests to
# discover without introducing a second source of truth.
MAX_WALL_CLOCK_SECONDS = MAX_WALL_CLOCK_SEC
MAX_MEMORY = MAX_MEMORY_BYTES
MAX_CPU_SECONDS = MAX_CPU_SEC

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class CodingWorkerLimitsError(ValueError):
    """A resource-limit fact or directly constructed result is malformed."""


class CodingWorkerLimitsState(StrEnum):
    """Closed outcomes for resource-limit admission."""

    EMPTY = "empty"
    BOUNDED = "bounded"
    BLOCKED = "blocked"


class CodingWorkerLimitsReason(StrEnum):
    """Non-sensitive reason for one resource-limit outcome."""

    NO_FACTS = "no_facts"
    LIMITS_BOUNDED = "limits_bounded"
    MISSING_WALL_CLOCK = "missing_wall_clock"
    MISSING_MEMORY = "missing_memory"
    MISSING_CPU = "missing_cpu"
    NON_POSITIVE = "non_positive"
    WALL_CLOCK_LIMIT = "wall_clock_limit"
    MEMORY_LIMIT = "memory_limit"
    CPU_LIMIT = "cpu_limit"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingWorkerLimitsError(f"{field} must be a bounded opaque identifier")
    return cast(str, value)


def _state(value: object) -> CodingWorkerLimitsState:
    if isinstance(value, CodingWorkerLimitsState):
        return value
    if type(value) is not str:
        raise CodingWorkerLimitsError("limits must be a closed value")
    try:
        return CodingWorkerLimitsState(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerLimitsError("limits must be a closed value") from exc


def _reason(value: object) -> CodingWorkerLimitsReason:
    if isinstance(value, CodingWorkerLimitsReason):
        return value
    if type(value) is not str:
        raise CodingWorkerLimitsError("limits reason must be a closed value")
    try:
        return CodingWorkerLimitsReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerLimitsError("limits reason must be a closed value") from exc


@dataclass(frozen=True, slots=True)
class CodingWorkerLimitsFactsV1:
    """Caller-supplied positive resource limits."""

    wall_clock_sec: int | None = None
    memory_bytes: int | None = None
    cpu_sec: int | None = None


@dataclass(frozen=True, slots=True)
class CodingWorkerLimitsV1:
    """Immutable bounded resource limits."""

    limits_id: str
    authenticated_turn_id: str
    limits: CodingWorkerLimitsState
    wall_clock_sec: int | None
    memory_bytes: int | None
    cpu_sec: int | None
    reason: CodingWorkerLimitsReason

    def __post_init__(self) -> None:
        _identifier(self.limits_id, field="limits_id", maximum=MAX_LIMITS_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        limits = _state(self.limits)
        reason = _reason(self.reason)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "reason", reason)
        values = (self.wall_clock_sec, self.memory_bytes, self.cpu_sec)
        if limits is not CodingWorkerLimitsState.BOUNDED:
            if any(value is not None for value in values):
                raise CodingWorkerLimitsError("empty or blocked limits cannot expose values")
            return
        for value, field, maximum in zip(
            values,
            ("wall_clock_sec", "memory_bytes", "cpu_sec"),
            (MAX_WALL_CLOCK_SEC, MAX_MEMORY_BYTES, MAX_CPU_SEC),
            strict=True,
        ):
            if type(value) is not int or value <= 0 or value > maximum:
                raise CodingWorkerLimitsError(f"{field} is outside the closed positive bound")

    @property
    def state(self) -> CodingWorkerLimitsState:
        return self.limits

    @property
    def decision(self) -> CodingWorkerLimitsState:
        return self.limits

    @property
    def closed_limits(self) -> CodingWorkerLimitsState:
        return self.limits

    @property
    def closed_reason(self) -> CodingWorkerLimitsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_WORKER_LIMITS_SCHEMA,
            "limits_id": self.limits_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "limits": self.limits.value,
            "wall_clock_sec": self.wall_clock_sec,
            "memory_bytes": self.memory_bytes,
            "cpu_sec": self.cpu_sec,
            "reason": self.reason.value,
        }


WorkerLimitsState = CodingWorkerLimitsState
WorkerLimitsReason = CodingWorkerLimitsReason
CodingWorkerResourceLimits = CodingWorkerLimitsV1
CodingWorkerLimitsDecision = CodingWorkerLimitsState
CodingWorkerResourceLimitFacts = CodingWorkerLimitsFactsV1
CodingWorkerResourceLimitsState = CodingWorkerLimitsState
CodingWorkerResourceLimitsReason = CodingWorkerLimitsReason
CodingWorkerResourceLimitsFactsV1 = CodingWorkerLimitsFactsV1
CODING_WORKER_RESOURCE_LIMITS_SCHEMA = CODING_WORKER_LIMITS_SCHEMA


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object, object]:
    allowed = {
        "schema",
        "limits_id",
        "authenticated_turn_id",
        "limits",
        "state",
        "reason",
        "wall_clock_sec",
        "wall_clock_seconds",
        "memory_bytes",
        "memory",
        "cpu_sec",
        "cpu_seconds",
    }
    if set(value) - allowed:
        raise CodingWorkerLimitsError("limits facts contain unknown fields")
    if value.get("schema", CODING_WORKER_LIMITS_SCHEMA) != CODING_WORKER_LIMITS_SCHEMA:
        raise CodingWorkerLimitsError("limits schema is invalid")
    wall = value.get("wall_clock_sec", value.get("wall_clock_seconds", _MISSING))
    memory = value.get("memory_bytes", value.get("memory", _MISSING))
    cpu = value.get("cpu_sec", value.get("cpu_seconds", _MISSING))
    return wall, memory, cpu


def _facts(value: object) -> tuple[object, object, object]:
    if value is None:
        return _MISSING, _MISSING, _MISSING
    if isinstance(value, CodingWorkerLimitsFactsV1):
        return value.wall_clock_sec, value.memory_bytes, value.cpu_sec
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingWorkerLimitsError("limits facts must be a mapping or facts object")


def _result(
    limits_id: str,
    authenticated_turn_id: str,
    limits: CodingWorkerLimitsState,
    reason: CodingWorkerLimitsReason,
    *,
    values: tuple[int, int, int] | None = None,
) -> CodingWorkerLimitsV1:
    wall = memory = cpu = None
    if limits is CodingWorkerLimitsState.BOUNDED:
        if values is None:
            raise CodingWorkerLimitsError("bounded limits need values")
        wall, memory, cpu = values
    return CodingWorkerLimitsV1(
        limits_id=limits_id,
        authenticated_turn_id=authenticated_turn_id,
        limits=limits,
        wall_clock_sec=wall,
        memory_bytes=memory,
        cpu_sec=cpu,
        reason=reason,
    )


def build_coding_worker_limits(
    limits_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingWorkerLimitsFactsV1 | Mapping[str, object] | None = None,
    *,
    wall_clock_sec: object = _MISSING,
    memory_bytes: object = _MISSING,
    cpu_sec: object = _MISSING,
) -> CodingWorkerLimitsV1:
    """Build closed positive limits from supplied values only."""

    if isinstance(limits_id, Mapping):
        raw = limits_id
        limits_id = raw.get("limits_id", "limits:worker")
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        if facts is not None or any(
            value is not _MISSING for value in (wall_clock_sec, memory_bytes, cpu_sec)
        ):
            raise CodingWorkerLimitsError("limits mapping and explicit facts cannot be mixed")
        facts = raw
    _identifier(limits_id, field="limits_id", maximum=MAX_LIMITS_ID_CHARS)
    _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        explicit = (wall_clock_sec, memory_bytes, cpu_sec)
        raw_facts = explicit if any(value is not _MISSING for value in explicit) else _facts(facts)
        if any(value is not _MISSING for value in explicit) and facts is not None:
            raise CodingWorkerLimitsError("facts and explicit limit facts cannot both be supplied")
    except CodingWorkerLimitsError:
        return _result(
            cast(str, limits_id),
            cast(str, authenticated_turn_id),
            CodingWorkerLimitsState.BLOCKED,
            CodingWorkerLimitsReason.INVALID_FACTS,
        )
    if all(value is _MISSING or value is None for value in raw_facts):
        return _result(
            cast(str, limits_id),
            cast(str, authenticated_turn_id),
            CodingWorkerLimitsState.EMPTY,
            CodingWorkerLimitsReason.NO_FACTS,
        )
    wall_fact, memory_fact, cpu_fact = raw_facts
    if wall_fact is _MISSING or wall_fact is None:
        reason = CodingWorkerLimitsReason.MISSING_WALL_CLOCK
    elif memory_fact is _MISSING or memory_fact is None:
        reason = CodingWorkerLimitsReason.MISSING_MEMORY
    elif cpu_fact is _MISSING or cpu_fact is None:
        reason = CodingWorkerLimitsReason.MISSING_CPU
    else:
        reason = None
    if reason is not None:
        return _result(
            cast(str, limits_id), cast(str, authenticated_turn_id), CodingWorkerLimitsState.BLOCKED, reason
        )
    values = (wall_fact, memory_fact, cpu_fact)
    if any(type(value) is not int for value in values):
        return _result(
            cast(str, limits_id),
            cast(str, authenticated_turn_id),
            CodingWorkerLimitsState.BLOCKED,
            CodingWorkerLimitsReason.INVALID_FACTS,
        )
    integer_values = cast(tuple[int, int, int], values)
    if any(value <= 0 for value in integer_values):
        reason = CodingWorkerLimitsReason.NON_POSITIVE
    elif integer_values[0] > MAX_WALL_CLOCK_SEC:
        reason = CodingWorkerLimitsReason.WALL_CLOCK_LIMIT
    elif integer_values[1] > MAX_MEMORY_BYTES:
        reason = CodingWorkerLimitsReason.MEMORY_LIMIT
    elif integer_values[2] > MAX_CPU_SEC:
        reason = CodingWorkerLimitsReason.CPU_LIMIT
    else:
        reason = None
    if reason is not None:
        return _result(
            cast(str, limits_id), cast(str, authenticated_turn_id), CodingWorkerLimitsState.BLOCKED, reason
        )
    return _result(
        cast(str, limits_id),
        cast(str, authenticated_turn_id),
        CodingWorkerLimitsState.BOUNDED,
        CodingWorkerLimitsReason.LIMITS_BOUNDED,
        values=integer_values,
    )


build_coding_worker_resource_limits = build_coding_worker_limits


def validate_coding_worker_limits(value: Mapping[str, object]) -> bool:
    """Return whether a mapping is a valid serialized limits result."""

    try:
        if value.get("schema") != CODING_WORKER_LIMITS_SCHEMA:
            return False
        limits_id = cast(str, value.get("limits_id"))
        turn = cast(str, value.get("authenticated_turn_id"))
        state_value = value.get("limits", value.get("state"))
        if state_value in {
            CodingWorkerLimitsState.EMPTY.value,
            CodingWorkerLimitsState.BLOCKED.value,
        }:
            state = CodingWorkerLimitsState(state_value)
            reason = CodingWorkerLimitsReason(cast(str, value.get("reason")))
            result = CodingWorkerLimitsV1(limits_id, turn, state, None, None, None, reason)
        else:
            result = build_coding_worker_limits(limits_id, turn, value)
        return result.to_mapping() == dict(value)
    except (CodingWorkerLimitsError, TypeError, ValueError):
        return False
