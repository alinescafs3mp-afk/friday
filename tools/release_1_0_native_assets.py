"""Bounded certificate custody for native transport; product pin/TLS checks still apply."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA = "friday.r10-native-ca.v1"
_BASENAME = "secondary-ca.pem"
_MAX_BYTES = 65_536
_FIELDS = {"schema", "source_path_sha256", "sha256", "size_bytes", "staged_path", "staged_stamp"}
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----\r?\n([A-Za-z0-9+/=\r\n]+)\r?\n-----END CERTIFICATE-----")


class NativeAssetError(RuntimeError):
    """Expose only closed native_* failure codes, never private paths or bytes."""


@dataclass(frozen=True)
class CapturedCA:
    source_path: Path = field(repr=False)
    content: bytes = field(repr=False)
    source_stamp: tuple[int, ...] = field(repr=False)


def _absolute(path: Path) -> Path:
    # Resolve relative inputs against the caller's cwd without following a final symlink.
    if not isinstance(path, Path):
        raise ValueError
    return Path(os.path.abspath(path))


def _stamp(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _certificate_bytes(raw: bytes) -> None:
    """Validate certificate-only PEM framing, not X.509 trust or profile admission."""
    text = raw.decode("ascii")
    blocks = _PEM.findall(text)
    if not blocks or _PEM.sub("", text).strip() or "PRIVATE KEY" in text:
        raise ValueError
    for block in blocks:
        if not base64.b64decode("".join(block.split()), validate=True):
            raise ValueError


def _read_stable(path: Path, *, private: bool = False) -> tuple[bytes, tuple[int, ...]]:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_mode & 0o022
        or not 1 <= before.st_size <= _MAX_BYTES
        or (
            private
            and (
                stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.getuid() or before.st_nlink != 1
            )
        )
    ):
        raise ValueError
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if _stamp(opened) != _stamp(before):
            raise ValueError
        chunks: list[bytes] = []
        remaining = _MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        path_after = path.lstat()
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if (
        len(raw) != before.st_size
        or not 1 <= len(raw) <= _MAX_BYTES
        or _stamp(after) != _stamp(before)
        or _stamp(path_after) != _stamp(before)
    ):
        raise ValueError
    _certificate_bytes(raw)
    return raw, _stamp(before)


def capture_ca(path: Path) -> CapturedCA:
    try:
        absolute = _absolute(path)
        absolute.as_posix().encode("utf-8")
        raw, stamp = _read_stable(absolute)
        return CapturedCA(absolute, raw, stamp)
    except (OSError, ValueError):
        raise NativeAssetError("native_ca_source_invalid") from None


def check_source(captured: CapturedCA) -> None:
    try:
        if (
            not isinstance(captured, CapturedCA)
            or not isinstance(captured.source_path, Path)
            or not captured.source_path.is_absolute()
            or type(captured.content) is not bytes
            or type(captured.source_stamp) is not tuple
        ):
            raise ValueError
        raw, stamp = _read_stable(captured.source_path)
        if stamp != captured.source_stamp or raw != captured.content:
            raise ValueError
    except (OSError, ValueError):
        raise NativeAssetError("native_ca_source_changed") from None


def _private_directory(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError
    return _stamp(metadata)[:5]


def stage_ca(captured: CapturedCA, evidence_dir: Path) -> dict[str, Any]:
    check_source(captured)
    try:
        parent = _absolute(evidence_dir)
        parent_stamp = _private_directory(parent)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if _stamp(os.fstat(directory))[:5] != parent_stamp:
                raise ValueError
            descriptor = os.open(
                _BASENAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                os.fchmod(descriptor, 0o600)
                remaining = memoryview(captured.content)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise ValueError
                    remaining = remaining[written:]
                os.fsync(descriptor)
                written_stamp = _stamp(os.fstat(descriptor))
            finally:
                os.close(descriptor)
            staged = parent / _BASENAME
            raw, stamp = _read_stable(staged, private=True)
            if (
                raw != captured.content
                or stamp != written_stamp
                or _private_directory(parent) != parent_stamp
            ):
                raise ValueError
        finally:
            os.close(directory)
        check_source(captured)
        return {
            "schema": _SCHEMA,
            "source_path_sha256": hashlib.sha256(str(captured.source_path).encode("utf-8")).hexdigest(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "staged_path": str(staged),
            "staged_stamp": list(stamp),
        }
    except (OSError, ValueError):
        raise NativeAssetError("native_ca_stage_invalid") from None


def validate_descriptor(descriptor: dict[str, Any]) -> None:
    """Validate the closed transport descriptor without filesystem access."""
    try:
        if (
            type(descriptor) is not dict
            or set(descriptor) != _FIELDS
            or type(descriptor["schema"]) is not str
            or descriptor["schema"] != _SCHEMA
            or type(descriptor["staged_path"]) is not str
            or type(descriptor["size_bytes"]) is not int
            or not 1 <= descriptor["size_bytes"] <= _MAX_BYTES
            or type(descriptor["staged_stamp"]) is not list
            or len(descriptor["staged_stamp"]) != 9
            or any(type(value) is not int or value < 0 for value in descriptor["staged_stamp"])
            or any(
                type(descriptor[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", descriptor[key]) is None
                for key in ("source_path_sha256", "sha256")
            )
        ):
            raise ValueError
        path = descriptor["staged_path"]
        stamp = descriptor["staged_stamp"]
        if (
            "\0" in path
            or not Path(path).is_absolute()
            or Path(path).name != _BASENAME
            or str(Path(path)) != path
            or os.path.normpath(path) != path
            or stamp[2] != stat.S_IFREG | 0o600
            or stamp[5] != 1
            or stamp[6] != descriptor["size_bytes"]
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise NativeAssetError("native_ca_descriptor_invalid") from None


def verify_staged(descriptor: dict[str, Any], evidence_dir: Path) -> Path:
    validate_descriptor(descriptor)
    try:
        parent = _absolute(evidence_dir)
        staged = parent / _BASENAME
        if descriptor["staged_path"] != str(staged):
            raise ValueError
    except (OSError, ValueError):
        raise NativeAssetError("native_ca_descriptor_invalid") from None
    try:
        parent_stamp = _private_directory(parent)
        raw, stamp = _read_stable(staged, private=True)
        if (
            len(raw) != descriptor["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]
            or list(stamp) != descriptor["staged_stamp"]
            or _private_directory(parent) != parent_stamp
        ):
            raise ValueError
    except (OSError, ValueError):
        raise NativeAssetError("native_ca_stage_invalid") from None
    return staged
