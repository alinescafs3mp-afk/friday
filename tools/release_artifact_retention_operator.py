#!/usr/bin/env python3
"""Apply one exactly reviewed Friday release-retention plan."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import signal
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import immutable_release_operator as release_operator  # noqa: E402
from tools import release_artifact_proc_probe as proc_probe  # noqa: E402
from tools import release_artifact_retention as retention  # noqa: E402
from tools import release_dr_generation_authentication as dr_auth  # noqa: E402
from tools import release_dr_generation_index as dr_index  # noqa: E402

APPLY_JOURNAL_SCHEMA = "friday.release-artifact-retention-apply-journal.v4"
APPLY_RECEIPT_SCHEMA = "friday.release-artifact-retention-apply-receipt.v3"
CONVERGENCE_RECEIPT_SCHEMA = "friday.release-artifact-retention-convergence-receipt.v1"
APPLY_JOURNAL_NAME = "release-artifact-retention-apply.v1.json"
APPLY_RECEIPT_DIRECTORY = "release-artifact-retention-receipts.v1"
APPLY_PLAN_DIRECTORY = "release-artifact-retention-plans.v1"
OBJECT_AUTHORITY_DIRECTORY = "release-artifact-retention-object-authority.v1"
MAX_PLAN_BYTES = 64 << 20
MAX_OBJECT_AUTHORITY_BYTES = retention.MAX_INVENTORY_ENTRIES * 32
MAX_DELETE_ENTRIES = 1_000_000
MAX_EFFECTFUL_BATCHES_PER_INVOCATION = 16
_HEX64 = frozenset("0123456789abcdef")
_RENAME_NOREPLACE = 1
_METADATA_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_METADATA_RDWR_FLAGS = (
    os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)


class RetentionApplyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _InjectedCrash(BaseException):
    pass


_REVIEWED_IDENTITY_KEYS = (
    "allocated_bytes",
    "device",
    "entry_count",
    "filesystem_magic",
    "identity",
    "inode",
    "inventory_sha256",
    "mode",
    "mount_id",
    "nlink",
    "path",
    "recursive_bytes",
    "type",
    "writable_authority_sha256",
)


def _fault(_point: str) -> None:
    """Test-only crash boundary; production deliberately does nothing."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _rename_noreplace(
    source_directory: int,
    source: str,
    target_directory: int,
    target: str,
) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_directory,
            os.fsencode(source),
            target_directory,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise OSError(errno.ENOSYS, "renameat2") from exc
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _body_free_code(value: object) -> str:
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value[0] not in "abcdefghijklmnopqrstuvwxyz"
        or not set(value) <= allowed
    ):
        return "retention_apply_unexpected_failure"
    return value


def _reviewed_identity(
    value: Mapping[str, Any],
    *,
    collection: str,
) -> dict[str, Any]:
    if collection not in {"backup_targets", "targets"} or any(
        key not in value for key in _REVIEWED_IDENTITY_KEYS
    ):
        raise RetentionApplyError("retention_convergence_review_invalid")
    return {
        "collection": collection,
        **{key: value[key] for key in _REVIEWED_IDENTITY_KEYS},
    }


def _reviewed_identity_sha256(value: Mapping[str, Any], *, collection: str) -> str:
    return hashlib.sha256(_canonical(_reviewed_identity(value, collection=collection))).hexdigest()


