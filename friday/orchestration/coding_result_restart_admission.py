"""Pure restart admission for a Coding Mode result operation.

Restart is bound to an exact operation and authenticated turn and to an
already-admitted source-archive pack.  Recency selectors are intentionally
not accepted: a restart must not silently attach to another operation.
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

CODING_RESULT_RESTART_ADMISSION_SCHEMA = "friday.coding-result-restart-admission.v1"
MAX_RESTART_ID_CHARS = 128
MAX_OPERATION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_REVISION_SELECTOR_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RECENCY_SELECTORS = frozenset({"latest", "head", "newest", "current"})


class CodingResultRestartAdmissionError(ValueError):
    """A restart identity or pack fact is malformed."""


class CodingResultRestartAdmissionState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingResultRestartAdmissionReason(StrEnum):
    NO_FACTS = "no_facts"
    RESTART_ADMITTED = "restart_admitted"
    MISSING_OPERATION_ID = "missing_operation_id"
    MISSING_PACK = "missing_pack"
    PACK_BLOCKED = "pack_blocked"
    PACK_NOT_ADMITTED = "pack_not_admitted"
    RECENCY_SELECTOR = "recency_selector"
    INVALID_OPERATION_ID = "invalid_operation_id"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"

    EXACT_OPERATION_BOUND = RESTART_ADMITTED
    BLOCKED_PACK = PACK_BLOCKED


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultRestartAdmissionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _selector(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > MAX_REVISION_SELECTOR_CHARS:
        _fail("revision_selector", "invalid")
    selector = cast(str, value)
    if selector.casefold() in _RECENCY_SELECTORS:
        _fail("revision_selector", "recency")
    if _ID_RE.fullmatch(selector) is None:
        _fail("revision_selector", "invalid")
    return selector


def _state(value: object) -> CodingResultRestartAdmissionState:
    try:
        return CodingResultRestartAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultRestartAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingResultRestartAdmissionReason:
    try:
        return CodingResultRestartAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultRestartAdmissionError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingResultRestartAdmissionV1:
    """Immutable exact-operation restart permission."""

    restart_id: str
    authenticated_turn_id: str
    admission: CodingResultRestartAdmissionState
    operation_id: str | None
    pack_id: str | None
    revision_selector: str | None
    reason: CodingResultRestartAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.restart_id, field="restart_id", maximum=MAX_RESTART_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        if admission is CodingResultRestartAdmissionState.ADMITTED:
            if self.operation_id is None or self.pack_id is None:
                _fail("admitted", "missing_identity")
            _identifier(self.operation_id, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
            _identifier(self.pack_id, field="pack_id", maximum=128)
            _selector(self.revision_selector)
        elif self.operation_id is not None or self.pack_id is not None or self.revision_selector is not None:
            _fail("blocked_or_empty_restart", "exposed")

    @property
    def state(self) -> CodingResultRestartAdmissionState:
        return self.admission

    @property
    def restart(self) -> CodingResultRestartAdmissionState:
        return self.admission

    @property
    def closed_reason(self) -> CodingResultRestartAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_RESTART_ADMISSION_SCHEMA,
            "restart_id": self.restart_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "operation_id": self.operation_id,
            "pack_id": self.pack_id,
            "revision_selector": self.revision_selector,
            "reason": self.reason.value,
        }


RestartAdmissionState = CodingResultRestartAdmissionState
RestartAdmissionReason = CodingResultRestartAdmissionReason
CodingResultRestartAdmission = CodingResultRestartAdmissionV1
CodingResultRestartAdmissionDecision = CodingResultRestartAdmissionState


def _result(
    restart_id: str,
    turn: str,
    state: CodingResultRestartAdmissionState,
    reason: CodingResultRestartAdmissionReason,
    *,
    operation_id: str | None = None,
    pack_id: str | None = None,
    revision_selector: str | None = None,
) -> CodingResultRestartAdmissionV1:
    if state is not CodingResultRestartAdmissionState.ADMITTED:
        operation_id = None
        pack_id = None
        revision_selector = None
    return CodingResultRestartAdmissionV1(
        restart_id,
        turn,
        state,
        operation_id,
        pack_id,
        revision_selector,
        reason,
    )


def _pack(value: object) -> CodingResultArchivePackAdmissionV1 | None:
    if isinstance(value, CodingResultArchivePackAdmissionV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        return build_coding_result_archive_pack_admission(value)
    except (TypeError, ValueError):
        return None


def build_coding_result_restart_admission(
    restart_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    operation_id: str | None = None,
    pack: object = None,
    *,
    pack_admission: object = None,
    revision_selector: object = None,
) -> CodingResultRestartAdmissionV1:
    """Admit restart only for an exact operation and admitted archive pack."""

    if isinstance(restart_id, Mapping):
        raw = restart_id
        allowed = {
            "schema",
            "restart_id",
            "authenticated_turn_id",
            "operation_id",
            "pack",
            "pack_admission",
            "revision_selector",
            "admission",
            "state",
            "pack_id",
            "reason",
        }
        if set(raw) - allowed:
            _fail("restart", "unknown_fields")
        if {"admission", "state", "reason"}.intersection(raw):
            return CodingResultRestartAdmissionV1(
                restart_id=cast(str, raw.get("restart_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                admission=cast(CodingResultRestartAdmissionState, raw.get("admission", raw.get("state"))),
                operation_id=cast(str | None, raw.get("operation_id")),
                pack_id=cast(str | None, raw.get("pack_id")),
                revision_selector=cast(str | None, raw.get("revision_selector")),
                reason=cast(CodingResultRestartAdmissionReason, raw.get("reason")),
            )
        restart_id = cast(str, raw.get("restart_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        operation_id = cast(str | None, raw.get("operation_id"))
        pack = raw.get("pack", raw.get("pack_admission"))
        revision_selector = raw.get("revision_selector")
    if pack_admission is not None:
        if pack is not None:
            _fail("restart", "duplicate_pack")
        pack = pack_admission
    restart_key = _identifier(restart_id, field="restart_id", maximum=MAX_RESTART_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if operation_id is None and pack is None and revision_selector is None:
        return _result(
            restart_key,
            turn_key,
            CodingResultRestartAdmissionState.EMPTY,
            CodingResultRestartAdmissionReason.NO_FACTS,
        )
    try:
        operation_key = _identifier(operation_id, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
        selector = _selector(revision_selector)
    except CodingResultRestartAdmissionError as exc:
        if "recency" in str(exc):
            reason = CodingResultRestartAdmissionReason.RECENCY_SELECTOR
        else:
            reason = CodingResultRestartAdmissionReason.INVALID_OPERATION_ID
        return _result(restart_key, turn_key, CodingResultRestartAdmissionState.BLOCKED, reason)
    if pack is None:
        return _result(
            restart_key,
            turn_key,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRestartAdmissionReason.MISSING_PACK,
        )
    pack_value = _pack(pack)
    if pack_value is None:
        return _result(
            restart_key,
            turn_key,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRestartAdmissionReason.INVALID_FACTS,
        )
    if pack_value.authenticated_turn_id != turn_key:
        return _result(
            restart_key,
            turn_key,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRestartAdmissionReason.IDENTITY_MISMATCH,
        )
    if pack_value.admission is CodingResultArchivePackAdmissionState.BLOCKED:
        return _result(
            restart_key,
            turn_key,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRestartAdmissionReason.PACK_BLOCKED,
        )
    if pack_value.admission is not CodingResultArchivePackAdmissionState.ADMITTED:
        return _result(
            restart_key,
            turn_key,
            CodingResultRestartAdmissionState.BLOCKED,
            CodingResultRestartAdmissionReason.PACK_NOT_ADMITTED,
        )
    return _result(
        restart_key,
        turn_key,
        CodingResultRestartAdmissionState.ADMITTED,
        CodingResultRestartAdmissionReason.RESTART_ADMITTED,
        operation_id=operation_key,
        pack_id=cast(str, pack_value.pack_id),
        revision_selector=selector,
    )


def validate_coding_result_restart_admission(value: object) -> bool:
    try:
        if isinstance(value, CodingResultRestartAdmissionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        required = {
            "schema",
            "restart_id",
            "authenticated_turn_id",
            "admission",
            "operation_id",
            "pack_id",
            "revision_selector",
            "reason",
        }
        if set(value) != required or value.get("schema") != CODING_RESULT_RESTART_ADMISSION_SCHEMA:
            return False
        CodingResultRestartAdmissionV1(
            cast(str, value.get("restart_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingResultRestartAdmissionState, value.get("admission")),
            cast(str | None, value.get("operation_id")),
            cast(str | None, value.get("pack_id")),
            cast(str | None, value.get("revision_selector")),
            cast(CodingResultRestartAdmissionReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_restart_admission = build_coding_result_restart_admission
validate_restart_admission = validate_coding_result_restart_admission


__all__ = [
    "CODING_RESULT_RESTART_ADMISSION_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_OPERATION_ID_CHARS",
    "MAX_RESTART_ID_CHARS",
    "MAX_REVISION_SELECTOR_CHARS",
    "CodingResultRestartAdmission",
    "CodingResultRestartAdmissionDecision",
    "CodingResultRestartAdmissionError",
    "CodingResultRestartAdmissionReason",
    "CodingResultRestartAdmissionState",
    "CodingResultRestartAdmissionV1",
    "RestartAdmissionReason",
    "RestartAdmissionState",
    "build_coding_result_restart_admission",
    "build_restart_admission",
    "validate_coding_result_restart_admission",
    "validate_restart_admission",
]
