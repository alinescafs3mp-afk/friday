from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.mixed_journey_identity import (
    MixedJourneyIdentityState,
    build_mixed_journey_identity,
)


def test_empty_and_bound_identity_are_immutable_and_round_trip() -> None:
    empty = build_mixed_journey_identity("journey", "turn")
    assert empty.state is MixedJourneyIdentityState.EMPTY
    bound = build_mixed_journey_identity(
        "journey",
        "turn",
        facts={"operation_id": "operation", "effect_owners": ["owner"], "publishers": ["publisher"]},
    )
    assert bound.state is MixedJourneyIdentityState.BOUND
    assert bound.to_mapping()["operation_id"] == "operation"
    assert build_mixed_journey_identity(bound.to_mapping()) == bound
    with pytest.raises(FrozenInstanceError):
        bound.operation_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "facts",
    [
        {"operation_id": "operation", "effect_owners": ["one", "two"]},
        {"operation_id": "operation", "publishers": ["one", "two"]},
        {"operation_id": "operation", "effect_owners": ["/private/path"]},
        {"operation_id": "operation", "publishers": ["https://secret"]},
        {"operation_id": "operation", "unknown": True},
    ],
)
def test_identity_hazards_fail_closed_without_owner_facts(facts: dict[str, object]) -> None:
    result = build_mixed_journey_identity("journey", "turn", facts=facts)
    assert result.state is MixedJourneyIdentityState.BLOCKED
    assert result.operation_id is None
    assert result.effect_owner_count == 0
    assert result.publisher_count == 0


def test_identity_requires_operation_id() -> None:
    result = build_mixed_journey_identity("journey", "turn", facts={"effect_owner_count": 1})
    assert result.state is MixedJourneyIdentityState.BLOCKED
