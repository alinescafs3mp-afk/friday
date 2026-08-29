"""Stable source identity for the offline archive-search benchmark."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Final

_RELEASE_SOURCE_ROOTS: Final = ("friday",)
_MAX_RELEASE_SOURCE_FILES: Final = 10_000
_MAX_RELEASE_SOURCE_BYTES: Final = 512 * 1024 * 1024
_MAX_SINGLE_SOURCE_BYTES: Final = 16 * 1024 * 1024


class RecallReleaseIdentityError(RuntimeError):
    """The executable source snapshot could not be identified stably."""


def archive_search_release_sha256() -> str:
    """Bind evidence to a stable snapshot of every Friday Python source."""

    digest = hashlib.sha256(b"friday/archive-search-release-source/v1\0")
    package_root = Path(__file__).resolve().parents[2]
    relative_paths = _release_source_paths(package_root)
    if not relative_paths:
        raise RecallReleaseIdentityError("archive release source set is unavailable")
    before_snapshot = {
        relative_name: _source_identity(source_path) for relative_name, source_path in relative_paths
    }
    if sum(identity[5] for identity in before_snapshot.values()) > _MAX_RELEASE_SOURCE_BYTES:
        raise RecallReleaseIdentityError("archive release source set exceeds its byte bound")
    for relative_name, source_path in relative_paths:
        source = _stable_source_bytes(source_path)
        try:
            name = relative_name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RecallReleaseIdentityError("archive release source name is not canonical ASCII") from exc
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    after_paths = _release_source_paths(package_root)
    after_snapshot = {
        relative_name: _source_identity(source_path) for relative_name, source_path in after_paths
    }
    if relative_paths != after_paths or before_snapshot != after_snapshot:
        raise RecallReleaseIdentityError("archive release source set changed during hashing")
    return digest.hexdigest()


def _release_source_paths(package_root: Path) -> tuple[tuple[str, Path], ...]:
    source_paths: list[Path] = []
    for root in _RELEASE_SOURCE_ROOTS:
        for path in (package_root / root).rglob("*.py"):
            source_paths.append(path)
            if len(source_paths) > _MAX_RELEASE_SOURCE_FILES:
                raise RecallReleaseIdentityError("archive release source set exceeds its file bound")
    return tuple(sorted({path.relative_to(package_root).as_posix(): path for path in source_paths}.items()))


def _source_status_identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_nlink),
        int(status.st_uid),
        int(status.st_size),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _source_identity(path: Path) -> tuple[int, ...]:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RecallReleaseIdentityError("archive release source is unavailable") from exc
    if not stat.S_ISREG(status.st_mode):
        raise RecallReleaseIdentityError("archive release source is not a regular file")
    return _source_status_identity(status)


def _stable_source_bytes(path: Path) -> bytes:
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        before_identity = _source_status_identity(before)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SINGLE_SOURCE_BYTES:
            raise RecallReleaseIdentityError("archive release source is not a bounded regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _source_status_identity(opened) != before_identity:
            raise RecallReleaseIdentityError("archive release source changed before hashing")
        chunks = bytearray()
        while len(chunks) <= before.st_size:
            chunk = os.read(
                descriptor,
                min(1 << 20, before.st_size + 1 - len(chunks)),
            )
            if not chunk:
                break
            chunks.extend(chunk)
        source = bytes(chunks)
        after_descriptor = os.fstat(descriptor)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RecallReleaseIdentityError("archive release source is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        before_identity != _source_status_identity(after_descriptor)
        or before_identity != _source_status_identity(after)
        or len(source) != before.st_size
    ):
        raise RecallReleaseIdentityError("archive release source changed during hashing")
    return source


__all__ = ["RecallReleaseIdentityError", "archive_search_release_sha256"]
