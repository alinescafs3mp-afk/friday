"""Pure rollback admission bound to an exact previous revision fact.

The caller supplies the previous revision identity.  This module does not
look up a repository, choose a branch head, or perform a rollback.  Recency
selectors are rejected and a missing previous revision is a blocked result,
not an empty one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA = "friday.coding-result-rollback-admission.v1"
MAX_ROLLBACK_ID_CHARS = 128
MAX_OPERATION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_PREVIOUS_REVISION_CHARS = 128
MAX_REVISION_SELECTOR_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RECENCY_SELECTORS = frozenset({"latest", "head", "newest", "current"})


class CodingResultRollbackAdmissionError(ValueError):
    """A rollback identity or previous-revision fact is malformed."""


class CodingResultRollbackAdmissionState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingResultRollbackAdmissionReason(StrEnum):
    NO_FACTS = "no_facts"
    ROLLBACK_ADMITTED = "rollback_admitted"
    MISSING_OPERATION_ID = "missing_operation_id"
    MISSING_PREVIOUS_REVISION = "missing_previous_revision"
    RECENCY_SELECTOR = "recency_selector"
    INVALID_REVISION = "invalid_revision"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"

    EXACT_PREVIOUS_REVISION = ROLLBACK_ADMITTED


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultRollbackAdmissionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _exact_revision(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_PREVIOUS_REVISION_CHARS:
        _fail("previous_revision", "invalid")
    revision = cast(str, value)
    if revision.casefold() in _RECENCY_SELECTORS:
        _fail("previous_revision", "recency")
    if _ID_RE.fullmatch(revision) is None and _SHA256_RE.fullmatch(revision) is None:
        _fail("previous_revision", "invalid")
    return revision


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


def _state(value: object) -> CodingResultRollbackAdmissionState:
    try:
        return CodingResultRollbackAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultRollbackAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingResultRollbackAdmissionReason:
    try:
        return CodingResultRollbackAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultRollbackAdmissionError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingResultRollbackAdmissionV1:
    """Immutable permission to target one exact previous revision."""

    rollback_id: str
    authenticated_turn_id: str
    admission: CodingResultRollbackAdmissionState
    operation_id: str | None
    previous_revision: str | None
    revision_selector: str | None
    reason: CodingResultRollbackAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.rollback_id, field="rollback_id", maximum=MAX_ROLLBACK_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        if admission is CodingResultRollbackAdmissionState.ADMITTED:
            if self.operation_id is None or self.previous_revision is None:
                _fail("admitted", "missing_identity")
            _identifier(self.operation_id, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
            _exact_revision(self.previous_revision)
            _selector(self.revision_selector)
        elif (
            self.operation_id is not None
            or self.previous_revision is not None
            or self.revision_selector is not None
        ):
            _fail("blocked_or_empty_rollback", "exposed")

    @property
    def state(self) -> CodingResultRollbackAdmissionState:
        return self.admission

    @property
    def admission_state(self) -> CodingResultRollbackAdmissionState:
        return self.admission

    @property
    def closed_admission(self) -> CodingResultRollbackAdmissionState:
        return self.admission

    @property
    def decision(self) -> CodingResultRollbackAdmissionState:
        return self.admission

    @property
    def rollback(self) -> CodingResultRollbackAdmissionState:
        return self.admission

    @property
    def revision(self) -> str | None:
        return self.previous_revision

    @property
    def closed_reason(self) -> CodingResultRollbackAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA,
            "rollback_id": self.rollback_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "operation_id": self.operation_id,
            "previous_revision": self.previous_revision,
            "revision_selector": self.revision_selector,
            "reason": self.reason.value,
        }


RollbackAdmissionState = CodingResultRollbackAdmissionState
RollbackAdmissionReason = CodingResultRollbackAdmissionReason
CodingResultRollbackState = CodingResultRollbackAdmissionState
CodingResultRollbackReason = CodingResultRollbackAdmissionReason
CodingResultRollbackAdmission = CodingResultRollbackAdmissionV1
CodingResultRollbackAdmissionDecision = CodingResultRollbackAdmissionState


def _result(
    rollback_id: str,
    turn: str,
    state: CodingResultRollbackAdmissionState,
    reason: CodingResultRollbackAdmissionReason,
    *,
    operation_id: str | None = None,
    previous_revision: str | None = None,
    revision_selector: str | None = None,
) -> CodingResultRollbackAdmissionV1:
    if state is not CodingResultRollbackAdmissionState.ADMITTED:
        operation_id = None
        previous_revision = None
        revision_selector = None
    return CodingResultRollbackAdmissionV1(
        rollback_id,
        turn,
        state,
        operation_id,
        previous_revision,
        revision_selector,
        reason,
    )


def _revision_fact(value: object) -> str:
    if not isinstance(value, Mapping):
        return _exact_revision(value)
    allowed = {
        "revision_id",
        "revision",
        "previous_revision",
        "sha256",
        "revision_sha256",
        "selector",
    }
    if set(value) - allowed:
        _fail("previous_revision", "unknown_fields")
    if value.get("selector") is not None:
        _selector(value.get("selector"))
        _fail("previous_revision", "recency")
    candidates = [
        value.get("revision_id"),
        value.get("revision"),
        value.get("previous_revision"),
        value.get("sha256", value.get("revision_sha256")),
    ]
    present = [candidate for candidate in candidates if candidate is not None]
    if len(present) != 1:
        _fail("previous_revision", "facts")
    return _exact_revision(present[0])


def build_coding_result_rollback_admission(
    rollback_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    operation_id: str | None = None,
    previous_revision: object = None,
    *,
    revision: object = None,
    revision_selector: object = None,
    selector: object = None,
) -> CodingResultRollbackAdmissionV1:
    """Admit rollback only to the caller-supplied exact previous revision."""

    if isinstance(rollback_id, Mapping):
        raw = rollback_id
        allowed = {
            "schema",
            "rollback_id",
            "authenticated_turn_id",
            "operation_id",
            "previous_revision",
            "revision",
            "revision_selector",
            "selector",
            "admission",
            "state",
            "reason",
        }
        if set(raw) - allowed:
            _fail("rollback", "unknown_fields")
        if {"admission", "state", "reason"}.intersection(raw):
            required = {
                "schema",
                "rollback_id",
                "authenticated_turn_id",
                "admission",
                "operation_id",
                "previous_revision",
                "revision_selector",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA:
                _fail("rollback", "serialized")
            return CodingResultRollbackAdmissionV1(
                rollback_id=cast(str, raw.get("rollback_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                admission=cast(CodingResultRollbackAdmissionState, raw.get("admission", raw.get("state"))),
                operation_id=cast(str | None, raw.get("operation_id")),
                previous_revision=cast(str | None, raw.get("previous_revision")),
                revision_selector=cast(str | None, raw.get("revision_selector")),
                reason=cast(CodingResultRollbackAdmissionReason, raw.get("reason")),
            )
        rollback_id = cast(str, raw.get("rollback_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        operation_id = cast(str | None, raw.get("operation_id"))
        previous_revision = raw.get("previous_revision", raw.get("revision"))
        revision_selector = raw.get("revision_selector", raw.get("selector"))
    if revision is not None:
        if previous_revision is not None:
            _fail("rollback", "duplicate_revision")
        previous_revision = revision
    if selector is not None:
        if revision_selector is not None:
            _fail("rollback", "duplicate_selector")
        revision_selector = selector
    rollback_key = _identifier(rollback_id, field="rollback_id", maximum=MAX_ROLLBACK_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if operation_id is None and previous_revision is None and revision_selector is None:
        return _result(
            rollback_key,
            turn_key,
            CodingResultRollbackAdmissionState.EMPTY,
            CodingResultRollbackAdmissionReason.NO_FACTS,
        )
    if operation_id is None:
        return _result(
            rollback_key,
            turn_key,
            CodingResultRollbackAdmissionState.BLOCKED,
            CodingResultRollbackAdmissionReason.MISSING_OPERATION_ID,
        )
    try:
        operation_key = _identifier(operation_id, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
        selector = _selector(revision_selector)
    except CodingResultRollbackAdmissionError as exc:
        reason = (
            CodingResultRollbackAdmissionReason.RECENCY_SELECTOR
            if "recency" in str(exc)
            else CodingResultRollbackAdmissionReason.INVALID_FACTS
        )
        return _result(rollback_key, turn_key, CodingResultRollbackAdmissionState.BLOCKED, reason)
    if previous_revision is None:
        return _result(
            rollback_key,
            turn_key,
            CodingResultRollbackAdmissionState.BLOCKED,
            CodingResultRollbackAdmissionReason.MISSING_PREVIOUS_REVISION,
        )
    try:
        previous_key = _revision_fact(previous_revision)
    except CodingResultRollbackAdmissionError as exc:
        reason = (
            CodingResultRollbackAdmissionReason.RECENCY_SELECTOR
            if "recency" in str(exc)
            else CodingResultRollbackAdmissionReason.INVALID_REVISION
        )
        return _result(rollback_key, turn_key, CodingResultRollbackAdmissionState.BLOCKED, reason)
    return _result(
        rollback_key,
        turn_key,
        CodingResultRollbackAdmissionState.ADMITTED,
        CodingResultRollbackAdmissionReason.ROLLBACK_ADMITTED,
        operation_id=operation_key,
        previous_revision=previous_key,
        revision_selector=selector,
    )


def validate_coding_result_rollback_admission(value: object) -> bool:
    try:
        if isinstance(value, CodingResultRollbackAdmissionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        required = {
            "schema",
            "rollback_id",
            "authenticated_turn_id",
            "admission",
            "operation_id",
            "previous_revision",
            "revision_selector",
            "reason",
        }
        if set(value) != required or value.get("schema") != CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA:
            return False
        CodingResultRollbackAdmissionV1(
            cast(str, value.get("rollback_id")),
            cast(str, value.get("authenticated_turn_id")),
            cast(CodingResultRollbackAdmissionState, value.get("admission")),
            cast(str | None, value.get("operation_id")),
            cast(str | None, value.get("previous_revision")),
            cast(str | None, value.get("revision_selector")),
            cast(CodingResultRollbackAdmissionReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_rollback_admission = build_coding_result_rollback_admission
validate_rollback_admission = validate_coding_result_rollback_admission


__all__ = [
    "CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_OPERATION_ID_CHARS",
    "MAX_PREVIOUS_REVISION_CHARS",
    "MAX_REVISION_SELECTOR_CHARS",
    "MAX_ROLLBACK_ID_CHARS",
    "CodingResultRollbackAdmission",
    "CodingResultRollbackAdmissionDecision",
    "CodingResultRollbackAdmissionError",
    "CodingResultRollbackAdmissionReason",
    "CodingResultRollbackAdmissionState",
    "CodingResultRollbackAdmissionV1",
    "RollbackAdmissionReason",
    "RollbackAdmissionState",
    "build_coding_result_rollback_admission",
    "build_rollback_admission",
    "validate_coding_result_rollback_admission",
    "validate_rollback_admission",
]
