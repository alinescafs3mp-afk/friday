"""Pure planner for bounded, privacy-safe public-web research missions.

The planner accepts an already observed public topic and emits only bounded
query text.  It has no provider, network, file, persistence, or live-route
dependency.  Currentness facts are used only as a closed admission decision;
private carriers never become query material.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.web_currentness_policy import (
    CurrentnessPolicyError,
    WebCurrentnessDecision,
    classify_web_currentness,
    seal_public_query_intent,
)
from friday.orchestration.web_evidence_bundle import (
    MAX_QUERY_PLAN,
    MAX_RESEARCH_SOURCES,
    MIN_QUERY_PLAN,
)
from friday.web_research_contract import MAX_OUTBOUND_WEB_QUERY_CHARS

WEB_RESEARCH_MISSION_SCHEMA = "friday.web-research-mission.v1"
MAX_MISSION_ID_CHARS = 128
MAX_PUBLIC_TOPIC_CHARS = 1_000
MAX_FRESHNESS_REQUIREMENT_CHARS = 160
MAX_COVERAGE_NOTE_CHARS = 32

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PATH_RE = re.compile(r"(?:^|[\s(\[\"'=,:])(?:/(?!/)|~(?:[/\\])|[A-Za-z]:[/\\]|\\\\)")
_URL_RE = re.compile(r"(?i)(?<![\w:])(?:[a-z][a-z0-9+.-]*://|//)[^\s<>\"']+")

# The labels deliberately name distinct retrieval lanes.  Their order is
# stable so a mission is reproducible without a random or provider-dependent
# planner.
_QUERY_LANES = (
    "official primary source",
    "independent corroboration",
    "recent update",
    "counterevidence disagreement",
)
_NON_CURRENT_LANE = "background context"


class WebResearchMissionError(ValueError):
    """A value is outside the closed public-web mission contract."""


class WebResearchDiversityCoverageNote(StrEnum):
    """Closed planner declaration for the lanes a mission must cover."""

    BALANCED = "balanced"
    BROAD = "broad"
    FOCUSED = "focused"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebResearchMissionError(f"{field}_{detail}")


def _text(value: object, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _fail(field, "text")
    if not allow_empty and not value:
        _fail(field, "empty")
    if value != value.strip():
        _fail(field, "whitespace")
    if len(value) > maximum:
        _fail(field, "too_long")
    if _UNSAFE_CONTROL_RE.search(value) or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        _fail(field, "control")
    if _PATH_RE.search(value):
        _fail(field, "path")
    return cast(str, value)


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _sequence(value: object, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail(field, "sequence")
    return cast(Sequence[Any], value)


def _mapping_field(mapping: Mapping[str, Any], *names: str, default: object = None) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _normalize_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_public_topic(value: object) -> str:
    topic = _text(value, field="public_topic", maximum=MAX_PUBLIC_TOPIC_CHARS)
    if _URL_RE.search(topic):
        _fail("public_topic", "url")
    try:
        seal_public_query_intent((topic,))
    except CurrentnessPolicyError as exc:
        raise WebResearchMissionError("public_topic_private_or_invalid") from exc
    return topic


def _validate_query(value: object, *, field: str = "query") -> str:
    query = _text(value, field=field, maximum=MAX_OUTBOUND_WEB_QUERY_CHARS)
    if _URL_RE.search(query):
        _fail(field, "url")
    try:
        sealed = seal_public_query_intent((query,))
    except CurrentnessPolicyError as exc:
        raise WebResearchMissionError(f"{field}_private_or_invalid") from exc
    # The sealer is allowed to truncate caller-provided concepts for its own
    # API.  A mission must never silently change an explicitly supplied query.
    if sealed.query != query:
        _fail(field, "not_bounded_exactly")
    return query


def _validate_query_plan(value: object) -> tuple[str, ...]:
    queries = _sequence(value, field="query_plan")
    if not MIN_QUERY_PLAN <= len(queries) <= MAX_QUERY_PLAN:
        _fail("query_plan", "count")
    result = tuple(_validate_query(query, field="query") for query in queries)
    normalized = tuple(_normalize_query(query) for query in result)
    if any(not query for query in normalized):
        _fail("query_plan", "empty")
    if len(set(normalized)) != len(normalized):
        _fail("query_plan", "duplicate")
    return result


def _currentness_decision(value: object) -> WebCurrentnessDecision | None:
    if value is None:
        return None
    if isinstance(value, WebCurrentnessDecision):
        return value
    if isinstance(value, Mapping) and "decision" in value:
        decision = value.get("decision")
        try:
            return WebCurrentnessDecision(str(decision))
        except ValueError as exc:
            raise WebResearchMissionError("currentness_decision_invalid") from exc
    try:
        return classify_web_currentness(value)  # type: ignore[arg-type]
    except (CurrentnessPolicyError, TypeError, ValueError) as exc:
        raise WebResearchMissionError("currentness_facts_invalid") from exc


def _reject_blocked_currentness(value: object) -> WebCurrentnessDecision | None:
    decision = _currentness_decision(value)
    if decision is WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE:
        # There is intentionally no fallback query here.  A blocked decision
        # must be observable by the caller and can never yield a public plan.
        raise WebResearchMissionError("search_blocked_private")
    return decision


def _coerce_note(value: object) -> WebResearchDiversityCoverageNote:
    if value is None:
        return WebResearchDiversityCoverageNote.BALANCED
    if isinstance(value, WebResearchDiversityCoverageNote):
        return value
    if isinstance(value, Mapping):
        keys = set(value)
        if keys - {"note", "value", "state"}:
            _fail("diversity_coverage_note", "closed")
        value = _mapping_field(value, "note", "value", "state")
    if type(value) is not str or len(value) > MAX_COVERAGE_NOTE_CHARS:
        _fail("diversity_coverage_note", "closed")
    try:
        return WebResearchDiversityCoverageNote(value)
    except ValueError as exc:
        raise WebResearchMissionError("diversity_coverage_note_closed") from exc


def _planned_queries(
    topic: str, *, freshness_requirement: str, decision: WebCurrentnessDecision | None
) -> tuple[str, ...]:
    current = bool(decision is WebCurrentnessDecision.SEARCH_REQUIRED) or _normalize_query(
        freshness_requirement
    ) not in {"", "none", "not required", "not_required", "timeless"}
    lanes = _QUERY_LANES if current else (_QUERY_LANES[0], _QUERY_LANES[1], _NON_CURRENT_LANE)
    queries: list[str] = []
    for lane in lanes:
        try:
            # Put the lane first so even an unusually long public topic cannot
            # truncate away every differentiating term at the 200-char bound.
            query = seal_public_query_intent((lane, topic)).query
        except CurrentnessPolicyError as exc:
            raise WebResearchMissionError("query_plan_private_or_invalid") from exc
        if len(query) > MAX_OUTBOUND_WEB_QUERY_CHARS:
            _fail("query", "too_long")
        if _normalize_query(query) not in {_normalize_query(item) for item in queries}:
            queries.append(query)
    if len(queries) < MIN_QUERY_PLAN:
        _fail("query_plan", "not_complementary")
    return tuple(queries)


@dataclass(frozen=True, slots=True)
class WebResearchMissionV1:
    """Immutable bounded mission metadata; query bodies are public concepts only."""

    mission_id: str
    authenticated_turn_id: str
    public_topic: str
    freshness_requirement: str
    query_plan: tuple[str, ...]
    diversity_coverage_note: WebResearchDiversityCoverageNote

    def __post_init__(self) -> None:
        _identifier(self.mission_id, field="mission_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        _validate_public_topic(self.public_topic)
        _text(
            self.freshness_requirement,
            field="freshness_requirement",
            maximum=MAX_FRESHNESS_REQUIREMENT_CHARS,
        )
        if type(self.query_plan) is not tuple:
            _fail("query_plan", "immutable")
        _validate_query_plan(self.query_plan)
        note = _coerce_note(self.diversity_coverage_note)
        if note is not self.diversity_coverage_note:
            object.__setattr__(self, "diversity_coverage_note", note)

    @property
    def coverage_note(self) -> WebResearchDiversityCoverageNote:
        return self.diversity_coverage_note

    @property
    def diversity_note(self) -> WebResearchDiversityCoverageNote:
        return self.diversity_coverage_note

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_RESEARCH_MISSION_SCHEMA,
            "mission_id": self.mission_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "public_topic": self.public_topic,
            "freshness_requirement": self.freshness_requirement,
            "query_plan": list(self.query_plan),
            "diversity_coverage_note": self.diversity_coverage_note.value,
        }


WebResearchMission = WebResearchMissionV1
WebResearchCoverageNote = WebResearchDiversityCoverageNote


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "mission_id",
        "authenticated_turn_id",
        "public_topic",
        "topic",
        "task_topic",
        "freshness_requirement",
        "query_plan",
        "diversity_coverage_note",
        "coverage_note",
        "diversity_note",
        "currentness",
        "currentness_facts",
        "freshness_facts",
        "currentness_decision",
    }
    unknown = sorted(key for key in raw if not isinstance(key, str) or key not in known)
    if unknown:
        _fail("mission", "unknown_fields")


def _raw_currentness(raw: Mapping[str, Any], explicit: object) -> object:
    if explicit is not None:
        return explicit
    for key in ("currentness_facts", "currentness", "freshness_facts", "currentness_decision"):
        if key in raw:
            return raw[key]
    return None


def build_web_research_mission(
    raw: Mapping[str, Any] | WebResearchMissionV1,
    *,
    currentness_facts: object = None,
) -> WebResearchMissionV1:
    """Build an immutable mission, planning queries when they are omitted."""

    if isinstance(raw, WebResearchMissionV1):
        _reject_blocked_currentness(currentness_facts)
        return raw
    if not isinstance(raw, Mapping):
        _fail("mission")
    _known_mapping_keys(raw)
    schema = raw.get("schema", WEB_RESEARCH_MISSION_SCHEMA)
    if schema != WEB_RESEARCH_MISSION_SCHEMA:
        _fail("schema")

    decision = _reject_blocked_currentness(_raw_currentness(raw, currentness_facts))
    topic = _validate_public_topic(_mapping_field(raw, "public_topic", "topic", "task_topic"))
    if decision is None:
        decision = _currentness_decision(topic)
    freshness = _text(
        _mapping_field(raw, "freshness_requirement", default="unspecified"),
        field="freshness_requirement",
        maximum=MAX_FRESHNESS_REQUIREMENT_CHARS,
    )
    if "query_plan" in raw:
        query_plan = _validate_query_plan(raw["query_plan"])
    else:
        query_plan = _planned_queries(topic, freshness_requirement=freshness, decision=decision)
    return WebResearchMissionV1(
        mission_id=_identifier(raw.get("mission_id"), field="mission_id"),
        authenticated_turn_id=_identifier(raw.get("authenticated_turn_id"), field="authenticated_turn_id"),
        public_topic=topic,
        freshness_requirement=freshness,
        query_plan=query_plan,
        diversity_coverage_note=_coerce_note(
            _mapping_field(raw, "diversity_coverage_note", "coverage_note", "diversity_note")
        ),
    )


def plan_web_research_mission(
    mission_id: str | Mapping[str, Any] | None = None,
    authenticated_turn_id: str | None = None,
    public_topic: str | None = None,
    freshness_requirement: str = "unspecified",
    currentness_facts: object = None,
    *,
    currentness: object = None,
    diversity_coverage_note: object = None,
) -> WebResearchMissionV1:
    """Plan 2-8 complementary public queries from one observed public topic."""

    if isinstance(mission_id, Mapping):
        if any(value is not None for value in (authenticated_turn_id, public_topic)):
            _fail("mission", "duplicate_arguments")
        mapping_raw = dict(mission_id)
        if "freshness_requirement" not in mapping_raw and freshness_requirement != "unspecified":
            mapping_raw["freshness_requirement"] = freshness_requirement
        if currentness is not None:
            if currentness_facts is not None:
                _fail("currentness", "duplicate_arguments")
            currentness_facts = currentness
        if diversity_coverage_note is not None:
            mapping_raw.setdefault("diversity_coverage_note", diversity_coverage_note)
        return build_web_research_mission(mapping_raw, currentness_facts=currentness_facts)

    if currentness is not None:
        if currentness_facts is not None:
            _fail("currentness", "duplicate_arguments")
        currentness_facts = currentness
    raw: dict[str, Any] = {
        "mission_id": mission_id,
        "authenticated_turn_id": authenticated_turn_id,
        "public_topic": public_topic,
        "freshness_requirement": freshness_requirement,
    }
    if diversity_coverage_note is not None:
        raw["diversity_coverage_note"] = diversity_coverage_note
    topic = _validate_public_topic(public_topic)
    decision = _reject_blocked_currentness(currentness_facts)
    if decision is None:
        decision = _currentness_decision(topic)
    freshness = _text(
        freshness_requirement,
        field="freshness_requirement",
        maximum=MAX_FRESHNESS_REQUIREMENT_CHARS,
    )
    raw["query_plan"] = _planned_queries(topic, freshness_requirement=freshness, decision=decision)
    return build_web_research_mission(raw)


def validate_web_research_mission(value: object) -> bool:
    """Return whether a mission or mapping satisfies the complete contract."""

    try:
        build_web_research_mission(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


build_research_mission = build_web_research_mission
plan_research_mission = plan_web_research_mission
validate_research_mission = validate_web_research_mission


__all__ = [
    "MAX_QUERY_PLAN",
    "MAX_RESEARCH_SOURCES",
    "MIN_QUERY_PLAN",
    "MAX_OUTBOUND_WEB_QUERY_CHARS",
    "WEB_RESEARCH_MISSION_SCHEMA",
    "WebResearchCoverageNote",
    "WebResearchDiversityCoverageNote",
    "WebResearchMission",
    "WebResearchMissionError",
    "WebResearchMissionV1",
    "build_research_mission",
    "build_web_research_mission",
    "plan_research_mission",
    "plan_web_research_mission",
    "validate_research_mission",
    "validate_web_research_mission",
]
