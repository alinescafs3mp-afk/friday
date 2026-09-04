from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
)
from friday.orchestration.web_research_mission import plan_web_research_mission
from friday.orchestration.web_research_readiness import (
    WEB_RESEARCH_READINESS_SCHEMA,
    WebResearchReadinessReason,
    WebResearchReadinessState,
    WebResearchReadinessV1,
    build_web_research_readiness,
    validate_web_research_readiness,
)
from friday.orchestration.web_source_diversity import build_web_source_diversity


def mission() -> object:
    return plan_web_research_mission(
        mission_id="mission:1",
        authenticated_turn_id="turn:1",
        public_topic="Python 3.14 public release",
        freshness_requirement="current",
    )


def diversity(note: str) -> object:
    urls = {
        "empty": [],
        "single_host": ["https://one.example.com/one"],
        "concentrated": [
            "https://one.example.com/one",
            "https://one.example.com/two",
            "https://one.example.com/three",
            "https://two.example.com/one",
        ],
        "diverse": ["https://one.example.com/one", "https://two.example.com/two"],
    }[note]
    return build_web_source_diversity(
        {
            "diversity_id": f"diversity:{note}",
            "authenticated_turn_id": "turn:1",
            "source_urls": urls,
        }
    )


def consumption(state: WebResearchConsumptionState) -> WebResearchConsumptionV1:
    if state is WebResearchConsumptionState.CONSUMABLE:
        reason = WebResearchConsumptionReason.PRIMARY_SOURCES
        provider = "yandex"
        count = 2
    elif state is WebResearchConsumptionState.CONSUMABLE_DEGRADED:
        reason = WebResearchConsumptionReason.PARTIAL_SOURCES
        provider = "yandex"
        count = 2
    elif state is WebResearchConsumptionState.BLOCKED_PRIVATE:
        reason = WebResearchConsumptionReason.CURRENTNESS_PRIVATE
        provider = None
        count = 0
    else:
        reason = WebResearchConsumptionReason.PROVIDER_UNAVAILABLE
        provider = None
        count = 0
    return WebResearchConsumptionV1(
        consumption_id=f"consumption:{state.value}",
        authenticated_turn_id="turn:1",
        usability=state,
        selected_provider_id=provider,
        admitted_source_count=count,
        reason=reason,
    )


def test_consumable_diverse_inputs_are_ready_with_all_identities() -> None:
    result = build_web_research_readiness(
        "readiness:1", mission(), diversity("diverse"), consumption(WebResearchConsumptionState.CONSUMABLE)
    )

    assert isinstance(result, WebResearchReadinessV1)
    assert result.readiness is WebResearchReadinessState.READY
    assert result.reason is WebResearchReadinessReason.READY_DIVERSE
    assert result.authenticated_turn_id == "turn:1"
    assert result.mission_id == "mission:1"
    assert result.diversity_id == "diversity:diverse"
    assert result.consumption_id == "consumption:consumable"


@pytest.mark.parametrize("note", ("single_host", "concentrated"))
def test_consumable_non_diverse_inputs_are_ready_degraded_at_most(note: str) -> None:
    result = build_web_research_readiness(
        "readiness:degraded",
        mission(),
        diversity(note),
        consumption(WebResearchConsumptionState.CONSUMABLE),
    )

    assert result.readiness is WebResearchReadinessState.READY_DEGRADED
    assert result.reason is WebResearchReadinessReason.READY_DEGRADED_DIVERSITY


def test_empty_diversity_cannot_be_ready() -> None:
    result = build_web_research_readiness(
        "readiness:empty",
        mission(),
        diversity("empty"),
        consumption(WebResearchConsumptionState.CONSUMABLE),
    )

    assert result.readiness is WebResearchReadinessState.NOT_READY
    assert result.reason is WebResearchReadinessReason.DIVERSITY_EMPTY


def test_degraded_consumption_is_ready_degraded_only_with_diverse_sources() -> None:
    result = build_web_research_readiness(
        "readiness:consumption-degraded",
        mission(),
        diversity("diverse"),
        consumption(WebResearchConsumptionState.CONSUMABLE_DEGRADED),
    )

    assert result.readiness is WebResearchReadinessState.READY_DEGRADED
    assert result.reason is WebResearchReadinessReason.READY_DEGRADED_CONSUMPTION

    single_host = build_web_research_readiness(
        "readiness:single-degraded",
        mission(),
        diversity("single_host"),
        consumption(WebResearchConsumptionState.CONSUMABLE_DEGRADED),
    )
    assert single_host.readiness is WebResearchReadinessState.NOT_READY
    assert single_host.reason is WebResearchReadinessReason.DIVERSITY_INSUFFICIENT


