"""Closed identities for the universal Engineer command kernel."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

SCHEMA = "friday.engineer.command.v4"
BASH_EXECUTABLE = "/usr/bin/bash"
SHELL_FLAG_PREFIX = ("--noprofile", "--norc", "-o", "pipefail", "-c")
SANDBOX_COMMAND = "/run/friday/command"
SANDBOX_INTERPRETER = "/run/friday/interpreter"
SANDBOX_SCRIPT = "/run/friday/script"
SANDBOX_EXPORT = "/run/friday/export"
SANDBOX_EXPORT_IMPL = "/run/friday/export-impl.py"
SANDBOX_STDIN = "/run/friday/stdin"
SANDBOX_JOB = "/job"
BWRAP_EXEC_FD = 3
BWRAP_SCRIPT_FD = 4
BWRAP_BLOCK_FD = 5
BWRAP_EXPORT_FD = 6
BWRAP_PATH_ROOT_FD_BASE = 7
MAX_TRUSTED_PATH_ROOTS = 16
BWRAP_STDIN_PAYLOAD_FD = BWRAP_PATH_ROOT_FD_BASE + MAX_TRUSTED_PATH_ROOTS
BWRAP_EXPORT_IMPL_FD = BWRAP_STDIN_PAYLOAD_FD + 1
BWRAP_EXECUTABLE = "/usr/bin/bwrap"
FIXED_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "PWD", "TMPDIR", "TZ")
ALLOWED_CHANNELS = frozenset({"telegram", "cli_test", "owner_console"})
GRANT_TTL_DEFAULT_SEC = 90
GRANT_TTL_MAX_SEC = 180
MAX_ARGV_ITEMS = 256
MAX_ARGV_BYTES = 128 * 1024
MAX_ARG_CHARS = 4096
MAX_SHELL_CHARS = 16 * 1024
MAX_STDIN_BYTES = 1 * 1024 * 1024
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_TIMEOUT_SEC = 3600
MAX_OUTPUT_FILES = 64
MAX_OUTPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_TREE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_DEPTH = 8
MAX_OUTPUT_DIRS = 64
MAX_GRANT_CHARS = 16384
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
DEFAULT_TRUSTED_PATH = ("/usr/bin", "/bin")
SENSITIVE_SANDBOX_PATH_ROOTS = (
    "/dev",
    "/etc",
    "/job",
    "/proc",
    "/run",
    "/sys",
    "/var/run",
)
SYSTEMD_RUN_EXECUTABLE = "/usr/bin/systemd-run"
SYSTEMCTL_EXECUTABLE = "/usr/bin/systemctl"
TRUE_EXECUTABLE = "/usr/bin/true"
SLEEP_EXECUTABLE = "/usr/bin/sleep"
DEFAULT_TASKS_MAX = 64
DEFAULT_MEMORY_MAX = 128 * 1024 * 1024
DEFAULT_MEMORY_SWAP_MAX = 0
DEFAULT_CPU_QUOTA_PERCENT = 80
DEFAULT_FSIZE_BYTES = 32 * 1024 * 1024
DEFAULT_TMPFS_TMP = 16 * 1024 * 1024
DEFAULT_TMPFS_WORKSPACE = 16 * 1024 * 1024
DEFAULT_TMPFS_JOB_TMP = 8 * 1024 * 1024
DEFAULT_OUTPUT_BYTES = MAX_OUTPUT_TREE_BYTES
DESTRUCTIVE_BASENAMES = frozenset(
    {
        "chage",
        "chown",
        "chroot",
        "doas",
        "docker",
        "ksu",
        "login",
        "machinectl",
        "mount",
        "nerdctl",
        "newgrp",
        "nsenter",
        "passwd",
        "pkexec",
        "podman",
        "sg",
        "su",
        "sudo",
        "systemctl",
        "umount",
        "unshare",
        "usermod",
        "visudo",
    }
)
FORBIDDEN_PATH_PREFIXES = (
    "/dev/",
    "/proc/",
    "/sys/",
    "/run/docker.sock",
    "/var/run/docker.sock",
    "/var/run/docker/",
    "/run/containerd/",
    "/root/",
)
FORBIDDEN_EXACT_PATHS = frozenset(
    {
        "/dev",
        "/proc",
        "/sys",
        "/run/docker.sock",
        "/var/run/docker.sock",
    }
)


class CommandError(ValueError):
    """A closed kernel refusal. ``code`` is the only machine-stable field."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        self.code = str(code or "command_failed")[:80]
        self.detail = str(detail or "")[:240]
        super().__init__(self.code)


