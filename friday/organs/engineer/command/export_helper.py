#!/usr/bin/python3
"""Sandbox PID-1 wrapper that exports regular output through a private dirfd.

This file is snapshotted into a sealed memfd by the host. It deliberately has
no imports from the Friday package because only the system runtime is mounted
inside the sandbox.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import stat
import sys

OUTPUT_DIR_FD = 29
MAX_FILES = 64
MAX_DIRS = 64
MAX_DEPTH = 8
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _disable_parent_proc_access() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if int(libc.prctl(4, 0, 0, 0, 0)) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "PR_SET_DUMPABLE")


def _copy_file(source_fd: int, dest_dir_fd: int, name: str, before: os.stat_result) -> int:
    dest_fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
        0o600,
        dir_fd=dest_dir_fd,
    )
    copied = 0
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_FILE_BYTES:
                raise ValueError("output_file_too_large")
            view = memoryview(chunk)
            while view:
                view = view[os.write(dest_fd, view) :]
        after = os.fstat(source_fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        )
        if before_identity != after_identity or copied != before.st_size:
            raise ValueError("output_identity_changed")
        os.fsync(dest_fd)
        os.fchmod(dest_fd, 0o400)
        return copied
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dest_dir_fd)
        raise
    finally:
        os.close(dest_fd)


def _export_tree(
    source_dir_fd: int,
    dest_dir_fd: int,
    *,
    depth: int,
    counters: list[int],
) -> None:
    if depth > MAX_DEPTH:
        raise ValueError("output_depth_overflow")
    for name in sorted(os.listdir(f"/proc/self/fd/{source_dir_fd}")):
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise ValueError("path_escape")
        before = os.stat(name, dir_fd=source_dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            counters[1] += 1
            if counters[1] > MAX_DIRS:
                raise ValueError("output_tree_overflow")
            source_child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
                dir_fd=source_dir_fd,
            )
            os.mkdir(name, 0o700, dir_fd=dest_dir_fd)
            dest_child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
                dir_fd=dest_dir_fd,
            )
            try:
                opened = os.fstat(source_child)
                if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                ):
                    raise ValueError("output_identity_changed")
                _export_tree(source_child, dest_child, depth=depth + 1, counters=counters)
            finally:
                os.close(dest_child)
                os.close(source_child)
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("output_not_regular")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("output_file_too_large")
        counters[0] += 1
        if counters[0] > MAX_FILES:
            raise ValueError("output_tree_overflow")
        source_fd = os.open(
            name,
            os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
            dir_fd=source_dir_fd,
        )
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise ValueError("output_identity_changed")
            copied = _copy_file(source_fd, dest_dir_fd, name, opened)
        finally:
            os.close(source_fd)
        counters[2] += copied
        if counters[2] > MAX_TREE_BYTES:
            raise ValueError("output_tree_overflow")


def _run_payload(argv0: str, command: list[str], output_fd: int) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            os.close(output_fd)
            os.execve(command[0], [argv0, *command[1:]], dict(os.environ))
        except BaseException:
            os._exit(126)
    _, status = os.waitpid(pid, 0)
    return int(os.waitstatus_to_exitcode(status))


def main() -> int:
    if len(sys.argv) < 3:
        return 125
    output_fd = -1
    try:
        output_fd = os.dup(OUTPUT_DIR_FD)
        os.close(OUTPUT_DIR_FD)
        output_stat = os.fstat(output_fd)
        if not stat.S_ISDIR(output_stat.st_mode):
            return 125
        os.set_inheritable(output_fd, False)
        stdin_stat = os.fstat(0)
        if not stat.S_ISREG(stdin_stat.st_mode):
            return 125
        _disable_parent_proc_access()
        payload_status = _run_payload(sys.argv[1], sys.argv[2:], output_fd)
        source_fd = os.open(
            "/job/output",
            os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
        )
        try:
            _export_tree(source_fd, output_fd, depth=0, counters=[0, 1, 0])
            os.fsync(output_fd)
        finally:
            os.close(source_fd)
    except BaseException:
        return 125
    finally:
        if output_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(output_fd)
    if payload_status < 0:
        return min(255, 128 + -payload_status)
    return min(255, payload_status)


if __name__ == "__main__":
    raise SystemExit(main())
