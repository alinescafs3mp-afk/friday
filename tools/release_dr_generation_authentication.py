#!/usr/bin/env python3
"""Authenticate one terminal activation backup as an exact DR candidate.

This module does not modify release, backup, journal, or index content.  It
derives every candidate field from the durable activation journal and sealed
artifacts while holding the repository-wide release-operator lock (whose
synchronization file may be created).  Its result is an observation, never DR
or deletion authority: a sealed controller must retain the same lock while it
pins namespaces, binds production configuration, durably publishes receipt
bodies, enrolls the index CAS, and revalidates the inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_index as dr_index

AUTHENTICATION_RECEIPT_SCHEMA = "friday.immutable-release-dr-authentication-receipt.v1"
MAX_ACTIVATION_RECEIPT_BYTES = 1 << 20

_HEX64 = frozenset("0123456789abcdef")


class DRGenerationAuthenticationError(RuntimeError):
    """A closed terminal-activation authentication failure."""


@dataclass(frozen=True)
class AuthenticatedDRCandidate:
    candidate: dict[str, Any]
    authentication_receipt: dict[str, Any]


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
        raise DRGenerationAuthenticationError("dr_authentication_noncanonical") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
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


def _hex64(value: object, *, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX64:
        raise DRGenerationAuthenticationError(code)
    return value


def _stable_private_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    private: bool = True,
) -> tuple[bytes, os.stat_result]:
    lexical = Path(os.path.abspath(path))
    if not path.is_absolute() or lexical != path or any(char in str(path) for char in "\x00\r\n"):
        raise DRGenerationAuthenticationError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(path, follow_symlinks=False)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (private and stat.S_IMODE(opened.st_mode) & 0o077)
                or not 0 < opened.st_size <= maximum
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise DRGenerationAuthenticationError(code)
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining == 0 and os.read(descriptor, 1):
                raise DRGenerationAuthenticationError(code)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise DRGenerationAuthenticationError(code) from exc

    if _file_identity(before) != _file_identity(after_open) or _file_identity(before) != _file_identity(after):
        raise DRGenerationAuthenticationError(code)
    return b"".join(chunks), opened


def _json(raw: bytes, *, code: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise DRGenerationAuthenticationError(code)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise DRGenerationAuthenticationError(code) from exc
    if not isinstance(value, dict):
        raise DRGenerationAuthenticationError(code)
    return value


def _release_record(
    release: release_operator.ReleaseIdentity,
) -> dict[str, Any]:
    metadata_path = release.root / "artifacts/immutable-release.json"
    raw, _status = _stable_private_file(
        metadata_path,
        maximum=1 << 20,
        code="dr_restore_release_metadata_invalid",
    )
    metadata = _json(raw, code="dr_restore_release_metadata_invalid")
    wheel_sha256 = _hex64(
        metadata.get("wheel_sha256"),
        code="dr_restore_release_metadata_invalid",
    )
    if (
        metadata.get("commit") != release.commit
        or metadata.get("version") != release.version
        or metadata.get("max_schema") != release.max_schema
    ):
        raise DRGenerationAuthenticationError("dr_restore_release_metadata_invalid")
    return {
        "commit": release.commit,
        "max_schema": release.max_schema,
        "root": str(release.root),
        "tree_manifest_sha256": release.tree_manifest_sha256,
        "version": release.version,
        "wheel_sha256": wheel_sha256,
    }


def _validate_activation_receipt(
    payload: dict[str, Any],
    *,
    state: dict[str, Any],
    backup: release_operator.DatabaseBackup,
) -> str:
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
    receipt_sha256 = _hex64(payload.get("receipt_sha256"), code="dr_activation_receipt_invalid")
    core = {key: value for key, value in payload.items() if key not in {"operator_schema", "receipt_sha256"}}
    if (
        set(payload) != expected
        or payload.get("schema") != release_operator.ACTIVATION_RECEIPT_SCHEMA
        or payload.get("operator_schema") != release_operator.OPERATOR_SCHEMA
        or payload.get("status") != "clear"
        or payload.get("backend_accepted") is not True
        or payload.get("bridge_accepted") is not True
        or payload.get("candidate_tree_sha256") != state["candidate"]["tree_manifest_sha256"]
        or payload.get("database_schema_before") != backup.schema_version
        or payload.get("backup_receipt_sha256") != backup.receipt_sha256
        or payload.get("inbox_backup_receipt_sha256") != backup.inbox_receipt_sha256
        or payload.get("obsidian_backup_receipt_sha256") != backup.obsidian_receipt_sha256
        or payload.get("engineer_backup_receipt_sha256") != backup.engineer_receipt_sha256
        or state.get("terminal_receipt_sha256") != receipt_sha256
        or receipt_sha256 != _sha256(_canonical(core))
        or not isinstance(payload.get("runtime_policy"), dict)
    ):
        raise DRGenerationAuthenticationError("dr_activation_receipt_invalid")
    try:
        release_operator._validated_alias_repair_receipt(payload["alias_repair"])  # noqa: SLF001
    except (KeyError, release_operator.ReleaseFailure) as exc:
        raise DRGenerationAuthenticationError("dr_activation_receipt_invalid") from exc
    return receipt_sha256


def _directory_identity(path: Path) -> tuple[int, ...]:
    try:
        status = os.stat(path, follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DRGenerationAuthenticationError("dr_backup_directory_invalid") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise DRGenerationAuthenticationError("dr_backup_directory_invalid")
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_nlink),
        int(status.st_uid),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _authenticate_locked(
    *,
    activation_journal: Path,
    activation_receipt: Path,
    backup_root: Path,
) -> AuthenticatedDRCandidate:
    state_directory = activation_journal.parent
    state_directory_identity = _directory_identity(state_directory)
    backup_root_identity = _directory_identity(backup_root)
    journal_raw_before, _journal_status = _stable_private_file(
        activation_journal,
        maximum=1 << 20,
        code="dr_activation_journal_invalid",
    )
    journal_file_payload = _json(
        journal_raw_before,
        code="dr_activation_journal_invalid",
    )
    activation_journal_sha256 = _hex64(
        journal_file_payload.get("journal_sha256"),
        code="dr_activation_journal_invalid",
    )
    journal_core = {
        key: value for key, value in journal_file_payload.items() if key != "journal_sha256"
    }
    if activation_journal_sha256 != _sha256(_canonical(journal_core)):
        raise DRGenerationAuthenticationError("dr_activation_journal_invalid")
    journal = release_operator.DurableActivationJournal(
        activation_journal,
        backup_root=backup_root,
        config_identity_sha256=None,
    )
    state = dict(journal.load())
    if state != journal_core:
        raise DRGenerationAuthenticationError("dr_activation_journal_changed")
    if state.get("phase") != "clear":
        raise DRGenerationAuthenticationError("dr_activation_not_clear")
    backup = journal.database_backup(verify_engineer_sqlite_integrity=False)
    if backup is None:
        raise DRGenerationAuthenticationError("dr_activation_backup_missing")
    _candidate, _previous, fallback = journal.release_identities()
    if fallback.max_schema < backup.schema_version:
        raise DRGenerationAuthenticationError("dr_restore_release_schema_incapable")
    restore_release = _release_record(fallback)
    payload = backup.opaque
    directory = getattr(payload, "directory", None)
    if (
        not isinstance(directory, Path)
        or getattr(payload, "obsidian", None) is None
        or getattr(payload, "engineer", None) is None
    ):
        raise DRGenerationAuthenticationError("dr_backup_identity_invalid")
    directory_identity_before = _directory_identity(directory)

    activation_raw, _activation_status = _stable_private_file(
        activation_receipt,
        maximum=MAX_ACTIVATION_RECEIPT_BYTES,
        code="dr_activation_receipt_invalid",
        private=False,
    )
    activation_payload = _json(activation_raw, code="dr_activation_receipt_invalid")
    activation_receipt_sha256 = _validate_activation_receipt(
        activation_payload,
        state=state,
        backup=backup,
    )
    raw_backup = state.get("backup")
    exact_backup_keys = {
        "directory",
        "engineer",
        "engineer_receipt_sha256",
        "files",
        "inbox_receipt_sha256",
        "obsidian",
        "obsidian_receipt_sha256",
        "receipt_sha256",
        "schema_version",
    }
    if not isinstance(raw_backup, dict) or set(raw_backup) != exact_backup_keys:
        raise DRGenerationAuthenticationError("dr_backup_identity_invalid")
    candidate = dr_index.normalize_generation_candidate(
        {
            "backup_directory": str(directory),
            "backup_record_sha256": _sha256(_canonical(raw_backup)),
            "database_receipt_sha256": backup.receipt_sha256,
            "engineer_receipt_sha256": backup.engineer_receipt_sha256,
            "inbox_receipt_sha256": backup.inbox_receipt_sha256,
            "obsidian_receipt_sha256": backup.obsidian_receipt_sha256,
            "restore_release": restore_release,
            "schema": dr_index.GENERATION_CANDIDATE_SCHEMA,
            "source_kind": "terminal_activation",
            "source_receipt_sha256": activation_receipt_sha256,
            "source_transaction_id": state["transaction_id"],
        }
    )
    candidate_sha256 = _sha256(_canonical(candidate))
    backup_manifest = directory / "manifest.json"
    backup_manifest_raw, manifest_status = _stable_private_file(
        backup_manifest,
        maximum=1 << 20,
        code="dr_backup_manifest_invalid",
    )
    operator_path = fallback.root / "artifacts/immutable_release_operator.py"
    operator_raw, operator_status = _stable_private_file(
        operator_path,
        maximum=4 << 20,
        code="dr_restore_operator_invalid",
    )
    receipt_core: dict[str, Any] = {
        "activation_journal_file_sha256": _sha256(journal_raw_before),
        "activation_journal_sha256": activation_journal_sha256,
        "activation_receipt_file_sha256": _sha256(activation_raw),
        "activation_receipt_sha256": activation_receipt_sha256,
        "backup_directory": {
            "device": directory_identity_before[0],
            "inode": directory_identity_before[1],
            "path": str(directory),
        },
        "backup_manifest_sha256": _sha256(backup_manifest_raw),
        "candidate_sha256": candidate_sha256,
        "restore_operator_sha256": _sha256(operator_raw),
        "schema": AUTHENTICATION_RECEIPT_SCHEMA,
        "status": "authenticated",
        "surface_receipts": {
            "database": backup.receipt_sha256,
            "engineer": backup.engineer_receipt_sha256,
            "inbox": backup.inbox_receipt_sha256,
            "obsidian": backup.obsidian_receipt_sha256,
        },
    }
    journal_raw_after, journal_status_after = _stable_private_file(
        activation_journal,
        maximum=1 << 20,
        code="dr_activation_journal_invalid",
    )
    activation_raw_after, activation_status_after = _stable_private_file(
        activation_receipt,
        maximum=MAX_ACTIVATION_RECEIPT_BYTES,
        code="dr_activation_receipt_invalid",
        private=False,
    )
    backup_after = journal.database_backup(verify_engineer_sqlite_integrity=False)
    _candidate_after, _previous_after, fallback_after = journal.release_identities()
    restore_release_after = _release_record(fallback_after)
    backup_manifest_after, manifest_status_after = _stable_private_file(
        backup_manifest,
        maximum=1 << 20,
        code="dr_backup_manifest_invalid",
    )
    operator_after, operator_status_after = _stable_private_file(
        operator_path,
        maximum=4 << 20,
        code="dr_restore_operator_invalid",
    )
    if (
        backup_after is None
        or journal_raw_after != journal_raw_before
        or _file_identity(journal_status_after) != _file_identity(_journal_status)
        or activation_raw_after != activation_raw
        or _file_identity(activation_status_after) != _file_identity(_activation_status)
        or _directory_identity(state_directory) != state_directory_identity
        or _directory_identity(backup_root) != backup_root_identity
        or _directory_identity(directory) != directory_identity_before
        or backup_after != backup
        or fallback_after != fallback
        or restore_release_after != restore_release
        or backup_manifest_after != backup_manifest_raw
        or _file_identity(manifest_status_after) != _file_identity(manifest_status)
        or operator_after != operator_raw
        or _file_identity(operator_status_after) != _file_identity(operator_status)
    ):
        raise DRGenerationAuthenticationError("dr_authentication_input_changed")
    authentication_receipt = {
        **receipt_core,
        "receipt_sha256": _sha256(_canonical(receipt_core)),
    }
    return AuthenticatedDRCandidate(candidate, authentication_receipt)


def authenticate_terminal_activation_backup(
    *,
    state_directory: Path,
    activation_receipt: Path,
    backup_root: Path,
) -> AuthenticatedDRCandidate:
    """Observe one exact candidate; callers must not treat it as enrollment authority."""

    activation_journal = state_directory / "immutable-release-activation.v1.json"
    with release_operator.OperatorTransactionLock(state_directory / "immutable-release-operator.v1.lock"):
        return _authenticate_locked(
            activation_journal=activation_journal,
            activation_receipt=activation_receipt,
            backup_root=backup_root,
        )


__all__ = [
    "AUTHENTICATION_RECEIPT_SCHEMA",
    "AuthenticatedDRCandidate",
    "DRGenerationAuthenticationError",
    "authenticate_terminal_activation_backup",
]
