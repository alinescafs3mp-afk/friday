"""Verified systemd/cgroup resource boundary. Fail closed if limits cannot be proven."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .contracts import (
    SLEEP_EXECUTABLE,
    SYSTEMCTL_EXECUTABLE,
    SYSTEMD_RUN_EXECUTABLE,
    CommandError,
    ResourceLimits,
)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _read_text_at(dir_fd: int, name: str) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
        try:
            return os.read(fd, 8192).decode("ascii").strip()
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        return None


def _current_cgroup_dir() -> Path | None:
    try:
        raw = Path("/proc/self/cgroup").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    for line in raw.splitlines():
        if not line.startswith("0::"):
            continue
        relative = line[3:]
        if not relative.startswith("/") or ".." in relative.split("/"):
            return None
        candidate = Path("/sys/fs/cgroup") / relative.lstrip("/")
        return candidate if candidate.is_dir() else None
    return None


def _finite_parent_limit(name: str, *, allow_zero: bool = False) -> int | None:
    parent = _current_cgroup_dir()
    if parent is None:
        return None
    raw = _read_text(parent / name)
    if raw is None or raw == "max" or not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 or allow_zero and value == 0 else None


def _physical_memory_bytes() -> int:
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = pages * page_size
    except (OSError, TypeError, ValueError):
        total = 1024**3
    return max(256 * 1024**2, total)


def host_user_resource_limits() -> ResourceLimits:
    """Use all available CPUs and practical finite limits below the parent cgroup."""

    physical = _physical_memory_bytes()
    parent_memory = _finite_parent_limit("memory.max")
    memory_max = physical if parent_memory is None else min(parent_memory, physical)
    parent_swap = _finite_parent_limit("memory.swap.max", allow_zero=True)
    memory_swap_max = physical if parent_swap is None else min(parent_swap, physical)
    pid_max_raw = _read_text(Path("/proc/sys/kernel/pid_max"))
    pid_max = int(pid_max_raw) if pid_max_raw and pid_max_raw.isdigit() else 65_536
    practical_tasks = max(1024, min(pid_max, 65_536))
    parent_tasks = _finite_parent_limit("pids.max")
    tasks_max = practical_tasks if parent_tasks is None else min(parent_tasks, practical_tasks)
    try:
        cpu_count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpu_count = int(os.cpu_count() or 1)
    return ResourceLimits(
        tasks_max=max(1, tasks_max),
        memory_max=max(1, memory_max),
        memory_swap_max=max(0, memory_swap_max),
        cpu_quota_percent=100 * max(1, cpu_count),
    )


def _open_cgroup_dir(cgroup: Path) -> int:
    try:
        return os.open(
            str(cgroup),
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise CommandError("resource_boundary_unproven") from exc


class _ScopeCleanupOwner:
    """Process-lifetime owner for scopes whose first bounded cleanup failed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, ProvenScope] = {}
        self._worker: threading.Thread | None = None

    def retain(self, scope: ProvenScope) -> None:
        with self._lock:
            self._pending[id(scope)] = scope
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="friday-command-scope-cleanup",
                    daemon=True,
                )
                self._worker.start()

    def discard(self, scope: ProvenScope) -> None:
        with self._lock:
            self._pending.pop(id(scope), None)

    def _run(self) -> None:
        while True:
            with self._lock:
                pending = tuple(self._pending.values())
                if not pending:
                    self._worker = None
                    return
            for scope in pending:
                try:
                    proven = scope._kill_once()
                except Exception:
                    proven = False
                if proven:
                    self.discard(scope)
            time.sleep(0.25)


_SCOPE_CLEANUP_OWNER = _ScopeCleanupOwner()


