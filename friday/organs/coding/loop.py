"""Isolated build/test of an already-extracted Coding workspace.

Build compiles admitted Python without executing it. Test runs stdlib unittest
only after worker admission and a confirmed isolation probe. Execute/run of
uploaded programs stay fail-closed. This is not a safety certification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from friday.orchestration.coding_mode_execute_claim import CodingModeExecuteOperation
from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
)
from friday.organs.coding.extract import (
    CodingArchiveExtractObserveState,
    CodingArchiveExtractObserveV1,
)
from friday.organs.coding.worker_boundary import (
    CodingWorkerBoundaryV1,
    coding_worker_hazard_paths,
)
from friday.organs.coding.worker_spawn import (
    BWRAP_EXECUTABLE,
    CodingWorkerRunner,
    CodingWorkerSpawnV1,
    coding_worker_bwrap_argv,
    default_coding_worker_runner,
)

MAX_LOOP_PY_FILES = 4096

_BUILD = (
    "import pathlib,py_compile,sys\n"
    "root=pathlib.Path(sys.argv[1])\n"
    "if not root.is_dir():\n"
    "    raise SystemExit(3)\n"
    "root=root.resolve()\n"
    "files=[]\n"
    "for path in root.rglob('*.py'):\n"
    "    if not path.is_file():\n"
    "        continue\n"
    "    resolved=path.resolve()\n"
    "    try:\n"
    "        resolved.relative_to(root)\n"
    "    except ValueError:\n"
    "        raise SystemExit(4)\n"
    "    files.append(path)\n"
    f"    if len(files)>{MAX_LOOP_PY_FILES}:\n"
    "        raise SystemExit(5)\n"
    "if not files:\n"
    "    raise SystemExit(2)\n"
    "for path in files:\n"
    "    py_compile.compile(str(path), doraise=True)\n"
    "raise SystemExit(0)\n"
)

_TEST = (
    "import sys,unittest\n"
    "root=sys.argv[1]\n"
    "loader=unittest.defaultTestLoader\n"
    "suite=loader.discover(root, pattern='test*.py', top_level_dir=root)\n"
    "count=suite.countTestCases()\n"
    "if count==0:\n"
    "    raise SystemExit(2)\n"
    "result=unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)\n"
    "raise SystemExit(0 if result.wasSuccessful() else 1)\n"
)


class CodingIsolatedLoopState(StrEnum):
    EMPTY = "empty"
    BUILT = "built"
    TESTED = "tested"
    BLOCKED = "blocked"


class CodingIsolatedLoopReason(StrEnum):
    NO_WORKSPACE = "no_workspace"
    NO_CLAIM = "no_claim"
    EXECUTE_FORBIDDEN = "execute_forbidden"
    WORKER_NOT_ADMITTED = "worker_not_admitted"
    PROBE_NOT_CONFIRMED = "probe_not_confirmed"
    OPERATION_INVALID = "operation_invalid"
    BUILD_EMPTY = "build_empty"
    BUILD_FAILED = "build_failed"
    BUILD_OK = "build_ok"
    NO_TESTS = "no_tests"
    TEST_FAILED = "test_failed"
    TEST_OK = "test_ok"
    SPAWN_FAILED = "spawn_failed"


@dataclass(frozen=True, slots=True)
class CodingIsolatedLoopV1:
    """Closed isolated-loop observation. Execute/run of uploads is never attempted."""

    state: CodingIsolatedLoopState
    reason: CodingIsolatedLoopReason
    untrusted_execute: bool = False


def _empty(reason: CodingIsolatedLoopReason = CodingIsolatedLoopReason.NO_WORKSPACE) -> CodingIsolatedLoopV1:
    return CodingIsolatedLoopV1(CodingIsolatedLoopState.EMPTY, reason, False)


def _blocked(reason: CodingIsolatedLoopReason) -> CodingIsolatedLoopV1:
    return CodingIsolatedLoopV1(CodingIsolatedLoopState.BLOCKED, reason, False)


def observe_coding_isolated_loop(
    *,
    admission: CodingWorkerAdmissionV1,
    boundary: CodingWorkerBoundaryV1,
    spawn: CodingWorkerSpawnV1,
    extract: CodingArchiveExtractObserveV1,
    operation: CodingModeExecuteOperation,
    runner: CodingWorkerRunner | None = None,
    created: object | None = None,
) -> CodingIsolatedLoopV1:
    """Compile or unittest extracted files after admission. Never run uploaded programs."""

    created_ready = getattr(getattr(created, "state", None), "value", None) == "written"
    if extract.state is not CodingArchiveExtractObserveState.EXTRACTED and not created_ready:
        return _empty()
    if operation in {CodingModeExecuteOperation.INSPECT, CodingModeExecuteOperation.STATIC}:
        return _empty(CodingIsolatedLoopReason.NO_CLAIM)
    if operation in {CodingModeExecuteOperation.EXECUTE, CodingModeExecuteOperation.RUN}:
        return _blocked(CodingIsolatedLoopReason.EXECUTE_FORBIDDEN)
    if operation not in {CodingModeExecuteOperation.BUILD, CodingModeExecuteOperation.TEST}:
        return _blocked(CodingIsolatedLoopReason.OPERATION_INVALID)
    if admission.admission is not CodingWorkerAdmissionState.ADMITTED:
        return _blocked(CodingIsolatedLoopReason.WORKER_NOT_ADMITTED)
    if spawn.probe != "confirmed":
        return _blocked(CodingIsolatedLoopReason.PROBE_NOT_CONFIRMED)
    workspace = admission.workspace
    limits = admission.limits
    if (
        workspace is None
        or limits is None
        or limits.wall_clock_sec is None
        or workspace.project_root is None
        or workspace.workspace_path is None
        or workspace.export_path is None
    ):
        return _blocked(CodingIsolatedLoopReason.WORKER_NOT_ADMITTED)
    host_workspace = Path(workspace.project_root) / workspace.workspace_path
    if not host_workspace.is_dir():
        return _blocked(CodingIsolatedLoopReason.NO_WORKSPACE)
    python_c = _BUILD if operation is CodingModeExecuteOperation.BUILD else _TEST
    argv = coding_worker_bwrap_argv(
        worker_root=workspace.project_root,
        workspace_path=workspace.workspace_path,
        export_path=workspace.export_path,
        hazards=coding_worker_hazard_paths(boundary),
        uid=os.geteuid(),
        gid=os.getegid(),
        python_c=python_c,
        python_args=(workspace.workspace_path,),
    )
    if not argv or argv[0] != BWRAP_EXECUTABLE or python_c not in argv:
        return _blocked(CodingIsolatedLoopReason.SPAWN_FAILED)
    execute = runner or default_coding_worker_runner
    code = execute(argv, limits.wall_clock_sec)
    if operation is CodingModeExecuteOperation.BUILD:
        if code == 0:
            return CodingIsolatedLoopV1(
                CodingIsolatedLoopState.BUILT,
                CodingIsolatedLoopReason.BUILD_OK,
                False,
            )
        if code == 2:
            return _blocked(CodingIsolatedLoopReason.BUILD_EMPTY)
        return _blocked(CodingIsolatedLoopReason.BUILD_FAILED)
    if code == 0:
        return CodingIsolatedLoopV1(
            CodingIsolatedLoopState.TESTED,
            CodingIsolatedLoopReason.TEST_OK,
            True,
        )
    if code == 2:
        return _blocked(CodingIsolatedLoopReason.NO_TESTS)
    return CodingIsolatedLoopV1(
        CodingIsolatedLoopState.BLOCKED,
        CodingIsolatedLoopReason.TEST_FAILED,
        True,
    )
