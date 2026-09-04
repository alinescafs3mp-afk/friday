from __future__ import annotations

from friday.orchestration.coding_mode_intent import (
    CodingModeIntentReason,
    CodingModeIntentState,
    build_coding_mode_intent,
)


def test_empty_prompt_upload_inspect_and_continue_states() -> None:
    assert build_coding_mode_intent("intent-1", "turn-1").state is CodingModeIntentState.EMPTY
    prompt = build_coding_mode_intent("intent-1", "turn-1", prompt="make a tiny app")
    assert prompt.state is CodingModeIntentState.PROMPT
    assert prompt.prompt_body == "make a tiny app"
    assert build_coding_mode_intent("intent-1", "turn-1", upload={"name": "source.zip"}).state is CodingModeIntentState.UPLOAD
    assert build_coding_mode_intent("intent-1", "turn-1", inspect=True).state is CodingModeIntentState.INSPECT
    continued = build_coding_mode_intent(
        "intent-1", "turn-1", project_id="project-1", revision_selector="revision-1"
    )
    assert continued.state is CodingModeIntentState.CONTINUE
    assert continued.project_id == "project-1"


def test_two_intents_block_without_prompt_body() -> None:
    result = build_coding_mode_intent("intent-1", "turn-1", prompt="secret body", inspect=True)
    assert result.state is CodingModeIntentState.BLOCKED
    assert result.reason is CodingModeIntentReason.MULTIPLE_INTENTS
    assert result.prompt is None


def test_recency_selectors_fail_closed() -> None:
    for selector in ("latest", "HEAD", "newest", "current"):
        result = build_coding_mode_intent(
            "intent-1", "turn-1", project_id="project-1", revision_selector=selector
        )
        assert result.state is CodingModeIntentState.BLOCKED
        assert result.reason is CodingModeIntentReason.RECENCY_REVISION_SELECTOR


def test_invalid_facts_and_mapping_roundtrip_fail_closed() -> None:
    invalid = build_coding_mode_intent("intent-1", "turn-1", facts={"prompt": object()})
    assert invalid.state is CodingModeIntentState.BLOCKED
    result = build_coding_mode_intent("intent-1", "turn-1", prompt="hello")
    assert build_coding_mode_intent(result.to_mapping()) == result
