"""Bounded adapter execution with a production systemd-user boundary."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from friday.host_control.adapters.base import ExecutionSpec
from friday.host_control.adapters.jq import MAX_JQ_INPUT_BYTES
from friday.host_control.contracts import ExecutableAttestation
from friday.host_control.plans import HostActionPlan
from friday.host_control.plans import WorkspaceGrant as PlanWorkspaceGrant

from .executable_attestation import verify_executable

_JOB_ID = re.compile(r"h?job_[0-9a-f]{16,64}")
_SEALED_INPUT_ATTEMPTS = 8
_STREAM_CHUNK_BYTES = 128 * 1024
_ENV_EXECUTABLE = "/usr/bin/env"
_BWRAP_EXECUTABLE = "/usr/bin/bwrap"
_PROBE_EXECUTABLE = "/usr/bin/test"
_PYTHON_EXECUTABLE = "/usr/bin/python3"
_CANCEL_TERM_WAIT_SEC = 1.0
_CANCEL_KILL_WAIT_SEC = 1.0
_CANCEL_POLL_SEC = 0.05
_MAX_SYSTEMD_OBSERVATION_BYTES = 8192
_VERIFIED_EXEC_SCRIPT = r"""
import hashlib
import os
import stat
import sys

def deny():
    raise SystemExit(126)

if len(sys.argv) < 9:
    deny()
path = sys.argv[1]
try:
    expected = (
        int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
        int(sys.argv[5]), int(sys.argv[6]), sys.argv[7],
    )
except ValueError:
    deny()
argv = sys.argv[8:]
if not argv or argv[0] != path or not path.startswith("/") or os.path.realpath(path) != path:
    deny()
descriptor = -1
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_mode & 0o022
        or not before.st_mode & 0o111
    ):
        deny()
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(descriptor)
    observed = (
        before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode),
        before.st_size, before.st_mtime_ns, digest.hexdigest(),
    )
    stable = (
        before.st_dev, before.st_ino, before.st_mode, before.st_uid,
        before.st_gid, before.st_size, before.st_mtime_ns,
    ) == (
        after.st_dev, after.st_ino, after.st_mode, after.st_uid,
        after.st_gid, after.st_size, after.st_mtime_ns,
    )
    if not stable or observed != expected:
        deny()
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    os.execve(f"/proc/self/fd/{descriptor}", argv, dict(os.environ))
except OSError:
    deny()
finally:
    if descriptor >= 0:
        os.close(descriptor)
