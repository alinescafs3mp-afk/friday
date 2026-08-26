"""Identity checks for fixed package-owned host executables."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from friday.host_control.contracts import ExecutableAttestation


class ExecutableAttestationError(RuntimeError):
    pass


def attest_executable(
    path: str | Path,
    *,
    allowed_paths: Iterable[str | Path],
    allowed_owner_uids: Iterable[int],
    package_name: str,
    package_version: str,
    architecture: str,
    adapter_id: str,
    adapter_schema_version: int,
    implementation_version: int,
    observed_version: str,
) -> ExecutableAttestation:
    """Attest one canonical file using package identity proven by inventory."""

    if not package_name or not package_version or not architecture or not observed_version:
        raise ExecutableAttestationError("package and observed-version identity are required")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ExecutableAttestationError("executable path must be absolute")
    allowed = {str(Path(item)) for item in allowed_paths}
    if str(candidate) not in allowed:
        raise ExecutableAttestationError("executable path is not in the adapter allowlist")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ExecutableAttestationError("executable could not be opened without following links") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExecutableAttestationError("executable is not a regular file")
        if observed.st_mode & 0o022:
            raise ExecutableAttestationError("executable is group- or world-writable")
        if observed.st_uid not in {int(value) for value in allowed_owner_uids}:
            raise ExecutableAttestationError("executable owner is not allowed")
        if not observed.st_mode & 0o111:
            raise ExecutableAttestationError("file is not executable")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
            observed.st_uid,
            observed.st_gid,
            observed.st_size,
            observed.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ExecutableAttestationError("executable changed while being attested")
    finally:
        os.close(descriptor)
    canonical = str(candidate.resolve(strict=True))
    if canonical != str(candidate):
        raise ExecutableAttestationError("executable path is not canonical")
    try:
        return ExecutableAttestation(
            schema_version=1,
            canonical_path=canonical,
            package_name=package_name,
            package_version=package_version,
            architecture=architecture,
            device=observed.st_dev,
            inode=observed.st_ino,
            mode=stat.S_IMODE(observed.st_mode),
            owner_uid=observed.st_uid,
            owner_gid=observed.st_gid,
            size_bytes=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            sha256=digest.hexdigest(),
            observed_version=observed_version,
            adapter_id=adapter_id,
            adapter_schema_version=adapter_schema_version,
            implementation_version=implementation_version,
        )
    except ValueError as exc:
        raise ExecutableAttestationError("executable identity violates the shared contract") from exc


def verify_executable(
    expected: ExecutableAttestation,
    *,
    allowed_owner_uids: Iterable[int] | None = None,
) -> ExecutableAttestation:
    with open_verified_executable(expected, allowed_owner_uids=allowed_owner_uids):
        pass
    return expected


@contextmanager
def open_verified_executable(
    expected: ExecutableAttestation,
    *,
    allowed_owner_uids: Iterable[int] | None = None,
) -> Iterator[int]:
    """Hold the exact attested inode open across the caller's next operation."""

    candidate = Path(expected.canonical_path)
    admitted_uids = {
        int(value) for value in ((expected.owner_uid,) if allowed_owner_uids is None else allowed_owner_uids)
    }
    descriptor = -1
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in admitted_uids
            or before.st_mode & 0o022
            or not before.st_mode & 0o111
            or str(candidate.resolve(strict=True)) != expected.canonical_path
        ):
            raise ExecutableAttestationError("executable metadata changed after planning")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        observed_identity = (
            before.st_dev,
            before.st_ino,
            stat.S_IMODE(before.st_mode),
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
            digest.hexdigest(),
        )
        expected_identity = (
            expected.device,
            expected.inode,
            expected.mode,
            expected.owner_uid,
            expected.owner_gid,
            expected.size_bytes,
            expected.mtime_ns,
            expected.sha256,
        )
        stable_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable_identity or observed_identity != expected_identity:
            raise ExecutableAttestationError("executable identity changed after planning")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor
    except OSError as exc:
        raise ExecutableAttestationError("executable could not be verified safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


__all__ = [
    "ExecutableAttestation",
    "ExecutableAttestationError",
    "attest_executable",
    "open_verified_executable",
    "verify_executable",
]
