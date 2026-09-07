"""Isolated build/test of an already-extracted Coding workspace.

Build compiles admitted Python without executing it. Default TEST is admitted
only after live aggregate cgroup-v2 tree limits are proved. Trusted injected
runners support adapter tests, not production certification. Execute/run of
uploaded programs stay fail-closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from friday.orchestration.coding_mode_execute_claim import CodingModeExecuteOperation
from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
)
from friday.organs.coding import worker_cgroup as coding_tree
from friday.organs.coding.extract import (
    CodingArchiveExtractObserveState,
    CodingArchiveExtractObserveV1,
)
from friday.organs.coding.worker_boundary import (
    CodingWorkerBoundaryV1,
    coding_worker_hazard_paths,
)
from friday.organs.coding.worker_programs import BUILD as _BUILD
from friday.organs.coding.worker_programs import TEST as _TEST
from friday.organs.coding.worker_spawn import (
    BWRAP_EXECUTABLE,
    CodingWorkerRunner,
    CodingWorkerSpawnV1,
    coding_worker_bwrap_argv,
    coding_worker_scope,
    default_coding_worker_runner,
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
    RESOURCE_ENFORCEMENT_UNAVAILABLE = "resource_enforcement_unavailable"


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
    """Compile admitted files; refuse untrusted tests without an enforced runner."""

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
    if spawn.probe != "confirmed" or spawn._admission is not admission:
        return _blocked(CodingIsolatedLoopReason.PROBE_NOT_CONFIRMED)
    workspace = admission.workspace
    limits = admission.limits
    if (
        workspace is None
        or limits is None
        or limits.wall_clock_sec is None
        or limits.memory_bytes is None
        or limits.cpu_sec is None
        or workspace.project_root is None
        or workspace.workspace_path is None
        or workspace.export_path is None
    ):
        return _blocked(CodingIsolatedLoopReason.WORKER_NOT_ADMITTED)
    # A probe of another admission or replaced directory is not reusable.
    try:
        if spawn._scope != coding_worker_scope(admission, boundary):
            return _blocked(CodingIsolatedLoopReason.PROBE_NOT_CONFIRMED)
    except (OSError, ValueError):
        return _blocked(CodingIsolatedLoopReason.PROBE_NOT_CONFIRMED)
    if (
        operation is CodingModeExecuteOperation.TEST
        and runner in (None, default_coding_worker_runner)
        and not coding_tree.coding_tree_enforcement_available()
    ):
        # RLIMIT_AS/CPU apply per process. TEST needs a proved aggregate tree.
        return _blocked(CodingIsolatedLoopReason.RESOURCE_ENFORCEMENT_UNAVAILABLE)
    python_c = _BUILD if operation is CodingModeExecuteOperation.BUILD else _TEST
    test_tree = operation is CodingModeExecuteOperation.TEST
    argv = coding_worker_bwrap_argv(
        worker_root=workspace.project_root,
        workspace_path=workspace.workspace_path,
        export_path=workspace.export_path,
        hazards=coding_worker_hazard_paths(boundary),
        uid=os.geteuid(),
        gid=os.getegid(),
        memory_bytes=limits.memory_bytes,
        cpu_sec=limits.cpu_sec,
        python_c=python_c,
        python_args=(workspace.workspace_path,),
        workspace_writable=not test_tree,
        export_writable=not test_tree,
    )
    if not argv or argv[0] != BWRAP_EXECUTABLE or python_c not in argv:
        return _blocked(CodingIsolatedLoopReason.SPAWN_FAILED)
    execute = runner or default_coding_worker_runner
    try:
        code = execute(argv, limits.wall_clock_sec)
    except (OSError, ValueError, TimeoutError):
        code = None
    if type(code) is not int:
        return CodingIsolatedLoopV1(
            CodingIsolatedLoopState.BLOCKED,
            CodingIsolatedLoopReason.SPAWN_FAILED,
            operation is CodingModeExecuteOperation.TEST,
        )
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
        # Discovery may already have imported uploaded modules, even at zero
        # tests. Lack of test cases is not evidence that no code was executed.
        return CodingIsolatedLoopV1(CodingIsolatedLoopState.BLOCKED, CodingIsolatedLoopReason.NO_TESTS, True)
    return CodingIsolatedLoopV1(
        CodingIsolatedLoopState.BLOCKED,
        CodingIsolatedLoopReason.TEST_FAILED,
        True,
    )