""".strip()


class RunnerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceGrant:
    job_id: str
    actor_own_id: str
    workspace_root: str
    grants: tuple[PlanWorkspaceGrant, ...]

    def resolve(self, reference: str) -> Path:
        if not _JOB_ID.fullmatch(self.job_id):
            raise ValueError("workspace grant job id is malformed")
        root = Path(self.workspace_root)
        root_text = str(root)
        if (
            len(root_text) > 1024
            or any(ord(character) < 32 for character in root_text)
            or not root.is_absolute()
            or root.is_symlink()
            or str(root.resolve(strict=True)) != root_text
        ):
            raise ValueError("workspace root is not a canonical directory")
        selected = {
            "job_input": root / "input",
            "job_work": root / "work",
            "job_output": root / "output",
            "job_evidence": root / "evidence",
        }.get(reference)
        if selected is None or selected.is_symlink():
            raise ValueError("working-directory grant is invalid")
        resolved = selected.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_relative_to(root):
            raise ValueError("working directory escapes the granted workspace")
        return resolved

    def validate_plan_grants(
        self,
        expected: tuple[PlanWorkspaceGrant, ...],
        *,
        verify_read_identity: bool = True,
    ) -> None:
        if self.grants != expected or any(item.actor_own_id != self.actor_own_id for item in self.grants):
            raise ValueError("workspace grants do not match the signed plan")
        root = Path(self.workspace_root).resolve(strict=True)
        for grant in self.grants:
            candidate = root / grant.relative_path
            parent = candidate.parent.resolve(strict=True)
            if not parent.is_relative_to(root) or candidate.is_symlink():
                raise ValueError("workspace grant escapes through a path or symlink")
            if grant.access in {"read", "replace"}:
                if not verify_read_identity:
                    continue
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file() or not resolved.is_relative_to(root):
                    raise ValueError("workspace input grant is not a regular in-workspace file")
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
                if digest != grant.identity_sha256:
                    raise ValueError("workspace input identity changed after planning")
            elif candidate.exists():
                raise ValueError("create grant cannot overwrite an existing workspace object")


@dataclass(frozen=True, slots=True)
class ResourceBudgets:
    memory_max_bytes: int = 512 * 1024 * 1024
    tasks_max: int = 64
    cpu_quota_percent: int = 100
    file_size_max_bytes: int = 64 * 1024 * 1024

    def validate(self) -> None:
        if not 16 * 1024 * 1024 <= self.memory_max_bytes <= 8 * 1024 * 1024 * 1024:
            raise ValueError("memory budget is invalid")
        if not 1 <= self.tasks_max <= 512:
            raise ValueError("task budget is invalid")
        if not 1 <= self.cpu_quota_percent <= 400:
            raise ValueError("CPU budget is invalid")
        if not 1024 <= self.file_size_max_bytes <= 1024 * 1024 * 1024:
            raise ValueError("file-size budget is invalid")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outcome: str
    effect_boundary_crossed: bool
    unit_id: str | None
    cgroup_identity: str | None
    exit_code: int | None
    signal: int | None
    started_at: float
    finished_at: float
    timed_out: bool
    cancelled: bool
    output_truncated: bool
    stdout: bytes
    stderr: bytes
    error_code: str | None = None


class ExecutionBackend(Protocol):
    def available(self) -> bool: ...

    def run(
        self,
        *,
        job_id: str,
        executable: ExecutableAttestation,
        execution: ExecutionSpec,
        working_directory: Path,
        budgets: ResourceBudgets,
        cancel_event: threading.Event | None,
    ) -> ProcessResult: ...

    def cancel(self, job_id: str) -> bool: ...

    def reconcile(self, job_id: str) -> dict[str, str]: ...


class SystemdUserBackend:
    """Production backend: a user-service cgroup around a mandatory bwrap boundary."""

    def __init__(
        self,
        *,
        systemd_run: str = "/usr/bin/systemd-run",
        systemctl: str = "/usr/bin/systemctl",
        probe_base: str | Path | None = None,
    ) -> None:
        self._systemd_run = systemd_run
        self._systemctl = systemctl
        self._probe_base = None if probe_base is None else Path(probe_base)
        self._cgroup_root = Path("/sys/fs/cgroup")
        self._availability: bool | None = None
        self._availability_lock = threading.Lock()

    def available(self) -> bool:
        with self._availability_lock:
            if self._availability is not None:
                return self._availability
            self._availability = self.static_available() and self._probe_effective_boundary()
            return self._availability

    def static_available(self) -> bool:
        """Validate fixed launch dependencies without requiring a live user bus."""

        return all(_trusted_root_executable(item) for item in self._commands) and all(
            _trusted_code_owned_executable(item)
            for item in (
                _ENV_EXECUTABLE,
                _BWRAP_EXECUTABLE,
                _PROBE_EXECUTABLE,
                _PYTHON_EXECUTABLE,
            )
        )

    @property
    def _commands(self) -> tuple[str, str]:
        return self._systemd_run, self._systemctl

    def run(
        self,
        *,
        job_id: str,
        executable: ExecutableAttestation,
        execution: ExecutionSpec,
        working_directory: Path,
        budgets: ResourceBudgets,
        cancel_event: threading.Event | None,
    ) -> ProcessResult:
        if not self.available():
            raise RunnerUnavailable("systemd user execution is unavailable")
        # The inventory identity may be old and availability probing can take
        # time.  Re-check the host-visible ownership and exact identity at the
        # last point before constructing the production launch command.  The
        # in-sandbox verifier below then closes the remaining pathname race.
        verify_executable(executable)
        unit = _unit_name(job_id)
        command = self._command(
            unit=unit,
            working_directory=working_directory,
            profile=execution.profile.value,
            timeout_sec=execution.timeout_sec,
            budgets=budgets,
            target_environment={**_MINIMAL_ENV, **dict(execution.environment)},
            target_argv=execution.argv,
            executable_attestation=executable,
        )
        result = _capture_process(
            command,
            launcher=self._systemd_run,
            environment=_systemd_user_environment(),
            timeout_sec=execution.timeout_sec + 5,
            max_output_bytes=execution.max_output_bytes,
            cancel_event=cancel_event,
            cancellation=lambda: self.cancel(job_id),
            unit_id=unit,
            cgroup_identity=f"systemd-user:{unit}",
        )
        if result.outcome == "failed" and b"Failed to connect to bus" in result.stderr:
            raise RunnerUnavailable("systemd user manager is unavailable")
        return result

    def _command(
        self,
        *,
        unit: str,
        working_directory: Path,
        profile: str,
        timeout_sec: int,
        budgets: ResourceBudgets,
        target_environment: dict[str, str],
        target_argv: tuple[str, ...],
        executable_attestation: ExecutableAttestation | None = None,
        collect: bool = False,
    ) -> list[str]:
        command = [
            self._systemd_run,
            "--user",
            "--wait",
            "--pipe",
            "--service-type=exec",
            f"--unit={unit}",
            f"--property=WorkingDirectory={working_directory}",
            f"--property=RuntimeMaxSec={timeout_sec}",
            f"--property=MemoryMax={budgets.memory_max_bytes}",
            f"--property=TasksMax={budgets.tasks_max}",
            f"--property=CPUQuota={budgets.cpu_quota_percent}%",
            f"--property=LimitFSIZE={budgets.file_size_max_bytes}",
            "--property=NoNewPrivileges=yes",
            "--property=LockPersonality=yes",
            "--property=RestrictSUIDSGID=yes",
            "--property=UMask=0077",
        ]
        if collect:
            command.insert(4, "--collect")
        if profile == "cli_network_unprivileged":
            command.append("--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK")
        else:
            command.append("--property=RestrictAddressFamilies=AF_UNIX AF_NETLINK")
        command.extend(["--", _ENV_EXECUTABLE, "--ignore-environment"])
        command.extend(f"{key}={value}" for key, value in target_environment.items())
        command.extend(
            _bubblewrap_argv(
                working_directory=working_directory,
                profile=profile,
                target_argv=target_argv,
                executable_attestation=executable_attestation,
            )
        )
        return command

    def _probe_effective_boundary(self) -> bool:
        try:
            with tempfile.TemporaryDirectory(
                prefix="friday-host-boundary-probe-",
                dir=self._probe_base,
            ) as temporary:
                working_directory = Path(temporary)
                working_directory.chmod(0o700)
                sentinel = working_directory.with_name(
                    f".friday-host-boundary-sentinel-{secrets.token_hex(8)}"
                )
                sentinel_descriptor = os.open(
                    sentinel,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                os.close(sentinel_descriptor)
                try:
                    for profile in ("cli_local_readonly", "cli_network_unprivileged"):
                        command = self._command(
                            unit=f"friday-host-probe-{secrets.token_hex(8)}.service",
                            working_directory=working_directory,
                            profile=profile,
                            timeout_sec=5,
                            budgets=ResourceBudgets(
                                memory_max_bytes=64 * 1024 * 1024,
                                tasks_max=8,
                                cpu_quota_percent=50,
                                file_size_max_bytes=1024 * 1024,
                            ),
                            target_environment=dict(_MINIMAL_ENV),
                            target_argv=(_PROBE_EXECUTABLE, "!", "-e", str(sentinel)),
                            collect=True,
                        )
                        result = subprocess.run(  # noqa: S603 - exact code-owned boundary probe
                            command,
                            executable=self._systemd_run,
                            env=_systemd_user_environment(),
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            timeout=10,
                            check=False,
                        )
                        if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 16_384:
                            return False
                finally:
                    sentinel.unlink(missing_ok=True)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
        return True

    def cancel(self, job_id: str) -> bool:
        unit = _unit_name(job_id)
        # An inactive unit before the request gives no evidence that this
        # cancellation caused its terminal state.  In particular, `systemctl
        # kill` returning zero is only command acceptance, never termination.
        if _terminal_unit_observed(self._observe_unit(unit), expected_unit=unit):
            return False
        term_accepted = self._signal_unit(unit, signal_name="SIGTERM")
        if term_accepted and self._wait_for_terminal(unit, timeout_sec=_CANCEL_TERM_WAIT_SEC):
            return True
        kill_accepted = self._signal_unit(unit, signal_name="SIGKILL")
        terminal = self._wait_for_terminal(unit, timeout_sec=_CANCEL_KILL_WAIT_SEC)
        return terminal and (term_accepted or kill_accepted)

    def reconcile(self, job_id: str) -> dict[str, str]:
        unit = _unit_name(job_id)
        return self._observe_unit(unit)

    def _signal_unit(self, unit: str, *, signal_name: str) -> bool:
        try:
            result = subprocess.run(  # noqa: S603 - fixed systemctl and unit grammar
                [
                    self._systemctl,
                    "--user",
                    "kill",
                    f"--signal={signal_name}",
                    "--kill-whom=all",
                    unit,
                ],
                executable=self._systemctl,
                env=_systemd_user_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return (
            result.returncode == 0
            and len(result.stdout) <= _MAX_SYSTEMD_OBSERVATION_BYTES
            and len(result.stderr) <= _MAX_SYSTEMD_OBSERVATION_BYTES
        )

    def _wait_for_terminal(self, unit: str, *, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while True:
            if _terminal_unit_observed(self._observe_unit(unit), expected_unit=unit):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_CANCEL_POLL_SEC, remaining))

    def _observe_unit(self, unit: str) -> dict[str, str]:
        try:
            result = subprocess.run(  # noqa: S603 - fixed systemctl and unit grammar
                [
                    self._systemctl,
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=Result",
                    "--property=ExecMainCode",
                    "--property=ExecMainStatus",
                    "--property=MainPID",
                    "--property=ControlPID",
                    "--property=ControlGroup",
                ],
                executable=self._systemctl,
                env=_systemd_user_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _unknown_unit_observation(unit)
        if (
            len(result.stdout) > _MAX_SYSTEMD_OBSERVATION_BYTES
            or len(result.stderr) > _MAX_SYSTEMD_OBSERVATION_BYTES
        ):
            return _unknown_unit_observation(unit)
        fields: dict[str, str] = {}
        try:
            lines = result.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError:
            return _unknown_unit_observation(unit)
        for line in lines:
            if "=" not in line:
                return _unknown_unit_observation(unit)
            key, value = line.split("=", 1)
            if key in fields:
                return _unknown_unit_observation(unit)
            fields[key] = value
        required = {
            "ActiveState",
            "ControlGroup",
            "ControlPID",
            "ExecMainCode",
            "ExecMainStatus",
            "LoadState",
            "MainPID",
            "Result",
            "SubState",
        }
        if not required.issubset(fields) or (
            result.returncode != 0
            and not (fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive")
        ):
            return _unknown_unit_observation(unit)
        cgroup_population = self._cgroup_population(fields["ControlGroup"])
        observation = {
            "state": fields["ActiveState"],
            "unit_id": unit,
            **fields,
            "cgroup_populated": ("unknown" if cgroup_population is None else str(cgroup_population)),
        }
        observation["terminal_observed"] = (
            "true" if _terminal_unit_observed(observation, expected_unit=unit) else "false"
        )
        return observation

    def _cgroup_population(self, control_group: str) -> int | None:
        if not control_group:
            return 0
        if len(control_group) > 2048 or not control_group.startswith("/") or "\x00" in control_group:
            return None
        parts = Path(control_group).parts[1:]
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        events = self._cgroup_root.joinpath(*parts, "cgroup.events")
        descriptor = -1
        try:
            descriptor = os.open(
                events,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            raw = os.read(descriptor, 4097)
            if len(raw) > 4096 or os.read(descriptor, 1):
                return None
        except FileNotFoundError:
            # A cgroup removed by systemd has no surviving processes.
            return 0
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        values: dict[str, str] = {}
        try:
            for line in raw.decode("ascii", errors="strict").splitlines():
                key, value = line.split(" ", 1)
                if key in values:
                    return None
                values[key] = value
        except (UnicodeDecodeError, ValueError):
            return None
        populated = values.get("populated")
        return int(populated) if populated in {"0", "1"} else None


class DirectExecTestBackend:
    """Explicit test backend; production callers must use SystemdUserBackend."""

    def available(self) -> bool:
        return True

    def run(
        self,
        *,
        job_id: str,
        executable: ExecutableAttestation,
        execution: ExecutionSpec,
        working_directory: Path,
        budgets: ResourceBudgets,
        cancel_event: threading.Event | None,
    ) -> ProcessResult:
        del budgets
        return _capture_process(
            list(execution.argv),
            launcher=executable.canonical_path,
            cwd=working_directory,
            environment=dict(execution.environment),
            timeout_sec=execution.timeout_sec,
            max_output_bytes=execution.max_output_bytes,
            cancel_event=cancel_event,
            unit_id=None,
            cgroup_identity=None,
        )

    def cancel(self, job_id: str) -> bool:
        del job_id
        return False

    def reconcile(self, job_id: str) -> dict[str, str]:
        return {"state": "unknown", "job_id": job_id, "reason": "test backend is not durable"}


class ProcessRunner:
    def __init__(
        self,
        backend: ExecutionBackend | None = None,
        *,
        workspace_base: str | Path | None = None,
        sealed_input_base: str | Path | None = None,
    ) -> None:
        self._backend = SystemdUserBackend() if backend is None else backend
        self._workspace_base = None if workspace_base is None else Path(workspace_base)
        if self._workspace_base is not None and (
            not self._workspace_base.is_absolute()
            or self._workspace_base.is_symlink()
            or not self._workspace_base.is_dir()
            or str(self._workspace_base.resolve(strict=True)) != str(self._workspace_base)
        ):
            raise ValueError("workspace base must be an existing canonical absolute directory")
        self._sealed_input_base = (
            None if sealed_input_base is None else _private_sealed_input_base(Path(sealed_input_base))
        )
        if (
            self._workspace_base is not None
            and self._sealed_input_base is not None
            and (
                self._sealed_input_base.is_relative_to(self._workspace_base)
                or self._workspace_base.is_relative_to(self._sealed_input_base)
            )
        ):
            raise ValueError("sealed inputs must be outside the shared workspace tree")

    def run(
        self,
        *,
        job_id: str,
        plan: HostActionPlan,
        executable: ExecutableAttestation,
        execution: ExecutionSpec,
        workspace: WorkspaceGrant,
        budgets: ResourceBudgets,
        cancel_event: threading.Event | None = None,
    ) -> ProcessResult:
        if job_id != workspace.job_id or not _JOB_ID.fullmatch(job_id):
            raise ValueError("job and workspace identities do not match")
        if self._workspace_base is None:
            raise RunnerUnavailable("host job workspace base is not configured")
        workspace_root = Path(workspace.workspace_root).resolve(strict=True)
        if (
            workspace_root.parent != self._workspace_base
            or workspace_root.name != job_id
            or not workspace_root.is_relative_to(self._workspace_base)
        ):
            raise ValueError("workspace is outside the configured per-job root")
        if plan.actor_own_id != workspace.actor_own_id:
            raise ValueError("plan actor does not own this workspace")
        workspace.validate_plan_grants(
            plan.workspace_grants,
            verify_read_identity=plan.adapter_id != "data.jq",
        )
        budgets.validate()
        working_directory = workspace.resolve(execution.working_directory_ref)
        verify_executable(executable)
        if (
            execution.executable != executable.canonical_path
            or execution.argv[0] != executable.canonical_path
        ):
            raise ValueError("execution is not bound to the attested executable")
        if (
            plan.executable_attestation_digest != executable.digest
            or plan.execution_profile != execution.profile
            or plan.timeout_sec != execution.timeout_sec
            or plan.max_output_bytes != execution.max_output_bytes
        ):
            raise ValueError("execution drifted from the signed host action plan")
        if not self._backend.available():
            raise RunnerUnavailable("configured process boundary is unavailable")
        if plan.adapter_id != "data.jq":
            return self._run_backend(
                job_id=job_id,
                executable=executable,
                execution=execution,
                working_directory=working_directory,
                budgets=budgets,
                cancel_event=cancel_event,
            )
        if self._sealed_input_base is None:
            raise RunnerUnavailable("agent-private jq input sealing is not configured")
        with _sealed_jq_input(
            workspace=workspace,
            plan=plan,
            execution=execution,
            sealed_input_base=self._sealed_input_base,
        ) as sealed_working_directory:
            return self._run_backend(
                job_id=job_id,
                executable=executable,
                execution=execution,
                working_directory=sealed_working_directory,
                budgets=budgets,
                cancel_event=cancel_event,
            )

    def _run_backend(
        self,
        *,
        job_id: str,
        executable: ExecutableAttestation,
        execution: ExecutionSpec,
        working_directory: Path,
        budgets: ResourceBudgets,
        cancel_event: threading.Event | None,
    ) -> ProcessResult:
        return self._backend.run(
            job_id=job_id,
            executable=executable,
            execution=execution,
            working_directory=working_directory,
            budgets=budgets,
            cancel_event=cancel_event,
        )

    def cancel(self, job_id: str) -> bool:
        return self._backend.cancel(job_id)

    def reconcile(self, job_id: str) -> dict[str, str]:
        return self._backend.reconcile(job_id)


def _private_sealed_input_base(path: Path) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or str(path.resolve(strict=True)) != str(path)
    ):
        raise ValueError("sealed input base must be an existing canonical absolute directory")
    observed = path.lstat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise ValueError("sealed input base must be an agent-owned mode-0700 directory")
    return path


@contextmanager
def _sealed_jq_input(
    *,
    workspace: WorkspaceGrant,
    plan: HostActionPlan,
    execution: ExecutionSpec,
    sealed_input_base: Path,
) -> Iterator[Path]:
    """Copy the exact signed jq input into an agent-private, read-only directory."""

    if (
        plan.action_id != "extract_fields"
        or execution.working_directory_ref != "job_input"
        or len(plan.workspace_grants) != 1
    ):
        raise ValueError("jq execution does not have one sealed read grant")
    grant = plan.workspace_grants[0]
    if grant.access != "read" or grant.identity_sha256 is None:
        raise ValueError("jq execution read grant is invalid")
    prefix, separator, input_name = grant.relative_path.partition("/")
    if (
        prefix != "input"
        or separator != "/"
        or not input_name
        or "/" in input_name
        or execution.argv[-1] != input_name
    ):
        raise ValueError("jq argv is not bound to its workspace grant")

    base_descriptor = _open_directory(sealed_input_base)
    snapshot_name = ""
    snapshot_descriptor = -1
    try:
        _require_private_directory(base_descriptor, mode=0o700, label="sealed input base")
        for _attempt in range(_SEALED_INPUT_ATTEMPTS):
            snapshot_name = f"{workspace.job_id}-{secrets.token_hex(12)}"
            try:
                os.mkdir(snapshot_name, mode=0o700, dir_fd=base_descriptor)
            except FileExistsError:
                continue
            break
        else:
            raise ValueError("could not allocate a private jq snapshot")
        snapshot_descriptor = os.open(
            snapshot_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=base_descriptor,
        )
        _require_private_directory(snapshot_descriptor, mode=0o700, label="jq snapshot")
        _copy_exact_grant_to_snapshot(
            workspace=workspace,
            grant=grant,
            destination_directory=snapshot_descriptor,
            destination_name=input_name,
        )
        os.fchmod(snapshot_descriptor, 0o500)
        _require_private_directory(snapshot_descriptor, mode=0o500, label="sealed jq snapshot")
        sealed_path = sealed_input_base / snapshot_name
        yield sealed_path
    finally:
        if snapshot_descriptor >= 0:
            with suppress(OSError):
                os.fchmod(snapshot_descriptor, 0o700)
            if input_name:
                with suppress(OSError):
                    os.unlink(input_name, dir_fd=snapshot_descriptor)
            os.close(snapshot_descriptor)
        if snapshot_name:
            with suppress(OSError):
                os.rmdir(snapshot_name, dir_fd=base_descriptor)
        os.close(base_descriptor)


def _copy_exact_grant_to_snapshot(
    *,
    workspace: WorkspaceGrant,
    grant: PlanWorkspaceGrant,
    destination_directory: int,
    destination_name: str,
) -> None:
    root_descriptor = _open_directory(Path(workspace.workspace_root))
    input_descriptor = -1
    source_descriptor = -1
    destination_descriptor = -1
    try:
        _require_private_directory(root_descriptor, mode=0o700, label="workspace root")
        input_descriptor = os.open(
            "input",
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        _require_private_directory(input_descriptor, mode=0o700, label="workspace input")
        source_descriptor = os.open(
            destination_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=input_descriptor,
        )
        before = os.fstat(source_descriptor)
        _require_private_regular_file(
            before,
            mode=0o600,
            maximum=MAX_JQ_INPUT_BYTES,
            label="workspace jq input",
        )
        destination_descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=destination_directory,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(
                source_descriptor,
                min(_STREAM_CHUNK_BYTES, MAX_JQ_INPUT_BYTES + 1 - copied),
            )
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_JQ_INPUT_BYTES:
                raise ValueError("workspace jq input exceeds the sealed-input limit")
            digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        after = os.fstat(source_descriptor)
        if _file_identity(before) != _file_identity(after) or copied != before.st_size:
            raise ValueError("workspace jq input changed while it was being sealed")
        if digest.hexdigest() != grant.identity_sha256:
            raise ValueError("workspace jq input identity changed after planning")
        os.fsync(destination_descriptor)
        os.fchmod(destination_descriptor, 0o400)
        sealed = os.fstat(destination_descriptor)
        _require_private_regular_file(
            sealed,
            mode=0o400,
            maximum=MAX_JQ_INPUT_BYTES,
            label="sealed jq input",
        )
        if sealed.st_size != copied:
            raise ValueError("sealed jq input size is inconsistent")
    except OSError as exc:
        raise ValueError("workspace jq input could not be sealed safely") from exc
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if input_descriptor >= 0:
            os.close(input_descriptor)
        os.close(root_descriptor)


def _open_directory(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ValueError("private directory could not be opened safely") from exc


def _require_private_directory(descriptor: int, *, mode: int, label: str) -> None:
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != mode
    ):
        raise ValueError(f"{label} metadata is unsafe")


def _require_private_regular_file(
    observed: os.stat_result,
    *,
    mode: int,
    maximum: int,
    label: str,
) -> None:
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != mode
        or observed.st_nlink != 1
        or not 0 <= observed.st_size <= maximum
    ):
        raise ValueError(f"{label} metadata is unsafe")


def _file_identity(observed: os.stat_result) -> tuple[int, ...]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_uid,
        observed.st_gid,
        observed.st_mode,
        observed.st_nlink,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while sealing jq input")
        offset += written


_MINIMAL_ENV = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}


def _systemd_user_environment() -> dict[str, str]:
    """Return the code-owned locator for this process's user manager."""

    runtime_dir = f"/run/user/{os.geteuid()}"
    return {
        **_MINIMAL_ENV,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
        "XDG_RUNTIME_DIR": runtime_dir,
    }


