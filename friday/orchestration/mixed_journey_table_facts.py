"""Read-only, body-free facts for a mixed-journey table organ.

An upstream table observer supplies an opaque id, SHA-256 digest, and optional
bounded dimensions.  Spreadsheet bytes, cells, paths, and row contents never
cross this adapter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_TABLE_FACTS_SCHEMA = "friday.mixed-journey-table-facts.v1"
MAX_TABLE_ID_CHARS = 128
MAX_TABLE_DIMENSION = 1_000_000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_NAME_RE = re.compile(
    r"(?i)(?:^|[_.-])(?:secret|private|password|passwd|credential|token|api[-_]?key|\.env)(?:$|[_.-])"
)
_MISSING = object()


class MixedJourneyTableFactsError(ValueError):
    """A table fact or serialized result is malformed."""


class MixedJourneyTableFactsState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    BLOCKED = "blocked"


class MixedJourneyTableFactsReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    INVALID_FACTS = "invalid_facts"
    INVALID_TABLE_ID = "invalid_table_id"
    INVALID_DIGEST = "invalid_digest"
    INVALID_DIMENSION = "invalid_dimension"
    PRIVATE_FACT = "private_fact"


@dataclass(frozen=True, slots=True)
class MixedJourneyTableFactsInputV1:
    """Facts supplied by a table observer, without spreadsheet bytes."""

    table_id: str | None = None
    sha256: str | None = None
    row_count: int | None = None
    column_count: int | None = None

    @property
    def digest(self) -> str | None:
        return self.sha256


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyTableFactsError(f"{field}_{detail}")


def _id(value: object) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail("table_id", "id")
    if _PRIVATE_NAME_RE.search(cast(str, value)):
        _fail("table_id", "private")
    return cast(str, value)


def _sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("sha256", "hex")
    return cast(str, value)


def _dimension(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TABLE_DIMENSION:
        _fail(field, "bound")
    return cast(int, value)


@dataclass(frozen=True, slots=True)
class MixedJourneyTableFactsV1:
    """One immutable table-organ result."""

    table_id: str | None
    state: MixedJourneyTableFactsState
    sha256: str | None
    row_count: int | None
    column_count: int | None
    reason: MixedJourneyTableFactsReason

    def __post_init__(self) -> None:
        if self.table_id is not None:
            _id(self.table_id)
        try:
            state = MixedJourneyTableFactsState(self.state)
            reason = MixedJourneyTableFactsReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyTableFactsError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if state is MixedJourneyTableFactsState.PRESENT:
            if self.table_id is None:
                _fail("table_id")
            _sha256(self.sha256)
            if self.row_count is not None:
                _dimension(self.row_count, field="row_count")
            if self.column_count is not None:
                _dimension(self.column_count, field="column_count")
        elif self.sha256 is not None or self.row_count is not None or self.column_count is not None:
            _fail("non_present", "leak")
        if state is MixedJourneyTableFactsState.BLOCKED and self.table_id is not None:
            _fail("blocked", "cell_leak")

    @property
    def fact_state(self) -> MixedJourneyTableFactsState:
        return self.state

    @property
    def table_state(self) -> MixedJourneyTableFactsState:
        return self.state

    @property
    def decision(self) -> MixedJourneyTableFactsState:
        return self.state

    @property
    def digest(self) -> str | None:
        return self.sha256

    @property
    def table_sha256(self) -> str | None:
        return self.sha256

    @property
    def summary_digest(self) -> str | None:
        return self.sha256

    @property
    def closed_reason(self) -> MixedJourneyTableFactsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_TABLE_FACTS_SCHEMA,
            "table_id": self.table_id,
            "state": self.state.value,
            "sha256": self.sha256,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "reason": self.reason.value,
        }


TableFactsInput = MixedJourneyTableFactsInputV1
TableFactsState = MixedJourneyTableFactsState
TableFactsReason = MixedJourneyTableFactsReason
MixedJourneyTableFacts = MixedJourneyTableFactsV1


def _blocked(
    reason: MixedJourneyTableFactsReason = MixedJourneyTableFactsReason.INVALID_FACTS,
) -> MixedJourneyTableFactsV1:
    return MixedJourneyTableFactsV1(None, MixedJourneyTableFactsState.BLOCKED, None, None, None, reason)


def _known(raw: Mapping[str, object]) -> None:
    allowed = {
        "schema",
        "table_id",
        "id",
        "state",
        "reason",
        "sha256",
        "digest",
        "table_sha256",
        "row_count",
        "column_count",
    }
    if set(raw) - allowed:
        _fail("facts", "unknown")
    if raw.get("schema", MIXED_JOURNEY_TABLE_FACTS_SCHEMA) != MIXED_JOURNEY_TABLE_FACTS_SCHEMA:
        _fail("schema")


def build_mixed_journey_table_facts(
    table_id: str | Mapping[str, object] | None = None,
    sha256: object = _MISSING,
    row_count: object = _MISSING,
    column_count: object = _MISSING,
    *,
    digest: object = _MISSING,
    table_sha256: object = _MISSING,
    facts: MixedJourneyTableFactsInputV1 | Mapping[str, object] | None = None,
) -> MixedJourneyTableFactsV1:
    """Validate already-supplied table metadata and fail closed."""

    if facts is not None:
        if table_id is not None or any(
            value is not _MISSING for value in (sha256, row_count, column_count, digest, table_sha256)
        ):
            return _blocked()
        if isinstance(facts, MixedJourneyTableFactsInputV1):
            table_id, sha256, row_count, column_count = (
                facts.table_id,
                facts.sha256,
                facts.row_count,
                facts.column_count,
            )
        elif isinstance(facts, Mapping):
            table_id = facts
        else:
            return _blocked()
    if digest is not _MISSING and sha256 is not _MISSING:
        return _blocked()
    if table_sha256 is not _MISSING:
        if sha256 is not _MISSING or digest is not _MISSING:
            return _blocked()
        sha256 = table_sha256
    if digest is not _MISSING:
        sha256 = digest
    if isinstance(table_id, Mapping):
        raw = table_id
        try:
            _known(raw)
            raw_id = raw.get("table_id", raw.get("id"))
            state = raw.get("state")
            if state in {"empty", "blocked"}:
                selected = MixedJourneyTableFactsState(state)
                return MixedJourneyTableFactsV1(
                    None
                    if selected is MixedJourneyTableFactsState.BLOCKED
                    else (None if raw_id is None else _id(raw_id)),
                    selected,
                    None,
                    None,
                    None,
                    MixedJourneyTableFactsReason(raw.get("reason", "no_facts")),
                )
            if any(value is not _MISSING for value in (sha256, row_count, column_count, digest)):
                _fail("facts", "duplicate")
            table_id = cast(str | None, raw_id)
            sha256 = raw.get("sha256", raw.get("table_sha256", raw.get("digest")))
            row_count = raw.get("row_count")
            column_count = raw.get("column_count")
        except (TypeError, ValueError, MixedJourneyTableFactsError):
            return _blocked()
    if table_id is None:
        return MixedJourneyTableFactsV1(
            None, MixedJourneyTableFactsState.EMPTY, None, None, None, MixedJourneyTableFactsReason.NO_FACTS
        )
    try:
        key = _id(table_id)
    except MixedJourneyTableFactsError as exc:
        return _blocked(
            MixedJourneyTableFactsReason.PRIVATE_FACT
            if "private" in str(exc)
            else MixedJourneyTableFactsReason.INVALID_TABLE_ID
        )
    if sha256 is _MISSING:
        sha256 = None
    if row_count is _MISSING:
        row_count = None
    if column_count is _MISSING:
        column_count = None
    if sha256 in (None, "") and row_count is None and column_count is None:
        return MixedJourneyTableFactsV1(
            key, MixedJourneyTableFactsState.EMPTY, None, None, None, MixedJourneyTableFactsReason.NO_FACTS
        )
    try:
        digest_value = _sha256(sha256)
        row_value = None if row_count is None else _dimension(row_count, field="row_count")
        column_value = None if column_count is None else _dimension(column_count, field="column_count")
    except MixedJourneyTableFactsError as exc:
        reason = (
            MixedJourneyTableFactsReason.INVALID_DIMENSION
            if "count" in str(exc)
            else MixedJourneyTableFactsReason.INVALID_DIGEST
        )
        return _blocked(reason)
    return MixedJourneyTableFactsV1(
        key,
        MixedJourneyTableFactsState.PRESENT,
        digest_value,
        row_value,
        column_value,
        MixedJourneyTableFactsReason.PRESENT,
    )


def validate_mixed_journey_table_facts(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyTableFactsV1)
            else build_mixed_journey_table_facts(cast(Mapping[str, object], value))
        )
        return (
            isinstance(result, MixedJourneyTableFactsV1)
            and result.state is not MixedJourneyTableFactsState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_table_facts = build_mixed_journey_table_facts
validate_table_facts = validate_mixed_journey_table_facts

__all__ = [
    "MIXED_JOURNEY_TABLE_FACTS_SCHEMA",
    "MAX_TABLE_DIMENSION",
    "TableFactsInput",
    "TableFactsReason",
    "TableFactsState",
    "MixedJourneyTableFacts",
    "MixedJourneyTableFactsError",
    "MixedJourneyTableFactsInputV1",
    "MixedJourneyTableFactsReason",
    "MixedJourneyTableFactsState",
    "MixedJourneyTableFactsV1",
    "build_mixed_journey_table_facts",
    "build_table_facts",
    "validate_mixed_journey_table_facts",
    "validate_table_facts",
]
