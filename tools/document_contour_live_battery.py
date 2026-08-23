#!/usr/bin/env python3
"""A small, isolated live battery for Friday's document contour.

This runner is deliberately separate from ``synthetic_live_battery.py``.  It
executes exactly ten document scenarios twice, sequentially, against real local
LLM/embedding/reranker services while every database, uploaded byte, MCP file,
prompt and model response lives below a fresh private temporary directory.

The controller refuses to start without an explicit frozen commit and an
operator assertion that the Telegram bridge is stopped.  ``--self-test`` is
offline: it never imports ``friday.server`` and never contacts a sidecar.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import ctypes
import errno
import hashlib
import html
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
_root_import_path = str(ROOT)
if not sys.path or sys.path[0] != _root_import_path:
    sys.path.insert(0, _root_import_path)

# An editable virtualenv can otherwise resolve ``friday`` from a different,
# dirty checkout even when this controller itself lives in an immutable release
# worktree.  Pin and attest the package origin before any Friday submodule is
# imported by a worker scenario.
import friday as _friday_package  # noqa: E402

_friday_origin = Path(str(_friday_package.__file__ or "")).resolve()
if not _friday_origin.is_relative_to(ROOT):
    raise RuntimeError("Friday package origin is outside the frozen release root")

RUNS = 2
CASES = 10
LIVE_CASE_IDS = ("D06", "D07", "D08")
LIVE_CASES = len(LIVE_CASE_IDS)
WORKER_TIMEOUT_SEC = 1_800
SCHEMA = "friday.document-contour-live-battery.v1"
WORKER_SCHEMA = "friday.document-contour-live-battery.worker.v1"
REPORT_SCHEMA = "friday.document-contour-live-battery.report.v1"
RUN_RECEIPT_SCHEMA = "friday.document-contour-live-battery.run-receipt.v1"
FAILURE_SUMMARY_SCHEMA = "friday.document-contour-live-battery.failure-summary.v1"
OBSERVER_REQUEST_SCHEMA = "friday.document-contour-live-battery.observer-request.v1"
OBSERVER_RESPONSE_SCHEMA = "friday.document-contour-live-battery.observer-response.v2"
_RUN_ID_ENV = "FRIDAY_DOCUMENT_BATTERY_RUN_ID"
_RUN_ID_RE = re.compile(r"[0-9a-f]{64}")
_RELEASE_PROFILE = "qwen36-27b-nvfp4-nvidia"
INTER_RUN_OBSERVER_TIMEOUT_SEC = 180.0
PROCESS_GROUP_EXIT_GRACE_SEC = 2.0
PROCESS_GROUP_TERM_GRACE_SEC = 5.0
PROCESS_GROUP_KILL_GRACE_SEC = 5.0
_CONTROLLER_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_MAX_PENDING_CONTROLLER_SIGNAL_DRAINS = 8
_RENAME_NOREPLACE = 1
_GIT_BINARY = "/usr/bin/git"

_LIFECYCLE_FAILURE_CODES = frozenset(
    {
        "mcp_cleanup_exception",
        "mcp_cleanup_timeout_warning",
        "server_shutdown_stranded_warning",
    }
)
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

_GENERATION_STAGES = (
    "direct_synthesis",
    "map",
    "reduce",
    "final_synthesis",
    "verifier",
    "unclassified",
)
_GENERATION_OUTCOMES = ("started", "completed", "failures", "cancellations")
_GENERATION_TELEMETRY_KEYS = (
    "llm_chat_attempts",
    "generation_telemetry_missing",
    "generation_admission_timeouts",
    "generation_submitted_timeouts",
    "hierarchy_calls",
    "hierarchy_complete",
    "hierarchy_failures",
    "hierarchy_cancellations",
    "map_planned",
    "map_active",
    "map_peak_active",
    *(f"{stage}_{outcome}" for stage in _GENERATION_STAGES for outcome in _GENERATION_OUTCOMES),
)


@dataclass(frozen=True)
class Scenario:
    case_id: str
    title: str
    contract: tuple[str, ...]


@dataclass(frozen=True)
class CaseIdentity:
    """Private invocation namespace for one run/case fixture universe."""

    run_id: str = field(repr=False)
    run_index: int
    case_id: str

    def token(self, purpose: str, *, length: int = 16) -> str:
        if not purpose or not 8 <= length <= 32:
            raise BatteryFailure("case_identity_request_invalid")
        payload = f"{self.run_index}\0{self.case_id}\0{purpose}".encode()
        return hashlib.sha256(bytes.fromhex(self.run_id) + b"\0" + payload).hexdigest()[:length]

    @property
    def cache_prefix(self) -> str:
        return f"docbat-{self.case_id.casefold()}-{self.token('cache-prefix')}"

    def marker(self, label: str) -> str:
        return f"{label}-{self.token('marker:' + label, length=12).upper()}"

    def source_ref(self, label: str) -> str:
        return f"telegram-file:{label}-{self.token('source-ref:' + label)}"

    def filename(self, stem: str, extension: str) -> str:
        suffix = self.token(f"filename:{stem}:{extension}", length=12)
        return f"{stem}-{suffix}.{extension.lstrip('.')}"

    def prompt_variant(self, key: str, count: int) -> int:
        if not key or not 1 <= count <= 2:
            raise BatteryFailure("prompt_variant_contract_invalid")
        return int(self.token("prompt-variant:" + key, length=8), 16) % count


@dataclass(frozen=True)
class WorkerProcessOutcome:
    stdout: bytes
    returncode: int
    worker_reaped: bool
    process_group_clear_initial: bool
    process_group_clear: bool
    timed_out: bool
    cleanup_failure_codes: tuple[str, ...]


@dataclass(frozen=True)
class WorkerCleanupOutcome:
    stdout: bytes
    worker_reaped: bool
    process_group_clear: bool
    cleanup_failure_codes: tuple[str, ...]
    deferred_baseexception: BaseException | None = field(default=None, repr=False)


@dataclass
class ControllerSignalHandlers:
    previous: dict[int, Any]
    previous_mask: frozenset[Any]
    activated: bool = False
    first_signal: int | None = None
    worker_cleanup_clear: bool | None = None
    worker_cleanup_failure_codes: tuple[str, ...] = ()


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "D01",
        "dedup re-upload reply pointer",
        (
            "same ODT bytes under two Telegram file refs resolve to one canonical Raw",
            "reply to the second ref selects only that canonical file, never a newer decoy",
        ),
    ),
    Scenario(
        "D02",
        "reply to prior assistant restores exact source",
        (
            "the prior assistant row owns exactly the source it used",
            "reply_source_message_id restores it and cannot drift to a newer/deleted/foreign file",
        ),
    ),
    Scenario(
        "D03",
        "fuzzy filename navigation",
        (
            "approximate stem/abbreviation/typo selects the intended document",
            "a newer differently named spreadsheet is not substituted",
        ),
    ),
    Scenario(
        "D04",
        "semantic XLSX heading lookup",
        (
            "real object/chunk embeddings are current before the query",
            "query-time embeddings and reranker both run and preserve canonical evidence",
            "the heading-bound target wins without a false absence",
        ),
    ),
    Scenario(
        "D05",
        "uploader and received-at aggregation",
        (
            "unique short typo GBL resolves to JBL",
            "arrival-date range returns only JBL files in exact descending order",
        ),
    ),
    Scenario(
        "D06",
        "small ODT fit-first summary",
        (
            "bare small-file summary uses complete current-turn text",
            "no false partial-material warning and no outside-deed refusal",
        ),
    ),
    Scenario(
        "D07",
        "multipage scan OCR beyond page four",
        (
            "OCR reads the fifth page and returns its marker",
            "coverage is explicit and advisory evidence is never called verified",
        ),
    ),
    Scenario(
        "D08",
        "larger-than-context hierarchy",
        (
            "whole-document hierarchy reaches head, middle and tail",
            "tail lookup and global summary share complete parser-owned coverage",
        ),
    ),
    Scenario(
        "D09",
        "encrypted archive exact password",
        (
            "missing password persists nothing",
            "leading/trailing Unicode password opens the nested ODT exact-first",
            "the password and normalization variants never persist",
        ),
    ),
    Scenario(
        "D10",
        "technical metadata, visible requisites and exports",
        (
            "container headers and visible number/grif/date/signatory remain distinct",
            "regular make_file export is delivered",
            "a separate owner-only workspace_create reaches the real MCP server create-only",
        ),
    ),
)

LIVE_SCENARIOS: tuple[Scenario, ...] = tuple(
    scenario for scenario in SCENARIOS if scenario.case_id in LIVE_CASE_IDS
)


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
    }
)
_SCRATCH_PATHS = {
    "FRIDAY_HOME": ".",
    "FRIDAY_DATA_DIR": "data",
    "FRIDAY_CACHE_DIR": "cache",
    "FRIDAY_LOG_DIR": "logs",
    "FRIDAY_MODEL_ROOT": "models",
    "FRIDAY_STATE_DIR": "data/state",
    "FRIDAY_DATABASE_PATH": "data/state/friday.sqlite3",
    "FRIDAY_FILES_DIR": "data/files",
    "FRIDAY_MEMORY_VAULT_DIR": "data/memory-vault",
    "FRIDAY_BACKUPS_DIR": "data/backups",
    "FRIDAY_EXPORTS_DIR": "data/exports",
    "FRIDAY_WHISPER_DOWNLOAD_ROOT": "models/whisper",
    "FRIDAY_TTS_DOWNLOAD_ROOT": "models/tts",
    "FRIDAY_MCP_WORKSPACE_INBOX_DIR": "mcp/inbox",
    "FRIDAY_MCP_WORKSPACE_OUTBOX_DIR": "mcp/outbox",
    "HOME": "process/home",
    "XDG_CONFIG_HOME": "process/xdg/config",
    "XDG_CACHE_HOME": "process/xdg/cache",
    "XDG_DATA_HOME": "process/xdg/data",
    "XDG_STATE_HOME": "process/xdg/state",
    "XDG_RUNTIME_DIR": "process/xdg/runtime",
    "PYTHONPYCACHEPREFIX": "process/pycache",
    "TMPDIR": "process/tmp",
}
_SAFE_OVERRIDES = {
    "FRIDAY_ENV_FILE": "config/no-live-env-file",
    "FRIDAY_DATABASE_MUST_EXIST": "0",
    "FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK": "1",
    "FRIDAY_API_USER_RATE_LIMIT_PER_MINUTE": "1000",
    "FRIDAY_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE": "1000",
    "FRIDAY_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE": "5000",
    "FRIDAY_TELEGRAM_OPEN_REGISTRATION": "0",
    "FRIDAY_SHARED_ARCHIVE": "1",
    "FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS": "0",
    "FRIDAY_NEW_ACCOUNT_PRESET": "",
    "FRIDAY_WORKERS_ENABLED": "0",
    "FRIDAY_AUTONOMY_ENABLED": "0",
    "FRIDAY_COGNITION_ENABLED": "0",
    "FRIDAY_REMINDERS_ENABLED": "0",
    "FRIDAY_MONITORS_ENABLED": "0",
    "FRIDAY_REFLECTION_ENABLED": "0",
    "FRIDAY_CHRONICLE_ENABLED": "0",
    "FRIDAY_SENTINEL_ENABLED": "0",
    "FRIDAY_CODE_EXECUTION_ENABLED": "0",
    "FRIDAY_WEB_DAILY_QUOTA": "0",
    "FRIDAY_WHISPER_ENABLED": "0",
    "FRIDAY_TTS_ENABLED": "0",
    "FRIDAY_MCP_ENABLED": "1",
    "FRIDAY_MCP_STARTUP_TIMEOUT_SEC": "15",
    "FRIDAY_MCP_CALL_TIMEOUT_SEC": "20",
    "FRIDAY_MCP_RESULT_CHARS": "7000",
    "FRIDAY_INGESTION_REVIEW_POLICY": "assessed",
    "FRIDAY_EMBEDDINGS_INDEX_REST_RATIO": "0",
    "FRIDAY_BACKUP_MIRROR_DIR": "",
    "FRIDAY_BACKUP_ENCRYPTION_KEY_FILE": "",
    "PYTHONUNBUFFERED": "1",
}
_LOCAL_SIDECAR_URL_KEYS = (
    "FRIDAY_LLM_BASE_URL",
    "FRIDAY_EMBEDDINGS_BASE_URL",
    "FRIDAY_RERANK_BASE_URL",
)
_LOCAL_SIDECAR_V4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_LOCAL_SIDECAR_V6_NETWORKS = (ipaddress.ip_network("fc00::/7"),)


class BatteryFailure(RuntimeError):
    """Closed-code battery failure; its message must never contain source text."""


class ControllerSignal(BaseException):
    """Catchable SIGINT/SIGTERM projection used only to unwind worker cleanup."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(signal_number)
        self.signal_number = signal_number
        self.worker_cleanup_clear: bool | None = None
        self.worker_cleanup_failure_codes: tuple[str, ...] = ()


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@contextmanager
def _owned_os_descriptor(
    path: str | os.PathLike[str],
    flags: int,
    mode: int | None = None,
    *,
    dir_fd: int | None = None,
):
    """Open and retain one fd before pending controller signals are delivered."""

    descriptor = -1
    try:
        previous_mask = _block_controller_signals()
        try:
            open_kwargs = {} if dir_fd is None else {"dir_fd": dir_fd}
            if mode is None:
                descriptor = os.open(path, flags, **open_kwargs)
            else:
                descriptor = os.open(path, flags, mode, **open_kwargs)
        finally:
            # If unmasking delivers ControllerSignal, descriptor has already
            # been stored and the outer finally closes it during the unwind.
            _restore_signal_mask(previous_mask)
        yield descriptor
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    with _owned_os_descriptor(path, flags, 0o600) as descriptor:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]


def _validated_private_barrier_path(
    path: Path,
) -> tuple[Path, tuple[int, ...], tuple[int, int, int, int]]:
    """Capture the lexical parent+barrier identities before either is opened."""

    lexical = Path(os.path.abspath(path.expanduser()))
    try:
        parent_metadata = os.lstat(lexical.parent)
        metadata = os.lstat(lexical)
    except OSError as exc:
        raise BatteryFailure("inter_run_barrier_dir_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or lexical.resolve() != lexical
    ):
        raise BatteryFailure("inter_run_barrier_dir_invalid")
    parent_identity = _parent_directory_identity(parent_metadata)
    if parent_identity[2] != os.getuid() or parent_identity[3] != 0o700:
        raise BatteryFailure("inter_run_barrier_dir_invalid")
    return lexical, parent_identity, _directory_identity(metadata)


def _require_private_barrier_dir(path: Path) -> Path:
    """Validate a single-use barrier below a dedicated owner-only parent."""

    lexical, _parent_identity, _identity = _validated_private_barrier_path(path)
    try:
        if any(lexical.iterdir()):
            raise BatteryFailure("inter_run_barrier_dir_not_empty")
    except OSError as exc:
        raise BatteryFailure("inter_run_barrier_dir_invalid") from exc
    return lexical


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise BatteryFailure("inter_run_barrier_dir_invalid")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        stat.S_IMODE(metadata.st_mode),
    )


def _parent_directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        *_directory_identity(metadata),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


@dataclass
class _PinnedBarrierDirectory:
    """Open parent+barrier descriptors retained for the controller lifetime."""

    path: Path
    parent_descriptor: int
    descriptor: int
    parent_identity: tuple[int, ...]
    identity: tuple[int, int, int, int]

    @classmethod
    def open(
        cls,
        value: Path,
        *,
        owner: list[_PinnedBarrierDirectory] | None = None,
    ) -> _PinnedBarrierDirectory:
        path, expected_parent_identity, expected_identity = _validated_private_barrier_path(value)
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = -1
        descriptor = -1
        pinned: _PinnedBarrierDirectory | None = None
        try:
            parent_descriptor = os.open(path.parent, directory_flags)
            parent_identity = _parent_directory_identity(os.fstat(parent_descriptor))
            if parent_identity != expected_parent_identity:
                raise BatteryFailure("inter_run_barrier_dir_changed")
            descriptor = os.open(path.name, directory_flags, dir_fd=parent_descriptor)
            identity = _directory_identity(os.fstat(descriptor))
            if identity != expected_identity:
                raise BatteryFailure("inter_run_barrier_dir_changed")
            pinned = cls(
                path=path,
                parent_descriptor=parent_descriptor,
                descriptor=descriptor,
                parent_identity=parent_identity,
                identity=identity,
            )
            pinned.revalidate()
            if os.listdir(descriptor):
                raise BatteryFailure("inter_run_barrier_dir_not_empty")
            if owner is not None:
                # Bind ownership before returning.  A BaseException between
                # RETURN_VALUE and the caller's STORE_FAST can then still be
                # closed by the controller's already-bound owner list.
                owner.append(pinned)
            return pinned
        except BaseException:
            if pinned is not None:
                pinned.close()
            else:
                if descriptor >= 0:
                    os.close(descriptor)
                if parent_descriptor >= 0:
                    os.close(parent_descriptor)
            raise

    def __truediv__(self, name: str) -> Path:
        return self.path / name

    def revalidate(self) -> None:
        if self.parent_descriptor < 0 or self.descriptor < 0:
            raise BatteryFailure("inter_run_barrier_dir_invalid")
        try:
            if _parent_directory_identity(os.fstat(self.parent_descriptor)) != self.parent_identity:
                raise BatteryFailure("inter_run_barrier_dir_changed")
            if (
                _parent_directory_identity(os.stat(self.path.parent, follow_symlinks=False))
                != self.parent_identity
            ):
                raise BatteryFailure("inter_run_barrier_dir_changed")
            if _directory_identity(os.fstat(self.descriptor)) != self.identity:
                raise BatteryFailure("inter_run_barrier_dir_changed")
            if (
                _directory_identity(
                    os.stat(
                        self.path.name,
                        dir_fd=self.parent_descriptor,
                        follow_symlinks=False,
                    )
                )
                != self.identity
                or self.path.resolve() != self.path
            ):
                raise BatteryFailure("inter_run_barrier_dir_changed")
        except BatteryFailure:
            raise
        except OSError as exc:
            raise BatteryFailure("inter_run_barrier_dir_changed") from exc

    def close(self) -> None:
        descriptor = self.descriptor
        parent_descriptor = self.parent_descriptor
        self.descriptor = -1
        self.parent_descriptor = -1
        first_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                first_error = exc
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


