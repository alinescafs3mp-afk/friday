#!/usr/bin/env python3
"""Fail-closed, target-scoped Linux proc reference observation.

This module is deliberately read-only.  It does not publish receipts, rename
artifacts, or delete anything.  A ``clear`` result means only that two bounded,
identical observations found no reference to the supplied inode set in the
namespace-visible Linux proc surfaces covered here.  It is not a universal
kernel open-object proof.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TARGET_INDEX_SCHEMA = "friday.release-artifact-proc-target-index.v1"
PROBE_RECEIPT_SCHEMA = "friday.release-artifact-proc-reference-receipt.v1"
PROBE_SCOPE = "namespace_visible_proc_references"

MAX_TARGETS = 4_096
MAX_TARGET_OBJECTS = 1_000_000
MAX_PIDS = 131_072
MAX_REFERENCES_PER_PROCESS = 262_144
MAX_MATCHES = 256
MAX_LINK_TARGET_BYTES = 4_096
MAX_PROC_FILE_BYTES = 32 << 20
MAX_RECEIPT_BYTES = 2 << 20

_TARGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_BOOT_ID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?\Z")
_MAP_LINE = re.compile(
    rb"([0-9a-f]+)-([0-9a-f]+) "
    rb"([r-][w-][x-][ps]) ([0-9a-f]+) ([0-9a-f]+):([0-9a-f]+) ([0-9]+)(?: +(.*))?\Z"
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_CORE_KEYS = frozenset(
    {
        "ambiguities",
        "complete",
        "fixed_point_passes",
        "matches",
        "observation_sha256",
        "pid_epoch_set_sha256",
        "process_count",
        "reference_count",
        "schema",
        "scope",
        "scope_identity",
        "status",
        "target_count",
        "target_index_sha256",
        "target_object_count",
        "universal_absence_proof",
    }
)
_MATCH_KEYS = frozenset(
    {
        "entry",
        "link_target_base64",
        "link_target_sha256",
        "mount_id",
        "object",
        "pid",
        "pid_epoch_sha256",
        "source",
        "target_ids",
    }
)
_ALLOWED_FILE_TYPES = frozenset(
    {
        stat.S_IFREG,
        stat.S_IFDIR,
        stat.S_IFLNK,
        stat.S_IFIFO,
        stat.S_IFSOCK,
        stat.S_IFBLK,
        stat.S_IFCHR,
    }
)


class ProcProbeInputError(ValueError):
    """The caller supplied a non-canonical or unbounded target/probe input."""


@dataclass(frozen=True, order=True)
class ObjectKey:
    """One underlying filesystem object, independent of its lexical aliases."""

    device: int
    inode: int
    file_type: int

    def __post_init__(self) -> None:
        if (
            type(self.device) is not int
            or type(self.inode) is not int
            or type(self.file_type) is not int
            or self.device < 0
            or self.inode <= 0
            or self.file_type not in _ALLOWED_FILE_TYPES
        ):
            raise ProcProbeInputError("target_object_invalid")

    @classmethod
    def from_stat(cls, value: os.stat_result) -> ObjectKey:
        return cls(int(value.st_dev), int(value.st_ino), stat.S_IFMT(value.st_mode))

    def projection(self) -> list[int]:
        return [self.device, self.inode, self.file_type]


@dataclass(frozen=True)
class ProbeTarget:
    """Caller-owned exact target identity used only for reference comparison."""

    target_id: str
    roots: tuple[Path, ...]
    objects: tuple[ObjectKey, ...]


@dataclass(frozen=True)
class TargetIndex:
    """Canonical target set and its exact content digest."""

    targets: tuple[ProbeTarget, ...]
    sha256: str
    object_count: int


@dataclass(frozen=True, order=True)
class _Reference:
    source: str
    entry: str
    object_key: ObjectKey
    mount_id: int | None
    link_target: bytes

    def fingerprint_projection(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "link_target_sha256": hashlib.sha256(self.link_target).hexdigest(),
            "mount_id": self.mount_id,
            "object": self.object_key.projection(),
            "source": self.source,
        }


@dataclass(frozen=True, order=True)
class _Match:
    target_ids: tuple[str, ...]
    pid: int
    pid_epoch_sha256: str
    reference: _Reference

    def receipt_projection(self) -> dict[str, Any]:
        encoded = base64.b64encode(self.reference.link_target).decode("ascii")
        return {
            "entry": self.reference.entry,
            "link_target_base64": encoded,
            "link_target_sha256": hashlib.sha256(self.reference.link_target).hexdigest(),
            "mount_id": self.reference.mount_id,
            "object": self.reference.object_key.projection(),
            "pid": self.pid,
            "pid_epoch_sha256": self.pid_epoch_sha256,
            "source": self.reference.source,
            "target_ids": list(self.target_ids),
        }


@dataclass(frozen=True, order=True)
class _ProcessObservation:
    pid: int
    epoch_sha256: str
    reference_count: int
    reference_sha256: str
    matches: tuple[_Match, ...]

    def projection(self) -> dict[str, Any]:
        return {
            "epoch_sha256": self.epoch_sha256,
            "matches": [match.receipt_projection() for match in self.matches],
            "pid": self.pid,
            "reference_count": self.reference_count,
            "reference_sha256": self.reference_sha256,
        }


@dataclass(frozen=True)
class _ScopeIdentity:
    boot_id_sha256: str
    proc_root: tuple[int, int]
    pid_namespace: tuple[int, int]
    mount_namespace: tuple[int, int]

    def projection(self) -> dict[str, Any]:
        return {
            "boot_id_sha256": self.boot_id_sha256,
            "mount_namespace": list(self.mount_namespace),
            "pid_namespace": list(self.pid_namespace),
            "proc_root": list(self.proc_root),
        }


@dataclass(frozen=True)
class _GlobalObservation:
    scope: _ScopeIdentity
    processes: tuple[_ProcessObservation, ...]

    @property
    def process_count(self) -> int:
        return len(self.processes)

    @property
    def reference_count(self) -> int:
        return sum(process.reference_count for process in self.processes)

    @property
    def matches(self) -> tuple[_Match, ...]:
        return tuple(sorted(match for process in self.processes for match in process.matches))

    @property
    def pid_epoch_set_sha256(self) -> str:
        value = [[process.pid, process.epoch_sha256] for process in self.processes]
        return hashlib.sha256(_canonical_json(value)).hexdigest()

    @property
    def observation_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "processes": [process.projection() for process in self.processes],
                    "scope": self.scope.projection(),
                }
            )
        ).hexdigest()


class _ProbeIssue(RuntimeError):
    def __init__(self, code: str, *, pid: int = 0, source: str = "proc") -> None:
        super().__init__(code)
        self.code = code
        self.pid = pid
        self.source = source


CapturePass = Callable[[TargetIndex], _GlobalObservation]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _absolute_lexical(path: Path) -> Path:
    if not path.is_absolute() or any(character in str(path) for character in "\x00\r\n"):
        raise ProcProbeInputError("target_root_invalid")
    lexical = Path(os.path.abspath(path))
    if lexical != path:
        raise ProcProbeInputError("target_root_invalid")
    return lexical


def build_target_index(targets: Sequence[ProbeTarget]) -> TargetIndex:
    """Normalize targets and bind every object/root to one canonical digest."""

    if not targets or len(targets) > MAX_TARGETS:
        raise ProcProbeInputError("target_count_invalid")
    normalized: list[ProbeTarget] = []
    identifiers: set[str] = set()
    total = 0
    for target in targets:
        if not isinstance(target, ProbeTarget) or _TARGET_ID.fullmatch(target.target_id) is None:
            raise ProcProbeInputError("target_id_invalid")
        if target.target_id in identifiers:
            raise ProcProbeInputError("target_id_duplicate")
        identifiers.add(target.target_id)
        if any(not isinstance(root, Path) for root in target.roots) or any(
            not isinstance(object_key, ObjectKey) for object_key in target.objects
        ):
            raise ProcProbeInputError("target_identity_invalid")
        roots = tuple(sorted({_absolute_lexical(root) for root in target.roots}, key=str))
        objects = tuple(sorted(set(target.objects)))
        if not roots or not objects:
            raise ProcProbeInputError("target_identity_empty")
        total += len(objects)
        if total > MAX_TARGET_OBJECTS:
            raise ProcProbeInputError("target_object_limit_exceeded")
        normalized.append(ProbeTarget(target.target_id, roots, objects))
    normalized.sort(key=lambda item: item.target_id)
    projection = {
        "schema": TARGET_INDEX_SCHEMA,
        "targets": [
            {
                "objects": [value.projection() for value in target.objects],
                "roots": [str(root) for root in target.roots],
                "target_id": target.target_id,
            }
            for target in normalized
        ],
    }
    return TargetIndex(
        targets=tuple(normalized),
        sha256=hashlib.sha256(_canonical_json(projection)).hexdigest(),
        object_count=total,
    )


def _target_lookup(index: TargetIndex) -> dict[ObjectKey, tuple[str, ...]]:
    lookup: dict[ObjectKey, list[str]] = {}
    for target in index.targets:
        for object_key in target.objects:
            lookup.setdefault(object_key, []).append(target.target_id)
    return {key: tuple(sorted(values)) for key, values in lookup.items()}


def _pid_epoch_sha256(
    boot_id_sha256: str,
    pid: int,
    starttime: int,
    proc_identity: tuple[int, int],
) -> str:
    if _HEX64.fullmatch(boot_id_sha256) is None:
        raise _ProbeIssue("proc_boot_id_invalid", pid=pid, source="boot_id")
    payload = b"friday-proc-epoch-v1\0" + boot_id_sha256.encode("ascii")
    payload += b"\0" + str(pid).encode() + b"\0" + str(starttime).encode()
    payload += b"\0" + str(proc_identity[0]).encode() + b":" + str(proc_identity[1]).encode()
    return hashlib.sha256(payload).hexdigest()


def _issue_from_oserror(error: OSError, *, pid: int, source: str) -> _ProbeIssue:
    if error.errno in {errno.EPERM, errno.EACCES}:
        return _ProbeIssue("proc_permission_denied", pid=pid, source=source)
    if error.errno in {errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}:
        return _ProbeIssue("proc_surface_unsupported", pid=pid, source=source)
    if error.errno in {errno.ENOENT, errno.ESRCH, errno.EBADF, errno.ESTALE}:
        return _ProbeIssue("proc_observation_raced", pid=pid, source=source)
    return _ProbeIssue("proc_observation_failed", pid=pid, source=source)


def _read_bounded_at(directory_fd: int, name: str, *, maximum: int, pid: int, source: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise _ProbeIssue("proc_body_limit_exceeded", pid=pid, source=source)
        return payload
    except _ProbeIssue:
        raise
    except OSError as exc:
        raise _issue_from_oserror(exc, pid=pid, source=source) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_starttime(raw: bytes, *, pid: int) -> tuple[str, int]:
    if not raw or b"\x00" in raw or not raw.endswith(b"\n"):
        raise _ProbeIssue("proc_stat_invalid", pid=pid, source="stat")
    closing = raw.rfind(b") ")
    fields = raw[closing + 2 :].strip().split() if closing > 0 else []
    if len(fields) < 20 or len(fields[0]) != 1 or not fields[19].isdigit():
        raise _ProbeIssue("proc_stat_invalid", pid=pid, source="stat")
    try:
        starttime = int(fields[19])
        state = fields[0].decode("ascii", errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise _ProbeIssue("proc_stat_invalid", pid=pid, source="stat") from exc
    if starttime <= 0:
        raise _ProbeIssue("proc_stat_invalid", pid=pid, source="stat")
    return state, starttime


def _parse_fdinfo(raw: bytes, *, pid: int) -> int:
    values: list[int] = []
    for line in raw.splitlines():
        if line.startswith(b"mnt_id:"):
            value = line.removeprefix(b"mnt_id:").strip()
            if not value.isdigit():
                raise _ProbeIssue("proc_fdinfo_invalid", pid=pid, source="fdinfo")
            values.append(int(value))
    if len(values) != 1 or values[0] <= 0:
        raise _ProbeIssue("proc_fdinfo_invalid", pid=pid, source="fdinfo")
    return values[0]


@dataclass(frozen=True, order=True)
class _MapRecord:
    start: int
    end: int
    device: int
    inode: int

    @property
    def entry(self) -> str:
        return f"{self.start:x}-{self.end:x}"


def _parse_maps(raw: bytes, *, pid: int) -> tuple[_MapRecord, ...]:
    records: list[_MapRecord] = []
    ranges: set[tuple[int, int]] = set()
    for line in raw.splitlines():
        match = _MAP_LINE.fullmatch(line)
        if match is None:
            raise _ProbeIssue("proc_maps_invalid", pid=pid, source="maps")
        try:
            start = int(match[1], 16)
            end = int(match[2], 16)
            major = int(match[5], 16)
            minor = int(match[6], 16)
            inode = int(match[7])
        except (ValueError, OverflowError) as exc:
            raise _ProbeIssue("proc_maps_invalid", pid=pid, source="maps") from exc
        if start >= end or (start, end) in ranges:
            raise _ProbeIssue("proc_maps_invalid", pid=pid, source="maps")
        ranges.add((start, end))
        if inode > 0:
            try:
                device = os.makedev(major, minor)
            except (ValueError, OverflowError) as exc:
                raise _ProbeIssue("proc_maps_invalid", pid=pid, source="maps") from exc
            records.append(_MapRecord(start, end, device, inode))
            if len(records) > MAX_REFERENCES_PER_PROCESS:
                raise _ProbeIssue("proc_reference_limit_exceeded", pid=pid, source="maps")
    return tuple(records)


def _parse_map_entry(name: str, *, pid: int) -> tuple[int, int]:
    fields = name.split("-", 1)
    try:
        start, end = int(fields[0], 16), int(fields[1], 16)
    except (IndexError, ValueError) as exc:
        raise _ProbeIssue("proc_map_files_invalid", pid=pid, source="map_files") from exc
    if start >= end or name != f"{start:x}-{end:x}":
        raise _ProbeIssue("proc_map_files_invalid", pid=pid, source="map_files")
    return start, end


class _LinuxProcScanner:
    def __init__(self, proc_root: Path, target_index: TargetIndex) -> None:
        self.proc_root = proc_root
        self.target_index = target_index
        self.lookup = _target_lookup(target_index)
        self.proc_fd = -1
        self._owned_fds: set[int] = set()

    def __enter__(self) -> _LinuxProcScanner:
        if sys.platform != "linux" or self.proc_root != Path("/proc"):
            raise _ProbeIssue("proc_surface_unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.proc_fd = os.open(self.proc_root, flags)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=0, source="proc") from exc
        self._owned_fds.add(self.proc_fd)
        try:
            self._verify_proc_mount()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        descriptor, self.proc_fd = self.proc_fd, -1
        if descriptor >= 0:
            self._owned_fds.discard(descriptor)
            os.close(descriptor)

    def _verify_proc_mount(self) -> None:
        raw = _read_bounded_at(
            self.proc_fd,
            "self/mountinfo",
            maximum=1 << 20,
            pid=os.getpid(),
            source="mountinfo",
        )
        device = os.fstat(self.proc_fd).st_dev
        expected = f"{os.major(device)}:{os.minor(device)}".encode()
        matches = []
        for line in raw.splitlines():
            left, separator, right = line.partition(b" - ")
            fields, tail = left.split(), right.split()
            if (
                separator
                and len(fields) >= 6
                and len(tail) >= 1
                and fields[2] == expected
                and fields[4] == b"/proc"
                and tail[0] == b"proc"
            ):
                matches.append(line)
        if len(matches) != 1:
            raise _ProbeIssue("proc_surface_unsupported", source="mountinfo")

    def _scope_identity(self) -> _ScopeIdentity:
        try:
            root = os.fstat(self.proc_fd)
            pid_ns = os.stat("self/ns/pid", dir_fd=self.proc_fd, follow_symlinks=True)
            mount_ns = os.stat("self/ns/mnt", dir_fd=self.proc_fd, follow_symlinks=True)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=os.getpid(), source="namespace") from exc
        boot = _read_bounded_at(
            self.proc_fd,
            "sys/kernel/random/boot_id",
            maximum=37,
            pid=0,
            source="boot_id",
        )
        if _BOOT_ID.fullmatch(boot) is None:
            raise _ProbeIssue("proc_boot_id_invalid", source="boot_id")
        return _ScopeIdentity(
            boot_id_sha256=hashlib.sha256(boot.strip()).hexdigest(),
            proc_root=(int(root.st_dev), int(root.st_ino)),
            pid_namespace=(int(pid_ns.st_dev), int(pid_ns.st_ino)),
            mount_namespace=(int(mount_ns.st_dev), int(mount_ns.st_ino)),
        )

    def _pid_names(self) -> tuple[int, ...]:
        try:
            names = os.listdir(self.proc_fd)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=0, source="proc") from exc
        numeric = [name for name in names if name.isdecimal()]
        if any(str(int(name)) != name or int(name) <= 0 for name in numeric):
            raise _ProbeIssue("proc_pid_inventory_invalid")
        values = sorted(int(name) for name in numeric)
        if len(values) > MAX_PIDS or len(values) != len(set(values)):
            raise _ProbeIssue("proc_pid_inventory_invalid")
        return tuple(values)

    def _open_pid(self, pid: int) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(pid), flags, dir_fd=self.proc_fd)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source="pid") from exc
        self._owned_fds.add(descriptor)
        return descriptor

    def _close_owned(self, descriptor: int) -> None:
        self._owned_fds.discard(descriptor)
        os.close(descriptor)

    def _epoch(self, pid: int, boot_id_sha256: str) -> tuple[str, int, tuple[int, int]]:
        descriptor = self._open_pid(pid)
        try:
            status = os.fstat(descriptor)
            _state, starttime = _parse_starttime(
                _read_bounded_at(descriptor, "stat", maximum=16 << 10, pid=pid, source="stat"),
                pid=pid,
            )
        finally:
            self._close_owned(descriptor)
        identity = (int(status.st_dev), int(status.st_ino))
        return _pid_epoch_sha256(boot_id_sha256, pid, starttime, identity), starttime, identity

    def _enumerate_epochs(self, boot_id_sha256: str) -> dict[int, tuple[str, int, tuple[int, int]]]:
        result: dict[int, tuple[str, int, tuple[int, int]]] = {}
        for pid in self._pid_names():
            result[pid] = self._epoch(pid, boot_id_sha256)
        return result

    @staticmethod
    def _link_bytes(directory_fd: int, name: str, *, pid: int, source: str) -> bytes:
        try:
            raw = os.fsencode(os.readlink(name, dir_fd=directory_fd))
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source=source) from exc
        if len(raw) > MAX_LINK_TARGET_BYTES or b"\x00" in raw:
            raise _ProbeIssue("proc_link_target_invalid", pid=pid, source=source)
        return raw

    @staticmethod
    def _stat_link(directory_fd: int, name: str, *, pid: int, source: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source=source) from exc

    def _reference(
        self, directory_fd: int, name: str, *, pid: int, source: str, mount_id: int | None
    ) -> _Reference:
        before = self._stat_link(directory_fd, name, pid=pid, source=source)
        raw = self._link_bytes(directory_fd, name, pid=pid, source=source)
        after = self._stat_link(directory_fd, name, pid=pid, source=source)
        if ObjectKey.from_stat(before) != ObjectKey.from_stat(after):
            raise _ProbeIssue("proc_observation_raced", pid=pid, source=source)
        return _Reference(source, name, ObjectKey.from_stat(before), mount_id, raw)

    def _open_directory_at(self, directory_fd: int, name: str, *, pid: int, source: str) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source=source) from exc
        self._owned_fds.add(descriptor)
        return descriptor

    def _fd_references(self, pid_fd: int, pid: int) -> list[_Reference]:
        fd_directory = self._open_directory_at(pid_fd, "fd", pid=pid, source="fd")
        try:
            try:
                names_before = os.listdir(fd_directory)
            except OSError as exc:
                raise _issue_from_oserror(exc, pid=pid, source="fd") from exc
            if any(not name.isdecimal() or str(int(name)) != name for name in names_before):
                raise _ProbeIssue("proc_fd_inventory_invalid", pid=pid, source="fd")
            own_pid = pid == os.getpid()
            names = sorted(
                name
                for name in names_before
                if name.isdecimal() and (not own_pid or int(name) not in self._owned_fds)
            )
            if len(names) != len(set(names)) or len(names) > MAX_REFERENCES_PER_PROCESS:
                raise _ProbeIssue("proc_reference_limit_exceeded", pid=pid, source="fd")
            references: list[_Reference] = []
            for name in names:
                info = _read_bounded_at(
                    pid_fd,
                    f"fdinfo/{name}",
                    maximum=64 << 10,
                    pid=pid,
                    source="fdinfo",
                )
                references.append(
                    self._reference(
                        fd_directory,
                        name,
                        pid=pid,
                        source="fd",
                        mount_id=_parse_fdinfo(info, pid=pid),
                    )
                )
            try:
                names_after = os.listdir(fd_directory)
            except OSError as exc:
                raise _issue_from_oserror(exc, pid=pid, source="fd") from exc
            if any(not name.isdecimal() or str(int(name)) != name for name in names_after):
                raise _ProbeIssue("proc_fd_inventory_invalid", pid=pid, source="fd")
            filtered_after = sorted(
                name
                for name in names_after
                if name.isdecimal() and (not own_pid or int(name) not in self._owned_fds)
            )
            if names != filtered_after:
                raise _ProbeIssue("proc_observation_raced", pid=pid, source="fd")
            return references
        finally:
            self._close_owned(fd_directory)

    def _special_reference(self, pid_fd: int, pid: int, name: str) -> _Reference | None:
        try:
            before = os.stat(name, dir_fd=pid_fd, follow_symlinks=True)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source=name) from exc
        raw = self._link_bytes(pid_fd, name, pid=pid, source=name)
        after = self._stat_link(pid_fd, name, pid=pid, source=name)
        if ObjectKey.from_stat(before) != ObjectKey.from_stat(after):
            raise _ProbeIssue("proc_observation_raced", pid=pid, source=name)
        return _Reference(name, name, ObjectKey.from_stat(before), None, raw)

    def _map_references(self, pid_fd: int, pid: int) -> list[_Reference]:
        maps_before = _parse_maps(
            _read_bounded_at(pid_fd, "maps", maximum=MAX_PROC_FILE_BYTES, pid=pid, source="maps"),
            pid=pid,
        )
        map_directory = self._open_directory_at(pid_fd, "map_files", pid=pid, source="map_files")
        try:
            try:
                names_before = os.listdir(map_directory)
            except OSError as exc:
                raise _issue_from_oserror(exc, pid=pid, source="map_files") from exc
            if len(names_before) > MAX_REFERENCES_PER_PROCESS:
                raise _ProbeIssue("proc_reference_limit_exceeded", pid=pid, source="map_files")
            by_range = {(record.start, record.end): record for record in maps_before}
            named: dict[tuple[int, int], str] = {}
            for name in names_before:
                address_range = _parse_map_entry(name, pid=pid)
                if address_range in named:
                    raise _ProbeIssue("proc_map_files_invalid", pid=pid, source="map_files")
                named[address_range] = name
            if set(named) != set(by_range):
                raise _ProbeIssue("proc_map_files_incomplete", pid=pid, source="map_files")
            references: list[_Reference] = []
            for address_range, name in sorted(named.items()):
                reference = self._reference(
                    map_directory,
                    name,
                    pid=pid,
                    source="map_files",
                    mount_id=None,
                )
                expected = by_range[address_range]
                if (
                    reference.object_key.device != expected.device
                    or reference.object_key.inode != expected.inode
                ):
                    raise _ProbeIssue("proc_map_files_incomplete", pid=pid, source="map_files")
                references.append(reference)
            maps_after = _parse_maps(
                _read_bounded_at(
                    pid_fd,
                    "maps",
                    maximum=MAX_PROC_FILE_BYTES,
                    pid=pid,
                    source="maps",
                ),
                pid=pid,
            )
            try:
                names_after = os.listdir(map_directory)
            except OSError as exc:
                raise _issue_from_oserror(exc, pid=pid, source="map_files") from exc
            if maps_before != maps_after or sorted(names_before) != sorted(names_after):
                raise _ProbeIssue("proc_observation_raced", pid=pid, source="map_files")
            return references
        finally:
            self._close_owned(map_directory)

    def _scan_process(
        self,
        pid: int,
        expected: tuple[str, int, tuple[int, int]],
    ) -> _ProcessObservation:
        pid_fd = self._open_pid(pid)
        try:
            status = os.fstat(pid_fd)
            before_state, before_start = _parse_starttime(
                _read_bounded_at(pid_fd, "stat", maximum=16 << 10, pid=pid, source="stat"),
                pid=pid,
            )
            del before_state
            if before_start != expected[1] or (int(status.st_dev), int(status.st_ino)) != expected[2]:
                raise _ProbeIssue("proc_observation_raced", pid=pid, source="pid")
            references = self._fd_references(pid_fd, pid)
            absent: list[str] = []
            for name in ("cwd", "root", "exe"):
                reference = self._special_reference(pid_fd, pid, name)
                if reference is None:
                    absent.append(name)
                else:
                    references.append(reference)
            references.extend(self._map_references(pid_fd, pid))
            _after_state, after_start = _parse_starttime(
                _read_bounded_at(pid_fd, "stat", maximum=16 << 10, pid=pid, source="stat"),
                pid=pid,
            )
            if after_start != before_start or (int(status.st_dev), int(status.st_ino)) != expected[2]:
                raise _ProbeIssue("proc_observation_raced", pid=pid, source="pid")
        finally:
            self._close_owned(pid_fd)
        if len(references) > MAX_REFERENCES_PER_PROCESS:
            raise _ProbeIssue("proc_reference_limit_exceeded", pid=pid)
        references.sort()
        projection = {
            "absent_special_links": sorted(absent),
            "references": [reference.fingerprint_projection() for reference in references],
        }
        matches: list[_Match] = []
        for reference in references:
            target_ids = self.lookup.get(reference.object_key)
            if target_ids is not None:
                matches.append(_Match(target_ids, pid, expected[0], reference))
        if len(matches) > MAX_MATCHES:
            raise _ProbeIssue("proc_match_limit_exceeded", pid=pid)
        return _ProcessObservation(
            pid=pid,
            epoch_sha256=expected[0],
            reference_count=len(references),
            reference_sha256=hashlib.sha256(_canonical_json(projection)).hexdigest(),
            matches=tuple(sorted(matches)),
        )

    def capture(self) -> _GlobalObservation:
        scope_before = self._scope_identity()
        epochs_before = self._enumerate_epochs(scope_before.boot_id_sha256)
        processes = tuple(
            self._scan_process(pid, expected) for pid, expected in sorted(epochs_before.items())
        )
        epochs_after = self._enumerate_epochs(scope_before.boot_id_sha256)
        scope_after = self._scope_identity()
        if scope_before != scope_after or epochs_before != epochs_after:
            raise _ProbeIssue("proc_observation_raced")
        return _GlobalObservation(scope_before, processes)


def _empty_scope() -> dict[str, Any]:
    return {
        "boot_id_sha256": "",
        "mount_namespace": [0, 0],
        "pid_namespace": [0, 0],
        "proc_root": [0, 0],
    }


def _receipt(
    index: TargetIndex,
    *,
    fixed_point_passes: int,
    observation: _GlobalObservation | None,
    ambiguity: _ProbeIssue | None,
) -> dict[str, Any]:
    matches = observation.matches if observation is not None else ()
    status = "ambiguous" if ambiguity is not None else ("referenced" if matches else "clear")
    core: dict[str, Any] = {
        "ambiguities": (
            []
            if ambiguity is None
            else [{"code": ambiguity.code, "pid": ambiguity.pid, "source": ambiguity.source}]
        ),
        "complete": ambiguity is None,
        "fixed_point_passes": fixed_point_passes,
        "matches": [match.receipt_projection() for match in matches],
        "observation_sha256": observation.observation_sha256 if observation is not None else "",
        "pid_epoch_set_sha256": observation.pid_epoch_set_sha256 if observation is not None else "",
        "process_count": observation.process_count if observation is not None else 0,
        "reference_count": observation.reference_count if observation is not None else 0,
        "schema": PROBE_RECEIPT_SCHEMA,
        "scope": PROBE_SCOPE,
        "scope_identity": observation.scope.projection() if observation is not None else _empty_scope(),
        "status": status,
        "target_count": len(index.targets),
        "target_index_sha256": index.sha256,
        "target_object_count": index.object_count,
        "universal_absence_proof": False,
    }
    raw = _canonical_json(core)
    if len(raw) > MAX_RECEIPT_BYTES:
        return _receipt(
            index,
            fixed_point_passes=fixed_point_passes,
            observation=None,
            ambiguity=_ProbeIssue("receipt_body_limit_exceeded"),
        )
    return {**core, "receipt_sha256": hashlib.sha256(raw).hexdigest()}


def probe_namespace_visible_proc_references(
    target_index: TargetIndex,
    *,
    proc_root: Path = Path("/proc"),
    fixed_point_passes: int = 2,
    _capture_pass: CapturePass | None = None,
) -> dict[str, Any]:
    """Return a canonical, bounded observation receipt without durable effects."""

    if (
        not isinstance(target_index, TargetIndex)
        or _HEX64.fullmatch(target_index.sha256) is None
        or not 2 <= fixed_point_passes <= 4
    ):
        raise ProcProbeInputError("probe_input_invalid")
    rebuilt = build_target_index(target_index.targets)
    if rebuilt != target_index:
        raise ProcProbeInputError("target_index_digest_invalid")
    observations: list[_GlobalObservation] = []
    try:
        if _capture_pass is None:
            with _LinuxProcScanner(proc_root, target_index) as scanner:
                for _ in range(fixed_point_passes):
                    observations.append(scanner.capture())
        else:
            for _ in range(fixed_point_passes):
                observations.append(_capture_pass(target_index))
        if any(observation != observations[0] for observation in observations[1:]):
            raise _ProbeIssue("proc_fixed_point_changed")
        observation = observations[0]
        if len(observation.matches) > MAX_MATCHES:
            raise _ProbeIssue("proc_match_limit_exceeded")
        return _receipt(
            target_index,
            fixed_point_passes=fixed_point_passes,
            observation=observation,
            ambiguity=None,
        )
    except _ProbeIssue as issue:
        return _receipt(
            target_index,
            fixed_point_passes=fixed_point_passes,
            observation=None,
            ambiguity=issue,
        )
    except (OSError, UnicodeError, ValueError, OverflowError):
        return _receipt(
            target_index,
            fixed_point_passes=fixed_point_passes,
            observation=None,
            ambiguity=_ProbeIssue("proc_observation_failed"),
        )


def canonical_probe_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    """Validate the self-digest and return the unique canonical receipt bytes."""

    value = dict(receipt)
    if set(value) != _RECEIPT_CORE_KEYS | {"receipt_sha256"}:
        raise ProcProbeInputError("probe_receipt_invalid")
    digest = value.pop("receipt_sha256", None)
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        raise ProcProbeInputError("probe_receipt_invalid")
    try:
        raw = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ProcProbeInputError("probe_receipt_invalid") from exc
    if len(raw) > MAX_RECEIPT_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ProcProbeInputError("probe_receipt_invalid")
    status = value.get("status")
    complete = value.get("complete")
    matches = value.get("matches")
    ambiguities = value.get("ambiguities")
    if (
        value.get("schema") != PROBE_RECEIPT_SCHEMA
        or value.get("scope") != PROBE_SCOPE
        or value.get("universal_absence_proof") is not False
        or status not in {"clear", "referenced", "ambiguous"}
        or complete is not (status in {"clear", "referenced"})
        or not isinstance(matches, list)
        or not isinstance(ambiguities, list)
        or len(matches) > MAX_MATCHES
        or (status == "clear" and matches)
        or (status == "referenced" and not matches)
        or (status == "ambiguous" and not ambiguities)
        or (status != "ambiguous" and ambiguities)
        or any(
            type(value.get(name)) is not int or int(value[name]) < 0
            for name in (
                "fixed_point_passes",
                "process_count",
                "reference_count",
                "target_count",
                "target_object_count",
            )
        )
        or not isinstance(value.get("target_index_sha256"), str)
        or _HEX64.fullmatch(str(value["target_index_sha256"])) is None
        or int(value["target_count"]) <= 0
        or int(value["target_object_count"]) <= 0
        or not 2 <= int(value["fixed_point_passes"]) <= 4
        or len(matches) > int(value["reference_count"])
    ):
        raise ProcProbeInputError("probe_receipt_invalid")
    scope_identity = value.get("scope_identity")
    if not isinstance(scope_identity, dict) or set(scope_identity) != {
        "boot_id_sha256",
        "mount_namespace",
        "pid_namespace",
        "proc_root",
    }:
        raise ProcProbeInputError("probe_receipt_invalid")
    for name in ("mount_namespace", "pid_namespace", "proc_root"):
        identity = scope_identity[name]
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(type(item) is not int or item < 0 for item in identity)
        ):
            raise ProcProbeInputError("probe_receipt_invalid")
    boot_id_sha256 = scope_identity["boot_id_sha256"]
    if (
        not isinstance(boot_id_sha256, str)
        or (complete and _HEX64.fullmatch(boot_id_sha256) is None)
        or (not complete and boot_id_sha256 not in {""} and _HEX64.fullmatch(boot_id_sha256) is None)
        or any(
            not isinstance(value.get(name), str)
            or (complete and _HEX64.fullmatch(str(value[name])) is None)
            or (not complete and value[name] not in {""} and _HEX64.fullmatch(str(value[name])) is None)
            for name in ("observation_sha256", "pid_epoch_set_sha256")
        )
    ):
        raise ProcProbeInputError("probe_receipt_invalid")
    for ambiguity in ambiguities:
        if (
            not isinstance(ambiguity, dict)
            or set(ambiguity) != {"code", "pid", "source"}
            or not isinstance(ambiguity["code"], str)
            or not ambiguity["code"]
            or type(ambiguity["pid"]) is not int
            or ambiguity["pid"] < 0
            or not isinstance(ambiguity["source"], str)
            or not ambiguity["source"]
        ):
            raise ProcProbeInputError("probe_receipt_invalid")
    for match in matches:
        if not isinstance(match, dict) or set(match) != _MATCH_KEYS:
            raise ProcProbeInputError("probe_receipt_invalid")
        try:
            link_target = base64.b64decode(match["link_target_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise ProcProbeInputError("probe_receipt_invalid") from exc
        object_value = match["object"]
        target_ids = match["target_ids"]
        if (
            len(link_target) > MAX_LINK_TARGET_BYTES
            or base64.b64encode(link_target).decode("ascii") != match["link_target_base64"]
            or hashlib.sha256(link_target).hexdigest() != match["link_target_sha256"]
            or not isinstance(object_value, list)
            or len(object_value) != 3
            or any(type(item) is not int for item in object_value)
            or not isinstance(target_ids, list)
            or not target_ids
            or any(
                not isinstance(target_id, str) or _TARGET_ID.fullmatch(target_id) is None
                for target_id in target_ids
            )
            or target_ids != sorted(set(target_ids))
            or type(match["pid"]) is not int
            or match["pid"] <= 0
            or not isinstance(match["pid_epoch_sha256"], str)
            or _HEX64.fullmatch(match["pid_epoch_sha256"]) is None
            or not isinstance(match["source"], str)
            or not match["source"]
            or not isinstance(match["entry"], str)
            or not match["entry"]
            or (
                match["mount_id"] is not None
                and (type(match["mount_id"]) is not int or match["mount_id"] <= 0)
            )
        ):
            raise ProcProbeInputError("probe_receipt_invalid")
    return _canonical_json({**value, "receipt_sha256": digest}) + b"\n"


__all__ = [
    "ObjectKey",
    "PROBE_RECEIPT_SCHEMA",
    "PROBE_SCOPE",
    "ProbeTarget",
    "ProcProbeInputError",
    "TARGET_INDEX_SCHEMA",
    "TargetIndex",
    "build_target_index",
    "canonical_probe_receipt_bytes",
    "probe_namespace_visible_proc_references",
]
