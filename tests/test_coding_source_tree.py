from __future__ import annotations

from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_source_tree import (
    CodingSourceTreeReason,
    CodingSourceTreeState,
    build_coding_source_tree,
)


def member(path: str, *, link: str = "none") -> CodingSourceMemberV1:
    return CodingSourceMemberV1(path, 1, "file", False, link)


def test_empty_tree_is_empty() -> None:
    result = build_coding_source_tree("tree-1", "turn-1", ())
    assert result.tree is CodingSourceTreeState.EMPTY
    assert result.member_count == 0
    assert result.members == ()
    assert result.reason is CodingSourceTreeReason.NO_MEMBERS


def test_tree_maps_clean_members_and_preserves_order() -> None:
    result = build_coding_source_tree("tree-1", "turn-1", (member("README.md"), member("src/main.py")))
    assert result.tree is CodingSourceTreeState.MAPPED
    assert result.member_count == 2
    assert tuple(item.relative_path for item in result.members) == ("README.md", "src/main.py")


def test_traversal_and_absolute_paths_are_blocked_without_counts() -> None:
    traversal = build_coding_source_tree(
        "tree-1", "turn-1", ({"path": "../escape", "size": 1, "kind": "file"},)
    )
    absolute = build_coding_source_tree("tree-2", "turn-1", ({"path": "/escape", "size": 1, "kind": "file"},))
    assert traversal.tree is CodingSourceTreeState.BLOCKED
    assert traversal.reason is CodingSourceTreeReason.PATH_TRAVERSAL
    assert absolute.tree is CodingSourceTreeState.BLOCKED
    assert absolute.reason is CodingSourceTreeReason.ABSOLUTE_PATH
    assert traversal.member_count == absolute.member_count == 0


def test_symlink_and_hardlink_are_blocked() -> None:
    symlink = build_coding_source_tree("tree-1", "turn-1", (member("link", link="symlink"),))
    hardlink = build_coding_source_tree("tree-2", "turn-1", (member("link", link="hardlink"),))
    assert symlink.tree is CodingSourceTreeState.BLOCKED
    assert symlink.reason is CodingSourceTreeReason.SYMLINK
    assert hardlink.tree is CodingSourceTreeState.BLOCKED
    assert hardlink.reason is CodingSourceTreeReason.HARDLINK
    assert symlink.members == hardlink.members == ()


def test_casefold_collision_is_blocked() -> None:
    result = build_coding_source_tree("tree-1", "turn-1", (member("Readme.md"), member("README.md")))
    assert result.tree is CodingSourceTreeState.BLOCKED
    assert result.reason is CodingSourceTreeReason.CASEFOLD_COLLISION
    assert result.member_count == 0


def test_invalid_member_shape_is_blocked() -> None:
    result = build_coding_source_tree("tree-1", "turn-1", ({"path": "file.py"},))
    assert result.tree is CodingSourceTreeState.BLOCKED
    assert result.reason is CodingSourceTreeReason.INVALID_FACTS
    assert result.member_count == 0
