import pytest

from friday.orchestration.coding_worker_workspace import (
    CodingWorkerWorkspaceFactsV1,
    CodingWorkerWorkspaceReason,
    CodingWorkerWorkspaceState,
    build_coding_worker_workspace,
)

SNAPSHOT = "a" * 64


def facts(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "operation_id": "operation:1",
        "project_root": "/srv/projects/friday",
        "workspace_path": "work/operation-1",
        "input_snapshot_sha256": SNAPSHOT,
        "export_path": "exports/result.json",
    }
    result.update(overrides)
    return result


def test_empty_facts_are_empty() -> None:
    result = build_coding_worker_workspace("workspace:1", "turn:1")

    assert result.workspace is CodingWorkerWorkspaceState.EMPTY
    assert result.workspace_path is None


def test_one_workspace_is_bound_under_the_supplied_root() -> None:
    result = build_coding_worker_workspace(
        "workspace:1",
        "turn:1",
        CodingWorkerWorkspaceFactsV1(
            operation_id="operation:1",
            project_root="/srv/projects/friday",
            workspace_path="work/operation-1",
            input_snapshot_sha256=SNAPSHOT,
            export_path="exports/result.json",
        ),
    )

    assert result.workspace is CodingWorkerWorkspaceState.BOUND
    assert result.operation_id == "operation:1"
    assert result.project_root == "/srv/projects/friday"
    assert result.workspace_path == "work/operation-1"
    assert result.export_path == "exports/result.json"
    assert result.input_snapshot_sha256 == SNAPSHOT


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("workspace_path", "../outside", CodingWorkerWorkspaceReason.WORKSPACE_TRAVERSAL),
        ("workspace_path", "/absolute", CodingWorkerWorkspaceReason.ABSOLUTE_WORKSPACE_PATH),
        ("workspace_path", r"C:\\absolute", CodingWorkerWorkspaceReason.ABSOLUTE_WORKSPACE_PATH),
        ("export_path", "../outside", CodingWorkerWorkspaceReason.EXPORT_TRAVERSAL),
        ("export_path", "/absolute", CodingWorkerWorkspaceReason.ABSOLUTE_EXPORT_PATH),
        ("input_snapshot_sha256", "A" * 64, CodingWorkerWorkspaceReason.INVALID_SNAPSHOT),
        ("input_snapshot_sha256", "not-a-digest", CodingWorkerWorkspaceReason.INVALID_SNAPSHOT),
    ),
)
def test_paths_and_snapshot_fail_closed_without_exposing_paths(
    field: str, value: object, reason: CodingWorkerWorkspaceReason
) -> None:
    result = build_coding_worker_workspace("workspace:1", "turn:1", facts(**{field: value}))

    assert result.workspace is CodingWorkerWorkspaceState.BLOCKED
    assert result.reason is reason
    assert result.project_root is None
    assert result.workspace_path is None
    assert result.export_path is None


def test_multiple_workspaces_and_missing_facts_block() -> None:
    multiple = build_coding_worker_workspace("workspace:1", "turn:1", facts(workspace_count=2))
    missing = build_coding_worker_workspace("workspace:1", "turn:1", facts(export_path=None))

    assert multiple.workspace is CodingWorkerWorkspaceState.BLOCKED
    assert multiple.reason is CodingWorkerWorkspaceReason.MULTIPLE_WORKSPACES
    assert missing.workspace is CodingWorkerWorkspaceState.BLOCKED
    assert missing.reason is CodingWorkerWorkspaceReason.MISSING_EXPORT_PATH


def test_unknown_fields_fail_closed_and_supplied_relative_root_is_accepted() -> None:
    unknown = build_coding_worker_workspace("workspace:1", "turn:1", {**facts(), "secret": "nope"})
    relative_root = build_coding_worker_workspace(
        "workspace:1", "turn:1", facts(project_root="relative/root")
    )

    assert unknown.workspace is CodingWorkerWorkspaceState.BLOCKED
    assert relative_root.workspace is CodingWorkerWorkspaceState.BOUND
    assert relative_root.project_root == "relative/root"
