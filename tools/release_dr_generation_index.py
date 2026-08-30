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

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INDEX_SCHEMA = "friday.immutable-release-dr-generations.v1"
GENERATION_CANDIDATE_SCHEMA = "friday.immutable-release-dr-generation-candidate.v1"
GENERATION_SCHEMA = "friday.immutable-release-dr-generation.v1"
GENERATION_RECEIPT_SCHEMA = "friday.immutable-release-dr-generation-receipt.v1"

INDEX_NAME = "immutable-release-dr-generations.v1.json"
RECEIPT_DIRECTORY_NAME = "immutable-release-dr-generation-receipts"
INDEX_PHASES = ("clear", "prepared", "authenticated", "rehearsed")
INDEX_INTENTS = ("bootstrap_current", "fill_older", "rotate_current")

MAX_INDEX_BYTES = 1 << 20
MAX_RECEIPT_BYTES = 1 << 20
MAX_CANDIDATE_BYTES = 1 << 18
ZERO_SHA256 = "0" * 64

AUTHENTICATION_RECEIPT_KIND = "authentication"
REHEARSAL_RECEIPT_KIND = "rehearsal"

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
    generation_id: str | None
    receipt_path: Path | None
    receipt_sha256: str | None
    restore_release_root: Path
    restore_release_commit: str
    restore_release_tree_manifest_sha256: str
    restore_release_wheel_sha256: str
    restore_release_max_schema: int
    restore_release_version: str


@dataclass(frozen=True)
class _PinnedDirectories:
    state_fd: int
    receipt_fd: int
    receipt_identity: tuple[int, int]


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
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
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
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
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


def _replace_private_durable_at(directory_fd: int, name: str, raw: bytes, *, code: str) -> None:
    """Durably replace one state file within an already pinned directory."""

    name = _entry_name(name, code=code)
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(12)}.new"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
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
            maximum_bytes=MAX_INDEX_BYTES,
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
            maximum_bytes=MAX_INDEX_BYTES,
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


