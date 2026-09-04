"""Pure exact-coverage contract for bounded public-web missions.

The contract compares already-observed executed query strings with a frozen
mission plan.  It performs no retrieval, file I/O, persistence, or live
wiring.  Matching is deliberately exact: normalization, fuzzy matching, and
extra executed queries cannot manufacture planned-query coverage.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.web_currentness_policy import CurrentnessPolicyError, seal_public_query_intent
from friday.orchestration.web_research_mission import (
    WebResearchMissionV1,
    build_web_research_mission,
)

WEB_MISSION_COVERAGE_SCHEMA = "friday.web-mission-coverage.v1"
MAX_COVERAGE_ID_CHARS = 128
MAX_EXECUTED_QUERIES = 64
MAX_QUERY_CHARS = 200

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class WebMissionCoverageError(ValueError):
    """An exact mission-coverage value is outside its closed contract."""


class WebMissionCoverageState(StrEnum):
    """Closed coverage outcomes for one observed mission execution."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    BLOCKED = "blocked"


class WebMissionCoverageReason(StrEnum):
    """Closed short reason for one mission-coverage outcome."""

    ALL_PLANNED_QUERIES_COVERED = "all_planned_queries_covered"
    SOME_PLANNED_QUERIES_COVERED = "some_planned_queries_covered"
    NO_EXECUTED_QUERIES = "no_executed_queries"
    MISSION_INVALID = "mission_invalid"
    PRIVATE_EXECUTED_QUERY = "private_executed_query"
    IDENTITY_MISMATCH = "identity_mismatch"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebMissionCoverageError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> WebMissionCoverageState:
    try:
        return WebMissionCoverageState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebMissionCoverageError("coverage_closed") from exc


def _reason(value: object) -> WebMissionCoverageReason:
    try:
        return WebMissionCoverageReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebMissionCoverageError("reason_closed") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_QUERY_CHARS:
        _fail(field, "range")
    return cast(int, value)


def _executed_queries(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("executed_queries", "sequence")
    if len(value) > MAX_EXECUTED_QUERIES:
        _fail("executed_queries", "bound")
    queries: list[str] = []
    for index, item in enumerate(value):
        query = item
        if isinstance(item, Mapping):
            keys = set(item)
            if keys - {"query", "executed_query"}:
                _fail("executed_queries_item", "closed")
            query = item.get("query", item.get("executed_query"))
        if type(query) is not str or not query or query != query.strip() or len(query) > MAX_QUERY_CHARS:
            _fail(f"executed_queries[{index}]", "text")
        try:
            sealed = seal_public_query_intent((query,))
        except CurrentnessPolicyError as exc:
            raise WebMissionCoverageError("private_executed_query") from exc
        if sealed.query != query:
            _fail(f"executed_queries[{index}]", "not_bounded_exactly")
        queries.append(query)
    return tuple(queries)


@dataclass(frozen=True, slots=True)
class WebMissionCoverageV1:
    """Immutable exact coverage for a mission's planned query entries."""

    coverage_id: str
    authenticated_turn_id: str | None
    coverage: WebMissionCoverageState
    covered_query_count: int
    planned_query_count: int
    reason: WebMissionCoverageReason
    mission_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.coverage_id, field="coverage_id")
        if self.authenticated_turn_id is not None:
            _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        if self.mission_id is not None:
            _identifier(self.mission_id, field="mission_id")
            if self.authenticated_turn_id is None:
                _fail("input_identities", "all_or_none")
        coverage = _state(self.coverage)
        reason = _reason(self.reason)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "reason", reason)
        covered = _count(self.covered_query_count, field="covered_query_count")
        planned = _count(self.planned_query_count, field="planned_query_count")
        if covered > planned:
            _fail("query_counts", "inconsistent")
        if coverage is WebMissionCoverageState.BLOCKED:
            if covered or planned:
                _fail("blocked_counts", "nonzero")
        elif coverage is WebMissionCoverageState.EMPTY:
            if covered:
                _fail("empty_coverage", "covered")
        elif coverage is WebMissionCoverageState.COMPLETE:
            if planned < 1 or covered != planned:
                _fail("complete_coverage", "inconsistent")
        elif covered < 1 or covered >= planned:
            _fail("partial_coverage", "inconsistent")

    @property
    def state(self) -> WebMissionCoverageState:
        return self.coverage

    @property
    def closed_coverage(self) -> WebMissionCoverageState:
        return self.coverage

    @property
    def decision(self) -> WebMissionCoverageState:
        return self.coverage

    @property
    def closed_reason(self) -> WebMissionCoverageReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_MISSION_COVERAGE_SCHEMA,
            "coverage_id": self.coverage_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "mission_id": self.mission_id,
            "coverage": self.coverage.value,
            "covered_query_count": self.covered_query_count,
            "planned_query_count": self.planned_query_count,
            "reason": self.reason.value,
        }


