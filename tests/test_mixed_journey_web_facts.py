from friday.orchestration.mixed_journey_web_facts import (
    MixedJourneyWebFactsState,
    build_mixed_journey_web_facts,
)
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
)


def _consumable() -> WebResearchConsumptionV1:
    return WebResearchConsumptionV1(
        "consumption-1",
        "turn-1",
        WebResearchConsumptionState.CONSUMABLE,
        "yandex",
        1,
        WebResearchConsumptionReason.PRIMARY_SOURCES,
    )


def test_empty_present_and_mapping_round_trip() -> None:
    assert build_mixed_journey_web_facts().state is MixedJourneyWebFactsState.EMPTY
    result = build_mixed_journey_web_facts(_consumable())
    assert result.state is MixedJourneyWebFactsState.PRESENT
    assert result.selected_provider_id == "yandex"
    assert build_mixed_journey_web_facts(result.to_mapping()) == result


def test_blocked_private_empty_after_outbound_and_invalid_provider_are_closed() -> None:
    private = WebResearchConsumptionV1(
        "consumption-1",
        "turn-1",
        WebResearchConsumptionState.BLOCKED_PRIVATE,
        None,
        0,
        WebResearchConsumptionReason.CURRENTNESS_PRIVATE,
    )
    empty = WebResearchConsumptionV1(
        "consumption-1",
        "turn-1",
        WebResearchConsumptionState.UNAVAILABLE,
        None,
        0,
        WebResearchConsumptionReason.NO_ADMITTED_SOURCES,
    )
    for result in (build_mixed_journey_web_facts(private), build_mixed_journey_web_facts(empty)):
        assert result.state is MixedJourneyWebFactsState.BLOCKED
        assert result.selected_provider_id is None
        assert result.admitted_source_count == 0
        assert "private.example" not in str(result.to_mapping())
    invalid = build_mixed_journey_web_facts(
        {
            "consumption_id": "consumption-1",
            "authenticated_turn_id": "turn-1",
            "usability": "consumable",
            "selected_provider_id": "not-a-provider",
            "admitted_source_count": 1,
            "reason": "primary_sources",
        }
    )
    assert invalid.state is MixedJourneyWebFactsState.BLOCKED


def test_private_url_fields_are_rejected_without_url_output() -> None:
    result = build_mixed_journey_web_facts(
        {
            "consumption_id": "consumption-1",
            "authenticated_turn_id": "turn-1",
            "usability": "consumable",
            "selected_provider_id": "yandex",
            "admitted_source_count": 1,
            "reason": "primary_sources",
            "source_urls": ["http://127.0.0.1/private"],
        }
    )
    assert result.state is MixedJourneyWebFactsState.BLOCKED
    assert "127.0.0.1" not in str(result.to_mapping())
