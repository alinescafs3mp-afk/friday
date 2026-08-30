#!/usr/bin/env python3
"""Fail-closed, target-scoped Linux proc reference observation.

This module is deliberately read-only.  It does not publish receipts, rename
artifacts, adapt results into a retention ``OpenInventorySnapshot``, or delete
anything.  A ``clear`` result means only that two bounded, identical diagnostic
observations found no reference to the supplied inode set in the
namespace-visible Linux proc surfaces covered here.  It is neither a universal
kernel open-object proof nor deletion authority.
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

TARGET_INDEX_SCHEMA = "friday.release-artifact-proc-target-index.v2"
PROBE_RECEIPT_SCHEMA = "friday.release-artifact-proc-reference-receipt.v2"
PROBE_SCOPE = "namespace_visible_proc_references"
PROBE_AUTHORITY = "diagnostic_only"
_SHARED_MM_PROOF_KIND = "linux_tgid_membership_plus_exact_maps_and_exe.v1"

MAX_TARGETS = 4_096
MAX_TARGET_OBJECTS = 1_000_000
MAX_TARGET_ROOTS = 65_536
MAX_TARGET_ROOT_BYTES = 4_096
MAX_TARGET_INDEX_BYTES = 64 << 20
MAX_PIDS = 131_072
MAX_TASKS = 262_144
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
_REFERENCE_SOURCES = frozenset({"cwd", "exe", "fd", "map_files", "root"})
_ISSUE_SOURCES = frozenset(
    {
        "boot_id",
        "cwd",
        "exe",
        "fd",
        "fdinfo",
        "map_files",
        "maps",
        "mountinfo",
        "namespace",
        "pid",
        "proc",
        "receipt",
        "stat",
        "task",
    }
)
_ISSUE_CODES = frozenset(
    {
        "proc_body_limit_exceeded",
        "proc_boot_id_invalid",
        "proc_fd_inventory_invalid",
        "proc_fdinfo_invalid",
        "proc_fixed_point_changed",
        "proc_link_target_invalid",
        "proc_map_files_incomplete",
        "proc_map_files_invalid",
        "proc_maps_invalid",
        "proc_match_limit_exceeded",
        "proc_observation_failed",
        "proc_observation_raced",
        "proc_permission_denied",
        "proc_pid_inventory_invalid",
        "proc_reference_limit_exceeded",
        "proc_shared_mm_unproven",
        "proc_stat_invalid",
        "proc_surface_unsupported",
        "proc_task_inventory_invalid",
        "receipt_body_limit_exceeded",
    }
)
_RECEIPT_CORE_KEYS = frozenset(
    {
        "ambiguities",
        "authority",
        "delete_authority",
        "diagnostic_complete",
        "fixed_point_passes",
        "matches",
        "observation_sha256",
        "open_inventory_complete_authority",
        "reference_count",
        "schema",
        "scope",
        "scope_identity",
        "status",
        "task_count",
        "task_epoch_set_sha256",
        "target_count",
        "target_index_sha256",
        "target_object_count",
        "target_root_count",
        "tgid_count",
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
        "source",
        "task_epoch_sha256",
        "target_ids",
        "tgid",
        "tid",
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
    root_count: int


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
    tgid: int
    tid: int
    task_epoch_sha256: str
    reference: _Reference

    def receipt_projection(self) -> dict[str, Any]:
        encoded = base64.b64encode(self.reference.link_target).decode("ascii")
        return {
            "entry": self.reference.entry,
            "link_target_base64": encoded,
            "link_target_sha256": hashlib.sha256(self.reference.link_target).hexdigest(),
            "mount_id": self.reference.mount_id,
            "object": self.reference.object_key.projection(),
            "source": self.reference.source,
            "task_epoch_sha256": self.task_epoch_sha256,
            "target_ids": list(self.target_ids),
            "tgid": self.tgid,
            "tid": self.tid,
        }


@dataclass(frozen=True, order=True)
class _TaskObservation:
    tgid: int
    tid: int
    epoch_sha256: str
    reference_count: int
    reference_sha256: str
    shared_mm_proof_sha256: str
    matches: tuple[_Match, ...]

    def projection(self) -> dict[str, Any]:
        return {
            "epoch_sha256": self.epoch_sha256,
            "matches": [match.receipt_projection() for match in self.matches],
            "reference_count": self.reference_count,
            "reference_sha256": self.reference_sha256,
            "shared_mm_proof_sha256": self.shared_mm_proof_sha256,
            "tgid": self.tgid,
            "tid": self.tid,
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
    tasks: tuple[_TaskObservation, ...]

    @property
    def tgid_count(self) -> int:
        return len({task.tgid for task in self.tasks})

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    @property
    def reference_count(self) -> int:
        return sum(task.reference_count for task in self.tasks)

    @property
    def matches(self) -> tuple[_Match, ...]:
        return tuple(sorted(match for task in self.tasks for match in task.matches))

    @property
    def task_epoch_set_sha256(self) -> str:
        value = [[task.tgid, task.tid, task.epoch_sha256] for task in self.tasks]
        return hashlib.sha256(_canonical_json(value)).hexdigest()

    @property
    def observation_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "scope": self.scope.projection(),
                    "tasks": [task.projection() for task in self.tasks],
                }
            )
        ).hexdigest()


class _ProbeIssue(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        tgid: int = 0,
        tid: int = 0,
        pid: int | None = None,
        source: str = "proc",
    ) -> None:
        super().__init__(code)
        if pid is not None:
            tgid = tgid or pid
            tid = tid or pid
        self.code = code if code in _ISSUE_CODES else "proc_observation_failed"
        self.tgid = tgid if type(tgid) is int and tgid >= 0 else 0
        self.tid = tid if type(tid) is int and tid >= 0 else 0
        self.source = source if source in _ISSUE_SOURCES else "proc"


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
    root_total = 0
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
        if any(len(os.fsencode(root)) > MAX_TARGET_ROOT_BYTES for root in roots):
            raise ProcProbeInputError("target_root_limit_exceeded")
        root_total += len(roots)
        total += len(objects)
        if root_total > MAX_TARGET_ROOTS:
            raise ProcProbeInputError("target_root_limit_exceeded")
        if total > MAX_TARGET_OBJECTS:
            raise ProcProbeInputError("target_object_limit_exceeded")
        normalized.append(ProbeTarget(target.target_id, roots, objects))
    normalized.sort(key=lambda item: item.target_id)
    projection = {
        "object_count": total,
        "root_count": root_total,
        "schema": TARGET_INDEX_SCHEMA,
        "target_count": len(normalized),
        "targets": [
            {
                "objects": [value.projection() for value in target.objects],
                "roots": [str(root) for root in target.roots],
                "target_id": target.target_id,
            }
            for target in normalized
        ],
    }
    raw = _canonical_json(projection)
    if len(raw) > MAX_TARGET_INDEX_BYTES:
        raise ProcProbeInputError("target_index_limit_exceeded")
    return TargetIndex(
        targets=tuple(normalized),
        sha256=hashlib.sha256(raw).hexdigest(),
        object_count=total,
        root_count=root_total,
    )


def _target_lookup(index: TargetIndex) -> dict[ObjectKey, tuple[str, ...]]:
    lookup: dict[ObjectKey, list[str]] = {}
    for target in index.targets:
        for object_key in target.objects:
            lookup.setdefault(object_key, []).append(target.target_id)
    return {key: tuple(sorted(values)) for key, values in lookup.items()}


def _task_epoch_sha256(
    boot_id_sha256: str,
    tgid: int,
    tid: int,
    starttime: int,
    proc_identity: tuple[int, int],
) -> str:
    if _HEX64.fullmatch(boot_id_sha256) is None:
        raise _ProbeIssue("proc_boot_id_invalid", tgid=tgid, tid=tid, source="boot_id")
    payload = b"friday-proc-task-epoch-v2\0" + boot_id_sha256.encode("ascii")
    payload += b"\0" + str(tgid).encode() + b"\0" + str(tid).encode()
    payload += b"\0" + str(starttime).encode()
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

    def _task_names(self, task_directory: int, tgid: int) -> tuple[int, ...]:
        try:
            names = os.listdir(task_directory)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=tgid, source="task") from exc
        if any(not name.isdecimal() or str(int(name)) != name or int(name) <= 0 for name in names):
            raise _ProbeIssue("proc_task_inventory_invalid", tgid=tgid, source="task")
        tids = tuple(sorted(int(name) for name in names))
        if not tids or tgid not in tids or len(tids) != len(set(tids)):
            raise _ProbeIssue("proc_task_inventory_invalid", tgid=tgid, source="task")
        return tids

    def _open_task(self, task_directory: int, tgid: int, tid: int) -> int:
        try:
            return self._open_directory_at(task_directory, str(tid), pid=tid, source="task")
        except _ProbeIssue as issue:
            raise _ProbeIssue(issue.code, tgid=tgid, tid=tid, source=issue.source) from issue

    @staticmethod
    def _task_epoch_from_fd(
        task_fd: int,
        *,
        boot_id_sha256: str,
        tgid: int,
        tid: int,
    ) -> tuple[str, int, tuple[int, int]]:
        status = os.fstat(task_fd)
        try:
            _state, starttime = _parse_starttime(
                _read_bounded_at(task_fd, "stat", maximum=16 << 10, pid=tid, source="stat"),
                pid=tid,
            )
        except _ProbeIssue as issue:
            raise _ProbeIssue(issue.code, tgid=tgid, tid=tid, source=issue.source) from issue
        identity = (int(status.st_dev), int(status.st_ino))
        return (
            _task_epoch_sha256(boot_id_sha256, tgid, tid, starttime, identity),
            starttime,
            identity,
        )

    def _enumerate_task_epochs(
        self, boot_id_sha256: str
    ) -> dict[tuple[int, int], tuple[str, int, tuple[int, int]]]:
        result: dict[tuple[int, int], tuple[str, int, tuple[int, int]]] = {}
        seen_tids: set[int] = set()
        for tgid in self._pid_names():
            tgid_fd = self._open_pid(tgid)
            try:
                task_directory = self._open_directory_at(tgid_fd, "task", pid=tgid, source="task")
                try:
                    tids = self._task_names(task_directory, tgid)
                    for tid in tids:
                        if len(result) >= MAX_TASKS:
                            raise _ProbeIssue("proc_task_inventory_invalid", source="task")
                        if tid in seen_tids:
                            raise _ProbeIssue(
                                "proc_task_inventory_invalid",
                                tgid=tgid,
                                tid=tid,
                                source="task",
                            )
                        task_fd = self._open_task(task_directory, tgid, tid)
                        try:
                            result[(tgid, tid)] = self._task_epoch_from_fd(
                                task_fd,
                                boot_id_sha256=boot_id_sha256,
                                tgid=tgid,
                                tid=tid,
                            )
                        finally:
                            self._close_owned(task_fd)
                        seen_tids.add(tid)
                    if tids != self._task_names(task_directory, tgid):
                        raise _ProbeIssue(
                            "proc_observation_raced",
                            tgid=tgid,
                            source="task",
                        )
                finally:
                    self._close_owned(task_directory)
            finally:
                self._close_owned(tgid_fd)
        if len(result) > MAX_TASKS:
            raise _ProbeIssue("proc_task_inventory_invalid", source="task")
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
            names = sorted(names_before)
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
            if names != sorted(names_after):
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

    def _map_references(self, pid_fd: int, pid: int) -> tuple[list[_Reference], tuple[_MapRecord, ...]]:
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
            return references, maps_before
        finally:
            self._close_owned(map_directory)

    def _scan_task_references(
        self,
        task_fd: int,
        *,
        tgid: int,
        tid: int,
        expected: tuple[str, int, tuple[int, int]],
        boot_id_sha256: str,
    ) -> tuple[list[_Reference], tuple[str, ...], tuple[_MapRecord, ...], _Reference | None]:
        try:
            before = self._task_epoch_from_fd(
                task_fd,
                boot_id_sha256=boot_id_sha256,
                tgid=tgid,
                tid=tid,
            )
            if before != expected:
                raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="task")
            maps_before = _parse_maps(
                _read_bounded_at(
                    task_fd,
                    "maps",
                    maximum=MAX_PROC_FILE_BYTES,
                    pid=tid,
                    source="maps",
                ),
                pid=tid,
            )
            exe_before = self._special_reference(task_fd, tid, "exe")
            references = self._fd_references(task_fd, tid)
            absent: list[str] = []
            for name in ("cwd", "root"):
                reference = self._special_reference(task_fd, tid, name)
                if reference is None:
                    absent.append(name)
                else:
                    references.append(reference)
            maps_after = _parse_maps(
                _read_bounded_at(
                    task_fd,
                    "maps",
                    maximum=MAX_PROC_FILE_BYTES,
                    pid=tid,
                    source="maps",
                ),
                pid=tid,
            )
            exe_after = self._special_reference(task_fd, tid, "exe")
            after = self._task_epoch_from_fd(
                task_fd,
                boot_id_sha256=boot_id_sha256,
                tgid=tgid,
                tid=tid,
            )
        except _ProbeIssue as issue:
            raise _ProbeIssue(issue.code, tgid=tgid, tid=tid, source=issue.source) from issue
        if before != after or maps_before != maps_after or exe_before != exe_after:
            raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="task")
        return references, tuple(sorted(absent)), maps_before, exe_before

    def _task_observation(
        self,
        *,
        tgid: int,
        tid: int,
        expected: tuple[str, int, tuple[int, int]],
        references: list[_Reference],
        absent: tuple[str, ...],
        shared_mm_proof_sha256: str,
    ) -> _TaskObservation:
        if len(references) > MAX_REFERENCES_PER_PROCESS:
            raise _ProbeIssue(
                "proc_reference_limit_exceeded",
                tgid=tgid,
                tid=tid,
                source="task",
            )
        references.sort()
        projection = {
            "absent_special_links": list(absent),
            "references": [reference.fingerprint_projection() for reference in references],
        }
        matches: list[_Match] = []
        for reference in references:
            target_ids = self.lookup.get(reference.object_key)
            if target_ids is not None:
                matches.append(_Match(target_ids, tgid, tid, expected[0], reference))
        if len(matches) > MAX_MATCHES:
            raise _ProbeIssue("proc_match_limit_exceeded", tgid=tgid, tid=tid, source="task")
        return _TaskObservation(
            tgid=tgid,
            tid=tid,
            epoch_sha256=expected[0],
            reference_count=len(references),
            reference_sha256=hashlib.sha256(_canonical_json(projection)).hexdigest(),
            shared_mm_proof_sha256=shared_mm_proof_sha256,
            matches=tuple(sorted(matches)),
        )

    def _scan_tgid(
        self,
        tgid: int,
        expected_tasks: Mapping[int, tuple[str, int, tuple[int, int]]],
        *,
        boot_id_sha256: str,
    ) -> tuple[_TaskObservation, ...]:
        tgid_fd = self._open_pid(tgid)
        try:
            task_directory = self._open_directory_at(tgid_fd, "task", pid=tgid, source="task")
            try:
                tids = self._task_names(task_directory, tgid)
                if tids != tuple(sorted(expected_tasks)):
                    raise _ProbeIssue("proc_observation_raced", tgid=tgid, source="task")
                leader_exe_before = self._special_reference(tgid_fd, tgid, "exe")
                map_references, leader_maps = self._map_references(tgid_fd, tgid)
                leader_exe_after = self._special_reference(tgid_fd, tgid, "exe")
                if leader_exe_before != leader_exe_after:
                    raise _ProbeIssue("proc_observation_raced", tgid=tgid, source="exe")

                captured: dict[
                    int,
                    tuple[list[_Reference], tuple[str, ...], tuple[_MapRecord, ...], _Reference | None],
                ] = {}
                mm_projection: list[dict[str, Any]] = []
                for tid in tids:
                    task_fd = self._open_task(task_directory, tgid, tid)
                    try:
                        task_capture = self._scan_task_references(
                            task_fd,
                            tgid=tgid,
                            tid=tid,
                            expected=expected_tasks[tid],
                            boot_id_sha256=boot_id_sha256,
                        )
                    finally:
                        self._close_owned(task_fd)
                    references, absent, task_maps, task_exe = task_capture
                    if task_maps != leader_maps or task_exe != leader_exe_before:
                        raise _ProbeIssue(
                            "proc_shared_mm_unproven",
                            tgid=tgid,
                            tid=tid,
                            source="maps",
                        )
                    captured[tid] = task_capture
                    mm_projection.append(
                        {
                            "exe": (None if task_exe is None else task_exe.fingerprint_projection()),
                            "maps_sha256": hashlib.sha256(
                                _canonical_json(
                                    [
                                        [record.start, record.end, record.device, record.inode]
                                        for record in task_maps
                                    ]
                                )
                            ).hexdigest(),
                            "tid": tid,
                        }
                    )
                if tids != self._task_names(task_directory, tgid):
                    raise _ProbeIssue("proc_observation_raced", tgid=tgid, source="task")
            finally:
                self._close_owned(task_directory)
        finally:
            self._close_owned(tgid_fd)

        # CLONE_THREAD requires CLONE_VM on Linux.  Do not rely on that kernel
        # rule alone: every enumerated TID must also expose the same stable maps
        # object projection and exe identity before map_files/exe are charged
        # once to the TGID leader.
        shared_mm_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "proof": _SHARED_MM_PROOF_KIND,
                    "tasks": mm_projection,
                    "tgid": tgid,
                }
            )
        ).hexdigest()
        observations: list[_TaskObservation] = []
        for tid in tids:
            references, absent, _task_maps, _task_exe = captured[tid]
            if tid == tgid:
                references.extend(map_references)
                if leader_exe_before is None:
                    absent = tuple(sorted((*absent, "exe")))
                else:
                    references.append(leader_exe_before)
            observations.append(
                self._task_observation(
                    tgid=tgid,
                    tid=tid,
                    expected=expected_tasks[tid],
                    references=references,
                    absent=absent,
                    shared_mm_proof_sha256=shared_mm_sha256,
                )
            )
        return tuple(observations)

    def capture(self) -> _GlobalObservation:
        scope_before = self._scope_identity()
        epochs_before = self._enumerate_task_epochs(scope_before.boot_id_sha256)
        by_tgid: dict[int, dict[int, tuple[str, int, tuple[int, int]]]] = {}
        for (tgid, tid), expected in epochs_before.items():
            by_tgid.setdefault(tgid, {})[tid] = expected
        tasks = tuple(
            task
            for tgid, expected_tasks in sorted(by_tgid.items())
            for task in self._scan_tgid(
                tgid,
                expected_tasks,
                boot_id_sha256=scope_before.boot_id_sha256,
            )
        )
        epochs_after = self._enumerate_task_epochs(scope_before.boot_id_sha256)
        scope_after = self._scope_identity()
        if scope_before != scope_after or epochs_before != epochs_after:
            raise _ProbeIssue("proc_observation_raced")
        return _GlobalObservation(scope_before, tuple(sorted(tasks)))


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
            else [
                {
                    "code": ambiguity.code,
                    "source": ambiguity.source,
                    "tgid": ambiguity.tgid,
                    "tid": ambiguity.tid,
                }
            ]
        ),
        "authority": PROBE_AUTHORITY,
        "delete_authority": False,
        "diagnostic_complete": ambiguity is None,
        "fixed_point_passes": fixed_point_passes,
        "matches": [match.receipt_projection() for match in matches],
        "observation_sha256": observation.observation_sha256 if observation is not None else "",
        "open_inventory_complete_authority": False,
        "reference_count": observation.reference_count if observation is not None else 0,
        "schema": PROBE_RECEIPT_SCHEMA,
        "scope": PROBE_SCOPE,
        "scope_identity": observation.scope.projection() if observation is not None else _empty_scope(),
        "status": status,
        "task_count": observation.task_count if observation is not None else 0,
        "task_epoch_set_sha256": (observation.task_epoch_set_sha256 if observation is not None else ""),
        "target_count": len(index.targets),
        "target_index_sha256": index.sha256,
        "target_object_count": index.object_count,
        "target_root_count": index.root_count,
        "tgid_count": observation.tgid_count if observation is not None else 0,
        "universal_absence_proof": False,
    }
    receipt = {**core, "receipt_sha256": hashlib.sha256(_canonical_json(core)).hexdigest()}
    if len(_canonical_json(receipt) + b"\n") > MAX_RECEIPT_BYTES:
        return _receipt(
            index,
            fixed_point_passes=fixed_point_passes,
            observation=None,
            ambiguity=_ProbeIssue("receipt_body_limit_exceeded", source="receipt"),
        )
    return receipt


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
        or not isinstance(target_index.sha256, str)
        or _HEX64.fullmatch(target_index.sha256) is None
        or type(fixed_point_passes) is not int
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


def canonical_probe_receipt_bytes(
    receipt: Mapping[str, Any],
    *,
    expected_target_index: TargetIndex,
) -> bytes:
    """Serialize one exact diagnostic receipt; this never grants effect authority."""

    if (
        not isinstance(expected_target_index, TargetIndex)
        or build_target_index(expected_target_index.targets) != expected_target_index
    ):
        raise ProcProbeInputError("target_index_digest_invalid")
    if not isinstance(receipt, Mapping):
        raise ProcProbeInputError("probe_receipt_invalid")
    try:
        value = dict(receipt)
    except (TypeError, ValueError) as exc:
        raise ProcProbeInputError("probe_receipt_invalid") from exc
    if set(value) != _RECEIPT_CORE_KEYS | {"receipt_sha256"}:
        raise ProcProbeInputError("probe_receipt_invalid")
    digest = value.pop("receipt_sha256", None)
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        raise ProcProbeInputError("probe_receipt_invalid")
    try:
        raw = _canonical_json(value)
        final = _canonical_json({**value, "receipt_sha256": digest}) + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProcProbeInputError("probe_receipt_invalid") from exc
    if len(final) > MAX_RECEIPT_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ProcProbeInputError("probe_receipt_invalid")
    status = value.get("status")
    diagnostic_complete = value.get("diagnostic_complete")
    matches = value.get("matches")
    ambiguities = value.get("ambiguities")
    integer_names = (
        "fixed_point_passes",
        "reference_count",
        "target_count",
        "target_object_count",
        "target_root_count",
        "task_count",
        "tgid_count",
    )
    if (
        value.get("schema") != PROBE_RECEIPT_SCHEMA
        or value.get("scope") != PROBE_SCOPE
        or value.get("authority") != PROBE_AUTHORITY
        or value.get("universal_absence_proof") is not False
        or value.get("delete_authority") is not False
        or value.get("open_inventory_complete_authority") is not False
        or status not in {"clear", "referenced", "ambiguous"}
        or diagnostic_complete is not (status in {"clear", "referenced"})
        or not isinstance(matches, list)
        or not isinstance(ambiguities, list)
        or len(matches) > MAX_MATCHES
        or (status == "clear" and matches)
        or (status == "referenced" and not matches)
        or (status == "ambiguous" and len(ambiguities) != 1)
        or (status != "ambiguous" and ambiguities)
        or any(type(value.get(name)) is not int or int(value[name]) < 0 for name in integer_names)
        or value.get("target_index_sha256") != expected_target_index.sha256
        or value.get("target_count") != len(expected_target_index.targets)
        or value.get("target_object_count") != expected_target_index.object_count
        or value.get("target_root_count") != expected_target_index.root_count
        or not 2 <= int(value["fixed_point_passes"]) <= 4
        or len(matches) > int(value["reference_count"])
        or int(value["tgid_count"]) > int(value["task_count"])
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
        or (diagnostic_complete and _HEX64.fullmatch(boot_id_sha256) is None)
        or (
            not diagnostic_complete
            and boot_id_sha256 not in {""}
            and _HEX64.fullmatch(boot_id_sha256) is None
        )
        or any(
            not isinstance(value.get(name), str)
            or (diagnostic_complete and _HEX64.fullmatch(str(value[name])) is None)
            or (
                not diagnostic_complete
                and value[name] not in {""}
                and _HEX64.fullmatch(str(value[name])) is None
            )
            for name in ("observation_sha256", "task_epoch_set_sha256")
        )
    ):
        raise ProcProbeInputError("probe_receipt_invalid")
    if diagnostic_complete:
        if int(value["tgid_count"]) <= 0 or int(value["task_count"]) < int(value["tgid_count"]):
            raise ProcProbeInputError("probe_receipt_invalid")
    elif any(
        value[name] not in {0, ""}
        for name in (
            "observation_sha256",
            "reference_count",
            "task_count",
            "task_epoch_set_sha256",
            "tgid_count",
        )
    ):
        raise ProcProbeInputError("probe_receipt_invalid")

    for ambiguity in ambiguities:
        if (
            not isinstance(ambiguity, dict)
            or set(ambiguity) != {"code", "source", "tgid", "tid"}
            or ambiguity["code"] not in _ISSUE_CODES
            or ambiguity["source"] not in _ISSUE_SOURCES
            or type(ambiguity["tgid"]) is not int
            or type(ambiguity["tid"]) is not int
            or ambiguity["tgid"] < 0
            or ambiguity["tid"] < 0
            or (ambiguity["tid"] > 0 and ambiguity["tgid"] <= 0)
        ):
            raise ProcProbeInputError("probe_receipt_invalid")

    lookup = _target_lookup(expected_target_index)
    parsed_matches: list[_Match] = []
    for match in matches:
        if not isinstance(match, dict) or set(match) != _MATCH_KEYS:
            raise ProcProbeInputError("probe_receipt_invalid")
        try:
            link_target = base64.b64decode(match["link_target_base64"], validate=True)
            object_value = match["object"]
            if not isinstance(object_value, list) or len(object_value) != 3:
                raise ProcProbeInputError("probe_receipt_invalid")
            object_key = ObjectKey(*object_value)
        except (TypeError, ValueError) as exc:
            raise ProcProbeInputError("probe_receipt_invalid") from exc
        target_ids = match["target_ids"]
        source = match["source"]
        entry = match["entry"]
        mount_id = match["mount_id"]
        tgid = match["tgid"]
        tid = match["tid"]
        epoch = match["task_epoch_sha256"]
        if (
            len(link_target) > MAX_LINK_TARGET_BYTES
            or base64.b64encode(link_target).decode("ascii") != match["link_target_base64"]
            or hashlib.sha256(link_target).hexdigest() != match["link_target_sha256"]
            or not isinstance(target_ids, list)
            or tuple(target_ids) != lookup.get(object_key)
            or source not in _REFERENCE_SOURCES
            or not isinstance(entry, str)
            or not entry
            or type(tgid) is not int
            or type(tid) is not int
            or tgid <= 0
            or tid <= 0
            or not isinstance(epoch, str)
            or _HEX64.fullmatch(epoch) is None
            or (source == "fd" and (not entry.isdecimal() or str(int(entry)) != entry))
            or (source == "fd" and (type(mount_id) is not int or mount_id <= 0))
            or (source != "fd" and mount_id is not None)
            or (source in {"cwd", "exe", "root"} and entry != source)
        ):
            raise ProcProbeInputError("probe_receipt_invalid")
        if source == "map_files":
            try:
                _parse_map_entry(entry, pid=tid)
            except _ProbeIssue as exc:
                raise ProcProbeInputError("probe_receipt_invalid") from exc
        parsed_matches.append(
            _Match(
                tuple(target_ids),
                tgid,
                tid,
                epoch,
                _Reference(source, entry, object_key, mount_id, link_target),
            )
        )
    if (
        parsed_matches != sorted(set(parsed_matches))
        or [match.receipt_projection() for match in parsed_matches] != matches
    ):
        raise ProcProbeInputError("probe_receipt_invalid")
    return final


__all__ = [
    "ObjectKey",
    "PROBE_AUTHORITY",
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
