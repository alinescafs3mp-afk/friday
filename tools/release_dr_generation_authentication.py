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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_index as dr_index

AUTHENTICATION_RECEIPT_SCHEMA = dr_index.AUTHENTICATION_RECEIPT_SCHEMA
MAX_ACTIVATION_RECEIPT_BYTES = 1 << 20

_HEX64 = frozenset("0123456789abcdef")


class DRGenerationAuthenticationError(RuntimeError):
    """A closed terminal-activation authentication failure."""


@dataclass(frozen=True)
class AuthenticatedDRCandidate:
    candidate: dict[str, Any]
    authentication_receipt: dict[str, Any]


@dataclass(frozen=True)
class AuthenticatedDRMaterial:
    """Exact four-surface material and releases from one locked source epoch."""

    authenticated: AuthenticatedDRCandidate
    backup: release_operator.DatabaseBackup
    activation_candidate: release_operator.ReleaseIdentity
    activation_previous: release_operator.ReleaseIdentity
    restore_fallback: release_operator.ReleaseIdentity


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
    allow_empty: bool = False,
) -> tuple[bytes, os.stat_result]:
    lexical = Path(os.path.abspath(path))
    if not path.is_absolute() or lexical != path or any(char in str(path) for char in "\x00\r\n"):
        raise DRGenerationAuthenticationError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise DRGenerationAuthenticationError(code)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (private and stat.S_IMODE(opened.st_mode) & 0o077)
                or not (0 <= opened.st_size <= maximum if allow_empty else 0 < opened.st_size <= maximum)
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

    if _file_identity(before) != _file_identity(after_open) or _file_identity(before) != _file_identity(
        after
    ):
        raise DRGenerationAuthenticationError(code)
    return b"".join(chunks), opened


