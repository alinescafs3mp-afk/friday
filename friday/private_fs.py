"""Owner-only filesystem primitives for Friday's local private state.

The project deliberately keeps user material on this machine.  Relying on the
caller's umask is therefore not a privacy boundary: the common ``022`` default
creates directories as ``0755`` and files as ``0644``.  These helpers establish
the mode both for new paths and for legacy paths before they are opened.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import BinaryIO, TextIO

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> Path:
    """Create *path* if needed and make the resulting directory owner-only."""

    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"private directory cannot be a symlink: {target}")
    target.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    target.chmod(_PRIVATE_DIRECTORY_MODE)
    return target


def restrict_private_file(path: Path) -> None:
    """Make an existing private-state file owner-only."""

    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"private file cannot be a symlink: {target}")
    try:
        mode = target.stat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise ValueError(f"private file must be regular: {target}")
    target.chmod(_PRIVATE_FILE_MODE)


def restrict_private_descriptor(descriptor: int) -> None:
    """Repair the mode on an already safely opened regular file."""

    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ValueError("private descriptor must refer to a regular file")
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(descriptor, _PRIVATE_FILE_MODE)


def prepare_private_file(path: Path) -> Path:
    """Create or repair a regular destination before a path-based writer uses it."""

    target = Path(path)
    _prepare_private_parent(target.parent)
    flags = os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    try:
        restrict_private_descriptor(descriptor)
    finally:
        os.close(descriptor)
    return target


def open_private_binary_append(path: Path) -> BinaryIO:
    """Open an owner-only append log without following its final symlink."""

    target = Path(path)
    _prepare_private_parent(target.parent)
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    try:
        restrict_private_descriptor(descriptor)
        return os.fdopen(descriptor, "ab")
    except BaseException:
        os.close(descriptor)
        raise


def open_private_text_write(path: Path, *, encoding: str = "utf-8") -> TextIO:
    """Create/truncate a private text report through a no-follow descriptor."""

    target = Path(path)
    _prepare_private_parent(target.parent)
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    try:
        restrict_private_descriptor(descriptor)
        return os.fdopen(descriptor, "w", encoding=encoding, newline="\n")
    except BaseException:
        os.close(descriptor)
        raise


def copy_private_file(source: Path, destination: Path) -> Path:
    """Copy bytes to a 0600 regular file without a permissive creation window."""

    target = Path(destination)
    _prepare_private_parent(target.parent)
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    target_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(Path(source), source_flags)
    try:
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise ValueError("private copy source must be regular")
        target_descriptor = os.open(target, target_flags, _PRIVATE_FILE_MODE)
        try:
            restrict_private_descriptor(target_descriptor)
            with (
                os.fdopen(source_descriptor, "rb", closefd=False) as source_handle,
                os.fdopen(target_descriptor, "wb", closefd=False) as target_handle,
            ):
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        finally:
            os.close(target_descriptor)
    finally:
        os.close(source_descriptor)
    return target


def restrict_private_tree(path: Path) -> Path:
    """Repair regular files and nested directories in an existing private tree."""

    root = ensure_private_directory(Path(path))
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if not current_path.is_symlink():
            current_path.chmod(_PRIVATE_DIRECTORY_MODE)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for name in filenames:
            candidate = current_path / name
            if candidate.is_symlink():
                continue
            try:
                mode = candidate.stat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode):
                candidate.chmod(_PRIVATE_FILE_MODE)
    return root


def _prepare_private_parent(path: Path) -> None:
    """Create a missing leaf parent, but never chmod an unrelated existing root."""

    parent = Path(path)
    if parent.is_symlink():
        raise ValueError(f"private file parent cannot be a symlink: {parent}")
    if parent.exists():
        if not parent.is_dir():
            raise ValueError(f"private file parent must be a directory: {parent}")
        return
    ensure_private_directory(parent)


def restrict_sqlite_files(database_path: Path) -> None:
    """Restrict a SQLite database and both possible WAL sidecars."""

    database = Path(database_path)
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        restrict_private_file(candidate)


def prepare_private_sqlite(database_path: Path) -> Path:
    """Secure a SQLite location before ``sqlite3.connect`` may create it.

    Pre-creating a new main file with ``0600`` also makes SQLite derive ``0600``
    for its WAL and shared-memory files.  Existing installations are tightened
    before any connection opens them.
    """

    database = Path(database_path)
    ensure_private_directory(database.parent)
    restrict_sqlite_files(database)
    if database.exists():
        return database

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(database, flags, _PRIVATE_FILE_MODE)
    except FileExistsError:
        # Another same-owner startup won the creation race.  Its mode is still
        # verified here before this process opens the database.
        restrict_private_file(database)
    else:
        os.close(descriptor)
        database.chmod(_PRIVATE_FILE_MODE)
    return database


def write_private_text_if_missing(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Create one owner-only text file without a world-readable creation window."""

    target = Path(path)
    ensure_private_directory(target.parent)
    if target.exists():
        restrict_private_file(target)
        return
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, _PRIVATE_FILE_MODE)
    except FileExistsError:
        restrict_private_file(target)
        return
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    target.chmod(_PRIVATE_FILE_MODE)


__all__ = [
    "copy_private_file",
    "ensure_private_directory",
    "open_private_binary_append",
    "open_private_text_write",
    "prepare_private_file",
    "prepare_private_sqlite",
    "restrict_private_descriptor",
    "restrict_private_file",
    "restrict_private_tree",
    "restrict_sqlite_files",
    "write_private_text_if_missing",
]