@pytest.mark.parametrize(
    "state",
    (WebResearchConsumptionState.BLOCKED_PRIVATE, WebResearchConsumptionState.UNAVAILABLE),
)
def test_blocked_or_unavailable_consumption_cannot_be_ready(state: WebResearchConsumptionState) -> None:
    result = build_web_research_readiness(
        "readiness:blocked", mission(), diversity("diverse"), consumption(state)
    )

    assert result.readiness is WebResearchReadinessState.NOT_READY
    assert result.reason in {
        WebResearchReadinessReason.CONSUMPTION_BLOCKED_PRIVATE,
        WebResearchReadinessReason.CONSUMPTION_UNAVAILABLE,
    }


def test_mismatched_authenticated_turn_is_not_ready_and_exposes_no_partial_identity_set() -> None:
    mismatched = build_web_source_diversity(
        {
            "diversity_id": "diversity:other-turn",
            "authenticated_turn_id": "turn:2",
            "source_urls": ["https://one.example.com/source", "https://two.example.com/source"],
        }
    )
    result = build_web_research_readiness(
        "readiness:mismatch",
        mission(),
        mismatched,
        consumption(WebResearchConsumptionState.CONSUMABLE),
    )

    assert result.readiness is WebResearchReadinessState.NOT_READY
    assert result.reason is WebResearchReadinessReason.IDENTITY_MISMATCH
    assert result.authenticated_turn_id is None
    assert result.mission_id is None
    assert result.diversity_id is None
    assert result.consumption_id is None


def test_explicit_authenticated_turn_must_match_all_facts() -> None:
    result = build_web_research_readiness(
        "readiness:explicit-mismatch",
        mission(),
        diversity("diverse"),
        consumption(WebResearchConsumptionState.CONSUMABLE),
        authenticated_turn_id="turn:other",
    )
    assert result.readiness is WebResearchReadinessState.NOT_READY
    assert result.reason is WebResearchReadinessReason.IDENTITY_MISMATCH


def test_missing_or_invalid_facts_fail_closed_to_not_ready() -> None:
    missing = build_web_research_readiness("readiness:missing", mission(), diversity("diverse"), None)
    assert missing.readiness is WebResearchReadinessState.NOT_READY
    assert missing.reason is WebResearchReadinessReason.INPUTS_INVALID

    invalid = build_web_research_readiness(
        "readiness:invalid",
        {"mission_id": "/private/mission"},
        diversity("diverse"),
        consumption(WebResearchConsumptionState.CONSUMABLE),
    )
    assert invalid.readiness is WebResearchReadinessState.NOT_READY
    assert invalid.reason is WebResearchReadinessReason.INPUTS_INVALID


def test_mapping_inputs_and_readiness_round_trip_are_supported() -> None:
    ready = build_web_research_readiness(
        {
            "schema": WEB_RESEARCH_READINESS_SCHEMA,
            "readiness_id": "readiness:mapping",
            "mission": mission(),
            "diversity": diversity("diverse"),
            "consumption": consumption(WebResearchConsumptionState.CONSUMABLE),
        }
    )
    assert ready.readiness is WebResearchReadinessState.READY
    encoded = ready.to_mapping()
    assert encoded["schema"] == WEB_RESEARCH_READINESS_SCHEMA
    assert build_web_research_readiness(encoded) == ready
    assert validate_web_research_readiness(encoded) is True


def test_result_is_frozen_and_closed() -> None:
    result = build_web_research_readiness(
        "readiness:frozen",
        mission(),
        diversity("diverse"),
        consumption(WebResearchConsumptionState.CONSUMABLE),
    )

    with pytest.raises(FrozenInstanceError):
        result.readiness = WebResearchReadinessState.NOT_READY  # type: ignore[misc]
    malformed = result.to_mapping()
    malformed["reason"] = "invented"
    assert validate_web_research_readiness(malformed) is False
    malformed = result.to_mapping()
    malformed["unknown"] = "not admitted"
    assert validate_web_research_readiness(malformed) is False


def test_currentness_decision_is_not_recomputed_or_allowed_to_override_consumption() -> None:
    # The readiness gate consumes an already-built result.  A blocked decision
    # belongs to the consumption input, not to an invented second web lookup.
    blocked = consumption(WebResearchConsumptionState.BLOCKED_PRIVATE)
    result = build_web_research_readiness("readiness:currentness", mission(), diversity("diverse"), blocked)
    assert result.readiness is WebResearchReadinessState.NOT_READY
    assert WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE.value == "search_blocked_private"
