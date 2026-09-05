"""Admit, then spawn, an isolated Coding worker.  Never execute uploads.

Spawn is gated on ``build_coding_worker_admission``.  The default runner uses a
Coding-specific bubblewrap profile (not Engineer sandbox, not Docker) and only
runs a closed isolation probe.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
    build_coding_worker_admission,
)
from friday.orchestration.coding_worker_limits import MAX_CPU_SEC, MAX_MEMORY_BYTES, MAX_WALL_CLOCK_SEC
from friday.organs.coding.worker_boundary import (
    CodingWorkerBoundaryV1,
    coding_worker_hazard_paths,
    observe_coding_worker_isolation,
)
from friday.private_fs import ensure_private_directory

BWRAP_EXECUTABLE = "/usr/bin/bwrap"
PYTHON_EXECUTABLE = "/usr/bin/python3"
CODING_WORKER_MOUNT = "/work"
DEFAULT_WALL_CLOCK_SEC = 60
DEFAULT_MEMORY_BYTES = 64 * 1024 * 1024
DEFAULT_CPU_SEC = 30

_PROBE = (
    "import os,sys;"
    "work,export,*hazards=sys.argv[1:];"
    "sys.exit(3 if not (os.path.isdir(work) and os.path.isdir(export)) else "
    "1 if any(os.path.exists(path) for path in hazards) else 0)"
)

CodingWorkerRunner = Callable[[tuple[str, ...], int], int]


@dataclass(frozen=True, slots=True)
class CodingWorkerSpawnV1:
    """Closed spawn outcome.  Untrusted execute is never attempted here."""

    spawned: bool
    admission: CodingWorkerAdmissionState
    probe: str
    untrusted_execute: bool = False


def compose_coding_worker_admission(
    *,
    admission_id: str,
    authenticated_turn_id: str,
    worker_id: str,
    operation_id: str,
    project_id: str,
    revision_selector: str,
    boundary: CodingWorkerBoundaryV1,
    wall_clock_sec: int = DEFAULT_WALL_CLOCK_SEC,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    cpu_sec: int = DEFAULT_CPU_SEC,
) -> CodingWorkerAdmissionV1:
    """Compose the five landed contracts from one planned boundary."""

    isolation = observe_coding_worker_isolation(boundary)
    if (
        not boundary.network_disabled
        or boundary.host_network
        or wall_clock_sec > MAX_WALL_CLOCK_SEC
        or memory_bytes > MAX_MEMORY_BYTES
        or cpu_sec > MAX_CPU_SEC
    ):
        network: dict[str, object] = {"policy": "disabled", "host_network": True, "unbounded": False}
    else:
        network = {"policy": "disabled", "host_network": False, "unbounded": False}
    return build_coding_worker_admission(
        admission_id,
        authenticated_turn_id,
        identity={
            "worker_id": worker_id,
            "operation_id": operation_id,
            "project_id": project_id,
            "revision_selector": revision_selector,
        },
        isolation=isolation,
        network=network,
        workspace={
            "operation_id": operation_id,
            "project_root": boundary.worker_root,
            "workspace_path": boundary.workspace_path,
            "input_snapshot_sha256": revision_selector,
            "export_path": boundary.export_path,
            "workspace_count": 1,
        },
        limits={
            "wall_clock_sec": wall_clock_sec,
            "memory_bytes": memory_bytes,
            "cpu_sec": cpu_sec,
        },
    )


def coding_worker_bwrap_argv(
    *,
    worker_root: str,
    workspace_path: str,
    export_path: str,
    hazards: tuple[str, ...],
    uid: int,
    gid: int,
    python_c: str | None = None,
    python_args: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return the Coding-specific bwrap argv.  Hazards are probe args, not binds."""

    source = _PROBE if python_c is None else python_c
    args = (workspace_path, export_path, *hazards) if python_args is None else python_args
    return (
        BWRAP_EXECUTABLE,
        "--unshare-all",
        "--unshare-user",
        "--uid",
        str(uid),
        "--gid",
        str(gid),
        "--cap-drop",
        "ALL",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run",
        "--bind",
        worker_root,
        CODING_WORKER_MOUNT,
        "--chdir",
        CODING_WORKER_MOUNT,
        "--",
        PYTHON_EXECUTABLE,
        "-c",
        source,
        *args,
    )


def default_coding_worker_runner(argv: tuple[str, ...], timeout_sec: int) -> int:
    """Run the isolation probe.  Never executes uploaded project code."""

    if not argv or argv[0] != BWRAP_EXECUTABLE:
        return 126
    try:
        completed = subprocess.run(
            argv,
            check=False,
            timeout=max(1, timeout_sec),
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "HOME": CODING_WORKER_MOUNT, "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return 124
    return completed.returncode


def spawn_coding_worker(
    admission: CodingWorkerAdmissionV1,
    boundary: CodingWorkerBoundaryV1,
    *,
    runner: CodingWorkerRunner | None = None,
) -> CodingWorkerSpawnV1:
    """Spawn the isolation probe only after admission.  Never execute uploads."""

    if admission.admission is not CodingWorkerAdmissionState.ADMITTED:
        return CodingWorkerSpawnV1(False, admission.admission, "skipped", False)
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
        return CodingWorkerSpawnV1(False, admission.admission, "skipped", False)
    project_root = workspace.project_root
    workspace_path = workspace.workspace_path
    export_path = workspace.export_path
    timeout_sec = limits.wall_clock_sec
    try:
        ensure_private_directory(Path(project_root) / workspace_path)
        ensure_private_directory(Path(project_root) / export_path)
    except (OSError, ValueError):
        return CodingWorkerSpawnV1(False, admission.admission, "failed", False)
    argv = coding_worker_bwrap_argv(
        worker_root=project_root,
        workspace_path=workspace_path,
        export_path=export_path,
        hazards=coding_worker_hazard_paths(boundary),
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    execute = runner or default_coding_worker_runner
    code = execute(argv, timeout_sec)
    return CodingWorkerSpawnV1(True, admission.admission, "confirmed" if code == 0 else "failed", False)
