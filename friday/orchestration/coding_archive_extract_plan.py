"""Pure destination plan for an already-admitted coding archive.

The planner consumes a frozen member catalog and a frozen extraction admission.
It returns relative destination names only; no archive, path, or filesystem is
opened and no destination is created.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_archive_extract_admission import (
    CodingArchiveExtractAdmissionState,
    CodingArchiveExtractAdmissionV1,
    build_coding_archive_extract_admission,
)
from friday.orchestration.coding_archive_member_catalog import (
    CodingArchiveMemberCatalogState,
    CodingArchiveMemberCatalogV1,
    build_coding_archive_member_catalog,
)

CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA = "friday.coding-archive-extract-plan.v1"
MAX_PLAN_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_DESTINATION_PATH_CHARS = 4_096
MAX_PLANNED_MEMBER_COUNT = 4_096

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingArchiveExtractPlanError(ValueError):
    """An extraction plan identity, fact, or destination is malformed."""


class CodingArchiveExtractPlanState(StrEnum):
    """Closed outcomes for one extraction plan."""

    EMPTY = "empty"
    PLANNED = "planned"
    BLOCKED = "blocked"


class CodingArchiveExtractPlanReason(StrEnum):
    """Closed reason for one extraction plan."""

    NO_MEMBERS = "no_members"
    ALL_DESTINATIONS_PLANNED = "all_destinations_planned"
    ADMISSION_NOT_GRANTED = "admission_not_granted"
    CATALOG_INVALID = "catalog_invalid"
    ADMISSION_INVALID = "admission_invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSAFE_DESTINATION = "unsafe_destination"
    DESTINATION_COLLISION = "destination_collision"
    INVALID_FACTS = "invalid_facts"

    PLAN_CREATED = ALL_DESTINATIONS_PLANNED
    NOT_ADMITTED = ADMISSION_NOT_GRANTED


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingArchiveExtractPlanError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingArchiveExtractPlanState:
    try:
        return CodingArchiveExtractPlanState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractPlanError("plan_closed") from exc


def _reason(value: object) -> CodingArchiveExtractPlanReason:
    try:
        return CodingArchiveExtractPlanReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveExtractPlanError("reason_closed") from exc


def _path_parts(value: object, *, field: str) -> tuple[str, ...]:
    if type(value) is not str or not value or len(value) > MAX_DESTINATION_PATH_CHARS:
        _fail(field, "path")
    path = cast(str, value)
    if path != path.strip() or any(unicodedata.category(character).startswith("C") for character in path):
        _fail(field, "path")
    if path.startswith(("/", "\\")) or _DRIVE_RE.match(path) is not None:
        _fail(field, "absolute")
    parts = tuple(part for part in re.split(r"[/\\]", path) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        _fail(field, "traversal")
    return parts


def _paths(value: object, *, field: str = "destination_paths") -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail(field, "sequence")
    if len(value) > MAX_PLANNED_MEMBER_COUNT:
        _fail(field, "count")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        parts = _path_parts(item, field=field)
        path = "/".join(parts)
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in seen:
            _fail(field, "collision")
        seen.add(folded)
        result.append(path)
    return tuple(result)


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_PLANNED_MEMBER_COUNT:
        _fail(field, "range")
    return cast(int, value)


def _catalog(value: object) -> CodingArchiveMemberCatalogV1 | None:
    try:
        if isinstance(value, CodingArchiveMemberCatalogV1):
            value.__post_init__()
            return value
        if isinstance(value, Mapping):
            return build_coding_archive_member_catalog(value)
    except (TypeError, ValueError):
        return None
    return None


def _admission(value: object) -> CodingArchiveExtractAdmissionV1 | None:
    try:
        if isinstance(value, CodingArchiveExtractAdmissionV1):
            value.__post_init__()
            return value
        if isinstance(value, Mapping):
            return build_coding_archive_extract_admission(value)
    except (TypeError, ValueError):
        return None
    return None


def _result(
    plan_id: str,
    authenticated_turn_id: str | None,
    state: CodingArchiveExtractPlanState,
    reason: CodingArchiveExtractPlanReason,
    *,
    paths: tuple[str, ...] = (),
) -> CodingArchiveExtractPlanV1:
    count = len(paths)
    return CodingArchiveExtractPlanV1(
        plan_id=plan_id,
        authenticated_turn_id=authenticated_turn_id,
        plan=state,
        planned_member_count=count if state is CodingArchiveExtractPlanState.PLANNED else 0,
        member_count=count if state is CodingArchiveExtractPlanState.PLANNED else 0,
        destination_paths=paths if state is not CodingArchiveExtractPlanState.BLOCKED else (),
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class CodingArchiveExtractPlanV1:
    """Immutable extraction destinations for an admitted member catalog."""

    plan_id: str
    authenticated_turn_id: str | None
    plan: CodingArchiveExtractPlanState
    planned_member_count: int
    member_count: int
    destination_paths: tuple[str, ...]
    reason: CodingArchiveExtractPlanReason

    def __post_init__(self) -> None:
        _identifier(self.plan_id, field="plan_id", maximum=MAX_PLAN_ID_CHARS)
        if self.authenticated_turn_id is not None:
            _identifier(
                self.authenticated_turn_id,
                field="authenticated_turn_id",
                maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
            )
        plan = _state(self.plan)
        reason = _reason(self.reason)
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "reason", reason)
        planned = _count(self.planned_member_count, field="planned_member_count")
        members = _count(self.member_count, field="member_count")
        paths = _paths(self.destination_paths)
        object.__setattr__(self, "destination_paths", paths)
        if planned != members or planned != len(paths):
            _fail("member_counts", "inconsistent")
        if plan is CodingArchiveExtractPlanState.BLOCKED and (planned or members or paths):
            _fail("blocked_plan", "nonempty")
        if plan is CodingArchiveExtractPlanState.EMPTY and (planned or members or paths):
            _fail("empty_plan", "nonempty")
        if plan is not CodingArchiveExtractPlanState.BLOCKED and self.authenticated_turn_id is None:
            _fail("authenticated_turn_id", "missing")
        if plan is CodingArchiveExtractPlanState.PLANNED and not planned:
            _fail("planned_members", "missing")

    @property
    def state(self) -> CodingArchiveExtractPlanState:
        return self.plan

    @property
    def closed_plan(self) -> CodingArchiveExtractPlanState:
        return self.plan

    @property
    def planned_paths(self) -> tuple[str, ...]:
        return self.destination_paths

    @property
    def planned_count(self) -> int:
        return self.planned_member_count

    @property
    def decision(self) -> CodingArchiveExtractPlanState:
        return self.plan

    @property
    def destinations(self) -> tuple[str, ...]:
        return self.destination_paths

    @property
    def closed_reason(self) -> CodingArchiveExtractPlanReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "plan": self.plan.value,
            "planned_member_count": self.planned_member_count,
            "member_count": self.member_count,
            "destination_paths": list(self.destination_paths),
            "reason": self.reason.value,
        }


ExtractPlanState = CodingArchiveExtractPlanState
ExtractPlanReason = CodingArchiveExtractPlanReason
CodingArchiveExtractPlan = CodingArchiveExtractPlanV1
CodingArchiveExtractPlanDecision = CodingArchiveExtractPlanState


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "plan_id",
        "authenticated_turn_id",
        "catalog",
        "member_catalog",
        "catalog_facts",
        "admission",
        "extract_admission",
        "admission_facts",
        "plan",
        "state",
        "planned_member_count",
        "member_count",
        "destination_paths",
        "destinations",
        "reason",
    }
    if set(raw) - known:
        _fail("plan", "unknown_fields")


def build_coding_archive_extract_plan(
    plan_id: str | Mapping[str, Any],
    authenticated_turn_id: str | object = None,
    catalog: object = None,
    admission: object = None,
) -> CodingArchiveExtractPlanV1:
    """Build relative extraction destinations from admitted member metadata."""

    if (
        not isinstance(plan_id, Mapping)
        and authenticated_turn_id is not None
        and not isinstance(authenticated_turn_id, str)
    ):
        if admission is not None:
            _fail("plan", "duplicate_arguments")
        admission = catalog
        catalog = authenticated_turn_id
        authenticated_turn_id = None

    if isinstance(plan_id, Mapping):
        raw = plan_id
        _known_mapping_keys(raw)
        if raw.get("schema", CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA) != CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA:
            _fail("schema")
        output_keys = {
            "plan",
            "state",
            "planned_member_count",
            "member_count",
            "destination_paths",
            "destinations",
            "reason",
        }
        fact_keys = {
            "catalog",
            "member_catalog",
            "catalog_facts",
            "admission",
            "extract_admission",
            "admission_facts",
        }
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("plan", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingArchiveExtractPlanV1(
                plan_id=cast(str, raw.get("plan_id")),
                authenticated_turn_id=cast(str | None, raw.get("authenticated_turn_id")),
                plan=cast(CodingArchiveExtractPlanState, raw.get("plan", raw.get("state"))),
                planned_member_count=cast(int, raw.get("planned_member_count")),
                member_count=cast(int, raw.get("member_count")),
                destination_paths=_paths(raw.get("destination_paths", raw.get("destinations", ()))),
                reason=cast(CodingArchiveExtractPlanReason, raw.get("reason")),
            )
        plan_id = cast(str, raw.get("plan_id"))
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        catalog = raw.get("catalog", raw.get("member_catalog", raw.get("catalog_facts", catalog)))
        admission = raw.get(
            "admission",
            raw.get("extract_admission", raw.get("admission_facts", admission)),
        )

    plan_key = _identifier(plan_id, field="plan_id", maximum=MAX_PLAN_ID_CHARS)
    catalog_value = _catalog(catalog)
    admission_value = _admission(admission)
    if catalog_value is None:
        turn_key = (
            _identifier(
                authenticated_turn_id,
                field="authenticated_turn_id",
                maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
            )
            if isinstance(authenticated_turn_id, str)
            else None
        )
        return _result(
            plan_key,
            turn_key,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.CATALOG_INVALID,
        )
    if catalog_value.catalog is CodingArchiveMemberCatalogState.BLOCKED:
        return _result(
            plan_key,
            catalog_value.authenticated_turn_id,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.CATALOG_INVALID,
        )
    expected_turn = catalog_value.authenticated_turn_id
    if admission_value is None:
        return _result(
            plan_key,
            expected_turn,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.ADMISSION_INVALID,
        )
    if (
        admission_value.authenticated_turn_id != expected_turn
        or authenticated_turn_id is not None
        and authenticated_turn_id != expected_turn
    ):
        return _result(
            plan_key,
            expected_turn,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.IDENTITY_MISMATCH,
        )
    if catalog_value.catalog is CodingArchiveMemberCatalogState.EMPTY:
        if admission_value.admission is CodingArchiveExtractAdmissionState.EMPTY:
            return _result(
                plan_key,
                expected_turn,
                CodingArchiveExtractPlanState.EMPTY,
                CodingArchiveExtractPlanReason.NO_MEMBERS,
            )
        return _result(
            plan_key,
            expected_turn,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.ADMISSION_NOT_GRANTED,
        )
    if catalog_value.catalog is not CodingArchiveMemberCatalogState.CATALOGUED:
        return _result(
            plan_key,
            expected_turn,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.CATALOG_INVALID,
        )
    if admission_value.admission is not CodingArchiveExtractAdmissionState.ADMITTED:
        return _result(
            plan_key,
            expected_turn,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.ADMISSION_NOT_GRANTED,
        )
    if admission_value.member_count != catalog_value.member_count:
        return _result(
            plan_key,
            expected_turn,
            CodingArchiveExtractPlanState.BLOCKED,
            CodingArchiveExtractPlanReason.IDENTITY_MISMATCH,
        )
    try:
        paths = _paths(tuple(member.path for member in catalog_value.members))
    except CodingArchiveExtractPlanError as exc:
        reason = (
            CodingArchiveExtractPlanReason.DESTINATION_COLLISION
            if "collision" in str(exc)
            else CodingArchiveExtractPlanReason.UNSAFE_DESTINATION
        )
        return _result(plan_key, expected_turn, CodingArchiveExtractPlanState.BLOCKED, reason)
    return _result(
        plan_key,
        expected_turn,
        CodingArchiveExtractPlanState.PLANNED,
        CodingArchiveExtractPlanReason.ALL_DESTINATIONS_PLANNED,
        paths=paths,
    )


def validate_coding_archive_extract_plan(value: object) -> bool:
    """Return whether a frozen plan or serialized plan is valid."""

    try:
        if isinstance(value, CodingArchiveExtractPlanV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        if value.get("schema") != CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA:
            return False
        required = {
            "schema",
            "plan_id",
            "authenticated_turn_id",
            "plan",
            "planned_member_count",
            "member_count",
            "destination_paths",
            "reason",
        }
        if set(value) != required:
            return False
        return (
            CodingArchiveExtractPlanV1(
                plan_id=cast(str, value.get("plan_id")),
                authenticated_turn_id=cast(str | None, value.get("authenticated_turn_id")),
                plan=cast(CodingArchiveExtractPlanState, value.get("plan")),
                planned_member_count=cast(int, value.get("planned_member_count")),
                member_count=cast(int, value.get("member_count")),
                destination_paths=_paths(value.get("destination_paths")),
                reason=cast(CodingArchiveExtractPlanReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


plan_coding_archive_extract = build_coding_archive_extract_plan
build_archive_extract_plan = build_coding_archive_extract_plan
validate_archive_extract_plan = validate_coding_archive_extract_plan


__all__ = [
    "CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_DESTINATION_PATH_CHARS",
    "MAX_PLAN_ID_CHARS",
    "MAX_PLANNED_MEMBER_COUNT",
    "CodingArchiveExtractPlan",
    "CodingArchiveExtractPlanDecision",
    "CodingArchiveExtractPlanError",
    "CodingArchiveExtractPlanReason",
    "CodingArchiveExtractPlanState",
    "CodingArchiveExtractPlanV1",
    "ExtractPlanReason",
    "ExtractPlanState",
    "build_archive_extract_plan",
    "build_coding_archive_extract_plan",
    "plan_coding_archive_extract",
    "validate_archive_extract_plan",
    "validate_coding_archive_extract_plan",
]
