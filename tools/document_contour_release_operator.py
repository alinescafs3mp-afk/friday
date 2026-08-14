#!/usr/bin/env python3
"""Run Friday's canonical document contour inside one recoverable bridge outage.

This operator is intentionally narrower than a general service manager.  It
binds one clean immutable candidate to one two-run document battery, publishes
the exact private inter-run observer response, and restores the Telegram bridge
once on every catchable exit after the stop contour has been armed.

Public output contains only closed codes, booleans and hashes.  Owner and model
credentials stay in process memory and are never placed in argv or reports.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import ipaddress
import json
import os
import re
import secrets
import select
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
_root_import_path = str(ROOT)
if not sys.path or sys.path[0] != _root_import_path:
    sys.path.insert(0, _root_import_path)

import friday as _friday_package  # noqa: E402
from friday.diagnostics import (  # noqa: E402
    collect_document_contour_guarded_bridge_queue_snapshot,
)
from friday.diagnostics.runtime_lease import (  # noqa: E402
    ProcessLease,
    inspect_process_lease,
    process_owns_lease,
)

OPERATOR_SCHEMA = "friday.document-contour-release-operator.v1"
OBSERVER_SNAPSHOT_SCHEMA = "friday.document-contour-observer-snapshot.v1"
GUARDED_QUEUE_SCHEMA = "friday.document-contour-guarded-bridge-queue.v1"
RUN_RECEIPT_SCHEMA = "friday.document-contour-live-battery.run-receipt.v1"
OBSERVER_REQUEST_SCHEMA = "friday.document-contour-live-battery.observer-request.v1"
OBSERVER_RESPONSE_SCHEMA = "friday.document-contour-live-battery.observer-response.v2"
BATTERY_REPORT_SCHEMA = "friday.document-contour-live-battery.report.v1"
BATTERY_WORKER_SCHEMA = "friday.document-contour-live-battery.worker.v1"
BATTERY_CASE_IDS = tuple(f"D{index:02d}" for index in range(1, 11))

_EXPECTED_DEPENDENCY_HASHES = {
    "tools/document_contour_live_battery.py": (
        "45f2944158f240cd1a61988aa27436b936a638e1c76051fdcefc019dc08cc3d1"
    ),
    "friday/diagnostics/__init__.py": ("86ce0798ec2666b3ebe05318fc1483042c2c9e35994f60d7f588cae47c779c06"),
    "friday/diagnostics/runtime_lease.py": (
        "6986bcef0d21d1754672ad784746fbc205b4822de708c71b16dd93576f3d1926"
    ),
    "friday/admin_api/_overview.py": ("056acbb8d761bd041a5ff465ad122156529b48e9828596cd39a1adf313166d47"),
}

_MODEL_ENV_ALLOWLIST = frozenset(
    {
        "FRIDAY_PROFILE",
        "FRIDAY_LLM_BASE_URL",
        "FRIDAY_LLM_MODEL",
        "FRIDAY_LLM_API_KEY",
        "FRIDAY_LLM_TIMEOUT_SEC",
        "FRIDAY_LLM_MAX_TOKENS",
        "FRIDAY_LLM_FOREGROUND_SLOTS",
        "FRIDAY_EMBEDDINGS_ENABLED",
        "FRIDAY_EMBEDDINGS_BASE_URL",
        "FRIDAY_EMBEDDINGS_API_KEY",
        "FRIDAY_EMBEDDINGS_MODEL",
        "FRIDAY_EMBEDDINGS_INDEX_BATCH",
        "FRIDAY_EMBEDDINGS_CHUNK_CHARS",
        "FRIDAY_EMBEDDINGS_CHUNK_OVERLAP_CHARS",
        "FRIDAY_EMBEDDINGS_CHUNK_MAX_PER_OBJECT",
        "FRIDAY_EMBEDDINGS_CHUNK_BLEND",
        "FRIDAY_EMBEDDINGS_CHUNK_SCAN_MULTIPLIER",
        "FRIDAY_EMBEDDINGS_MAX_INPUTS_PER_REQUEST",
        "FRIDAY_RETRIEVAL_DENSE_QUERY_BUDGET_SEC",
        "FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN",
        "FRIDAY_RETRIEVAL_POOL_MAX",
        "FRIDAY_RERANK_BASE_URL",
        "FRIDAY_RERANK_MODEL",
        "FRIDAY_RERANK_API_KEY",
        "FRIDAY_RERANK_TIMEOUT_SEC",
        "FRIDAY_RERANK_TOP",
        "FRIDAY_RERANK_CONFIDENT_MIN",
    }
)
_PROCESS_ENV_ALLOWLIST = frozenset(
    {
        "LANG",
        "LC_ALL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "XDG_RUNTIME_DIR",
    }
)
_CONTROL_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_UNIT_RE = re.compile(r"[A-Za-z0-9_.@:-]{1,128}\.service")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROMETHEUS_SAMPLE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{.*\})?\s+"
    r"(?P<value>[^\s]+)(?:\s+[0-9]+)?\s*$"
)

MAX_PRIVATE_JSON_BYTES = 1 << 20
MAX_ENV_BYTES = 1 << 20
MAX_HTTP_BYTES = 2 << 20
MAX_CHILD_OUTPUT_BYTES = 8 << 20
SYSTEMCTL_TIMEOUT_SEC = 20.0
SERVICE_STATE_TIMEOUT_SEC = 45.0
HTTP_TIMEOUT_SEC = 5.0
HTTP_TOTAL_TIMEOUT_SEC = 15.0
BATTERY_TIMEOUT_SEC = 4_000.0
CHILD_TERM_GRACE_SEC = 20.0
CHILD_KILL_GRACE_SEC = 5.0
POLL_INTERVAL_SEC = 0.05
MAX_SIGNAL_DRAIN_ATTEMPTS = 64
_RENAME_NOREPLACE = 1
_SYSTEMD_RUN_BINARY = "/usr/bin/systemd-run"
_SYSTEMCTL_BINARY = "/usr/bin/systemctl"
_GIT_BINARY = "/usr/bin/git"

_PROCESS_CLEANUP_FAILURE_CODES = frozenset(
    {
        "worker_group_kill_sent",
        "worker_group_term_sent",
        "worker_cleanup_exception",
        "worker_leader_not_reaped",
        "worker_process_exception",
        "worker_process_group_not_clear",
        "worker_process_group_survived",
        "worker_timeout",
    }
)
_LIFECYCLE_FAILURE_CODES = frozenset(
    {
        "mcp_cleanup_exception",
        "mcp_cleanup_timeout_warning",
        "server_shutdown_stranded_warning",
    }
)
_OBSERVER_BOOLEAN_FIELDS = (
    "bridge_stopped",
    "bridge_operator_guard_held",
    "backend_healthy",
    "backend_unchanged",
    "outbound_pending_zero",
    "inbound_pending_zero",
    "dead_letter_zero",
    "dispatcher_unchanged",
)


class OperatorFailure(RuntimeError):
    """Closed-code operational failure; its message is safe to publish."""


class OperatorSignal(BaseException):
    """Catchable SIGINT/SIGTERM projection used to enter the same finalizer."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(signal_number)
        self.signal_number = int(signal_number)


@dataclass
class SignalHandlers:
    previous: dict[Any, Any]
    previous_mask: frozenset[Any]
    first_signal: int | None = None
    finalizing: bool = False


@dataclass(frozen=True)
class OperatorConfig:
    freeze_commit: str
    env_file: Path
    barrier_dir: Path
    backend_unit: str
    bridge_unit: str
    report: Path | None = None


@dataclass(frozen=True)
class ServiceFingerprint:
    unit_id: str
    main_pid: int
    invocation_id: str
    nrestarts: int
    exec_started_monotonic: int
    control_group: str
    process_start_ticks: int
    boot_id: str


@dataclass(frozen=True)
class BatteryOutcome:
    returncode: int
    stdout: bytes
    process_group_clear: bool
    cleanup_used: bool


@dataclass
class ExecutionState:
    started_at: float
    stop_armed: bool = False
    start_attempted: bool = False
    bridge_online_after: bool = False
    backend: ServiceFingerprint | None = None
    guard: Any = None
    child: Any = None
    child_outcome: BatteryOutcome | None = None
    battery_report: dict[str, Any] | None = None
    failure_codes: set[str] = field(default_factory=set)
    evidence_hashes: dict[str, str] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)


class RuntimePort(Protocol):
    """Injected operational boundary; tests provide a filesystem-only fake."""

    def monotonic(self) -> float: ...

    def pause(self, seconds: float) -> None: ...

    def revalidate_environment(self) -> None: ...

    def backend_identity(self) -> ServiceFingerprint: ...

    def backend_identity_alive(self, expected: ServiceFingerprint) -> bool: ...

    def bridge_running_identity(self) -> ServiceFingerprint: ...

    def pre_stop_bridge_lease_matches(self, pid: int) -> bool: ...

    def health(self) -> Mapping[str, Any]: ...

    def observer_snapshot(self) -> Mapping[str, Any]: ...

    def dispatcher_epoch(self) -> str: ...

    def stop_bridge(self) -> None: ...

    def bridge_inactive(self, previous: ServiceFingerprint) -> bool: ...

    def acquire_guard(self, owner: ExecutionState) -> Any: ...

    def guard_held(self, boundary: Any) -> bool: ...

    def guarded_queue_snapshot(self, boundary: Any) -> Mapping[str, Any]: ...

    def spawn_battery(self, config: OperatorConfig, owner: ExecutionState) -> Any: ...

    def poll_child(self, child: Any) -> int | None: ...

    def child_contour_alive(self, child: Any) -> bool: ...

    def finish_child(self, child: Any) -> BatteryOutcome: ...

    def cleanup_child(self, child: Any) -> BatteryOutcome: ...

    def release_guard(self, boundary: Any) -> None: ...

    def start_bridge_once(self) -> bool: ...

    def close(self) -> None: ...


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _rename_noreplace(
    source_dir: int,
    source_name: str,
    target_dir: int,
    target_name: str,
) -> None:
    """Atomically publish one Linux-local file without replacing a raced target."""

    try:
        source = os.fsencode(source_name)
        target = os.fsencode(target_name)
    except (TypeError, UnicodeError) as exc:
        raise OperatorFailure("observer_response_write_failed") from exc
    if b"/" in source or b"/" in target or not source or not target:
        raise OperatorFailure("observer_response_write_failed")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise OperatorFailure("atomic_noreplace_unsupported") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(source_dir, source, target_dir, target, _RENAME_NOREPLACE) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise OperatorFailure("observer_response_exists")
        if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise OperatorFailure("atomic_noreplace_unsupported")
        raise OSError(error_number, os.strerror(error_number), target_name)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hash(value: Any, code: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise OperatorFailure(code)
    return value


def _require_bool(payload: Mapping[str, Any], key: str, code: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        raise OperatorFailure(code)
    return bool(value)


def _require_nonnegative_int(payload: Mapping[str, Any], key: str, code: str) -> int:
    value = payload.get(key)
    if type(value) is not int or int(value) < 0:
        raise OperatorFailure(code)
    return int(value)


def _private_regular_identity(status: os.stat_result, *, exact_mode: int = 0o600) -> tuple[int, ...]:
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != exact_mode
        or status.st_nlink != 1
    ):
        raise OperatorFailure("private_file_invalid")
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_uid),
        stat.S_IMODE(status.st_mode),
        int(status.st_nlink),
        int(status.st_size),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _private_directory_identity(status: os.stat_result) -> tuple[int, ...]:
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise OperatorFailure("private_directory_invalid")
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_uid),
        stat.S_IMODE(status.st_mode),
    )


