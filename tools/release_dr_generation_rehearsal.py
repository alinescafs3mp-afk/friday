#!/usr/bin/env python3
"""Rehearse one authenticated DR generation on an isolated production copy.

The controller owns no publication or deletion authority.  It may only move an
already-authenticated pending generation to ``rehearsed`` after the exact four
surfaces survive a bounded activation fault and rollback in an owner-private
``/var/tmp`` contour.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_authentication as dr_auth
from tools import release_dr_generation_index as dr_index

REHEARSAL_RECEIPT_SCHEMA = "friday.immutable-release-dr-rehearsal-receipt.v1"
_SCRATCH_PARENT = Path("/var/tmp")
_SCRATCH_PREFIX = "friday-dr-rehearsal-"
_SCRATCH_REGISTRY = ".friday-dr-rehearsal-registry.v1"
_SCRATCH_RECORD_SCHEMA = "friday.dr-rehearsal-scratch-record.v1"
_BWRAP = Path("/usr/bin/bwrap")
_CHILD_TIMEOUT_SECONDS = 600
_CHILD_KILL_GRACE_SECONDS = 5
_CHILD_INPUT_LIMIT_BYTES = 16_384
_CHILD_OUTPUT_LIMIT_BYTES = 32_768
_HEX64 = frozenset("0123456789abcdef")
_CHECKS = (
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
_RECEIPT_KEYS = frozenset(
    {
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
)


class DRGenerationRehearsalError(RuntimeError):
    """A closed rehearsal failure with a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _InjectedRehearsalFault(RuntimeError):
    pass


@dataclass(frozen=True)
class _RunResult:
    schema_version: int
    rollback_tree_sha256: str
    four_surface_sha256: str
    engineer_authority_present: bool
    database_reopen_count: int = 2
    inbox_reopen_count: int = 2


