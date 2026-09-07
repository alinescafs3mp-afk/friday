"""Independent behavior verification for an admitted Coding create/edit.

Runs BUILD, then the Friday-owned ORACLE program inside the same proved
cgroup-v2 tree used for TEST.  User EXECUTE/RUN stay fail-closed.  Project
unittests may supplement this result and cannot replace it.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

from friday.orchestration.coding_mode_execute_claim import CodingModeExecuteOperation
from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
)
from friday.organs.coding import worker_cgroup as coding_tree
from friday.organs.coding.behavior_oracle import (
    CodingBehaviorOracleState,
    CodingBehaviorOracleV1,
)
from friday.organs.coding.create import prepare_coding_creation
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
from friday.organs.coding.worker_boundary import (
    CodingWorkerBoundaryV1,
    coding_worker_hazard_paths,
)
from friday.organs.coding.worker_cgroup import run_admitted_coding_tree_report
from friday.organs.coding.worker_programs import ORACLE
from friday.organs.coding.worker_spawn import (
    BWRAP_EXECUTABLE,
    MAX_WORKER_TASKS,
    CodingWorkerRunner,
    CodingWorkerSpawnV1,
    admitted_memory_bytes,
    coding_worker_bwrap_argv,
    compose_coding_worker_admission,
    default_coding_worker_runner,
    spawn_coding_worker,
)
from friday.organs.coding.workspace_io import publish_members

MAX_ORACLE_STDERR = 512
MAX_CREATION_REPAIRS = 2


class CodingBehaviorVerificationState(StrEnum):
    EMPTY = "empty"
    VERIFIED = "verified"
    BLOCKED = "blocked"


class CodingBehaviorVerificationReason(StrEnum):
    NO_ORACLE = "no_oracle"
    NO_WORKSPACE = "no_workspace"
    WORKER_NOT_ADMITTED = "worker_not_admitted"
    PROBE_NOT_CONFIRMED = "probe_not_confirmed"
    RESOURCE_ENFORCEMENT_UNAVAILABLE = "resource_enforcement_unavailable"
    BUILD_FAILED = "build_failed"
    ORACLE_FAILED = "oracle_failed"
    ORACLE_OK = "oracle_ok"
    SPAWN_FAILED = "spawn_failed"
    PAYLOAD_INVALID = "payload_invalid"


@dataclass(frozen=True, slots=True)
class CodingBehaviorVerificationV1:
    """Closed verification bound to one oracle identity and one source revision."""

    state: CodingBehaviorVerificationState
    reason: CodingBehaviorVerificationReason
    oracle_id: str | None = None
    oracle_sha256: str | None = None
    revision_sha256: str | None = None
    failed_case: str | None = None
    detail: str = ""
    untrusted_execute: bool = False


def _empty(
    reason: CodingBehaviorVerificationReason = CodingBehaviorVerificationReason.NO_ORACLE,
) -> CodingBehaviorVerificationV1:
    return CodingBehaviorVerificationV1(CodingBehaviorVerificationState.EMPTY, reason)


def _blocked(
    reason: CodingBehaviorVerificationReason,
    *,
    oracle: CodingBehaviorOracleV1 | None = None,
    revision_sha256: str | None = None,
    failed_case: str | None = None,
    detail: str = "",
    untrusted_execute: bool = False,
) -> CodingBehaviorVerificationV1:
    return CodingBehaviorVerificationV1(
        CodingBehaviorVerificationState.BLOCKED,
        reason,
        None if oracle is None else oracle.oracle_id,
        None if oracle is None else oracle.oracle_sha256,
        revision_sha256,
        failed_case,
        detail,
        untrusted_execute,
    )


def _detail(raw: bytes) -> str:
    return raw[:MAX_ORACLE_STDERR].decode("utf-8", "replace").replace("\x00", "")


def _failed_case(raw: bytes) -> str | None:
    line = raw.split(b"\n", 1)[0].decode("ascii", "replace").strip()
    if not line or len(line) > 64:
        return None
    return line


def observe_coding_behavior_verification(
    *,
    admission: CodingWorkerAdmissionV1,
    boundary: CodingWorkerBoundaryV1,
    spawn: CodingWorkerSpawnV1,
    oracle: CodingBehaviorOracleV1,
    workspace: Path,
    runner: CodingWorkerRunner | None = None,
    revision_sha256: str | None = None,
) -> CodingBehaviorVerificationV1:
    """Compile, then run the frozen oracle inside the proved aggregate tree."""

    if oracle.state is not CodingBehaviorOracleState.ADMITTED:
        return _empty()
    if admission.admission is not CodingWorkerAdmissionState.ADMITTED:
        return _blocked(CodingBehaviorVerificationReason.WORKER_NOT_ADMITTED, oracle=oracle)
    if spawn.probe != "confirmed":
        return _blocked(CodingBehaviorVerificationReason.PROBE_NOT_CONFIRMED, oracle=oracle)
    try:
        root = Path(workspace)
        if not root.is_dir() or not (root / "main.py").is_file():
            return _blocked(CodingBehaviorVerificationReason.NO_WORKSPACE, oracle=oracle)
    except (OSError, TypeError, ValueError):
        return _blocked(CodingBehaviorVerificationReason.NO_WORKSPACE, oracle=oracle)
    limits = admission.limits
    workspace_state = admission.workspace
    if (
        limits is None
        or workspace_state is None
        or limits.wall_clock_sec is None
        or limits.memory_bytes is None
        or limits.cpu_sec is None
        or workspace_state.project_root is None
        or workspace_state.workspace_path is None
        or workspace_state.export_path is None
    ):
        return _blocked(CodingBehaviorVerificationReason.WORKER_NOT_ADMITTED, oracle=oracle)
    built = observe_coding_isolated_loop(
        admission=admission,
        boundary=boundary,
        spawn=spawn,
        extract=CodingArchiveExtractObserveV1(
            CodingArchiveExtractObserveState.EMPTY,
            CodingArchiveExtractObserveReason.NO_ARCHIVE,
            0,
            False,
        ),
        operation=CodingModeExecuteOperation.BUILD,
        runner=runner,
        created=SimpleNamespace(state=SimpleNamespace(value="written")),
    )
    if built.state is not CodingIsolatedLoopState.BUILT:
        reason = (
            CodingBehaviorVerificationReason.SPAWN_FAILED
            if built.reason is CodingIsolatedLoopReason.SPAWN_FAILED
            else CodingBehaviorVerificationReason.BUILD_FAILED
        )
        return _blocked(reason, oracle=oracle, revision_sha256=revision_sha256)
    if runner in (None, default_coding_worker_runner) and not coding_tree.coding_tree_enforcement_available():
        return _blocked(
            CodingBehaviorVerificationReason.RESOURCE_ENFORCEMENT_UNAVAILABLE,
            oracle=oracle,
            revision_sha256=revision_sha256,
        )
    argv = coding_worker_bwrap_argv(
        worker_root=workspace_state.project_root,
        workspace_path=workspace_state.workspace_path,
        export_path=workspace_state.export_path,
        hazards=coding_worker_hazard_paths(boundary),
        uid=os.geteuid(),
        gid=os.getegid(),
        memory_bytes=limits.memory_bytes,
        cpu_sec=limits.cpu_sec,
        python_c=ORACLE,
        python_args=(workspace_state.workspace_path,),
        workspace_writable=False,
        export_writable=False,
    )
    if not argv or argv[0] != BWRAP_EXECUTABLE or ORACLE not in argv:
        return _blocked(
            CodingBehaviorVerificationReason.SPAWN_FAILED,
            oracle=oracle,
            revision_sha256=revision_sha256,
        )
    try:
        if runner not in (None, default_coding_worker_runner):
            code = runner(argv, limits.wall_clock_sec)
            stderr = b""
        else:
            memory_bytes = admitted_memory_bytes(argv)
            if memory_bytes is None:
                return _blocked(
                    CodingBehaviorVerificationReason.SPAWN_FAILED,
                    oracle=oracle,
                    revision_sha256=revision_sha256,
                )
            report = run_admitted_coding_tree_report(
                argv,
                limits.wall_clock_sec,
                memory_bytes=memory_bytes,
                tasks_max=MAX_WORKER_TASKS,
            )
            code = report.code
            stderr = report.stderr
    except (OSError, ValueError, TimeoutError):
        return _blocked(
            CodingBehaviorVerificationReason.SPAWN_FAILED,
            oracle=oracle,
            revision_sha256=revision_sha256,
        )
    if type(code) is not int:
        return _blocked(
            CodingBehaviorVerificationReason.SPAWN_FAILED,
            oracle=oracle,
            revision_sha256=revision_sha256,
        )
    if code == 0:
        return CodingBehaviorVerificationV1(
            CodingBehaviorVerificationState.VERIFIED,
            CodingBehaviorVerificationReason.ORACLE_OK,
            oracle.oracle_id,
            oracle.oracle_sha256,
            revision_sha256,
            None,
            "",
            True,
        )
    return _blocked(
        CodingBehaviorVerificationReason.ORACLE_FAILED,
        oracle=oracle,
        revision_sha256=revision_sha256,
        failed_case=_failed_case(stderr),
        detail=_detail(stderr),
        untrusted_execute=True,
    )


def verify_creation_payload(
    *,
    payload: str,
    oracle: CodingBehaviorOracleV1,
    worker_boundary: CodingWorkerBoundaryV1,
    runner: CodingWorkerRunner | None = None,
    revision_sha256: str | None = None,
) -> CodingBehaviorVerificationV1:
    """Write a disposable copy and verify it.  Never persists a Coding turn."""

    if oracle.state is not CodingBehaviorOracleState.ADMITTED:
        return _empty()
    try:
        source = prepare_coding_creation(payload)
    except (ValueError, TypeError, RecursionError):
        return _blocked(CodingBehaviorVerificationReason.PAYLOAD_INVALID, oracle=oracle)
    digest = revision_sha256 or source.revision_sha256
    operation = "oracle-" + secrets.token_hex(8)
    boundary = replace(
        worker_boundary,
        workspace_path="work/" + operation,
        export_path="out/" + operation,
    )
    admission = compose_coding_worker_admission(
        admission_id="coding-oracle-adm-" + secrets.token_hex(8),
        authenticated_turn_id="coding-oracle-turn-" + secrets.token_hex(8),
        worker_id="coding-oracle-w-" + secrets.token_hex(8),
        operation_id=operation,
        project_id="coding-oracle",
        revision_selector=digest,
        boundary=boundary,
    )
    workspace = Path(boundary.worker_root) / boundary.workspace_path
    export = Path(boundary.worker_root) / boundary.export_path
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        export.mkdir(parents=True, exist_ok=True)
        publish_members(workspace, list(source.members))
        spawn = spawn_coding_worker(admission, boundary, runner=runner)
        return observe_coding_behavior_verification(
            admission=admission,
            boundary=boundary,
            spawn=spawn,
            oracle=oracle,
            workspace=workspace,
            runner=runner,
            revision_sha256=digest,
        )
    except (OSError, ValueError, TypeError, RecursionError):
        return _blocked(
            CodingBehaviorVerificationReason.SPAWN_FAILED,
            oracle=oracle,
            revision_sha256=digest,
        )
    finally:
        root = Path(boundary.worker_root).resolve()
        for path in (workspace, export):
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            shutil.rmtree(resolved, ignore_errors=True)


def repair_diagnostics(verification: CodingBehaviorVerificationV1) -> dict[str, object]:
    """Bounded, source-free repair facts.  The oracle itself is never rewritten."""

    payload: dict[str, object] = {
        "oracle_id": verification.oracle_id,
        "reason": verification.reason.value,
    }
    if verification.failed_case is not None:
        payload["failed_case"] = verification.failed_case
    if verification.detail:
        payload["detail"] = verification.detail[:MAX_ORACLE_STDERR]
    return payload