MissionCoverageState = WebMissionCoverageState
MissionCoverageReason = WebMissionCoverageReason
WebMissionCoverage = WebMissionCoverageV1


def _coerce_mission(value: object) -> WebResearchMissionV1 | None:
    try:
        result = value if isinstance(value, WebResearchMissionV1) else build_web_research_mission(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def _not_ready(
    coverage_id: str,
    reason: WebMissionCoverageReason,
    *,
    mission: WebResearchMissionV1 | None = None,
) -> WebMissionCoverageV1:
    return WebMissionCoverageV1(
        coverage_id=coverage_id,
        authenticated_turn_id=None if mission is None else mission.authenticated_turn_id,
        coverage=WebMissionCoverageState.BLOCKED,
        covered_query_count=0,
        planned_query_count=0,
        reason=reason,
        mission_id=None if mission is None else mission.mission_id,
    )


def _known_input_keys(raw: Mapping[str, Any]) -> bool:
    return not (
        set(raw)
        - {
            "schema",
            "coverage_id",
            "mission",
            "research_mission",
            "mission_facts",
            "executed_queries",
            "observed_queries",
            "queries",
            "authenticated_turn_id",
            "coverage",
            "state",
            "mission_id",
            "covered_query_count",
            "planned_query_count",
            "reason",
        }
    )


def build_web_mission_coverage(
    coverage_id: str | Mapping[str, Any],
    mission: object = None,
    executed_queries: object = (),
    *positional: object,
    authenticated_turn_id: object = None,
) -> WebMissionCoverageV1:
    """Build exact mission coverage without inventing or normalizing queries."""

    if positional:
        # Also accept the conventional (coverage_id, authenticated_turn_id,
        # mission, executed_queries) order used by sibling contracts.
        if len(positional) != 1 or authenticated_turn_id is not None:
            _fail("coverage", "duplicate_arguments")
        authenticated_turn_id = mission
        mission, executed_queries = executed_queries, positional[0]
    if isinstance(coverage_id, Mapping):
        raw = coverage_id
        if not _known_input_keys(raw):
            _fail("coverage", "unknown_fields")
        if raw.get("schema", WEB_MISSION_COVERAGE_SCHEMA) != WEB_MISSION_COVERAGE_SCHEMA:
            _fail("schema")
        output_keys = {
            "coverage",
            "state",
            "mission_id",
            "covered_query_count",
            "planned_query_count",
            "reason",
        }
        fact_keys = {
            "mission",
            "research_mission",
            "mission_facts",
            "executed_queries",
            "observed_queries",
            "queries",
        }
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("coverage", "duplicate_representations")
        if output_keys.intersection(raw):
            return WebMissionCoverageV1(
                coverage_id=cast(str, raw.get("coverage_id")),
                authenticated_turn_id=cast(str | None, raw.get("authenticated_turn_id")),
                coverage=cast(WebMissionCoverageState, raw.get("coverage", raw.get("state"))),
                covered_query_count=cast(int, raw.get("covered_query_count")),
                planned_query_count=cast(int, raw.get("planned_query_count")),
                reason=cast(WebMissionCoverageReason, raw.get("reason")),
                mission_id=cast(str | None, raw.get("mission_id")),
            )
        coverage_id = cast(str, raw.get("coverage_id"))
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        mission = raw.get("mission", raw.get("research_mission", raw.get("mission_facts")))
        executed_queries = raw.get("executed_queries", raw.get("observed_queries", raw.get("queries", ())))

    coverage_key = _identifier(coverage_id, field="coverage_id")
    mission_value = _coerce_mission(mission)
    if mission_value is None:
        return _not_ready(coverage_key, WebMissionCoverageReason.MISSION_INVALID)
    if authenticated_turn_id is not None:
        try:
            explicit_turn = _identifier(authenticated_turn_id, field="authenticated_turn_id")
        except WebMissionCoverageError:
            return _not_ready(coverage_key, WebMissionCoverageReason.IDENTITY_MISMATCH)
        if explicit_turn != mission_value.authenticated_turn_id:
            return _not_ready(coverage_key, WebMissionCoverageReason.IDENTITY_MISMATCH)
    try:
        observed = _executed_queries(executed_queries)
    except WebMissionCoverageError as exc:
        reason = (
            WebMissionCoverageReason.PRIVATE_EXECUTED_QUERY
            if "private_executed_query" in str(exc)
            else WebMissionCoverageReason.MISSION_INVALID
        )
        return _not_ready(coverage_key, reason, mission=mission_value)
    planned = mission_value.query_plan
    planned_count = len(planned)
    if not observed:
        return WebMissionCoverageV1(
            coverage_id=coverage_key,
            authenticated_turn_id=mission_value.authenticated_turn_id,
            coverage=WebMissionCoverageState.EMPTY,
            covered_query_count=0,
            planned_query_count=planned_count,
            reason=WebMissionCoverageReason.NO_EXECUTED_QUERIES,
            mission_id=mission_value.mission_id,
        )
    covered_count = sum(query in observed for query in planned)
    if covered_count == planned_count:
        state = WebMissionCoverageState.COMPLETE
        reason = WebMissionCoverageReason.ALL_PLANNED_QUERIES_COVERED
    else:
        state = WebMissionCoverageState.PARTIAL
        reason = WebMissionCoverageReason.SOME_PLANNED_QUERIES_COVERED
    return WebMissionCoverageV1(
        coverage_id=coverage_key,
        authenticated_turn_id=mission_value.authenticated_turn_id,
        coverage=state,
        covered_query_count=covered_count,
        planned_query_count=planned_count,
        reason=reason,
        mission_id=mission_value.mission_id,
    )


def validate_web_mission_coverage(value: object) -> bool:
    """Return whether a coverage object or serialized result is valid."""

    try:
        if isinstance(value, WebMissionCoverageV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or not _known_input_keys(value):
            return False
        if value.get("schema", WEB_MISSION_COVERAGE_SCHEMA) != WEB_MISSION_COVERAGE_SCHEMA:
            return False
        return (
            WebMissionCoverageV1(
                coverage_id=cast(str, value.get("coverage_id")),
                authenticated_turn_id=cast(str | None, value.get("authenticated_turn_id")),
                coverage=cast(WebMissionCoverageState, value.get("coverage", value.get("state"))),
                covered_query_count=cast(int, value.get("covered_query_count")),
                planned_query_count=cast(int, value.get("planned_query_count")),
                reason=cast(WebMissionCoverageReason, value.get("reason")),
                mission_id=cast(str | None, value.get("mission_id")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


calculate_web_mission_coverage = build_web_mission_coverage
decide_web_mission_coverage = build_web_mission_coverage
validate_mission_coverage = validate_web_mission_coverage


__all__ = [
    "MAX_EXECUTED_QUERIES",
    "WEB_MISSION_COVERAGE_SCHEMA",
    "MissionCoverageReason",
    "MissionCoverageState",
    "WebMissionCoverage",
    "WebMissionCoverageError",
    "WebMissionCoverageReason",
    "WebMissionCoverageState",
    "WebMissionCoverageV1",
    "build_web_mission_coverage",
    "calculate_web_mission_coverage",
    "decide_web_mission_coverage",
    "validate_mission_coverage",
    "validate_web_mission_coverage",
]
