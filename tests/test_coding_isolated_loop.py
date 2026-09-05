from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from friday.orchestration.coding_mode_execute_claim import CodingModeExecuteOperation
from friday.orchestration.coding_worker_admission import CodingWorkerAdmissionState
from friday.organs.coding.extract import (
    CodingArchiveExtractObserveReason,
    CodingArchiveExtractObserveState,
    CodingArchiveExtractObserveV1,
)
from friday.organs.coding.loop import (
    CodingIsolatedLoopReason,
    CodingIsolatedLoopState,
    observe_coding_isolated_loop,
)
from friday.organs.coding.worker_boundary import default_coding_worker_boundary
from friday.organs.coding.worker_spawn import (
    BWRAP_EXECUTABLE,
    compose_coding_worker_admission,
    spawn_coding_worker,
)

SNAPSHOT = "a" * 64


def _boundary(tmp_path: Path, **overrides: object):
    homes = {
        "friday_home": str(tmp_path / "friday-home"),
        "owner_home": str(tmp_path / "owner"),
        "database_path": str(tmp_path / "friday-home" / "data" / "state"),
        "worker_root": str(tmp_path / "friday-coding-worker"),
        "workspace_path": "work/operation.1",
        "export_path": "out/operation.1",
    }
    homes.update(overrides)
    return default_coding_worker_boundary(**homes)


def _admission(tmp_path: Path, boundary=None):
    boundary = boundary or _boundary(tmp_path)
    return compose_coding_worker_admission(
        admission_id="admission.1",
        authenticated_turn_id="turn.1",
        worker_id="worker.1",
        operation_id="operation.1",
        project_id="project.1",
        revision_selector=SNAPSHOT,
        boundary=boundary,
    )


def _extracted() -> CodingArchiveExtractObserveV1:
    return CodingArchiveExtractObserveV1(
        CodingArchiveExtractObserveState.EXTRACTED,
        CodingArchiveExtractObserveReason.EXTRACTED,
        1,
        False,
    )


def test_loop_module_does_not_import_docker_or_engineer() -> None:
    source = Path(observe_coding_isolated_loop.__code__.co_filename).read_text(encoding="utf-8")
    assert "import docker" not in source
    assert "friday.organs.engineer" not in source
    assert "from docker" not in source
    assert "ZipFile.extract" not in source


def test_missing_extract_is_empty(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    spawn = spawn_coding_worker(admission, boundary, runner=lambda argv, timeout: 0)
    empty = CodingArchiveExtractObserveV1(
        CodingArchiveExtractObserveState.EMPTY,
        CodingArchiveExtractObserveReason.NO_ARCHIVE,
        0,
        False,
    )
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=empty,
        operation=CodingModeExecuteOperation.TEST,
    )
    assert result.state is CodingIsolatedLoopState.EMPTY
    assert result.untrusted_execute is False


def test_execute_and_run_stay_fail_closed(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        calls.append(argv)
        del timeout_sec
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    for operation in (CodingModeExecuteOperation.EXECUTE, CodingModeExecuteOperation.RUN):
        result = observe_coding_isolated_loop(
            admission=admission,
            boundary=boundary,
            spawn=spawn,
            extract=_extracted(),
            operation=operation,
            runner=runner,
        )
        assert result.state is CodingIsolatedLoopState.BLOCKED
        assert result.reason is CodingIsolatedLoopReason.EXECUTE_FORBIDDEN
        assert result.untrusted_execute is False
    assert len(calls) == 1


def test_blocked_admission_does_not_run_loop(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path, worker_root=str(tmp_path / "friday-home"))
    admission = _admission(tmp_path, boundary)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        calls.append(argv)
        del timeout_sec
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.BUILD,
        runner=runner,
    )
    assert admission.admission is CodingWorkerAdmissionState.BLOCKED
    assert result.state is CodingIsolatedLoopState.BLOCKED
    assert result.reason is CodingIsolatedLoopReason.WORKER_NOT_ADMITTED
    assert result.untrusted_execute is False
    assert calls == []


def test_build_compiles_without_executing_upload(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        calls.append(argv)
        del timeout_sec
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.BUILD,
        runner=runner,
    )
    assert result.state is CodingIsolatedLoopState.BUILT
    assert result.reason is CodingIsolatedLoopReason.BUILD_OK
    assert result.untrusted_execute is False
    assert len(calls) == 2
    assert calls[1][0] == BWRAP_EXECUTABLE
    assert any("py_compile" in part for part in calls[1])
    assert "main.py" not in calls[1]


def test_test_without_tests_is_blocked_without_execute(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        del timeout_sec
        if any("loader.discover" in part for part in argv):
            return 2
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.TEST,
        runner=runner,
    )
    assert result.state is CodingIsolatedLoopState.BLOCKED
    assert result.reason is CodingIsolatedLoopReason.NO_TESTS
    assert result.untrusted_execute is False


def test_admitted_unittest_sets_untrusted_execute(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        calls.append(argv)
        del timeout_sec
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n",
        encoding="utf-8",
    )
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.TEST,
        runner=runner,
    )
    assert result.state is CodingIsolatedLoopState.TESTED
    assert result.untrusted_execute is True
    assert any("loader.discover" in part for part in calls[1])
    assert "--unshare-all" in calls[1]


def test_real_bwrap_compiles_extracted_python(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    spawn = spawn_coding_worker(admission, boundary)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.BUILD,
    )
    assert spawn.probe == "confirmed"
    assert result.state is CodingIsolatedLoopState.BUILT
    assert result.untrusted_execute is False


def test_real_bwrap_unittest_of_extracted_tests(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    spawn = spawn_coding_worker(admission, boundary)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "test_ok.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n",
        encoding="utf-8",
    )
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.TEST,
    )
    assert result.state is CodingIsolatedLoopState.TESTED
    assert result.untrusted_execute is True


def test_syntax_error_build_is_blocked(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    spawn = spawn_coding_worker(admission, boundary)
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "bad.py").write_text("def (\n", encoding="utf-8")
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.BUILD,
    )
    assert result.state is CodingIsolatedLoopState.BLOCKED
    assert result.reason is CodingIsolatedLoopReason.BUILD_FAILED
    assert result.untrusted_execute is False


def test_host_network_boundary_does_not_reach_loop(tmp_path: Path) -> None:
    boundary = replace(_boundary(tmp_path), host_network=True, network_disabled=False)
    admission = _admission(tmp_path, boundary)
    spawn = spawn_coding_worker(admission, boundary, runner=lambda argv, timeout: 0)
    result = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=_extracted(),
        operation=CodingModeExecuteOperation.TEST,
        runner=lambda argv, timeout: 0,
    )
    assert admission.admission is CodingWorkerAdmissionState.BLOCKED
    assert result.reason is CodingIsolatedLoopReason.WORKER_NOT_ADMITTED
    assert result.untrusted_execute is False
