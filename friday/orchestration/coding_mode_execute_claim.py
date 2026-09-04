"""Pure static/execute decision for Coding Mode.

This contract records a claim only from already-admitted facts.  It never
starts, supervises, or probes a process.  Static inspection remains available
without a worker; build/test/execute claims require an admitted worker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from friday.orchestration.coding_mode_intent import (
    CodingModeIntentState,
    CodingModeIntentV1,
    build_coding_mode_intent,
)
from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
    build_coding_worker_admission,
)

CODING_MODE_EXECUTE_CLAIM_SCHEMA = "friday.coding-mode-execute-claim.v1"
MAX_CLAIM_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CodingModeExecuteClaimError(ValueError):
    """An execute-claim identity or supplied fact is malformed."""


class CodingModeExecuteClaimState(StrEnum):
    EMPTY = "empty"
    STATIC = "static"
    EXECUTE_CLAIMED = "execute_claimed"
    BLOCKED = "blocked"


class CodingModeExecuteClaimReason(StrEnum):
    NO_FACTS = "no_facts"
    STATIC_INSPECTION = "static_inspection"
    EXECUTE_CLAIMED = "execute_claimed"
    WORKER_REQUIRED = "worker_required"
    WORKER_NOT_ADMITTED = "worker_not_admitted"
    WORKER_BLOCKED = "worker_blocked"
    INTENT_BLOCKED = "intent_blocked"
    INTENT_EMPTY = "intent_empty"
    TURN_MISMATCH = "turn_mismatch"
    OPERATION_INVALID = "operation_invalid"
    INVALID_FACTS = "invalid_facts"


class CodingModeExecuteOperation(StrEnum):
    INSPECT = "inspect"
    STATIC = "static"
    BUILD = "build"
    TEST = "test"
    EXECUTE = "execute"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class CodingModeExecuteClaimV1:
    """Immutable decision about whether an execution effect may be claimed."""

    claim_id: str
    authenticated_turn_id: str
    claim: CodingModeExecuteClaimState
    operation: CodingModeExecuteOperation | None
    worker_admission_id: str | None
    reason: CodingModeExecuteClaimReason

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id", MAX_CLAIM_ID_CHARS)
        _identifier(self.authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
        claim = _state(self.claim)
        reason = _reason(self.reason)
        operation = _operation(self.operation) if self.operation is not None else None
        object.__setattr__(self, "claim", claim)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "operation", operation)
        if claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED:
            if (
                operation
                not in {
                    CodingModeExecuteOperation.BUILD,
                    CodingModeExecuteOperation.TEST,
                    CodingModeExecuteOperation.EXECUTE,
                    CodingModeExecuteOperation.RUN,
                }
                or self.worker_admission_id is None
            ):
                raise CodingModeExecuteClaimError("execute_claim_missing_facts")
            _identifier(self.worker_admission_id, "worker_admission_id", 128)
        elif self.worker_admission_id is not None:
            raise CodingModeExecuteClaimError("non_execute_worker_exposed")

    @property
    def state(self) -> CodingModeExecuteClaimState:
        return self.claim

    @property
    def decision(self) -> CodingModeExecuteClaimState:
        return self.claim

    @property
    def execute(self) -> bool:
        return self.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED

    @property
    def closed_reason(self) -> CodingModeExecuteClaimReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_MODE_EXECUTE_CLAIM_SCHEMA,
            "claim_id": self.claim_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "claim": self.claim.value,
            "operation": self.operation.value if self.operation is not None else None,
            "worker_admission_id": self.worker_admission_id,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class CodingModeExecuteClaimFactsV1:
    """Frozen inputs for one static or execute claim."""

    intent: object | None = None
    worker_admission: object | None = None
    operation: object = CodingModeExecuteOperation.INSPECT
    execute_requested: object | None = None


CodingModeExecuteClaim = CodingModeExecuteClaimV1
CodingModeExecuteClaimFacts = CodingModeExecuteClaimFactsV1
ExecuteClaimState = CodingModeExecuteClaimState
ExecuteClaimReason = CodingModeExecuteClaimReason


def _identifier(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingModeExecuteClaimError(f"{field}_id_invalid")
    return cast(str, value)


def _state(value: object) -> CodingModeExecuteClaimState:
    try:
        return (
            value
            if isinstance(value, CodingModeExecuteClaimState)
            else CodingModeExecuteClaimState(cast(str, value))
        )
    except (TypeError, ValueError) as exc:
        raise CodingModeExecuteClaimError("claim_closed") from exc


def _reason(value: object) -> CodingModeExecuteClaimReason:
    try:
        return (
            value
            if isinstance(value, CodingModeExecuteClaimReason)
            else CodingModeExecuteClaimReason(cast(str, value))
        )
    except (TypeError, ValueError) as exc:
        raise CodingModeExecuteClaimError("reason_closed") from exc


def _operation(value: object) -> CodingModeExecuteOperation:
    if isinstance(value, CodingModeExecuteOperation):
        return value
    if type(value) is not str:
        raise CodingModeExecuteClaimError("operation_invalid")
    try:
        return CodingModeExecuteOperation(value.strip().casefold())
    except ValueError as exc:
        raise CodingModeExecuteClaimError("operation_invalid") from exc


def _intent(value: object, claim_id: str, turn: str) -> CodingModeIntentV1 | None:
    if isinstance(value, CodingModeIntentV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"intent", "state", "reason"}.intersection(value):
            return build_coding_mode_intent(value)
        return build_coding_mode_intent(f"{claim_id}:intent", turn, value)
    return None


def _worker(value: object) -> CodingWorkerAdmissionV1 | None:
    if isinstance(value, CodingWorkerAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_worker_admission(value)
    return None


def _result(
    claim_id: str,
    turn: str,
    state: CodingModeExecuteClaimState,
    reason: CodingModeExecuteClaimReason,
    *,
    operation: CodingModeExecuteOperation | None = None,
    worker_admission_id: str | None = None,
) -> CodingModeExecuteClaimV1:
    if state is not CodingModeExecuteClaimState.EXECUTE_CLAIMED:
        worker_admission_id = None
    return CodingModeExecuteClaimV1(claim_id, turn, state, operation, worker_admission_id, reason)


def build_coding_mode_execute_claim(
    claim_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    intent: object = None,
    worker_admission: object = None,
    *,
    worker: object = None,
    operation: object = CodingModeExecuteOperation.INSPECT,
    execute_requested: object = None,
    facts: CodingModeExecuteClaimFactsV1 | Mapping[str, object] | None = None,
) -> CodingModeExecuteClaimV1:
    """Build STATIC or EXECUTE_CLAIMED from supplied intent and worker facts."""

    if isinstance(claim_id, Mapping):
        raw = claim_id
        allowed = {
            "schema",
            "claim_id",
            "authenticated_turn_id",
            "claim",
            "state",
            "intent",
            "worker_admission",
            "worker",
            "operation",
            "execute_requested",
            "worker_admission_id",
            "reason",
        }
        if set(raw) - allowed:
            raise CodingModeExecuteClaimError("claim_mapping_unknown_fields")
        if {"claim", "state", "reason"}.intersection(raw):
            required = {
                "schema",
                "claim_id",
                "authenticated_turn_id",
                "claim",
                "operation",
                "worker_admission_id",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_MODE_EXECUTE_CLAIM_SCHEMA:
                raise CodingModeExecuteClaimError("claim_mapping_serialized_invalid")
            return CodingModeExecuteClaimV1(
                cast(str, raw.get("claim_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingModeExecuteClaimState, raw.get("claim", raw.get("state"))),
                cast(CodingModeExecuteOperation | None, raw.get("operation")),
                cast(str | None, raw.get("worker_admission_id")),
                cast(CodingModeExecuteClaimReason, raw.get("reason")),
            )
        claim_id = cast(str, raw.get("claim_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        intent = raw.get("intent")
        worker_admission = raw.get("worker_admission", raw.get("worker"))
        operation = raw.get("operation", operation)
        execute_requested = raw.get("execute_requested", execute_requested)
    claim_key = _identifier(claim_id, "claim_id", MAX_CLAIM_ID_CHARS)
    turn_key = _identifier(authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
    if facts is not None:
        if (
            intent is not None
            or worker_admission is not None
            or worker is not None
            or execute_requested is not None
            or operation != CodingModeExecuteOperation.INSPECT
        ):
            raise CodingModeExecuteClaimError("facts_and_explicit_claim_mixed")
        if isinstance(facts, CodingModeExecuteClaimFactsV1):
            intent = facts.intent
            worker_admission = facts.worker_admission
            operation = facts.operation
            execute_requested = facts.execute_requested
        elif isinstance(facts, Mapping):
            allowed_facts = {"intent", "worker_admission", "worker", "operation", "execute_requested"}
            if set(facts) - allowed_facts:
                return _result(
                    claim_key,
                    turn_key,
                    CodingModeExecuteClaimState.BLOCKED,
                    CodingModeExecuteClaimReason.INVALID_FACTS,
                )
            intent = facts.get("intent")
            worker_admission = facts.get("worker_admission", facts.get("worker"))
            operation = facts.get("operation", CodingModeExecuteOperation.INSPECT)
            execute_requested = facts.get("execute_requested")
        else:
            return _result(
                claim_key,
                turn_key,
                CodingModeExecuteClaimState.BLOCKED,
                CodingModeExecuteClaimReason.INVALID_FACTS,
            )
    if worker is not None:
        if worker_admission is not None:
            raise CodingModeExecuteClaimError("duplicate_worker")
        worker_admission = worker
    if (
        intent is None
        and worker_admission is None
        and execute_requested is None
        and operation
        in {
            CodingModeExecuteOperation.INSPECT,
            CodingModeExecuteOperation.STATIC,
            "inspect",
            "static",
        }
    ):
        return _result(
            claim_key, turn_key, CodingModeExecuteClaimState.EMPTY, CodingModeExecuteClaimReason.NO_FACTS
        )
    try:
        intent_value = _intent(intent, claim_key, turn_key) if intent is not None else None
        worker_value = _worker(worker_admission) if worker_admission is not None else None
        operation_value = _operation(operation)
    except (TypeError, ValueError):
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.INVALID_FACTS,
        )
    if intent_value is None:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.INVALID_FACTS,
        )
    if intent_value.authenticated_turn_id != turn_key:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.TURN_MISMATCH,
        )
    if intent_value.intent is CodingModeIntentState.BLOCKED:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.INTENT_BLOCKED,
        )
    if intent_value.intent is CodingModeIntentState.EMPTY:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.INTENT_EMPTY,
        )
    if worker_value is not None and worker_value.authenticated_turn_id != turn_key:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.TURN_MISMATCH,
        )
    if worker_value is not None and worker_value.admission is CodingWorkerAdmissionState.BLOCKED:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.WORKER_BLOCKED,
        )
    try:
        requested = (
            execute_requested
            if execute_requested is not None
            else operation_value
            not in {
                CodingModeExecuteOperation.INSPECT,
                CodingModeExecuteOperation.STATIC,
            }
        )
        if type(requested) is not bool:
            raise CodingModeExecuteClaimError("execute_requested_invalid")
    except CodingModeExecuteClaimError:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.INVALID_FACTS,
        )
    if not requested:
        if operation_value not in {CodingModeExecuteOperation.INSPECT, CodingModeExecuteOperation.STATIC}:
            return _result(
                claim_key,
                turn_key,
                CodingModeExecuteClaimState.BLOCKED,
                CodingModeExecuteClaimReason.OPERATION_INVALID,
            )
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.STATIC,
            CodingModeExecuteClaimReason.STATIC_INSPECTION,
            operation=operation_value,
        )
    if worker_value is None:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.WORKER_REQUIRED,
        )
    if worker_value.admission is not CodingWorkerAdmissionState.ADMITTED:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.WORKER_NOT_ADMITTED,
        )
    if operation_value in {CodingModeExecuteOperation.INSPECT, CodingModeExecuteOperation.STATIC}:
        return _result(
            claim_key,
            turn_key,
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimReason.OPERATION_INVALID,
        )
    return _result(
        claim_key,
        turn_key,
        CodingModeExecuteClaimState.EXECUTE_CLAIMED,
        CodingModeExecuteClaimReason.EXECUTE_CLAIMED,
        operation=operation_value,
        worker_admission_id=worker_value.admission_id,
    )


build_mode_execute_claim = build_coding_mode_execute_claim


__all__ = [
    "CODING_MODE_EXECUTE_CLAIM_SCHEMA",
    "CodingModeExecuteClaim",
    "CodingModeExecuteClaimError",
    "CodingModeExecuteClaimFacts",
    "CodingModeExecuteClaimFactsV1",
    "CodingModeExecuteClaimReason",
    "CodingModeExecuteClaimState",
    "CodingModeExecuteClaimV1",
    "CodingModeExecuteOperation",
    "ExecuteClaimReason",
    "ExecuteClaimState",
    "build_coding_mode_execute_claim",
    "build_mode_execute_claim",
]
