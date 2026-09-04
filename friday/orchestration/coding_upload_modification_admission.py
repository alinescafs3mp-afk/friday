"""Compose inspect, isolation, edit plan and optional extract family.

Admission is a frozen gate.  It does not write a project, extract an archive,
open paths, or spawn a coding worker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_archive_extract_admission import (
    CodingArchiveExtractAdmissionState,
    CodingArchiveExtractAdmissionV1,
    build_coding_archive_extract_admission,
)
from friday.orchestration.coding_archive_extract_plan import (
    CodingArchiveExtractPlanState,
    CodingArchiveExtractPlanV1,
    build_coding_archive_extract_plan,
)
from friday.orchestration.coding_archive_overwrite_plan import (
    CodingArchiveOverwritePlanState,
    CodingArchiveOverwritePlanV1,
    build_coding_archive_overwrite_plan,
)
from friday.orchestration.coding_implementation_plan import (
    CodingImplementationPlanState,
    CodingImplementationPlanV1,
    CodingPlanAction,
    build_coding_implementation_plan,
)
from friday.orchestration.coding_inspect_hazards import CodingInspectHazardsState
from friday.orchestration.coding_inspect_report import (
    CodingInspectReportState,
    CodingInspectReportV1,
    build_coding_inspect_report,
)
from friday.orchestration.coding_project_identity import (
    CodingProjectIdentityState,
    CodingProjectIdentityV1,
    build_coding_project_identity,
)
from friday.orchestration.coding_project_isolation_admission import (
    CodingProjectIsolationAdmissionState,
    CodingProjectIsolationAdmissionV1,
    build_coding_project_isolation_admission,
)

CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA = "friday.coding-upload-modification-admission.v1"


class CodingUploadModificationAdmissionError(ValueError):
    """A modification-admission identity or composed input is malformed."""


class CodingUploadModificationAdmissionState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingUploadModificationAdmissionReason(StrEnum):
    NO_FACTS = "no_facts"
    ALL_GATES_ADMITTED = "all_gates_admitted"
    IDENTITY_NOT_IDENTIFIED = "identity_not_identified"
    INSPECT_NOT_INSPECTED = "inspect_not_inspected"
    HAZARDS_PRESENT = "hazards_present"
    ISOLATION_NOT_ADMITTED = "isolation_not_admitted"
    PLAN_NOT_PLANNED = "plan_not_planned"
    PLAN_HAS_NO_EDIT = "plan_has_no_edit"
    EXTRACT_FAMILY_INCOMPLETE = "extract_family_incomplete"
    EXTRACT_NOT_ADMITTED = "extract_not_admitted"
    EXTRACT_NOT_PLANNED = "extract_not_planned"
    OVERWRITE_NOT_CLEAR = "overwrite_not_clear"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingUploadModificationAdmissionError(f"{field}_{detail}")


def _state(value: object) -> CodingUploadModificationAdmissionState:
    try:
        return CodingUploadModificationAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingUploadModificationAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingUploadModificationAdmissionReason:
    try:
        return CodingUploadModificationAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingUploadModificationAdmissionError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingUploadModificationAdmissionV1:
    admission_id: str
    authenticated_turn_id: str
    admission: CodingUploadModificationAdmissionState
    project_id: str | None
    revision_selector: str | None
    reason: CodingUploadModificationAdmissionReason

    def __post_init__(self) -> None:
        state = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", state)
        object.__setattr__(self, "reason", reason)
        if state is CodingUploadModificationAdmissionState.ADMITTED:
            if self.project_id is None or self.revision_selector is None:
                _fail("admitted", "missing_identity")
        elif self.project_id is not None or self.revision_selector is not None:
            _fail("blocked_or_empty_admission", "exposed")

    @property
    def state(self) -> CodingUploadModificationAdmissionState:
        return self.admission

    @property
    def closed_reason(self) -> CodingUploadModificationAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA,
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


def _inspect(value: object, *, admission_id: str, turn: str) -> CodingInspectReportV1 | None:
    if isinstance(value, CodingInspectReportV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_inspect_report(
            admission_id,
            turn,
            value.get("tree"),
            value.get("inspection"),
            value.get("hazards"),
            value.get("toolchain_hint"),
            members=value.get("members") if "members" in value else None,
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return build_coding_inspect_report(admission_id, turn, members=value)
    return None


def _isolation(value: object) -> CodingProjectIsolationAdmissionV1 | None:
    if isinstance(value, CodingProjectIsolationAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_project_isolation_admission(value)
    return None


def _plan(value: object, *, admission_id: str, turn: str) -> CodingImplementationPlanV1 | None:
    if isinstance(value, CodingImplementationPlanV1):
        return value
    if isinstance(value, Mapping):
        return build_coding_implementation_plan(admission_id, turn, value.get("steps"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return build_coding_implementation_plan(admission_id, turn, value)
    return None


def _extract_admission(value: object) -> CodingArchiveExtractAdmissionV1 | None:
    if isinstance(value, CodingArchiveExtractAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_archive_extract_admission(value)
    return None


def _extract_plan(value: object) -> CodingArchiveExtractPlanV1 | None:
    if isinstance(value, CodingArchiveExtractPlanV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_archive_extract_plan(value)
    return None


def _overwrite(value: object) -> CodingArchiveOverwritePlanV1 | None:
    if isinstance(value, CodingArchiveOverwritePlanV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_archive_overwrite_plan(value)
    return None


def _result(
    admission_id: str,
    authenticated_turn_id: str,
    state: CodingUploadModificationAdmissionState,
    reason: CodingUploadModificationAdmissionReason,
    *,
    project_id: str | None = None,
    revision_selector: str | None = None,
) -> CodingUploadModificationAdmissionV1:
    if state is not CodingUploadModificationAdmissionState.ADMITTED:
        project_id = None
        revision_selector = None
    return CodingUploadModificationAdmissionV1(
        admission_id=admission_id,
        authenticated_turn_id=authenticated_turn_id,
        admission=state,
        project_id=project_id,
        revision_selector=revision_selector,
        reason=reason,
    )


def _blocked(
    admission_id: str,
    authenticated_turn_id: str,
    reason: CodingUploadModificationAdmissionReason,
) -> CodingUploadModificationAdmissionV1:
    return _result(
        admission_id,
        authenticated_turn_id,
        CodingUploadModificationAdmissionState.BLOCKED,
        reason,
    )


def build_coding_upload_modification_admission(
    admission_id: str,
    authenticated_turn_id: str,
    *,
    identity: object = None,
    inspect_report: object = None,
    isolation: object = None,
    plan: object = None,
    extract_admission: object = None,
    extract_plan: object = None,
    overwrite_plan: object = None,
) -> CodingUploadModificationAdmissionV1:
    """Admit uploaded-project edits only when inspect, isolation and an edit plan pass.

    The extract family is optional.  If any extract fact is supplied, admission,
    extract plan and a clear overwrite plan must all pass.  Mapped-tree edits
    omit the extract family.  No archive is opened and no worker is spawned.
    """

    supplied = (
        identity,
        inspect_report,
        isolation,
        plan,
        extract_admission,
        extract_plan,
        overwrite_plan,
    )
    if all(item is None for item in supplied):
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionState.EMPTY,
            CodingUploadModificationAdmissionReason.NO_FACTS,
        )
    try:
        identity_result = (
            _identity(identity, admission_id=admission_id, turn=authenticated_turn_id)
            if identity is not None
            else None
        )
        inspect_result = (
            _inspect(inspect_report, admission_id=admission_id, turn=authenticated_turn_id)
            if inspect_report is not None
            else None
        )
        isolation_result = _isolation(isolation) if isolation is not None else None
        plan_result = (
            _plan(plan, admission_id=admission_id, turn=authenticated_turn_id) if plan is not None else None
        )
        extract_admission_result = (
            _extract_admission(extract_admission) if extract_admission is not None else None
        )
        extract_plan_result = _extract_plan(extract_plan) if extract_plan is not None else None
        overwrite_result = _overwrite(overwrite_plan) if overwrite_plan is not None else None
    except (TypeError, ValueError):
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.INVALID_FACTS,
        )
    required = (identity_result, inspect_result, isolation_result, plan_result)
    if any(item is None for item in required):
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.INVALID_FACTS,
        )
    extract_supplied = extract_admission is not None or extract_plan is not None or overwrite_plan is not None
    if extract_supplied and (
        extract_admission_result is None or extract_plan_result is None or overwrite_result is None
    ):
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.EXTRACT_FAMILY_INCOMPLETE,
        )
    turns = [
        identity_result.authenticated_turn_id,
        inspect_result.authenticated_turn_id,
        isolation_result.authenticated_turn_id,
        plan_result.authenticated_turn_id,
    ]
    if extract_supplied:
        turns.extend(
            (
                extract_admission_result.authenticated_turn_id,
                extract_plan_result.authenticated_turn_id,
                overwrite_result.authenticated_turn_id,
            )
        )
    if any(turn != authenticated_turn_id for turn in turns):
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.IDENTITY_MISMATCH,
        )
    if (
        identity_result.identity is CodingProjectIdentityState.EMPTY
        and inspect_result.report is CodingInspectReportState.EMPTY
        and isolation_result.admission is CodingProjectIsolationAdmissionState.EMPTY
        and plan_result.plan is CodingImplementationPlanState.EMPTY
        and not extract_supplied
    ):
        return _result(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionState.EMPTY,
            CodingUploadModificationAdmissionReason.NO_FACTS,
        )
    if identity_result.identity is not CodingProjectIdentityState.IDENTIFIED:
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.IDENTITY_NOT_IDENTIFIED,
        )
    if inspect_result.report is not CodingInspectReportState.INSPECTED:
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.INSPECT_NOT_INSPECTED,
        )
    hazards = inspect_result.hazards
    if hazards is None or hazards.hazards is not CodingInspectHazardsState.CLEAR:
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.HAZARDS_PRESENT,
        )
    if isolation_result.admission is not CodingProjectIsolationAdmissionState.ADMITTED:
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.ISOLATION_NOT_ADMITTED,
        )
    if plan_result.plan is not CodingImplementationPlanState.PLANNED:
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.PLAN_NOT_PLANNED,
        )
    if all(step.action is not CodingPlanAction.EDIT for step in plan_result.steps):
        return _blocked(
            admission_id,
            authenticated_turn_id,
            CodingUploadModificationAdmissionReason.PLAN_HAS_NO_EDIT,
        )
    if extract_supplied:
        if extract_admission_result.admission is not CodingArchiveExtractAdmissionState.ADMITTED:
            return _blocked(
                admission_id,
                authenticated_turn_id,
                CodingUploadModificationAdmissionReason.EXTRACT_NOT_ADMITTED,
            )
        if extract_plan_result.plan is not CodingArchiveExtractPlanState.PLANNED:
            return _blocked(
                admission_id,
                authenticated_turn_id,
                CodingUploadModificationAdmissionReason.EXTRACT_NOT_PLANNED,
            )
        if overwrite_result.plan is not CodingArchiveOverwritePlanState.CLEAR:
            return _blocked(
                admission_id,
                authenticated_turn_id,
                CodingUploadModificationAdmissionReason.OVERWRITE_NOT_CLEAR,
            )
    return _result(
        admission_id,
        authenticated_turn_id,
        CodingUploadModificationAdmissionState.ADMITTED,
        CodingUploadModificationAdmissionReason.ALL_GATES_ADMITTED,
        project_id=identity_result.project_id,
        revision_selector=identity_result.revision_selector,
    )


admit_coding_upload_modification = build_coding_upload_modification_admission


def validate_coding_upload_modification_admission(value: object) -> bool:
    if isinstance(value, CodingUploadModificationAdmissionV1):
        try:
            value.__post_init__()
        except (TypeError, ValueError):
            return False
        return True
    if not isinstance(value, Mapping):
        return False
    allowed = {
        "schema",
        "admission_id",
        "authenticated_turn_id",
        "admission",
        "project_id",
        "revision_selector",
        "reason",
    }
    if set(value) - allowed:
        return False
    if value.get("schema", CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA) != (
        CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA
    ):
        return False
    try:
        CodingUploadModificationAdmissionV1(
            admission_id=cast(str, value.get("admission_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            admission=cast(CodingUploadModificationAdmissionState, value.get("admission")),
            project_id=cast(str | None, value.get("project_id")),
            revision_selector=cast(str | None, value.get("revision_selector")),
            reason=cast(CodingUploadModificationAdmissionReason, value.get("reason")),
        )
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA",
    "CodingUploadModificationAdmissionError",
    "CodingUploadModificationAdmissionReason",
    "CodingUploadModificationAdmissionState",
    "CodingUploadModificationAdmissionV1",
    "admit_coding_upload_modification",
    "build_coding_upload_modification_admission",
    "validate_coding_upload_modification_admission",
]
