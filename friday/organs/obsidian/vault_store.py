"""Contained, durable filesystem primitives for one server-side vault checkout."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import threading
from array import array
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .contracts import (
    NoteAlreadyExistsError,
    NoteNotFoundError,
    RevisionConflictError,
    VaultLimitError,
    VaultPathError,
    validate_revision,
)

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_MAX_RELATIVE_PATH_CHARS = 2_048
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_OPEN_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_OPEN_MUTABLE_FILE_FLAGS = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_INTERNAL_VAULT_ROOTS = frozenset({".stfolder", ".stignore", ".stversions", ".trash"})
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_TRANSACTION_SCHEMA = "friday.vault-cas.v1"
_MUTATION_TRANSACTION_SCHEMA = "friday.vault-mutation.v1"
_MAX_TRANSACTION_BYTES = 8 * 1024
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_LEASE_REFUSED = frozenset(
    {
        errno.EACCES,
        errno.EAGAIN,
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        errno.EPERM,
    }
)
_LEASE_BREAK_SIGNAL = signal.SIGURG

_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


@dataclass(frozen=True, slots=True)
class VaultLimits:
    """Hard ceilings for data synchronized from an untrusted peer."""

    max_note_bytes: int = 4 * 1024 * 1024
    max_depth: int = 32
    max_entries: int = 20_000
    max_markdown_paths: int = 5_000
    max_total_markdown_bytes: int = 32 * 1024 * 1024
    max_list_results: int = 1_000
    max_search_results: int = 100

    def __post_init__(self) -> None:
        for name in (
            "max_note_bytes",
            "max_depth",
            "max_entries",
            "max_markdown_paths",
            "max_total_markdown_bytes",
            "max_list_results",
            "max_search_results",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class _MarkdownEntry:
    path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _LeasedFile:
    descriptor: int
    content: bytes
    previous_signal_mask: frozenset[int]
    signal_was_pending: bool


@dataclass(frozen=True, slots=True)
class VaultFile:
    path: str
    content: bytes
    revision: str
    size_bytes: int
    modified_at: datetime
    generation: tuple[int, int, int, int, int]

    def text(self) -> str:
        try:
            return self.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("vault note is not valid UTF-8") from exc


@dataclass(frozen=True, slots=True)
class VaultDeleteResult:
    """The locally durable effect of deleting one observed revision."""

    path: str
    deleted_revision: str
    applied: bool = True

    @property
    def revision(self) -> str:
        """Compatibility spelling for callers recording the removed revision."""

        return self.deleted_revision

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return (self.path,)

    @property
    def changed_revisions(self) -> tuple[tuple[str, str | None], ...]:
        return ((self.path, None),)


@dataclass(frozen=True, slots=True)
class VaultMoveResult:
    """The locally durable effect of moving one observed revision."""

    source_path: str
    destination_path: str
    revision: str
    applied: bool = True

    @property
    def path(self) -> str:
        return self.destination_path

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return (self.source_path, self.destination_path)

    @property
    def changed_revisions(self) -> tuple[tuple[str, str | None], ...]:
        return ((self.source_path, None), (self.destination_path, self.revision))


class VaultStore:
    """Read and atomically replace files without following vault symlinks.

    Parent directories are traversed through ``openat``-style directory file
    descriptors with ``O_NOFOLLOW``.  A symlink swap therefore cannot redirect
    a read, directory creation, temporary file, or final rename outside the
    configured checkout.
    """

    def __init__(self, root: str | Path, *, limits: VaultLimits | None = None) -> None:
        configured = Path(root)
        if configured.exists() and configured.is_symlink():
            raise VaultPathError("vault root must not be a symlink")
        configured.mkdir(parents=True, exist_ok=True)
        resolved = configured.resolve(strict=True)
        if not resolved.is_dir():
            raise VaultPathError("vault root must be a directory")
        self.root = resolved
        self.limits = limits or VaultLimits()
        self._lock = threading.RLock()
        root_stat = os.stat(self.root, follow_symlinks=False)
        self._root_identity = (int(root_stat.st_dev), int(root_stat.st_ino))
        identity = hashlib.sha256(
            f"{self.root}:{root_stat.st_dev}:{root_stat.st_ino}".encode("utf-8", errors="strict")
        ).hexdigest()[:24]
        self._transaction_name = f".friday-vault-{identity}.txn"
        self._transaction_next_name = f"{self._transaction_name}.next"
        with self._lock, self._vault_guard():
            pass

    def normalize_path(self, relative_path: str | PurePosixPath) -> str:
        """Return one canonical POSIX path or reject traversal/host paths."""

        raw = str(relative_path)
        if not raw or len(raw) > _MAX_RELATIVE_PATH_CHARS:
            raise VaultPathError("vault path must be non-empty and bounded")
        if "\x00" in raw or "\\" in raw or raw.startswith("/") or _WINDOWS_DRIVE.match(raw):
            raise VaultPathError("vault path must be a relative POSIX path")
        try:
            raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise VaultPathError("vault path must be valid UTF-8") from exc
        raw_parts = raw.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise VaultPathError("vault path contains an unsafe segment")
        first = raw_parts[0].casefold()
        if first in _INTERNAL_VAULT_ROOTS or first == ".obsidian":
            raise VaultPathError("vault path enters a reserved internal root")
        if len(raw_parts) - 1 > self.limits.max_depth:
            raise VaultLimitError(
                f"vault path exceeds the maximum depth of {self.limits.max_depth} directories"
            )
        path = PurePosixPath(*raw_parts)
        if path.is_absolute() or not path.name:
            raise VaultPathError("vault path must name a file")
        return path.as_posix()

    def normalize_ordinary_note_path(self, relative_path: str | PurePosixPath) -> str:
        """Reject internal, configuration, and conflict-copy paths from ordinary APIs."""

        normalized = self.normalize_path(relative_path)
        parts = PurePosixPath(normalized).parts
        folded = tuple(part.casefold() for part in parts)
        if ".obsidian" in folded:
            raise VaultPathError("ordinary note path enters an internal vault directory")
        if ".sync-conflict-" in folded[-1]:
            raise VaultPathError("ordinary note path names a Syncthing conflict copy")
        return normalized

    def validate_text_size(self, content: str) -> None:
        """Reject text before an encoding or render can grow without a bound."""

        self._encode_text(content)

    def _encode_text(self, content: str) -> bytes:
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("note content must be NUL-free text")
        limit = self.limits.max_note_bytes
        if len(content) > limit:
            raise VaultLimitError(f"note exceeds the maximum size of {limit} bytes")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("note content must be valid UTF-8") from exc
        if len(encoded) > limit:
            raise VaultLimitError(f"note exceeds the maximum size of {limit} bytes")
        return encoded

    def read(self, relative_path: str | PurePosixPath) -> VaultFile:
        normalized = self.normalize_path(relative_path)
        parts = PurePosixPath(normalized).parts
        with (
            self._lock,
            self._vault_guard(),
            self._parent_fd(parts, create=False) as (
                parent_fd,
                leaf,
            ),
        ):
            content, file_stat = self._read_at(parent_fd, leaf, normalized)
        return VaultFile(
            path=normalized,
            content=content,
            revision=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            modified_at=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
            generation=self._generation(file_stat),
        )

    def read_text(self, relative_path: str | PurePosixPath) -> VaultFile:
        stored = self.read(relative_path)
        stored.text()
        return stored

    def exists(self, relative_path: str | PurePosixPath) -> bool:
        try:
            self.read(relative_path)
        except NoteNotFoundError:
            return False
        return True

    def write_text(
        self,
        relative_path: str | PurePosixPath,
        content: str,
        *,
        expected_revision: str | None = None,
        create_only: bool = False,
    ) -> VaultFile:
        encoded = self._encode_text(content)
        return self.write(
            relative_path,
            encoded,
            expected_revision=expected_revision,
            create_only=create_only,
        )

    def write(
        self,
        relative_path: str | PurePosixPath,
        content: bytes,
        *,
        expected_revision: str | None = None,
        create_only: bool = False,
    ) -> VaultFile:
        """Atomically replace a file after an optional SHA-256 revision check."""

        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if len(content) > self.limits.max_note_bytes:
            raise VaultLimitError(f"note exceeds the maximum size of {self.limits.max_note_bytes} bytes")
        if expected_revision is not None:
            validate_revision(expected_revision)
        normalized = self.normalize_path(relative_path)
        parts = PurePosixPath(normalized).parts
        with (
            self._lock,
            self._vault_guard() as (_root_fd, journal_fd),
            self._parent_fd(parts, create=True) as (parent_fd, leaf),
        ):
            current: bytes | None = None
            current_stat: os.stat_result | None = None
            try:
                current, current_stat = self._read_at(parent_fd, leaf, normalized)
            except NoteNotFoundError:
                current = None
            if create_only and current is not None:
                raise NoteAlreadyExistsError(normalized)
            actual_revision = hashlib.sha256(current).hexdigest() if current is not None else None
            if expected_revision is not None and actual_revision != expected_revision:
                raise RevisionConflictError(expected_revision, actual_revision)
            self._atomic_replace(
                parent_fd,
                leaf,
                normalized,
                content,
                existing_mode=stat.S_IMODE(current_stat.st_mode) if current_stat is not None else 0o600,
                create_only=create_only,
                expected_revision=expected_revision,
                journal_fd=journal_fd,
            )
            written, written_stat = self._read_at(parent_fd, leaf, normalized)
            if written != content:
                raise OSError("atomic note write postcondition failed")
        return VaultFile(
            path=normalized,
            content=written,
            revision=hashlib.sha256(written).hexdigest(),
            size_bytes=len(written),
            modified_at=datetime.fromtimestamp(written_stat.st_mtime, tz=UTC),
            generation=self._generation(written_stat),
        )

    def delete(
        self,
        relative_path: str | PurePosixPath,
        *,
        expected_revision: str,
    ) -> VaultDeleteResult:
        """Durably delete exactly one observed file revision.

        The source is leased and copied to a durable recovery file before its
        directory entry is moved.  A peer replacement is therefore never
        unlinked by mistake: recovery restores the peer entry and preserves the
        observed Friday revision as a conflict copy.
        """

        validate_revision(expected_revision)
        normalized = self.normalize_path(relative_path)
        parts = PurePosixPath(normalized).parts
        lease: _LeasedFile | None = None
        transaction: dict[str, object] | None = None
        with (
            self._lock,
            self._vault_guard() as (_root_fd, journal_fd),
            self._parent_fd(parts, create=False) as (parent_fd, leaf),
        ):
            try:
                lease, source_identity, source_mode = self._lease_expected_at(
                    parent_fd,
                    leaf,
                    normalized,
                    expected_revision,
                )
                transaction_id = secrets.token_hex(16)
                backup_name, capture_name = self._mutation_staging_names(
                    transaction_id,
                    kind="delete",
                )
                self._write_durable_file(journal_fd, backup_name, lease.content, source_mode)
                os.fsync(journal_fd)
                transaction = {
                    "schema": _MUTATION_TRANSACTION_SCHEMA,
                    "kind": "delete",
                    "transaction_id": transaction_id,
                    "phase": "prepared",
                    "source_path": normalized,
                    "backup_name": backup_name,
                    "capture_name": capture_name,
                    "source_device": source_identity[0],
                    "source_inode": source_identity[1],
                    "expected_revision": expected_revision,
                }
                self._write_transaction(journal_fd, transaction, replace=False)
                try:
                    self._rename_noreplace(parent_fd, leaf, journal_fd, capture_name)
                except FileNotFoundError:
                    actual = self._revision_or_none(parent_fd, leaf, normalized)
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise RevisionConflictError(expected_revision, actual) from None
                except FileExistsError as exc:
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise VaultPathError("vault delete staging entry already exists") from exc
                os.fsync(parent_fd)
                os.fsync(journal_fd)
                if not self._entry_has_identity(journal_fd, capture_name, source_identity):
                    actual = self._revision_or_none(journal_fd, capture_name, normalized)
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise RevisionConflictError(expected_revision, actual)

                transaction["phase"] = "commit"
                self._write_transaction(journal_fd, transaction, replace=True)
                if not self._entry_has_identity(journal_fd, capture_name, source_identity):
                    actual = self._revision_or_none(journal_fd, capture_name, normalized)
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise RevisionConflictError(expected_revision, actual)
                os.unlink(capture_name, dir_fd=journal_fd)
                os.fsync(journal_fd)
                self._discard_mutation_backup(
                    journal_fd,
                    backup_name,
                    expected_revision=expected_revision,
                    conflict_parent_fd=parent_fd,
                    conflict_leaf=leaf,
                    transaction_id=transaction_id,
                )
                self._remove_transaction(journal_fd)
                transaction = None
            except BaseException:
                if transaction is not None and self._entry_exists(journal_fd, self._transaction_name):
                    try:
                        self._recover_mutation_transaction(journal_fd, transaction)
                    except Exception as recovery_exc:
                        raise OSError("conditional note delete recovery failed") from recovery_exc
                raise
            finally:
                if lease is not None:
                    self._release_lease(lease)
        return VaultDeleteResult(path=normalized, deleted_revision=expected_revision)

    def move(
        self,
        source_path: str | PurePosixPath,
        destination_path: str | PurePosixPath,
        *,
        expected_revision: str,
    ) -> VaultMoveResult:
        """Durably move exactly one observed revision without clobbering a peer."""

        validate_revision(expected_revision)
        source = self.normalize_path(source_path)
        destination = self.normalize_path(destination_path)
        if source == destination:
            raise ValueError("source and destination paths must differ")
        source_parts = PurePosixPath(source).parts
        destination_parts = PurePosixPath(destination).parts
        lease: _LeasedFile | None = None
        transaction: dict[str, object] | None = None
        with (
            self._lock,
            self._vault_guard() as (_root_fd, journal_fd),
            self._parent_fd(source_parts, create=False) as (source_fd, source_leaf),
            self._parent_fd(destination_parts, create=True) as (destination_fd, destination_leaf),
        ):
            try:
                self._assert_move_destination_available(
                    destination_fd,
                    destination_leaf,
                    destination,
                )
                lease, source_identity, source_mode = self._lease_expected_at(
                    source_fd,
                    source_leaf,
                    source,
                    expected_revision,
                )
                transaction_id = secrets.token_hex(16)
                backup_name, _unused_capture = self._mutation_staging_names(
                    transaction_id,
                    kind="move",
                )
                self._write_durable_file(journal_fd, backup_name, lease.content, source_mode)
                os.fsync(journal_fd)
                transaction = {
                    "schema": _MUTATION_TRANSACTION_SCHEMA,
                    "kind": "move",
                    "transaction_id": transaction_id,
                    "phase": "prepared",
                    "source_path": source,
                    "destination_path": destination,
                    "backup_name": backup_name,
                    "source_device": source_identity[0],
                    "source_inode": source_identity[1],
                    "expected_revision": expected_revision,
                }
                self._write_transaction(journal_fd, transaction, replace=False)
                try:
                    self._rename_noreplace(
                        source_fd,
                        source_leaf,
                        destination_fd,
                        destination_leaf,
                    )
                except FileNotFoundError:
                    actual = self._revision_or_none(source_fd, source_leaf, source)
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise RevisionConflictError(expected_revision, actual) from None
                except FileExistsError as exc:
                    self._recover_mutation_transaction(journal_fd, transaction)
                    try:
                        self._assert_move_destination_available(
                            destination_fd,
                            destination_leaf,
                            destination,
                        )
                    except (NoteAlreadyExistsError, VaultPathError):
                        raise
                    raise NoteAlreadyExistsError(destination) from exc
                os.fsync(source_fd)
                if destination_fd != source_fd:
                    os.fsync(destination_fd)
                if not self._entry_has_identity(
                    destination_fd,
                    destination_leaf,
                    source_identity,
                ):
                    actual = self._revision_or_none(destination_fd, destination_leaf, destination)
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise RevisionConflictError(expected_revision, actual)

                transaction["phase"] = "commit"
                self._write_transaction(journal_fd, transaction, replace=True)
                if not self._entry_has_identity(
                    destination_fd,
                    destination_leaf,
                    source_identity,
                ):
                    actual = self._revision_or_none(destination_fd, destination_leaf, destination)
                    self._recover_mutation_transaction(journal_fd, transaction)
                    raise RevisionConflictError(expected_revision, actual)
                self._discard_mutation_backup(
                    journal_fd,
                    backup_name,
                    expected_revision=expected_revision,
                    conflict_parent_fd=source_fd,
                    conflict_leaf=source_leaf,
                    transaction_id=transaction_id,
                )
                self._remove_transaction(journal_fd)
                transaction = None
            except BaseException:
                if transaction is not None and self._entry_exists(journal_fd, self._transaction_name):
                    try:
                        self._recover_mutation_transaction(journal_fd, transaction)
                    except Exception as recovery_exc:
                        raise OSError("conditional note move recovery failed") from recovery_exc
                raise
            finally:
                if lease is not None:
                    self._release_lease(lease)
        return VaultMoveResult(
            source_path=source,
            destination_path=destination,
            revision=expected_revision,
        )

    def delete_postcondition(
        self,
        relative_path: str | PurePosixPath,
        *,
        expected_revision: str | None = None,
    ) -> bool:
        """Reconcile an uncertain delete without replaying the destructive call."""

        if expected_revision is not None:
            validate_revision(expected_revision)
        try:
            self.read(relative_path)
        except NoteNotFoundError:
            return True
        return False

    def move_postcondition(
        self,
        source_path: str | PurePosixPath,
        destination_path: str | PurePosixPath,
        *,
        expected_revision: str,
    ) -> bool:
        """Return whether one uncertain move reached its exact no-copy state."""

        validate_revision(expected_revision)
        source = self.normalize_path(source_path)
        destination = self.normalize_path(destination_path)
        if source == destination:
            return False
        try:
            self.read(source)
        except NoteNotFoundError:
            pass
        else:
            return False
        try:
            moved = self.read(destination)
        except NoteNotFoundError:
            return False
        return moved.revision == expected_revision

    delete_postcondition_met = delete_postcondition
    move_postcondition_met = move_postcondition

    @staticmethod
    def _generation(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )

    def list_markdown_paths(self) -> tuple[str, ...]:
        """List ordinary Markdown notes, never following file or directory links."""

        return self._list_markdown_paths(conflicts_only=False)

    def list_sync_conflict_paths(self) -> tuple[str, ...]:
        """List preserved Syncthing Markdown conflict copies separately."""

        return self._list_markdown_paths(conflicts_only=True)

    def _list_markdown_paths(self, *, conflicts_only: bool) -> tuple[str, ...]:
        """Walk a bounded set of regular Markdown files through no-follow FDs."""

        return tuple(entry.path for entry in self._markdown_entries(conflicts_only=conflicts_only))

    def iter_markdown_files(
        self,
        *,
        conflicts_only: bool = False,
        max_results: int | None = None,
    ) -> Iterator[VaultFile]:
        """Read every selected note once under one aggregate byte budget."""

        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0
        ):
            raise ValueError("max_results must be a positive integer")
        entries = self._markdown_entries(
            conflicts_only=conflicts_only,
            max_results=max_results,
        )
        consumed = 0
        for entry in entries:
            try:
                stored = self.read(entry.path)
            except NoteNotFoundError:
                continue
            consumed += stored.size_bytes
            if consumed > self.limits.max_total_markdown_bytes:
                raise VaultLimitError(
                    "vault Markdown exceeds the aggregate read budget of "
                    f"{self.limits.max_total_markdown_bytes} bytes"
                )
            yield stored

    def iter_markdown_files_under(
        self,
        relative_directory: str | PurePosixPath,
        *,
        max_results: int | None = None,
    ) -> Iterator[VaultFile]:
        """Read ordinary Markdown files below one contained directory.

        Directory components and entries are opened with ``O_NOFOLLOW``.  A
        missing directory is an empty collection; a symlink in its path is a
        containment error rather than an alias to another tree.
        """

        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0
        ):
            raise ValueError("max_results must be a positive integer")
        normalized = self.normalize_path(relative_directory)
        probe = self.normalize_ordinary_note_path(f"{normalized}/.friday-template-probe.md")
        directory = PurePosixPath(probe).parent.as_posix()
        entries = self._markdown_entries(conflicts_only=False, directory=directory)
        if max_results is not None:
            entries = entries[:max_results]
        consumed = 0
        for entry in entries:
            try:
                stored = self.read(entry.path)
            except NoteNotFoundError:
                continue
            consumed += stored.size_bytes
            if consumed > self.limits.max_total_markdown_bytes:
                raise VaultLimitError(
                    "vault Markdown exceeds the aggregate read budget of "
                    f"{self.limits.max_total_markdown_bytes} bytes"
                )
            yield stored

    def _markdown_entries(
        self,
        *,
        conflicts_only: bool,
        max_results: int | None = None,
        directory: str | None = None,
    ) -> tuple[_MarkdownEntry, ...]:
        """Discover bounded Markdown metadata without materializing a wide directory."""

        entries: list[_MarkdownEntry] = []
        visited_entries = 0
        markdown_paths = 0
        declared_bytes = 0

        def visit(directory_fd: int, prefix: tuple[str, ...]) -> None:
            nonlocal declared_bytes, markdown_paths, visited_entries
            try:
                iterator = os.scandir(directory_fd)
            except OSError as exc:
                raise VaultPathError("vault directory cannot be scanned safely") from exc
            with iterator:
                for entry in iterator:
                    visited_entries += 1
                    if visited_entries > self.limits.max_entries:
                        raise VaultLimitError(
                            f"vault traversal exceeds the maximum entry count of {self.limits.max_entries}"
                        )
                    name = entry.name
                    folded_name = name.casefold()
                    if (not prefix and folded_name in _INTERNAL_VAULT_ROOTS) or folded_name == ".obsidian":
                        continue
                    relative_parts = (*prefix, name)
                    try:
                        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise VaultPathError("vault entry cannot be inspected safely") from exc
                    if stat.S_ISLNK(entry_stat.st_mode):
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if len(relative_parts) > self.limits.max_depth:
                            raise VaultLimitError(
                                "vault traversal exceeds the maximum depth of "
                                f"{self.limits.max_depth} directories"
                            )
                        try:
                            child_fd = os.open(name, _OPEN_DIRECTORY_FLAGS, dir_fd=directory_fd)
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                                continue
                            raise VaultPathError("vault directory cannot be opened safely") from exc
                        try:
                            visit(child_fd, relative_parts)
                        finally:
                            os.close(child_fd)
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode) or not folded_name.endswith(".md"):
                        continue
                    markdown_paths += 1
                    if markdown_paths > self.limits.max_markdown_paths:
                        raise VaultLimitError(
                            "vault traversal exceeds the maximum Markdown path count of "
                            f"{self.limits.max_markdown_paths}"
                        )
                    path = self.normalize_path(PurePosixPath(*relative_parts))
                    if entry_stat.st_size > self.limits.max_note_bytes:
                        raise VaultLimitError(
                            f"note {path!r} exceeds the maximum size of {self.limits.max_note_bytes} bytes"
                        )
                    is_conflict = ".sync-conflict-" in folded_name
                    if is_conflict != conflicts_only:
                        continue
                    if max_results is not None and len(entries) >= max_results:
                        raise VaultLimitError(
                            f"vault note listing exceeds the maximum result count of {max_results}"
                        )
                    declared_bytes += entry_stat.st_size
                    if declared_bytes > self.limits.max_total_markdown_bytes:
                        raise VaultLimitError(
                            "vault Markdown exceeds the aggregate read budget of "
                            f"{self.limits.max_total_markdown_bytes} bytes"
                        )
                    entries.append(_MarkdownEntry(path=path, size_bytes=entry_stat.st_size))

        directory_parts = () if directory is None else PurePosixPath(directory).parts
        with self._lock, self._vault_guard():
            try:
                directory_fd = os.open(self.root, _OPEN_DIRECTORY_FLAGS)
            except OSError as exc:
                raise VaultPathError("vault root cannot be opened safely") from exc
            try:
                try:
                    for component in directory_parts:
                        directory_fd = self._descend(directory_fd, component, create=False)
                except NoteNotFoundError:
                    return ()
                visit(directory_fd, directory_parts)
            finally:
                os.close(directory_fd)
        entries.sort(key=lambda entry: (entry.path.casefold(), entry.path))
        return tuple(entries)

    @contextmanager
    def _parent_fd(self, parts: tuple[str, ...], *, create: bool) -> Iterator[tuple[int, str]]:
        try:
            descriptor = os.open(self.root, _OPEN_DIRECTORY_FLAGS)
        except OSError as exc:
            raise VaultPathError("vault root cannot be opened safely") from exc
        try:
            for component in parts[:-1]:
                descriptor = self._descend(descriptor, component, create=create)
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def _descend(self, parent_fd: int, component: str, *, create: bool) -> int:
        try:
            child_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise NoteNotFoundError(component) from None
            with suppress(FileExistsError):
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            try:
                child_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise VaultPathError("vault directory cannot be created safely") from exc
        except OSError as exc:
            raise VaultPathError("vault directory cannot be inspected safely") from exc
        if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
            raise VaultPathError("vault path traverses a symlink or non-directory")
        try:
            child_fd = os.open(component, _OPEN_DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise VaultPathError("vault directory cannot be opened safely") from exc
        os.close(parent_fd)
        return child_fd

    def _lease_expected_at(
        self,
        parent_fd: int,
        leaf: str,
        relative_path: str,
        expected_revision: str,
    ) -> tuple[_LeasedFile, tuple[int, int], int]:
        """Lease one exact regular-file entry and validate its observed bytes."""

        try:
            candidate = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise RevisionConflictError(expected_revision, None) from None
        except OSError as exc:
            raise VaultPathError("vault source cannot be inspected safely") from exc
        if stat.S_ISLNK(candidate.st_mode) or not stat.S_ISREG(candidate.st_mode):
            raise VaultPathError("vault source is a symlink or non-regular file")
        lease = self._lease_and_read(parent_fd, leaf)
        if lease is None:
            actual = self._revision_or_none(parent_fd, leaf, relative_path)
            raise RevisionConflictError(expected_revision, actual)
        opened = os.fstat(lease.descriptor)
        identity = (int(opened.st_dev), int(opened.st_ino))
        actual_revision = hashlib.sha256(lease.content).hexdigest()
        if actual_revision != expected_revision or not self._entry_has_identity(
            parent_fd,
            leaf,
            identity,
        ):
            self._release_lease(lease)
            raise RevisionConflictError(expected_revision, actual_revision)
        return lease, identity, stat.S_IMODE(opened.st_mode)

    @staticmethod
    def _assert_move_destination_available(
        parent_fd: int,
        leaf: str,
        relative_path: str,
    ) -> None:
        try:
            destination = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise VaultPathError("vault move destination cannot be inspected safely") from exc
        if stat.S_ISLNK(destination.st_mode):
            raise VaultPathError("vault move destination is a symlink")
        raise NoteAlreadyExistsError(relative_path)

    def _read_at(self, parent_fd: int, leaf: str, relative_path: str) -> tuple[bytes, os.stat_result]:
        try:
            leaf_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise NoteNotFoundError(relative_path) from None
        except OSError as exc:
            raise VaultPathError("vault file cannot be inspected safely") from exc
        if stat.S_ISLNK(leaf_stat.st_mode) or not stat.S_ISREG(leaf_stat.st_mode):
            raise VaultPathError("vault path is a symlink or non-regular file")
        if leaf_stat.st_size > self.limits.max_note_bytes:
            raise VaultLimitError(
                f"note {relative_path!r} exceeds the maximum size of {self.limits.max_note_bytes} bytes"
            )
        try:
            file_fd = os.open(leaf, _OPEN_FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            raise NoteNotFoundError(relative_path) from None
        except OSError as exc:
            reason = (
                "vault path is a symlink"
                if exc.errno == errno.ELOOP
                else "vault file cannot be opened safely"
            )
            raise VaultPathError(reason) from exc
        try:
            opened_stat = os.fstat(file_fd)
            if not stat.S_ISREG(opened_stat.st_mode) or (opened_stat.st_dev, opened_stat.st_ino) != (
                leaf_stat.st_dev,
                leaf_stat.st_ino,
            ):
                raise VaultPathError("vault path is not a regular file")
            if opened_stat.st_size > self.limits.max_note_bytes:
                raise VaultLimitError(
                    f"note {relative_path!r} exceeds the maximum size of {self.limits.max_note_bytes} bytes"
                )
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                content = stream.read(self.limits.max_note_bytes + 1)
            final_stat = os.fstat(file_fd)
            if (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
                opened_stat.st_ctime_ns,
            ) != (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_size,
                final_stat.st_mtime_ns,
                final_stat.st_ctime_ns,
            ):
                raise VaultPathError("vault file changed while reading")
            if len(content) > self.limits.max_note_bytes:
                raise VaultLimitError(
                    f"note {relative_path!r} exceeds the maximum size of {self.limits.max_note_bytes} bytes"
                )
            return content, opened_stat
        finally:
            os.close(file_fd)

    def _atomic_replace(
        self,
        parent_fd: int,
        leaf: str,
        relative_path: str,
        content: bytes,
        *,
        existing_mode: int,
        create_only: bool,
        expected_revision: str | None,
        journal_fd: int,
    ) -> None:
        try:
            destination_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and stat.S_ISLNK(destination_stat.st_mode):
            raise VaultPathError("vault destination is a symlink")

        if expected_revision is not None:
            try:
                self._publish_if_revision(
                    journal_fd,
                    parent_fd,
                    leaf,
                    relative_path,
                    content,
                    existing_mode=existing_mode,
                    expected_revision=expected_revision,
                )
            except RevisionConflictError:
                raise
            except OSError as exc:
                raise OSError(f"atomic write failed for {relative_path}") from exc
            return

        temporary = f".friday-{secrets.token_hex(16)}.tmp"
        temporary_fd: int | None = None
        try:
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                existing_mode,
                dir_fd=parent_fd,
            )
            view = memoryview(content)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:  # pragma: no cover - defensive kernel postcondition
                    raise OSError("short write while persisting note")
                view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            if create_only:
                # linkat is the portable Unix no-clobber publication primitive:
                # unlike rename, it fails atomically if a peer created the note.
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=parent_fd)
            else:
                os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError as exc:
            if create_only:
                raise NoteAlreadyExistsError(relative_path) from exc
            raise OSError(f"atomic write failed for {relative_path}") from exc
        except OSError as exc:
            raise OSError(f"atomic write failed for {relative_path}") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_fd)

    def _publish_if_revision(
        self,
        journal_fd: int,
        parent_fd: int,
        leaf: str,
        relative_path: str,
        content: bytes,
        *,
        existing_mode: int,
        expected_revision: str,
    ) -> None:
        """Exchange a proposal atomically and recover every incomplete decision."""

        transaction_id = secrets.token_hex(16)
        swap_name, proposal_name = self._staging_names(transaction_id)
        proposal_revision = hashlib.sha256(content).hexdigest()
        swap_stat: os.stat_result | None = None
        lease: _LeasedFile | None = None
        try:
            swap_stat = self._write_durable_file(journal_fd, swap_name, content, existing_mode)
            self._write_durable_file(journal_fd, proposal_name, content, existing_mode)
            os.fsync(journal_fd)
            transaction: dict[str, object] = {
                "schema": _TRANSACTION_SCHEMA,
                "transaction_id": transaction_id,
                "phase": "prepared",
                "path": relative_path,
                "swap_name": swap_name,
                "proposal_name": proposal_name,
                "proposal_device": int(swap_stat.st_dev),
                "proposal_inode": int(swap_stat.st_ino),
                "proposal_revision": proposal_revision,
                "expected_revision": expected_revision,
            }
            self._write_transaction(journal_fd, transaction, replace=False)
            try:
                self._rename_exchange(journal_fd, swap_name, parent_fd, leaf)
            except FileNotFoundError:
                self._discard_staging(journal_fd, swap_name, proposal_name)
                self._remove_transaction(journal_fd)
                raise RevisionConflictError(expected_revision, None) from None
            os.fsync(parent_fd)
            os.fsync(journal_fd)

            if not self._canonical_is_proposal(
                parent_fd,
                leaf,
                relative_path,
                proposal_identity=(int(swap_stat.st_dev), int(swap_stat.st_ino)),
                proposal_revision=proposal_revision,
            ):
                actual = self._settle_peer_won(journal_fd, parent_fd, leaf, transaction)
                raise RevisionConflictError(expected_revision, actual)

            leased = self._lease_and_read(journal_fd, swap_name)
            if leased is None:
                actual = self._rollback_transaction(journal_fd, parent_fd, leaf, transaction)
                raise RevisionConflictError(expected_revision, actual)
            lease = leased
            captured_revision = hashlib.sha256(lease.content).hexdigest()
            if captured_revision != expected_revision:
                self._release_lease(lease)
                lease = None
                self._rollback_transaction(journal_fd, parent_fd, leaf, transaction)
                raise RevisionConflictError(expected_revision, captured_revision)
            if not self._canonical_is_proposal(
                parent_fd,
                leaf,
                relative_path,
                proposal_identity=(int(swap_stat.st_dev), int(swap_stat.st_ino)),
                proposal_revision=proposal_revision,
            ):
                self._release_lease(lease)
                lease = None
                actual = self._settle_peer_won(journal_fd, parent_fd, leaf, transaction)
                raise RevisionConflictError(expected_revision, actual)

            transaction["phase"] = "commit"
            self._write_transaction(journal_fd, transaction, replace=True)
            os.unlink(swap_name, dir_fd=journal_fd)
            os.fsync(journal_fd)
            self._release_lease(lease)
            lease = None
            os.unlink(proposal_name, dir_fd=journal_fd)
            os.fsync(journal_fd)
            self._remove_transaction(journal_fd)
        except BaseException:
            if lease is not None:
                self._release_lease(lease)
            if self._entry_exists(journal_fd, self._transaction_name):
                try:
                    self._recover_transaction(journal_fd)
                except Exception as recovery_exc:
                    raise OSError("conditional note publication recovery failed") from recovery_exc
            elif swap_stat is not None:
                self._discard_staging(journal_fd, swap_name, proposal_name)
            raise

    @contextmanager
    def _vault_guard(self) -> Iterator[tuple[int, int]]:
        root_fd: int | None = None
        journal_fd: int | None = None
        try:
            try:
                root_fd = os.open(self.root, _OPEN_DIRECTORY_FLAGS)
                root_stat = os.fstat(root_fd)
                if (int(root_stat.st_dev), int(root_stat.st_ino)) != self._root_identity:
                    raise VaultPathError("vault root changed after configuration")
                fcntl.flock(root_fd, fcntl.LOCK_EX)
                journal_fd = os.open(self.root.parent, _OPEN_DIRECTORY_FLAGS)
                self._recover_transaction(journal_fd)
            except OSError as exc:
                if isinstance(exc, VaultPathError):
                    raise
                raise VaultPathError("vault transaction boundary is unavailable") from exc
            yield root_fd, journal_fd
        finally:
            if journal_fd is not None:
                os.close(journal_fd)
            if root_fd is not None:
                with suppress(OSError):
                    fcntl.flock(root_fd, fcntl.LOCK_UN)
                os.close(root_fd)

    @staticmethod
    def _renameat2(
        source_fd: int,
        source: str,
        target_fd: int,
        target: str,
        flags: int,
    ) -> None:
        if _RENAMEAT2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is required for conditional vault writes")
        result = _RENAMEAT2(
            source_fd,
            os.fsencode(source),
            target_fd,
            os.fsencode(target),
            flags,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), source, target)

    def _rename_exchange(self, source_fd: int, source: str, target_fd: int, target: str) -> None:
        self._renameat2(source_fd, source, target_fd, target, _RENAME_EXCHANGE)

    def _rename_noreplace(self, source_fd: int, source: str, target_fd: int, target: str) -> None:
        self._renameat2(source_fd, source, target_fd, target, _RENAME_NOREPLACE)

    def _staging_names(self, transaction_id: str) -> tuple[str, str]:
        base = self._transaction_name.removesuffix(".txn")
        return f"{base}-{transaction_id}.swap", f"{base}-{transaction_id}.proposal"

    def _mutation_staging_names(self, transaction_id: str, *, kind: str) -> tuple[str, str]:
        if kind not in {"delete", "move"}:
            raise ValueError("unsupported vault mutation kind")
        base = self._transaction_name.removesuffix(".txn")
        return (
            f"{base}-{transaction_id}.{kind}.backup",
            f"{base}-{transaction_id}.{kind}.capture",
        )

    @staticmethod
    def _write_durable_file(directory_fd: int, name: str, content: bytes, mode: int) -> os.stat_result:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                mode,
                dir_fd=directory_fd,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive kernel postcondition
                    raise OSError("short write while persisting vault transaction")
                view = view[written:]
            os.fsync(descriptor)
            return os.fstat(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write_transaction(
        self,
        journal_fd: int,
        transaction: dict[str, object],
        *,
        replace: bool,
    ) -> None:
        encoded = (
            json.dumps(transaction, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8", errors="strict")
        if len(encoded) > _MAX_TRANSACTION_BYTES:
            raise VaultLimitError("vault transaction metadata is too large")
        if replace:
            with suppress(FileNotFoundError):
                os.unlink(self._transaction_next_name, dir_fd=journal_fd)
            self._write_durable_file(journal_fd, self._transaction_next_name, encoded, 0o600)
            os.replace(
                self._transaction_next_name,
                self._transaction_name,
                src_dir_fd=journal_fd,
                dst_dir_fd=journal_fd,
            )
        else:
            self._write_durable_file(journal_fd, self._transaction_name, encoded, 0o600)
        os.fsync(journal_fd)

    def _read_transaction(self, journal_fd: int) -> dict[str, object] | None:
        try:
            descriptor = os.open(self._transaction_name, _OPEN_FILE_FLAGS, dir_fd=journal_fd)
        except FileNotFoundError:
            if self._entry_exists(journal_fd, self._transaction_next_name):
                raise VaultPathError("orphaned vault transaction update") from None
            return None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_TRANSACTION_BYTES:
                raise VaultPathError("vault transaction metadata is not a bounded regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(_MAX_TRANSACTION_BYTES + 1)
            final = os.fstat(descriptor)
            if self._generation(opened) != self._generation(final) or len(encoded) > _MAX_TRANSACTION_BYTES:
                raise VaultPathError("vault transaction metadata changed while reading")
        finally:
            os.close(descriptor)
        try:
            raw = json.loads(encoded.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultPathError("vault transaction metadata is invalid") from exc
        if not isinstance(raw, dict):
            raise VaultPathError("vault transaction metadata is not an object")
        if raw.get("schema") == _MUTATION_TRANSACTION_SCHEMA:
            return self._validate_mutation_transaction(raw)
        required = {
            "schema",
            "transaction_id",
            "phase",
            "path",
            "swap_name",
            "proposal_name",
            "proposal_device",
            "proposal_inode",
            "proposal_revision",
            "expected_revision",
        }
        if set(raw) != required or raw.get("schema") != _TRANSACTION_SCHEMA:
            raise VaultPathError("vault transaction metadata has an unsupported schema")
        transaction_id = raw.get("transaction_id")
        if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise VaultPathError("vault transaction identifier is invalid")
        if raw.get("phase") not in {"prepared", "commit"}:
            raise VaultPathError("vault transaction phase is invalid")
        path = raw.get("path")
        if not isinstance(path, str) or self.normalize_path(path) != path:
            raise VaultPathError("vault transaction path is invalid")
        swap_name, proposal_name = self._staging_names(transaction_id)
        if raw.get("swap_name") != swap_name or raw.get("proposal_name") != proposal_name:
            raise VaultPathError("vault transaction staging names are invalid")
        for field in ("proposal_device", "proposal_inode"):
            value = raw.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VaultPathError("vault transaction inode identity is invalid")
        for field in ("proposal_revision", "expected_revision"):
            value = raw.get(field)
            if not isinstance(value, str):
                raise VaultPathError("vault transaction revision is invalid")
            validate_revision(value)
        return dict(raw)

    def _validate_mutation_transaction(self, raw: dict[object, object]) -> dict[str, object]:
        kind = raw.get("kind")
        common = {
            "schema",
            "kind",
            "transaction_id",
            "phase",
            "source_path",
            "backup_name",
            "source_device",
            "source_inode",
            "expected_revision",
        }
        if kind == "delete":
            required = common | {"capture_name"}
        elif kind == "move":
            required = common | {"destination_path"}
        else:
            raise VaultPathError("vault mutation kind is invalid")
        if set(raw) != required:
            raise VaultPathError("vault mutation metadata has an unsupported shape")
        transaction_id = raw.get("transaction_id")
        if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise VaultPathError("vault mutation identifier is invalid")
        if raw.get("phase") not in {"prepared", "commit"}:
            raise VaultPathError("vault mutation phase is invalid")
        source_path = raw.get("source_path")
        if not isinstance(source_path, str) or self.normalize_path(source_path) != source_path:
            raise VaultPathError("vault mutation source path is invalid")
        if kind == "move":
            destination_path = raw.get("destination_path")
            if (
                not isinstance(destination_path, str)
                or self.normalize_path(destination_path) != destination_path
                or destination_path == source_path
            ):
                raise VaultPathError("vault mutation destination path is invalid")
        backup_name, capture_name = self._mutation_staging_names(transaction_id, kind=str(kind))
        if raw.get("backup_name") != backup_name:
            raise VaultPathError("vault mutation backup name is invalid")
        if kind == "delete" and raw.get("capture_name") != capture_name:
            raise VaultPathError("vault mutation capture name is invalid")
        for field in ("source_device", "source_inode"):
            value = raw.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VaultPathError("vault mutation inode identity is invalid")
        expected_revision = raw.get("expected_revision")
        if not isinstance(expected_revision, str):
            raise VaultPathError("vault mutation revision is invalid")
        try:
            validate_revision(expected_revision)
        except ValueError as exc:
            raise VaultPathError("vault mutation revision is invalid") from exc
        return {str(key): value for key, value in raw.items()}

    def _recover_transaction(self, journal_fd: int) -> None:
        transaction = self._read_transaction(journal_fd)
        if transaction is None:
            return
        if transaction.get("schema") == _MUTATION_TRANSACTION_SCHEMA:
            self._recover_mutation_transaction(journal_fd, transaction)
            return
        path = str(transaction["path"])
        parts = PurePosixPath(path).parts
        try:
            parent_context = self._parent_fd(parts, create=False)
            with parent_context as (parent_fd, leaf):
                self._recover_transaction_at(journal_fd, parent_fd, leaf, transaction)
        except NoteNotFoundError as exc:
            raise VaultPathError("vault transaction parent directory disappeared") from exc

    def _recover_mutation_transaction(
        self,
        journal_fd: int,
        transaction: dict[str, object],
    ) -> None:
        source = str(transaction["source_path"])
        source_parts = PurePosixPath(source).parts
        with self._parent_fd(source_parts, create=True) as (source_fd, source_leaf):
            if transaction["kind"] == "delete":
                self._recover_delete_transaction(
                    journal_fd,
                    source_fd,
                    source_leaf,
                    transaction,
                )
                return
            destination = str(transaction["destination_path"])
            destination_parts = PurePosixPath(destination).parts
            with self._parent_fd(destination_parts, create=True) as (
                destination_fd,
                destination_leaf,
            ):
                self._recover_move_transaction(
                    journal_fd,
                    source_fd,
                    source_leaf,
                    destination_fd,
                    destination_leaf,
                    transaction,
                )

    def _recover_delete_transaction(
        self,
        journal_fd: int,
        source_fd: int,
        source_leaf: str,
        transaction: dict[str, object],
    ) -> None:
        expected_revision = str(transaction["expected_revision"])
        transaction_id = str(transaction["transaction_id"])
        backup_name = str(transaction["backup_name"])
        capture_name = str(transaction["capture_name"])
        source_identity = self._mutation_identity(transaction)
        capture_exists = self._entry_exists(journal_fd, capture_name)
        capture_is_source = self._entry_has_identity(
            journal_fd,
            capture_name,
            source_identity,
        )
        source_exists = self._entry_exists(source_fd, source_leaf)

        if transaction["phase"] == "commit":
            if capture_exists:
                if capture_is_source:
                    self._unlink_staging_identity(journal_fd, capture_name, source_identity)
                elif not source_exists:
                    self._require_regular_staging(journal_fd, capture_name)
                    self._rename_noreplace(journal_fd, capture_name, source_fd, source_leaf)
                    os.fsync(source_fd)
                    os.fsync(journal_fd)
                else:
                    self._move_to_conflict(
                        journal_fd,
                        capture_name,
                        source_fd,
                        source_leaf,
                        kind="delete-race",
                        transaction_id=transaction_id,
                    )
            self._discard_mutation_backup(
                journal_fd,
                backup_name,
                expected_revision=expected_revision,
                conflict_parent_fd=source_fd,
                conflict_leaf=source_leaf,
                transaction_id=transaction_id,
            )
            self._remove_transaction(journal_fd)
            return

        if capture_is_source:
            if not source_exists:
                self._rename_noreplace(journal_fd, capture_name, source_fd, source_leaf)
                os.fsync(source_fd)
                os.fsync(journal_fd)
            else:
                self._move_to_conflict(
                    journal_fd,
                    capture_name,
                    source_fd,
                    source_leaf,
                    kind="delete-rollback",
                    transaction_id=transaction_id,
                )
            self._discard_mutation_backup(
                journal_fd,
                backup_name,
                expected_revision=expected_revision,
                conflict_parent_fd=source_fd,
                conflict_leaf=source_leaf,
                transaction_id=transaction_id,
            )
        elif capture_exists:
            self._require_regular_staging(journal_fd, capture_name)
            if not source_exists:
                self._rename_noreplace(journal_fd, capture_name, source_fd, source_leaf)
                os.fsync(source_fd)
                os.fsync(journal_fd)
            else:
                self._move_to_conflict(
                    journal_fd,
                    capture_name,
                    source_fd,
                    source_leaf,
                    kind="delete-race",
                    transaction_id=transaction_id,
                )
            self._restore_or_preserve_mutation_backup(
                journal_fd,
                backup_name,
                source_fd,
                source_leaf,
                expected_revision=expected_revision,
                transaction_id=transaction_id,
                kind="delete-observed",
            )
        elif (
            self._revision_or_none(source_fd, source_leaf, str(transaction["source_path"]))
            == expected_revision
        ):
            self._discard_mutation_backup(
                journal_fd,
                backup_name,
                expected_revision=expected_revision,
                conflict_parent_fd=source_fd,
                conflict_leaf=source_leaf,
                transaction_id=transaction_id,
            )
        else:
            self._restore_or_preserve_mutation_backup(
                journal_fd,
                backup_name,
                source_fd,
                source_leaf,
                expected_revision=expected_revision,
                transaction_id=transaction_id,
                kind="delete-observed",
            )
        self._remove_transaction(journal_fd)

    def _recover_move_transaction(
        self,
        journal_fd: int,
        source_fd: int,
        source_leaf: str,
        destination_fd: int,
        destination_leaf: str,
        transaction: dict[str, object],
    ) -> None:
        expected_revision = str(transaction["expected_revision"])
        transaction_id = str(transaction["transaction_id"])
        backup_name = str(transaction["backup_name"])
        source_identity = self._mutation_identity(transaction)
        source_is_observed = self._entry_has_identity(source_fd, source_leaf, source_identity)
        destination_is_observed = self._entry_has_identity(
            destination_fd,
            destination_leaf,
            source_identity,
        )

        if destination_is_observed and transaction["phase"] == "prepared":
            if not self._entry_exists(source_fd, source_leaf):
                self._rename_noreplace(
                    destination_fd,
                    destination_leaf,
                    source_fd,
                    source_leaf,
                )
                os.fsync(source_fd)
                if destination_fd != source_fd:
                    os.fsync(destination_fd)
            else:
                self._move_to_conflict(
                    destination_fd,
                    destination_leaf,
                    source_fd,
                    source_leaf,
                    kind="move-rollback",
                    transaction_id=transaction_id,
                )
            self._discard_mutation_backup(
                journal_fd,
                backup_name,
                expected_revision=expected_revision,
                conflict_parent_fd=source_fd,
                conflict_leaf=source_leaf,
                transaction_id=transaction_id,
            )
        elif destination_is_observed or source_is_observed:
            self._discard_mutation_backup(
                journal_fd,
                backup_name,
                expected_revision=expected_revision,
                conflict_parent_fd=source_fd,
                conflict_leaf=source_leaf,
                transaction_id=transaction_id,
            )
        else:
            self._restore_or_preserve_mutation_backup(
                journal_fd,
                backup_name,
                source_fd,
                source_leaf,
                expected_revision=expected_revision,
                transaction_id=transaction_id,
                kind="move-observed",
            )
        self._remove_transaction(journal_fd)

    @staticmethod
    def _mutation_identity(transaction: dict[str, object]) -> tuple[int, int]:
        device = transaction.get("source_device")
        inode = transaction.get("source_inode")
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise VaultPathError("vault mutation inode identity is invalid")
        return device, inode

    def _restore_or_preserve_mutation_backup(
        self,
        journal_fd: int,
        backup_name: str,
        source_fd: int,
        source_leaf: str,
        *,
        expected_revision: str,
        transaction_id: str,
        kind: str,
    ) -> None:
        if not self._entry_exists(journal_fd, backup_name):
            raise VaultPathError("vault mutation lost its durable observed revision")
        if self._revision_or_none(source_fd, source_leaf, source_leaf) == expected_revision:
            self._discard_mutation_backup(
                journal_fd,
                backup_name,
                expected_revision=expected_revision,
                conflict_parent_fd=source_fd,
                conflict_leaf=source_leaf,
                transaction_id=transaction_id,
            )
            return
        if not self._entry_exists(source_fd, source_leaf):
            self._require_regular_staging(journal_fd, backup_name)
            self._rename_noreplace(journal_fd, backup_name, source_fd, source_leaf)
            os.fsync(source_fd)
            os.fsync(journal_fd)
            return
        self._move_to_conflict(
            journal_fd,
            backup_name,
            source_fd,
            source_leaf,
            kind=kind,
            transaction_id=transaction_id,
        )

    def _discard_mutation_backup(
        self,
        journal_fd: int,
        backup_name: str,
        *,
        expected_revision: str,
        conflict_parent_fd: int,
        conflict_leaf: str,
        transaction_id: str,
    ) -> None:
        if not self._entry_exists(journal_fd, backup_name):
            return
        if self._unlink_exact_under_lease(journal_fd, backup_name, expected_revision):
            return
        self._move_to_conflict(
            journal_fd,
            backup_name,
            conflict_parent_fd,
            conflict_leaf,
            kind="mutation-backup",
            transaction_id=transaction_id,
        )
        raise VaultPathError("vault mutation backup changed before cleanup")

    @staticmethod
    def _require_regular_staging(directory_fd: int, name: str) -> os.stat_result:
        try:
            item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise VaultPathError("vault mutation staging entry cannot be inspected safely") from exc
        if not stat.S_ISREG(item.st_mode):
            raise VaultPathError("vault mutation staging entry is not a regular file")
        return item

    def _unlink_staging_identity(
        self,
        directory_fd: int,
        name: str,
        identity: tuple[int, int],
    ) -> None:
        self._require_regular_staging(directory_fd, name)
        if not self._entry_has_identity(directory_fd, name, identity):
            raise VaultPathError("vault mutation staging identity changed")
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)

    def _recover_transaction_at(
        self,
        journal_fd: int,
        parent_fd: int,
        leaf: str,
        transaction: dict[str, object],
    ) -> None:
        swap_name = str(transaction["swap_name"])
        proposal_name = str(transaction["proposal_name"])
        proposal_identity = self._transaction_identity(transaction)
        proposal_revision = str(transaction["proposal_revision"])
        relative_path = str(transaction["path"])
        leaf_is_proposal = self._canonical_is_proposal(
            parent_fd,
            leaf,
            relative_path,
            proposal_identity=proposal_identity,
            proposal_revision=proposal_revision,
        )
        swap_is_proposal = self._entry_has_identity(journal_fd, swap_name, proposal_identity)
        swap_exists = self._entry_exists(journal_fd, swap_name)

        if transaction["phase"] == "commit":
            if leaf_is_proposal:
                if swap_exists:
                    swap_stat = os.stat(swap_name, dir_fd=journal_fd, follow_symlinks=False)
                    if not stat.S_ISREG(swap_stat.st_mode) or not self._unlink_exact_under_lease(
                        journal_fd,
                        swap_name,
                        str(transaction["expected_revision"]),
                    ):
                        self._move_to_conflict(
                            journal_fd,
                            swap_name,
                            parent_fd,
                            leaf,
                            kind="external",
                            transaction_id=str(transaction["transaction_id"]),
                        )
                with suppress(FileNotFoundError):
                    os.unlink(proposal_name, dir_fd=journal_fd)
                os.fsync(journal_fd)
                self._remove_transaction(journal_fd)
                return
            self._settle_peer_won(journal_fd, parent_fd, leaf, transaction)
            return

        if leaf_is_proposal:
            if not swap_exists:
                raise VaultPathError("prepared vault transaction lost its captured peer entry")
            self._rename_exchange(journal_fd, swap_name, parent_fd, leaf)
            os.fsync(parent_fd)
            os.fsync(journal_fd)
            self._finish_rollback(journal_fd, parent_fd, leaf, transaction)
            return
        if swap_is_proposal:
            if self._unlink_exact_under_lease(
                journal_fd,
                swap_name,
                proposal_revision,
            ):
                with suppress(FileNotFoundError):
                    os.unlink(proposal_name, dir_fd=journal_fd)
                os.fsync(journal_fd)
            else:
                self._move_to_conflict(
                    journal_fd,
                    swap_name,
                    parent_fd,
                    leaf,
                    kind="external",
                    transaction_id=str(transaction["transaction_id"]),
                )
                self._move_to_conflict(
                    journal_fd,
                    proposal_name,
                    parent_fd,
                    leaf,
                    kind="friday-write",
                    transaction_id=str(transaction["transaction_id"]),
                )
            self._remove_transaction(journal_fd)
            return
        if not self._entry_exists(parent_fd, leaf) and swap_exists:
            try:
                self._rename_noreplace(journal_fd, swap_name, parent_fd, leaf)
            except FileExistsError:
                self._settle_peer_won(journal_fd, parent_fd, leaf, transaction)
                return
            os.fsync(parent_fd)
            os.fsync(journal_fd)
            with suppress(FileNotFoundError):
                os.unlink(proposal_name, dir_fd=journal_fd)
            self._remove_transaction(journal_fd)
            return
        if not swap_exists and not self._entry_exists(parent_fd, leaf):
            raise VaultPathError("prepared vault transaction lost both canonical and captured entries")
        self._settle_peer_won(journal_fd, parent_fd, leaf, transaction)

    def _rollback_transaction(
        self,
        journal_fd: int,
        parent_fd: int,
        leaf: str,
        transaction: dict[str, object],
    ) -> str | None:
        proposal_identity = self._transaction_identity(transaction)
        if not self._canonical_is_proposal(
            parent_fd,
            leaf,
            str(transaction["path"]),
            proposal_identity=proposal_identity,
            proposal_revision=str(transaction["proposal_revision"]),
        ):
            return self._settle_peer_won(journal_fd, parent_fd, leaf, transaction)
        self._rename_exchange(journal_fd, str(transaction["swap_name"]), parent_fd, leaf)
        os.fsync(parent_fd)
        os.fsync(journal_fd)
        return self._finish_rollback(journal_fd, parent_fd, leaf, transaction)

    def _finish_rollback(
        self,
        journal_fd: int,
        parent_fd: int,
        leaf: str,
        transaction: dict[str, object],
    ) -> str | None:
        swap_name = str(transaction["swap_name"])
        proposal_name = str(transaction["proposal_name"])
        proposal_identity = self._transaction_identity(transaction)
        if self._entry_has_identity(
            journal_fd, swap_name, proposal_identity
        ) and self._unlink_exact_under_lease(
            journal_fd,
            swap_name,
            str(transaction["proposal_revision"]),
        ):
            pass
        elif self._entry_exists(journal_fd, swap_name):
            self._move_to_conflict(
                journal_fd,
                swap_name,
                parent_fd,
                leaf,
                kind="external",
                transaction_id=str(transaction["transaction_id"]),
            )
        self._move_to_conflict(
            journal_fd,
            proposal_name,
            parent_fd,
            leaf,
            kind="friday-write",
            transaction_id=str(transaction["transaction_id"]),
        )
        os.fsync(journal_fd)
        self._remove_transaction(journal_fd)
        return self._revision_or_none(parent_fd, leaf, str(transaction["path"]))

    def _settle_peer_won(
        self,
        journal_fd: int,
        parent_fd: int,
        leaf: str,
        transaction: dict[str, object],
    ) -> str | None:
        transaction_id = str(transaction["transaction_id"])
        self._move_to_conflict(
            journal_fd,
            str(transaction["swap_name"]),
            parent_fd,
            leaf,
            kind="external",
            transaction_id=transaction_id,
        )
        self._move_to_conflict(
            journal_fd,
            str(transaction["proposal_name"]),
            parent_fd,
            leaf,
            kind="friday-write",
            transaction_id=transaction_id,
        )
        os.fsync(journal_fd)
        self._remove_transaction(journal_fd)
        return self._revision_or_none(parent_fd, leaf, str(transaction["path"]))

    def _move_to_conflict(
        self,
        source_fd: int,
        source: str,
        parent_fd: int,
        leaf: str,
        *,
        kind: str,
        transaction_id: str,
    ) -> None:
        if not self._entry_exists(source_fd, source):
            return
        conflict = self._conflict_name(leaf, kind=kind, transaction_id=transaction_id)
        self._rename_noreplace(source_fd, source, parent_fd, conflict)
        os.fsync(parent_fd)
        os.fsync(source_fd)

    @staticmethod
    def _conflict_name(leaf: str, *, kind: str, transaction_id: str) -> str:
        suffix = ".md" if leaf.casefold().endswith(".md") else ".md"
        stem = leaf[: -len(suffix)] if leaf.casefold().endswith(suffix) else leaf
        marker = f".sync-conflict-friday-{kind}-{transaction_id}"
        maximum = 255 - len((marker + suffix).encode("utf-8"))
        while len(stem.encode("utf-8")) > maximum:
            stem = stem[:-1]
        return f"{stem or 'note'}{marker}{suffix}"

    def _canonical_is_proposal(
        self,
        parent_fd: int,
        leaf: str,
        relative_path: str,
        *,
        proposal_identity: tuple[int, int],
        proposal_revision: str,
    ) -> bool:
        if not self._entry_has_identity(parent_fd, leaf, proposal_identity):
            return False
        try:
            content, _item_stat = self._read_at(parent_fd, leaf, relative_path)
        except (NoteNotFoundError, VaultPathError, VaultLimitError):
            return False
        return hashlib.sha256(content).hexdigest() == proposal_revision

    def _lease_and_read(self, directory_fd: int, name: str) -> _LeasedFile | None:
        try:
            candidate = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(candidate.st_mode) or candidate.st_size > self.limits.max_note_bytes:
            return None
        try:
            descriptor = os.open(name, _OPEN_MUTABLE_FILE_FLAGS, dir_fd=directory_fd)
        except OSError:
            return None
        previous_signal_mask = frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, {_LEASE_BREAK_SIGNAL}))
        signal_was_pending = _LEASE_BREAK_SIGNAL in signal.sigpending()
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (candidate.st_dev, candidate.st_ino)
                or opened.st_size > self.limits.max_note_bytes
            ):
                os.close(descriptor)
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
                return None
            try:
                owner = array("i", (fcntl.F_OWNER_TID, threading.get_native_id()))
                fcntl.fcntl(descriptor, fcntl.F_SETOWN_EX, owner)
                fcntl.fcntl(descriptor, fcntl.F_SETSIG, _LEASE_BREAK_SIGNAL)
                fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_WRLCK)
            except OSError as exc:
                os.close(descriptor)
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
                if exc.errno in _LEASE_REFUSED:
                    return None
                raise
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = os.read(descriptor, self.limits.max_note_bytes + 1)
            final = os.fstat(descriptor)
            if (
                self._generation(opened) != self._generation(final)
                or len(content) > self.limits.max_note_bytes
            ):
                self._release_lease(
                    _LeasedFile(
                        descriptor=descriptor,
                        content=b"",
                        previous_signal_mask=previous_signal_mask,
                        signal_was_pending=signal_was_pending,
                    )
                )
                return None
            return _LeasedFile(
                descriptor=descriptor,
                content=content,
                previous_signal_mask=previous_signal_mask,
                signal_was_pending=signal_was_pending,
            )
        except BaseException:
            with suppress(OSError):
                fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
            with suppress(OSError):
                os.close(descriptor)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
            raise

    def _unlink_exact_under_lease(
        self,
        directory_fd: int,
        name: str,
        expected_revision: str,
    ) -> bool:
        leased = self._lease_and_read(directory_fd, name)
        if leased is None:
            return False
        try:
            if hashlib.sha256(leased.content).hexdigest() != expected_revision:
                return False
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            return True
        finally:
            self._release_lease(leased)

    @staticmethod
    def _release_lease(lease: _LeasedFile) -> None:
        try:
            fcntl.fcntl(lease.descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        finally:
            os.close(lease.descriptor)
            if not lease.signal_was_pending:
                while _LEASE_BREAK_SIGNAL in signal.sigpending():
                    signal.sigtimedwait({_LEASE_BREAK_SIGNAL}, 0)
            signal.pthread_sigmask(signal.SIG_SETMASK, lease.previous_signal_mask)

    @staticmethod
    def _entry_exists(directory_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _entry_has_identity(directory_fd: int, name: str, identity: tuple[int, int]) -> bool:
        try:
            item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (int(item_stat.st_dev), int(item_stat.st_ino)) == identity

    @staticmethod
    def _transaction_identity(transaction: dict[str, object]) -> tuple[int, int]:
        device = transaction.get("proposal_device")
        inode = transaction.get("proposal_inode")
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise VaultPathError("vault transaction inode identity is invalid")
        return device, inode

    def _revision_or_none(self, parent_fd: int, leaf: str, relative_path: str) -> str | None:
        try:
            content, _item_stat = self._read_at(parent_fd, leaf, relative_path)
        except (NoteNotFoundError, VaultPathError, VaultLimitError):
            return None
        return hashlib.sha256(content).hexdigest()

    def _discard_staging(self, journal_fd: int, swap_name: str, proposal_name: str) -> None:
        for name in (swap_name, proposal_name, self._transaction_next_name):
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=journal_fd)
        os.fsync(journal_fd)

    def _remove_transaction(self, journal_fd: int) -> None:
        with suppress(FileNotFoundError):
            os.unlink(self._transaction_next_name, dir_fd=journal_fd)
        os.unlink(self._transaction_name, dir_fd=journal_fd)
        os.fsync(journal_fd)


__all__ = [
    "VaultDeleteResult",
    "VaultFile",
    "VaultLimits",
    "VaultMoveResult",
    "VaultStore",
]