def _unknown_unit_observation(unit: str) -> dict[str, str]:
    return {
        "cgroup_populated": "unknown",
        "state": "unknown",
        "terminal_observed": "false",
        "unit_id": unit,
    }


def _terminal_unit_observed(observation: dict[str, str], *, expected_unit: str) -> bool:
    return (
        observation.get("unit_id") == expected_unit
        and observation.get("LoadState") in {"loaded", "not-found"}
        and observation.get("ActiveState") in {"inactive", "failed"}
        and observation.get("SubState") in {"dead", "exited", "failed"}
        and observation.get("MainPID") == "0"
        and observation.get("ControlPID") == "0"
        and observation.get("cgroup_populated") == "0"
    )


def _unit_name(job_id: str) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("job id is malformed")
    prefix = "hjob_" if job_id.startswith("hjob_") else "job_"
    return f"friday-host-{job_id.removeprefix(prefix)}.service"


def _bubblewrap_argv(
    *,
    working_directory: Path,
    profile: str,
    target_argv: tuple[str, ...],
    executable_attestation: ExecutableAttestation | None = None,
) -> tuple[str, ...]:
    if profile not in {"cli_local_readonly", "cli_network_unprivileged"}:
        raise ValueError("execution profile has no filesystem boundary")
    if not target_argv or not target_argv[0].startswith("/"):
        raise ValueError("sandbox target argv is invalid")
    sandbox_target = (
        target_argv
        if executable_attestation is None
        else _verified_exec_argv(executable_attestation, target_argv)
    )
    command = [
        _BWRAP_EXECUTABLE,
        "--unshare-all",
        "--unshare-user",
    ]
    if profile == "cli_network_unprivileged":
        command.append("--share-net")
    command.extend(
        [
            "--uid",
            str(os.geteuid()),
            "--gid",
            str(os.getegid()),
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
            "--dir",
            "/etc",
            "--ro-bind-try",
            "/etc/ld.so.cache",
            "/etc/ld.so.cache",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/run",
            "--ro-bind",
            str(working_directory),
            str(working_directory),
            "--chdir",
            str(working_directory),
            "--",
            *sandbox_target,
        ]
    )
    return tuple(command)


