from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.shared_operation_secondary import (
    SharedOperationSecondaryState,
    build_shared_operation_secondary,
    validate_shared_operation_secondary,
)


def test_empty_secondary_has_no_presence_fact() -> None:
    result = build_shared_operation_secondary("secondary-1", "turn-1")
    assert result.state is SharedOperationSecondaryState.EMPTY
    assert result.present is None


def test_present_secondary_is_advisory_and_cannot_own_anything() -> None:
    result = build_shared_operation_secondary(
        "secondary-1", "turn-1", facts={"present": True, "secondary_digest": "a" * 64}
    )
    assert result.state is SharedOperationSecondaryState.PRESENT
    assert result.is_present
    assert not result.can_own_tools
    assert not result.can_own_effects
    assert not result.can_publish
    assert build_shared_operation_secondary(result.to_mapping()) == result
    assert validate_shared_operation_secondary(result.to_mapping())


def test_absent_secondary_is_explicitly_absent() -> None:
    result = build_shared_operation_secondary("secondary-1", "turn-1", present=False)
    assert result.state is SharedOperationSecondaryState.ABSENT
    assert result.present is False
    assert result.secondary_digest is None


def test_secondary_ownership_claim_is_blocked_and_exposes_no_facts() -> None:
    result = build_shared_operation_secondary(
        "secondary-1", "turn-1", facts={"present": True, "owns_tools": True}
    )
    assert result.state is SharedOperationSecondaryState.BLOCKED
    assert result.present is None
    assert result.secondary_digest is None


@pytest.mark.parametrize("digest", ["not-a-digest", "A" * 64])
def test_invalid_secondary_digest_is_blocked(digest: str) -> None:
    result = build_shared_operation_secondary("secondary-1", "turn-1", present=True, secondary_digest=digest)
    assert result.state is SharedOperationSecondaryState.BLOCKED


def test_secondary_is_frozen() -> None:
    result = build_shared_operation_secondary("secondary-1", "turn-1", present=False)
    with pytest.raises(FrozenInstanceError):
        result.present = True  # type: ignore[misc]
