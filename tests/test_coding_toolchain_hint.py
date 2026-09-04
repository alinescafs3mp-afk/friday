from __future__ import annotations

from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_toolchain_hint import (
    CodingToolchainHintReason,
    CodingToolchainHintState,
    build_coding_toolchain_hint,
)


def member(path: str, *, kind: str = "file") -> CodingSourceMemberV1:
    return CodingSourceMemberV1(path, 1, kind, False, "none")


def test_empty_members_are_empty() -> None:
    result = build_coding_toolchain_hint("hint-1", "turn-1", members=())
    assert result.hint is CodingToolchainHintState.EMPTY
    assert result.member_count == 0
    assert result.reason is CodingToolchainHintReason.NO_MEMBERS
    assert result.installed is False


def test_filename_suffixes_produce_neutral_hints_only() -> None:
    result = build_coding_toolchain_hint("hint-1", "turn-1", members=(member("main.py"), member("web.ts")))
    assert result.hint is CodingToolchainHintState.HINTED
    assert result.detected_suffixes == (".py", ".ts")
    assert result.language_hints == ("python", "typescript")
    assert result.installed is False


def test_unknown_suffixes_and_directories_do_not_invent_a_toolchain() -> None:
    result = build_coding_toolchain_hint(
        "hint-1",
        "turn-1",
        members=(member("README.unknown"), member("src", kind="directory")),
    )
    assert result.hint is CodingToolchainHintState.EMPTY
    assert result.member_count == 2
    assert result.detected_suffixes == ()
    assert result.language_hints == ()
    assert result.reason is CodingToolchainHintReason.NO_KNOWN_SUFFIXES


def test_blocked_tree_is_not_used_for_hints() -> None:
    result = build_coding_toolchain_hint(
        "hint-1",
        "turn-1",
        members=({"path": "link.py", "size": 1, "kind": "file", "link": "symlink"},),
    )
    assert result.hint is CodingToolchainHintState.BLOCKED
    assert result.reason is CodingToolchainHintReason.TREE_BLOCKED
    assert result.member_count == 0


def test_invalid_hint_input_is_blocked() -> None:
    result = build_coding_toolchain_hint("hint-1", "turn-1", source_tree={"bad": ()})
    assert result.hint is CodingToolchainHintState.BLOCKED
    assert result.reason is CodingToolchainHintReason.INVALID_FACTS
