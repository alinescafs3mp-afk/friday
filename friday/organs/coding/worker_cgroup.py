"""Proven aggregate cgroup-v2 tree limits for Coding TEST.

This module does not import Engineer and does not execute uploaded programs
itself.  It creates a user-systemd transient unit, reads memory.max,
memory.swap.max, pids.max and cpu.max back from cgroupfs, then joins the
bubblewrap supervisor into that unit before wait().  prlimit on one ancestor
is not this boundary.
"""

from __future__ import annotations

import os
import secrets
import signal
import stat
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from friday.orchestration.coding_worker_limits import MAX_MEMORY_BYTES, MAX_WALL_CLOCK_SEC

SYSTEMD_RUN_EXECUTABLE = "/usr/bin/systemd-run"
SYSTEMCTL_EXECUTABLE = "/usr/bin/systemctl"
SLEEP_EXECUTABLE = "/usr/bin/sleep"
CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_TASKS_MAX = 32
DEFAULT_CPU_QUOTA_PERCENT = 100
DEFAULT_TMPFS_BYTES = 16 * 1024 * 1024
MAX_TASKS_MAX = 256
MAX_CPU_QUOTA_PERCENT = 400
_CPU_PERIOD_USEC = 100_000
_PROBE_MEMORY_BYTES = 32 * 1024 * 1024
_PROBE_TASKS = 8
_PROBE_CPU_QUOTA_PERCENT = 50
_PROBE_RUNTIME_SEC = 8
_LOCK = threading.Lock()
_AVAILABLE: bool | None = None


class CodingTreeEnforcementError(ValueError):
    """Aggregate tree limits could not be installed or proved."""


@dataclass(frozen=True, slots=True)
class CodingTreeLimitsV1:
    """Closed aggregate limits that must appear on the live cgroup."""

    memory_bytes: int
    tasks_max: int
    cpu_quota_percent: int
    runtime_sec: int
    swap_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _ProvenTree:
    unit: str
    cgroup: Path
    limits: CodingTreeLimitsV1


def _trusted_root_executable(path: str) -> bool:
    try:
        details = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return bool(
        os.path.isabs(path)
        and stat.S_ISREG(details.st_mode)
        and details.st_uid == 0
        and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and os.access(path, os.X_OK)
    )


def _holder_executable() -> bool:
    try:
        details = os.stat(SLEEP_EXECUTABLE, follow_symlinks=True)
    except OSError:
        return False
    return bool(
        stat.S_ISREG(details.st_mode)
        and details.st_uid == 0
        and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and os.access(SLEEP_EXECUTABLE, os.X_OK)
    )


def _user_env() -> dict[str, str]:
    runtime_dir = f"/run/user/{os.geteuid()}"
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "XDG_RUNTIME_DIR": runtime_dir,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
    }


def _cpu_max_value(percent: int) -> str:
    quota = int(_CPU_PERIOD_USEC * int(percent) / 100)
    return f"{quota} {_CPU_PERIOD_USEC}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _validate_limits(limits: CodingTreeLimitsV1) -> None:
    if type(limits.memory_bytes) is not int or not 1 <= limits.memory_bytes <= MAX_MEMORY_BYTES:
        raise CodingTreeEnforcementError("memory")
    if type(limits.tasks_max) is not int or not 2 <= limits.tasks_max <= MAX_TASKS_MAX:
        raise CodingTreeEnforcementError("tasks")
    if (
        type(limits.cpu_quota_percent) is not int
        or not 1 <= limits.cpu_quota_percent <= MAX_CPU_QUOTA_PERCENT
    ):
        raise CodingTreeEnforcementError("cpu")
    if type(limits.runtime_sec) is not int or not 1 <= limits.runtime_sec <= MAX_WALL_CLOCK_SEC:
        raise CodingTreeEnforcementError("runtime")
    if type(limits.swap_bytes) is not int or limits.swap_bytes != 0:
        raise CodingTreeEnforcementError("swap")


def reset_coding_tree_enforcement_cache() -> None:
    """Drop the process-local availability cache. Tests use this; production does not."""

    global _AVAILABLE
    with _LOCK:
        _AVAILABLE = None


def coding_tree_enforcement_available() -> bool:
    """Return whether this host can prove a finite Coding TEST cgroup."""

    global _AVAILABLE
    with _LOCK:
        if _AVAILABLE is not None:
            return _AVAILABLE
        _AVAILABLE = _probe_tree_enforcement()
        return _AVAILABLE