def _reviewed_candidate_identities(plan: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    paths: set[str] = set()
    for collection in ("targets", "backup_targets"):
        records = plan.get(collection)
        if not isinstance(records, list):
            raise RetentionApplyError("retention_convergence_review_invalid")
        for record in records:
            if not isinstance(record, Mapping):
                raise RetentionApplyError("retention_convergence_review_invalid")
            if (
                record.get("decision") != "delete_candidate"
                and record.get("reason") != "deferred_batch_bound"
            ):
                continue
            normalized = _reviewed_identity(record, collection=collection)
            path = str(normalized["path"])
            if path in paths:
                raise RetentionApplyError("retention_convergence_review_invalid")
            paths.add(path)
            values.append(normalized)
    return tuple(sorted(values, key=lambda item: (str(item["path"]), str(item["collection"]))))


def _reviewed_candidate_set_sha256(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(list(_reviewed_candidate_identities(plan)))).hexdigest()


def _has_deferred_batch_bound(plan: Mapping[str, Any]) -> bool:
    return any(
        isinstance(record, Mapping) and record.get("reason") == "deferred_batch_bound"
        for collection in ("targets", "backup_targets")
        for record in (plan.get(collection) if isinstance(plan.get(collection), list) else ())
    )


def _has_open_only_identity(plan: Mapping[str, Any]) -> bool:
    return any(
        isinstance(record, Mapping) and record.get("reason") == "open_reference"
        for collection in ("targets", "backup_targets")
        for record in (plan.get(collection) if isinstance(plan.get(collection), list) else ())
    )


def _preflight_reviewed_root(
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Reject reviewed roots that can never complete their accepted cycle."""

    if _has_open_only_identity(plan):
        raise RetentionApplyError("retention_convergence_review_unexecutable")
    identities = _reviewed_candidate_identities(plan)
    if any(
        type(identity.get("entry_count")) is not int
        or not 1 <= int(identity["entry_count"]) <= proc_probe.MAX_TARGET_OBJECTS
        for identity in identities
    ):
        raise RetentionApplyError("retention_convergence_review_unexecutable")
    return identities


def _read_reviewed_plan(
    path: Path,
    *,
    expected_sha256: str,
    allow_recoverable_two_link: bool = False,
) -> dict[str, Any]:
    if not _is_hex64(expected_sha256):
        raise RetentionApplyError("retention_apply_plan_digest_invalid")
    try:
        raw = retention._stable_file_bytes(  # noqa: SLF001
            path,
            private=True,
            code="retention_apply_plan_invalid",
            maximum_bytes=MAX_PLAN_BYTES,
            allowed_nlinks=(frozenset({1, 2}) if allow_recoverable_two_link else frozenset({1})),
        )
        value = retention._unique_json(raw, code="retention_apply_plan_invalid")  # noqa: SLF001
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_plan_invalid") from exc
    if raw != _canonical(value) + b"\n" or value.get("schema") != retention.PLAN_SCHEMA:
        raise RetentionApplyError("retention_apply_plan_invalid")
    supplied = value.get("plan_sha256")
    core = {name: item for name, item in value.items() if name != "plan_sha256"}
    if (
        supplied != expected_sha256
        or hashlib.sha256(_canonical(core)).hexdigest() != expected_sha256
        or value.get("mode") != "eligible_classification"
        or value.get("classification_status") != "eligible"
        or value.get("block_reason") != ""
        or value.get("effect_authority")
        != {
            "bounded_contour": retention.BOUNDED_DELETE_CONTOUR,
            "concurrent_open_attempts_excluded": True,
            "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
            "global_operator_lock": True,
            "per_regular_file_write_lease": True,
            "privileged_probe_role": "diagnostic_prerequisite",
            "sealed_quarantine_mode": "0700",
            "threat_boundary": retention.THREAT_BOUNDARY,
            "unique_mount_identity": True,
            "universal_absence_proof": False,
        }
    ):
        raise RetentionApplyError("retention_apply_plan_digest_mismatch")
    return value


def _read_plan(
    path: Path,
    *,
    expected_sha256: str,
    allow_recoverable_two_link: bool = False,
) -> dict[str, Any]:
    value = _read_reviewed_plan(
        path,
        expected_sha256=expected_sha256,
        allow_recoverable_two_link=allow_recoverable_two_link,
    )
    candidates = _candidate_records(value)
    open_inventory = value.get("open_inventory")
    source = open_inventory.get("source") if isinstance(open_inventory, Mapping) else None
    if (
        candidates
        and (
            value.get("apply_authority") is not True
            or source
            not in {
                "code_owned_privileged_target_proc_v1",
                "code_owned_privileged_target_diagnostic_v1",
            }
        )
    ) or (
        not candidates
        and not (
            (value.get("apply_authority") is False and source == "code_owned_no_delete_candidates_v1")
            or (
                value.get("apply_authority") is True
                and source
                in {
                    "code_owned_privileged_target_proc_v1",
                    "code_owned_privileged_target_diagnostic_v1",
                }
            )
        )
    ):
        raise RetentionApplyError("retention_apply_plan_digest_mismatch")
    return value


def _plan_inputs(plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        activation = plan["activation_journal"]
        unit = plan["unit_install_journal"]
        authority = plan["authority_bindings"]
        evidence_raw = authority["canonical_evidence_roots"]
        inventory_raw = plan["inventory_roots"]
        backup_inventory_raw = plan["backup_inventory_roots"]
        scratch_raw = plan["reviewed_scratch_targets"]
        evidence = tuple(
            retention.CanonicalEvidenceRoot(
                path=Path(item["path"]),
                authority_path=Path(item["authority_path"]),
                authority_sha256=item["authority_sha256"],
            )
            for item in evidence_raw
        )
        reviewed_scratch = tuple(
            retention.ReviewedScratchTarget(
                path=Path(item["path"]),
                inventory_sha256=item["inventory_sha256"],
                contour=item["contour"],
            )
            for item in scratch_raw
        )
        result: dict[str, Any] = {
            "activation_journal": Path(activation["path"]),
            "unit_journal": Path(unit["path"]),
            "backup_root": Path(plan["backup_root"]),
            "inventory_roots": tuple(Path(item["path"]) for item in inventory_raw),
            "backup_inventory_roots": tuple(Path(item["path"]) for item in backup_inventory_raw),
            "canonical_evidence_roots": evidence,
            "reviewed_scratch_targets": reviewed_scratch,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RetentionApplyError("retention_apply_plan_invalid") from exc
    if not result["inventory_roots"] or not result["canonical_evidence_roots"]:
        raise RetentionApplyError("retention_apply_plan_invalid")
    try:
        scope = retention.load_retention_scope_authority(
            activation_journal=result["activation_journal"],
        )
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_retention_scope_changed") from exc
    supplied_evidence = tuple(
        sorted(
            result["canonical_evidence_roots"],
            key=lambda item: (str(item.path), str(item.authority_path), item.authority_sha256),
        )
    )
    if (
        plan.get("retention_scope") != scope.receipt
        or result["backup_root"] != scope.backup_root
        or tuple(sorted(result["inventory_roots"], key=str)) != scope.inventory_roots
        or tuple(sorted(result["backup_inventory_roots"], key=str)) != scope.backup_inventory_roots
        or supplied_evidence != scope.canonical_evidence_roots
    ):
        raise RetentionApplyError("retention_apply_retention_scope_changed")
    return result


def _authority_projection(plan: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "activation_journal",
        "unit_install_journal",
        "authority_bindings",
        "open_inventory",
        "activation_backup",
        "backup_root",
        "inventory_roots",
        "backup_inventory_roots",
        "reviewed_scratch_targets",
        "protected_releases",
        "targets",
        "backup_targets",
        "classification_status",
        "block_reason",
        "apply_authority",
        "effect_authority",
        "mode",
        "scope",
        "retention_scope",
    )
    try:
        return {key: plan[key] for key in keys}
    except KeyError as exc:
        raise RetentionApplyError("retention_apply_plan_invalid") from exc


def _cycle_authority_projection(plan: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "activation_journal",
        "unit_install_journal",
        "authority_bindings",
        "backup_root",
        "inventory_roots",
        "backup_inventory_roots",
        "reviewed_scratch_targets",
        "protected_releases",
        "retention_scope",
    )
    try:
        return {key: plan[key] for key in keys}
    except KeyError as exc:
        raise RetentionApplyError("retention_apply_plan_invalid") from exc


def _candidate_records(plan: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    roots: dict[Path, tuple[int, int, int, int, str]] = {}
    for key in ("inventory_roots", "backup_inventory_roots"):
        raw_roots = plan.get(key)
        if not isinstance(raw_roots, list):
            raise RetentionApplyError("retention_apply_plan_invalid")
        for raw in raw_roots:
            if not isinstance(raw, Mapping):
                raise RetentionApplyError("retention_apply_plan_invalid")
            path = Path(str(raw.get("path") or ""))
            device = raw.get("device")
            inode = raw.get("inode")
            mount_id = raw.get("mount_id")
            filesystem_magic = raw.get("filesystem_magic")
            writable_authority = raw.get("writable_authority_sha256")
            if (
                not path.is_absolute()
                or type(device) is not int
                or type(inode) is not int
                or inode <= 0
                or type(mount_id) is not int
                or mount_id <= 0
                or type(filesystem_magic) is not int
                or filesystem_magic not in retention._SUPPORTED_FILESYSTEM_MAGICS  # noqa: SLF001
                or not _is_hex64(writable_authority)
                or path in roots
            ):
                raise RetentionApplyError("retention_apply_plan_invalid")
            roots[path] = (device, inode, mount_id, filesystem_magic, str(writable_authority))
    candidates: list[dict[str, Any]] = []
    paths: set[Path] = set()
    for key in ("targets", "backup_targets"):
        raw_targets = plan.get(key)
        if not isinstance(raw_targets, list):
            raise RetentionApplyError("retention_apply_plan_invalid")
        for raw in raw_targets:
            if not isinstance(raw, Mapping) or raw.get("decision") != "delete_candidate":
                continue
            path = Path(str(raw.get("path") or ""))
            root = path.parent
            if (
                root not in roots
                or path in paths
                or path.name in {"", ".", ".."}
                or raw.get("type") != "directory"
                or type(raw.get("device")) is not int
                or type(raw.get("inode")) is not int
                or type(raw.get("mount_id")) is not int
                or int(raw["mount_id"]) <= 0
                or type(raw.get("filesystem_magic")) is not int
                or raw.get("filesystem_magic") not in retention._SUPPORTED_FILESYSTEM_MAGICS  # noqa: SLF001
                or type(raw.get("mode")) is not int
                or not stat.S_ISDIR(int(raw["mode"]))
                or not _is_hex64(raw.get("writable_authority_sha256"))
                or type(raw.get("recursive_bytes")) is not int
                or type(raw.get("allocated_bytes")) is not int
                or type(raw.get("entry_count")) is not int
                or not _is_hex64(raw.get("inventory_sha256"))
                or not isinstance(raw.get("identity"), Mapping)
            ):
                raise RetentionApplyError("retention_apply_plan_invalid")
            paths.add(path)
            candidate = dict(raw)
            candidate["root_device"] = roots[root][0]
            candidate["root_inode"] = roots[root][1]
            candidate["root_mount_id"] = roots[root][2]
            candidate["root_filesystem_magic"] = roots[root][3]
            candidate["root_writable_authority_sha256"] = roots[root][4]
            candidate["candidate_sha256"] = hashlib.sha256(_canonical(dict(raw))).hexdigest()
            candidates.append(candidate)
            if len(candidates) > retention.MAX_DELETE_CANDIDATES_PER_PLAN:
                raise RetentionApplyError("retention_apply_candidate_bound_exceeded")
    candidates.sort(key=lambda item: str(item["path"]))
    return tuple(candidates)


_CYCLE_CONTEXT_KEYS = frozenset(
    {
        "accepted_root_plan_path",
        "accepted_root_plan_sha256",
        "batch_ordinal",
        "cycle_sha256",
        "previous_receipt_sha256",
        "retention_epoch_sha256",
        "reviewed_full_candidate_set_sha256",
    }
)


def _cycle_identity_core(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accepted_root_plan_path": value["accepted_root_plan_path"],
        "accepted_root_plan_sha256": value["accepted_root_plan_sha256"],
        "retention_epoch_sha256": value["retention_epoch_sha256"],
        "reviewed_full_candidate_set_sha256": value["reviewed_full_candidate_set_sha256"],
        "schema": CONVERGENCE_RECEIPT_SCHEMA,
    }


def _normalize_cycle_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CYCLE_CONTEXT_KEYS:
        raise RetentionApplyError("retention_apply_cycle_invalid")
    accepted_path = Path(str(value.get("accepted_root_plan_path") or ""))
    ordinal = value.get("batch_ordinal")
    previous = value.get("previous_receipt_sha256")
    normalized = {
        "accepted_root_plan_path": str(accepted_path),
        "accepted_root_plan_sha256": value.get("accepted_root_plan_sha256"),
        "batch_ordinal": ordinal,
        "cycle_sha256": value.get("cycle_sha256"),
        "previous_receipt_sha256": previous,
        "retention_epoch_sha256": value.get("retention_epoch_sha256"),
        "reviewed_full_candidate_set_sha256": value.get("reviewed_full_candidate_set_sha256"),
    }
    if (
        not accepted_path.is_absolute()
        or accepted_path != Path(os.path.abspath(accepted_path))
        or type(ordinal) is not int
        or not 0 <= int(ordinal) <= MAX_DELETE_ENTRIES
        or (ordinal == 0 and previous != "")
        or (ordinal > 0 and not _is_hex64(previous))
        or any(
            not _is_hex64(normalized[key])
            for key in (
                "accepted_root_plan_sha256",
                "cycle_sha256",
                "retention_epoch_sha256",
                "reviewed_full_candidate_set_sha256",
            )
        )
        or hashlib.sha256(_canonical(_cycle_identity_core(normalized))).hexdigest()
        != normalized["cycle_sha256"]
    ):
        raise RetentionApplyError("retention_apply_cycle_invalid")
    return normalized


def _new_cycle_context(
    *,
    accepted_root_plan_path: Path,
    accepted_root_plan_sha256: str,
    reviewed_full_candidate_set_sha256: str,
    retention_epoch_sha256: str,
    batch_ordinal: int,
    previous_receipt_sha256: str,
) -> dict[str, Any]:
    core = {
        "accepted_root_plan_path": str(Path(os.path.abspath(accepted_root_plan_path))),
        "accepted_root_plan_sha256": accepted_root_plan_sha256,
        "retention_epoch_sha256": retention_epoch_sha256,
        "reviewed_full_candidate_set_sha256": reviewed_full_candidate_set_sha256,
        "schema": CONVERGENCE_RECEIPT_SCHEMA,
    }
    return _normalize_cycle_context(
        {
            **{key: item for key, item in core.items() if key != "schema"},
            "batch_ordinal": batch_ordinal,
            "cycle_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
            "previous_receipt_sha256": previous_receipt_sha256,
        }
    )


def _standalone_cycle_context(
    plan: Mapping[str, Any],
    *,
    accepted_path: Path,
) -> dict[str, Any]:
    authority = plan.get("authority_bindings")
    epoch_material = {
        "authority_bindings_sha256": (
            authority.get("bindings_sha256") if isinstance(authority, Mapping) else ""
        ),
        "plan_sha256": plan.get("plan_sha256"),
        "retention_scope": plan.get("retention_scope"),
    }
    reviewed_set_sha256 = (
        _reviewed_candidate_set_sha256(plan)
        if all(isinstance(plan.get(key), list) for key in ("targets", "backup_targets"))
        else hashlib.sha256(_canonical([])).hexdigest()
    )
    return _new_cycle_context(
        accepted_root_plan_path=accepted_path,
        accepted_root_plan_sha256=str(plan["plan_sha256"]),
        reviewed_full_candidate_set_sha256=reviewed_set_sha256,
        retention_epoch_sha256=hashlib.sha256(_canonical(epoch_material)).hexdigest(),
        batch_ordinal=0,
        previous_receipt_sha256="",
    )


def _cycle_context_from_record(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = {key: value[key] for key in _CYCLE_CONTEXT_KEYS}
    except KeyError as exc:
        raise RetentionApplyError("retention_apply_cycle_invalid") from exc
    return _normalize_cycle_context(raw)


def _same_cycle(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    left = _normalize_cycle_context(first)
    right = _normalize_cycle_context(second)
    return _cycle_identity_core(left) == _cycle_identity_core(right)


def _tree_material(path: Path) -> tuple[Any, str, str]:
    try:
        snapshot = retention._snapshot(path)  # noqa: SLF001
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_target_raced") from exc
    objects = sorted({(record[1], record[2], record[9]) for record in snapshot.records})
    normalized: list[list[Any]] = []
    for record in snapshot.records:
        values = list(record)
        if values[0] == ".":
            values[7:9] = [0, 0]
        normalized.append(values)
    return (
        snapshot,
        hashlib.sha256(_canonical(objects)).hexdigest(),
        hashlib.sha256(_canonical(normalized)).hexdigest(),
    )


def _candidate_matches_observation(candidate: Mapping[str, Any], path: Path) -> tuple[Any, str, str]:
    observed = retention._observe_target(path)  # noqa: SLF001
    if (
        observed.raced
        or observed.device != candidate["device"]
        or observed.inode != candidate["inode"]
        or observed.mount_id != candidate["mount_id"]
        or observed.filesystem_magic != candidate["filesystem_magic"]
        or observed.mode != candidate["mode"]
        or observed.writable_authority_sha256 != candidate["writable_authority_sha256"]
        or observed.kind != "directory"
        or observed.total_bytes != candidate["recursive_bytes"]
        or observed.total_allocated_bytes != candidate["allocated_bytes"]
        or observed.entry_count != candidate["entry_count"]
        or observed.inventory_sha256 != candidate["inventory_sha256"]
        or not observed.owner_ok
        or observed.has_symlink
        or observed.has_special
        or observed.has_hardlink
        or observed.has_group_world_writable
    ):
        raise RetentionApplyError("retention_apply_target_raced")
    snapshot, objects_sha256, tree_sha256 = _tree_material(path)
    return snapshot, objects_sha256, tree_sha256


def _residual_record_digest(record: Sequence[Any]) -> bytes:
    relative, device, inode, mode, nlink, uid, size, mtime, _ctime, mount_id, gid, flags = record
    kind = stat.S_IFMT(int(mode))
    permissions = stat.S_IMODE(int(mode))
    if relative == ".":
        permissions = 0o700
    elif kind == stat.S_IFREG:
        permissions = (permissions & 0o700) | 0o600
    projection: list[Any] = [
        relative,
        int(device),
        int(inode),
        kind,
        permissions,
        int(uid),
        int(gid),
        int(mount_id),
        int(flags),
    ]
    if kind == stat.S_IFREG:
        if int(nlink) != 1:
            raise RetentionApplyError("retention_apply_lease_unavailable")
        projection.extend([int(nlink), int(size), int(mtime)])
    elif kind != stat.S_IFDIR:
        raise RetentionApplyError("retention_apply_child_unsafe")
    return hashlib.sha256(_canonical(projection)).digest()


def _residual_authority_payload(snapshot: Any) -> bytes:
    digests = sorted(_residual_record_digest(record) for record in snapshot.records)
    if not digests or len(digests) != len(set(digests)):
        raise RetentionApplyError("retention_apply_residual_authority_invalid")
    payload = b"".join(digests)
    if len(payload) > MAX_OBJECT_AUTHORITY_BYTES:
        raise RetentionApplyError("retention_apply_residual_authority_invalid")
    return payload


def _object_authority_directory(state_dir: Path) -> Path:
    path = state_dir / OBJECT_AUTHORITY_DIRECTORY
    try:
        with suppress(FileExistsError):
            path.mkdir(mode=0o700)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            status = os.fstat(descriptor)
            if (
                status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) != 0o700
                or retention._descriptor_has_posix_acl(descriptor)  # noqa: SLF001
            ):
                raise RetentionApplyError("retention_apply_residual_authority_invalid")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        parent = os.open(
            state_dir,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        retention._strict_private_directory(  # noqa: SLF001
            path,
            code="retention_apply_residual_authority_invalid",
        )
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_residual_authority_invalid") from exc
    return path


def _persist_residual_authority(
    state_dir: Path,
    transaction_id: str,
    index: int,
    snapshot: Any,
    *,
    guard: Callable[[], None],
) -> dict[str, Any]:
    payload = _residual_authority_payload(snapshot)
    digest = hashlib.sha256(payload).hexdigest()
    directory = _object_authority_directory(state_dir)
    path = directory / f"objects-{transaction_id}-{index:06d}.bin"
    guard()
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            stages = sorted(
                name
                for name in os.listdir(directory_fd)
                if name.startswith(f".{path.name}.") and name.endswith(".new")
            )
            if len(stages) > 32:
                raise RetentionApplyError("retention_apply_residual_authority_invalid")
            try:
                final_status = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                final_status = None
            for name in stages:
                staged = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                safe_stage = (
                    stat.S_ISREG(staged.st_mode)
                    and staged.st_uid == os.geteuid()
                    and stat.S_IMODE(staged.st_mode) == 0o600
                    and staged.st_nlink == 1
                )
                linked_final = (
                    final_status is not None
                    and staged.st_nlink == 2
                    and (staged.st_dev, staged.st_ino) == (final_status.st_dev, final_status.st_ino)
                )
                if not safe_stage and not linked_final:
                    raise RetentionApplyError("retention_apply_residual_authority_invalid")
                guard()
                os.unlink(name, dir_fd=directory_fd)
                guard()
            if stages:
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if not path.exists() and not path.is_symlink():
            guard()
            retention._write_atomic(path, payload)  # noqa: SLF001
            guard()
        observed = retention._stable_file_bytes(  # noqa: SLF001
            path,
            private=True,
            code="retention_apply_residual_authority_invalid",
            maximum_bytes=MAX_OBJECT_AUTHORITY_BYTES,
        )
        if observed != payload:
            raise RetentionApplyError("retention_apply_residual_authority_invalid")
        os.chmod(path, 0o400)
        descriptor = os.open(path, _METADATA_READ_FLAGS)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise RetentionApplyError("retention_apply_residual_authority_invalid")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, retention.RetentionPlanError) as exc:
        if isinstance(exc, RetentionApplyError):
            raise
        raise RetentionApplyError("retention_apply_residual_authority_invalid") from exc
    guard()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o400
    ):
        raise RetentionApplyError("retention_apply_residual_authority_invalid")
    return {
        "count": len(payload) // 32,
        "device": int(status.st_dev),
        "inode": int(status.st_ino),
        "path": str(path),
        "sha256": digest,
    }


def _load_residual_authority(
    binding: object,
    *,
    state_dir: Path,
) -> frozenset[bytes]:
    if not isinstance(binding, Mapping) or set(binding) != {"count", "device", "inode", "path", "sha256"}:
        raise RetentionApplyError("retention_apply_residual_authority_invalid")
    path = Path(str(binding.get("path") or ""))
    expected_directory = state_dir / OBJECT_AUTHORITY_DIRECTORY
    if (
        path.parent != expected_directory
        or not path.name.startswith("objects-")
        or type(binding.get("count")) is not int
        or not 1 <= int(binding["count"]) <= retention.MAX_INVENTORY_ENTRIES
        or type(binding.get("device")) is not int
        or type(binding.get("inode")) is not int
        or int(binding["inode"]) <= 0
        or not _is_hex64(binding.get("sha256"))
    ):
        raise RetentionApplyError("retention_apply_residual_authority_invalid")
    try:
        raw = retention._stable_file_bytes(  # noqa: SLF001
            path,
            private=True,
            code="retention_apply_residual_authority_invalid",
            maximum_bytes=MAX_OBJECT_AUTHORITY_BYTES,
        )
        status = os.lstat(path)
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_residual_authority_invalid") from exc
    chunks = tuple(raw[offset : offset + 32] for offset in range(0, len(raw), 32))
    if (
        len(raw) != int(binding["count"]) * 32
        or hashlib.sha256(raw).hexdigest() != binding["sha256"]
        or chunks != tuple(sorted(set(chunks)))
        or (status.st_dev, status.st_ino) != (binding["device"], binding["inode"])
        or not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o400
    ):
        raise RetentionApplyError("retention_apply_residual_authority_invalid")
    return frozenset(chunks)


def _quarantine_matches(
    candidate: Mapping[str, Any],
    path: Path,
    *,
    objects_sha256: str,
    tree_sha256: str,
) -> Any:
    observed = retention._observe_target(path)  # noqa: SLF001
    snapshot, observed_objects, observed_tree = _tree_material(path)
    if (
        observed.raced
        or observed.device != candidate["device"]
        or observed.inode != candidate["inode"]
        or observed.mount_id != candidate["mount_id"]
        or observed.filesystem_magic != candidate["filesystem_magic"]
        or observed.mode != candidate["mode"]
        or observed.writable_authority_sha256 != candidate["writable_authority_sha256"]
        or observed.kind != "directory"
        or observed.total_bytes != candidate["recursive_bytes"]
        or observed.total_allocated_bytes != candidate["allocated_bytes"]
        or observed.entry_count != candidate["entry_count"]
        or not observed.owner_ok
        or observed.has_symlink
        or observed.has_special
        or observed.has_hardlink
        or observed.has_group_world_writable
        or observed_objects != objects_sha256
        or observed_tree != tree_sha256
    ):
        raise RetentionApplyError("retention_apply_quarantine_changed")
    return snapshot


def _normalized_tree_sha256(snapshot: Any, *, root_mode: int | None = None) -> str:
    normalized: list[list[Any]] = []
    for record in snapshot.records:
        values = list(record)
        if values[0] == ".":
            values[7:9] = [0, 0]
            if root_mode is not None:
                values[3] = root_mode
        normalized.append(values)
    return hashlib.sha256(_canonical(normalized)).hexdigest()


def _sealed_quarantine_matches(
    candidate: Mapping[str, Any],
    path: Path,
    *,
    objects_sha256: str,
    tree_sha256: str,
    sealed_tree_sha256: str | None = None,
) -> tuple[Any, str]:
    observed = retention._observe_target(path)  # noqa: SLF001
    snapshot, observed_objects, observed_tree = _tree_material(path)
    if (
        observed.raced
        or observed.device != candidate["device"]
        or observed.inode != candidate["inode"]
        or observed.mount_id != candidate["mount_id"]
        or observed.filesystem_magic != candidate["filesystem_magic"]
        or observed.kind != "directory"
        or observed.mode is None
        or stat.S_IMODE(observed.mode) != 0o700
        or observed.total_bytes != candidate["recursive_bytes"]
        or observed.total_allocated_bytes != candidate["allocated_bytes"]
        or observed.entry_count != candidate["entry_count"]
        or not observed.owner_ok
        or observed.has_symlink
        or observed.has_special
        or observed.has_hardlink
        or observed.has_group_world_writable
        or observed_objects != objects_sha256
        or _normalized_tree_sha256(snapshot, root_mode=int(candidate["mode"])) != tree_sha256
        or sealed_tree_sha256 is not None
        and observed_tree != sealed_tree_sha256
    ):
        raise RetentionApplyError("retention_apply_quarantine_changed")
    return snapshot, observed_tree


def _seal_quarantine(
    candidate: Mapping[str, Any],
    quarantine_name: str,
    *,
    objects_sha256: str,
    tree_sha256: str,
    guard: Callable[[], None],
) -> str:
    quarantine = Path(str(candidate["path"])).parent / quarantine_name
    try:
        _quarantine_matches(
            candidate,
            quarantine,
            objects_sha256=objects_sha256,
            tree_sha256=tree_sha256,
        )
    except RetentionApplyError:
        _snapshot, sealed_tree = _sealed_quarantine_matches(
            candidate,
            quarantine,
            objects_sha256=objects_sha256,
            tree_sha256=tree_sha256,
        )
        return sealed_tree
    root_fd, _parts, _identities = _root_descriptor(candidate)
    child_fd = -1
    try:
        child_fd = os.open(
            quarantine_name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        opened = os.fstat(child_fd)
        if (
            (opened.st_dev, opened.st_ino) != (candidate["device"], candidate["inode"])
            or retention._descriptor_mount_id(child_fd) != candidate["mount_id"]  # noqa: SLF001
        ):
            raise RetentionApplyError("retention_apply_quarantine_changed")
        guard()
        os.fchmod(child_fd, 0o700)
        os.fsync(child_fd)
        os.fsync(root_fd)
        guard()
    except OSError as exc:
        raise RetentionApplyError("retention_apply_quarantine_changed") from exc
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(root_fd)
    _snapshot, sealed_tree = _sealed_quarantine_matches(
        candidate,
        quarantine,
        objects_sha256=objects_sha256,
        tree_sha256=tree_sha256,
    )
    return sealed_tree


def _partial_quarantine_contour(
    candidate: Mapping[str, Any],
    path: Path,
    *,
    residual_authority: frozenset[bytes],
) -> tuple[Any, str]:
    observed = retention._observe_target(path)  # noqa: SLF001
    snapshot, _objects, tree = _tree_material(path)
    if (
        observed.raced
        or observed.device != candidate["device"]
        or observed.inode != candidate["inode"]
        or observed.mount_id != candidate["mount_id"]
        or observed.filesystem_magic != candidate["filesystem_magic"]
        or observed.kind != "directory"
        or observed.mode is None
        or stat.S_IMODE(observed.mode) != 0o700
        or observed.total_bytes is None
        or observed.total_bytes > candidate["recursive_bytes"]
        or observed.total_allocated_bytes is None
        or observed.total_allocated_bytes > candidate["allocated_bytes"]
        or observed.entry_count is None
        or observed.entry_count > candidate["entry_count"]
        or not observed.owner_ok
        or observed.has_symlink
        or observed.has_special
        or observed.has_hardlink
        or observed.has_group_world_writable
    ):
        raise RetentionApplyError("retention_apply_partial_state_invalid")
    residual = tuple(_residual_record_digest(record) for record in snapshot.records)
    if len(residual) != len(set(residual)) or not set(residual).issubset(residual_authority):
        raise RetentionApplyError("retention_apply_partial_state_invalid")
    return snapshot, tree


def _live_authority_reauthenticate(plan: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
    try:
        scope = retention.load_retention_scope_authority(
            activation_journal=inputs["activation_journal"],
        )
        bindings = retention.build_retention_authority_bindings(
            activation_journal=inputs["activation_journal"],
            unit_journal=inputs["unit_journal"],
            canonical_evidence_roots=inputs["canonical_evidence_roots"],
        )
        authority = retention._normalize_authority_bindings(  # noqa: SLF001
            bindings,
            activation_sha256=bindings.activation_journal_sha256,
            unit_sha256=bindings.unit_install_journal_sha256,
            state_directory=Path(inputs["activation_journal"]).parent,
        )
    except (retention.RetentionPlanError, KeyError, TypeError) as exc:
        raise RetentionApplyError("retention_apply_live_authority_changed") from exc
    if (
        scope.receipt != plan.get("retention_scope")
        or scope.backup_root != inputs["backup_root"]
        or scope.inventory_roots != tuple(sorted(inputs["inventory_roots"], key=str))
        or scope.backup_inventory_roots != tuple(sorted(inputs["backup_inventory_roots"], key=str))
        or scope.canonical_evidence_roots
        != tuple(
            sorted(
                inputs["canonical_evidence_roots"],
                key=lambda item: (str(item.path), str(item.authority_path), item.authority_sha256),
            )
        )
        or bindings.activation_journal_sha256 != plan["activation_journal"]["sha256"]
        or bindings.unit_install_journal_sha256 != plan["unit_install_journal"]["sha256"]
        or authority.error
        or authority.receipt != plan["authority_bindings"]
    ):
        raise RetentionApplyError("retention_apply_live_authority_changed")
    if not authority.delete_authority_eligible:
        raise RetentionApplyError("retention_apply_dr_rollback_release_evidence_incomplete")


def _resume_candidate_reauthenticate(
    candidates: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    *,
    state_dir: Path,
) -> None:
    for candidate, entry in zip(candidates, entries, strict=True):
        status = entry["status"]
        if status == "deleted":
            continue
        source = Path(str(candidate["path"]))
        quarantine = source.parent / str(entry["quarantine_name"])
        if status == "pending":
            _candidate_matches_observation(candidate, source)
        elif status == "renaming":
            if source.exists() and not source.is_symlink():
                _candidate_matches_observation(candidate, source)
            elif quarantine.exists() and not quarantine.is_symlink():
                try:
                    _quarantine_matches(
                        candidate,
                        quarantine,
                        objects_sha256=str(entry["objects_sha256"]),
                        tree_sha256=str(entry["tree_sha256"]),
                    )
                except RetentionApplyError:
                    _sealed_quarantine_matches(
                        candidate,
                        quarantine,
                        objects_sha256=str(entry["objects_sha256"]),
                        tree_sha256=str(entry["tree_sha256"]),
                    )
            else:
                raise RetentionApplyError("retention_apply_target_raced") from None
        elif status == "sealed":
            _sealed_quarantine_matches(
                candidate,
                quarantine,
                objects_sha256=str(entry["objects_sha256"]),
                tree_sha256=str(entry["tree_sha256"]),
                sealed_tree_sha256=str(entry["sealed_tree_sha256"]),
            )
        elif status == "deleting":
            if quarantine.exists() and not quarantine.is_symlink():
                residual_authority = _load_residual_authority(
                    entry.get("residual_authority"),
                    state_dir=state_dir,
                )
                _partial_quarantine_contour(
                    candidate,
                    quarantine,
                    residual_authority=residual_authority,
                )
            elif source.exists() or source.is_symlink():
                raise RetentionApplyError("retention_apply_partial_state_invalid")
        else:
            raise RetentionApplyError("retention_apply_journal_invalid")


def _journal_core(value: Mapping[str, Any]) -> dict[str, Any]:
    core = dict(value)
    supplied = core.pop("journal_sha256", None)
    if (
        value.get("schema") != APPLY_JOURNAL_SCHEMA
        or not _is_hex64(supplied)
        or hashlib.sha256(_canonical(core)).hexdigest() != supplied
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    return core


def _load_journal(path: Path) -> dict[str, Any] | None:
    try:
        raw = retention._stable_file_bytes(  # noqa: SLF001
            path,
            private=True,
            code="retention_apply_journal_invalid",
        )
    except retention.RetentionPlanError as exc:
        if not path.exists() and not path.is_symlink():
            return None
        raise RetentionApplyError("retention_apply_journal_invalid") from exc
    try:
        value = retention._unique_json(raw, code="retention_apply_journal_invalid")  # noqa: SLF001
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_journal_invalid") from exc
    if raw != _canonical(value) + b"\n":
        raise RetentionApplyError("retention_apply_journal_invalid")
    _journal_core(value)
    return value


def _write_journal(
    path: Path,
    core: Mapping[str, Any],
    *,
    guard: Callable[[], None],
) -> dict[str, Any]:
    payload = {
        **dict(core),
        "journal_sha256": hashlib.sha256(_canonical(dict(core))).hexdigest(),
    }
    raw = _canonical(payload) + b"\n"
    directory_fd = -1
    temporary = f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.new"
    try:
        guard()
        directory_fd, parts, identities = retention._open_absolute_directory_chain(  # noqa: SLF001
            path.parent,
            code="retention_apply_journal_invalid",
        )
        retention._require_pinned_directory(  # noqa: SLF001
            directory_fd,
            parts,
            identities,
            code="retention_apply_journal_invalid",
            private=True,
        )
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        guard()
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = ""
        os.fsync(directory_fd)
        guard()
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_journal_invalid") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
    return payload


def _root_descriptor(candidate: Mapping[str, Any]) -> tuple[int, tuple[str, ...], tuple[Any, ...]]:
    root = Path(str(candidate["path"])).parent
    try:
        descriptor, parts, identities = retention._open_absolute_directory_chain(  # noqa: SLF001
            root,
            code="retention_apply_root_changed",
        )
        held = retention._require_pinned_directory(  # noqa: SLF001
            descriptor,
            parts,
            identities,
            code="retention_apply_root_changed",
            private=False,
        )
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_root_changed") from exc
    if (
        (held.st_dev, held.st_ino) != (candidate["root_device"], candidate["root_inode"])
        or retention._descriptor_mount_id(descriptor) != candidate["root_mount_id"]  # noqa: SLF001
        or retention._descriptor_filesystem_magic(descriptor)  # noqa: SLF001
        != candidate["root_filesystem_magic"]
        or retention._writable_mode_authority(  # noqa: SLF001
            held,
            has_acl=retention._descriptor_has_posix_acl(descriptor),  # noqa: SLF001
        )
        != candidate["root_writable_authority_sha256"]
    ):
        os.close(descriptor)
        raise RetentionApplyError("retention_apply_root_changed")
    return descriptor, parts, identities


def _named_identity(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RetentionApplyError("retention_apply_target_raced") from exc
    return int(status.st_dev), int(status.st_ino)


def _restore_quarantine(
    candidate: Mapping[str, Any],
    quarantine_name: str,
    *,
    guard: Callable[[], None],
) -> None:
    guard()
    descriptor, _parts, _identities = _root_descriptor(candidate)
    quarantine_fd = -1
    source_name = Path(str(candidate["path"])).name
    expected = (candidate["device"], candidate["inode"])
    try:
        if _named_identity(descriptor, source_name) is not None:
            raise RetentionApplyError("retention_apply_restore_blocked")
        if _named_identity(descriptor, quarantine_name) != expected:
            raise RetentionApplyError("retention_apply_restore_blocked")
        quarantine_fd = os.open(
            quarantine_name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        if (
            (os.fstat(quarantine_fd).st_dev, os.fstat(quarantine_fd).st_ino) != expected
            or retention._descriptor_mount_id(quarantine_fd) != candidate["mount_id"]  # noqa: SLF001
        ):
            raise RetentionApplyError("retention_apply_restore_blocked")
        os.fchmod(quarantine_fd, stat.S_IMODE(int(candidate["mode"])))
        os.fsync(quarantine_fd)
        guard()
        _rename_noreplace(descriptor, quarantine_name, descriptor, source_name)
        os.fsync(descriptor)
        guard()
    except OSError as exc:
        raise RetentionApplyError("retention_apply_restore_blocked") from exc
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)
        os.close(descriptor)


def _restore_full_batch(
    candidates: Sequence[Mapping[str, Any]],
    core: dict[str, Any],
    *,
    journal_path: Path,
    guard: Callable[[], None],
) -> dict[str, Any]:
    entries = core.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) or entry.get("status") in {"deleting", "deleted"} for entry in entries
    ):
        raise RetentionApplyError("retention_apply_batch_restore_unavailable")
    by_candidate = {str(entry["candidate_sha256"]): entry for entry in entries}
    for candidate in reversed(candidates):
        entry = by_candidate[str(candidate["candidate_sha256"])]
        if entry["status"] not in {"renaming", "sealed"}:
            continue
        if entry["status"] == "sealed":
            entry["status"] = "renaming"
            entry["sealed_tree_sha256"] = ""
            journal = _write_journal(journal_path, core, guard=guard)
            core = _journal_core(journal)
            raw_entries = core.get("entries")
            if not isinstance(raw_entries, list):
                raise RetentionApplyError("retention_apply_journal_invalid")
            by_candidate = {str(item["candidate_sha256"]): item for item in raw_entries}
            entry = by_candidate[str(candidate["candidate_sha256"])]
        source = Path(str(candidate["path"]))
        quarantine = source.parent / str(entry["quarantine_name"])
        if source.exists() and not source.is_symlink():
            _candidate_matches_observation(candidate, source)
            if quarantine.exists() or quarantine.is_symlink():
                # A foreign no-replace collision is never part of this
                # transaction.  Preserve it and the original failure while
                # still restoring every earlier, exact batch member.
                continue
        elif quarantine.exists() and not quarantine.is_symlink():
            _restore_quarantine(
                candidate,
                str(entry["quarantine_name"]),
                guard=guard,
            )
        else:
            raise RetentionApplyError("retention_apply_batch_restore_unavailable")
        entry.update(
            {
                "objects_sha256": "",
                "residual_authority": {},
                "sealed_tree_sha256": "",
                "status": "pending",
                "tree_sha256": "",
            }
        )
        journal = _write_journal(journal_path, core, guard=guard)
        core = _journal_core(journal)
        raw_entries = core.get("entries")
        if not isinstance(raw_entries, list):
            raise RetentionApplyError("retention_apply_journal_invalid")
        by_candidate = {str(item["candidate_sha256"]): item for item in raw_entries}
    return core


def _open_reference_after_rename(snapshot: Any, target_path: Path) -> bool:
    try:
        inventory = retention.build_complete_open_inventory(target_paths=(target_path,))
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_open_recheck_failed") from exc
    del snapshot
    return target_path in inventory.open_paths


def _preflight_filesystem_lease(
    candidate: Mapping[str, Any],
    *,
    guard: Callable[[], None],
) -> None:
    root_fd, _parts, _identities = _root_descriptor(candidate)
    descriptor = -1
    try:
        guard()
        descriptor = os.open(
            ".",
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_TMPFILE", 0),
            0o600,
            dir_fd=root_fd,
        )
        if retention._descriptor_mount_id(descriptor) != candidate["root_mount_id"]:  # noqa: SLF001
            raise RetentionApplyError("retention_apply_lease_unavailable")
        fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        if fcntl.fcntl(descriptor, fcntl.F_GETLEASE) != fcntl.F_WRLCK:
            raise RetentionApplyError("retention_apply_lease_unavailable")
        fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        guard()
    except RetentionApplyError:
        raise
    except OSError as exc:
        raise RetentionApplyError("retention_apply_lease_unavailable") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
            os.close(descriptor)
        os.close(root_fd)


def _unlink_regular_with_lease(
    descriptor: int,
    name: str,
    before: os.stat_result,
    *,
    root_mount_id: int,
    byte_counter: list[int],
    guard: Callable[[], None],
) -> None:
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid():
        raise RetentionApplyError("retention_apply_child_unsafe")
    file_fd = -1
    lease_set = False
    lease_signal = signal.SIGRTMIN + 3
    previous_mask: set[int | signal.Signals] | None = None
    try:
        safe_mode = (stat.S_IMODE(before.st_mode) & 0o700) | 0o600
        os.chmod(name, safe_mode, dir_fd=descriptor, follow_symlinks=False)
        guard()
        file_fd = os.open(
            name,
            _METADATA_RDWR_FLAGS,
            dir_fd=descriptor,
        )
        opened = os.fstat(file_fd)
        named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or retention._descriptor_mount_id(file_fd) != root_mount_id  # noqa: SLF001
        ):
            raise RetentionApplyError("retention_apply_child_raced")
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {lease_signal})
        while signal.sigtimedwait({lease_signal}, 0) is not None:
            pass
        fcntl.fcntl(file_fd, fcntl.F_SETSIG, lease_signal)
        try:
            fcntl.fcntl(file_fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        except OSError as exc:
            if exc.errno in {
                errno.EAGAIN,
                errno.EACCES,
                errno.EINVAL,
                errno.ENOSYS,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
            }:
                raise RetentionApplyError("retention_apply_lease_unavailable") from exc
            raise
        lease_set = True
        after_lease = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (
            (after_lease.st_dev, after_lease.st_ino) != (before.st_dev, before.st_ino)
            or after_lease.st_nlink != 1
            or fcntl.fcntl(file_fd, fcntl.F_GETLEASE) != fcntl.F_WRLCK
            or signal.sigtimedwait({lease_signal}, 0) is not None
        ):
            raise RetentionApplyError("retention_apply_lease_broken")
        byte_counter[0] += int(opened.st_size)
        guard()
        os.unlink(name, dir_fd=descriptor)
        guard()
        unlinked = os.fstat(file_fd)
        if (
            unlinked.st_nlink != 0
            or (unlinked.st_dev, unlinked.st_ino) != (before.st_dev, before.st_ino)
            or fcntl.fcntl(file_fd, fcntl.F_GETLEASE) != fcntl.F_WRLCK
        ):
            raise RetentionApplyError("retention_apply_lease_broken")
    except RetentionApplyError:
        raise
    except (OSError, ValueError) as exc:
        raise RetentionApplyError("retention_apply_delete_failed") from exc
    finally:
        if lease_set and file_fd >= 0:
            with suppress(OSError):
                fcntl.fcntl(file_fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        if file_fd >= 0:
            os.close(file_fd)
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _unlink_directory_bounded(
    descriptor: int,
    *,
    root_device: int,
    root_mount_id: int,
    counter: list[int],
    byte_counter: list[int],
    guard: Callable[[], None],
    depth: int = 0,
    skip_names: frozenset[str] = frozenset(),
    fault_point: str = "before_unlink_entry",
) -> None:
    if depth > retention.MAX_INVENTORY_DEPTH:
        raise RetentionApplyError("retention_apply_delete_bound_exceeded")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise RetentionApplyError("retention_apply_delete_failed") from exc
    for name in names:
        if name in skip_names:
            continue
        guard()
        counter[0] += 1
        if counter[0] > MAX_DELETE_ENTRIES or name in {"", ".", ".."}:
            raise RetentionApplyError("retention_apply_delete_bound_exceeded")
        _fault(fault_point)
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise RetentionApplyError("retention_apply_child_raced") from exc
        if before.st_uid != os.geteuid() or before.st_dev != root_device:
            raise RetentionApplyError("retention_apply_child_unsafe")
        if stat.S_ISDIR(before.st_mode):
            child = -1
            try:
                child = os.open(name, flags, dir_fd=descriptor)
                opened = os.fstat(child)
                if (
                    (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or retention._descriptor_mount_id(child) != root_mount_id  # noqa: SLF001
                ):
                    raise RetentionApplyError("retention_apply_child_raced")
                _unlink_directory_bounded(
                    child,
                    root_device=root_device,
                    root_mount_id=root_mount_id,
                    counter=counter,
                    byte_counter=byte_counter,
                    guard=guard,
                    depth=depth + 1,
                    skip_names=frozenset(),
                    fault_point=fault_point,
                )
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise RetentionApplyError("retention_apply_child_raced")
                guard()
                os.rmdir(name, dir_fd=descriptor)
                guard()
                if os.fstat(child).st_nlink != 0:
                    raise RetentionApplyError("retention_apply_child_raced")
            finally:
                if child >= 0:
                    os.close(child)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise RetentionApplyError("retention_apply_child_unsafe")
        _unlink_regular_with_lease(
            descriptor,
            name,
            before,
            root_mount_id=root_mount_id,
            byte_counter=byte_counter,
            guard=guard,
        )
    os.fsync(descriptor)


def _delete_quarantine(
    candidate: Mapping[str, Any],
    quarantine_name: str,
    *,
    residual_authority: frozenset[bytes],
    guard: Callable[[], None],
) -> tuple[int, int]:
    guard()
    quarantine = Path(str(candidate["path"])).parent / quarantine_name
    if quarantine.exists() and not quarantine.is_symlink():
        _partial_quarantine_contour(
            candidate,
            quarantine,
            residual_authority=residual_authority,
        )
    root_fd, _parts, _identities = _root_descriptor(candidate)
    child_fd = -1
    expected = (candidate["device"], candidate["inode"])
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            child_fd = os.open(quarantine_name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            source_name = Path(str(candidate["path"])).name
            if (
                _named_identity(root_fd, source_name) is not None
                or _named_identity(root_fd, quarantine_name) is not None
            ):
                raise RetentionApplyError("retention_apply_target_raced") from None
            return int(candidate["recursive_bytes"]), int(candidate["entry_count"])
        opened = os.fstat(child_fd)
        named = os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != expected
            or (named.st_dev, named.st_ino) != expected
            or opened.st_uid != os.geteuid()
            or not stat.S_ISDIR(opened.st_mode)
            or retention._descriptor_mount_id(child_fd) != candidate["mount_id"]  # noqa: SLF001
        ):
            raise RetentionApplyError("retention_apply_quarantine_changed")
        counter = [1]
        byte_counter = [0]
        _unlink_directory_bounded(
            child_fd,
            root_device=int(opened.st_dev),
            root_mount_id=int(candidate["mount_id"]),
            counter=counter,
            byte_counter=byte_counter,
            guard=guard,
        )
        after = os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != expected:
            raise RetentionApplyError("retention_apply_quarantine_changed")
        guard()
        os.rmdir(quarantine_name, dir_fd=root_fd)
        os.fsync(root_fd)
        guard()
        if os.fstat(child_fd).st_nlink != 0:
            raise RetentionApplyError("retention_apply_quarantine_changed")
        expected_bytes = int(candidate["recursive_bytes"])
        expected_inodes = int(candidate["entry_count"])
        if byte_counter[0] > expected_bytes or counter[0] > expected_inodes:
            raise RetentionApplyError("retention_apply_delete_accounting_mismatch")
        # A durable ``deleting`` phase can resume after an arbitrary prefix was
        # already removed.  The pre-delete authenticated tree is the exact
        # accounting authority for the eventually absent object.
        return expected_bytes, expected_inodes
    except OSError as exc:
        raise RetentionApplyError("retention_apply_delete_failed") from exc
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(root_fd)


def _remove_legacy_registration(
    candidate: Mapping[str, Any],
    *,
    guard: Callable[[], None],
    transaction_id: str,
) -> None:
    if candidate.get("reason") != "retirable_registered_legacy_worktree":
        return
    identity = candidate.get("identity")
    if not isinstance(identity, Mapping):
        raise RetentionApplyError("retention_apply_legacy_registration_invalid")
    try:
        git_dir = Path(str(identity["git_dir"]))
        common_dir = Path(str(identity["common_dir"]))
        git_identity = (int(identity["git_device"]), int(identity["git_inode"]))
        expected_manifest = identity["registration_manifest"]
        expected_manifest_sha256 = str(identity["registration_manifest_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RetentionApplyError("retention_apply_legacy_registration_invalid") from exc
    if (
        not git_dir.is_absolute()
        or not common_dir.is_absolute()
        or git_dir.parent.name != "worktrees"
        or git_dir.parent.parent != common_dir
        or not isinstance(expected_manifest, list)
        or not _is_hex64(expected_manifest_sha256)
        or hashlib.sha256(_canonical(expected_manifest)).hexdigest() != expected_manifest_sha256
        or not _is_hex64(transaction_id)
    ):
        raise RetentionApplyError("retention_apply_legacy_registration_invalid")
    source = Path(str(candidate["path"]))
    guard()
    root_fd, _parts, _identities = _root_descriptor(candidate)
    try:
        quarantine_names = (
            name for name in os.listdir(root_fd) if name.startswith(".friday-retention-q-v1-")
        )
        if _named_identity(root_fd, source.name) is not None or any(quarantine_names):
            raise RetentionApplyError("retention_apply_legacy_registration_live")
    finally:
        os.close(root_fd)
    worktrees_fd = -1
    git_fd = -1
    lock_fd = -1
    lock_name = ".friday-retention-registration.v1.lock"
    try:
        worktrees_fd, parts, identities = retention._open_absolute_directory_chain(  # noqa: SLF001
            git_dir.parent,
            code="retention_apply_legacy_registration_invalid",
        )
        retention._require_pinned_directory(  # noqa: SLF001
            worktrees_fd,
            parts,
            identities,
            code="retention_apply_legacy_registration_invalid",
            private=False,
        )
        guard()
        lock_fd = os.open(
            lock_name,
            _METADATA_RDWR_FLAGS | os.O_CREAT,
            0o600,
            dir_fd=worktrees_fd,
        )
        lock_status = os.fstat(lock_fd)
        lock_named = os.stat(lock_name, dir_fd=worktrees_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(lock_status.st_mode)
            or lock_status.st_uid != os.geteuid()
            or lock_status.st_nlink != 1
            or stat.S_IMODE(lock_status.st_mode) != 0o600
            or (lock_status.st_dev, lock_status.st_ino) != (lock_named.st_dev, lock_named.st_ino)
        ):
            raise RetentionApplyError("retention_apply_legacy_registration_invalid")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetentionApplyError("retention_apply_legacy_registration_locked") from exc
        guard()
        try:
            named = os.stat(git_dir.name, dir_fd=worktrees_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            (named.st_dev, named.st_ino) != git_identity
            or named.st_uid != os.geteuid()
            or not stat.S_ISDIR(named.st_mode)
        ):
            raise RetentionApplyError("retention_apply_legacy_registration_changed")
        git_fd = os.open(
            git_dir.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=worktrees_fd,
        )
        opened = os.fstat(git_fd)
        if (opened.st_dev, opened.st_ino) != git_identity:
            raise RetentionApplyError("retention_apply_legacy_registration_changed")
        if not os.listdir(git_fd):
            guard()
            os.rmdir(git_dir.name, dir_fd=worktrees_fd)
            os.fsync(worktrees_fd)
            guard()
            return
        expected_by_path = {
            str(item.get("path")): item for item in expected_manifest if isinstance(item, Mapping)
        }
        if len(expected_by_path) != len(expected_manifest) or "." not in expected_by_path:
            raise RetentionApplyError("retention_apply_legacy_registration_invalid")

        def require_subset(*, complete: bool) -> None:
            try:
                observed = retention._git_admin_manifest(  # noqa: SLF001
                    git_dir,
                    omit_locked=True,
                )
            except retention.RetentionPlanError as exc:
                raise RetentionApplyError("retention_apply_legacy_registration_changed") from exc
            if any(expected_by_path.get(str(item.get("path"))) != item for item in observed):
                raise RetentionApplyError("retention_apply_legacy_registration_changed")
            if complete and observed != expected_manifest:
                raise RetentionApplyError("retention_apply_legacy_registration_changed")

        locked_raw = f"friday-retention-v1:{transaction_id}:{candidate['candidate_sha256']}\n".encode("ascii")
        locked_identity = _named_identity(git_fd, "locked")
        if locked_identity is None:
            require_subset(complete=True)
            guard()
            locked_fd = os.open(
                "locked",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=git_fd,
            )
            try:
                offset = 0
                while offset < len(locked_raw):
                    offset += os.write(locked_fd, locked_raw[offset:])
                os.fsync(locked_fd)
            finally:
                os.close(locked_fd)
            os.fsync(git_fd)
            guard()
            require_subset(complete=True)
        else:
            try:
                observed_locked = retention._stable_file_bytes(  # noqa: SLF001
                    git_dir / "locked",
                    private=False,
                    code="retention_apply_legacy_registration_locked",
                    maximum_bytes=4096,
                )
            except retention.RetentionPlanError as exc:
                raise RetentionApplyError("retention_apply_legacy_registration_locked") from exc
            if observed_locked != locked_raw:
                raise RetentionApplyError("retention_apply_legacy_registration_locked")
            require_subset(complete=False)
        counter = [1]
        byte_counter = [0]
        _unlink_directory_bounded(
            git_fd,
            root_device=int(opened.st_dev),
            root_mount_id=retention._descriptor_mount_id(git_fd),  # noqa: SLF001
            counter=counter,
            byte_counter=byte_counter,
            guard=guard,
            skip_names=frozenset({"locked"}),
            fault_point="before_registration_unlink",
        )
        if os.listdir(git_fd) != ["locked"]:
            raise RetentionApplyError("retention_apply_legacy_registration_changed")
        locked_status = os.stat("locked", dir_fd=git_fd, follow_symlinks=False)
        _unlink_regular_with_lease(
            git_fd,
            "locked",
            locked_status,
            root_mount_id=retention._descriptor_mount_id(git_fd),  # noqa: SLF001
            byte_counter=[0],
            guard=guard,
        )
        os.fsync(git_fd)
        guard()
        _fault("after_registration_locked_unlink")
        after = os.stat(git_dir.name, dir_fd=worktrees_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != git_identity:
            raise RetentionApplyError("retention_apply_legacy_registration_changed")
        guard()
        os.rmdir(git_dir.name, dir_fd=worktrees_fd)
        os.fsync(worktrees_fd)
        guard()
    except RetentionApplyError:
        raise
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_legacy_registration_invalid") from exc
    finally:
        if git_fd >= 0:
            os.close(git_fd)
        if lock_fd >= 0:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if worktrees_fd >= 0:
            os.close(worktrees_fd)


def _post_apply_reauthenticate(reviewed: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    live_inputs = dict(inputs)
    deleted_reviewed_scratch = {
        Path(str(item["path"]))
        for item in reviewed.get("targets", [])
        if isinstance(item, Mapping)
        and item.get("decision") == "delete_candidate"
        and item.get("reason") == "retirable_reviewed_scratch"
    }
    scratch_inputs = inputs.get("reviewed_scratch_targets", ())
    if not isinstance(scratch_inputs, tuple):
        raise RetentionApplyError("retention_apply_post_authentication_failed")
    for path in deleted_reviewed_scratch:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RetentionApplyError("retention_apply_post_authentication_failed") from exc
        raise RetentionApplyError("retention_apply_post_authentication_failed")
    live_inputs["reviewed_scratch_targets"] = tuple(
        item for item in scratch_inputs if item.path not in deleted_reviewed_scratch
    )
    try:
        current = retention.build_eligible_retention_plan(**live_inputs)
    except (retention.RetentionPlanError, release_operator.ReleaseFailure) as exc:
        raise RetentionApplyError("retention_apply_post_authentication_failed") from exc
    for key in (
        "activation_journal",
        "unit_install_journal",
        "authority_bindings",
        "protected_releases",
        "retention_scope",
    ):
        if current.get(key) != reviewed.get(key):
            raise RetentionApplyError("retention_apply_post_authentication_failed")
    roles = {
        item.get("role")
        for item in current.get("authority_bindings", {}).get("dr_pins", [])
        if isinstance(item, Mapping)
    }
    protected_roles = {
        role
        for item in current.get("protected_releases", [])
        if isinstance(item, Mapping)
        for role in item.get("roles", [])
    }
    if not {"current", "older"}.issubset(roles) or not {
        "current",
        "previous",
        "fallback",
        "dr_restore_release",
    }.issubset(protected_roles):
        raise RetentionApplyError("retention_apply_post_authentication_failed")
    return current


def _terminal_absence(
    candidates: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    *,
    guard: Callable[[], None],
) -> None:
    for candidate, entry in zip(candidates, entries, strict=True):
        guard()
        descriptor, _parts, _identities = _root_descriptor(candidate)
        try:
            if (
                _named_identity(descriptor, Path(str(candidate["path"])).name) is not None
                or _named_identity(descriptor, str(entry["quarantine_name"])) is not None
            ):
                raise RetentionApplyError("retention_apply_terminal_absence_failed")
        finally:
            os.close(descriptor)
        guard()


def _receipt_with_digest(core: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(core),
        "receipt_sha256": hashlib.sha256(_canonical(dict(core))).hexdigest(),
    }


def _validate_apply_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "accepted_root_plan_sha256",
        "actual_deleted_inodes",
        "actual_deleted_logical_bytes",
        "admission_reason",
        "admission_status",
        "allocated_bytes_are_not_exact_physical_attribution",
        "authority_bindings_sha256",
        "batch_ordinal",
        "bounded_effect_contour",
        "candidate_set_sha256",
        "concurrent_open_attempts_excluded",
        "cycle_sha256",
        "deleted_authenticated_allocated_bytes",
        "deleted_candidate_count",
        "filesystem_after",
        "filesystem_before",
        "plan_sha256",
        "post_apply_reauthenticated",
        "pre_delete_authenticated_allocated_bytes",
        "pre_delete_authenticated_bytes",
        "pre_delete_authenticated_inodes",
        "previous_receipt_sha256",
        "privileged_probe_role",
        "receipt_sha256",
        "residual_authority_set_sha256",
        "retention_epoch_sha256",
        "retention_scope_schema",
        "retention_scope_sha256",
        "reviewed_full_candidate_set_sha256",
        "schema",
        "status",
        "statvfs_available_delta_bytes",
        "statvfs_concurrent_activity_unexcluded",
        "terminal_absence_observed",
        "threat_boundary",
        "transaction_id",
        "universal_absence_proof",
    }
    receipt = dict(value)
    core = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    ordinal = receipt.get("batch_ordinal")
    previous = receipt.get("previous_receipt_sha256")
    count = receipt.get("deleted_candidate_count")
    admission = receipt.get("admission_status")
    reason = receipt.get("admission_reason")
    if (
        set(receipt) != expected
        or receipt.get("schema") != APPLY_RECEIPT_SCHEMA
        or receipt.get("status") != "applied"
        or not _is_hex64(receipt.get("receipt_sha256"))
        or receipt["receipt_sha256"] != hashlib.sha256(_canonical(core)).hexdigest()
        or any(
            not _is_hex64(receipt.get(key))
            for key in (
                "accepted_root_plan_sha256",
                "authority_bindings_sha256",
                "candidate_set_sha256",
                "cycle_sha256",
                "plan_sha256",
                "residual_authority_set_sha256",
                "retention_epoch_sha256",
                "retention_scope_sha256",
                "reviewed_full_candidate_set_sha256",
                "transaction_id",
            )
        )
        or type(ordinal) is not int
        or not 0 <= int(ordinal) <= MAX_DELETE_ENTRIES
        or (ordinal == 0 and previous != "")
        or (ordinal > 0 and not _is_hex64(previous))
        or type(count) is not int
        or not 0 <= int(count) <= retention.MAX_DELETE_CANDIDATES_PER_PLAN
        or admission not in {"nonterminal", "release_admissible"}
        or (admission == "release_admissible") != (reason == "fresh_eligible_zero" and count == 0)
        or (
            admission == "nonterminal"
            and reason != ("effectful_applied" if isinstance(count, int) and count > 0 else "deferred_zero")
        )
        or any(
            type(receipt.get(key)) is not int or int(receipt[key]) < 0
            for key in (
                "actual_deleted_inodes",
                "actual_deleted_logical_bytes",
                "deleted_authenticated_allocated_bytes",
                "pre_delete_authenticated_allocated_bytes",
                "pre_delete_authenticated_bytes",
                "pre_delete_authenticated_inodes",
            )
        )
        or (
            count == 0
            and any(
                int(receipt[key]) != 0
                for key in (
                    "actual_deleted_inodes",
                    "actual_deleted_logical_bytes",
                    "deleted_authenticated_allocated_bytes",
                    "pre_delete_authenticated_allocated_bytes",
                    "pre_delete_authenticated_bytes",
                    "pre_delete_authenticated_inodes",
                )
            )
        )
        or receipt.get("retention_scope_schema") != retention.RETENTION_SCOPE_SCHEMA
        or receipt.get("terminal_absence_observed") is not True
        or receipt.get("post_apply_reauthenticated") is not True
    ):
        raise RetentionApplyError("retention_apply_receipt_invalid")
    return receipt


def _publish_receipt(
    state_dir: Path,
    receipt: Mapping[str, Any],
    *,
    guard: Callable[[], None],
) -> dict[str, Any]:
    receipt = _validate_apply_receipt(receipt)
    supplied = receipt.get("receipt_sha256")
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        receipt.get("schema") != APPLY_RECEIPT_SCHEMA
        or not _is_hex64(supplied)
        or hashlib.sha256(_canonical(core)).hexdigest() != supplied
        or not _is_hex64(receipt.get("transaction_id"))
    ):
        raise RetentionApplyError("retention_apply_receipt_invalid")
    state_fd = -1
    receipt_fd = -1
    name = f"receipt-{receipt['transaction_id']}.json"
    temporary = f".{name}.new"
    raw = _canonical(dict(receipt)) + b"\n"
    try:
        guard()
        state_fd, parts, identities = retention._open_absolute_directory_chain(  # noqa: SLF001
            state_dir,
            code="retention_apply_receipt_invalid",
        )
        retention._require_pinned_directory(  # noqa: SLF001
            state_fd,
            parts,
            identities,
            code="retention_apply_receipt_invalid",
            private=True,
        )
        try:
            guard()
            os.mkdir(APPLY_RECEIPT_DIRECTORY, 0o700, dir_fd=state_fd)
        except FileExistsError:
            pass
        os.fsync(state_fd)
        guard()
        receipt_fd = os.open(
            APPLY_RECEIPT_DIRECTORY,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=state_fd,
        )
        opened_directory = os.fstat(receipt_fd)
        named_directory = os.stat(
            APPLY_RECEIPT_DIRECTORY,
            dir_fd=state_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened_directory.st_mode)
            or opened_directory.st_uid != os.geteuid()
            or stat.S_IMODE(opened_directory.st_mode) != 0o700
            or (opened_directory.st_dev, opened_directory.st_ino)
            != (named_directory.st_dev, named_directory.st_ino)
        ):
            raise RetentionApplyError("retention_apply_receipt_invalid")

        def read_named(value: str) -> tuple[os.stat_result, bytes] | None:
            try:
                descriptor = os.open(
                    value,
                    _METADATA_READ_FLAGS,
                    dir_fd=receipt_fd,
                )
            except FileNotFoundError:
                return None
            try:
                status = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or status.st_nlink not in {1, 2}
                    or stat.S_IMODE(status.st_mode) != 0o400
                    or status.st_size != len(raw)
                ):
                    raise RetentionApplyError("retention_apply_receipt_changed")
                observed = b""
                while len(observed) <= len(raw):
                    chunk = os.read(descriptor, len(raw) + 1 - len(observed))
                    if not chunk:
                        break
                    observed += chunk
                named = os.stat(value, dir_fd=receipt_fd, follow_symlinks=False)
                if observed != raw or (status.st_dev, status.st_ino) != (
                    named.st_dev,
                    named.st_ino,
                ):
                    raise RetentionApplyError("retention_apply_receipt_changed")
                return status, observed
            finally:
                os.close(descriptor)

        def read_stage() -> tuple[os.stat_result, bytes] | None:
            try:
                descriptor = os.open(
                    temporary,
                    _METADATA_READ_FLAGS,
                    dir_fd=receipt_fd,
                )
            except FileNotFoundError:
                return None
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise RetentionApplyError("retention_apply_receipt_changed")
                observed = b""
                while len(observed) <= len(raw):
                    chunk = os.read(descriptor, len(raw) + 1 - len(observed))
                    if not chunk:
                        break
                    observed += chunk
                named = os.stat(temporary, dir_fd=receipt_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_uid != os.geteuid()
                    or status.st_nlink not in {1, 2}
                    or (status.st_dev, status.st_ino) != (named.st_dev, named.st_ino)
                    or (
                        not (
                            stat.S_IMODE(status.st_mode) == 0o400
                            and status.st_size == len(raw)
                            and observed == raw
                        )
                        and not (
                            status.st_nlink == 1
                            and stat.S_IMODE(status.st_mode) == 0o600
                            and status.st_size <= len(raw)
                        )
                    )
                ):
                    raise RetentionApplyError("retention_apply_receipt_changed")
                return status, observed
            finally:
                os.close(descriptor)

        existing = read_named(name)
        staged = read_stage()
        if existing is not None:
            existing_status, _existing_raw = existing
            if existing_status.st_nlink == 2:
                if (
                    staged is None
                    or staged[1] != raw
                    or stat.S_IMODE(staged[0].st_mode) != 0o400
                    or (staged[0].st_dev, staged[0].st_ino)
                    != (existing_status.st_dev, existing_status.st_ino)
                ):
                    raise RetentionApplyError("retention_apply_receipt_changed")
                guard()
                os.unlink(temporary, dir_fd=receipt_fd)
                os.fsync(receipt_fd)
                guard()
                existing = read_named(name)
            if existing is None or existing[0].st_nlink != 1:
                raise RetentionApplyError("retention_apply_receipt_changed")
            guard()
            return dict(receipt)
        if staged is not None and (staged[1] != raw or stat.S_IMODE(staged[0].st_mode) != 0o400):
            guard()
            os.unlink(temporary, dir_fd=receipt_fd)
            os.fsync(receipt_fd)
            guard()
            staged = None
        if staged is None:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            guard()
            descriptor = os.open(temporary, flags, 0o600, dir_fd=receipt_fd)
            try:
                offset = 0
                while offset < len(raw):
                    offset += os.write(descriptor, raw[offset:])
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            guard()
        elif staged[0].st_nlink != 1:
            raise RetentionApplyError("retention_apply_receipt_changed")
        guard()
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=receipt_fd,
                dst_dir_fd=receipt_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RetentionApplyError("retention_apply_receipt_changed") from exc
        guard()
        os.unlink(temporary, dir_fd=receipt_fd)
        temporary = ""
        os.fsync(receipt_fd)
        guard()
        final = os.stat(name, dir_fd=receipt_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_uid != os.geteuid()
            or final.st_nlink != 1
            or stat.S_IMODE(final.st_mode) != 0o400
            or final.st_size != len(raw)
        ):
            raise RetentionApplyError("retention_apply_receipt_changed")
        return dict(receipt)
    except RetentionApplyError:
        raise
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_receipt_invalid") from exc
    finally:
        if receipt_fd >= 0:
            os.close(receipt_fd)
        if state_fd >= 0:
            os.close(state_fd)


def _persist_reviewed_plan(
    state_dir: Path,
    plan: Mapping[str, Any],
    *,
    guard: Callable[[], None],
    allow_incomplete_stage_repair: bool,
) -> tuple[Path, int, int]:
    plan_sha256 = str(plan["plan_sha256"])
    name = f"plan-{plan_sha256}.json"
    raw = _canonical(dict(plan)) + b"\n"
    state_fd = plan_fd = descriptor = -1
    temporary = f".{name}.new"
    cleanup_temporary = False
    try:
        guard()
        state_fd, parts, identities = retention._open_absolute_directory_chain(  # noqa: SLF001
            state_dir,
            code="retention_apply_plan_invalid",
        )
        retention._require_pinned_directory(  # noqa: SLF001
            state_fd,
            parts,
            identities,
            code="retention_apply_plan_invalid",
            private=True,
        )
        with suppress(FileExistsError):
            os.mkdir(APPLY_PLAN_DIRECTORY, 0o700, dir_fd=state_fd)
        os.fsync(state_fd)
        guard()
        plan_fd = os.open(
            APPLY_PLAN_DIRECTORY,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=state_fd,
        )
        directory = os.fstat(plan_fd)
        named_directory = os.stat(APPLY_PLAN_DIRECTORY, dir_fd=state_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
            or (directory.st_dev, directory.st_ino) != (named_directory.st_dev, named_directory.st_ino)
        ):
            raise RetentionApplyError("retention_apply_plan_invalid")
        try:
            descriptor = os.open(
                name,
                _METADATA_READ_FLAGS,
                dir_fd=plan_fd,
            )
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    temporary,
                    _METADATA_READ_FLAGS,
                    dir_fd=plan_fd,
                )
            except FileNotFoundError:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=plan_fd,
                )
                cleanup_temporary = True
                offset = 0
                while offset < len(raw):
                    offset += os.write(descriptor, raw[offset:])
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = os.open(
                    temporary,
                    _METADATA_READ_FLAGS,
                    dir_fd=plan_fd,
                )
            staged_status = os.fstat(descriptor)
            if not stat.S_ISREG(staged_status.st_mode):
                raise RetentionApplyError("retention_apply_plan_changed") from None
            staged_raw = b""
            while len(staged_raw) <= len(raw):
                chunk = os.read(descriptor, len(raw) + 1 - len(staged_raw))
                if not chunk:
                    break
                staged_raw += chunk
            staged_exact = (
                staged_raw == raw
                and stat.S_ISREG(staged_status.st_mode)
                and staged_status.st_uid == os.geteuid()
                and staged_status.st_nlink == 1
                and stat.S_IMODE(staged_status.st_mode) == 0o400
            )
            recoverable_incomplete = (
                allow_incomplete_stage_repair
                and stat.S_ISREG(staged_status.st_mode)
                and staged_status.st_uid == os.geteuid()
                and staged_status.st_nlink == 1
                and stat.S_IMODE(staged_status.st_mode) == 0o600
                and staged_status.st_size <= len(raw)
            )
            if not staged_exact and not recoverable_incomplete:
                raise RetentionApplyError("retention_apply_plan_changed") from None
            os.close(descriptor)
            descriptor = -1
            if recoverable_incomplete:
                guard()
                os.unlink(temporary, dir_fd=plan_fd)
                os.fsync(plan_fd)
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=plan_fd,
                )
                cleanup_temporary = True
                offset = 0
                while offset < len(raw):
                    offset += os.write(descriptor, raw[offset:])
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
            guard()
            with suppress(FileExistsError):
                os.link(
                    temporary,
                    name,
                    src_dir_fd=plan_fd,
                    dst_dir_fd=plan_fd,
                    follow_symlinks=False,
                )
            os.unlink(temporary, dir_fd=plan_fd)
            temporary = ""
            cleanup_temporary = False
            os.fsync(plan_fd)
            descriptor = os.open(
                name,
                _METADATA_READ_FLAGS,
                dir_fd=plan_fd,
            )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise RetentionApplyError("retention_apply_plan_changed")
        observed = b""
        while len(observed) <= len(raw):
            chunk = os.read(descriptor, len(raw) + 1 - len(observed))
            if not chunk:
                break
            observed += chunk
        named = os.stat(name, dir_fd=plan_fd, follow_symlinks=False)
        if status.st_nlink == 2:
            staged = os.stat(temporary, dir_fd=plan_fd, follow_symlinks=False)
            if (staged.st_dev, staged.st_ino) != (status.st_dev, status.st_ino):
                raise RetentionApplyError("retention_apply_plan_changed")
            guard()
            os.unlink(temporary, dir_fd=plan_fd)
            temporary = ""
            cleanup_temporary = False
            os.fsync(plan_fd)
            guard()
            status = os.fstat(descriptor)
        if (
            observed != raw
            or not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) != 0o400
            or (status.st_dev, status.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RetentionApplyError("retention_apply_plan_changed")
        guard()
        return state_dir / APPLY_PLAN_DIRECTORY / name, int(status.st_dev), int(status.st_ino)
    except RetentionApplyError:
        raise
    except (OSError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_plan_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if cleanup_temporary and temporary and plan_fd >= 0:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=plan_fd)
        if plan_fd >= 0:
            os.close(plan_fd)
        if state_fd >= 0:
            os.close(state_fd)


def _filesystem_free_evidence(
    candidates: Sequence[Mapping[str, Any]],
    *,
    guard: Callable[[], None],
) -> list[dict[str, Any]]:
    evidence: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for candidate in candidates:
        guard()
        descriptor, _parts, _identities = _root_descriptor(candidate)
        try:
            before = os.fstat(descriptor)
            values = os.fstatvfs(descriptor)
            filesystem_magic, fsid_first, fsid_second = retention._descriptor_filesystem_identity(descriptor)  # noqa: SLF001
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or retention._descriptor_mount_id(descriptor) != candidate["root_mount_id"]  # noqa: SLF001
            ):
                raise RetentionApplyError("retention_apply_filesystem_evidence_failed")
        finally:
            os.close(descriptor)
        key = (int(before.st_dev), filesystem_magic, fsid_first, fsid_second)
        fragment_size = int(values.f_frsize or values.f_bsize)
        if fragment_size <= 0:
            raise RetentionApplyError("retention_apply_filesystem_evidence_failed")
        member = [int(candidate["root_inode"]), int(candidate["root_mount_id"])]
        existing = evidence.get(key)
        if existing is None:
            evidence[key] = {
                "available_blocks": int(values.f_bavail),
                "available_bytes": int(values.f_bavail) * fragment_size,
                "block_count": int(values.f_blocks),
                "device": key[0],
                "filesystem_magic": filesystem_magic,
                "fragment_size": fragment_size,
                "fsid": [fsid_first, fsid_second],
                "members": [member],
            }
        else:
            if any(
                existing[name] != value
                for name, value in {
                    "block_count": int(values.f_blocks),
                    "fragment_size": fragment_size,
                }.items()
            ):
                raise RetentionApplyError("retention_apply_filesystem_evidence_failed")
            existing["members"].append(member)
        guard()
    for item in evidence.values():
        item["members"] = [list(member) for member in sorted({tuple(value) for value in item["members"]})]
    return [evidence[key] for key in sorted(evidence)]


def _validate_filesystem_evidence(value: object) -> list[dict[str, Any]]:
    keys = {
        "available_blocks",
        "available_bytes",
        "block_count",
        "fragment_size",
        "device",
        "filesystem_magic",
        "fsid",
        "members",
    }
    if not isinstance(value, list):
        raise RetentionApplyError("retention_apply_journal_invalid")
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if (
            not isinstance(raw, Mapping)
            or set(raw) != keys
            or any(type(raw[key]) is not int for key in keys - {"fsid", "members"})
            or not isinstance(raw["fsid"], list)
            or len(raw["fsid"]) != 2
            or any(type(item) is not int or item < 0 for item in raw["fsid"])
            or not isinstance(raw["members"], list)
            or not raw["members"]
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or any(type(number) is not int or number <= 0 for number in item)
                for item in raw["members"]
            )
            or raw["members"] != [list(item) for item in sorted({tuple(item) for item in raw["members"]})]
            or raw["fragment_size"] <= 0
            or raw["available_blocks"] < 0
            or raw["available_bytes"] != raw["available_blocks"] * raw["fragment_size"]
            or raw["block_count"] < raw["available_blocks"]
        ):
            raise RetentionApplyError("retention_apply_journal_invalid")
        normalized.append(
            {
                **{key: int(raw[key]) for key in sorted(keys - {"fsid", "members"})},
                "fsid": list(raw["fsid"]),
                "members": [list(item) for item in raw["members"]],
            }
        )
    identities = [(item["device"], item["filesystem_magic"], *item["fsid"]) for item in normalized]
    if identities != sorted(set(identities)):
        raise RetentionApplyError("retention_apply_journal_invalid")
    return normalized


def _new_journal(
    plan: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    durable_plan: tuple[Path, int, int],
    filesystem_before: Sequence[Mapping[str, Any]],
    cycle_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    plan_sha256 = str(plan["plan_sha256"])
    cycle = (
        _normalize_cycle_context(cycle_context)
        if cycle_context is not None
        else (_standalone_cycle_context(plan, accepted_path=durable_plan[0]))
    )
    transaction = hashlib.sha256(
        _canonical(
            {
                "batch_ordinal": cycle["batch_ordinal"],
                "cycle_sha256": cycle["cycle_sha256"],
                "plan_sha256": plan_sha256,
                "previous_receipt_sha256": cycle["previous_receipt_sha256"],
                "schema": APPLY_JOURNAL_SCHEMA,
            }
        )
    ).hexdigest()
    scope = plan.get("retention_scope")
    if (
        not isinstance(scope, Mapping)
        or scope.get("schema") != retention.RETENTION_SCOPE_SCHEMA
        or not _is_hex64(scope.get("file_sha256"))
    ):
        raise RetentionApplyError("retention_apply_plan_invalid")
    return {
        "schema": APPLY_JOURNAL_SCHEMA,
        "transaction_id": transaction,
        **cycle,
        "plan_sha256": plan_sha256,
        "retention_scope_schema": scope["schema"],
        "retention_scope_sha256": scope["file_sha256"],
        "phase": "prepared",
        "durable_plan": {
            "device": durable_plan[1],
            "inode": durable_plan[2],
            "path": str(durable_plan[0]),
            "sha256": plan_sha256,
        },
        "filesystem_before": [dict(item) for item in filesystem_before],
        "filesystem_after": [],
        "entries": [
            {
                "candidate_sha256": candidate["candidate_sha256"],
                "registration_manifest_sha256": (
                    candidate["identity"].get("registration_manifest_sha256", "")
                    if isinstance(candidate.get("identity"), Mapping)
                    else ""
                ),
                "objects_sha256": "",
                "tree_sha256": "",
                "sealed_tree_sha256": "",
                "residual_authority": {},
                "quarantine_name": f".friday-retention-q-v1-{transaction[:16]}-{index:06d}",
                "status": "pending",
                "actual_bytes": 0,
                "actual_allocated_bytes": 0,
                "actual_inodes": 0,
            }
            for index, candidate in enumerate(candidates)
        ],
        "receipt_sha256": "",
    }


def _validate_journal_contract(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    durable_plan: tuple[Path, int, int],
    cycle_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    core = _journal_core(value)
    observed_cycle = _cycle_context_from_record(core)
    if cycle_context is not None and observed_cycle != _normalize_cycle_context(cycle_context):
        raise RetentionApplyError("retention_apply_journal_invalid")
    filesystem_before = _validate_filesystem_evidence(core.get("filesystem_before"))
    filesystem_after = _validate_filesystem_evidence(core.get("filesystem_after"))
    expected = _new_journal(
        plan,
        candidates,
        durable_plan=durable_plan,
        filesystem_before=filesystem_before,
        cycle_context=observed_cycle,
    )
    if set(core) != set(expected) or core.get("transaction_id") != expected["transaction_id"]:
        raise RetentionApplyError("retention_apply_journal_invalid")
    if core.get("plan_sha256") != plan.get("plan_sha256") or core.get("phase") not in {
        "prepared",
        "applying",
        "applied",
    }:
        raise RetentionApplyError("retention_apply_journal_invalid")
    if (
        core.get("retention_scope_schema") != expected["retention_scope_schema"]
        or core.get("retention_scope_sha256") != expected["retention_scope_sha256"]
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    if core.get("durable_plan") != expected["durable_plan"]:
        raise RetentionApplyError("retention_apply_journal_invalid")
    entries = core.get("entries")
    expected_entries = expected["entries"]
    if (
        not isinstance(entries, list)
        or not isinstance(expected_entries, list)
        or len(entries) != len(expected_entries)
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    state_directory = durable_plan[0].parent.parent
    for index, (entry, expected_entry, candidate) in enumerate(
        zip(entries, expected_entries, candidates, strict=True)
    ):
        if not isinstance(entry, dict) or set(entry) != set(expected_entry):
            raise RetentionApplyError("retention_apply_journal_invalid")
        static = (
            "candidate_sha256",
            "registration_manifest_sha256",
            "quarantine_name",
        )
        if any(entry.get(key) != expected_entry.get(key) for key in static):
            raise RetentionApplyError("retention_apply_journal_invalid")
        status = entry.get("status")
        objects = entry.get("objects_sha256")
        tree = entry.get("tree_sha256")
        sealed_tree = entry.get("sealed_tree_sha256")
        residual_authority = entry.get("residual_authority")
        actual_bytes = entry.get("actual_bytes")
        actual_allocated_bytes = entry.get("actual_allocated_bytes")
        actual_inodes = entry.get("actual_inodes")
        if (
            status not in {"pending", "renaming", "sealed", "deleting", "deleted"}
            or (objects != "" and not _is_hex64(objects))
            or (tree != "" and not _is_hex64(tree))
            or (status != "pending" and (not _is_hex64(objects) or not _is_hex64(tree)))
            or (status in {"sealed", "deleting", "deleted"} and not _is_hex64(sealed_tree))
            or (status not in {"sealed", "deleting", "deleted"} and sealed_tree != "")
            or (status == "pending" and residual_authority != {})
            or (
                status != "pending"
                and (
                    not isinstance(residual_authority, Mapping)
                    or set(residual_authority) != {"count", "device", "inode", "path", "sha256"}
                    or type(residual_authority.get("count")) is not int
                    or not 1 <= int(residual_authority["count"]) <= retention.MAX_INVENTORY_ENTRIES
                    or type(residual_authority.get("device")) is not int
                    or type(residual_authority.get("inode")) is not int
                    or int(residual_authority["inode"]) <= 0
                    or not _is_hex64(residual_authority.get("sha256"))
                    or not isinstance(residual_authority.get("path"), str)
                    or residual_authority.get("path")
                    != str(
                        state_directory
                        / OBJECT_AUTHORITY_DIRECTORY
                        / f"objects-{core['transaction_id']}-{index:06d}.bin"
                    )
                )
            )
            or type(actual_bytes) is not int
            or type(actual_inodes) is not int
            or type(actual_allocated_bytes) is not int
            or actual_bytes < 0
            or actual_inodes < 0
            or actual_allocated_bytes < 0
            or (
                status == "deleted"
                and (
                    actual_bytes != candidate["recursive_bytes"]
                    or actual_allocated_bytes != candidate["allocated_bytes"]
                    or actual_inodes != candidate["entry_count"]
                )
            )
            or (
                status != "deleted"
                and (actual_bytes != 0 or actual_allocated_bytes != 0 or actual_inodes != 0)
            )
        ):
            raise RetentionApplyError("retention_apply_journal_invalid")
    phase = core["phase"]
    receipt_sha256 = core.get("receipt_sha256")
    expected_filesystems = sorted(
        {
            (
                int(candidate["root_device"]),
                int(candidate["root_inode"]),
                int(candidate["root_mount_id"]),
            )
            for candidate in candidates
        }
    )
    before_filesystems = [
        (item["device"], member[0], member[1]) for item in filesystem_before for member in item["members"]
    ]
    after_filesystems = [
        (item["device"], member[0], member[1]) for item in filesystem_after for member in item["members"]
    ]
    filesystem_identity = lambda item: (  # noqa: E731
        item["device"],
        item["filesystem_magic"],
        tuple(item["fsid"]),
        tuple(tuple(member) for member in item["members"]),
        item["fragment_size"],
        item["block_count"],
    )
    if (
        (phase == "prepared" and any(entry["status"] != "pending" for entry in entries))
        or (phase == "applied" and any(entry["status"] != "deleted" for entry in entries))
        or (phase == "applied" and not _is_hex64(receipt_sha256))
        or (phase != "applied" and receipt_sha256 != "")
        or (filesystem_after and any(entry["status"] != "deleted" for entry in entries))
        or (phase == "applied" and filesystem_after == [] and bool(candidates))
        or before_filesystems != expected_filesystems
        or (filesystem_after and after_filesystems != expected_filesystems)
        or (
            filesystem_after
            and [filesystem_identity(item) for item in filesystem_before]
            != [filesystem_identity(item) for item in filesystem_after]
        )
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    return core


def _is_exact_terminal_zero_plan(
    plan: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> bool:
    open_inventory = plan.get("open_inventory")
    return (
        not candidates
        and plan.get("apply_authority") is False
        and isinstance(open_inventory, Mapping)
        and open_inventory.get("source") == "code_owned_no_delete_candidates_v1"
        and not _has_deferred_batch_bound(plan)
        and not _has_open_only_identity(plan)
    )


def _result_receipt(
    *,
    plan: Mapping[str, Any],
    journal: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    authority_bindings_sha256: str,
) -> dict[str, Any]:
    entries = journal.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(entry, Mapping) or entry.get("status") != "deleted" for entry in entries
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    authenticated_bytes = sum(int(candidate["recursive_bytes"]) for candidate in candidates)
    authenticated_allocated_bytes = sum(int(candidate["allocated_bytes"]) for candidate in candidates)
    authenticated_inodes = sum(int(candidate["entry_count"]) for candidate in candidates)
    if (
        journal.get("retention_scope_schema") != plan["retention_scope"]["schema"]
        or journal.get("retention_scope_sha256") != plan["retention_scope"]["file_sha256"]
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    if (
        sum(int(entry["actual_bytes"]) for entry in entries) != authenticated_bytes
        or sum(int(entry["actual_allocated_bytes"]) for entry in entries) != authenticated_allocated_bytes
        or sum(int(entry["actual_inodes"]) for entry in entries) != authenticated_inodes
    ):
        raise RetentionApplyError("retention_apply_delete_accounting_mismatch")
    filesystem_before = _validate_filesystem_evidence(journal.get("filesystem_before"))
    filesystem_after = _validate_filesystem_evidence(journal.get("filesystem_after"))
    if len(filesystem_before) != len(filesystem_after):
        raise RetentionApplyError("retention_apply_delete_accounting_mismatch")
    statvfs_delta = sum(
        after["available_bytes"] - before["available_bytes"]
        for before, after in zip(filesystem_before, filesystem_after, strict=True)
    )
    exact_terminal_zero = (
        _is_exact_terminal_zero_plan(plan, candidates)
        and not entries
        and not filesystem_before
        and not filesystem_after
        and authenticated_bytes == 0
        and authenticated_allocated_bytes == 0
        and authenticated_inodes == 0
        and statvfs_delta == 0
    )
    cycle = _cycle_context_from_record(journal)
    core = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "status": "applied",
        "admission_status": "release_admissible" if exact_terminal_zero else "nonterminal",
        "admission_reason": (
            "fresh_eligible_zero"
            if exact_terminal_zero
            else "effectful_applied"
            if candidates
            else "deferred_zero"
        ),
        "accepted_root_plan_sha256": cycle["accepted_root_plan_sha256"],
        "batch_ordinal": cycle["batch_ordinal"],
        "cycle_sha256": cycle["cycle_sha256"],
        "previous_receipt_sha256": cycle["previous_receipt_sha256"],
        "retention_epoch_sha256": cycle["retention_epoch_sha256"],
        "reviewed_full_candidate_set_sha256": cycle["reviewed_full_candidate_set_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "retention_scope_schema": plan["retention_scope"]["schema"],
        "retention_scope_sha256": plan["retention_scope"]["file_sha256"],
        "transaction_id": journal["transaction_id"],
        "candidate_set_sha256": hashlib.sha256(
            _canonical([candidate["candidate_sha256"] for candidate in candidates])
        ).hexdigest(),
        "residual_authority_set_sha256": hashlib.sha256(
            _canonical(
                [
                    {
                        "candidate_sha256": entry["candidate_sha256"],
                        "count": entry["residual_authority"]["count"],
                        "sha256": entry["residual_authority"]["sha256"],
                    }
                    for entry in entries
                ]
            )
        ).hexdigest(),
        "deleted_candidate_count": len(entries),
        "pre_delete_authenticated_bytes": authenticated_bytes,
        "pre_delete_authenticated_allocated_bytes": authenticated_allocated_bytes,
        "pre_delete_authenticated_inodes": authenticated_inodes,
        "actual_deleted_logical_bytes": authenticated_bytes,
        "deleted_authenticated_allocated_bytes": authenticated_allocated_bytes,
        "actual_deleted_inodes": authenticated_inodes,
        "authority_bindings_sha256": authority_bindings_sha256,
        "terminal_absence_observed": True,
        "post_apply_reauthenticated": True,
        "bounded_effect_contour": retention.BOUNDED_DELETE_CONTOUR,
        "concurrent_open_attempts_excluded": True,
        "privileged_probe_role": "diagnostic_prerequisite",
        "threat_boundary": retention.THREAT_BOUNDARY,
        "universal_absence_proof": False,
        "allocated_bytes_are_not_exact_physical_attribution": True,
        "statvfs_concurrent_activity_unexcluded": True,
        "statvfs_available_delta_bytes": statvfs_delta,
        "filesystem_before": filesystem_before,
        "filesystem_after": filesystem_after,
    }
    return _receipt_with_digest(core)


def _cleanup_object_authorities(
    state_dir: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    guard: Callable[[], None],
) -> None:
    directory = state_dir / OBJECT_AUTHORITY_DIRECTORY
    if not directory.exists() and not directory.is_symlink():
        return
    directory_fd = -1
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        for entry in entries:
            binding = entry.get("residual_authority")
            if not isinstance(binding, Mapping):
                raise RetentionApplyError("retention_apply_residual_authority_invalid")
            path = Path(str(binding.get("path") or ""))
            if path.parent != directory:
                raise RetentionApplyError("retention_apply_residual_authority_invalid")
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _load_residual_authority(binding, state_dir=state_dir)
            guard()
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            guard()
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RetentionApplyError("retention_apply_residual_authority_invalid")
    except OSError as exc:
        raise RetentionApplyError("retention_apply_residual_authority_invalid") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _cleanup_completed_transaction_before_rollover(
    state_dir: Path,
    journal: Mapping[str, Any],
    *,
    guard: Callable[[], None],
) -> None:
    plan_sha256 = journal.get("plan_sha256")
    if not _is_hex64(plan_sha256) or journal.get("phase") != "applied":
        raise RetentionApplyError("retention_apply_journal_invalid")
    reviewed, durable_plan, loaded = _resume_plan_from_state(
        state_dir,
        expected_plan_sha256=str(plan_sha256),
        guard=guard,
    )
    candidates = _candidate_records(reviewed)
    core = _validate_journal_contract(
        loaded,
        plan=reviewed,
        candidates=candidates,
        durable_plan=durable_plan,
    )
    entries = core.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(entry, Mapping) or entry.get("status") != "deleted" for entry in entries
    ):
        raise RetentionApplyError("retention_apply_journal_invalid")
    _terminal_absence(candidates, entries, guard=guard)
    authority = reviewed.get("authority_bindings")
    if not isinstance(authority, Mapping) or not _is_hex64(authority.get("bindings_sha256")):
        raise RetentionApplyError("retention_apply_journal_invalid")
    receipt = _result_receipt(
        plan=reviewed,
        journal=core,
        candidates=candidates,
        authority_bindings_sha256=str(authority["bindings_sha256"]),
    )
    if core.get("receipt_sha256") != receipt["receipt_sha256"]:
        raise RetentionApplyError("retention_apply_journal_invalid")
    if receipt.get("admission_status") != "release_admissible":
        _live_authority_reauthenticate(reviewed, _plan_inputs(reviewed))
    _publish_receipt(state_dir, receipt, guard=guard)
    _cleanup_object_authorities(state_dir, entries, guard=guard)


def _validate_mutation_namespaces(
    *,
    plan_path: Path,
    state_dir: Path,
    candidates: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    require_quarantine_absent: bool = True,
) -> None:
    plan_lexical = Path(os.path.abspath(plan_path))
    journal_path = state_dir / APPLY_JOURNAL_NAME
    receipt_path = state_dir / APPLY_RECEIPT_DIRECTORY
    durable_plan_directory = state_dir / APPLY_PLAN_DIRECTORY
    object_authority_directory = state_dir / OBJECT_AUTHORITY_DIRECTORY
    retention_scope_path = state_dir / retention.RETENTION_SCOPE_NAME
    inventory_roots = {Path(str(candidate["path"])).parent for candidate in candidates}
    if any(plan_lexical == root or root in plan_lexical.parents for root in inventory_roots):
        raise RetentionApplyError("retention_apply_plan_namespace_invalid")
    for candidate, entry in zip(candidates, entries, strict=True):
        target = Path(str(candidate["path"]))
        quarantine = target.parent / str(entry["quarantine_name"])
        for protected in (
            plan_lexical,
            journal_path,
            receipt_path,
            durable_plan_directory,
            object_authority_directory,
            retention_scope_path,
        ):
            if protected == target or protected in target.parents or target in protected.parents:
                raise RetentionApplyError("retention_apply_namespace_intersection")
        if quarantine == plan_lexical or quarantine in plan_lexical.parents:
            raise RetentionApplyError("retention_apply_plan_namespace_invalid")
        if require_quarantine_absent:
            try:
                os.stat(quarantine, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RetentionApplyError("retention_apply_quarantine_collision") from exc
            else:
                raise RetentionApplyError("retention_apply_quarantine_collision")


def _resume_plan_from_state(
    state_dir: Path,
    *,
    expected_plan_sha256: str,
    guard: Callable[[], None],
    repair_staged_publication: bool = True,
) -> tuple[dict[str, Any], tuple[Path, int, int], dict[str, Any]]:
    journal = _load_journal(state_dir / APPLY_JOURNAL_NAME)
    if journal is None or journal.get("plan_sha256") != expected_plan_sha256:
        raise RetentionApplyError("retention_apply_resume_authority_missing")
    durable = journal.get("durable_plan")
    if not isinstance(durable, Mapping) or set(durable) != {"device", "inode", "path", "sha256"}:
        raise RetentionApplyError("retention_apply_resume_authority_missing")
    expected_path = state_dir / APPLY_PLAN_DIRECTORY / f"plan-{expected_plan_sha256}.json"
    path = Path(str(durable.get("path") or ""))
    if (
        path != expected_path
        or durable.get("sha256") != expected_plan_sha256
        or type(durable.get("device")) is not int
        or type(durable.get("inode")) is not int
        or int(durable["inode"]) <= 0
    ):
        raise RetentionApplyError("retention_apply_resume_authority_missing")
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise RetentionApplyError("retention_apply_resume_authority_missing") from exc
    recoverable_two_link = False
    if status.st_nlink == 2:
        plan_fd = -1
        try:
            plan_fd, parts, identities = retention._open_absolute_directory_chain(  # noqa: SLF001
                path.parent,
                code="retention_apply_resume_authority_missing",
            )
            retention._require_pinned_directory(  # noqa: SLF001
                plan_fd,
                parts,
                identities,
                code="retention_apply_resume_authority_missing",
                private=True,
            )
            staged_name = f".{path.name}.new"
            staged = os.stat(staged_name, dir_fd=plan_fd, follow_symlinks=False)
            if (staged.st_dev, staged.st_ino) != (status.st_dev, status.st_ino):
                raise RetentionApplyError("retention_apply_resume_authority_missing")
            if repair_staged_publication:
                guard()
                os.unlink(staged_name, dir_fd=plan_fd)
                os.fsync(plan_fd)
                guard()
                status = os.lstat(path)
            else:
                recoverable_two_link = True
        except OSError as exc:
            raise RetentionApplyError("retention_apply_resume_authority_missing") from exc
        finally:
            if plan_fd >= 0:
                os.close(plan_fd)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink not in ({1} if repair_staged_publication else {1, 2})
        or stat.S_IMODE(status.st_mode) != 0o400
        or (status.st_dev, status.st_ino) != (durable["device"], durable["inode"])
    ):
        raise RetentionApplyError("retention_apply_resume_authority_missing")
    reviewed = _read_plan(
        path,
        expected_sha256=expected_plan_sha256,
        allow_recoverable_two_link=recoverable_two_link,
    )
    return reviewed, (path, int(status.st_dev), int(status.st_ino)), journal


def _resume_plan_after_live_authority(
    state_dir: Path,
    *,
    expected_plan_sha256: str,
    guard: Callable[[], None],
) -> tuple[dict[str, Any], tuple[Path, int, int], dict[str, Any], dict[str, Any]]:
    """Authenticate a durable plan before repairing its publication stage."""

    reviewed, _durable_plan, _journal = _resume_plan_from_state(
        state_dir,
        expected_plan_sha256=expected_plan_sha256,
        guard=guard,
        repair_staged_publication=False,
    )
    inputs = _plan_inputs(reviewed)
    _live_authority_reauthenticate(reviewed, inputs)
    if _journal.get("phase") != "applied" and not _candidate_records(reviewed):
        fresh = _fresh_plan_for_cycle(
            reviewed=reviewed,
            inputs=inputs,
            state_dir=state_dir,
            cycle_context=_cycle_context_from_record(_journal),
        )
        if _authority_projection(fresh) != _authority_projection(reviewed):
            raise RetentionApplyError("retention_apply_plan_drift")
    reviewed, durable_plan, journal = _resume_plan_from_state(
        state_dir,
        expected_plan_sha256=expected_plan_sha256,
        guard=guard,
    )
    inputs = _plan_inputs(reviewed)
    _live_authority_reauthenticate(reviewed, inputs)
    return reviewed, durable_plan, journal, inputs


def _load_accepted_root_plan(
    state_dir: Path,
    cycle_context: Mapping[str, Any],
) -> dict[str, Any]:
    cycle = _normalize_cycle_context(cycle_context)
    digest = str(cycle["accepted_root_plan_sha256"])
    path = state_dir / APPLY_PLAN_DIRECTORY / f"plan-{digest}.json"
    root = _read_reviewed_plan(path, expected_sha256=digest)
    if _reviewed_candidate_set_sha256(root) != cycle["reviewed_full_candidate_set_sha256"]:
        raise RetentionApplyError("retention_convergence_review_invalid")
    return root


def _build_reviewed_subset_plan(
    *,
    inputs: Mapping[str, Any],
    accepted_root: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one fresh bounded batch solely from the accepted root identities."""

    try:
        scope = retention.load_retention_scope_authority(
            activation_journal=inputs["activation_journal"],
        )
        bindings = retention.build_retention_authority_bindings(
            activation_journal=inputs["activation_journal"],
            unit_journal=inputs["unit_journal"],
            canonical_evidence_roots=inputs["canonical_evidence_roots"],
        )
        seed = retention.plan_release_artifact_retention(
            activation_journal=inputs["activation_journal"],
            unit_journal=inputs["unit_journal"],
            backup_root=scope.backup_root,
            inventory_roots=scope.inventory_roots,
            backup_inventory_roots=scope.backup_inventory_roots,
            reviewed_scratch_targets=inputs["reviewed_scratch_targets"],
            open_inventory=retention.OpenInventorySnapshot(
                source="code_owned_candidate_scope_seed_v1",
                complete=True,
            ),
            authority_bindings=bindings,
            executable=True,
            _scope_seed=True,  # noqa: SLF001
            _retention_scope=scope.receipt,  # noqa: SLF001
        )
    except (KeyError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_authority_failed") from exc
    if seed.get("classification_status") != "scope_seed":
        raise RetentionApplyError("retention_apply_authority_failed")

    accepted_values = _reviewed_candidate_identities(accepted_root)
    accepted_by_digest = {
        hashlib.sha256(_canonical(item)).hexdigest(): _canonical(item) for item in accepted_values
    }
    if len(accepted_by_digest) != len(accepted_values):
        raise RetentionApplyError("retention_convergence_review_invalid")

    matching: list[tuple[str, Mapping[str, Any]]] = []
    for collection in ("targets", "backup_targets"):
        raw_records = seed.get(collection)
        if not isinstance(raw_records, list):
            raise RetentionApplyError("retention_apply_authority_failed")
        for record in raw_records:
            if not isinstance(record, Mapping) or (
                record.get("decision") != "delete_candidate"
                and record.get("reason") != "deferred_batch_bound"
            ):
                continue
            normalized = _reviewed_identity(record, collection=collection)
            digest = hashlib.sha256(_canonical(normalized)).hexdigest()
            if accepted_by_digest.get(digest) == _canonical(normalized):
                matching.append((str(record["path"]), record))

    selected_paths: list[Path] = []
    selected_objects = 0
    for _path, record in sorted(matching, key=lambda item: item[0]):
        objects = int(record["entry_count"])
        if (
            len(selected_paths) >= retention.MAX_DELETE_CANDIDATES_PER_PLAN
            or selected_objects + objects > proc_probe.MAX_TARGET_OBJECTS
        ):
            continue
        selected_paths.append(Path(str(record["path"])))
        selected_objects += objects
    target_paths = tuple(selected_paths)
    try:
        inventory = (
            retention.build_complete_open_inventory(target_paths=target_paths)
            if target_paths
            else retention.OpenInventorySnapshot(
                source="code_owned_no_delete_candidates_v1",
                complete=True,
                authority_sha256=str(seed["plan_sha256"]),
            )
        )
        plan = retention.plan_release_artifact_retention(
            activation_journal=inputs["activation_journal"],
            unit_journal=inputs["unit_journal"],
            backup_root=scope.backup_root,
            inventory_roots=scope.inventory_roots,
            backup_inventory_roots=scope.backup_inventory_roots,
            reviewed_scratch_targets=inputs["reviewed_scratch_targets"],
            open_inventory=inventory,
            authority_bindings=bindings,
            executable=True,
            _candidate_scope_paths=frozenset(target_paths),  # noqa: SLF001
            _retention_scope=scope.receipt,  # noqa: SLF001
        )
    except (KeyError, retention.RetentionPlanError) as exc:
        raise RetentionApplyError("retention_apply_authority_failed") from exc
    if plan.get("classification_status") != "eligible":
        raise RetentionApplyError("retention_apply_authority_failed")
    if _cycle_authority_projection(plan) != _cycle_authority_projection(accepted_root):
        raise RetentionApplyError("retention_convergence_cycle_changed")
    candidates = _candidate_records(plan)
    candidate_identities = {
        _reviewed_identity_sha256(
            candidate,
            collection=(
                "targets"
                if any(
                    isinstance(item, Mapping) and item.get("path") == candidate.get("path")
                    for item in plan["targets"]
                )
                else "backup_targets"
            ),
        )
        for candidate in candidates
    }
    if not candidate_identities.issubset(accepted_by_digest):
        raise RetentionApplyError("retention_convergence_candidate_outside_review")
    if retention.load_retention_scope_authority(activation_journal=inputs["activation_journal"]) != scope:
        raise RetentionApplyError("retention_apply_retention_scope_changed")
    return plan


def _fresh_plan_for_cycle(
    *,
    reviewed: Mapping[str, Any],
    inputs: Mapping[str, Any],
    state_dir: Path,
    cycle_context: Mapping[str, Any],
) -> dict[str, Any]:
    cycle = _normalize_cycle_context(cycle_context)
    if cycle["batch_ordinal"] == 0 and reviewed.get("plan_sha256") == cycle["accepted_root_plan_sha256"]:
        try:
            return retention.build_eligible_retention_plan(**inputs)
        except retention.RetentionPlanError as exc:
            raise RetentionApplyError("retention_apply_authority_failed") from exc
    accepted_root = _load_accepted_root_plan(state_dir, cycle)
    return _build_reviewed_subset_plan(inputs=inputs, accepted_root=accepted_root)


def apply_retention_plan(
    *,
    plan_path: Path | None,
    expected_plan_sha256: str,
    state_dir: Path | None = None,
    _cycle_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resume_from_state = state_dir is not None
    if resume_from_state:
        assert state_dir is not None
        try:
            state_dir = retention._strict_private_directory(  # noqa: SLF001
                Path(state_dir),
                code="retention_apply_resume_authority_missing",
            )
        except retention.RetentionPlanError as exc:
            raise RetentionApplyError("retention_apply_resume_authority_missing") from exc
        reviewed: dict[str, Any] | None = None
        durable_plan = (Path(), 0, 0)
        source_plan_path = state_dir / APPLY_PLAN_DIRECTORY / f"plan-{expected_plan_sha256}.json"
        inputs: dict[str, Any] = {}
        candidates: tuple[dict[str, Any], ...] = ()
    else:
        if plan_path is None:
            raise RetentionApplyError("retention_apply_plan_invalid")
        source_plan_path = plan_path
        reviewed = _read_plan(source_plan_path, expected_sha256=expected_plan_sha256)
        inputs = _plan_inputs(reviewed)
        state_dir = Path(inputs["activation_journal"]).parent
        durable_plan = (Path(), 0, 0)
    if reviewed is not None:
        inputs = _plan_inputs(reviewed)
        candidates = _candidate_records(reviewed)
    assert state_dir is not None
    lock_path = state_dir / "immutable-release-operator.v1.lock"
    journal_path = state_dir / APPLY_JOURNAL_NAME
    try:
        lock_context = release_operator.OperatorTransactionLock(lock_path)
        with lock_context as lock:
            lock.assert_held()
            existing = _load_journal(journal_path)
            if resume_from_state:
                if existing is None:
                    raise RetentionApplyError("retention_apply_resume_authority_missing")
                cycle_context = _cycle_context_from_record(existing)
                if _cycle_context is not None and cycle_context != _normalize_cycle_context(_cycle_context):
                    raise RetentionApplyError("retention_apply_cycle_invalid")
                reviewed, durable_plan, existing, inputs = _resume_plan_after_live_authority(
                    state_dir,
                    expected_plan_sha256=expected_plan_sha256,
                    guard=lock.assert_held,
                )
                candidates = _candidate_records(reviewed)
            else:
                reviewed = _read_plan(source_plan_path, expected_sha256=expected_plan_sha256)
                inputs = _plan_inputs(reviewed)
                candidates = _candidate_records(reviewed)
                cycle_context = (
                    _normalize_cycle_context(_cycle_context)
                    if _cycle_context is not None
                    else _standalone_cycle_context(reviewed, accepted_path=source_plan_path)
                )
                source_lexical = Path(os.path.abspath(source_plan_path))
                if any(
                    source_lexical == Path(str(candidate["path"])).parent
                    or Path(str(candidate["path"])).parent in source_lexical.parents
                    for candidate in candidates
                ):
                    raise RetentionApplyError("retention_apply_plan_namespace_invalid")
                _live_authority_reauthenticate(reviewed, inputs)
                if (
                    existing is not None
                    and existing.get("phase") != "applied"
                    and existing.get("plan_sha256") != expected_plan_sha256
                ):
                    raise RetentionApplyError("retention_apply_in_progress")
                if existing is not None and existing.get("plan_sha256") == expected_plan_sha256:
                    if _cycle_context_from_record(existing) != cycle_context:
                        raise RetentionApplyError("retention_apply_cycle_invalid")
                    reviewed, durable_plan, existing, inputs = _resume_plan_after_live_authority(
                        state_dir,
                        expected_plan_sha256=expected_plan_sha256,
                        guard=lock.assert_held,
                    )
                    candidates = _candidate_records(reviewed)
                else:
                    dry_run = _new_journal(
                        reviewed,
                        candidates,
                        durable_plan=(source_plan_path, 1, 1),
                        filesystem_before=(),
                        cycle_context=cycle_context,
                    )
                    dry_entries = dry_run["entries"]
                    assert isinstance(dry_entries, list)
                    _validate_mutation_namespaces(
                        plan_path=source_plan_path,
                        state_dir=state_dir,
                        candidates=candidates,
                        entries=dry_entries,
                    )
                    fresh = _fresh_plan_for_cycle(
                        reviewed=reviewed,
                        inputs=inputs,
                        state_dir=state_dir,
                        cycle_context=cycle_context,
                    )
                    if _authority_projection(fresh) != _authority_projection(reviewed):
                        raise RetentionApplyError("retention_apply_plan_drift")
                    if (
                        existing is not None
                        and existing.get("phase") == "applied"
                        and existing.get("plan_sha256") != expected_plan_sha256
                    ):
                        _cleanup_completed_transaction_before_rollover(
                            state_dir,
                            existing,
                            guard=lock.assert_held,
                        )
                    durable_plan = _persist_reviewed_plan(
                        state_dir,
                        reviewed,
                        guard=lock.assert_held,
                        allow_incomplete_stage_repair=(
                            existing is None
                            or (
                                existing.get("phase") == "applied"
                                and existing.get("plan_sha256") != expected_plan_sha256
                            )
                        ),
                    )
            plan_path = durable_plan[0]
            new_transaction = existing is None or (
                existing.get("phase") == "applied" and existing.get("plan_sha256") != expected_plan_sha256
            )
            if new_transaction:
                initial = _new_journal(
                    reviewed,
                    candidates,
                    durable_plan=durable_plan,
                    filesystem_before=_filesystem_free_evidence(
                        candidates,
                        guard=lock.assert_held,
                    ),
                    cycle_context=cycle_context,
                )
                raw_initial_entries = initial["entries"]
                assert isinstance(raw_initial_entries, list)
                _validate_mutation_namespaces(
                    plan_path=source_plan_path,
                    state_dir=state_dir,
                    candidates=candidates,
                    entries=raw_initial_entries,
                )
                journal = _write_journal(
                    journal_path,
                    initial,
                    guard=lock.assert_held,
                )
            else:
                if existing is None:
                    raise RetentionApplyError("retention_apply_journal_invalid")
                journal = existing
                if journal.get("plan_sha256") != expected_plan_sha256:
                    raise RetentionApplyError("retention_apply_in_progress")
            core = _validate_journal_contract(
                journal,
                plan=reviewed,
                candidates=candidates,
                durable_plan=durable_plan,
                cycle_context=cycle_context,
            )
            entries = core.get("entries")
            if (
                not isinstance(entries, list)
                or len(entries) != len(candidates)
                or any(
                    not isinstance(entry, dict)
                    or entry.get("candidate_sha256") != candidate["candidate_sha256"]
                    for entry, candidate in zip(entries, candidates, strict=True)
                )
            ):
                raise RetentionApplyError("retention_apply_journal_invalid")
            _validate_mutation_namespaces(
                plan_path=plan_path,
                state_dir=state_dir,
                candidates=candidates,
                entries=entries,
                require_quarantine_absent=False,
            )
            if not resume_from_state:
                _validate_mutation_namespaces(
                    plan_path=source_plan_path,
                    state_dir=state_dir,
                    candidates=candidates,
                    entries=entries,
                    require_quarantine_absent=False,
                )
            for entry in entries:
                if core.get("phase") != "applied" and entry.get("status") != "pending":
                    _load_residual_authority(
                        entry.get("residual_authority"),
                        state_dir=state_dir,
                    )
            if core.get("phase") == "applied":
                authenticated = _post_apply_reauthenticate(reviewed, inputs)
                _terminal_absence(candidates, entries, guard=lock.assert_held)
                receipt = _result_receipt(
                    plan=reviewed,
                    journal=core,
                    candidates=candidates,
                    authority_bindings_sha256=authenticated["authority_bindings"]["bindings_sha256"],
                )
                if core.get("receipt_sha256") != receipt["receipt_sha256"]:
                    raise RetentionApplyError("retention_apply_journal_invalid")
                _live_authority_reauthenticate(reviewed, inputs)
                published = _publish_receipt(state_dir, receipt, guard=lock.assert_held)
                _cleanup_object_authorities(state_dir, entries, guard=lock.assert_held)
                return published

            if core.get("phase") == "prepared":
                core["phase"] = "applying"
                journal = _write_journal(journal_path, core, guard=lock.assert_held)
                core = _journal_core(journal)
                entries = core["entries"]
            if core.get("phase") != "applying":
                raise RetentionApplyError("retention_apply_journal_invalid")

            _live_authority_reauthenticate(reviewed, inputs)
            _resume_candidate_reauthenticate(candidates, entries, state_dir=state_dir)

            for candidate, entry in zip(candidates, entries, strict=True):
                if entry.get("status") != "deleted":
                    _preflight_filesystem_lease(candidate, guard=lock.assert_held)
                if entry.get("status") != "pending":
                    _load_residual_authority(entry.get("residual_authority"), state_dir=state_dir)

            prepared = False
            for index, (candidate, entry) in enumerate(zip(candidates, entries, strict=True)):
                if entry.get("status") != "pending":
                    continue
                source = Path(str(candidate["path"]))
                snapshot, objects_sha256, tree_sha256 = _candidate_matches_observation(candidate, source)
                entry["residual_authority"] = _persist_residual_authority(
                    state_dir,
                    str(core["transaction_id"]),
                    index,
                    snapshot,
                    guard=lock.assert_held,
                )
                entry["objects_sha256"] = objects_sha256
                entry["tree_sha256"] = tree_sha256
                entry["status"] = "renaming"
                prepared = True
            if prepared:
                journal = _write_journal(journal_path, core, guard=lock.assert_held)
                core = _journal_core(journal)
                entries = core["entries"]

            _live_authority_reauthenticate(reviewed, inputs)
            try:
                for index, candidate in enumerate(candidates):
                    entry = entries[index]
                    lock.assert_held()
                    status = entry.get("status")
                    if status in {"deleted", "deleting"}:
                        continue
                    if status not in {"renaming", "sealed"}:
                        raise RetentionApplyError("retention_apply_journal_invalid")
                    source = Path(str(candidate["path"]))
                    quarantine_name = str(entry["quarantine_name"])
                    quarantine = source.parent / quarantine_name
                    if status == "renaming":
                        _fault("before_rename")
                        root_fd, _parts, _identities = _root_descriptor(candidate)
                        try:
                            source_identity = _named_identity(root_fd, source.name)
                            quarantine_identity = _named_identity(root_fd, quarantine_name)
                            expected_identity = (candidate["device"], candidate["inode"])
                            if source_identity == expected_identity and quarantine_identity is None:
                                lock.assert_held()
                                try:
                                    _rename_noreplace(root_fd, source.name, root_fd, quarantine_name)
                                except OSError as exc:
                                    raise RetentionApplyError("retention_apply_target_raced") from exc
                                os.fsync(root_fd)
                                lock.assert_held()
                            elif source_identity is not None or quarantine_identity != expected_identity:
                                raise RetentionApplyError("retention_apply_target_raced")
                        finally:
                            os.close(root_fd)
                        _fault("after_rename")
                        entry["sealed_tree_sha256"] = _seal_quarantine(
                            candidate,
                            quarantine_name,
                            objects_sha256=str(entry["objects_sha256"]),
                            tree_sha256=str(entry["tree_sha256"]),
                            guard=lock.assert_held,
                        )
                        entry["status"] = "sealed"
                        journal = _write_journal(journal_path, core, guard=lock.assert_held)
                        core = _journal_core(journal)
                        entries = core["entries"]
                        _fault("after_quarantine")
                    else:
                        _sealed_quarantine_matches(
                            candidate,
                            quarantine,
                            objects_sha256=str(entry["objects_sha256"]),
                            tree_sha256=str(entry["tree_sha256"]),
                            sealed_tree_sha256=str(entry["sealed_tree_sha256"]),
                        )
            except RetentionApplyError:
                if all(entry.get("status") not in {"deleting", "deleted"} for entry in entries):
                    _restore_full_batch(
                        candidates,
                        core,
                        journal_path=journal_path,
                        guard=lock.assert_held,
                    )
                raise

            probe_paths = tuple(
                Path(str(candidate["path"])).parent / str(entry["quarantine_name"])
                for candidate, entry in zip(candidates, entries, strict=True)
                if entry.get("status") in {"sealed", "deleting"}
                and (Path(str(candidate["path"])).parent / str(entry["quarantine_name"])).exists()
            )
            try:
                if probe_paths:
                    inventory = retention.build_complete_open_inventory(target_paths=probe_paths)
                    if inventory.open_paths:
                        raise RetentionApplyError("retention_apply_open_reference")
            except retention.RetentionPlanError as exc:
                if all(entry.get("status") not in {"deleting", "deleted"} for entry in entries):
                    core = _restore_full_batch(
                        candidates,
                        core,
                        journal_path=journal_path,
                        guard=lock.assert_held,
                    )
                raise RetentionApplyError("retention_apply_open_recheck_failed") from exc
            except RetentionApplyError:
                if all(entry.get("status") not in {"deleting", "deleted"} for entry in entries):
                    core = _restore_full_batch(
                        candidates,
                        core,
                        journal_path=journal_path,
                        guard=lock.assert_held,
                    )
                raise

            deleting_started = False
            for candidate, entry in zip(candidates, entries, strict=True):
                if entry.get("status") != "sealed":
                    continue
                quarantine = Path(str(candidate["path"])).parent / str(entry["quarantine_name"])
                residual_authority = _load_residual_authority(
                    entry.get("residual_authority"),
                    state_dir=state_dir,
                )
                _partial_quarantine_contour(
                    candidate,
                    quarantine,
                    residual_authority=residual_authority,
                )
                entry["status"] = "deleting"
                deleting_started = True
            if deleting_started:
                journal = _write_journal(journal_path, core, guard=lock.assert_held)
                core = _journal_core(journal)
                entries = core["entries"]
                _fault("before_delete")

            for index, candidate in enumerate(candidates):
                entry = entries[index]
                if entry.get("status") == "deleted":
                    continue
                if entry.get("status") != "deleting":
                    raise RetentionApplyError("retention_apply_journal_invalid")
                quarantine_name = str(entry["quarantine_name"])
                residual_authority = _load_residual_authority(
                    entry.get("residual_authority"),
                    state_dir=state_dir,
                )
                actual_bytes, actual_inodes = _delete_quarantine(
                    candidate,
                    quarantine_name,
                    residual_authority=residual_authority,
                    guard=lock.assert_held,
                )
                _fault("after_delete")
                if actual_bytes != candidate["recursive_bytes"] or actual_inodes != candidate["entry_count"]:
                    raise RetentionApplyError("retention_apply_delete_accounting_mismatch")
                entry["actual_bytes"] = actual_bytes
                entry["actual_allocated_bytes"] = int(candidate["allocated_bytes"])
                entry["actual_inodes"] = actual_inodes
                entry["status"] = "deleted"
                journal = _write_journal(journal_path, core, guard=lock.assert_held)
                core = _journal_core(journal)
                entries = core["entries"]

            _terminal_absence(candidates, entries, guard=lock.assert_held)
            authenticated = _post_apply_reauthenticate(reviewed, inputs)
            if not core.get("filesystem_after") and candidates:
                core["filesystem_after"] = _filesystem_free_evidence(
                    candidates,
                    guard=lock.assert_held,
                )
                journal = _write_journal(journal_path, core, guard=lock.assert_held)
                core = _journal_core(journal)
            receipt = _result_receipt(
                plan=reviewed,
                journal=core,
                candidates=candidates,
                authority_bindings_sha256=authenticated["authority_bindings"]["bindings_sha256"],
            )
            _fault("before_receipt_publish")
            _live_authority_reauthenticate(reviewed, inputs)
            _publish_receipt(state_dir, receipt, guard=lock.assert_held)
            _fault("after_receipt_publish")
            core["phase"] = "applied"
            core["receipt_sha256"] = receipt["receipt_sha256"]
            journal = _write_journal(journal_path, core, guard=lock.assert_held)
            core = _journal_core(journal)
            _fault("after_applied_journal_before_cleanup")
            applied_entries = core.get("entries")
            if not isinstance(applied_entries, list):
                raise RetentionApplyError("retention_apply_journal_invalid")
            _cleanup_object_authorities(state_dir, applied_entries, guard=lock.assert_held)
            lock.assert_held()
            return receipt
    except RetentionApplyError:
        raise
    except release_operator.ReleaseFailure as exc:
        raise RetentionApplyError("retention_apply_operator_lock_failed") from exc
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_authority_failed") from exc


def _validate_friday_home(home: Path) -> Path:
    lexical = Path(os.path.abspath(home))
    try:
        status = os.lstat(home)
        resolved = home.resolve(strict=True)
    except OSError as exc:
        raise RetentionApplyError("retention_admission_friday_home_invalid") from exc
    if (
        not home.is_absolute()
        or home != lexical
        or resolved != home
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise RetentionApplyError("retention_admission_friday_home_invalid")
    return home


def _canonical_friday_home() -> Path:
    raw = os.environ.get("FRIDAY_HOME")
    if not raw or any(character in raw for character in "\x00\r\n"):
        raise RetentionApplyError("retention_admission_friday_home_invalid")
    return _validate_friday_home(Path(raw))


def _generation_candidate_sha256(candidate: Mapping[str, Any]) -> str:
    try:
        normalized = dr_index.normalize_generation_candidate(candidate)
    except dr_index.DRGenerationIndexError as exc:
        raise RetentionApplyError("retention_admission_dr_index_invalid") from exc
    return hashlib.sha256(_canonical(normalized)).hexdigest()


def _retention_epoch_locked(
    *,
    state_dir: Path,
    activation_receipt: Path,
    guard: Callable[[], None],
) -> tuple[dict[str, Any], str]:
    activation_journal = state_dir / "immutable-release-activation.v1.json"
    backup_root = state_dir.parent / "backups"
    index = dr_index.DurableDRGenerationIndex(state_dir)
    guard()
    first = dr_auth._authenticate_locked(  # noqa: SLF001
        activation_journal=activation_journal,
        activation_receipt=activation_receipt,
        backup_root=backup_root,
    )
    guard()
    state = index.load()
    guard()
    if state.get("phase") != "clear":
        raise RetentionApplyError("retention_admission_dr_index_invalid")
    current = index.current_generation_identity(
        expected_journal_sha256=str(state.get("journal_sha256") or ""),
    )
    if current is None:
        raise RetentionApplyError("retention_admission_dr_index_invalid")
    try:
        authentication_reference, _raw, _body = dr_index.validate_authentication_receipt(
            first.authentication_receipt,
            candidate=first.candidate,
        )
    except dr_index.DRGenerationIndexError as exc:
        raise RetentionApplyError("retention_admission_dr_index_invalid") from exc
    if (
        current.candidate != first.candidate
        or current.candidate_sha256 != _generation_candidate_sha256(first.candidate)
        or current.authentication_receipt != authentication_reference
    ):
        raise RetentionApplyError("retention_admission_activation_mismatch")
    snapshot = index.authority_snapshot()
    guard()
    second = dr_auth._authenticate_locked(  # noqa: SLF001
        activation_journal=activation_journal,
        activation_receipt=activation_receipt,
        backup_root=backup_root,
    )
    state_after = index.load()
    snapshot_after = index.authority_snapshot()
    guard()
    if second != first or state_after != state or snapshot_after != snapshot:
        raise RetentionApplyError("retention_admission_source_changed")

    pins = {pin.role: pin for pin in snapshot.pins}
    current_pin = pins.get("current")
    older_pin = pins.get("older")
    current_ref = state.get("current")
    older_ref = state.get("older")
    if (
        current_pin is None
        or not isinstance(current_ref, Mapping)
        or current_pin.generation_id != current_ref.get("generation_id")
        or current_pin.receipt_sha256 != current_ref.get("receipt_sha256")
        or (older_pin is None) != (older_ref is None)
        or (
            older_pin is not None
            and (
                not isinstance(older_ref, Mapping)
                or older_pin.generation_id != older_ref.get("generation_id")
                or older_pin.receipt_sha256 != older_ref.get("receipt_sha256")
                or current_pin.generation_id == older_pin.generation_id
            )
        )
    ):
        raise RetentionApplyError("retention_admission_dr_topology_invalid")
    current_v2 = current_pin.activation_receipt_file_sha256 is not None
    older_v2 = older_pin is not None and older_pin.activation_receipt_file_sha256 is not None
    anchor = state.get("preactivation_anchor")
    first_v2 = (
        current_v2
        and not older_v2
        and older_pin is not None
        and isinstance(anchor, Mapping)
        and dict(current_ref) == anchor.get("first_v2_generation")
        and isinstance(older_ref, Mapping)
        and dict(older_ref) == anchor.get("legacy_generation")
    )
    two_v2 = current_v2 and older_v2 and older_pin is not None and isinstance(anchor, Mapping)
    topology = "first_v2" if first_v2 else "two_v2" if two_v2 else "pre_v2"
    activation_file_sha256 = first.authentication_receipt.get("activation_receipt_file_sha256")
    activation_sha256 = first.authentication_receipt.get("activation_receipt_sha256")
    if (
        not _is_hex64(activation_file_sha256)
        or not _is_hex64(activation_sha256)
        or (current_v2 and current_pin.activation_receipt_file_sha256 != activation_file_sha256)
    ):
        raise RetentionApplyError("retention_admission_activation_mismatch")
    try:
        scope = retention.load_retention_scope_authority(activation_journal=activation_journal)
    except retention.RetentionPlanError as exc:
        if topology == "two_v2":
            raise RetentionApplyError("retention_admission_scope_required") from exc
        # Bootstrap/pre-v2 admission never grants deletion authority.  It may
        # therefore bridge the one-time first-v2 transition before scope
        # provisioning, while two-v2 review always requires the exact scope.
        scope = None
    guard()
    if (
        scope is not None
        and retention.load_retention_scope_authority(activation_journal=activation_journal) != scope
    ):
        raise RetentionApplyError("retention_admission_source_changed")
    guard()
    projection = {
        "activation_receipt_file_sha256": activation_file_sha256,
        "activation_receipt_sha256": activation_sha256,
        "current_candidate_sha256": _generation_candidate_sha256(current_pin.candidate),
        "current_generation_id": current_pin.generation_id,
        "current_generation_receipt_sha256": current_pin.receipt_sha256,
        "index_journal_sha256": state["journal_sha256"],
        "index_revision": state["revision"],
        "older_candidate_sha256": (
            _generation_candidate_sha256(older_pin.candidate) if older_pin is not None else ""
        ),
        "older_generation_id": older_pin.generation_id if older_pin is not None else "",
        "older_generation_receipt_sha256": (older_pin.receipt_sha256 if older_pin is not None else ""),
        "retention_scope_schema": scope.receipt["schema"] if scope is not None else "",
        "retention_scope_sha256": scope.receipt["file_sha256"] if scope is not None else "",
        "topology": topology,
    }
    return projection, hashlib.sha256(_canonical(projection)).hexdigest()


def _retention_epoch(
    *,
    state_dir: Path,
    activation_receipt: Path,
) -> tuple[dict[str, Any], str]:
    try:
        canonical_state = retention._strict_private_directory(  # noqa: SLF001
            state_dir,
            code="retention_admission_friday_home_invalid",
        )
        with release_operator.OperatorTransactionLock(
            canonical_state / "immutable-release-operator.v1.lock"
        ) as transaction:
            return _retention_epoch_locked(
                state_dir=canonical_state,
                activation_receipt=activation_receipt,
                guard=transaction.assert_held,
            )
    except RetentionApplyError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
        retention.RetentionPlanError,
    ) as exc:
        raise RetentionApplyError("retention_admission_reauthentication_failed") from exc


def _read_apply_receipt_path(path: Path) -> dict[str, Any]:
    try:
        raw = retention._stable_file_bytes(  # noqa: SLF001
            path,
            private=True,
            code="retention_apply_receipt_invalid",
            maximum_bytes=MAX_PLAN_BYTES,
        )
        value = retention._unique_json(raw, code="retention_apply_receipt_invalid")  # noqa: SLF001
    except retention.RetentionPlanError as exc:
        raise RetentionApplyError("retention_apply_receipt_invalid") from exc
    if raw != _canonical(value) + b"\n":
        raise RetentionApplyError("retention_apply_receipt_invalid")
    return _validate_apply_receipt(value)


def _read_apply_receipt(
    state_dir: Path,
    *,
    transaction_id: str,
) -> dict[str, Any]:
    if not _is_hex64(transaction_id):
        raise RetentionApplyError("retention_apply_receipt_invalid")
    return _read_apply_receipt_path(state_dir / APPLY_RECEIPT_DIRECTORY / f"receipt-{transaction_id}.json")


def _find_apply_receipt_by_sha256(state_dir: Path, receipt_sha256: str) -> dict[str, Any]:
    if not _is_hex64(receipt_sha256):
        raise RetentionApplyError("retention_apply_receipt_invalid")
    directory = state_dir / APPLY_RECEIPT_DIRECTORY
    try:
        names = sorted(
            name for name in os.listdir(directory) if name.startswith("receipt-") and name.endswith(".json")
        )
    except OSError as exc:
        raise RetentionApplyError("retention_apply_receipt_invalid") from exc
    if len(names) > MAX_DELETE_ENTRIES:
        raise RetentionApplyError("retention_apply_receipt_invalid")
    matches: list[dict[str, Any]] = []
    for name in names:
        transaction = name.removeprefix("receipt-").removesuffix(".json")
        if not _is_hex64(transaction):
            raise RetentionApplyError("retention_apply_receipt_invalid")
        receipt = _read_apply_receipt_path(directory / name)
        if receipt["receipt_sha256"] == receipt_sha256:
            matches.append(receipt)
    if len(matches) != 1:
        raise RetentionApplyError("retention_apply_receipt_invalid")
    return matches[0]


def _candidate_identity_digests(
    plan: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> set[str]:
    collections: dict[str, str] = {}
    for collection in ("targets", "backup_targets"):
        records = plan.get(collection)
        if not isinstance(records, list):
            raise RetentionApplyError("retention_apply_plan_invalid")
        for record in records:
            if isinstance(record, Mapping) and record.get("decision") == "delete_candidate":
                path = str(record.get("path") or "")
                if path in collections:
                    raise RetentionApplyError("retention_apply_plan_invalid")
                collections[path] = collection
    return {
        _reviewed_identity_sha256(candidate, collection=collections[str(candidate["path"])])
        for candidate in candidates
    }


def _validate_apply_receipt_plan(
    state_dir: Path,
    receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    plan_sha256 = str(receipt.get("plan_sha256") or "")
    plan = _read_plan(
        state_dir / APPLY_PLAN_DIRECTORY / f"plan-{plan_sha256}.json",
        expected_sha256=plan_sha256,
    )
    candidates = _candidate_records(plan)
    candidate_sha256s = [candidate["candidate_sha256"] for candidate in candidates]
    authority = plan.get("authority_bindings")
    expected_transaction = hashlib.sha256(
        _canonical(
            {
                "batch_ordinal": receipt["batch_ordinal"],
                "cycle_sha256": receipt["cycle_sha256"],
                "plan_sha256": plan_sha256,
                "previous_receipt_sha256": receipt["previous_receipt_sha256"],
                "schema": APPLY_JOURNAL_SCHEMA,
            }
        )
    ).hexdigest()
    authenticated_bytes = sum(int(candidate["recursive_bytes"]) for candidate in candidates)
    authenticated_allocated = sum(int(candidate["allocated_bytes"]) for candidate in candidates)
    authenticated_inodes = sum(int(candidate["entry_count"]) for candidate in candidates)
    if (
        receipt.get("transaction_id") != expected_transaction
        or receipt.get("candidate_set_sha256") != hashlib.sha256(_canonical(candidate_sha256s)).hexdigest()
        or receipt.get("deleted_candidate_count") != len(candidates)
        or receipt.get("pre_delete_authenticated_bytes") != authenticated_bytes
        or receipt.get("actual_deleted_logical_bytes") != authenticated_bytes
        or receipt.get("pre_delete_authenticated_allocated_bytes") != authenticated_allocated
        or receipt.get("deleted_authenticated_allocated_bytes") != authenticated_allocated
        or receipt.get("pre_delete_authenticated_inodes") != authenticated_inodes
        or receipt.get("actual_deleted_inodes") != authenticated_inodes
        or not isinstance(authority, Mapping)
        or receipt.get("authority_bindings_sha256") != authority.get("bindings_sha256")
        or receipt.get("retention_scope_schema") != plan["retention_scope"]["schema"]
        or receipt.get("retention_scope_sha256") != plan["retention_scope"]["file_sha256"]
        or (
            receipt.get("admission_status") == "release_admissible"
            and not _is_exact_terminal_zero_plan(plan, candidates)
        )
        or (
            receipt.get("admission_status") == "nonterminal"
            and bool(candidates) != (receipt.get("admission_reason") == "effectful_applied")
        )
    ):
        raise RetentionApplyError("retention_apply_receipt_invalid")
    return plan, candidates


def _applied_cycle_identities(
    state_dir: Path,
    *,
    latest_receipt: Mapping[str, Any],
    accepted_root: Mapping[str, Any],
    cycle_context: Mapping[str, Any],
) -> set[str]:
    cycle = _normalize_cycle_context(cycle_context)
    accepted = {
        hashlib.sha256(_canonical(item)).hexdigest() for item in _reviewed_candidate_identities(accepted_root)
    }
    applied: set[str] = set()
    receipt = dict(latest_receipt)
    expected_ordinal = int(receipt.get("batch_ordinal", -1))
    if expected_ordinal < 0:
        raise RetentionApplyError("retention_convergence_chain_invalid")
    while True:
        plan, candidates = _validate_apply_receipt_plan(state_dir, receipt)
        identities = _candidate_identity_digests(plan, candidates)
        if (
            receipt.get("cycle_sha256") != cycle["cycle_sha256"]
            or receipt.get("accepted_root_plan_sha256") != cycle["accepted_root_plan_sha256"]
            or receipt.get("reviewed_full_candidate_set_sha256")
            != cycle["reviewed_full_candidate_set_sha256"]
            or receipt.get("retention_epoch_sha256") != cycle["retention_epoch_sha256"]
            or receipt.get("batch_ordinal") != expected_ordinal
            or _cycle_authority_projection(plan) != _cycle_authority_projection(accepted_root)
            or not identities.issubset(accepted)
            or applied.intersection(identities)
        ):
            raise RetentionApplyError("retention_convergence_chain_invalid")
        applied.update(identities)
        previous = str(receipt["previous_receipt_sha256"])
        if expected_ordinal == 0:
            if previous != "":
                raise RetentionApplyError("retention_convergence_chain_invalid")
            return applied
        receipt = _find_apply_receipt_by_sha256(state_dir, previous)
        expected_ordinal -= 1


def _applied_cycle_identities_locked(
    state_dir: Path,
    *,
    latest_receipt: Mapping[str, Any],
    accepted_root: Mapping[str, Any],
    cycle_context: Mapping[str, Any],
) -> set[str]:
    try:
        with release_operator.OperatorTransactionLock(
            state_dir / "immutable-release-operator.v1.lock"
        ) as transaction:
            transaction.assert_held()
            result = _applied_cycle_identities(
                state_dir,
                latest_receipt=latest_receipt,
                accepted_root=accepted_root,
                cycle_context=cycle_context,
            )
            transaction.assert_held()
            return result
    except release_operator.ReleaseFailure as exc:
        raise RetentionApplyError("retention_apply_operator_lock_failed") from exc


def _validated_terminal_chain(
    state_dir: Path,
    *,
    retention_epoch_sha256: str,
    guard: Callable[[], None],
) -> dict[str, Any] | None:
    journal = _load_journal(state_dir / APPLY_JOURNAL_NAME)
    if journal is None:
        return None
    cycle = _cycle_context_from_record(journal)
    plan_sha256 = str(journal.get("plan_sha256") or "")
    reviewed, durable_plan, loaded = _resume_plan_from_state(
        state_dir,
        expected_plan_sha256=plan_sha256,
        guard=guard,
        repair_staged_publication=False,
    )
    candidates = _candidate_records(reviewed)
    core = _validate_journal_contract(
        loaded,
        plan=reviewed,
        candidates=candidates,
        durable_plan=durable_plan,
        cycle_context=cycle,
    )
    if core.get("phase") != "applied":
        return None
    authority = reviewed.get("authority_bindings")
    if not isinstance(authority, Mapping) or not _is_hex64(authority.get("bindings_sha256")):
        raise RetentionApplyError("retention_apply_journal_invalid")
    expected = _result_receipt(
        plan=reviewed,
        journal=core,
        candidates=candidates,
        authority_bindings_sha256=str(authority["bindings_sha256"]),
    )
    terminal = _read_apply_receipt(
        state_dir,
        transaction_id=str(core["transaction_id"]),
    )
    if terminal != expected or core.get("receipt_sha256") != terminal["receipt_sha256"]:
        raise RetentionApplyError("retention_apply_receipt_invalid")
    if (
        terminal.get("admission_status") != "release_admissible"
        or terminal.get("retention_epoch_sha256") != retention_epoch_sha256
    ):
        return None

    accepted_root = _load_accepted_root_plan(state_dir, cycle)
    _preflight_reviewed_root(accepted_root)
    accepted_identities = {
        hashlib.sha256(_canonical(item)).hexdigest() for item in _reviewed_candidate_identities(accepted_root)
    }
    applied_identities: set[str] = set()
    receipt = terminal
    expected_ordinal = int(terminal["batch_ordinal"])
    while True:
        guard()
        plan, batch_candidates = _validate_apply_receipt_plan(state_dir, receipt)
        if (
            _cycle_authority_projection(plan) != _cycle_authority_projection(accepted_root)
            or receipt.get("authority_bindings_sha256")
            != accepted_root["authority_bindings"]["bindings_sha256"]
            or receipt.get("retention_scope_sha256") != accepted_root["retention_scope"]["file_sha256"]
            or receipt.get("retention_scope_schema") != accepted_root["retention_scope"]["schema"]
            or receipt.get("cycle_sha256") != cycle["cycle_sha256"]
            or receipt.get("accepted_root_plan_sha256") != cycle["accepted_root_plan_sha256"]
            or receipt.get("reviewed_full_candidate_set_sha256")
            != cycle["reviewed_full_candidate_set_sha256"]
            or receipt.get("retention_epoch_sha256") != cycle["retention_epoch_sha256"]
            or receipt.get("batch_ordinal") != expected_ordinal
        ):
            raise RetentionApplyError("retention_convergence_chain_invalid")
        identities = _candidate_identity_digests(plan, batch_candidates)
        if not identities.issubset(accepted_identities) or applied_identities.intersection(identities):
            raise RetentionApplyError("retention_convergence_chain_invalid")
        applied_identities.update(identities)
        previous = str(receipt["previous_receipt_sha256"])
        if receipt.get("admission_status") != (
            "release_admissible" if receipt is terminal else "nonterminal"
        ):
            raise RetentionApplyError("retention_convergence_chain_invalid")
        if expected_ordinal == 0:
            if previous != "":
                raise RetentionApplyError("retention_convergence_chain_invalid")
            break
        receipt = _find_apply_receipt_by_sha256(state_dir, previous)
        expected_ordinal -= 1
    if applied_identities != accepted_identities:
        raise RetentionApplyError("retention_convergence_chain_invalid")
    return {
        "accepted_root_plan_sha256": cycle["accepted_root_plan_sha256"],
        "batch_ordinal": terminal["batch_ordinal"],
        "cycle_sha256": cycle["cycle_sha256"],
        "reviewed_full_candidate_set_sha256": cycle["reviewed_full_candidate_set_sha256"],
        "terminal_apply_receipt_sha256": terminal["receipt_sha256"],
    }


_RETENTION_EPOCH_KEYS = frozenset(
    {
        "activation_receipt_file_sha256",
        "activation_receipt_sha256",
        "current_candidate_sha256",
        "current_generation_id",
        "current_generation_receipt_sha256",
        "index_journal_sha256",
        "index_revision",
        "older_candidate_sha256",
        "older_generation_id",
        "older_generation_receipt_sha256",
        "retention_scope_schema",
        "retention_scope_sha256",
        "topology",
    }
)


def _validate_retention_epoch_projection(epoch: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(epoch, Mapping):
        raise RetentionApplyError("retention_admission_status_invalid")
    projection = dict(epoch)
    topology = projection.get("topology")
    scope_schema = projection.get("retention_scope_schema")
    scope_sha256 = projection.get("retention_scope_sha256")
    scope_absent = scope_schema == "" and scope_sha256 == ""
    scope_present = scope_schema == retention.RETENTION_SCOPE_SCHEMA and _is_hex64(scope_sha256)
    if (
        set(projection) != _RETENTION_EPOCH_KEYS
        or topology not in {"first_v2", "two_v2"}
        or any(
            not _is_hex64(projection.get(key))
            for key in (
                "activation_receipt_file_sha256",
                "activation_receipt_sha256",
                "current_candidate_sha256",
                "current_generation_id",
                "current_generation_receipt_sha256",
                "index_journal_sha256",
                "older_candidate_sha256",
                "older_generation_id",
                "older_generation_receipt_sha256",
            )
        )
        or type(projection.get("index_revision")) is not int
        or int(projection["index_revision"]) < 0
        or (topology == "first_v2" and not (scope_absent or scope_present))
        or (topology == "two_v2" and not scope_present)
    ):
        raise RetentionApplyError("retention_admission_status_invalid")
    return projection


def _convergence_receipt(
    *,
    epoch: Mapping[str, Any],
    status: str,
    terminal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"converged", "first_v2_deferred", "in_progress", "review_required"}:
        raise RetentionApplyError("retention_admission_status_invalid")
    epoch = _validate_retention_epoch_projection(epoch)
    if (status == "first_v2_deferred") != (epoch["topology"] == "first_v2"):
        raise RetentionApplyError("retention_admission_status_invalid")
    terminal_values = dict(terminal or {})
    core = {
        "accepted_root_plan_sha256": terminal_values.get("accepted_root_plan_sha256", ""),
        "activation_receipt_file_sha256": epoch["activation_receipt_file_sha256"],
        "activation_receipt_sha256": epoch["activation_receipt_sha256"],
        "batch_ordinal": terminal_values.get("batch_ordinal", -1),
        "current_candidate_sha256": epoch["current_candidate_sha256"],
        "current_generation_id": epoch["current_generation_id"],
        "current_generation_receipt_sha256": epoch["current_generation_receipt_sha256"],
        "cycle_sha256": terminal_values.get("cycle_sha256", ""),
        "index_journal_sha256": epoch["index_journal_sha256"],
        "index_revision": epoch["index_revision"],
        "older_candidate_sha256": epoch["older_candidate_sha256"],
        "older_generation_id": epoch["older_generation_id"],
        "older_generation_receipt_sha256": epoch["older_generation_receipt_sha256"],
        "retention_scope_schema": epoch["retention_scope_schema"],
        "retention_scope_sha256": epoch["retention_scope_sha256"],
        "reviewed_full_candidate_set_sha256": terminal_values.get("reviewed_full_candidate_set_sha256", ""),
        "schema": CONVERGENCE_RECEIPT_SCHEMA,
        "status": status,
        "terminal_apply_receipt_sha256": terminal_values.get("terminal_apply_receipt_sha256", ""),
    }
    if status in {"converged", "in_progress"}:
        if (
            epoch.get("topology") != "two_v2"
            or not _is_hex64(core["accepted_root_plan_sha256"])
            or type(core["batch_ordinal"]) is not int
            or int(core["batch_ordinal"]) < (-1 if status == "in_progress" else 0)
            or any(
                not _is_hex64(core[key])
                for key in (
                    "cycle_sha256",
                    "reviewed_full_candidate_set_sha256",
                )
            )
            or (status == "converged" and not _is_hex64(core["terminal_apply_receipt_sha256"]))
            or (
                status == "in_progress"
                and core["terminal_apply_receipt_sha256"] != ""
                and not _is_hex64(core["terminal_apply_receipt_sha256"])
            )
        ):
            raise RetentionApplyError("retention_admission_status_invalid")
    elif any(
        (
            core["accepted_root_plan_sha256"] != "",
            core["batch_ordinal"] != -1,
            core["cycle_sha256"] != "",
            core["reviewed_full_candidate_set_sha256"] != "",
            core["terminal_apply_receipt_sha256"] != "",
        )
    ):
        raise RetentionApplyError("retention_admission_status_invalid")
    return _receipt_with_digest(core)


def retention_release_admission(*, activation_receipt: Path) -> dict[str, Any]:
    """Derive the exact release admission from authenticated durable state."""

    home = _canonical_friday_home()
    try:
        with release_operator.OperatorTransactionLock(
            home / "data/state/immutable-release-operator.v1.lock"
        ) as transaction:
            return _retention_release_admission_locked(
                activation_receipt=activation_receipt,
                friday_home=home,
                namespace_guard=transaction.assert_held,
            )
    except RetentionApplyError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
        retention.RetentionPlanError,
    ) as exc:
        raise RetentionApplyError("retention_admission_reauthentication_failed") from exc


def _retention_release_admission_locked(
    *,
    activation_receipt: Path,
    friday_home: Path,
    namespace_guard: Callable[[], None],
) -> dict[str, Any]:
    """Locked adapter for install-units and other lock-owning release callers."""

    home = _validate_friday_home(friday_home)
    try:
        state_dir = retention._strict_private_directory(  # noqa: SLF001
            home / "data/state",
            code="retention_admission_friday_home_invalid",
        )
        namespace_guard()
        epoch, epoch_sha256 = _retention_epoch_locked(
            state_dir=state_dir,
            activation_receipt=activation_receipt,
            guard=namespace_guard,
        )
        topology = epoch.get("topology")
        if topology == "first_v2":
            result = _convergence_receipt(epoch=epoch, status="first_v2_deferred")
        elif topology == "two_v2":
            terminal = _validated_terminal_chain(
                state_dir,
                retention_epoch_sha256=epoch_sha256,
                guard=namespace_guard,
            )
            result = _convergence_receipt(
                epoch=epoch,
                status="converged" if terminal is not None else "review_required",
                terminal=terminal,
            )
        else:
            raise RetentionApplyError("retention_admission_dr_topology_invalid")
        namespace_guard()
        return result
    except RetentionApplyError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
        retention.RetentionPlanError,
    ) as exc:
        raise RetentionApplyError("retention_admission_reauthentication_failed") from exc


def _current_activation_receipt_path(state_dir: Path) -> Path:
    try:
        snapshot = dr_index.DurableDRGenerationIndex(state_dir).authority_snapshot()
    except dr_index.DRGenerationIndexError as exc:
        raise RetentionApplyError("retention_admission_dr_index_invalid") from exc
    current = next((pin for pin in snapshot.pins if pin.role == "current"), None)
    if current is None or current.activation_receipt_path is None:
        raise RetentionApplyError("retention_convergence_v2_required")
    return Path(current.activation_receipt_path)


def _journal_apply_receipt(state_dir: Path, journal: Mapping[str, Any]) -> dict[str, Any]:
    transaction = str(journal.get("transaction_id") or "")
    receipt = _read_apply_receipt(state_dir, transaction_id=transaction)
    if journal.get("receipt_sha256") != receipt.get("receipt_sha256"):
        raise RetentionApplyError("retention_apply_receipt_invalid")
    _validate_apply_receipt_plan(state_dir, receipt)
    return receipt


def _stage_generated_plan(state_dir: Path, plan: Mapping[str, Any]) -> Path:
    lock_path = state_dir / "immutable-release-operator.v1.lock"
    try:
        with release_operator.OperatorTransactionLock(lock_path) as transaction:
            durable = _persist_reviewed_plan(
                state_dir,
                plan,
                guard=transaction.assert_held,
                allow_incomplete_stage_repair=True,
            )
            return durable[0]
    except release_operator.ReleaseFailure as exc:
        raise RetentionApplyError("retention_apply_operator_lock_failed") from exc


def _progress_values(
    cycle_context: Mapping[str, Any],
    *,
    latest_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cycle = _normalize_cycle_context(cycle_context)
    return {
        "accepted_root_plan_sha256": cycle["accepted_root_plan_sha256"],
        "batch_ordinal": (latest_receipt["batch_ordinal"] if latest_receipt is not None else -1),
        "cycle_sha256": cycle["cycle_sha256"],
        "reviewed_full_candidate_set_sha256": cycle["reviewed_full_candidate_set_sha256"],
        "terminal_apply_receipt_sha256": (
            latest_receipt["receipt_sha256"] if latest_receipt is not None else ""
        ),
    }


def _converged_receipt_for_state(
    *,
    state_dir: Path,
    activation_receipt: Path,
) -> dict[str, Any]:
    with release_operator.OperatorTransactionLock(
        state_dir / "immutable-release-operator.v1.lock"
    ) as transaction:
        epoch, epoch_sha256 = _retention_epoch_locked(
            state_dir=state_dir,
            activation_receipt=activation_receipt,
            guard=transaction.assert_held,
        )
        terminal = _validated_terminal_chain(
            state_dir,
            retention_epoch_sha256=epoch_sha256,
            guard=transaction.assert_held,
        )
        if terminal is None:
            raise RetentionApplyError("retention_convergence_terminal_invalid")
        result = _convergence_receipt(epoch=epoch, status="converged", terminal=terminal)
        transaction.assert_held()
        return result


def converge_retention_cycle(
    *,
    reviewed_plan_path: Path,
    expected_reviewed_plan_sha256: str,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply at most sixteen reviewed effectful batches, then require fresh zero."""

    if not _is_hex64(expected_reviewed_plan_sha256):
        raise RetentionApplyError("retention_convergence_review_invalid")
    accepted_path = Path(os.path.abspath(reviewed_plan_path))
    if reviewed_plan_path != accepted_path:
        raise RetentionApplyError("retention_convergence_review_invalid")
    accepted_root = _read_reviewed_plan(
        reviewed_plan_path,
        expected_sha256=expected_reviewed_plan_sha256,
    )
    inputs = _plan_inputs(accepted_root)
    plan_state_dir = Path(inputs["activation_journal"]).parent
    if state_dir is not None:
        try:
            supplied_state = retention._strict_private_directory(  # noqa: SLF001
                state_dir,
                code="retention_convergence_state_invalid",
            )
        except retention.RetentionPlanError as exc:
            raise RetentionApplyError("retention_convergence_state_invalid") from exc
        if supplied_state != plan_state_dir:
            raise RetentionApplyError("retention_convergence_state_invalid")
    state_dir = plan_state_dir
    activation_receipt = _current_activation_receipt_path(state_dir)
    epoch, epoch_sha256 = _retention_epoch(
        state_dir=state_dir,
        activation_receipt=activation_receipt,
    )
    if epoch["topology"] == "first_v2":
        raise RetentionApplyError("retention_convergence_first_v2_deferred")
    if epoch["topology"] != "two_v2":
        raise RetentionApplyError("retention_convergence_v2_required")
    _preflight_reviewed_root(accepted_root)

    reviewed_set_sha256 = _reviewed_candidate_set_sha256(accepted_root)
    initial_context = _new_cycle_context(
        accepted_root_plan_path=accepted_path,
        accepted_root_plan_sha256=expected_reviewed_plan_sha256,
        reviewed_full_candidate_set_sha256=reviewed_set_sha256,
        retention_epoch_sha256=epoch_sha256,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    journal_path = state_dir / APPLY_JOURNAL_NAME
    existing = _load_journal(journal_path)
    latest_receipt: dict[str, Any] | None = None
    effectful_batches = 0
    active_context = initial_context
    if existing is not None:
        existing_context = _cycle_context_from_record(existing)
        if existing.get("phase") in {"prepared", "applying"}:
            if not _same_cycle(existing_context, initial_context):
                raise RetentionApplyError("retention_convergence_in_progress")
            before_phase = str(existing["phase"])
            latest_receipt = apply_retention_plan(
                plan_path=None,
                expected_plan_sha256=str(existing["plan_sha256"]),
                state_dir=state_dir,
                _cycle_context=existing_context,
            )
            if before_phase in {"prepared", "applying"} and latest_receipt["deleted_candidate_count"]:
                effectful_batches += 1
            active_context = existing_context
        elif existing.get("phase") == "applied":
            existing_receipt = _journal_apply_receipt(state_dir, existing)
            if _same_cycle(existing_context, initial_context):
                latest_receipt = apply_retention_plan(
                    plan_path=None,
                    expected_plan_sha256=str(existing["plan_sha256"]),
                    state_dir=state_dir,
                    _cycle_context=existing_context,
                )
                active_context = existing_context
            elif existing_receipt.get("admission_status") != "release_admissible":
                raise RetentionApplyError("retention_convergence_in_progress")
        else:
            raise RetentionApplyError("retention_apply_journal_invalid")

    if latest_receipt is None:
        latest_receipt = apply_retention_plan(
            plan_path=reviewed_plan_path,
            expected_plan_sha256=expected_reviewed_plan_sha256,
            _cycle_context=initial_context,
        )
        active_context = initial_context
        if latest_receipt["deleted_candidate_count"]:
            effectful_batches += 1
    if latest_receipt.get("admission_status") == "release_admissible":
        return _converged_receipt_for_state(
            state_dir=state_dir,
            activation_receipt=activation_receipt,
        )

    accepted_root = _load_accepted_root_plan(state_dir, active_context)
    accepted_identities = _reviewed_candidate_identities(accepted_root)
    while True:
        fresh = _build_reviewed_subset_plan(inputs=inputs, accepted_root=accepted_root)
        candidates = _candidate_records(fresh)
        exact_zero = _is_exact_terminal_zero_plan(fresh, candidates)
        if exact_zero:
            if _has_open_only_identity(accepted_root):
                return _convergence_receipt(
                    epoch=epoch,
                    status="in_progress",
                    terminal=_progress_values(active_context, latest_receipt=latest_receipt),
                )
            accepted_identity_sha256s = {
                hashlib.sha256(_canonical(item)).hexdigest() for item in accepted_identities
            }
            applied_identity_sha256s = _applied_cycle_identities_locked(
                state_dir,
                latest_receipt=latest_receipt,
                accepted_root=accepted_root,
                cycle_context=active_context,
            )
            if applied_identity_sha256s != accepted_identity_sha256s:
                return _convergence_receipt(
                    epoch=epoch,
                    status="in_progress",
                    terminal=_progress_values(active_context, latest_receipt=latest_receipt),
                )
        if not candidates and not exact_zero and latest_receipt.get("admission_reason") == "deferred_zero":
            return _convergence_receipt(
                epoch=epoch,
                status="in_progress",
                terminal=_progress_values(active_context, latest_receipt=latest_receipt),
            )
        if candidates and effectful_batches >= MAX_EFFECTFUL_BATCHES_PER_INVOCATION:
            return _convergence_receipt(
                epoch=epoch,
                status="in_progress",
                terminal=_progress_values(active_context, latest_receipt=latest_receipt),
            )
        if not candidates and not exact_zero and not accepted_identities:
            return _convergence_receipt(
                epoch=epoch,
                status="in_progress",
                terminal=_progress_values(active_context, latest_receipt=latest_receipt),
            )
        next_ordinal = int(latest_receipt["batch_ordinal"]) + 1
        next_context = _new_cycle_context(
            accepted_root_plan_path=accepted_path,
            accepted_root_plan_sha256=expected_reviewed_plan_sha256,
            reviewed_full_candidate_set_sha256=reviewed_set_sha256,
            retention_epoch_sha256=epoch_sha256,
            batch_ordinal=next_ordinal,
            previous_receipt_sha256=str(latest_receipt["receipt_sha256"]),
        )
        durable_path = _stage_generated_plan(state_dir, fresh)
        latest_receipt = apply_retention_plan(
            plan_path=durable_path,
            expected_plan_sha256=str(fresh["plan_sha256"]),
            _cycle_context=next_context,
        )
        active_context = next_context
        if candidates:
            effectful_batches += 1
        if latest_receipt.get("admission_status") == "release_admissible":
            return _converged_receipt_for_state(
                state_dir=state_dir,
                activation_receipt=activation_receipt,
            )
        if not candidates:
            return _convergence_receipt(
                epoch=epoch,
                status="in_progress",
                terminal=_progress_values(active_context, latest_receipt=latest_receipt),
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    apply = subcommands.add_parser("apply")
    source = apply.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=Path)
    source.add_argument("--state-dir", type=Path)
    apply.add_argument("--expected-plan-sha256", required=True)
    converge = subcommands.add_parser("converge")
    converge.add_argument("--reviewed-plan", required=True, type=Path)
    converge.add_argument("--expected-reviewed-plan-sha256", required=True)
    converge.add_argument("--state-dir", type=Path)
    admission = subcommands.add_parser("admission")
    admission.add_argument("--activation-receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "apply":
            receipt = apply_retention_plan(
                plan_path=args.plan,
                expected_plan_sha256=args.expected_plan_sha256,
                state_dir=args.state_dir,
            )
        elif args.command == "converge":
            receipt = converge_retention_cycle(
                reviewed_plan_path=args.reviewed_plan,
                expected_reviewed_plan_sha256=args.expected_reviewed_plan_sha256,
                state_dir=args.state_dir,
            )
        elif args.command == "admission":
            receipt = retention_release_admission(
                activation_receipt=args.activation_receipt,
            )
        else:
            raise RetentionApplyError("retention_apply_command_invalid")
        sys.stdout.buffer.write(_canonical(receipt) + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, RetentionApplyError) else None
        failure = {
            "failure_code": _body_free_code(code),
            "schema": (
                CONVERGENCE_RECEIPT_SCHEMA
                if "args" in locals() and args.command in {"admission", "converge"}
                else APPLY_RECEIPT_SCHEMA
            ),
            "status": "failed_closed",
        }
        sys.stderr.buffer.write(_canonical(failure) + b"\n")
        sys.stderr.buffer.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "APPLY_JOURNAL_SCHEMA",
    "APPLY_RECEIPT_SCHEMA",
    "CONVERGENCE_RECEIPT_SCHEMA",
    "MAX_EFFECTFUL_BATCHES_PER_INVOCATION",
    "RetentionApplyError",
    "_retention_release_admission_locked",
    "apply_retention_plan",
    "converge_retention_cycle",
    "retention_release_admission",
]
