"""Pure final-publication admission for Coding Mode results.

Publication composes the archive plan, pack, restart, rollback and
uncertainty contracts.  It only admits a FILE carrier without a pack or an
ARCHIVE carrier with an exact admitted pack and KNOWN uncertainty.  No bytes
are packed, no paths are opened, and no transport is invoked here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_result_archive_pack_admission import (
    CodingResultArchivePackAdmissionState,
    CodingResultArchivePackAdmissionV1,
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import (
    CodingResultArchivePlanState,
    CodingResultArchivePlanV1,
    build_coding_result_archive_plan,
)
from friday.orchestration.coding_result_restart_admission import (
    CodingResultRestartAdmissionState,
    CodingResultRestartAdmissionV1,
    build_coding_result_restart_admission,
)
from friday.orchestration.coding_result_rollback_admission import (
    CodingResultRollbackAdmissionState,
    CodingResultRollbackAdmissionV1,
    build_coding_result_rollback_admission,
)
from friday.orchestration.coding_result_uncertainty import (
    CodingResultUncertaintyState,
    CodingResultUncertaintyV1,
    build_coding_result_uncertainty,
)

CODING_RESULT_PUBLICATION_ADMISSION_SCHEMA = "friday.coding-result-publication-admission.v1"
MAX_PUBLICATION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128


class CodingResultPublicationAdmissionError(ValueError):
    """A publication identity or composed result fact is malformed."""


class CodingResultPublicationAdmissionState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingResultPublicationAdmissionReason(StrEnum):
    NO_FACTS = "no_facts"
    FILE_PUBLICATION_ADMITTED = "file_publication_admitted"
    ARCHIVE_PUBLICATION_ADMITTED = "archive_publication_admitted"
    PLAN_EMPTY = "plan_empty"
    PLAN_BLOCKED = "plan_blocked"
    PLAN_NOT_PUBLISHABLE = "plan_not_publishable"
    PACK_REQUIRED = "pack_required"
    PACK_NOT_ADMITTED = "pack_not_admitted"
    PACK_MISMATCH = "pack_mismatch"
    PACK_FORBIDDEN_FOR_FILE = "pack_forbidden_for_file"
    UNCERTAINTY_REQUIRED = "uncertainty_required"
    UNCERTAINTY_UNKNOWN = "uncertainty_unknown"
    UNCERTAINTY_BLOCKED = "uncertainty_blocked"
    RESTART_NOT_ADMITTED = "restart_not_admitted"
    ROLLBACK_NOT_ADMITTED = "rollback_not_admitted"
    COMPONENT_BLOCKED = "component_blocked"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"

    FILE_ADMITTED = FILE_PUBLICATION_ADMITTED
    ARCHIVE_ADMITTED = ARCHIVE_PUBLICATION_ADMITTED
    UNCERTAINTY_NOT_KNOWN = UNCERTAINTY_UNKNOWN


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultPublicationAdmissionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", value) is None
    ):
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingResultPublicationAdmissionState:
    try:
        return CodingResultPublicationAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultPublicationAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingResultPublicationAdmissionReason:
    try:
        return CodingResultPublicationAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultPublicationAdmissionError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingResultPublicationAdmissionV1:
    """Immutable final-carrier publication permission."""

    publication_id: str
    authenticated_turn_id: str
    admission: CodingResultPublicationAdmissionState
    carrier: str | None
    plan_id: str | None
    pack_id: str | None
    reason: CodingResultPublicationAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.publication_id, field="publication_id", maximum=MAX_PUBLICATION_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        if admission is CodingResultPublicationAdmissionState.ADMITTED:
            if self.carrier not in {"file", "archive"} or self.plan_id is None:
                _fail("admitted", "missing_carrier")
            _identifier(self.plan_id, field="plan_id", maximum=128)
            if self.carrier == "archive":
                if self.pack_id is None:
                    _fail("admitted", "missing_pack")
                _identifier(self.pack_id, field="pack_id", maximum=128)
            elif self.pack_id is not None:
                _fail("file", "pack_exposed")
        elif self.carrier is not None or self.plan_id is not None or self.pack_id is not None:
            _fail("blocked_or_empty_publication", "exposed")

    @property
    def state(self) -> CodingResultPublicationAdmissionState:
        return self.admission

    @property
    def publication(self) -> CodingResultPublicationAdmissionState:
        return self.admission

    @property
    def closed_reason(self) -> CodingResultPublicationAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_PUBLICATION_ADMISSION_SCHEMA,
            "publication_id": self.publication_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "carrier": self.carrier,
            "plan_id": self.plan_id,
            "pack_id": self.pack_id,
            "reason": self.reason.value,
        }


PublicationAdmissionState = CodingResultPublicationAdmissionState
PublicationAdmissionReason = CodingResultPublicationAdmissionReason
CodingResultPublicationAdmission = CodingResultPublicationAdmissionV1
CodingResultPublicationAdmissionDecision = CodingResultPublicationAdmissionState


def _result(
    publication_id: str,
    turn: str,
    state: CodingResultPublicationAdmissionState,
    reason: CodingResultPublicationAdmissionReason,
    *,
    carrier: str | None = None,
    plan_id: str | None = None,
    pack_id: str | None = None,
) -> CodingResultPublicationAdmissionV1:
    if state is not CodingResultPublicationAdmissionState.ADMITTED:
        carrier = None
        plan_id = None
        pack_id = None
    return CodingResultPublicationAdmissionV1(
        publication_id,
        turn,
        state,
        carrier,
        plan_id,
        pack_id,
        reason,
    )


def _plan(value: object) -> CodingResultArchivePlanV1 | None:
    if isinstance(value, CodingResultArchivePlanV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        if {"plan", "state", "carrier", "reason"}.intersection(value):
            return CodingResultArchivePlanV1(
                cast(str, value.get("plan_id")),
                cast(str, value.get("authenticated_turn_id")),
                cast(CodingResultArchivePlanState, value.get("plan", value.get("state"))),
                tuple(cast(list[str], value.get("files", []))),
                cast(Any, value.get("reason")),
            )
        return build_coding_result_archive_plan(
            cast(str, value.get("plan_id")),
            cast(str, value.get("authenticated_turn_id")),
            tree=value.get("tree"),
            files=value.get("files"),
            archive_requested=cast(bool, value.get("archive_requested", False)),
        )
    except (TypeError, ValueError):
        return None


def _pack(value: object) -> CodingResultArchivePackAdmissionV1 | None:
    if isinstance(value, CodingResultArchivePackAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_archive_pack_admission(value)
        except (TypeError, ValueError):
            return None
    return None


def _restart(value: object) -> CodingResultRestartAdmissionV1 | None:
    if isinstance(value, CodingResultRestartAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_restart_admission(value)
        except (TypeError, ValueError):
            return None
    return None


def _rollback(value: object) -> CodingResultRollbackAdmissionV1 | None:
    if isinstance(value, CodingResultRollbackAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_rollback_admission(value)
        except (TypeError, ValueError):
            return None
    return None


def _uncertainty(value: object) -> CodingResultUncertaintyV1 | None:
    if isinstance(value, CodingResultUncertaintyV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        try:
            return build_coding_result_uncertainty(value)
        except (TypeError, ValueError):
            return None
    return None


def build_coding_result_publication_admission(
    publication_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    plan: object = None,
    pack: object = None,
    restart: object = None,
    rollback: object = None,
    uncertainty: object = None,
    *,
    archive_plan: object = None,
    pack_admission: object = None,
    restart_admission: object = None,
    rollback_admission: object = None,
    uncertainty_facts: object = None,
) -> CodingResultPublicationAdmissionV1:
    """Admit a FILE or exact ARCHIVE publication after all five contracts."""

    if isinstance(publication_id, Mapping):
        raw = publication_id
        allowed = {
            "schema",
            "publication_id",
            "authenticated_turn_id",
            "plan",
            "archive_plan",
            "pack",
            "pack_admission",
            "restart",
            "restart_admission",
            "rollback",
            "rollback_admission",
            "uncertainty",
            "uncertainty_facts",
            "admission",
            "state",
            "carrier",
            "plan_id",
            "pack_id",
            "reason",
        }
        if set(raw) - allowed:
            _fail("publication", "unknown_fields")
        if {"admission", "state", "reason"}.intersection(raw):
            return CodingResultPublicationAdmissionV1(
                cast(str, raw.get("publication_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingResultPublicationAdmissionState, raw.get("admission", raw.get("state"))),
                cast(str | None, raw.get("carrier")),
                cast(str | None, raw.get("plan_id")),
                cast(str | None, raw.get("pack_id")),
                cast(CodingResultPublicationAdmissionReason, raw.get("reason")),
            )
        publication_id = cast(str, raw.get("publication_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        plan = raw.get("plan", raw.get("archive_plan"))
        pack = raw.get("pack", raw.get("pack_admission"))
        restart = raw.get("restart", raw.get("restart_admission"))
        rollback = raw.get("rollback", raw.get("rollback_admission"))
        uncertainty = raw.get("uncertainty", raw.get("uncertainty_facts"))
    if archive_plan is not None:
        if plan is not None:
            _fail("publication", "duplicate_plan")
        plan = archive_plan
    if pack_admission is not None:
        if pack is not None:
            _fail("publication", "duplicate_pack")
        pack = pack_admission
    if restart_admission is not None:
        if restart is not None:
            _fail("publication", "duplicate_restart")
        restart = restart_admission
    if rollback_admission is not None:
        if rollback is not None:
            _fail("publication", "duplicate_rollback")
        rollback = rollback_admission
    if uncertainty_facts is not None:
        if uncertainty is not None:
            _fail("publication", "duplicate_uncertainty")
        uncertainty = uncertainty_facts

    publication_key = _identifier(publication_id, field="publication_id", maximum=MAX_PUBLICATION_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    supplied = (plan, pack, restart, rollback, uncertainty)
    if all(item is None for item in supplied):
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.EMPTY,
            CodingResultPublicationAdmissionReason.NO_FACTS,
        )
    plan_value = _plan(plan) if plan is not None else None
    pack_value = _pack(pack) if pack is not None else None
    restart_value = _restart(restart) if restart is not None else None
    rollback_value = _rollback(rollback) if rollback is not None else None
    uncertainty_value = _uncertainty(uncertainty) if uncertainty is not None else None
    if (
        (plan is not None and plan_value is None)
        or (pack is not None and pack_value is None)
        or (restart is not None and restart_value is None)
        or (rollback is not None and rollback_value is None)
        or (uncertainty is not None and uncertainty_value is None)
    ):
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.INVALID_FACTS,
        )
    components = tuple(
        item
        for item in (plan_value, pack_value, restart_value, rollback_value, uncertainty_value)
        if item is not None
    )
    if any(getattr(item, "authenticated_turn_id", turn_key) != turn_key for item in components):
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.IDENTITY_MISMATCH,
        )
    if any(
        getattr(item, "plan", getattr(item, "uncertainty", getattr(item, "admission", None)))
        in {
            CodingResultArchivePlanState.BLOCKED,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRollbackAdmissionState.BLOCKED,
            CodingResultUncertaintyState.BLOCKED,
        }
        for item in components
    ):
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.COMPONENT_BLOCKED,
        )
    if plan_value is None:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.PLAN_NOT_PUBLISHABLE,
        )
    if plan_value.plan is CodingResultArchivePlanState.EMPTY:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.PLAN_EMPTY,
        )
    if uncertainty_value is None:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.UNCERTAINTY_REQUIRED,
        )
    if uncertainty_value.uncertainty is not CodingResultUncertaintyState.KNOWN:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.UNCERTAINTY_UNKNOWN,
        )
    if uncertainty_value.plan_id != plan_value.plan_id:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.IDENTITY_MISMATCH,
        )
    if plan_value.plan is CodingResultArchivePlanState.FILE:
        if pack_value is not None:
            return _result(
                publication_key,
                turn_key,
                CodingResultPublicationAdmissionState.BLOCKED,
                CodingResultPublicationAdmissionReason.PACK_FORBIDDEN_FOR_FILE,
            )
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.ADMITTED,
            CodingResultPublicationAdmissionReason.FILE_PUBLICATION_ADMITTED,
            carrier="file",
            plan_id=plan_value.plan_id,
        )
    if plan_value.plan is not CodingResultArchivePlanState.ARCHIVE:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.PLAN_NOT_PUBLISHABLE,
        )
    if pack_value is None:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.PACK_REQUIRED,
        )
    if pack_value.admission is not CodingResultArchivePackAdmissionState.ADMITTED:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.PACK_NOT_ADMITTED,
        )
    if pack_value.plan_id != plan_value.plan_id or tuple(pack_value.member_paths) != tuple(plan_value.files):
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.PACK_MISMATCH,
        )
    if uncertainty_value.pack_id != pack_value.pack_id:
        return _result(
            publication_key,
            turn_key,
            CodingResultPublicationAdmissionState.BLOCKED,
            CodingResultPublicationAdmissionReason.IDENTITY_MISMATCH,
        )
    return _result(
        publication_key,
        turn_key,
        CodingResultPublicationAdmissionState.ADMITTED,
        CodingResultPublicationAdmissionReason.ARCHIVE_PUBLICATION_ADMITTED,
        carrier="archive",
        plan_id=plan_value.plan_id,
        pack_id=pack_value.pack_id,
    )


def validate_coding_result_publication_admission(value: object) -> bool:
    try:
        if isinstance(value, CodingResultPublicationAdmissionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        required = {
            "schema",
            "publication_id",
            "authenticated_turn_id",
            "admission",
            "carrier",
            "plan_id",
            "pack_id",
            "reason",
        }
        if set(value) != required or value.get("schema") != CODING_RESULT_PUBLICATION_ADMISSION_SCHEMA:
            return False
        CodingResultPublicationAdmissionV1(
            cast(str, value.get("publication_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingResultPublicationAdmissionState, value.get("admission")),
            cast(str | None, value.get("carrier")),
            cast(str | None, value.get("plan_id")),
            cast(str | None, value.get("pack_id")),
            cast(CodingResultPublicationAdmissionReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_publication_admission = build_coding_result_publication_admission
validate_publication_admission = validate_coding_result_publication_admission


__all__ = [
    "CODING_RESULT_PUBLICATION_ADMISSION_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_PUBLICATION_ID_CHARS",
    "CodingResultPublicationAdmission",
    "CodingResultPublicationAdmissionDecision",
    "CodingResultPublicationAdmissionError",
    "CodingResultPublicationAdmissionReason",
    "CodingResultPublicationAdmissionState",
    "CodingResultPublicationAdmissionV1",
    "PublicationAdmissionReason",
    "PublicationAdmissionState",
    "build_coding_result_publication_admission",
    "build_publication_admission",
    "validate_coding_result_publication_admission",
    "validate_publication_admission",
]