def _probe_tree_enforcement() -> bool:
    if not _trusted_root_executable(SYSTEMD_RUN_EXECUTABLE) or not _trusted_root_executable(
        SYSTEMCTL_EXECUTABLE
    ):
        return False
    if not _holder_executable():
        return False
    limits = CodingTreeLimitsV1(
        memory_bytes=_PROBE_MEMORY_BYTES,
        tasks_max=_PROBE_TASKS,
        cpu_quota_percent=_PROBE_CPU_QUOTA_PERCENT,
        runtime_sec=_PROBE_RUNTIME_SEC,
    )
    try:
        tree = _allocate(limits)
    except (CodingTreeEnforcementError, OSError, subprocess.TimeoutExpired):
        return False
    try:
        _prove_limits(tree.cgroup, limits)
        return True
    except CodingTreeEnforcementError:
        return False
    finally:
        _stop_unit(tree.unit)


def _allocate(limits: CodingTreeLimitsV1) -> _ProvenTree:
    _validate_limits(limits)
    unit = f"friday-cw-{secrets.token_hex(8)}.service"
    argv = (
        SYSTEMD_RUN_EXECUTABLE,
        "--user",
        "--no-block",
        "--collect",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={limits.memory_bytes}",
        "-p",
        f"MemorySwapMax={limits.swap_bytes}",
        "-p",
        f"TasksMax={limits.tasks_max}",
        "-p",
        f"CPUQuota={limits.cpu_quota_percent}%",
        "-p",
        f"RuntimeMaxSec={limits.runtime_sec}",
        "-p",
        "KillMode=control-group",
        "-p",
        "Delegate=yes",
        "--",
        SLEEP_EXECUTABLE,
        "infinity",
    )
    try:
        completed = subprocess.run(  # noqa: S603 - closed argv, root-owned helpers
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            env=_user_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _stop_unit(unit)
        raise CodingTreeEnforcementError("allocate") from exc
    if completed.returncode not in {0, None}:
        _stop_unit(unit)
        raise CodingTreeEnforcementError("allocate")
    try:
        cgroup = _wait_unit_cgroup(unit)
        _prove_limits(cgroup, limits)
    except CodingTreeEnforcementError:
        _stop_unit(unit)
        raise
    return _ProvenTree(unit=unit, cgroup=cgroup, limits=limits)


def _wait_unit_cgroup(unit: str) -> Path:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            shown = subprocess.run(  # noqa: S603 - closed argv, root-owned helper
                (
                    SYSTEMCTL_EXECUTABLE,
                    "--user",
                    "show",
                    unit,
                    "-p",
                    "ControlGroup",
                    "-p",
                    "ActiveState",
                ),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                text=True,
                env=_user_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodingTreeEnforcementError("unit") from exc
        control = ""
        active = ""
        for line in (shown.stdout or "").splitlines():
            if line.startswith("ControlGroup="):
                control = line.split("=", 1)[1]
            elif line.startswith("ActiveState="):
                active = line.split("=", 1)[1]
        if active in {"active", "activating"} and control.startswith("/"):
            path = CGROUP_ROOT / control.lstrip("/")
            if path.is_dir() and (path / "memory.max").exists() and (path / "cgroup.kill").exists():
                return path
        time.sleep(0.05)
    raise CodingTreeEnforcementError("unit")


def _prove_limits(cgroup: Path, limits: CodingTreeLimitsV1) -> None:
    memory = _read_text(cgroup / "memory.max")
    swap = _read_text(cgroup / "memory.swap.max")
    pids = _read_text(cgroup / "pids.max")
    cpu = _read_text(cgroup / "cpu.max")
    if memory != str(int(limits.memory_bytes)):
        raise CodingTreeEnforcementError("memory")
    if swap != str(int(limits.swap_bytes)):
        raise CodingTreeEnforcementError("swap")
    if pids != str(int(limits.tasks_max)):
        raise CodingTreeEnforcementError("tasks")
    if cpu != _cpu_max_value(limits.cpu_quota_percent):
        raise CodingTreeEnforcementError("cpu")
    if not (cgroup / "cgroup.kill").exists():
        raise CodingTreeEnforcementError("kill")


def _pid_in_tree(pid: int, cgroup: Path) -> bool:
    try:
        raw = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="ascii")
    except OSError:
        return False
    relative = ""
    for line in raw.splitlines():
        if line.startswith("0::"):
            relative = line[3:]
            break
    if not relative.startswith("/"):
        return False
    actual = CGROUP_ROOT / relative.lstrip("/")
    expected = cgroup.resolve()
    try:
        resolved = actual.resolve()
    except OSError:
        return False
    return resolved == expected or expected in resolved.parents


def _join_cgroup(cgroup: Path) -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(cgroup / "cgroup.procs"), flags)
    try:
        os.write(fd, b"0\n")
    finally:
        os.close(fd)


