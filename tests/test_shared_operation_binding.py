from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.shared_operation_binding import (
    SharedOperationBindingState,
    build_shared_operation_binding,
    validate_shared_operation_binding,
)

OWNER = "a" * 64
CONVERSATION = "b" * 64


def test_empty_binding_has_no_facts() -> None:
    result = build_shared_operation_binding("binding-1", "turn-1")
    assert result.state is SharedOperationBindingState.EMPTY
    assert result.owner_digest is None
    assert result.conversation_digest is None
    assert result.binding_digest is None


def test_bound_binding_derives_one_safe_digest_and_round_trips() -> None:
    result = build_shared_operation_binding(
        "binding-1",
        "turn-1",
        owner_digest=OWNER,
        conversation_digest=CONVERSATION,
    )
    assert result.state is SharedOperationBindingState.BOUND
    assert len(result.binding_digest or "") == 64
    assert build_shared_operation_binding(result.to_mapping()) == result
    assert validate_shared_operation_binding(result.to_mapping())


@pytest.mark.parametrize(
    "facts",
    [
        {"owner_id": "raw-owner", "conversation_digest": CONVERSATION},
        {"owner_digest": OWNER, "conversation_id": "raw-conversation"},
        {"owner_digest": "/private/owner", "conversation_digest": CONVERSATION},
        {"owner_digest": "token-value", "conversation_digest": CONVERSATION},
    ],
)
def test_raw_owner_conversation_and_private_facts_fail_closed(facts: dict[str, str]) -> None:
    result = build_shared_operation_binding("binding-1", "turn-1", facts=facts)
    assert result.state is SharedOperationBindingState.BLOCKED
    assert result.owner_digest is None
    assert result.conversation_digest is None
    assert result.binding_digest is None


def test_missing_digest_is_blocked_without_partial_binding() -> None:
    result = build_shared_operation_binding("binding-1", "turn-1", owner_digest=OWNER)
    assert result.state is SharedOperationBindingState.BLOCKED
    assert result.owner_digest is None


def test_binding_is_frozen() -> None:
    result = build_shared_operation_binding(
        "binding-1", "turn-1", owner_digest=OWNER, conversation_digest=CONVERSATION
    )
    with pytest.raises(FrozenInstanceError):
        result.binding_id = "other"  # type: ignore[misc]
