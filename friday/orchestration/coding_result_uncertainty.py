"""Pure uncertainty classification for Coding Mode result publication.

The classifier combines supplied plan, pack, restart and rollback facts.  It
does not resolve a revision or inspect a repository.  A restart and rollback
claim together are explicitly UNKNOWN, and an archive pack without its exact
ARCHIVE plan is also UNKNOWN rather than publishable success.
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

CODING_RESULT_UNCERTAINTY_SCHEMA = "friday.coding-result-uncertainty.v1"
MAX_UNCERTAINTY_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128


class CodingResultUncertaintyError(ValueError):
    """An uncertainty identity or composed fact is malformed."""


class CodingResultUncertaintyState(StrEnum):
    EMPTY = "empty"
    KNOWN = "known"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class CodingResultUncertaintyReason(StrEnum):
    NO_FACTS = "no_facts"
    STABLE_FILE = "stable_file"
    STABLE_ARCHIVE = "stable_archive"
    RESTART_ROLLBACK_CONFLICT = "restart_rollback_conflict"
    ARCHIVE_PACK_PLAN_MISMATCH = "archive_pack_plan_mismatch"
    PACK_WITHOUT_ARCHIVE_PLAN = "pack_without_archive_plan"
    COMPONENT_BLOCKED = "component_blocked"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"

    RESULT_KNOWN = STABLE_FILE
    BOTH_RESTART_AND_ROLLBACK = RESTART_ROLLBACK_CONFLICT


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultUncertaintyError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", value) is None
    ):
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingResultUncertaintyState:
    try:
        return CodingResultUncertaintyState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultUncertaintyError("uncertainty_closed") from exc


def _reason(value: object) -> CodingResultUncertaintyReason:
    try:
        return CodingResultUncertaintyReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultUncertaintyError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingResultUncertaintyV1:
    """Immutable known/unknown classification for a result publication."""

    uncertainty_id: str
    authenticated_turn_id: str
    uncertainty: CodingResultUncertaintyState
    plan_id: str | None
    pack_id: str | None
    reason: CodingResultUncertaintyReason

    def __post_init__(self) -> None:
        _identifier(self.uncertainty_id, field="uncertainty_id", maximum=MAX_UNCERTAINTY_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        uncertainty = _state(self.uncertainty)
        reason = _reason(self.reason)
        object.__setattr__(self, "uncertainty", uncertainty)
        object.__setattr__(self, "reason", reason)
        if uncertainty is CodingResultUncertaintyState.KNOWN:
            if self.plan_id is None:
                _fail("known", "missing_plan")
            _identifier(self.plan_id, field="plan_id", maximum=128)
            if self.pack_id is not None:
                _identifier(self.pack_id, field="pack_id", maximum=128)
        elif self.plan_id is not None or self.pack_id is not None:
            _fail("nonknown_uncertainty", "exposed")

    @property
    def state(self) -> CodingResultUncertaintyState:
        return self.uncertainty

    @property
    def known(self) -> bool:
        return self.uncertainty is CodingResultUncertaintyState.KNOWN

    @property
    def closed_reason(self) -> CodingResultUncertaintyReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_UNCERTAINTY_SCHEMA,
            "uncertainty_id": self.uncertainty_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "uncertainty": self.uncertainty.value,
            "plan_id": self.plan_id,
            "pack_id": self.pack_id,
            "reason": self.reason.value,
        }


UncertaintyState = CodingResultUncertaintyState
UncertaintyReason = CodingResultUncertaintyReason
CodingResultUncertainty = CodingResultUncertaintyV1
CodingResultUncertaintyDecision = CodingResultUncertaintyState


def _result(
    uncertainty_id: str,
    turn: str,
    state: CodingResultUncertaintyState,
    reason: CodingResultUncertaintyReason,
    *,
    plan_id: str | None = None,
    pack_id: str | None = None,
) -> CodingResultUncertaintyV1:
    if state is not CodingResultUncertaintyState.KNOWN:
        plan_id = None
        pack_id = None
    return CodingResultUncertaintyV1(uncertainty_id, turn, state, plan_id, pack_id, reason)


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


def build_coding_result_uncertainty(
    uncertainty_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    plan: object = None,
    pack: object = None,
    restart: object = None,
    rollback: object = None,
    *,
    archive_plan: object = None,
    pack_admission: object = None,
    restart_admission: object = None,
    rollback_admission: object = None,
) -> CodingResultUncertaintyV1:
    """Classify whether the supplied result facts are safe to publish."""

    if isinstance(uncertainty_id, Mapping):
        raw = uncertainty_id
        allowed = {
            "schema",
            "uncertainty_id",
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
            "state",
            "plan_id",
            "pack_id",
            "reason",
        }
        if set(raw) - allowed:
            _fail("uncertainty", "unknown_fields")
        if {"uncertainty", "state", "reason"}.intersection(raw):
            return CodingResultUncertaintyV1(
                cast(str, raw.get("uncertainty_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingResultUncertaintyState, raw.get("uncertainty", raw.get("state"))),
                cast(str | None, raw.get("plan_id")),
                cast(str | None, raw.get("pack_id")),
                cast(CodingResultUncertaintyReason, raw.get("reason")),
            )
        uncertainty_id = cast(str, raw.get("uncertainty_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        plan = raw.get("plan", raw.get("archive_plan"))
        pack = raw.get("pack", raw.get("pack_admission"))
        restart = raw.get("restart", raw.get("restart_admission"))
        rollback = raw.get("rollback", raw.get("rollback_admission"))
    if archive_plan is not None:
        if plan is not None:
            _fail("uncertainty", "duplicate_plan")
        plan = archive_plan
    if pack_admission is not None:
        if pack is not None:
            _fail("uncertainty", "duplicate_pack")
        pack = pack_admission
    if restart_admission is not None:
        if restart is not None:
            _fail("uncertainty", "duplicate_restart")
        restart = restart_admission
    if rollback_admission is not None:
        if rollback is not None:
            _fail("uncertainty", "duplicate_rollback")
        rollback = rollback_admission

    uncertainty_key = _identifier(uncertainty_id, field="uncertainty_id", maximum=MAX_UNCERTAINTY_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    supplied = (plan, pack, restart, rollback)
    if all(item is None for item in supplied):
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.EMPTY,
            CodingResultUncertaintyReason.NO_FACTS,
        )
    plan_value = _plan(plan) if plan is not None else None
    pack_value = _pack(pack) if pack is not None else None
    restart_value = _restart(restart) if restart is not None else None
    rollback_value = _rollback(rollback) if rollback is not None else None
    if (
        (plan is not None and plan_value is None)
        or (pack is not None and pack_value is None)
        or (restart is not None and restart_value is None)
        or (rollback is not None and rollback_value is None)
    ):
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.BLOCKED,
            CodingResultUncertaintyReason.INVALID_FACTS,
        )
    components = tuple(
        item for item in (plan_value, pack_value, restart_value, rollback_value) if item is not None
    )
    if any(getattr(item, "authenticated_turn_id", turn_key) != turn_key for item in components):
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.BLOCKED,
            CodingResultUncertaintyReason.IDENTITY_MISMATCH,
        )
    if any(
        getattr(item, "plan", getattr(item, "manifest", getattr(item, "admission", None)))
        in {
            CodingResultArchivePlanState.BLOCKED,
            CodingResultArchivePackAdmissionState.BLOCKED,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRollbackAdmissionState.BLOCKED,
        }
        for item in components
    ):
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.BLOCKED,
            CodingResultUncertaintyReason.COMPONENT_BLOCKED,
        )
    restart_claimed = (
        restart_value is not None and restart_value.admission is not CodingResultRestartAdmissionState.EMPTY
    )
    rollback_claimed = (
        rollback_value is not None
        and rollback_value.admission is not CodingResultRollbackAdmissionState.EMPTY
    )
    if restart_claimed and rollback_claimed:
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.UNKNOWN,
            CodingResultUncertaintyReason.RESTART_ROLLBACK_CONFLICT,
        )
    if plan_value is None:
        if pack_value is not None and pack_value.admission is CodingResultArchivePackAdmissionState.ADMITTED:
            return _result(
                uncertainty_key,
                turn_key,
                CodingResultUncertaintyState.UNKNOWN,
                CodingResultUncertaintyReason.PACK_WITHOUT_ARCHIVE_PLAN,
            )
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.UNKNOWN,
            CodingResultUncertaintyReason.ARCHIVE_PACK_PLAN_MISMATCH,
        )
    if plan_value.plan is CodingResultArchivePlanState.ARCHIVE:
        if pack_value is None or pack_value.admission is not CodingResultArchivePackAdmissionState.ADMITTED:
            return _result(
                uncertainty_key,
                turn_key,
                CodingResultUncertaintyState.UNKNOWN,
                CodingResultUncertaintyReason.ARCHIVE_PACK_PLAN_MISMATCH,
            )
        if pack_value.plan_id != plan_value.plan_id or tuple(pack_value.member_paths) != tuple(
            plan_value.files
        ):
            return _result(
                uncertainty_key,
                turn_key,
                CodingResultUncertaintyState.UNKNOWN,
                CodingResultUncertaintyReason.ARCHIVE_PACK_PLAN_MISMATCH,
            )
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.KNOWN,
            CodingResultUncertaintyReason.STABLE_ARCHIVE,
            plan_id=plan_value.plan_id,
            pack_id=pack_value.pack_id,
        )
    if pack_value is not None:
        return _result(
            uncertainty_key,
            turn_key,
            CodingResultUncertaintyState.UNKNOWN,
            CodingResultUncertaintyReason.PACK_WITHOUT_ARCHIVE_PLAN,
        )
    return _result(
        uncertainty_key,
        turn_key,
        CodingResultUncertaintyState.KNOWN,
        CodingResultUncertaintyReason.STABLE_FILE,
        plan_id=plan_value.plan_id,
    )


def validate_coding_result_uncertainty(value: object) -> bool:
    try:
        if isinstance(value, CodingResultUncertaintyV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        required = {
            "schema",
            "uncertainty_id",
            "authenticated_turn_id",
            "uncertainty",
            "plan_id",
            "pack_id",
            "reason",
        }
        if set(value) != required or value.get("schema") != CODING_RESULT_UNCERTAINTY_SCHEMA:
            return False
        CodingResultUncertaintyV1(
            cast(str, value.get("uncertainty_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingResultUncertaintyState, value.get("uncertainty")),
            cast(str | None, value.get("plan_id")),
            cast(str | None, value.get("pack_id")),
            cast(CodingResultUncertaintyReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_result_uncertainty = build_coding_result_uncertainty
validate_result_uncertainty = validate_coding_result_uncertainty


__all__ = [
    "CODING_RESULT_UNCERTAINTY_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_UNCERTAINTY_ID_CHARS",
    "CodingResultUncertainty",
    "CodingResultUncertaintyDecision",
    "CodingResultUncertaintyError",
    "CodingResultUncertaintyReason",
    "CodingResultUncertaintyState",
    "CodingResultUncertaintyV1",
    "UncertaintyReason",
    "UncertaintyState",
    "build_coding_result_uncertainty",
    "build_result_uncertainty",
    "validate_coding_result_uncertainty",
    "validate_result_uncertainty",
]
