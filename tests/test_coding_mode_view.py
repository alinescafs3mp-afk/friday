from __future__ import annotations

from friday.orchestration.coding_inspect_report import build_coding_inspect_report
from friday.orchestration.coding_mode_execute_claim import build_coding_mode_execute_claim
from friday.orchestration.coding_mode_intent import build_coding_mode_intent
from friday.orchestration.coding_mode_view import (
    CodingModeViewState,
    build_coding_mode_view,
)


def test_no_facts_are_empty() -> None:
    result = build_coding_mode_view("view-1", "turn-1")
    assert result.state is CodingModeViewState.EMPTY
    assert result.live_process_claimed is False


def test_static_inspect_projects_without_worker() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", inspect=True)
    execute = build_coding_mode_execute_claim("claim-1", "turn-1", intent)
    report = build_coding_inspect_report(
        "report-1",
        "turn-1",
        members=(
            {"path": "main.py", "size": 1, "file_kind": "file", "executable": False, "link_kind": "none"},
        ),
    )
    result = build_coding_mode_view(
        "view-1", "turn-1", intent=intent, execute_claim=execute, inspect_report=report
    )
    assert result.state is CodingModeViewState.PROJECTED
    assert result.worker_admitted is False


def test_blocked_component_blocks_view_and_roundtrips() -> None:
    blocked_intent = build_coding_mode_intent("intent-1", "turn-1", prompt="x", inspect=True)
    result = build_coding_mode_view("view-1", "turn-1", intent=blocked_intent)
    assert result.state is CodingModeViewState.BLOCKED
    assert build_coding_mode_view(result.to_mapping()) == result


def test_turn_mismatch_blocks() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-other", inspect=True)
    result = build_coding_mode_view("view-1", "turn-1", intent=intent)
    assert result.state is CodingModeViewState.BLOCKED
