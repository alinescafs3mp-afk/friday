"""Verified systemd/cgroup resource boundary. Fail closed if limits cannot be proven."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import time
from dataclasses import dataclass
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


def _open_cgroup_dir(cgroup: Path) -> int:
    try:
        return os.open(
            str(cgroup),
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise CommandError("resource_boundary_unproven") from exc


@dataclass
class ProvenScope:
    job_id: str
    unit: str
    cgroup: Path
    limits: ResourceLimits
    cgroup_fd: int | None = None
    _tree_empty_proven: bool = False

    def kill(self) -> bool:
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

    def allocate(self, job_id: str, limits: ResourceLimits, *, timeout_sec: int) -> ProvenScope:
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
        timeout_sec: int,
    ) -> ProvenScope:
        """Re-attest persisted scope identity before any restart cleanup."""
        raise CommandError("resource_boundary_unproven")

    def stop(self, scope: ProvenScope | None) -> None:
        if scope is not None:
            scope.kill()


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
        timeout_sec: int,
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
            fd = _open_cgroup_dir(cgroup)
        except OSError as exc:
            raise CommandError("resource_boundary_unproven") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != (opened.st_dev, opened.st_ino):
                raise CommandError("resource_boundary_unproven")
            control_group, active = _show_unit_identity(unit)
            expected_control = cgroup_path.removeprefix("/sys/fs/cgroup")
            if active not in {"active", "activating", "deactivating"} or control_group != expected_control:
                raise CommandError("resource_boundary_unproven")
            runtime = max(1, int(timeout_sec) + int(limits.runtime_grace_sec))
            _prove_limits(cgroup, limits)
            _prove_unit_contract(unit, runtime_sec=runtime)
            return ProvenScope(job_id=job_id, unit=unit, cgroup=cgroup, limits=limits, cgroup_fd=fd)
        except Exception:
            os.close(fd)
            raise

    def allocate(self, job_id: str, limits: ResourceLimits, *, timeout_sec: int) -> ProvenScope:
        if len(job_id) != 32 or not all(ch in "0123456789abcdef" for ch in job_id):
            raise CommandError("invalid_job_id")
        if not os.path.isfile(SYSTEMD_RUN_EXECUTABLE) or not os.path.isfile(SYSTEMCTL_EXECUTABLE):
            raise CommandError("resource_boundary_unproven")
        unit = f"friday-ecmd-{job_id}.service"
        runtime = max(1, int(timeout_sec) + int(limits.runtime_grace_sec))
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
            f"RuntimeMaxSec={runtime}",
            "-p",
            "KillMode=control-group",
            "-p",
            "Delegate=yes",
            "--",
            SLEEP_EXECUTABLE,
            "9999",
        ]
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
    units = (
        ("usec", 1),
        ("us", 1),
        ("msec", 1_000),
        ("ms", 1_000),
        ("sec", 1_000_000),
        ("min", 60_000_000),
        ("h", 3_600_000_000),
        ("s", 1_000_000),
    )
    for suffix, multiplier in units:
        if text.endswith(suffix):
            number = text[: -len(suffix)]
            if not number:
                raise CommandError("resource_boundary_unproven")
            try:
                return int(float(number) * multiplier)
            except ValueError as exc:
                raise CommandError("resource_boundary_unproven") from exc
    try:
        return int(text)
    except ValueError as exc:
        raise CommandError("resource_boundary_unproven") from exc


def _prove_unit_contract(unit: str, *, runtime_sec: int) -> None:
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
    if _parse_systemd_usec(runtime_raw) != int(runtime_sec) * 1_000_000:
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
