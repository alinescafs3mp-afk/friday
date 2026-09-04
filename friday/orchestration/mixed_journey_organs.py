"""Closed presence facts for the organs participating in a mixed journey."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_ORGANS_SCHEMA = "friday.mixed-journey-organs.v1"
ORGAN_NAMES = ("file", "archive", "conversation", "web", "table", "engineer", "coding")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class MixedJourneyOrgansError(ValueError):
    """A mixed-journey organ fact is malformed."""


class MixedJourneyOrgan(StrEnum):
    FILE = "file"
    ARCHIVE = "archive"
    CONVERSATION = "conversation"
    WEB = "web"
    TABLE = "table"
    ENGINEER = "engineer"
    CODING = "coding"


class MixedJourneyOrgansState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    BLOCKED = "blocked"


class MixedJourneyOrgansReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    INVALID_FACTS = "invalid_facts"
    UNKNOWN_ORGAN = "unknown_organ"


@dataclass(frozen=True, slots=True)
class MixedJourneyOrgansFactsV1:
    file: bool | None = None
    archive: bool | None = None
    conversation: bool | None = None
    web: bool | None = None
    table: bool | None = None
    engineer: bool | None = None
    coding: bool | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyOrgansError(f"{field}_{detail}")


def _id(value: object, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _organ(value: object) -> str:
    if isinstance(value, MixedJourneyOrgan):
        return value.value
    if type(value) is not str:
        _fail("organ")
    result = cast(str, value).strip().casefold()
    if result not in ORGAN_NAMES:
        _fail("organ", "unknown")
    return result


@dataclass(frozen=True, slots=True)
class MixedJourneyOrgansV1:
    journey_id: str
    authenticated_turn_id: str
    state: MixedJourneyOrgansState
    organ_presence: tuple[tuple[str, bool], ...]
    reason: MixedJourneyOrgansReason

    def __post_init__(self) -> None:
        _id(self.journey_id, "journey_id")
        _id(self.authenticated_turn_id, "authenticated_turn_id")
        try:
            state = MixedJourneyOrgansState(self.state)
            reason = MixedJourneyOrgansReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyOrgansError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if not isinstance(self.organ_presence, tuple):
            _fail("organ_presence", "immutable")
        seen: set[str] = set()
        for name, present in self.organ_presence:
            key = _organ(name)
            if key in seen or type(present) is not bool:
                _fail("organ_presence")
            seen.add(key)
        if state is MixedJourneyOrgansState.PRESENT and seen != set(ORGAN_NAMES):
            _fail("present", "facts")
        if state is not MixedJourneyOrgansState.PRESENT and self.organ_presence:
            _fail("non_present", "leak")

    @property
    def organs_state(self) -> MixedJourneyOrgansState:
        return self.state

    @property
    def decision(self) -> MixedJourneyOrgansState:
        return self.state

    @property
    def present_organs(self) -> tuple[str, ...]:
        return tuple(name for name, present in self.organ_presence if present)

    @property
    def absent_organs(self) -> tuple[str, ...]:
        return tuple(name for name, present in self.organ_presence if not present)

    @property
    def organs(self) -> tuple[tuple[str, bool], ...]:
        return self.organ_presence

    @property
    def presence(self) -> tuple[tuple[str, bool], ...]:
        return self.organ_presence

    def is_present(self, organ: str | MixedJourneyOrgan) -> bool:
        key = _organ(organ)
        return dict(self.organ_presence).get(key, False)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_ORGANS_SCHEMA,
            "journey_id": self.journey_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "organ_presence": {name: present for name, present in self.organ_presence},
            "present_organs": list(self.present_organs),
            "absent_organs": list(self.absent_organs),
            "reason": self.reason.value,
        }


MixedJourneyOrganPresence = MixedJourneyOrgansV1
MixedJourneyOrgans = MixedJourneyOrgansV1
MixedJourneyOrgansFacts = MixedJourneyOrgansFactsV1
MixedJourneyOrganName = MixedJourneyOrgan
OrgansState = MixedJourneyOrgansState
OrgansReason = MixedJourneyOrgansReason


def _empty(journey_id: str, turn_id: str, reason: MixedJourneyOrgansReason) -> MixedJourneyOrgansV1:
    return MixedJourneyOrgansV1(journey_id, turn_id, MixedJourneyOrgansState.EMPTY, (), reason)


def _blocked(journey_id: str, turn_id: str, reason: MixedJourneyOrgansReason) -> MixedJourneyOrgansV1:
    return MixedJourneyOrgansV1(journey_id, turn_id, MixedJourneyOrgansState.BLOCKED, (), reason)


def _mapping_facts(raw: Mapping[str, Any]) -> dict[str, bool | None]:
    allowed = {
        "schema",
        "journey_id",
        "authenticated_turn_id",
        "turn_id",
        "organs",
        "organ_presence",
        "present_organs",
        "absent_organs",
        "state",
        "reason",
        *ORGAN_NAMES,
    }
    values: dict[str, bool | None] = {}
    for raw_name, value in raw.items():
        if type(raw_name) is str and raw_name.casefold() in ORGAN_NAMES:
            name = raw_name.casefold()
            if name in values:
                _fail("organ", "duplicate")
            values[name] = value
        elif raw_name not in allowed:
            _fail("facts", "unknown")
    supplied = raw.get("organs", raw.get("organ_presence"))
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            _fail("organs", "mapping")
        for key, value in supplied.items():
            name = _organ(key)
            if name in values:
                _fail("organ", "duplicate")
            values[name] = value
    for field in ("present_organs", "absent_organs"):
        listed = raw.get(field)
        if listed is None:
            continue
        if isinstance(listed, (str, bytes, bytearray)) or not isinstance(listed, (list, tuple)):
            _fail(field, "sequence")
        names = [_organ(name) for name in listed]
        if len(set(names)) != len(names):
            _fail(field, "duplicate")
        expected = {name for name, present in values.items() if present is (field == "present_organs")}
        if set(names) != expected:
            _fail(field, "mismatch")
    return values


def _parse_values(values: Mapping[str, object]) -> tuple[tuple[str, bool], ...]:
    if set(values) != set(ORGAN_NAMES):
        _fail("organ", "incomplete")
    result: list[tuple[str, bool]] = []
    for name in ORGAN_NAMES:
        value = values[name]
        if type(value) is not bool:
            _fail(name, "boolean")
        result.append((name, cast(bool, value)))
    return tuple(result)


def build_mixed_journey_organs(
    journey_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    *,
    facts: MixedJourneyOrgansFactsV1 | Mapping[str, Any] | None = None,
) -> MixedJourneyOrgansV1:
    """Admit closed boolean presence facts for all seven known organs."""

    if isinstance(journey_id, Mapping):
        raw = journey_id
        key = cast(str, raw.get("journey_id", "journey"))
        turn = cast(str, raw.get("authenticated_turn_id", raw.get("turn_id", "turn")))
        try:
            key, turn = _id(key, "journey_id"), _id(turn, "authenticated_turn_id")
            if raw.get("schema", MIXED_JOURNEY_ORGANS_SCHEMA) != MIXED_JOURNEY_ORGANS_SCHEMA:
                _fail("schema")
            if "state" in raw and raw.get("state") != MixedJourneyOrgansState.PRESENT.value:
                state = MixedJourneyOrgansState(raw["state"])
                reason = MixedJourneyOrgansReason(raw.get("reason", "invalid_facts"))
                return (
                    _blocked(key, turn, reason)
                    if state is MixedJourneyOrgansState.BLOCKED
                    else _empty(key, turn, reason)
                )
            values = _mapping_facts(raw)
            return _build_values(key, turn, values)
        except (TypeError, ValueError, MixedJourneyOrgansError):
            try:
                return _blocked(
                    _id(key, "journey_id"),
                    _id(turn, "authenticated_turn_id"),
                    MixedJourneyOrgansReason.INVALID_FACTS,
                )
            except MixedJourneyOrgansError:
                return _blocked("journey", "turn", MixedJourneyOrgansReason.INVALID_FACTS)
    key = _id(journey_id, "journey_id")
    turn = _id(authenticated_turn_id, "authenticated_turn_id")
    if facts is None:
        return _empty(key, turn, MixedJourneyOrgansReason.NO_FACTS)
    try:
        if isinstance(facts, MixedJourneyOrgansFactsV1):
            values = {name: getattr(facts, name) for name in ORGAN_NAMES}
        elif isinstance(facts, Mapping):
            if facts.get("schema", MIXED_JOURNEY_ORGANS_SCHEMA) != MIXED_JOURNEY_ORGANS_SCHEMA:
                _fail("schema")
            values = _mapping_facts(facts)
        else:
            _fail("facts", "type")
        return _build_values(key, turn, values)
    except (TypeError, ValueError, MixedJourneyOrgansError):
        return _blocked(key, turn, MixedJourneyOrgansReason.INVALID_FACTS)


def _build_values(key: str, turn: str, values: Mapping[str, object]) -> MixedJourneyOrgansV1:
    if not values or all(value is None for value in values.values()):
        return _empty(key, turn, MixedJourneyOrgansReason.NO_FACTS)
    try:
        presence = _parse_values(values)
    except (TypeError, ValueError, MixedJourneyOrgansError):
        return _blocked(key, turn, MixedJourneyOrgansReason.INVALID_FACTS)
    return MixedJourneyOrgansV1(
        key, turn, MixedJourneyOrgansState.PRESENT, presence, MixedJourneyOrgansReason.PRESENT
    )


def validate_mixed_journey_organs(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyOrgansV1)
            else build_mixed_journey_organs(cast(Mapping[str, Any], value))
        )
        return (
            isinstance(result, MixedJourneyOrgansV1) and result.state is not MixedJourneyOrgansState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_journey_organs = build_mixed_journey_organs
validate_journey_organs = validate_mixed_journey_organs

__all__ = [
    "MIXED_JOURNEY_ORGANS_SCHEMA",
    "ORGAN_NAMES",
    "MixedJourneyOrgan",
    "MixedJourneyOrganName",
    "MixedJourneyOrgans",
    "MixedJourneyOrgansError",
    "MixedJourneyOrgansFacts",
    "MixedJourneyOrgansFactsV1",
    "MixedJourneyOrgansReason",
    "MixedJourneyOrgansState",
    "MixedJourneyOrgansV1",
    "OrgansReason",
    "OrgansState",
    "build_journey_organs",
    "build_mixed_journey_organs",
    "validate_journey_organs",
    "validate_mixed_journey_organs",
]
