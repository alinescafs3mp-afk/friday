from __future__ import annotations

from friday.orchestration.coding_project_scaffold import (
    CodingProjectScaffoldReason,
    CodingProjectScaffoldState,
    build_coding_project_scaffold,
)


def test_relative_files_are_scaffolded() -> None:
    result = build_coding_project_scaffold("scaf-1", "turn-1", ["README.md", {"path": "src/main.py"}])
    assert result.scaffold is CodingProjectScaffoldState.SCAFFOLDED
    assert [item.path for item in result.files] == ["README.md", "src/main.py"]


def test_missing_files_are_empty() -> None:
    result = build_coding_project_scaffold("scaf-1", "turn-1")
    assert result.scaffold is CodingProjectScaffoldState.EMPTY
    assert result.files == ()


def test_secret_names_are_blocked() -> None:
    result = build_coding_project_scaffold("scaf-1", "turn-1", [".env"])
    assert result.scaffold is CodingProjectScaffoldState.BLOCKED
    assert result.reason is CodingProjectScaffoldReason.SECRET_NAME
    assert result.files == ()


def test_casefold_collision_is_blocked() -> None:
    result = build_coding_project_scaffold("scaf-1", "turn-1", ["Readme.md", "README.md"])
    assert result.scaffold is CodingProjectScaffoldState.BLOCKED
    assert result.reason is CodingProjectScaffoldReason.CASEFOLD_COLLISION
    assert result.files == ()


def test_traversal_is_blocked() -> None:
    result = build_coding_project_scaffold("scaf-1", "turn-1", ["../escape.py"])
    assert result.reason is CodingProjectScaffoldReason.UNSAFE_PATH
    assert result.files == ()
