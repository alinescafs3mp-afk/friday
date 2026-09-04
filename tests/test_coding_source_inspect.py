from __future__ import annotations

from friday.orchestration.coding_source_inspect import (
    CodingSourceInspectReason,
    CodingSourceInspectState,
    build_coding_source_inspect,
)
from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_source_tree import build_coding_source_tree


def member(path: str, *, executable: bool = False) -> CodingSourceMemberV1:
    return CodingSourceMemberV1(path, 4, "file", executable, "none")


def test_empty_source_is_empty() -> None:
    result = build_coding_source_inspect("inspect-1", "turn-1", members=())
    assert result.inspection is CodingSourceInspectState.EMPTY
    assert result.member_count == 0
    assert result.reason is CodingSourceInspectReason.NO_MEMBERS
    assert result.execution_attempted is False
    assert result.rebuild_attempted is False


def test_static_inspect_counts_members_without_execution_or_rebuild() -> None:
    result = build_coding_source_inspect(
        "inspect-1",
        "turn-1",
        members=(member("main.py", executable=True), member("src")),
    )
    assert result.inspection is CodingSourceInspectState.INSPECTED
    assert result.member_count == 2
    assert result.file_count == 2
    assert result.directory_count == 0
    assert result.executable_member_count == 1
    assert result.executed is False
    assert result.rebuilt is False


def test_inspect_accepts_a_mapped_tree() -> None:
    tree = build_coding_source_tree("tree-1", "turn-1", (member("main.py"),))
    result = build_coding_source_inspect("inspect-1", "turn-1", tree)
    assert result.inspection is CodingSourceInspectState.INSPECTED
    assert result.member_count == 1


def test_blocked_tree_is_not_inspected() -> None:
    result = build_coding_source_inspect(
        "inspect-1",
        "turn-1",
        members=({"path": "../escape", "size": 1, "kind": "file"},),
    )
    assert result.inspection is CodingSourceInspectState.BLOCKED
    assert result.reason is CodingSourceInspectReason.TREE_BLOCKED
    assert result.member_count == 0


def test_invalid_inspect_input_is_blocked() -> None:
    result = build_coding_source_inspect("inspect-1", "turn-1", source_tree={"unexpected": ()})
    assert result.inspection is CodingSourceInspectState.BLOCKED
    assert result.reason is CodingSourceInspectReason.INVALID_FACTS