@dataclass
class ProvenScope:
    job_id: str
    unit: str
    cgroup: Path
    limits: ResourceLimits
    cgroup_fd: int | None = None
    _tree_empty_proven: bool = False
    _cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def request_kill(self) -> bool:
        """Issue the cgroup kill without waiting for systemd collection.

        Process shutdown must signal every live scope before it starts joining
        reaper threads.  ``kill()`` also proves collection and can legitimately
        spend several seconds in ``systemctl``; doing that serially would make
        shutdown time grow with the number of jobs.  This method is the bounded
        first phase.  Each reaper still runs the full proof and durable receipt
        path after its process exits.
        """

        # A full cleanup which owns this lock always writes cgroup.kill before
        # doing its slower collection proof.  Consequently a failed try-lock
        # means the same kill is already being issued, while avoiding an fd-close
        # race with that proof path.
        if not self._cleanup_lock.acquire(blocking=False):
            return True
        try:
            held_fd = self.cgroup_fd
            if held_fd is None:
                return self._tree_empty_proven
            try:
                kill_fd = os.open(
                    "cgroup.kill",
                    os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=held_fd,
                )
                try:
                    os.write(kill_fd, b"1")
                finally:
                    os.close(kill_fd)
            except OSError:
                return False
            return True
        finally:
            self._cleanup_lock.release()

    def kill(self) -> bool:
        proven = self._kill_once()
        if proven:
            _SCOPE_CLEANUP_OWNER.discard(self)
        elif self.cgroup_fd is not None and self.unit == f"friday-ecmd-{self.job_id}.service":
            # Do not let callers dropping this object also drop the sole held
            # cgroup identity.  The daemon owner retries each bounded cleanup
            # pass for the lifetime of this process (restart reconciliation is
            # backed by the durable unit/cgroup row).
            _SCOPE_CLEANUP_OWNER.retain(self)
        return proven

    def _kill_once(self) -> bool:
        with self._cleanup_lock:
            return self._kill_once_locked()

    def _kill_once_locked(self) -> bool:
        if self.unit != f"friday-ecmd-{self.job_id}.service":
            if self.cgroup_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.cgroup_fd)
                self.cgroup_fd = None
            return False
        held_fd = self.cgroup_fd
        if held_fd is not None:
            try:
                kill_fd = os.open(
                    "cgroup.kill",
                    os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=held_fd,
                )
                try:
                    os.write(kill_fd, b"1")
                finally:
                    os.close(kill_fd)
            except OSError:
                pass
        empty = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            events = _read_text_at(held_fd, "cgroup.events") if held_fd is not None else None
            if any(line == "populated 0" for line in (events or "").splitlines()):
                empty = True
                break
            if not self.cgroup.is_dir():
                empty = True
                break
            time.sleep(0.05)
        collected = False
        for _attempt in range(2):
            collected = _stop_and_collect(self.unit)
            if collected:
                break
        if not self.cgroup.is_dir():
            empty = True
        if empty:
            self._tree_empty_proven = True
        proven = collected and self._tree_empty_proven
        if proven and held_fd is not None:
            self.cgroup_fd = None
            with contextlib.suppress(OSError):
                os.close(held_fd)
        return proven

    def tree_empty(self) -> bool:
        if self.cgroup_fd is None:
            return self._tree_empty_proven
        events = _read_text_at(self.cgroup_fd, "cgroup.events") or ""
        if any(line == "populated 0" for line in events.splitlines()):
            self._tree_empty_proven = True
        return self._tree_empty_proven

    def pids(self) -> list[int]:
        if self.cgroup_fd is None:
            return []
        raw = _read_text_at(self.cgroup_fd, "cgroup.procs") or ""
        return [int(line) for line in raw.splitlines() if line.isdigit()]


class ResourceBoundary:
    """Allocates a killable, durable systemd scope with proven controllers."""

    def allocate(self, job_id: str, limits: ResourceLimits, *, timeout_sec: int | None) -> ProvenScope:
        raise CommandError("resource_boundary_unproven")

    def prove_pid(self, scope: ProvenScope, pid: int) -> None:
        raise CommandError("resource_boundary_unproven")

    def recover_scope(
        self,
        job_id: str,
        unit: str,
        cgroup_path: str,
        limits: ResourceLimits,
        *,
        timeout_sec: int | None,
    ) -> ProvenScope:
        """Re-attest persisted scope identity before any restart cleanup."""
        raise CommandError("resource_boundary_unproven")

    def stop(self, scope: ProvenScope | None) -> bool:
        return True if scope is None else scope.kill()


