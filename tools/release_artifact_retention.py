#!/usr/bin/env python3
"""Plan bounded wheel-release retention without mutating release state.

The phase-one planner is deliberately incapable of deletion.  It authenticates
the exact release identities carried by the durable release journals, observes
only direct children of caller-supplied inventory roots, and emits one closed
decision per child.  A production CLI invocation has no complete open-file
inventory, so it fails closed to ``retain`` until a code-owned phase-two probe
is available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import immutable_release_operator as release_operator  # noqa: E402

PLAN_SCHEMA = "friday.release-artifact-retention-plan.v2"
AUTHORITY_BINDINGS_SCHEMA = "friday.release-artifact-retention-authority-bindings.v1"
OPEN_INVENTORY_SCHEMA = "friday.release-artifact-open-inventory.v1"
MAX_JOURNAL_BYTES = 1 << 20
MAX_RELEASE_MANIFEST_BYTES = 64 << 20
MAX_INVENTORY_ENTRIES = 1_000_000

_HEX64 = frozenset("0123456789abcdef")
_OPEN_SOURCES = frozenset({"unavailable", "code_owned_fd_inventory_v1", "synthetic_test"})
_REASONS = frozenset(
    {
        "activation_journal_invalid",
        "activation_journal_digest_mismatch",
        "activation_backup",
        "activation_not_clear",
        "backup_inventory_root_raced",
        "canonical_evidence",
        "canonical_evidence_invalid",
        "canonical_evidence_unavailable",
        "current_release",
        "dr_current_backup",
        "dr_index_invalid",
        "dr_older_backup",
        "dr_pending_backup",
        "dr_pins_invalid",
        "dr_pins_unavailable",
        "fallback_release",
        "hardlinked_artifact",
        "inventory_root_raced",
        "journal_identity_mismatch",
        "journal_referenced",
        "malformed_release",
        "legacy_or_unknown_backup",
        "non_owned_artifact",
        "open_reference",
        "open_state_ambiguous",
        "previous_release",
        "protected_release_authentication_failed",
        "raced_artifact",
        "retirable_authenticated_release",
        "special_artifact",
        "symlink_artifact",
        "unit_install_journal_invalid",
        "unit_install_journal_digest_mismatch",
        "unit_install_not_complete",
        "retention_authority_unbound",
        "unknown_artifact",
    }
)


class RetentionPlanError(RuntimeError):
    """A closed planner-input failure safe to expose in a CLI receipt."""


@dataclass(frozen=True)
class OpenInventorySnapshot:
    """Injected closed-world open-path observation.

    The CLI intentionally cannot construct a complete snapshot.  Phase two can
    supply ``code_owned_fd_inventory_v1`` after implementing an authenticated
    probe; unit tests use ``synthetic_test``.
    """

    source: str
    complete: bool
    open_paths: tuple[Path, ...] = ()
    open_identities: tuple[tuple[int, int], ...] = ()


INCOMPLETE_OPEN_INVENTORY = OpenInventorySnapshot(source="unavailable", complete=False)


@dataclass(frozen=True)
class DRGenerationPin:
    """One already-authenticated DR-index projection supplied by its owner."""

    role: str
    backup_directory: Path
    generation_id: str | None
    receipt_path: Path | None
    receipt_sha256: str | None


@dataclass(frozen=True)
class CanonicalEvidenceRoot:
    """An exact evidence root bound to one code-owned authority file."""

    path: Path
    authority_path: Path
    authority_sha256: str


@dataclass(frozen=True)
class RetentionAuthorityBindings:
    """Caller-authenticated authority snapshot used only to add retention pins.

    A binding can never grant mutation authority.  The planner re-observes every
    file digest and directory identity so a stale or forged projection blocks
    classification instead of weakening retention.
    """

    activation_journal_sha256: str
    unit_install_journal_sha256: str
    dr_index_path: Path
    dr_index_sha256: str
    dr_pins: tuple[DRGenerationPin, ...]
    canonical_evidence_roots: tuple[CanonicalEvidenceRoot, ...]


@dataclass(frozen=True)
class _TreeSnapshot:
    records: tuple[tuple[str, int, int, int, int, int, int, int, int], ...]
    total_bytes: int
    entry_count: int
    owner_ok: bool
    has_symlink: bool
    has_special: bool
    has_hardlink: bool


@dataclass(frozen=True)
class _TargetObservation:
    path: Path
    device: int | None
    inode: int | None
    kind: str
    nlink: int | None
    total_bytes: int | None
    entry_count: int | None
    inventory_sha256: str | None
    object_identities: frozenset[tuple[int, int]]
    owner_ok: bool
    has_symlink: bool
    has_special: bool
    has_hardlink: bool
    raced: bool


@dataclass(frozen=True)
class _JournalResult:
    state: Mapping[str, Any] | None
    sha256: str
    error: str
    activation_backup: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _AuthorityResult:
    receipt: Mapping[str, Any]
    dr_role_paths: Mapping[str, Path]
    evidence_paths: frozenset[Path]
    reference_paths: frozenset[Path]
    error: str


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _absolute_lexical(path: Path, *, code: str) -> Path:
    if not path.is_absolute() or any(character in str(path) for character in "\x00\r\n"):
        raise RetentionPlanError(code)
    lexical = Path(os.path.abspath(path))
    if lexical != path:
        raise RetentionPlanError(code)
    return lexical


def _strict_private_directory(path: Path, *, code: str) -> Path:
    lexical = _absolute_lexical(path, code=code)
    try:
        status = os.lstat(lexical)
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RetentionPlanError(code) from exc
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise RetentionPlanError(code)
    return lexical


def _strict_inventory_root(path: Path) -> tuple[Path, os.stat_result]:
    lexical = _absolute_lexical(path, code="inventory_root_invalid")
    try:
        status = os.lstat(lexical)
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RetentionPlanError("inventory_root_invalid") from exc
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o022
    ):
        raise RetentionPlanError("inventory_root_invalid")
    return lexical, status


def _stable_file_bytes(
    path: Path,
    *,
    private: bool,
    code: str,
    maximum_bytes: int = MAX_JOURNAL_BYTES,
) -> bytes:
    lexical = _absolute_lexical(path, code=code)
    if not lexical.name or lexical.name in {".", ".."}:
        raise RetentionPlanError(code)
    parent_fd = -1
    try:
        parent_fd, parent_parts, parent_identities = _open_absolute_directory_chain(
            lexical.parent,
            code=code,
        )
        _require_pinned_directory(
            parent_fd,
            parent_parts,
            parent_identities,
            code=code,
            private=private,
        )
        before = os.stat(lexical.name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, RetentionPlanError) as exc:
        if parent_fd >= 0:
            os.close(parent_fd)
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError(code) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or not 0 < before.st_size <= maximum_bytes
        or (private and stat.S_IMODE(before.st_mode) & 0o077)
    ):
        os.close(parent_fd)
        raise RetentionPlanError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    chunks: list[bytes] = []
    try:
        descriptor = os.open(lexical.name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RetentionPlanError(code)
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining == 0 and os.read(descriptor, 1):
                raise RetentionPlanError(code)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = os.stat(lexical.name, dir_fd=parent_fd, follow_symlinks=False)
        _require_pinned_directory(
            parent_fd,
            parent_parts,
            parent_identities,
            code=code,
            private=private,
        )
    except OSError as exc:
        raise RetentionPlanError(code) from exc
    finally:
        os.close(parent_fd)
    identity = lambda item: (  # noqa: E731 - compact immutable comparison
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if identity(before) != identity(after_open) or identity(before) != identity(after):
        raise RetentionPlanError(code)
    return b"".join(chunks)


def _stable_file_sha256(
    path: Path,
    *,
    private: bool,
    code: str,
    maximum_bytes: int = MAX_JOURNAL_BYTES,
) -> str:
    return hashlib.sha256(
        _stable_file_bytes(path, private=private, code=code, maximum_bytes=maximum_bytes)
    ).hexdigest()


def _unique_json(raw: bytes, *, code: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise RetentionPlanError(code)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RetentionPlanError(code) from exc
    if not isinstance(value, dict):
        raise RetentionPlanError(code)
    return value


def _journal_core(raw: bytes, *, code: str) -> dict[str, Any]:
    payload = _unique_json(raw, code=code)
    if raw != _canonical_json(payload) + b"\n" or "journal_sha256" not in payload:
        raise RetentionPlanError(code)
    supplied = payload.pop("journal_sha256")
    if not _is_hex64(supplied) or supplied != hashlib.sha256(_canonical_json(payload)).hexdigest():
        raise RetentionPlanError(code)
    return payload


def _bound_release_metadata(root: Path, tree_sha256: str, *, code: str) -> dict[str, Any]:
    manifest = _stable_file_bytes(
        root / "artifacts/release-tree.sha256",
        private=False,
        code=code,
        maximum_bytes=MAX_RELEASE_MANIFEST_BYTES,
    )
    if hashlib.sha256(manifest).hexdigest() != tree_sha256:
        raise RetentionPlanError(code)
    declared: list[str] = []
    try:
        for line in manifest.decode("utf-8").splitlines():
            fields = line.split(" ", 3)
            if (
                len(fields) == 4
                and fields[0] == "F"
                and fields[3] == "artifacts/immutable-release.json"
                and _is_hex64(fields[2])
            ):
                declared.append(fields[2])
    except UnicodeError as exc:
        raise RetentionPlanError(code) from exc
    if len(declared) != 1:
        raise RetentionPlanError(code)
    metadata = _stable_file_bytes(
        root / "artifacts/immutable-release.json",
        private=False,
        code=code,
    )
    if hashlib.sha256(metadata).hexdigest() != declared[0]:
        raise RetentionPlanError(code)
    return _unique_json(metadata, code=code)


def _read_activation_journal(path: Path, backup_root: Path) -> _JournalResult:
    try:
        _strict_private_directory(path.parent, code="activation_journal_invalid")
        strict_backup = _strict_private_directory(backup_root, code="activation_journal_invalid")
        before_raw = _stable_file_bytes(path, private=True, code="activation_journal_invalid")
        expected_state = _journal_core(before_raw, code="activation_journal_invalid")
        journal = release_operator.DurableActivationJournal(
            path,
            backup_root=strict_backup,
            config_identity_sha256=None,
        )
        state = dict(journal.load())
        if state != expected_state:
            raise RetentionPlanError("activation_journal_invalid")
        raw_backup = state.get("backup")
        # Classification is strictly observational.  Re-authenticate the exact
        # durable receipts without creating the Engineer SQLite scratch copy
        # used by activation-time integrity verification.
        verified_backup = journal.database_backup(verify_engineer_sqlite_integrity=False)
        activation_backup: dict[str, Any] | None = None
        if raw_backup is None:
            if verified_backup is not None:
                raise RetentionPlanError("activation_journal_invalid")
        else:
            if not isinstance(raw_backup, Mapping) or verified_backup is None:
                raise RetentionPlanError("activation_journal_invalid")
            directory = _absolute_lexical(
                Path(str(raw_backup.get("directory") or "")),
                code="activation_journal_invalid",
            )
            activation_backup = {
                "path": str(directory),
                "schema_version": verified_backup.schema_version,
                "database_receipt_sha256": verified_backup.receipt_sha256,
                "inbox_receipt_sha256": verified_backup.inbox_receipt_sha256,
                "obsidian_receipt_sha256": verified_backup.obsidian_receipt_sha256,
                "engineer_receipt_sha256": verified_backup.engineer_receipt_sha256,
            }
        after_raw = _stable_file_bytes(path, private=True, code="activation_journal_invalid")
        if before_raw != after_raw:
            raise RetentionPlanError("activation_journal_invalid")
        return _JournalResult(
            state=state,
            sha256=hashlib.sha256(after_raw).hexdigest(),
            error="",
            activation_backup=activation_backup,
        )
    except (OSError, RetentionPlanError, release_operator.ReleaseFailure):
        return _JournalResult(state=None, sha256="", error="activation_journal_invalid")


def _read_unit_journal(path: Path) -> _JournalResult:
    try:
        _strict_private_directory(path.parent, code="unit_install_journal_invalid")
        before_raw = _stable_file_bytes(path, private=True, code="unit_install_journal_invalid")
        expected_state = _journal_core(before_raw, code="unit_install_journal_invalid")
        state = dict(release_operator.DurableUnitInstallJournal(path).load())
        if state != expected_state:
            raise RetentionPlanError("unit_install_journal_invalid")
        after_raw = _stable_file_bytes(path, private=True, code="unit_install_journal_invalid")
        if before_raw != after_raw:
            raise RetentionPlanError("unit_install_journal_invalid")
        return _JournalResult(
            state=state,
            sha256=hashlib.sha256(after_raw).hexdigest(),
            error="",
        )
    except (OSError, RetentionPlanError, release_operator.ReleaseFailure):
        return _JournalResult(state=None, sha256="", error="unit_install_journal_invalid")


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block"
    if stat.S_ISCHR(mode):
        return "character"
    return "unknown"


def _snapshot(path: Path) -> _TreeSnapshot:
    records: list[tuple[str, int, int, int, int, int, int, int, int]] = []
    total_bytes = 0
    owner_ok = True
    has_symlink = False
    has_special = False
    has_hardlink = False
    root_status = os.lstat(path)
    root_device = int(root_status.st_dev)
    pending: list[tuple[int, str]] = []

    def observe(relative: str, status: os.stat_result) -> None:
        nonlocal total_bytes, owner_ok, has_symlink, has_special, has_hardlink
        kind = _kind(status.st_mode)
        records.append(
            (
                relative,
                int(status.st_dev),
                int(status.st_ino),
                int(status.st_mode),
                int(status.st_nlink),
                int(status.st_uid),
                int(status.st_size),
                int(status.st_mtime_ns),
                int(status.st_ctime_ns),
            )
        )
        if len(records) > MAX_INVENTORY_ENTRIES:
            raise RetentionPlanError("inventory_target_too_large")
        owner_ok = owner_ok and status.st_uid == os.geteuid()
        has_special = has_special or int(status.st_dev) != root_device
        if kind == "regular":
            total_bytes += int(status.st_size)
            has_hardlink = has_hardlink or status.st_nlink != 1
        elif kind == "symlink":
            has_symlink = True
        elif kind != "directory":
            has_special = True

    observe(".", root_status)
    if stat.S_ISDIR(root_status.st_mode):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_descriptor = os.open(path, flags)
        opened_root = os.fstat(root_descriptor)
        if _root_identity(opened_root) != _root_identity(root_status):
            os.close(root_descriptor)
            raise RetentionPlanError("raced_artifact")
        pending.append((root_descriptor, "."))
    try:
        while pending:
            descriptor, relative = pending.pop()
            try:
                before_directory = os.fstat(descriptor)
                children = sorted(os.listdir(descriptor), reverse=True)
                for name in children:
                    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    child_relative = name if relative == "." else f"{relative}/{name}"
                    observe(child_relative, status)
                    if stat.S_ISDIR(status.st_mode):
                        child_descriptor = os.open(name, flags, dir_fd=descriptor)
                        try:
                            opened = os.fstat(child_descriptor)
                            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                            if _root_identity(opened) != _root_identity(status) or _root_identity(
                                named
                            ) != _root_identity(status):
                                raise RetentionPlanError("raced_artifact")
                        except BaseException:
                            os.close(child_descriptor)
                            raise
                        pending.append((child_descriptor, child_relative))
                if _root_identity(os.fstat(descriptor)) != _root_identity(before_directory):
                    raise RetentionPlanError("raced_artifact")
            finally:
                os.close(descriptor)
    finally:
        for descriptor, _relative in pending:
            os.close(descriptor)
    records.sort(key=lambda item: item[0])
    return _TreeSnapshot(
        records=tuple(records),
        total_bytes=total_bytes,
        entry_count=len(records),
        owner_ok=owner_ok,
        has_symlink=has_symlink,
        has_special=has_special,
        has_hardlink=has_hardlink,
    )


def _observe_target(path: Path) -> _TargetObservation:
    try:
        first = _snapshot(path)
        second = _snapshot(path)
    except (OSError, RetentionPlanError):
        try:
            status = os.lstat(path)
        except OSError:
            return _TargetObservation(
                path=path,
                device=None,
                inode=None,
                kind="unknown",
                nlink=None,
                total_bytes=None,
                entry_count=None,
                inventory_sha256=None,
                object_identities=frozenset(),
                owner_ok=False,
                has_symlink=False,
                has_special=False,
                has_hardlink=False,
                raced=True,
            )
        return _TargetObservation(
            path=path,
            device=int(status.st_dev),
            inode=int(status.st_ino),
            kind=_kind(status.st_mode),
            nlink=int(status.st_nlink),
            total_bytes=None,
            entry_count=None,
            inventory_sha256=None,
            object_identities=frozenset({(int(status.st_dev), int(status.st_ino))}),
            owner_ok=status.st_uid == os.geteuid(),
            has_symlink=stat.S_ISLNK(status.st_mode),
            has_special=not (
                stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode)
            ),
            has_hardlink=stat.S_ISREG(status.st_mode) and status.st_nlink != 1,
            raced=True,
        )
    root = next(record for record in first.records if record[0] == ".")
    return _TargetObservation(
        path=path,
        device=root[1],
        inode=root[2],
        kind=_kind(root[3]),
        nlink=root[4],
        total_bytes=first.total_bytes,
        entry_count=first.entry_count,
        inventory_sha256=hashlib.sha256(_canonical_json(first.records)).hexdigest(),
        object_identities=frozenset((record[1], record[2]) for record in first.records),
        owner_ok=first.owner_ok,
        has_symlink=first.has_symlink,
        has_special=first.has_special,
        has_hardlink=first.has_hardlink,
        raced=first != second,
    )


def _normalize_open_inventory(
    snapshot: OpenInventorySnapshot,
) -> tuple[dict[str, Any], frozenset[Path], frozenset[tuple[int, int]]]:
    if snapshot.source not in _OPEN_SOURCES or type(snapshot.complete) is not bool:
        raise RetentionPlanError("open_inventory_invalid")
    if snapshot.source == "unavailable" and (
        snapshot.complete or snapshot.open_paths or snapshot.open_identities
    ):
        raise RetentionPlanError("open_inventory_invalid")
    if snapshot.source == "code_owned_fd_inventory_v1" and not snapshot.complete:
        raise RetentionPlanError("open_inventory_invalid")
    paths: set[Path] = set()
    for path in snapshot.open_paths:
        paths.add(_absolute_lexical(path, code="open_inventory_invalid"))
    canonical_paths = tuple(sorted(str(path) for path in paths))
    identities: set[tuple[int, int]] = set()
    for identity in snapshot.open_identities:
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or type(identity[0]) is not int
            or type(identity[1]) is not int
            or identity[0] < 0
            or identity[1] <= 0
        ):
            raise RetentionPlanError("open_inventory_invalid")
        identities.add(identity)
    canonical_identities = tuple(sorted(identities))
    core = {
        "schema": OPEN_INVENTORY_SCHEMA,
        "source": snapshot.source,
        "complete": snapshot.complete,
        "open_paths": canonical_paths,
        "open_identities": canonical_identities,
    }
    return (
        {
            "schema": OPEN_INVENTORY_SCHEMA,
            "source": snapshot.source,
            "complete": snapshot.complete,
            "open_path_count": len(paths),
            "open_identity_count": len(identities),
            "snapshot_sha256": hashlib.sha256(_canonical_json(core)).hexdigest(),
        },
        frozenset(paths),
        frozenset(identities),
    )


def _normalize_authority_bindings(
    bindings: RetentionAuthorityBindings | None,
    *,
    activation_sha256: str,
    unit_sha256: str,
) -> _AuthorityResult:
    if bindings is None:
        unbound_core: dict[str, Any] = {
            "schema": AUTHORITY_BINDINGS_SCHEMA,
            "status": "unbound",
            "activation_journal_sha256": "",
            "unit_install_journal_sha256": "",
            "dr_index": None,
            "dr_pins": [],
            "canonical_evidence_roots": [],
        }
        return _AuthorityResult(
            receipt={
                **unbound_core,
                "bindings_sha256": hashlib.sha256(_canonical_json(unbound_core)).hexdigest(),
            },
            dr_role_paths={},
            evidence_paths=frozenset(),
            reference_paths=frozenset(),
            error="retention_authority_unbound",
        )
    if not isinstance(bindings, RetentionAuthorityBindings):
        raise RetentionPlanError("retention_authority_invalid")
    if not _is_hex64(bindings.activation_journal_sha256) or not _is_hex64(
        bindings.unit_install_journal_sha256
    ):
        raise RetentionPlanError("retention_authority_invalid")
    if type(bindings.dr_pins) is not tuple or type(bindings.canonical_evidence_roots) is not tuple:
        raise RetentionPlanError("retention_authority_invalid")

    error = ""
    if activation_sha256 and bindings.activation_journal_sha256 != activation_sha256:
        error = "activation_journal_digest_mismatch"
    if unit_sha256 and bindings.unit_install_journal_sha256 != unit_sha256 and not error:
        error = "unit_install_journal_digest_mismatch"

    dr_index_path = _absolute_lexical(bindings.dr_index_path, code="dr_pins_invalid")
    if not _is_hex64(bindings.dr_index_sha256):
        raise RetentionPlanError("dr_pins_invalid")
    observed_dr_index_sha256 = ""
    try:
        observed_dr_index_sha256 = _stable_file_sha256(
            dr_index_path,
            private=True,
            code="dr_index_invalid",
        )
        if observed_dr_index_sha256 != bindings.dr_index_sha256:
            error = error or "dr_index_invalid"
    except RetentionPlanError:
        error = error or "dr_index_invalid"

    role_paths: dict[str, Path] = {}
    pin_records: list[dict[str, Any]] = []
    generation_ids: set[str] = set()
    receipt_paths: set[Path] = set()
    for pin in bindings.dr_pins:
        if not isinstance(pin, DRGenerationPin) or pin.role not in {"current", "older", "pending"}:
            raise RetentionPlanError("dr_pins_invalid")
        if pin.role in role_paths:
            raise RetentionPlanError("dr_pins_invalid")
        backup_directory = _absolute_lexical(pin.backup_directory, code="dr_pins_invalid")
        for existing in role_paths.values():
            if (
                backup_directory == existing
                or backup_directory in existing.parents
                or existing in backup_directory.parents
            ):
                raise RetentionPlanError("dr_pins_invalid")
        role_paths[pin.role] = backup_directory

        identity_values = (pin.generation_id, pin.receipt_path, pin.receipt_sha256)
        if pin.role in {"current", "older"} and any(value is None for value in identity_values):
            raise RetentionPlanError("dr_pins_invalid")
        if (
            pin.role == "pending"
            and any(value is None for value in identity_values)
            and not all(value is None for value in identity_values)
        ):
            raise RetentionPlanError("dr_pins_invalid")
        receipt_path: Path | None = None
        observed_receipt_sha256 = ""
        if pin.generation_id is not None:
            if (
                pin.receipt_path is None
                or not _is_hex64(pin.generation_id)
                or not _is_hex64(pin.receipt_sha256)
            ):
                raise RetentionPlanError("dr_pins_invalid")
            receipt_path = _absolute_lexical(Path(pin.receipt_path), code="dr_pins_invalid")
            if pin.generation_id in generation_ids or receipt_path in receipt_paths:
                raise RetentionPlanError("dr_pins_invalid")
            generation_ids.add(pin.generation_id)
            receipt_paths.add(receipt_path)
            try:
                observed_receipt_sha256 = _stable_file_sha256(
                    receipt_path,
                    private=True,
                    code="dr_pins_invalid",
                )
                if observed_receipt_sha256 != pin.receipt_sha256:
                    error = error or "dr_pins_invalid"
            except RetentionPlanError:
                error = error or "dr_pins_invalid"
        try:
            _strict_private_directory(backup_directory, code="dr_pins_invalid")
        except RetentionPlanError:
            error = error or "dr_pins_invalid"
        pin_records.append(
            {
                "role": pin.role,
                "backup_directory": str(backup_directory),
                "generation_id": pin.generation_id,
                "receipt_path": str(receipt_path) if receipt_path is not None else None,
                "receipt_sha256": pin.receipt_sha256,
                "observed_receipt_sha256": observed_receipt_sha256,
            }
        )
    if not {"current", "older"}.issubset(role_paths):
        error = error or "dr_pins_unavailable"

    evidence_paths: set[Path] = set()
    evidence_authority_paths: set[Path] = set()
    evidence_records: list[dict[str, Any]] = []
    if len(bindings.canonical_evidence_roots) > 128:
        raise RetentionPlanError("canonical_evidence_invalid")
    for evidence in bindings.canonical_evidence_roots:
        if not isinstance(evidence, CanonicalEvidenceRoot) or not _is_hex64(evidence.authority_sha256):
            raise RetentionPlanError("canonical_evidence_invalid")
        root = _absolute_lexical(evidence.path, code="canonical_evidence_invalid")
        authority_path = _absolute_lexical(
            evidence.authority_path,
            code="canonical_evidence_invalid",
        )
        if root != authority_path and root not in authority_path.parents:
            raise RetentionPlanError("canonical_evidence_invalid")
        if authority_path in evidence_authority_paths:
            raise RetentionPlanError("canonical_evidence_invalid")
        for existing in evidence_paths:
            if root == existing or root in existing.parents or existing in root.parents:
                raise RetentionPlanError("canonical_evidence_invalid")
        for dr_path in role_paths.values():
            if root == dr_path or root in dr_path.parents or dr_path in root.parents:
                raise RetentionPlanError("canonical_evidence_invalid")
        evidence_paths.add(root)
        evidence_authority_paths.add(authority_path)
        observed_authority_sha256 = ""
        device: int | None = None
        inode: int | None = None
        try:
            _strict_root, root_status = _strict_inventory_root(root)
            device = int(root_status.st_dev)
            inode = int(root_status.st_ino)
            observed_authority_sha256 = _stable_file_sha256(
                authority_path,
                private=False,
                code="canonical_evidence_invalid",
            )
            if observed_authority_sha256 != evidence.authority_sha256:
                error = error or "canonical_evidence_invalid"
        except RetentionPlanError:
            error = error or "canonical_evidence_invalid"
        evidence_records.append(
            {
                "path": str(root),
                "device": device,
                "inode": inode,
                "authority_path": str(authority_path),
                "authority_sha256": evidence.authority_sha256,
                "observed_authority_sha256": observed_authority_sha256,
            }
        )
    if not evidence_paths:
        error = error or "canonical_evidence_unavailable"

    ordered_roles = {"current": 0, "older": 1, "pending": 2}
    pin_records.sort(key=lambda value: ordered_roles[str(value["role"])])
    evidence_records.sort(key=lambda value: str(value["path"]))
    core: dict[str, Any] = {
        "schema": AUTHORITY_BINDINGS_SCHEMA,
        "status": "authenticated" if not error else "invalid",
        "activation_journal_sha256": bindings.activation_journal_sha256,
        "unit_install_journal_sha256": bindings.unit_install_journal_sha256,
        "dr_index": {
            "path": str(dr_index_path),
            "sha256": bindings.dr_index_sha256,
            "observed_sha256": observed_dr_index_sha256,
        },
        "dr_pins": pin_records,
        "canonical_evidence_roots": evidence_records,
    }
    return _AuthorityResult(
        receipt={**core, "bindings_sha256": hashlib.sha256(_canonical_json(core)).hexdigest()},
        dr_role_paths=role_paths,
        evidence_paths=frozenset(evidence_paths),
        reference_paths=frozenset(
            {
                dr_index_path,
                *role_paths.values(),
                *receipt_paths,
                *evidence_paths,
                *evidence_authority_paths,
            }
        ),
        error=error,
    )


def _record_path(record: Mapping[str, Any]) -> Path:
    raw = record.get("root")
    if not isinstance(raw, str):
        raise RetentionPlanError("journal_identity_mismatch")
    return _absolute_lexical(Path(raw), code="journal_identity_mismatch")


def _same_identity(left: object, right: object) -> bool:
    return isinstance(left, Mapping) and isinstance(right, Mapping) and dict(left) == dict(right)


def _authenticate_release(record: Mapping[str, Any]) -> dict[str, Any]:
    root = _record_path(record)
    try:
        status = os.lstat(root)
        if root.resolve(strict=True) != root or not stat.S_ISDIR(status.st_mode):
            raise RetentionPlanError("protected_release_authentication_failed")
        tree_sha256 = str(record.get("tree_manifest_sha256") or "")
        if not _is_hex64(tree_sha256):
            raise RetentionPlanError("protected_release_authentication_failed")
        identity = release_operator.load_release_identity(root, expected_tree_sha256=tree_sha256)
        release_operator.verify_release_tree(identity)
        metadata = _bound_release_metadata(
            root,
            tree_sha256,
            code="protected_release_authentication_failed",
        )
        if (
            identity.root != root
            or identity.commit != record.get("commit")
            or identity.version != record.get("version")
            or identity.max_schema != record.get("max_schema")
            or identity.tree_manifest_sha256 != tree_sha256
            or metadata.get("commit") != identity.commit
            or metadata.get("version") != identity.version
            or metadata.get("max_schema") != identity.max_schema
        ):
            raise RetentionPlanError("protected_release_authentication_failed")
        wheel_sha256 = metadata.get("wheel_sha256")
        if not _is_hex64(wheel_sha256):
            raise RetentionPlanError("protected_release_authentication_failed")
    except (OSError, UnicodeError, ValueError, release_operator.ReleaseFailure) as exc:
        raise RetentionPlanError("protected_release_authentication_failed") from exc
    return {
        "path": str(root),
        "commit": identity.commit,
        "version": identity.version,
        "max_schema": identity.max_schema,
        "tree_manifest_sha256": identity.tree_manifest_sha256,
        "wheel_sha256": str(wheel_sha256),
    }


def _discover_release(path: Path) -> dict[str, Any] | None:
    manifest = path / "artifacts/release-tree.sha256"
    metadata = path / "artifacts/immutable-release.json"
    if (
        not manifest.exists()
        and not manifest.is_symlink()
        and not metadata.exists()
        and not metadata.is_symlink()
    ):
        return None
    try:
        tree_sha256 = _stable_file_sha256(
            manifest,
            private=False,
            code="malformed_release",
            maximum_bytes=MAX_RELEASE_MANIFEST_BYTES,
        )
        identity = release_operator.load_release_identity(path, expected_tree_sha256=tree_sha256)
        release_operator.verify_release_tree(identity)
        payload = _bound_release_metadata(path, tree_sha256, code="malformed_release")
        if (
            identity.root != path
            or identity.tree_manifest_sha256 != tree_sha256
            or payload.get("commit") != identity.commit
            or payload.get("version") != identity.version
            or payload.get("max_schema") != identity.max_schema
        ):
            raise RetentionPlanError("malformed_release")
        wheel_sha256 = payload.get("wheel_sha256")
        if not _is_hex64(wheel_sha256):
            raise RetentionPlanError("malformed_release")
    except (OSError, UnicodeError, ValueError, RetentionPlanError, release_operator.ReleaseFailure):
        return {"invalid": True}
    return {
        "commit": identity.commit,
        "version": identity.version,
        "max_schema": identity.max_schema,
        "tree_manifest_sha256": identity.tree_manifest_sha256,
        "wheel_sha256": str(wheel_sha256),
    }


def _path_intersects(path: Path, references: frozenset[Path]) -> bool:
    for reference in references:
        if path == reference or path in reference.parents or reference in path.parents:
            return True
    return False


def _root_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_nlink),
        int(status.st_uid),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _inode_identity(status: os.stat_result) -> tuple[int, int]:
    return int(status.st_dev), int(status.st_ino)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_absolute_directory_chain(
    path: Path,
    *,
    code: str = "output_path_invalid",
) -> tuple[int, tuple[str, ...], tuple[tuple[int, int], ...]]:
    parts = tuple(path.parts[1:])
    if not path.is_absolute() or path.anchor != os.sep or any(part in {"", ".", ".."} for part in parts):
        raise RetentionPlanError(code)
    current = -1
    identities: list[tuple[int, int]] = []
    try:
        current = os.open(os.sep, _directory_open_flags())
        identities.append(_inode_identity(os.fstat(current)))
        for part in parts:
            child = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISDIR(opened.st_mode) or _inode_identity(opened) != _inode_identity(named):
                    raise RetentionPlanError(code)
                identities.append(_inode_identity(opened))
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current, parts, tuple(identities)
    except BaseException as exc:
        if current >= 0:
            os.close(current)
        if isinstance(exc, RetentionPlanError):
            raise
        if not isinstance(exc, (OSError, ValueError)):
            raise
        raise RetentionPlanError(code) from exc


def _require_pinned_directory(
    descriptor: int,
    parts: tuple[str, ...],
    identities: tuple[tuple[int, int], ...],
    *,
    code: str = "output_path_invalid",
    private: bool = True,
) -> os.stat_result:
    current = -1
    try:
        if len(identities) != len(parts) + 1:
            raise RetentionPlanError(code)
        current = os.open(os.sep, _directory_open_flags())
        if _inode_identity(os.fstat(current)) != identities[0]:
            raise RetentionPlanError(code)
        for part, expected in zip(parts, identities[1:], strict=True):
            child = os.open(part, _directory_open_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _inode_identity(opened) != expected
                    or _inode_identity(named) != expected
                ):
                    raise RetentionPlanError(code)
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        held = os.fstat(descriptor)
        if (
            _inode_identity(held) != identities[-1]
            or held.st_uid != os.geteuid()
            or (private and stat.S_IMODE(held.st_mode) & 0o077)
            or (not private and stat.S_IMODE(held.st_mode) & 0o022)
        ):
            raise RetentionPlanError(code)
        return held
    except (OSError, ValueError) as exc:
        raise RetentionPlanError(code) from exc
    finally:
        if current >= 0:
            os.close(current)


def plan_release_artifact_retention(
    *,
    activation_journal: Path,
    unit_journal: Path,
    backup_root: Path,
    inventory_roots: Sequence[Path],
    backup_inventory_roots: Sequence[Path] = (),
    open_inventory: OpenInventorySnapshot = INCOMPLETE_OPEN_INVENTORY,
    authority_bindings: RetentionAuthorityBindings | None = None,
) -> dict[str, Any]:
    """Return a canonicalizable, read-only v2 retention plan."""

    if not inventory_roots:
        raise RetentionPlanError("inventory_roots_required")
    activation_path = _absolute_lexical(activation_journal, code="activation_journal_invalid")
    unit_path = _absolute_lexical(unit_journal, code="unit_install_journal_invalid")
    backup_path = _absolute_lexical(backup_root, code="activation_journal_invalid")
    open_receipt, open_paths, open_identities = _normalize_open_inventory(open_inventory)

    roots_with_status = [_strict_inventory_root(path) for path in inventory_roots]
    roots = tuple(sorted({path for path, _status in roots_with_status}, key=str))
    if len(roots) != len(inventory_roots):
        raise RetentionPlanError("inventory_roots_duplicate")
    backup_roots_with_status = [_strict_inventory_root(path) for path in backup_inventory_roots]
    backup_roots = tuple(sorted({path for path, _status in backup_roots_with_status}, key=str))
    if len(backup_roots) != len(backup_inventory_roots):
        raise RetentionPlanError("inventory_roots_duplicate")
    all_roots = tuple(sorted((*roots, *backup_roots), key=str))
    if len(set(all_roots)) != len(all_roots):
        raise RetentionPlanError("inventory_roots_duplicate")
    for index, root in enumerate(all_roots):
        for other in all_roots[index + 1 :]:
            if root in other.parents or other in root.parents:
                raise RetentionPlanError("inventory_roots_overlap")
    root_statuses = {path: status for path, status in roots_with_status}
    backup_root_statuses = {path: status for path, status in backup_roots_with_status}

    activation = _read_activation_journal(activation_path, backup_path)
    unit = _read_unit_journal(unit_path)
    blocker = activation.error or unit.error
    activation_state = activation.state
    unit_state = unit.state
    if not blocker and activation_state is not None and activation_state.get("phase") != "clear":
        blocker = "activation_not_clear"
    if not blocker and unit_state is not None and unit_state.get("phase") != "complete":
        blocker = "unit_install_not_complete"
    authority = _normalize_authority_bindings(
        authority_bindings,
        activation_sha256=activation.sha256,
        unit_sha256=unit.sha256,
    )
    blocker = blocker or authority.error

    references: set[Path] = {activation_path, unit_path, *authority.reference_paths}
    role_paths: dict[str, Path] = {}
    protected_identities: list[dict[str, Any]] = []
    role_records: dict[str, Mapping[str, Any]] = {}
    if activation_state is not None:
        try:
            role_records = {
                "current": activation_state["candidate"],
                "previous": activation_state["previous"],
                "fallback": activation_state["fallback"],
            }
            for role, record in role_records.items():
                if not isinstance(record, Mapping):
                    raise RetentionPlanError("journal_identity_mismatch")
                role_paths[role] = _record_path(record)
                references.add(role_paths[role])
            backup = activation_state.get("backup")
            if isinstance(backup, Mapping) and isinstance(backup.get("directory"), str):
                references.add(
                    _absolute_lexical(Path(str(backup["directory"])), code="journal_identity_mismatch")
                )
            next_env_file = activation_state.get("next_env_file")
            if isinstance(next_env_file, str) and next_env_file:
                references.add(_absolute_lexical(Path(next_env_file), code="journal_identity_mismatch"))
            if unit_state is not None:
                for key in ("candidate", "previous"):
                    unit_record = unit_state.get(key)
                    if not isinstance(unit_record, Mapping):
                        raise RetentionPlanError("journal_identity_mismatch")
                    references.add(_record_path(unit_record))
                transition_root = unit_state.get("transition_root")
                if not isinstance(transition_root, str):
                    raise RetentionPlanError("journal_identity_mismatch")
                references.add(_absolute_lexical(Path(transition_root), code="journal_identity_mismatch"))
                if not _same_identity(
                    unit_state.get("candidate"), role_records["current"]
                ) or not _same_identity(unit_state.get("previous"), role_records["previous"]):
                    raise RetentionPlanError("journal_identity_mismatch")
            authenticated: dict[Path, dict[str, Any]] = {}
            for record in role_records.values():
                record_path = _record_path(record)
                if record_path not in authenticated:
                    authenticated[record_path] = _authenticate_release(record)
            for path in sorted(authenticated, key=str):
                roles = sorted(role for role, role_path in role_paths.items() if role_path == path)
                protected_identities.append({**authenticated[path], "roles": roles})
        except RetentionPlanError as exc:
            blocker = blocker or str(exc)

    activation_backup_path: Path | None = None
    if activation.activation_backup is not None:
        try:
            activation_backup_path = _absolute_lexical(
                Path(str(activation.activation_backup["path"])),
                code="activation_journal_invalid",
            )
            references.add(activation_backup_path)
        except (KeyError, RetentionPlanError):
            blocker = blocker or "activation_journal_invalid"
    if not open_inventory.complete:
        blocker = blocker or "open_state_ambiguous"

    observations: list[tuple[_TargetObservation, Path]] = []
    for root in roots:
        try:
            with os.scandir(root) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError:
            names = []
            blocker = blocker or "inventory_root_raced"
        for name in names:
            observations.append((_observe_target(root / name), root))

    backup_observations: list[tuple[_TargetObservation, Path]] = []
    for root in backup_roots:
        try:
            with os.scandir(root) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError:
            names = []
            blocker = blocker or "backup_inventory_root_raced"
        for name in names:
            backup_observations.append((_observe_target(root / name), root))

    root_raced: set[Path] = set()
    for root in roots:
        try:
            if _root_identity(os.lstat(root)) != _root_identity(root_statuses[root]):
                root_raced.add(root)
        except OSError:
            root_raced.add(root)
    if root_raced:
        blocker = blocker or "inventory_root_raced"

    backup_roots_raced: set[Path] = set()
    for root in backup_roots:
        try:
            if _root_identity(os.lstat(root)) != _root_identity(backup_root_statuses[root]):
                backup_roots_raced.add(root)
        except OSError:
            backup_roots_raced.add(root)
    if backup_roots_raced:
        blocker = blocker or "backup_inventory_root_raced"

    backup_observations_by_path = {
        observation.path: observation for observation, _root in backup_observations
    }
    for dr_path in authority.dr_role_paths.values():
        if dr_path not in backup_observations_by_path:
            blocker = blocker or "dr_pins_invalid"
    if activation_backup_path is not None and activation_backup_path not in backup_observations_by_path:
        blocker = blocker or "activation_journal_invalid"
    all_observations = (*observations, *backup_observations)
    for evidence_path in authority.evidence_paths:
        matching_targets = [
            observation
            for observation, _root in all_observations
            if _path_intersects(observation.path, frozenset({evidence_path}))
        ]
        if len(matching_targets) != 1 or matching_targets[0].raced:
            blocker = blocker or "canonical_evidence_invalid"

    activation_after = _read_activation_journal(activation_path, backup_path)
    unit_after = _read_unit_journal(unit_path)
    if activation_after != activation:
        blocker = blocker or "activation_journal_invalid"
    if unit_after != unit:
        blocker = blocker or "unit_install_journal_invalid"
    authority_after = _normalize_authority_bindings(
        authority_bindings,
        activation_sha256=activation.sha256,
        unit_sha256=unit.sha256,
    )
    if authority_after != authority:
        blocker = blocker or authority_after.error or "dr_pins_invalid"

    protected_raced: set[Path] = set()
    protected_failed: set[Path] = set()
    initial_observations = {observation.path: observation for observation, _root in observations}
    for role_path in frozenset(role_paths.values()):
        observation = initial_observations.get(role_path)
        if observation is None:
            protected_failed.add(role_path)
            blocker = blocker or "protected_release_authentication_failed"
            continue
        record = next(record for role, record in role_records.items() if role_paths[role] == role_path)
        try:
            _authenticate_release(record)
            after_authentication = _observe_target(role_path)
        except RetentionPlanError:
            protected_failed.add(role_path)
            blocker = blocker or "protected_release_authentication_failed"
            continue
        if after_authentication.raced or after_authentication != observation:
            protected_raced.add(role_path)
            blocker = blocker or "raced_artifact"

    entries: list[dict[str, Any]] = []
    frozen_references = frozenset(references)
    for observation, root in sorted(observations, key=lambda item: str(item[0].path)):
        release_identity: dict[str, Any] | None = None
        reason = ""
        if observation.raced:
            reason = "raced_artifact"
        elif root in root_raced:
            reason = "inventory_root_raced"
        elif observation.path in protected_raced:
            reason = "raced_artifact"
        elif observation.path in protected_failed:
            reason = "protected_release_authentication_failed"
        elif observation.path == role_paths.get("current"):
            reason = "current_release"
        elif observation.path == role_paths.get("previous"):
            reason = "previous_release"
        elif observation.path == role_paths.get("fallback"):
            reason = "fallback_release"
        elif _path_intersects(observation.path, authority.evidence_paths):
            reason = "canonical_evidence"
        elif observation.kind == "symlink":
            reason = "symlink_artifact"
        elif (
            observation.kind not in {"directory", "regular"}
            or observation.device != int(root_statuses[root].st_dev)
            or observation.has_special
        ):
            reason = "special_artifact"
        elif not observation.owner_ok:
            reason = "non_owned_artifact"
        elif observation.has_hardlink:
            reason = "hardlinked_artifact"
        elif _path_intersects(observation.path, frozen_references):
            reason = "journal_referenced"
        elif blocker:
            reason = blocker
        elif _path_intersects(observation.path, open_paths) or not observation.object_identities.isdisjoint(
            open_identities
        ):
            reason = "open_reference"
        else:
            discovered = _discover_release(observation.path) if observation.kind == "directory" else None
            if discovered is not None and discovered.get("invalid") is not True:
                release_identity = discovered
                after_discovery = _observe_target(observation.path)
                if after_discovery.raced or after_discovery != observation:
                    reason = "raced_artifact"
                    release_identity = None
                else:
                    reason = "retirable_authenticated_release"
            elif discovered is not None:
                reason = "malformed_release"
            elif observation.has_symlink:
                reason = "symlink_artifact"
            else:
                reason = "unknown_artifact"
        if reason not in _REASONS:
            reason = "unknown_artifact"
        decision = "delete_candidate" if reason == "retirable_authenticated_release" else "retain"
        entries.append(
            {
                "path": str(observation.path),
                "device": observation.device,
                "inode": observation.inode,
                "type": observation.kind,
                "nlink": observation.nlink,
                "recursive_bytes": observation.total_bytes,
                "entry_count": observation.entry_count,
                "inventory_sha256": observation.inventory_sha256,
                "identity": release_identity,
                "decision": decision,
                "reason": reason,
            }
        )

    dr_pin_identities: dict[Path, dict[str, Any]] = {}
    raw_dr_pins = authority.receipt.get("dr_pins")
    if isinstance(raw_dr_pins, list):
        for raw_pin in raw_dr_pins:
            if isinstance(raw_pin, Mapping) and isinstance(raw_pin.get("backup_directory"), str):
                dr_pin_identities[Path(str(raw_pin["backup_directory"]))] = dict(raw_pin)
    evidence_identities: dict[Path, dict[str, Any]] = {}
    raw_evidence = authority.receipt.get("canonical_evidence_roots")
    if isinstance(raw_evidence, list):
        for raw_item in raw_evidence:
            if isinstance(raw_item, Mapping) and isinstance(raw_item.get("path"), str):
                evidence_identities[Path(str(raw_item["path"]))] = dict(raw_item)

    backup_entries: list[dict[str, Any]] = []
    for observation, root in sorted(backup_observations, key=lambda item: str(item[0].path)):
        backup_identity: dict[str, Any] | None = None
        reason = ""
        if observation.raced:
            reason = "raced_artifact"
        elif root in backup_roots_raced:
            reason = "backup_inventory_root_raced"
        elif observation.path == activation_backup_path:
            reason = "activation_backup"
            backup_identity = dict(activation.activation_backup or {})
        elif observation.path == authority.dr_role_paths.get("current"):
            reason = "dr_current_backup"
            backup_identity = dr_pin_identities.get(observation.path)
        elif observation.path == authority.dr_role_paths.get("older"):
            reason = "dr_older_backup"
            backup_identity = dr_pin_identities.get(observation.path)
        elif observation.path == authority.dr_role_paths.get("pending"):
            reason = "dr_pending_backup"
            backup_identity = dr_pin_identities.get(observation.path)
        elif _path_intersects(observation.path, authority.evidence_paths):
            reason = "canonical_evidence"
            backup_identity = next(
                (
                    evidence_identity
                    for evidence_path, evidence_identity in evidence_identities.items()
                    if _path_intersects(observation.path, frozenset({evidence_path}))
                ),
                None,
            )
        elif observation.kind == "symlink":
            reason = "symlink_artifact"
        elif (
            observation.kind not in {"directory", "regular"}
            or observation.device != int(backup_root_statuses[root].st_dev)
            or observation.has_special
        ):
            reason = "special_artifact"
        elif not observation.owner_ok:
            reason = "non_owned_artifact"
        elif observation.has_hardlink:
            reason = "hardlinked_artifact"
        elif _path_intersects(observation.path, frozen_references):
            reason = "journal_referenced"
        elif blocker:
            reason = blocker
        elif _path_intersects(observation.path, open_paths) or not observation.object_identities.isdisjoint(
            open_identities
        ):
            reason = "open_reference"
        else:
            reason = "legacy_or_unknown_backup"
        if reason not in _REASONS:
            reason = "legacy_or_unknown_backup"
        backup_entries.append(
            {
                "path": str(observation.path),
                "device": observation.device,
                "inode": observation.inode,
                "type": observation.kind,
                "nlink": observation.nlink,
                "recursive_bytes": observation.total_bytes,
                "entry_count": observation.entry_count,
                "inventory_sha256": observation.inventory_sha256,
                "identity": backup_identity,
                "decision": "retain",
                "reason": reason,
            }
        )

    core: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mode": "read_only_classification",
        "scope": "release_and_backup_inventory",
        "apply_authority": False,
        "inventory_roots": [
            {
                "path": str(path),
                "device": int(root_statuses[path].st_dev),
                "inode": int(root_statuses[path].st_ino),
                "type": "directory",
                "nlink": int(root_statuses[path].st_nlink),
                "uid": int(root_statuses[path].st_uid),
            }
            for path in roots
        ],
        "backup_inventory_roots": [
            {
                "path": str(path),
                "device": int(backup_root_statuses[path].st_dev),
                "inode": int(backup_root_statuses[path].st_ino),
                "type": "directory",
                "nlink": int(backup_root_statuses[path].st_nlink),
                "uid": int(backup_root_statuses[path].st_uid),
            }
            for path in backup_roots
        ],
        "activation_journal": {
            "path": str(activation_path),
            "sha256": activation.sha256,
            "phase": activation_state.get("phase") if activation_state is not None else "invalid",
        },
        "unit_install_journal": {
            "path": str(unit_path),
            "sha256": unit.sha256,
            "phase": unit_state.get("phase") if unit_state is not None else "invalid",
        },
        "authority_bindings": authority.receipt,
        "activation_backup": activation.activation_backup,
        "open_inventory": open_receipt,
        "classification_status": "eligible" if blocker == "" else "blocked",
        "block_reason": blocker,
        "protected_releases": protected_identities,
        "targets": entries,
        "backup_targets": backup_entries,
    }
    return {**core, "plan_sha256": hashlib.sha256(_canonical_json(core)).hexdigest()}


def _write_atomic(path: Path, payload: bytes) -> None:
    lexical = _absolute_lexical(path, code="output_path_invalid")
    if not lexical.name:
        raise RetentionPlanError("output_path_invalid")
    parent = _absolute_lexical(lexical.parent, code="output_path_invalid")
    directory_fd, parent_parts, parent_identities = _open_absolute_directory_chain(parent)
    temporary = ""
    published = False
    published_identity: tuple[int, int] | None = None
    success = False
    try:
        _require_pinned_directory(directory_fd, parent_parts, parent_identities)
        try:
            os.stat(lexical.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RetentionPlanError("output_exists")
        temporary = f".{lexical.name}.{os.getpid()}.{os.urandom(8).hex()}.new"
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _require_pinned_directory(directory_fd, parent_parts, parent_identities)
        published_identity = _inode_identity(os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False))
        try:
            os.link(
                temporary,
                lexical.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RetentionPlanError("output_exists") from exc
        except OSError as exc:
            raise RetentionPlanError("output_publish_failed") from exc
        published = True
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = ""
        os.fsync(directory_fd)
        _require_pinned_directory(directory_fd, parent_parts, parent_identities)
        try:
            final_status = os.stat(lexical.name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise RetentionPlanError("output_publish_raced") from exc
        if _inode_identity(final_status) != published_identity:
            raise RetentionPlanError("output_publish_raced")
        success = True
    finally:
        if not success and published and published_identity is not None:
            try:
                final_status = os.stat(lexical.name, dir_fd=directory_fd, follow_symlinks=False)
                if _inode_identity(final_status) == published_identity:
                    os.unlink(lexical.name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
            except OSError:
                pass
        if temporary:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan bounded Friday wheel-release retention")
    parser.add_argument("--activation-journal", required=True, type=Path)
    parser.add_argument("--unit-journal", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--inventory-root", required=True, action="append", type=Path)
    parser.add_argument("--backup-inventory-root", action="append", default=[], type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = plan_release_artifact_retention(
            activation_journal=args.activation_journal,
            unit_journal=args.unit_journal,
            backup_root=args.backup_root,
            inventory_roots=args.inventory_root,
            backup_inventory_roots=args.backup_inventory_root,
        )
        payload = _canonical_json(plan) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        else:
            _write_atomic(args.output, payload)
        return 0
    except RetentionPlanError as exc:
        failure = {
            "schema": PLAN_SCHEMA,
            "status": "failed_closed",
            "failure_code": str(exc),
        }
        sys.stderr.buffer.write(_canonical_json(failure) + b"\n")
        sys.stderr.buffer.flush()
        return 2


__all__ = [
    "AUTHORITY_BINDINGS_SCHEMA",
    "CanonicalEvidenceRoot",
    "DRGenerationPin",
    "INCOMPLETE_OPEN_INVENTORY",
    "OPEN_INVENTORY_SCHEMA",
    "OpenInventorySnapshot",
    "PLAN_SCHEMA",
    "RetentionPlanError",
    "RetentionAuthorityBindings",
    "plan_release_artifact_retention",
]


if __name__ == "__main__":
    raise SystemExit(main())