@contextmanager
def _private_worker_log(path: Path):
    """Open one private log without a raw-fd ownership-transfer window."""

    descriptor = -1
    stream = None
    try:
        previous_mask = _block_controller_signals()
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "wb")
            descriptor = -1
        finally:
            # A pending controller signal is delivered only after either the
            # raw fd or its stream owner is bound in this frame.  The outer
            # finally then closes that bound owner during the unwind.
            _restore_signal_mask(previous_mask)
        yield stream
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _validate_private_regular_file(path: Path, metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
    ):
        raise BatteryFailure("inter_run_barrier_file_invalid")


def _private_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    _validate_private_regular_file(Path("."), metadata)
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        stat.S_IMODE(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _validate_barrier_name(name: str) -> str:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise BatteryFailure("inter_run_barrier_file_invalid")
    return name


def _rename_noreplace(
    source_dir: int,
    source_name: str,
    target_dir: int,
    target_name: str,
) -> None:
    """Atomically publish one Linux-local file without replacing a target."""

    try:
        source = os.fsencode(source_name)
        target = os.fsencode(target_name)
    except (TypeError, UnicodeError) as exc:
        raise BatteryFailure("inter_run_barrier_write_failed") from exc
    if not source or not target or b"/" in source or b"/" in target:
        raise BatteryFailure("inter_run_barrier_write_failed")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise BatteryFailure("inter_run_barrier_atomic_publish_unsupported") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(source_dir, source, target_dir, target, _RENAME_NOREPLACE) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise BatteryFailure("inter_run_barrier_file_exists")
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise BatteryFailure("inter_run_barrier_atomic_publish_unsupported")
    raise OSError(error_number, os.strerror(error_number), target_name)


def _atomic_pinned_private_write(
    barrier: _PinnedBarrierDirectory,
    name: str,
    data: bytes,
) -> Path:
    """Publish one create-only private file through the held directory fd."""

    name = _validate_barrier_name(name)
    barrier.revalidate()
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        with _owned_os_descriptor(
            temporary,
            flags,
            0o600,
            dir_fd=barrier.descriptor,
        ) as descriptor:
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        _rename_noreplace(
            barrier.descriptor,
            temporary,
            barrier.descriptor,
            name,
        )
        os.fsync(barrier.descriptor)
        metadata = os.stat(name, dir_fd=barrier.descriptor, follow_symlinks=False)
        _validate_private_regular_file(barrier.path / name, metadata)
        barrier.revalidate()
        return barrier.path / name
    except BatteryFailure:
        raise
    except OSError as exc:
        raise BatteryFailure("inter_run_barrier_write_failed") from exc
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=barrier.descriptor)


def _read_pinned_private_json(
    barrier: _PinnedBarrierDirectory,
    name: str,
    *,
    max_bytes: int = 16_384,
) -> dict[str, Any]:
    """Read one stable owner-only file relative to the held barrier fd."""

    name = _validate_barrier_name(name)
    barrier.revalidate()
    try:
        before = os.stat(name, dir_fd=barrier.descriptor, follow_symlinks=False)
        before_identity = _private_file_identity(before)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        with _owned_os_descriptor(name, flags, dir_fd=barrier.descriptor) as descriptor:
            opened = os.fstat(descriptor)
            opened_identity = _private_file_identity(opened)
            if opened_identity != before_identity:
                raise BatteryFailure("inter_run_barrier_file_changed")
            if opened.st_size <= 0 or opened.st_size > max_bytes:
                raise BatteryFailure("inter_run_barrier_file_invalid")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 65_536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                _private_file_identity(after) != opened_identity
                or sum(len(chunk) for chunk in chunks) != opened.st_size
            ):
                raise BatteryFailure("inter_run_barrier_file_changed")
        lexical_after = os.stat(name, dir_fd=barrier.descriptor, follow_symlinks=False)
        if _private_file_identity(lexical_after) != opened_identity:
            raise BatteryFailure("inter_run_barrier_file_changed")
        barrier.revalidate()
        raw = b"".join(chunks)
        parsed = json.loads(raw.decode("utf-8"))
    except BatteryFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatteryFailure("inter_run_barrier_file_invalid") from exc
    if not isinstance(parsed, dict):
        raise BatteryFailure("inter_run_barrier_file_invalid")
    if raw != _canonical_json(parsed) + b"\n":
        raise BatteryFailure("inter_run_barrier_file_invalid")
    return parsed


class _LifecycleLogHandler(logging.Handler):
    """Project only authoritative shutdown warnings into closed event codes."""

    def __init__(self, failure_codes: set[str]) -> None:
        super().__init__(level=logging.WARNING)
        self.failure_codes = failure_codes

    def emit(self, record: logging.LogRecord) -> None:
        template = str(record.msg or "")
        if record.name == "friday.mcp_runtime.client" and template.startswith(
            "MCP server %s cleanup exceeded"
        ):
            self.failure_codes.add("mcp_cleanup_timeout_warning")
        elif record.name == "friday.server" and template.startswith("Shutting down with %s still executing"):
            self.failure_codes.add("server_shutdown_stranded_warning")


class LifecycleAudit:
    """Observe lifecycle boundaries without retaining log messages or payloads."""

    def __init__(self) -> None:
        self.failure_codes: set[str] = set()
        self._handler = _LifecycleLogHandler(self.failure_codes)
        self._original_mcp_close: Callable[..., Any] | None = None

    def install(self) -> None:
        import friday.mcp_runtime.client as mcp_client

        if self._original_mcp_close is not None:
            raise BatteryFailure("lifecycle_audit_already_installed")
        original = mcp_client._bounded_stack_close
        self._original_mcp_close = original

        async def audited_mcp_close(*args: Any, **kwargs: Any) -> Any:
            try:
                return await original(*args, **kwargs)
            except BaseException:
                self.failure_codes.add("mcp_cleanup_exception")
                raise

        mcp_client._bounded_stack_close = audited_mcp_close
        logging.getLogger("friday.mcp_runtime.client").addHandler(self._handler)
        logging.getLogger("friday.server").addHandler(self._handler)

    def close(self) -> None:
        import friday.mcp_runtime.client as mcp_client

        logging.getLogger("friday.mcp_runtime.client").removeHandler(self._handler)
        logging.getLogger("friday.server").removeHandler(self._handler)
        if self._original_mcp_close is not None:
            mcp_client._bounded_stack_close = self._original_mcp_close
            self._original_mcp_close = None

    def closed_failure_codes(self) -> tuple[str, ...]:
        return tuple(sorted(code for code in self.failure_codes if code in _LIFECYCLE_FAILURE_CODES))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _new_run_id() -> str:
    return secrets.token_hex(32)


def _validated_run_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _RUN_ID_RE.fullmatch(normalized) is None:
        raise BatteryFailure("battery_run_id_invalid")
    return normalized


def _run_id_hash(run_id: str) -> str:
    return _sha256(bytes.fromhex(_validated_run_id(run_id)))


def _run_token(run_id: str, run_index: int, purpose: str, *, length: int = 16) -> str:
    if not 1 <= run_index <= RUNS or not purpose or not 8 <= length <= 32:
        raise BatteryFailure("run_identity_request_invalid")
    payload = f"{run_index}\0{purpose}".encode()
    return hashlib.sha256(bytes.fromhex(_validated_run_id(run_id)) + b"\0" + payload).hexdigest()[:length]


def _run_owner_chats(run_id: str, run_index: int) -> tuple[int, ...]:
    # Telegram identifiers are signed 64-bit integers.  Reserving two decimal
    # digits for the role makes the eleven identities collision-free per run.
    base = 1_000_000_000 + int(_run_token(run_id, run_index, "telegram-chats", length=10), 16) * 100
    return tuple(base + role for role in range(1, 12))


def _case_identity(run_id: str, run_index: int, case_id: str) -> CaseIdentity:
    if case_id not in {item.case_id for item in SCENARIOS}:
        raise BatteryFailure("unknown_case_identity")
    return CaseIdentity(_validated_run_id(run_id), run_index, case_id)


def _marker(harness: Any, label: str, *, fallback: str = "") -> str:
    identity = getattr(harness, "identity", None)
    if isinstance(identity, CaseIdentity):
        return identity.marker(label)
    return fallback or f"{label}-{int(harness.run_index)}"


def _source_ref(harness: Any, label: str, *, fallback: str = "") -> str:
    identity = getattr(harness, "identity", None)
    if isinstance(identity, CaseIdentity):
        return identity.source_ref(label)
    return fallback or f"telegram-file:{label}-{int(harness.run_index)}"


def _filename(harness: Any, stem: str, extension: str, *, fallback: str = "") -> str:
    identity = getattr(harness, "identity", None)
    if isinstance(identity, CaseIdentity):
        return identity.filename(stem, extension)
    return fallback or f"{stem}.{extension.lstrip('.')}"


def _scoped_prompt(harness: Any, key: str, message: str) -> str:
    """Give non-empty live prompts one of two natural equivalent forms.

    Cache/run identity belongs to the isolated chat, source refs, filenames and
    fixture facts.  It must never become an artificial body-search term in the
    user-visible request itself.
    """

    identity = getattr(harness, "identity", None)
    if not message or not isinstance(identity, CaseIdentity):
        return message
    if identity.prompt_variant(key, 2) == 0:
        return message
    return f"Пожалуйста.\n{message}"


def _load_env_file_values(path: Path) -> dict[str, str]:
    """Read only allowlisted sidecar values without mutating the controller env."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in _MODEL_ENV_ALLOWLIST:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _require_local_sidecar_url(name: str, value: str) -> None:
    """Refuse a battery configuration that could export private fixtures."""

    parsed = urlsplit(value)
    host = str(parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise BatteryFailure(f"{name.casefold()}_not_local")
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise BatteryFailure(f"{name.casefold()}_not_local") from exc
    private_networks = _LOCAL_SIDECAR_V4_NETWORKS if address.version == 4 else _LOCAL_SIDECAR_V6_NETWORKS
    if not (address.is_loopback or any(address in network for network in private_networks)):
        raise BatteryFailure(f"{name.casefold()}_not_local")


def build_worker_environment(
    run_dir: Path,
    *,
    owner_chats: Sequence[int],
    source_env_file: Path | None = None,
    run_id: str | None = None,
    operator_model_env_only: bool = False,
) -> dict[str, str]:
    run_dir = run_dir.resolve()
    if run_dir == Path(run_dir.anchor) or not run_dir.is_dir():
        raise BatteryFailure("unsafe_run_directory")
    if operator_model_env_only:
        if source_env_file is not None:
            raise BatteryFailure("operator_model_env_only_source_env_file_conflict")
        if _MODEL_ENV_ALLOWLIST.difference(os.environ):
            raise BatteryFailure("operator_model_env_only_incomplete")
        source = {key: os.environ[key] for key in _MODEL_ENV_ALLOWLIST}
    else:
        source_path = source_env_file.resolve() if source_env_file is not None else ROOT / ".env.local"
        source = _load_env_file_values(source_path)
        for key in _MODEL_ENV_ALLOWLIST:
            if key in os.environ:
                source[key] = os.environ[key]
    for key in _LOCAL_SIDECAR_URL_KEYS:
        if value := source.get(key):
            _require_local_sidecar_url(key, value)
    environment = {key: value for key, value in os.environ.items() if key in _PROCESS_ENV_ALLOWLIST}
    environment.update(source)
    environment.update(_SAFE_OVERRIDES)
    for key, relative in _SCRATCH_PATHS.items():
        destination = (run_dir / relative).resolve()
        if not _inside(destination, run_dir):
            raise BatteryFailure("scratch_path_escape")
        environment[key] = str(destination)
    environment["FRIDAY_ENV_FILE"] = str((run_dir / "config/no-live-env-file").resolve())
    environment["FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS"] = ",".join(str(value) for value in owner_chats)
    environment["FRIDAY_TELEGRAM_OWNER_CHAT_IDS"] = ",".join(str(value) for value in owner_chats[:-1])
    environment["FRIDAY_TELEGRAM_BRIDGE_SECRET"] = secrets.token_urlsafe(48)
    environment["FRIDAY_API_TOKEN"] = secrets.token_urlsafe(48)
    environment["FRIDAY_DOCUMENT_BATTERY_RUN_DIR"] = str(run_dir)
    environment["FRIDAY_DOCUMENT_BATTERY_EVIDENCE"] = str(run_dir / "private-evidence.json")
    environment[_RUN_ID_ENV] = _validated_run_id(run_id or _new_run_id())
    for relative in set(_SCRATCH_PATHS.values()) | {"fixtures", "private"}:
        _private_dir((run_dir / relative).resolve())
    return environment


def _scenario_manifest() -> list[dict[str, Any]]:
    return [
        {"case_id": item.case_id, "title": item.title, "contract": list(item.contract)} for item in SCENARIOS
    ]


def case_state_paths(
    run_dir: Path,
    case_id: str,
    identity: CaseIdentity | None = None,
) -> dict[str, Path]:
    """Closed per-case mutable roots; scenarios never share a DB or file tree."""

    if case_id not in {item.case_id for item in SCENARIOS}:
        raise BatteryFailure("unknown_case_state")
    suffix = f"-{identity.token('state-path')}" if identity is not None else ""
    case_root = (run_dir.resolve() / f"case-{case_id.casefold()}{suffix}").resolve()
    if not _inside(case_root, run_dir):
        raise BatteryFailure("case_state_escape")
    return {
        "root": case_root,
        "data": case_root / "data",
        "cache": case_root / "cache",
        "logs": case_root / "logs",
        "models": case_root / "models",
        "state": case_root / "data/state",
        "database": case_root / "data/state/friday.sqlite3",
        "files": case_root / "data/files",
        "memory": case_root / "data/memory-vault",
        "backups": case_root / "data/backups",
        "exports": case_root / "data/exports",
        "mcp_inbox": case_root / "mcp/inbox",
        "mcp_outbox": case_root / "mcp/outbox",
        "evidence": case_root / "private-evidence.json",
    }


def offline_self_test() -> dict[str, Any]:
    ids = [item.case_id for item in SCENARIOS]
    if len(SCENARIOS) != CASES or ids != [f"D{index:02d}" for index in range(1, CASES + 1)]:
        raise BatteryFailure("scenario_manifest_invalid")
    if len(set(ids)) != CASES or any(not item.contract for item in SCENARIOS):
        raise BatteryFailure("scenario_contract_invalid")
    live_ids = [item.case_id for item in LIVE_SCENARIOS]
    if tuple(live_ids) != LIVE_CASE_IDS or len(set(live_ids)) != LIVE_CASES:
        raise BatteryFailure("live_scenario_manifest_invalid")
    with tempfile.TemporaryDirectory(prefix="friday-document-battery-selftest-") as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        run_ids = (_new_run_id(), _new_run_id())
        if run_ids[0] == run_ids[1]:
            raise BatteryFailure("invocation_identity_not_fresh")
        chats = _run_owner_chats(run_ids[0], 1)
        environment = build_worker_environment(root, owner_chats=chats, run_id=run_ids[0])
        for key, relative in _SCRATCH_PATHS.items():
            expected = (root / relative).resolve()
            if Path(environment[key]).resolve() != expected or not _inside(expected, root):
                raise BatteryFailure("scratch_isolation_invalid")
        if environment["FRIDAY_ENV_FILE"] != str((root / "config/no-live-env-file").resolve()):
            raise BatteryFailure("live_env_not_blocked")
        if environment.get("FRIDAY_WORKERS_ENABLED") != "0":
            raise BatteryFailure("workers_not_blocked")
        private = root / "private" / "mode-check.bin"
        _private_write(private, b"closed")
        if stat.S_IMODE(private.stat().st_mode) != 0o600:
            raise BatteryFailure("private_file_mode_invalid")
        identities = [
            _case_identity(run_id, run_index, scenario.case_id)
            for run_id in run_ids
            for run_index in range(1, RUNS + 1)
            for scenario in SCENARIOS
        ]
        databases = {
            case_state_paths(root, identity.case_id, identity)["database"] for identity in identities
        }
        if len(databases) != len(identities) or any(not _inside(path, root) for path in databases):
            raise BatteryFailure("case_database_isolation_invalid")
        identity_sets: dict[str, set[Any]] = {
            "cache": {identity.cache_prefix for identity in identities},
            "marker": {identity.marker("SELFTEST") for identity in identities},
            "ref": {identity.source_ref("SELFTEST") for identity in identities},
            "filename": {identity.filename("selftest", "odt") for identity in identities},
            "chat_ref": {f"document-live:{identity.token('chat-ref:1')}" for identity in identities},
            "message": {int(identity.token("message:1", length=15), 16) for identity in identities},
        }
        if any(len(values) != len(identities) for values in identity_sets.values()):
            raise BatteryFailure("fixture_identity_not_disjoint")
        prompt_forms = {
            _scoped_prompt(
                type("PromptProbe", (), {"identity": identity})(),
                "selftest",
                "Обобщи документ.",
            )
            for identity in identities
        }
        if not prompt_forms or not prompt_forms.issubset(
            {"Обобщи документ.", "Пожалуйста.\nОбобщи документ."}
        ):
            raise BatteryFailure("prompt_variant_not_natural")
        # A model cache sees the whole conversation, including the isolated
        # document fact/name, not only the final natural instruction.  Assert
        # that this real identity surface remains disjoint without teaching the
        # product to ignore a synthetic token in user text.
        conversation_prompts = {
            "\n".join(
                (
                    identity.filename("selftest", "odt"),
                    identity.marker("SELFTEST"),
                    _scoped_prompt(
                        type("PromptProbe", (), {"identity": identity})(),
                        "selftest",
                        "Обобщи документ.",
                    ),
                )
            )
            for identity in identities
        }
        if len(conversation_prompts) != len(identities):
            raise BatteryFailure("conversation_prompt_identity_not_disjoint")
    return {
        "schema": SCHEMA,
        "self_test": "passed",
        "runs": RUNS,
        "cases_per_run": CASES,
        "scenario_ids": ids,
        "live_cases_per_run": LIVE_CASES,
        "live_scenario_ids": live_ids,
        "identity_count": len(identities),
        "identity_disjoint": True,
        "prompt_variants": 2,
    }


def _odt_bytes(
    paragraphs: Sequence[str],
    *,
    title: str = "",
    creator: str = "Synthetic Friday Battery",
    creation_date: str = "2025-01-02T03:04:05+00:00",
    modified_date: str = "2025-01-03T04:05:06+00:00",
) -> bytes:
    body = "".join(f"<text:p>{html.escape(value)}</text:p>" for value in paragraphs)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text>{body}</office:text></office:body>
</office:document-content>"""
    meta = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
 <office:meta>
  <dc:title>{html.escape(title)}</dc:title>
  <dc:creator>{html.escape(creator)}</dc:creator>
  <meta:creation-date>{creation_date}</meta:creation-date>
  <dc:date>{modified_date}</dc:date>
  <meta:editing-cycles>7</meta:editing-cycles>
  <meta:document-statistic meta:page-count="3" meta:paragraph-count="8" meta:word-count="44"/>
 </office:meta>