def _verified_exec_argv(
    executable: ExecutableAttestation,
    target_argv: tuple[str, ...],
) -> tuple[str, ...]:
    """Build the stdlib-only final identity check and held-FD exec boundary."""

    if not target_argv or target_argv[0] != executable.canonical_path:
        raise ValueError("verified execution argv is not bound to its attestation")
    return (
        _PYTHON_EXECUTABLE,
        "-I",
        "-c",
        _VERIFIED_EXEC_SCRIPT,
        executable.canonical_path,
        str(executable.device),
        str(executable.inode),
        str(executable.mode),
        str(executable.size_bytes),
        str(executable.mtime_ns),
        executable.sha256,
        *target_argv,
    )


def _trusted_code_owned_executable(value: str) -> bool:
    path = Path(value)
    try:
        if not path.is_absolute() or path.lstat().st_uid != 0:
            return False
        return _trusted_root_executable(str(path.resolve(strict=True)))
    except OSError:
        return False


def _trusted_root_executable(value: str) -> bool:
    path = Path(value)
    try:
        observed = path.lstat()
        return (
            path.is_absolute()
            and stat.S_ISREG(observed.st_mode)
            and observed.st_uid == 0
            and not observed.st_mode & 0o022
            and bool(observed.st_mode & 0o111)
            and str(path.resolve(strict=True)) == str(path)
        )
    except OSError:
        return False


