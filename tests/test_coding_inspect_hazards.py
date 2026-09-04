from __future__ import annotations

from friday.orchestration.coding_inspect_hazards import (
    CodingInspectHazardKind,
    CodingInspectHazardsReason,
    CodingInspectHazardsState,
    build_coding_inspect_hazards,
)
from friday.orchestration.coding_source_member import CodingSourceMemberV1


def member(path: str, *, executable: bool = False) -> CodingSourceMemberV1:
    return CodingSourceMemberV1(path, 4, "file", executable, "none")


def test_empty_hazard_scan_is_empty() -> None:
    result = build_coding_inspect_hazards("hazard-1", "turn-1", members=())
    assert result.hazards is CodingInspectHazardsState.EMPTY
    assert result.member_count == 0
    assert result.reason is CodingInspectHazardsReason.NO_MEMBERS


def test_clean_names_and_metadata_are_clear() -> None:
    result = build_coding_inspect_hazards("hazard-1", "turn-1", members=(member("src/main.py"),))
    assert result.hazards is CodingInspectHazardsState.CLEAR
    assert result.member_count == 1
    assert result.hazard_count == 0
    assert result.hazard_kinds == ()


def test_secret_name_executable_bit_and_nested_vcs_are_present() -> None:
    result = build_coding_inspect_hazards(
        "hazard-1",
        "turn-1",
        members=(
            member(".env"),
            member("bin/run.sh", executable=True),
            member("vendor/.git/config"),
        ),
    )
    assert result.hazards is CodingInspectHazardsState.PRESENT
    assert result.member_count == 3
    assert result.hazard_count == 3
    assert result.secret_name_count == 1
    assert result.executable_member_count == 1
    assert result.nested_vcs_dir_count == 1
    assert set(result.hazard_kinds) == {
        CodingInspectHazardKind.SECRET_LOOKING_NAME,
        CodingInspectHazardKind.EXECUTABLE_BIT,
        CodingInspectHazardKind.NESTED_VCS_DIRECTORY,
    }


def test_blocked_tree_is_not_scanned() -> None:
    result = build_coding_inspect_hazards(
        "hazard-1",
        "turn-1",
        members=({"path": "link", "size": 1, "kind": "file", "link": "symlink"},),
    )
    assert result.hazards is CodingInspectHazardsState.BLOCKED
    assert result.reason is CodingInspectHazardsReason.TREE_BLOCKED
    assert result.member_count == 0


def test_invalid_hazard_input_is_blocked() -> None:
    result = build_coding_inspect_hazards("hazard-1", "turn-1", source_tree={"unknown": ()})
    assert result.hazards is CodingInspectHazardsState.BLOCKED
    assert result.reason is CodingInspectHazardsReason.INVALID_FACTS