class PinnedPrivateFile:
    """Owner-only regular file pinned by descriptor and lexical identity."""

    def __init__(self, path: Path, *, maximum_bytes: int, invalid_code: str) -> None:
        lexical = Path(os.path.abspath(path.expanduser()))
        if not lexical.is_absolute():
            raise OperatorFailure(invalid_code)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            if lexical.resolve() != lexical:
                raise OperatorFailure(invalid_code)
            before = os.stat(lexical, follow_symlinks=False)
            identity = _private_regular_identity(before)
            descriptor = os.open(lexical, flags)
            opened = _private_regular_identity(os.fstat(descriptor))
            if opened != identity or opened[5] <= 0 or opened[5] > maximum_bytes:
                raise OperatorFailure(invalid_code)
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = _private_regular_identity(os.fstat(descriptor))
            lexical_after = _private_regular_identity(os.stat(lexical, follow_symlinks=False))
            if after != opened or lexical_after != opened or len(content) != opened[5]:
                raise OperatorFailure(invalid_code)
        except OperatorFailure as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if str(exc) == invalid_code:
                raise
            raise OperatorFailure(invalid_code) from exc
        except (OSError, RuntimeError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise OperatorFailure(invalid_code) from exc
        self.path = lexical
        self.descriptor = descriptor
        self.identity = identity
        self.content = content
        self.content_sha256 = _sha256(content)
        self.invalid_code = invalid_code

    def revalidate(self) -> None:
        try:
            opened = _private_regular_identity(os.fstat(self.descriptor))
            lexical = _private_regular_identity(os.stat(self.path, follow_symlinks=False))
            if opened != self.identity or lexical != self.identity or self.path.resolve() != self.path:
                raise OperatorFailure(self.invalid_code)
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = len(self.content) + 1
            while remaining > 0:
                chunk = os.read(self.descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if _sha256(b"".join(chunks)) != self.content_sha256:
                raise OperatorFailure(self.invalid_code)
        except OperatorFailure as exc:
            if str(exc) == self.invalid_code:
                raise
            raise OperatorFailure(self.invalid_code) from exc
        except (OSError, RuntimeError) as exc:
            raise OperatorFailure(self.invalid_code) from exc

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor = self.descriptor
            self.descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> PinnedPrivateFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


class PinnedBarrier:
    """Single-use barrier pinned through parent and directory descriptors."""

    def __init__(self, path: Path) -> None:
        lexical = Path(os.path.abspath(path.expanduser()))
        parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        parent_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = parent_flags
        self.parent_descriptor = -1
        self.descriptor = -1
        try:
            self.parent_descriptor = os.open(lexical.parent, parent_flags)
            parent_status = os.fstat(self.parent_descriptor)
            self.parent_identity = (
                *_private_directory_identity(parent_status),
                int(parent_status.st_mtime_ns),
                int(parent_status.st_ctime_ns),
            )
            self.descriptor = os.open(lexical.name, directory_flags, dir_fd=self.parent_descriptor)
            self.identity = _private_directory_identity(os.fstat(self.descriptor))
            self.path = lexical
            self._revalidate_entries()
            if os.listdir(self.descriptor):
                raise OperatorFailure("barrier_not_empty")
        except BaseException:
            self.close()
            raise

    def _revalidate_entries(self) -> None:
        try:
            if self.descriptor < 0 or self.parent_descriptor < 0:
                raise OperatorFailure("barrier_changed")
            parent_status = os.fstat(self.parent_descriptor)
            current_parent = (
                int(parent_status.st_dev),
                int(parent_status.st_ino),
                int(parent_status.st_uid),
                stat.S_IMODE(parent_status.st_mode),
                int(parent_status.st_mtime_ns),
                int(parent_status.st_ctime_ns),
            )
            if current_parent != self.parent_identity:
                raise OperatorFailure("barrier_parent_changed")
            lexical_parent = os.stat(self.path.parent, follow_symlinks=False)
            lexical_parent_identity = (
                *_private_directory_identity(lexical_parent),
                int(lexical_parent.st_mtime_ns),
                int(lexical_parent.st_ctime_ns),
            )
            if lexical_parent_identity != self.parent_identity:
                raise OperatorFailure("barrier_parent_changed")
            if _private_directory_identity(os.fstat(self.descriptor)) != self.identity:
                raise OperatorFailure("barrier_changed")
            lexical = _private_directory_identity(
                os.stat(self.path.name, dir_fd=self.parent_descriptor, follow_symlinks=False)
            )
            if lexical != self.identity or self.path.resolve() != self.path:
                raise OperatorFailure("barrier_changed")
        except OperatorFailure:
            raise
        except (OSError, RuntimeError) as exc:
            raise OperatorFailure("barrier_changed") from exc

    def names(self) -> set[str]:
        self._revalidate_entries()
        try:
            return set(os.listdir(self.descriptor))
        except OSError as exc:
            raise OperatorFailure("barrier_changed") from exc

    def revalidate(self) -> None:
        self._revalidate_entries()

    def exists(self, name: str) -> bool:
        self._revalidate_entries()
        try:
            os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise OperatorFailure("barrier_changed") from exc
        return True

    def read_canonical_json(self, name: str) -> tuple[dict[str, Any], bytes]:
        self._revalidate_entries()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            before = _private_regular_identity(os.stat(name, dir_fd=self.descriptor, follow_symlinks=False))
            descriptor = os.open(name, flags, dir_fd=self.descriptor)
            opened = _private_regular_identity(os.fstat(descriptor))
            if before != opened or opened[5] <= 0 or opened[5] > MAX_PRIVATE_JSON_BYTES:
                raise OperatorFailure("barrier_file_invalid")
            chunks: list[bytes] = []
            remaining = MAX_PRIVATE_JSON_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = _private_regular_identity(os.fstat(descriptor))
            lexical_after = _private_regular_identity(
                os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            )
            if after != opened or lexical_after != opened or len(raw) != opened[5]:
                raise OperatorFailure("barrier_file_changed")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict) or raw != _canonical_json(parsed) + b"\n":
                raise OperatorFailure("barrier_file_not_canonical")
            return parsed, raw
        except OperatorFailure:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorFailure("barrier_file_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def atomic_write_json(self, name: str, payload: Mapping[str, Any]) -> bytes:
        self._revalidate_entries()
        if self.exists(name):
            raise OperatorFailure("observer_response_exists")
        temporary = f".{name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        encoded = _canonical_json(payload) + b"\n"
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=self.descriptor)
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short observer write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            _rename_noreplace(
                self.descriptor,
                temporary,
                self.descriptor,
                name,
            )
            os.fsync(self.descriptor)
            parsed, reread = self.read_canonical_json(name)
            if parsed != dict(payload) or reread != encoded:
                raise OperatorFailure("observer_response_changed")
            return encoded
        except OperatorFailure:
            raise
        except OSError as exc:
            raise OperatorFailure("observer_response_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor = self.descriptor
            self.descriptor = -1
            os.close(descriptor)
        if self.parent_descriptor >= 0:
            descriptor = self.parent_descriptor
            self.parent_descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> PinnedBarrier:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


def _validate_run_receipt(
    payload: Mapping[str, Any],
    *,
    commit: str,
    run_index: int,
) -> None:
    exact_keys = {
        "schema",
        "commit",
        "run_id_hash",
        "run_index",
        "worker_report_sha256",
        "worker_status",
        "worker_exit_code",
        "worker_reaped",
        "process_group_clear_initial",
        "process_group_clear",
        "process_cleanup_failure_codes",
        "lifecycle_contract_clear",
        "lifecycle_teardown_clear",
        "lifecycle_failure_codes",
        "teardown_clear",
    }
    process_codes = payload.get("process_cleanup_failure_codes")
    lifecycle_codes = payload.get("lifecycle_failure_codes")
    if (
        set(payload) != exact_keys
        or payload.get("schema") != RUN_RECEIPT_SCHEMA
        or payload.get("commit") != commit
        or payload.get("run_index") != run_index
        or _HEX64_RE.fullmatch(str(payload.get("run_id_hash") or "")) is None
        or _HEX64_RE.fullmatch(str(payload.get("worker_report_sha256") or "")) is None
        or payload.get("worker_status") != "passed"
        or type(payload.get("worker_exit_code")) is not int
        or payload.get("worker_exit_code") != 0
        or any(
            type(payload.get(key)) is not bool
            for key in (
                "worker_reaped",
                "process_group_clear_initial",
                "process_group_clear",
                "lifecycle_contract_clear",
                "lifecycle_teardown_clear",
                "teardown_clear",
            )
        )
        or not isinstance(process_codes, list)
        or process_codes != sorted(set(process_codes))
        or any(code not in _PROCESS_CLEANUP_FAILURE_CODES for code in process_codes)
        or not isinstance(lifecycle_codes, list)
        or lifecycle_codes != sorted(set(lifecycle_codes))
        or any(code not in _LIFECYCLE_FAILURE_CODES for code in lifecycle_codes)
    ):
        raise OperatorFailure("run_receipt_invalid")
    expected_clear = bool(
        payload["worker_exit_code"] == 0
        and payload["worker_reaped"]
        and payload["process_group_clear_initial"]
        and payload["process_group_clear"]
        and not process_codes
        and payload["lifecycle_contract_clear"]
        and payload["lifecycle_teardown_clear"]
        and not lifecycle_codes
    )
    if payload.get("teardown_clear") is not expected_clear or not expected_clear:
        raise OperatorFailure("run_receipt_not_clear")


def _validate_observer_request(
    request: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_bytes: bytes,
    *,
    commit: str,
) -> None:
    exact_keys = {
        "schema",
        "commit",
        "run_id_hash",
        "run_index",
        "run_receipt_sha256",
        "worker_report_sha256",
        "challenge",
    }
    if (
        set(request) != exact_keys
        or request.get("schema") != OBSERVER_REQUEST_SCHEMA
        or request.get("commit") != commit
        or request.get("run_index") != 1
        or request.get("run_id_hash") != receipt.get("run_id_hash")
        or request.get("worker_report_sha256") != receipt.get("worker_report_sha256")
        or request.get("run_receipt_sha256") != _sha256(receipt_bytes)
        or _HEX64_RE.fullmatch(str(request.get("run_id_hash") or "")) is None
        or _HEX64_RE.fullmatch(str(request.get("run_receipt_sha256") or "")) is None
        or _HEX64_RE.fullmatch(str(request.get("worker_report_sha256") or "")) is None
        or _HEX64_RE.fullmatch(str(request.get("challenge") or "")) is None
    ):
        raise OperatorFailure("observer_request_invalid")
    distinct = {
        str(request["run_id_hash"]),
        str(request["run_receipt_sha256"]),
        str(request["worker_report_sha256"]),
        str(request["challenge"]),
    }
    if len(distinct) != 4:
        raise OperatorFailure("observer_request_invalid")


def _validate_stopped_snapshot(payload: Mapping[str, Any], backend_pid: int) -> None:
    exact_keys = {
        "schema",
        "backend_pid",
        "backend_lease_owned",
        "physical_outbound_pending",
        "bridge_queue_state",
        "bridge_lease_acquired_for_snapshot",
        "bridge_lease_released",
        "inbound_pending",
        "dead_letter",
    }
    if set(payload) != exact_keys or payload.get("schema") != OBSERVER_SNAPSHOT_SCHEMA:
        raise OperatorFailure("stopped_snapshot_invalid")
    if (
        type(payload.get("backend_pid")) is not int
        or payload.get("backend_pid") != backend_pid
        or _require_bool(payload, "backend_lease_owned", "stopped_snapshot_invalid") is not True
        or payload.get("bridge_queue_state") != "present"
        or _require_bool(
            payload,
            "bridge_lease_acquired_for_snapshot",
            "stopped_snapshot_invalid",
        )
        is not True
        or _require_bool(payload, "bridge_lease_released", "stopped_snapshot_invalid") is not True
        or _require_nonnegative_int(
            payload,
            "physical_outbound_pending",
            "stopped_snapshot_invalid",
        )
        != 0
        or _require_nonnegative_int(payload, "inbound_pending", "stopped_snapshot_invalid") != 0
        or _require_nonnegative_int(payload, "dead_letter", "stopped_snapshot_invalid") != 0
    ):
        raise OperatorFailure("stopped_snapshot_not_clear")


def _validate_held_snapshot(payload: Mapping[str, Any], backend_pid: int) -> None:
    exact_keys = {
        "schema",
        "backend_pid",
        "backend_lease_owned",
        "physical_outbound_pending",
        "bridge_queue_state",
        "bridge_lease_acquired_for_snapshot",
        "bridge_lease_released",
        "inbound_pending",
        "dead_letter",
    }
    if set(payload) != exact_keys or payload.get("schema") != OBSERVER_SNAPSHOT_SCHEMA:
        raise OperatorFailure("held_snapshot_invalid")
    if (
        type(payload.get("backend_pid")) is not int
        or payload.get("backend_pid") != backend_pid
        or _require_bool(payload, "backend_lease_owned", "held_snapshot_invalid") is not True
        or payload.get("bridge_queue_state") != "active_uninspected"
        or _require_bool(payload, "bridge_lease_acquired_for_snapshot", "held_snapshot_invalid") is not False
        or _require_bool(payload, "bridge_lease_released", "held_snapshot_invalid") is not False
        or payload.get("inbound_pending") is not None
        or payload.get("dead_letter") is not None
        or _require_nonnegative_int(
            payload,
            "physical_outbound_pending",
            "held_snapshot_invalid",
        )
        != 0
    ):
        raise OperatorFailure("held_snapshot_not_clear")


def _validate_active_snapshot(payload: Mapping[str, Any], backend_pid: int) -> None:
    """Pre-stop snapshot: bridge is active, but physical outbound must be empty."""

    _validate_held_snapshot(payload, backend_pid)


def _validate_guarded_queue(payload: Mapping[str, Any]) -> None:
    exact = {
        "schema",
        "bridge_guard_held",
        "bridge_queue_state",
        "inbound_pending",
        "dead_letter",
    }
    if (
        set(payload) != exact
        or payload.get("schema") != GUARDED_QUEUE_SCHEMA
        or _require_bool(payload, "bridge_guard_held", "guarded_queue_invalid") is not True
        or payload.get("bridge_queue_state") != "present"
        or _require_nonnegative_int(payload, "inbound_pending", "guarded_queue_invalid") != 0
        or _require_nonnegative_int(payload, "dead_letter", "guarded_queue_invalid") != 0
    ):
        raise OperatorFailure("guarded_queue_not_clear")


def _validate_health(payload: Mapping[str, Any]) -> None:
    if payload.get("status") != "ok":
        raise OperatorFailure("backend_health_not_clear")


def _observer_response(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": OBSERVER_RESPONSE_SCHEMA,
        "commit": request["commit"],
        "run_id_hash": request["run_id_hash"],
        "run_index": request["run_index"],
        "run_receipt_sha256": request["run_receipt_sha256"],
        "worker_report_sha256": request["worker_report_sha256"],
        "challenge": request["challenge"],
        "status": "passed",
        **{key: True for key in _OBSERVER_BOOLEAN_FIELDS},
    }


def _validate_d10_diagnostics(payload: Any) -> None:
    if not isinstance(payload, Mapping) or set(payload) != {"subturns"}:
        raise OperatorFailure("battery_report_case_mismatch")
    subturns = payload.get("subturns")
    if not isinstance(subturns, Mapping) or set(subturns) != {"metadata", "regular", "mcp"}:
        raise OperatorFailure("battery_report_case_mismatch")
    attempt_keys = {
        "llm_chat_attempts",
        "late_make_file_attempts",
        "workspace_create_kernel_attempts",
        "workspace_create_mcp_attempts",
    }
    for name in ("metadata", "regular", "mcp"):
        projection = subturns.get(name)
        expected = {
            "duration_ms",
            "http_returned",
            "llm_failed",
            "files_count",
            "tools_count",
            "attempts",
        }
        if name != "metadata":
            expected.add("reply_ref_bound_before")
        attempts = projection.get("attempts") if isinstance(projection, Mapping) else None
        if (
            not isinstance(projection, Mapping)
            or set(projection) != expected
            or type(projection.get("duration_ms")) is not int
            or int(projection["duration_ms"]) < 0
            or type(projection.get("http_returned")) is not bool
            or projection.get("http_returned") is not True
            or type(projection.get("llm_failed")) is not bool
            or projection.get("llm_failed") is not False
            or type(projection.get("files_count")) is not int
            or int(projection["files_count"]) < 0
            or type(projection.get("tools_count")) is not int
            or int(projection["tools_count"]) < 0
            or not isinstance(attempts, Mapping)
            or set(attempts) != attempt_keys
            or any(type(value) is not int or int(value) < 0 for value in attempts.values())
            or (
                name != "metadata"
                and (
                    type(projection.get("reply_ref_bound_before")) is not bool
                    or projection.get("reply_ref_bound_before") is not True
                )
            )
        ):
            raise OperatorFailure("battery_report_case_mismatch")


def _validate_battery_report(
    payload: Mapping[str, Any],
    *,
    commit: str,
    response_sha256: str,
    receipt_hashes: Mapping[int, str],
    receipt_payloads: Mapping[int, Mapping[str, Any]],
) -> None:
    receipts = payload.get("run_receipts")
    observer = payload.get("inter_run_observer")
    runs = payload.get("runs")
    first_receipt = receipt_payloads.get(1, {})
    exact_top_keys = {
        "schema",
        "commit",
        "run_id_hash",
        "runs_expected",
        "runs_completed",
        "cases_expected_per_run",
        "failure_codes",
        "status",
        "run_receipts",
        "inter_run_observer",
        "runs",
    }
    if (
        set(payload) != exact_top_keys
        or payload.get("schema") != BATTERY_REPORT_SCHEMA
        or payload.get("commit") != commit
        or payload.get("run_id_hash") != first_receipt.get("run_id_hash")
        or _HEX64_RE.fullmatch(str(payload.get("run_id_hash") or "")) is None
        or payload.get("status") != "passed"
        or payload.get("runs_expected") != 2
        or payload.get("runs_completed") != 2
        or payload.get("cases_expected_per_run") != len(BATTERY_CASE_IDS)
        or payload.get("failure_codes") != []
        or not isinstance(receipts, list)
        or len(receipts) != 2
        or not isinstance(observer, Mapping)
        or not isinstance(runs, list)
        or len(runs) != 2
    ):
        raise OperatorFailure("battery_report_invalid")
    for index, item in enumerate(receipts, start=1):
        receipt = receipt_payloads.get(index, {})
        if (
            not isinstance(item, Mapping)
            or set(item) != {"run_index", "sha256", "worker_report_sha256", "teardown_clear"}
            or item.get("run_index") != index
            or item.get("sha256") != receipt_hashes.get(index)
            or item.get("worker_report_sha256") != receipt.get("worker_report_sha256")
            or item.get("teardown_clear") is not True
            or _HEX64_RE.fullmatch(str(item.get("worker_report_sha256") or "")) is None
        ):
            raise OperatorFailure("battery_report_receipt_mismatch")
    expected_observer_keys = {
        "schema",
        "status",
        "run_index",
        "run_receipt_sha256",
        "worker_report_sha256",
        "response_sha256",
        *_OBSERVER_BOOLEAN_FIELDS,
    }
    if (
        set(observer) != expected_observer_keys
        or observer.get("schema") != OBSERVER_RESPONSE_SCHEMA
        or observer.get("status") != "passed"
        or observer.get("run_index") != 1
        or observer.get("run_receipt_sha256") != receipt_hashes.get(1)
        or observer.get("worker_report_sha256") != first_receipt.get("worker_report_sha256")
        or observer.get("response_sha256") != response_sha256
        or any(observer.get(key) is not True for key in _OBSERVER_BOOLEAN_FIELDS)
    ):
        raise OperatorFailure("battery_report_observer_mismatch")
    for index, run in enumerate(runs, start=1):
        receipt = receipt_payloads.get(index, {})
        if not isinstance(run, Mapping):
            raise OperatorFailure("battery_report_run_mismatch")
        expected_run_keys = {
            "schema",
            "run_index",
            "run_id_hash",
            "status",
            "failure_codes",
            "lifecycle_teardown_clear",
            "lifecycle_failure_codes",
            "duration_ms",
            "cases",
            "teardown",
        }
        teardown = run.get("teardown")
        cases = run.get("cases")
        if (
            set(run) != expected_run_keys
            or run.get("schema") != BATTERY_WORKER_SCHEMA
            or run.get("run_index") != index
            or run.get("run_id_hash") != first_receipt.get("run_id_hash")
            or run.get("status") != "passed"
            or run.get("failure_codes") != []
            or run.get("lifecycle_teardown_clear") is not True
            or run.get("lifecycle_failure_codes") != []
            or type(run.get("duration_ms")) is not int
            or int(run["duration_ms"]) < 0
            or not isinstance(teardown, Mapping)
            or not isinstance(cases, list)
            or len(cases) != len(BATTERY_CASE_IDS)
            or _sha256(_canonical_json(run)) != receipt.get("worker_report_sha256")
        ):
            raise OperatorFailure("battery_report_run_mismatch")
        expected_teardown = {
            "worker_report_identity_clear",
            "worker_exit_code",
            "worker_reaped",
            "process_group_clear_initial",
            "process_group_clear",
            "process_cleanup_failure_codes",
            "lifecycle_contract_clear",
            "lifecycle_teardown_clear",
            "lifecycle_failure_codes",
            "teardown_clear",
        }
        if (
            set(teardown) != expected_teardown
            or teardown.get("worker_report_identity_clear") is not True
            or type(teardown.get("worker_exit_code")) is not int
            or teardown.get("worker_exit_code") != 0
            or any(
                teardown.get(key) is not True
                for key in (
                    "worker_reaped",
                    "process_group_clear_initial",
                    "process_group_clear",
                    "lifecycle_contract_clear",
                    "lifecycle_teardown_clear",
                    "teardown_clear",
                )
            )
            or teardown.get("process_cleanup_failure_codes") != []
            or teardown.get("lifecycle_failure_codes") != []
        ):
            raise OperatorFailure("battery_report_run_mismatch")
        observed_case_ids: list[str] = []
        for case in cases:
            if not isinstance(case, Mapping):
                raise OperatorFailure("battery_report_case_mismatch")
            case_id = str(case.get("case_id") or "")
            checks = case.get("checks")
            counters = case.get("counters")
            expected_case_keys = {
                "case_id",
                "status",
                "failure_codes",
                "duration_ms",
                "checks",
                "counters",
                "fresh_database",
            }
            if case_id == "D10":
                expected_case_keys.add("diagnostics")
            if (
                set(case) != expected_case_keys
                or case.get("status") != "passed"
                or case.get("failure_codes") != []
                or type(case.get("duration_ms")) is not int
                or int(case["duration_ms"]) < 0
                or case.get("fresh_database") is not True
                or not isinstance(checks, Mapping)
                or not checks
                or any(value is not True for value in checks.values())
                or not isinstance(counters, Mapping)
                or any(type(value) is not int or int(value) < 0 for value in counters.values())
            ):
                raise OperatorFailure("battery_report_case_mismatch")
            if case_id == "D10":
                _validate_d10_diagnostics(case.get("diagnostics"))
            observed_case_ids.append(case_id)
        if tuple(observed_case_ids) != BATTERY_CASE_IDS:
            raise OperatorFailure("battery_report_case_mismatch")


def _parse_env(content: bytes) -> dict[str, str]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise OperatorFailure("env_file_invalid") from exc
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or _ENV_KEY_RE.fullmatch(key) is None or key in values:
            raise OperatorFailure("env_file_invalid")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    for key, value in values.items():
        if not key.startswith("FRIDAY_"):
            continue
        legacy = f"JERICHO_{key.removeprefix('FRIDAY_')}"
        if legacy in values and values[legacy] != value:
            raise OperatorFailure("env_alias_conflict")
    return values


def _load_settings_from_values(values: Mapping[str, str]) -> Any:
    from friday.config import load_settings, validate_settings

    relevant = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("FRIDAY_") or key.startswith("JERICHO_")
    }
    try:
        for key in relevant:
            os.environ.pop(key, None)
        os.environ.update(
            {
                key: value
                for key, value in values.items()
                if key.startswith("FRIDAY_") or key.startswith("JERICHO_")
            }
        )
        # The already pinned bytes above are the only configuration authority.
        os.environ["FRIDAY_ENV_FILE"] = "/dev/null"
        settings = load_settings()
        errors = [
            item
            for item in validate_settings(settings, production=not settings.is_loopback_bind)
            if not item.startswith("warning:")
        ]
        if errors:
            raise OperatorFailure("runtime_settings_invalid")
        return settings
    except OperatorFailure:
        raise
    except Exception as exc:
        raise OperatorFailure("runtime_settings_invalid") from exc
    finally:
        for key in list(os.environ):
            if key.startswith("FRIDAY_") or key.startswith("JERICHO_"):
                os.environ.pop(key, None)
        os.environ.update(relevant)


def _model_environment(settings: Any) -> dict[str, str]:
    values = {
        "FRIDAY_PROFILE": str(settings.profile.name),
        "FRIDAY_LLM_BASE_URL": str(settings.llm_base_url),
        "FRIDAY_LLM_MODEL": str(settings.llm_model),
        "FRIDAY_LLM_API_KEY": str(settings.llm_api_key),
        "FRIDAY_LLM_TIMEOUT_SEC": str(settings.llm_timeout_sec),
        "FRIDAY_LLM_MAX_TOKENS": str(settings.llm_max_tokens),
        "FRIDAY_LLM_FOREGROUND_SLOTS": str(settings.llm_foreground_slots),
        "FRIDAY_EMBEDDINGS_ENABLED": "1" if settings.embeddings_enabled else "0",
        "FRIDAY_EMBEDDINGS_BASE_URL": str(settings.embeddings_base_url),
        "FRIDAY_EMBEDDINGS_API_KEY": str(settings.embeddings_api_key),
        "FRIDAY_EMBEDDINGS_MODEL": str(settings.embeddings_model),
        "FRIDAY_EMBEDDINGS_INDEX_BATCH": str(settings.embeddings_index_batch),
        "FRIDAY_EMBEDDINGS_CHUNK_CHARS": str(settings.embeddings_chunk_chars),
        "FRIDAY_EMBEDDINGS_CHUNK_OVERLAP_CHARS": str(settings.embeddings_chunk_overlap_chars),
        "FRIDAY_EMBEDDINGS_CHUNK_MAX_PER_OBJECT": str(settings.embeddings_chunk_max_per_object),
        "FRIDAY_EMBEDDINGS_CHUNK_BLEND": str(settings.embeddings_chunk_blend),
        "FRIDAY_EMBEDDINGS_CHUNK_SCAN_MULTIPLIER": str(settings.embeddings_chunk_scan_multiplier),
        "FRIDAY_EMBEDDINGS_MAX_INPUTS_PER_REQUEST": str(settings.embeddings_max_inputs_per_request),
        "FRIDAY_RETRIEVAL_DENSE_QUERY_BUDGET_SEC": str(settings.retrieval_dense_query_budget_sec),
        "FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN": str(settings.retrieval_dense_evidence_min),
        "FRIDAY_RETRIEVAL_POOL_MAX": str(settings.retrieval_pool_max),
        "FRIDAY_RERANK_BASE_URL": str(settings.rerank_base_url),
        "FRIDAY_RERANK_MODEL": str(settings.rerank_model),
        "FRIDAY_RERANK_API_KEY": str(settings.rerank_api_key),
        "FRIDAY_RERANK_TIMEOUT_SEC": str(settings.rerank_timeout_sec),
        "FRIDAY_RERANK_TOP": str(settings.rerank_top),
        "FRIDAY_RERANK_CONFIDENT_MIN": str(settings.rerank_confident_min),
    }
    if set(values) != _MODEL_ENV_ALLOWLIST:
        raise OperatorFailure("model_environment_contract_invalid")
    return values


def _require_numeric_local_url(value: str, *, code: str, allow_private: bool) -> str:
    parsed = urlsplit(value)
    host = str(parsed.hostname or "")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OperatorFailure(code)
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise OperatorFailure(code) from exc
    if not address.is_loopback and not (allow_private and address.is_private):
        raise OperatorFailure(code)
    return value.rstrip("/")


def _backend_base_url(settings: Any) -> str:
    host = str(settings.api_host).strip().casefold()
    if host in {"0.0.0.0", "127.0.0.1", "localhost"}:
        target = "127.0.0.1"
    elif host in {"::", "::1"}:
        target = "[::1]"
    else:
        raise OperatorFailure("backend_numeric_loopback_unavailable")
    scheme = "https" if settings.api_tls_enabled else "http"
    return f"{scheme}://{target}:{int(settings.api_port)}"


def _user_runtime_directory() -> Path:
    raw = str(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.geteuid()}")
    lexical = Path(os.path.abspath(raw))
    try:
        metadata = os.stat(lexical, follow_symlinks=False)
    except OSError as exc:
        raise OperatorFailure("user_systemd_runtime_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or lexical.resolve() != lexical
    ):
        raise OperatorFailure("user_systemd_runtime_invalid")
    return lexical


def _tls_context(settings: Any) -> ssl.SSLContext | bool:
    if not settings.api_tls_enabled:
        return True
    ca_value = str(settings.backend_ca_file or settings.ssl_certfile or "").strip()
    if not ca_value:
        raise OperatorFailure("backend_tls_ca_missing")
    try:
        return ssl.create_default_context(cafile=ca_value)
    except (OSError, ssl.SSLError) as exc:
        raise OperatorFailure("backend_tls_ca_invalid") from exc


def _parse_dispatcher_epoch(body: bytes) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeError as exc:
        raise OperatorFailure("dispatcher_metrics_invalid") from exc
    epochs: list[str] = []
    for line in text.splitlines():
        match = _PROMETHEUS_SAMPLE_RE.fullmatch(line.strip())
        if match is None or match.group("name") != "process_start_time_seconds":
            continue
        value = match.group("value")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise OperatorFailure("dispatcher_metrics_invalid") from exc
        if not number.is_finite() or number <= 0:
            raise OperatorFailure("dispatcher_metrics_invalid")
        epochs.append(value)
    if len(epochs) != 1:
        raise OperatorFailure("dispatcher_metrics_epoch_missing")
    # Hashing this normalized numeric identity prevents accidental public output
    # of host timing data while retaining exact equality semantics.
    return _sha256(str(Decimal(epochs[0]).normalize()).encode("ascii"))


def _bounded_response_body(
    response: httpx.Response,
    *,
    maximum_bytes: int,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    iterator = iter(response.iter_bytes())
    while True:
        if monotonic() >= deadline:
            raise OperatorFailure("http_response_deadline_exceeded")
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        if monotonic() >= deadline:
            raise OperatorFailure("http_response_deadline_exceeded")
        observed += len(chunk)
        if observed > maximum_bytes:
            raise OperatorFailure("http_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_systemctl_show(raw: bytes, *, expected_unit: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise OperatorFailure("systemd_state_invalid") from exc
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in parsed:
            raise OperatorFailure("systemd_state_invalid")
        parsed[key] = value
    exact = {
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "ControlPID",
        "InvocationID",
        "NRestarts",
        "ExecMainStartTimestampMonotonic",
        "ControlGroup",
    }
    if set(parsed) != exact or parsed.get("Id") != expected_unit or parsed.get("LoadState") != "loaded":
        raise OperatorFailure("systemd_state_invalid")
    return parsed


def _parse_scope_show(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise OperatorFailure("battery_scope_invalid") from exc
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in parsed:
            raise OperatorFailure("battery_scope_invalid")
        parsed[key] = value
    if set(parsed) != {
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "ControlGroup",
        "KillMode",
    }:
        raise OperatorFailure("battery_scope_invalid")
    return parsed


def _cgroup_populated(
    control_group: str,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> bool:
    if not control_group.startswith("/") or "\x00" in control_group:
        raise OperatorFailure("cgroup_identity_invalid")
    try:
        root = cgroup_root.resolve(strict=True)
        path = (root / control_group.lstrip("/")).resolve(strict=False)
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OperatorFailure("cgroup_identity_invalid") from exc
    try:
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(metadata.st_mode):
            raise OperatorFailure("cgroup_state_unavailable")
        events = (path / "cgroup.events").read_text(encoding="ascii")
        populated_values: list[str] = []
        for line in events.splitlines():
            pieces = line.split()
            if len(pieces) != 2:
                raise OperatorFailure("cgroup_state_unavailable")
            if pieces[0] == "populated":
                populated_values.append(pieces[1])
        if len(populated_values) != 1 or populated_values[0] not in {"0", "1"}:
            raise OperatorFailure("cgroup_state_unavailable")
        for name in ("cgroup.procs", "cgroup.threads"):
            target = path / name
            if not target.is_file():
                raise OperatorFailure("cgroup_state_unavailable")
            if target.read_text(encoding="ascii").strip():
                return True
        return populated_values[0] == "1"
    except FileNotFoundError:
        # A cgroup may disappear between the existence test and the reads only
        # after its final member has left it.
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise OperatorFailure("cgroup_state_unavailable") from exc
        raise OperatorFailure("cgroup_state_unavailable") from None
    except OperatorFailure:
        raise
    except (OSError, UnicodeError) as exc:
        raise OperatorFailure("cgroup_state_unavailable") from exc


def _read_process_start_ticks(pid: int) -> int:
    if pid <= 0:
        raise OperatorFailure("process_identity_invalid")
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise OperatorFailure("process_identity_invalid") from exc
    close = content.rfind(")")
    if close < 0:
        raise OperatorFailure("process_identity_invalid")
    fields = content[close + 2 :].split()
    try:
        # The first post-comm field is field 3; starttime is field 22.
        value = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise OperatorFailure("process_identity_invalid") from exc
    if value <= 0:
        raise OperatorFailure("process_identity_invalid")
    return value


def _read_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise OperatorFailure("boot_identity_unavailable") from exc
    if re.fullmatch(r"[0-9a-fA-F-]{36}", value) is None:
        raise OperatorFailure("boot_identity_unavailable")
    return value.casefold()


def _pidfd_open(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        raise OperatorFailure("pidfd_unsupported")
    try:
        return int(opener(pid, 0))
    except OSError as exc:
        raise OperatorFailure("pidfd_open_failed") from exc


def _pidfd_alive(descriptor: int) -> bool:
    try:
        readable, _writable, _exceptional = select.select([descriptor], [], [], 0)
    except (OSError, ValueError) as exc:
        raise OperatorFailure("pidfd_check_failed") from exc
    return not readable


@dataclass
class LiveBatteryProcess:
    process: subprocess.Popen[bytes]
    stdout_file: Any
    stderr_file: Any
    process_group: int
    scope_unit: str
    scope_control_group: str
    pidfd: int
    finished: bool = False


class LiveRuntime:
    """Real Linux/systemd/HTTP boundary.  Tests never instantiate this class."""

    _SHOW_PROPERTIES = (
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "ControlPID",
        "InvocationID",
        "NRestarts",
        "ExecMainStartTimestampMonotonic",
        "ControlGroup",
    )

    def __init__(
        self,
        settings: Any,
        model_environment: Mapping[str, str],
        pinned_environment: PinnedPrivateFile,
        *,
        backend_unit: str,
        bridge_unit: str,
    ) -> None:
        self.settings = settings
        self.model_environment = dict(model_environment)
        self.pinned_environment = pinned_environment
        self.backend_unit = backend_unit
        self.bridge_unit = bridge_unit
        self.user_runtime_directory = _user_runtime_directory()
        self.backend_url = _backend_base_url(settings)
        self.model_url = _require_numeric_local_url(
            str(settings.llm_base_url),
            code="dispatcher_url_invalid",
            allow_private=True,
        )
        if not str(settings.api_token):
            raise OperatorFailure("owner_api_token_missing")
        self._backend_client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.api_token}", "Accept": "application/json"},
            verify=_tls_context(settings),
            trust_env=False,
            follow_redirects=False,
        )
        model_headers = {"Accept": "text/plain"}
        if settings.llm_api_key:
            model_headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._dispatcher_client = httpx.Client(
            headers=model_headers,
            verify=True,
            trust_env=False,
            follow_redirects=False,
        )
        self._backend_pidfd = -1
        self._bridge_pidfd = -1
        self._backend_baseline: ServiceFingerprint | None = None
        self._bridge_baseline: ServiceFingerprint | None = None

    def monotonic(self) -> float:
        return time.monotonic()

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)

    def revalidate_environment(self) -> None:
        self.pinned_environment.revalidate()

    def _run_command(
        self,
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = {key: value for key, value in os.environ.items() if key in _PROCESS_ENV_ALLOWLIST}
        environment["XDG_RUNTIME_DIR"] = str(self.user_runtime_directory)
        try:
            completed = subprocess.run(  # noqa: S603 - exact argv, no shell
                list(command),
                check=False,
                capture_output=True,
                env=environment,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperatorFailure("systemctl_command_failed") from exc
        if len(completed.stdout) > 65_536 or len(completed.stderr) > 65_536:
            raise OperatorFailure("systemctl_output_too_large")
        return completed

    def _service_state(self, unit: str) -> dict[str, str]:
        command = [_SYSTEMCTL_BINARY, "--user", "show", "--no-pager"]
        command.extend(f"--property={name}" for name in self._SHOW_PROPERTIES)
        command.append(unit)
        completed = self._run_command(command, timeout=SYSTEMCTL_TIMEOUT_SEC)
        if completed.returncode != 0:
            raise OperatorFailure("systemd_state_unavailable")
        return _parse_systemctl_show(completed.stdout, expected_unit=unit)

    def _active_fingerprint(self, unit: str) -> ServiceFingerprint:
        state = self._service_state(unit)
        try:
            main_pid = int(state["MainPID"])
            nrestarts = int(state["NRestarts"])
            exec_started = int(state["ExecMainStartTimestampMonotonic"])
        except (KeyError, ValueError) as exc:
            raise OperatorFailure("service_identity_invalid") from exc
        invocation = state.get("InvocationID", "")
        control_group = state.get("ControlGroup", "")
        if (
            state.get("ActiveState") != "active"
            or state.get("SubState") != "running"
            or main_pid <= 0
            or nrestarts < 0
            or exec_started <= 0
            or re.fullmatch(r"[0-9a-fA-F]{32}", invocation) is None
            or not control_group.startswith("/")
        ):
            raise OperatorFailure("service_not_running")
        return ServiceFingerprint(
            unit_id=unit,
            main_pid=main_pid,
            invocation_id=invocation.casefold(),
            nrestarts=nrestarts,
            exec_started_monotonic=exec_started,
            control_group=control_group,
            process_start_ticks=_read_process_start_ticks(main_pid),
            boot_id=_read_boot_id(),
        )

    def backend_identity(self) -> ServiceFingerprint:
        identity = self._active_fingerprint(self.backend_unit)
        if self._backend_baseline is None:
            previous_mask = _block_control_signals()
            try:
                self._backend_pidfd = _pidfd_open(identity.main_pid)
                self._backend_baseline = identity
            finally:
                _restore_signal_mask(previous_mask)
        return identity

    def backend_identity_alive(self, expected: ServiceFingerprint) -> bool:
        return bool(
            self._backend_pidfd >= 0
            and _pidfd_alive(self._backend_pidfd)
            and self._active_fingerprint(self.backend_unit) == expected
        )

    def bridge_running_identity(self) -> ServiceFingerprint:
        identity = self._active_fingerprint(self.bridge_unit)
        if self._bridge_baseline is None:
            previous_mask = _block_control_signals()
            try:
                self._bridge_pidfd = _pidfd_open(identity.main_pid)
                self._bridge_baseline = identity
            finally:
                _restore_signal_mask(previous_mask)
        return identity

    def pre_stop_bridge_lease_matches(self, pid: int) -> bool:
        path = self.settings.state_dir / "telegram-inbox.sqlite3.lock"
        inspected = inspect_process_lease(path, protocol="friday.telegram-bridge.v1")
        return bool(
            inspected.get("active") is True
            and inspected.get("protocol_matches") is True
            and inspected.get("recorded_protocol") == "friday.telegram-bridge.v1"
            and inspected.get("pid") == pid
        )

    def _get_json(self, path: str) -> Mapping[str, Any]:
        deadline = self.monotonic() + HTTP_TOTAL_TIMEOUT_SEC
        try:
            with self._backend_client.stream(
                "GET",
                f"{self.backend_url}{path}",
                timeout=HTTP_TIMEOUT_SEC,
            ) as response:
                if response.status_code != 200:
                    raise OperatorFailure("backend_http_status_failed")
                body = _bounded_response_body(
                    response,
                    maximum_bytes=MAX_HTTP_BYTES,
                    deadline=deadline,
                    monotonic=self.monotonic,
                )
            parsed = json.loads(body.decode("utf-8"))
        except OperatorFailure:
            raise
        except (httpx.HTTPError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorFailure("backend_http_failed") from exc
        if not isinstance(parsed, Mapping):
            raise OperatorFailure("backend_http_response_invalid")
        return parsed

    def health(self) -> Mapping[str, Any]:
        return self._get_json("/api/health")

    def observer_snapshot(self) -> Mapping[str, Any]:
        return self._get_json("/api/admin/document-contour-observer-snapshot")

    def dispatcher_epoch(self) -> str:
        parsed = urlsplit(self.model_url)
        metrics_url = urlunsplit((parsed.scheme, parsed.netloc, "/metrics", "", ""))
        deadline = self.monotonic() + HTTP_TOTAL_TIMEOUT_SEC
        try:
            with self._dispatcher_client.stream(
                "GET",
                metrics_url,
                timeout=HTTP_TIMEOUT_SEC,
            ) as response:
                if response.status_code != 200:
                    raise OperatorFailure("dispatcher_metrics_status_failed")
                body = _bounded_response_body(
                    response,
                    maximum_bytes=MAX_HTTP_BYTES,
                    deadline=deadline,
                    monotonic=self.monotonic,
                )
        except OperatorFailure:
            raise
        except httpx.HTTPError as exc:
            raise OperatorFailure("dispatcher_metrics_failed") from exc
        return _parse_dispatcher_epoch(body)

    def stop_bridge(self) -> None:
        completed = self._run_command(
            [_SYSTEMCTL_BINARY, "--user", "stop", self.bridge_unit],
            timeout=SYSTEMCTL_TIMEOUT_SEC,
        )
        if completed.returncode != 0:
            raise OperatorFailure("bridge_stop_failed")

    @staticmethod
    def _cgroup_empty(control_group: str) -> bool:
        return not _cgroup_populated(control_group)

    def bridge_inactive(self, previous: ServiceFingerprint) -> bool:
        deadline = self.monotonic() + SERVICE_STATE_TIMEOUT_SEC
        while True:
            state = self._service_state(self.bridge_unit)
            try:
                main_pid = int(state["MainPID"])
                control_pid = int(state["ControlPID"])
            except (KeyError, ValueError) as exc:
                raise OperatorFailure("bridge_state_invalid") from exc
            old_exited = self._bridge_pidfd >= 0 and not _pidfd_alive(self._bridge_pidfd)
            clear = bool(
                state.get("ActiveState") == "inactive"
                and state.get("SubState") == "dead"
                and main_pid == 0
                and control_pid == 0
                and old_exited
                and self._cgroup_empty(previous.control_group)
            )
            if clear:
                return True
            if self.monotonic() >= deadline:
                return False
            self.pause(POLL_INTERVAL_SEC)

    def acquire_guard(self, owner: ExecutionState) -> ProcessLease:
        path = self.settings.state_dir / "telegram-inbox.sqlite3.lock"
        boundary = ProcessLease(path, protocol="friday.telegram-bridge.v1")
        try:
            boundary.acquire()
            if not self.guard_held(boundary):
                raise OperatorFailure("bridge_guard_not_held")
            # Bind ownership before returning.  A process-directed signal can
            # otherwise be projected by Python between RETURN_VALUE and the
            # caller's STORE_ATTR even while this thread's POSIX mask is set.
            owner.guard = boundary
        except BaseException as exc:
            with suppress(BaseException):
                boundary.release()
            if owner.guard is boundary:
                owner.guard = None
            if isinstance(exc, OperatorFailure):
                raise
            if isinstance(exc, (OSError, RuntimeError)):
                raise OperatorFailure("bridge_guard_acquire_failed") from exc
            raise
        return boundary

    def guard_held(self, boundary: Any) -> bool:
        path = self.settings.state_dir / "telegram-inbox.sqlite3.lock"
        try:
            lexical = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        return bool(
            isinstance(boundary, ProcessLease)
            and boundary.path == path
            and boundary.protocol == "friday.telegram-bridge.v1"
            and boundary.acquired
            and process_owns_lease(path, protocol="friday.telegram-bridge.v1")
            and boundary.held_file_identity == (int(lexical.st_dev), int(lexical.st_ino))
        )

    def guarded_queue_snapshot(self, boundary: Any) -> Mapping[str, Any]:
        if not isinstance(boundary, ProcessLease):
            raise OperatorFailure("bridge_guard_invalid")
        try:
            return collect_document_contour_guarded_bridge_queue_snapshot(self.settings, boundary)
        except Exception as exc:
            raise OperatorFailure("guarded_queue_snapshot_failed") from exc

    def spawn_battery(
        self,
        config: OperatorConfig,
        owner: ExecutionState,
    ) -> LiveBatteryProcess:
        stdout_file: Any = None
        stderr_file: Any = None
        process: subprocess.Popen[bytes] | None = None
        child: LiveBatteryProcess | None = None
        try:
            stdout_file = tempfile.TemporaryFile(  # noqa: SIM115 - child lifecycle owns it
                mode="w+b"
            )
            stderr_file = tempfile.TemporaryFile(  # noqa: SIM115 - child lifecycle owns it
                mode="w+b"
            )
            environment = {key: value for key, value in os.environ.items() if key in _PROCESS_ENV_ALLOWLIST}
            environment["XDG_RUNTIME_DIR"] = str(self.user_runtime_directory)
            environment.update(self.model_environment)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            battery_command = [
                sys.executable,
                "-B",
                str(ROOT / "tools/document_contour_live_battery.py"),
                "--run-live",
                "--freeze-commit",
                config.freeze_commit,
                "--operator-model-env-only",
                "--bridge-stopped",
                "--inter-run-barrier-dir",
                str(config.barrier_dir),
            ]
            scope_unit = f"friday-document-contour-{os.getpid()}-{secrets.token_hex(6)}.scope"
            command = [
                _SYSTEMD_RUN_BINARY,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={scope_unit}",
                "--property=KillMode=control-group",
                "--expand-environment=no",
                "--",
                *battery_command,
            ]
            process = subprocess.Popen(  # noqa: S603 - exact immutable candidate argv
                command,
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            child = LiveBatteryProcess(
                process=process,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
                process_group=process.pid,
                scope_unit=scope_unit,
                scope_control_group="",
                pidfd=-1,
            )
        except BaseException as exc:
            if process is not None:
                emergency = child or LiveBatteryProcess(
                    process=process,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                    process_group=process.pid,
                    scope_unit=scope_unit,
                    scope_control_group="",
                    pidfd=-1,
                )
                try:
                    outcome = self.cleanup_child(emergency)
                except BaseException as cleanup_exc:
                    raise OperatorFailure("battery_spawn_cleanup_not_clear") from cleanup_exc
                if not outcome.process_group_clear:
                    raise OperatorFailure("battery_spawn_cleanup_not_clear") from exc
            else:
                if stdout_file is not None:
                    with suppress(BaseException):
                        stdout_file.close()
                if stderr_file is not None:
                    with suppress(BaseException):
                        stderr_file.close()
            raise
        assert child is not None
        try:
            child.pidfd = _pidfd_open(process.pid)
            child.scope_control_group = self._wait_scope_control_group(scope_unit, process)
            if (
                process.poll() is not None
                or not _pidfd_alive(child.pidfd)
                or not _cgroup_populated(child.scope_control_group)
            ):
                raise OperatorFailure("battery_scope_unavailable")
        except BaseException as exc:
            try:
                outcome = self.cleanup_child(child)
            except BaseException as cleanup_exc:
                raise OperatorFailure("battery_spawn_cleanup_not_clear") from cleanup_exc
            if not outcome.process_group_clear:
                raise OperatorFailure("battery_spawn_cleanup_not_clear") from exc
            raise
        try:
            # The same owner-handoff closes the child RETURN_VALUE/STORE_ATTR
            # gap.  From this assignment onward the outer finalizer can always
            # reach the exact controller and its transient scope.
            owner.child = child
            return child
        except BaseException as exc:
            try:
                owner.child_outcome = self.cleanup_child(child)
            except BaseException as cleanup_exc:
                raise OperatorFailure("battery_spawn_cleanup_not_clear") from cleanup_exc
            if not owner.child_outcome.process_group_clear:
                raise OperatorFailure("battery_spawn_cleanup_not_clear") from exc
            raise

    def _wait_scope_control_group(
        self,
        scope_unit: str,
        process: subprocess.Popen[bytes],
    ) -> str:
        deadline = self.monotonic() + 5.0
        while True:
            completed = self._run_command(
                [
                    _SYSTEMCTL_BINARY,
                    "--user",
                    "show",
                    "--no-pager",
                    "--property=Id",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=ControlGroup",
                    "--property=KillMode",
                    scope_unit,
                ],
                timeout=SYSTEMCTL_TIMEOUT_SEC,
            )
            if completed.returncode == 0:
                parsed = _parse_scope_show(completed.stdout)
                control_group = str(parsed.get("ControlGroup") or "")
                if (
                    parsed.get("Id") == scope_unit
                    and parsed.get("LoadState") == "loaded"
                    and parsed.get("ActiveState") == "active"
                    and parsed.get("SubState") == "running"
                    and parsed.get("KillMode") == "control-group"
                    and control_group.startswith("/")
                ):
                    return control_group
            if process.poll() is not None or self.monotonic() >= deadline:
                raise OperatorFailure("battery_scope_unavailable")
            self.pause(POLL_INTERVAL_SEC)

    def poll_child(self, child: LiveBatteryProcess) -> int | None:
        return child.process.poll()

    def child_contour_alive(self, child: LiveBatteryProcess) -> bool:
        try:
            return bool(
                not child.finished
                and child.process.poll() is None
                and child.pidfd >= 0
                and _pidfd_alive(child.pidfd)
                and _cgroup_populated(child.scope_control_group)
            )
        except OperatorFailure:
            return False

    @staticmethod
    def _read_child_stdout(child: LiveBatteryProcess) -> bytes:
        child.stdout_file.flush()
        child.stdout_file.seek(0)
        output = child.stdout_file.read(MAX_CHILD_OUTPUT_BYTES + 1)
        if len(output) > MAX_CHILD_OUTPUT_BYTES:
            raise OperatorFailure("battery_output_too_large")
        return bytes(output)

    def finish_child(self, child: LiveBatteryProcess) -> BatteryOutcome:
        if child.finished:
            raise OperatorFailure("battery_child_already_finished")
        returncode = child.process.poll()
        if returncode is None:
            raise OperatorFailure("battery_child_still_running")
        clear = self._cgroup_empty(child.scope_control_group) and not _pidfd_alive(child.pidfd)
        cleanup_used = False
        if not clear:
            cleanup_used = True
            outcome = self.cleanup_child(child)
            return BatteryOutcome(
                returncode=returncode,
                stdout=outcome.stdout,
                process_group_clear=outcome.process_group_clear,
                cleanup_used=True,
            )
        stdout = self._read_child_stdout(child)
        child.finished = True
        os.close(child.pidfd)
        child.pidfd = -1
        child.stdout_file.close()
        child.stderr_file.close()
        return BatteryOutcome(returncode, stdout, True, cleanup_used)

    def _signal_scope_once(self, child: LiveBatteryProcess, selected: signal.Signals) -> bool:
        try:
            completed = self._run_command(
                [
                    _SYSTEMCTL_BINARY,
                    "--user",
                    "kill",
                    "--kill-whom=all",
                    f"--signal={selected.name}",
                    child.scope_unit,
                ],
                timeout=SYSTEMCTL_TIMEOUT_SEC,
            )
        except BaseException:
            return False
        return completed.returncode == 0

    def _wait_scope_empty(self, child: LiveBatteryProcess, timeout: float) -> bool:
        try:
            deadline = self.monotonic() + timeout
            while True:
                if self._cgroup_empty(child.scope_control_group):
                    return True
                if self.monotonic() >= deadline:
                    return False
                self.pause(POLL_INTERVAL_SEC)
        except BaseException:
            return False

    def cleanup_child(self, child: LiveBatteryProcess) -> BatteryOutcome:
        if child.finished:
            return BatteryOutcome(
                int(child.process.returncode if child.process.returncode is not None else -1),
                b"",
                True,
                False,
            )
        cleanup_used = True
        if not child.scope_control_group:
            with suppress(BaseException):
                child.scope_control_group = self._wait_scope_control_group(
                    child.scope_unit,
                    child.process,
                )
        self._signal_scope_once(child, signal.SIGTERM)
        term_scope_clear = bool(
            child.scope_control_group and self._wait_scope_empty(child, CHILD_TERM_GRACE_SEC)
        )
        leader_reaped = False
        if term_scope_clear:
            try:
                child.process.wait(timeout=CHILD_KILL_GRACE_SEC)
                leader_reaped = child.process.returncode is not None
            except BaseException:
                leader_reaped = False

        escalated = bool(not term_scope_clear or not leader_reaped)
        if escalated:
            # A cgroup-empty result does not prove that the synchronous
            # systemd-run leader was reaped.  Any leader wait uncertainty uses
            # the same single escalation as a populated scope: KILL every
            # scope member and the controller PGID, then make a second bounded
            # wait before the final descriptor/cgroup audit.
            self._signal_scope_once(child, signal.SIGKILL)
            with suppress(BaseException):
                os.killpg(child.process_group, signal.SIGKILL)
            kill_scope_clear = bool(
                child.scope_control_group and self._wait_scope_empty(child, CHILD_KILL_GRACE_SEC)
            )
            try:
                child.process.wait(timeout=CHILD_KILL_GRACE_SEC)
                leader_reaped = child.process.returncode is not None
            except BaseException:
                leader_reaped = False
            clear = bool(kill_scope_clear and leader_reaped)
        else:
            clear = True
        try:
            if not child.scope_control_group or not self._cgroup_empty(child.scope_control_group):
                clear = False
        except BaseException:
            clear = False
        if child.pidfd < 0:
            clear = False
        else:
            try:
                if _pidfd_alive(child.pidfd):
                    clear = False
            except BaseException:
                clear = False
            finally:
                with suppress(OSError):
                    os.close(child.pidfd)
                child.pidfd = -1
        try:
            stdout = self._read_child_stdout(child)
        except BaseException:
            stdout = b""
            clear = False
        child.finished = True
        with suppress(BaseException):
            child.stdout_file.close()
        with suppress(BaseException):
            child.stderr_file.close()
        return BatteryOutcome(
            int(child.process.returncode if child.process.returncode is not None else -1),
            stdout,
            clear,
            cleanup_used,
        )

    def release_guard(self, boundary: Any) -> None:
        if isinstance(boundary, ProcessLease):
            boundary.release()

    def start_bridge_once(self) -> bool:
        completed = self._run_command(
            [_SYSTEMCTL_BINARY, "--user", "--no-block", "start", self.bridge_unit],
            timeout=SYSTEMCTL_TIMEOUT_SEC,
        )
        if completed.returncode != 0:
            return False
        deadline = self.monotonic() + SERVICE_STATE_TIMEOUT_SEC
        lease_path = self.settings.state_dir / "telegram-inbox.sqlite3.lock"
        while True:
            try:
                identity = self._active_fingerprint(self.bridge_unit)
                inspected = inspect_process_lease(
                    lease_path,
                    protocol="friday.telegram-bridge.v1",
                )
                if (
                    inspected.get("active") is True
                    and inspected.get("protocol_matches") is True
                    and inspected.get("recorded_protocol") == "friday.telegram-bridge.v1"
                    and inspected.get("pid") == identity.main_pid
                ):
                    return True
            except OperatorFailure:
                pass
            if self.monotonic() >= deadline:
                return False
            self.pause(POLL_INTERVAL_SEC)

    def close(self) -> None:
        failed = False
        for client in (self._backend_client, self._dispatcher_client):
            try:
                client.close()
            except BaseException:
                failed = True
        for attribute in ("_backend_pidfd", "_bridge_pidfd"):
            descriptor = int(getattr(self, attribute, -1))
            setattr(self, attribute, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException:
                    failed = True
        if failed:
            raise OperatorFailure("runtime_close_failed")


def _git_output(*args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if key in {"LANG", "LC_ALL"}}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed Git binary and arguments
            [_GIT_BINARY, "-c", "core.fsmonitor=false", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorFailure("candidate_git_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 1 << 20:
        raise OperatorFailure("candidate_git_failed")
    return completed.stdout.strip()


def _validate_candidate(commit: str) -> None:
    if _HEX40_RE.fullmatch(commit) is None:
        raise OperatorFailure("freeze_commit_invalid")
    friday_origin = Path(str(_friday_package.__file__ or "")).resolve()
    if not friday_origin.is_relative_to(ROOT):
        raise OperatorFailure("package_origin_invalid")
    if _git_output("rev-parse", "HEAD") != commit:
        raise OperatorFailure("freeze_commit_is_not_head")
    if _git_output("rev-parse", f"{commit}^{{commit}}") != commit:
        raise OperatorFailure("freeze_commit_invalid")
    if _git_output("status", "--porcelain=v1", "--untracked-files=all"):
        raise OperatorFailure("release_worktree_is_dirty")
    for relative, expected in _EXPECTED_DEPENDENCY_HASHES.items():
        if _file_sha256(ROOT / relative) != expected:
            raise OperatorFailure("frozen_dependency_changed")


def _validate_unit_name(value: str) -> str:
    if _UNIT_RE.fullmatch(value) is None or value.startswith("-") or "/" in value:
        raise OperatorFailure("systemd_unit_invalid")
    return value


def _backend_attestation(
    runtime: RuntimePort,
    expected: ServiceFingerprint,
    *,
    held: bool,
) -> None:
    if not runtime.backend_identity_alive(expected):
        raise OperatorFailure("backend_identity_changed")
    before = runtime.backend_identity()
    if before != expected:
        raise OperatorFailure("backend_identity_changed")
    _validate_health(runtime.health())
    snapshot = runtime.observer_snapshot()
    if held:
        _validate_held_snapshot(snapshot, expected.main_pid)
    else:
        _validate_stopped_snapshot(snapshot, expected.main_pid)
    after = runtime.backend_identity()
    if after != expected or not runtime.backend_identity_alive(expected):
        raise OperatorFailure("backend_identity_changed")


def _held_barrier_attestation(
    runtime: RuntimePort,
    state: ExecutionState,
    backend: ServiceFingerprint,
    bridge: ServiceFingerprint,
    dispatcher_epoch: str,
) -> None:
    if state.guard is None or not runtime.guard_held(state.guard):
        raise OperatorFailure("bridge_guard_lost")
    if not runtime.bridge_inactive(bridge):
        raise OperatorFailure("bridge_became_active")
    first_queue = runtime.guarded_queue_snapshot(state.guard)
    _validate_guarded_queue(first_queue)
    if runtime.backend_identity() != backend or not runtime.backend_identity_alive(backend):
        raise OperatorFailure("backend_identity_changed")
    _validate_health(runtime.health())
    _validate_held_snapshot(runtime.observer_snapshot(), backend.main_pid)
    if runtime.dispatcher_epoch() != dispatcher_epoch:
        raise OperatorFailure("dispatcher_epoch_changed")
    if runtime.backend_identity() != backend or not runtime.backend_identity_alive(backend):
        raise OperatorFailure("backend_identity_changed")
    second_queue = runtime.guarded_queue_snapshot(state.guard)
    _validate_guarded_queue(second_queue)
    if dict(first_queue) != dict(second_queue):
        raise OperatorFailure("guarded_queue_changed")
    if not runtime.guard_held(state.guard) or not runtime.bridge_inactive(bridge):
        raise OperatorFailure("bridge_guard_lost")


def _wait_for_request_or_child(
    runtime: RuntimePort,
    state: ExecutionState,
    barrier: PinnedBarrier,
    *,
    deadline: float,
) -> bool:
    if state.child is None:
        raise OperatorFailure("battery_child_missing")
    while True:
        if barrier.exists("run-1-observer-request.json"):
            if runtime.poll_child(state.child) is not None:
                raise OperatorFailure("battery_exited_before_observer")
            return True
        if runtime.poll_child(state.child) is not None:
            return False
        if runtime.monotonic() >= deadline:
            raise OperatorFailure("battery_deadline_exhausted")
        runtime.pause(POLL_INTERVAL_SEC)


def _wait_for_child_exit(
    runtime: RuntimePort,
    state: ExecutionState,
    *,
    deadline: float,
) -> None:
    if state.child is None:
        raise OperatorFailure("battery_child_missing")
    while runtime.poll_child(state.child) is None:
        if runtime.monotonic() >= deadline:
            raise OperatorFailure("battery_deadline_exhausted")
        runtime.pause(POLL_INTERVAL_SEC)
    state.child_outcome = runtime.finish_child(state.child)
    if (
        state.child_outcome.returncode != 0
        or not state.child_outcome.process_group_clear
        or state.child_outcome.cleanup_used
    ):
        raise OperatorFailure("battery_process_failed")


def _parse_battery_stdout(outcome: BatteryOutcome) -> dict[str, Any]:
    try:
        parsed = json.loads(outcome.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorFailure("battery_output_invalid") from exc
    expected = json.dumps(parsed, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    if not isinstance(parsed, dict) or outcome.stdout != expected:
        raise OperatorFailure("battery_output_invalid")
    return parsed


def _workflow(
    config: OperatorConfig,
    runtime: RuntimePort,
    barrier: PinnedBarrier,
    state: ExecutionState,
) -> None:
    runtime.revalidate_environment()
    backend = runtime.backend_identity()
    state.backend = backend
    if not runtime.backend_identity_alive(backend):
        raise OperatorFailure("backend_identity_changed")
    bridge = runtime.bridge_running_identity()
    if not runtime.pre_stop_bridge_lease_matches(bridge.main_pid):
        raise OperatorFailure("bridge_service_lease_mismatch")
    _validate_health(runtime.health())
    _validate_active_snapshot(runtime.observer_snapshot(), backend.main_pid)
    if runtime.backend_identity() != backend or not runtime.backend_identity_alive(backend):
        raise OperatorFailure("backend_identity_changed")
    dispatcher_epoch = runtime.dispatcher_epoch()
    state.checks["preflight_clear"] = True

    # From this assignment onward every catchable exit must make exactly one
    # bounded start attempt.  It intentionally precedes the stop command.
    state.stop_armed = True
    runtime.stop_bridge()
    if not runtime.bridge_inactive(bridge):
        raise OperatorFailure("bridge_not_inactive")
    runtime.revalidate_environment()
    _backend_attestation(runtime, backend, held=False)
    if runtime.dispatcher_epoch() != dispatcher_epoch:
        raise OperatorFailure("dispatcher_epoch_changed")
    state.checks["bridge_stopped_clear"] = True

    previous_mask = _block_control_signals()
    try:
        runtime.acquire_guard(state)
    finally:
        _restore_signal_mask(previous_mask)
    if not runtime.guard_held(state.guard):
        raise OperatorFailure("bridge_guard_not_held")
    _held_barrier_attestation(runtime, state, backend, bridge, dispatcher_epoch)
    state.checks["bridge_guard_clear"] = True

    runtime.revalidate_environment()
    # The runtime binds the process handle into ``state`` before returning, and
    # the outer mask covers that owner handoff.  The canonical controller then
    # explicitly unblocks the inherited mask while installing its own handlers.
    previous_mask = _block_control_signals()
    try:
        runtime.spawn_battery(config, state)
    finally:
        _restore_signal_mask(previous_mask)
    deadline = runtime.monotonic() + BATTERY_TIMEOUT_SEC
    request_seen = _wait_for_request_or_child(
        runtime,
        state,
        barrier,
        deadline=deadline,
    )
    if not request_seen:
        _wait_for_child_exit(runtime, state, deadline=deadline)
        raise OperatorFailure("battery_stopped_before_observer")

    expected_during_barrier = {
        "run-1-receipt.json",
        "run-1-observer-request.json",
    }
    if barrier.names() != expected_during_barrier:
        raise OperatorFailure("barrier_contents_invalid")
    receipt_1, receipt_1_bytes = barrier.read_canonical_json("run-1-receipt.json")
    request, request_bytes = barrier.read_canonical_json("run-1-observer-request.json")
    _validate_run_receipt(receipt_1, commit=config.freeze_commit, run_index=1)
    _validate_observer_request(
        request,
        receipt_1,
        receipt_1_bytes,
        commit=config.freeze_commit,
    )
    state.evidence_hashes["run_1_receipt_sha256"] = _sha256(receipt_1_bytes)
    state.evidence_hashes["observer_request_sha256"] = _sha256(request_bytes)

    runtime.revalidate_environment()
    _held_barrier_attestation(runtime, state, backend, bridge, dispatcher_epoch)
    reread_receipt, reread_receipt_bytes = barrier.read_canonical_json("run-1-receipt.json")
    reread_request, reread_request_bytes = barrier.read_canonical_json("run-1-observer-request.json")
    if (
        reread_receipt != receipt_1
        or reread_receipt_bytes != receipt_1_bytes
        or reread_request != request
        or reread_request_bytes != request_bytes
    ):
        raise OperatorFailure("barrier_binding_changed")
    if state.child is None or not runtime.child_contour_alive(state.child):
        raise OperatorFailure("battery_exited_before_observer")

    response = _observer_response(request)
    response_bytes = barrier.atomic_write_json("run-1-observer.json", response)
    response_sha256 = _sha256(response_bytes)
    state.evidence_hashes["observer_response_sha256"] = response_sha256
    if not runtime.guard_held(state.guard):
        raise OperatorFailure("bridge_guard_lost")
    state.checks["inter_run_observer_clear"] = True

    _wait_for_child_exit(runtime, state, deadline=deadline)
    barrier.revalidate()
    if state.child_outcome is None:
        raise OperatorFailure("battery_outcome_missing")
    battery_report = _parse_battery_stdout(state.child_outcome)

    expected_after = {
        "run-1-receipt.json",
        "run-1-observer-request.json",
        "run-1-observer.json",
        "run-2-receipt.json",
    }
    if barrier.names() != expected_after:
        raise OperatorFailure("barrier_final_contents_invalid")
    receipt_2, receipt_2_bytes = barrier.read_canonical_json("run-2-receipt.json")
    _validate_run_receipt(receipt_2, commit=config.freeze_commit, run_index=2)
    if receipt_2.get("run_id_hash") != receipt_1.get("run_id_hash"):
        raise OperatorFailure("run_receipt_binding_mismatch")
    response_reread, response_reread_bytes = barrier.read_canonical_json("run-1-observer.json")
    if response_reread != response or response_reread_bytes != response_bytes:
        raise OperatorFailure("observer_response_changed")
    request_final, request_final_bytes = barrier.read_canonical_json("run-1-observer-request.json")
    receipt_1_final, receipt_1_final_bytes = barrier.read_canonical_json("run-1-receipt.json")
    if (
        request_final != request
        or request_final_bytes != request_bytes
        or receipt_1_final != receipt_1
        or receipt_1_final_bytes != receipt_1_bytes
    ):
        raise OperatorFailure("barrier_binding_changed")
    state.evidence_hashes["run_2_receipt_sha256"] = _sha256(receipt_2_bytes)
    state.evidence_hashes["battery_report_sha256"] = _sha256(state.child_outcome.stdout)
    _validate_battery_report(
        battery_report,
        commit=config.freeze_commit,
        response_sha256=response_sha256,
        receipt_hashes={1: _sha256(receipt_1_bytes), 2: _sha256(receipt_2_bytes)},
        receipt_payloads={1: receipt_1, 2: receipt_2},
    )

    runtime.revalidate_environment()
    _held_barrier_attestation(runtime, state, backend, bridge, dispatcher_epoch)
    barrier.revalidate()
    state.battery_report = battery_report
    state.checks["battery_clear"] = True
    state.checks["backend_unchanged"] = True
    state.checks["dispatcher_unchanged"] = True


def _cleanup_and_restart(runtime: RuntimePort, state: ExecutionState) -> None:
    if state.child is not None and state.child_outcome is None:
        try:
            state.child_outcome = runtime.cleanup_child(state.child)
            if not state.child_outcome.process_group_clear or state.child_outcome.returncode == -1:
                state.failure_codes.add("battery_cleanup_not_clear")
        except BaseException:
            state.failure_codes.add("battery_cleanup_exception")
    if state.guard is not None:
        try:
            runtime.release_guard(state.guard)
        except BaseException:
            state.failure_codes.add("bridge_guard_release_failed")
        state.guard = None
    if state.stop_armed and not state.start_attempted:
        # Set before invoking the external boundary: an exception or signal at
        # any later bytecode position must never schedule a second start.
        state.start_attempted = True
        try:
            state.bridge_online_after = runtime.start_bridge_once()
        except BaseException:
            state.bridge_online_after = False
        if not state.bridge_online_after:
            state.failure_codes.add("bridge_start_not_confirmed")
        if state.backend is not None:
            try:
                backend_unchanged = runtime.backend_identity_alive(state.backend)
            except BaseException:
                backend_unchanged = False
            state.checks["backend_unchanged"] = bool(
                state.checks.get("backend_unchanged") and backend_unchanged
            )
            if not backend_unchanged:
                state.failure_codes.add("backend_identity_changed")
    try:
        runtime.close()
    except BaseException:
        state.failure_codes.add("runtime_close_failed")


def _closed_exception_code(exc: BaseException) -> str:
    if isinstance(exc, OperatorFailure):
        return str(exc)
    if isinstance(exc, OperatorSignal):
        return "interrupted_sigint" if exc.signal_number == signal.SIGINT else "interrupted_sigterm"
    return "operator_baseexception" if not isinstance(exc, Exception) else "operator_exception"


def _build_public_report(
    config: OperatorConfig,
    state: ExecutionState,
    *,
    signal_number: int | None,
) -> dict[str, Any]:
    checks = {
        "preflight_clear": bool(state.checks.get("preflight_clear")),
        "bridge_stopped_clear": bool(state.checks.get("bridge_stopped_clear")),
        "bridge_guard_clear": bool(state.checks.get("bridge_guard_clear")),
        "inter_run_observer_clear": bool(state.checks.get("inter_run_observer_clear")),
        "battery_clear": bool(state.checks.get("battery_clear")),
        "backend_unchanged": bool(state.checks.get("backend_unchanged")),
        "dispatcher_unchanged": bool(state.checks.get("dispatcher_unchanged")),
        "bridge_start_attempted": state.start_attempted,
        "bridge_online_after": state.bridge_online_after,
    }
    clean = bool(
        not state.failure_codes
        and state.battery_report is not None
        and all(checks.values())
        and signal_number is None
    )
    return {
        "schema": OPERATOR_SCHEMA,
        "status": "passed" if clean else "failed",
        "commit": config.freeze_commit,
        "duration_ms": max(0, round((time.monotonic() - state.started_at) * 1000)),
        "failure_codes": sorted(state.failure_codes),
        "signal": (signal.Signals(signal_number).name if signal_number in _CONTROL_SIGNALS else None),
        "checks": checks,
        "evidence_sha256": dict(sorted(state.evidence_hashes.items())),
    }


def execute_operator(
    config: OperatorConfig,
    runtime: RuntimePort,
    barrier: PinnedBarrier,
    *,
    signal_state: SignalHandlers | None = None,
) -> tuple[dict[str, Any], int]:
    state = ExecutionState(started_at=time.monotonic())
    primary: BaseException | None = None
    try:
        _workflow(config, runtime, barrier, state)
        if signal_state is not None:
            # Close the successful-return/finalizer boundary while this call is
            # still inside the exception contour.  A first signal is therefore
            # caught below or stays pending for the bounded finalizer.
            _block_control_signals()
    except BaseException as exc:
        primary = exc
        state.failure_codes.add(_closed_exception_code(exc))
        if signal_state is not None:
            try:
                _block_control_signals()
            except OperatorSignal as deferred:
                state.failure_codes.add(_closed_exception_code(deferred))
            except BaseException:
                state.failure_codes.add("signal_lifecycle_failed")
    finally:

        def cleanup() -> None:
            _cleanup_and_restart(runtime, state)

        if signal_state is None:
            cleanup()
        else:
            _finalize_signal_handlers(signal_state, cleanup)
    if signal_state is not None and signal_state.first_signal is not None:
        state.failure_codes.add(_closed_exception_code(OperatorSignal(int(signal_state.first_signal))))
    signal_number = (
        signal_state.first_signal
        if signal_state is not None and signal_state.first_signal is not None
        else (primary.signal_number if isinstance(primary, OperatorSignal) else None)
    )
    report = _build_public_report(config, state, signal_number=signal_number)
    if signal_number is not None:
        return report, 128 + int(signal_number)
    return report, 0 if report["status"] == "passed" else 1


def _require_posix_signal_lifecycle() -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "killpg")
        or not hasattr(signal, "pthread_sigmask")
        or not hasattr(signal, "SIG_BLOCK")
        or not hasattr(signal, "SIG_SETMASK")
        or not hasattr(signal, "sigpending")
        or not hasattr(signal, "sigtimedwait")
    ):
        raise OperatorFailure("signal_lifecycle_unsupported")


def _block_control_signals() -> frozenset[Any]:
    _require_posix_signal_lifecycle()
    return frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, _CONTROL_SIGNALS))


def _restore_signal_mask(previous: frozenset[Any]) -> None:
    signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _drain_pending_control_signals() -> None:
    targets = frozenset(_CONTROL_SIGNALS)
    for _attempt in range(MAX_SIGNAL_DRAIN_ATTEMPTS):
        pending = targets.intersection(signal.sigpending())
        if not pending:
            return
        # A process-directed signal can be consumed by another runtime thread
        # between sigpending() and the wait.  A zero-timeout consume keeps the
        # finalizer bounded while draining pending repeats up to the fixed cap.
        if signal.sigtimedwait(pending, 0) is None:
            return


def _take_pending_control_signal() -> int | None:
    pending = frozenset(_CONTROL_SIGNALS).intersection(signal.sigpending())
    if not pending:
        return None
    observed = signal.sigtimedwait(pending, 0)
    return int(observed.si_signo) if observed is not None else None


def _install_signal_handlers() -> SignalHandlers:
    inherited_mask = _block_control_signals()
    # A launcher may use a blocked mask as a spawn transport detail.  Once our
    # handlers are installed, this process must explicitly own INT+TERM while
    # preserving every unrelated inherited signal block.
    previous_mask = frozenset(item for item in inherited_mask if item not in _CONTROL_SIGNALS)
    state = SignalHandlers(previous={}, previous_mask=previous_mask)

    def interrupt(signal_number: int, _frame: Any) -> None:
        if state.first_signal is not None:
            return
        state.first_signal = int(signal_number)
        _block_control_signals()
        if state.finalizing:
            return
        raise OperatorSignal(signal_number)

    try:
        for selected in _CONTROL_SIGNALS:
            state.previous[selected] = signal.getsignal(selected)
            signal.signal(selected, interrupt)
    except BaseException:
        for restored_signal, handler in state.previous.items():
            signal.signal(restored_signal, handler)
        _restore_signal_mask(inherited_mask)
        raise
    return state


def _activate_signal_handlers(state: SignalHandlers) -> None:
    _restore_signal_mask(state.previous_mask)


def _finalize_signal_handlers(state: SignalHandlers, cleanup: Callable[[], None]) -> None:
    state.finalizing = True
    try:
        try:
            _block_control_signals()
        except OperatorSignal:
            # The handler records the first signal and blocks the complete set
            # before raising, so cleanup remains protected.
            pass
        except BaseException:
            # This interface was proven before the bridge stop.  Even if it
            # later fails, do not skip the one restoration attempt.
            cleanup()
            raise
        try:
            cleanup()
        finally:
            # A first signal can arrive after the workflow has returned but
            # while restoration is blocked.  Consume and project it before
            # restoring the old dispositions; otherwise cleanup could finish
            # while the public result falsely remained green.
            if state.first_signal is None:
                state.first_signal = _take_pending_control_signal()
            try:
                _drain_pending_control_signals()
                for selected, handler in state.previous.items():
                    signal.signal(selected, handler)
            finally:
                try:
                    _drain_pending_control_signals()
                finally:
                    _restore_signal_mask(state.previous_mask)
    finally:
        state.finalizing = False


def _atomic_private_report(path: Path, payload: Mapping[str, Any]) -> None:
    lexical = Path(os.path.abspath(path.expanduser()))
    parent_descriptor = -1
    descriptor = -1
    temporary = f".{lexical.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    encoded = _canonical_json(payload) + b"\n"
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = os.open(lexical.parent, flags)
        parent_identity = _private_directory_identity(os.fstat(parent_descriptor))
        if (
            _private_directory_identity(os.stat(lexical.parent, follow_symlinks=False)) != parent_identity
            or lexical.parent.resolve() != lexical.parent
        ):
            raise OperatorFailure("report_path_invalid")
        try:
            os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OperatorFailure("report_path_exists")
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, file_flags, 0o600, dir_fd=parent_descriptor)
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "short report write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _rename_noreplace(parent_descriptor, temporary, parent_descriptor, lexical.name)
        os.fsync(parent_descriptor)
        metadata = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        identity = _private_regular_identity(metadata)
        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        read_flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lexical.name, read_flags, dir_fd=parent_descriptor)
        if _private_regular_identity(os.fstat(descriptor)) != identity:
            raise OperatorFailure("report_write_failed")
        chunks: list[bytes] = []
        remaining = len(encoded) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = _private_regular_identity(os.fstat(descriptor))
        lexical_after = _private_regular_identity(
            os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        )
        if after != identity or lexical_after != identity or b"".join(chunks) != encoded:
            raise OperatorFailure("report_write_failed")
        os.close(descriptor)
        descriptor = -1
        if (
            _private_directory_identity(os.stat(lexical.parent, follow_symlinks=False)) != parent_identity
            or lexical.parent.resolve() != lexical.parent
        ):
            raise OperatorFailure("report_path_invalid")
    except OperatorFailure:
        raise
    except (OSError, RuntimeError) as exc:
        raise OperatorFailure("report_write_failed") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if parent_descriptor >= 0:
            with suppress(FileNotFoundError, OSError):
                os.unlink(temporary, dir_fd=parent_descriptor)
            with suppress(OSError):
                os.close(parent_descriptor)


def _build_runtime(config: OperatorConfig, pinned_env: PinnedPrivateFile) -> LiveRuntime:
    values = _parse_env(pinned_env.content)
    settings = _load_settings_from_values(values)
    return LiveRuntime(
        settings,
        _model_environment(settings),
        pinned_env,
        backend_unit=config.backend_unit,
        bridge_unit=config.bridge_unit,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", help="authorize the one live contour")
    parser.add_argument("--freeze-commit", required=True, help="exact immutable 40-hex commit")
    parser.add_argument("--env-file", required=True, help="absolute owner-only deployed env file")
    parser.add_argument(
        "--inter-run-barrier-dir",
        required=True,
        help="pre-created empty owner-only 0700 single-use directory",
    )
    parser.add_argument("--backend-unit", required=True, help="exact systemd --user backend unit")
    parser.add_argument("--bridge-unit", required=True, help="exact systemd --user bridge unit")
    parser.add_argument("--report", default="", help="optional sanitized owner-only JSON report")
    return parser


def _config_from_args(args: argparse.Namespace) -> OperatorConfig:
    if not bool(args.run_live):
        raise OperatorFailure("run_live_authorization_required")
    commit = str(args.freeze_commit or "").strip().casefold()
    env_input = Path(str(args.env_file)).expanduser()
    barrier_input = Path(str(args.inter_run_barrier_dir)).expanduser()
    report_value = str(args.report or "").strip()
    report_input = Path(report_value).expanduser() if report_value else None
    if (
        not env_input.is_absolute()
        or not barrier_input.is_absolute()
        or (report_input is not None and not report_input.is_absolute())
    ):
        raise OperatorFailure("operator_path_not_absolute")
    env_path = Path(os.path.abspath(env_input))
    barrier_path = Path(os.path.abspath(barrier_input))
    report = Path(os.path.abspath(report_input)) if report_input is not None else None
    if report is not None and (report == barrier_path or report.is_relative_to(barrier_path)):
        raise OperatorFailure("report_must_be_outside_barrier")
    if report == env_path:
        raise OperatorFailure("report_conflicts_with_env")
    backend_unit = _validate_unit_name(str(args.backend_unit or ""))
    bridge_unit = _validate_unit_name(str(args.bridge_unit or ""))
    if backend_unit == bridge_unit:
        raise OperatorFailure("systemd_units_not_distinct")
    return OperatorConfig(
        freeze_commit=commit,
        env_file=env_path,
        barrier_dir=barrier_path,
        backend_unit=backend_unit,
        bridge_unit=bridge_unit,
        report=report,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pinned_env: PinnedPrivateFile | None = None
    barrier: PinnedBarrier | None = None
    runtime: LiveRuntime | None = None
    try:
        config = _config_from_args(args)
        _validate_candidate(config.freeze_commit)
        pinned_env = PinnedPrivateFile(
            config.env_file,
            maximum_bytes=MAX_ENV_BYTES,
            invalid_code="env_file_invalid",
        )
        barrier = PinnedBarrier(config.barrier_dir)
        runtime = _build_runtime(config, pinned_env)
    except BaseException as exc:
        if runtime is not None:
            with suppress(BaseException):
                runtime.close()
        if barrier is not None:
            with suppress(BaseException):
                barrier.close()
        if pinned_env is not None:
            with suppress(BaseException):
                pinned_env.close()
        raw_commit = str(getattr(args, "freeze_commit", "") or "").strip().casefold()
        early_report = {
            "schema": OPERATOR_SCHEMA,
            "status": "failed",
            "commit": raw_commit if _HEX40_RE.fullmatch(raw_commit) is not None else "",
            "duration_ms": 0,
            "failure_codes": [_closed_exception_code(exc)],
            "signal": None,
            "checks": {},
            "evidence_sha256": {},
        }
        sys.stdout.buffer.write(_canonical_json(early_report) + b"\n")
        return 1

    signal_state: SignalHandlers | None = None
    report: dict[str, Any] = {}
    exit_code = 1
    try:
        signal_state = _install_signal_handlers()
        _activate_signal_handlers(signal_state)
        report, exit_code = execute_operator(
            config,
            runtime,
            barrier,
            signal_state=signal_state,
        )
        signal_state = None
    except BaseException as exc:
        signal_number = (
            signal_state.first_signal
            if signal_state is not None and signal_state.first_signal is not None
            else (exc.signal_number if isinstance(exc, OperatorSignal) else None)
        )
        report = {
            "schema": OPERATOR_SCHEMA,
            "status": "failed",
            "commit": config.freeze_commit,
            "duration_ms": 0,
            "failure_codes": [_closed_exception_code(exc)],
            "signal": (signal.Signals(signal_number).name if signal_number in _CONTROL_SIGNALS else None),
            "checks": {},
            "evidence_sha256": {},
        }
        exit_code = 128 + int(signal_number) if signal_number is not None else 1
    finally:
        if signal_state is not None:
            _finalize_signal_handlers(signal_state, lambda: None)
            if signal_state.first_signal is not None:
                signal_number = int(signal_state.first_signal)
                report["status"] = "failed"
                report["signal"] = signal.Signals(signal_number).name
                codes = report.get("failure_codes")
                failure_codes = list(codes) if isinstance(codes, list) else []
                projected = _closed_exception_code(OperatorSignal(signal_number))
                if projected not in failure_codes:
                    failure_codes.append(projected)
                report["failure_codes"] = sorted(failure_codes)
                exit_code = 128 + signal_number
        if runtime is not None:
            with suppress(BaseException):
                runtime.close()
        barrier.close()
        pinned_env.close()

    if config.report is not None:
        try:
            _atomic_private_report(config.report, report)
        except OperatorFailure:
            report["status"] = "failed"
            raw_codes = report.get("failure_codes")
            codes = list(raw_codes) if isinstance(raw_codes, list) else []
            if "report_write_failed" not in codes:
                codes.append("report_write_failed")
            report["failure_codes"] = sorted(codes)
            exit_code = 1
    sys.stdout.buffer.write(_canonical_json(report) + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
