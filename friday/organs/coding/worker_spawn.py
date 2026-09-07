"""Admit and bound code-owned Coding probes, compilation, and proved TEST trees.

Only the current operation's workspace/export are mounted. Uploaded unittest and independent ORACLE
execution are admitted only after a live cgroup-v2 tree (memory, swap, pids, CPU
quota) is installed and read back. Execute/run of uploaded programs stay outside
this runner.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
    build_coding_worker_admission,
)
from friday.orchestration.coding_worker_limits import MAX_CPU_SEC, MAX_MEMORY_BYTES, MAX_WALL_CLOCK_SEC
from friday.organs.coding import worker_cgroup as coding_tree
from friday.organs.coding.worker_boundary import (
    CodingWorkerBoundaryV1,
    coding_worker_hazard_paths,
    observe_coding_worker_isolation,
)
from friday.organs.coding.worker_cgroup import DEFAULT_TASKS_MAX, DEFAULT_TMPFS_BYTES
from friday.organs.coding.worker_programs import BUILD, ORACLE, PROBE, TEST
from friday.private_fs import ensure_private_directory

BWRAP_EXECUTABLE = "/usr/bin/bwrap"
PYTHON_EXECUTABLE = "/usr/bin/python3"
PRLIMIT_EXECUTABLE = "/usr/bin/prlimit"
CODING_WORKER_MOUNT = "/work"
DEFAULT_WALL_CLOCK_SEC = 60
DEFAULT_MEMORY_BYTES = 64 * 1024 * 1024
DEFAULT_CPU_SEC = 30
MAX_WORKER_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKER_TASKS = DEFAULT_TASKS_MAX

CodingWorkerRunner = Callable[[tuple[str, ...], int], int]


@dataclass(frozen=True, slots=True)
class CodingWorkerSpawnV1:
    """Process-local probe outcome bound to one admission and directory pair."""

    spawned: bool
    admission: CodingWorkerAdmissionState
    probe: str
    untrusted_execute: bool = False
    _admission: CodingWorkerAdmissionV1 | None = field(default=None, repr=False, compare=False)
    _scope: tuple[int, ...] | None = field(default=None, repr=False, compare=False)


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
        or type(wall_clock_sec) is not int
        or not 1 <= wall_clock_sec <= MAX_WALL_CLOCK_SEC
        or type(memory_bytes) is not int
        or not 1 <= memory_bytes <= MAX_MEMORY_BYTES
        or type(cpu_sec) is not int
        or not 1 <= cpu_sec <= MAX_CPU_SEC
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


def _worker_bind_paths(worker_root: str, workspace_path: str, export_path: str) -> tuple[Path, Path]:
    root = PurePosixPath(worker_root)
    if not root.is_absolute() or str(root) != worker_root or worker_root == "/" or ".." in root.parts:
        raise ValueError("invalid Coding root")
    paths = []
    for value in (workspace_path, export_path):
        relative = PurePosixPath(value)
        if (
            not value
            or str(relative) != value
            or relative.is_absolute()
            or any(part in {".", ".."} for part in relative.parts)
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("invalid Coding operation path")
        path = Path(root / relative)
        # Check existing ancestors as well as the leaf, including dangling links.
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("Coding operation path traverses a symlink")
        paths.append(path)
    workspace, export = paths
    if workspace == export or workspace in export.parents or export in workspace.parents:
        raise ValueError("Coding workspace and export must be disjoint")
    return workspace, export


def coding_worker_scope(
    admission: CodingWorkerAdmissionV1, boundary: CodingWorkerBoundaryV1
) -> tuple[int, ...]:
    """Recheck the same admitted namespace and pin operation-directory identities."""

    workspace = admission.workspace
    if (
        admission.admission is not CodingWorkerAdmissionState.ADMITTED
        or workspace is None
        or workspace.project_root != boundary.worker_root
        or workspace.workspace_path != boundary.workspace_path
        or workspace.export_path != boundary.export_path
        or boundary.network_disabled is not True
        or boundary.host_network is not False
        or any(observe_coding_worker_isolation(boundary).values())
    ):
        raise ValueError("Coding boundary changed after admission")
    directories = _worker_bind_paths(boundary.worker_root, boundary.workspace_path, boundary.export_path)
    identities: list[int] = []
    for directory in directories:
        if not directory.is_dir():
            raise ValueError("Coding operation directory is unavailable")
        info = directory.stat(follow_symlinks=False)
        identities.extend((info.st_dev, info.st_ino))
    return tuple(identities)


def coding_worker_bwrap_argv(
    *,
    worker_root: str,
    workspace_path: str,
    export_path: str,
    hazards: tuple[str, ...],
    uid: int,
    gid: int,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    cpu_sec: int = DEFAULT_CPU_SEC,
    python_c: str | None = None,
    python_args: tuple[str, ...] | None = None,
    workspace_writable: bool = True,
    export_writable: bool = True,
    tmpfs_bytes: int = DEFAULT_TMPFS_BYTES,
) -> tuple[str, ...]:
    """Mount only this operation and apply hard rlimits before starting Python."""

    if type(memory_bytes) is not int or not 1 <= memory_bytes <= MAX_MEMORY_BYTES:
        raise ValueError("invalid Coding memory limit")
    if type(cpu_sec) is not int or not 1 <= cpu_sec <= MAX_CPU_SEC:
        raise ValueError("invalid Coding CPU limit")
    if type(tmpfs_bytes) is not int or not 1 <= tmpfs_bytes <= MAX_WORKER_FILE_BYTES:
        raise ValueError("invalid Coding tmpfs limit")
    workspace, export = _worker_bind_paths(worker_root, workspace_path, export_path)
    source = PROBE if python_c is None else python_c
    args = (workspace_path, export_path, *hazards) if python_args is None else python_args
    workspace_bind = "--bind" if workspace_writable else "--ro-bind"
    export_bind = "--bind" if export_writable else "--ro-bind"
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
        "--size",
        str(tmpfs_bytes),
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run",
        "--dir",
        CODING_WORKER_MOUNT,
        workspace_bind,
        str(workspace),
        str(PurePosixPath(CODING_WORKER_MOUNT) / workspace_path),
        export_bind,
        str(export),
        str(PurePosixPath(CODING_WORKER_MOUNT) / export_path),
        "--chdir",
        CODING_WORKER_MOUNT,
        "--",
        PRLIMIT_EXECUTABLE,
        f"--as={memory_bytes}:{memory_bytes}",
        f"--cpu={cpu_sec}:{cpu_sec}",
        "--nofile=128:128",
        f"--fsize={MAX_WORKER_FILE_BYTES}:{MAX_WORKER_FILE_BYTES}",
        "--core=0:0",
        "--",
        PYTHON_EXECUTABLE,
        "-I",
        "-c",
        source,
        *args,
    )


def admitted_memory_bytes(argv: tuple[str, ...]) -> int | None:
    for part in argv:
        if not part.startswith("--as="):
            continue
        payload = part[5:]
        if payload.count(":") != 1:
            return None
        soft, hard = payload.split(":", 1)
        if soft != hard or not soft.isdigit():
            return None
        value = int(soft)
        if not 1 <= value <= MAX_MEMORY_BYTES:
            return None
        return value
    return None


def default_coding_worker_runner(argv: tuple[str, ...], timeout_sec: int) -> int:
    """Run probe/compile directly; run TEST/ORACLE only inside a proved aggregate cgroup."""

    if (
        not argv
        or argv[0] != BWRAP_EXECUTABLE
        or type(timeout_sec) is not int
        or not 1 <= timeout_sec <= MAX_WALL_CLOCK_SEC
    ):
        return 126
    try:
        source_index = argv.index("-c") + 1
        source = argv[source_index]
        if (
            source not in (PROBE, BUILD, TEST, ORACLE)
            or argv[source_index - 3 : source_index] != (PYTHON_EXECUTABLE, "-I", "-c")
            or PRLIMIT_EXECUTABLE not in argv
        ):
            return 126
    except (ValueError, IndexError):
        return 126
    if source in (TEST, ORACLE):
        memory_bytes = admitted_memory_bytes(argv)
        if memory_bytes is None or not coding_tree.coding_tree_enforcement_available():
            return 126
        return coding_tree.run_admitted_coding_tree(
            argv,
            timeout_sec,
            memory_bytes=memory_bytes,
            tasks_max=MAX_WORKER_TASKS,
        )
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "HOME": CODING_WORKER_MOUNT, "LANG": "C"},
        )
    except (OSError, ValueError):
        return 126
    try:
        return process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return 124
    finally:
        # Killing the bwrap supervisor also kills the PID namespace through
        # --die-with-parent + --unshare-all. Reap even on interruption/error.
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def spawn_coding_worker(
    admission: CodingWorkerAdmissionV1,
    boundary: CodingWorkerBoundaryV1,
    *,
    runner: CodingWorkerRunner | None = None,
) -> CodingWorkerSpawnV1:
    """Spawn the closed probe only after admission and fresh path validation."""

    if admission.admission is not CodingWorkerAdmissionState.ADMITTED:
        return CodingWorkerSpawnV1(False, admission.admission, "skipped")
    workspace = admission.workspace
    limits = admission.limits
    if (
        workspace is None
        or limits is None
        or limits.wall_clock_sec is None
        or limits.memory_bytes is None
        or limits.cpu_sec is None
    ):
        return CodingWorkerSpawnV1(False, admission.admission, "skipped")
    try:
        # Do not create anything at a boundary that differs from the admission.
        if (
            workspace.project_root != boundary.worker_root
            or workspace.workspace_path != boundary.workspace_path
            or workspace.export_path != boundary.export_path
            or boundary.network_disabled is not True
            or boundary.host_network is not False
            or any(observe_coding_worker_isolation(boundary).values())
        ):
            raise ValueError("Coding boundary changed after admission")
        for path in _worker_bind_paths(boundary.worker_root, boundary.workspace_path, boundary.export_path):
            ensure_private_directory(path)
        scope = coding_worker_scope(admission, boundary)
        argv = coding_worker_bwrap_argv(
            worker_root=boundary.worker_root,
            workspace_path=boundary.workspace_path,
            export_path=boundary.export_path,
            hazards=coding_worker_hazard_paths(boundary),
            uid=os.geteuid(),
            gid=os.getegid(),
            memory_bytes=limits.memory_bytes,
            cpu_sec=limits.cpu_sec,
        )
        execute = runner or default_coding_worker_runner
        code = execute(argv, limits.wall_clock_sec)
        if type(code) is not int or scope != coding_worker_scope(admission, boundary):
            raise ValueError("Coding probe did not return the admitted scope")
    except (OSError, ValueError, TimeoutError):
        return CodingWorkerSpawnV1(False, admission.admission, "failed")
    return CodingWorkerSpawnV1(
        code != 126, admission.admission, "confirmed" if code == 0 else "failed", False, admission, scope
    )
