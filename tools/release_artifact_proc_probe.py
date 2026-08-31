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

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TARGET_INDEX_SCHEMA = "friday.release-artifact-proc-target-index.v2"
PROBE_RECEIPT_SCHEMA = "friday.release-artifact-proc-reference-receipt.v2"
PRIVILEGED_RECEIPT_SCHEMA = "friday.release-artifact-privileged-proc-receipt.v2"
HOST_SCOPE_AUTHORITY_SCHEMA = "friday.release-artifact-proc-host-scope.v1"
HOST_SCOPE_AUTHORITY_PATH = Path("/usr/libexec/friday/release_artifact_proc_scope.v1.json")
INSTALL_LOCK_PATH = Path("/usr/libexec/friday/.release-artifact-proc-probe.install.lock")
PROBE_SCOPE = "namespace_visible_proc_references"
PROBE_AUTHORITY = "diagnostic_only"
_SHARED_MM_PROOF_KIND = "linux_tgid_membership_plus_exact_maps_and_exe.v1"
_NS_GET_PARENT = 0xB702
_AT_EMPTY_PATH = 0x1000
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_MNT_ID_UNIQUE = 0x4000

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
MAX_PRIVILEGED_INPUT_BYTES = MAX_TARGET_INDEX_BYTES + (1 << 20)

_TARGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_BOOT_ID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?\Z")
_MAP_LINE = re.compile(
    rb"([0-9a-f]+)-([0-9a-f]+) "
    rb"([r-][w-][x-][ps]) ([0-9a-f]+) ([0-9a-f]+):([0-9a-f]+) ([0-9]+)(?: +(.*))?\Z"
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE_SOURCES = frozenset({"cwd", "exe", "fd", "map_files", "mount", "root"})
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


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("seconds", ctypes.c_int64),
        ("nanoseconds", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("block_size", ctypes.c_uint32),
        ("attributes", ctypes.c_uint64),
        ("nlink", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("inode", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("blocks", ctypes.c_uint64),
        ("attributes_mask", ctypes.c_uint64),
        ("atime", _StatxTimestamp),
        ("btime", _StatxTimestamp),
        ("ctime", _StatxTimestamp),
        ("mtime", _StatxTimestamp),
        ("rdev_major", ctypes.c_uint32),
        ("rdev_minor", ctypes.c_uint32),
        ("dev_major", ctypes.c_uint32),
        ("dev_minor", ctypes.c_uint32),
        ("mount_id", ctypes.c_uint64),
        ("dio_mem_align", ctypes.c_uint32),
        ("dio_offset_align", ctypes.c_uint32),
        ("subvolume", ctypes.c_uint64),
        ("atomic_write_unit_min", ctypes.c_uint32),
        ("atomic_write_unit_max", ctypes.c_uint32),
        ("atomic_write_segments_max", ctypes.c_uint32),
        ("spare1", ctypes.c_uint32),
        ("spare", ctypes.c_uint64 * 9),
    ]


def _descriptor_unique_mount_id(descriptor: int) -> int:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        statx_call = libc.statx
        statx_call.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_Statx),
        ]
        statx_call.restype = ctypes.c_int
        result = _Statx()
        status = statx_call(
            descriptor,
            b"",
            _AT_EMPTY_PATH | _AT_SYMLINK_NOFOLLOW,
            _STATX_MNT_ID_UNIQUE,
            ctypes.byref(result),
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise _ProbeIssue("proc_observation_failed", source="mountinfo") from exc
    if status != 0 or not result.mask & _STATX_MNT_ID_UNIQUE or result.mount_id <= 0:
        raise _ProbeIssue("proc_observation_failed", source="mountinfo")
    return int(result.mount_id)


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
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_IN_ROOT = 0x10
_OPENAT2_SYSCALLS = {"aarch64": 437, "x86_64": 437}


class ProcProbeInputError(ValueError):
    """The caller supplied a non-canonical or unbounded target/probe input."""


@dataclass(frozen=True)
class SameEUIDOpenSnapshot:
    """One fixed-point inventory of every visible same-euid open file object."""

    paths: tuple[Path, ...]
    identities: tuple[tuple[int, int], ...]
    process_epoch_sha256: str
    process_count: int


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


def canonical_target_index_bytes(index: TargetIndex) -> bytes:
    """Serialize an exact bounded target index for the privileged observer."""

    if not isinstance(index, TargetIndex) or build_target_index(index.targets) != index:
        raise ProcProbeInputError("target_index_digest_invalid")
    value = {
        "object_count": index.object_count,
        "root_count": index.root_count,
        "schema": TARGET_INDEX_SCHEMA,
        "target_count": len(index.targets),
        "target_index_sha256": index.sha256,
        "targets": [
            {
                "objects": [item.projection() for item in target.objects],
                "roots": [str(root) for root in target.roots],
                "target_id": target.target_id,
            }
            for target in index.targets
        ],
    }
    raw = _canonical_json(value) + b"\n"
    if len(raw) > MAX_PRIVILEGED_INPUT_BYTES:
        raise ProcProbeInputError("target_index_limit_exceeded")
    return raw


def parse_target_index_bytes(raw: bytes) -> TargetIndex:
    """Parse only the canonical serializer output; duplicate JSON keys fail."""

    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or len(raw) > MAX_PRIVILEGED_INPUT_BYTES:
        raise ProcProbeInputError("target_index_invalid")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ProcProbeInputError("target_index_invalid")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ProcProbeInputError("target_index_invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "object_count",
            "root_count",
            "schema",
            "target_count",
            "target_index_sha256",
            "targets",
        }
        or value.get("schema") != TARGET_INDEX_SCHEMA
        or not isinstance(value.get("targets"), list)
    ):
        raise ProcProbeInputError("target_index_invalid")
    targets: list[ProbeTarget] = []
    try:
        for item in value["targets"]:
            if not isinstance(item, dict) or set(item) != {"objects", "roots", "target_id"}:
                raise ProcProbeInputError("target_index_invalid")
            if not isinstance(item["objects"], list) or not isinstance(item["roots"], list):
                raise ProcProbeInputError("target_index_invalid")
            targets.append(
                ProbeTarget(
                    item["target_id"],
                    tuple(Path(root) for root in item["roots"]),
                    tuple(ObjectKey(*object_key) for object_key in item["objects"]),
                )
            )
        index = build_target_index(targets)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProcProbeInputError("target_index_invalid") from exc
    if (
        type(value["target_count"]) is not int
        or type(value["object_count"]) is not int
        or type(value["root_count"]) is not int
        or value["target_count"] != len(index.targets)
        or value["object_count"] != index.object_count
        or value["root_count"] != index.root_count
        or value["target_index_sha256"] != index.sha256
        or raw != canonical_target_index_bytes(index)
    ):
        raise ProcProbeInputError("target_index_invalid")
    return index


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


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _openat2_in_root(root_fd: int, relative: bytes, *, pid: int) -> int:
    machine = os.uname().machine
    syscall_number = _OPENAT2_SYSCALLS.get(machine)
    if syscall_number is None or not relative or b"\x00" in relative or relative.startswith(b"/"):
        raise _ProbeIssue("proc_surface_unsupported", pid=pid, source="mountinfo")
    how = _OpenHow(
        getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
        0,
        _RESOLVE_IN_ROOT | _RESOLVE_NO_MAGICLINKS,
    )
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = int(
        libc.syscall(
            ctypes.c_long(syscall_number),
            ctypes.c_int(root_fd),
            ctypes.c_char_p(relative),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
    )
    if descriptor < 0:
        error = OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        raise _issue_from_oserror(error, pid=pid, source="mountinfo") from error
    return descriptor


def _mountinfo_path(raw: bytes, *, pid: int) -> bytes:
    if not raw.startswith(b"/") or b"\x00" in raw:
        raise _ProbeIssue("proc_observation_failed", pid=pid, source="mountinfo")
    result = bytearray()
    index = 0
    escapes = {b"040": 0x20, b"011": 0x09, b"012": 0x0A, b"134": 0x5C}
    while index < len(raw):
        if raw[index : index + 1] == b"\\":
            encoded = raw[index + 1 : index + 4]
            value = escapes.get(encoded)
            if value is None:
                raise _ProbeIssue("proc_observation_failed", pid=pid, source="mountinfo")
            result.append(value)
            index += 4
        else:
            result.append(raw[index])
            index += 1
    if b"\x00" in result or b"\n" in result or b"\r" in result:
        raise _ProbeIssue("proc_observation_failed", pid=pid, source="mountinfo")
    return bytes(result)


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


def _lexical_open_path(raw: str) -> Path | None:
    suffix = " (deleted)"
    value = raw[: -len(suffix)] if raw.endswith(suffix) else raw
    if not value.startswith(os.sep) or any(character in value for character in "\x00\r\n"):
        return None
    lexical = Path(os.path.abspath(value))
    return lexical if lexical.is_absolute() else None


def _same_euid_task_snapshot(
    task_path: Path,
    *,
    tgid: int,
    tid: int,
    expected: os.stat_result,
) -> tuple[int, int, tuple[int, int], tuple[str, ...], tuple[tuple[int, int], ...]]:
    directory_fd = -1
    fd_directory = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    paths: set[str] = set()
    identities: set[tuple[int, int]] = set()

    def observe_link(name: str) -> None:
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
            target = os.readlink(name, dir_fd=directory_fd)
            status_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=tid, source=name) from exc
        identity = (int(status.st_dev), int(status.st_ino))
        if identity != (int(status_after.st_dev), int(status_after.st_ino)) or identity[1] <= 0:
            raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source=name)
        identities.add(identity)
        lexical = _lexical_open_path(target)
        if lexical is not None:
            paths.add(str(lexical))

    try:
        directory_fd = os.open(str(task_path), flags)
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ) or opened.st_uid != os.geteuid():
            raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="pid")
        stat_before = _read_bounded_at(
            directory_fd,
            "stat",
            maximum=MAX_PROC_FILE_BYTES,
            pid=tid,
            source="stat",
        )
        _state, starttime = _parse_starttime(stat_before, pid=tid)
        for name in ("cwd", "exe", "root"):
            observe_link(name)

        fd_directory = os.open("fd", flags, dir_fd=directory_fd)
        fd_names = tuple(os.listdir(fd_directory))
        if any(not name.isdecimal() or str(int(name)) != name for name in fd_names):
            raise _ProbeIssue("proc_fd_inventory_invalid", tgid=tgid, tid=tid, source="fd")
        fd_names = tuple(sorted(fd_names, key=int))
        own_directory_name = str(fd_directory) if tgid == os.getpid() else ""
        for name in fd_names:
            if name == own_directory_name:
                continue
            try:
                status = os.stat(name, dir_fd=fd_directory, follow_symlinks=True)
                target = os.readlink(name, dir_fd=fd_directory)
                status_after = os.stat(name, dir_fd=fd_directory, follow_symlinks=True)
            except OSError as exc:
                raise _issue_from_oserror(exc, pid=tid, source="fd") from exc
            identity = (int(status.st_dev), int(status.st_ino))
            if identity != (int(status_after.st_dev), int(status_after.st_ino)) or identity[1] <= 0:
                raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="fd")
            identities.add(identity)
            lexical = _lexical_open_path(target)
            if lexical is not None:
                paths.add(str(lexical))
        fd_names_after = tuple(os.listdir(fd_directory))
        if any(not name.isdecimal() or str(int(name)) != name for name in fd_names_after):
            raise _ProbeIssue("proc_fd_inventory_invalid", tgid=tgid, tid=tid, source="fd")
        fd_names_after = tuple(sorted(fd_names_after, key=int))
        if fd_names_after != fd_names:
            raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="fd")

        maps_raw = _read_bounded_at(
            directory_fd,
            "maps",
            maximum=MAX_PROC_FILE_BYTES,
            pid=tid,
            source="maps",
        )
        for record in _parse_maps(maps_raw, pid=tid):
            identities.add((record.device, record.inode))
        stat_after = _read_bounded_at(
            directory_fd,
            "stat",
            maximum=MAX_PROC_FILE_BYTES,
            pid=tid,
            source="stat",
        )
        _state_after, starttime_after = _parse_starttime(stat_after, pid=tid)
        if starttime_after != starttime:
            raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="stat")
        named = os.stat(str(task_path), follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise _ProbeIssue("proc_observation_raced", tgid=tgid, tid=tid, source="pid")
        return (
            tid,
            starttime,
            (int(opened.st_dev), int(opened.st_ino)),
            tuple(sorted(paths)),
            tuple(sorted(identities)),
        )
    except _ProbeIssue:
        raise
    except OSError as exc:
        raise _issue_from_oserror(exc, pid=tid, source="proc") from exc
    finally:
        if fd_directory >= 0:
            os.close(fd_directory)
        if directory_fd >= 0:
            os.close(directory_fd)


def _same_euid_process_snapshot(
    proc_root: Path,
    *,
    pid: int,
    expected: os.stat_result,
) -> tuple[int, str, tuple[str, ...], tuple[tuple[int, int], ...]]:
    process_fd = -1
    task_fd = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        process_path = proc_root / str(pid)
        process_fd = os.open(str(process_path), flags)
        opened = os.fstat(process_fd)
        if (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ) or opened.st_uid != os.geteuid():
            raise _ProbeIssue("proc_observation_raced", pid=pid, source="pid")
        task_fd = os.open("task", flags, dir_fd=process_fd)
        task_names = tuple(os.listdir(task_fd))
        if any(not name.isdecimal() or str(int(name)) != name for name in task_names):
            raise _ProbeIssue("proc_task_inventory_invalid", pid=pid, source="task")
        task_names = tuple(sorted(task_names, key=int))
        if str(pid) not in task_names or len(task_names) > MAX_TASKS:
            raise _ProbeIssue("proc_task_inventory_invalid", pid=pid, source="task")
        tasks: list[tuple[int, int, tuple[int, int], tuple[str, ...], tuple[tuple[int, int], ...]]] = []
        for name in task_names:
            tid = int(name)
            status = os.stat(name, dir_fd=task_fd, follow_symlinks=False)
            if status.st_uid != os.geteuid():
                raise _ProbeIssue("proc_observation_raced", tgid=pid, tid=tid, source="task")
            tasks.append(
                _same_euid_task_snapshot(
                    process_path / "task" / name,
                    tgid=pid,
                    tid=tid,
                    expected=status,
                )
            )
        task_names_after = tuple(os.listdir(task_fd))
        if any(not name.isdecimal() or str(int(name)) != name for name in task_names_after):
            raise _ProbeIssue("proc_task_inventory_invalid", pid=pid, source="task")
        if tuple(sorted(task_names_after, key=int)) != task_names:
            raise _ProbeIssue("proc_observation_raced", pid=pid, source="task")
        named = os.stat(process_path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise _ProbeIssue("proc_observation_raced", pid=pid, source="pid")
        paths = tuple(
            sorted({path for _tid, _start, _identity, values, _objects in tasks for path in values})
        )
        identities = tuple(
            sorted(
                {identity for _tid, _start, _task_identity, _paths, objects in tasks for identity in objects}
            )
        )
        epochs = [[tid, starttime, *identity] for tid, starttime, identity, _paths, _objects in tasks]
        return pid, hashlib.sha256(_canonical_json(epochs)).hexdigest(), paths, identities
    except _ProbeIssue:
        raise
    except OSError as exc:
        raise _issue_from_oserror(exc, pid=pid, source="proc") from exc
    finally:
        if task_fd >= 0:
            os.close(task_fd)
        if process_fd >= 0:
            os.close(process_fd)


def _same_euid_open_pass(
    proc_root: Path,
) -> tuple[tuple[int, str, tuple[str, ...], tuple[tuple[int, int], ...]], ...]:
    try:
        root_status = os.stat(proc_root, follow_symlinks=False)
        names = sorted(
            (name for name in os.listdir(proc_root) if name.isdecimal() and str(int(name)) == name),
            key=int,
        )
    except OSError as exc:
        raise ProcProbeInputError("open_inventory_proc_unavailable") from exc
    if not stat.S_ISDIR(root_status.st_mode) or len(names) > MAX_PIDS:
        raise ProcProbeInputError("open_inventory_proc_invalid")
    result: list[tuple[int, str, tuple[str, ...], tuple[tuple[int, int], ...]]] = []
    for name in names:
        pid = int(name)
        try:
            status = os.stat(proc_root / name, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProcProbeInputError("open_inventory_proc_unreadable") from exc
        if status.st_uid != os.geteuid():
            continue
        try:
            result.append(_same_euid_process_snapshot(proc_root, pid=pid, expected=status))
        except _ProbeIssue as exc:
            raise ProcProbeInputError("open_inventory_proc_incomplete") from exc
    return tuple(result)


def snapshot_same_euid_open_files(
    *,
    proc_root: Path = Path("/proc"),
) -> SameEUIDOpenSnapshot:
    """Return one complete same-euid fixed point or fail closed on any ambiguity."""

    first = _same_euid_open_pass(proc_root)
    second = _same_euid_open_pass(proc_root)
    if first != second:
        raise ProcProbeInputError("open_inventory_proc_changed")
    paths = tuple(sorted({Path(path) for _pid, _start, values, _objects in first for path in values}))
    identities = tuple(sorted({identity for _pid, _start, _paths, objects in first for identity in objects}))
    if len(paths) > MAX_TARGET_ROOTS or len(identities) > MAX_TARGET_OBJECTS:
        raise ProcProbeInputError("open_inventory_proc_bound_exceeded")
    epochs = [[pid, starttime] for pid, starttime, _paths, _objects in first]
    return SameEUIDOpenSnapshot(
        paths=paths,
        identities=identities,
        process_epoch_sha256=hashlib.sha256(_canonical_json(epochs)).hexdigest(),
        process_count=len(first),
    )


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
        self.identity_lookup: dict[tuple[int, int], tuple[ObjectKey, ...]] = {}
        for object_key in self.lookup:
            self.identity_lookup.setdefault((object_key.device, object_key.inode), ())
            self.identity_lookup[(object_key.device, object_key.inode)] = tuple(
                sorted((*self.identity_lookup[(object_key.device, object_key.inode)], object_key))
            )
        self.proc_fd = -1
        self._owned_fds: set[int] = set()
        self._mount_cache: dict[
            tuple[int, int, str, int, int, int, int],
            tuple[tuple[_Reference, ...], str, int, bytes, ObjectKey, int],
        ] = {}

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
                reference = self._reference(
                    fd_directory,
                    name,
                    pid=pid,
                    source="fd",
                    mount_id=_parse_fdinfo(info, pid=pid),
                )
                if reference.link_target == b"anon_inode:[io_uring]":
                    raise _ProbeIssue("proc_surface_unsupported", pid=pid, source="fdinfo")
                if reference.link_target.startswith(b"mnt:[") and reference.link_target.endswith(b"]"):
                    # A mount namespace can outlive its last task through an
                    # nsfs descriptor.  Its bind aliases are not represented by
                    # any task mountinfo, so this bounded observer must stop.
                    raise _ProbeIssue("proc_surface_unsupported", pid=pid, source="fdinfo")
                references.append(reference)
                if reference.link_target in {
                    b"anon_inode:inotify",
                    b"anon_inode:[inotify]",
                    b"anon_inode:[fanotify]",
                }:
                    for line in info.splitlines():
                        if not (line.startswith(b"inotify ") or line.startswith(b"fanotify ")):
                            continue
                        fields = {
                            key: value
                            for token in line.split()
                            if b":" in token
                            for key, value in [token.split(b":", 1)]
                        }
                        inode_raw = fields.get(b"ino")
                        device_raw = fields.get(b"sdev")
                        if inode_raw is None or device_raw is None:
                            raise _ProbeIssue(
                                "proc_fdinfo_invalid",
                                pid=pid,
                                source="fdinfo",
                            )
                        try:
                            inode = int(inode_raw, 16)
                            encoded_device = int(device_raw, 16)
                            major = (encoded_device >> 8) & 0xFFF
                            minor = (encoded_device & 0xFF) | ((encoded_device >> 12) & 0xFFF00)
                            device = os.makedev(major, minor)
                        except (ValueError, OverflowError) as exc:
                            raise _ProbeIssue(
                                "proc_fdinfo_invalid",
                                pid=pid,
                                source="fdinfo",
                            ) from exc
                        for watched in self.identity_lookup.get((device, inode), ()):
                            references.append(
                                _Reference("fd", name, watched, reference.mount_id, reference.link_target)
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

    def _mount_references(self, pid_fd: int, pid: int) -> tuple[list[_Reference], str]:
        try:
            namespace_before = os.stat("ns/mnt", dir_fd=pid_fd, follow_symlinks=True)
            raw_before = _read_bounded_at(
                pid_fd,
                "mountinfo",
                maximum=MAX_PROC_FILE_BYTES,
                pid=pid,
                source="mountinfo",
            )
            root_fd = os.open(
                "root",
                getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=pid_fd,
            )
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source="mountinfo") from exc
        self._owned_fds.add(root_fd)
        namespace_identity = (int(namespace_before.st_dev), int(namespace_before.st_ino))
        root_identity = ObjectKey.from_stat(os.fstat(root_fd))
        root_mount_id = _descriptor_unique_mount_id(root_fd)
        raw_sha256 = hashlib.sha256(raw_before).hexdigest()
        cache_key = (
            namespace_identity[0],
            namespace_identity[1],
            raw_sha256,
            root_identity.device,
            root_identity.inode,
            root_identity.file_type,
            root_mount_id,
        )
        cached = self._mount_cache.get(cache_key)
        if cached is not None:
            try:
                raw_after = _read_bounded_at(
                    pid_fd,
                    "mountinfo",
                    maximum=MAX_PROC_FILE_BYTES,
                    pid=pid,
                    source="mountinfo",
                )
                namespace_after = os.stat("ns/mnt", dir_fd=pid_fd, follow_symlinks=True)
                named_root_after = ObjectKey.from_stat(os.stat("root", dir_fd=pid_fd, follow_symlinks=True))
                root_after_fd = os.open(
                    "root",
                    getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=pid_fd,
                )
                try:
                    root_mount_after = _descriptor_unique_mount_id(root_after_fd)
                finally:
                    os.close(root_after_fd)
                if (
                    raw_after != raw_before
                    or namespace_identity
                    != (
                        int(namespace_after.st_dev),
                        int(namespace_after.st_ino),
                    )
                    or named_root_after != root_identity
                    or root_mount_after != root_mount_id
                ):
                    raise _ProbeIssue("proc_observation_raced", pid=pid, source="mountinfo")
                return list(cached[0]), cached[1]
            finally:
                self._close_owned(root_fd)
        references: list[_Reference] = []
        projection: list[list[Any]] = []
        try:
            lines = raw_before.splitlines()
            if not lines or len(lines) > MAX_REFERENCES_PER_PROCESS:
                raise _ProbeIssue("proc_reference_limit_exceeded", pid=pid, source="mountinfo")
            seen_mount_ids: set[int] = set()
            for line in lines:
                left, separator, right = line.partition(b" - ")
                fields = left.split()
                if not separator or not right or len(fields) < 6 or not fields[0].isdigit():
                    raise _ProbeIssue("proc_observation_failed", pid=pid, source="mountinfo")
                mount_id = int(fields[0])
                if mount_id <= 0 or mount_id in seen_mount_ids:
                    raise _ProbeIssue("proc_observation_failed", pid=pid, source="mountinfo")
                seen_mount_ids.add(mount_id)
                mountpoint = _mountinfo_path(fields[4], pid=pid)
                relative = mountpoint.lstrip(b"/") or b"."
                descriptor = _openat2_in_root(root_fd, relative, pid=pid)
                try:
                    status = os.fstat(descriptor)
                    object_key = ObjectKey.from_stat(status)
                    fdinfo = _read_bounded_at(
                        self.proc_fd,
                        f"self/fdinfo/{descriptor}",
                        maximum=64 << 10,
                        pid=pid,
                        source="fdinfo",
                    )
                    if _parse_fdinfo(fdinfo, pid=pid) != mount_id:
                        raise _ProbeIssue(
                            "proc_observation_failed",
                            pid=pid,
                            source="mountinfo",
                        )
                finally:
                    os.close(descriptor)
                projection.append([mount_id, object_key.projection()])
                if object_key in self.lookup:
                    references.append(_Reference("mount", str(mount_id), object_key, mount_id, b""))
            raw_after = _read_bounded_at(
                pid_fd,
                "mountinfo",
                maximum=MAX_PROC_FILE_BYTES,
                pid=pid,
                source="mountinfo",
            )
            namespace_after = os.stat("ns/mnt", dir_fd=pid_fd, follow_symlinks=True)
            named_root_after = ObjectKey.from_stat(os.stat("root", dir_fd=pid_fd, follow_symlinks=True))
            root_after_fd = os.open(
                "root",
                getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=pid_fd,
            )
            try:
                root_mount_after = _descriptor_unique_mount_id(root_after_fd)
            finally:
                os.close(root_after_fd)
            if (
                raw_after != raw_before
                or (namespace_before.st_dev, namespace_before.st_ino)
                != (
                    namespace_after.st_dev,
                    namespace_after.st_ino,
                )
                or named_root_after != root_identity
                or root_mount_after != root_mount_id
            ):
                raise _ProbeIssue("proc_observation_raced", pid=pid, source="mountinfo")
            proof = {
                "mount_namespace": list(namespace_identity),
                "mounts": projection,
                "raw_sha256": hashlib.sha256(raw_before).hexdigest(),
                "root_identity": root_identity.projection(),
                "root_mount_id": root_mount_id,
            }
            proof_sha256 = hashlib.sha256(_canonical_json(proof)).hexdigest()
            self._mount_cache[cache_key] = (
                tuple(references),
                proof_sha256,
                pid,
                raw_before,
                root_identity,
                root_mount_id,
            )
            return references, proof_sha256
        except _ProbeIssue:
            raise
        except OSError as exc:
            raise _issue_from_oserror(exc, pid=pid, source="mountinfo") from exc
        finally:
            self._close_owned(root_fd)

    def _verify_mount_cache(self) -> None:
        for (
            device,
            inode,
            _raw_sha256,
            _root_device,
            _root_inode,
            _root_type,
            expected_root_mount,
        ), (_references, _proof, pid, expected_raw, expected_root, cached_root_mount) in sorted(
            self._mount_cache.items()
        ):
            pid_fd = self._open_pid(pid)
            try:
                namespace = os.stat("ns/mnt", dir_fd=pid_fd, follow_symlinks=True)
                raw = _read_bounded_at(
                    pid_fd,
                    "mountinfo",
                    maximum=MAX_PROC_FILE_BYTES,
                    pid=pid,
                    source="mountinfo",
                )
                root = ObjectKey.from_stat(os.stat("root", dir_fd=pid_fd, follow_symlinks=True))
                root_fd = os.open(
                    "root",
                    getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=pid_fd,
                )
                try:
                    observed_root_mount = _descriptor_unique_mount_id(root_fd)
                finally:
                    os.close(root_fd)
                if (
                    (namespace.st_dev, namespace.st_ino) != (device, inode)
                    or raw != expected_raw
                    or root != expected_root
                    or cached_root_mount != expected_root_mount
                    or observed_root_mount != expected_root_mount
                ):
                    raise _ProbeIssue("proc_observation_raced", pid=pid, source="mountinfo")
            finally:
                self._close_owned(pid_fd)

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
        references = sorted(set(references))
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
                        task_mount_references, task_mount_proof_sha256 = self._mount_references(
                            task_fd,
                            tid,
                        )
                    finally:
                        self._close_owned(task_fd)
                    references, absent, task_maps, task_exe = task_capture
                    references.extend(task_mount_references)
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
                            "mount_proof_sha256": task_mount_proof_sha256,
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
        self._mount_cache = {}
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
        self._verify_mount_cache()
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
            or (
                source == "mount"
                and (
                    not entry.isdecimal()
                    or str(int(entry)) != entry
                    or type(mount_id) is not int
                    or mount_id <= 0
                    or int(entry) != mount_id
                    or link_target
                )
            )
            or (source not in {"fd", "mount"} and mount_id is not None)
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


def _implementation_sha256() -> str:
    path = Path(__file__)
    try:
        before = os.lstat(path)
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise ProcProbeInputError("privileged_probe_code_invalid") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise ProcProbeInputError("privileged_probe_code_invalid")
    return hashlib.sha256(raw).hexdigest()


def _stable_kernel_bytes(path: Path, *, maximum: int) -> bytes:
    try:
        before = os.lstat(path)
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise ProcProbeInputError("privileged_probe_incomplete") from exc
    identity = lambda value: (  # noqa: E731
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if not stat.S_ISREG(before.st_mode) or identity(before) != identity(after) or len(raw) > maximum:
        raise ProcProbeInputError("privileged_probe_incomplete")
    return raw


def _kernel_target_references(index: TargetIndex) -> tuple[tuple[str, ...], str]:
    lookup = _target_lookup(index)
    referenced: set[str] = set()
    projection: list[list[Any]] = []

    def observe_path(kind: str, raw_path: bytes) -> None:
        path_bytes = _mountinfo_path(raw_path, pid=0)
        path = Path(os.fsdecode(path_bytes))
        try:
            before = os.stat(path, follow_symlinks=True)
            after = os.stat(path, follow_symlinks=True)
        except OSError as exc:
            raise ProcProbeInputError("privileged_probe_incomplete") from exc
        before_key = ObjectKey.from_stat(before)
        if before_key != ObjectKey.from_stat(after):
            raise ProcProbeInputError("privileged_probe_incomplete")
        target_ids = lookup.get(before_key, ())
        referenced.update(target_ids)
        projection.append(
            [kind, before_key.projection(), hashlib.sha256(path_bytes).hexdigest(), list(target_ids)]
        )

    swaps = _stable_kernel_bytes(Path("/proc/swaps"), maximum=4 << 20)
    lines = swaps.splitlines()
    if not lines or not lines[0].startswith(b"Filename"):
        raise ProcProbeInputError("privileged_probe_incomplete")
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 5:
            raise ProcProbeInputError("privileged_probe_incomplete")
        observe_path("swap", fields[0])

    try:
        block_names_before = sorted(path.name for path in Path("/sys/block").iterdir())
    except OSError as exc:
        raise ProcProbeInputError("privileged_probe_incomplete") from exc
    for name in block_names_before:
        if not re.fullmatch(r"loop[0-9]+", name):
            continue
        backing = Path("/sys/block") / name / "loop/backing_file"
        try:
            raw = _stable_kernel_bytes(backing, maximum=MAX_LINK_TARGET_BYTES).strip()
        except ProcProbeInputError:
            if not backing.exists() and not backing.is_symlink():
                continue
            raise
        if raw:
            observe_path("loop", raw)
    try:
        block_names_after = sorted(path.name for path in Path("/sys/block").iterdir())
    except OSError as exc:
        raise ProcProbeInputError("privileged_probe_incomplete") from exc
    swaps_after = _stable_kernel_bytes(Path("/proc/swaps"), maximum=4 << 20)
    if block_names_after != block_names_before or swaps_after != swaps:
        raise ProcProbeInputError("privileged_probe_incomplete")
    projection.sort()
    return tuple(sorted(referenced)), hashlib.sha256(_canonical_json(projection)).hexdigest()


def _capture_privileged_target_observation(
    index: TargetIndex,
) -> tuple[_GlobalObservation, tuple[str, ...], str]:
    """Find two consecutive target/process fixed points, ignoring unrelated FD churn."""

    previous: tuple[_GlobalObservation, tuple[str, ...], str] | None = None
    try:
        with _LinuxProcScanner(Path("/proc"), index) as scanner:
            for _attempt in range(6):
                try:
                    kernel_before = _kernel_target_references(index)
                    current = scanner.capture()
                    kernel_after = _kernel_target_references(index)
                except _ProbeIssue as issue:
                    if issue.code not in {"proc_observation_raced", "proc_fixed_point_changed"}:
                        raise
                    previous = None
                    continue
                if kernel_before != kernel_after:
                    previous = None
                    continue
                combined = (current, *kernel_after)
                if previous is not None and (
                    previous[0].scope == current.scope
                    and previous[0].task_epoch_set_sha256 == current.task_epoch_set_sha256
                    and previous[0].matches == current.matches
                    and previous[1:] == combined[1:]
                ):
                    return combined
                previous = combined
    except (_ProbeIssue, ProcProbeInputError) as exc:
        raise ProcProbeInputError("privileged_probe_incomplete") from exc
    raise ProcProbeInputError("privileged_probe_incomplete")


def _host_scope_authority() -> tuple[dict[str, Any], str]:
    """Authenticate the initial-host proc/PID scope pinned during root install."""

    try:
        before = os.lstat(HOST_SCOPE_AUTHORITY_PATH)
        raw = HOST_SCOPE_AUTHORITY_PATH.read_bytes()
        after = os.lstat(HOST_SCOPE_AUTHORITY_PATH)
        value = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcProbeInputError("privileged_probe_authority_invalid") from exc
    identity = lambda item: (  # noqa: E731
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"required_capabilities", "schema", "scope"}
        or raw != _canonical_json(value) + b"\n"
        or value.get("schema") != HOST_SCOPE_AUTHORITY_SCHEMA
        or value.get("scope") != "initial_pid_namespace_and_proc_v1"
        or value.get("required_capabilities") != ["CAP_SYS_ADMIN", "CAP_SYS_PTRACE"]
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o400
        or not 0 < before.st_size <= 4096
        or identity(before) != identity(after)
    ):
        raise ProcProbeInputError("privileged_probe_authority_invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _require_initial_host_scope(observation: _GlobalObservation) -> None:
    try:
        status = Path("/proc/self/status").read_bytes()
        capability_lines = [line for line in status.splitlines() if line.startswith(b"CapEff:\t")]
        if len(capability_lines) != 1:
            raise ProcProbeInputError("privileged_probe_authority_invalid")
        capabilities = int(capability_lines[0].split(b"\t", 1)[1], 16)
        if not capabilities & (1 << 21) or not capabilities & (1 << 19):
            raise ProcProbeInputError("privileged_probe_authority_invalid")
        self_namespace = os.stat("/proc/self/ns/pid")
        init_namespace = os.stat("/proc/1/ns/pid")
        if (self_namespace.st_dev, self_namespace.st_ino) != (
            init_namespace.st_dev,
            init_namespace.st_ino,
        ):
            raise ProcProbeInputError("privileged_probe_authority_invalid")
        descriptor = os.open(
            "/proc/self/ns/pid",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened_namespace = os.fstat(descriptor)
            named_namespace_after = os.stat("/proc/self/ns/pid")
            expected_namespace = (int(self_namespace.st_dev), int(self_namespace.st_ino))
            if (
                (int(opened_namespace.st_dev), int(opened_namespace.st_ino)) != expected_namespace
                or (int(named_namespace_after.st_dev), int(named_namespace_after.st_ino))
                != expected_namespace
                or observation.scope.pid_namespace != expected_namespace
            ):
                raise ProcProbeInputError("privileged_probe_authority_invalid")
            try:
                parent = fcntl.ioctl(descriptor, _NS_GET_PARENT)
            except OSError as exc:
                if exc.errno != errno.EPERM:
                    raise ProcProbeInputError("privileged_probe_authority_invalid") from exc
            else:
                os.close(parent)
                raise ProcProbeInputError("privileged_probe_authority_invalid")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ProcProbeInputError):
            raise
        raise ProcProbeInputError("privileged_probe_authority_invalid") from exc
    if observation.scope.pid_namespace != (
        int(self_namespace.st_dev),
        int(self_namespace.st_ino),
    ):
        raise ProcProbeInputError("privileged_probe_authority_invalid")


def privileged_target_reference_receipt(index: TargetIndex) -> dict[str, Any]:
    """Observe exact targets from a root/CAP_SYS_PTRACE helper, body-free."""

    if os.geteuid() != 0 or build_target_index(index.targets) != index:
        raise ProcProbeInputError("privileged_probe_authority_invalid")
    host_scope, host_scope_sha256 = _host_scope_authority()
    observation, kernel_referenced, kernel_epoch_sha256 = _capture_privileged_target_observation(index)
    del host_scope
    _require_initial_host_scope(observation)
    matches = [match.receipt_projection() for match in observation.matches]
    referenced = sorted(
        {
            target_id
            for match in matches
            if isinstance(match, Mapping)
            for target_id in match.get("target_ids", [])
            if isinstance(target_id, str)
        }
        | set(kernel_referenced)
    )
    if any(target_id not in {target.target_id for target in index.targets} for target_id in referenced):
        raise ProcProbeInputError("privileged_probe_incomplete")
    scope = observation.scope.projection()
    core: dict[str, Any] = {
        "authority": "code_owned_privileged_bounded_proc_diagnostic_v1",
        "host_scope_authority_sha256": host_scope_sha256,
        "implementation_sha256": _implementation_sha256(),
        "kernel_epoch_sha256": kernel_epoch_sha256,
        "observation_sha256": observation.observation_sha256,
        "observer_euid": 0,
        "process_epoch_sha256": observation.task_epoch_set_sha256,
        "referenced_target_ids": referenced,
        "schema": PRIVILEGED_RECEIPT_SCHEMA,
        "scope_identity_sha256": hashlib.sha256(_canonical_json(dict(scope))).hexdigest(),
        "status": "referenced" if referenced else "clear",
        "target_count": len(index.targets),
        "target_index_sha256": index.sha256,
        "task_count": observation.task_count,
        "tgid_count": observation.tgid_count,
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical_json(core)).hexdigest()}


def canonical_privileged_receipt_bytes(
    receipt: Mapping[str, Any],
    *,
    expected_target_index: TargetIndex,
    expected_implementation_sha256: str,
    expected_host_scope_authority_sha256: str,
) -> bytes:
    """Validate the exact body-free privileged observer result."""

    if not isinstance(receipt, Mapping):
        raise ProcProbeInputError("privileged_probe_receipt_invalid")
    value = dict(receipt)
    required = {
        "authority",
        "implementation_sha256",
        "host_scope_authority_sha256",
        "kernel_epoch_sha256",
        "observation_sha256",
        "observer_euid",
        "process_epoch_sha256",
        "receipt_sha256",
        "referenced_target_ids",
        "schema",
        "scope_identity_sha256",
        "status",
        "target_count",
        "target_index_sha256",
        "task_count",
        "tgid_count",
    }
    digest = value.pop("receipt_sha256", None)
    known_ids = {target.target_id for target in expected_target_index.targets}
    referenced = value.get("referenced_target_ids")
    if (
        set(receipt) != required
        or not _is_hex_digest(digest)
        or hashlib.sha256(_canonical_json(value)).hexdigest() != digest
        or value.get("schema") != PRIVILEGED_RECEIPT_SCHEMA
        or value.get("authority") != "code_owned_privileged_bounded_proc_diagnostic_v1"
        or value.get("observer_euid") != 0
        or value.get("implementation_sha256") != expected_implementation_sha256
        or value.get("host_scope_authority_sha256") != expected_host_scope_authority_sha256
        or value.get("target_index_sha256") != expected_target_index.sha256
        or value.get("target_count") != len(expected_target_index.targets)
        or value.get("status") not in {"clear", "referenced"}
        or not isinstance(referenced, list)
        or referenced != sorted(set(referenced))
        or any(not isinstance(item, str) or item not in known_ids for item in referenced)
        or (value.get("status") == "clear" and referenced)
        or (value.get("status") == "referenced" and not referenced)
        or any(
            not _is_hex_digest(value.get(name))
            for name in (
                "implementation_sha256",
                "host_scope_authority_sha256",
                "kernel_epoch_sha256",
                "observation_sha256",
                "process_epoch_sha256",
                "scope_identity_sha256",
            )
        )
        or type(value.get("task_count")) is not int
        or type(value.get("tgid_count")) is not int
        or value["tgid_count"] <= 0
        or value["task_count"] < value["tgid_count"]
    ):
        raise ProcProbeInputError("privileged_probe_receipt_invalid")
    raw = _canonical_json(dict(receipt)) + b"\n"
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ProcProbeInputError("privileged_probe_receipt_invalid")
    return raw


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _privileged_main() -> int:
    lock_fd = -1
    try:
        if os.geteuid() != 0:
            raise ProcProbeInputError("privileged_probe_authority_invalid")
        lock_fd = os.open(
            INSTALL_LOCK_PATH,
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        lock_status = os.fstat(lock_fd)
        lock_named = os.stat(INSTALL_LOCK_PATH, follow_symlinks=False)
        if (
            not stat.S_ISREG(lock_status.st_mode)
            or lock_status.st_uid != 0
            or lock_status.st_nlink != 1
            or stat.S_IMODE(lock_status.st_mode) != 0o600
            or (lock_status.st_dev, lock_status.st_ino) != (lock_named.st_dev, lock_named.st_ino)
        ):
            raise ProcProbeInputError("privileged_probe_authority_invalid")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProcProbeInputError("privileged_probe_authority_invalid") from exc
        raw = sys.stdin.buffer.read(MAX_PRIVILEGED_INPUT_BYTES + 1)
        if len(raw) > MAX_PRIVILEGED_INPUT_BYTES:
            raise ProcProbeInputError("target_index_limit_exceeded")
        index = parse_target_index_bytes(raw)
        receipt = privileged_target_reference_receipt(index)
        _scope, scope_sha256 = _host_scope_authority()
        sys.stdout.buffer.write(
            canonical_privileged_receipt_bytes(
                receipt,
                expected_target_index=index,
                expected_implementation_sha256=_implementation_sha256(),
                expected_host_scope_authority_sha256=scope_sha256,
            )
        )
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        failure = {
            "schema": PRIVILEGED_RECEIPT_SCHEMA,
            "status": "failed_closed",
        }
        sys.stderr.buffer.write(_canonical_json(failure) + b"\n")
        sys.stderr.buffer.flush()
        return 2
    finally:
        if lock_fd >= 0:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("privileged-target-probe",))
    args = parser.parse_args(argv)
    if args.command == "privileged-target-probe":
        return _privileged_main()
    return 2


__all__ = [
    "ObjectKey",
    "PROBE_AUTHORITY",
    "PROBE_RECEIPT_SCHEMA",
    "PROBE_SCOPE",
    "ProbeTarget",
    "ProcProbeInputError",
    "SameEUIDOpenSnapshot",
    "TARGET_INDEX_SCHEMA",
    "TargetIndex",
    "build_target_index",
    "canonical_privileged_receipt_bytes",
    "canonical_probe_receipt_bytes",
    "canonical_target_index_bytes",
    "parse_target_index_bytes",
    "privileged_target_reference_receipt",
    "probe_namespace_visible_proc_references",
    "snapshot_same_euid_open_files",
]


if __name__ == "__main__":
    raise SystemExit(main())
