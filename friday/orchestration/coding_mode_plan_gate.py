"""Pure plan-selection gate for Coding Mode.

The gate only consumes frozen intent, execute-claim and admission facts.  It
does not create an implementation plan and it does not execute one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from friday.orchestration.coding_create_admission import (
    CodingCreateAdmissionState,
    CodingCreateAdmissionV1,
    build_coding_create_admission,
)
from friday.orchestration.coding_mode_execute_claim import (
    CodingModeExecuteClaimState,
    CodingModeExecuteClaimV1,
    build_coding_mode_execute_claim,
)
from friday.orchestration.coding_mode_intent import (
    CodingModeIntentState,
    CodingModeIntentV1,
    build_coding_mode_intent,
)
from friday.orchestration.coding_upload_modification_admission import (
    CodingUploadModificationAdmissionState,
    CodingUploadModificationAdmissionV1,
    build_coding_upload_modification_admission,
)
from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
    build_coding_worker_admission,
)

CODING_MODE_PLAN_GATE_SCHEMA = "friday.coding-mode-plan-gate.v1"
MAX_GATE_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CodingModePlanGateError(ValueError):
    """A plan-gate identity or consumed fact is malformed."""


class CodingModePlanGateState(StrEnum):
    EMPTY = "empty"
    INSPECT_ONLY = "inspect_only"
    CREATE = "create"
    MODIFY = "modify"
    BLOCKED = "blocked"


class CodingModePlanGateReason(StrEnum):
    NO_FACTS = "no_facts"
    INSPECT_ONLY = "inspect_only"
    CREATE_ADMITTED = "create_admitted"
    MODIFY_ADMITTED = "modify_admitted"
    INTENT_BLOCKED = "intent_blocked"
    INTENT_EMPTY = "intent_empty"
    CREATE_REQUIRED = "create_required"
    MODIFY_REQUIRED = "modify_required"
    ADMISSION_EMPTY = "admission_empty"
    ADMISSION_NOT_ADMITTED = "admission_not_admitted"
    ADMISSION_BLOCKED = "admission_blocked"
    WORKER_REQUIRED = "worker_required"
    WORKER_NOT_ADMITTED = "worker_not_admitted"
    EXECUTE_CLAIM_BLOCKED = "execute_claim_blocked"
    TURN_MISMATCH = "turn_mismatch"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class CodingModePlanGateV1:
    """Immutable admitted plan shape for one Coding Mode turn."""

    gate_id: str
    authenticated_turn_id: str
    gate: CodingModePlanGateState
    admission_id: str | None
    plan_id: str | None
    reason: CodingModePlanGateReason

    def __post_init__(self) -> None:
        _identifier(self.gate_id, "gate_id", MAX_GATE_ID_CHARS)
        _identifier(self.authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
        gate = _state(self.gate)
        reason = _reason(self.reason)
        object.__setattr__(self, "gate", gate)
        object.__setattr__(self, "reason", reason)
        if gate in {CodingModePlanGateState.CREATE, CodingModePlanGateState.MODIFY}:
            if self.admission_id is None:
                raise CodingModePlanGateError("admitted_gate_missing_admission")
            _identifier(self.admission_id, "admission_id", 128)
            if self.plan_id is not None:
                _identifier(self.plan_id, "plan_id", 128)
        elif self.admission_id is not None or self.plan_id is not None:
            raise CodingModePlanGateError("non_admitted_gate_exposes_plan")

    @property
    def state(self) -> CodingModePlanGateState:
        return self.gate

    @property
    def decision(self) -> CodingModePlanGateState:
        return self.gate

    @property
    def plan(self) -> CodingModePlanGateState:
        return self.gate

    @property
    def closed_reason(self) -> CodingModePlanGateReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_MODE_PLAN_GATE_SCHEMA,
            "gate_id": self.gate_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "gate": self.gate.value,
            "admission_id": self.admission_id,
            "plan_id": self.plan_id,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class CodingModePlanGateFactsV1:
    """Frozen inputs for one plan-selection gate."""

    intent: object | None = None
    execute_claim: object | None = None
    create_admission: object | None = None
    modification_admission: object | None = None
    worker_admission: object | None = None


CodingModePlanGate = CodingModePlanGateV1
PlanGateState = CodingModePlanGateState
PlanGateReason = CodingModePlanGateReason
CodingModePlanGateFacts = CodingModePlanGateFactsV1


def _identifier(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingModePlanGateError(f"{field}_id_invalid")
    return cast(str, value)


def _state(value: object) -> CodingModePlanGateState:
    try:
        return (
            value if isinstance(value, CodingModePlanGateState) else CodingModePlanGateState(cast(str, value))
        )
    except (TypeError, ValueError) as exc:
        raise CodingModePlanGateError("gate_closed") from exc


def _reason(value: object) -> CodingModePlanGateReason:
    try:
        return (
            value
            if isinstance(value, CodingModePlanGateReason)
            else CodingModePlanGateReason(cast(str, value))
        )
    except (TypeError, ValueError) as exc:
        raise CodingModePlanGateError("reason_closed") from exc


def _intent(value: object, gate_id: str, turn: str) -> CodingModeIntentV1 | None:
    if isinstance(value, CodingModeIntentV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"intent", "state", "reason"}.intersection(value):
            return build_coding_mode_intent(value)
        return build_coding_mode_intent(f"{gate_id}:intent", turn, value)
    return None


def _execute(value: object, gate_id: str, turn: str) -> CodingModeExecuteClaimV1 | None:
    if isinstance(value, CodingModeExecuteClaimV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"claim", "state", "reason"}.intersection(value):
            return build_coding_mode_execute_claim(value)
        return build_coding_mode_execute_claim(f"{gate_id}:execute", turn, facts=value)
    return None


def _worker(value: object) -> CodingWorkerAdmissionV1 | None:
    if isinstance(value, CodingWorkerAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_worker_admission(value)
    return None


def _create(value: object, gate_id: str, turn: str) -> CodingCreateAdmissionV1 | None:
    if isinstance(value, CodingCreateAdmissionV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return None
    if {"admission", "state", "reason"}.intersection(value):
        return CodingCreateAdmissionV1(
            cast(str, value.get("admission_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingCreateAdmissionState, value.get("admission", value.get("state"))),
            cast(str | None, value.get("project_id")),
            cast(str | None, value.get("revision_selector")),
            cast(Any, value.get("reason")),
        )
    return build_coding_create_admission(
        cast(str, value.get("admission_id", f"{gate_id}:create")),
        cast(str, value.get("authenticated_turn_id", turn)),
        identity=value.get("identity"),
        prompt=value.get("prompt"),
        plan=value.get("plan"),
        scaffold=value.get("scaffold"),
    )


def _modify(value: object, gate_id: str, turn: str) -> CodingUploadModificationAdmissionV1 | None:
    if isinstance(value, CodingUploadModificationAdmissionV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return None
    if {"admission", "state", "reason"}.intersection(value):
        return CodingUploadModificationAdmissionV1(
            cast(str, value.get("admission_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingUploadModificationAdmissionState, value.get("admission", value.get("state"))),
            cast(str | None, value.get("project_id")),
            cast(str | None, value.get("revision_selector")),
            cast(Any, value.get("reason")),
        )
    return build_coding_upload_modification_admission(
        cast(str, value.get("admission_id", f"{gate_id}:modify")),
        cast(str, value.get("authenticated_turn_id", turn)),
        identity=value.get("identity"),
        inspect_report=value.get("inspect_report", value.get("inspect")),
        isolation=value.get("isolation"),
        plan=value.get("plan"),
        extract_admission=value.get("extract_admission"),
        extract_plan=value.get("extract_plan"),
        overwrite_plan=value.get("overwrite_plan"),
    )


def _result(
    gate_id: str,
    turn: str,
    state: CodingModePlanGateState,
    reason: CodingModePlanGateReason,
    *,
    admission_id: str | None = None,
    plan_id: str | None = None,
) -> CodingModePlanGateV1:
    if state not in {CodingModePlanGateState.CREATE, CodingModePlanGateState.MODIFY}:
        admission_id = None
        plan_id = None
    return CodingModePlanGateV1(gate_id, turn, state, admission_id, plan_id, reason)


def build_coding_mode_plan_gate(
    gate_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    intent: object = None,
    execute_claim: object = None,
    create_admission: object = None,
    modification_admission: object = None,
    *,
    worker_admission: object = None,
    create: object = None,
    modify: object = None,
    upload_modification_admission: object = None,
    facts: CodingModePlanGateFactsV1 | Mapping[str, object] | None = None,
) -> CodingModePlanGateV1:
    """Select inspect-only, create, or modify from closed admissions."""

    if isinstance(gate_id, Mapping):
        raw = gate_id
        allowed = {
            "schema",
            "gate_id",
            "authenticated_turn_id",
            "gate",
            "state",
            "intent",
            "execute_claim",
            "create_admission",
            "create",
            "modification_admission",
            "modify",
            "upload_modification_admission",
            "worker_admission",
            "admission_id",
            "plan_id",
            "reason",
        }
        if set(raw) - allowed:
            raise CodingModePlanGateError("gate_mapping_unknown_fields")
        if {"gate", "state", "reason"}.intersection(raw):
            required = {
                "schema",
                "gate_id",
                "authenticated_turn_id",
                "gate",
                "admission_id",
                "plan_id",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_MODE_PLAN_GATE_SCHEMA:
                raise CodingModePlanGateError("gate_mapping_serialized_invalid")
            return CodingModePlanGateV1(
                cast(str, raw.get("gate_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingModePlanGateState, raw.get("gate", raw.get("state"))),
                cast(str | None, raw.get("admission_id")),
                cast(str | None, raw.get("plan_id")),
                cast(CodingModePlanGateReason, raw.get("reason")),
            )
        gate_id = cast(str, raw.get("gate_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        intent = raw.get("intent")
        execute_claim = raw.get("execute_claim")
        create_admission = raw.get("create_admission", raw.get("create"))
        modification_admission = raw.get(
            "modification_admission",
            raw.get("modify", raw.get("upload_modification_admission")),
        )
        worker_admission = raw.get("worker_admission")
    gate_key = _identifier(gate_id, "gate_id", MAX_GATE_ID_CHARS)
    turn_key = _identifier(authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
    if facts is not None:
        if any(
            item is not None
            for item in (
                intent,
                execute_claim,
                create_admission,
                modification_admission,
                worker_admission,
                create,
                modify,
                upload_modification_admission,
            )
        ):
            raise CodingModePlanGateError("facts_and_explicit_gate_mixed")
        if isinstance(facts, CodingModePlanGateFactsV1):
            intent = facts.intent
            execute_claim = facts.execute_claim
            create_admission = facts.create_admission
            modification_admission = facts.modification_admission
            worker_admission = facts.worker_admission
        elif isinstance(facts, Mapping):
            allowed_facts = {
                "intent",
                "execute_claim",
                "create_admission",
                "create",
                "modification_admission",
                "modify",
                "upload_modification_admission",
                "worker_admission",
            }
            if set(facts) - allowed_facts:
                return _result(
                    gate_key,
                    turn_key,
                    CodingModePlanGateState.BLOCKED,
                    CodingModePlanGateReason.INVALID_FACTS,
                )
            intent = facts.get("intent")
            execute_claim = facts.get("execute_claim")
            create_admission = facts.get("create_admission", facts.get("create"))
            modification_admission = facts.get(
                "modification_admission",
                facts.get("modify", facts.get("upload_modification_admission")),
            )
            worker_admission = facts.get("worker_admission")
        else:
            return _result(
                gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.INVALID_FACTS
            )
    if create is not None:
        if create_admission is not None:
            raise CodingModePlanGateError("duplicate_create_admission")
        create_admission = create
    if modify is not None or upload_modification_admission is not None:
        if modification_admission is not None:
            raise CodingModePlanGateError("duplicate_modify_admission")
        modification_admission = modify if modify is not None else upload_modification_admission
    if all(
        item is None
        for item in (intent, execute_claim, create_admission, modification_admission, worker_admission)
    ):
        return _result(gate_key, turn_key, CodingModePlanGateState.EMPTY, CodingModePlanGateReason.NO_FACTS)
    try:
        intent_value = _intent(intent, gate_key, turn_key) if intent is not None else None
        execute_value = _execute(execute_claim, gate_key, turn_key) if execute_claim is not None else None
        create_value = _create(create_admission, gate_key, turn_key) if create_admission is not None else None
        modify_value = (
            _modify(modification_admission, gate_key, turn_key)
            if modification_admission is not None
            else None
        )
        worker_value = _worker(worker_admission) if worker_admission is not None else None
    except (TypeError, ValueError):
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.INVALID_FACTS
        )
    components = tuple(
        item
        for item in (intent_value, execute_value, create_value, modify_value, worker_value)
        if item is not None
    )
    if intent_value is None:
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.INVALID_FACTS
        )
    if any(getattr(item, "authenticated_turn_id", turn_key) != turn_key for item in components):
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.TURN_MISMATCH
        )

    def _component_blocked(item: object) -> bool:
        marker = getattr(item, "intent", None)
        if marker is None:
            marker = getattr(item, "claim", None)
        if marker is None:
            marker = getattr(item, "admission", None)
        return getattr(marker, "value", None) == "blocked"

    if any(_component_blocked(item) for item in components):
        if execute_value is not None and execute_value.claim is CodingModeExecuteClaimState.BLOCKED:
            return _result(
                gate_key,
                turn_key,
                CodingModePlanGateState.BLOCKED,
                CodingModePlanGateReason.EXECUTE_CLAIM_BLOCKED,
            )
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.ADMISSION_BLOCKED
        )
    if intent_value.intent is CodingModeIntentState.BLOCKED:
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.INTENT_BLOCKED
        )
    if intent_value.intent is CodingModeIntentState.EMPTY:
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.INTENT_EMPTY
        )
    if execute_value is not None and execute_value.claim is CodingModeExecuteClaimState.BLOCKED:
        return _result(
            gate_key,
            turn_key,
            CodingModePlanGateState.BLOCKED,
            CodingModePlanGateReason.EXECUTE_CLAIM_BLOCKED,
        )
    if worker_value is not None and worker_value.admission is CodingWorkerAdmissionState.BLOCKED:
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.ADMISSION_BLOCKED
        )
    if intent_value.intent is CodingModeIntentState.INSPECT:
        return _result(
            gate_key, turn_key, CodingModePlanGateState.INSPECT_ONLY, CodingModePlanGateReason.INSPECT_ONLY
        )
    if intent_value.intent is CodingModeIntentState.PROMPT:
        if create_value is None:
            return _result(
                gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.CREATE_REQUIRED
            )
        if create_value.admission is not CodingCreateAdmissionState.ADMITTED:
            reason = (
                CodingModePlanGateReason.ADMISSION_EMPTY
                if create_value.admission is CodingCreateAdmissionState.EMPTY
                else CodingModePlanGateReason.ADMISSION_NOT_ADMITTED
            )
            return _result(gate_key, turn_key, CodingModePlanGateState.BLOCKED, reason)
        if (
            execute_value is not None
            and execute_value.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED
            and (worker_value is None or worker_value.admission is not CodingWorkerAdmissionState.ADMITTED)
        ):
            return _result(
                gate_key,
                turn_key,
                CodingModePlanGateState.BLOCKED,
                CodingModePlanGateReason.WORKER_NOT_ADMITTED,
            )
        return _result(
            gate_key,
            turn_key,
            CodingModePlanGateState.CREATE,
            CodingModePlanGateReason.CREATE_ADMITTED,
            admission_id=create_value.admission_id,
        )
    if modify_value is None:
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.MODIFY_REQUIRED
        )
    if modify_value.admission is not CodingUploadModificationAdmissionState.ADMITTED:
        reason = (
            CodingModePlanGateReason.ADMISSION_EMPTY
            if modify_value.admission is CodingUploadModificationAdmissionState.EMPTY
            else CodingModePlanGateReason.ADMISSION_NOT_ADMITTED
        )
        return _result(gate_key, turn_key, CodingModePlanGateState.BLOCKED, reason)
    if (
        execute_value is not None
        and execute_value.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED
        and (worker_value is None or worker_value.admission is not CodingWorkerAdmissionState.ADMITTED)
    ):
        return _result(
            gate_key, turn_key, CodingModePlanGateState.BLOCKED, CodingModePlanGateReason.WORKER_NOT_ADMITTED
        )
    return _result(
        gate_key,
        turn_key,
        CodingModePlanGateState.MODIFY,
        CodingModePlanGateReason.MODIFY_ADMITTED,
        admission_id=modify_value.admission_id,
    )


build_mode_plan_gate = build_coding_mode_plan_gate


__all__ = [
    "CODING_MODE_PLAN_GATE_SCHEMA",
    "CodingModePlanGate",
    "CodingModePlanGateError",
    "CodingModePlanGateFacts",
    "CodingModePlanGateFactsV1",
    "CodingModePlanGateReason",
    "CodingModePlanGateState",
    "CodingModePlanGateV1",
    "PlanGateReason",
    "PlanGateState",
    "build_coding_mode_plan_gate",
    "build_mode_plan_gate",
]
