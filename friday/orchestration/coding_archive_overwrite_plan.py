"""Pure overwrite/case-fold collision plan for archive destinations.

The planner consumes relative destinations and caller-supplied existence facts.
It never checks a directory, opens a path, or overwrites anything.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_archive_extract_plan import (
    CodingArchiveExtractPlanState,
    CodingArchiveExtractPlanV1,
    build_coding_archive_extract_plan,
)

CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA = "friday.coding-archive-overwrite-plan.v1"
MAX_OVERWRITE_PLAN_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_OVERWRITE_PATH_CHARS = 4_096
MAX_OVERWRITE_DESTINATION_COUNT = 4_096

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingArchiveOverwritePlanError(ValueError):
    """An overwrite-plan identity, path, or existence fact is malformed."""


class CodingArchiveOverwritePlanState(StrEnum):
    """Closed overwrite-plan outcomes."""

    EMPTY = "empty"
    CLEAR = "clear"
    COLLISION = "collision"
    BLOCKED = "blocked"


class CodingArchiveOverwritePlanReason(StrEnum):
    """Closed reason for one overwrite plan."""

    NO_DESTINATIONS = "no_destinations"
    NO_COLLISIONS = "no_collisions"
    CASEFOLD_COLLISION = "casefold_collision"
    EXISTING_DESTINATION = "existing_destination"
    PLAN_INVALID = "plan_invalid"
    UNSAFE_DESTINATION = "unsafe_destination"
    INVALID_FACTS = "invalid_facts"

    CASE_FOLD_COLLISION = CASEFOLD_COLLISION
    EXISTING_DESTINATIONS = EXISTING_DESTINATION


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingArchiveOverwritePlanError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _path_parts(value: object, *, field: str) -> tuple[str, ...]:
    if type(value) is not str or not value or len(value) > MAX_OVERWRITE_PATH_CHARS:
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


def _canonical_path(value: object, *, field: str) -> tuple[str, str]:
    parts = _path_parts(value, field=field)
    path = "/".join(parts)
    return path, unicodedata.normalize("NFC", path).casefold()


def _destination_paths(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("destination_paths", "sequence")
    if len(value) > MAX_OVERWRITE_DESTINATION_COUNT:
        _fail("destination_paths", "count")
    return tuple(_canonical_path(item, field="destination_paths")[0] for item in value)


@dataclass(frozen=True, slots=True)
class CodingArchiveExistingDestinationFactV1:
    """Frozen caller-observed existence fact for one destination."""

    path: str
    exists: bool

    def __post_init__(self) -> None:
        _canonical_path(self.path, field="path")
        if type(self.exists) is not bool:
            _fail("exists", "boolean")

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists}


@dataclass(frozen=True, slots=True)
class CodingArchiveOverwriteInputV1:
    """Frozen overwrite inputs for callers that already have both facts."""

    destination_paths: tuple[str, ...] = ()
    existing_destinations: tuple[CodingArchiveExistingDestinationFactV1, ...] = ()


def _existing_fact(value: object) -> CodingArchiveExistingDestinationFactV1:
    if isinstance(value, CodingArchiveExistingDestinationFactV1):
        return value
    if isinstance(value, str):
        return CodingArchiveExistingDestinationFactV1(value, True)
    if not isinstance(value, Mapping):
        _fail("existing_destination", "type")
    allowed = {"path", "destination", "destination_path", "exists", "present", "existing"}
    if set(value) - allowed:
        _fail("existing_destination", "unknown_fields")
    path = value.get("path", value.get("destination", value.get("destination_path")))
    exists = value.get("exists", value.get("present", value.get("existing")))
    return CodingArchiveExistingDestinationFactV1(cast(str, path), cast(bool, exists))


def _existing_facts(value: object) -> tuple[CodingArchiveExistingDestinationFactV1, ...]:
    if isinstance(value, CodingArchiveOverwriteInputV1):
        return value.existing_destinations
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("existing_destinations", "sequence")
    if len(value) > MAX_OVERWRITE_DESTINATION_COUNT:
        _fail("existing_destinations", "count")
    return tuple(_existing_fact(item) for item in value)


def _state(value: object) -> CodingArchiveOverwritePlanState:
    try:
        return CodingArchiveOverwritePlanState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveOverwritePlanError("plan_closed") from exc


def _reason(value: object) -> CodingArchiveOverwritePlanReason:
    try:
        return CodingArchiveOverwritePlanReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveOverwritePlanError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class CodingArchiveOverwritePlanV1:
    """Immutable overwrite decision and the paths it would affect."""

    plan_id: str
    authenticated_turn_id: str | None
    plan: CodingArchiveOverwritePlanState
    destination_paths: tuple[str, ...]
    collision_paths: tuple[str, ...]
    existing_destination_paths: tuple[str, ...]
    reason: CodingArchiveOverwritePlanReason

    def __post_init__(self) -> None:
        _identifier(self.plan_id, field="plan_id", maximum=MAX_OVERWRITE_PLAN_ID_CHARS)
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
        destinations = _destination_paths(self.destination_paths)
        collisions = _destination_paths(self.collision_paths)
        existing = _destination_paths(self.existing_destination_paths)
        object.__setattr__(self, "destination_paths", destinations)
        object.__setattr__(self, "collision_paths", collisions)
        object.__setattr__(self, "existing_destination_paths", existing)
        destination_fold_set = {unicodedata.normalize("NFC", path).casefold() for path in destinations}
        if any(
            unicodedata.normalize("NFC", path).casefold() not in destination_fold_set for path in collisions
        ):
            _fail("collision_paths", "outside_destinations")
        if plan is CodingArchiveOverwritePlanState.BLOCKED and (destinations or collisions or existing):
            _fail("blocked_paths", "nonempty")
        if plan is CodingArchiveOverwritePlanState.EMPTY and (destinations or collisions or existing):
            _fail("empty_paths", "nonempty")
        if plan is not CodingArchiveOverwritePlanState.BLOCKED and self.authenticated_turn_id is None:
            _fail("authenticated_turn_id", "missing")
        if (
            plan in {CodingArchiveOverwritePlanState.CLEAR, CodingArchiveOverwritePlanState.COLLISION}
            and not destinations
        ):
            _fail("planned_paths", "missing")
        if plan is CodingArchiveOverwritePlanState.CLEAR and collisions:
            _fail("clear_collisions", "nonempty")
        if plan is CodingArchiveOverwritePlanState.COLLISION and not collisions:
            _fail("collision_paths", "missing")
        destination_folds: dict[str, list[str]] = {}
        for path in destinations:
            destination_folds.setdefault(unicodedata.normalize("NFC", path).casefold(), []).append(path)
        internal_collisions = {
            path for paths in destination_folds.values() if len(paths) > 1 for path in paths
        }
        if plan is CodingArchiveOverwritePlanState.CLEAR and internal_collisions:
            _fail("clear_collisions", "missing")
        if plan is CodingArchiveOverwritePlanState.COLLISION and not internal_collisions.issubset(collisions):
            _fail("collision_paths", "incomplete")

    @property
    def state(self) -> CodingArchiveOverwritePlanState:
        return self.plan

    @property
    def closed_plan(self) -> CodingArchiveOverwritePlanState:
        return self.plan

    @property
    def decision(self) -> CodingArchiveOverwritePlanState:
        return self.plan

    @property
    def paths(self) -> tuple[str, ...]:
        return self.destination_paths

    @property
    def collisions(self) -> tuple[str, ...]:
        return self.collision_paths

    @property
    def existing_paths(self) -> tuple[str, ...]:
        return self.existing_destination_paths

    @property
    def closed_reason(self) -> CodingArchiveOverwritePlanReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "plan": self.plan.value,
            "destination_paths": list(self.destination_paths),
            "collision_paths": list(self.collision_paths),
            "existing_destination_paths": list(self.existing_destination_paths),
            "reason": self.reason.value,
        }


OverwritePlanState = CodingArchiveOverwritePlanState
OverwritePlanReason = CodingArchiveOverwritePlanReason
CodingArchiveOverwritePlan = CodingArchiveOverwritePlanV1
CodingArchiveOverwriteDecision = CodingArchiveOverwritePlanState


def _coerce_extract_plan(value: object) -> CodingArchiveExtractPlanV1 | None:
    try:
        if isinstance(value, CodingArchiveExtractPlanV1):
            value.__post_init__()
            return value
        if isinstance(value, Mapping):
            return build_coding_archive_extract_plan(value)
    except (TypeError, ValueError):
        return None
    return None


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "plan_id",
        "authenticated_turn_id",
        "extract_plan",
        "archive_extract_plan",
        "plan_facts",
        "destination_paths",
        "destinations",
        "existing_destinations",
        "existing_destination_facts",
        "existing_paths",
        "plan",
        "state",
        "collision_paths",
        "collisions",
        "existing_destination_paths",
        "reason",
    }
    if set(raw) - known:
        _fail("overwrite", "unknown_fields")


def _result(
    plan_id: str,
    authenticated_turn_id: str | None,
    state: CodingArchiveOverwritePlanState,
    reason: CodingArchiveOverwritePlanReason,
    *,
    destinations: tuple[str, ...] = (),
    collisions: tuple[str, ...] = (),
    existing: tuple[str, ...] = (),
) -> CodingArchiveOverwritePlanV1:
    if state is CodingArchiveOverwritePlanState.BLOCKED:
        destinations = ()
        collisions = ()
        existing = ()
    return CodingArchiveOverwritePlanV1(
        plan_id=plan_id,
        authenticated_turn_id=authenticated_turn_id,
        plan=state,
        destination_paths=destinations,
        collision_paths=collisions,
        existing_destination_paths=existing,
        reason=reason,
    )


def build_coding_archive_overwrite_plan(
    plan_id: str | Mapping[str, Any],
    authenticated_turn_id: str | object = None,
    extract_plan: object = None,
    existing_destinations: object = (),
    *,
    destination_paths: object = None,
    existing_paths: object = None,
) -> CodingArchiveOverwritePlanV1:
    """Build a no-overwrite plan from relative paths and existence facts."""

    if (
        not isinstance(plan_id, Mapping)
        and authenticated_turn_id is not None
        and not isinstance(authenticated_turn_id, str)
    ):
        if existing_destinations != () and extract_plan is not None:
            _fail("overwrite", "duplicate_arguments")
        if extract_plan is not None:
            existing_destinations = extract_plan
        extract_plan = authenticated_turn_id
        authenticated_turn_id = None
    if destination_paths is not None and extract_plan is not None:
        _fail("destinations", "duplicate_arguments")
    if existing_paths is not None:
        if existing_destinations != ():
            _fail("existing_destinations", "duplicate_arguments")
        existing_destinations = existing_paths

    if isinstance(plan_id, Mapping):
        raw = plan_id
        _known_mapping_keys(raw)
        if raw.get("schema", CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA) != CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA:
            _fail("schema")
        output_keys = {
            "plan",
            "state",
            "destination_paths",
            "destinations",
            "collision_paths",
            "collisions",
            "existing_destination_paths",
            "reason",
        }
        fact_keys = {
            "extract_plan",
            "archive_extract_plan",
            "plan_facts",
            "existing_destinations",
            "existing_destination_facts",
            "existing_paths",
        }
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("overwrite", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingArchiveOverwritePlanV1(
                plan_id=cast(str, raw.get("plan_id")),
                authenticated_turn_id=cast(str | None, raw.get("authenticated_turn_id")),
                plan=cast(CodingArchiveOverwritePlanState, raw.get("plan", raw.get("state"))),
                destination_paths=_destination_paths(
                    raw.get("destination_paths", raw.get("destinations", ()))
                ),
                collision_paths=_destination_paths(raw.get("collision_paths", raw.get("collisions", ()))),
                existing_destination_paths=_destination_paths(raw.get("existing_destination_paths", ())),
                reason=cast(CodingArchiveOverwritePlanReason, raw.get("reason")),
            )
        plan_id = cast(str, raw.get("plan_id"))
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        extract_plan = raw.get(
            "extract_plan",
            raw.get("archive_extract_plan", raw.get("plan_facts", extract_plan)),
        )
        destination_paths = raw.get("destination_paths", raw.get("destinations", destination_paths))
        existing_destinations = raw.get(
            "existing_destinations",
            raw.get("existing_destination_facts", raw.get("existing_paths", existing_destinations)),
        )

    plan_key = _identifier(plan_id, field="plan_id", maximum=MAX_OVERWRITE_PLAN_ID_CHARS)
    if isinstance(extract_plan, CodingArchiveOverwriteInputV1):
        if destination_paths is not None or existing_destinations != ():
            _fail("overwrite", "duplicate_arguments")
        destination_paths = extract_plan.destination_paths
        existing_destinations = extract_plan.existing_destinations
        extract_plan = None
    plan_value = _coerce_extract_plan(extract_plan)
    raw_destinations: object
    if plan_value is not None:
        turn_key = plan_value.authenticated_turn_id
        if authenticated_turn_id is not None and authenticated_turn_id != turn_key:
            return _result(
                plan_key,
                turn_key,
                CodingArchiveOverwritePlanState.BLOCKED,
                CodingArchiveOverwritePlanReason.INVALID_FACTS,
            )
        if plan_value.plan is CodingArchiveExtractPlanState.EMPTY:
            return _result(
                plan_key,
                turn_key,
                CodingArchiveOverwritePlanState.EMPTY,
                CodingArchiveOverwritePlanReason.NO_DESTINATIONS,
            )
        if plan_value.plan is not CodingArchiveExtractPlanState.PLANNED:
            return _result(
                plan_key,
                turn_key,
                CodingArchiveOverwritePlanState.BLOCKED,
                CodingArchiveOverwritePlanReason.PLAN_INVALID,
            )
        raw_destinations = plan_value.destination_paths
    else:
        if extract_plan is not None:
            if authenticated_turn_id is None:
                _fail("authenticated_turn_id", "id")
            turn_key = _identifier(
                authenticated_turn_id,
                field="authenticated_turn_id",
                maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
            )
            return _result(
                plan_key,
                turn_key,
                CodingArchiveOverwritePlanState.BLOCKED,
                CodingArchiveOverwritePlanReason.PLAN_INVALID,
            )
        if authenticated_turn_id is None:
            _fail("authenticated_turn_id", "id")
        turn_key = _identifier(
            authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        raw_destinations = destination_paths if destination_paths is not None else ()

    try:
        destinations = _destination_paths(raw_destinations)
        existing_facts = _existing_facts(existing_destinations)
    except CodingArchiveOverwritePlanError:
        return _result(
            plan_key,
            turn_key,
            CodingArchiveOverwritePlanState.BLOCKED,
            CodingArchiveOverwritePlanReason.INVALID_FACTS,
        )
    if not destinations:
        return _result(
            plan_key,
            turn_key,
            CodingArchiveOverwritePlanState.EMPTY,
            CodingArchiveOverwritePlanReason.NO_DESTINATIONS,
        )

    destination_folds: dict[str, list[str]] = {}
    for path in destinations:
        folded = unicodedata.normalize("NFC", path).casefold()
        destination_folds.setdefault(folded, []).append(path)
    collision_paths = {path for paths in destination_folds.values() if len(paths) > 1 for path in paths}
    existing_paths_by_fold: dict[str, list[str]] = {}
    for fact in existing_facts:
        if fact.exists:
            path, folded = _canonical_path(fact.path, field="existing_destination")
            existing_paths_by_fold.setdefault(folded, []).append(path)
    for folded, paths in destination_folds.items():
        if folded in existing_paths_by_fold:
            collision_paths.update(paths)

    ordered_collisions = tuple(path for path in destinations if path in collision_paths)
    ordered_existing = tuple(
        path
        for fact in existing_facts
        if fact.exists
        for path, folded in (_canonical_path(fact.path, field="existing_destination"),)
        if any(folded == destination_fold for destination_fold in destination_folds)
    )
    if ordered_collisions:
        reason = (
            CodingArchiveOverwritePlanReason.CASEFOLD_COLLISION
            if any(len(paths) > 1 for paths in destination_folds.values())
            else CodingArchiveOverwritePlanReason.EXISTING_DESTINATION
        )
        return _result(
            plan_key,
            turn_key,
            CodingArchiveOverwritePlanState.COLLISION,
            reason,
            destinations=destinations,
            collisions=ordered_collisions,
            existing=ordered_existing,
        )
    return _result(
        plan_key,
        turn_key,
        CodingArchiveOverwritePlanState.CLEAR,
        CodingArchiveOverwritePlanReason.NO_COLLISIONS,
        destinations=destinations,
    )


def validate_coding_archive_overwrite_plan(value: object) -> bool:
    """Return whether a frozen overwrite plan or serialized plan is valid."""

    try:
        if isinstance(value, CodingArchiveOverwritePlanV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        if value.get("schema") != CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA:
            return False
        required = {
            "schema",
            "plan_id",
            "authenticated_turn_id",
            "plan",
            "destination_paths",
            "collision_paths",
            "existing_destination_paths",
            "reason",
        }
        if set(value) != required:
            return False
        return (
            CodingArchiveOverwritePlanV1(
                plan_id=cast(str, value.get("plan_id")),
                authenticated_turn_id=cast(str | None, value.get("authenticated_turn_id")),
                plan=cast(CodingArchiveOverwritePlanState, value.get("plan")),
                destination_paths=_destination_paths(value.get("destination_paths")),
                collision_paths=_destination_paths(value.get("collision_paths")),
                existing_destination_paths=_destination_paths(value.get("existing_destination_paths")),
                reason=cast(CodingArchiveOverwritePlanReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


plan_coding_archive_overwrites = build_coding_archive_overwrite_plan
build_archive_overwrite_plan = build_coding_archive_overwrite_plan
validate_archive_overwrite_plan = validate_coding_archive_overwrite_plan


__all__ = [
    "CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_OVERWRITE_DESTINATION_COUNT",
    "MAX_OVERWRITE_PATH_CHARS",
    "MAX_OVERWRITE_PLAN_ID_CHARS",
    "CodingArchiveExistingDestinationFactV1",
    "CodingArchiveOverwriteDecision",
    "CodingArchiveOverwriteInputV1",
    "CodingArchiveOverwritePlan",
    "CodingArchiveOverwritePlanError",
    "CodingArchiveOverwritePlanReason",
    "CodingArchiveOverwritePlanState",
    "CodingArchiveOverwritePlanV1",
    "OverwritePlanReason",
    "OverwritePlanState",
    "build_archive_overwrite_plan",
    "build_coding_archive_overwrite_plan",
    "plan_coding_archive_overwrites",
    "validate_archive_overwrite_plan",
    "validate_coding_archive_overwrite_plan",
]
