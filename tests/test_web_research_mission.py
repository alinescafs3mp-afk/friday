from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_research_mission import (
    MAX_OUTBOUND_WEB_QUERY_CHARS,
    MAX_QUERY_PLAN,
    MAX_RESEARCH_SOURCES,
    MIN_QUERY_PLAN,
    WEB_RESEARCH_MISSION_SCHEMA,
    WebResearchDiversityCoverageNote,
    WebResearchMissionError,
    WebResearchMissionV1,
    build_web_research_mission,
    plan_web_research_mission,
    validate_web_research_mission,
)


def _raw() -> dict[str, object]:
    return {
        "schema": WEB_RESEARCH_MISSION_SCHEMA,
        "mission_id": "mission:2026-09-04:1",
        "authenticated_turn_id": "turn:2026-09-04:1",
        "public_topic": "Python 3.14 official release behavior",
        "freshness_requirement": "current",
        "diversity_coverage_note": "balanced",
    }


def test_planner_emits_frozen_bounded_complementary_public_queries() -> None:
    mission = plan_web_research_mission(_raw())

    assert isinstance(mission, WebResearchMissionV1)
    assert mission.diversity_coverage_note is WebResearchDiversityCoverageNote.BALANCED
    assert MIN_QUERY_PLAN <= len(mission.query_plan) <= MAX_QUERY_PLAN
    assert all(len(query) <= MAX_OUTBOUND_WEB_QUERY_CHARS for query in mission.query_plan)
    assert len({" ".join(query.casefold().split()) for query in mission.query_plan}) == len(
        mission.query_plan
    )
    assert all("Python 3.14" in query for query in mission.query_plan)

    with pytest.raises(FrozenInstanceError):
        mission.public_topic = "another topic"  # type: ignore[misc]


def test_mapping_round_trip_is_stable_and_nested_plan_is_immutable() -> None:
    mission = plan_web_research_mission(
        mission_id="mission:1",
        authenticated_turn_id="turn:1",
        public_topic="Rust 2024 language changes",
        freshness_requirement="current",
    )

    encoded = mission.to_mapping()
    assert encoded["schema"] == WEB_RESEARCH_MISSION_SCHEMA
    assert isinstance(encoded["query_plan"], list)
    assert build_web_research_mission(encoded) == mission
    assert isinstance(mission.query_plan, tuple)


def test_currentness_facts_change_lane_without_leaking_private_question() -> None:
    mission = plan_web_research_mission(
        mission_id="mission:2",
        authenticated_turn_id="turn:2",
        public_topic="Python 3.14 release",
        freshness_requirement="unspecified",
        currentness_facts={
            "question": "Check current documentation in this attached file",
            "public_concepts": ("Python 3.14 release",),
        },
    )

    assert "recent update" in mission.query_plan[2]
    assert all("attached" not in query.casefold() for query in mission.query_plan)


def test_blocked_private_currentness_never_emits_a_public_query_plan() -> None:
    with pytest.raises(WebResearchMissionError, match="search_blocked_private"):
        plan_web_research_mission(
            mission_id="mission:3",
            authenticated_turn_id="turn:3",
            public_topic="public release status",
            freshness_requirement="current",
            currentness_facts=WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE,
        )


@pytest.mark.parametrize(
    "topic",
    (
        "summarize /home/owner/report.pdf",
        "compare private report.pdf with release notes",
        "latest result for job_abc123",
        "what does this attached document say",
        "inspect https://127.0.0.1/admin",
        "inspect https://research.example.test/notes",
    ),
)
def test_private_topic_material_and_non_public_urls_are_rejected(topic: str) -> None:
    with pytest.raises(WebResearchMissionError, match="public_topic"):
        plan_web_research_mission(
            mission_id="mission:private",
            authenticated_turn_id="turn:private",
            public_topic=topic,
        )


@pytest.mark.parametrize(
    "query",
    (
        "summarize /home/owner/report.pdf",
        "compare private report.pdf with release notes",
        "inspect https://127.0.0.1/admin",
        "inspect https://service.internal/report",
    ),
)
def test_supplied_query_plan_is_revalidated_at_the_public_boundary(query: str) -> None:
    raw = _raw()
    raw["query_plan"] = [query, "independent public release analysis"]

    with pytest.raises(WebResearchMissionError, match="query"):
        build_web_research_mission(raw)


@pytest.mark.parametrize(
    "query_plan",
    (
        [],
        ["one query"],
        ["same query", "same   query"],
        [f"public query {index}" for index in range(MAX_QUERY_PLAN + 1)],
    ),
)
def test_supplied_query_plan_is_closed_and_complementary(query_plan: list[str]) -> None:
    raw = _raw()
    raw["query_plan"] = query_plan

    with pytest.raises(WebResearchMissionError, match="query_plan"):
        build_web_research_mission(raw)


def test_coverage_note_is_closed() -> None:
    raw = _raw()
    raw["diversity_coverage_note"] = "made up"

    with pytest.raises(WebResearchMissionError, match="diversity_coverage_note"):
        build_web_research_mission(raw)


def test_builder_can_use_an_explicit_plan_and_rejects_unknown_metadata() -> None:
    raw = _raw()
    raw["query_plan"] = [
        "official primary source for Python 3.14",
        "independent release analysis for Python 3.14",
    ]
    mission = build_web_research_mission(raw)
    assert mission.query_plan == tuple(cast(list[str], raw["query_plan"]))
    assert MAX_RESEARCH_SOURCES == 8

    raw["provider"] = "not part of the mission planner"
    assert validate_web_research_mission(raw) is False


def test_validator_is_fail_closed_without_network_or_file_access() -> None:
    valid = _raw()
    assert validate_web_research_mission(valid) is True
    valid["query_plan"] = ["public query", "public query"]
    assert validate_web_research_mission(valid) is False
