#!/usr/bin/env python3
"""Durable two-generation disaster-recovery authority.

This module owns only the small durable index.  It deliberately does not find,
authenticate, restore, or rehearse backups: callers must supply exact receipts
from those code-owned operations.  No ordering decision is derived from a glob,
directory timestamp, or mutable symlink.

All state and receipt operations are serialized on the pinned state directory.
Release and retention callers must additionally hold the repository-wide
immutable release operator lock; the state-directory lock is always inner.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INDEX_SCHEMA = "friday.immutable-release-dr-generations.v1"
HEAD_FENCE_SCHEMA = "friday.immutable-release-dr-generation-head-fence.v1"
AUTHENTICATION_RECEIPT_SCHEMA = "friday.immutable-release-dr-authentication-receipt.v1"
REHEARSAL_RECEIPT_SCHEMA = "friday.immutable-release-dr-rehearsal-receipt.v1"
AUTHENTICATION_RECEIPT_SCHEMA_V2 = "friday.immutable-release-dr-authentication-receipt.v2"
REHEARSAL_RECEIPT_SCHEMA_V2 = "friday.immutable-release-dr-rehearsal-receipt.v2"
REHEARSAL_BINDING_SCHEMA = "friday.immutable-release-dr-rehearsal-binding.v1"
GENERATION_CANDIDATE_SCHEMA = "friday.immutable-release-dr-generation-candidate.v1"
GENERATION_SCHEMA = "friday.immutable-release-dr-generation.v1"
GENERATION_RECEIPT_SCHEMA = "friday.immutable-release-dr-generation-receipt.v1"

INDEX_NAME = "immutable-release-dr-generations.v1.json"
RECEIPT_DIRECTORY_NAME = "immutable-release-dr-generation-receipts"
HEAD_FENCE_DIRECTORY_PREFIX = ".immutable-release-dr-generation-heads-v1"
HEAD_FENCE_NAME = "current-head.json"
HEAD_FENCE_STAGING_NAME = ".current-head.json.new"
INDEX_PHASES = ("clear", "prepared", "authenticated", "rehearsed")
INDEX_INTENTS = ("bootstrap_current", "fill_older", "rotate_current")

MAX_INDEX_BYTES = 1 << 20
MAX_HEAD_FENCE_BYTES = 2 << 20
MAX_RECEIPT_BYTES = 1 << 20
MAX_CANDIDATE_BYTES = 1 << 18
ZERO_SHA256 = "0" * 64

AUTHENTICATION_RECEIPT_KIND = "authentication"
REHEARSAL_RECEIPT_KIND = "rehearsal"
ACTIVATION_RECEIPT_KIND = "activation"

_ACTIVATION_RECEIPT_SCHEMA = "friday.immutable-release-activation.v1"
_ACTIVATION_OPERATOR_SCHEMA = "friday.immutable-release-operator.v1"

DR_REHEARSAL_CHECKS = (
    "authenticated_pending_bound",
    "activation_source_reauthenticated",
    "retained_release_identities_verified",
    "database_materialized",
    "inbox_materialized",
    "obsidian_materialized_exactly",
    "engineer_materialized_with_fresh_identity",
    "scratch_checkpoint_authenticated",
    "fault_after_migration_before_provision",
    "rollback_restore_attempted",
    "activation_rolled_back",
    "four_surface_restore_exact",
    "database_reopened_twice",
    "inbox_reopened_twice",
    "zero_systemctl_or_network_calls",
    "scratch_removed_before_admission",
    "source_unchanged_before_cas",
)

_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_SCHEMA_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[a-z0-9.+-]*)?")
_SOURCE_KINDS = frozenset({"explicit_older_adoption", "terminal_activation"})
_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)


class DRGenerationIndexError(RuntimeError):
    """A closed durable-index failure."""


@dataclass(frozen=True)
class GenerationPin:
    """One exact retention pin projected from the authenticated index."""

    role: str
    backup_directory: Path
    candidate: dict[str, Any]
    generation_id: str | None
    receipt_path: Path | None
    receipt_sha256: str | None
    authentication_receipt_path: Path | None
    authentication_receipt_sha256: str | None
    activation_receipt_path: Path | None
    activation_receipt_file_sha256: str | None
    rehearsal_receipt_path: Path | None
    rehearsal_receipt_sha256: str | None
    rehearsal_binding: dict[str, Any] | None
    restore_release_root: Path
    restore_release_commit: str
    restore_release_tree_manifest_sha256: str
    restore_release_wheel_sha256: str
    restore_release_max_schema: int
    restore_release_version: str


@dataclass(frozen=True)
class DRGenerationAuthoritySnapshot:
    """One atomic, self-consistent observation of the durable DR authority."""

    index_path: Path
    index_raw: bytes
    index_sha256: str
    pins: tuple[GenerationPin, ...]


@dataclass(frozen=True)
class CurrentDRGenerationIdentity:
    """Validated, detached identity of the generation in the current slot.

    Authentication and rehearsal receipt bodies intentionally stay private to
    this module.  Their compact references, the complete candidate identity,
    and the exact index revision are sufficient for a caller to prove an
    already-current admission without reopening authority files by path.
    """

    index_journal_sha256: str
    index_phase: str
    index_revision: int
    generation_id: str
    generation_receipt_sha256: str
    candidate: dict[str, Any]
    candidate_sha256: str
    authentication_receipt: dict[str, str]
    rehearsal_receipt: dict[str, str]


@dataclass(frozen=True)
class PendingDRGenerationIdentity:
    """One exact pending identity and its validated external receipt bodies."""

    index_journal_sha256: str
    authenticated_journal_sha256: str
    index_phase: str
    index_revision: int
    index_transaction_id: str
    intent: str
    candidate: dict[str, Any]
    candidate_sha256: str
    authentication_receipt: dict[str, Any]
    rehearsal_receipt: dict[str, Any] | None


@dataclass(frozen=True)
class _PinnedDirectories:
    parent_fd: int
    state_fd: int
    receipt_fd: int
    head_fd: int
    parent_identity: tuple[int, int]
    receipt_identity: tuple[int, int]
    head_identity: tuple[int, int]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise DRGenerationIndexError("noncanonical_json_value") from exc


def _unique_json(raw: bytes, *, code: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise DRGenerationIndexError(code)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise DRGenerationIndexError(code) from exc
    if not isinstance(value, dict):
        raise DRGenerationIndexError(code)
    return value


def _hex64(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise DRGenerationIndexError(code)
    return value


def _schema(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _SCHEMA_RE.fullmatch(value) is None:
        raise DRGenerationIndexError(code)
    return value


def _absolute_lexical(path: Path, *, code: str) -> Path:
    if not path.is_absolute() or any(character in str(path) for character in "\x00\r\n"):
        raise DRGenerationIndexError(code)
    lexical = Path(os.path.abspath(path))
    if lexical != path:
        raise DRGenerationIndexError(code)
    return lexical


def _private_directory(path: Path, *, code: str) -> tuple[Path, os.stat_result]:
    lexical = _absolute_lexical(path, code=code)
    try:
        status = os.lstat(lexical)
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise DRGenerationIndexError(code) from exc
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise DRGenerationIndexError(code)
    return lexical, status


def _entry_name(value: str, *, code: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\x00" in value:
        raise DRGenerationIndexError(code)
    return value


def _stable_private_file_at(
    directory_fd: int,
    name: str,
    *,
    mode: int,
    maximum_bytes: int,
    code: str,
) -> bytes:
    name = _entry_name(name, code=code)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DRGenerationIndexError(code) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode
        or not 0 < before.st_size <= maximum_bytes
    ):
        raise DRGenerationIndexError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != mode
                or not 0 < opened.st_size <= maximum_bytes
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise DRGenerationIndexError(code)
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining == 0 and os.read(descriptor, 1):
                raise DRGenerationIndexError(code)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DRGenerationIndexError(code) from exc

    def identity(value: os.stat_result) -> tuple[int, ...]:
        # Timestamps are race sensors only.  They never select or order a generation.
        return (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_mode),
            int(value.st_nlink),
            int(value.st_uid),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )

    if identity(before) != identity(after_open) or identity(before) != identity(after):
        raise DRGenerationIndexError(code)
    return b"".join(chunks)


def _stable_staging_file_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int,
    code: str,
) -> tuple[bytes, int]:
    """Read one journal-authorized deterministic staging inode, including a partial write."""

    name = _entry_name(name, code=code)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DRGenerationIndexError(code) from exc
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or mode not in {0o400, 0o600}
        or not 0 <= before.st_size <= maximum_bytes
    ):
        raise DRGenerationIndexError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) not in {0o400, 0o600}
                or not 0 <= opened.st_size <= maximum_bytes
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise DRGenerationIndexError(code)
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise DRGenerationIndexError(code) from exc
    identity = lambda value: (  # noqa: E731 - compact immutable race comparison
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after_open) or identity(before) != identity(after):
        raise DRGenerationIndexError(code)
    return b"".join(chunks), mode


def _entry_exists_at(directory_fd: int, name: str) -> bool:
    name = _entry_name(name, code="durable_entry_name_invalid")
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DRGenerationIndexError("durable_entry_observation_failed") from exc
    return True


def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    """Linux atomic no-replace publication within one pinned directory."""

    source = _entry_name(source, code="durable_noreplace_name_invalid")
    destination = _entry_name(destination, code="durable_noreplace_name_invalid")
    try:
        renameat2 = _LIBC.renameat2
    except AttributeError as exc:  # pragma: no cover - production is Linux; fail closed elsewhere.
        raise DRGenerationIndexError("durable_noreplace_unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), destination)


def _replace_private_durable_at_limit(
    directory_fd: int,
    name: str,
    raw: bytes,
    *,
    maximum_bytes: int,
    code: str,
    fixed_temporary_name: str | None = None,
) -> None:
    """Durably replace one bounded file within an already pinned directory."""

    name = _entry_name(name, code=code)
    temporary = fixed_temporary_name or f".{name}.{os.getpid()}.{secrets.token_hex(12)}.new"
    temporary = _entry_name(temporary, code=code)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if fixed_temporary_name is not None:
            try:
                stale = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                stale = None
            if stale is not None:
                if (
                    not stat.S_ISREG(stale.st_mode)
                    or stale.st_uid != os.geteuid()
                    or stale.st_nlink != 1
                    or stat.S_IMODE(stale.st_mode) != 0o600
                    or not 0 <= stale.st_size <= maximum_bytes
                ):
                    raise DRGenerationIndexError(code)
                os.unlink(temporary, dir_fd=directory_fd)
                os.fsync(directory_fd)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(raw)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        staged = _stable_private_file_at(
            directory_fd,
            temporary,
            mode=0o600,
            maximum_bytes=maximum_bytes,
            code=code,
        )
        if staged != raw:
            raise DRGenerationIndexError(code)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        durable = _stable_private_file_at(
            directory_fd,
            name,
            mode=0o600,
            maximum_bytes=maximum_bytes,
            code=code,
        )
        if durable != raw:
            raise DRGenerationIndexError(code)
    except DRGenerationIndexError:
        raise
    except OSError as exc:
        raise DRGenerationIndexError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)


def _replace_private_durable_at(directory_fd: int, name: str, raw: bytes, *, code: str) -> None:
    """Durably replace the bounded mutable index projection."""

    _replace_private_durable_at_limit(
        directory_fd,
        name,
        raw,
        maximum_bytes=MAX_INDEX_BYTES,
        code=code,
    )


def _replace_private_head_durable_at(
    directory_fd: int,
    raw: bytes,
    *,
    code: str,
) -> None:
    """Atomically replace the single bounded external authoritative head."""

    _replace_private_durable_at_limit(
        directory_fd,
        HEAD_FENCE_NAME,
        raw,
        maximum_bytes=MAX_HEAD_FENCE_BYTES,
        code=code,
        fixed_temporary_name=HEAD_FENCE_STAGING_NAME,
    )


def _normalize_external_receipt(value: object, *, code: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"schema", "sha256"}:
        raise DRGenerationIndexError(code)
    return {
        "schema": _schema(value.get("schema"), code=code),
        "sha256": _hex64(value.get("sha256"), code=code),
    }


def _external_receipt_body(
    value: object,
    *,
    code: str,
) -> tuple[dict[str, str], bytes]:
    """Validate and detach one complete producer receipt body.

    External producers own the fields beyond the common self-authenticating
    envelope.  The index retains the whole canonical body but journals only the
    schema and core digest needed to name and reauthenticate it.
    """

    if not isinstance(value, dict) or not {"receipt_sha256", "schema"} <= set(value):
        raise DRGenerationIndexError(code)
    try:
        canonical = _canonical_json(value)
    except DRGenerationIndexError as exc:
        raise DRGenerationIndexError(code) from exc
    if not canonical or len(canonical) + 1 > MAX_RECEIPT_BYTES:
        raise DRGenerationIndexError(code)
    payload = _unique_json(canonical, code=code)
    if canonical != _canonical_json(payload):
        raise DRGenerationIndexError(code)
    schema = _schema(payload.get("schema"), code=code)
    supplied = _hex64(payload.get("receipt_sha256"), code=code)
    core = {key: item for key, item in payload.items() if key != "receipt_sha256"}
    if supplied != _sha256(_canonical_json(core)):
        raise DRGenerationIndexError(code)
    return {"schema": schema, "sha256": supplied}, canonical + b"\n"


DR_REHEARSAL_CHECKSET_SHA256 = _sha256(_canonical_json(DR_REHEARSAL_CHECKS))


def _normalize_bound_release(value: object, *, code: str) -> dict[str, Any]:
    expected = {
        "commit",
        "max_schema",
        "root",
        "tree_manifest_sha256",
        "version",
        "wheel_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise DRGenerationIndexError(code)
    commit = value.get("commit")
    max_schema = value.get("max_schema")
    version = value.get("version")
    if (
        not isinstance(commit, str)
        or _HEX40_RE.fullmatch(commit) is None
        or type(max_schema) is not int
        or int(max_schema) <= 0
        or not isinstance(version, str)
        or _VERSION_RE.fullmatch(version) is None
    ):
        raise DRGenerationIndexError(code)
    root = _absolute_lexical(Path(str(value.get("root") or "")), code=code)
    return {
        "commit": commit,
        "max_schema": int(max_schema),
        "root": str(root),
        "tree_manifest_sha256": _hex64(value.get("tree_manifest_sha256"), code=code),
        "version": version,
        "wheel_sha256": _hex64(value.get("wheel_sha256"), code=code),
    }


def _normalize_release_records(
    value: object,
    *,
    candidate: Mapping[str, Any],
    code: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"fallback", "previous"}:
        raise DRGenerationIndexError(code)
    records = {
        role: _normalize_bound_release(value.get(role), code=code) for role in ("fallback", "previous")
    }
    rollback_trees = sorted(
        {
            records["fallback"]["tree_manifest_sha256"],
            records["previous"]["tree_manifest_sha256"],
        }
    )
    if (
        records["fallback"] != candidate["restore_release"]
        or rollback_trees != candidate["allowed_rollback_tree_sha256s"]
        or candidate["database_schema"] > records["fallback"]["max_schema"]
        or candidate["database_schema"] > records["previous"]["max_schema"]
    ):
        raise DRGenerationIndexError(code)
    return records


def activation_receipt_evidence(
    authentication_receipt: Mapping[str, Any],
) -> tuple[dict[str, str], bytes, dict[str, Any]]:
    """Return the exact v2 activation body and its durable file identity."""

    code = "activation_receipt_invalid"
    if authentication_receipt.get("schema") != AUTHENTICATION_RECEIPT_SCHEMA_V2:
        raise DRGenerationIndexError(code)
    value = authentication_receipt.get("activation_receipt")
    expected = {
        "alias_repair",
        "backend_accepted",
        "backup_receipt_sha256",
        "bridge_accepted",
        "candidate_tree_sha256",
        "database_schema_before",
        "engineer_backup_receipt_sha256",
        "inbox_backup_receipt_sha256",
        "obsidian_backup_receipt_sha256",
        "operator_schema",
        "receipt_sha256",
        "runtime_policy",
        "schema",
        "status",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise DRGenerationIndexError(code)
    try:
        canonical = _canonical_json(value)
    except DRGenerationIndexError as exc:
        raise DRGenerationIndexError(code) from exc
    payload = _unique_json(canonical, code=code)
    if canonical != _canonical_json(payload):
        raise DRGenerationIndexError(code)
    receipt_sha256 = _hex64(payload.get("receipt_sha256"), code=code)
    semantic_core = {
        key: item for key, item in payload.items() if key not in {"operator_schema", "receipt_sha256"}
    }
    raw = canonical + b"\n"
    file_sha256 = _sha256(raw)
    if (
        payload.get("schema") != _ACTIVATION_RECEIPT_SCHEMA
        or payload.get("operator_schema") != _ACTIVATION_OPERATOR_SCHEMA
        or payload.get("status") != "clear"
        or payload.get("backend_accepted") is not True
        or payload.get("bridge_accepted") is not True
        or not isinstance(payload.get("alias_repair"), dict)
        or not isinstance(payload.get("runtime_policy"), dict)
        or receipt_sha256 != _sha256(_canonical_json(semantic_core))
        or authentication_receipt.get("activation_receipt_sha256") != receipt_sha256
        or authentication_receipt.get("activation_receipt_file_sha256") != file_sha256
    ):
        raise DRGenerationIndexError(code)
    return {"schema": _ACTIVATION_RECEIPT_SCHEMA, "sha256": file_sha256}, raw, payload


def validate_authentication_receipt(
    value: object,
    *,
    candidate: Mapping[str, Any],
) -> tuple[dict[str, str], bytes, dict[str, Any]]:
    """Authenticate the sole code-owned DR authentication receipt contract."""

    normalized_candidate = normalize_generation_candidate(candidate)
    expected_v1 = {
        "allowed_rollback_tree_sha256s",
        "activation_journal_file_sha256",
        "activation_journal_sha256",
        "activation_receipt_file_sha256",
        "activation_receipt_sha256",
        "backup_directory",
        "backup_manifest_sha256",
        "candidate_sha256",
        "database_schema",
        "receipt_sha256",
        "restore_operator_sha256",
        "schema",
        "source_transaction_id",
        "status",
        "surface_receipts",
    }
    if not isinstance(value, dict):
        raise DRGenerationIndexError("authentication_receipt_invalid")
    reference, raw = _external_receipt_body(value, code="authentication_receipt_invalid")
    payload = _unique_json(raw, code="authentication_receipt_invalid")
    schema = reference["schema"]
    expected = (
        expected_v1
        if schema == AUTHENTICATION_RECEIPT_SCHEMA
        else expected_v1 | {"activation_receipt", "release_records"}
    )
    if (
        schema not in {AUTHENTICATION_RECEIPT_SCHEMA, AUTHENTICATION_RECEIPT_SCHEMA_V2}
        or set(payload) != expected
    ):
        raise DRGenerationIndexError("authentication_receipt_invalid")
    directory = payload.get("backup_directory")
    surfaces = payload.get("surface_receipts")
    if (
        payload.get("status") != "authenticated"
        or payload.get("candidate_sha256") != _sha256(_canonical_json(normalized_candidate))
        or type(payload.get("database_schema")) is not int
        or payload.get("database_schema") != normalized_candidate["database_schema"]
        or payload.get("allowed_rollback_tree_sha256s")
        != normalized_candidate["allowed_rollback_tree_sha256s"]
        or payload.get("source_transaction_id") != normalized_candidate["source_transaction_id"]
        or payload.get("activation_receipt_sha256") != normalized_candidate["source_receipt_sha256"]
        or not isinstance(directory, dict)
        or set(directory) != {"device", "inode", "path"}
        or type(directory.get("device")) is not int
        or int(directory["device"]) < 0
        or type(directory.get("inode")) is not int
        or int(directory["inode"]) <= 0
        or directory.get("path") != normalized_candidate["backup_directory"]
        or not isinstance(surfaces, dict)
        or set(surfaces) != {"database", "engineer", "inbox", "obsidian"}
        or surfaces
        != {
            "database": normalized_candidate["database_receipt_sha256"],
            "engineer": normalized_candidate["engineer_receipt_sha256"],
            "inbox": normalized_candidate["inbox_receipt_sha256"],
            "obsidian": normalized_candidate["obsidian_receipt_sha256"],
        }
    ):
        raise DRGenerationIndexError("authentication_receipt_invalid")
    for key in (
        "activation_journal_file_sha256",
        "activation_journal_sha256",
        "activation_receipt_file_sha256",
        "activation_receipt_sha256",
        "backup_manifest_sha256",
        "candidate_sha256",
        "restore_operator_sha256",
    ):
        _hex64(payload.get(key), code="authentication_receipt_invalid")
    if schema == AUTHENTICATION_RECEIPT_SCHEMA_V2:
        try:
            _activation_reference, _activation_raw, activation = activation_receipt_evidence(payload)
            records = _normalize_release_records(
                payload.get("release_records"),
                candidate=normalized_candidate,
                code="authentication_receipt_invalid",
            )
        except DRGenerationIndexError as exc:
            raise DRGenerationIndexError("authentication_receipt_invalid") from exc
        if (
            activation.get("database_schema_before") != normalized_candidate["database_schema"]
            or activation.get("backup_receipt_sha256") != normalized_candidate["database_receipt_sha256"]
            or activation.get("engineer_backup_receipt_sha256")
            != normalized_candidate["engineer_receipt_sha256"]
            or activation.get("inbox_backup_receipt_sha256") != normalized_candidate["inbox_receipt_sha256"]
            or activation.get("obsidian_backup_receipt_sha256")
            != normalized_candidate["obsidian_receipt_sha256"]
            or records != payload["release_records"]
        ):
            raise DRGenerationIndexError("authentication_receipt_invalid")
    return reference, raw, payload


def validate_rehearsal_receipt(
    value: object,
    *,
    candidate: Mapping[str, Any],
    authentication_receipt: Mapping[str, Any],
    index_transaction_id: str,
    index_revision: int,
    index_journal_sha256: str,
) -> tuple[dict[str, str], bytes, dict[str, Any]]:
    """Authenticate and bind the code-owned rehearsal result to one CAS epoch."""

    if type(index_revision) is not int or index_revision < 0:
        raise DRGenerationIndexError("rehearsal_receipt_invalid")
    normalized_candidate = normalize_generation_candidate(candidate)
    auth_ref, _auth_raw, auth = validate_authentication_receipt(
        authentication_receipt,
        candidate=normalized_candidate,
    )
    expected_v1 = {
        "authentication_receipt_sha256",
        "candidate_sha256",
        "check_count",
        "checkset_sha256",
        "database_foreign_keys_clear",
        "database_integrity_clear",
        "database_reopen_count",
        "database_schema",
        "engineer_authority_present",
        "engineer_exact",
        "fault_boundary",
        "four_surface_exact",
        "four_surface_sha256",
        "index_journal_sha256",
        "index_revision",
        "index_transaction_id",
        "inbox_foreign_keys_clear",
        "inbox_integrity_clear",
        "inbox_reopen_count",
        "network_call_count",
        "obsidian_exact",
        "production_surface_write_count",
        "receipt_sha256",
        "restore_release",
        "rollback_restore_observed",
        "rollback_tree_sha256",
        "rolled_back",
        "schema",
        "scratch_removed",
        "source",
        "status",
        "systemctl_call_count",
    }
    if not isinstance(value, dict):
        raise DRGenerationIndexError("rehearsal_receipt_invalid")
    reference, raw = _external_receipt_body(value, code="rehearsal_receipt_invalid")
    payload = _unique_json(raw, code="rehearsal_receipt_invalid")
    receipt_schema = reference["schema"]
    expected = (
        expected_v1 if receipt_schema == REHEARSAL_RECEIPT_SCHEMA else expected_v1 | {"exercised_release"}
    )
    expected_pair = {
        (AUTHENTICATION_RECEIPT_SCHEMA, REHEARSAL_RECEIPT_SCHEMA),
        (AUTHENTICATION_RECEIPT_SCHEMA_V2, REHEARSAL_RECEIPT_SCHEMA_V2),
    }
    if set(payload) != expected or (auth_ref["schema"], receipt_schema) not in expected_pair:
        raise DRGenerationIndexError("rehearsal_receipt_invalid")
    restore = normalized_candidate["restore_release"]
    restore_projection = {
        key: restore[key]
        for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")
    }
    source_keys = (
        "activation_journal_file_sha256",
        "activation_journal_sha256",
        "activation_receipt_file_sha256",
        "activation_receipt_sha256",
        "backup_manifest_sha256",
        "restore_operator_sha256",
        "surface_receipts",
    )
    source_projection = {key: auth[key] for key in source_keys}
    four_surface_sha256 = _sha256(
        _canonical_json(
            {
                "database": normalized_candidate["database_receipt_sha256"],
                "engineer": normalized_candidate["engineer_receipt_sha256"],
                "inbox": normalized_candidate["inbox_receipt_sha256"],
                "obsidian": normalized_candidate["obsidian_receipt_sha256"],
            }
        )
    )
    boolean_keys = (
        "database_foreign_keys_clear",
        "database_integrity_clear",
        "engineer_exact",
        "four_surface_exact",
        "inbox_foreign_keys_clear",
        "inbox_integrity_clear",
        "obsidian_exact",
        "rollback_restore_observed",
        "rolled_back",
        "scratch_removed",
    )
    if (
        payload.get("status") != "rehearsed"
        or payload.get("candidate_sha256") != _sha256(_canonical_json(normalized_candidate))
        or payload.get("authentication_receipt_sha256") != auth_ref["sha256"]
        or payload.get("index_transaction_id")
        != _hex64(index_transaction_id, code="rehearsal_receipt_invalid")
        or type(payload.get("index_revision")) is not int
        or payload.get("index_revision") != index_revision
        or payload.get("index_journal_sha256")
        != _hex64(index_journal_sha256, code="rehearsal_receipt_invalid")
        or payload.get("check_count") != len(DR_REHEARSAL_CHECKS)
        or payload.get("checkset_sha256") != DR_REHEARSAL_CHECKSET_SHA256
        or payload.get("database_reopen_count") != 2
        or payload.get("inbox_reopen_count") != 2
        or type(payload.get("database_schema")) is not int
        or payload.get("database_schema") != normalized_candidate["database_schema"]
        or payload.get("fault_boundary") != "after_migration_before_provision_or_network"
        or payload.get("four_surface_sha256") != four_surface_sha256
        or payload.get("restore_release") != restore_projection
        or payload.get("source") != source_projection
        or payload.get("systemctl_call_count") != 0
        or payload.get("network_call_count") != 0
        or payload.get("production_surface_write_count") != 0
        or type(payload.get("engineer_authority_present")) is not bool
        or any(payload.get(key) is not True for key in boolean_keys)
    ):
        raise DRGenerationIndexError("rehearsal_receipt_invalid")
    if payload.get("rollback_tree_sha256") not in normalized_candidate["allowed_rollback_tree_sha256s"]:
        raise DRGenerationIndexError("rehearsal_receipt_invalid")
    if receipt_schema == REHEARSAL_RECEIPT_SCHEMA_V2:
        try:
            authentication_records = _normalize_release_records(
                auth.get("release_records"),
                candidate=normalized_candidate,
                code="rehearsal_receipt_invalid",
            )
            exercised = _normalize_bound_release(
                payload.get("exercised_release"), code="rehearsal_receipt_invalid"
            )
        except DRGenerationIndexError as exc:
            raise DRGenerationIndexError("rehearsal_receipt_invalid") from exc
        if (
            exercised not in authentication_records.values()
            or payload.get("rollback_tree_sha256") != exercised["tree_manifest_sha256"]
            or normalized_candidate["database_schema"] > exercised["max_schema"]
        ):
            raise DRGenerationIndexError("rehearsal_receipt_invalid")
    return reference, raw, payload


def _external_receipt_name(kind: str, sha256: str) -> str:
    if kind not in {
        ACTIVATION_RECEIPT_KIND,
        AUTHENTICATION_RECEIPT_KIND,
        REHEARSAL_RECEIPT_KIND,
    }:
        raise DRGenerationIndexError("external_receipt_kind_invalid")
    digest = _hex64(sha256, code="external_receipt_ref_invalid")
    return f"{kind}-{digest}.json"


def _normalize_restore_release(value: object) -> dict[str, Any]:
    expected = {
        "commit",
        "max_schema",
        "root",
        "tree_manifest_sha256",
        "version",
        "wheel_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise DRGenerationIndexError("generation_restore_release_invalid")
    commit = value.get("commit")
    max_schema = value.get("max_schema")
    version = value.get("version")
    if (
        not isinstance(commit, str)
        or _HEX40_RE.fullmatch(commit) is None
        or type(max_schema) is not int
        or int(max_schema) <= 0
        or not isinstance(version, str)
        or _VERSION_RE.fullmatch(version) is None
    ):
        raise DRGenerationIndexError("generation_restore_release_invalid")
    root = _absolute_lexical(
        Path(str(value.get("root") or "")),
        code="generation_restore_release_invalid",
    )
    return {
        "commit": commit,
        "max_schema": int(max_schema),
        "root": str(root),
        "tree_manifest_sha256": _hex64(
            value.get("tree_manifest_sha256"),
            code="generation_restore_release_invalid",
        ),
        "version": version,
        "wheel_sha256": _hex64(
            value.get("wheel_sha256"),
            code="generation_restore_release_invalid",
        ),
    }


def normalize_generation_candidate(value: object) -> dict[str, Any]:
    """Validate and detach one exact caller-supplied backup identity."""

    expected = {
        "allowed_rollback_tree_sha256s",
        "backup_directory",
        "backup_record_sha256",
        "database_schema",
        "database_receipt_sha256",
        "engineer_receipt_sha256",
        "inbox_receipt_sha256",
        "obsidian_receipt_sha256",
        "restore_release",
        "schema",
        "source_kind",
        "source_receipt_sha256",
        "source_transaction_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise DRGenerationIndexError("generation_candidate_invalid")
    source_kind = value.get("source_kind")
    allowed_rollbacks = value.get("allowed_rollback_tree_sha256s")
    database_schema = value.get("database_schema")
    if source_kind not in _SOURCE_KINDS:
        raise DRGenerationIndexError("generation_candidate_invalid")
    if (
        not isinstance(allowed_rollbacks, list)
        or not 1 <= len(allowed_rollbacks) <= 2
        or allowed_rollbacks != sorted(set(allowed_rollbacks))
        or any(not isinstance(item, str) or _HEX64_RE.fullmatch(item) is None for item in allowed_rollbacks)
        or type(database_schema) is not int
        or database_schema <= 0
    ):
        raise DRGenerationIndexError("generation_candidate_invalid")
    backup_directory = _absolute_lexical(
        Path(str(value.get("backup_directory") or "")),
        code="generation_candidate_invalid",
    )
    normalized: dict[str, Any] = {
        "allowed_rollback_tree_sha256s": list(allowed_rollbacks),
        "backup_directory": str(backup_directory),
        "backup_record_sha256": _hex64(
            value.get("backup_record_sha256"), code="generation_candidate_invalid"
        ),
        "database_receipt_sha256": _hex64(
            value.get("database_receipt_sha256"), code="generation_candidate_invalid"
        ),
        "database_schema": database_schema,
        "engineer_receipt_sha256": _hex64(
            value.get("engineer_receipt_sha256"), code="generation_candidate_invalid"
        ),
        "inbox_receipt_sha256": _hex64(
            value.get("inbox_receipt_sha256"), code="generation_candidate_invalid"
        ),
        "obsidian_receipt_sha256": _hex64(
            value.get("obsidian_receipt_sha256"), code="generation_candidate_invalid"
        ),
        "restore_release": _normalize_restore_release(value.get("restore_release")),
        "schema": str(value.get("schema") or ""),
        "source_kind": str(source_kind),
        "source_receipt_sha256": _hex64(
            value.get("source_receipt_sha256"), code="generation_candidate_invalid"
        ),
        "source_transaction_id": _hex64(
            value.get("source_transaction_id"), code="generation_candidate_invalid"
        ),
    }
    if normalized["schema"] != GENERATION_CANDIDATE_SCHEMA:
        raise DRGenerationIndexError("generation_candidate_invalid")
    if (
        normalized["restore_release"]["tree_manifest_sha256"]
        not in normalized["allowed_rollback_tree_sha256s"]
        or normalized["database_schema"] > normalized["restore_release"]["max_schema"]
    ):
        raise DRGenerationIndexError("generation_candidate_invalid")
    if len(_canonical_json(normalized)) > MAX_CANDIDATE_BYTES:
        raise DRGenerationIndexError("generation_candidate_invalid")
    return normalized


def _normalize_rehearsal_binding(
    value: object,
    *,
    candidate: Mapping[str, Any],
    authentication_receipt: Mapping[str, str],
) -> dict[str, Any]:
    expected = {
        "allowed_rollback_tree_sha256s",
        "authentication_receipt_sha256",
        "candidate_sha256",
        "database_schema",
        "index_journal_sha256",
        "index_revision",
        "index_transaction_id",
        "schema",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise DRGenerationIndexError("rehearsal_binding_invalid")
    revision = value.get("index_revision")
    normalized = {
        "allowed_rollback_tree_sha256s": value.get("allowed_rollback_tree_sha256s"),
        "authentication_receipt_sha256": _hex64(
            value.get("authentication_receipt_sha256"),
            code="rehearsal_binding_invalid",
        ),
        "candidate_sha256": _hex64(value.get("candidate_sha256"), code="rehearsal_binding_invalid"),
        "database_schema": value.get("database_schema"),
        "index_journal_sha256": _hex64(value.get("index_journal_sha256"), code="rehearsal_binding_invalid"),
        "index_revision": revision,
        "index_transaction_id": _hex64(value.get("index_transaction_id"), code="rehearsal_binding_invalid"),
        "schema": value.get("schema"),
    }
    if (
        normalized["schema"] != REHEARSAL_BINDING_SCHEMA
        or type(revision) is not int
        or revision <= 0
        or normalized["index_transaction_id"] == ZERO_SHA256
        or normalized["index_journal_sha256"] == ZERO_SHA256
        or type(normalized["database_schema"]) is not int
        or not isinstance(normalized["allowed_rollback_tree_sha256s"], list)
        or normalized["candidate_sha256"] != _sha256(_canonical_json(candidate))
        or normalized["authentication_receipt_sha256"] != authentication_receipt["sha256"]
        or normalized["database_schema"] != candidate["database_schema"]
        or normalized["allowed_rollback_tree_sha256s"] != candidate["allowed_rollback_tree_sha256s"]
    ):
        raise DRGenerationIndexError("rehearsal_binding_invalid")
    return normalized


def _rehearsal_binding(
    *,
    candidate: Mapping[str, Any],
    authentication_receipt: Mapping[str, str],
    index_transaction_id: str,
    index_revision: int,
    index_journal_sha256: str,
) -> dict[str, Any]:
    normalized_candidate = normalize_generation_candidate(candidate)
    normalized_authentication = _normalize_external_receipt(
        authentication_receipt,
        code="authentication_receipt_invalid",
    )
    return _normalize_rehearsal_binding(
        {
            "allowed_rollback_tree_sha256s": normalized_candidate["allowed_rollback_tree_sha256s"],
            "authentication_receipt_sha256": normalized_authentication["sha256"],
            "candidate_sha256": _sha256(_canonical_json(normalized_candidate)),
            "database_schema": normalized_candidate["database_schema"],
            "index_journal_sha256": _hex64(index_journal_sha256, code="rehearsal_binding_invalid"),
            "index_revision": index_revision,
            "index_transaction_id": _hex64(index_transaction_id, code="rehearsal_binding_invalid"),
            "schema": REHEARSAL_BINDING_SCHEMA,
        },
        candidate=normalized_candidate,
        authentication_receipt=normalized_authentication,
    )


def _normalize_generation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "authentication_receipt",
        "candidate",
        "rehearsal_binding",
        "rehearsal_receipt",
        "schema",
    }:
        raise DRGenerationIndexError("generation_receipt_invalid")
    authentication = _normalize_external_receipt(
        value.get("authentication_receipt"), code="generation_receipt_invalid"
    )
    candidate = normalize_generation_candidate(value.get("candidate"))
    generation = {
        "authentication_receipt": authentication,
        "candidate": candidate,
        "rehearsal_binding": _normalize_rehearsal_binding(
            value.get("rehearsal_binding"),
            candidate=candidate,
            authentication_receipt=authentication,
        ),
        "rehearsal_receipt": _normalize_external_receipt(
            value.get("rehearsal_receipt"), code="generation_receipt_invalid"
        ),
        "schema": str(value.get("schema") or ""),
    }
    if generation["schema"] != GENERATION_SCHEMA:
        raise DRGenerationIndexError("generation_receipt_invalid")
    return generation


def _normalize_generation_ref(value: object, *, code: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"generation_id", "receipt_sha256"}:
        raise DRGenerationIndexError(code)
    return {
        "generation_id": _hex64(value.get("generation_id"), code=code),
        "receipt_sha256": _hex64(value.get("receipt_sha256"), code=code),
    }


def _generation_receipt(generation: Mapping[str, Any]) -> tuple[dict[str, str], bytes]:
    normalized = _normalize_generation(generation)
    generation_id = _sha256(_canonical_json(normalized))
    core: dict[str, Any] = {
        "generation": normalized,
        "generation_id": generation_id,
        "schema": GENERATION_RECEIPT_SCHEMA,
    }
    receipt_sha256 = _sha256(_canonical_json(core))
    payload = {**core, "receipt_sha256": receipt_sha256}
    return {
        "generation_id": generation_id,
        "receipt_sha256": receipt_sha256,
    }, _canonical_json(payload) + b"\n"


def _encode_state(core: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    digest = _sha256(_canonical_json(core))
    payload = {**dict(core), "journal_sha256": digest}
    return payload, _canonical_json(payload) + b"\n"


def _head_fence(value: bytes) -> bytes:
    state = _unique_json(value, code="dr_generation_head_fence_invalid")
    revision = state.get("revision")
    journal_sha256 = _hex64(
        state.get("journal_sha256"),
        code="dr_generation_head_fence_invalid",
    )
    if type(revision) is not int or revision < 0:
        raise DRGenerationIndexError("dr_generation_head_fence_invalid")
    core = {
        "index_raw_base64": base64.b64encode(value).decode("ascii"),
        "index_sha256": _sha256(value),
        "journal_sha256": journal_sha256,
        "revision": revision,
        "schema": HEAD_FENCE_SCHEMA,
    }
    payload = {**core, "receipt_sha256": _sha256(_canonical_json(core))}
    return _canonical_json(payload) + b"\n"


class DurableDRGenerationIndex:
    """A crash-safe fixed-slot current/older DR generation index."""

    def __init__(self, state_directory: Path) -> None:
        self.state_directory, status = _private_directory(
            state_directory,
            code="dr_generation_state_directory_invalid",
        )
        self._state_directory_identity = (int(status.st_dev), int(status.st_ino))
        self.parent_directory = _absolute_lexical(
            self.state_directory.parent,
            code="dr_generation_state_parent_invalid",
        )
        try:
            parent_status = os.stat(self.parent_directory, follow_symlinks=False)
            parent_resolved = self.parent_directory.resolve(strict=True)
        except OSError as exc:
            raise DRGenerationIndexError("dr_generation_state_parent_invalid") from exc
        if (
            parent_resolved != self.parent_directory
            or not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) & 0o022
        ):
            raise DRGenerationIndexError("dr_generation_state_parent_invalid")
        self._parent_directory_identity = (
            int(parent_status.st_dev),
            int(parent_status.st_ino),
        )
        scope = _sha256(os.fsencode(str(self.state_directory)))[:32]
        self.head_directory = self.parent_directory / f"{HEAD_FENCE_DIRECTORY_PREFIX}-{scope}"
        self.path = self.state_directory / INDEX_NAME
        self.receipt_directory = self.state_directory / RECEIPT_DIRECTORY_NAME

    @staticmethod
    def _directory_is_private(status: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(status.st_mode)
            and status.st_uid == os.geteuid()
            and not stat.S_IMODE(status.st_mode) & 0o077
        )

    @staticmethod
    def _directory_is_owned(status: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(status.st_mode)
            and status.st_uid == os.geteuid()
            and not stat.S_IMODE(status.st_mode) & 0o022
        )

    def _open_state_directory(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.state_directory, flags)
        except OSError as exc:
            raise DRGenerationIndexError("dr_generation_state_directory_invalid") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not self._directory_is_owned(opened)
                or (int(opened.st_dev), int(opened.st_ino)) != self._state_directory_identity
            ):
                raise DRGenerationIndexError("dr_generation_state_directory_changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_parent_directory(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(self.parent_directory, flags)
            opened = os.fstat(descriptor)
            if (
                not self._directory_is_private(opened)
                or (int(opened.st_dev), int(opened.st_ino)) != self._parent_directory_identity
            ):
                raise DRGenerationIndexError("dr_generation_state_parent_changed")
            return descriptor
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    def _require_pinned_namespace(self, pins: _PinnedDirectories) -> None:
        try:
            parent_open = os.fstat(pins.parent_fd)
            state_open = os.fstat(pins.state_fd)
            receipt_open = os.fstat(pins.receipt_fd)
            head_open = os.fstat(pins.head_fd)
            parent_named = os.stat(self.parent_directory, follow_symlinks=False)
            state_named = os.stat(self.state_directory, follow_symlinks=False)
            head_named = os.stat(
                self.head_directory.name,
                dir_fd=pins.parent_fd,
                follow_symlinks=False,
            )
            receipt_named = os.stat(
                RECEIPT_DIRECTORY_NAME,
                dir_fd=pins.state_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise DRGenerationIndexError("dr_generation_directory_changed") from exc
        if (
            not self._directory_is_owned(parent_open)
            or not self._directory_is_private(state_open)
            or not self._directory_is_private(receipt_open)
            or not self._directory_is_private(head_open)
            or (int(parent_open.st_dev), int(parent_open.st_ino)) != pins.parent_identity
            or (int(parent_named.st_dev), int(parent_named.st_ino)) != pins.parent_identity
            or (int(state_open.st_dev), int(state_open.st_ino)) != self._state_directory_identity
            or (int(state_named.st_dev), int(state_named.st_ino)) != self._state_directory_identity
            or (int(receipt_open.st_dev), int(receipt_open.st_ino)) != pins.receipt_identity
            or (int(receipt_named.st_dev), int(receipt_named.st_ino)) != pins.receipt_identity
            or (int(head_open.st_dev), int(head_open.st_ino)) != pins.head_identity
            or (int(head_named.st_dev), int(head_named.st_ino)) != pins.head_identity
        ):
            raise DRGenerationIndexError("dr_generation_directory_changed")

    @contextmanager
    def _guard(self, *, create_receipt_directory: bool = False) -> Iterator[_PinnedDirectories]:
        parent_fd = self._open_parent_directory()
        state_fd = -1
        receipt_fd = -1
        head_fd = -1
        try:
            if create_receipt_directory:
                try:
                    os.mkdir(self.head_directory.name, mode=0o700, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise DRGenerationIndexError("dr_generation_head_directory_invalid") from exc
            try:
                head_named = os.stat(
                    self.head_directory.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise DRGenerationIndexError("dr_generation_head_directory_invalid") from exc
            if not self._directory_is_private(head_named):
                raise DRGenerationIndexError("dr_generation_head_directory_invalid")
            directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            head_fd = os.open(self.head_directory.name, directory_flags, dir_fd=parent_fd)
            head_open = os.fstat(head_fd)
            head_identity = (int(head_open.st_dev), int(head_open.st_ino))
            if not self._directory_is_private(head_open) or head_identity != (
                int(head_named.st_dev),
                int(head_named.st_ino),
            ):
                raise DRGenerationIndexError("dr_generation_head_directory_changed")
            fcntl.flock(head_fd, fcntl.LOCK_EX)
            state_fd = self._open_state_directory()
            # The receipt directory is replaceable during adversarial recovery.
            # Lock the state-directory inode that owns both the CAS file and the
            # receipt entry, so a replacement receipt directory cannot create a
            # second independent writer domain.
            fcntl.flock(state_fd, fcntl.LOCK_EX)
            if create_receipt_directory:
                try:
                    os.mkdir(RECEIPT_DIRECTORY_NAME, mode=0o700, dir_fd=state_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise DRGenerationIndexError("dr_generation_receipt_directory_invalid") from exc
            try:
                receipt_named = os.stat(
                    RECEIPT_DIRECTORY_NAME,
                    dir_fd=state_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise DRGenerationIndexError("dr_generation_receipt_directory_invalid") from exc
            if not self._directory_is_private(receipt_named):
                raise DRGenerationIndexError("dr_generation_receipt_directory_invalid")
            receipt_fd = os.open(RECEIPT_DIRECTORY_NAME, directory_flags, dir_fd=state_fd)
            receipt_open = os.fstat(receipt_fd)
            receipt_identity = (int(receipt_open.st_dev), int(receipt_open.st_ino))
            if not self._directory_is_private(receipt_open) or receipt_identity != (
                int(receipt_named.st_dev),
                int(receipt_named.st_ino),
            ):
                raise DRGenerationIndexError("dr_generation_receipt_directory_changed")
            pins = _PinnedDirectories(
                parent_fd=parent_fd,
                state_fd=state_fd,
                receipt_fd=receipt_fd,
                head_fd=head_fd,
                parent_identity=self._parent_directory_identity,
                receipt_identity=receipt_identity,
                head_identity=head_identity,
            )
            self._require_pinned_namespace(pins)
            yield pins
            self._require_pinned_namespace(pins)
        finally:
            if receipt_fd >= 0:
                os.close(receipt_fd)
            if state_fd >= 0:
                with suppress(OSError):
                    fcntl.flock(state_fd, fcntl.LOCK_UN)
                os.close(state_fd)
            if head_fd >= 0:
                with suppress(OSError):
                    fcntl.flock(head_fd, fcntl.LOCK_UN)
                os.close(head_fd)
            os.close(parent_fd)

    @staticmethod
    def _publish_no_replace(
        *,
        directory_fd: int,
        name: str,
        raw: bytes,
        final_mode: int,
        maximum_bytes: int,
        code: str,
    ) -> None:
        name = _entry_name(name, code=code)
        temporary = f".{name}.{_sha256(raw)}.new"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            try:
                descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                existing_temporary, temporary_mode = _stable_staging_file_at(
                    directory_fd,
                    temporary,
                    maximum_bytes=maximum_bytes,
                    code=code,
                )
                if existing_temporary == raw and temporary_mode == final_mode:
                    pass
                elif temporary_mode == 0o600 and raw.startswith(existing_temporary):
                    os.unlink(temporary, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
                else:
                    raise DRGenerationIndexError(code) from None
            if descriptor >= 0:
                view = memoryview(raw)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("short write")
                    written += count
                # First make every content byte durable while the staging inode
                # is still owner-writable.  A crash here leaves recoverable 0600
                # staging, never a torn object that looks final/0400.
                os.fsync(descriptor)
                os.fchmod(descriptor, final_mode)
                # Then persist final metadata before atomic no-replace rename.
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
            try:
                _rename_noreplace(directory_fd, temporary, name)
            except FileExistsError:
                existing = _stable_private_file_at(
                    directory_fd,
                    name,
                    mode=final_mode,
                    maximum_bytes=maximum_bytes,
                    code=code,
                )
                if existing != raw:
                    raise DRGenerationIndexError(code) from None
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
            published = _stable_private_file_at(
                directory_fd,
                name,
                mode=final_mode,
                maximum_bytes=maximum_bytes,
                code=code,
            )
            if published != raw:
                raise DRGenerationIndexError(code)
        except DRGenerationIndexError:
            raise
        except OSError as exc:
            raise DRGenerationIndexError(code) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _head_record_unlocked(
        self,
        pins: _PinnedDirectories,
    ) -> tuple[int, str, bytes]:
        try:
            names = set(os.listdir(pins.head_fd))
        except OSError as exc:
            raise DRGenerationIndexError("dr_generation_head_fence_invalid") from exc
        if not names <= {HEAD_FENCE_NAME, HEAD_FENCE_STAGING_NAME}:
            raise DRGenerationIndexError("dr_generation_head_fence_invalid")
        if HEAD_FENCE_STAGING_NAME in names:
            try:
                staged = os.stat(
                    HEAD_FENCE_STAGING_NAME,
                    dir_fd=pins.head_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise DRGenerationIndexError("dr_generation_head_fence_invalid") from exc
            if (
                not stat.S_ISREG(staged.st_mode)
                or staged.st_uid != os.geteuid()
                or staged.st_nlink != 1
                or stat.S_IMODE(staged.st_mode) != 0o600
                or not 0 <= staged.st_size <= MAX_HEAD_FENCE_BYTES
            ):
                raise DRGenerationIndexError("dr_generation_head_fence_invalid")
        if HEAD_FENCE_NAME not in names:
            raise DRGenerationIndexError("dr_generation_head_fence_missing")
        raw = _stable_private_file_at(
            pins.head_fd,
            HEAD_FENCE_NAME,
            mode=0o600,
            maximum_bytes=MAX_HEAD_FENCE_BYTES,
            code="dr_generation_head_fence_invalid",
        )
        payload = _unique_json(raw, code="dr_generation_head_fence_invalid")
        if raw != _canonical_json(payload) + b"\n" or set(payload) != {
            "index_raw_base64",
            "index_sha256",
            "journal_sha256",
            "receipt_sha256",
            "revision",
            "schema",
        }:
            raise DRGenerationIndexError("dr_generation_head_fence_invalid")
        core = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        revision = payload.get("revision")
        journal = _hex64(payload.get("journal_sha256"), code="dr_generation_head_fence_invalid")
        if (
            payload.get("schema") != HEAD_FENCE_SCHEMA
            or type(revision) is not int
            or revision < 0
            or payload.get("receipt_sha256") != _sha256(_canonical_json(core))
        ):
            raise DRGenerationIndexError("dr_generation_head_fence_invalid")
        encoded = payload.get("index_raw_base64")
        if not isinstance(encoded, str):
            raise DRGenerationIndexError("dr_generation_head_fence_invalid")
        try:
            index_raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeError, ValueError) as exc:
            raise DRGenerationIndexError("dr_generation_head_fence_invalid") from exc
        state = _unique_json(index_raw, code="dr_generation_head_fence_invalid")
        state_core = {key: value for key, value in state.items() if key != "journal_sha256"}
        if (
            not index_raw
            or len(index_raw) > MAX_INDEX_BYTES
            or index_raw != _canonical_json(state) + b"\n"
            or state.get("schema") != INDEX_SCHEMA
            or state.get("revision") != revision
            or state.get("journal_sha256") != journal
            or journal != _sha256(_canonical_json(state_core))
            or payload.get("index_sha256") != _sha256(index_raw)
        ):
            raise DRGenerationIndexError("dr_generation_head_fence_invalid")
        return revision, journal, index_raw

    def _require_head_matches(self, pins: _PinnedDirectories, raw: bytes) -> None:
        _revision, _journal, authoritative_raw = self._head_record_unlocked(pins)
        if raw != authoritative_raw:
            raise DRGenerationIndexError("dr_generation_head_rollback_detected")

    def _replace_head_unlocked(
        self,
        pins: _PinnedDirectories,
        raw: bytes,
    ) -> None:
        _replace_private_head_durable_at(
            pins.head_fd,
            _head_fence(raw),
            code="dr_generation_head_publication_failed",
        )
        if self._head_record_unlocked(pins)[2] != raw:
            raise DRGenerationIndexError("dr_generation_head_publication_failed")

    def initialize(
        self,
        *,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Create the empty authority exactly once, or authenticate the existing one."""

        guard = namespace_guard or (lambda: None)
        guard()
        with self._guard(create_receipt_directory=True) as pins:
            guard()
            if _entry_exists_at(pins.state_fd, INDEX_NAME):
                current_raw = _stable_private_file_at(
                    pins.state_fd,
                    INDEX_NAME,
                    mode=0o600,
                    maximum_bytes=MAX_INDEX_BYTES,
                    code="dr_generation_index_invalid",
                )
                current = self._decode_state(current_raw, pins.receipt_fd)
                head_revision, _head_journal, authoritative_raw = self._head_record_unlocked(pins)
                authoritative = self._decode_state(authoritative_raw, pins.receipt_fd)
                if current_raw != authoritative_raw:
                    current_revision = int(current["revision"])
                    if current_revision >= head_revision:
                        code = (
                            "dr_generation_head_backup_skew"
                            if current_revision > head_revision
                            else "dr_generation_head_rollback_detected"
                        )
                        raise DRGenerationIndexError(code)
                    guard()
                    _replace_private_durable_at(
                        pins.state_fd,
                        INDEX_NAME,
                        authoritative_raw,
                        code="dr_generation_head_projection_recovery_failed",
                    )
                    guard()
                loaded = self._load_unlocked(pins)
                if loaded != authoritative:
                    raise DRGenerationIndexError("dr_generation_head_projection_recovery_failed")
                guard()
                return loaded
            core = {
                "base_clear_sha256": ZERO_SHA256,
                "current": None,
                "older": None,
                "pending": None,
                "phase": "clear",
                "revision": 0,
                "schema": INDEX_SCHEMA,
                "transaction_id": ZERO_SHA256,
            }
            payload, raw = _encode_state(core)
            try:
                head_record = self._head_record_unlocked(pins)
            except DRGenerationIndexError as exc:
                if str(exc) != "dr_generation_head_fence_missing":
                    raise
                head_record = None
            if head_record is not None:
                if head_record[0] != 0 or head_record[2] != raw:
                    raise DRGenerationIndexError("dr_generation_head_rollback_detected")
            else:
                guard()
                self._replace_head_unlocked(pins, raw)
                guard()
            guard()
            self._publish_no_replace(
                directory_fd=pins.state_fd,
                name=INDEX_NAME,
                raw=raw,
                final_mode=0o600,
                maximum_bytes=MAX_INDEX_BYTES,
                code="dr_generation_index_initialization_failed",
            )
            guard()
            loaded = self._load_unlocked(pins)
            if loaded != payload:
                raise DRGenerationIndexError("dr_generation_index_initialization_failed")
            guard()
            return loaded

    def _receipt_path(self, generation_id: str) -> Path:
        return self.receipt_directory / f"{_hex64(generation_id, code='generation_ref_invalid')}.json"

    def _external_receipt_path(self, kind: str, sha256: str) -> Path:
        return self.receipt_directory / _external_receipt_name(kind, sha256)

    def _load_activation_receipt(
        self,
        authentication_receipt: Mapping[str, Any],
        receipt_fd: int,
    ) -> dict[str, Any]:
        reference, expected_raw, expected = activation_receipt_evidence(authentication_receipt)
        raw = _stable_private_file_at(
            receipt_fd,
            _external_receipt_name(ACTIVATION_RECEIPT_KIND, reference["sha256"]),
            mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code="activation_receipt_invalid",
        )
        payload = _unique_json(raw, code="activation_receipt_invalid")
        if raw != expected_raw or payload != expected:
            raise DRGenerationIndexError("activation_receipt_invalid")
        return payload

    def _load_external_receipt(
        self,
        reference: Mapping[str, str],
        receipt_fd: int,
        *,
        kind: str,
        code: str,
    ) -> dict[str, Any]:
        normalized_ref = _normalize_external_receipt(reference, code=code)
        raw = _stable_private_file_at(
            receipt_fd,
            _external_receipt_name(kind, normalized_ref["sha256"]),
            mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code=code,
        )
        payload = _unique_json(raw, code=code)
        body_ref, expected_raw = _external_receipt_body(payload, code=code)
        if raw != expected_raw or body_ref != normalized_ref:
            raise DRGenerationIndexError(code)
        if kind == AUTHENTICATION_RECEIPT_KIND and body_ref["schema"] == AUTHENTICATION_RECEIPT_SCHEMA_V2:
            self._load_activation_receipt(payload, receipt_fd)
        return payload

    def _publish_activation_receipt(
        self,
        *,
        authentication_receipt: Mapping[str, Any],
        pins: _PinnedDirectories,
        namespace_guard: Callable[[], None] | None = None,
    ) -> None:
        guard = namespace_guard or (lambda: None)
        reference, raw, _payload = activation_receipt_evidence(authentication_receipt)
        guard()
        self._publish_no_replace(
            directory_fd=pins.receipt_fd,
            name=_external_receipt_name(ACTIVATION_RECEIPT_KIND, reference["sha256"]),
            raw=raw,
            final_mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code="activation_receipt_publication_failed",
        )
        guard()
        self._load_activation_receipt(authentication_receipt, pins.receipt_fd)
        self._require_pinned_namespace(pins)
        guard()

    def _publish_external_receipt(
        self,
        *,
        reference: Mapping[str, str],
        raw: bytes,
        pins: _PinnedDirectories,
        kind: str,
        code: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> None:
        guard = namespace_guard or (lambda: None)
        normalized_ref = _normalize_external_receipt(reference, code=code)
        guard()
        self._publish_no_replace(
            directory_fd=pins.receipt_fd,
            name=_external_receipt_name(kind, normalized_ref["sha256"]),
            raw=raw,
            final_mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code=f"{kind}_receipt_publication_failed",
        )
        guard()
        self._load_external_receipt(
            normalized_ref,
            pins.receipt_fd,
            kind=kind,
            code=code,
        )
        self._require_pinned_namespace(pins)
        guard()

    def _load_receipt(self, reference: Mapping[str, str], receipt_fd: int) -> dict[str, Any]:
        normalized_ref = _normalize_generation_ref(reference, code="generation_ref_invalid")
        raw = _stable_private_file_at(
            receipt_fd,
            f"{normalized_ref['generation_id']}.json",
            mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code="generation_receipt_invalid",
        )
        payload = _unique_json(raw, code="generation_receipt_invalid")
        if raw != _canonical_json(payload) + b"\n" or set(payload) != {
            "generation",
            "generation_id",
            "receipt_sha256",
            "schema",
        }:
            raise DRGenerationIndexError("generation_receipt_invalid")
        supplied = str(payload.get("receipt_sha256") or "")
        core = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        if (
            payload.get("schema") != GENERATION_RECEIPT_SCHEMA
            or supplied != _sha256(_canonical_json(core))
            or supplied != normalized_ref["receipt_sha256"]
        ):
            raise DRGenerationIndexError("generation_receipt_invalid")
        generation = _normalize_generation(payload.get("generation"))
        generation_id = _sha256(_canonical_json(generation))
        if generation_id != payload.get("generation_id") or generation_id != normalized_ref["generation_id"]:
            raise DRGenerationIndexError("generation_receipt_invalid")
        authentication_body = self._load_external_receipt(
            generation["authentication_receipt"],
            receipt_fd,
            kind=AUTHENTICATION_RECEIPT_KIND,
            code="authentication_receipt_invalid",
        )
        validate_authentication_receipt(
            authentication_body,
            candidate=generation["candidate"],
        )
        rehearsal_body = self._load_external_receipt(
            generation["rehearsal_receipt"],
            receipt_fd,
            kind=REHEARSAL_RECEIPT_KIND,
            code="rehearsal_receipt_invalid",
        )
        binding = generation["rehearsal_binding"]
        validate_rehearsal_receipt(
            rehearsal_body,
            candidate=generation["candidate"],
            authentication_receipt=authentication_body,
            index_transaction_id=binding["index_transaction_id"],
            index_revision=binding["index_revision"],
            index_journal_sha256=binding["index_journal_sha256"],
        )
        return payload

    def _reference_backup_directory(
        self,
        reference: Mapping[str, str] | None,
        receipt_fd: int,
    ) -> str:
        if reference is None:
            return ""
        receipt = self._load_receipt(reference, receipt_fd)
        return str(receipt["generation"]["candidate"]["backup_directory"])

    def _decode_state(self, raw: bytes, receipt_fd: int) -> dict[str, Any]:
        payload = _unique_json(raw, code="dr_generation_index_invalid")
        if raw != _canonical_json(payload) + b"\n" or set(payload) != {
            "base_clear_sha256",
            "current",
            "journal_sha256",
            "older",
            "pending",
            "phase",
            "revision",
            "schema",
            "transaction_id",
        }:
            raise DRGenerationIndexError("dr_generation_index_invalid")
        supplied = _hex64(payload.get("journal_sha256"), code="dr_generation_index_invalid")
        core = {key: value for key, value in payload.items() if key != "journal_sha256"}
        if supplied != _sha256(_canonical_json(core)):
            raise DRGenerationIndexError("dr_generation_index_digest_mismatch")
        phase = payload.get("phase")
        revision = payload.get("revision")
        if (
            payload.get("schema") != INDEX_SCHEMA
            or phase not in INDEX_PHASES
            or type(revision) is not int
            or int(revision) < 0
        ):
            raise DRGenerationIndexError("dr_generation_index_invalid")
        base_clear = _hex64(payload.get("base_clear_sha256"), code="dr_generation_index_invalid")
        transaction_id = _hex64(payload.get("transaction_id"), code="dr_generation_index_invalid")
        if int(revision) == 0:
            if base_clear != ZERO_SHA256 or transaction_id != ZERO_SHA256:
                raise DRGenerationIndexError("dr_generation_index_invalid")
        elif base_clear == ZERO_SHA256 or transaction_id == ZERO_SHA256:
            raise DRGenerationIndexError("dr_generation_index_invalid")
        current = payload.get("current")
        older = payload.get("older")
        current_ref = (
            None if current is None else _normalize_generation_ref(current, code="generation_ref_invalid")
        )
        older_ref = None if older is None else _normalize_generation_ref(older, code="generation_ref_invalid")
        if older_ref is not None and current_ref is None:
            raise DRGenerationIndexError("dr_generation_index_invalid")
        if current_ref is not None and current_ref == older_ref:
            raise DRGenerationIndexError("dr_generation_index_invalid")
        current_receipt = self._load_receipt(current_ref, receipt_fd) if current_ref is not None else None
        older_receipt = self._load_receipt(older_ref, receipt_fd) if older_ref is not None else None
        current_backup = (
            str(current_receipt["generation"]["candidate"]["backup_directory"])
            if current_receipt is not None
            else ""
        )
        older_backup = (
            str(older_receipt["generation"]["candidate"]["backup_directory"])
            if older_receipt is not None
            else ""
        )
        if current_backup and current_backup == older_backup:
            raise DRGenerationIndexError("dr_generation_duplicate_slot")

        pending = payload.get("pending")
        if phase == "clear":
            if pending is not None:
                raise DRGenerationIndexError("dr_generation_index_invalid")
        else:
            if not isinstance(pending, dict) or set(pending) != {
                "authentication_receipt",
                "candidate",
                "candidate_sha256",
                "generation",
                "intent",
                "rehearsal_receipt",
            }:
                raise DRGenerationIndexError("dr_generation_index_invalid")
            intent = pending.get("intent")
            if intent not in INDEX_INTENTS:
                raise DRGenerationIndexError("dr_generation_index_invalid")
            candidate = normalize_generation_candidate(pending.get("candidate"))
            if pending.get("candidate_sha256") != _sha256(_canonical_json(candidate)):
                raise DRGenerationIndexError("dr_generation_index_invalid")
            authentication = pending.get("authentication_receipt")
            rehearsal = pending.get("rehearsal_receipt")
            generation_ref = pending.get("generation")
            normalized_generation_ref: dict[str, str] | None = None
            if phase == "prepared":
                if authentication is not None or rehearsal is not None or generation_ref is not None:
                    raise DRGenerationIndexError("dr_generation_index_invalid")
            else:
                authentication_ref = _normalize_external_receipt(
                    authentication, code="authentication_receipt_invalid"
                )
                authentication_body = self._load_external_receipt(
                    authentication_ref,
                    receipt_fd,
                    kind=AUTHENTICATION_RECEIPT_KIND,
                    code="authentication_receipt_invalid",
                )
                validate_authentication_receipt(
                    authentication_body,
                    candidate=candidate,
                )
                if phase == "authenticated":
                    if rehearsal is not None or generation_ref is not None:
                        raise DRGenerationIndexError("dr_generation_index_invalid")
                else:
                    rehearsal_ref = _normalize_external_receipt(rehearsal, code="rehearsal_receipt_invalid")
                    rehearsal_body = self._load_external_receipt(
                        rehearsal_ref,
                        receipt_fd,
                        kind=REHEARSAL_RECEIPT_KIND,
                        code="rehearsal_receipt_invalid",
                    )
                    predecessor_pending = dict(pending)
                    predecessor_pending["generation"] = None
                    predecessor_pending["rehearsal_receipt"] = None
                    predecessor_core = {
                        **{key: item for key, item in payload.items() if key != "journal_sha256"},
                        "pending": predecessor_pending,
                        "phase": "authenticated",
                        "revision": int(revision) - 1,
                    }
                    validate_rehearsal_receipt(
                        rehearsal_body,
                        candidate=candidate,
                        authentication_receipt=authentication_body,
                        index_transaction_id=transaction_id,
                        index_revision=int(revision) - 1,
                        index_journal_sha256=_sha256(_canonical_json(predecessor_core)),
                    )
                    rehearsal_binding = _rehearsal_binding(
                        candidate=candidate,
                        authentication_receipt=authentication_ref,
                        index_transaction_id=transaction_id,
                        index_revision=int(revision) - 1,
                        index_journal_sha256=_sha256(_canonical_json(predecessor_core)),
                    )
                    normalized_generation_ref = _normalize_generation_ref(
                        generation_ref, code="generation_ref_invalid"
                    )
                    expected_ref, expected_raw = _generation_receipt(
                        {
                            "authentication_receipt": authentication_ref,
                            "candidate": candidate,
                            "rehearsal_binding": rehearsal_binding,
                            "rehearsal_receipt": rehearsal_ref,
                            "schema": GENERATION_SCHEMA,
                        }
                    )
                    if normalized_generation_ref != expected_ref:
                        raise DRGenerationIndexError("dr_generation_index_invalid")
                    receipt_name = f"{expected_ref['generation_id']}.json"
                    if _entry_exists_at(receipt_fd, receipt_name):
                        self._load_receipt(expected_ref, receipt_fd)
                        if (
                            _stable_private_file_at(
                                receipt_fd,
                                receipt_name,
                                mode=0o400,
                                maximum_bytes=MAX_RECEIPT_BYTES,
                                code="generation_receipt_invalid",
                            )
                            != expected_raw
                        ):
                            raise DRGenerationIndexError("generation_receipt_invalid")

            if intent == "bootstrap_current" and (current_ref is not None or older_ref is not None):
                raise DRGenerationIndexError("dr_generation_index_invalid")
            if intent == "fill_older" and (current_ref is None or older_ref is not None):
                raise DRGenerationIndexError("dr_generation_index_invalid")
            if intent == "rotate_current" and current_ref is None:
                raise DRGenerationIndexError("dr_generation_index_invalid")
            if (intent == "fill_older") != (candidate["source_kind"] == "explicit_older_adoption"):
                raise DRGenerationIndexError("dr_generation_index_invalid")
            pending_backup = str(candidate["backup_directory"])
            if intent == "fill_older" and pending_backup == current_backup:
                raise DRGenerationIndexError("dr_generation_duplicate_slot")
            if intent == "rotate_current" and pending_backup == older_backup:
                raise DRGenerationIndexError("dr_generation_duplicate_slot")
            if (
                phase == "rehearsed"
                and intent == "rotate_current"
                and pending_backup == current_backup
                and normalized_generation_ref != current_ref
            ):
                raise DRGenerationIndexError("dr_generation_duplicate_slot")
        return payload

    def _load_raw_unlocked(self, pins: _PinnedDirectories) -> bytes:
        raw = _stable_private_file_at(
            pins.state_fd,
            INDEX_NAME,
            mode=0o600,
            maximum_bytes=MAX_INDEX_BYTES,
            code="dr_generation_index_invalid",
        )
        self._require_head_matches(pins, raw)
        return raw

    def _load_unlocked(self, pins: _PinnedDirectories) -> dict[str, Any]:
        return self._decode_state(self._load_raw_unlocked(pins), pins.receipt_fd)

    def load(self) -> dict[str, Any]:
        """Authenticate the index and every committed receipt reference."""

        with self._guard() as pins:
            return self._load_unlocked(pins)

    def current_generation_identity(
        self,
        *,
        expected_journal_sha256: str,
    ) -> CurrentDRGenerationIdentity | None:
        """Return the fully validated current identity from one guard epoch.

        The caller must bind the observation to an index journal it already
        authenticated.  Generation, authentication, and rehearsal receipt
        bodies are all validated while the state and receipt directory inodes
        remain pinned; only compact external-receipt references escape.
        """

        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            current = state["current"]
            if current is None:
                return None
            generation_receipt = self._load_receipt(current, pins.receipt_fd)
            generation = _normalize_generation(generation_receipt["generation"])
            candidate = normalize_generation_candidate(generation["candidate"])
            current_ref = _normalize_generation_ref(current, code="generation_ref_invalid")
            authentication_ref = _normalize_external_receipt(
                generation["authentication_receipt"],
                code="authentication_receipt_invalid",
            )
            rehearsal_ref = _normalize_external_receipt(
                generation["rehearsal_receipt"],
                code="rehearsal_receipt_invalid",
            )
            self._require_pinned_namespace(pins)
            return CurrentDRGenerationIdentity(
                index_journal_sha256=str(state["journal_sha256"]),
                index_phase=str(state["phase"]),
                index_revision=int(state["revision"]),
                generation_id=current_ref["generation_id"],
                generation_receipt_sha256=current_ref["receipt_sha256"],
                candidate=candidate,
                candidate_sha256=_sha256(_canonical_json(candidate)),
                authentication_receipt=authentication_ref,
                rehearsal_receipt=rehearsal_ref,
            )

    def current_activation_receipt_path(
        self,
        *,
        expected_journal_sha256: str,
    ) -> Path | None:
        """Resolve the exact durable activation body for the current slot."""

        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            current = state["current"]
            if current is None:
                return None
            generation = self._load_receipt(current, pins.receipt_fd)["generation"]
            authentication = self._load_external_receipt(
                generation["authentication_receipt"],
                pins.receipt_fd,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
            )
            if authentication.get("schema") != AUTHENTICATION_RECEIPT_SCHEMA_V2:
                return None
            reference, _raw, _payload = activation_receipt_evidence(authentication)
            self._require_pinned_namespace(pins)
            return self._external_receipt_path(ACTIVATION_RECEIPT_KIND, reference["sha256"])

    def pending_activation_receipt_path(
        self,
        *,
        expected_journal_sha256: str,
    ) -> Path | None:
        """Resolve the exact durable activation body for authenticated pending work."""

        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] not in {"authenticated", "rehearsed"}:
                raise DRGenerationIndexError("dr_generation_pending_not_authenticated")
            authentication = self._load_external_receipt(
                state["pending"]["authentication_receipt"],
                pins.receipt_fd,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
            )
            if authentication.get("schema") != AUTHENTICATION_RECEIPT_SCHEMA_V2:
                return None
            reference, _raw, _payload = activation_receipt_evidence(authentication)
            self._require_pinned_namespace(pins)
            return self._external_receipt_path(ACTIVATION_RECEIPT_KIND, reference["sha256"])

    def pending_generation_identity(
        self,
        *,
        expected_journal_sha256: str,
    ) -> PendingDRGenerationIdentity:
        """Return a validated pending candidate and exact receipt bodies.

        All state and body reads occur inside one pinned state/receipt namespace
        epoch.  This is intentionally unavailable for ``prepared`` state: a DR
        rehearsal may consume only a candidate whose complete authentication
        receipt is already durable.
        """

        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            phase = str(state["phase"])
            if phase not in {"authenticated", "rehearsed"}:
                raise DRGenerationIndexError("dr_generation_pending_not_authenticated")
            pending = state["pending"]
            candidate = normalize_generation_candidate(pending["candidate"])
            candidate_sha256 = _sha256(_canonical_json(candidate))
            if pending["candidate_sha256"] != candidate_sha256:
                raise DRGenerationIndexError("dr_generation_index_invalid")
            authentication = self._load_external_receipt(
                pending["authentication_receipt"],
                pins.receipt_fd,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
            )
            rehearsal = (
                self._load_external_receipt(
                    pending["rehearsal_receipt"],
                    pins.receipt_fd,
                    kind=REHEARSAL_RECEIPT_KIND,
                    code="rehearsal_receipt_invalid",
                )
                if phase == "rehearsed"
                else None
            )
            authenticated_journal_sha256 = str(state["journal_sha256"])
            if phase == "rehearsed":
                predecessor_pending = dict(pending)
                predecessor_pending["generation"] = None
                predecessor_pending["rehearsal_receipt"] = None
                predecessor_core = {
                    **self._core_from_state(state),
                    "pending": predecessor_pending,
                    "phase": "authenticated",
                    "revision": int(state["revision"]) - 1,
                }
                authenticated_journal_sha256 = _sha256(_canonical_json(predecessor_core))
            self._require_pinned_namespace(pins)
            return PendingDRGenerationIdentity(
                index_journal_sha256=str(state["journal_sha256"]),
                authenticated_journal_sha256=authenticated_journal_sha256,
                index_phase=phase,
                index_revision=int(state["revision"]),
                index_transaction_id=str(state["transaction_id"]),
                intent=str(pending["intent"]),
                candidate=candidate,
                candidate_sha256=candidate_sha256,
                authentication_receipt=authentication,
                rehearsal_receipt=rehearsal,
            )

    @staticmethod
    def _core_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in state.items() if key != "journal_sha256"}

    @staticmethod
    def _require_cas(state: Mapping[str, Any], expected_journal_sha256: str) -> None:
        expected = _hex64(expected_journal_sha256, code="dr_generation_cas_invalid")
        if state.get("journal_sha256") != expected:
            raise DRGenerationIndexError("dr_generation_cas_mismatch")

    def _cas_replace_locked(
        self,
        current: Mapping[str, Any],
        following_core: Mapping[str, Any],
        pins: _PinnedDirectories,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        guard = namespace_guard or (lambda: None)
        guard()
        self._require_pinned_namespace(pins)
        observed = self._load_unlocked(pins)
        if observed.get("journal_sha256") != current.get("journal_sha256"):
            raise DRGenerationIndexError("dr_generation_cas_mismatch")
        expected_payload, raw = _encode_state(following_core)
        guard()
        self._replace_head_unlocked(pins, raw)
        guard()
        _replace_private_durable_at(
            pins.state_fd,
            INDEX_NAME,
            raw,
            code="dr_generation_cas_publication_failed",
        )
        guard()
        self._require_pinned_namespace(pins)
        durable = self._load_unlocked(pins)
        if durable != expected_payload:
            raise DRGenerationIndexError("dr_generation_cas_publication_failed")
        guard()
        return durable

    def prepare(
        self,
        *,
        intent: str,
        candidate: Mapping[str, Any],
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Freeze an exact candidate; no backup discovery is performed."""

        if intent not in INDEX_INTENTS:
            raise DRGenerationIndexError("dr_generation_intent_invalid")
        normalized_candidate = normalize_generation_candidate(candidate)
        guard = namespace_guard or (lambda: None)
        guard()
        with self._guard() as pins:
            guard()
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] != "clear":
                raise DRGenerationIndexError("dr_generation_transition_invalid")
            current = state["current"]
            older = state["older"]
            if intent == "bootstrap_current" and (current is not None or older is not None):
                raise DRGenerationIndexError("dr_generation_intent_invalid")
            if intent == "fill_older" and (current is None or older is not None):
                raise DRGenerationIndexError("dr_generation_intent_invalid")
            if intent == "rotate_current" and current is None:
                raise DRGenerationIndexError("dr_generation_intent_invalid")
            source_kind = normalized_candidate["source_kind"]
            if (intent == "fill_older") != (source_kind == "explicit_older_adoption"):
                raise DRGenerationIndexError("dr_generation_intent_invalid")
            pending_backup = str(normalized_candidate["backup_directory"])
            current_backup = self._reference_backup_directory(current, pins.receipt_fd)
            older_backup = self._reference_backup_directory(older, pins.receipt_fd)
            if intent == "fill_older" and pending_backup == current_backup:
                raise DRGenerationIndexError("dr_generation_duplicate_slot")
            if intent == "rotate_current" and pending_backup == older_backup:
                raise DRGenerationIndexError("dr_generation_duplicate_slot")
            pending = {
                "authentication_receipt": None,
                "candidate": normalized_candidate,
                "candidate_sha256": _sha256(_canonical_json(normalized_candidate)),
                "generation": None,
                "intent": intent,
                "rehearsal_receipt": None,
            }
            following = {
                **self._core_from_state(state),
                "base_clear_sha256": state["journal_sha256"],
                "pending": pending,
                "phase": "prepared",
                "revision": int(state["revision"]) + 1,
                "transaction_id": secrets.token_hex(32),
            }
            if namespace_guard is None:
                return self._cas_replace_locked(state, following, pins)
            return self._cas_replace_locked(state, following, pins, namespace_guard=namespace_guard)

    def record_authenticated(
        self,
        *,
        receipt: Mapping[str, Any],
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Retain a complete authentication body before binding its compact ref."""

        guard = namespace_guard or (lambda: None)
        guard()
        with self._guard() as pins:
            guard()
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] != "prepared":
                raise DRGenerationIndexError("dr_generation_transition_invalid")
            normalized_receipt, receipt_raw, payload = validate_authentication_receipt(
                receipt,
                candidate=state["pending"]["candidate"],
            )
            if normalized_receipt["schema"] == AUTHENTICATION_RECEIPT_SCHEMA_V2:
                self._publish_activation_receipt(
                    authentication_receipt=payload,
                    pins=pins,
                    namespace_guard=namespace_guard,
                )
            self._publish_external_receipt(
                reference=normalized_receipt,
                raw=receipt_raw,
                pins=pins,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
                namespace_guard=namespace_guard,
            )
            pending = dict(state["pending"])
            pending["authentication_receipt"] = normalized_receipt
            following = {
                **self._core_from_state(state),
                "pending": pending,
                "phase": "authenticated",
                "revision": int(state["revision"]) + 1,
            }
            if namespace_guard is None:
                return self._cas_replace_locked(state, following, pins)
            return self._cas_replace_locked(state, following, pins, namespace_guard=namespace_guard)

    def record_rehearsed(
        self,
        *,
        receipt: Mapping[str, Any],
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Retain a complete rehearsal body before binding its compact ref."""

        guard = namespace_guard or (lambda: None)
        guard()
        with self._guard() as pins:
            guard()
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] != "authenticated":
                raise DRGenerationIndexError("dr_generation_transition_invalid")
            pending = dict(state["pending"])
            authentication_body = self._load_external_receipt(
                pending["authentication_receipt"],
                pins.receipt_fd,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
            )
            normalized_receipt, receipt_raw, _payload = validate_rehearsal_receipt(
                receipt,
                candidate=pending["candidate"],
                authentication_receipt=authentication_body,
                index_transaction_id=str(state["transaction_id"]),
                index_revision=int(state["revision"]),
                index_journal_sha256=str(state["journal_sha256"]),
            )
            pending["rehearsal_receipt"] = normalized_receipt
            generation = {
                "authentication_receipt": pending["authentication_receipt"],
                "candidate": pending["candidate"],
                "rehearsal_binding": _rehearsal_binding(
                    candidate=pending["candidate"],
                    authentication_receipt=pending["authentication_receipt"],
                    index_transaction_id=str(state["transaction_id"]),
                    index_revision=int(state["revision"]),
                    index_journal_sha256=str(state["journal_sha256"]),
                ),
                "rehearsal_receipt": normalized_receipt,
                "schema": GENERATION_SCHEMA,
            }
            generation_ref, _raw = _generation_receipt(generation)
            pending_backup = str(pending["candidate"]["backup_directory"])
            current_backup = self._reference_backup_directory(state["current"], pins.receipt_fd)
            if (
                pending["intent"] == "rotate_current"
                and pending_backup == current_backup
                and generation_ref != state["current"]
            ):
                raise DRGenerationIndexError("dr_generation_duplicate_slot")
            self._publish_external_receipt(
                reference=normalized_receipt,
                raw=receipt_raw,
                pins=pins,
                kind=REHEARSAL_RECEIPT_KIND,
                code="rehearsal_receipt_invalid",
                namespace_guard=namespace_guard,
            )
            pending["generation"] = generation_ref
            following = {
                **self._core_from_state(state),
                "pending": pending,
                "phase": "rehearsed",
                "revision": int(state["revision"]) + 1,
            }
            if namespace_guard is None:
                return self._cas_replace_locked(state, following, pins)
            return self._cas_replace_locked(state, following, pins, namespace_guard=namespace_guard)

    def _publish_locked(
        self,
        state: Mapping[str, Any],
        pins: _PinnedDirectories,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        guard = namespace_guard or (lambda: None)
        guard()
        if state["phase"] != "rehearsed":
            raise DRGenerationIndexError("dr_generation_transition_invalid")
        pending = dict(state["pending"])
        generation = {
            "authentication_receipt": pending["authentication_receipt"],
            "candidate": pending["candidate"],
            "rehearsal_binding": _rehearsal_binding(
                candidate=pending["candidate"],
                authentication_receipt=pending["authentication_receipt"],
                index_transaction_id=str(state["transaction_id"]),
                index_revision=int(state["revision"]) - 1,
                index_journal_sha256=self._authenticated_predecessor_journal(state),
            ),
            "rehearsal_receipt": pending["rehearsal_receipt"],
            "schema": GENERATION_SCHEMA,
        }
        generation_ref, receipt_raw = _generation_receipt(generation)
        if generation_ref != pending["generation"]:
            raise DRGenerationIndexError("dr_generation_index_invalid")
        current = state["current"]
        older = state["older"]
        intent = pending["intent"]
        if intent == "fill_older" and generation_ref == current:
            raise DRGenerationIndexError("dr_generation_duplicate_slot")
        if intent == "rotate_current" and generation_ref == older:
            raise DRGenerationIndexError("dr_generation_duplicate_slot")
        guard()
        self._publish_no_replace(
            directory_fd=pins.receipt_fd,
            name=f"{generation_ref['generation_id']}.json",
            raw=receipt_raw,
            final_mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code="generation_receipt_publication_failed",
        )
        guard()
        self._load_receipt(generation_ref, pins.receipt_fd)
        self._require_pinned_namespace(pins)
        guard()

        if intent == "bootstrap_current":
            next_current, next_older = generation_ref, None
        elif intent == "fill_older":
            next_current, next_older = current, generation_ref
        else:
            if generation_ref == current:
                next_current, next_older = current, older
            else:
                next_current, next_older = generation_ref, current
        following = {
            **self._core_from_state(state),
            "current": next_current,
            "older": next_older,
            "pending": None,
            "phase": "clear",
            "revision": int(state["revision"]) + 1,
        }
        if namespace_guard is None:
            return self._cas_replace_locked(state, following, pins)
        return self._cas_replace_locked(state, following, pins, namespace_guard=namespace_guard)

    def _authenticated_predecessor_journal(self, state: Mapping[str, Any]) -> str:
        if state.get("phase") != "rehearsed" or not isinstance(state.get("pending"), dict):
            raise DRGenerationIndexError("dr_generation_index_invalid")
        predecessor_pending = dict(state["pending"])
        predecessor_pending["generation"] = None
        predecessor_pending["rehearsal_receipt"] = None
        predecessor_core = {
            **self._core_from_state(state),
            "pending": predecessor_pending,
            "phase": "authenticated",
            "revision": int(state["revision"]) - 1,
        }
        return _sha256(_canonical_json(predecessor_core))

    def publish(
        self,
        *,
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Publish the immutable receipt first, then CAS the two-slot index."""

        guard = namespace_guard or (lambda: None)
        guard()
        with self._guard() as pins:
            guard()
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            return self._publish_locked(
                state,
                pins,
                namespace_guard=namespace_guard,
            )

    def recover(
        self,
        *,
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Finish only a fully rehearsed publication; earlier phases stay pinned."""

        guard = namespace_guard or (lambda: None)
        guard()
        with self._guard() as pins:
            guard()
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] == "clear":
                return state
            if state["phase"] != "rehearsed":
                raise DRGenerationIndexError("dr_generation_recovery_receipt_required")
            return self._publish_locked(
                state,
                pins,
                namespace_guard=namespace_guard,
            )

    def _pins_from_state_unlocked(
        self,
        state: Mapping[str, Any],
        directories: _PinnedDirectories,
    ) -> tuple[GenerationPin, ...]:
        pins: list[GenerationPin] = []

        def restore_release_fields(candidate: Mapping[str, Any]) -> dict[str, Any]:
            release = candidate["restore_release"]
            return {
                "restore_release_root": Path(release["root"]),
                "restore_release_commit": str(release["commit"]),
                "restore_release_tree_manifest_sha256": str(release["tree_manifest_sha256"]),
                "restore_release_wheel_sha256": str(release["wheel_sha256"]),
                "restore_release_max_schema": int(release["max_schema"]),
                "restore_release_version": str(release["version"]),
            }

        def activation_receipt_fields(
            authentication_reference: Mapping[str, str] | None,
        ) -> dict[str, Any]:
            if authentication_reference is None:
                return {
                    "activation_receipt_path": None,
                    "activation_receipt_file_sha256": None,
                }
            authentication_body = self._load_external_receipt(
                authentication_reference,
                directories.receipt_fd,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
            )
            if authentication_body.get("schema") != AUTHENTICATION_RECEIPT_SCHEMA_V2:
                return {
                    "activation_receipt_path": None,
                    "activation_receipt_file_sha256": None,
                }
            activation_reference, _raw, _payload = activation_receipt_evidence(authentication_body)
            return {
                "activation_receipt_path": self._external_receipt_path(
                    ACTIVATION_RECEIPT_KIND,
                    activation_reference["sha256"],
                ),
                "activation_receipt_file_sha256": activation_reference["sha256"],
            }

        for role in ("current", "older"):
            reference = state[role]
            if reference is None:
                continue
            receipt = self._load_receipt(reference, directories.receipt_fd)
            candidate = receipt["generation"]["candidate"]
            authentication_ref = receipt["generation"]["authentication_receipt"]
            rehearsal_ref = receipt["generation"]["rehearsal_receipt"]
            rehearsal_binding = receipt["generation"]["rehearsal_binding"]
            pins.append(
                GenerationPin(
                    role=role,
                    backup_directory=Path(candidate["backup_directory"]),
                    candidate=dict(candidate),
                    generation_id=str(reference["generation_id"]),
                    receipt_path=self._receipt_path(str(reference["generation_id"])),
                    receipt_sha256=str(reference["receipt_sha256"]),
                    authentication_receipt_path=self._external_receipt_path(
                        AUTHENTICATION_RECEIPT_KIND,
                        str(authentication_ref["sha256"]),
                    ),
                    authentication_receipt_sha256=str(authentication_ref["sha256"]),
                    **activation_receipt_fields(authentication_ref),
                    rehearsal_receipt_path=self._external_receipt_path(
                        REHEARSAL_RECEIPT_KIND,
                        str(rehearsal_ref["sha256"]),
                    ),
                    rehearsal_receipt_sha256=str(rehearsal_ref["sha256"]),
                    rehearsal_binding=dict(rehearsal_binding),
                    **restore_release_fields(candidate),
                )
            )
        if state["phase"] != "clear":
            pending = state["pending"]
            reference = pending["generation"]
            authentication_ref = pending["authentication_receipt"]
            rehearsal_ref = pending["rehearsal_receipt"]
            pending_rehearsal_binding: dict[str, Any] | None = None
            if reference is not None:
                pending_rehearsal_binding = _rehearsal_binding(
                    candidate=pending["candidate"],
                    authentication_receipt=authentication_ref,
                    index_transaction_id=str(state["transaction_id"]),
                    index_revision=int(state["revision"]) - 1,
                    index_journal_sha256=self._authenticated_predecessor_journal(state),
                )
            pins.append(
                GenerationPin(
                    role="pending",
                    backup_directory=Path(pending["candidate"]["backup_directory"]),
                    candidate=dict(pending["candidate"]),
                    generation_id=(str(reference["generation_id"]) if reference is not None else None),
                    receipt_path=(
                        self._receipt_path(str(reference["generation_id"])) if reference is not None else None
                    ),
                    receipt_sha256=(str(reference["receipt_sha256"]) if reference is not None else None),
                    authentication_receipt_path=(
                        self._external_receipt_path(
                            AUTHENTICATION_RECEIPT_KIND,
                            str(authentication_ref["sha256"]),
                        )
                        if authentication_ref is not None
                        else None
                    ),
                    authentication_receipt_sha256=(
                        str(authentication_ref["sha256"]) if authentication_ref is not None else None
                    ),
                    **activation_receipt_fields(authentication_ref),
                    rehearsal_receipt_path=(
                        self._external_receipt_path(
                            REHEARSAL_RECEIPT_KIND,
                            str(rehearsal_ref["sha256"]),
                        )
                        if rehearsal_ref is not None
                        else None
                    ),
                    rehearsal_receipt_sha256=(
                        str(rehearsal_ref["sha256"]) if rehearsal_ref is not None else None
                    ),
                    rehearsal_binding=pending_rehearsal_binding,
                    **restore_release_fields(pending["candidate"]),
                )
            )
        return tuple(pins)

    def authority_snapshot(self) -> DRGenerationAuthoritySnapshot:
        """Return index bytes, their file digest, and pins from one guard epoch."""

        with self._guard() as directories:
            raw = self._load_raw_unlocked(directories)
            state = self._decode_state(raw, directories.receipt_fd)
            authority_pins = self._pins_from_state_unlocked(state, directories)
            return DRGenerationAuthoritySnapshot(
                index_path=self.path,
                index_raw=raw,
                index_sha256=_sha256(raw),
                pins=authority_pins,
            )

    def pins(self) -> tuple[GenerationPin, ...]:
        """Return exact backup and restore-release pins from authenticated state."""

        return self.authority_snapshot().pins


__all__ = [
    "ACTIVATION_RECEIPT_KIND",
    "AUTHENTICATION_RECEIPT_SCHEMA",
    "AUTHENTICATION_RECEIPT_SCHEMA_V2",
    "AUTHENTICATION_RECEIPT_KIND",
    "CurrentDRGenerationIdentity",
    "DR_REHEARSAL_CHECKS",
    "DR_REHEARSAL_CHECKSET_SHA256",
    "DRGenerationAuthoritySnapshot",
    "DRGenerationIndexError",
    "DurableDRGenerationIndex",
    "GENERATION_CANDIDATE_SCHEMA",
    "GENERATION_RECEIPT_SCHEMA",
    "GENERATION_SCHEMA",
    "GenerationPin",
    "HEAD_FENCE_DIRECTORY_PREFIX",
    "HEAD_FENCE_NAME",
    "HEAD_FENCE_SCHEMA",
    "INDEX_INTENTS",
    "INDEX_NAME",
    "INDEX_PHASES",
    "INDEX_SCHEMA",
    "MAX_HEAD_FENCE_BYTES",
    "PendingDRGenerationIdentity",
    "RECEIPT_DIRECTORY_NAME",
    "REHEARSAL_RECEIPT_SCHEMA",
    "REHEARSAL_RECEIPT_SCHEMA_V2",
    "REHEARSAL_RECEIPT_KIND",
    "REHEARSAL_BINDING_SCHEMA",
    "activation_receipt_evidence",
    "normalize_generation_candidate",
    "validate_authentication_receipt",
    "validate_rehearsal_receipt",
]
