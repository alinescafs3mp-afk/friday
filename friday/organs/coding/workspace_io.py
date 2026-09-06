"""Descriptor-relative directory primitives shared by Coding input and output.

No authorization is granted here. Callers supply an admitted operation root;
these helpers prevent path aliases from changing that root during local I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path


def directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("descriptor-relative Coding I/O is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextmanager
def open_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open every component without following aliases; create only missing ones."""

    path = Path(path).absolute()
    if path.anchor != "/" or ".." in path.parts or path == Path(path.anchor):
        raise ValueError("invalid Coding operation directory")
    flags = directory_flags()
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if create:
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise ValueError("Coding operation directory is not owned by this process")
            os.fchmod(descriptor, 0o700)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_relative_directory(root: int, parts: tuple[str, ...]) -> Iterator[int]:
    if type(parts) is not tuple or any(
        type(part) is not str or not part or part in {".", ".."} or "/" in part or "\x00" in part
        for part in parts
    ):
        raise ValueError("invalid relative Coding directory component")
    descriptor = os.dup(root)
    try:
        for part in parts:
            child = os.open(part, directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def entry_stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _rollback_members(
    root: int,
    files: list[tuple[tuple[str, ...], tuple[int, int]]],
    directories: list[tuple[tuple[str, ...], tuple[int, int]]],
) -> None:
    """Remove only our exact new inodes, never another writer's replacements."""

    for rows, directory in ((files, False), (directories, True)):
        for parts, identity in reversed(rows):
            with (
                suppress(OSError, ValueError),
                open_relative_directory(root, parts[:-1]) as parent,
            ):
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != identity:
                    continue
                if directory:
                    os.rmdir(parts[-1], dir_fd=parent)
                else:
                    os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)


def publish_members(workspace: Path, pending: list[tuple[str, bytes | None]]) -> None:
    """Create admitted files exclusively; rollback controlled failures.

    Keep the workspace inode stable for the worker's already-admitted mount.
    This is not an atomic directory replacement or a crash-recovery receipt.
    """

    seen: set[str] = set()
    for path, body in pending:
        if (
            type(path) is not str
            or not path
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen
            or (body is not None and type(body) is not bytes)
        ):
            raise ValueError("invalid admitted source member")
        seen.add(path)
    directories: set[tuple[str, ...]] = set()
    for path, body in pending:
        parts = tuple(path.split("/"))
        directories.update(parts[:index] for index in range(1, len(parts)))
        if body is None:
            directories.add(parts)
    new_directories: list[tuple[tuple[str, ...], tuple[int, int]]] = []
    new_files: list[tuple[tuple[str, ...], tuple[int, int]]] = []
    completed: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
    with open_directory(workspace, create=True) as root:
        root_info = os.fstat(root)
        try:
            for parts in sorted(directories, key=lambda item: (len(item), item)):
                with open_relative_directory(root, parts[:-1]) as parent:
                    created = False
                    try:
                        os.mkdir(parts[-1], 0o700, dir_fd=parent)
                        created = True
                    except FileExistsError:
                        pass
                    info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        raise ValueError("source destination parent is not a directory")
                    if created:
                        new_directories.append((parts, (info.st_dev, info.st_ino)))
            for path, body in pending:
                if body is None:
                    continue
                parts = tuple(path.split("/"))
                with open_relative_directory(root, parts[:-1]) as parent:
                    descriptor = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=parent,
                    )
                    try:
                        info = os.fstat(descriptor)
                        new_files.append((parts, (info.st_dev, info.st_ino)))
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ValueError("source destination is not a unique regular file")
                        view = memoryview(body)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short source write")
                            view = view[written:]
                        os.fsync(descriptor)
                        current = os.fstat(descriptor)
                        if current.st_size != len(body) or current.st_nlink != 1:
                            raise ValueError("source destination changed during write")
                        completed.append((parts, entry_stamp(current)))
                    finally:
                        os.close(descriptor)
            for parts, expected in completed:
                with open_relative_directory(root, parts[:-1]) as parent:
                    current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if entry_stamp(current) != expected:
                        raise ValueError("source destination changed before completion")
                    os.fsync(parent)
            for parts in sorted(directories, key=len, reverse=True):
                with open_relative_directory(root, parts) as directory:
                    os.fsync(directory)
            with open_directory(workspace) as current_root:
                current = os.fstat(current_root)
                if (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino):
                    raise ValueError("source root changed before completion")
            os.fsync(root)
        except BaseException:
            _rollback_members(root, new_files, new_directories)
            raise


def source_revision_sha256(digests: Mapping[str, str]) -> str:
    """Preserve the original create identity serialization for exact source bytes."""

    return hashlib.sha256(json.dumps(dict(digests), sort_keys=True).encode()).hexdigest()
