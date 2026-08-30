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

PLAN_SCHEMA = "friday.release-artifact-retention-plan.v1"
OPEN_INVENTORY_SCHEMA = "friday.release-artifact-open-inventory.v1"
MAX_JOURNAL_BYTES = 1 << 20
MAX_RELEASE_MANIFEST_BYTES = 64 << 20
MAX_INVENTORY_ENTRIES = 1_000_000

_HEX64 = frozenset("0123456789abcdef")
_OPEN_SOURCES = frozenset({"unavailable", "code_owned_fd_inventory_v1", "synthetic_test"})
_REASONS = frozenset(
    {
        "activation_journal_invalid",
        "activation_not_clear",
        "current_release",
        "fallback_release",
        "hardlinked_artifact",
        "inventory_root_raced",
        "journal_identity_mismatch",
        "journal_referenced",
        "malformed_release",
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
        "unit_install_not_complete",
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
    try:
        before = os.lstat(lexical)
    except OSError as exc:
        raise RetentionPlanError(code) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or not 0 < before.st_size <= maximum_bytes
        or (private and stat.S_IMODE(before.st_mode) & 0o077)
    ):
        raise RetentionPlanError(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    chunks: list[bytes] = []
    try:
        descriptor = os.open(lexical, flags)
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
        after = os.lstat(lexical)
    except OSError as exc:
        raise RetentionPlanError(code) from exc
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
        before = _stable_file_sha256(path, private=True, code="activation_journal_invalid")
        state = release_operator.DurableActivationJournal(
            path,
            backup_root=strict_backup,
            config_identity_sha256=None,
        ).load()
        after = _stable_file_sha256(path, private=True, code="activation_journal_invalid")
        if before != after:
            raise RetentionPlanError("activation_journal_invalid")
        return _JournalResult(state=dict(state), sha256=after, error="")
    except (OSError, RetentionPlanError, release_operator.ReleaseFailure):
        return _JournalResult(state=None, sha256="", error="activation_journal_invalid")


def _read_unit_journal(path: Path) -> _JournalResult:
    try:
        _strict_private_directory(path.parent, code="unit_install_journal_invalid")
        before = _stable_file_sha256(path, private=True, code="unit_install_journal_invalid")
        state = release_operator.DurableUnitInstallJournal(path).load()
        after = _stable_file_sha256(path, private=True, code="unit_install_journal_invalid")
        if before != after:
            raise RetentionPlanError("unit_install_journal_invalid")
        return _JournalResult(state=dict(state), sha256=after, error="")
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
) -> tuple[int, tuple[str, ...], tuple[tuple[int, int], ...]]:
    parts = tuple(path.parts[1:])
    if not path.is_absolute() or path.anchor != os.sep or any(part in {"", ".", ".."} for part in parts):
        raise RetentionPlanError("output_path_invalid")
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
                    raise RetentionPlanError("output_path_invalid")
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
        raise RetentionPlanError("output_path_invalid") from exc


def _require_pinned_directory(
    descriptor: int,
    parts: tuple[str, ...],
    identities: tuple[tuple[int, int], ...],
) -> os.stat_result:
    current = -1
    try:
        if len(identities) != len(parts) + 1:
            raise RetentionPlanError("output_path_invalid")
        current = os.open(os.sep, _directory_open_flags())
        if _inode_identity(os.fstat(current)) != identities[0]:
            raise RetentionPlanError("output_path_invalid")
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
                    raise RetentionPlanError("output_path_invalid")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        held = os.fstat(descriptor)
        if (
            _inode_identity(held) != identities[-1]
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) & 0o077
        ):
            raise RetentionPlanError("output_path_invalid")
        return held
    except (OSError, ValueError) as exc:
        raise RetentionPlanError("output_path_invalid") from exc
    finally:
        if current >= 0:
            os.close(current)


def plan_release_artifact_retention(
    *,
    activation_journal: Path,
    unit_journal: Path,
    backup_root: Path,
    inventory_roots: Sequence[Path],
    open_inventory: OpenInventorySnapshot = INCOMPLETE_OPEN_INVENTORY,
) -> dict[str, Any]:
    """Return a canonicalizable, read-only v1 retention plan."""

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
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root in other.parents or other in root.parents:
                raise RetentionPlanError("inventory_roots_overlap")
    root_statuses = {path: status for path, status in roots_with_status}

    activation = _read_activation_journal(activation_path, backup_path)
    unit = _read_unit_journal(unit_path)
    blocker = activation.error or unit.error
    activation_state = activation.state
    unit_state = unit.state
    if not blocker and activation_state is not None and activation_state.get("phase") != "clear":
        blocker = "activation_not_clear"
    if not blocker and unit_state is not None and unit_state.get("phase") != "complete":
        blocker = "unit_install_not_complete"

    references: set[Path] = {activation_path, unit_path}
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

    root_raced: set[Path] = set()
    for root in roots:
        try:
            if _root_identity(os.lstat(root)) != _root_identity(root_statuses[root]):
                root_raced.add(root)
        except OSError:
            root_raced.add(root)
    if root_raced:
        blocker = blocker or "inventory_root_raced"

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
        identity: dict[str, Any] | None = None
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
                identity = discovered
                after_discovery = _observe_target(observation.path)
                if after_discovery.raced or after_discovery != observation:
                    reason = "raced_artifact"
                    identity = None
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
                "identity": identity,
                "decision": decision,
                "reason": reason,
            }
        )

    core: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mode": "read_only_classification",
        "scope": "wheel_release_inventory_only",
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
        "open_inventory": open_receipt,
        "classification_status": "eligible" if blocker == "" else "blocked",
        "block_reason": blocker,
        "protected_releases": protected_identities,
        "targets": entries,
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
    "INCOMPLETE_OPEN_INVENTORY",
    "OPEN_INVENTORY_SCHEMA",
    "OpenInventorySnapshot",
    "PLAN_SCHEMA",
    "RetentionPlanError",
    "plan_release_artifact_retention",
]


if __name__ == "__main__":
    raise SystemExit(main())
