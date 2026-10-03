"""Permanent one-shot ledger. No reset, deletion, repair or reuse operation."""
import hashlib
import os
import stat
from dataclasses import dataclass

from canonical import ContractError, canonical_bytes, parse_canonical_object, validate_digest, validate_integer

LEDGER_EFFECTS = ("exclusive_create", "record_write", "file_fsync", "parent_fsync", "reopen")
LEDGER_FAULTS = tuple(side + ":ledger:" + effect for effect in LEDGER_EFFECTS for side in ("pre", "post"))
RECORD_KEYS = frozenset(("schema", "attempt_id", "candidate_commit", "candidate_tree", "attempt_generation",
                         "authority_sha256", "live_grant_sha256", "package_index_sha256",
                         "snapshot_manifest_sha256", "broker_bundle_sha256"))


def _fault(callback, point):
    if callback is not None:
        callback(point)


def descriptor_identity(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def descriptor_mount(fd):
    with open("/proc/self/fdinfo/" + str(fd), "r", encoding="ascii") as source:
        for line in source:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise ContractError("descriptor mount identity unavailable")


def validate_parent(fd, uid, gid):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (uid, gid) or info.st_mode & 0o022:
        raise ContractError("unprotected ledger parent")
    return descriptor_identity(fd), descriptor_mount(fd)


def record_for(authority, authority_sha256, live_grant_sha256):
    for name in ("authority_sha256", "live_grant_sha256"):
        validate_digest(locals()[name])
    for name in ("package_index_sha256", "snapshot_manifest_sha256", "broker_bundle_sha256"):
        validate_digest(authority[name])
    for name in ("candidate_commit", "candidate_tree"):
        value = authority[name]
        if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ContractError("invalid Git identity")
    validate_integer(authority["attempt_generation"], minimum=1)
    # Ledger identity is candidate/generation, independent of snapshot/package changes.
    attempt_id = authority["candidate_commit"] + "-" + str(authority["attempt_generation"])
    return {"schema": "consumed-attempt.v1", "attempt_id": attempt_id,
            **{key: authority[key] for key in ("candidate_commit", "candidate_tree", "attempt_generation",
                "package_index_sha256", "snapshot_manifest_sha256", "broker_bundle_sha256")},
            "authority_sha256": authority_sha256, "live_grant_sha256": live_grant_sha256}


@dataclass(frozen=True)
class ConsumedAttempt:
    dir_fd: int
    record: dict
    identity: tuple
    parent_identity: tuple
    mount: int
    record_sha256: str
    durable: bool
    owner_uid: int
    owner_gid: int
    fresh: bool = True

    def validate(self):
        if not self.durable:
            raise ContractError("consumption durability incomplete")
        if validate_parent(self.dir_fd, self.owner_uid, self.owner_gid) != (self.parent_identity, self.mount):
            raise ContractError("ledger parent substitution")
        before = os.stat("consumed.v1", dir_fd=self.dir_fd, follow_symlinks=False)
        before_identity = (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid, before.st_nlink)
        if before_identity != self.identity or not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1:
            raise ContractError("consumed pathname identity invalid")
        fd = os.open("consumed.v1", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.dir_fd)
        try:
            if descriptor_identity(fd) != self.identity or descriptor_mount(fd) != self.mount:
                raise ContractError("consumed record substitution")
            raw = os.read(fd, 65537)
            parsed = parse_canonical_object(raw, RECORD_KEYS, schema="consumed-attempt.v1", expected_sha256=self.record_sha256)
            opened = os.fstat(fd)
            if parsed != self.record or descriptor_identity(fd) != self.identity or opened.st_size != len(raw):
                raise ContractError("consumed record drift")
            after = os.stat("consumed.v1", dir_fd=self.dir_fd, follow_symlinks=False)
            after_identity = (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_nlink)
            if after_identity != self.identity or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ContractError("consumed pathname changed during read")
        finally:
            os.close(fd)
        return True

    def close(self):
        os.close(self.dir_fd)


def consume_once(ledger_dir_fd, fixed_authority, *, live_grant_sha256, authority_sha256,
                 owner_uid=0, owner_gid=0, fault=None):
    """Create, write, fsync, parent-fsync, reopen; every existing name refuses."""
    parent_identity, mount = validate_parent(ledger_dir_fd, owner_uid, owner_gid)
    record = record_for(fixed_authority, authority_sha256, live_grant_sha256)
    raw = canonical_bytes(record)
    write_fd = None
    _fault(fault, "pre:ledger:exclusive_create")
    try:
        write_fd = os.open("consumed.v1", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o400, dir_fd=ledger_dir_fd)
    except OSError as error:
        raise ContractError("ATTEMPT_ALREADY_CONSUMED_OR_LEDGER_INVALID") from error
    try:
        identity = descriptor_identity(write_fd)
        if (identity[3], identity[4], identity[5]) != (owner_uid, owner_gid, 1) or stat.S_IMODE(identity[2]) != 0o400:
            raise ContractError("new consumed ledger ownership/mode invalid")
        _fault(fault, "post:ledger:exclusive_create")
        _fault(fault, "pre:ledger:record_write")
        offset = 0
        while offset < len(raw):
            amount = os.write(write_fd, raw[offset:])
            if amount <= 0:
                raise ContractError("short ledger write")
            offset += amount
        _fault(fault, "post:ledger:record_write")
        _fault(fault, "pre:ledger:file_fsync")
        os.fsync(write_fd)
        _fault(fault, "post:ledger:file_fsync")
        _fault(fault, "pre:ledger:parent_fsync")
        os.fsync(ledger_dir_fd)
        _fault(fault, "post:ledger:parent_fsync")
        if validate_parent(ledger_dir_fd, owner_uid, owner_gid) != (parent_identity, mount):
            raise ContractError("ledger parent changed during consumption")
        _fault(fault, "pre:ledger:reopen")
        result = ConsumedAttempt(os.dup(ledger_dir_fd), record, identity, parent_identity, mount,
                                 hashlib.sha256(raw).hexdigest(), True, owner_uid, owner_gid)
        try:
            result.validate()
            _fault(fault, "post:ledger:reopen")
        except BaseException:
            result.close()
            raise
        return result
    finally:
        os.close(write_fd)


def load_consumed_for_recovery(ledger_dir_fd, fixed_authority, *, authority_sha256, live_grant_sha256,
                               owner_uid=0, owner_gid=0):
    """Read existing record for terminal recovery only; never live-admissible."""
    parent, mount = validate_parent(ledger_dir_fd, owner_uid, owner_gid)
    expected = record_for(fixed_authority, authority_sha256, live_grant_sha256)
    info = os.stat("consumed.v1", dir_fd=ledger_dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ContractError("ambiguous consumed ledger; recovery remains FAIL")
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)
    result = ConsumedAttempt(os.dup(ledger_dir_fd), expected, identity, parent, mount,
                             hashlib.sha256(canonical_bytes(expected)).hexdigest(), True, owner_uid, owner_gid, False)
    try:
        result.validate()
    except BaseException:
        result.close()
        raise
    return result
