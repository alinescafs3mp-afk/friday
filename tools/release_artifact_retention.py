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
import ctypes
import fcntl
import grp
import hashlib
import json
import os
import pwd
import stat
import subprocess
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
from tools import release_artifact_proc_probe as proc_probe  # noqa: E402
from tools import release_dr_generation_authentication as dr_auth  # noqa: E402
from tools import release_dr_generation_index as dr_index  # noqa: E402

PLAN_SCHEMA = "friday.release-artifact-retention-plan.v3"
AUTHORITY_BINDINGS_SCHEMA = "friday.release-artifact-retention-authority-bindings.v1"
OPEN_INVENTORY_SCHEMA = "friday.release-artifact-open-inventory.v1"
RETENTION_SCOPE_SCHEMA = "friday.release-artifact-retention-scope.v1"
RETENTION_SCOPE_NAME = "release-artifact-retention-scope.v1.json"
BOUNDED_DELETE_CONTOUR = "sealed-localfs-proc-mount-kernel-lease-global-lock-v1"
THREAT_BOUNDARY = "non_hostile_same_euid_and_root_admin_no_concurrent_open_attempts"
PRIVILEGED_PROC_HELPER = Path("/usr/libexec/friday/release_artifact_proc_probe.py")
PRIVILEGED_SCOPE_AUTHORITY = Path("/usr/libexec/friday/release_artifact_proc_scope.v1.json")
MAX_JOURNAL_BYTES = 1 << 20
MAX_RETENTION_SCOPE_BYTES = 1 << 20
MAX_RELEASE_MANIFEST_BYTES = 64 << 20
MAX_BACKUP_FILE_BYTES = 1 << 40
MAX_INVENTORY_ENTRIES = 1_000_000
MAX_INVENTORY_DEPTH = 256
MAX_DELETE_CANDIDATES_PER_PLAN = 16
MAX_DIRECT_INVENTORY_TARGETS = 65_536
MAX_AGGREGATE_INVENTORY_ENTRIES = 2_000_000
_AT_EMPTY_PATH = 0x1000
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_MNT_ID_UNIQUE = 0x4000
_FS_IOC_GETFLAGS = 0x80086601
_FS_IMMUTABLE_FL = 0x00000010
_FS_APPEND_FL = 0x00000020
_EXT4_SUPER_MAGIC = 0xEF53
_SUPPORTED_FILESYSTEM_MAGICS = frozenset({_EXT4_SUPER_MAGIC})

_HEX64 = frozenset("0123456789abcdef")
_OPEN_SOURCES = frozenset(
    {
        "unavailable",
        "code_owned_fd_inventory_v1",
        "code_owned_candidate_scope_seed_v1",
        "code_owned_privileged_target_proc_v1",
        "code_owned_privileged_target_diagnostic_v1",
        "code_owned_no_delete_candidates_v1",
        "synthetic_test",
    }
)
_APPLY_AUTHORITY_OPEN_SOURCES = frozenset(
    {
        "code_owned_privileged_target_proc_v1",
        "code_owned_privileged_target_diagnostic_v1",
    }
)
_SCRATCH_CONTOUR = "exact_owner_tree_without_git_v1"
# Reader-first release: no currently accepted receipt schema binds the full
# previous + fallback + exercised rollback release set needed for deletion.
# The writer package may add a pair only together with exact validators.
_DELETE_AUTHORITY_EVIDENCE_SCHEMA_PAIRS: frozenset[tuple[str, str]] = frozenset()
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
        "dr_rollback_release_evidence_incomplete",
        "dr_restore_release",
        "deferred_batch_bound",
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
        "retirable_authenticated_backup",
        "retirable_registered_legacy_worktree",
        "registered_legacy_requires_secondary_root",
        "retirable_reviewed_scratch",
        "special_artifact",
        "symlink_artifact",
        "unit_install_journal_invalid",
        "unit_install_journal_digest_mismatch",
        "unit_install_not_complete",
        "unsupported_filesystem",
        "retention_authority_unbound",
        "reviewed_scratch_invalid",
        "writable_artifact",
        "unknown_artifact",
    }
)


class RetentionPlanError(RuntimeError):
    """A closed planner-input failure safe to expose in a CLI receipt."""


@dataclass(frozen=True)
class OpenInventorySnapshot:
    """Injected bounded diagnostic open-path observation.

    The CLI intentionally cannot construct a complete snapshot.  Phase two can
    ``complete`` means the named diagnostic surfaces reached a fixed point; it
    is never a universal kernel absence or standalone deletion authority.
    """

    source: str
    complete: bool
    open_paths: tuple[Path, ...] = ()
    open_identities: tuple[tuple[int, int], ...] = ()
    authority_sha256: str = ""
    target_index_sha256: str = ""
    process_epoch_sha256: str = ""


INCOMPLETE_OPEN_INVENTORY = OpenInventorySnapshot(source="unavailable", complete=False)


@dataclass(frozen=True)
class DRGenerationPin:
    """One exact expected projection checked against the code-owned DR index."""

    role: str
    backup_directory: Path
    candidate: dict[str, Any]
    generation_id: str | None
    receipt_path: Path | None
    receipt_sha256: str | None
    authentication_receipt_path: Path | None
    authentication_receipt_sha256: str | None
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
class CanonicalEvidenceRoot:
    """An exact evidence root bound to one code-owned authority file."""

    path: Path
    authority_path: Path
    authority_sha256: str


@dataclass(frozen=True)
class ReviewedScratchTarget:
    """One operator-reviewed exact disposable tree, never a name/age inference."""

    path: Path
    inventory_sha256: str
    contour: str = _SCRATCH_CONTOUR


@dataclass(frozen=True)
class RetentionScopeAuthority:
    """One exact code-owned production retention scope registry."""

    receipt: dict[str, Any]
    backup_root: Path
    inventory_roots: tuple[Path, ...]
    backup_inventory_roots: tuple[Path, ...]
    canonical_evidence_roots: tuple[CanonicalEvidenceRoot, ...]


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
    records: tuple[tuple[str, int, int, int, int, int, int, int, int, int, int, int], ...]
    total_bytes: int
    total_allocated_bytes: int
    entry_count: int
    mount_id: int
    filesystem_magic: int
    owner_ok: bool
    has_symlink: bool
    has_special: bool
    has_hardlink: bool
    has_group_world_writable: bool
    writable_authority_sha256: str


@dataclass(frozen=True)
class _TargetObservation:
    path: Path
    device: int | None
    inode: int | None
    mount_id: int | None
    filesystem_magic: int | None
    mode: int | None
    kind: str
    nlink: int | None
    total_bytes: int | None
    total_allocated_bytes: int | None
    entry_count: int | None
    inventory_sha256: str | None
    object_identities: frozenset[tuple[int, int]]
    owner_ok: bool
    has_symlink: bool
    has_special: bool
    has_hardlink: bool
    has_group_world_writable: bool
    writable_authority_sha256: str | None
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
    dr_restore_release_records: Mapping[Path, Mapping[str, Any]]
    evidence_paths: frozenset[Path]
    reference_paths: frozenset[Path]
    delete_authority_eligible: bool
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


def _single_principal_group_authority(gid: int) -> str:
    """Authenticate a writable group whose only NSS principal is this euid."""

    try:
        current = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(gid)
        primary = {(entry.pw_uid, entry.pw_name) for entry in pwd.getpwall() if entry.pw_gid == gid}
        supplementary = {(pwd.getpwnam(name).pw_uid, name) for name in group.gr_mem}
    except (KeyError, OSError, UnicodeError):
        return ""
    principals = primary | supplementary
    if principals != {(os.geteuid(), current.pw_name)}:
        return ""
    projection = {
        "gid": gid,
        "group": group.gr_name,
        "principals": [[uid, name] for uid, name in sorted(principals)],
    }
    return hashlib.sha256(_canonical_json(projection)).hexdigest()


def _descriptor_has_posix_acl(descriptor: int) -> bool:
    try:
        names = os.listxattr(descriptor)
    except OSError as exc:
        raise RetentionPlanError("writable_authority_unavailable") from exc
    return any(name in {"system.posix_acl_access", "system.posix_acl_default"} for name in names)


def _writable_mode_authority(status: os.stat_result, *, has_acl: bool = False) -> str:
    permissions = stat.S_IMODE(status.st_mode)
    if has_acl or permissions & 0o002:
        return ""
    if permissions & 0o020:
        return _single_principal_group_authority(int(status.st_gid))
    return hashlib.sha256(b"friday-owner-only-write-v1").hexdigest()