class CommandLane(StrEnum):
    ARGV = "argv"
    SHELL = "shell"


class CommandOrigin(StrEnum):
    OWNER_TURN = "owner_turn"
    MODEL = "model"
    DOCUMENT = "document"
    WEB = "web"
    MEMORY = "memory"
    ATTACHMENT = "attachment"


class CommandStatus(StrEnum):
    PLANNED = "planned"
    ADMITTED = "admitted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class IsolationProfile(StrEnum):
    ISOLATED_WORKSPACE = "isolated_workspace"
    HOST_USER = "host_user"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def framed_argv_digest(argv: tuple[str, ...]) -> str:
    framed = b"".join(len(item.encode("utf-8")).to_bytes(4, "big") + item.encode("utf-8") for item in argv)
    return sha256_bytes(framed)


def path_root_is_sensitive(path: str) -> bool:
    normalized = path if path == "/" else path.rstrip("/")
    if normalized == "/":
        return True
    return any(
        normalized == root
        or normalized.startswith(root + "/")
        or root.startswith(normalized + "/")
        for root in SENSITIVE_SANDBOX_PATH_ROOTS
    )


@dataclass(frozen=True, slots=True)
class TrustedPathContract:
    """Code-owned PATH used for both resolution and the child environment."""

    directories: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.directories or len(self.directories) > MAX_TRUSTED_PATH_ROOTS:
            raise CommandError("invalid_trusted_path")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in self.directories:
            if not isinstance(item, str) or not item.startswith("/") or "\x00" in item:
                raise CommandError("invalid_trusted_path")
            if any(part in {".", ".."} for part in Path(item).parts):
                raise CommandError("invalid_trusted_path")
            normalized = item if item == "/" else item.rstrip("/")
            if path_root_is_sensitive(normalized):
                raise CommandError("invalid_trusted_path")
            if normalized in seen:
                raise CommandError("invalid_trusted_path")
            seen.add(normalized)
            cleaned.append(normalized)
        object.__setattr__(self, "directories", tuple(cleaned))

    @classmethod
    def default(cls) -> TrustedPathContract:
        return cls(directories=DEFAULT_TRUSTED_PATH)

    @property
    def runtime_path(self) -> str:
        return ":".join(self.directories)