def _kill_tree(cgroup: Path) -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(cgroup / "cgroup.kill"), flags)
    except OSError:
        return
    try:
        os.write(fd, b"1\n")
    except OSError:
        return
    finally:
        os.close(fd)


def _stop_unit(unit: str) -> None:
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603 - closed argv, root-owned helper
            (SYSTEMCTL_EXECUTABLE, "--user", "stop", unit),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=_user_env(),
        )
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603 - closed argv, root-owned helper
            (SYSTEMCTL_EXECUTABLE, "--user", "reset-failed", unit),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=_user_env(),
        )


class CodingTreeCommandV1(NamedTuple):
    code: int
    stderr: bytes


def run_admitted_coding_tree(
    argv: tuple[str, ...],
    timeout_sec: int,
    *,
    memory_bytes: int,
    tasks_max: int = DEFAULT_TASKS_MAX,
    cpu_quota_percent: int = DEFAULT_CPU_QUOTA_PERCENT,
) -> int:
    """Run an already-admitted bubblewrap argv inside a proved aggregate cgroup."""

    return run_admitted_coding_tree_report(
        argv,
        timeout_sec,
        memory_bytes=memory_bytes,
        tasks_max=tasks_max,
        cpu_quota_percent=cpu_quota_percent,
        stderr_limit=0,
    ).code


def run_admitted_coding_tree_report(
    argv: tuple[str, ...],
    timeout_sec: int,
    *,
    memory_bytes: int,
    tasks_max: int = DEFAULT_TASKS_MAX,
    cpu_quota_percent: int = DEFAULT_CPU_QUOTA_PERCENT,
    stderr_limit: int = 2048,
) -> CodingTreeCommandV1:
    """Same tree as TEST; optionally keep a bounded stderr slice for repair."""

    empty = CodingTreeCommandV1(126, b"")
    if (
        not argv
        or type(timeout_sec) is not int
        or not 1 <= timeout_sec <= MAX_WALL_CLOCK_SEC
        or type(stderr_limit) is not int
        or stderr_limit < 0
        or stderr_limit > 8192
    ):
        return empty
    runtime = min(MAX_WALL_CLOCK_SEC, timeout_sec + 5)
    limits = CodingTreeLimitsV1(
        memory_bytes=memory_bytes,
        tasks_max=tasks_max,
        cpu_quota_percent=cpu_quota_percent,
        runtime_sec=runtime,
    )
    try:
        tree = _allocate(limits)
    except (CodingTreeEnforcementError, OSError, subprocess.TimeoutExpired):
        return empty
    process: subprocess.Popen[bytes] | None = None
    stderr = b""
    try:

        def _preexec() -> None:
            _join_cgroup(tree.cgroup)

        capture = stderr_limit > 0
        process = subprocess.Popen(  # noqa: S603 - caller-admitted closed argv
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/work", "LANG": "C"},
            preexec_fn=_preexec,
        )
        if type(process.pid) is not int or process.pid <= 1 or not _pid_in_tree(process.pid, tree.cgroup):
            raise CodingTreeEnforcementError("join")
        _prove_limits(tree.cgroup, limits)
        try:
            if capture:
                captured = process.communicate(timeout=timeout_sec)[1] or b""
                stderr = captured[:stderr_limit]
                code = process.returncode
                if type(code) is not int:
                    return CodingTreeCommandV1(126, stderr)
                return CodingTreeCommandV1(code, stderr)
            return CodingTreeCommandV1(process.wait(timeout=timeout_sec), b"")
        except subprocess.TimeoutExpired:
            return CodingTreeCommandV1(124, stderr)
    except (CodingTreeEnforcementError, OSError, ValueError):
        return empty
    finally:
        if process is not None and process.poll() is None:
            _kill_tree(tree.cgroup)
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(OSError, subprocess.TimeoutExpired):
                if process.stderr is not None:
                    process.communicate(timeout=5)
                else:
                    process.wait(timeout=5)
        _stop_unit(tree.unit)
