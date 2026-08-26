"""Private per-job workspace. Outputs are sealed via openat, never followable evidence."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
from pathlib import Path

from .contracts import (
    MAX_OUTPUT_DEPTH,
    MAX_OUTPUT_DIRS,
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TREE_BYTES,
    SANDBOX_JOB,
    CommandError,
    GeneratedFile,
)
from .store import atomic_write, open_dir_nofollow


def _listdir_fd(dir_fd: int) -> list[str]:
    return sorted(os.listdir(f"/proc/self/fd/{dir_fd}"))


def _mkdirat(dir_fd: int, name: str, *, mode: int = 0o700) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, mode, dir_fd=dir_fd)


class JobWorkspace:
    def __init__(self, job_dir: Path) -> None:
        self.job_dir = job_dir
        self.home = job_dir / "workspace"
        self.tmp = job_dir / "tmp"
        self.output = job_dir / "output"
        self.evidence = job_dir / "evidence"
        self.sealed = job_dir / "sealed"
        self.stdout_path = self.evidence / "stdout.bin"
        self.stderr_path = self.evidence / "stderr.bin"

    def materialize(self) -> None:
        for path in (self.home, self.tmp, self.output, self.evidence, self.sealed):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        atomic_write(self.stdout_path, b"")
        atomic_write(self.stderr_path, b"")

    def env(self, *, path_value: str, isolated: bool) -> dict[str, str]:
        home = f"{SANDBOX_JOB}/workspace" if isolated else str(self.home)
        tmp = f"{SANDBOX_JOB}/tmp" if isolated else str(self.tmp)
        pwd = SANDBOX_JOB if isolated else str(self.job_dir)
        return {
            "HOME": home,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": path_value,
            "PWD": pwd,
            "TMPDIR": tmp,
            "TZ": "UTC",
        }

    def open_evidence(self, name: str) -> int:
        if name not in {"stdout.bin", "stderr.bin"}:
            raise CommandError("invalid_evidence")
        dir_fd = open_dir_nofollow(self.evidence)
        flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
            os.fchmod(fd, 0o600)
            return fd
        finally:
            os.close(dir_fd)

    def admit_generated_files(self) -> tuple[GeneratedFile, ...]:
        output_fd = open_dir_nofollow(self.output)
        sealed_fd = open_dir_nofollow(self.sealed)
        try:
            admitted, _dirs, _bytes = self._walk(
                output_fd,
                sealed_fd,
                relative="",
                depth=0,
                dir_count=1,
                total_bytes=0,
                files=[],
            )
            return tuple(admitted)
        finally:
            os.close(sealed_fd)
            os.close(output_fd)

    def _walk(
        self,
        dir_fd: int,
        sealed_fd: int,
        *,
        relative: str,
        depth: int,
        dir_count: int,
        total_bytes: int,
        files: list[GeneratedFile],
    ) -> tuple[list[GeneratedFile], int, int]:
        if depth > MAX_OUTPUT_DEPTH:
            raise CommandError("output_depth_overflow")
        for name in _listdir_fd(dir_fd):
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise CommandError("path_escape")
            child_rel = name if not relative else f"{relative}/{name}"
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                child_fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=dir_fd)
                is_dir = True
            except OSError:
                try:
                    child_fd = os.open(name, flags, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise CommandError("output_symlink_refused") from exc
                    raise CommandError("output_unreadable") from exc
                is_dir = False
            try:
                st = os.fstat(child_fd)
                if is_dir:
                    if not stat.S_ISDIR(st.st_mode):
                        raise CommandError("output_not_regular")
                    dir_count += 1
                    if dir_count > MAX_OUTPUT_DIRS:
                        raise CommandError("output_tree_overflow")
                    _mkdirat(sealed_fd, name)
                    nested = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=sealed_fd,
                    )
                    try:
                        files, dir_count, total_bytes = self._walk(
                            child_fd,
                            nested,
                            relative=child_rel,
                            depth=depth + 1,
                            dir_count=dir_count,
                            total_bytes=total_bytes,
                            files=files,
                        )
                    finally:
                        os.close(nested)
                    continue
                if stat.S_ISLNK(st.st_mode):
                    raise CommandError("output_symlink_refused")
                if not stat.S_ISREG(st.st_mode):
                    raise CommandError("output_not_regular")
                if st.st_nlink > 1:
                    raise CommandError("output_hardlink_refused")
                if st.st_size > MAX_OUTPUT_FILE_BYTES:
                    raise CommandError("output_file_too_large")
                if len(files) >= MAX_OUTPUT_FILES:
                    raise CommandError("output_tree_overflow")
                digest, copied = self._seal_file(child_fd, sealed_fd, name, before=st)
                total_bytes += copied
                if total_bytes > MAX_OUTPUT_TREE_BYTES:
                    raise CommandError("output_tree_overflow")
                files.append(
                    GeneratedFile(
                        relative_path=child_rel,
                        size_bytes=copied,
                        sha256=digest,
                        mode=int(stat.S_IMODE(st.st_mode)),
                    )
                )
            finally:
                os.close(child_fd)
        return files, dir_count, total_bytes

    def _seal_file(self, source_fd: int, sealed_dir_fd: int, name: str, *, before: os.stat_result) -> tuple[str, int]:
        dest_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=sealed_dir_fd,
        )
        hasher = hashlib.sha256()
        copied = 0
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_OUTPUT_FILE_BYTES:
                    raise CommandError("output_file_too_large")
                hasher.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(dest_fd, view)
                    view = view[written:]
            after = os.fstat(source_fd)
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink)
                or after.st_nlink != 1
                or copied != before.st_size
            ):
                raise CommandError("output_identity_changed")
            os.fsync(dest_fd)
            os.fchmod(dest_fd, 0o400)
        finally:
            os.close(dest_fd)
        return hasher.hexdigest(), copied
