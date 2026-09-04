"""Compose prompt, identity, plan and scaffold into one create-admission.

Admission is a frozen gate.  It does not write a project, spawn a worker, or
open git.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_implementation_plan import (
    CodingImplementationPlanState,
    CodingImplementationPlanV1,
    build_coding_implementation_plan,
)
from friday.orchestration.coding_project_identity import (
    CodingProjectIdentityState,
    CodingProjectIdentityV1,
    build_coding_project_identity,
)
from friday.orchestration.coding_project_scaffold import (
    CodingProjectScaffoldState,
    CodingProjectScaffoldV1,
    build_coding_project_scaffold,
)
from friday.orchestration.coding_prompt_normalization import (
    CodingPromptNormalizationState,
    CodingPromptNormalizationV1,
    build_coding_prompt_normalization,
)

CODING_CREATE_ADMISSION_SCHEMA = "friday.coding-create-admission.v1"


class CodingCreateAdmissionError(ValueError):
    """A create-admission identity or composed input is malformed."""


class CodingCreateAdmissionState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingCreateAdmissionReason(StrEnum):
    NO_FACTS = "no_facts"
    ALL_GATES_ADMITTED = "all_gates_admitted"
    IDENTITY_NOT_IDENTIFIED = "identity_not_identified"
    PROMPT_NOT_NORMALIZED = "prompt_not_normalized"
    PLAN_NOT_PLANNED = "plan_not_planned"
    SCAFFOLD_NOT_SCAFFOLDED = "scaffold_not_scaffolded"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingCreateAdmissionError(f"{field}_{detail}")


def _state(value: object) -> CodingCreateAdmissionState:
    try:
        return CodingCreateAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingCreateAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingCreateAdmissionReason:
    try:
        return CodingCreateAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingCreateAdmissionError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingCreateAdmissionV1:
    admission_id: str
    authenticated_turn_id: str
    admission: CodingCreateAdmissionState
    project_id: str | None
    revision_selector: str | None
    reason: CodingCreateAdmissionReason

    def __post_init__(self) -> None:
        state = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", state)
        object.__setattr__(self, "reason", reason)
        if state is CodingCreateAdmissionState.ADMITTED:
            if self.project_id is None or self.revision_selector is None:
                _fail("admitted", "missing_identity")
        elif self.project_id is not None or self.revision_selector is not None:
            _fail("blocked_or_empty_admission", "exposed")

    @property
    def state(self) -> CodingCreateAdmissionState:
        return self.admission

    @property
    def closed_reason(self) -> CodingCreateAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_CREATE_ADMISSION_SCHEMA,
            "admission_id": self.admission_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "project_id": self.project_id,
            "revision_selector": self.revision_selector,
            "reason": self.reason.value,
        }


def _identity(value: object, *, admission_id: str, turn: str) -> CodingProjectIdentityV1 | None:
    if isinstance(value, CodingProjectIdentityV1):
        return value
    if isinstance(value, Mapping):
        return build_coding_project_identity(admission_id, turn, value)
    return None


def _prompt(value: object, *, admission_id: str, turn: str) -> CodingPromptNormalizationV1 | None:
    if isinstance(value, CodingPromptNormalizationV1):
        return value
    if isinstance(value, Mapping):
        return build_coding_prompt_normalization(admission_id, turn, value)
    return None


def _plan(value: object, *, admission_id: str, turn: str) -> CodingImplementationPlanV1 | None:
    if isinstance(value, CodingImplementationPlanV1):
        return value
    if isinstance(value, Mapping):
        return build_coding_implementation_plan(admission_id, turn, value.get("steps"))
    return None


def _scaffold(value: object, *, admission_id: str, turn: str) -> CodingProjectScaffoldV1 | None:
    if isinstance(value, CodingProjectScaffoldV1):
        return value
    if isinstance(value, Mapping):
        return build_coding_project_scaffold(admission_id, turn, value.get("files"))
    return None


def _result(
    admission_id: str,
    authenticated_turn_id: str,
    state: CodingCreateAdmissionState,
    reason: CodingCreateAdmissionReason,
    *,
    project_id: str | None = None,
    revision_selector: str | None = None,
) -> CodingCreateAdmissionV1:
    if state is not CodingCreateAdmissionState.ADMITTED:
        project_id = None
        revision_selector = None
    return CodingCreateAdmissionV1(
        admission_id=admission_id,
        authenticated_turn_id=authenticated_turn_id,
        admission=state,
        project_id=project_id,
        revision_selector=revision_selector,
        reason=reason,
    )


def build_coding_create_admission(
    admission_id: str,
    authenticated_turn_id: str,
    *,
    identity: object = None,
    prompt: object = None,
    plan: object = None,
    scaffold: object = None,
) -> CodingCreateAdmissionV1:
    """Admit create only when identity, prompt, plan and scaffold all pass."""

    if identity is None and prompt is None and plan is None and scaffold is None:
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.EMPTY,
            CodingCreateAdmissionReason.NO_FACTS,
        )
    try:
        identity_result = (
            _identity(identity, admission_id=admission_id, turn=authenticated_turn_id)
            if identity is not None
            else None
        )
        prompt_result = (
            _prompt(prompt, admission_id=admission_id, turn=authenticated_turn_id)
            if prompt is not None
            else None
        )
        plan_result = (
            _plan(plan, admission_id=admission_id, turn=authenticated_turn_id) if plan is not None else None
        )
        scaffold_result = (
            _scaffold(scaffold, admission_id=admission_id, turn=authenticated_turn_id)
            if scaffold is not None
            else None
        )
    except (TypeError, ValueError):
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.BLOCKED,
            CodingCreateAdmissionReason.INVALID_FACTS,
        )
    if identity_result is None or prompt_result is None or plan_result is None or scaffold_result is None:
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.BLOCKED,
            CodingCreateAdmissionReason.INVALID_FACTS,
        )
    for item in (identity_result, prompt_result, plan_result, scaffold_result):
        if item.authenticated_turn_id != authenticated_turn_id:
            return _result(
                admission_id,
                authenticated_turn_id,
                CodingCreateAdmissionState.BLOCKED,
                CodingCreateAdmissionReason.IDENTITY_MISMATCH,
            )
    if (
        identity_result.identity is CodingProjectIdentityState.EMPTY
        and prompt_result.prompt is CodingPromptNormalizationState.EMPTY
        and plan_result.plan is CodingImplementationPlanState.EMPTY
        and scaffold_result.scaffold is CodingProjectScaffoldState.EMPTY
    ):
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.EMPTY,
            CodingCreateAdmissionReason.NO_FACTS,
        )
    if identity_result.identity is not CodingProjectIdentityState.IDENTIFIED:
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.BLOCKED,
            CodingCreateAdmissionReason.IDENTITY_NOT_IDENTIFIED,
        )
    if prompt_result.prompt is not CodingPromptNormalizationState.NORMALIZED:
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.BLOCKED,
            CodingCreateAdmissionReason.PROMPT_NOT_NORMALIZED,
        )
    if plan_result.plan is not CodingImplementationPlanState.PLANNED:
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.BLOCKED,
            CodingCreateAdmissionReason.PLAN_NOT_PLANNED,
        )
    if scaffold_result.scaffold is not CodingProjectScaffoldState.SCAFFOLDED:
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingCreateAdmissionState.BLOCKED,
            CodingCreateAdmissionReason.SCAFFOLD_NOT_SCAFFOLDED,
        )
    return _result(
        admission_id,
        authenticated_turn_id,
        CodingCreateAdmissionState.ADMITTED,
        CodingCreateAdmissionReason.ALL_GATES_ADMITTED,
        project_id=identity_result.project_id,
        revision_selector=identity_result.revision_selector,
    )


admit_coding_create = build_coding_create_admission

__all__ = [
    "CODING_CREATE_ADMISSION_SCHEMA",
    "CodingCreateAdmissionError",
    "CodingCreateAdmissionReason",
    "CodingCreateAdmissionState",
    "CodingCreateAdmissionV1",
    "admit_coding_create",
    "build_coding_create_admission",
]
