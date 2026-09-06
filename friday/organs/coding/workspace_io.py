"""Descriptor-relative directory primitives shared by Coding input and output.

No authorization is granted here. Callers supply an admitted operation root;
these helpers prevent path aliases from changing that root during local I/O.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
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
