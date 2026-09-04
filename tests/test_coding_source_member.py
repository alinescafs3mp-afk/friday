from __future__ import annotations

import pytest

from friday.orchestration.coding_source_member import (
    CodingSourceFileKind,
    CodingSourceLinkKind,
    CodingSourceMemberError,
    CodingSourceMemberV1,
    build_coding_source_member,
)


def test_regular_member_is_frozen_and_preserves_metadata() -> None:
    member = CodingSourceMemberV1(
        "src/main.py", 42, CodingSourceFileKind.FILE, True, CodingSourceLinkKind.NONE
    )
    assert member.relative_path == "src/main.py"
    assert member.path == "src/main.py"
    assert member.size == 42
    assert member.file_kind is CodingSourceFileKind.REGULAR_FILE
    assert member.executable_bit is True
    with pytest.raises(AttributeError):
        member.size = 43  # type: ignore[misc]


def test_mapping_aliases_build_the_same_member() -> None:
    member = build_coding_source_member(
        {
            "path": "README.md",
            "bytes": 10,
            "kind": "file",
            "executable_bit": False,
            "link": "none",
        }
    )
    assert member.relative_path == "README.md"
    assert member.size == 10
    assert member.file_kind is CodingSourceFileKind.REGULAR_FILE


@pytest.mark.parametrize(
    "path",
    ("", ".", "..", "../escape.py", "src/../../escape.py", "/tmp/x", "C:/tmp/x", "src\\x.py", "src//x.py"),
)
def test_absolute_and_traversal_paths_are_invalid(path: str) -> None:
    with pytest.raises(CodingSourceMemberError):
        CodingSourceMemberV1(path, 1, "file", False, "none")


def test_link_facts_are_recorded_for_tree_gate() -> None:
    symlink = CodingSourceMemberV1("link", 1, "file", False, "symlink")
    hardlink = CodingSourceMemberV1("hard", 1, "file", False, "hard_link")
    assert symlink.link_kind is CodingSourceLinkKind.SYMLINK
    assert hardlink.link_kind is CodingSourceLinkKind.HARDLINK


@pytest.mark.parametrize(
    ("size", "executable", "file_kind"),
    ((-1, False, "file"), (1, 1, "file"), (1, False, "device")),
)
def test_invalid_member_facts_raise(size: object, executable: object, file_kind: object) -> None:
    with pytest.raises(CodingSourceMemberError):
        CodingSourceMemberV1("file.py", size, file_kind, executable, "none")  # type: ignore[arg-type]