@dataclass(frozen=True)
class _ScratchLease:
    root: Path
    registry: Path
    record: Path
    key: str
    transaction_id: str
    candidate_sha256: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _SealedReleaseCopy:
    source: release_operator.ReleaseIdentity
    root: Path
    identity: release_operator.ReleaseIdentity


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_noncanonical") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _canonical_friday_home() -> Path:
    raw = os.environ.get("FRIDAY_HOME")
    if not raw or any(character in raw for character in "\x00\r\n"):
        raise DRGenerationRehearsalError("dr_rehearsal_friday_home_invalid")
    home = Path(raw)
    lexical = Path(os.path.abspath(home))
    try:
        status = os.lstat(home)
        resolved = home.resolve(strict=True)
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_friday_home_invalid") from exc
    if (
        not home.is_absolute()
        or home != lexical
        or resolved != home
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_friday_home_invalid")
    return home


def _private_directory(path: Path) -> Path:
    lexical = Path(os.path.abspath(path))
    parent = lexical.parent
    descriptor = -1
    try:
        if path != lexical or parent.resolve(strict=True) != parent:
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_invalid")
        parent_fd = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            created = False
            try:
                os.mkdir(lexical.name, mode=0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            descriptor = os.open(
                lexical.name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            if created:
                os.fchmod(descriptor, 0o700)
                os.fsync(parent_fd)
            opened = os.fstat(descriptor)
            observed = os.stat(lexical.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
                or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            ):
                raise DRGenerationRehearsalError("dr_rehearsal_scratch_invalid")
        finally:
            os.close(parent_fd)
        return lexical
    except DRGenerationRehearsalError:
        raise
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _scratch_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_invalid") from exc
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_uid),
        stat.S_IMODE(status.st_mode),
    )


def _scratch_record(
    *,
    key: str,
    transaction_id: str,
    candidate_sha256: str,
    scratch_name: str,
    phase: str,
    identity: tuple[int, int, int, int] | None,
) -> bytes:
    core = {
        "candidate_sha256": candidate_sha256,
        "identity": list(identity) if identity is not None else None,
        "key": key,
        "phase": phase,
        "schema": _SCRATCH_RECORD_SCHEMA,
        "scratch_name": scratch_name,
        "transaction_id": transaction_id,
    }
    return _canonical({**core, "record_sha256": _sha256(_canonical(core))}) + b"\n"


def _write_scratch_record(registry_fd: int, name: str, raw: bytes) -> None:
    temporary = f".{name}.new"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        with suppress(FileNotFoundError):
            existing = os.stat(temporary, dir_fd=registry_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != os.geteuid()
                or existing.st_nlink != 1
                or stat.S_IMODE(existing.st_mode) != 0o600
            ):
                raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid")
            os.unlink(temporary, dir_fd=registry_fd)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=registry_fd)
        release_operator._write_all(descriptor, raw)  # noqa: SLF001
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=registry_fd, dst_dir_fd=registry_fd)
        os.fsync(registry_fd)
    except DRGenerationRehearsalError:
        raise
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_scratch_record(registry_fd: int, name: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=registry_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= 4_096
        ):
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid")
        chunks: list[bytes] = []
        remaining = 4_097
        while remaining and (chunk := os.read(descriptor, remaining)):
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        lexical = os.stat(name, dir_fd=registry_fd, follow_symlinks=False)
        if (
            len(raw) > 4_096
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or (before.st_dev, before.st_ino) != (lexical.st_dev, lexical.st_ino)
        ):
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid")
        try:
            value = json.loads(raw.decode("ascii"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid") from exc
        if not isinstance(value, dict) or raw != _canonical(value) + b"\n":
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid")
        return value
    finally:
        os.close(descriptor)


def _remove_registered_tree(
    registry: Path,
    scratch_name: str,
    *,
    expected_identity: tuple[int, int, int, int] | None,
    require_empty: bool = False,
) -> bool:
    """Remove only the exact pinned scratch inode via an unexposed quarantine."""

    if not {
        os.open,
        os.rename,
        os.rmdir,
        os.stat,
        os.unlink,
    }.issubset(os.supports_dir_fd) or os.stat not in os.supports_follow_symlinks:
        raise DRGenerationRehearsalError("dr_rehearsal_safe_cleanup_unavailable")
    registry_fd = os.open(
        registry,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    scratch_fd = -1
    quarantine = f".{scratch_name}.cleanup"
    try:
        try:
            lexical = os.stat(scratch_name, dir_fd=registry_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        scratch_fd = os.open(scratch_name, flags, dir_fd=registry_fd)
        opened = os.fstat(scratch_fd)
        observed = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_uid),
            stat.S_IMODE(opened.st_mode),
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(lexical.st_mode)
            or observed
            != (
                int(lexical.st_dev),
                int(lexical.st_ino),
                int(lexical.st_uid),
                stat.S_IMODE(lexical.st_mode),
            )
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or expected_identity is not None
            and observed != expected_identity
            or require_empty
            and os.listdir(scratch_fd)
        ):
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
        try:
            os.stat(quarantine, dir_fd=registry_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
        os.rename(
            scratch_name,
            quarantine,
            src_dir_fd=registry_fd,
            dst_dir_fd=registry_fd,
        )
        quarantined = os.stat(quarantine, dir_fd=registry_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (quarantined.st_dev, quarantined.st_ino):
            with suppress(OSError):
                os.rename(
                    quarantine,
                    scratch_name,
                    src_dir_fd=registry_fd,
                    dst_dir_fd=registry_fd,
                )
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
        # Traverse only through the descriptor pinned before the rename.  A
        # replacement of the quarantined pathname is therefore never walked
        # or deleted; the final identity check below fails closed instead.
        _empty_pinned_scratch_directory(scratch_fd)
        final = os.stat(quarantine, dir_fd=registry_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino):
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
        os.rmdir(quarantine, dir_fd=registry_fd)
        os.fsync(registry_fd)
        try:
            os.stat(quarantine, dir_fd=registry_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed")
    except DRGenerationRehearsalError:
        raise
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed") from exc
    finally:
        if scratch_fd >= 0:
            os.close(scratch_fd)
        os.close(registry_fd)


def _empty_pinned_scratch_directory(directory_fd: int) -> None:
    """Empty one scratch directory without following a mutable root pathname."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for name in sorted(os.listdir(directory_fd)):
        if name in {"", ".", ".."}:
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
        try:
            lexical = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused") from exc
        if lexical.st_uid != os.geteuid():
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
        if stat.S_ISDIR(lexical.st_mode):
            child_fd = -1
            try:
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                opened = os.fstat(child_fd)
                if (
                    (opened.st_dev, opened.st_ino) != (lexical.st_dev, lexical.st_ino)
                    or stat.S_IMODE(opened.st_mode) & 0o022
                ):
                    raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
                _empty_pinned_scratch_directory(child_fd)
                final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino):
                    raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
            continue
        if stat.S_ISREG(lexical.st_mode):
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            file_flags |= getattr(os, "O_NOFOLLOW", 0)
            file_fd = -1
            try:
                file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                opened = os.fstat(file_fd)
                if (
                    (opened.st_dev, opened.st_ino) != (lexical.st_dev, lexical.st_ino)
                    or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) & 0o022
                ):
                    raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
                final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino):
                    raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
                os.unlink(name, dir_fd=directory_fd)
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
            continue
        if stat.S_ISLNK(lexical.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
    os.fsync(directory_fd)


def _new_scratch(*, transaction_id: str, candidate_sha256: str) -> _ScratchLease:
    if not _is_hex64(transaction_id) or not _is_hex64(candidate_sha256):
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_binding_invalid")
    key = _sha256(_canonical({"candidate": candidate_sha256, "transaction": transaction_id}))
    scratch_name = f"{_SCRATCH_PREFIX}{key}"
    record_name = f"record-{key}.json"
    try:
        parent_status = os.lstat(_SCRATCH_PARENT)
        if not stat.S_ISDIR(parent_status.st_mode) or stat.S_ISLNK(parent_status.st_mode):
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_parent_invalid")
        registry = _private_directory(_SCRATCH_PARENT / _SCRATCH_REGISTRY)
        registry_fd = os.open(
            registry,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            existing = _read_scratch_record(registry_fd, record_name)
            if existing is not None:
                core = {name: value for name, value in existing.items() if name != "record_sha256"}
                expected_keys = {
                    "candidate_sha256",
                    "identity",
                    "key",
                    "phase",
                    "record_sha256",
                    "schema",
                    "scratch_name",
                    "transaction_id",
                }
                identity_raw = existing.get("identity")
                identity: tuple[int, int, int, int] | None = None
                identity_valid = identity_raw is None
                if (
                    isinstance(identity_raw, list)
                    and len(identity_raw) == 4
                    and all(type(value) is int for value in identity_raw)
                ):
                    identity = (
                        identity_raw[0],
                        identity_raw[1],
                        identity_raw[2],
                        identity_raw[3],
                    )
                    identity_valid = True
                if (
                    set(existing) != expected_keys
                    or not identity_valid
                    or existing.get("schema") != _SCRATCH_RECORD_SCHEMA
                    or existing.get("key") != key
                    or existing.get("transaction_id") != transaction_id
                    or existing.get("candidate_sha256") != candidate_sha256
                    or existing.get("scratch_name") != scratch_name
                    or existing.get("phase") not in {"prepared", "active"}
                    or existing.get("record_sha256") != _sha256(_canonical(core))
                    or (existing.get("phase") == "prepared") != (identity is None)
                ):
                    raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid")
                _remove_registered_tree(
                    registry,
                    scratch_name,
                    expected_identity=identity,
                    require_empty=identity is None,
                )
                os.unlink(record_name, dir_fd=registry_fd)
                os.fsync(registry_fd)
            try:
                os.stat(scratch_name, dir_fd=registry_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise DRGenerationRehearsalError("dr_rehearsal_unbound_scratch_exists")
            prepared = _scratch_record(
                key=key,
                transaction_id=transaction_id,
                candidate_sha256=candidate_sha256,
                scratch_name=scratch_name,
                phase="prepared",
                identity=None,
            )
            _write_scratch_record(registry_fd, record_name, prepared)
            os.mkdir(scratch_name, mode=0o700, dir_fd=registry_fd)
            os.fsync(registry_fd)
            root = registry / scratch_name
            identity = _scratch_identity(root)
            active = _scratch_record(
                key=key,
                transaction_id=transaction_id,
                candidate_sha256=candidate_sha256,
                scratch_name=scratch_name,
                phase="active",
                identity=identity,
            )
            _write_scratch_record(registry_fd, record_name, active)
        finally:
            os.close(registry_fd)
        return _ScratchLease(
            root=root,
            registry=registry,
            record=registry / record_name,
            key=key,
            transaction_id=transaction_id,
            candidate_sha256=candidate_sha256,
            identity=identity,
        )
    except DRGenerationRehearsalError:
        raise
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_unavailable") from exc


def _remove_current_scratch(lease: _ScratchLease) -> None:
    record_name = lease.record.name
    registry_fd = os.open(
        lease.registry,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        record = _read_scratch_record(registry_fd, record_name)
        expected = _scratch_record(
            key=lease.key,
            transaction_id=lease.transaction_id,
            candidate_sha256=lease.candidate_sha256,
            scratch_name=lease.root.name,
            phase="active",
            identity=lease.identity,
        )
        if record is None or _canonical(record) + b"\n" != expected:
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_record_invalid")
        _remove_registered_tree(
            lease.registry,
            lease.root.name,
            expected_identity=lease.identity,
        )
        os.unlink(record_name, dir_fd=registry_fd)
        os.fsync(registry_fd)
    except DRGenerationRehearsalError:
        raise
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed") from exc
    finally:
        os.close(registry_fd)


def _scratch_config(scratch: Path, *, obsidian_present: bool) -> release_operator.SystemdConfig:
    data = _private_directory(scratch / "data")
    state = _private_directory(data / "state")
    backup = _private_directory(data / "backups")
    unit_dir = _private_directory(scratch / "units")
    cache = _private_directory(scratch / "cache")
    logs = _private_directory(scratch / "logs")
    env_file = scratch / "friday.env"
    health_ca = scratch / "health-ca.pem"
    environment_values = {
        "FRIDAY_CACHE_DIR": str(cache),
        "FRIDAY_DATABASE_MUST_EXIST": "1",
        "FRIDAY_DATABASE_PATH": str(data / "friday.sqlite3"),
        "FRIDAY_DATA_DIR": str(data),
        "FRIDAY_ENGINEER_COMMAND_ENABLED": "0",
        "FRIDAY_ENGINEER_MODE_ENABLED": "0",
        "FRIDAY_HOME": str(scratch),
        "FRIDAY_LOG_DIR": str(logs),
        "FRIDAY_MEMORY_VAULT_MODE": "disabled",
        "FRIDAY_OBSIDIAN_ENABLED": "1" if obsidian_present else "0",
        "FRIDAY_OBSIDIAN_ROOT": str(data / "obsidian"),
        "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
        "FRIDAY_SECONDARY_LLM_ENABLED": "0",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "off",
        "FRIDAY_STATE_DIR": str(state),
    }
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in environment_values.items()),
        encoding="ascii",
    )
    health_ca.write_bytes(b"isolated-dr-rehearsal\n")
    env_file.chmod(0o600)
    health_ca.chmod(0o600)
    return release_operator.SystemdConfig(
        anchor=scratch / "active",
        env_file=env_file,
        env_file_sha256=_sha256(env_file.read_bytes()),
        friday_home=scratch,
        unit_dir=unit_dir,
        database=data / "friday.sqlite3",
        inbox_database=state / "telegram-inbox.sqlite3",
        backup_dir=backup,
        state_dir=state,
        health_ca=health_ca,
        health_ca_sha256=_sha256(health_ca.read_bytes()),
        obsidian_mode="enabled" if obsidian_present else "disabled",
        obsidian_root=data / "obsidian",
    )


def _release_identity_projection(release: release_operator.ReleaseIdentity) -> tuple[object, ...]:
    return (
        release.commit,
        release.version,
        release.tree_manifest_sha256,
        release.max_schema,
        release.memory_vault_mode_contract,
        release.venv_relocation_contract,
        release.obsidian_cutover_contract,
        release.secondary_product_runner_sha256,
        release.engineer_command_lifecycle_contract,
        release.operator_transaction_lock_scope_contract,
        release.operator_transaction_lock_scope_sha256,
    )


def _materialize_exact_release_copy(
    release: release_operator.ReleaseIdentity,
    destination: Path,
) -> _SealedReleaseCopy:
    """Copy, seal and reauthenticate one release before any executable use."""

    try:
        before = release_operator.load_release_identity(
            release.root,
            expected_tree_sha256=release.tree_manifest_sha256,
        )
        if before != release or destination.exists() or destination.is_symlink():
            raise DRGenerationRehearsalError("dr_rehearsal_release_copy_invalid")
        parent = _private_directory(destination.parent)
        shutil.copytree(
            release.root,
            destination,
            symlinks=True,
            copy_function=shutil.copy2,
        )
        copied = release_operator.load_release_identity(
            destination,
            expected_tree_sha256=release.tree_manifest_sha256,
        )
        after = release_operator.load_release_identity(
            release.root,
            expected_tree_sha256=release.tree_manifest_sha256,
        )
        if (
            _release_identity_projection(copied) != _release_identity_projection(release)
            or after != release
            or destination.parent != parent
            or stat.S_IMODE(os.lstat(destination).st_mode) != 0o500
        ):
            raise DRGenerationRehearsalError("dr_rehearsal_release_copy_changed")
        return _SealedReleaseCopy(source=release, root=destination, identity=copied)
    except DRGenerationRehearsalError:
        raise
    except (OSError, release_operator.ReleaseFailure, shutil.Error) as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_release_copy_invalid") from exc


def _materialize_exact_releases(
    releases: Sequence[release_operator.ReleaseIdentity],
    sealed_root: Path,
) -> dict[release_operator.ReleaseIdentity, _SealedReleaseCopy]:
    _private_directory(sealed_root)
    result: dict[release_operator.ReleaseIdentity, _SealedReleaseCopy] = {}
    for index, release in enumerate(releases):
        if release in result:
            continue
        result[release] = _materialize_exact_release_copy(
            release,
            sealed_root / f"release-{index}-{release.tree_manifest_sha256}",
        )
    return result


def _release_store_environment(config: release_operator.SystemdConfig) -> dict[str, str]:
    return {
        "FRIDAY_DATABASE_MUST_EXIST": "1",
        "FRIDAY_DATABASE_PATH": str(config.database),
        "FRIDAY_ENV_FILE": str(config.env_file),
        "FRIDAY_HOME": str(config.friday_home),
        "HOME": str(config.friday_home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/run/friday/no-executables",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _trusted_bwrap_identity() -> tuple[int, int, int, int]:
    try:
        status = os.stat(_BWRAP, follow_symlinks=False)
        resolved = _BWRAP.resolve(strict=True)
    except OSError as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_bwrap_unavailable") from exc
    if (
        resolved != _BWRAP
        or not stat.S_ISREG(status.st_mode)
        or status.st_uid != 0
        or status.st_nlink != 1
        or status.st_mode & 0o022
        or not os.access(_BWRAP, os.X_OK)
    ):
        raise release_operator.ReleaseFailure("dr_rehearsal_bwrap_untrusted")
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_size),
        int(status.st_mtime_ns),
    )


def _bwrap_directory_arguments(paths: Sequence[Path]) -> list[str]:
    directories: set[Path] = set()
    for path in paths:
        current = path.parent
        while current != Path("/"):
            if current not in {Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")}:
                directories.add(current)
            current = current.parent
    arguments: list[str] = []
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        arguments.extend(("--dir", str(directory)))
    return arguments


def _bwrap_argv(
    sealed: _SealedReleaseCopy,
    config: release_operator.SystemdConfig,
    *,
    release_fd: int,
    workspace_fd: int,
    script: str,
    arguments: Sequence[str],
) -> list[str]:
    environment = _release_store_environment(config)
    command = [
        str(_BWRAP),
        "--unshare-all",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--dir",
        "/usr",
        "--ro-bind",
        "/usr/lib",
        "/usr/lib",
        "--ro-bind-try",
        "/usr/lib64",
        "/usr/lib64",
        "--dir",
        "/lib",
        "--ro-bind-try",
        "/lib/x86_64-linux-gnu",
        "/lib/x86_64-linux-gnu",
        "--dir",
        "/lib64",
        "--ro-bind-try",
        "/lib64/ld-linux-x86-64.so.2",
        "/lib64/ld-linux-x86-64.so.2",
        "--dir",
        "/etc",
        "--ro-bind-try",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(64 << 20),
        "--tmpfs",
        "/tmp",
        *_bwrap_directory_arguments((sealed.source.root, config.friday_home)),
        "--ro-bind-fd",
        str(release_fd),
        str(sealed.source.root),
        "--bind-fd",
        str(workspace_fd),
        str(config.friday_home),
    ]
    for key, value in sorted(environment.items()):
        command.extend(("--setenv", key, value))
    command.extend(
        (
            "--chdir",
            str(config.friday_home),
            "--",
            str(sealed.source.root / "venv/bin/python"),
            "-I",
            "-B",
            "-c",
            script,
            *arguments,
        )
    )
    return command


def _kill_bwrap_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_child_cleanup_failed") from exc
    try:
        process.wait(timeout=_CHILD_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_child_cleanup_failed") from exc
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_child_cleanup_failed") from exc
    raise release_operator.ReleaseFailure("dr_rehearsal_child_descendant_survived")


def _bounded_communicate(
    process: subprocess.Popen[bytes],
    *,
    input_bytes: bytes,
    timeout: int,
) -> tuple[bytes, bytes]:
    """Drain a sealed child without unbounded pipe buffering."""

    if (
        len(input_bytes) > _CHILD_INPUT_LIMIT_BYTES
        or process.stdin is None
        or process.stdout is None
        or process.stderr is None
    ):
        raise release_operator.ReleaseFailure("dr_rehearsal_release_io_invalid")
    try:
        process.stdin.write(input_bytes)
        process.stdin.close()
    except BrokenPipeError:
        with suppress(BrokenPipeError):
            process.stdin.close()
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout.fileno(): ("stdout", process.stdout),
        process.stderr.fileno(): ("stderr", process.stderr),
    }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        for descriptor, (_name, stream) in streams.items():
            os.set_blocking(descriptor, False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _mask in selector.select(timeout=min(remaining, 0.25)):
                descriptor = int(key.fd)
                name, _stream = streams[descriptor]
                chunk = os.read(descriptor, 8_192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[name].extend(chunk)
                if sum(len(value) for value in buffers.values()) > _CHILD_OUTPUT_LIMIT_BYTES:
                    raise release_operator.ReleaseFailure("dr_rehearsal_release_output_too_large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        process.wait(timeout=remaining)
    finally:
        selector.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _execute_bwrap(
    command: Sequence[str],
    *,
    input_bytes: bytes,
    timeout: int,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[bytes]:
    identity = _trusted_bwrap_identity()
    try:
        process = subprocess.Popen(  # noqa: S603
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path("/"),
            env={"LANG": "C", "LC_ALL": "C"},
            start_new_session=True,
            pass_fds=tuple(pass_fds),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_release_open_failed") from exc
    if _trusted_bwrap_identity() != identity:
        _kill_bwrap_process_group(process)
        raise release_operator.ReleaseFailure("dr_rehearsal_bwrap_changed")
    try:
        stdout, stderr = _bounded_communicate(
            process,
            input_bytes=input_bytes,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _kill_bwrap_process_group(process)
        raise release_operator.ReleaseFailure("dr_rehearsal_release_open_timeout") from exc
    except BaseException:
        _kill_bwrap_process_group(process)
        raise
    # bwrap is PID 1 of the private process namespace and does not return until
    # its descendants are gone.  A surviving outer process group is a failure.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_child_cleanup_failed") from exc
    else:
        _kill_bwrap_process_group(process)
        raise release_operator.ReleaseFailure("dr_rehearsal_child_descendant_survived")
    return subprocess.CompletedProcess(command, int(process.returncode or 0), stdout, stderr)


def _run_release_python(
    sealed: _SealedReleaseCopy,
    config: release_operator.SystemdConfig,
    *,
    script: str,
    arguments: Sequence[str],
    input_bytes: bytes = b"",
    maximum_output: int = 8_192,
) -> bytes:
    release_fd = -1
    workspace_fd = -1
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        release_fd = os.open(sealed.root, flags)
        workspace_fd = os.open(config.friday_home, flags)
        release_opened = os.fstat(release_fd)
        release_lexical = os.stat(sealed.root, follow_symlinks=False)
        workspace_opened = os.fstat(workspace_fd)
        workspace_lexical = os.stat(config.friday_home, follow_symlinks=False)
        if (
            (release_opened.st_dev, release_opened.st_ino)
            != (release_lexical.st_dev, release_lexical.st_ino)
            or (workspace_opened.st_dev, workspace_opened.st_ino)
            != (workspace_lexical.st_dev, workspace_lexical.st_ino)
            or stat.S_IMODE(release_opened.st_mode) != 0o500
            or stat.S_IMODE(workspace_opened.st_mode) != 0o700
        ):
            raise release_operator.ReleaseFailure("dr_rehearsal_mount_identity_invalid")
        command = _bwrap_argv(
            sealed,
            config,
            release_fd=release_fd,
            workspace_fd=workspace_fd,
            script=script,
            arguments=arguments,
        )
        completed = _execute_bwrap(
            command,
            input_bytes=input_bytes,
            timeout=_CHILD_TIMEOUT_SECONDS,
            pass_fds=(release_fd, workspace_fd),
        )
        copied_after = release_operator.load_release_identity(
            sealed.root,
            expected_tree_sha256=sealed.source.tree_manifest_sha256,
        )
        if (
            _release_identity_projection(copied_after) != _release_identity_projection(sealed.source)
            or (os.fstat(release_fd).st_dev, os.fstat(release_fd).st_ino)
            != (os.stat(sealed.root, follow_symlinks=False).st_dev, os.stat(sealed.root, follow_symlinks=False).st_ino)
            or (os.fstat(workspace_fd).st_dev, os.fstat(workspace_fd).st_ino)
            != (
                os.stat(config.friday_home, follow_symlinks=False).st_dev,
                os.stat(config.friday_home, follow_symlinks=False).st_ino,
            )
        ):
            raise release_operator.ReleaseFailure("dr_rehearsal_mount_identity_changed")
    finally:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        if release_fd >= 0:
            os.close(release_fd)
    if (
        completed.returncode != 0
        or completed.stderr
        or not 0 < len(completed.stdout) <= maximum_output
        or not completed.stdout.endswith(b"\n")
    ):
        raise release_operator.ReleaseFailure("dr_rehearsal_release_open_failed")
    return completed.stdout


def _run_release_store(
    sealed: _SealedReleaseCopy,
    config: release_operator.SystemdConfig,
) -> dict[str, Any]:
    """Open both SQLite surfaces with one exact copied release interpreter."""

    script = r"""
import json,logging,os,pathlib,sys
logging.disable(logging.CRITICAL)
os.environ["FRIDAY_ENV_FILE"]=sys.argv[1]
from friday import __version__
from friday.config import load_local_env_file,load_settings
from friday.storage import FridayStorage,SCHEMA_VERSION
from friday.telegram_bridge import _UpdateInbox
load_local_env_file(); settings=load_settings()
assert settings.database_path.resolve(strict=True)==pathlib.Path(sys.argv[2]).resolve(strict=True)
assert pathlib.Path(sys.argv[3]).resolve(strict=True)==pathlib.Path(settings.state_dir,"telegram-inbox.sqlite3").resolve(strict=True)
assert settings.database_must_exist is True
assert settings.home.resolve(strict=True)==pathlib.Path(sys.argv[4]).resolve(strict=True)
assert SCHEMA_VERSION==int(sys.argv[5])
store=FridayStorage(settings)
try:
 row=store.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
 assert row is not None
 out={"main_fk":len(store.conn.execute("PRAGMA foreign_key_check").fetchall()),"main_integrity":store.conn.execute("PRAGMA integrity_check").fetchone()[0],"main_schema":int(row[0]),"version":__version__}
finally: store.close(final=True)
inbox=_UpdateInbox(str(pathlib.Path(sys.argv[3])))
try:
 out["inbox_fk"]=len(inbox._conn.execute("PRAGMA foreign_key_check").fetchall())
 out["inbox_integrity"]=inbox._conn.execute("PRAGMA integrity_check").fetchone()[0]
finally: inbox.close()
print(json.dumps(out,sort_keys=True,separators=(",",":")))
"""
    release = sealed.source
    stdout = _run_release_python(
        sealed,
        config,
        script=script,
        arguments=(
            str(config.env_file),
            str(config.database),
            str(config.inbox_database),
            str(config.friday_home),
            str(release.max_schema),
        ),
        maximum_output=4_096,
    )

    def unique_pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise release_operator.ReleaseFailure("dr_rehearsal_release_receipt_invalid")
            result[key] = value
        return result

    try:
        receipt = json.loads(
            stdout.decode("ascii"),
            object_pairs_hook=unique_pairs,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_release_receipt_invalid") from exc
    expected = {
        "inbox_fk": 0,
        "inbox_integrity": "ok",
        "main_fk": 0,
        "main_integrity": "ok",
        "main_schema": release.max_schema,
        "version": release.version,
    }
    if (
        not isinstance(receipt, dict)
        or receipt != expected
        or stdout != _canonical(expected) + b"\n"
    ):
        raise release_operator.ReleaseFailure("dr_rehearsal_release_receipt_invalid")
    return receipt


def _run_release_engineer_authority(
    sealed: _SealedReleaseCopy,
    config: release_operator.SystemdConfig,
    *,
    action: str,
    database_sha256: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> object:
    if action not in {"snapshot", "attest", "verify"}:
        raise release_operator.ReleaseFailure("dr_rehearsal_engineer_authority_invalid")
    store, key, state = release_operator._engineer_artifact_paths(config)  # noqa: SLF001
    script = r"""
import json,logging,pathlib,sys
logging.disable(logging.CRITICAL)
from friday.config import load_local_env_file,load_settings
from friday.organs.engineer.command_tools import open_engineer_command_backup_authority
load_local_env_file(); settings=load_settings()
action=sys.argv[1]; home=pathlib.Path(sys.argv[2]); store=pathlib.Path(sys.argv[3]); key=pathlib.Path(sys.argv[4]); state=pathlib.Path(sys.argv[5]); digest=sys.argv[6]
assert pathlib.Path(settings.home).resolve(strict=True)==home.resolve(strict=True)
assert pathlib.Path(settings.engineer_command_store_dir)==store
assert pathlib.Path(settings.engineer_command_key_file)==key
assert pathlib.Path(settings.state_dir)==state
with open_engineer_command_backup_authority(settings,exclusive=False) as authority:
 if action=='snapshot': result={'snapshot':authority.backup_authority_snapshot()}
 elif action=='attest':
  before=authority.backup_authority_snapshot(); proof=authority.attest_main_database_backup(digest); verified=authority.verify_main_database_backup_authority(proof,digest); after=authority.backup_authority_snapshot(); result={'before':before,'evidence':proof,'verified':verified,'after':after}
 elif action=='verify':
  proof=json.loads(sys.stdin.buffer.read()); before=authority.backup_authority_snapshot(); verified=authority.verify_main_database_backup_authority(proof,digest); after=authority.backup_authority_snapshot(); result={'before':before,'verified':verified,'after':after}
 else: raise RuntimeError('action mismatch')
print(json.dumps(result,sort_keys=True,separators=(',',':')))
"""
    stdout = _run_release_python(
        sealed,
        config,
        script=script,
        arguments=(
            action,
            str(config.friday_home),
            str(store),
            str(key),
            str(state),
            database_sha256,
        ),
        input_bytes=_canonical(dict(evidence)) if evidence is not None else b"",
    )
    try:
        parsed = json.loads(stdout.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_engineer_authority_invalid") from exc
    if not isinstance(parsed, dict) or stdout != _canonical(parsed) + b"\n":
        raise release_operator.ReleaseFailure("dr_rehearsal_engineer_authority_invalid")
    if action == "snapshot":
        if set(parsed) != {"snapshot"}:
            raise release_operator.ReleaseFailure("dr_rehearsal_engineer_authority_invalid")
        return parsed["snapshot"]
    if set(parsed) != {"after", "before", "verified"} | ({"evidence"} if action == "attest" else set()):
        raise release_operator.ReleaseFailure("dr_rehearsal_engineer_authority_invalid")
    return parsed


def _surface_digest(
    config: release_operator.SystemdConfig,
    *,
    include_sqlite: bool = True,
) -> str:
    sqlite_files: list[dict[str, Any]] = []
    for label, database in (("database", config.database), ("inbox", config.inbox_database)):
        for suffix in ("", "-wal"):
            path = Path(f"{database}{suffix}")
            if not path.exists():
                continue
            status = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_nlink != 1
                or stat.S_IMODE(status.st_mode) & 0o077
            ):
                raise DRGenerationRehearsalError("dr_rehearsal_surface_invalid")
            sqlite_files.append(
                {
                    "name": f"{label}.sqlite3{suffix}",
                    "sha256": release_operator._sha256_file(path),  # noqa: SLF001
                    "size": int(status.st_size),
                }
            )
    obsidian, _identities = release_operator._capture_obsidian_tree(  # noqa: SLF001
        release_operator._obsidian_root(config),  # noqa: SLF001
        destination=None,
    )
    engineer = release_operator._scan_engineer_artifacts(config, destination=None)  # noqa: SLF001
    # The fresh-copy API intentionally assigns a new Engineer database inode.
    # Device/inode values therefore prove local restore continuity in memory,
    # but must never make the durable rehearsal receipt nondeterministic.
    engineer = dict(engineer)
    engineer["entries"] = [
        {
            key: value
            for key, value in item.items()
            if not (item.get("path") == "store/kernel.sqlite" and key in {"device", "inode"})
        }
        for item in engineer["entries"]
    ]
    projection: dict[str, Any] = {
        "engineer": engineer,
        "obsidian": obsidian,
    }
    if include_sqlite:
        projection["sqlite"] = sqlite_files
    return _sha256(_canonical(projection))


def _engineer_authority_present(backup: release_operator.DatabaseBackup) -> bool:
    payload = backup.opaque
    if not isinstance(payload, release_operator._ExactBackupPayload):  # noqa: SLF001
        raise DRGenerationRehearsalError("dr_rehearsal_backup_identity_invalid")
    descriptor = payload.engineer
    if descriptor is None:
        raise DRGenerationRehearsalError("dr_rehearsal_four_surface_required")
    manifest = release_operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        descriptor,
        verify_sqlite_integrity=False,
    )
    return isinstance(manifest.get("engineer_command_ledger_authority"), dict)


def _four_surface_receipt_sha256(backup: release_operator.DatabaseBackup) -> str:
    surfaces = {
        "database": backup.receipt_sha256,
        "engineer": backup.engineer_receipt_sha256,
        "inbox": backup.inbox_receipt_sha256,
        "obsidian": backup.obsidian_receipt_sha256,
    }
    if any(not _is_hex64(value) for value in surfaces.values()):
        raise DRGenerationRehearsalError("dr_rehearsal_surface_receipt_invalid")
    return _sha256(_canonical(surfaces))


class _TrackingJournal:
    def __init__(self, journal: release_operator.DurableActivationJournal) -> None:
        self.journal = journal
        self.phases: list[str] = []

    def begin(
        self,
        *,
        candidate: release_operator.ReleaseIdentity,
        previous: release_operator.ReleaseIdentity,
        fallback: release_operator.ReleaseIdentity,
    ) -> None:
        self.journal.begin(candidate=candidate, previous=previous, fallback=fallback)
        self.phases.append("prepared")

    def record(self, phase: str, **kwargs: Any) -> None:
        self.journal.record(phase, **kwargs)
        self.phases.append(phase)

    def load(self) -> Mapping[str, Any]:
        return self.journal.load()

    def release_identities(
        self,
    ) -> tuple[
        release_operator.ReleaseIdentity,
        release_operator.ReleaseIdentity,
        release_operator.ReleaseIdentity,
    ]:
        return self.journal.release_identities()

    def database_backup(self) -> release_operator.DatabaseBackup | None:
        return self.journal.database_backup()


class _IsolatedActivationPort:
    """Activation port that can touch only its private scratch contour."""

    def __init__(
        self,
        config: release_operator.SystemdConfig,
        *,
        releases: Sequence[release_operator.ReleaseIdentity],
        sealed_releases: Mapping[release_operator.ReleaseIdentity, _SealedReleaseCopy],
    ) -> None:
        self.config = config
        self.releases = tuple(releases)
        self.sealed_releases = dict(sealed_releases)
        if set(self.sealed_releases) != set(self.releases):
            raise release_operator.ReleaseFailure("dr_rehearsal_release_copy_set_invalid")
        self.leases = False
        self.checkpoint: release_operator.DatabaseBackup | None = None
        self.checkpoint_digest = ""
        self.checkpoint_non_sqlite_digest = ""
        self.restored_digest = ""
        self.rollback_tree_sha256 = ""
        self.release_open_trees: list[str] = []
        self.rollback_release_receipt: dict[str, Any] | None = None
        self.database_reopen_count = 0
        self.inbox_reopen_count = 0
        self.systemctl_calls = 0
        self.network_calls = 0
        self.production_write_calls = 0

    def activation_policy_receipt(self) -> Mapping[str, str]:
        raise release_operator.ReleaseFailure("dr_rehearsal_fault_not_reached")

    def verify_release(
        self,
        release: release_operator.ReleaseIdentity,
        *,
        use_predecessor_config: bool = False,
    ) -> None:
        del use_predecessor_config
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_release_identity_invalid")
        sealed = self.sealed_releases[release]
        observed = release_operator.load_release_identity(
            sealed.root,
            expected_tree_sha256=release.tree_manifest_sha256,
        )
        if _release_identity_projection(observed) != _release_identity_projection(release):
            raise release_operator.ReleaseFailure("dr_rehearsal_release_identity_changed")

    def verify_units(self, candidate: release_operator.ReleaseIdentity) -> None:
        if candidate != self.releases[0]:
            raise release_operator.ReleaseFailure("dr_rehearsal_candidate_changed")

    def verify_active_anchor(
        self,
        previous: release_operator.ReleaseIdentity,
        candidate: release_operator.ReleaseIdentity,
    ) -> None:
        if previous != self.releases[1] or candidate != self.releases[0]:
            raise release_operator.ReleaseFailure("dr_rehearsal_anchor_identity_changed")

    def stop_bridge(self) -> None:
        return

    def stop_backend(self) -> None:
        return

    def services_inactive(self) -> bool:
        return True

    def writer_leases_held(self) -> bool:
        return self.leases

    def acquire_writer_leases(self) -> None:
        if self.leases:
            return
        self.leases = True

    def release_writer_leases(self) -> None:
        self.leases = False

    def validate_staged_config_transition(self, *_args: Any) -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_staged_transition_forbidden")

    def activate_staged_config_transition(self, *_args: Any) -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_staged_transition_forbidden")

    def select_predecessor_config_transition(self, *_args: Any) -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_staged_transition_forbidden")

    def validate_engineer_recovery_contour(
        self,
        releases: Sequence[release_operator.ReleaseIdentity],
    ) -> None:
        if tuple(releases) != self.releases or any(
            release.root == self.config.friday_home or release.root.is_relative_to(self.config.friday_home)
            for release in releases
        ):
            raise release_operator.ReleaseFailure("dr_rehearsal_release_contour_invalid")

    def engineer_store_lifecycle_required(self) -> bool:
        manifest = release_operator._scan_engineer_artifacts(  # noqa: SLF001
            self.config,
            destination=None,
        )
        return bool(manifest["store_present"] or manifest["key_present"])

    def engineer_store_lifecycle_provisioned(self) -> bool:
        _store, _key, state = release_operator._engineer_artifact_paths(self.config)  # noqa: SLF001
        return (state / "engineer-command-store.anchor.json").exists()

    def provision_engineer_store(self, release: release_operator.ReleaseIdentity) -> None:
        del release
        raise release_operator.ReleaseFailure("dr_rehearsal_provision_boundary_crossed")

    def backup_database(
        self,
        release: release_operator.ReleaseIdentity,
    ) -> release_operator.DatabaseBackup:
        if release != self.releases[0] or not self.leases:
            raise release_operator.ReleaseFailure("dr_rehearsal_checkpoint_not_authorized")
        sealed = self.sealed_releases[release]
        checkpoint = release_operator._exact_sqlite_backup(  # noqa: SLF001
            self.config,
            require_engineer_authority=True,
            engineer_authority_snapshot=lambda: _run_release_engineer_authority(
                sealed,
                self.config,
                action="snapshot",
            ),
            engineer_authority_attest=lambda digest: _run_release_engineer_authority(
                sealed,
                self.config,
                action="attest",
                database_sha256=digest,
            ),
        )
        self.checkpoint = checkpoint
        self.checkpoint_digest = _surface_digest(self.config)
        self.checkpoint_non_sqlite_digest = _surface_digest(
            self.config,
            include_sqlite=False,
        )
        return checkpoint

    @staticmethod
    def _mutate_sqlite(path: Path, table: str) -> None:
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute(f"CREATE TABLE {table}(value INTEGER NOT NULL)")
            connection.execute(f"INSERT INTO {table}(value) VALUES (1)")
        finally:
            connection.close()

    def offline_migrate(
        self,
        release: release_operator.ReleaseIdentity,
        backup: release_operator.DatabaseBackup,
    ) -> None:
        if release != self.releases[0] or backup != self.checkpoint or not self.leases:
            raise release_operator.ReleaseFailure("dr_rehearsal_migration_not_authorized")
        receipt = _run_release_store(self.sealed_releases[release], self.config)
        if receipt.get("main_schema") != self.checkpoint.schema_version:
            raise release_operator.ReleaseFailure("dr_rehearsal_candidate_reopen_mismatch")
        self.release_open_trees.append(release.tree_manifest_sha256)
        self.database_reopen_count += 1
        self.inbox_reopen_count += 1
        self._mutate_sqlite(self.config.database, "friday_dr_rehearsal_main_fault")
        self._mutate_sqlite(self.config.inbox_database, "friday_dr_rehearsal_inbox_fault")
        obsidian = release_operator._obsidian_root(self.config)  # noqa: SLF001
        obsidian.mkdir(mode=0o700, exist_ok=True)
        (obsidian / ".friday-dr-rehearsal-fault").write_bytes(b"fault\n")
        (obsidian / ".friday-dr-rehearsal-fault").chmod(0o600)
        store, _key, _state = release_operator._engineer_artifact_paths(self.config)  # noqa: SLF001
        store.mkdir(parents=True, mode=0o700, exist_ok=True)
        store.chmod(0o700)
        (store / ".friday-dr-rehearsal-fault").write_bytes(b"fault\n")
        (store / ".friday-dr-rehearsal-fault").chmod(0o600)

    def repair_file_aliases(
        self,
        release: release_operator.ReleaseIdentity,
        backup: release_operator.DatabaseBackup,
    ) -> Mapping[str, Any]:
        if release != self.releases[0] or backup != self.checkpoint:
            raise release_operator.ReleaseFailure("dr_rehearsal_fault_boundary_changed")
        raise _InjectedRehearsalFault("after_migration_before_provision")

    def switch_anchor(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")
        self.rollback_tree_sha256 = release.tree_manifest_sha256

    def start_backend(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")

    def accept_backend(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases or release.tree_manifest_sha256 != self.rollback_tree_sha256:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")
        self.rollback_release_receipt = _run_release_store(
            self.sealed_releases[release],
            self.config,
        )
        self.release_open_trees.append(release.tree_manifest_sha256)
        self.database_reopen_count += 1
        self.inbox_reopen_count += 1

    def start_bridge(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")

    def accept_bridge(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")

    def restore_database(
        self,
        backup: release_operator.DatabaseBackup,
        release: release_operator.ReleaseIdentity,
    ) -> None:
        if backup != self.checkpoint or release != self.releases[0] or not self.leases:
            raise release_operator.ReleaseFailure("dr_rehearsal_restore_not_authorized")
        sealed = self.sealed_releases[release]
        release_operator._restore_exact_sqlite_backup(  # noqa: SLF001
            self.config,
            backup,
            require_engineer_authority=True,
            engineer_authority_verify=lambda evidence, digest: _run_release_engineer_authority(
                sealed,
                self.config,
                action="verify",
                database_sha256=digest,
                evidence=evidence,
            ),
        )
        self.restored_digest = _surface_digest(self.config)
        if self.restored_digest != self.checkpoint_digest:
            raise release_operator.ReleaseFailure("dr_rehearsal_four_surface_restore_mismatch")


def _run_isolated_rehearsal(
    material: dr_auth.AuthenticatedDRMaterial,
    scratch: Path,
) -> _RunResult:
    payload = material.backup.opaque
    if not isinstance(payload, release_operator._ExactBackupPayload):  # noqa: SLF001
        raise DRGenerationRehearsalError("dr_rehearsal_backup_identity_invalid")
    obsidian = payload.obsidian
    if obsidian is None or payload.engineer is None:
        raise DRGenerationRehearsalError("dr_rehearsal_four_surface_required")
    releases = (
        material.activation_candidate,
        material.activation_previous,
        material.restore_fallback,
    )
    sealed_releases = _materialize_exact_releases(releases, scratch / "sealed")
    config = _scratch_config(
        _private_directory(scratch / "work"),
        obsidian_present=obsidian.present,
    )
    materialized = release_operator.materialize_exact_backup_into_fresh_contour(
        config,
        material.backup,
    )
    source_engineer_manifest = release_operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        payload.engineer,
        verify_sqlite_integrity=False,
    )
    source_paths = {str(item["path"]) for item in source_engineer_manifest["entries"]}
    lifecycle_backed_kernel = {
        "store/kernel.sqlite",
        "state/engineer-command-store.anchor.json",
    }.issubset(source_paths)
    if materialized.engineer_fresh_identity_assigned is not lifecycle_backed_kernel:
        raise DRGenerationRehearsalError("dr_rehearsal_engineer_fresh_identity_mismatch")
    port = _IsolatedActivationPort(
        config,
        releases=releases,
        sealed_releases=sealed_releases,
    )
    journal = _TrackingJournal(
        release_operator.DurableActivationJournal(
            config.state_dir / "immutable-release-activation.v1.json",
            backup_root=config.backup_dir,
            config_identity_sha256=_sha256(b"isolated-dr-rehearsal-config-v1"),
            memory_vault_mode="disabled",
            obsidian_mode=config.obsidian_mode,
            obsidian_root_sha256=_sha256(str(config.obsidian_root).encode("utf-8")),
        )
    )
    try:
        release_operator.activate_release(
            port,
            journal,
            candidate=material.activation_candidate,
            previous=material.activation_previous,
            schema_capable_fallback=material.restore_fallback,
        )
    except release_operator.ReleaseFailure as exc:
        if str(exc) != "activation_failed_rolled_back" or not isinstance(
            exc.__cause__,
            _InjectedRehearsalFault,
        ):
            raise DRGenerationRehearsalError("dr_rehearsal_activation_failed") from exc
    else:  # pragma: no cover - the injected boundary is unconditional.
        raise DRGenerationRehearsalError("dr_rehearsal_fault_not_observed")
    state = dict(journal.load())
    if (
        state.get("phase") != "rolled_back"
        or "rollback_restore_attempted" not in journal.phases
        or not journal.phases
        or journal.phases[-1] != "rolled_back"
        or port.checkpoint is None
        or not port.checkpoint_digest
        or port.restored_digest != port.checkpoint_digest
        or not port.rollback_tree_sha256
        or port.systemctl_calls != 0
        or port.network_calls != 0
        or port.production_write_calls != 0
        or port.release_open_trees
        != [
            material.activation_candidate.tree_manifest_sha256,
            port.rollback_tree_sha256,
        ]
        or port.rollback_release_receipt is None
        or port.rollback_release_receipt.get("main_schema") != materialized.schema_version
        or port.rollback_release_receipt.get("main_integrity") != "ok"
        or port.rollback_release_receipt.get("main_fk") != 0
        or port.rollback_release_receipt.get("inbox_integrity") != "ok"
        or port.rollback_release_receipt.get("inbox_fk") != 0
        or port.database_reopen_count != 2
        or port.inbox_reopen_count != 2
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_rollback_not_exact")
    checkpoint_authority = _engineer_authority_present(port.checkpoint)
    if (
        checkpoint_authority is not lifecycle_backed_kernel
        or _surface_digest(config, include_sqlite=False) != port.checkpoint_non_sqlite_digest
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_second_reopen_mismatch")
    return _RunResult(
        schema_version=int(port.rollback_release_receipt["main_schema"]),
        rollback_tree_sha256=port.rollback_tree_sha256,
        # The scratch anchor necessarily binds a newly assigned inode.  Keep
        # that exact digest in-memory for rollback comparison, while the
        # durable receipt binds only the authenticated source surface receipts.
        four_surface_sha256=_four_surface_receipt_sha256(material.backup),
        engineer_authority_present=checkpoint_authority,
        database_reopen_count=port.database_reopen_count,
        inbox_reopen_count=port.inbox_reopen_count,
    )


def _candidate_sha256(candidate: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dr_index.normalize_generation_candidate(candidate)))


def _authentication_reference(receipt: Mapping[str, Any]) -> str:
    supplied = receipt.get("receipt_sha256")
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not isinstance(supplied, str) or len(supplied) != 64 or supplied != _sha256(_canonical(core)):
        raise DRGenerationRehearsalError("dr_rehearsal_authentication_receipt_invalid")
    return supplied


def _bind_pending(
    pending: dr_index.PendingDRGenerationIdentity,
    material: dr_auth.AuthenticatedDRMaterial,
) -> None:
    authenticated = material.authenticated
    if (
        pending.candidate != authenticated.candidate
        or pending.candidate_sha256 != _candidate_sha256(authenticated.candidate)
        or pending.authentication_receipt != authenticated.authentication_receipt
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_pending_identity_mismatch")


def _source_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "activation_journal_file_sha256",
        "activation_journal_sha256",
        "activation_receipt_file_sha256",
        "activation_receipt_sha256",
        "backup_manifest_sha256",
        "restore_operator_sha256",
        "surface_receipts",
    )
    projection = {key: receipt.get(key) for key in keys}
    if any(value is None for value in projection.values()) or not isinstance(
        projection["surface_receipts"],
        dict,
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_authentication_receipt_invalid")
    return projection


def _receipt(
    *,
    pending: dr_index.PendingDRGenerationIdentity,
    material: dr_auth.AuthenticatedDRMaterial,
    result: _RunResult,
) -> dict[str, Any]:
    authentication = material.authenticated.authentication_receipt
    restore = pending.candidate["restore_release"]
    restore_identity = {
        key: restore[key]
        for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")
    }
    core: dict[str, Any] = {
        "authentication_receipt_sha256": _authentication_reference(authentication),
        "candidate_sha256": pending.candidate_sha256,
        "check_count": len(_CHECKS),
        "checkset_sha256": _sha256(_canonical(_CHECKS)),
        "database_foreign_keys_clear": True,
        "database_integrity_clear": True,
        "database_reopen_count": result.database_reopen_count,
        "database_schema": result.schema_version,
        "engineer_authority_present": result.engineer_authority_present,
        "engineer_exact": True,
        "fault_boundary": "after_migration_before_provision_or_network",
        "four_surface_exact": True,
        "four_surface_sha256": result.four_surface_sha256,
        "index_journal_sha256": pending.authenticated_journal_sha256,
        "index_revision": pending.index_revision,
        "index_transaction_id": pending.index_transaction_id,
        "inbox_foreign_keys_clear": True,
        "inbox_integrity_clear": True,
        "inbox_reopen_count": result.inbox_reopen_count,
        "network_call_count": 0,
        "obsidian_exact": True,
        "production_surface_write_count": 0,
        "restore_release": restore_identity,
        "rollback_restore_observed": True,
        "rollback_tree_sha256": result.rollback_tree_sha256,
        "rolled_back": True,
        "schema": REHEARSAL_RECEIPT_SCHEMA,
        "scratch_removed": True,
        "source": _source_projection(authentication),
        "status": "rehearsed",
        "systemctl_call_count": 0,
    }
    return {**core, "receipt_sha256": _sha256(_canonical(core))}


def _validate_existing_receipt(
    receipt: Mapping[str, Any],
    *,
    pending: dr_index.PendingDRGenerationIdentity,
    material: dr_auth.AuthenticatedDRMaterial,
) -> dict[str, Any]:
    supplied = receipt.get("receipt_sha256")
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    candidate = material.authenticated.candidate
    restore = candidate["restore_release"]
    allowed_rollbacks = {
        material.activation_previous.tree_manifest_sha256,
        material.restore_fallback.tree_manifest_sha256,
    }
    if (
        set(receipt) != _RECEIPT_KEYS
        or supplied != _sha256(_canonical(core))
        or receipt.get("schema") != REHEARSAL_RECEIPT_SCHEMA
        or receipt.get("status") != "rehearsed"
        or receipt.get("candidate_sha256") != pending.candidate_sha256
        or receipt.get("authentication_receipt_sha256")
        != _authentication_reference(material.authenticated.authentication_receipt)
        or receipt.get("index_transaction_id") != pending.index_transaction_id
        or receipt.get("index_revision") != pending.index_revision - 1
        or receipt.get("index_journal_sha256") != pending.authenticated_journal_sha256
        or receipt.get("check_count") != len(_CHECKS)
        or receipt.get("checkset_sha256") != _sha256(_canonical(_CHECKS))
        or receipt.get("database_schema") != material.backup.schema_version
        or receipt.get("database_reopen_count") != 2
        or receipt.get("inbox_reopen_count") != 2
        or receipt.get("database_integrity_clear") is not True
        or receipt.get("database_foreign_keys_clear") is not True
        or receipt.get("inbox_integrity_clear") is not True
        or receipt.get("inbox_foreign_keys_clear") is not True
        or receipt.get("fault_boundary") != "after_migration_before_provision_or_network"
        or not _is_hex64(receipt.get("four_surface_sha256"))
        or receipt.get("restore_release")
        != {
            key: restore[key]
            for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")
        }
        or receipt.get("source") != _source_projection(material.authenticated.authentication_receipt)
        or receipt.get("rollback_tree_sha256") not in allowed_rollbacks
        or receipt.get("rolled_back") is not True
        or receipt.get("rollback_restore_observed") is not True
        or receipt.get("four_surface_exact") is not True
        or receipt.get("obsidian_exact") is not True
        or receipt.get("engineer_exact") is not True
        or receipt.get("scratch_removed") is not True
        or receipt.get("systemctl_call_count") != 0
        or receipt.get("network_call_count") != 0
        or receipt.get("production_surface_write_count") != 0
        or receipt.get("engineer_authority_present") != _engineer_authority_present(material.backup)
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_existing_receipt_invalid")
    return dict(receipt)


def rehearse_authenticated_generation(*, activation_receipt: Path) -> dict[str, Any]:
    """Rehearse the exact authenticated pending generation and CAS only that state."""

    friday_home = _canonical_friday_home()
    state_directory = friday_home / "data/state"
    backup_root = friday_home / "data/backups"
    activation_journal = state_directory / "immutable-release-activation.v1.json"
    index = dr_index.DurableDRGenerationIndex(state_directory)
    try:
        with release_operator.OperatorTransactionLock(state_directory / "immutable-release-operator.v1.lock"):
            state = index.load()
            pending = index.pending_generation_identity(
                expected_journal_sha256=str(state["journal_sha256"]),
            )
            material = dr_auth._authenticate_material_locked(  # noqa: SLF001
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
            )
            _bind_pending(pending, material)
            if pending.index_phase == "rehearsed":
                if pending.rehearsal_receipt is None:
                    raise DRGenerationRehearsalError("dr_rehearsal_existing_receipt_missing")
                return _validate_existing_receipt(
                    pending.rehearsal_receipt,
                    pending=pending,
                    material=material,
                )

            scratch = _new_scratch(
                transaction_id=pending.index_transaction_id,
                candidate_sha256=pending.candidate_sha256,
            )
            run_error: BaseException | None = None
            result: _RunResult | None = None
            try:
                result = _run_isolated_rehearsal(material, scratch.root)
            except BaseException as exc:  # cleanup must precede any error propagation.
                run_error = exc
            try:
                _remove_current_scratch(scratch)
            except BaseException as cleanup_error:
                raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed") from cleanup_error
            if run_error is not None:
                raise DRGenerationRehearsalError("dr_rehearsal_isolated_run_failed") from run_error
            assert result is not None

            material_after = dr_auth._authenticate_material_locked(  # noqa: SLF001
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
            )
            if material_after != material:
                raise DRGenerationRehearsalError("dr_rehearsal_source_changed")
            pending_after = index.pending_generation_identity(
                expected_journal_sha256=pending.index_journal_sha256,
            )
            if pending_after != pending:
                raise DRGenerationRehearsalError("dr_rehearsal_index_changed")
            body = _receipt(pending=pending, material=material_after, result=result)
            rehearsed = index.record_rehearsed(
                receipt=body,
                expected_journal_sha256=pending.index_journal_sha256,
            )
            if rehearsed["phase"] != "rehearsed" or rehearsed["revision"] != pending.index_revision + 1:
                raise DRGenerationRehearsalError("dr_rehearsal_index_publication_failed")
            return body
    except DRGenerationRehearsalError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
    ) as exc:
        raise DRGenerationRehearsalError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = rehearse_authenticated_generation(activation_receipt=args.activation_receipt)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DRGenerationRehearsalError",
    "REHEARSAL_RECEIPT_SCHEMA",
    "rehearse_authenticated_generation",
]