</office:document-meta>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", content)
        archive.writestr("meta.xml", meta)
    return output.getvalue()


def _xlsx_bytes(rows: Sequence[Sequence[str]]) -> bytes:
    from openpyxl import Workbook  # type: ignore[import-untyped]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Штат"
    for row in rows:
        sheet.append(list(row))
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


# Keep the five-page scan genuinely raster-only while fitting all pages into
# two vision batches in one concurrent wave.  The previous 1600x2200 pages
# exceeded the batch pixel ceiling individually, turning this coverage canary
# into five near-identical long model calls.  480x660 remains comfortably
# readable after the PDF page scales it up, but proves page-five coverage with
# the production batch planner rather than benchmarking the same OCR prompt.
_SCAN_FIXTURE_WIDTH = 480
_SCAN_FIXTURE_HEIGHT = 660
_SCAN_FIXTURE_TEXT_X = 28
_SCAN_FIXTURE_RIGHT_MARGIN = 28
_SCAN_FIXTURE_FONT_SIZE = 26
_SCAN_SECRET_LABEL_Y = 240
_SCAN_SECRET_VALUE_Y = 292
_SCAN_PDF_WIDTH = 200
_SCAN_PDF_HEIGHT = 275


def _scan_fixture_font(text: str, font_path: Path, *, max_width: int) -> Any:
    """Return the largest fixture font whose complete text fits the page."""

    from PIL import ImageFont

    if font_path.is_file():
        for size in range(_SCAN_FIXTURE_FONT_SIZE, 11, -1):
            candidate_font = ImageFont.truetype(str(font_path), size)
            left, _top, right, _bottom = candidate_font.getbbox(text)
            if right - left <= max_width:
                return candidate_font
    fallback_font = ImageFont.load_default()
    left, _top, right, _bottom = fallback_font.getbbox(text)
    if right - left <= max_width:
        return fallback_font
    raise BatteryFailure("scan_fixture_text_does_not_fit")


def _scan_pdf(marker: str, *, pages: int = 5, fixture_scope: str = "") -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
    from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font: Any = (
        ImageFont.truetype(str(font_path), _SCAN_FIXTURE_FONT_SIZE)
        if font_path.is_file()
        else ImageFont.load_default()
    )
    secret_width = _SCAN_FIXTURE_WIDTH - _SCAN_FIXTURE_TEXT_X - _SCAN_FIXTURE_RIGHT_MARGIN
    output = io.BytesIO()
    # PDFium renders ordinary PDF coordinates at 2.5x.  A 200x275 MediaBox
    # therefore yields about 344k pixels per visible page: three pages fit the
    # 1,048,576-pixel batch ceiling and the five-page scan becomes 3+2.
    pdf = canvas.Canvas(
        output,
        pagesize=(_SCAN_PDF_WIDTH, _SCAN_PDF_HEIGHT),
        pageCompression=1,
    )
    for page in range(1, pages + 1):
        image = Image.new("RGB", (_SCAN_FIXTURE_WIDTH, _SCAN_FIXTURE_HEIGHT), "white")
        draw = ImageDraw.Draw(image)
        draw.text((28, 54), f"SYNTHETIC SCAN PAGE {page}", fill="black", font=font)
        draw.text((28, 138), f"CONTROL PAGE NUMBER {page}", fill="black", font=font)
        if fixture_scope:
            scope_font = _scan_fixture_font(
                f"FIXTURE SCOPE {fixture_scope} PAGE {page}",
                font_path,
                max_width=secret_width,
            )
            draw.text(
                (_SCAN_FIXTURE_TEXT_X, 190),
                f"FIXTURE SCOPE {fixture_scope} PAGE {page}",
                fill="black",
                font=scope_font,
            )
        if page == pages:
            draw.text(
                (_SCAN_FIXTURE_TEXT_X, _SCAN_SECRET_LABEL_Y),
                "SECRET CODE",
                fill="black",
                font=font,
            )
            secret_font = _scan_fixture_font(marker, font_path, max_width=secret_width)
            draw.text(
                (_SCAN_FIXTURE_TEXT_X, _SCAN_SECRET_VALUE_Y),
                marker,
                fill="black",
                font=secret_font,
            )
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=True)
        encoded.seek(0)
        pdf.drawImage(
            ImageReader(encoded),
            0,
            0,
            width=_SCAN_PDF_WIDTH,
            height=_SCAN_PDF_HEIGHT,
        )
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def _long_document_fixture(markers: tuple[str, str, str], *, fixture_scope: str) -> str:
    """Build a large, varied source which still requires four real MAP leaves.

    The former fixture repeated one almost identical sentence 400 times.  Its
    changing ordinal defeated the exact-RLE fast path, but the language model
    could still enter a repeated-token degeneration while summarising it.  This
    source retains the same hard properties (complete source, >4x the isolated
    8K context, head/middle/tail facts, no lossless RLE) with ordinary varied
    prose instead of benchmarking a pathological prompt.
    """

    topics = (
        "приёмка оборудования",
        "планирование смен",
        "сверка накладных",
        "контроль качества",
        "учёт материалов",
        "проверка маршрутов",
        "архивирование актов",
        "согласование графика",
        "инвентаризация склада",
        "подготовка отчёта",
        "разбор отклонений",
    )
    actions = (
        "рабочая группа сопоставила записи с реестром",
        "ответственный сотрудник проверил исходные даты",
        "координатор разнёс пункты по очередности",
        "дежурная смена отметила подтверждённые позиции",
        "исполнитель сравнил план с фактическим состоянием",
        "секретарь связал решение с номером протокола",
        "аналитик выделил расхождения без изменения источника",
        "оператор проверил полноту приложенного перечня",
        "руководитель уточнил владельца следующего шага",
        "наблюдатель зафиксировал результат повторной сверки",
        "комиссия разделила закрытые и открытые вопросы",
        "архивариус сохранил порядок исходных строк",
    )
    outcomes = (
        "расхождения вынесены в отдельный список",
        "подтверждённые строки оставлены без изменений",
        "следующая проверка назначена после получения акта",
        "неполные позиции возвращены ответственному владельцу",
        "итоговая таблица сохранена в исходном порядке",
        "дубликаты отмечены, но не удалены автоматически",
        "контрольная сумма записана в журнал сверки",
        "замечания сгруппированы по участкам работы",
        "переход к следующему этапу явно зафиксирован",
        "источник каждого вывода указан в рабочем реестре",
        "неопределённые значения оставлены на ручную проверку",
        "закрытые пункты отделены от ожидающих подтверждения",
        "решение связано с исходной записью журнала",
    )

    lines = [f"Начало документа. Контрольный код {markers[0]}. Контур {fixture_scope}.\n"]
    paragraph_count = 216
    middle_at = paragraph_count // 2
    for index in range(paragraph_count):
        if index == middle_at:
            lines.append(f"Середина документа. Контрольный код {markers[1]}.\n")
        topic = topics[index % len(topics)]
        action = actions[(index * 5 + 1) % len(actions)]
        outcome = outcomes[(index * 7 + 2) % len(outcomes)]
        lines.append(
            f"Абзац {index:03d}. Тема «{topic}»: {action}; результат — {outcome}. "
            "Это нейтральная запись наблюдения, а не команда к внешнему действию.\n"
        )
    lines.append(f"Конец документа. Контрольный код {markers[2]}.\n")
    return "".join(lines)