@dataclass(frozen=True, slots=True)
class PathRoot:
    """Attested trusted-PATH directory held open through sandbox setup."""

    path: str
    owner_uid: int
    owner_gid: int
    mode: int
    device: int
    inode: int
    mtime_ns: int
    dir_fd: int

    def __post_init__(self) -> None:
        if not self.path.startswith("/") or path_root_is_sensitive(self.path):
            raise CommandError("invalid_trusted_path")


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Finite cgroup/tmpfs/rlimit envelope. Admission fails if these cannot be proven."""

    tasks_max: int = DEFAULT_TASKS_MAX
    memory_max: int = DEFAULT_MEMORY_MAX
    memory_swap_max: int = DEFAULT_MEMORY_SWAP_MAX
    cpu_quota_percent: int = DEFAULT_CPU_QUOTA_PERCENT
    fsize_bytes: int = DEFAULT_FSIZE_BYTES
    tmpfs_tmp: int = DEFAULT_TMPFS_TMP
    tmpfs_workspace: int = DEFAULT_TMPFS_WORKSPACE
    tmpfs_job_tmp: int = DEFAULT_TMPFS_JOB_TMP
    output_bytes: int = DEFAULT_OUTPUT_BYTES
    runtime_grace_sec: int = 5

    def __post_init__(self) -> None:
        for name in (
            "tasks_max",
            "memory_max",
            "fsize_bytes",
            "tmpfs_tmp",
            "tmpfs_workspace",
            "tmpfs_job_tmp",
            "output_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise CommandError("invalid_resource_limits")
        if isinstance(self.memory_swap_max, bool) or self.memory_swap_max < 0:
            raise CommandError("invalid_resource_limits")
        if isinstance(self.cpu_quota_percent, bool) or not 1 <= self.cpu_quota_percent <= 100:
            raise CommandError("invalid_resource_limits")

    @classmethod
    def default(cls) -> ResourceLimits:
        return cls()


@dataclass(frozen=True, slots=True)
class OwnerSource:
    """HMAC-sealed authenticated current source. Not inventable from origin/argv."""

    actor_id: str
    tenant_id: str
    conversation_id: str
    channel: str
    source_row_id: str
    source_hash: str
    telegram_update_id: str
    isolation_profile: IsolationProfile
    idempotency_key: str
    mac: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "idempotency_key": self.idempotency_key,
            "isolation_profile": self.isolation_profile.value,
            "schema": SCHEMA,
            "source_hash": self.source_hash,
            "source_row_id": self.source_row_id,
            "telegram_update_id": self.telegram_update_id,
            "tenant_id": self.tenant_id,
            "v": 4,
        }


@dataclass(frozen=True, slots=True)
class OwnerConfirmation:
    """Separately sealed current-owner confirmation row. Not a boolean or a hash claim."""

    actor_id: str
    tenant_id: str
    conversation_id: str
    channel: str
    confirmation_row_id: str
    confirmation_update_id: str
    command_digest: str
    expires_at: int
    nonce: str
    mac: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "channel": self.channel,
            "command_digest": self.command_digest,
            "confirmation_row_id": self.confirmation_row_id,
            "confirmation_update_id": self.confirmation_update_id,
            "conversation_id": self.conversation_id,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "schema": SCHEMA,
            "tenant_id": self.tenant_id,
            "v": 4,
        }


@dataclass(frozen=True, slots=True)
class DestructiveApproval:
    """Alias kept for the grant seam: a sealed OwnerConfirmation, never a boolean."""

    confirmation: OwnerConfirmation


@dataclass(frozen=True, slots=True)
class CommandRequest:
    lane: CommandLane
    origin: CommandOrigin
    argv: tuple[str, ...] = ()
    shell_command: str | None = None
    stdin: bytes = b""
    timeout_sec: int = 30
    max_stdout_bytes: int = MAX_STDOUT_BYTES
    max_stderr_bytes: int = MAX_STDERR_BYTES
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if self.lane is CommandLane.ARGV:
            if not self.argv or self.shell_command is not None:
                raise CommandError("invalid_request", detail="argv")
            if len(self.argv) > MAX_ARGV_ITEMS:
                raise CommandError("argv_overflow")
            total = 0
            for item in self.argv:
                if not isinstance(item, str) or "\x00" in item or not item or len(item) > MAX_ARG_CHARS:
                    raise CommandError("invalid_arguments")
                total += len(item.encode("utf-8")) + 1
            if total > MAX_ARGV_BYTES:
                raise CommandError("argv_overflow")
        elif self.lane is CommandLane.SHELL:
            text = self.shell_command
            if self.argv or not isinstance(text, str) or not text or "\x00" in text:
                raise CommandError("invalid_request", detail="shell")
            if len(text) > MAX_SHELL_CHARS:
                raise CommandError("shell_overflow")
        else:
            raise CommandError("invalid_request")
        if isinstance(self.timeout_sec, bool) or not 1 <= self.timeout_sec <= MAX_TIMEOUT_SEC:
            raise CommandError("invalid_request", detail="timeout")
        if isinstance(self.max_stdout_bytes, bool) or not 1 <= self.max_stdout_bytes <= 8 * 1024 * 1024:
            raise CommandError("invalid_request", detail="stdout")
        if isinstance(self.max_stderr_bytes, bool) or not 1 <= self.max_stderr_bytes <= 2 * 1024 * 1024:
            raise CommandError("invalid_request", detail="stderr")
        if not isinstance(self.stdin, bytes):
            raise CommandError("invalid_stdin")
        if len(self.stdin) > MAX_STDIN_BYTES:
            raise CommandError("stdin_overflow")
        key = self.idempotency_key
        if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
            raise CommandError("invalid_request", detail="idempotency_key")

    @property
    def digest(self) -> str:
        payload = {
            "argv": list(self.argv),
            "lane": self.lane.value,
            "max_stderr_bytes": self.max_stderr_bytes,
            "max_stdout_bytes": self.max_stdout_bytes,
            "origin": self.origin.value,
            "schema": SCHEMA,
            "shell_command": self.shell_command,
            "stdin_sha256": sha256_bytes(self.stdin),
            "timeout_sec": self.timeout_sec,
        }
        return sha256_bytes(canonical_json_bytes(payload))

    @property
    def argv_sha256(self) -> str:
        if self.lane is CommandLane.SHELL:
            return framed_argv_digest((*SHELL_FLAG_PREFIX, self.shell_command or ""))
        return framed_argv_digest(self.argv)


@dataclass(frozen=True, slots=True)
class VerifiedCommandGrant:
    actor_id: str
    tenant_id: str
    conversation_id: str
    channel: str
    source_row_id: str
    source_hash: str
    telegram_update_id: str
    isolation_profile: IsolationProfile
    idempotency_key: str
    command_digest: str
    argv_sha256: str
    lane: CommandLane
    origin: CommandOrigin
    destructive_confirmed: bool
    confirmation_nonce: str
    confirmation_expires_at: int
    expires_at: int
    nonce: str


@dataclass(frozen=True, slots=True)
class ResolvedExecutable:
    requested: str
    canonical_path: str
    owner_uid: int
    owner_gid: int
    mode: int
    device: int
    inode: int
    size_bytes: int
    mtime_ns: int
    sha256: str

    def identity_tuple(self) -> tuple[int, int, int, int, int, int, str]:
        return (
            self.device,
            self.inode,
            self.mode,
            self.owner_uid,
            self.owner_gid,
            self.size_bytes,
            self.sha256,
        )

    def to_public_payload(self) -> dict[str, Any]:
        return {
            "canonical_path": self.canonical_path,
            "mode": self.mode,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(slots=True)
class HeldExecutable:
    resolved: ResolvedExecutable
    executable_fd: int
    executable_sealed: bool = True
    interpreter: ResolvedExecutable | None = None
    interpreter_fd: int | None = None
    script: ResolvedExecutable | None = None
    script_fd: int | None = None
    inner_rest: tuple[str, ...] = ()

    def close(self) -> None:
        seen: set[int] = set()
        for fd in (self.executable_fd, self.interpreter_fd, self.script_fd):
            if fd is None or fd < 0 or fd in seen:
                continue
            seen.add(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        self.executable_fd = -1
        self.interpreter_fd = None
        self.script_fd = None


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    relative_path: str
    size_bytes: int
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class CommandProgress:
    job_id: str
    status: CommandStatus
    elapsed_sec: float
    stdout_bytes: int
    stderr_bytes: int
    output_activity: bool
    isolation_profile: IsolationProfile = IsolationProfile.ISOLATED_WORKSPACE
    percent: float | None = None
    eta_sec: float | None = None

    def to_public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "elapsed_sec": round(self.elapsed_sec, 3),
            "isolated": self.isolation_profile is IsolationProfile.ISOLATED_WORKSPACE,
            "isolation_profile": self.isolation_profile.value,
            "job_id": self.job_id,
            "output_activity": self.output_activity,
            "status": self.status.value,
            "stderr_bytes": self.stderr_bytes,
            "stdout_bytes": self.stdout_bytes,
        }
        if self.percent is not None:
            payload["percent"] = self.percent
        if self.eta_sec is not None:
            payload["eta_sec"] = self.eta_sec
        return payload


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    job_id: str
    status: CommandStatus
    lane: CommandLane
    origin: CommandOrigin
    isolation_profile: IsolationProfile
    command_digest: str
    argv_sha256: str
    source_hash: str
    exit_code: int | None
    signal: int | None
    timed_out: bool
    cancelled: bool
    truncated_stdout: bool
    truncated_stderr: bool
    started_at: float
    finished_at: float | None
    executable: ResolvedExecutable | None
    stdout_sha256: str
    stderr_sha256: str
    stdout: bytes
    stderr: bytes
    generated_files: tuple[GeneratedFile, ...]
    error_code: str
    effect_boundary_crossed: bool
    receipt_mac: str
    shell_subcommands_attested: Literal[False] = False
    authorization_complete: Literal[False] = False

    def to_public_payload(self) -> dict[str, Any]:
        payload = {
            "authorization_complete": False,
            "argv_sha256": self.argv_sha256,
            "cancelled": self.cancelled,
            "command_digest": self.command_digest,
            "effect_boundary_crossed": self.effect_boundary_crossed,
            "error_code": self.error_code,
            "exit_code": self.exit_code,
            "generated_file_count": len(self.generated_files),
            "isolated": self.isolation_profile is IsolationProfile.ISOLATED_WORKSPACE,
            "isolation_profile": self.isolation_profile.value,
            "job_id": self.job_id,
            "lane": self.lane.value,
            "receipt_mac": self.receipt_mac,
            "signal": self.signal,
            "source_hash": self.source_hash,
            "status": self.status.value,
            "stderr_sha256": self.stderr_sha256,
            "stdout_sha256": self.stdout_sha256,
            "timed_out": self.timed_out,
            "truncated_stderr": self.truncated_stderr,
            "truncated_stdout": self.truncated_stdout,
        }
        if self.lane is CommandLane.SHELL:
            payload["shell_subcommands_attested"] = False
        return payload


__all__ = [
    "ALLOWED_CHANNELS",
    "BASH_EXECUTABLE",
    "BWRAP_EXECUTABLE",
    "DEFAULT_TRUSTED_PATH",
    "DESTRUCTIVE_BASENAMES",
    "FIXED_ENV_KEYS",
    "FORBIDDEN_EXACT_PATHS",
    "FORBIDDEN_PATH_PREFIXES",
    "GRANT_TTL_DEFAULT_SEC",
    "GRANT_TTL_MAX_SEC",
    "MAX_ARG_CHARS",
    "MAX_ARGV_BYTES",
    "MAX_ARGV_ITEMS",
    "MAX_EXECUTABLE_BYTES",
    "MAX_GRANT_CHARS",
    "MAX_OUTPUT_DEPTH",
    "MAX_OUTPUT_DIRS",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_TREE_BYTES",
    "MAX_SHELL_CHARS",
    "MAX_STDERR_BYTES",
    "MAX_STDIN_BYTES",
    "MAX_STDOUT_BYTES",
    "MAX_TIMEOUT_SEC",
    "SANDBOX_COMMAND",
    "SANDBOX_EXPORT",
    "SANDBOX_EXPORT_IMPL",
    "SANDBOX_INTERPRETER",
    "SANDBOX_JOB",
    "SANDBOX_STDIN",
    "SANDBOX_SCRIPT",
    "BWRAP_BLOCK_FD",
    "BWRAP_EXEC_FD",
    "BWRAP_EXPORT_FD",
    "BWRAP_EXPORT_IMPL_FD",
    "BWRAP_STDIN_PAYLOAD_FD",
    "BWRAP_PATH_ROOT_FD_BASE",
    "BWRAP_SCRIPT_FD",
    "SCHEMA",
    "SENSITIVE_SANDBOX_PATH_ROOTS",
    "SHELL_FLAG_PREFIX",
    "CommandError",
    "CommandLane",
    "CommandOrigin",
    "CommandProgress",
    "CommandReceipt",
    "CommandRequest",
    "CommandStatus",
    "DestructiveApproval",
    "GeneratedFile",
    "HeldExecutable",
    "IsolationProfile",
    "OwnerConfirmation",
    "OwnerSource",
    "PathRoot",
    "ResolvedExecutable",
    "ResourceLimits",
    "TrustedPathContract",
    "VerifiedCommandGrant",
    "canonical_json_bytes",
    "framed_argv_digest",
    "path_root_is_sensitive",
    "sha256_bytes",
]
