"""Pure admission facts for the isolated Coding worker boundary.

The booleans in this module are supplied observations.  This contract does
not inspect a host to produce them.  A single unsafe observation blocks the
worker, and blocked results redact every isolation fact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

CODING_WORKER_ISOLATION_SCHEMA = "friday.coding-worker-isolation.v1"
MAX_ISOLATION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class CodingWorkerIsolationError(ValueError):
    """An isolation fact or directly constructed result is malformed."""


class CodingWorkerIsolationState(StrEnum):
    """Closed outcomes for the supplied isolation observations."""

    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingWorkerIsolationReason(StrEnum):
    """Non-sensitive reason for one isolation outcome."""

    NO_FACTS = "no_facts"
    SAFE_FACTS = "safe_facts"
    HOST_SECRETS_VISIBLE = "host_secrets_visible"
    DOCKER_SOCKET_PRESENT = "docker_socket_present"
    PRODUCTION_DATABASE_REACHABLE = "production_database_reachable"
    OWNER_SSH_KEYS_VISIBLE = "owner_ssh_keys_visible"
    MISSING_FACT = "missing_fact"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingWorkerIsolationError(f"{field} must be a bounded opaque identifier")
    return cast(str, value)


def _state(value: object) -> CodingWorkerIsolationState:
    if isinstance(value, CodingWorkerIsolationState):
        return value
    if type(value) is not str:
        raise CodingWorkerIsolationError("isolation must be a closed value")
    try:
        return CodingWorkerIsolationState(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerIsolationError("isolation must be a closed value") from exc


def _reason(value: object) -> CodingWorkerIsolationReason:
    if isinstance(value, CodingWorkerIsolationReason):
        return value
    if type(value) is not str:
        raise CodingWorkerIsolationError("isolation reason must be a closed value")
    try:
        return CodingWorkerIsolationReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerIsolationError("isolation reason must be a closed value") from exc


_FACT_NAMES = (
    "host_secrets_visible",
    "docker_socket_present",
    "production_database_reachable",
    "owner_ssh_keys_visible",
)


@dataclass(frozen=True, slots=True)
class CodingWorkerIsolationFactsV1:
    """Boolean isolation observations supplied by the caller."""

    host_secrets_visible: bool | None = None
    docker_socket_present: bool | None = None
    production_database_reachable: bool | None = None
    owner_ssh_keys_visible: bool | None = None


@dataclass(frozen=True, slots=True)
class CodingWorkerIsolationAdmissionV1:
    """Immutable isolation decision with unsafe blocked facts redacted."""

    isolation_id: str
    authenticated_turn_id: str
    isolation: CodingWorkerIsolationState
    host_secrets_visible: bool | None
    docker_socket_present: bool | None
    production_database_reachable: bool | None
    owner_ssh_keys_visible: bool | None
    reason: CodingWorkerIsolationReason

    def __post_init__(self) -> None:
        _identifier(self.isolation_id, field="isolation_id", maximum=MAX_ISOLATION_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        isolation = _state(self.isolation)
        reason = _reason(self.reason)
        object.__setattr__(self, "isolation", isolation)
        object.__setattr__(self, "reason", reason)
        facts = (
            self.host_secrets_visible,
            self.docker_socket_present,
            self.production_database_reachable,
            self.owner_ssh_keys_visible,
        )
        if isolation is CodingWorkerIsolationState.ADMITTED:
            if facts != (False, False, False, False):
                raise CodingWorkerIsolationError("admitted isolation must contain four false facts")
        elif any(fact is not None for fact in facts):
            raise CodingWorkerIsolationError("empty or blocked isolation cannot expose safety facts")

    @property
    def state(self) -> CodingWorkerIsolationState:
        return self.isolation

    @property
    def admission(self) -> CodingWorkerIsolationState:
        return self.isolation

    @property
    def closed_isolation(self) -> CodingWorkerIsolationState:
        return self.isolation

    @property
    def decision(self) -> CodingWorkerIsolationState:
        return self.isolation

    @property
    def closed_reason(self) -> CodingWorkerIsolationReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_WORKER_ISOLATION_SCHEMA,
            "isolation_id": self.isolation_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "isolation": self.isolation.value,
            "host_secrets_visible": self.host_secrets_visible,
            "docker_socket_present": self.docker_socket_present,
            "production_database_reachable": self.production_database_reachable,
            "owner_ssh_keys_visible": self.owner_ssh_keys_visible,
            "reason": self.reason.value,
        }


WorkerIsolationState = CodingWorkerIsolationState
WorkerIsolationReason = CodingWorkerIsolationReason
CodingWorkerIsolation = CodingWorkerIsolationAdmissionV1
CodingWorkerIsolationDecision = CodingWorkerIsolationState
CodingWorkerIsolationFacts = CodingWorkerIsolationFactsV1
CodingWorkerIsolationAdmissionState = CodingWorkerIsolationState
CodingWorkerIsolationAdmissionReason = CodingWorkerIsolationReason
CodingWorkerIsolationAdmissionFactsV1 = CodingWorkerIsolationFactsV1
CODING_WORKER_ISOLATION_ADMISSION_SCHEMA = CODING_WORKER_ISOLATION_SCHEMA


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object, object, object]:
    allowed = {
        "schema",
        "isolation_id",
        "authenticated_turn_id",
        "isolation",
        "state",
        "admission",
        "reason",
        "host_secrets_visible",
        "docker_socket_present",
        "production_database_reachable",
        "owner_ssh_keys_visible",
    }
    if set(value) - allowed:
        raise CodingWorkerIsolationError("isolation facts contain unknown fields")
    if value.get("schema", CODING_WORKER_ISOLATION_SCHEMA) != CODING_WORKER_ISOLATION_SCHEMA:
        raise CodingWorkerIsolationError("isolation schema is invalid")
    return tuple(value.get(name, _MISSING) for name in _FACT_NAMES)  # type: ignore[return-value]


def _facts(value: object) -> tuple[object, object, object, object]:
    if value is None:
        return (_MISSING, _MISSING, _MISSING, _MISSING)
    if isinstance(value, CodingWorkerIsolationFactsV1):
        return (
            value.host_secrets_visible,
            value.docker_socket_present,
            value.production_database_reachable,
            value.owner_ssh_keys_visible,
        )
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingWorkerIsolationError("isolation facts must be a mapping or facts object")


def _result(
    isolation_id: str,
    authenticated_turn_id: str,
    isolation: CodingWorkerIsolationState,
    reason: CodingWorkerIsolationReason,
    *,
    facts: tuple[bool | None, bool | None, bool | None, bool | None] = (None, None, None, None),
) -> CodingWorkerIsolationAdmissionV1:
    if isolation is not CodingWorkerIsolationState.ADMITTED:
        facts = (None, None, None, None)
    return CodingWorkerIsolationAdmissionV1(
        isolation_id=isolation_id,
        authenticated_turn_id=authenticated_turn_id,
        isolation=isolation,
        host_secrets_visible=facts[0],
        docker_socket_present=facts[1],
        production_database_reachable=facts[2],
        owner_ssh_keys_visible=facts[3],
        reason=reason,
    )


def build_coding_worker_isolation(
    isolation_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingWorkerIsolationFactsV1 | Mapping[str, object] | None = None,
    *,
    host_secrets_visible: object = _MISSING,
    docker_socket_present: object = _MISSING,
    production_database_reachable: object = _MISSING,
    owner_ssh_keys_visible: object = _MISSING,
) -> CodingWorkerIsolationAdmissionV1:
    """Build an isolation decision without observing or touching the host."""

    if isinstance(isolation_id, Mapping):
        raw = isolation_id
        isolation_id = raw.get("isolation_id", "isolation:worker")
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        if facts is not None or any(
            value is not _MISSING
            for value in (
                host_secrets_visible,
                docker_socket_present,
                production_database_reachable,
                owner_ssh_keys_visible,
            )
        ):
            raise CodingWorkerIsolationError("isolation mapping and explicit facts cannot be mixed")
        facts = raw
    _identifier(isolation_id, field="isolation_id", maximum=MAX_ISOLATION_ID_CHARS)
    _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        explicit = (
            host_secrets_visible,
            docker_socket_present,
            production_database_reachable,
            owner_ssh_keys_visible,
        )
        raw_facts = explicit if any(value is not _MISSING for value in explicit) else _facts(facts)
        if explicit != (_MISSING, _MISSING, _MISSING, _MISSING) and facts is not None:
            raise CodingWorkerIsolationError("facts and explicit isolation facts cannot both be supplied")
    except CodingWorkerIsolationError:
        return _result(
            cast(str, isolation_id),
            cast(str, authenticated_turn_id),
            CodingWorkerIsolationState.BLOCKED,
            CodingWorkerIsolationReason.INVALID_FACTS,
        )
    if all(value is _MISSING or value is None for value in raw_facts):
        return _result(
            cast(str, isolation_id),
            cast(str, authenticated_turn_id),
            CodingWorkerIsolationState.EMPTY,
            CodingWorkerIsolationReason.NO_FACTS,
        )
    if any(type(value) is not bool for value in raw_facts):
        return _result(
            cast(str, isolation_id),
            cast(str, authenticated_turn_id),
            CodingWorkerIsolationState.BLOCKED,
            CodingWorkerIsolationReason.INVALID_FACTS,
        )
    bool_facts = cast(tuple[bool, bool, bool, bool], raw_facts)
    for fact_name, fact_value in zip(_FACT_NAMES, bool_facts, strict=True):
        if fact_value:
            return _result(
                cast(str, isolation_id),
                cast(str, authenticated_turn_id),
                CodingWorkerIsolationState.BLOCKED,
                CodingWorkerIsolationReason(fact_name),
            )
    return _result(
        cast(str, isolation_id),
        cast(str, authenticated_turn_id),
        CodingWorkerIsolationState.ADMITTED,
        CodingWorkerIsolationReason.SAFE_FACTS,
        facts=bool_facts,
    )


build_coding_worker_isolation_admission = build_coding_worker_isolation


def validate_coding_worker_isolation(value: Mapping[str, object]) -> bool:
    """Return whether a mapping is a valid serialized result."""

    try:
        if value.get("schema") != CODING_WORKER_ISOLATION_SCHEMA:
            return False
        isolation_id = cast(str, value.get("isolation_id"))
        turn = cast(str, value.get("authenticated_turn_id"))
        state_value = value.get("isolation", value.get("admission", value.get("state")))
        if state_value in {
            CodingWorkerIsolationState.EMPTY.value,
            CodingWorkerIsolationState.BLOCKED.value,
        }:
            state = CodingWorkerIsolationState(state_value)
            reason = CodingWorkerIsolationReason(cast(str, value.get("reason")))
            result = CodingWorkerIsolationAdmissionV1(
                isolation_id, turn, state, None, None, None, None, reason
            )
        else:
            result = build_coding_worker_isolation(isolation_id, turn, value)
        return result.to_mapping() == dict(value)
    except (CodingWorkerIsolationError, TypeError, ValueError):
        return False