def _external_receipt_name(kind: str, sha256: str) -> str:
    if kind not in {AUTHENTICATION_RECEIPT_KIND, REHEARSAL_RECEIPT_KIND}:
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
        "backup_directory",
        "backup_record_sha256",
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
    if source_kind not in _SOURCE_KINDS:
        raise DRGenerationIndexError("generation_candidate_invalid")
    backup_directory = _absolute_lexical(
        Path(str(value.get("backup_directory") or "")),
        code="generation_candidate_invalid",
    )
    normalized: dict[str, Any] = {
        "backup_directory": str(backup_directory),
        "backup_record_sha256": _hex64(
            value.get("backup_record_sha256"), code="generation_candidate_invalid"
        ),
        "database_receipt_sha256": _hex64(
            value.get("database_receipt_sha256"), code="generation_candidate_invalid"
        ),
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
    if len(_canonical_json(normalized)) > MAX_CANDIDATE_BYTES:
        raise DRGenerationIndexError("generation_candidate_invalid")
    return normalized


def _normalize_generation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "authentication_receipt",
        "candidate",
        "rehearsal_receipt",
        "schema",
    }:
        raise DRGenerationIndexError("generation_receipt_invalid")
    generation = {
        "authentication_receipt": _normalize_external_receipt(
            value.get("authentication_receipt"), code="generation_receipt_invalid"
        ),
        "candidate": normalize_generation_candidate(value.get("candidate")),
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


class DurableDRGenerationIndex:
    """A crash-safe fixed-slot current/older DR generation index."""

    def __init__(self, state_directory: Path) -> None:
        self.state_directory, status = _private_directory(
            state_directory,
            code="dr_generation_state_directory_invalid",
        )
        self._state_directory_identity = (int(status.st_dev), int(status.st_ino))
        self.path = self.state_directory / INDEX_NAME
        self.receipt_directory = self.state_directory / RECEIPT_DIRECTORY_NAME

    @staticmethod
    def _directory_is_private(status: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(status.st_mode)
            and status.st_uid == os.geteuid()
            and not stat.S_IMODE(status.st_mode) & 0o077
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
                not self._directory_is_private(opened)
                or (int(opened.st_dev), int(opened.st_ino)) != self._state_directory_identity
            ):
                raise DRGenerationIndexError("dr_generation_state_directory_changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _require_pinned_namespace(self, pins: _PinnedDirectories) -> None:
        try:
            state_open = os.fstat(pins.state_fd)
            receipt_open = os.fstat(pins.receipt_fd)
            state_named = os.stat(self.state_directory, follow_symlinks=False)
            receipt_named = os.stat(
                RECEIPT_DIRECTORY_NAME,
                dir_fd=pins.state_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise DRGenerationIndexError("dr_generation_directory_changed") from exc
        if (
            not self._directory_is_private(state_open)
            or not self._directory_is_private(receipt_open)
            or (int(state_open.st_dev), int(state_open.st_ino)) != self._state_directory_identity
            or (int(state_named.st_dev), int(state_named.st_ino)) != self._state_directory_identity
            or (int(receipt_open.st_dev), int(receipt_open.st_ino)) != pins.receipt_identity
            or (int(receipt_named.st_dev), int(receipt_named.st_ino)) != pins.receipt_identity
        ):
            raise DRGenerationIndexError("dr_generation_directory_changed")

    @contextmanager
    def _guard(self, *, create_receipt_directory: bool = False) -> Iterator[_PinnedDirectories]:
        state_fd = self._open_state_directory()
        receipt_fd = -1
        try:
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
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            receipt_fd = os.open(RECEIPT_DIRECTORY_NAME, flags, dir_fd=state_fd)
            receipt_open = os.fstat(receipt_fd)
            receipt_identity = (int(receipt_open.st_dev), int(receipt_open.st_ino))
            if not self._directory_is_private(receipt_open) or receipt_identity != (
                int(receipt_named.st_dev),
                int(receipt_named.st_ino),
            ):
                raise DRGenerationIndexError("dr_generation_receipt_directory_changed")
            pins = _PinnedDirectories(
                state_fd=state_fd,
                receipt_fd=receipt_fd,
                receipt_identity=receipt_identity,
            )
            self._require_pinned_namespace(pins)
            yield pins
            self._require_pinned_namespace(pins)
        finally:
            if receipt_fd >= 0:
                os.close(receipt_fd)
            with suppress(OSError):
                fcntl.flock(state_fd, fcntl.LOCK_UN)
            os.close(state_fd)

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

    def initialize(self) -> dict[str, Any]:
        """Create the empty authority exactly once, or authenticate the existing one."""

        with self._guard(create_receipt_directory=True) as pins:
            if _entry_exists_at(pins.state_fd, INDEX_NAME):
                return self._load_unlocked(pins)
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
            self._publish_no_replace(
                directory_fd=pins.state_fd,
                name=INDEX_NAME,
                raw=raw,
                final_mode=0o600,
                maximum_bytes=MAX_INDEX_BYTES,
                code="dr_generation_index_initialization_failed",
            )
            loaded = self._load_unlocked(pins)
            if loaded != payload:
                raise DRGenerationIndexError("dr_generation_index_initialization_failed")
            return loaded

    def _receipt_path(self, generation_id: str) -> Path:
        return self.receipt_directory / f"{_hex64(generation_id, code='generation_ref_invalid')}.json"

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
        return payload

    def _publish_external_receipt(
        self,
        *,
        reference: Mapping[str, str],
        raw: bytes,
        pins: _PinnedDirectories,
        kind: str,
        code: str,
    ) -> None:
        normalized_ref = _normalize_external_receipt(reference, code=code)
        self._publish_no_replace(
            directory_fd=pins.receipt_fd,
            name=_external_receipt_name(kind, normalized_ref["sha256"]),
            raw=raw,
            final_mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code=f"{kind}_receipt_publication_failed",
        )
        self._load_external_receipt(
            normalized_ref,
            pins.receipt_fd,
            kind=kind,
            code=code,
        )
        self._require_pinned_namespace(pins)

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
        self._load_external_receipt(
            generation["authentication_receipt"],
            receipt_fd,
            kind=AUTHENTICATION_RECEIPT_KIND,
            code="authentication_receipt_invalid",
        )
        self._load_external_receipt(
            generation["rehearsal_receipt"],
            receipt_fd,
            kind=REHEARSAL_RECEIPT_KIND,
            code="rehearsal_receipt_invalid",
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
                self._load_external_receipt(
                    authentication_ref,
                    receipt_fd,
                    kind=AUTHENTICATION_RECEIPT_KIND,
                    code="authentication_receipt_invalid",
                )
                if phase == "authenticated":
                    if rehearsal is not None or generation_ref is not None:
                        raise DRGenerationIndexError("dr_generation_index_invalid")
                else:
                    rehearsal_ref = _normalize_external_receipt(rehearsal, code="rehearsal_receipt_invalid")
                    self._load_external_receipt(
                        rehearsal_ref,
                        receipt_fd,
                        kind=REHEARSAL_RECEIPT_KIND,
                        code="rehearsal_receipt_invalid",
                    )
                    normalized_generation_ref = _normalize_generation_ref(
                        generation_ref, code="generation_ref_invalid"
                    )
                    expected_ref, expected_raw = _generation_receipt(
                        {
                            "authentication_receipt": authentication_ref,
                            "candidate": candidate,
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

    def _load_unlocked(self, pins: _PinnedDirectories) -> dict[str, Any]:
        raw = _stable_private_file_at(
            pins.state_fd,
            INDEX_NAME,
            mode=0o600,
            maximum_bytes=MAX_INDEX_BYTES,
            code="dr_generation_index_invalid",
        )
        return self._decode_state(raw, pins.receipt_fd)

    def load(self) -> dict[str, Any]:
        """Authenticate the index and every committed receipt reference."""

        with self._guard() as pins:
            return self._load_unlocked(pins)

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
    ) -> dict[str, Any]:
        self._require_pinned_namespace(pins)
        observed = self._load_unlocked(pins)
        if observed.get("journal_sha256") != current.get("journal_sha256"):
            raise DRGenerationIndexError("dr_generation_cas_mismatch")
        expected_payload, raw = _encode_state(following_core)
        _replace_private_durable_at(
            pins.state_fd,
            INDEX_NAME,
            raw,
            code="dr_generation_cas_publication_failed",
        )
        self._require_pinned_namespace(pins)
        durable = self._load_unlocked(pins)
        if durable != expected_payload:
            raise DRGenerationIndexError("dr_generation_cas_publication_failed")
        return durable

    def prepare(
        self,
        *,
        intent: str,
        candidate: Mapping[str, Any],
        expected_journal_sha256: str,
    ) -> dict[str, Any]:
        """Freeze an exact candidate; no backup discovery is performed."""

        if intent not in INDEX_INTENTS:
            raise DRGenerationIndexError("dr_generation_intent_invalid")
        normalized_candidate = normalize_generation_candidate(candidate)
        with self._guard() as pins:
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
            return self._cas_replace_locked(state, following, pins)

    def record_authenticated(
        self,
        *,
        receipt: Mapping[str, Any],
        expected_journal_sha256: str,
    ) -> dict[str, Any]:
        """Retain a complete authentication body before binding its compact ref."""

        normalized_receipt, receipt_raw = _external_receipt_body(
            receipt,
            code="authentication_receipt_invalid",
        )
        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] != "prepared":
                raise DRGenerationIndexError("dr_generation_transition_invalid")
            self._publish_external_receipt(
                reference=normalized_receipt,
                raw=receipt_raw,
                pins=pins,
                kind=AUTHENTICATION_RECEIPT_KIND,
                code="authentication_receipt_invalid",
            )
            pending = dict(state["pending"])
            pending["authentication_receipt"] = normalized_receipt
            following = {
                **self._core_from_state(state),
                "pending": pending,
                "phase": "authenticated",
                "revision": int(state["revision"]) + 1,
            }
            return self._cas_replace_locked(state, following, pins)

    def record_rehearsed(
        self,
        *,
        receipt: Mapping[str, Any],
        expected_journal_sha256: str,
    ) -> dict[str, Any]:
        """Retain a complete rehearsal body before binding its compact ref."""

        normalized_receipt, receipt_raw = _external_receipt_body(
            receipt,
            code="rehearsal_receipt_invalid",
        )
        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] != "authenticated":
                raise DRGenerationIndexError("dr_generation_transition_invalid")
            pending = dict(state["pending"])
            pending["rehearsal_receipt"] = normalized_receipt
            generation = {
                "authentication_receipt": pending["authentication_receipt"],
                "candidate": pending["candidate"],
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
            )
            pending["generation"] = generation_ref
            following = {
                **self._core_from_state(state),
                "pending": pending,
                "phase": "rehearsed",
                "revision": int(state["revision"]) + 1,
            }
            return self._cas_replace_locked(state, following, pins)

    def _publish_locked(
        self,
        state: Mapping[str, Any],
        pins: _PinnedDirectories,
    ) -> dict[str, Any]:
        if state["phase"] != "rehearsed":
            raise DRGenerationIndexError("dr_generation_transition_invalid")
        pending = dict(state["pending"])
        generation = {
            "authentication_receipt": pending["authentication_receipt"],
            "candidate": pending["candidate"],
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
        self._publish_no_replace(
            directory_fd=pins.receipt_fd,
            name=f"{generation_ref['generation_id']}.json",
            raw=receipt_raw,
            final_mode=0o400,
            maximum_bytes=MAX_RECEIPT_BYTES,
            code="generation_receipt_publication_failed",
        )
        self._load_receipt(generation_ref, pins.receipt_fd)
        self._require_pinned_namespace(pins)

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
        return self._cas_replace_locked(state, following, pins)

    def publish(self, *, expected_journal_sha256: str) -> dict[str, Any]:
        """Publish the immutable receipt first, then CAS the two-slot index."""

        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            return self._publish_locked(state, pins)

    def recover(self, *, expected_journal_sha256: str) -> dict[str, Any]:
        """Finish only a fully rehearsed publication; earlier phases stay pinned."""

        with self._guard() as pins:
            state = self._load_unlocked(pins)
            self._require_cas(state, expected_journal_sha256)
            if state["phase"] == "clear":
                return state
            if state["phase"] != "rehearsed":
                raise DRGenerationIndexError("dr_generation_recovery_receipt_required")
            return self._publish_locked(state, pins)

    def pins(self) -> tuple[GenerationPin, ...]:
        """Return exact backup and restore-release pins from authenticated state."""

        with self._guard() as directories:
            state = self._load_unlocked(directories)
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

            for role in ("current", "older"):
                reference = state[role]
                if reference is None:
                    continue
                receipt = self._load_receipt(reference, directories.receipt_fd)
                candidate = receipt["generation"]["candidate"]
                pins.append(
                    GenerationPin(
                        role=role,
                        backup_directory=Path(candidate["backup_directory"]),
                        generation_id=str(reference["generation_id"]),
                        receipt_path=self._receipt_path(str(reference["generation_id"])),
                        receipt_sha256=str(reference["receipt_sha256"]),
                        **restore_release_fields(candidate),
                    )
                )
            if state["phase"] != "clear":
                pending = state["pending"]
                reference = pending["generation"]
                pins.append(
                    GenerationPin(
                        role="pending",
                        backup_directory=Path(pending["candidate"]["backup_directory"]),
                        generation_id=(str(reference["generation_id"]) if reference is not None else None),
                        receipt_path=(
                            self._receipt_path(str(reference["generation_id"]))
                            if reference is not None
                            else None
                        ),
                        receipt_sha256=(str(reference["receipt_sha256"]) if reference is not None else None),
                        **restore_release_fields(pending["candidate"]),
                    )
                )
            return tuple(pins)


__all__ = [
    "AUTHENTICATION_RECEIPT_KIND",
    "DRGenerationIndexError",
    "DurableDRGenerationIndex",
    "GENERATION_CANDIDATE_SCHEMA",
    "GENERATION_RECEIPT_SCHEMA",
    "GENERATION_SCHEMA",
    "GenerationPin",
    "INDEX_INTENTS",
    "INDEX_NAME",
    "INDEX_PHASES",
    "INDEX_SCHEMA",
    "RECEIPT_DIRECTORY_NAME",
    "REHEARSAL_RECEIPT_KIND",
    "normalize_generation_candidate",
]
