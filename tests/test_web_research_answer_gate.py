from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.web_citation_coverage import build_web_citation_coverage
from friday.orchestration.web_research_answer_gate import (
    WEB_RESEARCH_ANSWER_GATE_SCHEMA,
    WebResearchAnswerAdmission,
    WebResearchAnswerGateReason,
    WebResearchAnswerGateV1,
    build_web_research_answer_gate,
    validate_web_research_answer_gate,
)
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
)
from friday.orchestration.web_research_mission import plan_web_research_mission
from friday.orchestration.web_research_readiness import (
    WebResearchReadinessState,
    WebResearchReadinessV1,
    build_web_research_readiness,
)
from friday.orchestration.web_source_diversity import build_web_source_diversity


def mission() -> object:
    return plan_web_research_mission(
        mission_id="mission:1",
        authenticated_turn_id="turn:1",
        public_topic="Python 3.14 public release",
        freshness_requirement="current",
    )


def diversity() -> object:
    return build_web_source_diversity(
        {
            "diversity_id": "diversity:1",
            "authenticated_turn_id": "turn:1",
            "source_urls": ["https://one.example.com/source", "https://two.example.com/source"],
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


def coverage(state: str) -> object:
    admitted = ("https://one.example.com/source", "https://two.example.com/source")
    cited = {
        "complete": admitted,
        "partial": (admitted[0],),
        "empty": (),
        "blocked_private": ("https://localhost/private",),
    }[state]
    return build_web_citation_coverage(
        f"coverage:{state}",
        "turn:1",
        admitted,
        cited,
    )


def readiness(state: WebResearchReadinessState) -> WebResearchReadinessV1:
    if state is WebResearchReadinessState.READY:
        return build_web_research_readiness(
            "readiness:ready", mission(), diversity(), consumption(WebResearchConsumptionState.CONSUMABLE)
        )
    if state is WebResearchReadinessState.READY_DEGRADED:
        return build_web_research_readiness(
            "readiness:degraded",
            mission(),
            diversity(),
            consumption(WebResearchConsumptionState.CONSUMABLE_DEGRADED),
        )
    return build_web_research_readiness(
        "readiness:not-ready",
        mission(),
        build_web_source_diversity(
            {
                "diversity_id": "diversity:empty",
                "authenticated_turn_id": "turn:1",
                "source_urls": [],
            }
        ),
        consumption(WebResearchConsumptionState.CONSUMABLE),
    )


def test_ready_complete_is_admitted_with_both_input_identities() -> None:
    result = build_web_research_answer_gate(
        "gate:1", readiness(WebResearchReadinessState.READY), coverage("complete")
    )

    assert isinstance(result, WebResearchAnswerGateV1)
    assert result.admission is WebResearchAnswerAdmission.ADMITTED
    assert result.reason is WebResearchAnswerGateReason.READY_COMPLETE_COVERAGE
    assert result.authenticated_turn_id == "turn:1"
    assert result.readiness_id == "readiness:ready"
    assert result.coverage_id == "coverage:complete"


@pytest.mark.parametrize("coverage_state", ("partial",))
def test_partial_coverage_is_admitted_degraded_at_most(coverage_state: str) -> None:
    result = build_web_research_answer_gate(
        "gate:partial", readiness(WebResearchReadinessState.READY), coverage(coverage_state)
    )

    assert result.admission is WebResearchAnswerAdmission.ADMITTED_DEGRADED
    assert result.reason is WebResearchAnswerGateReason.PARTIAL_COVERAGE


def test_degraded_readiness_cannot_become_fully_admitted() -> None:
    result = build_web_research_answer_gate(
        "gate:degraded", readiness(WebResearchReadinessState.READY_DEGRADED), coverage("complete")
    )

    assert result.admission is WebResearchAnswerAdmission.ADMITTED_DEGRADED
    assert result.reason is WebResearchAnswerGateReason.READY_DEGRADED_READINESS


def test_empty_coverage_cannot_be_admitted() -> None:
    result = build_web_research_answer_gate(
        "gate:empty", readiness(WebResearchReadinessState.READY), coverage("empty")
    )

    assert result.admission is WebResearchAnswerAdmission.HOLD
    assert result.reason is WebResearchAnswerGateReason.COVERAGE_EMPTY


def test_blocked_private_coverage_is_blocked_not_hold_or_admitted() -> None:
    result = build_web_research_answer_gate(
        "gate:private", readiness(WebResearchReadinessState.READY), coverage("blocked_private")
    )

    assert result.admission is WebResearchAnswerAdmission.BLOCKED
    assert result.reason is WebResearchAnswerGateReason.COVERAGE_BLOCKED_PRIVATE


def test_not_ready_readiness_cannot_be_admitted() -> None:
    result = build_web_research_answer_gate(
        "gate:not-ready", readiness(WebResearchReadinessState.NOT_READY), coverage("complete")
    )

    assert result.admission is WebResearchAnswerAdmission.HOLD
    assert result.reason is WebResearchAnswerGateReason.READINESS_NOT_READY


def test_mismatched_turn_exposes_no_partial_identity_set() -> None:
    mismatched = build_web_citation_coverage(
        "coverage:other-turn",
        "turn:2",
        ("https://one.example.com/source", "https://two.example.com/source"),
        ("https://one.example.com/source", "https://two.example.com/source"),
    )
    result = build_web_research_answer_gate(
        "gate:mismatch", readiness(WebResearchReadinessState.READY), mismatched
    )

    assert result.admission is WebResearchAnswerAdmission.HOLD
    assert result.reason is WebResearchAnswerGateReason.IDENTITY_MISMATCH
    assert result.authenticated_turn_id is None
    assert result.readiness_id is None
    assert result.coverage_id is None


def test_explicit_turn_must_match_facts() -> None:
    result = build_web_research_answer_gate(
        "gate:explicit-mismatch",
        readiness(WebResearchReadinessState.READY),
        coverage("complete"),
        authenticated_turn_id="turn:other",
    )
    assert result.admission is WebResearchAnswerAdmission.HOLD
    assert result.reason is WebResearchAnswerGateReason.IDENTITY_MISMATCH


def test_invalid_or_missing_inputs_fail_closed_to_hold() -> None:
    invalid = build_web_research_answer_gate("gate:invalid", {"readiness": "invented"}, coverage("complete"))
    missing = build_web_research_answer_gate("gate:missing", readiness(WebResearchReadinessState.READY), None)

    assert invalid.admission is WebResearchAnswerAdmission.HOLD
    assert invalid.reason is WebResearchAnswerGateReason.INPUTS_INVALID
    assert missing.admission is WebResearchAnswerAdmission.HOLD
    assert missing.reason is WebResearchAnswerGateReason.INPUTS_INVALID


def test_mapping_inputs_and_gate_round_trip_are_supported() -> None:
    result = build_web_research_answer_gate(
        {
            "schema": WEB_RESEARCH_ANSWER_GATE_SCHEMA,
            "gate_id": "gate:mapping",
            "readiness": readiness(WebResearchReadinessState.READY).to_mapping(),
            "coverage": coverage("complete"),
        }
    )
    assert result.admission is WebResearchAnswerAdmission.ADMITTED
    assert build_web_research_answer_gate(result.to_mapping()) == result
    assert validate_web_research_answer_gate(result.to_mapping()) is True


def test_gate_is_frozen_and_closed() -> None:
    result = build_web_research_answer_gate(
        "gate:frozen", readiness(WebResearchReadinessState.READY), coverage("complete")
    )
    with pytest.raises(FrozenInstanceError):
        result.admission = WebResearchAnswerAdmission.HOLD  # type: ignore[misc]
    malformed = result.to_mapping()
    malformed["reason"] = "invented"
    assert validate_web_research_answer_gate(malformed) is False
