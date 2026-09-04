"""Pure identity facts for one isolated Coding worker.

This module is deliberately a closed, body-free contract.  It consumes facts
that a caller already knows and never discovers a worker, revision, host, or
project by consulting the machine.  In particular, a host name is accepted
only as an input fact and is never present in an output identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

CODING_WORKER_IDENTITY_SCHEMA = "friday.coding-worker-identity.v1"
MAX_WORKER_ID_CHARS = 128
MAX_OPERATION_ID_CHARS = 128
MAX_PROJECT_ID_CHARS = 128
MAX_REVISION_SELECTOR_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RECENCY_SELECTORS = frozenset({"latest", "head", "newest", "current"})
_MISSING = object()


class CodingWorkerIdentityError(ValueError):
    """An identity argument or directly constructed result is malformed."""


class CodingWorkerIdentityState(StrEnum):
    """Closed outcomes for worker identity admission."""

    EMPTY = "empty"
    IDENTIFIED = "identified"
    BLOCKED = "blocked"


class CodingWorkerIdentityReason(StrEnum):
    """Non-sensitive reason for one identity outcome."""

    NO_FACTS = "no_facts"
    IDENTIFIED = "identified"
    MISSING_WORKER_ID = "missing_worker_id"
    MISSING_OPERATION_ID = "missing_operation_id"
    MISSING_PROJECT_ID = "missing_project_id"
    MISSING_REVISION_SELECTOR = "missing_revision_selector"
    RECENCY_REVISION_SELECTOR = "recency_revision_selector"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingWorkerIdentityError(f"{field} must be a bounded opaque identifier")
    return cast(str, value)


def _state(value: object) -> CodingWorkerIdentityState:
    if isinstance(value, CodingWorkerIdentityState):
        return value
    if type(value) is not str:
        raise CodingWorkerIdentityError("identity must be a closed value")
    try:
        return CodingWorkerIdentityState(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerIdentityError("identity must be a closed value") from exc


def _reason(value: object) -> CodingWorkerIdentityReason:
    if isinstance(value, CodingWorkerIdentityReason):
        return value
    if type(value) is not str:
        raise CodingWorkerIdentityError("identity reason must be a closed value")
    try:
        return CodingWorkerIdentityReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerIdentityError("identity reason must be a closed value") from exc


def _is_recency_selector(value: object) -> bool:
    return type(value) is str and value.strip().casefold() in _RECENCY_SELECTORS


@dataclass(frozen=True, slots=True)
class CodingWorkerIdentityFactsV1:
    """Facts supplied by a caller for one worker operation."""

    worker_id: str | None = None
    operation_id: str | None = None
    project_id: str | None = None
    revision_selector: str | None = None
    # Deliberately input-only.  A host name is never copied to a result.
    host_hostname: object | None = None


@dataclass(frozen=True, slots=True)
class CodingWorkerIdentityV1:
    """Immutable worker identity with host identity permanently redacted."""

    identity_id: str
    authenticated_turn_id: str
    identity: CodingWorkerIdentityState
    worker_id: str | None
    operation_id: str | None
    project_id: str | None
    revision_selector: str | None
    reason: CodingWorkerIdentityReason
    # Kept as an explicit safe field so consumers cannot accidentally infer
    # that the host name is available.  It is always None, including ADMITTED.
    host_hostname: None = None

    def __post_init__(self) -> None:
        _identifier(self.identity_id, field="identity_id", maximum=MAX_WORKER_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        identity = _state(self.identity)
        reason = _reason(self.reason)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "reason", reason)
        if self.host_hostname is not None:
            raise CodingWorkerIdentityError("host hostname is not part of worker identity")
        values = (self.worker_id, self.operation_id, self.project_id, self.revision_selector)
        if identity is not CodingWorkerIdentityState.IDENTIFIED:
            if any(value is not None for value in values):
                raise CodingWorkerIdentityError("empty or blocked identity cannot expose worker facts")
            return
        if any(value is None for value in values[:3]):
            raise CodingWorkerIdentityError("identified identity needs worker, operation, and project facts")
        _identifier(self.worker_id, field="worker_id", maximum=MAX_WORKER_ID_CHARS)
        _identifier(self.operation_id, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
        _identifier(self.project_id, field="project_id", maximum=MAX_PROJECT_ID_CHARS)
        if self.revision_selector is not None:
            _identifier(
                self.revision_selector,
                field="revision_selector",
                maximum=MAX_REVISION_SELECTOR_CHARS,
            )
            if _is_recency_selector(self.revision_selector):
                raise CodingWorkerIdentityError("identified identity cannot use a recency selector")

    @property
    def state(self) -> CodingWorkerIdentityState:
        return self.identity

    @property
    def closed_identity(self) -> CodingWorkerIdentityState:
        return self.identity

    @property
    def decision(self) -> CodingWorkerIdentityState:
        return self.identity

    @property
    def closed_reason(self) -> CodingWorkerIdentityReason:
        return self.reason

    @property
    def hostname(self) -> None:
        """The host name is intentionally unavailable at this boundary."""

        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_WORKER_IDENTITY_SCHEMA,
            "identity_id": self.identity_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "identity": self.identity.value,
            "worker_id": self.worker_id,
            "operation_id": self.operation_id,
            "project_id": self.project_id,
            "revision_selector": self.revision_selector,
            "reason": self.reason.value,
            "host_hostname": None,
        }


WorkerIdentityState = CodingWorkerIdentityState
WorkerIdentityReason = CodingWorkerIdentityReason
CodingWorkerIdentity = CodingWorkerIdentityV1
CodingWorkerIdentityDecision = CodingWorkerIdentityState
CodingWorkerFacts = CodingWorkerIdentityFactsV1
CodingWorkerIdentityFacts = CodingWorkerIdentityFactsV1


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object, object, object, object]:
    allowed = {
        "schema",
        "identity_id",
        "authenticated_turn_id",
        "identity",
        "state",
        "reason",
        "worker_id",
        "worker",
        "worker_identifier",
        "operation_id",
        "operation",
        "project_id",
        "project",
        "revision_selector",
        "revision",
        "revision_id",
        "host_hostname",
        "hostname",
        "host",
    }
    if set(value) - allowed:
        raise CodingWorkerIdentityError("identity facts contain unknown fields")
    if value.get("schema", CODING_WORKER_IDENTITY_SCHEMA) != CODING_WORKER_IDENTITY_SCHEMA:
        raise CodingWorkerIdentityError("identity schema is invalid")
    worker = value.get("worker_id", value.get("worker", value.get("worker_identifier", _MISSING)))
    operation = value.get("operation_id", value.get("operation", _MISSING))
    project = value.get("project_id", value.get("project", _MISSING))
    revision = value.get(
        "revision_selector",
        value.get("revision", value.get("revision_id", _MISSING)),
    )
    hostname = value.get("host_hostname", value.get("hostname", value.get("host", _MISSING)))
    return worker, operation, project, revision, hostname


def _facts(value: object) -> tuple[object, object, object, object, object]:
    if value is None:
        return _MISSING, _MISSING, _MISSING, _MISSING, _MISSING
    if isinstance(value, CodingWorkerIdentityFactsV1):
        return (
            value.worker_id,
            value.operation_id,
            value.project_id,
            value.revision_selector,
            value.host_hostname,
        )
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingWorkerIdentityError("identity facts must be a mapping or facts object")


def _result(
    identity_id: str,
    authenticated_turn_id: str,
    identity: CodingWorkerIdentityState,
    reason: CodingWorkerIdentityReason,
    *,
    worker_id: str | None = None,
    operation_id: str | None = None,
    project_id: str | None = None,
    revision_selector: str | None = None,
) -> CodingWorkerIdentityV1:
    if identity is not CodingWorkerIdentityState.IDENTIFIED:
        worker_id = operation_id = project_id = revision_selector = None
    return CodingWorkerIdentityV1(
        identity_id=identity_id,
        authenticated_turn_id=authenticated_turn_id,
        identity=identity,
        worker_id=worker_id,
        operation_id=operation_id,
        project_id=project_id,
        revision_selector=revision_selector,
        reason=reason,
    )


def build_coding_worker_identity(
    identity_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingWorkerIdentityFactsV1 | Mapping[str, object] | None = None,
    revision_selector: object = _MISSING,
    *,
    worker_id: object = _MISSING,
    operation_id: object = _MISSING,
    project_id: object = _MISSING,
) -> CodingWorkerIdentityV1:
    """Build an identity from already-supplied opaque facts only."""

    if isinstance(identity_id, Mapping):
        raw = identity_id
        identity_id = raw.get("identity_id", "identity:worker")
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        if facts is not None or any(
            value is not _MISSING for value in (worker_id, operation_id, project_id, revision_selector)
        ):
            raise CodingWorkerIdentityError("identity mapping and explicit facts cannot be mixed")
        facts = raw
    _identifier(identity_id, field="identity_id", maximum=MAX_WORKER_ID_CHARS)
    _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    identity_value = cast(str, identity_id)
    turn_value = cast(str, authenticated_turn_id)
    try:
        if any(value is not _MISSING for value in (worker_id, operation_id, project_id, revision_selector)):
            if facts is not None:
                raise CodingWorkerIdentityError("facts and explicit worker facts cannot both be supplied")
            worker_fact, operation_fact, project_fact, revision_fact, _ = (
                worker_id,
                operation_id,
                project_id,
                revision_selector,
                _MISSING,
            )
        else:
            worker_fact, operation_fact, project_fact, revision_fact, _ = _facts(facts)
    except CodingWorkerIdentityError:
        return _result(
            identity_value,
            turn_value,
            CodingWorkerIdentityState.BLOCKED,
            CodingWorkerIdentityReason.INVALID_FACTS,
        )

    values = (worker_fact, operation_fact, project_fact, revision_fact)
    if all(value is _MISSING or value is None for value in values):
        return _result(
            identity_value,
            turn_value,
            CodingWorkerIdentityState.EMPTY,
            CodingWorkerIdentityReason.NO_FACTS,
        )
    if _is_recency_selector(revision_fact):
        return _result(
            identity_value,
            turn_value,
            CodingWorkerIdentityState.BLOCKED,
            CodingWorkerIdentityReason.RECENCY_REVISION_SELECTOR,
        )
    if worker_fact is _MISSING or worker_fact is None:
        reason = CodingWorkerIdentityReason.MISSING_WORKER_ID
    elif operation_fact is _MISSING or operation_fact is None:
        reason = CodingWorkerIdentityReason.MISSING_OPERATION_ID
    elif project_fact is _MISSING or project_fact is None:
        reason = CodingWorkerIdentityReason.MISSING_PROJECT_ID
    else:
        reason = None
    if reason is not None:
        return _result(
            identity_value,
            turn_value,
            CodingWorkerIdentityState.BLOCKED,
            reason,
        )
    try:
        worker_value = _identifier(worker_fact, field="worker_id", maximum=MAX_WORKER_ID_CHARS)
        operation_value = _identifier(operation_fact, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
        project_value = _identifier(project_fact, field="project_id", maximum=MAX_PROJECT_ID_CHARS)
        revision_value = None
        if revision_fact is not _MISSING and revision_fact is not None:
            revision_value = _identifier(
                revision_fact,
                field="revision_selector",
                maximum=MAX_REVISION_SELECTOR_CHARS,
            )
    except CodingWorkerIdentityError:
        return _result(
            identity_value,
            turn_value,
            CodingWorkerIdentityState.BLOCKED,
            CodingWorkerIdentityReason.INVALID_FACTS,
        )
    if _is_recency_selector(revision_value):
        return _result(
            identity_value,
            turn_value,
            CodingWorkerIdentityState.BLOCKED,
            CodingWorkerIdentityReason.RECENCY_REVISION_SELECTOR,
        )
    return _result(
        identity_value,
        turn_value,
        CodingWorkerIdentityState.IDENTIFIED,
        CodingWorkerIdentityReason.IDENTIFIED,
        worker_id=worker_value,
        operation_id=operation_value,
        project_id=project_value,
        revision_selector=revision_value,
    )


def validate_coding_worker_identity(value: Mapping[str, object]) -> bool:
    """Return whether a serialized identity is a valid closed result."""

    try:
        if value.get("schema") != CODING_WORKER_IDENTITY_SCHEMA:
            return False
        identity_id = cast(str, value.get("identity_id"))
        turn = cast(str, value.get("authenticated_turn_id"))
        state_value = value.get("identity", value.get("state"))
        if state_value in {CodingWorkerIdentityState.EMPTY.value, CodingWorkerIdentityState.BLOCKED.value}:
            state = CodingWorkerIdentityState(state_value)
            reason = CodingWorkerIdentityReason(cast(str, value.get("reason")))
            result = CodingWorkerIdentityV1(identity_id, turn, state, None, None, None, None, reason)
        else:
            result = build_coding_worker_identity(identity_id, turn, value)
        return result.to_mapping() == dict(value)
    except (CodingWorkerIdentityError, TypeError, ValueError):
        return False
