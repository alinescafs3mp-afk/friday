import pytest

from friday.orchestration.mixed_journey_restart import (
    MixedJourneyRestartState,
    build_mixed_journey_restart,
)


def test_empty_continuing_and_restarted_states() -> None:
    assert build_mixed_journey_restart("journey", "turn").state is MixedJourneyRestartState.EMPTY
    continuing = build_mixed_journey_restart(
        "journey", "turn", facts={"status": "running", "execution": "running"}
    )
    assert continuing.state is MixedJourneyRestartState.CONTINUING
    restarted = build_mixed_journey_restart(
        "journey", "turn", facts={"status": "restarted", "execution": "running", "restarted": True}
    )
    assert restarted.state is MixedJourneyRestartState.RESTARTED
    assert build_mixed_journey_restart(restarted.to_mapping()) == restarted


@pytest.mark.parametrize(
    "facts",
    [
        {"status": "running"},
        {"status": "bogus", "execution": "running"},
        {"status": "running", "execution": "running", "effect_owners": ["one", "two"]},
        {"status": "running", "execution": "running", "recency_selector": "latest"},
        {"status": "running", "execution": "running", "recency_selector": "HEAD"},
        {"status": "running", "execution": "running", "recency_selector": "/tmp/current"},
    ],
)
def test_restart_hazards_fail_closed(facts: dict[str, object]) -> None:
    result = build_mixed_journey_restart("journey", "turn", facts=facts)
    assert result.state is MixedJourneyRestartState.BLOCKED
    assert result.effect_owner_count == 0
    assert result.recency_selector is None


def test_fixed_revision_selector_is_explicit() -> None:
    result = build_mixed_journey_restart(
        "journey",
        "turn",
        facts={"status": "running", "execution": "running", "recency_selector": "revision:2"},
    )
    assert result.state is MixedJourneyRestartState.CONTINUING
    assert result.recency_selector == "revision:2"
