"""Closed identities for the universal Engineer command kernel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

SCHEMA = "friday.engineer.command.v1"
TRUSTED_PATH = ("/usr/bin", "/bin", "/usr/local/bin")
BASH_EXECUTABLE = "/bin/bash"
SHELL_ARGV_PREFIX = ("/bin/bash", "--noprofile", "--norc", "-o", "pipefail", "-c")
FIXED_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "PWD", "TMPDIR", "TZ")
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
MAX_GRANT_CHARS = 8192
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


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def framed_argv_digest(argv: tuple[str, ...]) -> str:
    framed = b"".join(len(item.encode("utf-8")).to_bytes(4, "big") + item.encode("utf-8") for item in argv)
    return sha256_bytes(framed)


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


@dataclass(frozen=True, slots=True)
class VerifiedCommandGrant:
    actor_id: str
    turn_id: str
    command_digest: str
    lane: CommandLane
    origin: CommandOrigin
    destructive_confirmed: bool
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
    percent: float | None = None
    eta_sec: float | None = None

    def to_public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "elapsed_sec": round(self.elapsed_sec, 3),
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
    argv: tuple[str, ...]
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
    authorization_complete: Literal[False] = False

    def to_public_payload(self) -> dict[str, Any]:
        return {
            "authorization_complete": False,
            "cancelled": self.cancelled,
            "effect_boundary_crossed": self.effect_boundary_crossed,
            "error_code": self.error_code,
            "exit_code": self.exit_code,
            "generated_file_count": len(self.generated_files),
            "job_id": self.job_id,
            "lane": self.lane.value,
            "signal": self.signal,
            "status": self.status.value,
            "stderr_sha256": self.stderr_sha256,
            "stdout_sha256": self.stdout_sha256,
            "timed_out": self.timed_out,
            "truncated_stderr": self.truncated_stderr,
            "truncated_stdout": self.truncated_stdout,
        }


__all__ = [
    "BASH_EXECUTABLE",
    "DESTRUCTIVE_BASENAMES",
    "FIXED_ENV_KEYS",
    "FORBIDDEN_EXACT_PATHS",
    "FORBIDDEN_PATH_PREFIXES",
    "GRANT_TTL_DEFAULT_SEC",
    "GRANT_TTL_MAX_SEC",
    "MAX_ARG_CHARS",
    "MAX_ARGV_BYTES",
    "MAX_ARGV_ITEMS",
    "MAX_GRANT_CHARS",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_TREE_BYTES",
    "MAX_SHELL_CHARS",
    "MAX_STDERR_BYTES",
    "MAX_STDIN_BYTES",
    "MAX_STDOUT_BYTES",
    "MAX_TIMEOUT_SEC",
    "SCHEMA",
    "SHELL_ARGV_PREFIX",
    "TRUSTED_PATH",
    "CommandError",
    "CommandLane",
    "CommandOrigin",
    "CommandProgress",
    "CommandReceipt",
    "CommandRequest",
    "CommandStatus",
    "GeneratedFile",
    "ResolvedExecutable",
    "VerifiedCommandGrant",
    "canonical_json_bytes",
    "framed_argv_digest",
    "sha256_bytes",
]
