import pytest

from friday.orchestration.mixed_journey_conversation_facts import (
    MixedJourneyConversationFactsState,
    build_mixed_journey_conversation_facts,
)


def test_empty_present_and_mapping_round_trip() -> None:
    assert build_mixed_journey_conversation_facts().state is MixedJourneyConversationFactsState.EMPTY
    result = build_mixed_journey_conversation_facts("conversation-1", "turn-1", "revision:4")
    assert result.state is MixedJourneyConversationFactsState.PRESENT
    assert result.summary_digest
    assert build_mixed_journey_conversation_facts(result.to_mapping()) == result


@pytest.mark.parametrize(
    "facts",
    [
        {
            "conversation_id": "conversation-1",
            "authenticated_turn_id": "turn-1",
            "recency_selector": "latest",
        },
        {"conversation_id": "conversation-1", "authenticated_turn_id": "turn-1", "recency_selector": "HEAD"},
        {"conversation_id": "/private/conversation", "authenticated_turn_id": "turn-1"},
        {"conversation_id": "conversation-1", "authenticated_turn_id": "turn-1", "messages": ["body"]},
        {"conversation_id": "conversation-1"},
    ],
)
def test_unsafe_recency_and_message_facts_block_without_body(facts: dict[str, object]) -> None:
    result = build_mixed_journey_conversation_facts(facts)
    assert result.state is MixedJourneyConversationFactsState.BLOCKED
    assert result.conversation_id is None
    assert result.authenticated_turn_id is None
    assert result.summary_digest is None
    assert "body" not in str(result.to_mapping())