def _strict_private_directory(path: Path, *, code: str) -> Path:
    lexical = _absolute_lexical(path, code=code)
    try:
        status = os.lstat(lexical)
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RetentionPlanError(code) from exc
    descriptor = -1
    try:
        descriptor = os.open(
            lexical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        has_acl = _descriptor_has_posix_acl(descriptor)
        opened = os.fstat(descriptor)
    except (OSError, RetentionPlanError) as exc:
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or _root_identity(status) != _root_identity(opened)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
        or has_acl
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
    descriptor = -1
    try:
        descriptor = os.open(
            lexical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        has_acl = _descriptor_has_posix_acl(descriptor)
    except (OSError, RetentionPlanError) as exc:
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError("inventory_root_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or _root_identity(status) != _root_identity(opened)
        or status.st_uid != os.geteuid()
        or not _writable_mode_authority(status, has_acl=has_acl)
    ):
        raise RetentionPlanError("inventory_root_invalid")
    return lexical, status


def _stable_file_bytes(
    path: Path,
    *,
    private: bool,
    code: str,
    maximum_bytes: int = MAX_JOURNAL_BYTES,
    allowed_nlinks: frozenset[int] = frozenset({1}),
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
        or before.st_nlink not in allowed_nlinks
        or before.st_uid != os.geteuid()
        or not 0 < before.st_size <= maximum_bytes
        or (private and stat.S_IMODE(before.st_mode) & 0o077)
    ):
        os.close(parent_fd)
        raise RetentionPlanError(code)
    # A pathname which passed the pre-open stat can still be replaced by a FIFO
    # before open(2).  O_NONBLOCK keeps that race fail-closed and bounded; the
    # descriptor and post-open identity checks below still require the exact
    # reviewed regular file.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
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


def _stable_file_sha256_streaming(
    path: Path,
    *,
    expected_size: int,
    private: bool,
    code: str,
) -> str:
    """Hash one exact private file without retaining its body in memory."""

    if not 0 <= expected_size <= MAX_BACKUP_FILE_BYTES:
        raise RetentionPlanError(code)
    lexical = _absolute_lexical(path, code=code)
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd, parts, identities = _open_absolute_directory_chain(lexical.parent, code=code)
        _require_pinned_directory(parent_fd, parts, identities, code=code, private=private)
        before = os.stat(lexical.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or (private and stat.S_IMODE(before.st_mode) & 0o077)
            or (not private and stat.S_IMODE(before.st_mode) & 0o022)
            or before.st_size != expected_size
        ):
            raise RetentionPlanError(code)
        descriptor = os.open(
            lexical.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RetentionPlanError(code)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise RetentionPlanError(code)
            digest.update(chunk)
        after_open = os.fstat(descriptor)
        after = os.stat(lexical.name, dir_fd=parent_fd, follow_symlinks=False)
        _require_pinned_directory(parent_fd, parts, identities, code=code, private=private)
    except (OSError, RetentionPlanError) as exc:
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
    identity = lambda item: (  # noqa: E731
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if (
        total != expected_size
        or identity(before) != identity(after_open)
        or identity(before) != identity(after)
    ):
        raise RetentionPlanError(code)
    return digest.hexdigest()


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


def load_retention_scope_authority(*, activation_journal: Path) -> RetentionScopeAuthority:
    """Load the sole exact production retention scope beside activation state."""

    code = "retention_scope_invalid"
    activation_path = _absolute_lexical(activation_journal, code=code)
    state_directory = _strict_private_directory(activation_path.parent, code=code)
    scope_path = state_directory / RETENTION_SCOPE_NAME
    try:
        before = os.lstat(scope_path)
        raw = _stable_file_bytes(
            scope_path,
            private=True,
            code=code,
            maximum_bytes=MAX_RETENTION_SCOPE_BYTES,
        )
        after = os.lstat(scope_path)
    except (OSError, RetentionPlanError) as exc:
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError(code) from exc
    stable_identity = lambda item: (  # noqa: E731
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    descriptor = -1
    try:
        descriptor = os.open(
            scope_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        has_acl = _descriptor_has_posix_acl(descriptor)
    except (OSError, RetentionPlanError) as exc:
        if isinstance(exc, RetentionPlanError):
            raise RetentionPlanError(code) from exc
        raise RetentionPlanError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stable_identity(before) != stable_identity(after)
        or stable_identity(after) != stable_identity(opened)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or has_acl
    ):
        raise RetentionPlanError(code)
    value = _unique_json(raw, code=code)
    expected_keys = {
        "backup_inventory_roots",
        "backup_root",
        "canonical_evidence_roots",
        "inventory_roots",
        "schema",
    }
    if raw != _canonical_json(value) + b"\n" or set(value) != expected_keys:
        raise RetentionPlanError(code)
    if value.get("schema") != RETENTION_SCOPE_SCHEMA:
        raise RetentionPlanError(code)

    def exact_paths(raw_paths: object, *, required: bool = True) -> tuple[Path, ...]:
        if not isinstance(raw_paths, list) or (required and not raw_paths):
            raise RetentionPlanError(code)
        paths = tuple(_absolute_lexical(Path(item), code=code) for item in raw_paths if isinstance(item, str))
        if len(paths) != len(raw_paths) or paths != tuple(sorted(set(paths), key=str)):
            raise RetentionPlanError(code)
        return paths

    inventory_roots = exact_paths(value.get("inventory_roots"))
    backup_inventory_roots = exact_paths(value.get("backup_inventory_roots"))
    backup_raw = value.get("backup_root")
    if not isinstance(backup_raw, str):
        raise RetentionPlanError(code)
    backup_root = _absolute_lexical(Path(backup_raw), code=code)
    evidence_raw = value.get("canonical_evidence_roots")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        raise RetentionPlanError(code)
    evidence: list[CanonicalEvidenceRoot] = []
    normalized_evidence: list[dict[str, str]] = []
    for item in evidence_raw:
        if not isinstance(item, Mapping) or set(item) != {
            "authority_path",
            "authority_sha256",
            "path",
        }:
            raise RetentionPlanError(code)
        path_raw = item.get("path")
        authority_raw = item.get("authority_path")
        authority_sha256 = item.get("authority_sha256")
        if (
            not isinstance(path_raw, str)
            or not isinstance(authority_raw, str)
            or not _is_hex64(authority_sha256)
        ):
            raise RetentionPlanError(code)
        entry = CanonicalEvidenceRoot(
            path=_absolute_lexical(Path(path_raw), code=code),
            authority_path=_absolute_lexical(Path(authority_raw), code=code),
            authority_sha256=str(authority_sha256),
        )
        evidence.append(entry)
        normalized_evidence.append(
            {
                "authority_path": str(entry.authority_path),
                "authority_sha256": entry.authority_sha256,
                "path": str(entry.path),
            }
        )
    if normalized_evidence != sorted(
        normalized_evidence,
        key=lambda item: (item["path"], item["authority_path"], item["authority_sha256"]),
    ) or len({(item.path, item.authority_path) for item in evidence}) != len(evidence):
        raise RetentionPlanError(code)
    receipt = {
        "device": int(after.st_dev),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "inode": int(after.st_ino),
        "path": str(scope_path),
        "schema": RETENTION_SCOPE_SCHEMA,
    }
    return RetentionScopeAuthority(
        receipt=receipt,
        backup_root=backup_root,
        inventory_roots=inventory_roots,
        backup_inventory_roots=backup_inventory_roots,
        canonical_evidence_roots=tuple(evidence),
    )


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


def _bounded_directory_names(
    directory: Path | int,
    *,
    maximum: int,
    code: str,
) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                names.append(entry.name)
                if len(names) > maximum:
                    raise RetentionPlanError(code)
    except OSError as exc:
        raise RetentionPlanError(code) from exc
    names.sort()
    return names


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("seconds", ctypes.c_int64),
        ("nanoseconds", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("block_size", ctypes.c_uint32),
        ("attributes", ctypes.c_uint64),
        ("nlink", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("inode", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("blocks", ctypes.c_uint64),
        ("attributes_mask", ctypes.c_uint64),
        ("atime", _StatxTimestamp),
        ("btime", _StatxTimestamp),
        ("ctime", _StatxTimestamp),
        ("mtime", _StatxTimestamp),
        ("rdev_major", ctypes.c_uint32),
        ("rdev_minor", ctypes.c_uint32),
        ("dev_major", ctypes.c_uint32),
        ("dev_minor", ctypes.c_uint32),
        ("mount_id", ctypes.c_uint64),
        ("dio_mem_align", ctypes.c_uint32),
        ("dio_offset_align", ctypes.c_uint32),
        ("subvolume", ctypes.c_uint64),
        ("atomic_write_unit_min", ctypes.c_uint32),
        ("atomic_write_unit_max", ctypes.c_uint32),
        ("atomic_write_segments_max", ctypes.c_uint32),
        ("spare1", ctypes.c_uint32),
        ("spare", ctypes.c_uint64 * 9),
    ]


class _Statfs(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_long),
        ("block_size", ctypes.c_long),
        ("blocks", ctypes.c_uint64),
        ("blocks_free", ctypes.c_uint64),
        ("blocks_available", ctypes.c_uint64),
        ("files", ctypes.c_uint64),
        ("files_free", ctypes.c_uint64),
        ("fsid", ctypes.c_int * 2),
        ("name_length", ctypes.c_long),
        ("fragment_size", ctypes.c_long),
        ("mount_flags", ctypes.c_long),
        ("spare", ctypes.c_long * 4),
    ]


def _descriptor_filesystem_identity(descriptor: int) -> tuple[int, int, int]:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fstatfs = libc.fstatfs
        fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_Statfs)]
        fstatfs.restype = ctypes.c_int
        result = _Statfs()
        if fstatfs(descriptor, ctypes.byref(result)) != 0:
            raise OSError(ctypes.get_errno(), "fstatfs")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise RetentionPlanError("filesystem_identity_unavailable") from exc
    mask = (1 << (ctypes.sizeof(ctypes.c_long) * 8)) - 1
    return (
        int(result.type) & mask,
        int(result.fsid[0]) & 0xFFFFFFFF,
        int(result.fsid[1]) & 0xFFFFFFFF,
    )


def _descriptor_filesystem_magic(descriptor: int) -> int:
    return _descriptor_filesystem_identity(descriptor)[0]


def _descriptor_inode_flags(descriptor: int) -> int:
    value = bytearray(4)
    try:
        fcntl.ioctl(descriptor, _FS_IOC_GETFLAGS, value, True)
    except OSError as exc:
        raise RetentionPlanError("inode_flags_unavailable") from exc
    return int.from_bytes(value, byteorder=sys.byteorder, signed=False)


def _descriptor_mount_id(descriptor: int) -> int:
    """Return the non-recycling Linux mount identity for one open descriptor."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        statx = libc.statx
        statx.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_Statx),
        ]
        statx.restype = ctypes.c_int
        result = _Statx()
        return_code = statx(
            descriptor,
            b"",
            _AT_EMPTY_PATH | _AT_SYMLINK_NOFOLLOW,
            _STATX_MNT_ID_UNIQUE,
            ctypes.byref(result),
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise RetentionPlanError("mount_identity_unavailable") from exc
    if return_code != 0 or not result.mask & _STATX_MNT_ID_UNIQUE or result.mount_id <= 0:
        raise RetentionPlanError("mount_identity_unavailable")
    return int(result.mount_id)


def _path_mount_id(path: Path, *, directory: bool) -> int:
    del directory
    flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        before = os.lstat(path)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        if _root_identity(before) != _root_identity(opened) or _root_identity(before) != _root_identity(
            after
        ):
            raise RetentionPlanError("raced_artifact")
        return _descriptor_mount_id(descriptor)
    except OSError as exc:
        raise RetentionPlanError("mount_identity_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_filesystem_magic(path: Path) -> int:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        return _descriptor_filesystem_magic(descriptor)
    except OSError as exc:
        raise RetentionPlanError("filesystem_identity_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _snapshot(path: Path) -> _TreeSnapshot:
    records: list[tuple[str, int, int, int, int, int, int, int, int, int, int, int]] = []
    writable_authorities: dict[int, str] = {}
    try:
        accounts = tuple(pwd.getpwall())
        groups = {entry.gr_gid: entry for entry in grp.getgrall()}
        account_by_name = {entry.pw_name: entry for entry in accounts}
        current = pwd.getpwuid(os.geteuid())
    except (KeyError, OSError, UnicodeError) as exc:
        raise RetentionPlanError("writable_authority_unavailable") from exc
    total_bytes = 0
    total_allocated_bytes = 0
    owner_ok = True
    has_symlink = False
    has_special = False
    has_hardlink = False
    has_group_world_writable = False
    root_status = os.lstat(path)
    root_device = int(root_status.st_dev)
    pending: list[tuple[int, str]] = []

    def writable_authority(status: os.stat_result) -> str:
        permissions = stat.S_IMODE(status.st_mode)
        if permissions & 0o002:
            return ""
        if not permissions & 0o020:
            return hashlib.sha256(b"friday-owner-only-write-v1").hexdigest()
        gid = int(status.st_gid)
        if gid in writable_authorities:
            return writable_authorities[gid]
        group = groups.get(gid)
        if group is None:
            writable_authorities[gid] = ""
            return ""
        primary = {(entry.pw_uid, entry.pw_name) for entry in accounts if entry.pw_gid == gid}
        try:
            supplementary = {
                (account_by_name[name].pw_uid, name) for name in group.gr_mem if name in account_by_name
            }
        except (KeyError, UnicodeError):
            writable_authorities[gid] = ""
            return ""
        principals = primary | supplementary
        if principals != {(os.geteuid(), current.pw_name)} or len(supplementary) != len(group.gr_mem):
            writable_authorities[gid] = ""
            return ""
        projection = {
            "gid": gid,
            "group": group.gr_name,
            "principals": [[uid, name] for uid, name in sorted(principals)],
        }
        authority = hashlib.sha256(_canonical_json(projection)).hexdigest()
        writable_authorities[gid] = authority
        return authority

    def observe(
        relative: str,
        status: os.stat_result,
        mount_id: int,
        has_acl: bool,
        inode_flags: int | None,
    ) -> None:
        nonlocal total_bytes, total_allocated_bytes, owner_ok, has_symlink, has_special, has_hardlink
        nonlocal has_group_world_writable
        if relative != "." and relative.count("/") + 1 > MAX_INVENTORY_DEPTH:
            raise RetentionPlanError("inventory_target_too_deep")
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
                mount_id,
                int(status.st_gid),
                -1 if inode_flags is None else inode_flags,
            )
        )
        if len(records) > MAX_INVENTORY_ENTRIES:
            raise RetentionPlanError("inventory_target_too_large")
        owner_ok = owner_ok and status.st_uid == os.geteuid()
        has_special = (
            has_special
            or int(status.st_dev) != root_device
            or mount_id != root_mount_id
            or inode_flags is None
            or bool(inode_flags & (_FS_IMMUTABLE_FL | _FS_APPEND_FL))
        )
        mode_authority = writable_authority(status)
        has_group_world_writable = has_group_world_writable or not mode_authority or has_acl
        total_allocated_bytes += int(status.st_blocks) * 512
        if kind == "regular":
            total_bytes += int(status.st_size)
            has_hardlink = has_hardlink or status.st_nlink != 1
        elif kind == "symlink":
            has_symlink = True
        elif kind != "directory":
            has_special = True

    root_mount_id = _path_mount_id(path, directory=stat.S_ISDIR(root_status.st_mode))
    root_filesystem_magic = _path_filesystem_magic(path)
    root_inode_flags: int | None
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
        if _descriptor_mount_id(root_descriptor) != root_mount_id:
            os.close(root_descriptor)
            raise RetentionPlanError("raced_artifact")
        pending.append((root_descriptor, "."))
        root_acl = _descriptor_has_posix_acl(root_descriptor)
        root_inode_flags = _descriptor_inode_flags(root_descriptor)
    else:
        root_acl_descriptor = os.open(
            path,
            (
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                if stat.S_ISREG(root_status.st_mode)
                else getattr(os, "O_PATH", os.O_RDONLY)
            )
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            is_regular = stat.S_ISREG(root_status.st_mode)
            root_acl = not is_regular or _descriptor_has_posix_acl(root_acl_descriptor)
            root_inode_flags = _descriptor_inode_flags(root_acl_descriptor) if is_regular else None
        finally:
            os.close(root_acl_descriptor)
    observe(".", root_status, root_mount_id, root_acl, root_inode_flags)
    try:
        while pending:
            descriptor, relative = pending.pop()
            try:
                before_directory = os.fstat(descriptor)
                children = list(
                    reversed(
                        _bounded_directory_names(
                            descriptor,
                            maximum=MAX_INVENTORY_ENTRIES - len(records),
                            code="inventory_target_too_large",
                        )
                    )
                )
                for name in children:
                    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    child_relative = name if relative == "." else f"{relative}/{name}"
                    if stat.S_ISDIR(status.st_mode):
                        child_descriptor = os.open(name, flags, dir_fd=descriptor)
                        try:
                            opened = os.fstat(child_descriptor)
                            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                            if _root_identity(opened) != _root_identity(status) or _root_identity(
                                named
                            ) != _root_identity(status):
                                raise RetentionPlanError("raced_artifact")
                            child_mount_id = _descriptor_mount_id(child_descriptor)
                        except BaseException:
                            os.close(child_descriptor)
                            raise
                        observe(
                            child_relative,
                            status,
                            child_mount_id,
                            _descriptor_has_posix_acl(child_descriptor),
                            _descriptor_inode_flags(child_descriptor),
                        )
                        if int(status.st_dev) == root_device and child_mount_id == root_mount_id:
                            pending.append((child_descriptor, child_relative))
                        else:
                            os.close(child_descriptor)
                    else:
                        path_descriptor = os.open(
                            name,
                            (
                                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                                if stat.S_ISREG(status.st_mode)
                                else getattr(os, "O_PATH", os.O_RDONLY)
                            )
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=descriptor,
                        )
                        try:
                            opened = os.fstat(path_descriptor)
                            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                            if _root_identity(opened) != _root_identity(status) or _root_identity(
                                named
                            ) != _root_identity(status):
                                raise RetentionPlanError("raced_artifact")
                            child_mount_id = _descriptor_mount_id(path_descriptor)
                            child_acl = not stat.S_ISREG(status.st_mode) or _descriptor_has_posix_acl(
                                path_descriptor
                            )
                            child_inode_flags = (
                                _descriptor_inode_flags(path_descriptor)
                                if stat.S_ISREG(status.st_mode)
                                else None
                            )
                        finally:
                            os.close(path_descriptor)
                        observe(
                            child_relative,
                            status,
                            child_mount_id,
                            child_acl,
                            child_inode_flags,
                        )
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
        total_allocated_bytes=total_allocated_bytes,
        entry_count=len(records),
        mount_id=root_mount_id,
        filesystem_magic=root_filesystem_magic,
        owner_ok=owner_ok,
        has_symlink=has_symlink,
        has_special=has_special,
        has_hardlink=has_hardlink,
        has_group_world_writable=has_group_world_writable,
        writable_authority_sha256=hashlib.sha256(
            _canonical_json([[gid, authority] for gid, authority in sorted(writable_authorities.items())])
        ).hexdigest(),
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
                mount_id=None,
                filesystem_magic=None,
                mode=None,
                kind="unknown",
                nlink=None,
                total_bytes=None,
                total_allocated_bytes=None,
                entry_count=None,
                inventory_sha256=None,
                object_identities=frozenset(),
                owner_ok=False,
                has_symlink=False,
                has_special=False,
                has_hardlink=False,
                has_group_world_writable=False,
                writable_authority_sha256=None,
                raced=True,
            )
        return _TargetObservation(
            path=path,
            device=int(status.st_dev),
            inode=int(status.st_ino),
            mount_id=None,
            filesystem_magic=None,
            mode=int(status.st_mode),
            kind=_kind(status.st_mode),
            nlink=int(status.st_nlink),
            total_bytes=None,
            total_allocated_bytes=None,
            entry_count=None,
            inventory_sha256=None,
            object_identities=frozenset({(int(status.st_dev), int(status.st_ino))}),
            owner_ok=status.st_uid == os.geteuid(),
            has_symlink=stat.S_ISLNK(status.st_mode),
            has_special=not (
                stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode)
            ),
            has_hardlink=stat.S_ISREG(status.st_mode) and status.st_nlink != 1,
            has_group_world_writable=bool(stat.S_IMODE(status.st_mode) & 0o022),
            writable_authority_sha256=None,
            raced=True,
        )
    root = next(record for record in first.records if record[0] == ".")
    return _TargetObservation(
        path=path,
        device=root[1],
        inode=root[2],
        mount_id=root[9],
        filesystem_magic=first.filesystem_magic,
        mode=root[3],
        kind=_kind(root[3]),
        nlink=root[4],
        total_bytes=first.total_bytes,
        total_allocated_bytes=first.total_allocated_bytes,
        entry_count=first.entry_count,
        inventory_sha256=hashlib.sha256(_canonical_json(first.records)).hexdigest(),
        object_identities=frozenset((record[1], record[2]) for record in first.records),
        owner_ok=first.owner_ok,
        has_symlink=first.has_symlink,
        has_special=first.has_special,
        has_hardlink=first.has_hardlink,
        has_group_world_writable=first.has_group_world_writable,
        writable_authority_sha256=first.writable_authority_sha256,
        raced=first != second,
    )


def _normalize_open_inventory(
    snapshot: OpenInventorySnapshot,
) -> tuple[dict[str, Any], frozenset[Path], frozenset[tuple[int, int]]]:
    if snapshot.source not in _OPEN_SOURCES or type(snapshot.complete) is not bool:
        raise RetentionPlanError("open_inventory_invalid")
    if snapshot.source == "unavailable" and (
        snapshot.complete
        or snapshot.open_paths
        or snapshot.open_identities
        or snapshot.authority_sha256
        or snapshot.target_index_sha256
        or snapshot.process_epoch_sha256
    ):
        raise RetentionPlanError("open_inventory_invalid")
    if snapshot.source.startswith("code_owned_") and not snapshot.complete:
        raise RetentionPlanError("open_inventory_invalid")
    privileged = snapshot.source in {
        "code_owned_privileged_target_proc_v1",
        "code_owned_privileged_target_diagnostic_v1",
    }
    no_candidates = snapshot.source == "code_owned_no_delete_candidates_v1"
    metadata = (
        snapshot.authority_sha256,
        snapshot.target_index_sha256,
        snapshot.process_epoch_sha256,
    )
    if privileged:
        if not all(_is_hex64(value) for value in metadata):
            raise RetentionPlanError("open_inventory_invalid")
    elif no_candidates:
        if (
            not snapshot.complete
            or snapshot.open_paths
            or snapshot.open_identities
            or not _is_hex64(snapshot.authority_sha256)
            or snapshot.target_index_sha256
            or snapshot.process_epoch_sha256
        ):
            raise RetentionPlanError("open_inventory_invalid")
    elif any(metadata):
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
        "authority_sha256": snapshot.authority_sha256,
        "target_index_sha256": snapshot.target_index_sha256,
        "process_epoch_sha256": snapshot.process_epoch_sha256,
        "observation_role": "diagnostic_prerequisite",
        "universal_absence_proof": False,
    }
    return (
        {
            "schema": OPEN_INVENTORY_SCHEMA,
            "source": snapshot.source,
            "complete": snapshot.complete,
            "open_path_count": len(paths),
            "open_identity_count": len(identities),
            "authority_sha256": snapshot.authority_sha256,
            "target_index_sha256": snapshot.target_index_sha256,
            "process_epoch_sha256": snapshot.process_epoch_sha256,
            "observation_role": "diagnostic_prerequisite",
            "universal_absence_proof": False,
            "snapshot_sha256": hashlib.sha256(_canonical_json(core)).hexdigest(),
        },
        frozenset(paths),
        frozenset(identities),
    )


def _normalize_reviewed_scratch_targets(
    values: Sequence[ReviewedScratchTarget],
    *,
    inventory_roots: frozenset[Path],
) -> dict[Path, ReviewedScratchTarget]:
    if len(values) > 512:
        raise RetentionPlanError("reviewed_scratch_invalid")
    result: dict[Path, ReviewedScratchTarget] = {}
    for value in values:
        if (
            not isinstance(value, ReviewedScratchTarget)
            or value.contour != _SCRATCH_CONTOUR
            or not _is_hex64(value.inventory_sha256)
        ):
            raise RetentionPlanError("reviewed_scratch_invalid")
        path = _absolute_lexical(value.path, code="reviewed_scratch_invalid")
        if path.parent not in inventory_roots or path.name in {"", ".", ".."} or path in result:
            raise RetentionPlanError("reviewed_scratch_invalid")
        result[path] = ReviewedScratchTarget(path, value.inventory_sha256, value.contour)
    return result


def _reviewed_scratch_identity(
    observation: _TargetObservation,
    authority: ReviewedScratchTarget,
) -> dict[str, Any] | None:
    """Authenticate one explicit full-tree review without trusting its basename."""

    if (
        observation.path != authority.path
        or observation.kind != "directory"
        or observation.raced
        or observation.inventory_sha256 != authority.inventory_sha256
        or not observation.owner_ok
        or observation.has_symlink
        or observation.has_special
        or observation.has_hardlink
        or observation.has_group_world_writable
    ):
        return None
    try:
        snapshot = _snapshot(observation.path)
        snapshot_after = _snapshot(observation.path)
    except (OSError, RetentionPlanError):
        return None
    if snapshot != snapshot_after:
        return None
    # Git worktrees have independent registration lifecycle and are handled by
    # the exact registered-worktree contour.  A nested marker is rejected too,
    # so deleting an aggregate scratch root cannot orphan Git metadata.
    if any(Path(record[0]).name == ".git" for record in snapshot.records if record[0] != "."):
        return None
    digest = hashlib.sha256(_canonical_json(snapshot.records)).hexdigest()
    if digest != authority.inventory_sha256:
        return None
    return {
        "contour": authority.contour,
        "entry_count": snapshot.entry_count,
        "inventory_sha256": digest,
        "recursive_bytes": snapshot.total_bytes,
        "allocated_bytes": snapshot.total_allocated_bytes,
    }


def _full_rollback_release_evidence_complete(
    authentication_receipt: Mapping[str, Any],
    rehearsal_receipt: Mapping[str, Any],
) -> bool:
    """Reject legacy evidence until the full-root writer contract exists.

    The v1 pair authenticates the backup and one rollback tree, but does not
    bind the complete previous, fallback, and exercised release identities.
    Unknown future schemas also fail closed; the writer package must add one
    exact, fully validated contract here before it can authorize deletion.
    """

    schemas = (
        authentication_receipt.get("schema"),
        rehearsal_receipt.get("schema"),
    )
    return schemas in _DELETE_AUTHORITY_EVIDENCE_SCHEMA_PAIRS


def _normalize_authority_bindings(
    bindings: RetentionAuthorityBindings | None,
    *,
    activation_sha256: str,
    unit_sha256: str,
    state_directory: Path,
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
            dr_restore_release_records={},
            evidence_paths=frozenset(),
            reference_paths=frozenset(),
            delete_authority_eligible=False,
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

    canonical_state = _strict_private_directory(state_directory, code="dr_index_invalid")
    dr_index_path = _absolute_lexical(bindings.dr_index_path, code="dr_pins_invalid")
    if dr_index_path != canonical_state / dr_index.INDEX_NAME:
        raise RetentionPlanError("dr_pins_invalid")
    if not _is_hex64(bindings.dr_index_sha256):
        raise RetentionPlanError("dr_pins_invalid")
    observed_dr_index_sha256 = ""
    actual_pins: tuple[DRGenerationPin, ...] = ()
    generation_index: dr_index.DurableDRGenerationIndex | None = None
    try:
        generation_index = dr_index.DurableDRGenerationIndex(canonical_state)
        dr_snapshot = generation_index.authority_snapshot()
        if dr_snapshot.index_path != dr_index_path:
            raise RetentionPlanError("dr_index_invalid")
        observed_dr_index_sha256 = dr_snapshot.index_sha256
        if observed_dr_index_sha256 != bindings.dr_index_sha256:
            error = error or "dr_index_invalid"
        actual_pins = tuple(
            DRGenerationPin(
                role=pin.role,
                backup_directory=pin.backup_directory,
                candidate=dict(pin.candidate),
                generation_id=pin.generation_id,
                receipt_path=pin.receipt_path,
                receipt_sha256=pin.receipt_sha256,
                authentication_receipt_path=pin.authentication_receipt_path,
                authentication_receipt_sha256=pin.authentication_receipt_sha256,
                rehearsal_receipt_path=pin.rehearsal_receipt_path,
                rehearsal_receipt_sha256=pin.rehearsal_receipt_sha256,
                rehearsal_binding=(
                    dict(pin.rehearsal_binding) if pin.rehearsal_binding is not None else None
                ),
                restore_release_root=pin.restore_release_root,
                restore_release_commit=pin.restore_release_commit,
                restore_release_tree_manifest_sha256=(pin.restore_release_tree_manifest_sha256),
                restore_release_wheel_sha256=pin.restore_release_wheel_sha256,
                restore_release_max_schema=pin.restore_release_max_schema,
                restore_release_version=pin.restore_release_version,
            )
            for pin in dr_snapshot.pins
        )
    except (RetentionPlanError, dr_index.DRGenerationIndexError):
        error = error or "dr_index_invalid"
    if any(not isinstance(pin, DRGenerationPin) for pin in bindings.dr_pins):
        raise RetentionPlanError("dr_pins_invalid")
    if bindings.dr_pins != actual_pins:
        error = error or "dr_pins_invalid"

    role_paths: dict[str, Path] = {}
    backup_restore_identities: dict[Path, Mapping[str, Any]] = {}
    restore_release_records: dict[Path, Mapping[str, Any]] = {}
    pin_records: list[dict[str, Any]] = []
    generation_ids: set[str] = set()
    receipt_paths: set[Path] = set()
    evidence_receipt_paths: set[Path] = set()
    reauthenticated_candidate_sha256s: set[str] = set()
    complete_delete_evidence_roles: set[str] = set()
    incomplete_delete_evidence = False
    for pin in actual_pins:
        if pin.role not in {"current", "older", "pending"}:
            raise RetentionPlanError("dr_pins_invalid")
        if pin.role in role_paths:
            raise RetentionPlanError("dr_pins_invalid")
        backup_directory = _absolute_lexical(pin.backup_directory, code="dr_pins_invalid")
        for existing in role_paths.values():
            if backup_directory == existing:
                continue
            if backup_directory in existing.parents or existing in backup_directory.parents:
                error = error or "dr_pins_invalid"
        role_paths[pin.role] = backup_directory

        try:
            candidate = dr_index.normalize_generation_candidate(pin.candidate)
        except dr_index.DRGenerationIndexError:
            error = error or "dr_pins_invalid"
            candidate = {}
        candidate_sha256 = hashlib.sha256(_canonical_json(candidate)).hexdigest() if candidate else ""
        if candidate and candidate.get("backup_directory") != str(backup_directory):
            error = error or "dr_pins_invalid"

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
        observed_receipt_file_sha256 = ""
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
                observed_receipt_file_sha256 = _stable_file_sha256(
                    receipt_path,
                    private=True,
                    code="dr_pins_invalid",
                )
            except RetentionPlanError:
                error = error or "dr_pins_invalid"
        authentication_path: Path | None = None
        rehearsal_path: Path | None = None
        authentication_body: dict[str, Any] | None = None
        authentication_reference: dict[str, str] | None = None
        rehearsal_body: dict[str, Any] | None = None
        rehearsal_reference: dict[str, str] | None = None
        if pin.authentication_receipt_path is not None:
            if not _is_hex64(pin.authentication_receipt_sha256) or generation_index is None:
                error = error or "dr_pins_invalid"
            else:
                authentication_path = _absolute_lexical(
                    pin.authentication_receipt_path,
                    code="dr_pins_invalid",
                )
                expected_authentication_path = generation_index.receipt_directory / (
                    f"authentication-{pin.authentication_receipt_sha256}.json"
                )
                if (
                    authentication_path != expected_authentication_path
                    or authentication_path in evidence_receipt_paths
                ):
                    error = error or "dr_pins_invalid"
                evidence_receipt_paths.add(authentication_path)
                try:
                    authentication_raw = _stable_file_bytes(
                        authentication_path,
                        private=True,
                        code="dr_pins_invalid",
                    )
                    authentication_body = _unique_json(
                        authentication_raw,
                        code="dr_pins_invalid",
                    )
                    if authentication_raw != _canonical_json(authentication_body) + b"\n":
                        raise RetentionPlanError("dr_pins_invalid")
                    authentication_reference, _raw, _payload = dr_index.validate_authentication_receipt(
                        authentication_body,
                        candidate=candidate,
                    )
                    if authentication_reference["sha256"] != pin.authentication_receipt_sha256:
                        raise RetentionPlanError("dr_pins_invalid")
                    dr_auth.reauthenticate_generation_candidate(
                        candidate=candidate,
                        authentication_receipt=authentication_body,
                    )
                    reauthenticated_candidate_sha256s.add(candidate_sha256)
                except (
                    RetentionPlanError,
                    dr_auth.DRGenerationAuthenticationError,
                    dr_index.DRGenerationIndexError,
                ):
                    error = error or "dr_pins_invalid"
        elif pin.authentication_receipt_sha256 is not None:
            error = error or "dr_pins_invalid"
        if pin.rehearsal_receipt_path is not None:
            if not _is_hex64(pin.rehearsal_receipt_sha256) or generation_index is None:
                error = error or "dr_pins_invalid"
            else:
                rehearsal_path = _absolute_lexical(
                    pin.rehearsal_receipt_path,
                    code="dr_pins_invalid",
                )
                expected_rehearsal_path = generation_index.receipt_directory / (
                    f"rehearsal-{pin.rehearsal_receipt_sha256}.json"
                )
                if rehearsal_path != expected_rehearsal_path or rehearsal_path in evidence_receipt_paths:
                    error = error or "dr_pins_invalid"
                evidence_receipt_paths.add(rehearsal_path)
                try:
                    rehearsal_raw = _stable_file_bytes(
                        rehearsal_path,
                        private=True,
                        code="dr_pins_invalid",
                    )
                    rehearsal_body = _unique_json(rehearsal_raw, code="dr_pins_invalid")
                    if rehearsal_raw != _canonical_json(rehearsal_body) + b"\n":
                        raise RetentionPlanError("dr_pins_invalid")
                    if authentication_body is None:
                        raise RetentionPlanError("dr_pins_invalid")
                    if authentication_reference is None or pin.rehearsal_binding is None:
                        raise RetentionPlanError("dr_pins_invalid")
                    rehearsal_binding = dr_index._normalize_rehearsal_binding(  # noqa: SLF001
                        pin.rehearsal_binding,
                        candidate=candidate,
                        authentication_receipt=authentication_reference,
                    )
                    rehearsal_reference, _raw, _payload = dr_index.validate_rehearsal_receipt(
                        rehearsal_body,
                        candidate=candidate,
                        authentication_receipt=authentication_body,
                        index_transaction_id=rehearsal_binding["index_transaction_id"],
                        index_revision=rehearsal_binding["index_revision"],
                        index_journal_sha256=rehearsal_binding["index_journal_sha256"],
                    )
                    if rehearsal_reference["sha256"] != pin.rehearsal_receipt_sha256:
                        raise RetentionPlanError("dr_pins_invalid")
                except (RetentionPlanError, dr_index.DRGenerationIndexError):
                    error = error or "dr_pins_invalid"
        elif pin.rehearsal_receipt_sha256 is not None or pin.rehearsal_binding is not None:
            error = error or "dr_pins_invalid"
        if (
            authentication_reference is not None
            and rehearsal_reference is not None
            and authentication_body is not None
            and rehearsal_body is not None
            and _full_rollback_release_evidence_complete(
                authentication_body,
                rehearsal_body,
            )
        ):
            complete_delete_evidence_roles.add(pin.role)
        else:
            incomplete_delete_evidence = True
        if pin.role in {"current", "older"} and (
            authentication_path is None or rehearsal_path is None or pin.rehearsal_binding is None
        ):
            error = error or "dr_pins_invalid"
        if (
            pin.role == "pending"
            and authentication_path is None
            and candidate_sha256 not in reauthenticated_candidate_sha256s
        ):
            # A prepared-but-unauthenticated backup remains retained, but cannot
            # grant any classification/deletion authority.
            error = error or "dr_pins_invalid"
        try:
            _strict_private_directory(backup_directory, code="dr_pins_invalid")
        except RetentionPlanError:
            error = error or "dr_pins_invalid"
        restore_root = _absolute_lexical(pin.restore_release_root, code="dr_pins_invalid")
        restore_record: dict[str, Any] = {
            "commit": pin.restore_release_commit,
            "max_schema": pin.restore_release_max_schema,
            "root": str(restore_root),
            "tree_manifest_sha256": pin.restore_release_tree_manifest_sha256,
            "version": pin.restore_release_version,
        }
        backup_restore_identity = {
            **restore_record,
            "wheel_sha256": pin.restore_release_wheel_sha256,
        }
        existing_backup_restore = backup_restore_identities.get(backup_directory)
        if existing_backup_restore is not None and dict(existing_backup_restore) != backup_restore_identity:
            error = error or "dr_pins_invalid"
        backup_restore_identities[backup_directory] = backup_restore_identity
        existing_restore = restore_release_records.get(restore_root)
        if existing_restore is not None and dict(existing_restore) != restore_record:
            error = error or "dr_pins_invalid"
        restore_release_records[restore_root] = restore_record
        try:
            authenticated_restore = _authenticate_release(restore_record)
            if authenticated_restore["wheel_sha256"] != pin.restore_release_wheel_sha256:
                raise RetentionPlanError("protected_release_authentication_failed")
        except RetentionPlanError:
            error = error or "dr_pins_invalid"
        pin_records.append(
            {
                "role": pin.role,
                "backup_directory": str(backup_directory),
                "generation_id": pin.generation_id,
                "receipt_path": str(receipt_path) if receipt_path is not None else None,
                "receipt_sha256": pin.receipt_sha256,
                "observed_receipt_file_sha256": observed_receipt_file_sha256,
                "candidate_sha256": candidate_sha256,
                "authentication_receipt_path": (
                    str(authentication_path) if authentication_path is not None else None
                ),
                "authentication_receipt_sha256": pin.authentication_receipt_sha256,
                "rehearsal_receipt_path": str(rehearsal_path) if rehearsal_path is not None else None,
                "rehearsal_receipt_sha256": pin.rehearsal_receipt_sha256,
                "rehearsal_binding": pin.rehearsal_binding,
                "restore_release": {
                    **restore_record,
                    "wheel_sha256": pin.restore_release_wheel_sha256,
                },
            }
        )
    if not {"current", "older"}.issubset(role_paths):
        error = error or "dr_pins_unavailable"
    delete_authority_eligible = not incomplete_delete_evidence and {"current", "older"}.issubset(
        complete_delete_evidence_roles
    )

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
        dr_restore_release_records=restore_release_records,
        evidence_paths=frozenset(evidence_paths),
        reference_paths=frozenset(
            {
                dr_index_path,
                *role_paths.values(),
                *restore_release_records,
                *receipt_paths,
                *evidence_receipt_paths,
                *((generation_index.receipt_directory,) if generation_index is not None else ()),
                *evidence_paths,
                *evidence_authority_paths,
            }
        ),
        delete_authority_eligible=delete_authority_eligible,
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


def _discover_exact_backup(path: Path) -> dict[str, Any] | None:
    manifest_path = path / "manifest.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    try:
        _strict_private_directory(path, code="legacy_or_unknown_backup")
        manifest_raw = _stable_file_bytes(
            manifest_path,
            private=True,
            code="legacy_or_unknown_backup",
        )
        manifest = _unique_json(manifest_raw, code="legacy_or_unknown_backup")
        if manifest_raw != _canonical_json(manifest) + b"\n" or set(manifest) != {
            "database_schema",
            "files",
            "schema",
        }:
            raise RetentionPlanError("legacy_or_unknown_backup")
        schema_version = manifest.get("database_schema")
        files = manifest.get("files")
        if (
            manifest.get("schema") != "friday.immutable-cutover-exact-backup.v1"
            or type(schema_version) is not int
            or schema_version <= 0
            or not isinstance(files, list)
            or not files
        ):
            raise RetentionPlanError("legacy_or_unknown_backup")
        allowed_names = {
            "database.sqlite3",
            "database.sqlite3-wal",
            "inbox.sqlite3",
            "inbox.sqlite3-wal",
        }
        normalized_files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
                raise RetentionPlanError("legacy_or_unknown_backup")
            name = item.get("name")
            digest = item.get("sha256")
            size = item.get("size")
            if (
                not isinstance(name, str)
                or name not in allowed_names
                or name in seen
                or not _is_hex64(digest)
                or type(size) is not int
                or size < 0
            ):
                raise RetentionPlanError("legacy_or_unknown_backup")
            observed_digest = _stable_file_sha256_streaming(
                path / name,
                expected_size=size,
                private=True,
                code="legacy_or_unknown_backup",
            )
            if observed_digest != digest:
                raise RetentionPlanError("legacy_or_unknown_backup")
            seen.add(name)
            normalized_files.append({"name": name, "sha256": digest, "size": size})
        normalized_files.sort(key=lambda item: str(item["name"]))
        if files != normalized_files or not {"database.sqlite3", "inbox.sqlite3"}.issubset(seen):
            raise RetentionPlanError("legacy_or_unknown_backup")

        obsidian_raw = _stable_file_bytes(
            path / "obsidian-manifest.json",
            private=True,
            code="legacy_or_unknown_backup",
        )
        obsidian = _unique_json(obsidian_raw, code="legacy_or_unknown_backup")
        if obsidian_raw != _canonical_json(obsidian) + b"\n":
            raise RetentionPlanError("legacy_or_unknown_backup")
        obsidian_files = obsidian.get("files")
        if not isinstance(obsidian_files, list) or type(obsidian.get("present")) is not bool:
            raise RetentionPlanError("legacy_or_unknown_backup")
        release_api: Any = release_operator
        obsidian_type: Any = release_api._ExactObsidianBackup  # noqa: SLF001
        verify_obsidian: Any = release_api._verify_obsidian_backup  # noqa: SLF001
        obsidian_descriptor = obsidian_type(
            present=obsidian["present"],
            manifest_sha256=hashlib.sha256(obsidian_raw).hexdigest(),
            file_count=len(obsidian_files),
            total_bytes=sum(
                int(item.get("size", -1)) if isinstance(item, Mapping) else -1 for item in obsidian_files
            ),
        )
        verify_obsidian(path, obsidian_descriptor)

        engineer_raw = _stable_file_bytes(
            path / "engineer-manifest.json",
            private=True,
            code="legacy_or_unknown_backup",
        )
        engineer = _unique_json(engineer_raw, code="legacy_or_unknown_backup")
        if engineer_raw != _canonical_json(engineer) + b"\n":
            raise RetentionPlanError("legacy_or_unknown_backup")
        engineer_type: Any = release_api._ExactEngineerBackup  # noqa: SLF001
        verify_engineer: Any = release_api._verify_engineer_backup  # noqa: SLF001
        engineer_descriptor = engineer_type(
            manifest_sha256=hashlib.sha256(engineer_raw).hexdigest(),
            entry_count=engineer.get("entry_count"),
            total_bytes=engineer.get("total_bytes"),
            store_present=engineer.get("store_present"),
            key_present=engineer.get("key_present"),
        )
        verify_engineer(path, engineer_descriptor, verify_sqlite_integrity=False)
        expected_names = seen | {
            "manifest.json",
            "obsidian-manifest.json",
            "engineer-manifest.json",
            "engineer-recovery",
        }
        if obsidian["present"]:
            expected_names.add("obsidian-root")
        actual_names = {entry.name for entry in os.scandir(path)}
        if actual_names != expected_names:
            raise RetentionPlanError("legacy_or_unknown_backup")
        return {
            "database_schema": schema_version,
            "engineer_manifest_sha256": hashlib.sha256(engineer_raw).hexdigest(),
            "file_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "obsidian_manifest_sha256": hashlib.sha256(obsidian_raw).hexdigest(),
            "schema": "friday.immutable-cutover-exact-backup.v1",
        }
    except (
        AttributeError,
        OSError,
        RetentionPlanError,
        TypeError,
        ValueError,
        release_operator.ReleaseFailure,
    ):
        return {"invalid": True}


def _git_command(path: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(  # noqa: S603
        [
            "/usr/bin/git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(path),
            *arguments,
        ],
        check=False,
        capture_output=True,
        env=environment,
        timeout=10,
    )


def _git_admin_manifest(path: Path, *, omit_locked: bool = False) -> list[dict[str, Any]]:
    try:
        _strict_inventory_root(path)
        snapshot = _snapshot(path)
    except (OSError, RetentionPlanError) as exc:
        raise RetentionPlanError("legacy_worktree_invalid") from exc
    if (
        not snapshot.owner_ok
        or snapshot.has_symlink
        or snapshot.has_special
        or snapshot.has_hardlink
        or snapshot.has_group_world_writable
        or snapshot.entry_count > MAX_INVENTORY_ENTRIES
    ):
        raise RetentionPlanError("legacy_worktree_invalid")
    records: list[dict[str, Any]] = []
    for item in snapshot.records:
        relative = str(item[0])
        mode = int(item[3])
        if relative == "locked" and omit_locked:
            continue
        if stat.S_IMODE(mode) & 0o022:
            raise RetentionPlanError("legacy_worktree_invalid")
        record: dict[str, Any] = {
            "device": int(item[1]),
            "inode": int(item[2]),
            "kind": "directory" if stat.S_ISDIR(mode) else "regular",
            "mode": stat.S_IMODE(mode),
            "path": relative,
        }
        if stat.S_ISREG(mode):
            size = int(item[6])
            record["sha256"] = _stable_file_sha256_streaming(
                path / relative,
                expected_size=size,
                private=False,
                code="legacy_worktree_invalid",
            )
            record["size"] = size
        elif not stat.S_ISDIR(mode):
            raise RetentionPlanError("legacy_worktree_invalid")
        records.append(record)
    records.sort(key=lambda item: str(item["path"]))
    return records


def _discover_registered_legacy_worktree(path: Path) -> dict[str, Any] | None:
    marker = path / ".git"
    if not marker.exists() and not marker.is_symlink():
        return None
    try:
        marker_raw = _stable_file_bytes(
            marker,
            private=False,
            code="legacy_worktree_invalid",
            maximum_bytes=4096,
        )
        if not marker_raw.startswith(b"gitdir: ") or not marker_raw.endswith(b"\n"):
            raise RetentionPlanError("legacy_worktree_invalid")
        git_dir = _absolute_lexical(
            Path(marker_raw.removeprefix(b"gitdir: ").removesuffix(b"\n").decode("utf-8")),
            code="legacy_worktree_invalid",
        )
        git_dir, git_status = _strict_inventory_root(git_dir)
        common_result = _git_command(path, ("rev-parse", "--path-format=absolute", "--git-common-dir"))
        git_result = _git_command(path, ("rev-parse", "--path-format=absolute", "--git-dir"))
        top_result = _git_command(path, ("rev-parse", "--path-format=absolute", "--show-toplevel"))
        head_result = _git_command(path, ("rev-parse", "--verify", "HEAD"))
        detached_result = _git_command(path, ("symbolic-ref", "-q", "HEAD"))
        clean_result = _git_command(path, ("status", "--porcelain=v1", "--untracked-files=all"))
        results = (common_result, git_result, top_result, head_result, clean_result)
        if any(result.returncode != 0 or result.stderr for result in results):
            raise RetentionPlanError("legacy_worktree_invalid")
        if detached_result.returncode != 1 or detached_result.stdout or detached_result.stderr:
            raise RetentionPlanError("legacy_worktree_invalid")
        if clean_result.stdout:
            raise RetentionPlanError("legacy_worktree_invalid")

        def output_path(result: subprocess.CompletedProcess[bytes]) -> Path:
            raw = result.stdout
            if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
                raise RetentionPlanError("legacy_worktree_invalid")
            return _absolute_lexical(
                Path(raw.removesuffix(b"\n").decode("utf-8")),
                code="legacy_worktree_invalid",
            )

        common_dir = output_path(common_result)
        observed_git_dir = output_path(git_result)
        observed_top = output_path(top_result)
        head = head_result.stdout.removesuffix(b"\n").decode("ascii")
        if (
            observed_git_dir != git_dir
            or observed_top != path
            or len(head) not in {40, 64}
            or not set(head) <= _HEX64
            or common_dir == git_dir
            or git_dir.parent.name != "worktrees"
            or git_dir.parent.parent != common_dir
        ):
            raise RetentionPlanError("legacy_worktree_invalid")
        common_dir, common_status = _strict_inventory_root(common_dir)
        if (git_dir / "locked").exists() or (git_dir / "locked").is_symlink():
            raise RetentionPlanError("legacy_worktree_invalid")
        registration_manifest = _git_admin_manifest(git_dir)
        gitdir_binding = _stable_file_bytes(
            git_dir / "gitdir",
            private=False,
            code="legacy_worktree_invalid",
            maximum_bytes=4096,
        )
        commondir_binding = _stable_file_bytes(
            git_dir / "commondir",
            private=False,
            code="legacy_worktree_invalid",
            maximum_bytes=4096,
        )
        head_binding = _stable_file_bytes(
            git_dir / "HEAD",
            private=False,
            code="legacy_worktree_invalid",
            maximum_bytes=4096,
        )
        if (
            gitdir_binding != f"{path / '.git'}\n".encode()
            or commondir_binding != b"../..\n"
            or head_binding != f"{head}\n".encode("ascii")
        ):
            raise RetentionPlanError("legacy_worktree_invalid")
        listing = _git_command(path, ("worktree", "list", "--porcelain"))
        if listing.returncode != 0 or listing.stderr:
            raise RetentionPlanError("legacy_worktree_invalid")
        record = f"worktree {path}\nHEAD {head}\ndetached"
        if record not in listing.stdout.decode("utf-8").split("\n\n"):
            raise RetentionPlanError("legacy_worktree_invalid")
        if (
            _stable_file_bytes(
                marker,
                private=False,
                code="legacy_worktree_invalid",
                maximum_bytes=4096,
            )
            != marker_raw
        ):
            raise RetentionPlanError("legacy_worktree_invalid")
        if _git_admin_manifest(git_dir) != registration_manifest:
            raise RetentionPlanError("legacy_worktree_invalid")
        return {
            "common_device": int(common_status.st_dev),
            "common_inode": int(common_status.st_ino),
            "common_dir": str(common_dir),
            "commondir_sha256": hashlib.sha256(commondir_binding).hexdigest(),
            "git_device": int(git_status.st_dev),
            "git_inode": int(git_status.st_ino),
            "git_dir": str(git_dir),
            "gitdir_sha256": hashlib.sha256(gitdir_binding).hexdigest(),
            "head": head,
            "head_sha256": hashlib.sha256(head_binding).hexdigest(),
            "marker_sha256": hashlib.sha256(marker_raw).hexdigest(),
            "registration_manifest": registration_manifest,
            "registration_manifest_sha256": hashlib.sha256(
                _canonical_json(registration_manifest)
            ).hexdigest(),
            "schema": "friday.registered-detached-clean-worktree.v1",
        }
    except (
        OSError,
        RetentionPlanError,
        subprocess.SubprocessError,
        UnicodeError,
        ValueError,
    ):
        return {"invalid": True}


def _path_intersects(path: Path, references: frozenset[Path]) -> bool:
    for reference in references:
        if path == reference or path in reference.parents or reference in path.parents:
            return True
    return False


def _root_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_nlink),
        int(status.st_uid),
        int(status.st_gid),
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
            or (
                not private
                and not _writable_mode_authority(
                    held,
                    has_acl=_descriptor_has_posix_acl(descriptor),
                )
            )
        ):
            raise RetentionPlanError(code)
        return held
    except (OSError, ValueError) as exc:
        raise RetentionPlanError(code) from exc
    finally:
        if current >= 0:
            os.close(current)


def build_retention_authority_bindings(
    *,
    activation_journal: Path,
    unit_journal: Path,
    canonical_evidence_roots: Sequence[CanonicalEvidenceRoot],
) -> RetentionAuthorityBindings:
    """Build, rather than accept, the exact durable authority projection."""

    activation_path = _absolute_lexical(
        activation_journal,
        code="activation_journal_invalid",
    )
    unit_path = _absolute_lexical(unit_journal, code="unit_install_journal_invalid")
    if not canonical_evidence_roots:
        raise RetentionPlanError("canonical_evidence_unavailable")
    try:
        snapshot = dr_index.DurableDRGenerationIndex(activation_path.parent).authority_snapshot()
    except dr_index.DRGenerationIndexError as exc:
        raise RetentionPlanError("dr_index_invalid") from exc
    pins = tuple(
        DRGenerationPin(
            role=pin.role,
            backup_directory=pin.backup_directory,
            candidate=dict(pin.candidate),
            generation_id=pin.generation_id,
            receipt_path=pin.receipt_path,
            receipt_sha256=pin.receipt_sha256,
            authentication_receipt_path=pin.authentication_receipt_path,
            authentication_receipt_sha256=pin.authentication_receipt_sha256,
            rehearsal_receipt_path=pin.rehearsal_receipt_path,
            rehearsal_receipt_sha256=pin.rehearsal_receipt_sha256,
            rehearsal_binding=(dict(pin.rehearsal_binding) if pin.rehearsal_binding is not None else None),
            restore_release_root=pin.restore_release_root,
            restore_release_commit=pin.restore_release_commit,
            restore_release_tree_manifest_sha256=pin.restore_release_tree_manifest_sha256,
            restore_release_wheel_sha256=pin.restore_release_wheel_sha256,
            restore_release_max_schema=pin.restore_release_max_schema,
            restore_release_version=pin.restore_release_version,
        )
        for pin in snapshot.pins
    )
    if not {"current", "older"}.issubset({pin.role for pin in pins}):
        raise RetentionPlanError("dr_pins_unavailable")
    evidence: list[CanonicalEvidenceRoot] = []
    for item in canonical_evidence_roots:
        if not isinstance(item, CanonicalEvidenceRoot):
            raise RetentionPlanError("canonical_evidence_invalid")
        observed = _stable_file_sha256(
            item.authority_path,
            private=False,
            code="canonical_evidence_invalid",
        )
        if observed != item.authority_sha256:
            raise RetentionPlanError("canonical_evidence_invalid")
        evidence.append(item)
    return RetentionAuthorityBindings(
        activation_journal_sha256=_stable_file_sha256(
            activation_path,
            private=True,
            code="activation_journal_invalid",
        ),
        unit_install_journal_sha256=_stable_file_sha256(
            unit_path,
            private=True,
            code="unit_install_journal_invalid",
        ),
        dr_index_path=snapshot.index_path,
        dr_index_sha256=snapshot.index_sha256,
        dr_pins=pins,
        canonical_evidence_roots=tuple(evidence),
    )


def _root_owned_file_sha256(path: Path, *, setuid: bool = False) -> tuple[Path, str]:
    lexical = _absolute_lexical(path, code="privileged_proc_helper_invalid")
    try:
        resolved = lexical.resolve(strict=True)
        parents = (Path(os.sep), *reversed(resolved.parents[:-1]))
        for parent in parents:
            status = os.lstat(parent)
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0 or stat.S_IMODE(status.st_mode) & 0o022:
                raise RetentionPlanError("privileged_proc_helper_invalid")
        before = os.lstat(resolved)
        raw = resolved.read_bytes()
        after = os.lstat(resolved)
    except (OSError, RetentionPlanError) as exc:
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError("privileged_proc_helper_invalid") from exc
    identity = lambda value: (  # noqa: E731
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or bool(before.st_mode & stat.S_ISUID) is not setuid
        or identity(before) != identity(after)
        or not raw
        or len(raw) > (16 << 20)
    ):
        raise RetentionPlanError("privileged_proc_helper_invalid")
    return resolved, hashlib.sha256(raw).hexdigest()


def _target_probe_index(
    paths: Sequence[Path],
) -> tuple[proc_probe.TargetIndex, dict[str, tuple[Path, _TargetObservation]]]:
    targets: list[proc_probe.ProbeTarget] = []
    by_id: dict[str, tuple[Path, _TargetObservation]] = {}
    for path in sorted({_absolute_lexical(item, code="open_inventory_invalid") for item in paths}, key=str):
        observation = _observe_target(path)
        if observation.raced or observation.kind == "unknown":
            raise RetentionPlanError("open_state_ambiguous")
        try:
            snapshot = _snapshot(path)
            snapshot_after = _snapshot(path)
            if snapshot != snapshot_after:
                raise RetentionPlanError("open_state_ambiguous")
            objects = tuple(
                proc_probe.ObjectKey(record[1], record[2], stat.S_IFMT(record[3]))
                for record in snapshot.records
            )
            target_id = f"artifact-{hashlib.sha256(str(path).encode()).hexdigest()[:32]}"
            target = proc_probe.ProbeTarget(target_id, (path,), objects)
        except (OSError, proc_probe.ProcProbeInputError) as exc:
            raise RetentionPlanError("open_state_ambiguous") from exc
        if target_id in by_id:
            raise RetentionPlanError("open_state_ambiguous")
        by_id[target_id] = (path, observation)
        targets.append(target)
    if not targets:
        raise RetentionPlanError("open_state_ambiguous")
    try:
        return proc_probe.build_target_index(targets), by_id
    except proc_probe.ProcProbeInputError as exc:
        raise RetentionPlanError("open_state_ambiguous") from exc


def _run_privileged_target_probe(index: proc_probe.TargetIndex) -> tuple[dict[str, Any], str]:
    helper, helper_sha256 = _root_owned_file_sha256(PRIVILEGED_PROC_HELPER)
    _scope_authority, scope_authority_sha256 = _root_owned_file_sha256(PRIVILEGED_SCOPE_AUTHORITY)
    sudo, sudo_sha256 = _root_owned_file_sha256(Path("/usr/bin/sudo"), setuid=True)
    python, python_sha256 = _root_owned_file_sha256(Path("/usr/bin/python3"))
    command = [
        str(sudo),
        "-n",
        "--",
        str(python),
        "-I",
        "-B",
        "-S",
        str(helper),
        "privileged-target-probe",
    ]
    try:
        local_sha256 = _stable_file_sha256(
            Path(proc_probe.__file__),
            private=False,
            code="privileged_proc_helper_invalid",
            maximum_bytes=4 << 20,
        )
        if helper_sha256 != local_sha256:
            raise RetentionPlanError("privileged_proc_helper_invalid")
        result = subprocess.run(  # noqa: S603
            command,
            input=proc_probe.canonical_target_index_bytes(index),
            capture_output=True,
            check=False,
            timeout=180,
            env={"HOME": "/", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin"},
        )
        if result.returncode != 0 or len(result.stdout) > proc_probe.MAX_RECEIPT_BYTES:
            raise RetentionPlanError("open_state_ambiguous")
        receipt = _unique_json(result.stdout, code="open_state_ambiguous")
        canonical = proc_probe.canonical_privileged_receipt_bytes(
            receipt,
            expected_target_index=index,
            expected_implementation_sha256=helper_sha256,
            expected_host_scope_authority_sha256=scope_authority_sha256,
        )
        if canonical != result.stdout:
            raise RetentionPlanError("open_state_ambiguous")
        transport = {
            "argv": command,
            "helper_sha256": helper_sha256,
            "python_sha256": python_sha256,
            "scope_authority_sha256": scope_authority_sha256,
            "sudo_sha256": sudo_sha256,
        }
        return receipt, hashlib.sha256(_canonical_json(transport)).hexdigest()
    except (
        OSError,
        subprocess.SubprocessError,
        proc_probe.ProcProbeInputError,
        RetentionPlanError,
    ) as exc:
        if isinstance(exc, RetentionPlanError):
            raise
        raise RetentionPlanError("open_state_ambiguous") from exc


def build_complete_open_inventory(*, target_paths: Sequence[Path]) -> OpenInventorySnapshot:
    """Use one exact root observer result for only the reviewed inventory targets."""

    index, seeds = _target_probe_index(target_paths)
    receipt, runtime_authority_sha256 = _run_privileged_target_probe(index)
    referenced = receipt.get("referenced_target_ids")
    if not isinstance(referenced, list):
        raise RetentionPlanError("open_state_ambiguous")
    open_paths = tuple(seeds[target_id][0] for target_id in referenced)
    for path, expected in seeds.values():
        if _observe_target(path) != expected:
            raise RetentionPlanError("open_state_ambiguous")
    return OpenInventorySnapshot(
        source="code_owned_privileged_target_diagnostic_v1",
        complete=True,
        open_paths=open_paths,
        authority_sha256=hashlib.sha256(
            _canonical_json(
                {
                    "observer_receipt_sha256": receipt["receipt_sha256"],
                    "runtime_authority_sha256": runtime_authority_sha256,
                }
            )
        ).hexdigest(),
        target_index_sha256=index.sha256,
        process_epoch_sha256=str(receipt["process_epoch_sha256"]),
    )


def _inventory_target_paths(roots: Sequence[Path]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in roots:
        root, before = _strict_inventory_root(value)
        try:
            names = _bounded_directory_names(
                root,
                maximum=MAX_DIRECT_INVENTORY_TARGETS,
                code="open_state_ambiguous",
            )
        except OSError as exc:
            raise RetentionPlanError("open_state_ambiguous") from exc
        if any(name in {"", ".", ".."} or "/" in name for name in names):
            raise RetentionPlanError("open_state_ambiguous")
        try:
            after = os.lstat(root)
        except OSError as exc:
            raise RetentionPlanError("open_state_ambiguous") from exc
        if _root_identity(before) != _root_identity(after):
            raise RetentionPlanError("open_state_ambiguous")
        paths.extend(root / name for name in names)
    if len(paths) != len(set(paths)):
        raise RetentionPlanError("open_state_ambiguous")
    return tuple(sorted(paths, key=str))


def plan_release_artifact_retention(
    *,
    activation_journal: Path,
    unit_journal: Path,
    backup_root: Path,
    inventory_roots: Sequence[Path],
    backup_inventory_roots: Sequence[Path] = (),
    reviewed_scratch_targets: Sequence[ReviewedScratchTarget] = (),
    open_inventory: OpenInventorySnapshot = INCOMPLETE_OPEN_INVENTORY,
    authority_bindings: RetentionAuthorityBindings | None = None,
    executable: bool = False,
    _scope_seed: bool = False,
    _candidate_scope_paths: frozenset[Path] | None = None,
    _retention_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonicalizable, read-only v2 retention plan."""

    if not inventory_roots:
        raise RetentionPlanError("inventory_roots_required")
    activation_path = _absolute_lexical(activation_journal, code="activation_journal_invalid")
    unit_path = _absolute_lexical(unit_journal, code="unit_install_journal_invalid")
    backup_path = _absolute_lexical(backup_root, code="activation_journal_invalid")
    open_receipt, open_paths, open_identities = _normalize_open_inventory(open_inventory)
    apply_open_authority = open_inventory.source in _APPLY_AUTHORITY_OPEN_SOURCES or (
        open_inventory.source == "code_owned_no_delete_candidates_v1"
        and _candidate_scope_paths == frozenset()
    )

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
    root_mount_ids = {path: _path_mount_id(path, directory=True) for path in roots}
    backup_root_mount_ids = {path: _path_mount_id(path, directory=True) for path in backup_roots}
    root_filesystem_magics = {path: _path_filesystem_magic(path) for path in roots}
    backup_root_filesystem_magics = {path: _path_filesystem_magic(path) for path in backup_roots}
    reviewed_scratch = _normalize_reviewed_scratch_targets(
        reviewed_scratch_targets,
        inventory_roots=frozenset(roots),
    )

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
        state_directory=activation_path.parent,
    )
    blocker = blocker or authority.error
    if executable and not authority.delete_authority_eligible:
        blocker = blocker or "dr_rollback_release_evidence_incomplete"

    references: set[Path] = {
        activation_path,
        unit_path,
        activation_path.parent / RETENTION_SCOPE_NAME,
        *authority.reference_paths,
    }
    role_paths: dict[str, Path] = {}
    protected_identities: list[dict[str, Any]] = []
    role_records: dict[str, Mapping[str, Any]] = {}
    protected_records: dict[Path, Mapping[str, Any]] = {}
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
            for record in role_records.values():
                record_path = _record_path(record)
                existing = protected_records.get(record_path)
                if existing is not None and dict(existing) != dict(record):
                    raise RetentionPlanError("journal_identity_mismatch")
                protected_records[record_path] = record
        except RetentionPlanError as exc:
            blocker = blocker or str(exc)

    try:
        for path, record in authority.dr_restore_release_records.items():
            existing = protected_records.get(path)
            if existing is not None and dict(existing) != dict(record):
                raise RetentionPlanError("dr_pins_invalid")
            protected_records[path] = record
        for path in sorted(protected_records, key=str):
            authenticated = _authenticate_release(protected_records[path])
            roles = sorted(role for role, role_path in role_paths.items() if role_path == path)
            if path in authority.dr_restore_release_records:
                roles = sorted((*roles, "dr_restore_release"))
            protected_identities.append({**authenticated, "roles": roles})
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
    if _scope_seed and (
        not executable
        or open_inventory.source != "code_owned_candidate_scope_seed_v1"
        or not open_inventory.complete
        or open_inventory.open_paths
        or open_inventory.open_identities
    ):
        raise RetentionPlanError("open_inventory_invalid")
    if not open_inventory.complete:
        blocker = blocker or "open_state_ambiguous"

    observations: list[tuple[_TargetObservation, Path]] = []
    aggregate_entries = 0
    direct_targets = 0
    for root in roots:
        try:
            names = _bounded_directory_names(
                root,
                maximum=MAX_DIRECT_INVENTORY_TARGETS - direct_targets,
                code="inventory_target_count_exceeded",
            )
        except (OSError, RetentionPlanError):
            names = []
            blocker = blocker or "inventory_root_raced"
        direct_targets += len(names)
        if direct_targets > MAX_DIRECT_INVENTORY_TARGETS:
            raise RetentionPlanError("inventory_target_count_exceeded")
        for name in names:
            observation = _observe_target(root / name)
            aggregate_entries += observation.entry_count or 1
            if aggregate_entries > MAX_AGGREGATE_INVENTORY_ENTRIES:
                raise RetentionPlanError("inventory_aggregate_too_large")
            observations.append((observation, root))

    backup_observations: list[tuple[_TargetObservation, Path]] = []
    for root in backup_roots:
        try:
            names = _bounded_directory_names(
                root,
                maximum=MAX_DIRECT_INVENTORY_TARGETS - direct_targets,
                code="inventory_target_count_exceeded",
            )
        except (OSError, RetentionPlanError):
            names = []
            blocker = blocker or "backup_inventory_root_raced"
        direct_targets += len(names)
        if direct_targets > MAX_DIRECT_INVENTORY_TARGETS:
            raise RetentionPlanError("inventory_target_count_exceeded")
        for name in names:
            observation = _observe_target(root / name)
            aggregate_entries += observation.entry_count or 1
            if aggregate_entries > MAX_AGGREGATE_INVENTORY_ENTRIES:
                raise RetentionPlanError("inventory_aggregate_too_large")
            backup_observations.append((observation, root))

    root_raced: set[Path] = set()
    for root in roots:
        try:
            if (
                _root_identity(os.lstat(root)) != _root_identity(root_statuses[root])
                or _path_mount_id(root, directory=True) != root_mount_ids[root]
                or _path_filesystem_magic(root) != root_filesystem_magics[root]
            ):
                root_raced.add(root)
        except (OSError, RetentionPlanError):
            root_raced.add(root)
    if root_raced:
        blocker = blocker or "inventory_root_raced"

    backup_roots_raced: set[Path] = set()
    for root in backup_roots:
        try:
            if (
                _root_identity(os.lstat(root)) != _root_identity(backup_root_statuses[root])
                or _path_mount_id(root, directory=True) != backup_root_mount_ids[root]
                or _path_filesystem_magic(root) != backup_root_filesystem_magics[root]
            ):
                backup_roots_raced.add(root)
        except (OSError, RetentionPlanError):
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
    observations_by_path = {observation.path: observation for observation, _root in observations}
    reviewed_scratch_identities: dict[Path, dict[str, Any]] = {}
    for path, reviewed in reviewed_scratch.items():
        reviewed_observation = observations_by_path.get(path)
        identity = (
            _reviewed_scratch_identity(reviewed_observation, reviewed)
            if reviewed_observation is not None
            else None
        )
        if identity is None:
            blocker = blocker or "reviewed_scratch_invalid"
        else:
            reviewed_scratch_identities[path] = identity
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
        state_directory=activation_path.parent,
    )
    if authority_after != authority:
        blocker = blocker or authority_after.error or "dr_pins_invalid"

    protected_raced: set[Path] = set()
    protected_failed: set[Path] = set()
    initial_observations = {observation.path: observation for observation, _root in observations}
    for role_path, record in protected_records.items():
        protected_observation = initial_observations.get(role_path)
        if protected_observation is None:
            protected_failed.add(role_path)
            blocker = blocker or "protected_release_authentication_failed"
            continue
        try:
            _authenticate_release(record)
            after_authentication = _observe_target(role_path)
        except RetentionPlanError:
            protected_failed.add(role_path)
            blocker = blocker or "protected_release_authentication_failed"
            continue
        if after_authentication.raced or after_authentication != protected_observation:
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
        elif observation.path in authority.dr_restore_release_records:
            reason = "dr_restore_release"
        elif _path_intersects(observation.path, authority.evidence_paths):
            reason = "canonical_evidence"
        elif observation.kind == "symlink":
            reason = "symlink_artifact"
        elif observation.filesystem_magic not in _SUPPORTED_FILESYSTEM_MAGICS:
            reason = "unsupported_filesystem"
        elif (
            observation.kind not in {"directory", "regular"}
            or observation.device != int(root_statuses[root].st_dev)
            or observation.mount_id != root_mount_ids[root]
            or observation.has_symlink
            or observation.has_special
        ):
            reason = "symlink_artifact" if observation.has_symlink else "special_artifact"
        elif not observation.owner_ok:
            reason = "non_owned_artifact"
        elif observation.has_hardlink:
            reason = "hardlinked_artifact"
        elif observation.has_group_world_writable:
            reason = "writable_artifact"
        elif _path_intersects(observation.path, frozen_references):
            reason = "journal_referenced"
        elif blocker:
            reason = blocker
        elif _path_intersects(observation.path, open_paths) or not observation.object_identities.isdisjoint(
            open_identities
        ):
            reason = "open_reference"
        else:
            reviewed_identity = reviewed_scratch_identities.get(observation.path)
            discovered = _discover_release(observation.path) if observation.kind == "directory" else None
            if reviewed_identity is not None:
                release_identity = reviewed_identity
                reason = "retirable_reviewed_scratch"
            elif discovered is not None and discovered.get("invalid") is not True:
                release_identity = discovered
                after_discovery = _observe_target(observation.path)
                if after_discovery.raced or after_discovery != observation:
                    reason = "raced_artifact"
                    release_identity = None
                else:
                    reason = "retirable_authenticated_release"
            elif executable:
                legacy = _discover_registered_legacy_worktree(observation.path)
                if legacy is not None and legacy.get("invalid") is not True:
                    release_identity = legacy
                    after_discovery = _observe_target(observation.path)
                    if after_discovery.raced or after_discovery != observation:
                        reason = "raced_artifact"
                        release_identity = None
                    else:
                        # The Git admin directory is a second mutation root.  P0H
                        # retains it until that root has its own journaled seal,
                        # privileged diagnostic and filesystem lease contour.
                        reason = "registered_legacy_requires_secondary_root"
                elif discovered is not None or legacy is not None:
                    reason = "malformed_release" if discovered is not None else "unknown_artifact"
                elif observation.has_symlink:
                    reason = "symlink_artifact"
                else:
                    reason = "unknown_artifact"
            elif discovered is not None:
                reason = "malformed_release"
            elif observation.has_symlink:
                reason = "symlink_artifact"
            else:
                reason = "unknown_artifact"
        if reason not in _REASONS:
            reason = "unknown_artifact"
        decision = (
            "delete_candidate"
            if reason
            in {
                "retirable_authenticated_release",
                "retirable_reviewed_scratch",
            }
            else "retain"
        )
        entries.append(
            {
                "path": str(observation.path),
                "device": observation.device,
                "inode": observation.inode,
                "mount_id": observation.mount_id,
                "filesystem_magic": observation.filesystem_magic,
                "mode": observation.mode,
                "writable_authority_sha256": observation.writable_authority_sha256,
                "type": observation.kind,
                "nlink": observation.nlink,
                "recursive_bytes": observation.total_bytes,
                "allocated_bytes": observation.total_allocated_bytes,
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
                dr_pin_identities.setdefault(Path(str(raw_pin["backup_directory"])), dict(raw_pin))
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
        elif observation.filesystem_magic not in _SUPPORTED_FILESYSTEM_MAGICS:
            reason = "unsupported_filesystem"
        elif (
            observation.kind not in {"directory", "regular"}
            or observation.device != int(backup_root_statuses[root].st_dev)
            or observation.mount_id != backup_root_mount_ids[root]
            or observation.has_symlink
            or observation.has_special
        ):
            reason = "symlink_artifact" if observation.has_symlink else "special_artifact"
        elif not observation.owner_ok:
            reason = "non_owned_artifact"
        elif observation.has_hardlink:
            reason = "hardlinked_artifact"
        elif observation.has_group_world_writable:
            reason = "writable_artifact"
        elif _path_intersects(observation.path, frozen_references):
            reason = "journal_referenced"
        elif blocker:
            reason = blocker
        elif _path_intersects(observation.path, open_paths) or not observation.object_identities.isdisjoint(
            open_identities
        ):
            reason = "open_reference"
        else:
            discovered_backup = _discover_exact_backup(observation.path) if executable else None
            if discovered_backup is not None and discovered_backup.get("invalid") is not True:
                backup_identity = discovered_backup
                after_discovery = _observe_target(observation.path)
                if after_discovery.raced or after_discovery != observation:
                    reason = "raced_artifact"
                    backup_identity = None
                else:
                    reason = "retirable_authenticated_backup"
            else:
                reason = "legacy_or_unknown_backup"
        if reason not in _REASONS:
            reason = "legacy_or_unknown_backup"
        backup_entries.append(
            {
                "path": str(observation.path),
                "device": observation.device,
                "inode": observation.inode,
                "mount_id": observation.mount_id,
                "filesystem_magic": observation.filesystem_magic,
                "mode": observation.mode,
                "writable_authority_sha256": observation.writable_authority_sha256,
                "type": observation.kind,
                "nlink": observation.nlink,
                "recursive_bytes": observation.total_bytes,
                "allocated_bytes": observation.total_allocated_bytes,
                "entry_count": observation.entry_count,
                "inventory_sha256": observation.inventory_sha256,
                "identity": backup_identity,
                "decision": ("delete_candidate" if reason == "retirable_authenticated_backup" else "retain"),
                "reason": reason,
            }
        )

    delete_records = sorted(
        (item for item in (*entries, *backup_entries) if item["decision"] == "delete_candidate"),
        key=lambda item: str(item["path"]),
    )
    if _candidate_scope_paths is None:
        selected_count = 0
        selected_objects = 0
        deferred_records = []
        for item in delete_records:
            candidate_objects = int(item["entry_count"])
            if (
                selected_count >= MAX_DELETE_CANDIDATES_PER_PLAN
                or selected_objects + candidate_objects > proc_probe.MAX_TARGET_OBJECTS
            ):
                deferred_records.append(item)
                continue
            selected_count += 1
            selected_objects += candidate_objects
    else:
        deferred_records = [
            item for item in delete_records if Path(str(item["path"])) not in _candidate_scope_paths
        ]
    for deferred in deferred_records:
        deferred["decision"] = "retain"
        deferred["reason"] = "deferred_batch_bound"

    core: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mode": (
            "candidate_scope_seed"
            if _scope_seed
            else "eligible_classification"
            if executable
            else "read_only_classification"
        ),
        "scope": "release_and_backup_inventory",
        "retention_scope": dict(_retention_scope or {}),
        "apply_authority": executable and blocker == "" and not _scope_seed and apply_open_authority,
        "effect_authority": {
            "bounded_contour": BOUNDED_DELETE_CONTOUR,
            "concurrent_open_attempts_excluded": True,
            "filesystem_magic": _EXT4_SUPER_MAGIC,
            "global_operator_lock": True,
            "per_regular_file_write_lease": True,
            "privileged_probe_role": "diagnostic_prerequisite",
            "sealed_quarantine_mode": "0700",
            "threat_boundary": THREAT_BOUNDARY,
            "unique_mount_identity": True,
            "universal_absence_proof": False,
        },
        "backup_root": str(backup_path),
        "inventory_roots": [
            {
                "path": str(path),
                "device": int(root_statuses[path].st_dev),
                "inode": int(root_statuses[path].st_ino),
                "mount_id": root_mount_ids[path],
                "filesystem_magic": root_filesystem_magics[path],
                "writable_authority_sha256": _writable_mode_authority(root_statuses[path]),
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
                "mount_id": backup_root_mount_ids[path],
                "filesystem_magic": backup_root_filesystem_magics[path],
                "writable_authority_sha256": _writable_mode_authority(backup_root_statuses[path]),
                "type": "directory",
                "nlink": int(backup_root_statuses[path].st_nlink),
                "uid": int(backup_root_statuses[path].st_uid),
            }
            for path in backup_roots
        ],
        "reviewed_scratch_targets": [
            {
                "contour": value.contour,
                "inventory_sha256": value.inventory_sha256,
                "path": str(value.path),
            }
            for value in sorted(reviewed_scratch.values(), key=lambda item: str(item.path))
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
        "classification_status": (
            "scope_seed" if _scope_seed and blocker == "" else "eligible" if blocker == "" else "blocked"
        ),
        "block_reason": blocker,
        "protected_releases": protected_identities,
        "targets": entries,
        "backup_targets": backup_entries,
    }
    return {**core, "plan_sha256": hashlib.sha256(_canonical_json(core)).hexdigest()}


def build_eligible_retention_plan(
    *,
    activation_journal: Path,
    unit_journal: Path,
    backup_root: Path,
    inventory_roots: Sequence[Path],
    backup_inventory_roots: Sequence[Path],
    canonical_evidence_roots: Sequence[CanonicalEvidenceRoot],
    reviewed_scratch_targets: Sequence[ReviewedScratchTarget] = (),
) -> dict[str, Any]:
    """Build one executable plan solely from live code-owned authorities."""

    scope = load_retention_scope_authority(activation_journal=activation_journal)
    inventory_values = tuple(inventory_roots)
    backup_inventory_values = tuple(backup_inventory_roots)
    evidence_values = tuple(canonical_evidence_roots)
    try:
        supplied_backup_root = _absolute_lexical(backup_root, code="retention_scope_mismatch")
        supplied_inventory = tuple(
            sorted(
                {_absolute_lexical(path, code="retention_scope_mismatch") for path in inventory_values},
                key=str,
            )
        )
        supplied_backup_inventory = tuple(
            sorted(
                {
                    _absolute_lexical(path, code="retention_scope_mismatch")
                    for path in backup_inventory_values
                },
                key=str,
            )
        )
        supplied_evidence = tuple(
            sorted(
                (
                    CanonicalEvidenceRoot(
                        path=_absolute_lexical(item.path, code="retention_scope_mismatch"),
                        authority_path=_absolute_lexical(
                            item.authority_path,
                            code="retention_scope_mismatch",
                        ),
                        authority_sha256=item.authority_sha256,
                    )
                    for item in evidence_values
                ),
                key=lambda item: (str(item.path), str(item.authority_path), item.authority_sha256),
            )
        )
    except (AttributeError, TypeError) as exc:
        raise RetentionPlanError("retention_scope_mismatch") from exc
    if (
        len(supplied_inventory) != len(inventory_values)
        or len(supplied_backup_inventory) != len(backup_inventory_values)
        or len({(item.path, item.authority_path) for item in supplied_evidence}) != len(evidence_values)
        or supplied_backup_root != scope.backup_root
        or supplied_inventory != scope.inventory_roots
        or supplied_backup_inventory != scope.backup_inventory_roots
        or supplied_evidence != scope.canonical_evidence_roots
    ):
        raise RetentionPlanError("retention_scope_mismatch")

    bindings = build_retention_authority_bindings(
        activation_journal=activation_journal,
        unit_journal=unit_journal,
        canonical_evidence_roots=scope.canonical_evidence_roots,
    )
    scope_seed = plan_release_artifact_retention(
        activation_journal=activation_journal,
        unit_journal=unit_journal,
        backup_root=scope.backup_root,
        inventory_roots=scope.inventory_roots,
        backup_inventory_roots=scope.backup_inventory_roots,
        reviewed_scratch_targets=reviewed_scratch_targets,
        open_inventory=OpenInventorySnapshot(
            source="code_owned_candidate_scope_seed_v1",
            complete=True,
        ),
        authority_bindings=bindings,
        executable=True,
        _scope_seed=True,
        _retention_scope=scope.receipt,
    )
    if scope_seed["classification_status"] != "scope_seed":
        raise RetentionPlanError(str(scope_seed["block_reason"] or "retention_authority_unbound"))
    target_paths = tuple(
        sorted(
            {
                Path(str(item["path"]))
                for key in ("targets", "backup_targets")
                for item in scope_seed[key]
                if item["decision"] == "delete_candidate"
            },
            key=str,
        )
    )
    inventory = (
        build_complete_open_inventory(target_paths=target_paths)
        if target_paths
        else OpenInventorySnapshot(
            source="code_owned_no_delete_candidates_v1",
            complete=True,
            authority_sha256=str(scope_seed["plan_sha256"]),
        )
    )
    plan = plan_release_artifact_retention(
        activation_journal=activation_journal,
        unit_journal=unit_journal,
        backup_root=scope.backup_root,
        inventory_roots=scope.inventory_roots,
        backup_inventory_roots=scope.backup_inventory_roots,
        reviewed_scratch_targets=reviewed_scratch_targets,
        open_inventory=inventory,
        authority_bindings=bindings,
        executable=True,
        _candidate_scope_paths=frozenset(target_paths),
        _retention_scope=scope.receipt,
    )
    if plan["classification_status"] != "eligible":
        raise RetentionPlanError(str(plan["block_reason"] or "retention_authority_unbound"))
    if target_paths:
        if plan["apply_authority"] is not True:
            raise RetentionPlanError(str(plan["block_reason"] or "retention_authority_unbound"))
    elif (
        plan["apply_authority"] is not True
        or inventory.source != "code_owned_no_delete_candidates_v1"
    ):
        raise RetentionPlanError(str(plan["block_reason"] or "retention_authority_unbound"))
    if inventory.source == "code_owned_privileged_target_diagnostic_v1":
        after_index, _after = _target_probe_index(target_paths)
        if after_index.sha256 != inventory.target_index_sha256:
            raise RetentionPlanError("open_state_ambiguous")
    expected_candidates = set(target_paths).difference(inventory.open_paths)
    actual_candidates = {
        Path(str(item["path"]))
        for key in ("targets", "backup_targets")
        for item in plan[key]
        if item["decision"] == "delete_candidate"
    }
    if actual_candidates != expected_candidates:
        raise RetentionPlanError("open_state_ambiguous")
    if load_retention_scope_authority(activation_journal=activation_journal) != scope:
        raise RetentionPlanError("retention_scope_changed")
    return plan


def _write_atomic(path: Path, payload: bytes) -> None:
    lexical = _absolute_lexical(path, code="output_path_invalid")
    if not lexical.name:
        raise RetentionPlanError("output_path_invalid")
    parent = _absolute_lexical(lexical.parent, code="output_path_invalid")
    directory_fd, parent_parts, parent_identities = _open_absolute_directory_chain(parent)
    temporary = ""
    descriptor = -1
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
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        _require_pinned_directory(directory_fd, parent_parts, parent_identities)
        held_status = os.fstat(descriptor)
        temporary_status = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        published_identity = _inode_identity(held_status)
        if _inode_identity(temporary_status) != published_identity:
            raise RetentionPlanError("output_publish_raced")
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
        if (
            _inode_identity(final_status) != published_identity
            or _inode_identity(os.fstat(descriptor)) != published_identity
        ):
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
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _validate_output_namespace(path: Path, plan: Mapping[str, Any]) -> None:
    lexical = _absolute_lexical(path, code="output_path_invalid")
    protected_roots: set[Path] = set()
    try:
        activation = plan["activation_journal"]
        protected_roots.add(
            _absolute_lexical(
                Path(str(activation["path"])).parent,
                code="output_path_invalid",
            )
        )
        protected_roots.add(_absolute_lexical(Path(str(plan["backup_root"])), code="output_path_invalid"))
        for key in ("inventory_roots", "backup_inventory_roots"):
            values = plan.get(key)
            if not isinstance(values, list):
                raise RetentionPlanError("output_path_invalid")
            for item in values:
                if not isinstance(item, Mapping):
                    raise RetentionPlanError("output_path_invalid")
                protected_roots.add(
                    _absolute_lexical(Path(str(item["path"])), code="output_path_invalid")
                )
        authority = plan.get("authority_bindings")
        if isinstance(authority, Mapping):
            evidence = authority.get("canonical_evidence_roots")
            if evidence not in (None, []) and not isinstance(evidence, list):
                raise RetentionPlanError("output_path_invalid")
            for item in evidence or []:
                if not isinstance(item, Mapping):
                    raise RetentionPlanError("output_path_invalid")
                protected_roots.add(
                    _absolute_lexical(Path(str(item["path"])), code="output_path_invalid")
                )
                protected_roots.add(
                    _absolute_lexical(
                        Path(str(item["authority_path"])),
                        code="output_path_invalid",
                    )
                )
    except (KeyError, TypeError, ValueError) as exc:
        raise RetentionPlanError("output_path_invalid") from exc
    if any(lexical == protected or protected in lexical.parents for protected in protected_roots):
        raise RetentionPlanError("output_path_protected")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan bounded Friday wheel-release retention")
    parser.add_argument("--activation-journal", required=True, type=Path)
    parser.add_argument("--unit-journal", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--inventory-root", required=True, action="append", type=Path)
    parser.add_argument("--backup-inventory-root", action="append", default=[], type=Path)
    parser.add_argument("--eligible", action="store_true")
    parser.add_argument(
        "--evidence-authority",
        action="append",
        default=[],
        metavar=("ROOT", "AUTHORITY", "SHA256"),
        nargs=3,
    )
    parser.add_argument(
        "--reviewed-scratch",
        action="append",
        default=[],
        metavar=("PATH", "INVENTORY_SHA256"),
        nargs=2,
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.eligible:
            evidence = tuple(
                CanonicalEvidenceRoot(
                    path=Path(values[0]),
                    authority_path=Path(values[1]),
                    authority_sha256=values[2],
                )
                for values in args.evidence_authority
            )
            reviewed_scratch = tuple(
                ReviewedScratchTarget(path=Path(values[0]), inventory_sha256=values[1])
                for values in args.reviewed_scratch
            )
            plan = build_eligible_retention_plan(
                activation_journal=args.activation_journal,
                unit_journal=args.unit_journal,
                backup_root=args.backup_root,
                inventory_roots=args.inventory_root,
                backup_inventory_roots=args.backup_inventory_root,
                canonical_evidence_roots=evidence,
                reviewed_scratch_targets=reviewed_scratch,
            )
        else:
            if args.evidence_authority or args.reviewed_scratch:
                raise RetentionPlanError("retention_authority_unbound")
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
            _validate_output_namespace(args.output, plan)
            _write_atomic(args.output, payload)
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, RetentionPlanError) else "retention_unexpected_failure"
        if (
            not code
            or len(code) > 128
            or code[0] not in "abcdefghijklmnopqrstuvwxyz"
            or not set(code) <= frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
        ):
            code = "retention_unexpected_failure"
        failure = {
            "schema": PLAN_SCHEMA,
            "status": "failed_closed",
            "failure_code": code,
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
    "RETENTION_SCOPE_NAME",
    "RETENTION_SCOPE_SCHEMA",
    "RetentionPlanError",
    "RetentionScopeAuthority",
    "RetentionAuthorityBindings",
    "ReviewedScratchTarget",
    "build_complete_open_inventory",
    "build_eligible_retention_plan",
    "build_retention_authority_bindings",
    "load_retention_scope_authority",
    "plan_release_artifact_retention",
]


if __name__ == "__main__":
    raise SystemExit(main())
