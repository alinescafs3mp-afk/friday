"""Verified systemd/cgroup resource boundary. Fail closed if limits cannot be proven."""

from __future__ import annotations

import contextlib
import os
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


@dataclass
class ProvenScope:
    job_id: str
    unit: str
    cgroup: Path
    limits: ResourceLimits

    def kill(self) -> bool:
        kill_path = self.cgroup / "cgroup.kill"
        with contextlib.suppress(OSError):
            kill_path.write_text("1", encoding="ascii")
        empty = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            events = _read_text(self.cgroup / "cgroup.events") or ""
            if any(line == "populated 0" for line in events.splitlines()):
                empty = True
                break
            if not self.cgroup.is_dir():
                empty = True
                break
            time.sleep(0.05)
        subprocess.run(
            [SYSTEMCTL_EXECUTABLE, "--user", "stop", self.unit],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if not self.cgroup.is_dir():
            empty = True
        return empty

    def tree_empty(self) -> bool:
        events = _read_text(self.cgroup / "cgroup.events") or ""
        return any(line == "populated 0" for line in events.splitlines())

    def pids(self) -> list[int]:
        raw = _read_text(self.cgroup / "cgroup.procs") or ""
        return [int(line) for line in raw.splitlines() if line.isdigit()]


class ResourceBoundary:
    """Allocates a killable, durable systemd scope with proven controllers."""

    def allocate(self, job_id: str, limits: ResourceLimits, *, timeout_sec: int) -> ProvenScope:
        raise CommandError("resource_boundary_unproven")

    def prove_pid(self, scope: ProvenScope, pid: int) -> None:
        raise CommandError("resource_boundary_unproven")

    def stop(self, scope: ProvenScope | None) -> None:
        if scope is not None:
            scope.kill()


class MissingControllerBoundary(ResourceBoundary):
    """Test double: admission must fail closed when controllers are missing."""


class SystemdCgroupBoundary(ResourceBoundary):
    def allocate(self, job_id: str, limits: ResourceLimits, *, timeout_sec: int) -> ProvenScope:
        if not job_id or "/" in job_id or not all(ch in "0123456789abcdef" for ch in job_id):
            raise CommandError("invalid_job_id")
        if not os.path.isfile(SYSTEMD_RUN_EXECUTABLE) or not os.path.isfile(SYSTEMCTL_EXECUTABLE):
            raise CommandError("resource_boundary_unproven")
        unit = f"friday-ecmd-{job_id}.service"
        runtime = max(1, int(timeout_sec) + int(limits.runtime_grace_sec))
        argv = [
            SYSTEMD_RUN_EXECUTABLE,
            "--user",
            "--no-block",
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
            "KillMode=process",
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
            raise CommandError("resource_boundary_unproven") from exc
        if completed.returncode not in {0, None}:
            raise CommandError("resource_boundary_unproven")
        cgroup = _wait_unit_cgroup(unit)
        try:
            _prove_limits(cgroup, limits)
        except CommandError:
            subprocess.run(
                [SYSTEMCTL_EXECUTABLE, "--user", "stop", unit],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            raise
        return ProvenScope(job_id=job_id, unit=unit, cgroup=cgroup, limits=limits)

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


def _cpu_quota_usec(percent: int) -> str:
    period = 100_000
    quota = int(period * int(percent) / 100)
    return f"{quota} {period}"


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
