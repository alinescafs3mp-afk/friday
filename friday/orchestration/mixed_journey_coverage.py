"""Body-free coverage facts for the organs in a mixed journey."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.mixed_journey_organs import (
    ORGAN_NAMES,
    MixedJourneyOrgansFactsV1,
    MixedJourneyOrgansState,
    MixedJourneyOrgansV1,
    build_mixed_journey_organs,
)

MIXED_JOURNEY_COVERAGE_SCHEMA = "friday.mixed-journey-coverage.v1"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_SUMMARIES = len(ORGAN_NAMES)


class MixedJourneyCoverageError(ValueError):
    """A body-free mixed-journey coverage fact is malformed."""


class MixedJourneyCoverageState(StrEnum):
    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class MixedJourneyCoverageReason(StrEnum):
    NO_FACTS = "no_facts"
    PARTIAL = "partial"
    COMPLETE = "complete"
    INVALID_FACTS = "invalid_facts"
    UNKNOWN_ORGAN = "unknown_organ"
    ORGANS_BLOCKED = "organs_blocked"


@dataclass(frozen=True, slots=True)
class MixedJourneyCoverageFactsV1:
    organs: MixedJourneyOrgansV1 | MixedJourneyOrgansFactsV1 | Mapping[str, Any] | None = None
    summaries: Mapping[str, object] | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyCoverageError(f"{field}_{detail}")


def _id(value: object, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _digest(value: object, *, field: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(field, "digest")
    return cast(str, value)


def _summary_digest(value: object) -> str:
    if type(value) is str:
        return _digest(value, field="summary")
    if not isinstance(value, Mapping):
        _fail("summary", "body")
    allowed = {"digest", "summary_digest", "artifact_digest", "sha256", "count", "class"}
    if set(value) - allowed:
        _fail("summary", "body")
    digest = value.get(
        "digest", value.get("summary_digest", value.get("artifact_digest", value.get("sha256")))
    )
    return _digest(digest, field="summary")


@dataclass(frozen=True, slots=True)
class MixedJourneyCoverageV1:
    journey_id: str
    authenticated_turn_id: str
    state: MixedJourneyCoverageState
    covered_organs: tuple[str, ...]
    missing_organs: tuple[str, ...]
    summary_digests: tuple[tuple[str, str], ...]
    reason: MixedJourneyCoverageReason

    def __post_init__(self) -> None:
        _id(self.journey_id, "journey_id")
        _id(self.authenticated_turn_id, "authenticated_turn_id")
        try:
            state = MixedJourneyCoverageState(self.state)
            reason = MixedJourneyCoverageReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyCoverageError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        covered = tuple(self.covered_organs)
        missing = tuple(self.missing_organs)
        if len(set(covered)) != len(covered) or len(set(missing)) != len(missing):
            _fail("organs", "duplicate")
        for name in (*covered, *missing):
            if name not in ORGAN_NAMES:
                _fail("organ", "unknown")
        if set(covered) & set(missing):
            _fail("organs", "overlap")
        if not isinstance(self.summary_digests, tuple) or len(self.summary_digests) > MAX_SUMMARIES:
            _fail("summary_digests", "count")
        digest_names: set[str] = set()
        for name, digest in self.summary_digests:
            if name not in ORGAN_NAMES or name in digest_names:
                _fail("summary", "organ")
            _digest(digest, field="summary")
            digest_names.add(name)
        if not digest_names <= set(covered):
            _fail("summary", "organ")
        if state is MixedJourneyCoverageState.COMPLETE and set(missing):
            _fail("complete", "missing")
        if state is MixedJourneyCoverageState.PARTIAL and not set(missing):
            _fail("partial", "missing")
        if state in {MixedJourneyCoverageState.EMPTY, MixedJourneyCoverageState.BLOCKED} and (
            covered or missing or self.summary_digests
        ):
            _fail("non_complete", "leak")

    @property
    def coverage_state(self) -> MixedJourneyCoverageState:
        return self.state

    @property
    def decision(self) -> MixedJourneyCoverageState:
        return self.state

    @property
    def covered(self) -> tuple[str, ...]:
        return self.covered_organs

    @property
    def missing(self) -> tuple[str, ...]:
        return self.missing_organs

    @property
    def digest_by_organ(self) -> tuple[tuple[str, str], ...]:
        return self.summary_digests

    @property
    def summary_digest_by_organ(self) -> tuple[tuple[str, str], ...]:
        return self.summary_digests

    @property
    def complete(self) -> bool:
        return self.state is MixedJourneyCoverageState.COMPLETE

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_COVERAGE_SCHEMA,
            "journey_id": self.journey_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "covered_organs": list(self.covered_organs),
            "missing_organs": list(self.missing_organs),
            "summary_digests": {name: digest for name, digest in self.summary_digests},
            "reason": self.reason.value,
        }


MixedJourneyCoverage = MixedJourneyCoverageV1
MixedJourneyCoverageFacts = MixedJourneyCoverageFactsV1
CoverageState = MixedJourneyCoverageState
CoverageReason = MixedJourneyCoverageReason


def _empty(key: str, turn: str, reason: MixedJourneyCoverageReason) -> MixedJourneyCoverageV1:
    return MixedJourneyCoverageV1(key, turn, MixedJourneyCoverageState.EMPTY, (), (), (), reason)


def _blocked(key: str, turn: str, reason: MixedJourneyCoverageReason) -> MixedJourneyCoverageV1:
    return MixedJourneyCoverageV1(key, turn, MixedJourneyCoverageState.BLOCKED, (), (), (), reason)


def _organs(value: object, *, key: str, turn: str) -> MixedJourneyOrgansV1:
    if isinstance(value, MixedJourneyOrgansV1):
        if value.journey_id != key or value.authenticated_turn_id != turn:
            _fail("organs", "identity")
        return value
    if isinstance(value, MixedJourneyOrgansFactsV1):
        return build_mixed_journey_organs(key, turn, facts=value)
    if isinstance(value, Mapping):
        result = (
            build_mixed_journey_organs(value)
            if value.get("schema")
            else build_mixed_journey_organs(key, turn, facts=value)
        )
        if result.journey_id != key or result.authenticated_turn_id != turn:
            _fail("organs", "identity")
        return result
    _fail("organs", "type")


def _parse_summaries(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail("summaries", "mapping")
    if len(value) > MAX_SUMMARIES:
        _fail("summaries", "count")
    result: dict[str, str] = {}
    for raw_name, raw_summary in value.items():
        if type(raw_name) is not str or raw_name.casefold() not in ORGAN_NAMES:
            _fail("organ", "unknown")
        name = raw_name.casefold()
        if name in result:
            _fail("organ", "duplicate")
        result[name] = _summary_digest(raw_summary)
    return result


def build_mixed_journey_coverage(
    journey_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    organs: MixedJourneyOrgansV1 | MixedJourneyOrgansFactsV1 | Mapping[str, Any] | None = None,
    summaries: Mapping[str, object] | None = None,
    *,
    facts: MixedJourneyCoverageFactsV1 | Mapping[str, Any] | None = None,
    organ_summaries: Mapping[str, object] | None = None,
) -> MixedJourneyCoverageV1:
    """Admit coverage only from immutable, digest-only organ summaries."""

    if isinstance(journey_id, Mapping):
        raw = journey_id
        key = cast(str, raw.get("journey_id", "journey"))
        turn = cast(str, raw.get("authenticated_turn_id", raw.get("turn_id", "turn")))
        try:
            key, turn = _id(key, "journey_id"), _id(turn, "authenticated_turn_id")
            if raw.get("schema", MIXED_JOURNEY_COVERAGE_SCHEMA) != MIXED_JOURNEY_COVERAGE_SCHEMA:
                _fail("schema")
            if raw.get("state") in {"empty", "blocked"}:
                state = MixedJourneyCoverageState(raw["state"])
                reason = MixedJourneyCoverageReason(raw.get("reason", "invalid_facts"))
                return (
                    _blocked(key, turn, reason)
                    if state is MixedJourneyCoverageState.BLOCKED
                    else _empty(key, turn, reason)
                )
            organs = raw.get("organs", raw.get("organ_presence"))
            summaries = raw.get("summary_digests", raw.get("summaries"))
            if organs is None:
                covered = raw.get("covered_organs", ())
                missing = raw.get("missing_organs", ())
                if not isinstance(covered, (list, tuple)) or not isinstance(missing, (list, tuple)):
                    _fail("organs", "inferred")
                if any(
                    type(name) is not str or name.casefold() not in ORGAN_NAMES
                    for name in (*covered, *missing)
                ):
                    _fail("organ", "unknown")
                present = set(covered) | set(missing)
                organs = {name: name in present for name in ORGAN_NAMES}
            return _build(key, turn, organs, summaries)
        except (TypeError, ValueError, MixedJourneyCoverageError):
            try:
                return _blocked(
                    _id(key, "journey_id"),
                    _id(turn, "authenticated_turn_id"),
                    MixedJourneyCoverageReason.INVALID_FACTS,
                )
            except MixedJourneyCoverageError:
                return _blocked("journey", "turn", MixedJourneyCoverageReason.INVALID_FACTS)
    key = _id(journey_id, "journey_id")
    turn = _id(authenticated_turn_id, "authenticated_turn_id")
    if organ_summaries is not None:
        if summaries is not None:
            return _blocked(key, turn, MixedJourneyCoverageReason.INVALID_FACTS)
        summaries = organ_summaries
    if facts is not None:
        try:
            if any(value is not None for value in (organs, summaries)):
                _fail("facts", "duplicate")
            if isinstance(facts, MixedJourneyCoverageFactsV1):
                organs, summaries = facts.organs, facts.summaries
            elif isinstance(facts, Mapping):
                if facts.get("schema", MIXED_JOURNEY_COVERAGE_SCHEMA) != MIXED_JOURNEY_COVERAGE_SCHEMA:
                    _fail("schema")
                organs = facts.get("organs", facts.get("organ_presence"))
                summaries = facts.get("summaries", facts.get("summary_digests"))
            else:
                _fail("facts", "type")
        except (TypeError, ValueError, MixedJourneyCoverageError):
            return _blocked(key, turn, MixedJourneyCoverageReason.INVALID_FACTS)
    if organs is None and summaries is None:
        return _empty(key, turn, MixedJourneyCoverageReason.NO_FACTS)
    return _build(key, turn, organs, summaries)


def _build(key: str, turn: str, organs_raw: object, summaries_raw: object) -> MixedJourneyCoverageV1:
    try:
        organ_value = _organs(organs_raw, key=key, turn=turn)
        if organ_value.state is MixedJourneyOrgansState.BLOCKED:
            return _blocked(key, turn, MixedJourneyCoverageReason.ORGANS_BLOCKED)
        summaries = _parse_summaries(summaries_raw)
        present = set(organ_value.present_organs)
        # The parser already rejects names outside the closed organ set.  A
        # known-but-ABSENT organ may have a stale summary; it is ignored and
        # cannot make a journey complete or incomplete.
        missing = tuple(name for name in ORGAN_NAMES if name in present and name not in summaries)
        covered = tuple(name for name in ORGAN_NAMES if name in present and name in summaries)
        if not missing:
            return MixedJourneyCoverageV1(
                key,
                turn,
                MixedJourneyCoverageState.COMPLETE,
                covered,
                (),
                tuple((name, summaries[name]) for name in covered),
                MixedJourneyCoverageReason.COMPLETE,
            )
        return MixedJourneyCoverageV1(
            key,
            turn,
            MixedJourneyCoverageState.PARTIAL,
            covered,
            missing,
            tuple((name, summaries[name]) for name in covered),
            MixedJourneyCoverageReason.PARTIAL,
        )
    except MixedJourneyCoverageError as exc:
        reason = (
            MixedJourneyCoverageReason.UNKNOWN_ORGAN
            if "organ_unknown" in str(exc)
            else MixedJourneyCoverageReason.INVALID_FACTS
        )
        return _blocked(key, turn, reason)
    except (TypeError, ValueError):
        return _blocked(key, turn, MixedJourneyCoverageReason.INVALID_FACTS)


def validate_mixed_journey_coverage(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyCoverageV1)
            else build_mixed_journey_coverage(cast(Mapping[str, Any], value))
        )
        return (
            isinstance(result, MixedJourneyCoverageV1)
            and result.state is not MixedJourneyCoverageState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_journey_coverage = build_mixed_journey_coverage
validate_journey_coverage = validate_mixed_journey_coverage

__all__ = [
    "MIXED_JOURNEY_COVERAGE_SCHEMA",
    "CoverageReason",
    "CoverageState",
    "MixedJourneyCoverage",
    "MixedJourneyCoverageError",
    "MixedJourneyCoverageFacts",
    "MixedJourneyCoverageFactsV1",
    "MixedJourneyCoverageReason",
    "MixedJourneyCoverageState",
    "MixedJourneyCoverageV1",
    "build_journey_coverage",
    "build_mixed_journey_coverage",
    "validate_journey_coverage",
    "validate_mixed_journey_coverage",
]
