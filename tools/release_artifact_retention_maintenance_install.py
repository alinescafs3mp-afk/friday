#!/usr/bin/env python3
"""Install or remove one unarmed release-retention maintenance image.

The fixed host journal is the only authority for materialized maintenance
artifacts.  It is published and fsynced before the first transaction stage,
contains no boot identity, and makes every subsequent operation replayable or
removable after a process, kernel, or power failure.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

REQUEST_SCHEMA = "friday.release-artifact-retention-maintenance-request.v1"
IMAGE_AUTHORITY_SCHEMA = "friday.release-artifact-retention-maintenance-image-authority.v1"
JOURNAL_SCHEMA = "friday.release-artifact-retention-maintenance-host-transaction.v2"
ARTIFACT_SET_SCHEMA = "friday.release-artifact-retention-maintenance-host-artifact-set.v2"
TEST_INITRD_SCHEMA = "friday.release-artifact-retention-maintenance-test-image.v1"

JOURNAL_PATH = "/usr/libexec/friday/release_artifact_retention_maintenance_host_transaction.v2.json"
JOURNAL_STAGE_PATH = (
    "/usr/libexec/friday/.release_artifact_retention_maintenance_host_transaction.v2.json.new"
)
LOCK_PATH = "/usr/libexec/friday/.release-artifact-retention-maintenance.install.lock"
PRIVILEGED_PROC_HELPER_PATH = "/usr/libexec/friday/release_artifact_proc_probe.py"
PRIVILEGED_PROC_INSTALL_LOCK_PATH = "/usr/libexec/friday/.release-artifact-proc-probe.install.lock"
MAINTENANCE_POLICY_PATH = "/etc/sudoers.d/friday-retention-maintenance-probe"

INSTALL_PHASES = (
    "install_prepared",
    "payloads_staged",
    "components_publishing",
    "components_published",
    "config_publishing",
    "config_published",
    "initrd_building",
    "initrd_staged",
    "initrd_publishing",
    "policy_publishing",
    "policy_published",
    "installed_not_armed",
)
REMOVE_PHASES = (
    "remove_prepared",
    "policy_revoking",
    "initrd_removing",
    "config_removing",
    "components_removing",
    "private_stages_removing",
    "removed",
)
ALL_PHASES = (*INSTALL_PHASES, *REMOVE_PHASES)
_TRANSITIONS = {
    **{
        phase: frozenset({INSTALL_PHASES[index + 1], "remove_prepared"})
        for index, phase in enumerate(INSTALL_PHASES[:-1])
    },
    "installed_not_armed": frozenset({"remove_prepared"}),
    **{phase: frozenset({REMOVE_PHASES[index + 1]}) for index, phase in enumerate(REMOVE_PHASES[:-1])},
    "removed": frozenset(),
}

MAX_REQUEST_BYTES = 64 << 20
MAX_JOURNAL_BYTES = 128 << 10
MAX_SOURCE_BYTES = 4 << 20
MAX_POLICY_BYTES = 4 << 10
MAX_PROFILE_BYTES = 256 << 20
MAX_INITRD_BYTES = 1 << 30
MAX_RETIRED_TRANSACTIONS = 1024
MAX_PRIVATE_TREE_ENTRIES = 1 << 20
MAX_PRIVATE_TREE_DEPTH = 128
MAX_BLOCK_DEVICES = 4096
MAX_BLKID_OUTPUT_BYTES = 256
BLOCK_SCAN_TIMEOUT_SECONDS = 120
BLKID_PATH = Path("/usr/sbin/blkid")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_FILESYSTEM_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_BLOCK_DEVICE_ID = re.compile(r"[0-9]+:[0-9]+\Z")
_BLOCK_DEVICE_NAME = re.compile(r"[A-Za-z0-9_.!+-]{1,255}\Z")
_SAFE_USER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SAFE_CONFIG = re.compile(r"[A-Za-z0-9_./:-]{1,4096}\Z")
_CONFIG_NAMES = (
    "controller-path",
    "controller-sha256",
    "image-authority-path",
    "image-authority-sha256",
    "image-authority.v1.json",
    "maintenance-cmdline-sha256",
    "ordinary-root-device-id",
    "ordinary-root-filesystem-uuid",
    "owner-uid",
    "owner-user",
    "request-file-sha256",
    "request-path",
    "request-sha256",
    "root-request-path",
    "transaction-id",
)


class MaintenanceInstallError(RuntimeError):
    """A body-free fail-closed host installation error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        _raise("maintenance_install_arguments_invalid")


def _raise(code: str) -> NoReturn:
    raise MaintenanceInstallError(code)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise MaintenanceInstallError("maintenance_install_contract_invalid") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            _raise("maintenance_install_contract_invalid")
        result[name] = value
    return result


def _constant(_value: str) -> NoReturn:
    _raise("maintenance_install_contract_invalid")


def _canonical_object(raw: bytes, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        MaintenanceInstallError,
        RecursionError,
    ) as exc:
        if isinstance(exc, MaintenanceInstallError):
            raise MaintenanceInstallError(code) from exc
        raise MaintenanceInstallError(code) from exc
    if not isinstance(value, dict) or raw != _canonical(value) + b"\n":
        _raise(code)
    return value