def _encrypted_zip(inner_name: str, inner_bytes: bytes, password: str) -> bytes:
    import pyzipper  # type: ignore[import-untyped]

    output = io.BytesIO()
    with pyzipper.AESZipFile(
        output,
        mode="w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        archive.writestr(inner_name, inner_bytes)
    return output.getvalue()


def _json_metadata(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    value = row.get("metadata_json")
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _contains_all(value: str, expected: Sequence[str]) -> bool:
    normalized = _normalized(value)
    return all(_normalized(item) in normalized for item in expected)


def _d04_answer_has_requested_identity(value: str) -> bool:
    """Check the public semantic answer, not a hidden fixture nonce."""

    normalized = _normalized(value)
    if not _contains_all(normalized, ("РЭБ", "командир", "взвод", "капитан Орлов")):
        return False
    identity = r"капитан\s+орлов"
    role = r"командир\w*\s+взвод\w*"
    for clause in re.split(r"[.!?;\n]+", normalized):
        identity_match = re.search(identity, clause)
        role_match = re.search(role, clause)
        if identity_match is None or role_match is None or re.search(r"\bрэб\b", clause) is None:
            continue
        if abs(identity_match.start() - role_match.start()) > 120:
            continue
        if re.search(r"\b(?:не|никто|иной|другой)\b", clause):
            continue
        return True
    return False


def _docx_non_title_lines(payload: bytes) -> tuple[str, ...]:
    """Return normalized non-empty DOCX paragraphs, excluding its title."""

    if not payload:
        return ()
    try:
        from docx import Document

        document = Document(io.BytesIO(payload))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return ()
    lines: list[str] = []
    for paragraph in document.paragraphs:
        line = _normalized(paragraph.text)
        if not line:
            continue
        style = getattr(paragraph, "style", None)
        style_name = _normalized(str(getattr(style, "name", "") or ""))
        if style_name == "title":
            continue
        lines.append(line)
    return tuple(lines)


class LiveProbes:
    """Content-free counters plus the last closed source-search projection.

    Generation stages come from runtime call boundaries, never prompt text.  A
    ``ContextVar`` keeps concurrent hierarchy leaves correctly attributed while
    the raw LLM wrapper owns the exact started/completed/failure lifecycle.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.counts = {
            **{key: 0 for key in _GENERATION_TELEMETRY_KEYS},
            "embedding_calls": 0,
            "embedding_successes": 0,
            "reranker_calls": 0,
            "reranker_successes": 0,
            "embedding_http": 0,
            "reranker_http": 0,
            "source_search_calls": 0,
            "source_search_successes": 0,
            "late_make_file_attempts": 0,
            "workspace_create_kernel_attempts": 0,
            "workspace_create_kernel": 0,
            "workspace_create_mcp_attempts": 0,
            "workspace_create_mcp": 0,
            "forbidden_web_calls": 0,
        }
        self.last_source_search: dict[str, Any] = {}
        self._restore: list[Callable[[], None]] = []
        self._generation_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"friday_document_generation_stage_{id(self)}",
            default=None,
        )

    def snapshot(self) -> dict[str, int]:
        return dict(self.counts)

    def delta(self, before: Mapping[str, int]) -> dict[str, int]:
        return {key: int(value) - int(before.get(key, 0)) for key, value in self.counts.items()}

    def install(self) -> None:
        import httpx

        agent = self.app.state.agent
        llm: Any = getattr(agent, "llm", None)
        original_llm_chat = getattr(llm, "chat", None)
        stage_boundaries = (
            "_attachment_primary_chat",
            "_reduce_attachment_map_records",
            "_build_attachment_hierarchy_bundle",
            "_hierarchical_attachment_response",
            "_verify_response",
        )
        if not callable(original_llm_chat) or any(
            not callable(getattr(agent, name, None)) for name in stage_boundaries
        ):
            raise BatteryFailure("generation_stage_telemetry_unavailable")

        embeddings = self.app.state.embeddings
        original_embed = embeddings.embed

        async def embed(texts: list[str], **kwargs: Any):
            self.counts["embedding_calls"] += 1
            result = await original_embed(texts, **kwargs)
            if (
                isinstance(result, list)
                and len(result) == len(texts)
                and result
                and all(isinstance(row, list) and row for row in result)
            ):
                self.counts["embedding_successes"] += 1
            return result

        embeddings.embed = embed
        self._restore.append(lambda: setattr(embeddings, "embed", original_embed))

        searcher = self.app.state.hybrid_searcher
        original_reranker = searcher._reranker
        if original_reranker is not None:

            async def reranker(query: str, rows: list[dict[str, Any]]):
                self.counts["reranker_calls"] += 1
                before_ids = {str(item.get("id") or "") for item in rows}
                result = await original_reranker(query, rows)
                after_ids = (
                    {str(item.get("id") or "") for item in result if isinstance(item, Mapping)}
                    if isinstance(result, list)
                    else set()
                )
                valid_scores = bool(
                    isinstance(result, list)
                    and before_ids == after_ids
                    and result
                    and all(
                        isinstance(item.get("_rerank_score"), (int, float))
                        for item in result
                        if isinstance(item, Mapping)
                    )
                )
                if valid_scores:
                    self.counts["reranker_successes"] += 1
                return result

            searcher._reranker = reranker
            self._restore.append(lambda: setattr(searcher, "_reranker", original_reranker))

        kernel = self.app.state.kernel
        original_execute = kernel.execute

        async def execute(name: str, arguments: dict[str, Any], **kwargs: Any):
            if name in {"web_search", "web_fetch", "web_research"}:
                # In Friday, a zero daily quota means "quota disabled", not
                # "network disabled".  Keep the requested settings sentinel,
                # but independently make any ordinary web-tool attempt a
                # content-free battery failure after the turn finishes.
                self.counts["forbidden_web_calls"] += 1
                raise BatteryFailure("external_web_tool_attempted")
            if name == "workspace_create":
                self.counts["workspace_create_kernel_attempts"] += 1
            result = await original_execute(name, arguments, **kwargs)
            if name == "source_search":
                self.counts["source_search_calls"] += 1
                if result.success:
                    self.counts["source_search_successes"] += 1
                data = _mapping(result.data)
                raw_rows = data.get("results")
                rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
                coverage = _mapping(data.get("coverage"))
                first = _mapping(rows[0]) if rows else {}
                self.last_source_search = {
                    "success": bool(result.success),
                    "raw_ids": [
                        str(item.get("raw_object_id") or "") for item in rows if isinstance(item, Mapping)
                    ],
                    "first_excerpt": str(first.get("excerpt") or ""),
                    "first_match_kind": str(first.get("retrieval_match_kind") or ""),
                    "coverage": {
                        key: coverage.get(key)
                        for key in (
                            "complete",
                            "semantic_recall",
                            "semantic_reranked",
                            "uploader_scoped",
                        )
                    },
                }
            elif name == "workspace_create" and result.success:
                self.counts["workspace_create_kernel"] += 1
            return result

        kernel.execute = execute
        self._restore.append(lambda: setattr(kernel, "execute", original_execute))

        async def llm_chat(*args: Any, **kwargs: Any):
            from friday.agent_runtime.llm import LLMDeadlineError

            stage = self._generation_stage.get() or "unclassified"
            if stage not in _GENERATION_STAGES:  # pragma: no cover - private invariant
                stage = "unclassified"
            self.counts["llm_chat_attempts"] += 1
            self.counts[f"{stage}_started"] += 1
            if stage == "map":
                self.counts["map_active"] += 1
                self.counts["map_peak_active"] = max(
                    self.counts["map_peak_active"],
                    self.counts["map_active"],
                )
            try:
                result = await original_llm_chat(*args, **kwargs)
            except asyncio.CancelledError:
                self.counts[f"{stage}_cancellations"] += 1
                raise
            except LLMDeadlineError as exc:
                self.counts[f"{stage}_failures"] += 1
                self.counts[f"generation_{exc.phase}_timeouts"] += 1
                raise
            except BaseException:
                self.counts[f"{stage}_failures"] += 1
                raise
            else:
                self.counts[f"{stage}_completed"] += 1
                return result
            finally:
                if stage == "map":
                    self.counts["map_active"] -= 1

        llm.chat = llm_chat
        self._restore.append(lambda: setattr(llm, "chat", original_llm_chat))

        original_primary = agent._attachment_primary_chat

        async def primary(*args: Any, **kwargs: Any):
            token: contextvars.Token[str | None] | None = None
            if self._generation_stage.get() is None:
                token = self._generation_stage.set("direct_synthesis")
            try:
                return await original_primary(*args, **kwargs)
            finally:
                if token is not None:
                    self._generation_stage.reset(token)

        agent._attachment_primary_chat = primary
        self._restore.append(lambda: setattr(agent, "_attachment_primary_chat", original_primary))

        original_reduce = agent._reduce_attachment_map_records

        async def reduce(*args: Any, **kwargs: Any):
            token = self._generation_stage.set("reduce")
            try:
                return await original_reduce(*args, **kwargs)
            finally:
                self._generation_stage.reset(token)

        agent._reduce_attachment_map_records = reduce
        self._restore.append(lambda: setattr(agent, "_reduce_attachment_map_records", original_reduce))

        original_hierarchy = agent._build_attachment_hierarchy_bundle

        async def hierarchy(*args: Any, **kwargs: Any):
            self.counts["hierarchy_calls"] += 1
            token = self._generation_stage.set("map")
            try:
                result = await original_hierarchy(*args, **kwargs)
            except asyncio.CancelledError:
                self.counts["hierarchy_cancellations"] += 1
                raise
            except BaseException:
                self.counts["hierarchy_failures"] += 1
                raise
            else:
                bundle = result[0] if isinstance(result, tuple) and len(result) == 2 else None
                raw_planned = (
                    bundle.get("chunks_planned")
                    if isinstance(bundle, Mapping)
                    else getattr(bundle, "chunks_planned", None)
                )
                if type(raw_planned) is int and raw_planned >= 0:
                    self.counts["map_planned"] += raw_planned
                else:
                    self.counts["generation_telemetry_missing"] += 1
                if isinstance(result, tuple) and len(result) == 2 and result[1] is True:
                    self.counts["hierarchy_complete"] += 1
                return result
            finally:
                self._generation_stage.reset(token)

        agent._build_attachment_hierarchy_bundle = hierarchy
        self._restore.append(lambda: setattr(agent, "_build_attachment_hierarchy_bundle", original_hierarchy))

        original_hierarchical_response = agent._hierarchical_attachment_response

        async def hierarchical_response(*args: Any, **kwargs: Any):
            token = self._generation_stage.set("final_synthesis")
            try:
                return await original_hierarchical_response(*args, **kwargs)
            finally:
                self._generation_stage.reset(token)

        agent._hierarchical_attachment_response = hierarchical_response
        self._restore.append(
            lambda: setattr(agent, "_hierarchical_attachment_response", original_hierarchical_response)
        )

        original_verify = agent._verify_response

        async def verify(*args: Any, **kwargs: Any):
            token = self._generation_stage.set("verifier")
            try:
                return await original_verify(*args, **kwargs)
            finally:
                self._generation_stage.reset(token)

        agent._verify_response = verify
        self._restore.append(lambda: setattr(agent, "_verify_response", original_verify))

        original_late_make_file = agent._file_for_a_request_that_wanted_one

        async def late_make_file(*args: Any, **kwargs: Any):
            self.counts["late_make_file_attempts"] += 1
            return await original_late_make_file(*args, **kwargs)

        agent._file_for_a_request_that_wanted_one = late_make_file
        self._restore.append(
            lambda: setattr(agent, "_file_for_a_request_that_wanted_one", original_late_make_file)
        )

        original_send = httpx.AsyncClient.send
        embedding_base = str(self.app.state.settings.embeddings_base_url).rstrip("/")
        rerank_base = str(self.app.state.settings.rerank_base_url).rstrip("/")

        async def send(client: Any, request: Any, *args: Any, **kwargs: Any):
            url = str(request.url)
            if embedding_base and url.startswith(embedding_base) and request.url.path.endswith("/embeddings"):
                self.counts["embedding_http"] += 1
            if rerank_base and url.startswith(rerank_base) and request.url.path.endswith("/rerank"):
                self.counts["reranker_http"] += 1
            return await original_send(client, request, *args, **kwargs)

        cast(Any, httpx.AsyncClient).send = send
        self._restore.append(lambda: setattr(httpx.AsyncClient, "send", original_send))

        manager = getattr(self.app.state, "mcp", None)
        if manager is not None:
            original_call = manager.call_tool

            async def call_tool(alias: str, name: str, arguments: dict[str, Any]):
                if alias == "workspace" and name == "exchange_create":
                    self.counts["workspace_create_mcp_attempts"] += 1
                result = await original_call(alias, name, arguments)
                if alias == "workspace" and name == "exchange_create":
                    self.counts["workspace_create_mcp"] += 1
                return result

            manager.call_tool = call_tool
            self._restore.append(lambda: setattr(manager, "call_tool", original_call))

    def close(self) -> None:
        for restore in reversed(self._restore):
            restore()
        self._restore.clear()


class Harness:
    def __init__(
        self,
        app: Any,
        client: Any,
        settings: Any,
        run_dir: Path,
        run_index: int,
        identity: CaseIdentity | None = None,
    ) -> None:
        self.app = app
        self.client = client
        self.settings = settings
        self.storage = app.state.storage
        self.run_dir = run_dir
        self.run_index = run_index
        self.identity = identity
        chats = (
            _run_owner_chats(identity.run_id, run_index)
            if identity is not None
            else tuple(9911000 + index for index in range(1, 12))
        )
        self.owner_chats = {item.case_id: chats[index] for index, item in enumerate(SCENARIOS)}
        self.jbl_chat = chats[-1]
        self.sequence = 0
        self.raw_evidence: list[dict[str, Any]] = []
        self.probes = LiveProbes(app)
        self.probes.install()
        owner_case = identity.case_id if identity is not None else "D01"
        self.owner_id = self._me(self.owner_chats[owner_case])["actor"]["user_id"]
        self.jbl_id = self._me(self.jbl_chat)["actor"]["user_id"]
        self.storage.update_user(self.jbl_id, display_name="JBL", username="jbl", preset_key="user")

    def close(self) -> None:
        self.probes.close()

    def _headers(self, method: str, path: str, body: bytes, chat: int) -> dict[str, str]:
        from friday.security import sign_bridge_request

        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        secret = str(self.settings.telegram_bridge_secret)
        return {
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": str(chat),
            "X-Friday-Chat": str(chat),
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                secret,
                timestamp=timestamp,
                method=method,
                path=path,
                external_user_id=str(chat),
                chat_id=str(chat),
                nonce=nonce,
                body=body,
            ),
        }

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        chat: int,
        case_id: str,
    ) -> dict[str, Any]:
        body = _canonical_json(payload) if payload is not None else b""
        response = self.client.request(
            method,
            path,
            content=body or None,
            headers=self._headers(method, path, body, chat),
        )
        try:
            parsed = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        self.raw_evidence.append(
            {
                "case_id": case_id,
                "request": payload,
                "status": response.status_code,
                "response": parsed,
            }
        )
        if response.status_code != 200 or not isinstance(parsed, dict):
            raise BatteryFailure(f"{case_id}_http_failure")
        return parsed

    def _me(self, chat: int) -> dict[str, Any]:
        return self._call("GET", "/api/me", None, chat=chat, case_id="BOOT")

    def chat(
        self,
        case_id: str,
        message: str,
        *,
        chat: int | None = None,
        document: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if self.identity is not None and case_id != self.identity.case_id:
            raise BatteryFailure("case_harness_identity_mismatch")
        self.sequence += 1
        active_chat = chat if chat is not None else self.owner_chats[case_id]
        if self.identity is not None:
            top_source_ref = f"document-live:{self.identity.token(f'chat-ref:{self.sequence}')}"
            telegram_message_id = int(self.identity.token(f"message:{self.sequence}", length=15), 16)
            username = f"synthetic_{case_id.casefold()}_{self.identity.token('telegram-user', length=8)}"
        else:
            top_source_ref = f"document-live:{self.run_index}:{case_id}:{self.sequence}"
            telegram_message_id = self.run_index * 100_000 + self.sequence
            username = f"synthetic_{case_id.casefold()}"
        if active_chat == self.jbl_chat:
            telegram_user = {
                "id": active_chat,
                "first_name": "JBL",
                "last_name": "",
                "username": "jbl",
                "language_code": "ru",
            }
        else:
            telegram_user = {
                "id": active_chat,
                "first_name": "Synthetic",
                "last_name": case_id,
                "username": username,
                "language_code": "ru",
            }
        payload: dict[str, Any] = {
            "message": _scoped_prompt(self, f"{case_id}:{self.sequence}", message),
            "source_ref": top_source_ref,
            "telegram_message_id": telegram_message_id,
            "telegram_user": telegram_user,
            "enable_tools": True,
            **fields,
        }
        if document is not None:
            payload["document"] = document
        return self._call("POST", "/api/chat", payload, chat=active_chat, case_id=case_id)

    @staticmethod
    def document(filename: str, mime_type: str, payload: bytes, source_ref: str) -> dict[str, Any]:
        return {
            "filename": filename,
            "mime_type": mime_type,
            "media_kind": "document",
            "source_ref": source_ref,
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }

    def message_row(self, response: Mapping[str, Any]) -> dict[str, Any]:
        message_id = str(response.get("message_id") or "")
        row = self.storage.get_message(message_id, self.owner_id) if message_id else None
        return dict(row or {})

    def last_user_metadata(self, response: Mapping[str, Any]) -> dict[str, Any]:
        conversation_id = str(response.get("conversation_id") or "")
        rows = self.storage.get_conversation_messages(
            conversation_id,
            user_id=self.owner_id,
            limit=100,
        )
        users = [row for row in rows if row.get("role") == "user"]
        return _json_metadata(users[-1] if users else None)

    def resolve_ref(self, source_ref: str, *, uploader: str | None = None) -> str:
        raw_id = self.storage.resolve_owned_file_source_ref(
            self.owner_id,
            uploader or self.owner_id,
            source_ref,
        )
        return str(raw_id or "")

    def ingest(
        self,
        case_id: str,
        payload: bytes,
        filename: str,
        *,
        uploader: str | None = None,
        source_ref: str = "",
        archive_password: str | None = None,
    ) -> dict[str, Any]:
        owner = uploader or self.owner_id
        channel = "document-live-battery"
        fallback_ref = f"battery-seed:{self.run_index}:{case_id}:{secrets.token_hex(6)}"
        if self.identity is not None:
            channel = self.identity.cache_prefix
            fallback_ref = self.identity.source_ref(f"seed-{self.sequence}-{secrets.token_hex(4)}")
        result = asyncio.run(
            self.app.state.ingestion.ingest_file(
                self.owner_id,
                None,
                payload,
                filename=filename,
                metadata={"uploaded_by": owner, "channel": channel},
                source_ref=source_ref or fallback_ref,
                archive_password=archive_password,
            )
        )
        self.raw_evidence.append({"case_id": case_id, "ingestion": result})
        return result

    def require_promoted(self, case_id: str, result: Mapping[str, Any]) -> tuple[str, str]:
        raw_id = str(result.get("raw_object_id") or "")
        knowledge = result.get("knowledge_object")
        knowledge_id = str(knowledge.get("id") or "") if isinstance(knowledge, Mapping) else ""
        if result.get("promoted") is not True or not raw_id or not knowledge_id:
            raise BatteryFailure(f"{case_id}_seed_not_promoted")
        return raw_id, knowledge_id

    def case_result(
        self,
        case_id: str,
        started: float,
        checks: Mapping[str, bool],
        counters: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        failed = sorted(key for key, value in checks.items() if value is not True)
        return {
            "case_id": case_id,
            "status": "passed" if not failed else "failed",
            "failure_codes": [f"{case_id}_{name}" for name in failed],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "checks": {key: bool(value) for key, value in checks.items()},
            "counters": {key: int(value) for key, value in (counters or {}).items()},
        }


def _generation_integrity_checks(
    counters: Mapping[str, int],
    *,
    hierarchy_required: bool,
) -> dict[str, bool]:
    """Validate semantic route completion without freezing implementation counts.

    The release cares that every admitted generation is classified, completed,
    and accounted for, and that a hierarchy covers every planned MAP leaf.  It
    does not care whether a safe response needed zero or one verifier pass, nor
    how many conservative leaves a future tokenizer-safe planner selects.
    """

    telemetry_complete = all(
        key in counters and type(counters[key]) is int and counters[key] >= 0
        for key in _GENERATION_TELEMETRY_KEYS
    )
    values = {key: int(counters[key]) if telemetry_complete else -1 for key in _GENERATION_TELEMETRY_KEYS}

    def stage_closed(stage: str) -> bool:
        return bool(
            telemetry_complete
            and values[f"{stage}_started"] == values[f"{stage}_completed"]
            and values[f"{stage}_failures"] == 0
            and values[f"{stage}_cancellations"] == 0
        )

    planned = values["map_planned"]
    classified_started = sum(values[f"{stage}_started"] for stage in _GENERATION_STAGES)
    no_failures = bool(
        telemetry_complete
        and values["hierarchy_failures"] == 0
        and all(values[f"{stage}_failures"] == 0 for stage in _GENERATION_STAGES)
    )
    no_cancellations = bool(
        telemetry_complete
        and values["hierarchy_cancellations"] == 0
        and all(values[f"{stage}_cancellations"] == 0 for stage in _GENERATION_STAGES)
    )
    no_unclassified = bool(
        telemetry_complete and all(values[f"unclassified_{outcome}"] == 0 for outcome in _GENERATION_OUTCOMES)
    )
    attempts_accounted = bool(
        telemetry_complete and no_unclassified and values["llm_chat_attempts"] == classified_started
    )
    stages_closed = bool(telemetry_complete and all(stage_closed(stage) for stage in _GENERATION_STAGES))
    hierarchy_closed = bool(
        telemetry_complete
        and values["hierarchy_failures"] == 0
        and values["hierarchy_cancellations"] == 0
        and values["hierarchy_calls"] == values["hierarchy_complete"]
    )
    direct_route_complete = bool(
        telemetry_complete
        and not hierarchy_required
        and hierarchy_closed
        and values["hierarchy_calls"] == 0
        and planned == 0
        and values["map_started"] == values["map_completed"] == values["map_active"] == 0
        and values["map_peak_active"] == 0
        and values["direct_synthesis_completed"] >= 1
    )
    hierarchy_route_complete = bool(
        telemetry_complete
        and hierarchy_required
        and hierarchy_closed
        and values["hierarchy_calls"] >= 1
        and planned > 0
        and values["map_started"] == values["map_completed"] == planned
        and values["map_active"] == 0
        and values["direct_synthesis_started"] == 0
        and values["final_synthesis_completed"] >= 1
    )
    return {
        "generation_telemetry_complete": bool(
            telemetry_complete and values["generation_telemetry_missing"] == 0
        ),
        "generation_stages_complete": stages_closed,
        "direct_route_complete": direct_route_complete if not hierarchy_required else True,
        "hierarchy_route_complete": hierarchy_route_complete if hierarchy_required else True,
        "map_concurrency_within_limit": bool(
            telemetry_complete and values["map_peak_active"] == (1 if hierarchy_required else 0)
        ),
        "generation_failures_zero": no_failures,
        "generation_cancellations_zero": no_cancellations,
        "unclassified_generations_zero": no_unclassified,
        "generation_attempts_accounted": attempts_accounted,
    }


def _case_01(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "ALIAS-ORBIT")
    target = _odt_bytes([f"Контрольный код документа: {marker}."], title="Canonical alias")
    decoy_marker = _marker(h, "DECOY-NEWEST")
    decoy = _odt_bytes([f"Контрольный код: {decoy_marker}."], title="Wrong newest")
    refs = {label: _source_ref(h, label) for label in ("ALIAS-A", "ALIAS-B", "DECOY")}
    for label in ("ALIAS-A", "ALIAS-B"):
        h.ingest(
            "D01",
            target,
            _filename(h, "канонический отчёт", "odt", fallback="канонический отчёт.odt"),
            source_ref=refs[label],
        )
    h.ingest(
        "D01",
        decoy,
        _filename(h, "другой новый отчёт", "odt", fallback="другой новый отчёт.odt"),
        source_ref=refs["DECOY"],
    )
    first = h.resolve_ref(refs["ALIAS-A"])
    second = h.resolve_ref(refs["ALIAS-B"])
    decoy_id = h.resolve_ref(refs["DECOY"])
    answer = h.chat(
        "D01",
        "Какой контрольный код указан именно в этом документе?",
        reply_document_source_ref=refs["ALIAS-B"],
        reply_to="Прими файл.",
    )
    metadata = h.last_user_metadata(answer)
    attached = list(metadata.get("conversation_attachment_raw_ids") or [])
    checks = {
        "dedup_alias_same_raw": bool(first and first == second and first != decoy_id),
        "reply_origin_exact": metadata.get("attachment_origin") == "reply_reference",
        "reply_raw_exact": attached == [first],
        "answer_target": marker.casefold() in str(answer.get("message") or "").casefold(),
        "answer_no_decoy": decoy_marker.casefold() not in str(answer.get("message") or "").casefold(),
    }
    return h.case_result("D01", started, checks)


def _case_02(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    chat = h.owner_chats["D02"]
    marker = _marker(h, "LINEAGE-TARGET")
    decoy_marker = _marker(h, "LINEAGE-DECOY")
    deleted_marker = _marker(h, "LINEAGE-DELETED")
    foreign_marker = _marker(h, "LINEAGE-FOREIGN")
    target_ref = _source_ref(h, "LINEAGE-T")
    decoy_ref = _source_ref(h, "LINEAGE-D")
    deleted_ref = _source_ref(h, "LINEAGE-X")
    foreign_ref = _source_ref(h, "LINEAGE-F")
    target_upload = h.chat(
        "D02",
        "Назови контрольный код из этого документа.",
        chat=chat,
        document=h.document(
            _filename(h, "старый источник", "odt", fallback="старый источник.odt"),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes([f"Контрольный код: {marker}."], title="Older exact source"),
            target_ref,
        ),
    )
    target_id = h.resolve_ref(target_ref)
    assistant_metadata = _json_metadata(h.message_row(target_upload))
    h.chat(
        "D02",
        "Прими новый файл.",
        chat=chat,
        document=h.document(
            _filename(
                h,
                "новейший ложный источник",
                "odt",
                fallback="новейший ложный источник.odt",
            ),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes([f"Контрольный код: {decoy_marker}."], title="Newer decoy"),
            decoy_ref,
        ),
    )
    decoy_id = h.resolve_ref(decoy_ref)
    deleted_seed = h.ingest(
        "D02",
        _odt_bytes([f"Контрольный код: {deleted_marker}."], title="Deleted control"),
        _filename(h, "удалённый контроль", "odt", fallback="удалённый контроль.odt"),
        source_ref=deleted_ref,
    )
    deleted_id = str(deleted_seed.get("raw_object_id") or "")
    h.storage.execute(
        "UPDATE raw_objects SET deleted_at=? WHERE id=? AND user_id=?",
        ("2026-08-11T00:00:00+00:00", deleted_id, h.owner_id),
    )
    h.storage.commit()
    foreign_seed = h.ingest(
        "D02",
        _odt_bytes([f"Контрольный код: {foreign_marker}."], title="Foreign control"),
        _filename(h, "чужой контроль", "odt", fallback="чужой контроль.odt"),
        uploader=h.jbl_id,
        source_ref=foreign_ref,
    )
    foreign_id = str(foreign_seed.get("raw_object_id") or "")
    reply = h.chat(
        "D02",
        "Повтори контрольный код именно из источника процитированного ответа.",
        chat=chat,
        reply_source_message_id=str(target_upload.get("message_id") or ""),
        reply_to=str(target_upload.get("message") or "")[:1000],
    )
    user_metadata = h.last_user_metadata(reply)
    attached = list(user_metadata.get("conversation_attachment_raw_ids") or [])
    source_lineage = list(assistant_metadata.get("conversation_attachment_raw_ids") or [])
    checks = {
        "assistant_owned_target": bool(
            assistant_metadata.get("attachment_context_used") is True and source_lineage == [target_id]
        ),
        "reply_origin_exact": user_metadata.get("attachment_origin") == "reply_assistant",
        "controls_distinct": bool(
            deleted_id and foreign_id and len({target_id, decoy_id, deleted_id, foreign_id}) == 4
        ),
        "deleted_control_closed": bool(h.resolve_ref(deleted_ref) == ""),
        "foreign_control_scoped": bool(
            h.resolve_ref(foreign_ref, uploader=h.jbl_id) == foreign_id and h.resolve_ref(foreign_ref) == ""
        ),
        "reply_raw_exact": bool(
            attached == [target_id] and not {decoy_id, deleted_id, foreign_id}.intersection(attached)
        ),
        "answer_target": marker.casefold() in str(reply.get("message") or "").casefold(),
        "answer_no_decoy": decoy_marker.casefold() not in str(reply.get("message") or "").casefold(),
        "answer_no_deleted": deleted_marker.casefold() not in str(reply.get("message") or "").casefold(),
        "answer_no_foreign": foreign_marker.casefold() not in str(reply.get("message") or "").casefold(),
    }
    return h.case_result("D02", started, checks)


_D03_PROMPT = "В ранее загруженном файле «список камендатур ЛНР» найди отдел в Молодогвардейске и его код."


def _case_03(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "ОТДЕЛ-МОЛОДОГВАРДЕЙСК")
    target = h.ingest(
        "D03",
        _odt_bytes(
            [
                "Список комендатур Луганской Народной Республики.",
                f"Молодогвардейск — отдел координации, код {marker}.",
            ],
            title="Список комендатур ЛНР 2026",
        ),
        _filename(
            h,
            "Список комендатур Луганской Народной Республики 2026",
            "odt",
            fallback="Список комендатур Луганской Народной Республики 2026.odt",
        ),
        source_ref=_source_ref(h, "COMMANDANTS"),
    )
    decoy_scope = _marker(h, "SUV-CONTROL")
    decoy = h.ingest(
        "D03",
        _xlsx_bytes((("СУВ", "Отдел"), ("5_222", "Совсем другой город"), ("Контроль", decoy_scope))),
        _filename(h, "СУВ 5_222", "xlsx", fallback="СУВ 5_222.xlsx"),
        source_ref=_source_ref(h, "SUV-DECOY"),
    )
    answer = h.chat(
        "D03",
        _D03_PROMPT,
    )
    metadata = h.last_user_metadata(answer)
    attached = list(metadata.get("conversation_attachment_raw_ids") or [])
    checks = {
        "target_ingested": bool(target.get("raw_object_id")),
        "decoy_ingested": bool(decoy.get("raw_object_id")),
        "fuzzy_target_selected": attached == [str(target.get("raw_object_id") or "")],
        "decoy_not_selected": str(decoy.get("raw_object_id") or "") not in attached,
        "answer_target": marker.casefold() in str(answer.get("message") or "").casefold(),
    }
    return h.case_result("D03", started, checks)


def _index_barrier(h: Harness, case_id: str, knowledge_ids: Sequence[str]) -> None:
    from friday.retrieval import chunk_scheme

    deadline = time.monotonic() + 240
    attempts = 0
    while attempts < 24 and time.monotonic() < deadline:
        attempts += 1
        missing = h.storage.count_knowledge_missing_embedding(
            h.settings.embeddings_model,
            chunk_scheme=chunk_scheme(h.settings),
            chunk_threshold=h.settings.embeddings_chunk_chars,
        )
        if missing == 0:
            break
        indexed = asyncio.run(h.app.state.workers._embeddings_index_pass())
        if indexed == 0:
            raise BatteryFailure(f"{case_id}_embedding_index_stalled")
    else:
        raise BatteryFailure(f"{case_id}_embedding_index_timeout")
    placeholders = ",".join("?" for _ in knowledge_ids)
    rows = h.storage.execute(
        f"""SELECT knowledge_object_id, COUNT(*) AS count
              FROM knowledge_embeddings
             WHERE knowledge_object_id IN ({placeholders}) AND model=?
             GROUP BY knowledge_object_id""",  # nosec B608 - closed placeholders only
        (*knowledge_ids, h.settings.embeddings_model),
    ).fetchall()
    embedded = {str(row["knowledge_object_id"]) for row in rows if int(row["count"]) > 0}
    if embedded != set(knowledge_ids):
        raise BatteryFailure(f"{case_id}_target_vectors_missing")


def _case_04(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "КАПИТАН-ОРЛОВ")
    target_result = h.ingest(
        "D04",
        _xlsx_bytes(
            (
                ("Подразделение РЭБ", "", ""),
                ("Командир взвода", f"капитан Орлов {marker}", "узел Северный"),
                ("", "", ""),
                ("Тыловое обеспечение", "кладовщик", "узел Южный"),
            )
        ),
        _filename(h, "штатное расписание", "xlsx", fallback="штатное расписание.xlsx"),
        source_ref=_source_ref(h, "SEM-XLSX"),
    )
    seeds = [h.require_promoted("D04", target_result)]
    for index, text in enumerate(
        (
            "Подразделение связи. Командир аппаратной — капитан Соколов.",
            "Радиоэлектронная защита оборудования выполняется дежурной группой.",
            "Штатное расписание тыловой службы и командиры отделений.",
        ),
        start=1,
    ):
        scoped_text = f"{text} Контроль выборки: {_marker(h, f'SEM-DECOY-{index}')}"
        result = h.ingest(
            "D04",
            _odt_bytes([scoped_text], title=f"Semantic decoy {index}"),
            _filename(
                h,
                f"семантический кандидат {index}",
                "odt",
                fallback=f"семантический кандидат {index}.odt",
            ),
            source_ref=_source_ref(
                h,
                f"SEM-DECOY-{index}",
                fallback=f"telegram-file:SEM-DECOY-{h.run_index}-{index}",
            ),
        )
        seeds.append(h.require_promoted("D04", result))
    _index_barrier(h, "D04", [knowledge_id for _raw_id, knowledge_id in seeds])
    before = h.probes.snapshot()
    prompt = "Посмотри в ранее загруженной штатке: кто командиром взвода РЭБ числится?"
    from friday.agent_runtime import _archived_source_search_focus, _archived_source_search_query

    routed_query = _archived_source_search_query(prompt)
    routed_focus = _archived_source_search_focus(prompt, routed_query)
    if not routed_query or not routed_focus:
        raise BatteryFailure("D04_source_search_route_not_recognized")
    answer = h.chat(
        "D04",
        prompt,
    )
    delta = h.probes.delta(before)
    target_raw = seeds[0][0]
    source = h.probes.last_source_search
    coverage = _mapping(source.get("coverage"))
    checks = {
        "query_embedding_real": delta["embedding_successes"] >= 1 and delta["embedding_http"] >= 1,
        "query_reranker_real": delta["reranker_successes"] >= 1 and delta["reranker_http"] >= 1,
        "source_search_real": delta["source_search_successes"] >= 1,
        "semantic_coverage": coverage.get("semantic_recall") is True,
        "semantic_reranked": coverage.get("semantic_reranked") is True,
        "semantic_not_exhaustive": coverage.get("complete") is False,
        "target_first": bool(source.get("raw_ids") and source["raw_ids"][0] == target_raw),
        "canonical_excerpt": _contains_all(
            str(source.get("first_excerpt") or ""),
            ("Подразделение РЭБ", "Командир взвода", "капитан Орлов", marker),
        ),
        "answer_target": _d04_answer_has_requested_identity(str(answer.get("message") or "")),
        "no_false_absence": "не найден" not in _normalized(str(answer.get("message") or "")),
    }
    return h.case_result("D04", started, checks, delta)


def _case_05(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    expected: list[str] = []
    dated_ids: list[tuple[int, str]] = []
    expected_markers = tuple(_marker(h, label) for label in ("JBL-FIRST", "JBL-SECOND", "JBL-THIRD"))
    for index, (day, marker) in enumerate(zip((7, 9, 11), expected_markers, strict=True), 1):
        nested_source_ref = _source_ref(
            h,
            f"JBL-{index}",
            fallback=f"telegram-file:JBL-{h.run_index}-{index}",
        )
        h.chat(
            "D05",
            "Прими документ.",
            chat=h.jbl_chat,
            document=h.document(
                _filename(h, f"jbl-{index}", "odt", fallback=f"jbl-{index}.odt"),
                "application/vnd.oasis.opendocument.text",
                _odt_bytes([marker], title=f"JBL {index}"),
                nested_source_ref,
            ),
        )
        raw_id = h.resolve_ref(nested_source_ref, uploader=h.jbl_id)
        if not raw_id or raw_id in expected:
            raise BatteryFailure("D05_fixture_source_ref_resolution_failed")
        expected.append(raw_id)
        dated_ids.append((day, raw_id))
    foreign = h.ingest(
        "D05",
        _odt_bytes([_marker(h, "FOREIGN-DECOY")], title="Foreign owner decoy"),
        _filename(h, "foreign-decoy", "odt", fallback="foreign-decoy.odt"),
        source_ref=_source_ref(h, "FOREIGN"),
    )
    # Seed every upload before opening the direct fixture transaction.  Leaving
    # the first UPDATE pending while the next TestClient/ingestion request starts
    # its own transaction on the shared Storage connection raises SQLite's
    # "cannot start a transaction within a transaction" before product code is
    # exercised at all.
    for day, raw_id in dated_ids:
        h.storage.execute(
            "UPDATE raw_objects SET received_at=? WHERE id=?",
            (f"2026-08-{day:02d}T09:00:00+00:00", raw_id),
        )
    h.storage.execute(
        "UPDATE raw_objects SET received_at=? WHERE id=?",
        ("2026-08-08T09:00:00+00:00", str(foreign.get("raw_object_id") or "")),
    )
    h.storage.commit()
    answer = h.chat(
        "D05",
        "Обобщи данные, которые приходили от пользователя GBL с 7 по 11 августа 2026 года; назови все три маркера.",
    )
    metadata = h.last_user_metadata(answer)
    selected = list(metadata.get("conversation_attachment_raw_ids") or [])
    authorized = h.storage.get_searchable_file_sources(
        h.owner_id,
        selected,
        uploaded_by=h.jbl_id,
        include_content=False,
        limit=max(1, len(selected)),
    )
    answer_text = str(answer.get("message") or "")
    checks = {
        "all_expected_ids": bool(
            len(expected) == len(set(expected)) == 3 and selected == list(reversed(expected))
        ),
        "uploader_reauthorized": bool(
            len(selected) == len(authorized) == 3
            and [str(row.get("id") or "") for row in authorized] == selected
        ),
        "foreign_excluded": str(foreign.get("raw_object_id") or "") not in selected,
        "answer_all_markers": _contains_all(
            answer_text,
            expected_markers,
        ),
        "answer_no_foreign": _marker(h, "FOREIGN-DECOY").casefold() not in answer_text.casefold(),
    }
    return h.case_result("D05", started, checks)


def _case_06(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    markers = (
        _marker(h, "SMALL-ALPHA"),
        _marker(h, "SMALL-BETA"),
        _marker(h, "SMALL-GAMMA"),
    )
    paragraphs = [
        f"Краткий служебный материал. Первый факт {markers[0]}.",
        f"Второй факт {markers[1]}. Третий факт {markers[2]}.",
        "Материал предназначен только для обобщения; никаких внешних действий не требуется.",
    ]
    before = h.probes.snapshot()
    answer = h.chat(
        "D06",
        (
            "Сделай краткую сводку материала. Затем отдельной строкой дословно перечисли "
            "все три значения после меток «Первый факт», «Второй факт» и «Третий факт»; "
            "не пропускай ни одно."
        ),
        document=h.document(
            _filename(h, "малый материал", "odt", fallback="малый материал.odt"),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes(paragraphs, title="Small fit first"),
            _source_ref(h, "SMALL"),
        ),
    )
    delta = h.probes.delta(before)
    text = str(answer.get("message") or "")
    file_ingestion = _mapping(answer.get("file_ingestion"))
    extraction = _mapping(file_ingestion.get("extraction"))
    metadata = _json_metadata(h.message_row(answer))
    partial_words = ("не весь", "частичн", "не удалось разобрать", "не удалось обработать")
    checks = {
        "summary_has_all_facts": _contains_all(text, markers),
        "no_false_partial": not any(word in _normalized(text) for word in partial_words),
        "source_complete": not any(
            extraction.get(key) is True
            for key in (
                "text_truncated",
                "parse_deadline_reached",
                "parse_pages_truncated",
                "archive_truncated",
                "source_truncated_for_parse",
            )
        ),
        "fit_first_no_hierarchy": delta["hierarchy_calls"] == 0,
        "attachment_owned": metadata.get("attachment_context_used") is True,
        "no_deed_guard": metadata.get("fabricated_outside_deed_request") is not True,
        **_generation_integrity_checks(delta, hierarchy_required=False),
    }
    return h.case_result("D06", started, checks, delta)


def _case_07(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "SCAN-PAGE-FIVE")
    fixture_scope = _marker(h, "SCAN-SCOPE")
    answer = h.chat(
        "D07",
        (
            "Прочитай все пять страниц скана. На пятой странице найди строку "
            "SECRET CODE и дословно перепиши полное значение сразу под ней, без сокращений."
        ),
        document=h.document(
            _filename(h, "пятистраничный скан", "pdf", fallback="пятистраничный скан.pdf"),
            "application/pdf",
            _scan_pdf(marker, fixture_scope=fixture_scope),
            _source_ref(h, "SCAN"),
        ),
    )
    file_ingestion = _mapping(answer.get("file_ingestion"))
    extraction = _mapping(file_ingestion.get("extraction"))
    text = str(answer.get("message") or "")
    checks = {
        "ocr_beyond_page_four": int(extraction.get("vision_pages_read") or 0) >= 5,
        "ocr_total_known": int(extraction.get("vision_pages_total") or 0) == 5,
        "ocr_not_truncated": extraction.get("parse_pages_truncated") is not True,
        "answer_target": marker.casefold() in text.casefold(),
        "advisory_not_verified": answer.get("verified") is not True,
        "advisory_visible": bool(answer.get("verification_caution") or answer.get("grounding_warning")),
    }
    return h.case_result("D07", started, checks)


def _case_08(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    markers = (
        _marker(h, "LONG-HEAD"),
        _marker(h, "LONG-MIDDLE"),
        _marker(h, "LONG-TAIL"),
    )
    fixture_scope = _marker(h, "LONG-SCOPE")

    # D08 runs with an isolated 8K context profile.  The varied source remains
    # larger than four such contexts and crosses four real MAP leaves, while
    # avoiding the repeated-token trap of the old 400-line near-duplicate.
    payload = _long_document_fixture(markers, fixture_scope=fixture_scope).encode("utf-8")
    before = h.probes.snapshot()
    answer = h.chat(
        "D08",
        "Обобщи весь документ целиком и отдельно перечисли контрольные коды из начала, середины и хвоста.",
        document=h.document(
            _filename(h, "большой документ", "txt", fallback="большой документ.txt"),
            "text/plain",
            payload,
            _source_ref(h, "LONG"),
        ),
    )
    delta = h.probes.delta(before)
    extraction = _mapping(_mapping(answer.get("file_ingestion")).get("extraction"))
    checks = {
        "fixture_larger_than_model_context": len(payload.decode("utf-8"))
        > int(h.settings.profile.max_model_len) * 4,
        "answer_head_middle_tail": _contains_all(str(answer.get("message") or ""), markers),
        "parser_source_complete": not any(
            extraction.get(key) is True
            for key in (
                "text_truncated",
                "parse_deadline_reached",
                "parse_pages_truncated",
                "source_truncated_for_parse",
            )
        ),
        **_generation_integrity_checks(delta, hierarchy_required=True),
    }
    return h.case_result("D08", started, checks, delta)


def _secret_variants(secret: str) -> tuple[str, ...]:
    values = [secret, unicodedata.normalize("NFC", secret), unicodedata.normalize("NFD", secret)]
    return tuple(dict.fromkeys(values))


def _tree_contains_any(root: Path, needles: Sequence[bytes]) -> bool:
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "private-evidence.json":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(needle and needle in data for needle in needles):
            return True
    return False


def _case_09(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    identity = getattr(h, "identity", None)
    password_token = (
        identity.token("archive-password", length=12)
        if isinstance(identity, CaseIdentity)
        else secrets.token_hex(6)
    )
    password = f"  Cafe\u0301-{password_token}-🔐  "
    marker = _marker(h, "ARCHIVE-NESTED")
    inner_name = _filename(h, "nested/document", "odt", fallback="nested/document.odt")
    archive = _encrypted_zip(
        inner_name,
        _odt_bytes([f"Вложенный контрольный код: {marker}."], title="Nested protected"),
        password,
    )
    source_ref = _source_ref(h, "ENCRYPTED")
    archive_name = _filename(h, "защищённый", "zip", fallback="защищённый.zip")
    initial_row = h.storage.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (h.owner_id,),
    ).fetchone()
    initial_count = int(initial_row["count"] if initial_row else 0)
    missing = h.chat(
        "D09",
        "Какой код находится во вложенном документе?",
        document=h.document(archive_name, "application/zip", archive, source_ref),
    )
    after_missing_row = h.storage.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (h.owner_id,),
    ).fetchone()
    after_missing_count = int(after_missing_row["count"] if after_missing_row else 0)
    missing_raw_id = h.resolve_ref(source_ref)
    success = h.chat(
        "D09",
        "Какой код находится во вложенном документе?",
        document=h.document(archive_name, "application/zip", archive, source_ref),
        archive_password=password,
    )
    after_row = h.storage.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (h.owner_id,),
    ).fetchone()
    after_count = int(after_row["count"] if after_row else 0)
    success_ingestion = _mapping(success.get("file_ingestion"))
    persisted_raw_id = h.resolve_ref(source_ref)
    variants = tuple(value.encode("utf-8") for value in _secret_variants(password))
    checks = {
        "challenge_required": bool(
            missing.get("archive_password_required") is True
            and (missing.get("file_ingestion") or {}).get("persisted") is False
        ),
        "missing_not_persisted": bool(after_missing_count == initial_count and not missing_raw_id),
        "success_persisted_once": bool(
            success_ingestion.get("persisted") is True
            and persisted_raw_id
            and after_count == initial_count + 1
        ),
        "answer_nested_marker": marker.casefold() in str(success.get("message") or "").casefold(),
        "secret_not_in_state": not _tree_contains_any(h.run_dir, variants),
    }
    # The private evidence intentionally held the request during the turn.  Erase
    # this synthetic secret before the evidence file is written, without reporting
    # which normalized candidate the extractor accepted.
    for record in h.raw_evidence:
        if record.get("case_id") == "D09" and isinstance(record.get("request"), dict):
            record["request"].pop("archive_password", None)
    return h.case_result("D09", started, checks)


_D10_ATTEMPT_COUNTERS = (
    "llm_chat_attempts",
    "late_make_file_attempts",
    "workspace_create_kernel_attempts",
    "workspace_create_mcp_attempts",
)


def _closed_d10_subturn(
    response: Mapping[str, Any],
    started: float,
    counters: Mapping[str, int],
    *,
    reply_ref_bound: bool | None = None,
) -> dict[str, Any]:
    """Content-free routing evidence for one returned D10 HTTP turn."""

    raw_files = response.get("files")
    raw_tools = response.get("tools_used")
    context = _mapping(response.get("context"))
    closed = {
        "duration_ms": round((time.monotonic() - started) * 1000),
        # Harness._call raises on every non-200/non-object response, so reaching
        # this projection is itself the closed HTTP-success signal.
        "http_returned": True,
        "llm_failed": context.get("llm_failed") is True,
        "files_count": len(raw_files) if isinstance(raw_files, list) else 0,
        "tools_count": len(raw_tools) if isinstance(raw_tools, list) else 0,
        "attempts": {name: int(counters.get(name, 0)) for name in _D10_ATTEMPT_COUNTERS},
    }
    if reply_ref_bound is not None:
        closed["reply_ref_bound_before"] = bool(reply_ref_bound)
    return closed


def _case_10(h: Harness) -> dict[str, Any]:
    started = time.monotonic()
    marker = _marker(h, "META-EXPORT")
    identity = getattr(h, "identity", None)
    number = (
        f"17-ДСП/{identity.token('document-number', length=8).upper()}"
        if isinstance(identity, CaseIdentity)
        else f"17-ДСП/{h.run_index}"
    )
    body_date = "10 августа 2026 года"
    body = (
        "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
        f"ПРИКАЗ № {number}",
        f"Дата документа: {body_date}",
        f"Контрольный маркер: {marker}",
        "Подписант: начальник отдела Иван Иванович Иванов",
    )
    source_ref = _source_ref(h, "METADATA")
    regular_name = _filename(h, "metadata-export", "docx", fallback="metadata-export.docx")
    mcp_name = _filename(h, "mcp-metadata", "txt", fallback="mcp-metadata.txt")
    metadata_before = h.probes.snapshot()
    metadata_started = time.monotonic()
    metadata = h.chat(
        "D10",
        "Покажи все технические метаданные контейнера и все видимые реквизиты этого документа.",
        document=h.document(
            _filename(h, "приказ с реквизитами", "odt", fallback="приказ с реквизитами.odt"),
            "application/vnd.oasis.opendocument.text",
            _odt_bytes(
                body,
                title="Технический заголовок контейнера",
                creator="Редактор Контейнера",
                creation_date="2022-02-03T04:05:06+00:00",
                modified_date="2022-02-04T05:06:07+00:00",
            ),
            source_ref,
        ),
    )
    metadata_diagnostic = _closed_d10_subturn(
        metadata,
        metadata_started,
        h.probes.delta(metadata_before),
    )
    text = str(metadata.get("message") or "")
    regular_reply_ref_bound = bool(h.resolve_ref(source_ref))
    regular_before = h.probes.snapshot()
    regular_started = time.monotonic()
    regular = h.chat(
        "D10",
        f"Создай обычный Word-файл {regular_name} по процитированному документу. "
        "Включи ровно четыре строки: гриф, номер документа, видимую дату документа "
        "и подписанта из предыдущего ответа.",
        reply_document_source_ref=source_ref,
        reply_to=text[:1000],
    )
    regular_diagnostic = _closed_d10_subturn(
        regular,
        regular_started,
        h.probes.delta(regular_before),
        reply_ref_bound=regular_reply_ref_bound,
    )
    raw_files = regular.get("files")
    files: list[Any] = raw_files if isinstance(raw_files, list) else []
    regular_payload = b""
    if files and isinstance(files[0], Mapping):
        try:
            regular_payload = base64.b64decode(str(files[0].get("content_base64") or ""), validate=True)
        except (TypeError, ValueError):
            regular_payload = b""
    regular_text = ""
    regular_extraction_success = False
    if regular_payload and files and isinstance(files[0], Mapping):
        from friday.documents import DocumentExtractor

        extracted = DocumentExtractor(secret_values=()).extract(
            regular_payload,
            str(files[0].get("filename") or regular_name),
            str(files[0].get("mime_type") or ""),
        )
        regular_extraction_success = extracted.success
        regular_text = extracted.text if extracted.success else ""
    regular_lines = _docx_non_title_lines(regular_payload)
    regular_expected_fields = tuple(
        _normalized(value)
        for value in (
            "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
            number,
            body_date,
            "Иван Иванович Иванов",
        )
    )
    regular_exact_four_lines = bool(
        len(regular_lines) == len(regular_expected_fields)
        and all(
            expected in regular_lines[index]
            and all(
                other not in regular_lines[index]
                for other_index, other in enumerate(regular_expected_fields)
                if other_index != index
            )
            for index, expected in enumerate(regular_expected_fields)
        )
    )
    mcp_reply_ref_bound = bool(h.resolve_ref(source_ref))
    before = h.probes.snapshot()
    mcp_started = time.monotonic()
    mcp = h.chat(
        "D10",
        f"Используй именно workspace_create и создай в MCP outbox файл {mcp_name}. "
        "Первая строка — только значение номера документа без подписи. Вторая строка — "
        "только значение контрольного маркера без подписи. Никаких других строк.",
        reply_document_source_ref=source_ref,
        reply_to=text[:1000],
    )
    delta = h.probes.delta(before)
    mcp_diagnostic = _closed_d10_subturn(
        mcp,
        mcp_started,
        delta,
        reply_ref_bound=mcp_reply_ref_bound,
    )
    outbox = Path(str(h.settings.mcp_workspace_outbox_dir)) / mcp_name
    outbox_bytes = outbox.read_bytes() if outbox.is_file() else b""
    outbox_lines = tuple(
        _normalized(line)
        for line in outbox_bytes[:8_193].decode("utf-8", "ignore").splitlines()
        if line.strip()
    )
    before_overwrite = _sha256(outbox_bytes) if outbox_bytes else ""
    overwrite_refused = False
    if outbox.is_file():
        from friday.permissions import ActorContext

        owner = ActorContext(
            user_id=h.owner_id,
            person_id=h.owner_id,
            preset_key="owner",
            source="document-live-battery",
        )

        async def repeat_workspace_create() -> Any:
            return await h.app.state.kernel.execute(
                "workspace_create",
                {"filename": mcp_name, "content": "must-not-overwrite"},
                actor=owner,
            )

        repeated = h.client.portal.call(repeat_workspace_create)
        overwrite_refused = bool(
            repeated.success is False
            and outbox.is_file()
            and _sha256(outbox.read_bytes()) == before_overwrite
        )
    checks = {
        "technical_title": "Технический заголовок контейнера".casefold() in text.casefold(),
        "technical_creator": "Редактор Контейнера".casefold() in text.casefold(),
        "technical_all_stored_fields": _contains_all(
            text,
            (
                "Дата создания в свойствах контейнера: 2022-02-03",
                "Дата изменения в свойствах контейнера: 2022-02-04",
                "Циклы редактирования: 7",
                "Страницы: 3",
                "Абзацы: 8",
                "Слова: 44",
            ),
        ),
        "technical_dates_distinct": "2022-02-03" in text and body_date.casefold() in text.casefold(),
        "visible_requisites": _contains_all(text, (number, "служебного пользования", "Иван Иванович Иванов")),
        "regular_file_delivered": bool(
            len(files) == 1
            and isinstance(files[0], Mapping)
            and str(files[0].get("filename") or "") == regular_name
            and regular_payload
            and regular_extraction_success
        ),
        "regular_file_grounded": _contains_all(
            regular_text,
            (number, body_date, "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ", "Иван Иванович Иванов"),
        ),
        "regular_file_exact_four_lines": regular_exact_four_lines,
        "mcp_kernel_real": delta["workspace_create_kernel"] == 1,
        "mcp_transport_real": delta["workspace_create_mcp"] == 1,
        "mcp_exact_content": bool(
            len(outbox_bytes) <= 8_192 and outbox_lines == (_normalized(number), _normalized(marker))
        ),
        "mcp_private_mode": bool(outbox.is_file() and stat.S_IMODE(outbox.stat().st_mode) == 0o600),
        "mcp_create_only": overwrite_refused,
        "mcp_reported_tool": "workspace_create" in list(mcp.get("tools_used") or []),
        "mcp_no_duplicate_chat_file": not list(mcp.get("files") or []),
    }
    result = h.case_result("D10", started, checks, delta)
    result["diagnostics"] = {
        "subturns": {
            "metadata": metadata_diagnostic,
            "regular": regular_diagnostic,
            "mcp": mcp_diagnostic,
        }
    }
    return result


_CASE_RUNNERS: tuple[Callable[[Harness], dict[str, Any]], ...] = (
    _case_01,
    _case_02,
    _case_03,
    _case_04,
    _case_05,
    _case_06,
    _case_07,
    _case_08,
    _case_09,
    _case_10,
)

_RUNNER_BY_CASE_ID = {
    scenario.case_id: runner for scenario, runner in zip(SCENARIOS, _CASE_RUNNERS, strict=True)
}
_LIVE_CASE_RUNNERS: tuple[Callable[[Harness], dict[str, Any]], ...] = tuple(
    _RUNNER_BY_CASE_ID[scenario.case_id] for scenario in LIVE_SCENARIOS
)


def _assert_worker_settings(settings: Any, run_dir: Path, *, require_mcp: bool) -> None:
    profile = getattr(settings, "profile", None)
    if str(getattr(profile, "name", "") or "") != _RELEASE_PROFILE:
        raise BatteryFailure("release_profile_mismatch")
    map_concurrency = getattr(profile, "document_map_max_concurrency", None)
    if type(map_concurrency) is not int or map_concurrency != 1:
        raise BatteryFailure("document_map_concurrency_not_one")
    for path in (
        settings.home,
        settings.data_dir,
        settings.cache_dir,
        settings.log_dir,
        settings.state_dir,
        settings.database_path,
        settings.files_dir,
        settings.memory_vault_dir,
        settings.backups_dir,
        settings.exports_dir,
        settings.mcp_workspace_inbox_dir,
        settings.mcp_workspace_outbox_dir,
    ):
        if path is None or not _inside(Path(path), run_dir):
            raise BatteryFailure("worker_path_not_isolated")
    if settings.workers_enabled or settings.code_execution_enabled:
        raise BatteryFailure("unsafe_worker_feature_enabled")
    if settings.web_daily_quota != 0:
        raise BatteryFailure("web_access_not_disabled")
    if not settings.llm_enabled:
        raise BatteryFailure("llm_not_enabled")
    if not settings.embeddings_enabled or not settings.embeddings_model:
        raise BatteryFailure("embeddings_not_enabled")
    if settings.rerank_top <= 0 or not settings.rerank_base_url or not settings.rerank_model:
        raise BatteryFailure("reranker_not_enabled")
    if require_mcp and not settings.mcp_enabled:
        raise BatteryFailure("mcp_not_enabled")


def _settings_for_case(
    base: Any,
    run_dir: Path,
    case_id: str,
    identity: CaseIdentity | None = None,
) -> tuple[Any, Path, Path]:
    paths = case_state_paths(run_dir, case_id, identity)
    for key, path in paths.items():
        if key not in {"database", "evidence"}:
            _private_dir(path)
    mcp_enabled = case_id == "D10"
    profile = replace(base.profile, max_model_len=8_192) if case_id == "D08" else base.profile
    settings = replace(
        base,
        profile=profile,
        home=paths["root"],
        data_dir=paths["data"],
        cache_dir=paths["cache"],
        log_dir=paths["logs"],
        model_root=paths["models"],
        model_dir=paths["models"] / base.profile.model_dir_name,
        state_dir=paths["state"],
        database_path=paths["database"],
        database_must_exist=False,
        files_dir=paths["files"],
        memory_vault_dir=paths["memory"],
        backups_dir=paths["backups"],
        exports_dir=paths["exports"],
        backup_mirror_dir=None,
        backup_encryption_key_file=None,
        whisper_download_root=str(paths["models"] / "whisper"),
        tts_download_root=str(paths["models"] / "tts"),
        mcp_enabled=mcp_enabled,
        mcp_workspace_inbox_dir=paths["mcp_inbox"],
        mcp_workspace_outbox_dir=paths["mcp_outbox"],
    )
    return settings, paths["root"], paths["evidence"]


def execute_worker(run_index: int) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    run_dir = Path(os.environ["FRIDAY_DOCUMENT_BATTERY_RUN_DIR"]).resolve()
    evidence_path = Path(os.environ["FRIDAY_DOCUMENT_BATTERY_EVIDENCE"]).resolve()
    run_id = _validated_run_id(os.environ.get(_RUN_ID_ENV, ""))
    run_hash = _run_id_hash(run_id)
    if not 1 <= run_index <= RUNS or not _inside(evidence_path, run_dir):
        raise BatteryFailure("worker_request_invalid")
    from friday.config import ensure_runtime_dirs, load_settings, validate_settings

    base_settings = load_settings()
    _assert_worker_settings(base_settings, run_dir, require_mcp=True)
    problems = [item for item in validate_settings(base_settings) if not item.startswith("warning:")]
    if problems:
        raise BatteryFailure("isolated_settings_invalid")
    lifecycle_audit = LifecycleAudit()
    lifecycle_audit.install()
    from friday.server import create_app

    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for scenario, runner in zip(LIVE_SCENARIOS, _LIVE_CASE_RUNNERS, strict=True):
        identity = _case_identity(run_id, run_index, scenario.case_id)
        state = case_state_paths(run_dir, scenario.case_id, identity)
        case_dir = _private_dir(state["root"])
        case_evidence_path = state["evidence"]
        raw_evidence: list[dict[str, Any]] = []
        result: dict[str, Any]
        try:
            settings, case_dir, case_evidence_path = _settings_for_case(
                base_settings,
                run_dir,
                scenario.case_id,
                identity,
            )
            _assert_worker_settings(
                settings,
                case_dir,
                require_mcp=scenario.case_id == "D10",
            )
            case_problems = [item for item in validate_settings(settings) if not item.startswith("warning:")]
            if case_problems:
                raise BatteryFailure("isolated_case_settings_invalid")
            ensure_runtime_dirs(settings)
            app = create_app(settings)
            with TestClient(app) as client:
                manager = getattr(app.state, "mcp", None)
                if scenario.case_id == "D10" and (manager is None or not manager.is_available("workspace")):
                    raise BatteryFailure("mcp_workspace_unavailable")
                harness = Harness(app, client, settings, case_dir, run_index, identity)
                try:
                    result = runner(harness)
                    if harness.probes.counts["forbidden_web_calls"]:
                        raise BatteryFailure("external_web_tool_attempted")
                    raw_evidence = harness.raw_evidence
                finally:
                    harness.close()
        except BatteryFailure as exc:
            result = {
                "case_id": scenario.case_id,
                "status": "failed",
                "failure_codes": [str(exc)],
                "duration_ms": 0,
                "checks": {},
                "counters": {},
            }
        except Exception as exc:  # noqa: BLE001 - private trace stays in worker log
            result = {
                "case_id": scenario.case_id,
                "status": "failed",
                "failure_codes": [f"{scenario.case_id}_exception_{type(exc).__name__}"],
                "duration_ms": 0,
                "checks": {},
                "counters": {},
            }
        finally:
            _private_write(
                case_evidence_path,
                _canonical_json(
                    {
                        "schema": WORKER_SCHEMA,
                        "run_index": run_index,
                        "run_id_hash": run_hash,
                        "case_id": scenario.case_id,
                        "fresh_database": True,
                        "raw_private_evidence": raw_evidence,
                        "closed_result": result,
                    }
                ),
            )
        result["fresh_database"] = True
        results.append(result)
    lifecycle_audit.close()
    lifecycle_failure_codes = lifecycle_audit.closed_failure_codes()
    lifecycle_teardown_clear = not lifecycle_failure_codes
    _private_write(
        evidence_path,
        _canonical_json(
            {
                "schema": WORKER_SCHEMA,
                "run_index": run_index,
                "run_id_hash": run_hash,
                "fresh_database_per_case": True,
                "lifecycle_teardown_clear": lifecycle_teardown_clear,
                "lifecycle_failure_codes": list(lifecycle_failure_codes),
                "closed_results": results,
            }
        ),
    )
    return {
        "schema": WORKER_SCHEMA,
        "run_index": run_index,
        "run_id_hash": run_hash,
        "status": (
            "passed"
            if lifecycle_teardown_clear and all(item["status"] == "passed" for item in results)
            else "failed"
        ),
        "failure_codes": list(lifecycle_failure_codes),
        "lifecycle_teardown_clear": lifecycle_teardown_clear,
        "lifecycle_failure_codes": list(lifecycle_failure_codes),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "cases": results,
    }


def _worker_main(args: argparse.Namespace) -> int:
    try:
        # The controller blocks its control-signal set across Popen and PGID
        # binding.  POSIX children inherit that mask, so undo it before any
        # worker setup; a TERM pending since spawn is delivered here.
        _unblock_worker_control_signals()
        result = execute_worker(int(args.run_index))
    except Exception as exc:  # noqa: BLE001 - emit a closed code only
        result = {
            "schema": WORKER_SCHEMA,
            "run_index": int(args.run_index),
            "status": "failed",
            "failure_codes": [f"worker_exception_{type(exc).__name__}"],
            "lifecycle_teardown_clear": False,
            "lifecycle_failure_codes": [],
            "cases": [],
        }
    sys.stdout.buffer.write(_canonical_json(result) + b"\n")
    return 0 if result.get("status") == "passed" else 1


def _require_posix_signal_lifecycle() -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "killpg")
        or not hasattr(signal, "pthread_sigmask")
        or not hasattr(signal, "SIG_BLOCK")
        or not hasattr(signal, "SIG_SETMASK")
        or not hasattr(signal, "SIG_UNBLOCK")
        or not hasattr(signal, "sigpending")
        or not hasattr(signal, "sigtimedwait")
    ):
        raise BatteryFailure("worker_process_groups_unsupported")


def _block_controller_signals() -> frozenset[Any]:
    """Block INT+TERM together and return the calling thread's old mask."""

    _require_posix_signal_lifecycle()
    return frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, _CONTROLLER_SIGNALS))