class MissingControllerBoundary(ResourceBoundary):
    """Test double: admission must fail closed when controllers are missing."""


class SystemdCgroupBoundary(ResourceBoundary):
    def recover_scope(
        self,
        job_id: str,
        unit: str,
        cgroup_path: str,
        limits: ResourceLimits,
        *,
        timeout_sec: int | None,
    ) -> ProvenScope:
        if len(job_id) != 32 or not all(ch in "0123456789abcdef" for ch in job_id):
            raise CommandError("resource_boundary_unproven")
        expected_unit = f"friday-ecmd-{job_id}.service"
        if unit != expected_unit:
            raise CommandError("resource_boundary_unproven")
        cgroup = Path(cgroup_path)
        normalized = os.path.normpath(cgroup_path)
        if (
            not cgroup.is_absolute()
            or normalized != cgroup_path
            or not normalized.startswith("/sys/fs/cgroup/")
            or cgroup.name != expected_unit
        ):
            raise CommandError("resource_boundary_unproven")
        try:
            st = os.lstat(cgroup)
        except FileNotFoundError as exc:
            # A cgroup cannot disappear while populated.  Collection of the
            # strictly validated transient unit completes the absence proof
            # for a cleanup-pending row recovered after restart.
            if _stop_and_collect(unit):
                return ProvenScope(
                    job_id=job_id,
                    unit=unit,
                    cgroup=cgroup,
                    limits=limits,
                    cgroup_fd=None,
                    _tree_empty_proven=True,
                )
            raise CommandError("resource_boundary_unproven") from exc
        except OSError as exc:
            raise CommandError("resource_boundary_unproven") from exc
        fd = _open_cgroup_dir(cgroup)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != (opened.st_dev, opened.st_ino):
                raise CommandError("resource_boundary_unproven")
            control_group, active = _show_unit_identity(unit)
            expected_control = cgroup_path.removeprefix("/sys/fs/cgroup")
            if active not in {"active", "activating", "deactivating"} or control_group != expected_control:
                raise CommandError("resource_boundary_unproven")
            runtime = (
                None if timeout_sec is None else max(1, int(timeout_sec) + int(limits.runtime_grace_sec))
            )
            _prove_limits(cgroup, limits)
            _prove_unit_contract(unit, runtime_sec=runtime)
            return ProvenScope(job_id=job_id, unit=unit, cgroup=cgroup, limits=limits, cgroup_fd=fd)
        except Exception:
            os.close(fd)
            raise

    def allocate(self, job_id: str, limits: ResourceLimits, *, timeout_sec: int | None) -> ProvenScope:
        if len(job_id) != 32 or not all(ch in "0123456789abcdef" for ch in job_id):
            raise CommandError("invalid_job_id")
        if not os.path.isfile(SYSTEMD_RUN_EXECUTABLE) or not os.path.isfile(SYSTEMCTL_EXECUTABLE):
            raise CommandError("resource_boundary_unproven")
        unit = f"friday-ecmd-{job_id}.service"
        runtime = None if timeout_sec is None else max(1, int(timeout_sec) + int(limits.runtime_grace_sec))
        argv = [
            SYSTEMD_RUN_EXECUTABLE,
            "--user",
            "--no-block",
            "--collect",
            f"--unit=friday-ecmd-{job_id}",
            "-p",
            f"MemoryMax={int(limits.memory_max)}",
            "-p",
            f"MemorySwapMax={int(limits.memory_swap_max)}",
            "-p",
            f"TasksMax={int(limits.tasks_max)}",
            "-p",
            f"CPUQuota={int(limits.cpu_quota_percent)}%",
            "-p",
            "KillMode=control-group",
            "-p",
            "Delegate=yes",
            "--",
            SLEEP_EXECUTABLE,
            "infinity",
        ]
        if runtime is not None:
            marker = argv.index("KillMode=control-group") - 1
            argv[marker:marker] = ["-p", f"RuntimeMaxSec={runtime}"]
        try:
            completed = subprocess.run(  # noqa: S603 - closed argv, root-owned helpers
                argv,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _stop_and_collect(unit)
            raise CommandError("resource_boundary_unproven") from exc
        if completed.returncode not in {0, None}:
            _stop_and_collect(unit)
            raise CommandError("resource_boundary_unproven")
        try:
            cgroup = _wait_unit_cgroup(unit)
            _prove_limits(cgroup, limits)
            _prove_unit_contract(unit, runtime_sec=runtime)
        except CommandError:
            _stop_and_collect(unit)
            raise
        try:
            cgroup_fd = _open_cgroup_dir(cgroup)
        except CommandError:
            _stop_and_collect(unit)
            raise
        return ProvenScope(job_id=job_id, unit=unit, cgroup=cgroup, limits=limits, cgroup_fd=cgroup_fd)

    def prove_pid(self, scope: ProvenScope, pid: int) -> None:
        try:
            raw = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="ascii")
        except OSError as exc:
            raise CommandError("resource_boundary_unproven") from exc
        relative = ""
        for line in raw.splitlines():
            if line.startswith("0::"):
                relative = line[3:]
                break
        expected = str(scope.cgroup)
        prefix = "/sys/fs/cgroup"
        actual = prefix + relative if relative.startswith("/") else prefix + "/" + relative
        if actual != expected and not actual.startswith(expected + "/"):
            raise CommandError("resource_boundary_unproven")
        _prove_limits(scope.cgroup, scope.limits)

    def move_pid(self, scope: ProvenScope, pid: int) -> None:
        try:
            fd = os.open(str(scope.cgroup / "cgroup.procs"), os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.write(fd, f"{int(pid)}\n".encode("ascii"))
            finally:
                os.close(fd)
        except OSError as exc:
            raise CommandError("cgroup_move_failed") from exc
        self.prove_pid(scope, pid)


def _wait_unit_cgroup(unit: str) -> Path:
    deadline = time.monotonic() + 5.0
    last = ""
    while time.monotonic() < deadline:
        try:
            shown = subprocess.run(  # noqa: S603
                [SYSTEMCTL_EXECUTABLE, "--user", "show", unit, "-p", "ControlGroup", "-p", "ActiveState"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandError("resource_boundary_unproven") from exc
        last = shown.stdout or ""
        control = ""
        active = ""
        for line in last.splitlines():
            if line.startswith("ControlGroup="):
                control = line.split("=", 1)[1]
            elif line.startswith("ActiveState="):
                active = line.split("=", 1)[1]
        if active in {"active", "activating"} and control.startswith("/"):
            path = Path("/sys/fs/cgroup") / control.lstrip("/")
            if path.is_dir() and (path / "memory.max").exists() and (path / "cgroup.kill").exists():
                return path
        time.sleep(0.05)
    raise CommandError("resource_boundary_unproven")


def _show_unit_identity(unit: str) -> tuple[str, str]:
    try:
        shown = subprocess.run(  # noqa: S603
            [SYSTEMCTL_EXECUTABLE, "--user", "show", unit, "-p", "ControlGroup", "-p", "ActiveState"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("resource_boundary_unproven") from exc
    if shown.returncode not in {0, None}:
        raise CommandError("resource_boundary_unproven")
    control = ""
    active = ""
    for line in (shown.stdout or "").splitlines():
        if line.startswith("ControlGroup="):
            control = line.split("=", 1)[1]
        elif line.startswith("ActiveState="):
            active = line.split("=", 1)[1]
    return control, active


def _stop_and_collect(unit: str) -> bool:
    """Bounded cleanup for a validated transient unit; verify it unloads."""
    if not unit.startswith("friday-ecmd-") or not unit.endswith(".service"):
        return False
    middle = unit.removeprefix("friday-ecmd-").removesuffix(".service")
    if len(middle) != 32 or not all(ch in "0123456789abcdef" for ch in middle):
        return False
    for verb in ("stop", "reset-failed"):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [SYSTEMCTL_EXECUTABLE, "--user", verb, unit],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            shown = subprocess.run(  # noqa: S603
                [SYSTEMCTL_EXECUTABLE, "--user", "show", unit, "-p", "LoadState"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        load_state = ""
        for line in (shown.stdout or "").splitlines():
            if line.startswith("LoadState="):
                load_state = line.split("=", 1)[1]
        if load_state == "not-found":
            return True
        if shown.returncode not in {0, None}:
            return False
        time.sleep(0.05)
    return False


def _cpu_quota_usec(percent: int) -> str:
    period = 100_000
    quota = int(period * int(percent) / 100)
    return f"{quota} {period}"


def _parse_systemd_usec(raw: str) -> int:
    text = str(raw or "").strip().lower()
    if not text or text in {"infinity", "inf", "[not set]"}:
        raise CommandError("resource_boundary_unproven")
    if text.isdigit():
        return int(text)
    multipliers = {
        "usec": 1,
        "us": 1,
        "msec": 1_000,
        "ms": 1_000,
        "sec": 1_000_000,
        "s": 1_000_000,
        "min": 60_000_000,
        "h": 3_600_000_000,
    }
    token = re.compile(r"(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>usec|msec|sec|min|us|ms|h|s)")
    total = Decimal(0)
    end = 0
    matched = False
    try:
        for match in token.finditer(text):
            if text[end : match.start()].strip():
                raise CommandError("resource_boundary_unproven")
            matched = True
            total += Decimal(match.group("number")) * multipliers[match.group("unit")]
            end = match.end()
    except (InvalidOperation, ValueError) as exc:
        raise CommandError("resource_boundary_unproven") from exc
    if not matched or text[end:].strip() or total != total.to_integral_value():
        raise CommandError("resource_boundary_unproven")
    return int(total)


def _prove_unit_contract(unit: str, *, runtime_sec: int | None) -> None:
    try:
        shown = subprocess.run(  # noqa: S603
            [
                SYSTEMCTL_EXECUTABLE,
                "--user",
                "show",
                unit,
                "-p",
                "KillMode",
                "-p",
                "RuntimeMaxUSec",
                "-p",
                "Delegate",
                "-p",
                "CollectMode",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("resource_boundary_unproven") from exc
    kill_mode = ""
    runtime_raw = ""
    delegate = ""
    collect_mode = ""
    for line in (shown.stdout or "").splitlines():
        if line.startswith("KillMode="):
            kill_mode = line.split("=", 1)[1]
        elif line.startswith("RuntimeMaxUSec="):
            runtime_raw = line.split("=", 1)[1]
        elif line.startswith("Delegate="):
            delegate = line.split("=", 1)[1]
        elif line.startswith("CollectMode="):
            collect_mode = line.split("=", 1)[1]
    if kill_mode != "control-group":
        raise CommandError("resource_boundary_unproven")
    if delegate.strip().lower() not in {"yes", "1", "true"}:
        raise CommandError("resource_boundary_unproven")
    if collect_mode != "inactive-or-failed":
        raise CommandError("resource_boundary_unproven")
    if runtime_sec is None:
        if runtime_raw.strip().lower() not in {"infinity", "inf", "[not set]"}:
            raise CommandError("resource_boundary_unproven")
    elif _parse_systemd_usec(runtime_raw) != int(runtime_sec) * 1_000_000:
        raise CommandError("resource_boundary_unproven")


def _prove_limits(cgroup: Path, limits: ResourceLimits) -> None:
    memory = _read_text(cgroup / "memory.max")
    swap = _read_text(cgroup / "memory.swap.max")
    pids = _read_text(cgroup / "pids.max")
    cpu = _read_text(cgroup / "cpu.max")
    if memory != str(int(limits.memory_max)):
        raise CommandError("resource_boundary_unproven")
    expected_swap = "0" if limits.memory_swap_max == 0 else str(int(limits.memory_swap_max))
    if swap != expected_swap:
        raise CommandError("resource_boundary_unproven")
    if pids != str(int(limits.tasks_max)):
        raise CommandError("resource_boundary_unproven")
    if cpu != _cpu_quota_usec(limits.cpu_quota_percent):
        raise CommandError("resource_boundary_unproven")
    if not (cgroup / "cgroup.kill").exists():
        raise CommandError("resource_boundary_unproven")