def _stable_private_file_digest(
    path: Path,
    *,
    maximum: int,
    code: str,
    allow_empty: bool = False,
) -> tuple[str, int, os.stat_result]:
    """Stream one stable owner-private file without materializing it in RAM."""

    lexical = Path(os.path.abspath(path))
    if not path.is_absolute() or lexical != path or any(char in str(path) for char in "\x00\r\n"):
        raise DRGenerationAuthenticationError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise DRGenerationAuthenticationError(code)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) & 0o077
                or not (0 <= opened.st_size <= maximum if allow_empty else 0 < opened.st_size <= maximum)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise DRGenerationAuthenticationError(code)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise DRGenerationAuthenticationError(code)
                digest.update(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise DRGenerationAuthenticationError(code) from exc
    if (
        size != int(opened.st_size)
        or _file_identity(before) != _file_identity(after_open)
        or _file_identity(before) != _file_identity(after)
    ):
        raise DRGenerationAuthenticationError(code)
    return digest.hexdigest(), size, opened


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
    journal_core = {key: value for key, value in journal_file_payload.items() if key != "journal_sha256"}
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
    _candidate, previous, fallback = journal.release_identities()
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
            "allowed_rollback_tree_sha256s": sorted(
                {
                    previous.tree_manifest_sha256,
                    fallback.tree_manifest_sha256,
                }
            ),
            "backup_directory": str(directory),
            "backup_record_sha256": _sha256(_canonical(raw_backup)),
            "database_receipt_sha256": backup.receipt_sha256,
            "database_schema": backup.schema_version,
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
        "allowed_rollback_tree_sha256s": list(candidate["allowed_rollback_tree_sha256s"]),
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
        "database_schema": candidate["database_schema"],
        "restore_operator_sha256": _sha256(operator_raw),
        "schema": AUTHENTICATION_RECEIPT_SCHEMA,
        "source_transaction_id": candidate["source_transaction_id"],
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
    _candidate_after, previous_after, fallback_after = journal.release_identities()
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
        or previous_after != previous
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


def reauthenticate_generation_candidate(
    *,
    candidate: Mapping[str, Any],
    authentication_receipt: Mapping[str, Any],
) -> release_operator.DatabaseBackup:
    """Reauthenticate every retained backup byte before granting retention authority."""

    try:
        normalized = dr_index.normalize_generation_candidate(candidate)
        _reference, _raw, receipt = dr_index.validate_authentication_receipt(
            authentication_receipt,
            candidate=normalized,
        )
        directory = Path(normalized["backup_directory"])
        directory_before = _directory_identity(directory)
        expected_directory = receipt["backup_directory"]
        if (
            expected_directory["device"] != directory_before[0]
            or expected_directory["inode"] != directory_before[1]
        ):
            raise DRGenerationAuthenticationError("dr_retained_backup_identity_mismatch")

        manifest_raw, manifest_status = _stable_private_file(
            directory / "manifest.json",
            maximum=1 << 20,
            code="dr_retained_backup_manifest_invalid",
        )
        if _sha256(manifest_raw) != receipt["backup_manifest_sha256"]:
            raise DRGenerationAuthenticationError("dr_retained_backup_manifest_mismatch")
        manifest = _json(manifest_raw, code="dr_retained_backup_manifest_invalid")
        if (
            set(manifest) != {"database_schema", "files", "schema"}
            or manifest.get("schema") != "friday.immutable-cutover-exact-backup.v1"
        ):
            raise DRGenerationAuthenticationError("dr_retained_backup_manifest_invalid")
        schema_version = manifest.get("database_schema")
        files_raw = manifest.get("files")
        if type(schema_version) is not int or schema_version <= 0 or not isinstance(files_raw, list):
            raise DRGenerationAuthenticationError("dr_retained_backup_manifest_invalid")
        if schema_version != normalized["database_schema"]:
            raise DRGenerationAuthenticationError("dr_retained_backup_schema_mismatch")
        allowed = {
            "database.sqlite3",
            "database.sqlite3-wal",
            "inbox.sqlite3",
            "inbox.sqlite3-wal",
        }
        files: list[tuple[str, str, int]] = []
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        file_statuses: dict[str, tuple[int, ...]] = {}
        for item in files_raw:
            if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
                raise DRGenerationAuthenticationError("dr_retained_backup_manifest_invalid")
            name = item.get("name")
            size = item.get("size")
            digest = _hex64(item.get("sha256"), code="dr_retained_backup_manifest_invalid")
            if (
                not isinstance(name, str)
                or name not in allowed
                or name in seen
                or type(size) is not int
                or size < 0
            ):
                raise DRGenerationAuthenticationError("dr_retained_backup_manifest_invalid")
            observed_digest, observed_size, file_status = _stable_private_file_digest(
                directory / name,
                maximum=1 << 40,
                code="dr_retained_backup_file_invalid",
                allow_empty=True,
            )
            if observed_size != size or observed_digest != digest:
                raise DRGenerationAuthenticationError("dr_retained_backup_file_changed")
            seen.add(name)
            files.append((name, digest, size))
            entries.append({"name": name, "sha256": digest, "size": size})
            file_statuses[name] = _file_identity(file_status)
        entries.sort(key=lambda item: str(item["name"]))
        files.sort()
        if manifest["files"] != entries or not {"database.sqlite3", "inbox.sqlite3"}.issubset(seen):
            raise DRGenerationAuthenticationError("dr_retained_backup_manifest_invalid")
        database_receipt = _sha256(
            _canonical([item for item in entries if str(item["name"]).startswith("database")])
        )
        inbox_receipt = _sha256(
            _canonical([item for item in entries if str(item["name"]).startswith("inbox")])
        )
        if (
            database_receipt != normalized["database_receipt_sha256"]
            or inbox_receipt != normalized["inbox_receipt_sha256"]
        ):
            raise DRGenerationAuthenticationError("dr_retained_backup_receipt_mismatch")

        obsidian_raw, obsidian_status = _stable_private_file(
            directory / "obsidian-manifest.json",
            maximum=release_operator.MAX_EXACT_MANIFEST_BYTES,
            code="dr_retained_obsidian_manifest_invalid",
        )
        if _sha256(obsidian_raw) != normalized["obsidian_receipt_sha256"]:
            raise DRGenerationAuthenticationError("dr_retained_obsidian_receipt_mismatch")
        obsidian_manifest = _json(obsidian_raw, code="dr_retained_obsidian_manifest_invalid")
        present, _directories, obsidian_files = release_operator._validate_obsidian_manifest(  # noqa: SLF001
            obsidian_manifest
        )
        obsidian = release_operator._ExactObsidianBackup(  # noqa: SLF001
            present=present,
            manifest_sha256=normalized["obsidian_receipt_sha256"],
            file_count=len(obsidian_files),
            total_bytes=sum(int(item["size"]) for item in obsidian_files.values()),
        )
        release_operator._verify_obsidian_backup(directory, obsidian)  # noqa: SLF001

        engineer_raw, engineer_status = _stable_private_file(
            directory / "engineer-manifest.json",
            maximum=release_operator.MAX_EXACT_MANIFEST_BYTES,
            code="dr_retained_engineer_manifest_invalid",
        )
        if _sha256(engineer_raw) != normalized["engineer_receipt_sha256"]:
            raise DRGenerationAuthenticationError("dr_retained_engineer_receipt_mismatch")
        engineer_manifest = _json(engineer_raw, code="dr_retained_engineer_manifest_invalid")
        engineer = release_operator._ExactEngineerBackup(  # noqa: SLF001
            manifest_sha256=normalized["engineer_receipt_sha256"],
            entry_count=int(engineer_manifest.get("entry_count", -1)),
            total_bytes=int(engineer_manifest.get("total_bytes", -1)),
            store_present=bool(engineer_manifest.get("store_present")),
            key_present=bool(engineer_manifest.get("key_present")),
        )
        release_operator._validated_engineer_manifest(engineer_raw, engineer)  # noqa: SLF001
        release_operator._verify_engineer_backup(  # noqa: SLF001
            directory,
            engineer,
            verify_sqlite_integrity=False,
        )

        backup_record = {
            "directory": str(directory),
            "engineer": {
                "entry_count": engineer.entry_count,
                "key_present": engineer.key_present,
                "manifest_sha256": engineer.manifest_sha256,
                "store_present": engineer.store_present,
                "total_bytes": engineer.total_bytes,
            },
            "engineer_receipt_sha256": engineer.manifest_sha256,
            "files": entries,
            "inbox_receipt_sha256": inbox_receipt,
            "obsidian": {
                "file_count": obsidian.file_count,
                "manifest_sha256": obsidian.manifest_sha256,
                "present": obsidian.present,
                "total_bytes": obsidian.total_bytes,
            },
            "obsidian_receipt_sha256": obsidian.manifest_sha256,
            "receipt_sha256": database_receipt,
            "schema_version": schema_version,
        }
        if _sha256(_canonical(backup_record)) != normalized["backup_record_sha256"]:
            raise DRGenerationAuthenticationError("dr_retained_backup_record_mismatch")

        expected_top_level = set(seen) | {
            "engineer-manifest.json",
            "engineer-recovery",
            "manifest.json",
            "obsidian-manifest.json",
        }
        if obsidian.present:
            expected_top_level.add("obsidian-root")
        if {path.name for path in directory.iterdir()} != expected_top_level:
            raise DRGenerationAuthenticationError("dr_retained_backup_manifest_mismatch")

        restore = normalized["restore_release"]
        fallback = release_operator.load_release_identity(
            Path(restore["root"]),
            expected_tree_sha256=restore["tree_manifest_sha256"],
        )
        release_operator.verify_release_tree(fallback)
        if _release_record(fallback) != restore:
            raise DRGenerationAuthenticationError("dr_retained_restore_release_mismatch")
        if fallback.tree_manifest_sha256 not in normalized["allowed_rollback_tree_sha256s"]:
            raise DRGenerationAuthenticationError("dr_retained_rollback_identity_mismatch")
        operator_raw, operator_status = _stable_private_file(
            fallback.root / "artifacts/immutable_release_operator.py",
            maximum=4 << 20,
            code="dr_retained_restore_operator_invalid",
        )
        if _sha256(operator_raw) != receipt["restore_operator_sha256"]:
            raise DRGenerationAuthenticationError("dr_retained_restore_operator_mismatch")

        manifest_after, manifest_after_status = _stable_private_file(
            directory / "manifest.json",
            maximum=1 << 20,
            code="dr_retained_backup_manifest_invalid",
        )
        obsidian_after, obsidian_after_status = _stable_private_file(
            directory / "obsidian-manifest.json",
            maximum=release_operator.MAX_EXACT_MANIFEST_BYTES,
            code="dr_retained_obsidian_manifest_invalid",
        )
        engineer_after, engineer_after_status = _stable_private_file(
            directory / "engineer-manifest.json",
            maximum=release_operator.MAX_EXACT_MANIFEST_BYTES,
            code="dr_retained_engineer_manifest_invalid",
        )
        if (
            _directory_identity(directory) != directory_before
            or manifest_after != manifest_raw
            or _file_identity(manifest_after_status) != _file_identity(manifest_status)
            or obsidian_after != obsidian_raw
            or _file_identity(obsidian_after_status) != _file_identity(obsidian_status)
            or engineer_after != engineer_raw
            or _file_identity(engineer_after_status) != _file_identity(engineer_status)
            or any(
                _file_identity(
                    _stable_private_file_digest(
                        directory / name,
                        maximum=1 << 40,
                        code="dr_retained_backup_file_invalid",
                        allow_empty=True,
                    )[2]
                )
                != identity
                for name, identity in file_statuses.items()
            )
            or _file_identity(
                _stable_private_file(
                    fallback.root / "artifacts/immutable_release_operator.py",
                    maximum=4 << 20,
                    code="dr_retained_restore_operator_invalid",
                )[1]
            )
            != _file_identity(operator_status)
        ):
            raise DRGenerationAuthenticationError("dr_retained_backup_changed")
        return release_operator.DatabaseBackup(
            schema_version=schema_version,
            receipt_sha256=database_receipt,
            inbox_receipt_sha256=inbox_receipt,
            obsidian_receipt_sha256=obsidian.manifest_sha256,
            engineer_receipt_sha256=engineer.manifest_sha256,
            opaque=release_operator._ExactBackupPayload(  # noqa: SLF001
                directory=directory,
                files=tuple(files),
                obsidian=obsidian,
                engineer=engineer,
            ),
        )
    except DRGenerationAuthenticationError:
        raise
    except (
        OSError,
        TypeError,
        ValueError,
        release_operator.ReleaseFailure,
        dr_index.DRGenerationIndexError,
    ) as exc:
        raise DRGenerationAuthenticationError("dr_retained_backup_invalid") from exc


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


def _authenticate_material_locked(
    *,
    activation_journal: Path,
    activation_receipt: Path,
    backup_root: Path,
) -> AuthenticatedDRMaterial:
    """Load rehearsal material while the caller holds the operator lock."""

    first = _authenticate_locked(
        activation_journal=activation_journal,
        activation_receipt=activation_receipt,
        backup_root=backup_root,
    )
    journal = release_operator.DurableActivationJournal(
        activation_journal,
        backup_root=backup_root,
        config_identity_sha256=None,
    )
    # Material authentication is strictly read-only.  The copied Engineer
    # database is integrity-checked later inside the rehearsal scratch contour.
    backup = journal.database_backup(verify_engineer_sqlite_integrity=False)
    if backup is None:
        raise DRGenerationAuthenticationError("dr_activation_backup_missing")
    activation_candidate, activation_previous, fallback = journal.release_identities()
    payload = backup.opaque
    allowed_rollback_tree_sha256s = sorted(
        {
            activation_previous.tree_manifest_sha256,
            fallback.tree_manifest_sha256,
        }
    )
    if (
        not isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
        or payload.obsidian is None
        or payload.engineer is None
        or str(payload.directory) != first.candidate["backup_directory"]
        or backup.receipt_sha256 != first.candidate["database_receipt_sha256"]
        or backup.schema_version != first.candidate["database_schema"]
        or backup.inbox_receipt_sha256 != first.candidate["inbox_receipt_sha256"]
        or backup.obsidian_receipt_sha256 != first.candidate["obsidian_receipt_sha256"]
        or backup.engineer_receipt_sha256 != first.candidate["engineer_receipt_sha256"]
        or allowed_rollback_tree_sha256s != first.candidate["allowed_rollback_tree_sha256s"]
        or _release_record(fallback) != first.candidate["restore_release"]
    ):
        raise DRGenerationAuthenticationError("dr_rehearsal_material_mismatch")
    second = _authenticate_locked(
        activation_journal=activation_journal,
        activation_receipt=activation_receipt,
        backup_root=backup_root,
    )
    backup_after = journal.database_backup(verify_engineer_sqlite_integrity=False)
    releases_after = journal.release_identities()
    if (
        second != first
        or backup_after != backup
        or releases_after != (activation_candidate, activation_previous, fallback)
    ):
        raise DRGenerationAuthenticationError("dr_rehearsal_material_changed")
    return AuthenticatedDRMaterial(
        authenticated=second,
        backup=backup,
        activation_candidate=activation_candidate,
        activation_previous=activation_previous,
        restore_fallback=fallback,
    )


__all__ = [
    "AUTHENTICATION_RECEIPT_SCHEMA",
    "AuthenticatedDRCandidate",
    "AuthenticatedDRMaterial",
    "DRGenerationAuthenticationError",
    "authenticate_terminal_activation_backup",
    "reauthenticate_generation_candidate",
]
