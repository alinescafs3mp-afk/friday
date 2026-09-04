from __future__ import annotations

from friday.orchestration.coding_result_archive_plan import (
    CODING_RESULT_ARCHIVE_FILENAME,
    CodingResultArchivePlanReason,
    CodingResultArchivePlanState,
    build_coding_result_archive_plan,
)
from friday.orchestration.coding_source_tree import build_coding_source_tree
from friday.orchestration.engineer_result_carrier import EngineerResultCarrierKind


def _member(path: str, *, kind: str = "regular_file") -> dict[str, object]:
    return {
        "relative_path": path,
        "size": 32,
        "file_kind": kind,
        "executable": False,
        "link_kind": "none",
    }


def _tree(*paths: str, turn: str = "turn-1") -> object:
    members = [_member(path) for path in paths]
    return build_coding_source_tree("tree-1", turn, members)


def test_no_files_are_empty_text() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1")
    assert result.plan is CodingResultArchivePlanState.EMPTY
    assert result.carrier is EngineerResultCarrierKind.TEXT
    assert result.files == ()
    assert result.archive_filename is None


def test_one_ordinary_file_is_not_zipped() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["src/main.py"])
    assert result.plan is CodingResultArchivePlanState.FILE
    assert result.files == ("src/main.py",)
    assert result.archive_filename is None


def test_two_files_select_one_source_archive() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["b.py", "a.py"])
    assert result.plan is CodingResultArchivePlanState.ARCHIVE
    assert result.carrier is EngineerResultCarrierKind.ARCHIVE
    assert result.files == ("a.py", "b.py")
    assert result.archive_filename == CODING_RESULT_ARCHIVE_FILENAME


def test_archive_requested_with_one_file_stays_file() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["only.py"], archive_requested=True)
    assert result.plan is CodingResultArchivePlanState.FILE
    assert result.files == ("only.py",)


def test_secret_names_are_blocked() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=[".env"])
    assert result.plan is CodingResultArchivePlanState.BLOCKED
    assert result.reason is CodingResultArchivePlanReason.SECRET_NAME
    assert result.files == ()


def test_traversal_is_blocked() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["../escape.py"])
    assert result.reason is CodingResultArchivePlanReason.UNSAFE_PATH
    assert result.files == ()


def test_casefold_collision_is_blocked() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["Readme.md", "README.md"])
    assert result.reason is CodingResultArchivePlanReason.CASEFOLD_COLLISION
    assert result.files == ()


def test_internal_receipts_are_not_user_files() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["receipt.json"])
    assert result.plan is CodingResultArchivePlanState.EMPTY
    assert result.files == ()


def test_directories_are_skipped_from_mapped_tree() -> None:
    tree = build_coding_source_tree(
        "tree-1",
        "turn-1",
        [_member("src", kind="directory"), _member("src/main.py"), _member("README.md")],
    )
    result = build_coding_result_archive_plan("plan-1", "turn-1", tree=tree)
    assert result.plan is CodingResultArchivePlanState.ARCHIVE
    assert result.files == ("README.md", "src/main.py")


def test_blocked_tree_does_not_expose_paths() -> None:
    tree = build_coding_source_tree(
        "tree-1",
        "turn-1",
        [_member("link.py") | {"link_kind": "symlink"}],
    )
    result = build_coding_result_archive_plan("plan-1", "turn-1", tree=tree)
    assert result.plan is CodingResultArchivePlanState.BLOCKED
    assert result.reason is CodingResultArchivePlanReason.TREE_BLOCKED
    assert result.files == ()


def test_turn_mismatch_is_blocked() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", tree=_tree("a.py", turn="turn-other"))
    assert result.reason is CodingResultArchivePlanReason.IDENTITY_MISMATCH
    assert result.files == ()


def test_file_limit_is_blocked() -> None:
    files = [f"f{index}.py" for index in range(33)]
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=files)
    assert result.reason is CodingResultArchivePlanReason.FILE_LIMIT
    assert result.files == ()


def test_mapping_roundtrip_agrees() -> None:
    result = build_coding_result_archive_plan("plan-1", "turn-1", files=["a.py", "b.py"])
    encoded = result.to_mapping()
    assert encoded["schema"] == "friday.coding-result-archive-plan.v1"
    assert encoded["carrier"] == "archive"
    assert encoded["files"] == ["a.py", "b.py"]
