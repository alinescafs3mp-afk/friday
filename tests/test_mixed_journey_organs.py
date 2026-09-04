import pytest

from friday.orchestration.mixed_journey_organs import (
    ORGAN_NAMES,
    MixedJourneyOrgansState,
    build_mixed_journey_organs,
)


def _facts(**overrides: bool) -> dict[str, bool]:
    values = {name: False for name in ORGAN_NAMES}
    values.update(overrides)
    return values


def test_empty_and_all_closed_organ_presence() -> None:
    empty = build_mixed_journey_organs("journey", "turn")
    assert empty.state is MixedJourneyOrgansState.EMPTY
    result = build_mixed_journey_organs("journey", "turn", facts=_facts(file=True, web=True))
    assert result.state is MixedJourneyOrgansState.PRESENT
    assert result.present_organs == ("file", "web")
    assert result.absent_organs == ("archive", "conversation", "table", "engineer", "coding")
    assert build_mixed_journey_organs(result.to_mapping()) == result


@pytest.mark.parametrize(
    "facts",
    [
        {"file": True},
        {"file": "yes"},
        {"FILE": True, **_facts()},
        {"file": True, "unknown": False, **_facts()},
    ],
)
def test_unknown_incomplete_and_non_boolean_facts_block(facts: dict[str, object]) -> None:
    result = build_mixed_journey_organs("journey", "turn", facts=facts)
    assert result.state is MixedJourneyOrgansState.BLOCKED
    assert result.organ_presence == ()


def test_boolean_false_is_absent_not_missing() -> None:
    result = build_mixed_journey_organs("journey", "turn", facts=_facts())
    assert result.state is MixedJourneyOrgansState.PRESENT
    assert result.present_organs == ()
    assert set(result.absent_organs) == set(ORGAN_NAMES)