def _capture_process(
    argv: list[str],
    *,
    launcher: str,
    timeout_sec: float,
    max_output_bytes: int,
    cancel_event: threading.Event | None,
    unit_id: str | None,
    cgroup_identity: str | None,
    cancellation: object | None = None,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> ProcessResult:
    started = time.time()
    process: subprocess.Popen[bytes] | None = None
    stdout = bytearray()
    stderr = bytearray()
    truncated = False
    timed_out = False
    cancelled = False
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed launcher, already validated argv
            argv,
            executable=launcher,
            cwd=cwd,
            env=_MINIMAL_ENV if environment is None else {**_MINIMAL_ENV, **environment},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + timeout_sec
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        while selector.get_map():
            if not cancelled and cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate(process, cancellation)
            elif not timed_out and time.monotonic() >= deadline:
                timed_out = True
                _terminate(process, cancellation)
            for key, _mask in selector.select(0.05):
                file_object = cast(BinaryIO, key.fileobj)
                chunk = _read_pipe(file_object.fileno(), 65536)
                if not chunk:
                    selector.unregister(file_object)
                    file_object.close()
                    continue
                target: bytearray = key.data
                remaining = max(0, max_output_bytes - len(stdout) - len(stderr))
                target.extend(chunk[:remaining])
                truncated = truncated or len(chunk) > remaining
        return_code = process.wait(timeout=2)
        outcome = (
            "cancelled"
            if cancelled
            else "timed_out"
            if timed_out
            else "completed"
            if return_code == 0
            else "failed"
        )
        return ProcessResult(
            outcome=outcome,
            effect_boundary_crossed=True,
            unit_id=unit_id,
            cgroup_identity=cgroup_identity,
            exit_code=return_code if return_code >= 0 else None,
            signal=-return_code if return_code < 0 else None,
            started_at=started,
            finished_at=time.time(),
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=truncated,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        )
    except Exception:
        if process is None:
            raise
        observed = _terminate(process, cancellation)
        return ProcessResult(
            outcome="unknown",
            effect_boundary_crossed=True,
            unit_id=unit_id,
            cgroup_identity=cgroup_identity,
            exit_code=None,
            signal=None,
            started_at=started,
            finished_at=time.time(),
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=truncated,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            error_code="runner_failure_after_start" if observed else "termination_unconfirmed",
        )


def _terminate(process: subprocess.Popen[bytes], cancellation: object | None) -> bool:
    if callable(cancellation):
        try:
            if bool(cancellation()):
                return True
        except Exception:
            pass
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None
        return True


def _read_pipe(descriptor: int, size: int) -> bytes:
    return os.read(descriptor, size)


__all__ = [
    "DirectExecTestBackend",
    "ProcessResult",
    "ProcessRunner",
    "ResourceBudgets",
    "RunnerUnavailable",
    "SystemdUserBackend",
    "WorkspaceGrant",
]
