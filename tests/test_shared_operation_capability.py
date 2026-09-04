from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.shared_operation_capability import (
    SharedOperationCapabilityState,
    build_shared_operation_capability,
    validate_shared_operation_capability,
)


def test_empty_capability_has_no_availability() -> None:
    result = build_shared_operation_capability("capability-1", "turn-1")
    assert result.state is SharedOperationCapabilityState.EMPTY
    assert result.available is None


@pytest.mark.parametrize(
    ("available", "expected"),
    [(True, SharedOperationCapabilityState.AVAILABLE), (False, SharedOperationCapabilityState.UNAVAILABLE)],
)
def test_capability_projects_availability_only(
    available: bool, expected: SharedOperationCapabilityState
) -> None:
    result = build_shared_operation_capability(
        "files.read", "turn-1", facts={"capability_id": "files.read", "available": available}
    )
    assert result.state is expected
    assert result.available is available
    assert "authority" not in result.to_mapping()
    assert build_shared_operation_capability(result.to_mapping()) == result
    assert validate_shared_operation_capability(result.to_mapping())


def test_authority_and_execution_claims_are_blocked() -> None:
    result = build_shared_operation_capability(
        "files.read",
        "turn-1",
        facts={"capability_id": "files.read", "available": True, "authority_token": "secret"},
    )
    assert result.state is SharedOperationCapabilityState.BLOCKED
    assert result.available is None


@pytest.mark.parametrize("facts", [{"capability_id": "files.read"}, {"available": "maybe"}])
def test_invalid_capability_facts_fail_closed(facts: dict[str, object]) -> None:
    result = build_shared_operation_capability("files.read", "turn-1", facts=facts)
    assert result.state is SharedOperationCapabilityState.BLOCKED
    assert result.available is None


def test_capability_is_frozen() -> None:
    result = build_shared_operation_capability("files.read", "turn-1", available=True)
    with pytest.raises(FrozenInstanceError):
        result.available = False  # type: ignore[misc]