def _restore_signal_mask(previous: frozenset[Any]) -> None:
    _require_posix_signal_lifecycle()
    signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _unblock_worker_control_signals() -> None:
    """First worker action after exec: reverse the mask inherited at spawn."""

    _require_posix_signal_lifecycle()
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _CONTROLLER_SIGNALS)


def _drain_pending_controller_signals() -> bool:
    """Consume a bounded number of repeat signals while the set is blocked."""

    _require_posix_signal_lifecycle()
    targets = frozenset(_CONTROLLER_SIGNALS)
    for _attempt in range(_MAX_PENDING_CONTROLLER_SIGNAL_DRAINS):
        pending = targets.intersection(signal.sigpending())
        if not pending:
            return True
        # Another runtime thread may consume a process-pending signal between
        # sigpending() and the wait.  A zero-timeout consume keeps finalization
        # bounded while still draining every signal that remains pending here.
        if signal.sigtimedwait(pending, 0) is None:
            return True
    return not bool(targets.intersection(signal.sigpending()))


def _process_group_exists(process_group: int) -> bool:
    _require_posix_signal_lifecycle()
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _wait_process_group_clear(process_group: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _signal_process_group(process_group: int, selected_signal: int) -> bool:
    if not _process_group_exists(process_group):
        return False
    try:
        os.killpg(process_group, selected_signal)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise BatteryFailure("worker_process_group_signal_failed") from exc
    return True


def _cleanup_bound_worker(
    process: subprocess.Popen[bytes],
    process_group: int,
) -> WorkerCleanupOutcome:
    """Run one bounded TERM/wait/KILL/reap/final-audit sequence.

    Group cleanup is deliberately independent of the leader state: an exited
    leader may have left a TERM-ignoring descendant in its session.  The caller
    blocks both controller signals before entering this function.  Every phase
    is best-effort so even an unexpected BaseException cannot skip the final
    group audit; the first such BaseException is returned for truthful unwind.
    """

    cleanup_codes: set[str] = set()
    deferred: BaseException | None = None
    stdout = b""

    def record_cleanup_exception(exc: BaseException) -> None:
        nonlocal deferred
        cleanup_codes.add("worker_cleanup_exception")
        if not isinstance(exc, Exception) and deferred is None:
            deferred = exc

    term_sent = False
    try:
        term_sent = _signal_process_group(process_group, signal.SIGTERM)
    except BaseException as exc:  # cleanup must continue through final audit
        record_cleanup_exception(exc)
    if term_sent:
        cleanup_codes.add("worker_group_term_sent")

    clear_after_term = False
    try:
        clear_after_term = _wait_process_group_clear(
            process_group,
            PROCESS_GROUP_TERM_GRACE_SEC,
        )
    except BaseException as exc:  # cleanup must still attempt KILL
        record_cleanup_exception(exc)

    if not clear_after_term:
        kill_sent = False
        try:
            kill_sent = _signal_process_group(process_group, signal.SIGKILL)
        except BaseException as exc:  # reap and final audit are still mandatory
            record_cleanup_exception(exc)
        if kill_sent:
            cleanup_codes.add("worker_group_kill_sent")

    worker_reaped = False
    try:
        worker_reaped = process.poll() is not None
    except BaseException as exc:
        record_cleanup_exception(exc)

    if not worker_reaped:
        try:
            stdout, _stderr = process.communicate(timeout=PROCESS_GROUP_KILL_GRACE_SEC)
            worker_reaped = process.poll() is not None
        except subprocess.TimeoutExpired:
            cleanup_codes.add("worker_leader_not_reaped")
        except BaseException as exc:  # final group audit must still execute
            record_cleanup_exception(exc)
            try:
                worker_reaped = process.poll() is not None
            except BaseException as poll_exc:
                record_cleanup_exception(poll_exc)
    if not worker_reaped:
        cleanup_codes.add("worker_leader_not_reaped")

    process_group_clear = False
    try:
        process_group_clear = _wait_process_group_clear(
            process_group,
            PROCESS_GROUP_KILL_GRACE_SEC,
        )
    except BaseException as exc:
        record_cleanup_exception(exc)
    if not process_group_clear:
        cleanup_codes.add("worker_process_group_not_clear")

    return WorkerCleanupOutcome(
        stdout=stdout,
        worker_reaped=worker_reaped,
        process_group_clear=process_group_clear,
        cleanup_failure_codes=tuple(sorted(cleanup_codes)),
        deferred_baseexception=deferred,
    )


def _run_worker_process(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    private_log: Any,
    controller_signal_handlers: ControllerSignalHandlers | None = None,
) -> WorkerProcessOutcome:
    """Run one worker under a single fail-closed spawn-to-audit lifecycle."""

    _require_posix_signal_lifecycle()
    if controller_signal_handlers is not None:
        initial_mask = _block_controller_signals()
        try:
            # Until a bound final audit proves otherwise, a signal at any later
            # Python delivery boundary must report worker cleanup as uncertain.
            controller_signal_handlers.worker_cleanup_clear = False
            controller_signal_handlers.worker_cleanup_failure_codes = ("worker_process_group_not_clear",)
        finally:
            _restore_signal_mask(initial_mask)
    process: subprocess.Popen[bytes] | None = None
    process_group: int | None = None
    timed_out = False
    cleanup_codes: set[str] = set()
    stdout = b""
    worker_reaped = False
    process_group_clear_initial = False
    process_group_clear = False
    cleanup_required = False
    primary: BaseException | None = None
    primary_traceback: Any = None
    cleanup_outcome: WorkerCleanupOutcome | None = None
    lifecycle_pending = True
    cleanup_started = False
    cleanup_mask: frozenset[Any] | None = None

    def capture_primary(exc: BaseException) -> None:
        nonlocal primary, primary_traceback
        if primary is None or (
            isinstance(exc, ControllerSignal) and not isinstance(primary, ControllerSignal)
        ):
            primary = exc
            primary_traceback = exc.__traceback__

    # A signal may be raised at any Python boundary, including immediately
    # before the first cleanup-mask syscall.  Retrying only that not-yet-started
    # cleanup phase under the mask set by the signal handler closes this last
    # window without ever spawning twice or running two cleanup sequences.
    while True:
        try:
            if lifecycle_pending:
                lifecycle_pending = False
                # Blocking the complete set in one syscall closes both the
                # Popen return and process-handle/PGID binding windows.  The
                # hidden worker explicitly unblocks the inherited mask first.
                spawn_mask = _block_controller_signals()
                try:
                    process = subprocess.Popen(  # noqa: S603 - fixed candidate-local argv
                        list(command),
                        cwd=ROOT,
                        env=dict(environment),
                        stdout=subprocess.PIPE,
                        stderr=private_log,
                        start_new_session=True,
                        restore_signals=True,
                    )
                    process_group = int(process.pid)
                finally:
                    _restore_signal_mask(spawn_mask)

                try:
                    stdout, _stderr = process.communicate(timeout=WORKER_TIMEOUT_SEC)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    cleanup_required = True
                    cleanup_codes.add("worker_timeout")
                else:
                    worker_reaped = process.poll() is not None
                    # A signal here must enter the same bounded cleanup.
                    process_group_clear_initial = _wait_process_group_clear(
                        process_group,
                        PROCESS_GROUP_EXIT_GRACE_SEC,
                    )
                    process_group_clear = process_group_clear_initial
                    if not process_group_clear_initial:
                        cleanup_codes.add("worker_process_group_survived")
                        cleanup_required = True
                    if not worker_reaped:
                        cleanup_required = True

            if process is not None and process_group is not None and cleanup_required:
                # Repeat INT/TERM delivery is deferred as one atomic set until
                # TERM/wait/KILL/reap/final-audit has completely finished.
                cleanup_mask = _block_controller_signals()
                cleanup_started = True
                cleanup_outcome = _cleanup_bound_worker(process, process_group)
            break
        except BaseException as exc:
            capture_primary(exc)
            cleanup_required = process is not None
            if isinstance(exc, Exception):
                cleanup_codes.add(
                    "worker_cleanup_exception" if cleanup_started else "worker_process_exception"
                )
            if process is not None and process_group is not None and not cleanup_started:
                continue
            break

    if process is None or process_group is None:
        if primary is not None:
            raise primary.with_traceback(primary_traceback)
        raise BatteryFailure("worker_process_spawn_failed")

    cleanup_codes_tuple: tuple[str, ...] = ()

    def attach_cleanup_projection(exc: ControllerSignal) -> None:
        exc.worker_cleanup_clear = bool(worker_reaped and process_group_clear)
        exc.worker_cleanup_failure_codes = cleanup_codes_tuple

    try:
        # Keep INT+TERM blocked until the final audit has been projected into
        # immutable local truth and, for an existing signal, attached to the
        # public exception.  Restoring the mask before this block created a
        # deterministic late-first-signal window that could discard RED.
        if cleanup_outcome is not None:
            if cleanup_outcome.stdout:
                stdout = cleanup_outcome.stdout
            worker_reaped = cleanup_outcome.worker_reaped
            process_group_clear = cleanup_outcome.process_group_clear
            cleanup_codes.update(cleanup_outcome.cleanup_failure_codes)
            if primary is None and cleanup_outcome.deferred_baseexception is not None:
                primary = cleanup_outcome.deferred_baseexception
                primary_traceback = primary.__traceback__

        if not worker_reaped:
            cleanup_codes.add("worker_leader_not_reaped")
        if not process_group_clear:
            cleanup_codes.add("worker_process_group_not_clear")

        cleanup_codes_tuple = tuple(sorted(cleanup_codes))
        if controller_signal_handlers is not None:
            controller_signal_handlers.worker_cleanup_clear = bool(worker_reaped and process_group_clear)
            controller_signal_handlers.worker_cleanup_failure_codes = cleanup_codes_tuple
        if (
            cleanup_mask is not None
            and controller_signal_handlers is not None
            and not isinstance(primary, ControllerSignal)
        ):
            pending = frozenset(_CONTROLLER_SIGNALS).intersection(signal.sigpending())
            if pending:
                selected_signal = int(signal.sigwait(pending))
                if controller_signal_handlers.first_signal is None:
                    controller_signal_handlers.first_signal = selected_signal
                primary = ControllerSignal(int(controller_signal_handlers.first_signal))
                primary_traceback = primary.__traceback__
        if isinstance(primary, ControllerSignal):
            attach_cleanup_projection(primary)

        bound_outcome = WorkerProcessOutcome(
            stdout=stdout,
            returncode=int(process.returncode if process.returncode is not None else -1),
            worker_reaped=worker_reaped,
            process_group_clear_initial=process_group_clear_initial,
            process_group_clear=process_group_clear,
            timed_out=timed_out,
            cleanup_failure_codes=cleanup_codes_tuple,
        )
    except BaseException:
        if cleanup_mask is not None:
            mask_to_restore = cleanup_mask
            cleanup_mask = None
            _restore_signal_mask(mask_to_restore)
        raise

    try:
        if cleanup_mask is not None:
            mask_to_restore = cleanup_mask
            cleanup_mask = None
            _restore_signal_mask(mask_to_restore)
        if isinstance(primary, ControllerSignal):
            raise primary.with_traceback(primary_traceback)
        if primary is not None and not isinstance(primary, Exception):
            if not worker_reaped or not process_group_clear:
                raise BatteryFailure("worker_process_group_not_clear") from primary
            raise primary.with_traceback(primary_traceback)
        return bound_outcome
    except ControllerSignal as exc:
        # Covers a first signal delivered by mask restoration or at any point
        # through the bound return.  Its public projection can no longer be
        # default/empty after a false final audit.
        attach_cleanup_projection(exc)
        raise


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
    completed = subprocess.run(
        [_GIT_BINARY, "-c", "core.fsmonitor=false", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )
    return completed.stdout.strip()


def _validate_live_gate(freeze_commit: str, bridge_stopped: bool) -> str:
    if not bridge_stopped:
        raise BatteryFailure("bridge_stop_assertion_required")
    if re.fullmatch(r"[0-9a-fA-F]{40}", freeze_commit or "") is None:
        raise BatteryFailure("freeze_commit_required")
    head = _git_output("rev-parse", "HEAD")
    resolved = _git_output("rev-parse", f"{freeze_commit}^{{commit}}")
    if resolved != head:
        raise BatteryFailure("freeze_commit_is_not_head")
    if _git_output("status", "--porcelain=v1", "--untracked-files=all"):
        raise BatteryFailure("release_worktree_is_dirty")
    return head


def _controller_source_env_file(value: str) -> Path | None:
    configured = str(value or os.environ.get("FRIDAY_ENV_FILE") or "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        raise BatteryFailure("source_env_file_missing")
    return path


def _worker_lifecycle_projection(report: Mapping[str, Any]) -> tuple[bool, bool, tuple[str, ...]]:
    raw_clear = report.get("lifecycle_teardown_clear")
    raw_codes = report.get("lifecycle_failure_codes")
    if type(raw_clear) is not bool or not isinstance(raw_codes, list):
        return False, False, ()
    codes = tuple(raw_codes)
    if (
        any(not isinstance(code, str) or code not in _LIFECYCLE_FAILURE_CODES for code in codes)
        or len(codes) != len(set(codes))
        or list(codes) != sorted(codes)
        or (raw_clear and codes)
    ):
        return False, False, ()
    return True, bool(raw_clear), codes


def _mark_run_failed(report: dict[str, Any], code: str) -> None:
    report["status"] = "failed"
    raw_codes = report.get("failure_codes")
    codes = [item for item in raw_codes if isinstance(item, str)] if isinstance(raw_codes, list) else []
    if code not in codes:
        codes.append(code)
    report["failure_codes"] = sorted(codes)


def _build_run_receipt(
    *,
    commit: str,
    run_hash: str,
    run_index: int,
    report: Mapping[str, Any],
    worker_report_sha256: str,
) -> dict[str, Any]:
    teardown = report.get("teardown")
    if not isinstance(teardown, Mapping):
        raise BatteryFailure("run_teardown_receipt_missing")
    return {
        "schema": RUN_RECEIPT_SCHEMA,
        "commit": commit,
        "run_id_hash": run_hash,
        "run_index": run_index,
        "worker_report_sha256": worker_report_sha256,
        "worker_status": str(report.get("status") or "failed"),
        "worker_exit_code": int(teardown.get("worker_exit_code", -1)),
        "worker_reaped": teardown.get("worker_reaped") is True,
        "process_group_clear_initial": teardown.get("process_group_clear_initial") is True,
        "process_group_clear": teardown.get("process_group_clear") is True,
        "process_cleanup_failure_codes": list(teardown.get("process_cleanup_failure_codes") or []),
        "lifecycle_contract_clear": teardown.get("lifecycle_contract_clear") is True,
        "lifecycle_teardown_clear": teardown.get("lifecycle_teardown_clear") is True,
        "lifecycle_failure_codes": list(teardown.get("lifecycle_failure_codes") or []),
        "teardown_clear": teardown.get("teardown_clear") is True,
    }


def _persist_run_receipt(
    barrier_dir: _PinnedBarrierDirectory,
    payload: Mapping[str, Any],
) -> tuple[Path, str]:
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
    if set(payload) != exact_keys or payload.get("schema") != RUN_RECEIPT_SCHEMA:
        raise BatteryFailure("run_teardown_receipt_invalid")
    run_index = int(payload.get("run_index") or 0)
    if not 1 <= run_index <= RUNS:
        raise BatteryFailure("run_teardown_receipt_invalid")
    process_codes = payload.get("process_cleanup_failure_codes")
    lifecycle_codes = payload.get("lifecycle_failure_codes")
    if (
        not isinstance(payload.get("commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("commit") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("run_id_hash") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("worker_report_sha256") or "")) is None
        or payload.get("worker_status") not in {"passed", "failed"}
        or type(payload.get("worker_exit_code")) is not int
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
        raise BatteryFailure("run_teardown_receipt_invalid")
    expected_teardown_clear = bool(
        payload["worker_exit_code"] == 0
        and payload["worker_reaped"]
        and payload["process_group_clear_initial"]
        and payload["process_group_clear"]
        and not process_codes
        and payload["lifecycle_contract_clear"]
        and payload["lifecycle_teardown_clear"]
        and not lifecycle_codes
    )
    if payload.get("teardown_clear") is not expected_teardown_clear:
        raise BatteryFailure("run_teardown_receipt_inconsistent")
    name = f"run-{run_index}-receipt.json"
    encoded = _canonical_json(dict(payload)) + b"\n"
    path = _atomic_pinned_private_write(barrier_dir, name, encoded)
    if _read_pinned_private_json(barrier_dir, name) != dict(payload):
        raise BatteryFailure("run_teardown_receipt_changed")
    return path, _sha256(encoded)


def _closed_failure_tokens(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            token
            for token in value
            if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_]{1,128}", token) is not None
        }
    )


def _build_failure_summary(
    *,
    commit: str,
    run_hash: str,
    run_index: int,
    report: Mapping[str, Any],
    worker_report_sha256: str,
) -> dict[str, Any]:
    failed_cases: list[dict[str, Any]] = []
    raw_cases = report.get("cases")
    if isinstance(raw_cases, list):
        for raw_case in raw_cases:
            if not isinstance(raw_case, Mapping) or raw_case.get("status") != "failed":
                continue
            case_id = str(raw_case.get("case_id") or "")
            if case_id not in LIVE_CASE_IDS:
                continue
            raw_checks = raw_case.get("checks")
            failed_checks = (
                sorted(
                    key
                    for key, value in raw_checks.items()
                    if isinstance(key, str)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,128}", key) is not None
                    and value is False
                )
                if isinstance(raw_checks, Mapping)
                else []
            )
            raw_counters = raw_case.get("counters")
            counters = (
                {
                    key: value
                    for key, value in sorted(raw_counters.items())
                    if isinstance(key, str)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,128}", key) is not None
                    and type(value) is int
                    and value >= 0
                }
                if isinstance(raw_counters, Mapping)
                else {}
            )
            failed_cases.append(
                {
                    "case_id": case_id,
                    "failure_codes": _closed_failure_tokens(raw_case.get("failure_codes")),
                    "failed_checks": failed_checks,
                    "counters": counters,
                }
            )
    return {
        "schema": FAILURE_SUMMARY_SCHEMA,
        "commit": commit,
        "run_id_hash": run_hash,
        "run_index": run_index,
        "worker_report_sha256": worker_report_sha256,
        "worker_failure_codes": _closed_failure_tokens(report.get("failure_codes")),
        "failed_cases": sorted(failed_cases, key=lambda item: item["case_id"]),
    }


def _persist_failure_summary(
    barrier_dir: _PinnedBarrierDirectory,
    payload: Mapping[str, Any],
) -> None:
    run_index = int(payload.get("run_index") or 0)
    if (
        set(payload)
        != {
            "schema",
            "commit",
            "run_id_hash",
            "run_index",
            "worker_report_sha256",
            "worker_failure_codes",
            "failed_cases",
        }
        or payload.get("schema") != FAILURE_SUMMARY_SCHEMA
        or not 1 <= run_index <= RUNS
    ):
        raise BatteryFailure("failure_summary_invalid")
    name = f"run-{run_index}-failure-summary.json"
    encoded = _canonical_json(dict(payload)) + b"\n"
    _atomic_pinned_private_write(barrier_dir, name, encoded)
    if _read_pinned_private_json(barrier_dir, name) != dict(payload):
        raise BatteryFailure("failure_summary_changed")


def _observer_request(
    *,
    commit: str,
    run_hash: str,
    receipt_sha256: str,
    worker_report_sha256: str,
    challenge: str,
) -> dict[str, Any]:
    return {
        "schema": OBSERVER_REQUEST_SCHEMA,
        "commit": commit,
        "run_id_hash": run_hash,
        "run_index": 1,
        "run_receipt_sha256": receipt_sha256,
        "worker_report_sha256": worker_report_sha256,
        "challenge": challenge,
    }


def _validate_observer_response(
    response: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    boolean_fields = (
        "bridge_stopped",
        "bridge_operator_guard_held",
        "backend_healthy",
        "backend_unchanged",
        "outbound_pending_zero",
        "inbound_pending_zero",
        "dead_letter_zero",
        "dispatcher_unchanged",
    )
    exact_keys = {
        "schema",
        "commit",
        "run_id_hash",
        "run_index",
        "run_receipt_sha256",
        "worker_report_sha256",
        "challenge",
        "status",
        *boolean_fields,
    }
    if set(response) != exact_keys:
        raise BatteryFailure("inter_run_observer_response_invalid")
    for key in (
        "commit",
        "run_id_hash",
        "run_index",
        "run_receipt_sha256",
        "worker_report_sha256",
        "challenge",
    ):
        if response.get(key) != request.get(key):
            raise BatteryFailure("inter_run_observer_binding_mismatch")
    if response.get("schema") != OBSERVER_RESPONSE_SCHEMA or response.get("status") != "passed":
        raise BatteryFailure("inter_run_observer_not_clear")
    for key in boolean_fields:
        if response.get(key) is not True:
            raise BatteryFailure(f"inter_run_observer_{key}_failed")
    return {
        "schema": OBSERVER_RESPONSE_SCHEMA,
        "status": "passed",
        "run_index": 1,
        "run_receipt_sha256": str(request["run_receipt_sha256"]),
        "worker_report_sha256": str(request["worker_report_sha256"]),
        **{key: True for key in boolean_fields},
    }


def _await_inter_run_observer(
    barrier_dir: _PinnedBarrierDirectory,
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    request_name = "run-1-observer-request.json"
    response_name = "run-1-observer.json"
    request_bytes = _canonical_json(dict(request)) + b"\n"
    _atomic_pinned_private_write(barrier_dir, request_name, request_bytes)
    if _read_pinned_private_json(barrier_dir, request_name) != dict(request):
        raise BatteryFailure("inter_run_observer_request_changed")
    deadline = time.monotonic() + INTER_RUN_OBSERVER_TIMEOUT_SEC
    while True:
        barrier_dir.revalidate()
        try:
            response_metadata = os.stat(
                response_name,
                dir_fd=barrier_dir.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise BatteryFailure("inter_run_observer_timeout") from None
            time.sleep(0.05)
            continue
        _validate_private_regular_file(barrier_dir.path / response_name, response_metadata)
        response = _read_pinned_private_json(barrier_dir, response_name)
        projection = _validate_observer_response(response, request)
        encoded_response = _canonical_json(response) + b"\n"
        return projection, _sha256(encoded_response)


def _install_controller_signal_handlers() -> ControllerSignalHandlers:
    """Install both handlers while their complete signal set is blocked.

    The returned state is deliberately handed to the caller *before* the old
    mask is restored.  This closes the same return/STORE_FAST interruption
    window that the worker spawn lifecycle closes for Popen and its PGID.
    """

    inherited_mask = _block_controller_signals()
    # A parent operator may block INT+TERM across Popen and child-handle
    # binding.  That mask is a spawn transport detail, not controller policy:
    # once both handlers are installed this process must explicitly own and
    # receive its control signals.  Preserve every unrelated inherited mask.
    previous_mask = frozenset(item for item in inherited_mask if item not in _CONTROLLER_SIGNALS)
    state = ControllerSignalHandlers(previous={}, previous_mask=previous_mask)

    def interrupt(signal_number: int, _frame: Any) -> None:
        # The first signal owns the unwind.  A repeat is consumed without
        # raising another BaseException; explicit lifecycle masks still keep it
        # out of cleanup, and the original exception cannot be replaced.
        if state.first_signal is not None:
            return
        state.first_signal = signal_number
        _block_controller_signals()
        projected = ControllerSignal(state.first_signal)
        projected.worker_cleanup_clear = state.worker_cleanup_clear
        projected.worker_cleanup_failure_codes = state.worker_cleanup_failure_codes
        raise projected

    try:
        for selected in _CONTROLLER_SIGNALS:
            state.previous[selected] = signal.getsignal(selected)
            signal.signal(selected, interrupt)
    except BaseException:
        for stored_signal, handler in state.previous.items():
            signal.signal(stored_signal, handler)
        _restore_signal_mask(previous_mask)
        raise
    return state


def _activate_controller_signal_handlers(state: ControllerSignalHandlers) -> None:
    """Unmask only after the caller has bound the installed-handler state."""

    # Record the transition before unmasking.  If a pending signal raises from
    # pthread_sigmask, finalization must treat this as an activated contour.
    state.activated = True
    _restore_signal_mask(state.previous_mask)


def _finalize_controller_signal_handlers(
    state: ControllerSignalHandlers,
    cleanup: Callable[[], None],
) -> None:
    """Run controller cleanup with INT+TERM blocked, then restore dispositions."""

    previous_mask = _block_controller_signals()
    restore_mask = previous_mask if state.activated else state.previous_mask
    try:
        cleanup()
    finally:
        if state.first_signal is None:
            # No controller signal is being preserved.  Restore the caller's
            # dispositions under the full blocked set, then deliver anything
            # that arrived during cleanup according to those dispositions.
            try:
                for selected, handler in state.previous.items():
                    signal.signal(selected, handler)
            finally:
                try:
                    _restore_signal_mask(restore_mask)
                finally:
                    state.activated = False
        else:
            # A first controller signal is already the active unwind reason.
            # Drain only repeats synchronously while the complete set remains
            # blocked.  This avoids both a live sequential-disposition window
            # and Python's pending-signal/SIG_IGN race.
            try:
                _drain_pending_controller_signals()
                for selected, handler in state.previous.items():
                    signal.signal(selected, handler)
            finally:
                try:
                    _drain_pending_controller_signals()
                finally:
                    try:
                        _restore_signal_mask(state.previous_mask)
                    finally:
                        state.activated = False


def run_controller(args: argparse.Namespace) -> dict[str, Any]:
    commit = _validate_live_gate(str(args.freeze_commit or ""), bool(args.bridge_stopped))
    operator_model_env_only = bool(getattr(args, "operator_model_env_only", False))
    explicit_source_env = str(args.source_env_file or "").strip()
    if operator_model_env_only:
        if explicit_source_env:
            raise BatteryFailure("operator_model_env_only_source_env_file_conflict")
        source_env_file = None
    else:
        source_env_file = _controller_source_env_file(explicit_source_env)
    barrier_value = str(getattr(args, "inter_run_barrier_dir", "") or "").strip()
    if not barrier_value:
        raise BatteryFailure("inter_run_barrier_dir_required")
    run_id = _new_run_id()
    run_hash = _run_id_hash(run_id)
    controller_signals = _install_controller_signal_handlers()
    owned_barriers: list[_PinnedBarrierDirectory] = []
    barrier_dir: _PinnedBarrierDirectory | None = None
    private_root: Path | None = None

    def close_owned_barriers() -> None:
        for owned in reversed(owned_barriers):
            owned.close()

    def cleanup_failed_setup() -> None:
        if private_root is not None:
            shutil.rmtree(private_root, ignore_errors=True)
        close_owned_barriers()

    try:
        barrier_dir = _PinnedBarrierDirectory.open(
            Path(barrier_value),
            owner=owned_barriers,
        )
        private_root = Path(tempfile.mkdtemp(prefix="friday-document-live-battery-")).resolve()
        private_root.chmod(0o700)
    except BaseException:
        _finalize_controller_signal_handlers(
            controller_signals,
            cleanup_failed_setup,
        )
        raise
    assert private_root is not None
    assert barrier_dir is not None
    reports: list[dict[str, Any]] = []
    run_receipts: list[dict[str, Any]] = []
    controller_failure_codes: list[str] = []
    observer_projection: dict[str, Any] = {"status": "not_run"}
    observer_response_sha256 = ""

    def cleanup_controller_resources() -> None:
        if not args.keep_private_run_dir:
            shutil.rmtree(private_root, ignore_errors=True)
        close_owned_barriers()

    try:
        # Keep INT+TERM blocked through every setup ownership transfer and
        # until this cleanup-protected contour is active.  A pending signal is
        # delivered here, where both the pinned directory and private root are
        # already bound for unconditional cleanup.
        _activate_controller_signal_handlers(controller_signals)
        for run_index in range(1, RUNS + 1):
            barrier_dir.revalidate()
            run_token = _run_token(run_id, run_index, "state-path")
            run_dir = _private_dir(private_root / f"run-{run_index}-{run_token}")
            owner_chats = _run_owner_chats(run_id, run_index)
            environment = build_worker_environment(
                run_dir,
                owner_chats=owner_chats,
                source_env_file=source_env_file,
                run_id=run_id,
                operator_model_env_only=operator_model_env_only,
            )
            log_path = run_dir / "private-worker.log"
            with _private_worker_log(log_path) as log:
                try:
                    outcome = _run_worker_process(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--worker",
                            "--run-index",
                            str(run_index),
                        ],
                        environment=environment,
                        private_log=log,
                        controller_signal_handlers=controller_signals,
                    )
                except Exception:  # noqa: BLE001 - closed controller code only
                    outcome = WorkerProcessOutcome(
                        stdout=b"",
                        returncode=-1,
                        worker_reaped=False,
                        process_group_clear_initial=False,
                        process_group_clear=False,
                        timed_out=False,
                        cleanup_failure_codes=("worker_process_group_not_clear",),
                    )
            try:
                report = json.loads(outcome.stdout.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                report = {
                    "schema": WORKER_SCHEMA,
                    "run_index": run_index,
                    "status": "failed",
                    "failure_codes": ["worker_output_invalid"],
                    "cases": [],
                }
            if not isinstance(report, dict):
                report = {
                    "schema": WORKER_SCHEMA,
                    "run_index": run_index,
                    "status": "failed",
                    "failure_codes": ["worker_output_invalid"],
                    "cases": [],
                }
            report_identity_clear = bool(
                report.get("schema") == WORKER_SCHEMA
                and report.get("run_index") == run_index
                and report.get("run_id_hash") == run_hash
            )
            if not report_identity_clear:
                report = {
                    "schema": WORKER_SCHEMA,
                    "run_index": run_index,
                    "run_id_hash": run_hash,
                    "status": "failed",
                    "failure_codes": ["worker_identity_mismatch"],
                    "cases": [],
                }
            lifecycle_contract_clear, lifecycle_teardown_clear, lifecycle_codes = (
                _worker_lifecycle_projection(report)
            )
            raw_cleanup_codes = tuple(outcome.cleanup_failure_codes)
            process_cleanup_codes = (
                raw_cleanup_codes
                if (
                    len(raw_cleanup_codes) == len(set(raw_cleanup_codes))
                    and list(raw_cleanup_codes) == sorted(raw_cleanup_codes)
                    and all(code in _PROCESS_CLEANUP_FAILURE_CODES for code in raw_cleanup_codes)
                )
                else ("worker_process_group_not_clear",)
            )
            teardown_clear = bool(
                report_identity_clear
                and outcome.returncode == 0
                and outcome.worker_reaped
                and outcome.process_group_clear_initial
                and outcome.process_group_clear
                and not outcome.timed_out
                and not process_cleanup_codes
                and lifecycle_contract_clear
                and lifecycle_teardown_clear
                and not lifecycle_codes
            )
            report["teardown"] = {
                "worker_report_identity_clear": report_identity_clear,
                "worker_exit_code": int(outcome.returncode),
                "worker_reaped": bool(outcome.worker_reaped),
                "process_group_clear_initial": bool(outcome.process_group_clear_initial),
                "process_group_clear": bool(outcome.process_group_clear),
                "process_cleanup_failure_codes": list(process_cleanup_codes),
                "lifecycle_contract_clear": lifecycle_contract_clear,
                "lifecycle_teardown_clear": lifecycle_teardown_clear,
                "lifecycle_failure_codes": list(lifecycle_codes),
                "teardown_clear": teardown_clear,
            }
            if outcome.returncode != 0:
                _mark_run_failed(report, "worker_exit_nonzero")
            if process_cleanup_codes:
                _mark_run_failed(report, "worker_process_teardown_failed")
            if not lifecycle_contract_clear:
                _mark_run_failed(report, "worker_lifecycle_contract_invalid")
            elif not lifecycle_teardown_clear or lifecycle_codes:
                _mark_run_failed(report, "worker_lifecycle_teardown_failed")
            if not teardown_clear:
                _mark_run_failed(report, "worker_teardown_not_clear")
            reports.append(report)

            worker_report_sha256 = _sha256(_canonical_json(report))
            receipt_payload = _build_run_receipt(
                commit=commit,
                run_hash=run_hash,
                run_index=run_index,
                report=report,
                worker_report_sha256=worker_report_sha256,
            )
            try:
                receipt_path, receipt_sha256 = _persist_run_receipt(barrier_dir, receipt_payload)
                if _read_pinned_private_json(barrier_dir, receipt_path.name) != receipt_payload:
                    raise BatteryFailure("run_teardown_receipt_changed")
            except Exception:  # noqa: BLE001 - never start another worker after receipt uncertainty
                _mark_run_failed(report, "run_teardown_receipt_failed")
                controller_failure_codes.append("run_teardown_receipt_failed")
                break
            run_receipts.append(
                {
                    "run_index": run_index,
                    "sha256": receipt_sha256,
                    "worker_report_sha256": worker_report_sha256,
                    "teardown_clear": teardown_clear,
                }
            )
            if report.get("status") != "passed" or not teardown_clear:
                # A failed first streak must be fixed on a new frozen commit;
                # spending another full live run cannot turn it into 2/2.
                try:
                    _persist_failure_summary(
                        barrier_dir,
                        _build_failure_summary(
                            commit=commit,
                            run_hash=run_hash,
                            run_index=run_index,
                            report=report,
                            worker_report_sha256=worker_report_sha256,
                        ),
                    )
                except Exception:  # noqa: BLE001 - the run is already terminal RED
                    controller_failure_codes.append("failure_summary_write_failed")
                break
            if run_index == 1:
                challenge = _new_run_id()
                request = _observer_request(
                    commit=commit,
                    run_hash=run_hash,
                    receipt_sha256=receipt_sha256,
                    worker_report_sha256=worker_report_sha256,
                    challenge=challenge,
                )
                try:
                    observer_projection, observer_response_sha256 = _await_inter_run_observer(
                        barrier_dir,
                        request,
                    )
                    barrier_dir.revalidate()
                    if _read_pinned_private_json(barrier_dir, receipt_path.name) != receipt_payload:
                        raise BatteryFailure("run_teardown_receipt_changed")
                except BatteryFailure as exc:
                    controller_failure_codes.append(str(exc))
                    observer_projection = {"status": "failed"}
                    break
                except Exception:  # noqa: BLE001 - observer implementation stays private
                    controller_failure_codes.append("inter_run_observer_exception")
                    observer_projection = {"status": "failed"}
                    break
        barrier_dir.revalidate()
        aggregate = {
            "schema": REPORT_SCHEMA,
            "commit": commit,
            "run_id_hash": run_hash,
            "runs_expected": RUNS,
            "runs_completed": len(reports),
            "cases_expected_per_run": LIVE_CASES,
            "failure_codes": sorted(set(controller_failure_codes)),
            "status": (
                "passed"
                if (
                    len(reports) == RUNS
                    and not controller_failure_codes
                    and observer_projection.get("status") == "passed"
                    and all(
                        item.get("status") == "passed"
                        and isinstance(item.get("teardown"), Mapping)
                        and item["teardown"].get("teardown_clear") is True
                        for item in reports
                    )
                )
                else "failed"
            ),
            "run_receipts": run_receipts,
            "inter_run_observer": {
                **observer_projection,
                **({"response_sha256": observer_response_sha256} if observer_response_sha256 else {}),
            },
            "runs": reports,
        }
        if args.keep_private_run_dir:
            aggregate["private_run_dir"] = str(private_root)
        barrier_dir.revalidate()
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            _private_write(report_path, _canonical_json(aggregate) + b"\n")
        barrier_dir.revalidate()
        return aggregate
    finally:
        _finalize_controller_signal_handlers(
            controller_signals,
            cleanup_controller_resources,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="offline manifest/isolation proof only")
    parser.add_argument("--run-live", action="store_true", help="explicitly authorize the live battery")
    parser.add_argument("--freeze-commit", default="", help="exact 40-hex commit frozen for the run")
    parser.add_argument(
        "--source-env-file",
        default="",
        help=(
            "operator env file used only to copy allowlisted local sidecar settings; "
            "defaults to controller FRIDAY_ENV_FILE"
        ),
    )
    parser.add_argument(
        "--operator-model-env-only",
        action="store_true",
        help=(
            "require the complete allowlisted model environment from this controller "
            "and do not read a source env file"
        ),
    )
    parser.add_argument(
        "--bridge-stopped",
        action="store_true",
        help="operator assertion that the production Telegram bridge is stopped",
    )
    parser.add_argument(
        "--inter-run-barrier-dir",
        default="",
        help=(
            "pre-created empty owner-only directory below a dedicated, quiescent "
            "owner-only parent for sanitized run receipts and the external "
            "between-run service/queue attestation"
        ),
    )
    parser.add_argument("--report", default="", help="optional closed aggregate JSON path")
    parser.add_argument(
        "--keep-private-run-dir",
        action="store_true",
        help="retain private raw evidence directory (0600); off by default",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-index", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.worker:
        return _worker_main(args)
    if args.self_test:
        print(json.dumps(offline_self_test(), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.run_live:
        raise SystemExit("Refusing live execution: use --run-live after code freeze and bridge stop")
    try:
        report = run_controller(args)
    except ControllerSignal as exc:
        if exc.worker_cleanup_clear is False:
            sys.stderr.write("controller_signal_worker_cleanup_not_clear\n")
        return 128 + int(exc.signal_number)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