def _hex64(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _raise(code)
    return value


def _root_filesystem_uuid(cmdline: bytes, *, code: str) -> str:
    roots = [token for token in cmdline.split() if token.startswith(b"root=")]
    if len(roots) != 1 or not roots[0].startswith(b"root=UUID="):
        _raise(code)
    try:
        value = roots[0][len(b"root=UUID=") :].decode("ascii")
    except UnicodeError as exc:
        raise MaintenanceInstallError(code) from exc
    if _FILESYSTEM_UUID.fullmatch(value) is None:
        _raise(code)
    return value


def _safe_absolute(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        _raise(code)
    path = Path(value)
    if path != Path(os.path.abspath(path)) or not path.name or ".." in path.parts:
        _raise(code)
    return value


def _safe_root(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        _raise("maintenance_install_root_invalid")
    path = Path(value)
    if path != Path(os.path.abspath(path)) or ".." in path.parts:
        _raise("maintenance_install_root_invalid")
    return value


def _safe_config(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _SAFE_CONFIG.fullmatch(value) is None:
        _raise(code)
    if ".." in Path(value).parts:
        _raise(code)
    return value


def _identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_nlink,
        status.st_uid,
        status.st_gid,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _descriptor_mount_id(descriptor: int) -> int:
    """Return Linux's mount identity for one already-open descriptor."""

    if type(descriptor) is not int or descriptor < 0:
        _raise("maintenance_install_directory_invalid")
    fdinfo = -1
    try:
        fdinfo = os.open(
            f"/proc/self/fdinfo/{descriptor}",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fdinfo, min(4097 - total, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 4096:
                _raise("maintenance_install_directory_invalid")
    except (OSError, MaintenanceInstallError) as exc:
        if isinstance(exc, MaintenanceInstallError):
            raise
        raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc
    finally:
        if fdinfo >= 0:
            os.close(fdinfo)
    try:
        lines = b"".join(chunks).decode("ascii").splitlines()
    except UnicodeError as exc:
        raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc
    values = [line.partition(":")[2].strip() for line in lines if line.partition(":")[0] == "mnt_id"]
    if len(values) != 1 or not values[0] or not values[0].isdigit() or int(values[0]) <= 0:
        _raise("maintenance_install_directory_invalid")
    return int(values[0])


@dataclass(frozen=True)
class FileEvidence:
    sha256: str
    mode: int
    size: int
    raw: bytes | None = None


def _external_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    expected_uid: int | None = None,
    allowed_modes: frozenset[int] | None = None,
    retain: bool = False,
) -> FileEvidence:
    lexical = Path(os.path.abspath(path))
    if path != lexical or not path.name:
        _raise(code)
    parts = lexical.parts[1:]
    parent = descriptor = -1
    try:
        parent = os.open(
            "/",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            status = os.fstat(child)
            if not stat.S_ISDIR(status.st_mode):
                os.close(child)
                _raise(code)
            os.close(parent)
            parent = child
        before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            _raise(code)
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, maximum + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            if retain:
                chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                _raise(code)
        after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    except (OSError, MaintenanceInstallError) as exc:
        if isinstance(exc, MaintenanceInstallError):
            raise
        raise MaintenanceInstallError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (expected_uid is not None and before.st_uid != expected_uid)
        or (allowed_modes is not None and mode not in allowed_modes)
        or mode & 0o022
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
    ):
        _raise(code)
    return FileEvidence(
        sha256=digest.hexdigest(),
        mode=mode,
        size=total,
        raw=b"".join(chunks) if retain else None,
    )


class RootFS:
    """A pinned root and descriptor-relative, no-follow file operations."""

    def __init__(self, root: Path, *, uid: int, gid: int) -> None:
        lexical = Path(os.path.abspath(root))
        if root != lexical or not root.is_absolute():
            _raise("maintenance_install_root_invalid")
        descriptor = -1
        try:
            before = os.lstat(root)
            descriptor = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            after = os.lstat(root)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise MaintenanceInstallError("maintenance_install_root_invalid") from exc
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) & 0o022
            or _identity(before) != _identity(opened)
            or _identity(before) != _identity(after)
        ):
            os.close(descriptor)
            _raise("maintenance_install_root_invalid")
        self.root = root
        self.uid = uid
        self.gid = gid
        self.fd = descriptor

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> RootFS:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _parts(logical: str) -> tuple[str, ...]:
        if not isinstance(logical, str) or not logical.startswith("/") or logical == "/" or "\x00" in logical:
            _raise("maintenance_install_path_invalid")
        parts = tuple(logical[1:].split("/"))
        if any(not part or part in {".", ".."} for part in parts):
            _raise("maintenance_install_path_invalid")
        return parts

    def host_path(self, logical: str) -> Path:
        self._parts(logical)
        return Path(logical) if self.root == Path("/") else self.root / logical[1:]

    def open_dir(
        self,
        logical: str,
        *,
        create: bool = False,
        final_mode: int | None = None,
    ) -> int:
        parts = self._parts(logical)
        current = os.dup(self.fd)
        try:
            for index, name in enumerate(parts):
                last = index == len(parts) - 1
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=current,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    mode = final_mode if last and final_mode is not None else 0o755
                    previous_umask = os.umask(0)
                    try:
                        os.mkdir(name, mode, dir_fd=current)
                    finally:
                        os.umask(previous_umask)
                    os.fsync(current)
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=current,
                    )
                    os.fchmod(child, mode)
                    os.fchown(child, self.uid, self.gid)
                    os.fsync(child)
                    os.fsync(current)
                status = os.fstat(child)
                expected_mode = final_mode if last else None
                if (
                    not stat.S_ISDIR(status.st_mode)
                    or status.st_uid != self.uid
                    or status.st_gid != self.gid
                    or stat.S_IMODE(status.st_mode) & 0o022
                    or (expected_mode is not None and stat.S_IMODE(status.st_mode) != expected_mode)
                ):
                    os.close(child)
                    _raise("maintenance_install_directory_invalid")
                os.close(current)
                current = child
            return current
        except FileNotFoundError:
            os.close(current)
            raise
        except (OSError, MaintenanceInstallError) as exc:
            os.close(current)
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc

    def ensure_dir(self, logical: str, *, mode: int) -> None:
        descriptor = self.open_dir(logical, create=True, final_mode=mode)
        os.close(descriptor)

    def _parent(self, logical: str, *, create: bool = False) -> tuple[int, str]:
        parts = self._parts(logical)
        parent = "/" + "/".join(parts[:-1])
        if len(parts) == 1:
            return os.dup(self.fd), parts[-1]
        return self.open_dir(parent, create=create), parts[-1]

    def list_dir(self, logical: str) -> tuple[str, ...]:
        try:
            descriptor = self.open_dir(logical)
        except FileNotFoundError as exc:
            raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc
        try:
            return tuple(sorted(os.listdir(descriptor)))
        except OSError as exc:
            raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc
        finally:
            os.close(descriptor)

    def status(self, logical: str) -> os.stat_result | None:
        parent = -1
        try:
            parent, name = self._parent(logical)
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if parent >= 0:
                os.close(parent)

    def _read(
        self,
        logical: str,
        *,
        maximum: int,
        modes: frozenset[int],
        links: frozenset[int] = frozenset({1}),
        retain: bool = True,
    ) -> tuple[bytes | None, os.stat_result, str]:
        parent = descriptor = -1
        try:
            parent, name = self._parent(logical)
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                _raise("maintenance_install_artifact_invalid")
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, min(1 << 20, maximum + 1 - total))
                if not chunk:
                    break
                digest.update(chunk)
                if retain:
                    chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    _raise("maintenance_install_artifact_invalid")
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except (OSError, MaintenanceInstallError) as exc:
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != self.uid
            or before.st_gid != self.gid
            or stat.S_IMODE(before.st_mode) not in modes
            or before.st_nlink not in links
            or _identity(before) != _identity(opened)
            or _identity(before) != _identity(after)
        ):
            _raise("maintenance_install_artifact_invalid")
        return (b"".join(chunks) if retain else None), before, digest.hexdigest()

    def read_exact(
        self,
        logical: str,
        *,
        expected_sha256: str,
        mode: int,
        maximum: int = MAX_INITRD_BYTES,
        links: frozenset[int] = frozenset({1}),
        retain: bool = True,
    ) -> bytes:
        raw, _status, observed_sha256 = self._read(
            logical,
            maximum=maximum,
            modes=frozenset({mode}),
            links=links,
            retain=retain,
        )
        if observed_sha256 != expected_sha256:
            _raise("maintenance_install_artifact_invalid")
        return raw if raw is not None else b""

    def write_new(self, logical: str, raw: bytes, *, mode: int) -> None:
        parent = descriptor = -1
        try:
            parent, name = self._parent(logical)
            previous_umask = os.umask(0)
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    mode,
                    dir_fd=parent,
                )
            finally:
                os.umask(previous_umask)
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, self.uid, self.gid)
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.fsync(parent)
        except OSError as exc:
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)

    def chmod(self, logical: str, mode: int) -> None:
        parent = descriptor = -1
        try:
            parent, name = self._parent(logical)
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != self.uid
                or status.st_gid != self.gid
                or status.st_nlink != 1
            ):
                _raise("maintenance_install_artifact_invalid")
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            os.fsync(parent)
        except (OSError, MaintenanceInstallError) as exc:
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)

    def replace(self, source: str, target: str) -> None:
        source_parent = target_parent = -1
        try:
            source_parent, source_name = self._parent(source)
            target_parent, target_name = self._parent(target)
            os.replace(
                source_name,
                target_name,
                src_dir_fd=source_parent,
                dst_dir_fd=target_parent,
            )
            os.fsync(source_parent)
            if target_parent != source_parent:
                os.fsync(target_parent)
        except OSError as exc:
            raise MaintenanceInstallError("maintenance_install_journal_invalid") from exc
        finally:
            if source_parent >= 0:
                os.close(source_parent)
            if target_parent >= 0:
                os.close(target_parent)

    def unlink_exact(
        self,
        logical: str,
        *,
        expected_sha256: str,
        mode: int,
        maximum: int = MAX_INITRD_BYTES,
        allow_absent: bool = True,
        retain: bool = True,
    ) -> None:
        status = self.status(logical)
        if status is None:
            if allow_absent:
                return
            _raise("maintenance_install_artifact_invalid")
        self.read_exact(
            logical,
            expected_sha256=expected_sha256,
            mode=mode,
            maximum=maximum,
            links=frozenset({1, 2}),
            retain=retain,
        )
        parent = -1
        try:
            parent, name = self._parent(logical)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(current) != _identity(status):
                _raise("maintenance_install_artifact_invalid")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        except (OSError, MaintenanceInstallError) as exc:
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if parent >= 0:
                os.close(parent)

    def unlink_structural(
        self,
        logical: str,
        *,
        modes: frozenset[int],
        allow_absent: bool = True,
    ) -> None:
        status = self.status(logical)
        if status is None:
            if allow_absent:
                return
            _raise("maintenance_install_artifact_invalid")
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != self.uid
            or status.st_gid != self.gid
            or stat.S_IMODE(status.st_mode) not in modes
            or status.st_nlink != 1
        ):
            _raise("maintenance_install_artifact_invalid")
        parent = -1
        try:
            parent, name = self._parent(logical)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(current) != _identity(status):
                _raise("maintenance_install_artifact_invalid")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        except (OSError, MaintenanceInstallError) as exc:
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if parent >= 0:
                os.close(parent)

    def publish_link(
        self,
        stage: str,
        target: str,
        *,
        expected_sha256: str,
        mode: int,
        maximum: int = MAX_INITRD_BYTES,
        retain: bool = True,
    ) -> None:
        target_status = self.status(target)
        stage_status = self.status(stage)
        if target_status is not None:
            self.read_exact(
                target,
                expected_sha256=expected_sha256,
                mode=mode,
                maximum=maximum,
                links=frozenset({1, 2}),
                retain=retain,
            )
            if stage_status is not None:
                self.read_exact(
                    stage,
                    expected_sha256=expected_sha256,
                    mode=mode,
                    maximum=maximum,
                    links=frozenset({1, 2}),
                    retain=retain,
                )
                current_target = self.status(target)
                current_stage = self.status(stage)
                if (
                    current_target is None
                    or current_stage is None
                    or (current_target.st_dev, current_target.st_ino)
                    != (current_stage.st_dev, current_stage.st_ino)
                ):
                    _raise("maintenance_install_artifact_invalid")
                self.unlink_exact(
                    stage,
                    expected_sha256=expected_sha256,
                    mode=mode,
                    maximum=maximum,
                    retain=retain,
                )
            self.read_exact(
                target,
                expected_sha256=expected_sha256,
                mode=mode,
                maximum=maximum,
                retain=retain,
            )
            return
        if stage_status is None:
            _raise("maintenance_install_artifact_invalid")
        self.read_exact(
            stage,
            expected_sha256=expected_sha256,
            mode=mode,
            maximum=maximum,
            retain=retain,
        )
        source_parent = target_parent = -1
        try:
            source_parent, source_name = self._parent(stage)
            target_parent, target_name = self._parent(target)
            os.link(
                source_name,
                target_name,
                src_dir_fd=source_parent,
                dst_dir_fd=target_parent,
                follow_symlinks=False,
            )
            os.fsync(target_parent)
        except OSError as exc:
            raise MaintenanceInstallError("maintenance_install_artifact_invalid") from exc
        finally:
            if source_parent >= 0:
                os.close(source_parent)
            if target_parent >= 0:
                os.close(target_parent)
        self.publish_link(
            stage,
            target,
            expected_sha256=expected_sha256,
            mode=mode,
            maximum=maximum,
            retain=retain,
        )

    def remove_empty_dir(self, logical: str, *, mode: int) -> None:
        status = self.status(logical)
        if status is None:
            return
        if not stat.S_ISDIR(status.st_mode):
            _raise("maintenance_install_directory_invalid")
        descriptor = parent = -1
        try:
            parent, name = self._parent(logical)
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            opened = os.fstat(descriptor)
            if (
                _identity(status) != _identity(opened)
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != self.uid
                or opened.st_gid != self.gid
                or stat.S_IMODE(opened.st_mode) != mode
                or os.listdir(descriptor)
            ):
                _raise("maintenance_install_directory_invalid")
            os.rmdir(name, dir_fd=parent)
            os.fsync(parent)
        except (OSError, MaintenanceInstallError) as exc:
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)

    def remove_private_tree(self, logical: str, *, mode: int) -> None:
        """Remove one journal-private tree without following any descendant."""

        status = self.status(logical)
        if status is None:
            return
        parent = descriptor = -1
        try:
            parent, name = self._parent(logical)
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
            opened = os.fstat(descriptor)
            parent_mount_id = _descriptor_mount_id(parent)
            root_mount_id = _descriptor_mount_id(descriptor)
            if (
                _identity(status) != _identity(opened)
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != self.uid
                or opened.st_gid != self.gid
                or stat.S_IMODE(opened.st_mode) != mode
                or root_mount_id != parent_mount_id
            ):
                _raise("maintenance_install_directory_invalid")
            root_device = opened.st_dev
            entries = [0]

            def clear(current: int, depth: int) -> None:
                if depth > MAX_PRIVATE_TREE_DEPTH:
                    _raise("maintenance_install_directory_invalid")
                while True:
                    names: list[str] = []
                    with os.scandir(current) as iterator:
                        for entry in iterator:
                            names.append(entry.name)
                            if len(names) >= 1024:
                                break
                    if not names:
                        break
                    for child_name in sorted(names):
                        entries[0] += 1
                        if entries[0] > MAX_PRIVATE_TREE_ENTRIES:
                            _raise("maintenance_install_directory_invalid")
                        child_status = os.stat(
                            child_name,
                            dir_fd=current,
                            follow_symlinks=False,
                        )
                        if stat.S_ISDIR(child_status.st_mode):
                            child = os.open(
                                child_name,
                                os.O_RDONLY
                                | getattr(os, "O_CLOEXEC", 0)
                                | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0)
                                | getattr(os, "O_NONBLOCK", 0),
                                dir_fd=current,
                            )
                            try:
                                child_opened = os.fstat(child)
                                if (
                                    _identity(child_status) != _identity(child_opened)
                                    or child_opened.st_dev != root_device
                                    or _descriptor_mount_id(child) != root_mount_id
                                ):
                                    _raise("maintenance_install_directory_invalid")
                                clear(child, depth + 1)
                                child_final = os.fstat(child)
                                current_child = os.stat(
                                    child_name,
                                    dir_fd=current,
                                    follow_symlinks=False,
                                )
                                with os.scandir(child) as final_iterator:
                                    child_not_empty = next(final_iterator, None) is not None
                                if _identity(child_final) != _identity(current_child) or child_not_empty:
                                    _raise("maintenance_install_directory_invalid")
                                os.fsync(child)
                            finally:
                                os.close(child)
                            os.rmdir(child_name, dir_fd=current)
                            os.fsync(current)
                        else:
                            if child_status.st_dev != root_device:
                                _raise("maintenance_install_directory_invalid")
                            current_child = os.stat(
                                child_name,
                                dir_fd=current,
                                follow_symlinks=False,
                            )
                            if _identity(child_status) != _identity(current_child):
                                _raise("maintenance_install_directory_invalid")
                            os.unlink(child_name, dir_fd=current)
                os.fsync(current)

            clear(descriptor, 0)
            final = os.fstat(descriptor)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(final) != _identity(current):
                _raise("maintenance_install_directory_invalid")
            os.close(descriptor)
            descriptor = -1
            os.rmdir(name, dir_fd=parent)
            os.fsync(parent)
        except (OSError, MaintenanceInstallError) as exc:
            if isinstance(exc, MaintenanceInstallError):
                raise
            raise MaintenanceInstallError("maintenance_install_directory_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)


@dataclass(frozen=True)
class Layout:
    transaction_id: str

    @property
    def launcher(self) -> str:
        return "/usr/libexec/friday/release_artifact_retention_maintenance_launcher"

    @property
    def controller(self) -> str:
        return "/usr/libexec/friday/release_artifact_retention_maintenance.py"

    @property
    def module_dir(self) -> str:
        return "/usr/lib/dracut/modules.d/99friday-retention-maintenance"

    @property
    def module(self) -> str:
        return f"{self.module_dir}/module-setup.sh"

    @property
    def hook(self) -> str:
        return "/usr/libexec/friday/release_artifact_retention_maintenance_hook.sh"

    @property
    def runner(self) -> str:
        return "/usr/libexec/friday/release_artifact_retention_maintenance_runner.sh"

    @property
    def root_request(self) -> str:
        return (
            "/usr/libexec/friday/"
            f"release_artifact_retention_maintenance_request-{self.transaction_id}.v1.json"
        )

    @property
    def image_authority(self) -> str:
        return (
            f"/usr/libexec/friday/release_artifact_retention_maintenance_image-{self.transaction_id}.v1.json"
        )

    @property
    def config_dir(self) -> str:
        return f"/usr/libexec/friday/retention-maintenance-image-config-{self.transaction_id}.v1"

    @property
    def initrd(self) -> str:
        return f"/boot/friday-retention-maintenance-{self.transaction_id}.img"

    @property
    def initrd_stage(self) -> str:
        return f"/boot/.friday-retention-maintenance-{self.transaction_id}.img.new"

    @property
    def maintenance_policy(self) -> str:
        return MAINTENANCE_POLICY_PATH

    @property
    def maintenance_policy_stage(self) -> str:
        return f"/etc/sudoers.d/.friday-retention-maintenance-probe.{self.transaction_id}.new"

    @property
    def dracut_tmp_dir(self) -> str:
        return f"/usr/libexec/friday/.maintenance-dracut-{self.transaction_id}.v1"

    @property
    def launcher_source_stage(self) -> str:
        return f"/usr/libexec/friday/.maintenance-launcher-{self.transaction_id}.S"

    @property
    def launcher_object_stage(self) -> str:
        return f"/usr/libexec/friday/.maintenance-launcher-{self.transaction_id}.o"

    def component_stage(self, role: str) -> str:
        targets = {
            "launcher": self.launcher,
            "controller": self.controller,
            "module": self.module,
            "hook": self.hook,
            "runner": self.runner,
            "request": self.root_request,
            "image_authority": self.image_authority,
        }
        target = targets[role]
        path = Path(target)
        return str(path.parent / f".{path.name}.{self.transaction_id}.new")

    def config_stage(self, name: str) -> str:
        return f"{self.config_dir}/.{name}.{self.transaction_id}.new"

    def artifact_projection(self) -> dict[str, Any]:
        components = {
            role: {
                "mode": mode,
                "stage": self.component_stage(role),
                "target": target,
            }
            for role, target, mode in (
                ("launcher", self.launcher, 0o555),
                ("controller", self.controller, 0o555),
                ("module", self.module, 0o555),
                ("hook", self.hook, 0o555),
                ("runner", self.runner, 0o555),
                ("request", self.root_request, 0o444),
                ("image_authority", self.image_authority, 0o400),
            )
        }
        components["launcher"]["transient_stage_modes"] = [
            0o400,
            0o555,
            0o600,
            0o644,
            0o700,
            0o755,
        ]
        return {
            "components": components,
            "config": {
                "directory": self.config_dir,
                "directory_mode": 0o700,
                "entries": {
                    name: {
                        "mode": 0o400,
                        "stage": self.config_stage(name),
                        "target": f"{self.config_dir}/{name}",
                    }
                    for name in _CONFIG_NAMES
                },
            },
            "initrd": {"mode": 0o600, "stage": self.initrd_stage, "target": self.initrd},
            "maintenance_policy": {
                "mode": 0o440,
                "stage": self.maintenance_policy_stage,
                "target": self.maintenance_policy,
            },
            "privileged_proc_helper": {
                "install_lock": PRIVILEGED_PROC_INSTALL_LOCK_PATH,
                "install_lock_mode": 0o600,
                "mode": 0o755,
                "target": PRIVILEGED_PROC_HELPER_PATH,
            },
            "initrd_build_directory": {"mode": 0o700, "path": self.dracut_tmp_dir},
            "module_directory": {"mode": 0o755, "path": self.module_dir},
            "private_compile_stages": {
                "object": {
                    "path": self.launcher_object_stage,
                    "transient_modes": [0o400, 0o600, 0o644],
                },
                "source": {"mode": 0o400, "path": self.launcher_source_stage},
            },
            "schema": ARTIFACT_SET_SCHEMA,
            "transaction_id": self.transaction_id,
        }

    def reserved_paths(self) -> frozenset[str]:
        component_roles = (
            "launcher",
            "controller",
            "module",
            "hook",
            "runner",
            "request",
            "image_authority",
        )
        paths = {
            JOURNAL_PATH,
            JOURNAL_STAGE_PATH,
            LOCK_PATH,
            self.config_dir,
            self.dracut_tmp_dir,
            self.initrd,
            self.initrd_stage,
            self.maintenance_policy,
            self.maintenance_policy_stage,
            self.launcher_source_stage,
            self.launcher_object_stage,
            self.module_dir,
            *(self.component_stage(role) for role in component_roles),
            *(self.config_stage(name) for name in _CONFIG_NAMES),
            *(f"{self.config_dir}/{name}" for name in _CONFIG_NAMES),
        }
        paths.update(
            {
                self.launcher,
                self.controller,
                self.module,
                self.hook,
                self.runner,
                self.root_request,
                self.image_authority,
            }
        )
        return frozenset(paths)


@dataclass(frozen=True)
class InstallInputs:
    request_path: str
    request: dict[str, Any]
    request_raw: bytes
    owner: str
    owner_uid: int
    source_payloads: dict[str, bytes]
    launcher_source_sha256: str
    module_sha256: str
    hook_sha256: str
    runner_sha256: str
    controller_sha256: str
    toolchain_root: str
    toolchain_manifest_sha256: str
    ordinary_profile_sha256: str
    ordinary_io_uring_disabled: int
    ordinary_root_device_id: str
    ordinary_root_filesystem_uuid: str
    maintenance_cmdline_sha256: str
    maintenance_policy_python: str
    maintenance_policy_python_sha256: str
    maintenance_policy_sha256: str
    privileged_proc_helper_sha256: str
    request_sha256: str
    request_file_sha256: str
    transaction_id: str

    def journal_identity(self) -> dict[str, Any]:
        layout = Layout(self.transaction_id)
        artifact_set = {
            **layout.artifact_projection(),
            "controller_sha256": self.controller_sha256,
            "hook_sha256": self.hook_sha256,
            "launcher_source_sha256": self.launcher_source_sha256,
            "maintenance_policy_python": self.maintenance_policy_python,
            "maintenance_policy_python_sha256": self.maintenance_policy_python_sha256,
            "maintenance_policy_sha256": self.maintenance_policy_sha256,
            "module_sha256": self.module_sha256,
            "privileged_proc_helper_sha256": self.privileged_proc_helper_sha256,
            "request_file_sha256": self.request_file_sha256,
            "runner_sha256": self.runner_sha256,
        }
        return {
            "artifact_set": artifact_set,
            "artifact_set_sha256": hashlib.sha256(_canonical(artifact_set)).hexdigest(),
            "controller_sha256": self.controller_sha256,
            "hook_sha256": self.hook_sha256,
            "launcher_source_sha256": self.launcher_source_sha256,
            "maintenance_cmdline_sha256": self.maintenance_cmdline_sha256,
            "maintenance_policy_python": self.maintenance_policy_python,
            "maintenance_policy_python_sha256": self.maintenance_policy_python_sha256,
            "maintenance_policy_sha256": self.maintenance_policy_sha256,
            "module_sha256": self.module_sha256,
            "ordinary_io_uring_disabled": self.ordinary_io_uring_disabled,
            "ordinary_profile_sha256": self.ordinary_profile_sha256,
            "ordinary_root_device_id": self.ordinary_root_device_id,
            "ordinary_root_filesystem_uuid": self.ordinary_root_filesystem_uuid,
            "owner": self.owner,
            "owner_uid": self.owner_uid,
            "privileged_proc_helper_sha256": self.privileged_proc_helper_sha256,
            "request_file_sha256": self.request_file_sha256,
            "request_path": self.request_path,
            "request_sha256": self.request_sha256,
            "runner_sha256": self.runner_sha256,
            "toolchain_manifest_sha256": self.toolchain_manifest_sha256,
            "toolchain_root": self.toolchain_root,
            "transaction_id": self.transaction_id,
        }


_IDENTITY_KEYS = frozenset(
    {
        "artifact_set",
        "artifact_set_sha256",
        "controller_sha256",
        "hook_sha256",
        "launcher_source_sha256",
        "maintenance_cmdline_sha256",
        "maintenance_policy_python",
        "maintenance_policy_python_sha256",
        "maintenance_policy_sha256",
        "module_sha256",
        "ordinary_io_uring_disabled",
        "ordinary_profile_sha256",
        "ordinary_root_device_id",
        "ordinary_root_filesystem_uuid",
        "owner",
        "owner_uid",
        "privileged_proc_helper_sha256",
        "request_file_sha256",
        "request_path",
        "request_sha256",
        "runner_sha256",
        "toolchain_manifest_sha256",
        "toolchain_root",
        "transaction_id",
    }
)
_JOURNAL_KEYS = frozenset(
    {
        *_IDENTITY_KEYS,
        "generation",
        "image_authority_sha256",
        "journal_sha256",
        "launcher_sha256",
        "maintenance_initrd_sha256",
        "phase",
        "previous_journal_sha256",
        "retired_transaction_ids",
        "schema",
    }
)


def _new_journal(
    inputs: InstallInputs,
    *,
    predecessor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if predecessor is None:
        generation = 0
        previous = ""
        retired: list[str] = []
    else:
        current = _validate_journal(predecessor)
        if current["phase"] != "removed":
            _raise("maintenance_install_transition_invalid")
        retired = [
            *current["retired_transaction_ids"],
            str(current["transaction_id"]),
        ]
        if len(retired) > MAX_RETIRED_TRANSACTIONS or inputs.transaction_id in retired:
            _raise("maintenance_install_transaction_replayed")
        generation = int(current["generation"]) + 1
        previous = str(current["journal_sha256"])
    core = {
        **inputs.journal_identity(),
        "generation": generation,
        "image_authority_sha256": "",
        "launcher_sha256": "",
        "maintenance_initrd_sha256": "",
        "phase": "install_prepared",
        "previous_journal_sha256": previous,
        "retired_transaction_ids": retired,
        "schema": JOURNAL_SCHEMA,
    }
    return {**core, "journal_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _validate_journal(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    digest = result.get("journal_sha256")
    core = {name: item for name, item in result.items() if name != "journal_sha256"}
    phase = result.get("phase")
    launcher = result.get("launcher_sha256")
    image = result.get("image_authority_sha256")
    initrd = result.get("maintenance_initrd_sha256")
    retired = result.get("retired_transaction_ids")
    if (
        set(result) != _JOURNAL_KEYS
        or result.get("schema") != JOURNAL_SCHEMA
        or phase not in ALL_PHASES
        or type(result.get("generation")) is not int
        or int(result["generation"]) < 0
        or not isinstance(result.get("previous_journal_sha256"), str)
        or result["previous_journal_sha256"] != ""
        and _HEX64.fullmatch(str(result["previous_journal_sha256"])) is None
        or (int(result["generation"]) == 0) != (result["previous_journal_sha256"] == "")
        or not isinstance(digest, str)
        or _HEX64.fullmatch(digest) is None
        or digest != hashlib.sha256(_canonical(core)).hexdigest()
        or any(
            _HEX64.fullmatch(str(result.get(name))) is None
            for name in (
                "artifact_set_sha256",
                "controller_sha256",
                "hook_sha256",
                "launcher_source_sha256",
                "maintenance_cmdline_sha256",
                "maintenance_policy_python_sha256",
                "maintenance_policy_sha256",
                "module_sha256",
                "ordinary_profile_sha256",
                "privileged_proc_helper_sha256",
                "request_file_sha256",
                "request_sha256",
                "runner_sha256",
                "toolchain_manifest_sha256",
                "transaction_id",
            )
        )
        or not isinstance(result.get("owner"), str)
        or _SAFE_USER.fullmatch(str(result["owner"])) is None
        or result["owner"] in {"ALL", "root"}
        or type(result.get("owner_uid")) is not int
        or int(result["owner_uid"]) <= 0
        or type(result.get("ordinary_io_uring_disabled")) is not int
        or result.get("ordinary_io_uring_disabled") not in {0, 1}
        or not isinstance(result.get("ordinary_root_device_id"), str)
        or re.fullmatch(r"[0-9]+:[0-9]+", str(result["ordinary_root_device_id"])) is None
        or not isinstance(result.get("ordinary_root_filesystem_uuid"), str)
        or _FILESYSTEM_UUID.fullmatch(str(result["ordinary_root_filesystem_uuid"])) is None
        or not isinstance(launcher, str)
        or launcher != ""
        and _HEX64.fullmatch(launcher) is None
        or not isinstance(image, str)
        or image != ""
        and _HEX64.fullmatch(image) is None
        or bool(launcher) != bool(image)
        or not isinstance(initrd, str)
        or initrd != ""
        and _HEX64.fullmatch(initrd) is None
        or bool(initrd)
        and not bool(launcher)
        or not isinstance(retired, list)
        or len(retired) > MAX_RETIRED_TRANSACTIONS
        or any(not isinstance(item, str) or _HEX64.fullmatch(item) is None for item in retired)
        or len(retired) != len(set(retired))
        or result.get("transaction_id") in retired
    ):
        _raise("maintenance_install_journal_invalid")
    for name in ("request_path", "toolchain_root"):
        _safe_absolute(result.get(name), code="maintenance_install_journal_invalid")
    _safe_config(result.get("request_path"), code="maintenance_install_journal_invalid")
    python = _safe_absolute(
        result.get("maintenance_policy_python"),
        code="maintenance_install_journal_invalid",
    )
    _safe_config(python, code="maintenance_install_journal_invalid")
    if (
        hashlib.sha256(
            _maintenance_policy_payload(
                owner_uid=int(result["owner_uid"]),
                python=python,
            )
        ).hexdigest()
        != result["maintenance_policy_sha256"]
    ):
        _raise("maintenance_install_journal_invalid")
    if phase in INSTALL_PHASES[1:] and not launcher:
        _raise("maintenance_install_journal_invalid")
    if (
        phase
        in {
            "initrd_staged",
            "initrd_publishing",
            "policy_publishing",
            "policy_published",
            "installed_not_armed",
        }
        and not initrd
    ):
        _raise("maintenance_install_journal_invalid")
    layout = Layout(str(result["transaction_id"]))
    artifact_set = {
        **layout.artifact_projection(),
        "controller_sha256": result["controller_sha256"],
        "hook_sha256": result["hook_sha256"],
        "launcher_source_sha256": result["launcher_source_sha256"],
        "maintenance_policy_python": result["maintenance_policy_python"],
        "maintenance_policy_python_sha256": result["maintenance_policy_python_sha256"],
        "maintenance_policy_sha256": result["maintenance_policy_sha256"],
        "module_sha256": result["module_sha256"],
        "privileged_proc_helper_sha256": result["privileged_proc_helper_sha256"],
        "request_file_sha256": result["request_file_sha256"],
        "runner_sha256": result["runner_sha256"],
    }
    if (
        result["artifact_set"] != artifact_set
        or result["artifact_set_sha256"] != hashlib.sha256(_canonical(artifact_set)).hexdigest()
    ):
        _raise("maintenance_install_journal_invalid")
    return result


def _journal_bytes(value: Mapping[str, Any]) -> bytes:
    return _canonical(_validate_journal(value)) + b"\n"


def _same_identity(journal: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    return all(journal.get(name) == identity.get(name) for name in _IDENTITY_KEYS)


def _valid_transition(current: Mapping[str, Any], successor: Mapping[str, Any]) -> bool:
    try:
        left = _validate_journal(current)
        right = _validate_journal(successor)
    except MaintenanceInstallError:
        return False
    if (
        right["phase"] not in _TRANSITIONS[str(left["phase"])]
        or right["generation"] != int(left["generation"]) + 1
        or right["previous_journal_sha256"] != left["journal_sha256"]
        or not _same_identity(right, left)
        or right["retired_transaction_ids"] != left["retired_transaction_ids"]
    ):
        return False
    changed = {
        name
        for name in (
            "launcher_sha256",
            "image_authority_sha256",
            "maintenance_initrd_sha256",
        )
        if left[name] != right[name]
    }
    allowed: set[str] = set()
    if left["phase"] == "install_prepared" and right["phase"] == "payloads_staged":
        allowed = {"launcher_sha256", "image_authority_sha256"}
    elif left["phase"] == "initrd_building" and right["phase"] == "initrd_staged":
        allowed = {"maintenance_initrd_sha256"}
    return changed == allowed


def _valid_rollover(current: Mapping[str, Any], successor: Mapping[str, Any]) -> bool:
    try:
        left = _validate_journal(current)
        right = _validate_journal(successor)
    except MaintenanceInstallError:
        return False
    expected_retired = [
        *left["retired_transaction_ids"],
        str(left["transaction_id"]),
    ]
    return (
        left["phase"] == "removed"
        and right["phase"] == "install_prepared"
        and right["generation"] == int(left["generation"]) + 1
        and right["previous_journal_sha256"] == left["journal_sha256"]
        and right["retired_transaction_ids"] == expected_retired
        and right["transaction_id"] not in expected_retired
        and right["launcher_sha256"] == ""
        and right["image_authority_sha256"] == ""
        and right["maintenance_initrd_sha256"] == ""
    )


def _successor(
    current: Mapping[str, Any],
    phase: str,
    *,
    launcher_sha256: str | None = None,
    image_authority_sha256: str | None = None,
    maintenance_initrd_sha256: str | None = None,
) -> dict[str, Any]:
    value = _validate_journal(current)
    if phase not in _TRANSITIONS[str(value["phase"])]:
        _raise("maintenance_install_transition_invalid")
    core = {name: item for name, item in value.items() if name != "journal_sha256"}
    core.update(
        {
            "generation": int(value["generation"]) + 1,
            "phase": phase,
            "previous_journal_sha256": value["journal_sha256"],
        }
    )
    if launcher_sha256 is not None:
        core["launcher_sha256"] = _hex64(launcher_sha256, code="maintenance_install_transition_invalid")
    if image_authority_sha256 is not None:
        core["image_authority_sha256"] = _hex64(
            image_authority_sha256, code="maintenance_install_transition_invalid"
        )
    if maintenance_initrd_sha256 is not None:
        core["maintenance_initrd_sha256"] = _hex64(
            maintenance_initrd_sha256,
            code="maintenance_install_transition_invalid",
        )
    result = {**core, "journal_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    if not _valid_transition(value, result):
        _raise("maintenance_install_transition_invalid")
    return result


def _source_payload(
    directory: Path,
    name: str,
    expected_sha256: str,
) -> bytes:
    evidence = _external_file(
        directory / name,
        maximum=MAX_SOURCE_BYTES,
        code="maintenance_install_source_invalid",
        retain=True,
    )
    if evidence.sha256 != expected_sha256 or evidence.raw is None or evidence.size == 0:
        _raise("maintenance_install_source_invalid")
    return evidence.raw


def _resolved_root_owned_python() -> tuple[str, str]:
    code = "maintenance_install_policy_invalid"
    try:
        resolved = Path("/usr/bin/python3").resolve(strict=True)
        if re.fullmatch(r"/usr/bin/python3\.[0-9]+", str(resolved)) is None:
            _raise(code)
        for parent in (Path("/"), *reversed(resolved.parents[:-1])):
            status = os.lstat(parent)
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != 0 or stat.S_IMODE(status.st_mode) & 0o022:
                _raise(code)
    except (OSError, MaintenanceInstallError) as exc:
        if isinstance(exc, MaintenanceInstallError):
            raise
        raise MaintenanceInstallError(code) from exc
    evidence = _external_file(
        resolved,
        maximum=MAX_PROFILE_BYTES,
        code=code,
        expected_uid=0,
    )
    if evidence.size == 0 or evidence.mode & 0o6000 or not evidence.mode & 0o111:
        _raise(code)
    return str(resolved), evidence.sha256


def _block_inventory(*, code: str) -> tuple[tuple[str, str, str], ...]:
    """Take one bounded, internally stable snapshot of current block devices."""

    directory = Path("/sys/class/block")
    try:
        names_before = tuple(sorted(os.listdir(directory)))
        directory_before = os.stat(directory, follow_symlinks=False)
    except OSError as exc:
        raise MaintenanceInstallError(code) from exc
    if (
        not 1 <= len(names_before) <= MAX_BLOCK_DEVICES
        or not stat.S_ISDIR(directory_before.st_mode)
        or directory_before.st_uid != 0
        or len(set(names_before)) != len(names_before)
    ):
        _raise(code)
    inventory: list[tuple[str, str, str]] = []
    device_ids: set[str] = set()
    try:
        for name in names_before:
            if _BLOCK_DEVICE_NAME.fullmatch(name) is None:
                _raise(code)
            sys_device = directory / name
            device_before = os.stat(sys_device)
            resolved_before = str(sys_device.resolve(strict=True))
            dev_path = sys_device / "dev"
            dev_before = os.stat(dev_path)
            raw = dev_path.read_bytes()
            dev_after = os.stat(dev_path)
            resolved_after = str(sys_device.resolve(strict=True))
            device_after = os.stat(sys_device)
            if (
                not stat.S_ISDIR(device_before.st_mode)
                or device_before.st_uid != 0
                or _identity(device_before) != _identity(device_after)
                or resolved_before != resolved_after
                or not resolved_before.startswith("/sys/devices/")
                or not stat.S_ISREG(dev_before.st_mode)
                or dev_before.st_uid != 0
                or _identity(dev_before) != _identity(dev_after)
                or len(raw) > 32
                or re.fullmatch(rb"[0-9]+:[0-9]+\n", raw) is None
            ):
                _raise(code)
            device_id = raw[:-1].decode("ascii")
            if device_id in device_ids:
                _raise(code)
            device_ids.add(device_id)
            inventory.append((name, device_id, resolved_before))
        names_after = tuple(sorted(os.listdir(directory)))
        directory_after = os.stat(directory, follow_symlinks=False)
    except (OSError, UnicodeError, MaintenanceInstallError) as exc:
        if isinstance(exc, MaintenanceInstallError):
            raise
        raise MaintenanceInstallError(code) from exc
    if names_after != names_before or _identity(directory_before) != _identity(directory_after):
        _raise(code)
    return tuple(inventory)


def _block_node_evidence(
    device_id: str,
    *,
    code: str,
) -> tuple[Path, tuple[int, ...], tuple[int, ...], str]:
    if _BLOCK_DEVICE_ID.fullmatch(device_id) is None:
        _raise(code)
    path = Path("/dev/block") / device_id
    try:
        link_before = os.lstat(path)
        target_before = os.stat(path)
        resolved_before = str(path.resolve(strict=True))
        target_after = os.stat(path)
        link_after = os.lstat(path)
        resolved_after = str(path.resolve(strict=True))
    except OSError as exc:
        raise MaintenanceInstallError(code) from exc
    expected_major, expected_minor = (int(value) for value in device_id.split(":"))
    if (
        not (stat.S_ISLNK(link_before.st_mode) or stat.S_ISBLK(link_before.st_mode))
        or link_before.st_uid != 0
        or _identity(link_before) != _identity(link_after)
        or not stat.S_ISBLK(target_before.st_mode)
        or target_before.st_uid != 0
        or os.major(target_before.st_rdev) != expected_major
        or os.minor(target_before.st_rdev) != expected_minor
        or _identity(target_before) != _identity(target_after)
        or resolved_before != resolved_after
    ):
        _raise(code)
    return path, _identity(link_before), _identity(target_before), resolved_before


def _blkid_executable_identity(*, code: str) -> tuple[int, ...]:
    try:
        status = os.stat(BLKID_PATH, follow_symlinks=False)
    except OSError as exc:
        raise MaintenanceInstallError(code) from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != 0
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) & 0o022
        or not stat.S_IMODE(status.st_mode) & 0o111
    ):
        _raise(code)
    return _identity(status)


def _uncached_blkid_value(
    device: Path,
    tag: str,
    *,
    deadline: float,
    code: str,
) -> str | None:
    if tag not in {"TYPE", "UUID"}:
        _raise(code)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise(code)
    try:
        result = subprocess.run(  # noqa: S603
            [
                str(BLKID_PATH),
                "-p",
                "-c",
                "/dev/null",
                "-s",
                tag,
                "-o",
                "value",
                str(device),
            ],
            check=False,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            },
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            timeout=min(5.0, remaining),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MaintenanceInstallError(code) from exc
    if result.returncode == 2 and result.stdout == b"":
        return None
    if (
        result.returncode != 0
        or not 2 <= len(result.stdout) <= MAX_BLKID_OUTPUT_BYTES
        or result.stdout.count(b"\n") != 1
        or not result.stdout.endswith(b"\n")
    ):
        _raise(code)
    try:
        value = result.stdout[:-1].decode("ascii")
    except UnicodeError as exc:
        raise MaintenanceInstallError(code) from exc
    if not value or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        _raise(code)
    return value


def _resolve_unique_root_device_id(root_filesystem_uuid: str, *, code: str) -> str:
    """Resolve a durable UUID to one fresh direct ext4 dev_t without caches."""

    if _FILESYSTEM_UUID.fullmatch(root_filesystem_uuid) is None:
        _raise(code)
    deadline = time.monotonic() + BLOCK_SCAN_TIMEOUT_SECONDS
    blkid_before = _blkid_executable_identity(code=code)
    inventory_before = _block_inventory(code=code)
    matches: list[tuple[str, str]] = []
    for _name, device_id, sys_path in inventory_before:
        device, link_before, target_before, resolved_before = _block_node_evidence(
            device_id,
            code=code,
        )
        candidate_uuid = _uncached_blkid_value(
            device,
            "UUID",
            deadline=deadline,
            code=code,
        )
        _device, link_after, target_after, resolved_after = _block_node_evidence(
            device_id,
            code=code,
        )
        if link_before != link_after or target_before != target_after or resolved_before != resolved_after:
            _raise(code)
        if candidate_uuid == root_filesystem_uuid:
            matches.append((device_id, sys_path))
    inventory_after = _block_inventory(code=code)
    if inventory_after != inventory_before or len(matches) != 1:
        _raise(code)
    device_id, expected_sys_path = matches[0]
    sys_device_path = Path("/sys/dev/block") / device_id
    try:
        sys_path_before = str(sys_device_path.resolve(strict=True))
        slaves_before = tuple(sorted(os.listdir(sys_device_path / "slaves")))
        sys_path_after = str(sys_device_path.resolve(strict=True))
        slaves_after = tuple(sorted(os.listdir(sys_device_path / "slaves")))
    except OSError as exc:
        raise MaintenanceInstallError(code) from exc
    if (
        sys_path_before != expected_sys_path
        or sys_path_after != expected_sys_path
        or slaves_before
        or slaves_after
    ):
        _raise(code)
    device, link_before, target_before, resolved_before = _block_node_evidence(
        device_id,
        code=code,
    )
    if (
        _uncached_blkid_value(
            device,
            "UUID",
            deadline=deadline,
            code=code,
        )
        != root_filesystem_uuid
        or _uncached_blkid_value(
            device,
            "TYPE",
            deadline=deadline,
            code=code,
        )
        != "ext4"
    ):
        _raise(code)
    _device, link_after, target_after, resolved_after = _block_node_evidence(
        device_id,
        code=code,
    )
    if (
        link_before != link_after
        or target_before != target_after
        or resolved_before != resolved_after
        or _block_inventory(code=code) != inventory_before
        or _blkid_executable_identity(code=code) != blkid_before
    ):
        _raise(code)
    return device_id


def _validate_live_ordinary_profile(
    profile: Mapping[str, Any],
    *,
    transaction_id: str,
    maintenance_cmdline_sha256: str,
) -> str:
    cmdline_evidence = _external_file(
        Path("/proc/cmdline"),
        maximum=64 << 10,
        code="maintenance_install_profile_invalid",
        expected_uid=0,
        allowed_modes=frozenset({0o444}),
        retain=True,
    )
    io_evidence = _external_file(
        Path("/proc/sys/kernel/io_uring_disabled"),
        maximum=3,
        code="maintenance_install_profile_invalid",
        expected_uid=0,
        allowed_modes=frozenset({0o644}),
        retain=True,
    )
    mountinfo_evidence = _external_file(
        Path(f"/proc/{os.getpid()}/mountinfo"),
        maximum=4 << 20,
        code="maintenance_install_profile_invalid",
        expected_uid=0,
        allowed_modes=frozenset({0o444}),
        retain=True,
    )
    cmdline_raw = cmdline_evidence.raw
    io_raw = io_evidence.raw
    mountinfo_raw = mountinfo_evidence.raw
    if cmdline_raw is None or io_raw is None or io_raw not in {b"0\n", b"1\n"} or mountinfo_raw is None:
        _raise("maintenance_install_profile_invalid")
    cmdline = cmdline_raw.rstrip(b"\n")
    if (
        any(token.startswith(b"rd.friday.retention=") for token in cmdline.split())
        or hashlib.sha256(cmdline).hexdigest() != profile["cmdline_sha256"]
        or _root_filesystem_uuid(
            cmdline,
            code="maintenance_install_profile_invalid",
        )
        != profile["root_filesystem_uuid"]
        or int(io_raw[:1]) != profile["io_uring_disabled"]
        or hashlib.sha256(cmdline + b" rd.friday.retention=" + transaction_id.encode("ascii")).hexdigest()
        != maintenance_cmdline_sha256
    ):
        _raise("maintenance_install_profile_invalid")
    current_root_device_id = _resolve_unique_root_device_id(
        str(profile["root_filesystem_uuid"]),
        code="maintenance_install_profile_invalid",
    )
    if (
        _external_file(
            Path("/proc/cmdline"),
            maximum=64 << 10,
            code="maintenance_install_profile_invalid",
            expected_uid=0,
            allowed_modes=frozenset({0o444}),
            retain=True,
        ).raw
        != cmdline_raw
        or _external_file(
            Path("/proc/sys/kernel/io_uring_disabled"),
            maximum=3,
            code="maintenance_install_profile_invalid",
            expected_uid=0,
            allowed_modes=frozenset({0o644}),
            retain=True,
        ).raw
        != io_raw
        or _external_file(
            Path(f"/proc/{os.getpid()}/mountinfo"),
            maximum=4 << 20,
            code="maintenance_install_profile_invalid",
            expected_uid=0,
            allowed_modes=frozenset({0o444}),
            retain=True,
        ).raw
        != mountinfo_raw
    ):
        _raise("maintenance_install_profile_invalid")
    roots: list[str] = []
    for line in mountinfo_raw.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[4] == b"/":
            try:
                root_device = fields[2].decode("ascii")
            except UnicodeError as exc:
                raise MaintenanceInstallError("maintenance_install_profile_invalid") from exc
            if re.fullmatch(r"[0-9]+:[0-9]+", root_device) is None:
                _raise("maintenance_install_profile_invalid")
            roots.append(root_device)
    if roots != [current_root_device_id]:
        _raise("maintenance_install_profile_invalid")
    return current_root_device_id


def _load_install_inputs(
    args: argparse.Namespace,
    *,
    system_uid: int,
    root_path: Path,
) -> InstallInputs:
    if os.uname().machine != "x86_64":
        _raise("maintenance_install_platform_invalid")
    expected_digests = {
        "launcher": _hex64(
            args.expected_launcher_source_sha256,
            code="maintenance_install_source_invalid",
        ),
        "module": _hex64(
            args.expected_module_sha256,
            code="maintenance_install_source_invalid",
        ),
        "hook": _hex64(
            args.expected_hook_sha256,
            code="maintenance_install_source_invalid",
        ),
        "runner": _hex64(
            args.expected_runner_sha256,
            code="maintenance_install_source_invalid",
        ),
        "proc_probe": _hex64(
            args.expected_proc_probe_sha256,
            code="maintenance_install_source_invalid",
        ),
    }
    expected_request = _hex64(
        args.expected_request_sha256,
        code="maintenance_install_request_invalid",
    )
    source_directory = Path(_safe_absolute(args.source_directory, code="maintenance_install_source_invalid"))
    request_path = Path(_safe_absolute(args.request, code="maintenance_install_request_invalid"))
    _safe_config(str(request_path), code="maintenance_install_request_invalid")
    if (
        not isinstance(args.owner_user, str)
        or _SAFE_USER.fullmatch(args.owner_user) is None
        or args.owner_user in {"ALL", "root"}
    ):
        _raise("maintenance_install_owner_invalid")
    try:
        owner_record = pwd.getpwnam(args.owner_user)
    except KeyError as exc:
        raise MaintenanceInstallError("maintenance_install_owner_invalid") from exc
    if owner_record.pw_uid <= 0:
        _raise("maintenance_install_owner_invalid")

    helper_path = (
        Path(PRIVILEGED_PROC_HELPER_PATH)
        if root_path == Path("/")
        else root_path / PRIVILEGED_PROC_HELPER_PATH[1:]
    )
    helper_evidence = _external_file(
        helper_path,
        maximum=MAX_SOURCE_BYTES,
        code="maintenance_install_source_invalid",
        expected_uid=system_uid,
        allowed_modes=frozenset({0o755}),
    )
    if helper_evidence.size == 0 or helper_evidence.sha256 != expected_digests["proc_probe"]:
        _raise("maintenance_install_source_invalid")
    policy_python, policy_python_sha256 = _resolved_root_owned_python()
    policy_sha256 = hashlib.sha256(
        _maintenance_policy_payload(
            owner_uid=owner_record.pw_uid,
            python=policy_python,
        )
    ).hexdigest()

    request_evidence = _external_file(
        request_path,
        maximum=MAX_REQUEST_BYTES,
        code="maintenance_install_request_invalid",
        expected_uid=owner_record.pw_uid,
        allowed_modes=frozenset({0o400, 0o600}),
        retain=True,
    )
    if request_evidence.raw is None:
        _raise("maintenance_install_request_invalid")
    request = _canonical_object(
        request_evidence.raw,
        code="maintenance_install_request_invalid",
    )
    expected_request_keys = {
        "candidate_count",
        "candidate_set_sha256",
        "completion_output_path",
        "controller_sha256",
        "inputs",
        "installed_controller_path",
        "maintenance_cmdline_sha256",
        "ordinary_profile",
        "ordinary_profile_sha256",
        "owner_uid",
        "plan_output_path",
        "request_sha256",
        "result_output_path",
        "reviewed_candidates",
        "schema",
        "scope_seed_plan_sha256",
        "toolchain_manifest_sha256",
        "toolchain_root",
        "transaction_id",
    }
    profile = request.get("ordinary_profile")
    profile_keys = {
        "cmdline_sha256",
        "io_uring_disabled",
        "kernel_config_path",
        "kernel_config_sha256",
        "kernel_image_path",
        "kernel_image_sha256",
        "kernel_release",
        "kernel_version_sha256",
        "ordinary_initrd_path",
        "ordinary_initrd_sha256",
        "root_device_id",
        "root_filesystem_uuid",
    }
    request_core = {name: item for name, item in request.items() if name != "request_sha256"}
    candidates = request.get("reviewed_candidates")
    if (
        set(request) != expected_request_keys
        or request.get("schema") != REQUEST_SCHEMA
        or request.get("request_sha256") != expected_request
        or expected_request != hashlib.sha256(_canonical(request_core)).hexdigest()
        or not isinstance(profile, dict)
        or set(profile) != profile_keys
        or request.get("ordinary_profile_sha256") != hashlib.sha256(_canonical(profile)).hexdigest()
        or type(request.get("owner_uid")) is not int
        or request.get("owner_uid") != owner_record.pw_uid
        or not isinstance(candidates, list)
        or not candidates
        or type(request.get("candidate_count")) is not int
        or int(request["candidate_count"]) != len(candidates)
        or request.get("candidate_set_sha256") != hashlib.sha256(_canonical(candidates)).hexdigest()
        or not isinstance(request.get("inputs"), dict)
        or type(profile.get("io_uring_disabled")) is not int
        or profile.get("io_uring_disabled") not in {0, 1}
        or not isinstance(profile.get("kernel_release"), str)
        or not 1 <= len(str(profile["kernel_release"])) <= 256
        or not isinstance(profile.get("root_device_id"), str)
        or re.fullmatch(r"[0-9]+:[0-9]+", str(profile["root_device_id"])) is None
        or not isinstance(profile.get("root_filesystem_uuid"), str)
        or _FILESYSTEM_UUID.fullmatch(str(profile["root_filesystem_uuid"])) is None
    ):
        _raise("maintenance_install_request_invalid")
    for name in (
        "candidate_set_sha256",
        "controller_sha256",
        "maintenance_cmdline_sha256",
        "ordinary_profile_sha256",
        "scope_seed_plan_sha256",
        "toolchain_manifest_sha256",
        "transaction_id",
    ):
        _hex64(request.get(name), code="maintenance_install_request_invalid")
    for name in (
        "cmdline_sha256",
        "kernel_config_sha256",
        "kernel_image_sha256",
        "kernel_version_sha256",
        "ordinary_initrd_sha256",
    ):
        _hex64(profile.get(name), code="maintenance_install_request_invalid")
    for name in ("completion_output_path", "plan_output_path", "result_output_path"):
        _safe_absolute(request.get(name), code="maintenance_install_request_invalid")
    for name in ("kernel_config_path", "kernel_image_path", "ordinary_initrd_path"):
        _safe_absolute(profile.get(name), code="maintenance_install_request_invalid")

    if system_uid == 0:
        _validate_live_ordinary_profile(
            profile,
            transaction_id=str(request["transaction_id"]),
            maintenance_cmdline_sha256=str(request["maintenance_cmdline_sha256"]),
        )
    elif root_path != Path("/") and os.environ.get("FRIDAY_RETENTION_MAINTENANCE_INSTALL_TEST_MODE") == "1":
        # The fake-root harness cannot manufacture block devices.  Its explicit
        # resolver observation exercises cross-boot dev_t rebinding while the
        # authenticated request continues to bind the reviewed UUID/profile.
        test_device_id = os.environ.get(
            "FRIDAY_RETENTION_MAINTENANCE_TEST_RESOLVED_ROOT_DEVICE_ID",
            "",
        )
        test_filesystem_uuid = os.environ.get(
            "FRIDAY_RETENTION_MAINTENANCE_TEST_RESOLVED_ROOT_FILESYSTEM_UUID",
            "",
        )
        if (
            _BLOCK_DEVICE_ID.fullmatch(test_device_id) is None
            or _FILESYSTEM_UUID.fullmatch(test_filesystem_uuid) is None
            or test_filesystem_uuid != profile["root_filesystem_uuid"]
        ):
            _raise("maintenance_install_profile_invalid")

    if request.get("installed_controller_path") != Layout(str(request["transaction_id"])).controller:
        _raise("maintenance_install_request_invalid")
    toolchain_root = Path(
        _safe_absolute(request.get("toolchain_root"), code="maintenance_install_request_invalid")
    )
    layout = Layout(str(request["transaction_id"]))
    reserved = {
        str(Path(logical) if root_path == Path("/") else root_path / logical[1:])
        for logical in layout.reserved_paths()
    }
    reserved_directories = {
        Path(layout.config_dir) if root_path == Path("/") else root_path / layout.config_dir[1:],
        Path(layout.dracut_tmp_dir) if root_path == Path("/") else root_path / layout.dracut_tmp_dir[1:],
        Path(layout.module_dir) if root_path == Path("/") else root_path / layout.module_dir[1:],
    }
    external_paths = {
        request_path,
        helper_path,
        toolchain_root,
        toolchain_root / "manifest.json",
        *(
            source_directory / name
            for name in (
                "release_artifact_retention_maintenance.py",
                "friday-retention-maintenance-launcher.S",
                "module-setup.sh",
                "friday-retention-maintenance-hook.sh",
                "friday-retention-maintenance-runner.sh",
            )
        ),
        *(
            Path(str(profile[name]))
            for name in (
                "kernel_config_path",
                "kernel_image_path",
                "ordinary_initrd_path",
            )
        ),
        *(
            Path(str(request[name]))
            for name in (
                "completion_output_path",
                "plan_output_path",
                "result_output_path",
            )
        ),
    }
    if any(
        str(path) in reserved
        or any(path == directory or directory in path.parents for directory in reserved_directories)
        for path in external_paths
    ):
        _raise("maintenance_install_path_collision")
    manifest = _external_file(
        toolchain_root / "manifest.json",
        maximum=MAX_REQUEST_BYTES,
        code="maintenance_install_toolchain_invalid",
        expected_uid=owner_record.pw_uid,
        allowed_modes=frozenset({0o400}),
    )
    if manifest.sha256 != request["toolchain_manifest_sha256"] or manifest.size == 0:
        _raise("maintenance_install_toolchain_invalid")
    for path_name, digest_name in (
        ("kernel_config_path", "kernel_config_sha256"),
        ("kernel_image_path", "kernel_image_sha256"),
        ("ordinary_initrd_path", "ordinary_initrd_sha256"),
    ):
        evidence = _external_file(
            Path(str(profile[path_name])),
            maximum=MAX_PROFILE_BYTES,
            code="maintenance_install_profile_invalid",
            expected_uid=system_uid,
        )
        if evidence.sha256 != profile[digest_name] or evidence.size == 0:
            _raise("maintenance_install_profile_invalid")
    if (
        profile["kernel_release"] != os.uname().release
        or profile["kernel_version_sha256"] != hashlib.sha256(os.uname().version.encode("utf-8")).hexdigest()
    ):
        _raise("maintenance_install_profile_invalid")

    payloads = {
        "controller": _source_payload(
            source_directory,
            "release_artifact_retention_maintenance.py",
            str(request["controller_sha256"]),
        ),
        "launcher": _source_payload(
            source_directory,
            "friday-retention-maintenance-launcher.S",
            expected_digests["launcher"],
        ),
        "module": _source_payload(
            source_directory,
            "module-setup.sh",
            expected_digests["module"],
        ),
        "hook": _source_payload(
            source_directory,
            "friday-retention-maintenance-hook.sh",
            expected_digests["hook"],
        ),
        "runner": _source_payload(
            source_directory,
            "friday-retention-maintenance-runner.sh",
            expected_digests["runner"],
        ),
    }
    return InstallInputs(
        request_path=str(request_path),
        request=request,
        request_raw=request_evidence.raw,
        owner=args.owner_user,
        owner_uid=owner_record.pw_uid,
        source_payloads=payloads,
        launcher_source_sha256=expected_digests["launcher"],
        module_sha256=expected_digests["module"],
        hook_sha256=expected_digests["hook"],
        runner_sha256=expected_digests["runner"],
        controller_sha256=str(request["controller_sha256"]),
        toolchain_root=str(toolchain_root),
        toolchain_manifest_sha256=manifest.sha256,
        ordinary_profile_sha256=str(request["ordinary_profile_sha256"]),
        ordinary_io_uring_disabled=int(profile["io_uring_disabled"]),
        ordinary_root_device_id=str(profile["root_device_id"]),
        ordinary_root_filesystem_uuid=str(profile["root_filesystem_uuid"]),
        maintenance_cmdline_sha256=str(request["maintenance_cmdline_sha256"]),
        maintenance_policy_python=policy_python,
        maintenance_policy_python_sha256=policy_python_sha256,
        maintenance_policy_sha256=policy_sha256,
        privileged_proc_helper_sha256=helper_evidence.sha256,
        request_sha256=expected_request,
        request_file_sha256=request_evidence.sha256,
        transaction_id=str(request["transaction_id"]),
    )


def _test_mode(root: Path) -> tuple[bool, int, int]:
    enabled = os.environ.get("FRIDAY_RETENTION_MAINTENANCE_INSTALL_TEST_MODE", "0") == "1"
    uid = os.geteuid()
    gid = os.getegid()
    if uid == 0:
        if root != Path("/") or enabled:
            _raise("maintenance_install_root_invalid")
        return False, 0, 0
    if not enabled or root == Path("/"):
        _raise("maintenance_install_root_invalid")
    return True, uid, gid


def _bootstrap(root: RootFS) -> None:
    for directory in ("/etc", "/etc/sudoers.d"):
        if root.status(directory) is None:
            root.ensure_dir(directory, mode=0o755)
        else:
            descriptor = root.open_dir(directory)
            os.close(descriptor)
    root.ensure_dir("/usr", mode=0o755)
    root.ensure_dir("/usr/lib", mode=0o755)
    root.ensure_dir("/usr/libexec", mode=0o755)
    root.ensure_dir("/usr/libexec/friday", mode=0o755)


def _acquire_lock(root: RootFS) -> int:
    status = root.status(LOCK_PATH)
    if status is None:
        root.write_new(LOCK_PATH, b"", mode=0o600)
    else:
        raw, _opened, _digest = root._read(  # noqa: SLF001
            LOCK_PATH,
            maximum=0,
            modes=frozenset({0o600}),
        )
        if raw:
            _raise("maintenance_install_lock_invalid")
    parent = descriptor = -1
    try:
        parent, name = root._parent(LOCK_PATH)  # noqa: SLF001
        descriptor = os.open(
            name,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != root.uid
            or opened.st_gid != root.gid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != 0
        ):
            _raise("maintenance_install_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except (OSError, MaintenanceInstallError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, MaintenanceInstallError):
            raise
        raise MaintenanceInstallError("maintenance_install_lock_invalid") from exc
    finally:
        if parent >= 0:
            os.close(parent)


def _acquire_privileged_proc_lock(root: RootFS) -> int:
    raw, _status, _digest = root._read(  # noqa: SLF001
        PRIVILEGED_PROC_INSTALL_LOCK_PATH,
        maximum=0,
        modes=frozenset({0o600}),
    )
    if raw:
        _raise("maintenance_install_lock_invalid")
    parent = descriptor = -1
    try:
        parent, name = root._parent(PRIVILEGED_PROC_INSTALL_LOCK_PATH)  # noqa: SLF001
        descriptor = os.open(
            name,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != root.uid
            or opened.st_gid != root.gid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != 0
        ):
            _raise("maintenance_install_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except (OSError, MaintenanceInstallError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, MaintenanceInstallError):
            raise
        raise MaintenanceInstallError("maintenance_install_lock_invalid") from exc
    finally:
        if parent >= 0:
            os.close(parent)


def _load_journal_file(
    root: RootFS,
    path: str,
    *,
    links: frozenset[int] = frozenset({1}),
) -> tuple[dict[str, Any], bytes]:
    raw, _status, _digest = root._read(  # noqa: SLF001
        path,
        maximum=MAX_JOURNAL_BYTES,
        modes=frozenset({0o400}),
        links=links,
    )
    if raw is None:
        _raise("maintenance_install_journal_invalid")
    value = _canonical_object(raw, code="maintenance_install_journal_invalid")
    return _validate_journal(value), raw


def _recover_journal_stage(
    root: RootFS,
    *,
    expected_transaction: str | None = None,
) -> None:
    stage_status = root.status(JOURNAL_STAGE_PATH)
    if stage_status is None:
        return
    current_status = root.status(JOURNAL_PATH)
    if current_status is not None and (
        current_status.st_dev,
        current_status.st_ino,
    ) == (stage_status.st_dev, stage_status.st_ino):
        # Initial publication is a durable hard-link followed by an unlink of
        # the fixed stage.  Power loss between those operations legitimately
        # leaves both exact names on the same two-link inode.
        staged, staged_raw = _load_journal_file(
            root,
            JOURNAL_STAGE_PATH,
            links=frozenset({2}),
        )
        current, current_raw = _load_journal_file(
            root,
            JOURNAL_PATH,
            links=frozenset({2}),
        )
        if (
            staged != current
            or staged_raw != current_raw
            or staged["phase"] != "install_prepared"
            or staged["generation"] != 0
            or staged["previous_journal_sha256"] != ""
            or staged["retired_transaction_ids"]
        ):
            _raise("maintenance_install_journal_invalid")
        if expected_transaction is not None and staged["transaction_id"] != expected_transaction:
            _raise("maintenance_install_transaction_conflict")
        root.publish_link(
            JOURNAL_STAGE_PATH,
            JOURNAL_PATH,
            expected_sha256=hashlib.sha256(staged_raw).hexdigest(),
            mode=0o400,
            maximum=MAX_JOURNAL_BYTES,
        )
        recovered, recovered_raw = _load_journal_file(root, JOURNAL_PATH)
        if recovered != current or recovered_raw != current_raw:
            _raise("maintenance_install_journal_invalid")
        return
    try:
        staged, staged_raw = _load_journal_file(root, JOURNAL_STAGE_PATH)
    except MaintenanceInstallError:
        # The fixed stage is transaction-private infrastructure.  A torn first
        # write or torn rollover is not authority and may be discarded only
        # when its root-owned structure is exact; the current journal, if any,
        # remains the sole recovery point.
        root.unlink_structural(
            JOURNAL_STAGE_PATH,
            modes=frozenset({0o400}),
        )
        return
    if expected_transaction is not None and staged["transaction_id"] != expected_transaction:
        _raise("maintenance_install_transaction_conflict")
    if current_status is None:
        if (
            staged["phase"] != "install_prepared"
            or staged["generation"] != 0
            or staged["previous_journal_sha256"] != ""
            or staged["retired_transaction_ids"]
        ):
            _raise("maintenance_install_journal_invalid")
        root.publish_link(
            JOURNAL_STAGE_PATH,
            JOURNAL_PATH,
            expected_sha256=hashlib.sha256(staged_raw).hexdigest(),
            mode=0o400,
            maximum=MAX_JOURNAL_BYTES,
        )
        return
    current, _raw = _load_journal_file(root, JOURNAL_PATH)
    if not _valid_rollover(current, staged) and not _valid_transition(current, staged):
        _raise("maintenance_install_journal_invalid")
    root.replace(JOURNAL_STAGE_PATH, JOURNAL_PATH)
    recovered, recovered_raw = _load_journal_file(root, JOURNAL_PATH)
    if recovered != staged or recovered_raw != staged_raw:
        _raise("maintenance_install_journal_invalid")


def _fault(name: str, *, test_mode: bool) -> None:
    if test_mode and os.environ.get("FRIDAY_RETENTION_MAINTENANCE_FAIL_AFTER", "") == name:
        os._exit(86)


def _run_host_tool(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: int,
    lock_fd: int,
) -> subprocess.CompletedProcess[bytes]:
    """Run one builder while its exec lineage retains the host transaction lock."""

    if type(lock_fd) is not int or lock_fd < 0:
        _raise("maintenance_install_lock_invalid")
    try:
        fcntl.fcntl(lock_fd, fcntl.F_GETFD)
    except OSError as exc:
        raise MaintenanceInstallError("maintenance_install_lock_invalid") from exc
    return subprocess.run(  # noqa: S603
        list(arguments),
        check=False,
        env=dict(environment),
        pass_fds=(lock_fd,),
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        timeout=timeout,
    )


def _write_journal(
    root: RootFS,
    value: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | None,
    test_mode: bool,
) -> dict[str, Any]:
    normalized = _validate_journal(value)
    if root.status(JOURNAL_STAGE_PATH) is not None:
        _raise("maintenance_install_journal_invalid")
    if current is None:
        if root.status(JOURNAL_PATH) is not None:
            _raise("maintenance_install_journal_invalid")
    else:
        observed, _raw = _load_journal_file(root, JOURNAL_PATH)
        if observed != dict(current):
            _raise("maintenance_install_journal_invalid")
        if current["phase"] == "removed" and normalized["phase"] == "install_prepared":
            if not _valid_rollover(current, normalized):
                _raise("maintenance_install_transition_invalid")
        elif not _valid_transition(current, normalized):
            _raise("maintenance_install_transition_invalid")
    raw = _journal_bytes(normalized)
    if len(raw) > MAX_JOURNAL_BYTES:
        _raise("maintenance_install_journal_invalid")
    root.write_new(JOURNAL_STAGE_PATH, raw, mode=0o400)
    _fault(f"journal_stage:{normalized['phase']}", test_mode=test_mode)
    root.replace(JOURNAL_STAGE_PATH, JOURNAL_PATH)
    observed, observed_raw = _load_journal_file(root, JOURNAL_PATH)
    if observed != normalized or observed_raw != raw:
        _raise("maintenance_install_journal_invalid")
    _fault(str(normalized["phase"]), test_mode=test_mode)
    return observed


def _transition(
    root: RootFS,
    current: Mapping[str, Any],
    phase: str,
    *,
    test_mode: bool,
    launcher_sha256: str | None = None,
    image_authority_sha256: str | None = None,
    maintenance_initrd_sha256: str | None = None,
) -> dict[str, Any]:
    return _write_journal(
        root,
        _successor(
            current,
            phase,
            launcher_sha256=launcher_sha256,
            image_authority_sha256=image_authority_sha256,
            maintenance_initrd_sha256=maintenance_initrd_sha256,
        ),
        current=current,
        test_mode=test_mode,
    )


def _ensure_stage(root: RootFS, path: str, raw: bytes, *, mode: int) -> None:
    expected = hashlib.sha256(raw).hexdigest()
    status = root.status(path)
    if status is None:
        root.write_new(path, raw, mode=mode)
    else:
        try:
            root.read_exact(
                path,
                expected_sha256=expected,
                mode=mode,
                maximum=max(len(raw), 1),
            )
        except MaintenanceInstallError:
            # A journaled private stage may contain a torn write.  Never repair
            # aliases or foreign structures; exact regular ownership/mode/link
            # count is the deletion authority.
            root.unlink_structural(path, modes=frozenset({mode}))
            root.write_new(path, raw, mode=mode)


def _image_authority(inputs: InstallInputs, launcher_sha256: str) -> bytes:
    profile = dict(inputs.request["ordinary_profile"])
    value = {
        "controller_path": Layout(inputs.transaction_id).controller,
        "controller_sha256": inputs.controller_sha256,
        "hook_sha256": inputs.hook_sha256,
        "kernel_config_mode": format(
            _external_file(
                Path(str(profile["kernel_config_path"])),
                maximum=MAX_PROFILE_BYTES,
                code="maintenance_install_profile_invalid",
                expected_uid=os.geteuid(),
            ).mode,
            "o",
        ),
        "kernel_config_path": profile["kernel_config_path"],
        "kernel_config_sha256": profile["kernel_config_sha256"],
        "kernel_image_mode": format(
            _external_file(
                Path(str(profile["kernel_image_path"])),
                maximum=MAX_PROFILE_BYTES,
                code="maintenance_install_profile_invalid",
                expected_uid=os.geteuid(),
            ).mode,
            "o",
        ),
        "kernel_image_path": profile["kernel_image_path"],
        "kernel_image_sha256": profile["kernel_image_sha256"],
        "kernel_release": profile["kernel_release"],
        "kernel_version_sha256": profile["kernel_version_sha256"],
        "launcher_sha256": launcher_sha256,
        "maintenance_cmdline_sha256": inputs.maintenance_cmdline_sha256,
        "module_sha256": inputs.module_sha256,
        "ordinary_initrd_mode": format(
            _external_file(
                Path(str(profile["ordinary_initrd_path"])),
                maximum=MAX_PROFILE_BYTES,
                code="maintenance_install_profile_invalid",
                expected_uid=os.geteuid(),
            ).mode,
            "o",
        ),
        "ordinary_initrd_path": profile["ordinary_initrd_path"],
        "ordinary_initrd_sha256": profile["ordinary_initrd_sha256"],
        "ordinary_io_uring_disabled": inputs.ordinary_io_uring_disabled,
        "ordinary_root_device_id": inputs.ordinary_root_device_id,
        "ordinary_root_filesystem_uuid": inputs.ordinary_root_filesystem_uuid,
        "owner_uid": inputs.owner_uid,
        "request_file_sha256": inputs.request_file_sha256,
        "request_sha256": inputs.request_sha256,
        "runner_sha256": inputs.runner_sha256,
        "schema": IMAGE_AUTHORITY_SCHEMA,
        "toolchain_manifest_sha256": inputs.toolchain_manifest_sha256,
        "toolchain_root": inputs.toolchain_root,
        "transaction_id": inputs.transaction_id,
    }
    return _canonical(value) + b"\n"


def _compile_launcher(
    root: RootFS,
    inputs: InstallInputs,
    layout: Layout,
    *,
    lock_fd: int,
) -> str:
    for path in (
        layout.launcher_source_stage,
        layout.launcher_object_stage,
        layout.component_stage("launcher"),
    ):
        root.unlink_structural(
            path,
            modes=frozenset({0o400, 0o555, 0o600, 0o644, 0o700, 0o755}),
        )
    root.write_new(
        layout.launcher_source_stage,
        inputs.source_payloads["launcher"],
        mode=0o400,
    )
    source = root.host_path(layout.launcher_source_stage)
    object_stage = root.host_path(layout.launcher_object_stage)
    launcher_stage = root.host_path(layout.component_stage("launcher"))
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    try:
        assembled = _run_host_tool(
            ["/usr/bin/as", "--64", "-o", str(object_stage), str(source)],
            environment=environment,
            timeout=60,
            lock_fd=lock_fd,
        )
        if assembled.returncode != 0:
            _raise("maintenance_install_launcher_build_failed")
        root.chmod(layout.launcher_object_stage, 0o400)
        linked = _run_host_tool(
            [
                "/usr/bin/ld",
                "-s",
                "-nostdlib",
                "-static",
                "--build-id=none",
                "-z",
                "noexecstack",
                "-o",
                str(launcher_stage),
                str(object_stage),
            ],
            environment=environment,
            timeout=60,
            lock_fd=lock_fd,
        )
        if linked.returncode != 0:
            _raise("maintenance_install_launcher_build_failed")
        root.chmod(layout.component_stage("launcher"), 0o555)
        raw, _status, _digest = root._read(  # noqa: SLF001
            layout.component_stage("launcher"),
            maximum=MAX_SOURCE_BYTES,
            modes=frozenset({0o555}),
        )
        if raw is None:
            _raise("maintenance_install_launcher_build_failed")
    except (OSError, subprocess.SubprocessError) as exc:
        raise MaintenanceInstallError("maintenance_install_launcher_build_failed") from exc
    finally:
        root.unlink_structural(
            layout.launcher_source_stage,
            modes=frozenset({0o400}),
        )
        root.unlink_structural(
            layout.launcher_object_stage,
            modes=frozenset({0o400, 0o600, 0o644}),
        )
    return hashlib.sha256(raw).hexdigest()


def _component_records(
    inputs: InstallInputs,
    journal: Mapping[str, Any],
) -> tuple[tuple[str, str, str, bytes | None, int, int], ...]:
    layout = Layout(inputs.transaction_id)
    image = _image_authority(inputs, str(journal["launcher_sha256"]))
    if hashlib.sha256(image).hexdigest() != journal["image_authority_sha256"]:
        _raise("maintenance_install_artifact_invalid")
    return (
        (
            "launcher",
            layout.component_stage("launcher"),
            layout.launcher,
            None,
            0o555,
            MAX_SOURCE_BYTES,
        ),
        (
            "controller",
            layout.component_stage("controller"),
            layout.controller,
            inputs.source_payloads["controller"],
            0o555,
            MAX_SOURCE_BYTES,
        ),
        (
            "module",
            layout.component_stage("module"),
            layout.module,
            inputs.source_payloads["module"],
            0o555,
            MAX_SOURCE_BYTES,
        ),
        (
            "hook",
            layout.component_stage("hook"),
            layout.hook,
            inputs.source_payloads["hook"],
            0o555,
            MAX_SOURCE_BYTES,
        ),
        (
            "runner",
            layout.component_stage("runner"),
            layout.runner,
            inputs.source_payloads["runner"],
            0o555,
            MAX_SOURCE_BYTES,
        ),
        (
            "request",
            layout.component_stage("request"),
            layout.root_request,
            inputs.request_raw,
            0o444,
            MAX_REQUEST_BYTES,
        ),
        (
            "image_authority",
            layout.component_stage("image_authority"),
            layout.image_authority,
            image,
            0o400,
            MAX_REQUEST_BYTES,
        ),
    )


def _component_digest(role: str, journal: Mapping[str, Any]) -> str:
    names = {
        "launcher": "launcher_sha256",
        "controller": "controller_sha256",
        "module": "module_sha256",
        "hook": "hook_sha256",
        "runner": "runner_sha256",
        "request": "request_file_sha256",
        "image_authority": "image_authority_sha256",
    }
    return str(journal[names[role]])


def _prepare_payloads(
    root: RootFS,
    inputs: InstallInputs,
    journal: Mapping[str, Any],
    *,
    lock_fd: int,
) -> tuple[str, str]:
    layout = Layout(inputs.transaction_id)
    root.ensure_dir(layout.module_dir, mode=0o755)
    launcher_sha256 = _compile_launcher(root, inputs, layout, lock_fd=lock_fd)
    image = _image_authority(inputs, launcher_sha256)
    image_sha256 = hashlib.sha256(image).hexdigest()
    for role, raw, mode in (
        ("controller", inputs.source_payloads["controller"], 0o555),
        ("module", inputs.source_payloads["module"], 0o555),
        ("hook", inputs.source_payloads["hook"], 0o555),
        ("runner", inputs.source_payloads["runner"], 0o555),
        ("request", inputs.request_raw, 0o444),
        ("image_authority", image, 0o400),
    ):
        _ensure_stage(root, layout.component_stage(role), raw, mode=mode)
    if journal["phase"] != "install_prepared":
        _raise("maintenance_install_transition_invalid")
    return launcher_sha256, image_sha256


def _ensure_component_stages(
    root: RootFS,
    inputs: InstallInputs,
    journal: Mapping[str, Any],
    *,
    lock_fd: int,
) -> None:
    layout = Layout(inputs.transaction_id)
    for role, stage, target, raw, mode, maximum in _component_records(inputs, journal):
        expected = _component_digest(role, journal)
        target_status = root.status(target)
        stage_status = root.status(stage)
        if target_status is not None:
            root.read_exact(
                target,
                expected_sha256=expected,
                mode=mode,
                maximum=maximum,
                links=frozenset({1, 2}),
            )
            if stage_status is not None:
                root.read_exact(
                    stage,
                    expected_sha256=expected,
                    mode=mode,
                    maximum=maximum,
                    links=frozenset({1, 2}),
                )
            continue
        if stage_status is not None:
            root.read_exact(
                stage,
                expected_sha256=expected,
                mode=mode,
                maximum=maximum,
            )
            continue
        if role == "launcher":
            rebuilt = _compile_launcher(root, inputs, layout, lock_fd=lock_fd)
            if rebuilt != expected:
                _raise("maintenance_install_launcher_build_failed")
        else:
            if raw is None or hashlib.sha256(raw).hexdigest() != expected:
                _raise("maintenance_install_artifact_invalid")
            root.write_new(stage, raw, mode=mode)


def _publish_components(
    root: RootFS,
    inputs: InstallInputs,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
    lock_fd: int,
) -> None:
    _ensure_component_stages(root, inputs, journal, lock_fd=lock_fd)
    for role, stage, target, _raw, mode, maximum in _component_records(inputs, journal):
        root.publish_link(
            stage,
            target,
            expected_sha256=_component_digest(role, journal),
            mode=mode,
            maximum=maximum,
        )
        _fault(f"effect:publish:{role}", test_mode=test_mode)


def _config_payloads(
    journal: Mapping[str, Any],
) -> tuple[tuple[str, bytes], ...]:
    layout = Layout(str(journal["transaction_id"]))
    values = {
        "controller-path": layout.controller,
        "controller-sha256": journal["controller_sha256"],
        "image-authority-path": layout.image_authority,
        "image-authority-sha256": journal["image_authority_sha256"],
        "maintenance-cmdline-sha256": journal["maintenance_cmdline_sha256"],
        "ordinary-root-device-id": journal["ordinary_root_device_id"],
        "ordinary-root-filesystem-uuid": journal["ordinary_root_filesystem_uuid"],
        "owner-uid": str(journal["owner_uid"]),
        "owner-user": journal["owner"],
        "request-file-sha256": journal["request_file_sha256"],
        "request-path": journal["request_path"],
        "request-sha256": journal["request_sha256"],
        "root-request-path": layout.root_request,
        "transaction-id": journal["transaction_id"],
    }
    if set(values).union({"image-authority.v1.json"}) != set(_CONFIG_NAMES):
        _raise("maintenance_install_journal_invalid")
    for value in values.values():
        _safe_config(value, code="maintenance_install_journal_invalid")
    return tuple((name, f"{values[name]}\n".encode("ascii")) for name in sorted(values))


def _publish_config(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    root.ensure_dir(layout.config_dir, mode=0o700)
    payloads = (*_config_payloads(journal), ("image-authority.v1.json", None))
    for name, optional_raw in payloads:
        raw = (
            root.read_exact(
                layout.image_authority,
                expected_sha256=str(journal["image_authority_sha256"]),
                mode=0o400,
                maximum=MAX_REQUEST_BYTES,
            )
            if optional_raw is None
            else optional_raw
        )
        stage = layout.config_stage(name)
        target = f"{layout.config_dir}/{name}"
        expected = hashlib.sha256(raw).hexdigest()
        if root.status(target) is None:
            _ensure_stage(root, stage, raw, mode=0o400)
        elif root.status(stage) is not None:
            try:
                root.read_exact(
                    stage,
                    expected_sha256=expected,
                    mode=0o400,
                    maximum=MAX_REQUEST_BYTES,
                    links=frozenset({1, 2}),
                )
            except MaintenanceInstallError:
                root.unlink_structural(stage, modes=frozenset({0o400}))
        root.publish_link(
            stage,
            target,
            expected_sha256=expected,
            mode=0o400,
            maximum=MAX_REQUEST_BYTES,
        )
        _fault(f"effect:config:{name}", test_mode=test_mode)


def _build_initrd(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
    lock_fd: int,
) -> str:
    layout = Layout(str(journal["transaction_id"]))
    root.ensure_dir("/boot", mode=0o755)
    if root.status(layout.initrd) is not None:
        _raise("maintenance_install_artifact_invalid")
    root.unlink_structural(
        layout.initrd_stage,
        modes=frozenset({0o400, 0o600, 0o644}),
    )
    root.remove_private_tree(layout.dracut_tmp_dir, mode=0o700)
    root.ensure_dir(layout.dracut_tmp_dir, mode=0o700)
    try:
        if test_mode:
            raw = (
                _canonical(
                    {
                        "image_authority_sha256": journal["image_authority_sha256"],
                        "schema": TEST_INITRD_SCHEMA,
                        "transaction_id": journal["transaction_id"],
                    }
                )
                + b"\n"
            )
            root.write_new(layout.initrd_stage, raw, mode=0o600)
        else:
            environment = {
                "FRIDAY_RETENTION_MAINTENANCE_BUILD": "1",
                "FRIDAY_RETENTION_MAINTENANCE_CONFIG": str(root.host_path(layout.config_dir)),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            }
            try:
                result = _run_host_tool(
                    [
                        "/usr/bin/dracut",
                        "--force",
                        "--add",
                        "friday-retention-maintenance",
                        "--tmpdir",
                        str(root.host_path(layout.dracut_tmp_dir)),
                        str(root.host_path(layout.initrd_stage)),
                        os.uname().release,
                    ],
                    environment=environment,
                    timeout=900,
                    lock_fd=lock_fd,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise MaintenanceInstallError("maintenance_install_initrd_build_failed") from exc
            if result.returncode != 0:
                _raise("maintenance_install_initrd_build_failed")
            root.chmod(layout.initrd_stage, 0o600)
    finally:
        root.remove_private_tree(layout.dracut_tmp_dir, mode=0o700)
    _raw, status, digest = root._read(  # noqa: SLF001
        layout.initrd_stage,
        maximum=MAX_INITRD_BYTES,
        modes=frozenset({0o600}),
        retain=False,
    )
    if status.st_size <= 0:
        _raise("maintenance_install_initrd_build_failed")
    return digest


def _publish_initrd(root: RootFS, journal: Mapping[str, Any]) -> None:
    layout = Layout(str(journal["transaction_id"]))
    root.publish_link(
        layout.initrd_stage,
        layout.initrd,
        expected_sha256=str(journal["maintenance_initrd_sha256"]),
        mode=0o600,
        maximum=MAX_INITRD_BYTES,
        retain=False,
    )


def _maintenance_policy_payload(*, owner_uid: int, python: str) -> bytes:
    if type(owner_uid) is not int or owner_uid <= 0:
        _raise("maintenance_install_policy_invalid")
    resolved = _safe_absolute(python, code="maintenance_install_policy_invalid")
    _safe_config(resolved, code="maintenance_install_policy_invalid")
    return (
        f"#{owner_uid} ALL=(root) NOPASSWD: {resolved} -I -B -S "
        f"{PRIVILEGED_PROC_HELPER_PATH} maintenance-target-probe\n"
    ).encode("ascii")


def _validate_install_prerequisites(
    root: RootFS,
    inputs: InstallInputs,
    journal: Mapping[str, Any],
) -> None:
    python, python_sha256 = _resolved_root_owned_python()
    if python != inputs.maintenance_policy_python or python_sha256 != inputs.maintenance_policy_python_sha256:
        _raise("maintenance_install_policy_invalid")
    root.read_exact(
        PRIVILEGED_PROC_HELPER_PATH,
        expected_sha256=inputs.privileged_proc_helper_sha256,
        mode=0o755,
        maximum=MAX_SOURCE_BYTES,
    )
    for role, _stage, target, _raw, mode, maximum in _component_records(inputs, journal):
        root.read_exact(
            target,
            expected_sha256=_component_digest(role, journal),
            mode=mode,
            maximum=maximum,
        )
    layout = Layout(inputs.transaction_id)
    for name, raw in _config_payloads(journal):
        root.read_exact(
            f"{layout.config_dir}/{name}",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            mode=0o400,
            maximum=MAX_REQUEST_BYTES,
        )
    root.read_exact(
        f"{layout.config_dir}/image-authority.v1.json",
        expected_sha256=str(journal["image_authority_sha256"]),
        mode=0o400,
        maximum=MAX_REQUEST_BYTES,
    )
    root.read_exact(
        layout.initrd,
        expected_sha256=str(journal["maintenance_initrd_sha256"]),
        mode=0o600,
        maximum=MAX_INITRD_BYTES,
        retain=False,
    )
    private_paths = {
        layout.dracut_tmp_dir,
        layout.initrd_stage,
        layout.launcher_source_stage,
        layout.launcher_object_stage,
        *(
            layout.component_stage(role)
            for role in (
                "launcher",
                "controller",
                "module",
                "hook",
                "runner",
                "request",
                "image_authority",
            )
        ),
        *(layout.config_stage(name) for name in _CONFIG_NAMES),
    }
    if any(root.status(path) is not None for path in private_paths):
        _raise("maintenance_install_artifact_invalid")
    if set(root.list_dir(layout.config_dir)) != set(_CONFIG_NAMES):
        _raise("maintenance_install_artifact_invalid")
    if set(root.list_dir(layout.module_dir)) != {"module-setup.sh"}:
        _raise("maintenance_install_artifact_invalid")
    profile = inputs.request["ordinary_profile"]
    for path_name, digest_name in (
        ("kernel_config_path", "kernel_config_sha256"),
        ("kernel_image_path", "kernel_image_sha256"),
        ("ordinary_initrd_path", "ordinary_initrd_sha256"),
    ):
        evidence = _external_file(
            Path(str(profile[path_name])),
            maximum=MAX_PROFILE_BYTES,
            code="maintenance_install_profile_invalid",
            expected_uid=os.geteuid(),
        )
        if evidence.sha256 != profile[digest_name]:
            _raise("maintenance_install_profile_invalid")


def _publish_maintenance_policy(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
    lock_fd: int,
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    raw = _maintenance_policy_payload(
        owner_uid=int(journal["owner_uid"]),
        python=str(journal["maintenance_policy_python"]),
    )
    expected = str(journal["maintenance_policy_sha256"])
    if hashlib.sha256(raw).hexdigest() != expected:
        _raise("maintenance_install_journal_invalid")
    if root.status(layout.maintenance_policy_stage) is None:
        if root.status(layout.maintenance_policy) is None:
            root.write_new(layout.maintenance_policy_stage, raw, mode=0o440)
        else:
            root.read_exact(
                layout.maintenance_policy,
                expected_sha256=expected,
                mode=0o440,
                maximum=MAX_POLICY_BYTES,
            )
    if root.status(layout.maintenance_policy_stage) is not None:
        try:
            root.read_exact(
                layout.maintenance_policy_stage,
                expected_sha256=expected,
                mode=0o440,
                maximum=MAX_POLICY_BYTES,
                links=frozenset({1, 2}),
            )
        except MaintenanceInstallError:
            root.unlink_structural(
                layout.maintenance_policy_stage,
                modes=frozenset({0o440}),
            )
            root.write_new(layout.maintenance_policy_stage, raw, mode=0o440)
        result = _run_host_tool(
            [
                "/usr/sbin/visudo",
                "-cf",
                str(root.host_path(layout.maintenance_policy_stage)),
            ],
            environment={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            },
            timeout=5,
            lock_fd=lock_fd,
        )
        if result.returncode != 0:
            _raise("maintenance_install_policy_invalid")
    _fault("effect:policy:stage", test_mode=test_mode)
    root.publish_link(
        layout.maintenance_policy_stage,
        layout.maintenance_policy,
        expected_sha256=expected,
        mode=0o440,
        maximum=MAX_POLICY_BYTES,
    )
    _fault("effect:policy:publish", test_mode=test_mode)


def _validate_installed(
    root: RootFS,
    inputs: InstallInputs,
    journal: Mapping[str, Any],
) -> None:
    _validate_install_prerequisites(root, inputs, journal)
    layout = Layout(inputs.transaction_id)
    raw = root.read_exact(
        layout.maintenance_policy,
        expected_sha256=str(journal["maintenance_policy_sha256"]),
        mode=0o440,
        maximum=MAX_POLICY_BYTES,
    )
    if raw != _maintenance_policy_payload(
        owner_uid=inputs.owner_uid,
        python=inputs.maintenance_policy_python,
    ) or set(_maintenance_policy_residue_names(root)) != {Path(layout.maintenance_policy).name}:
        _raise("maintenance_install_policy_invalid")


def _known_residue_names(root: RootFS) -> tuple[str, ...]:
    # Both callers run after _bootstrap established this exact root-owned
    # directory.  Missing or substituted structure is therefore a fence, not
    # evidence that the residue set is empty.
    libexec = root.list_dir("/usr/libexec/friday")
    prefixes = (
        ".maintenance-",
        ".release_artifact_retention_maintenance",
        "release_artifact_retention_maintenance_image-",
        "release_artifact_retention_maintenance_install-",
        "release_artifact_retention_maintenance_request-",
        "retention-maintenance-image-config-",
    )
    exact = {
        Path(JOURNAL_PATH).name,
        Path(JOURNAL_STAGE_PATH).name,
        Path(LOCK_PATH).name,
        "release_artifact_retention_maintenance_hook.sh",
        "release_artifact_retention_maintenance.py",
        "release_artifact_retention_maintenance_launcher",
        "release_artifact_retention_maintenance_runner.sh",
    }
    return tuple(name for name in libexec if name in exact or name.startswith(prefixes))


def _maintenance_policy_residue_names(root: RootFS) -> tuple[str, ...]:
    target = Path(MAINTENANCE_POLICY_PATH).name
    return tuple(
        name
        for name in root.list_dir("/etc/sudoers.d")
        if name == target or name.startswith(".friday-retention-maintenance-probe.")
    )


def _all_artifact_paths(journal: Mapping[str, Any]) -> tuple[str, ...]:
    layout = Layout(str(journal["transaction_id"]))
    values = [
        layout.launcher,
        layout.controller,
        layout.module,
        layout.hook,
        layout.runner,
        layout.root_request,
        layout.image_authority,
        layout.dracut_tmp_dir,
        layout.initrd,
        layout.initrd_stage,
        layout.maintenance_policy,
        layout.maintenance_policy_stage,
        layout.launcher_source_stage,
        layout.launcher_object_stage,
    ]
    values.extend(
        layout.component_stage(role)
        for role in (
            "launcher",
            "controller",
            "module",
            "hook",
            "runner",
            "request",
            "image_authority",
        )
    )
    values.extend(f"{layout.config_dir}/{name}" for name in _CONFIG_NAMES)
    values.extend(layout.config_stage(name) for name in _CONFIG_NAMES)
    return tuple(values)


def _validate_removed(root: RootFS, journal: Mapping[str, Any]) -> None:
    if any(root.status(path) is not None for path in _all_artifact_paths(journal)):
        _raise("maintenance_install_removal_incomplete")
    layout = Layout(str(journal["transaction_id"]))
    if root.status(layout.config_dir) is not None or root.status(layout.module_dir) is not None:
        _raise("maintenance_install_removal_incomplete")
    allowed = {Path(JOURNAL_PATH).name, Path(LOCK_PATH).name}
    residue = set(_known_residue_names(root)).difference(allowed)
    if residue or _maintenance_policy_residue_names(root):
        _raise("maintenance_install_removal_incomplete")


def _fence_initial_state(root: RootFS) -> None:
    allowed = {Path(LOCK_PATH).name}
    if set(_known_residue_names(root)).difference(allowed):
        _raise("maintenance_install_transaction_conflict")
    if root.status("/usr/lib/dracut/modules.d/99friday-retention-maintenance") is not None:
        _raise("maintenance_install_transaction_conflict")
    if _maintenance_policy_residue_names(root):
        _raise("maintenance_install_transaction_conflict")
    try:
        boot_names = root.list_dir("/boot")
    except MaintenanceInstallError:
        boot_names = ()
    if any(
        name.startswith("friday-retention-maintenance-") or name.startswith(".friday-retention-maintenance-")
        for name in boot_names
    ):
        _raise("maintenance_install_transaction_conflict")


def _remove_maintenance_policy(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    expected = str(journal["maintenance_policy_sha256"])
    root.unlink_exact(
        layout.maintenance_policy,
        expected_sha256=expected,
        mode=0o440,
        maximum=MAX_POLICY_BYTES,
    )
    _fault("effect:remove:policy", test_mode=test_mode)
    stage_status = root.status(layout.maintenance_policy_stage)
    if stage_status is not None:
        try:
            root.unlink_exact(
                layout.maintenance_policy_stage,
                expected_sha256=expected,
                mode=0o440,
                maximum=MAX_POLICY_BYTES,
            )
        except MaintenanceInstallError:
            # The initial journal reserves this private, sudo-ignored stage
            # before its first create-only write.  A power loss can leave an
            # incomplete 0440 regular file without ever publishing authority.
            root.unlink_structural(
                layout.maintenance_policy_stage,
                modes=frozenset({0o440}),
            )


def _remove_initrd(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    expected = str(journal["maintenance_initrd_sha256"])
    if expected:
        root.unlink_exact(
            layout.initrd,
            expected_sha256=expected,
            mode=0o600,
            maximum=MAX_INITRD_BYTES,
            retain=False,
        )
        _fault("effect:remove:initrd", test_mode=test_mode)
        root.unlink_exact(
            layout.initrd_stage,
            expected_sha256=expected,
            mode=0o600,
            maximum=MAX_INITRD_BYTES,
            retain=False,
        )
    else:
        if root.status(layout.initrd) is not None:
            _raise("maintenance_install_artifact_invalid")
        root.unlink_structural(
            layout.initrd_stage,
            modes=frozenset({0o400, 0o600, 0o644}),
        )
    root.remove_private_tree(layout.dracut_tmp_dir, mode=0o700)


def _remove_config(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    if not journal["image_authority_sha256"]:
        if root.status(layout.config_dir) is not None:
            _raise("maintenance_install_artifact_invalid")
        return
    for name, raw in (*_config_payloads(journal), ("image-authority.v1.json", None)):
        expected = str(journal["image_authority_sha256"]) if raw is None else hashlib.sha256(raw).hexdigest()
        target = f"{layout.config_dir}/{name}"
        stage = layout.config_stage(name)
        root.unlink_exact(
            target,
            expected_sha256=expected,
            mode=0o400,
            maximum=MAX_REQUEST_BYTES,
        )
        _fault(f"effect:remove:config:{name}", test_mode=test_mode)
        try:
            root.unlink_exact(
                stage,
                expected_sha256=expected,
                mode=0o400,
                maximum=MAX_REQUEST_BYTES,
            )
        except MaintenanceInstallError:
            # The journal reserves this create-only name before write_new.  A
            # power loss can therefore leave a short 0400 regular file whose
            # final digest never existed; structural authority is intentionally
            # limited to the exact private name/mode/owner and one link.
            root.unlink_structural(stage, modes=frozenset({0o400}))
    root.remove_empty_dir(layout.config_dir, mode=0o700)


def _remove_components(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    targets = (
        ("launcher", layout.launcher, 0o555, MAX_SOURCE_BYTES),
        ("controller", layout.controller, 0o555, MAX_SOURCE_BYTES),
        ("module", layout.module, 0o555, MAX_SOURCE_BYTES),
        ("hook", layout.hook, 0o555, MAX_SOURCE_BYTES),
        ("runner", layout.runner, 0o555, MAX_SOURCE_BYTES),
        ("request", layout.root_request, 0o444, MAX_REQUEST_BYTES),
        ("image_authority", layout.image_authority, 0o400, MAX_REQUEST_BYTES),
    )
    if not journal["launcher_sha256"]:
        if any(root.status(path) is not None for _role, path, _mode, _maximum in targets):
            _raise("maintenance_install_artifact_invalid")
        return
    for role, target, mode, maximum in targets:
        root.unlink_exact(
            target,
            expected_sha256=_component_digest(role, journal),
            mode=mode,
            maximum=maximum,
        )
        _fault(f"effect:remove:component:{role}", test_mode=test_mode)


def _remove_private_stages(
    root: RootFS,
    journal: Mapping[str, Any],
) -> None:
    layout = Layout(str(journal["transaction_id"]))
    known = (
        ("launcher", 0o555, MAX_SOURCE_BYTES),
        ("controller", 0o555, MAX_SOURCE_BYTES),
        ("module", 0o555, MAX_SOURCE_BYTES),
        ("hook", 0o555, MAX_SOURCE_BYTES),
        ("runner", 0o555, MAX_SOURCE_BYTES),
        ("request", 0o444, MAX_REQUEST_BYTES),
        ("image_authority", 0o400, MAX_REQUEST_BYTES),
    )
    if journal["launcher_sha256"]:
        for role, mode, maximum in known:
            root.unlink_exact(
                layout.component_stage(role),
                expected_sha256=_component_digest(role, journal),
                mode=mode,
                maximum=maximum,
            )
    else:
        for role, mode, _maximum in known:
            root.unlink_structural(
                layout.component_stage(role),
                modes=(
                    frozenset({0o400, 0o555, 0o600, 0o644, 0o700, 0o755})
                    if role == "launcher"
                    else frozenset({mode})
                ),
            )
    root.unlink_structural(
        layout.launcher_source_stage,
        modes=frozenset({0o400}),
    )
    root.unlink_structural(
        layout.launcher_object_stage,
        modes=frozenset({0o400, 0o600, 0o644}),
    )
    if root.status(layout.module_dir) is not None:
        root.remove_empty_dir(layout.module_dir, mode=0o755)


def _remove_transaction(
    root: RootFS,
    journal: Mapping[str, Any],
    *,
    test_mode: bool,
) -> dict[str, Any]:
    current = _validate_journal(journal)
    if current["phase"] in INSTALL_PHASES:
        current = _transition(
            root,
            current,
            "remove_prepared",
            test_mode=test_mode,
        )
    while current["phase"] != "removed":
        phase = str(current["phase"])
        if phase == "remove_prepared":
            current = _transition(
                root,
                current,
                "policy_revoking",
                test_mode=test_mode,
            )
        elif phase == "policy_revoking":
            _remove_maintenance_policy(root, current, test_mode=test_mode)
            current = _transition(
                root,
                current,
                "initrd_removing",
                test_mode=test_mode,
            )
        elif phase == "initrd_removing":
            _remove_initrd(root, current, test_mode=test_mode)
            current = _transition(
                root,
                current,
                "config_removing",
                test_mode=test_mode,
            )
        elif phase == "config_removing":
            _remove_config(root, current, test_mode=test_mode)
            current = _transition(
                root,
                current,
                "components_removing",
                test_mode=test_mode,
            )
        elif phase == "components_removing":
            _remove_components(root, current, test_mode=test_mode)
            current = _transition(
                root,
                current,
                "private_stages_removing",
                test_mode=test_mode,
            )
        elif phase == "private_stages_removing":
            _remove_private_stages(root, current)
            current = _transition(
                root,
                current,
                "removed",
                test_mode=test_mode,
            )
        else:
            _raise("maintenance_install_transition_invalid")
    _validate_removed(root, current)
    return current


def install(args: argparse.Namespace) -> dict[str, Any]:
    root_path = Path(_safe_root(args.root))
    test_mode, uid, gid = _test_mode(root_path)
    inputs = _load_install_inputs(args, system_uid=uid, root_path=root_path)
    with RootFS(root_path, uid=uid, gid=gid) as root:
        _bootstrap(root)
        lock = _acquire_lock(root)
        proc_lock = -1
        try:
            # Lock ordering is fixed: the maintenance transaction lock is
            # always acquired before the existing ordinary proc-probe lock.
            proc_lock = _acquire_privileged_proc_lock(root)
            root.read_exact(
                PRIVILEGED_PROC_HELPER_PATH,
                expected_sha256=inputs.privileged_proc_helper_sha256,
                mode=0o755,
                maximum=MAX_SOURCE_BYTES,
            )
            _recover_journal_stage(root, expected_transaction=inputs.transaction_id)
            current_status = root.status(JOURNAL_PATH)
            if current_status is None:
                _fence_initial_state(root)
                current = _write_journal(
                    root,
                    _new_journal(inputs),
                    current=None,
                    test_mode=test_mode,
                )
            else:
                current, _raw = _load_journal_file(root, JOURNAL_PATH)
                if current["phase"] == "removed":
                    _validate_removed(root, current)
                    if (
                        current["transaction_id"] == inputs.transaction_id
                        or inputs.transaction_id in current["retired_transaction_ids"]
                    ):
                        _raise("maintenance_install_transaction_replayed")
                    current = _write_journal(
                        root,
                        _new_journal(inputs, predecessor=current),
                        current=current,
                        test_mode=test_mode,
                    )
                elif not _same_identity(current, inputs.journal_identity()):
                    _raise("maintenance_install_transaction_conflict")
                elif current["phase"] in REMOVE_PHASES:
                    _raise("maintenance_install_transaction_removing")

            layout = Layout(inputs.transaction_id)
            root.ensure_dir(layout.module_dir, mode=0o755)
            while current["phase"] != "installed_not_armed":
                phase = str(current["phase"])
                if phase == "install_prepared":
                    launcher, image = _prepare_payloads(
                        root,
                        inputs,
                        current,
                        lock_fd=lock,
                    )
                    current = _transition(
                        root,
                        current,
                        "payloads_staged",
                        launcher_sha256=launcher,
                        image_authority_sha256=image,
                        test_mode=test_mode,
                    )
                elif phase == "payloads_staged":
                    _ensure_component_stages(
                        root,
                        inputs,
                        current,
                        lock_fd=lock,
                    )
                    current = _transition(
                        root,
                        current,
                        "components_publishing",
                        test_mode=test_mode,
                    )
                elif phase == "components_publishing":
                    _publish_components(
                        root,
                        inputs,
                        current,
                        test_mode=test_mode,
                        lock_fd=lock,
                    )
                    current = _transition(
                        root,
                        current,
                        "components_published",
                        test_mode=test_mode,
                    )
                elif phase == "components_published":
                    current = _transition(
                        root,
                        current,
                        "config_publishing",
                        test_mode=test_mode,
                    )
                elif phase == "config_publishing":
                    _publish_config(root, current, test_mode=test_mode)
                    current = _transition(
                        root,
                        current,
                        "config_published",
                        test_mode=test_mode,
                    )
                elif phase == "config_published":
                    current = _transition(
                        root,
                        current,
                        "initrd_building",
                        test_mode=test_mode,
                    )
                elif phase == "initrd_building":
                    initrd = _build_initrd(
                        root,
                        current,
                        test_mode=test_mode,
                        lock_fd=lock,
                    )
                    current = _transition(
                        root,
                        current,
                        "initrd_staged",
                        maintenance_initrd_sha256=initrd,
                        test_mode=test_mode,
                    )
                elif phase == "initrd_staged":
                    current = _transition(
                        root,
                        current,
                        "initrd_publishing",
                        test_mode=test_mode,
                    )
                elif phase == "initrd_publishing":
                    _publish_initrd(root, current)
                    current = _transition(
                        root,
                        current,
                        "policy_publishing",
                        test_mode=test_mode,
                    )
                elif phase == "policy_publishing":
                    _validate_install_prerequisites(root, inputs, current)
                    _publish_maintenance_policy(
                        root,
                        current,
                        test_mode=test_mode,
                        lock_fd=lock,
                    )
                    current = _transition(
                        root,
                        current,
                        "policy_published",
                        test_mode=test_mode,
                    )
                elif phase == "policy_published":
                    _validate_installed(root, inputs, current)
                    current = _transition(
                        root,
                        current,
                        "installed_not_armed",
                        test_mode=test_mode,
                    )
                else:
                    _raise("maintenance_install_transition_invalid")
            _validate_installed(root, inputs, current)
            return current
        finally:
            if proc_lock >= 0:
                os.close(proc_lock)
            os.close(lock)


def uninstall(args: argparse.Namespace) -> dict[str, Any]:
    transaction = _hex64(
        args.transaction_id,
        code="maintenance_install_transaction_invalid",
    )
    expected_request = _hex64(
        args.expected_request_sha256,
        code="maintenance_install_transaction_invalid",
    )
    root_path = Path(_safe_root(args.root))
    test_mode, uid, gid = _test_mode(root_path)
    with RootFS(root_path, uid=uid, gid=gid) as root:
        _bootstrap(root)
        lock = _acquire_lock(root)
        proc_lock = -1
        try:
            # Use the same global order as install; ordinary tooling never
            # acquires the maintenance lock, so this order cannot cycle.
            proc_lock = _acquire_privileged_proc_lock(root)
            _recover_journal_stage(root, expected_transaction=transaction)
            if root.status(JOURNAL_PATH) is None:
                _raise("maintenance_install_journal_missing")
            current, _raw = _load_journal_file(root, JOURNAL_PATH)
            if current["transaction_id"] != transaction:
                _raise("maintenance_install_transaction_conflict")
            if current["request_sha256"] != expected_request:
                _raise("maintenance_install_transaction_conflict")
            return _remove_transaction(root, current, test_mode=test_mode)
        finally:
            if proc_lock >= 0:
                os.close(proc_lock)
            os.close(lock)


def _parser() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParser(
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser(
        "install",
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    install_parser.add_argument("--source-directory", required=True)
    install_parser.add_argument("--request", required=True)
    install_parser.add_argument("--expected-request-sha256", required=True)
    install_parser.add_argument("--owner-user", required=True)
    install_parser.add_argument("--expected-launcher-source-sha256", required=True)
    install_parser.add_argument("--expected-module-sha256", required=True)
    install_parser.add_argument("--expected-hook-sha256", required=True)
    install_parser.add_argument("--expected-runner-sha256", required=True)
    install_parser.add_argument("--expected-proc-probe-sha256", required=True)
    install_parser.add_argument("--root", default="/")
    remove_parser = commands.add_parser(
        "remove",
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    remove_parser.add_argument("--transaction-id", required=True)
    remove_parser.add_argument("--expected-request-sha256", required=True)
    remove_parser.add_argument("--root", default="/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    values = tuple(sys.argv[1:] if argv is None else argv)
    operation = values[0] if values else ""
    try:
        option_tokens = values[1::2] if len(values) % 2 == 1 else ()
        if len(values) < 3 or len(values) % 2 != 1 or len(option_tokens) != len(set(option_tokens)):
            _raise("maintenance_install_arguments_invalid")
        args = _parser().parse_args(values)
        result = install(args) if args.command == "install" else uninstall(args)
    except (
        argparse.ArgumentError,
        MaintenanceInstallError,
        OSError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        message = (
            "friday_retention_maintenance_uninstall_failed"
            if operation == "remove"
            else "friday_retention_maintenance_install_failed"
        )
        sys.stderr.write(f"{message}\n")
        return 2
    if args.command == "install":
        sys.stdout.write(
            "friday_retention_maintenance_installed_not_armed "
            f"transaction_id={result['transaction_id']} "
            f"initrd_sha256={result['maintenance_initrd_sha256']}\n"
        )
    else:
        sys.stdout.write(
            f"friday_retention_maintenance_uninstalled transaction_id={result['transaction_id']}\n"
        )
    return 0


__all__ = [
    "ALL_PHASES",
    "INSTALL_PHASES",
    "JOURNAL_PATH",
    "JOURNAL_SCHEMA",
    "MaintenanceInstallError",
    "REMOVE_PHASES",
    "install",
    "main",
    "uninstall",
]


if __name__ == "__main__":
    raise SystemExit(main())
