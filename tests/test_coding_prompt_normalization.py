from __future__ import annotations

import pytest

from friday.orchestration.coding_prompt_normalization import (
    CodingPromptFactsV1,
    CodingPromptNormalizationReason,
    CodingPromptNormalizationState,
    build_coding_prompt_normalization,
)


def test_title_and_goal_normalize() -> None:
    result = build_coding_prompt_normalization(
        "prompt-1",
        "turn-1",
        {"title": "photo-indexer", "goal": "Index local photos by date", "language_hint": "python"},
    )
    assert result.prompt is CodingPromptNormalizationState.NORMALIZED
    assert result.title == "photo-indexer"
    assert result.language_hint == "python"
    with pytest.raises(AttributeError):
        result.title = "other"  # type: ignore[misc]


def test_missing_facts_are_empty() -> None:
    result = build_coding_prompt_normalization("prompt-1", "turn-1")
    assert result.prompt is CodingPromptNormalizationState.EMPTY
    assert result.reason is CodingPromptNormalizationReason.NO_FACTS
    assert result.goal is None


def test_title_only_is_blocked_without_leaking() -> None:
    result = build_coding_prompt_normalization("prompt-1", "turn-1", title="photo-indexer")
    assert result.prompt is CodingPromptNormalizationState.BLOCKED
    assert result.reason is CodingPromptNormalizationReason.MISSING_GOAL
    assert result.title is None


@pytest.mark.parametrize("goal", ("Do everything", "universal agent", "что угодно"))
def test_unbounded_goals_are_blocked(goal: str) -> None:
    result = build_coding_prompt_normalization("prompt-1", "turn-1", goal=goal)
    assert result.prompt is CodingPromptNormalizationState.BLOCKED
    assert result.reason is CodingPromptNormalizationReason.UNBOUNDED_GOAL
    assert result.goal is None


@pytest.mark.parametrize(
    "goal",
    ("read /etc/passwd", "see https://example.com", "token: abc", "copy ../secret"),
)
def test_unsafe_text_is_blocked(goal: str) -> None:
    result = build_coding_prompt_normalization("prompt-1", "turn-1", goal=goal)
    assert result.prompt is CodingPromptNormalizationState.BLOCKED
    assert result.reason is CodingPromptNormalizationReason.UNSAFE_TEXT
    assert result.goal is None


def test_frozen_facts_and_mapping_agree() -> None:
    frozen = build_coding_prompt_normalization(
        "prompt-1",
        "turn-1",
        CodingPromptFactsV1("notes", "Write a notes CLI", "go"),
    )
    mapped = build_coding_prompt_normalization(
        "prompt-1",
        "turn-1",
        {"title": "notes", "goal": "Write a notes CLI", "language": "go"},
    )
    assert frozen.prompt is CodingPromptNormalizationState.NORMALIZED
    assert mapped.language_hint == "go"
