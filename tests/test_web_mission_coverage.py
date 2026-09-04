from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.web_mission_coverage import (
    WEB_MISSION_COVERAGE_SCHEMA,
    WebMissionCoverageReason,
    WebMissionCoverageState,
    WebMissionCoverageV1,
    build_web_mission_coverage,
    validate_web_mission_coverage,
)
from friday.orchestration.web_research_mission import WebResearchMissionV1, plan_web_research_mission


def mission() -> WebResearchMissionV1:
    return plan_web_research_mission(
        mission_id="mission:1",
        authenticated_turn_id="turn:1",
        public_topic="Python 3.14 public release",
        freshness_requirement="current",
    )


def test_all_planned_queries_are_complete_and_extra_queries_do_not_count() -> None:
    facts = mission()
    executed = (*facts.query_plan, "independent extra public query")
    result = build_web_mission_coverage("coverage:complete", facts, executed)

    assert isinstance(result, WebMissionCoverageV1)
    assert result.coverage is WebMissionCoverageState.COMPLETE
    assert result.reason is WebMissionCoverageReason.ALL_PLANNED_QUERIES_COVERED
    assert result.covered_query_count == result.planned_query_count == len(facts.query_plan)
    assert result.mission_id == "mission:1"


def test_extra_executed_queries_cannot_complete_an_uncovered_plan_entry() -> None:
    facts = mission()
    result = build_web_mission_coverage(
        "coverage:partial",
        facts,
        (*facts.query_plan[:2], "unplanned public query source"),
    )

    assert result.coverage is WebMissionCoverageState.PARTIAL
    assert result.covered_query_count == 2
    assert result.planned_query_count == len(facts.query_plan)


def test_empty_executed_set_is_empty_not_complete() -> None:
    result = build_web_mission_coverage("coverage:empty", mission(), ())

    assert result.coverage is WebMissionCoverageState.EMPTY
    assert result.covered_query_count == 0
    assert result.planned_query_count > 0
    assert result.reason is WebMissionCoverageReason.NO_EXECUTED_QUERIES


def test_sibling_contract_argument_order_with_authenticated_turn_is_supported() -> None:
    facts = mission()
    result = build_web_mission_coverage("coverage:ordered", "turn:1", facts, facts.query_plan)

    assert result.coverage is WebMissionCoverageState.COMPLETE


def test_matching_is_exact_and_duplicate_execution_does_not_overcount() -> None:
    facts = mission()
    altered = facts.query_plan[0].replace("official", "Official")
    result = build_web_mission_coverage(
        "coverage:exact",
        facts,
        (facts.query_plan[0], facts.query_plan[0], altered),
    )

    assert result.coverage is WebMissionCoverageState.PARTIAL
    assert result.covered_query_count == 1


def test_private_executed_query_fails_closed_as_blocked() -> None:
    result = build_web_mission_coverage(
        "coverage:private",
        mission(),
        ("private report.pdf",),
    )

    assert result.coverage is WebMissionCoverageState.BLOCKED
    assert result.reason is WebMissionCoverageReason.PRIVATE_EXECUTED_QUERY
    assert result.covered_query_count == 0
    assert result.planned_query_count == 0


def test_invalid_mission_facts_fail_closed_as_blocked() -> None:
    result = build_web_mission_coverage(
        "coverage:invalid",
        {"mission_id": "mission:bad", "authenticated_turn_id": "turn:1", "query_plan": []},
        (),
    )

    assert result.coverage is WebMissionCoverageState.BLOCKED
    assert result.reason is WebMissionCoverageReason.MISSION_INVALID
    assert result.authenticated_turn_id is None
    assert result.mission_id is None


def test_authenticated_turn_mismatch_exposes_no_partial_coverage() -> None:
    result = build_web_mission_coverage(
        "coverage:mismatch",
        mission(),
        mission().query_plan,
        authenticated_turn_id="turn:other",
    )

    assert result.coverage is WebMissionCoverageState.BLOCKED
    assert result.reason is WebMissionCoverageReason.IDENTITY_MISMATCH
    assert result.authenticated_turn_id is None
    assert result.mission_id is None
    assert result.covered_query_count == 0


def test_mapping_inputs_and_serialized_result_round_trip() -> None:
    facts = mission()
    result = build_web_mission_coverage(
        {
            "schema": WEB_MISSION_COVERAGE_SCHEMA,
            "coverage_id": "coverage:mapping",
            "mission": facts.to_mapping(),
            "executed_queries": list(facts.query_plan),
        }
    )

    assert result.coverage is WebMissionCoverageState.COMPLETE
    encoded = result.to_mapping()
    assert build_web_mission_coverage(encoded) == result
    assert validate_web_mission_coverage(encoded) is True


def test_coverage_result_is_frozen_and_validator_is_closed() -> None:
    result = build_web_mission_coverage("coverage:frozen", mission(), mission().query_plan)
    with pytest.raises(FrozenInstanceError):
        result.coverage = WebMissionCoverageState.EMPTY  # type: ignore[misc]

    malformed = result.to_mapping()
    malformed["reason"] = "invented"
    assert validate_web_mission_coverage(malformed) is False
