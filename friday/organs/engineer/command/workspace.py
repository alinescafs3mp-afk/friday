"""Private per-job workspace. Outputs are sealed via openat, never followable evidence."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
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

EXPORT_ERROR_MARKER = ".friday-export-error.v1"
EXPORT_ERROR_CODES = frozenset(
    {
        "output_depth_overflow",
        "output_export_failed",
        "output_file_too_large",
        "output_hardlink_refused",
        "output_identity_changed",
        "output_not_regular",
        "output_reserved_name",
        "output_symlink_refused",
        "output_tree_overflow",
        "path_escape",
    }
)


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

    def read_evidence_verified(self, name: str, *, expected_sha256: str, cap: int) -> bytes:
        if name not in {"stdout.bin", "stderr.bin"}:
            raise CommandError("corrupt_evidence")
        if not expected_sha256 or len(expected_sha256) != 64:
            raise CommandError("corrupt_evidence")
        dir_fd = open_dir_nofollow(self.evidence)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            try:
                fd = os.open(name, flags, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise CommandError("corrupt_evidence") from exc
                raise CommandError("corrupt_evidence") from exc
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise CommandError("corrupt_evidence")
                if before.st_size > cap:
                    raise CommandError("corrupt_evidence")
                hasher = hashlib.sha256()
                chunks: list[bytes] = []
                total = 0
                os.lseek(fd, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        raise CommandError("corrupt_evidence")
                    hasher.update(chunk)
                    chunks.append(chunk)
                after = os.fstat(fd)
                digest = hasher.hexdigest()
                if (
                    digest != expected_sha256
                    or total != before.st_size
                    or after.st_ino != before.st_ino
                    or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                ):
                    raise CommandError("corrupt_evidence")
                return b"".join(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)

    def read_generated_file_verified(self, generated: GeneratedFile) -> bytes:
        """Re-read one sealed output only while its receipt identity remains exact."""

        parts = self._validated_generated_file_receipt(generated)
        try:
            root_fd = open_dir_nofollow(self.sealed)
        except CommandError as exc:
            raise CommandError("corrupt_generated_output") from exc
        open_fds = [root_fd]
        linked_identities: list[tuple[int, str, os.stat_result]] = []
        try:
            root_before = os.fstat(root_fd)
            if not stat.S_ISDIR(root_before.st_mode):
                raise CommandError("corrupt_generated_output")
            parent_fd = root_fd
            directory_flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            for name in parts[:-1]:
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise CommandError("corrupt_generated_output") from exc
                open_fds.append(child_fd)
                opened = os.fstat(child_fd)
                if not stat.S_ISDIR(opened.st_mode):
                    raise CommandError("corrupt_generated_output")
                linked_identities.append((parent_fd, name, opened))
                parent_fd = child_fd

            file_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                file_fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
            except OSError as exc:
                raise CommandError("corrupt_generated_output") from exc
            open_fds.append(file_fd)
            before = os.fstat(file_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size != generated.size_bytes
            ):
                raise CommandError("corrupt_generated_output")
            linked_identities.append((parent_fd, parts[-1], before))

            hasher = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > generated.size_bytes or total > MAX_OUTPUT_FILE_BYTES:
                    raise CommandError("corrupt_generated_output")
                hasher.update(chunk)
                chunks.append(chunk)
            after = os.fstat(file_fd)
            if (
                not self._same_file_identity(before, after)
                or total != generated.size_bytes
                or not hmac.compare_digest(hasher.hexdigest(), generated.sha256)
            ):
                raise CommandError("corrupt_generated_output")

            for link_parent_fd, name, expected in linked_identities:
                try:
                    linked = os.stat(name, dir_fd=link_parent_fd, follow_symlinks=False)
                except OSError as exc:
                    raise CommandError("corrupt_generated_output") from exc
                if not self._same_file_identity(expected, linked):
                    raise CommandError("corrupt_generated_output")
            try:
                root_linked = os.stat(self.sealed, follow_symlinks=False)
            except OSError as exc:
                raise CommandError("corrupt_generated_output") from exc
            if not self._same_file_identity(root_before, root_linked):
                raise CommandError("corrupt_generated_output")
            return b"".join(chunks)
        except CommandError:
            raise
        except OSError as exc:
            raise CommandError("corrupt_generated_output") from exc
        finally:
            for fd in reversed(open_fds):
                with contextlib.suppress(OSError):
                    os.close(fd)

    @staticmethod
    def _validated_generated_file_receipt(generated: GeneratedFile) -> tuple[str, ...]:
        if type(generated) is not GeneratedFile:
            raise CommandError("corrupt_generated_output")
        path = generated.relative_path
        if not isinstance(path, str) or not path or "\x00" in path or path.startswith("/"):
            raise CommandError("corrupt_generated_output")
        parts = tuple(path.split("/"))
        if (
            not parts
            or any(not part or part in {".", ".."} for part in parts)
            or len(parts) - 1 > MAX_OUTPUT_DEPTH
            or isinstance(generated.size_bytes, bool)
            or not isinstance(generated.size_bytes, int)
            or not 0 <= generated.size_bytes <= MAX_OUTPUT_FILE_BYTES
            or not isinstance(generated.sha256, str)
            or len(generated.sha256) != 64
            or any(char not in "0123456789abcdef" for char in generated.sha256)
            or isinstance(generated.mode, bool)
            or not isinstance(generated.mode, int)
            or not 0 <= generated.mode <= 0o7777
        ):
            raise CommandError("corrupt_generated_output")
        return parts

    @staticmethod
    def _same_file_identity(before: os.stat_result, after: os.stat_result) -> bool:
        return (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        )

    def open_evidence(self, name: str) -> int:
        if name not in {"stdout.bin", "stderr.bin"}:
            raise CommandError("invalid_evidence")
        dir_fd = open_dir_nofollow(self.evidence)
        flags = (
            os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise CommandError("invalid_evidence")
                os.fchmod(fd, 0o600)
                return fd
            except Exception:
                os.close(fd)
                raise
        finally:
            os.close(dir_fd)

    def admit_generated_files(self) -> tuple[GeneratedFile, ...]:
        output_fd = open_dir_nofollow(self.output)
        sealed_fd = open_dir_nofollow(self.sealed)
        try:
            export_error = self._consume_export_error(output_fd)
            if export_error is not None:
                raise CommandError(export_error)
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

    @staticmethod
    def _consume_export_error(output_fd: int) -> str | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            marker_fd = os.open(EXPORT_ERROR_MARKER, flags, dir_fd=output_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CommandError("output_export_failed") from exc
        try:
            before = os.fstat(marker_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 1 <= before.st_size <= 64:
                raise CommandError("output_export_failed")
            payload = os.read(marker_fd, 65)
            after = os.fstat(marker_fd)
            if (
                len(payload) != before.st_size
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise CommandError("output_export_failed")
            try:
                code = payload.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise CommandError("output_export_failed") from exc
            if code not in EXPORT_ERROR_CODES:
                raise CommandError("output_export_failed")
        finally:
            os.close(marker_fd)
        try:
            os.unlink(EXPORT_ERROR_MARKER, dir_fd=output_fd)
        except OSError as exc:
            raise CommandError("output_export_failed") from exc
        return code

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
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
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
