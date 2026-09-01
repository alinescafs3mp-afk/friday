from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import immutable_release_operator
from tools import release_artifact_proc_probe as proc_probe
from tools import release_artifact_retention as retention
from tools import release_artifact_retention_operator as operator


class _TestTransaction:
    def __enter__(self) -> _TestTransaction:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def assert_held(self) -> None:
        return None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _portable_candidate(index: int) -> dict[str, Any]:
    return {
        "allocated_bytes": index * 4096,
        "collection": "targets",
        "entry_count": 1,
        "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "identity": {"generation": "g" * 64, "ordinal": index},
        "inode": index + 1,
        "mode": stat.S_IFDIR | 0o700,
        "nlink": 2,
        "path": f"/srv/friday/releases/candidate-{index:06d}",
        "portable_inventory_sha256": hashlib.sha256(f"inventory-{index}".encode("ascii")).hexdigest(),
        "recursive_bytes": index,
        "type": "directory",
        "writable_authority_sha256": "a" * 64,
    }


def _terminal_plan() -> dict[str, Any]:
    open_core = {
        "authority_sha256": "a" * 64,
        "complete": True,
        "observation_role": "diagnostic_prerequisite",
        "open_identities": [],
        "open_paths": [],
        "process_epoch_sha256": "",
        "schema": retention.OPEN_INVENTORY_SCHEMA,
        "source": "code_owned_no_delete_candidates_v1",
        "target_index_sha256": "",
        "universal_absence_proof": False,
    }
    open_inventory = {
        "authority_sha256": open_core["authority_sha256"],
        "complete": True,
        "observation_role": open_core["observation_role"],
        "open_identity_count": 0,
        "open_path_count": 0,
        "process_epoch_sha256": "",
        "schema": retention.OPEN_INVENTORY_SCHEMA,
        "snapshot_sha256": hashlib.sha256(_canonical(open_core)).hexdigest(),
        "source": open_core["source"],
        "target_index_sha256": "",
        "universal_absence_proof": False,
    }
    core: dict[str, Any] = {
        "apply_authority": False,
        "authority_bindings": {"bindings_sha256": "b" * 64},
        "backup_targets": [],
        "block_reason": "",
        "classification_status": "eligible",
        "effect_authority": {
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
        },
        "mode": "eligible_classification",
        "open_inventory": open_inventory,
        "retention_scope": {
            "file_sha256": "c" * 64,
            "schema": retention.RETENTION_SCOPE_SCHEMA,
        },
        "schema": retention.PLAN_SCHEMA,
        "targets": [],
    }
    return {
        **core,
        "plan_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def _epoch() -> dict[str, Any]:
    return {
        "activation_receipt_file_sha256": "1" * 64,
        "activation_receipt_sha256": "2" * 64,
        "current_candidate_sha256": "3" * 64,
        "current_generation_id": "4" * 64,
        "current_generation_receipt_sha256": "5" * 64,
        "index_journal_sha256": "6" * 64,
        "index_revision": 9,
        "older_candidate_sha256": "7" * 64,
        "older_generation_id": "8" * 64,
        "older_generation_receipt_sha256": "9" * 64,
        "retention_scope_schema": retention.RETENTION_SCOPE_SCHEMA,
        "retention_scope_sha256": "a" * 64,
        "topology": "two_v2",
    }


def _recovery_binding(
    *,
    accepted_root_plan_sha256: str,
    reviewed_candidate_set_sha256: str,
    boot: str,
) -> dict[str, Any]:
    core = {
        "executing_initrd_sha256": "3" * 64,
        "maintenance_authority_sha256": "4" * 64,
        "maintenance_boot_id_sha256": boot * 64,
        "maintenance_namespace_epoch_sha256": "5" * 64,
        "maintenance_premount_receipt_sha256": "6" * 64,
        "maintenance_process_epoch_sha256": "7" * 64,
        "maintenance_target_receipt_sha256": "8" * 64,
        "plan_sha256": accepted_root_plan_sha256,
        "request_file_sha256": "9" * 64,
        "request_sha256": "a" * 64,
        "reviewed_candidate_set_sha256": reviewed_candidate_set_sha256,
        "schema": operator.MAINTENANCE_RECOVERY_SCHEMA,
        "transaction_id": "b" * 64,
    }
    return {
        **core,
        "recovery_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def _recovery_epoch(
    *,
    binding: dict[str, Any],
    reviewed_authority: dict[str, Any],
) -> dict[str, Any]:
    core = {
        "binding": binding,
        "candidate_epoch": [],
        "operator_target_receipt_sha256": "c" * 64,
        "previous_epoch_sha256": "",
        "reviewed_authority": reviewed_authority,
        "schema": operator.MAINTENANCE_RECOVERY_EPOCH_SCHEMA,
        "stable_candidates": [],
    }
    return {
        **core,
        "epoch_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def _authority_bindings(*, device: int) -> dict[str, Any]:
    core = {
        "activation_journal_sha256": "1" * 64,
        "canonical_evidence_roots": [
            {
                "authority_path": "/srv/friday/evidence/authority.json",
                "authority_sha256": "2" * 64,
                "device": device,
                "inode": 41,
                "observed_authority_sha256": "2" * 64,
                "path": "/srv/friday/evidence",
            }
        ],
        "dr_index": {
            "observed_sha256": "3" * 64,
            "path": "/srv/friday/state/dr-index.json",
            "sha256": "3" * 64,
        },
        "dr_pins": [],
        "schema": retention.AUTHORITY_BINDINGS_SCHEMA,
        "status": "authenticated",
        "unit_install_journal_sha256": "4" * 64,
    }
    return {
        **core,
        "bindings_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def _plan_with_cycle_authority(
    *,
    device: int,
    mount_id: int,
    maintenance: bool,
) -> dict[str, Any]:
    plan = _terminal_plan()
    plan.update(
        {
            "activation_backup": {},
            "activation_journal": {
                "path": "/srv/friday/state/activation.json",
                "phase": "committed",
                "sha256": "1" * 64,
            },
            "authority_bindings": _authority_bindings(device=device + 1000),
            "backup_inventory_roots": [],
            "backup_root": "/srv/friday/backups",
            "inventory_roots": [
                {
                    "device": device,
                    "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
                    "inode": 31,
                    "mount_id": mount_id,
                    "nlink": 2,
                    "path": "/srv/friday/releases",
                    "type": "directory",
                    "uid": os.geteuid(),
                    "writable_authority_sha256": "5" * 64,
                }
            ],
            "protected_releases": [],
            "retention_scope": {
                "device": device + 2000,
                "file_sha256": "c" * 64,
                "inode": 51,
                "path": "/srv/friday/state/release-artifact-retention-scope.v1.json",
                "schema": retention.RETENTION_SCOPE_SCHEMA,
            },
            "reviewed_scratch_targets": [],
            "schema": (retention.MAINTENANCE_PLAN_SCHEMA if maintenance else retention.PLAN_SCHEMA),
            "scope": "release_and_backup_inventory",
            "unit_install_journal": {
                "path": "/srv/friday/state/unit.json",
                "phase": "committed",
                "sha256": "4" * 64,
            },
        }
    )
    core = {name: item for name, item in plan.items() if name != "plan_sha256"}
    return {
        **core,
        "plan_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def test_ordinary_public_contract_and_maintenance_schemas_are_disjoint(
    tmp_path: Path,
) -> None:
    assert tuple(field.name for field in fields(retention.OpenInventorySnapshot)) == (
        "source",
        "complete",
        "open_paths",
        "open_identities",
        "authority_sha256",
        "target_index_sha256",
        "process_epoch_sha256",
    )
    assert retention.__all__ == [
        "AUTHORITY_BINDINGS_SCHEMA",
        "CanonicalEvidenceRoot",
        "DRGenerationPin",
        "INCOMPLETE_OPEN_INVENTORY",
        "OPEN_INVENTORY_SCHEMA",
        "OpenInventorySnapshot",
        "PLAN_SCHEMA",
        "RETENTION_SCOPE_NAME",
        "RETENTION_SCOPE_SCHEMA",
        "RetentionPlanError",
        "RetentionScopeAuthority",
        "RetentionAuthorityBindings",
        "ReviewedScratchTarget",
        "build_complete_open_inventory",
        "build_eligible_retention_plan",
        "build_retention_authority_bindings",
        "load_retention_scope_authority",
        "plan_release_artifact_retention",
        "provision_retention_scope_authority",
    ]
    assert proc_probe.__all__ == [
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

    plan_dir = tmp_path / "state" / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(parents=True, mode=0o700)

    def publish_plan(value: dict[str, Any]) -> Path:
        path = plan_dir / f"plan-{value['plan_sha256']}.json"
        path.write_bytes(_canonical(value) + b"\n")
        path.chmod(0o400)
        return path

    ordinary = _terminal_plan()
    operator._read_reviewed_plan(  # noqa: SLF001
        publish_plan(ordinary),
        expected_sha256=str(ordinary["plan_sha256"]),
    )
    for changes in (
        {"schema": retention.MAINTENANCE_PLAN_SCHEMA},
        {
            "open_inventory": {
                **ordinary["open_inventory"],
                "schema": retention.MAINTENANCE_OPEN_INVENTORY_SCHEMA,
            }
        },
    ):
        core = {
            **{name: item for name, item in ordinary.items() if name != "plan_sha256"},
            **changes,
        }
        relabeled = {
            **core,
            "plan_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
        }
        with pytest.raises(
            operator.RetentionApplyError,
            match="^retention_apply_plan_invalid$",
        ):
            operator._read_reviewed_plan(  # noqa: SLF001
                publish_plan(relabeled),
                expected_sha256=str(relabeled["plan_sha256"]),
            )


def test_apply_and_convergence_schema_relabeling_is_closed(
    tmp_path: Path,
) -> None:
    plan = _terminal_plan()
    durable = tmp_path / f"plan-{plan['plan_sha256']}.json"
    durable.write_bytes(_canonical(plan) + b"\n")
    durable.chmod(0o400)
    status = durable.stat()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=durable,
        accepted_root_plan_sha256=str(plan["plan_sha256"]),
        reviewed_full_candidate_set_sha256=hashlib.sha256(_canonical([])).hexdigest(),
        retention_epoch_sha256="e" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    journal = operator._new_journal(  # noqa: SLF001
        plan,
        (),
        durable_plan=(durable, status.st_dev, status.st_ino),
        filesystem_before=(),
        cycle_context=cycle,
    )
    ordinary = operator._result_receipt(  # noqa: SLF001
        plan=plan,
        journal=journal,
        candidates=(),
        authority_bindings_sha256="b" * 64,
    )
    assert operator._validate_apply_receipt(ordinary) == ordinary  # noqa: SLF001

    maintenance_core = {
        **{name: item for name, item in ordinary.items() if name != "receipt_sha256"},
        "maintenance_candidate_identity_sha256s": [],
        "maintenance_executing_initrd_sha256": "1" * 64,
        "maintenance_premount_receipt_sha256": "2" * 64,
        "maintenance_recovery_sha256": "3" * 64,
        "privileged_probe_role": "global_premount_effect_authority",
        "schema": operator.MAINTENANCE_APPLY_RECEIPT_SCHEMA,
        "universal_absence_proof": True,
    }
    maintenance_receipt = {
        **maintenance_core,
        "receipt_sha256": hashlib.sha256(_canonical(maintenance_core)).hexdigest(),
    }
    assert (
        operator._validate_apply_receipt(maintenance_receipt)  # noqa: SLF001
        == maintenance_receipt
    )
    for source, schema in (
        (ordinary, operator.MAINTENANCE_APPLY_RECEIPT_SCHEMA),
        (maintenance_receipt, operator.APPLY_RECEIPT_SCHEMA),
    ):
        relabeled_core = {
            **{name: item for name, item in source.items() if name != "receipt_sha256"},
            "schema": schema,
        }
        relabeled = {
            **relabeled_core,
            "receipt_sha256": hashlib.sha256(_canonical(relabeled_core)).hexdigest(),
        }
        with pytest.raises(
            operator.RetentionApplyError,
            match="^retention_apply_receipt_invalid$",
        ):
            operator._validate_apply_receipt(relabeled)  # noqa: SLF001

    terminal = {
        "accepted_root_plan_sha256": "4" * 64,
        "batch_ordinal": 0,
        "cycle_sha256": "5" * 64,
        "reviewed_full_candidate_set_sha256": "6" * 64,
        "terminal_apply_receipt_sha256": "7" * 64,
    }
    convergence = operator._convergence_receipt(  # noqa: SLF001
        epoch=_epoch(),
        status="converged",
        terminal=terminal,
    )
    assert (
        immutable_release_operator._validated_retention_release_admission(  # noqa: SLF001
            convergence,
            allow_first_v2_deferred=False,
        )
        == convergence
    )
    maintenance_progress = operator._convergence_receipt(  # noqa: SLF001
        epoch=_epoch(),
        status="in_progress",
        terminal={**terminal, "batch_ordinal": -1, "terminal_apply_receipt_sha256": ""},
        maintenance_recovery_sha256="8" * 64,
    )
    assert maintenance_progress["schema"] == operator.MAINTENANCE_CONVERGENCE_RECEIPT_SCHEMA
    with pytest.raises(
        immutable_release_operator.ReleaseFailure,
        match="^retention_release_admission_invalid$",
    ):
        immutable_release_operator._validated_retention_release_admission(  # noqa: SLF001
            maintenance_progress,
            allow_first_v2_deferred=False,
        )


def test_terminal_recovery_sidecar_is_immutable_and_reauthenticates_result_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(parents=True, mode=0o700)
    accepted_root_sha256 = "d" * 64
    terminal = {
        "accepted_root_plan_sha256": accepted_root_sha256,
        "batch_ordinal": 2,
        "cycle_sha256": "e" * 64,
        "reviewed_full_candidate_set_sha256": "f" * 64,
        "terminal_apply_receipt_sha256": "1" * 64,
    }
    recovery = _recovery_binding(
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256="2" * 64,
        boot="3",
    )
    _payload, raw = operator._maintenance_convergence_record(  # noqa: SLF001
        terminal=terminal,
        recovery=recovery,
    )
    sidecar = plan_dir / f"maintenance-convergence-{terminal['cycle_sha256']}.json"
    staged = sidecar.with_name(f".{sidecar.name}.new")
    staged.write_bytes(raw[: len(raw) // 2])
    staged.chmod(0o600)
    authority = operator._persist_maintenance_convergence_authority(  # noqa: SLF001
        state_dir,
        terminal=terminal,
        recovery=recovery,
        guard=lambda: None,
    )
    assert not staged.exists()
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o400
    assert sidecar.stat().st_nlink == 1
    assert (
        operator._load_maintenance_convergence_authority(  # noqa: SLF001
            state_dir,
            terminal=terminal,
        )
        == authority
    )

    accepted_root = {"maintenance": True}
    durable = plan_dir / "terminal.json"
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=durable,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_full_candidate_set_sha256=terminal["reviewed_full_candidate_set_sha256"],
        retention_epoch_sha256="4" * 64,
        batch_ordinal=terminal["batch_ordinal"],
        previous_receipt_sha256="5" * 64,
    )
    monkeypatch.setattr(
        operator.release_operator,
        "OperatorTransactionLock",
        lambda _path: _TestTransaction(),
    )
    monkeypatch.setattr(
        operator,
        "_retention_epoch_locked",
        lambda **_kwargs: (_epoch(), "6" * 64),
    )
    monkeypatch.setattr(
        operator,
        "_validated_terminal_chain",
        lambda *_args, **_kwargs: terminal,
    )
    monkeypatch.setattr(operator, "_load_journal", lambda _path: cycle)
    monkeypatch.setattr(
        operator,
        "_load_accepted_root_plan",
        lambda *_args, **_kwargs: accepted_root,
    )
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: {"authority": True} if value is accepted_root else None,
    )
    os.link(sidecar, staged)
    assert sidecar.stat().st_nlink == 2
    convergence = operator._converged_receipt_for_state(  # noqa: SLF001
        state_dir=state_dir,
        activation_receipt=tmp_path / "activation.json",
        maintenance_recovery_sha256=str(recovery["recovery_sha256"]),
    )
    assert convergence["schema"] == operator.CONVERGENCE_RECEIPT_SCHEMA
    assert "maintenance_recovery_sha256" not in convergence
    assert not staged.exists()
    assert sidecar.stat().st_nlink == 1

    os.link(sidecar, staged)
    assert (
        operator._maintenance_convergence_authority_for_state(  # noqa: SLF001
            state_dir=state_dir,
            activation_receipt=tmp_path / "activation.json",
        )
        == authority
    )
    assert not staged.exists()
    assert sidecar.stat().st_nlink == 1
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._converged_receipt_for_state(  # noqa: SLF001
            state_dir=state_dir,
            activation_receipt=tmp_path / "activation.json",
            maintenance_recovery_sha256="7" * 64,
        )


def test_large_reviewed_set_is_sidecar_bound_and_only_device_can_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(mode=0o700)
    reviewed = [_portable_candidate(index) for index in range(4096)]
    accepted_root_sha256 = "d" * 64
    _payload, raw = operator._maintenance_review_record(  # noqa: SLF001
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidates=reviewed,
    )
    assert retention.MAX_JOURNAL_BYTES < len(raw) <= operator.MAX_MAINTENANCE_REVIEW_AUTHORITY_BYTES

    original_bound = operator.MAX_MAINTENANCE_REVIEW_AUTHORITY_BYTES
    monkeypatch.setattr(
        operator,
        "MAX_MAINTENANCE_REVIEW_AUTHORITY_BYTES",
        len(raw) - 1,
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._persist_maintenance_review_authority(  # noqa: SLF001
            state_dir,
            accepted_root_plan_sha256=accepted_root_sha256,
            reviewed_candidates=reviewed,
            guard=lambda: None,
        )
    sidecar = plan_dir / f"maintenance-reviewed-{accepted_root_sha256}.json"
    assert not sidecar.exists()

    monkeypatch.setattr(
        operator,
        "MAX_MAINTENANCE_REVIEW_AUTHORITY_BYTES",
        original_bound,
    )
    staged_sidecar = plan_dir / f".{sidecar.name}.new"
    staged_sidecar.write_bytes(raw[: len(raw) // 2])
    staged_sidecar.chmod(0o600)
    binding = operator._persist_maintenance_review_authority(  # noqa: SLF001
        state_dir,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidates=reviewed,
        guard=lambda: None,
    )
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o400
    assert not staged_sidecar.exists()
    assert len(_canonical(binding)) < retention.MAX_JOURNAL_BYTES
    reviewed_sha256 = hashlib.sha256(_canonical(reviewed)).hexdigest()
    loaded, exact = operator._load_maintenance_review_authority(  # noqa: SLF001
        binding,
        state_dir=state_dir,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256=reviewed_sha256,
        allow_device_rebind=False,
    )
    assert loaded == reviewed
    assert exact == binding

    old_boot = {**binding, "device": binding["device"] + 1}
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._load_maintenance_review_authority(  # noqa: SLF001
            old_boot,
            state_dir=state_dir,
            accepted_root_plan_sha256=accepted_root_sha256,
            reviewed_candidate_set_sha256=reviewed_sha256,
            allow_device_rebind=False,
        )
    rebound, rebound_binding = operator._load_maintenance_review_authority(  # noqa: SLF001
        old_boot,
        state_dir=state_dir,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256=reviewed_sha256,
        allow_device_rebind=True,
    )
    assert rebound == reviewed
    assert rebound_binding == binding
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=plan_dir / f"plan-{accepted_root_sha256}.json",
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_full_candidate_set_sha256=operator._portable_identity_set_sha256(  # noqa: SLF001
            reviewed
        ),
        retention_epoch_sha256="e" * 64,
        batch_ordinal=1,
        previous_receipt_sha256="f" * 64,
    )
    assert old_boot["device"] != sidecar.stat().st_dev
    assert operator._load_maintenance_cycle_reviewed_candidates(  # noqa: SLF001
        state_dir,
        cycle,
    ) == tuple(reviewed)

    replayed_inode = {**old_boot, "inode": binding["inode"] + 1}
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._load_maintenance_review_authority(  # noqa: SLF001
            replayed_inode,
            state_dir=state_dir,
            accepted_root_plan_sha256=accepted_root_sha256,
            reviewed_candidate_set_sha256=reviewed_sha256,
            allow_device_rebind=True,
        )

    oversized_journal = state_dir / "oversized-maintenance-journal.json"
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._write_journal(  # noqa: SLF001
            oversized_journal,
            {
                "padding": "x" * retention.MAX_JOURNAL_BYTES,
                "schema": operator.MAINTENANCE_APPLY_JOURNAL_SCHEMA,
            },
            guard=lambda: None,
        )
    assert not oversized_journal.exists()

    redirected = {
        **binding,
        "path": str(tmp_path / "other-state" / operator.APPLY_PLAN_DIRECTORY / sidecar.name),
    }
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._load_maintenance_review_authority(  # noqa: SLF001
            redirected,
            state_dir=state_dir,
            accepted_root_plan_sha256=accepted_root_sha256,
            reviewed_candidate_set_sha256=reviewed_sha256,
            allow_device_rebind=True,
        )


def test_pre_first_journal_rebind_reconstructs_only_boot_local_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mode = stat.S_IFDIR | 0o700
    old_record = [".", 101, 7, mode, 2, 1000, 0, 0, 0, 201, 1000, 0]
    current_record = [".", 102, 7, mode, 2, 1000, 0, 0, 0, 202, 1000, 0]
    exact = {
        "allocated_bytes": 0,
        "collection": "targets",
        "device": 101,
        "entry_count": 1,
        "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "identity": {"generation": "g" * 64},
        "inode": 7,
        "inventory_sha256": hashlib.sha256(_canonical([old_record])).hexdigest(),
        "mode": mode,
        "mount_id": 201,
        "nlink": 2,
        "path": "/srv/friday/releases/candidate",
        "recursive_bytes": 0,
        "type": "directory",
        "writable_authority_sha256": "a" * 64,
    }
    snapshot = SimpleNamespace(records=(tuple(current_record),))
    monkeypatch.setattr(
        operator,
        "_reviewed_candidate_identities",
        lambda _plan: (exact,),
    )
    monkeypatch.setattr(retention, "_snapshot", lambda _path: snapshot)

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_review_changed$",
    ):
        operator.portable_reviewed_candidate_identities({})
    rebound = operator.portable_reviewed_candidate_identities(
        {},
        allow_boot_rebind=True,
    )
    reviewed_sha256 = hashlib.sha256(_canonical(list(rebound))).hexdigest()
    monkeypatch.setattr(
        operator,
        "_require_live_maintenance_recovery",
        lambda **_kwargs: ({"reviewed_candidate_set_sha256": reviewed_sha256}, {}),
    )
    assert (
        operator._fresh_maintenance_portable_identities(  # noqa: SLF001
            plan={},
            durable_plan_path=tmp_path / "accepted-plan.json",
            recovery={},
            accepted_root_plan_sha256="d" * 64,
        )
        == rebound
    )
    candidate = {
        **{name: value for name, value in exact.items() if name != "collection"},
        "candidate_sha256": "e" * 64,
        "decision": "delete_candidate",
        "root_device": 101,
        "root_filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "root_inode": 41,
        "root_mount_id": 201,
        "root_writable_authority_sha256": "f" * 64,
    }
    stable = operator._maintenance_initial_stable_candidates(  # noqa: SLF001
        {"backup_targets": [], "targets": [candidate]},
        (candidate,),
        rebound,
    )
    assert stable == [
        {
            "candidate_sha256": "e" * 64,
            "inode": 7,
            "portable_inventory_sha256": rebound[0]["portable_inventory_sha256"],
            "root_inode": 41,
        }
    ]

    changed = list(current_record)
    changed[6] = 1
    monkeypatch.setattr(
        retention,
        "_snapshot",
        lambda _path: SimpleNamespace(records=(tuple(changed),)),
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_review_changed$",
    ):
        operator.portable_reviewed_candidate_identities(
            {},
            allow_boot_rebind=True,
        )


def test_ordinary_candidate_match_keeps_single_snapshot_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate"
    mode = stat.S_IFDIR | 0o700
    record = (".", 11, 12, mode, 2, 1000, 0, 0, 0, 13, 1000, 0)
    snapshot = SimpleNamespace(records=(record,))
    inventory_sha256 = hashlib.sha256(_canonical([list(record)])).hexdigest()
    candidate = {
        "allocated_bytes": 0,
        "device": 11,
        "entry_count": 1,
        "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "inode": 12,
        "inventory_sha256": inventory_sha256,
        "mode": mode,
        "mount_id": 13,
        "recursive_bytes": 0,
        "writable_authority_sha256": "a" * 64,
    }
    observed = SimpleNamespace(
        allocated_bytes=0,
        device=11,
        entry_count=1,
        filesystem_magic=retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        has_group_world_writable=False,
        has_hardlink=False,
        has_special=False,
        has_symlink=False,
        inode=12,
        inventory_sha256=inventory_sha256,
        kind="directory",
        mode=mode,
        mount_id=13,
        owner_ok=True,
        raced=False,
        total_allocated_bytes=0,
        total_bytes=0,
        writable_authority_sha256="a" * 64,
    )
    traversals: list[Path] = []
    monkeypatch.setattr(retention, "_observe_target", lambda _path: observed)

    def take_snapshot(value: Path) -> Any:
        traversals.append(value)
        return snapshot

    monkeypatch.setattr(retention, "_snapshot", take_snapshot)

    operator._candidate_matches_observation(candidate, path)  # noqa: SLF001

    assert traversals == [path]


def test_terminal_maintenance_surface_recovers_after_ordinary_receipt_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(mode=0o700)
    plan = _terminal_plan()
    durable_path = plan_dir / f"plan-{plan['plan_sha256']}.json"
    durable_path.write_bytes(_canonical(plan) + b"\n")
    durable_path.chmod(0o400)
    status = durable_path.stat()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=durable_path,
        accepted_root_plan_sha256="e" * 64,
        reviewed_full_candidate_set_sha256="f" * 64,
        retention_epoch_sha256="1" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    maintenance = operator._new_journal(  # noqa: SLF001
        plan,
        (),
        durable_plan=(durable_path, status.st_dev, status.st_ino),
        filesystem_before=(),
        cycle_context=cycle,
        maintenance_recovery={},
    )
    maintenance["phase"] = "applying"
    operator._write_journal(  # noqa: SLF001
        state_dir / operator.APPLY_JOURNAL_NAME,
        maintenance,
        guard=lambda: None,
    )

    def crash(point: str) -> None:
        if point == "after_terminal_ordinary_receipt_publish":
            raise operator._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(operator, "_fault", crash)
    with pytest.raises(operator._InjectedCrash):  # noqa: SLF001
        operator._canonicalize_maintenance_terminal_surface(  # noqa: SLF001
            state_dir,
            plan=plan,
            candidates=(),
            maintenance_journal=maintenance,
            authority_bindings_sha256="b" * 64,
            guard=lambda: None,
        )
    interrupted = operator._load_journal(  # noqa: SLF001
        state_dir / operator.APPLY_JOURNAL_NAME
    )
    assert interrupted is not None
    assert interrupted["schema"] == operator.MAINTENANCE_APPLY_JOURNAL_SCHEMA
    assert interrupted["phase"] == "applying"
    assert interrupted["receipt_sha256"] == ""

    monkeypatch.setattr(operator, "_fault", lambda _point: None)
    receipt = operator._canonicalize_maintenance_terminal_surface(  # noqa: SLF001
        state_dir,
        plan=plan,
        candidates=(),
        maintenance_journal=interrupted,
        authority_bindings_sha256="b" * 64,
        guard=lambda: None,
    )
    final = operator._load_journal(  # noqa: SLF001
        state_dir / operator.APPLY_JOURNAL_NAME
    )
    assert final is not None
    assert final["schema"] == operator.APPLY_JOURNAL_SCHEMA
    assert set(final) == {
        "accepted_root_plan_path",
        "accepted_root_plan_sha256",
        "batch_ordinal",
        "cycle_sha256",
        "durable_plan",
        "entries",
        "filesystem_after",
        "filesystem_before",
        "journal_sha256",
        "phase",
        "plan_sha256",
        "previous_receipt_sha256",
        "receipt_sha256",
        "retention_epoch_sha256",
        "retention_scope_schema",
        "retention_scope_sha256",
        "reviewed_full_candidate_set_sha256",
        "schema",
        "transaction_id",
    }
    assert final["durable_plan"]["device"] == durable_path.stat().st_dev
    terminal_plan = json.loads(durable_path.read_bytes())
    assert terminal_plan["schema"] == retention.PLAN_SCHEMA
    assert terminal_plan["open_inventory"]["schema"] == retention.OPEN_INVENTORY_SCHEMA
    assert receipt["privileged_probe_role"] == "diagnostic_prerequisite"
    assert receipt["universal_absence_proof"] is False
    immutable_release_operator._require_retention_apply_quiesced(state_dir)  # noqa: SLF001


def test_apply_terminal_path_publishes_only_the_ordinary_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    source = tmp_path / "reviewed.json"
    plan = _terminal_plan()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=source,
        accepted_root_plan_sha256=str(plan["plan_sha256"]),
        reviewed_full_candidate_set_sha256="d" * 64,
        retention_epoch_sha256="e" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    recovery = _recovery_binding(
        accepted_root_plan_sha256=str(plan["plan_sha256"]),
        reviewed_candidate_set_sha256=hashlib.sha256(_canonical([])).hexdigest(),
        boot="1",
    )
    fresh_recovery = _recovery_binding(
        accepted_root_plan_sha256=str(plan["plan_sha256"]),
        reviewed_candidate_set_sha256=hashlib.sha256(_canonical([])).hexdigest(),
        boot="2",
    )
    monkeypatch.setattr(
        operator.release_operator,
        "OperatorTransactionLock",
        lambda _path: _TestTransaction(),
    )
    real_load_journal = operator._load_journal  # noqa: SLF001
    monkeypatch.setattr(operator, "_read_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        operator,
        "_plan_inputs",
        lambda _plan, **_kwargs: {"activation_journal": state_dir / "activation.json"},
    )
    monkeypatch.setattr(operator, "_candidate_records", lambda _plan: ())
    monkeypatch.setattr(
        operator,
        "_live_authority_reauthenticate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(operator, "_fresh_plan_for_cycle", lambda **_kwargs: plan)
    monkeypatch.setattr(
        operator,
        "_cycle_authority_equivalent",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(operator, "_validate_mutation_namespaces", lambda **_kwargs: None)
    monkeypatch.setattr(operator, "_terminal_absence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        operator,
        "_post_apply_reauthenticate",
        lambda *_args, **_kwargs: {"authority_bindings": {"bindings_sha256": "b" * 64}},
    )
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda _plan: {
            "executing_initrd_sha256": recovery["executing_initrd_sha256"],
            "request_file_sha256": recovery["request_file_sha256"],
            "transaction_id": recovery["transaction_id"],
        },
    )
    monkeypatch.setattr(
        operator,
        "_require_live_maintenance_recovery",
        lambda **kwargs: (dict(kwargs["recovery"]), {}),
    )
    recovery_epochs = iter(
        [
            ({"boot": "1"}, ()),
            ({"boot": "1"}, ()),
            ({"boot": "2"}, ()),
        ]
    )
    monkeypatch.setattr(
        operator,
        "_new_maintenance_recovery_epoch",
        lambda **_kwargs: next(recovery_epochs),
    )
    monkeypatch.setattr(
        operator,
        "_validate_maintenance_recovery_epoch",
        lambda value, _candidates: dict(value),
    )
    monkeypatch.setattr(
        operator,
        "_validate_journal_contract",
        lambda value, **_kwargs: operator._journal_core(value),  # noqa: SLF001
    )

    durable_plan: list[tuple[Path, int, int]] = []

    def persist(
        _state_dir: Path,
        _plan: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[Path, int, int]:
        directory = _state_dir / operator.APPLY_PLAN_DIRECTORY
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"plan-{_plan['plan_sha256']}.json"
        path.write_bytes(_canonical(_plan) + b"\n")
        path.chmod(0o400)
        status = path.stat()
        result = (path, status.st_dev, status.st_ino)
        durable_plan[:] = [result]
        return result

    monkeypatch.setattr(operator, "_persist_reviewed_plan", persist)
    monkeypatch.setattr(
        operator,
        "_resume_plan_after_live_authority",
        lambda *_args, **_kwargs: (
            plan,
            durable_plan[0],
            real_load_journal(state_dir / operator.APPLY_JOURNAL_NAME),
            {"activation_journal": state_dir / "activation.json"},
        ),
    )
    published: list[dict[str, Any]] = []
    real_publish = operator._publish_receipt  # noqa: SLF001

    def publish(
        directory: Path,
        receipt: dict[str, Any],
        *,
        guard: Any,
    ) -> dict[str, Any]:
        published.append(dict(receipt))
        return real_publish(directory, receipt, guard=guard)

    monkeypatch.setattr(operator, "_publish_receipt", publish)
    crash_armed = True

    def crash(point: str) -> None:
        nonlocal crash_armed
        if crash_armed and point == "after_terminal_ordinary_receipt_publish":
            crash_armed = False
            raise operator._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(operator, "_fault", crash)
    with pytest.raises(operator._InjectedCrash):  # noqa: SLF001
        operator.apply_retention_plan(
            plan_path=source,
            expected_plan_sha256=str(plan["plan_sha256"]),
            _cycle_context=cycle,
            _maintenance_recovery=recovery,
        )
    interrupted = real_load_journal(state_dir / operator.APPLY_JOURNAL_NAME)
    assert interrupted is not None
    assert interrupted["schema"] == operator.MAINTENANCE_APPLY_JOURNAL_SCHEMA
    assert interrupted["phase"] == "applying"

    result = operator.apply_retention_plan(
        plan_path=None,
        expected_plan_sha256=str(plan["plan_sha256"]),
        state_dir=state_dir,
        _cycle_context=cycle,
        _maintenance_recovery=fresh_recovery,
    )

    assert result == published[0]
    assert len(published) == 2
    assert published[0] == published[1]
    assert {receipt["privileged_probe_role"] for receipt in published} == {"diagnostic_prerequisite"}
    assert len(list((state_dir / operator.APPLY_RECEIPT_DIRECTORY).iterdir())) == 1
    final = real_load_journal(state_dir / operator.APPLY_JOURNAL_NAME)
    assert final is not None
    assert final["schema"] == operator.APPLY_JOURNAL_SCHEMA
    final_plan_path = Path(final["durable_plan"]["path"])
    final_plan = json.loads(final_plan_path.read_bytes())
    assert final_plan["schema"] == retention.PLAN_SCHEMA
    assert final_plan["open_inventory"]["schema"] == retention.OPEN_INVENTORY_SCHEMA
    immutable_release_operator._require_retention_apply_quiesced(state_dir)  # noqa: SLF001


def test_published_maintenance_receipt_is_bound_to_stored_epoch_on_fresh_boot(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / operator.APPLY_PLAN_DIRECTORY).mkdir(mode=0o700)
    accepted_root_sha256 = "d" * 64
    reviewed = [_portable_candidate(0)]
    reviewed_set_sha256 = hashlib.sha256(_canonical(reviewed)).hexdigest()
    reviewed_authority = operator._persist_maintenance_review_authority(  # noqa: SLF001
        state_dir,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidates=reviewed,
        guard=lambda: None,
    )
    plan = _terminal_plan()
    durable_path = state_dir / operator.APPLY_PLAN_DIRECTORY / f"plan-{plan['plan_sha256']}.json"
    durable_path.write_bytes(_canonical(plan) + b"\n")
    durable_path.chmod(0o400)
    durable = durable_path.stat()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=durable_path,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_full_candidate_set_sha256=operator._portable_identity_set_sha256(  # noqa: SLF001
            reviewed
        ),
        retention_epoch_sha256="e" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    stored_binding = _recovery_binding(
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256=reviewed_set_sha256,
        boot="1",
    )
    stored_epoch = _recovery_epoch(
        binding=stored_binding,
        reviewed_authority=reviewed_authority,
    )
    stored_journal = operator._new_journal(  # noqa: SLF001
        plan,
        (),
        durable_plan=(durable_path, durable.st_dev, durable.st_ino),
        filesystem_before=(),
        cycle_context=cycle,
        maintenance_recovery=stored_epoch,
    )
    stored_receipt = operator._result_receipt(  # noqa: SLF001
        plan=plan,
        journal=stored_journal,
        candidates=(),
        authority_bindings_sha256="b" * 64,
    )
    operator._publish_receipt(  # noqa: SLF001
        state_dir,
        stored_receipt,
        guard=lambda: None,
    )

    fresh_epoch = _recovery_epoch(
        binding=_recovery_binding(
            accepted_root_plan_sha256=accepted_root_sha256,
            reviewed_candidate_set_sha256=reviewed_set_sha256,
            boot="2",
        ),
        reviewed_authority=reviewed_authority,
    )
    fresh_journal = {**stored_journal, "maintenance_recovery": fresh_epoch}
    fresh_receipt = operator._result_receipt(  # noqa: SLF001
        plan=plan,
        journal=fresh_journal,
        candidates=(),
        authority_bindings_sha256="b" * 64,
    )
    assert fresh_receipt["transaction_id"] == stored_receipt["transaction_id"]
    assert fresh_receipt["receipt_sha256"] != stored_receipt["receipt_sha256"]
    assert operator._published_maintenance_receipt_matches_stored_epoch(  # noqa: SLF001
        state_dir,
        plan=plan,
        journal=stored_journal,
        candidates=(),
        authority_bindings_sha256="b" * 64,
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_apply_receipt_changed$",
    ):
        operator._publish_receipt(  # noqa: SLF001
            state_dir,
            fresh_receipt,
            guard=lambda: None,
        )


def test_apply_recovers_published_receipt_with_stored_epoch_on_fresh_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(mode=0o700)
    accepted_root_sha256 = "d" * 64
    reviewed = [_portable_candidate(0)]
    reviewed_set_sha256 = hashlib.sha256(_canonical(reviewed)).hexdigest()
    reviewed_authority = operator._persist_maintenance_review_authority(  # noqa: SLF001
        state_dir,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidates=reviewed,
        guard=lambda: None,
    )
    plan = _terminal_plan()
    durable_path = plan_dir / f"plan-{plan['plan_sha256']}.json"
    durable_path.write_bytes(_canonical(plan) + b"\n")
    durable_path.chmod(0o400)
    durable = durable_path.stat()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=durable_path,
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_full_candidate_set_sha256=operator._portable_identity_set_sha256(  # noqa: SLF001
            reviewed
        ),
        retention_epoch_sha256="e" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    stored_binding = _recovery_binding(
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256=reviewed_set_sha256,
        boot="1",
    )
    stored_epoch = _recovery_epoch(
        binding=stored_binding,
        reviewed_authority=reviewed_authority,
    )
    fresh_binding = _recovery_binding(
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256=reviewed_set_sha256,
        boot="2",
    )
    fresh_epoch = _recovery_epoch(
        binding=fresh_binding,
        reviewed_authority=reviewed_authority,
    )
    monkeypatch.setattr(operator, "_is_exact_terminal_zero_plan", lambda *_args: False)
    stored_core = operator._new_journal(  # noqa: SLF001
        plan,
        (),
        durable_plan=(durable_path, durable.st_dev, durable.st_ino),
        filesystem_before=(),
        cycle_context=cycle,
        maintenance_recovery=stored_epoch,
    )
    stored_core["phase"] = "applying"
    stored_journal = operator._write_journal(  # noqa: SLF001
        state_dir / operator.APPLY_JOURNAL_NAME,
        stored_core,
        guard=lambda: None,
    )
    stored_receipt = operator._result_receipt(  # noqa: SLF001
        plan=plan,
        journal=stored_core,
        candidates=(),
        authority_bindings_sha256="b" * 64,
    )
    operator._publish_receipt(  # noqa: SLF001
        state_dir,
        stored_receipt,
        guard=lambda: None,
    )

    accepted_root = {"maintenance": True}
    real_load_journal = operator._load_journal  # noqa: SLF001
    monkeypatch.setattr(
        operator.release_operator,
        "OperatorTransactionLock",
        lambda _path: _TestTransaction(),
    )
    monkeypatch.setattr(operator, "_load_journal", lambda _path: stored_journal)
    monkeypatch.setattr(
        operator,
        "_resume_plan_after_live_authority",
        lambda *_args, **_kwargs: (
            plan,
            (durable_path, durable.st_dev, durable.st_ino),
            stored_journal,
            {"activation_journal": state_dir / "activation.json"},
        ),
    )
    monkeypatch.setattr(operator, "_candidate_records", lambda _plan: ())
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: {"authority": True} if value is accepted_root else None,
    )
    monkeypatch.setattr(
        operator,
        "_load_accepted_root_plan",
        lambda *_args, **_kwargs: accepted_root,
    )
    live_checks: list[str] = []
    monkeypatch.setattr(
        operator,
        "_require_live_maintenance_recovery",
        lambda **_kwargs: live_checks.append("fresh") or (fresh_binding, {}),
    )
    monkeypatch.setattr(
        operator,
        "_live_authority_reauthenticate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(operator, "_validate_mutation_namespaces", lambda **_kwargs: None)
    monkeypatch.setattr(operator, "_resume_candidate_reauthenticate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(operator, "_terminal_absence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        operator,
        "_post_apply_reauthenticate",
        lambda *_args, **_kwargs: {"authority_bindings": {"bindings_sha256": "b" * 64}},
    )
    monkeypatch.setattr(
        operator,
        "_validate_journal_contract",
        lambda value, **_kwargs: operator._journal_core(value),  # noqa: SLF001
    )
    monkeypatch.setattr(
        operator,
        "_new_maintenance_recovery_epoch",
        lambda **_kwargs: (fresh_epoch, ()),
    )

    recovered = operator.apply_retention_plan(
        plan_path=None,
        expected_plan_sha256=str(plan["plan_sha256"]),
        state_dir=state_dir,
        _cycle_context=cycle,
        _maintenance_recovery=fresh_binding,
    )

    assert recovered == stored_receipt
    assert live_checks
    final = real_load_journal(state_dir / operator.APPLY_JOURNAL_NAME)
    assert final is not None
    assert final["phase"] == "applied"
    assert final["maintenance_recovery"] == stored_epoch
    assert final["receipt_sha256"] == stored_receipt["receipt_sha256"]


def test_live_recovery_is_reacquired_and_rejected_when_boot_authority_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted_root_sha256 = "d" * 64
    reviewed_sha256 = "e" * 64
    recovery = _recovery_binding(
        accepted_root_plan_sha256=accepted_root_sha256,
        reviewed_candidate_set_sha256=reviewed_sha256,
        boot="1",
    )
    authority = {
        "executing_initrd_sha256": recovery["executing_initrd_sha256"],
        "request_file_sha256": recovery["request_file_sha256"],
        "transaction_id": recovery["transaction_id"],
    }
    live_projection = {
        "authority_sha256": recovery["maintenance_authority_sha256"],
        "boot_id_sha256": recovery["maintenance_boot_id_sha256"],
        "executing_initrd_sha256": recovery["executing_initrd_sha256"],
        "namespace_epoch_sha256": recovery["maintenance_namespace_epoch_sha256"],
        "premount_receipt_sha256": recovery["maintenance_premount_receipt_sha256"],
        "process_epoch_sha256": recovery["maintenance_process_epoch_sha256"],
        "request_file_sha256": recovery["request_file_sha256"],
        "target_receipt_sha256": "f" * 64,
        "transaction_id": recovery["transaction_id"],
    }

    class LiveAuthority:
        def __init__(self, projection: dict[str, Any]) -> None:
            self._projection = projection

        def projection(self) -> dict[str, Any]:
            return dict(self._projection)

    live_values = [
        LiveAuthority(live_projection),
        LiveAuthority({**live_projection, "boot_id_sha256": "2" * 64}),
    ]
    monkeypatch.setattr(operator, "_maintenance_plan_authority", lambda _plan: authority)
    monkeypatch.setattr(
        retention,
        "build_maintenance_effect_authority",
        lambda **_kwargs: live_values.pop(0),
    )
    monkeypatch.setattr(
        retention,
        "_validated_maintenance_effect_authority",
        lambda value, **_kwargs: value,
    )
    operator._require_live_maintenance_recovery(  # noqa: SLF001
        plan={},
        durable_plan_path=tmp_path / "plan.json",
        recovery=recovery,
        accepted_root_plan_sha256=accepted_root_sha256,
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._require_live_maintenance_recovery(  # noqa: SLF001
            plan={},
            durable_plan_path=tmp_path / "plan.json",
            recovery=recovery,
            accepted_root_plan_sha256=accepted_root_sha256,
        )


def test_maintenance_cycle_equivalence_allows_only_boot_local_root_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    rebound = _plan_with_cycle_authority(
        device=102,
        mount_id=202,
        maintenance=False,
    )
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: {"maintenance": True} if value is accepted else None,
    )

    assert operator._maintenance_authority_bindings_equivalent(  # noqa: SLF001
        rebound["authority_bindings"],
        accepted["authority_bindings"],
    )
    assert operator._maintenance_retention_scope_equivalent(  # noqa: SLF001
        rebound["retention_scope"],
        accepted["retention_scope"],
    )
    assert (
        operator._receipt_authority_bindings_sha256(  # noqa: SLF001
            accepted,
            rebound,
            maintenance=True,
        )
        == accepted["authority_bindings"]["bindings_sha256"]
    )
    assert operator._cycle_authority_equivalent(rebound, accepted)  # noqa: SLF001

    for field, changed in (
        ("file_sha256", "d" * 64),
        ("inode", 52),
        ("path", "/srv/friday/state/other-scope.json"),
    ):
        tampered_scope = {
            **rebound["retention_scope"],
            field: changed,
        }
        assert not operator._maintenance_retention_scope_equivalent(  # noqa: SLF001
            tampered_scope,
            accepted["retention_scope"],
        )

    tampered_binding = json.loads(_canonical(rebound["authority_bindings"]))
    tampered_binding["canonical_evidence_roots"][0]["inode"] += 1
    binding_core = {name: item for name, item in tampered_binding.items() if name != "bindings_sha256"}
    tampered_binding["bindings_sha256"] = hashlib.sha256(_canonical(binding_core)).hexdigest()
    assert not operator._maintenance_authority_bindings_equivalent(  # noqa: SLF001
        tampered_binding,
        accepted["authority_bindings"],
    )

    tampered_root = json.loads(_canonical(rebound))
    tampered_root["inventory_roots"][0]["inode"] += 1
    assert not operator._cycle_authority_equivalent(  # noqa: SLF001
        tampered_root,
        accepted,
    )

    stale_digest = json.loads(_canonical(rebound))
    stale_digest["authority_bindings"]["canonical_evidence_roots"][0]["device"] += 1
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_apply_plan_invalid$",
    ):
        operator._cycle_authority_equivalent(  # noqa: SLF001
            stale_digest,
            accepted,
        )

    ordinary = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=False,
    )
    monkeypatch.setattr(operator, "_maintenance_plan_authority", lambda _value: None)
    assert not operator._cycle_authority_equivalent(  # noqa: SLF001
        rebound,
        ordinary,
    )
    evidence = tuple(
        retention.CanonicalEvidenceRoot(
            path=Path(item["path"]),
            authority_path=Path(item["authority_path"]),
            authority_sha256=str(item["authority_sha256"]),
        )
        for item in ordinary["authority_bindings"]["canonical_evidence_roots"]
    )
    live_scope = SimpleNamespace(
        backup_root=Path(str(rebound["backup_root"])),
        inventory_roots=tuple(Path(item["path"]) for item in rebound["inventory_roots"]),
        backup_inventory_roots=(),
        canonical_evidence_roots=evidence,
        receipt=rebound["retention_scope"],
    )
    monkeypatch.setattr(
        retention,
        "load_retention_scope_authority",
        lambda **_kwargs: live_scope,
    )
    assert operator._plan_inputs(ordinary, maintenance=True)[  # noqa: SLF001
        "inventory_roots"
    ] == (Path("/srv/friday/releases"),)
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_apply_retention_scope_changed$",
    ):
        operator._plan_inputs(ordinary)  # noqa: SLF001


def test_maintenance_cycle_nlink_delta_requires_applied_reviewed_direct_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    accepted["inventory_roots"][0]["nlink"] = 4
    accepted["targets"] = [
        {
            **{
                name: value
                for name, value in _portable_candidate(index).items()
                if name not in {"collection", "portable_inventory_sha256"}
            },
            "decision": "retain",
            "device": 101,
            "inventory_sha256": str(index) * 64,
            "mount_id": 201,
            "path": f"/srv/friday/releases/candidate-{suffix}",
            "reason": "deferred_batch_bound",
        }
        for index, suffix in ((1, "a"), (2, "b"))
    ]
    current = _plan_with_cycle_authority(
        device=102,
        mount_id=202,
        maintenance=False,
    )
    current["inventory_roots"][0]["nlink"] = 2
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: {"maintenance": True} if value is accepted else None,
    )
    applied = frozenset(
        {
            "/srv/friday/releases/candidate-a",
            "/srv/friday/releases/candidate-b",
        }
    )

    assert operator._cycle_authority_equivalent(  # noqa: SLF001
        current,
        accepted,
        applied_before_paths=applied,
    )
    assert not operator._cycle_authority_equivalent(  # noqa: SLF001
        current,
        accepted,
        applied_before_paths=frozenset({"/srv/friday/releases/candidate-a"}),
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_convergence_chain_invalid$",
    ):
        operator._cycle_authority_equivalent(  # noqa: SLF001
            current,
            accepted,
            applied_before_paths=frozenset({"/srv/other/candidate-a"}),
        )


def test_scratch_cycle_rebind_requires_sealed_portable_candidate_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/srv/friday/releases/scratch"
    old_digest = "1" * 64
    current_digest = "2" * 64

    def scratch_target(*, digest: str, decision: str, reason: str) -> dict[str, Any]:
        return {
            "allocated_bytes": 0,
            "decision": decision,
            "device": 101,
            "entry_count": 1,
            "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
            "identity": {
                "allocated_bytes": 0,
                "contour": retention._SCRATCH_CONTOUR,  # noqa: SLF001
                "entry_count": 1,
                "inventory_sha256": digest,
                "recursive_bytes": 0,
            },
            "inode": 71,
            "inventory_sha256": digest,
            "mode": stat.S_IFDIR | 0o700,
            "mount_id": 201,
            "nlink": 2,
            "path": path,
            "reason": reason,
            "recursive_bytes": 0,
            "type": "directory",
            "writable_authority_sha256": "a" * 64,
        }

    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    accepted["reviewed_scratch_targets"] = [
        {
            "contour": retention._SCRATCH_CONTOUR,  # noqa: SLF001
            "inventory_sha256": old_digest,
            "path": path,
        }
    ]
    accepted["targets"] = [
        scratch_target(
            digest=old_digest,
            decision="retain",
            reason="deferred_batch_bound",
        )
    ]
    current = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    current["reviewed_scratch_targets"] = [
        {
            "contour": retention._SCRATCH_CONTOUR,  # noqa: SLF001
            "inventory_sha256": current_digest,
            "path": path,
        }
    ]
    current["targets"] = [
        scratch_target(
            digest=current_digest,
            decision="delete_candidate",
            reason="retirable_reviewed_scratch",
        )
    ]
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: {"maintenance": True} if value is accepted or value is current else None,
    )

    assert operator._cycle_authority_equivalent(  # noqa: SLF001
        current,
        accepted,
        portable_candidate_paths=frozenset({path}),
    )
    assert not operator._cycle_authority_equivalent(current, accepted)  # noqa: SLF001


def test_later_batch_scratch_rebinds_present_and_preserves_deleted_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scratch"
    path.mkdir(mode=0o700)
    mode = stat.S_IFDIR | 0o700
    record = (".", 102, 31, mode, 2, os.getuid(), 0, 0, 0, 202, os.getgid(), 0)
    snapshot = SimpleNamespace(records=(record,))
    exact_sha256 = hashlib.sha256(_canonical(snapshot.records)).hexdigest()
    portable_sha256 = operator._portable_inventory_sha256(snapshot)  # noqa: SLF001
    accepted = _portable_candidate(30)
    accepted.update(
        {
            "allocated_bytes": 0,
            "entry_count": 1,
            "identity": {
                "allocated_bytes": 0,
                "contour": retention._SCRATCH_CONTOUR,  # noqa: SLF001
                "entry_count": 1,
                "inventory_sha256": portable_sha256,
                "recursive_bytes": 0,
            },
            "inode": 31,
            "mode": mode,
            "nlink": 2,
            "path": str(path),
            "portable_inventory_sha256": portable_sha256,
            "recursive_bytes": 0,
        }
    )
    sealed = retention.ReviewedScratchTarget(
        path=path,
        inventory_sha256="1" * 64,
    )
    monkeypatch.setattr(retention, "_snapshot", lambda _path: snapshot)

    rebound = operator._rebound_maintenance_reviewed_scratch_targets(  # noqa: SLF001
        (sealed,),
        (accepted,),
    )

    assert rebound[0].inventory_sha256 == exact_sha256
    path.rmdir()
    assert operator._rebound_maintenance_reviewed_scratch_targets(  # noqa: SLF001
        (sealed,),
        (accepted,),
    ) == (sealed,)

    path.mkdir(mode=0o700)
    changed = list(record)
    changed[2] = 32
    monkeypatch.setattr(
        retention,
        "_snapshot",
        lambda _path: SimpleNamespace(records=(tuple(changed),)),
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_review_changed$",
    ):
        operator._rebound_maintenance_reviewed_scratch_targets(  # noqa: SLF001
            (sealed,),
            (accepted,),
        )


def test_prior_applied_reappearance_cannot_hide_behind_root_nlink_balance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "inventory"
    root.mkdir(mode=0o700)
    applied = root / "applied-a"
    unrelated = root / "unrelated-x"
    applied.mkdir(mode=0o700)
    unrelated.mkdir(mode=0o700)

    def target(path: Path, index: int) -> dict[str, Any]:
        portable = _portable_candidate(index)
        return {
            **{
                name: value
                for name, value in portable.items()
                if name not in {"collection", "portable_inventory_sha256"}
            },
            "decision": "delete_candidate",
            "device": 101,
            "inventory_sha256": f"{index:x}" * 64,
            "mount_id": 201,
            "path": str(path),
            "reason": "retirable_release",
        }

    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    accepted["inventory_roots"] = [
        {
            "device": 101,
            "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
            "inode": root.stat().st_ino,
            "mount_id": 201,
            "nlink": root.stat().st_nlink,
            "path": str(root),
            "type": "directory",
            "uid": os.geteuid(),
            "writable_authority_sha256": "f" * 64,
        }
    ]
    accepted["targets"] = [target(applied, 1), target(unrelated, 2)]
    applied.rmdir()
    unrelated.rmdir()
    applied.mkdir(mode=0o700)
    monkeypatch.setattr(
        retention,
        "_descriptor_filesystem_magic",
        lambda _descriptor: retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
    )
    monkeypatch.setattr(
        retention,
        "_descriptor_has_posix_acl",
        lambda _descriptor: False,
    )
    monkeypatch.setattr(
        retention,
        "_writable_mode_authority",
        lambda *_args, **_kwargs: "f" * 64,
    )
    before = tuple(sorted(root.iterdir()))

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_convergence_chain_invalid$",
    ):
        operator._require_maintenance_applied_paths_absent(  # noqa: SLF001
            accepted,
            frozenset({str(applied)}),
            guard=lambda: None,
        )

    assert tuple(sorted(root.iterdir())) == before
    assert applied.is_dir()


def test_effective_candidates_enforce_exact_current_batch_root_nlink_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "inventory"
    root.mkdir(mode=0o700)
    unrelated = root / "unrelated"
    unrelated.mkdir(mode=0o700)
    candidate = {
        "allocated_bytes": 0,
        "candidate_sha256": "1" * 64,
        "device": 101,
        "entry_count": 1,
        "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "identity": {"release": "sealed"},
        "inode": 31,
        "inventory_sha256": "2" * 64,
        "mode": stat.S_IFDIR | 0o700,
        "mount_id": 201,
        "nlink": 2,
        "path": str(root / "already-deleted"),
        "recursive_bytes": 0,
        "root_device": 101,
        "root_filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "root_inode": root.stat().st_ino,
        "root_mount_id": 201,
        "root_nlink": root.stat().st_nlink + 1,
        "root_writable_authority_sha256": "3" * 64,
        "type": "directory",
        "writable_authority_sha256": "4" * 64,
    }
    entry = {
        "quarantine_name": ".friday-retention-q-v1-test-000000",
        "status": "deleted",
    }
    stable = {"portable_inventory_sha256": "5" * 64}

    def root_epoch(_candidate: dict[str, Any]) -> tuple[dict[str, int], int]:
        return (
            {
                "root_device": 102,
                "root_inode": root.stat().st_ino,
                "root_mount_id": 202,
            },
            root.stat().st_nlink,
        )

    monkeypatch.setattr(operator, "_maintenance_root_epoch", root_epoch)
    effective, epoch = operator._maintenance_effective_candidates(  # noqa: SLF001
        (candidate,),
        (entry,),
        (stable,),
    )
    assert effective[0]["device"] == 102
    assert epoch[0]["presence"] == "absent"

    foreign = root / "foreign"
    foreign.mkdir(mode=0o700)
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._maintenance_effective_candidates(  # noqa: SLF001
            (candidate,),
            (entry,),
            (stable,),
        )
    foreign.rmdir()
    unrelated.rmdir()
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_maintenance_recovery_invalid$",
    ):
        operator._maintenance_effective_candidates(  # noqa: SLF001
            (candidate,),
            (entry,),
            (stable,),
        )


def test_effective_scratch_rebind_flows_into_maintenance_result_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(parents=True, mode=0o700)
    root = tmp_path / "inventory"
    root.mkdir(mode=0o700)
    source = root / "scratch"
    source.mkdir(mode=0o700)
    old_digest = "1" * 64
    current_digest = "2" * 64
    portable_digest = "3" * 64
    candidate = {
        "allocated_bytes": 0,
        "candidate_sha256": "4" * 64,
        "device": 101,
        "entry_count": 1,
        "filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "identity": {
            "allocated_bytes": 0,
            "contour": retention._SCRATCH_CONTOUR,  # noqa: SLF001
            "entry_count": 1,
            "inventory_sha256": old_digest,
            "recursive_bytes": 0,
        },
        "inode": source.stat().st_ino,
        "inventory_sha256": old_digest,
        "mode": stat.S_IFDIR | 0o700,
        "mount_id": 201,
        "nlink": 2,
        "path": str(source),
        "recursive_bytes": 0,
        "root_device": 101,
        "root_filesystem_magic": retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "root_inode": root.stat().st_ino,
        "root_mount_id": 201,
        "root_nlink": root.stat().st_nlink,
        "root_writable_authority_sha256": "5" * 64,
        "type": "directory",
        "writable_authority_sha256": "6" * 64,
    }
    stable = {"portable_inventory_sha256": portable_digest}
    monkeypatch.setattr(
        operator,
        "_maintenance_root_epoch",
        lambda _candidate: (
            {
                "root_device": 102,
                "root_inode": root.stat().st_ino,
                "root_mount_id": 202,
            },
            root.stat().st_nlink,
        ),
    )
    monkeypatch.setattr(
        retention,
        "_observe_target",
        lambda _path: SimpleNamespace(
            allocated_bytes=0,
            entry_count=1,
            filesystem_magic=retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
            has_group_world_writable=False,
            has_hardlink=False,
            has_special=False,
            has_symlink=False,
            inode=source.stat().st_ino,
            inventory_sha256=current_digest,
            kind="directory",
            mount_id=202,
            owner_ok=True,
            raced=False,
            device=102,
            total_allocated_bytes=0,
            total_bytes=0,
        ),
    )
    monkeypatch.setattr(operator, "_candidate_matches_observation", lambda *_args: None)
    effective, _candidate_epoch = operator._maintenance_effective_candidates(  # noqa: SLF001
        (candidate,),
        ({"quarantine_name": ".friday-retention-q-v1-test-000000", "status": "pending"},),
        (stable,),
    )
    assert effective[0]["inventory_sha256"] == current_digest
    assert effective[0]["identity"]["inventory_sha256"] == current_digest

    exact = operator._reviewed_identity(effective[0], collection="targets")  # noqa: SLF001
    accepted = operator._portable_reviewed_identity_projection(  # noqa: SLF001
        exact,
        portable_inventory_sha256=portable_digest,
    )
    binding = {
        "executing_initrd_sha256": "7" * 64,
        "maintenance_premount_receipt_sha256": "8" * 64,
        "plan_sha256": "9" * 64,
        "recovery_sha256": "a" * 64,
        "reviewed_candidate_set_sha256": "b" * 64,
    }
    recovery_epoch = {
        "binding": binding,
        "reviewed_authority": {},
        "stable_candidates": [stable],
    }
    monkeypatch.setattr(
        operator,
        "_validate_maintenance_recovery_epoch",
        lambda *_args, **_kwargs: recovery_epoch,
    )
    monkeypatch.setattr(
        operator,
        "_load_maintenance_review_authority",
        lambda *_args, **_kwargs: ([accepted], {}),
    )
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=plan_dir / "accepted.json",
        accepted_root_plan_sha256="9" * 64,
        reviewed_full_candidate_set_sha256=hashlib.sha256(_canonical([accepted])).hexdigest(),
        retention_epoch_sha256="c" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    plan = {
        "backup_targets": [],
        "plan_sha256": "d" * 64,
        "retention_scope": {
            "file_sha256": "e" * 64,
            "schema": retention.RETENTION_SCOPE_SCHEMA,
        },
        "targets": [{"path": str(source)}],
    }
    journal = {
        **cycle,
        "durable_plan": {"path": str(plan_dir / "current.json")},
        "entries": [
            {
                "actual_allocated_bytes": 0,
                "actual_bytes": 0,
                "actual_inodes": 1,
                "candidate_sha256": candidate["candidate_sha256"],
                "residual_authority": {"count": 1, "sha256": "f" * 64},
                "status": "deleted",
            }
        ],
        "filesystem_after": [],
        "filesystem_before": [],
        "maintenance_recovery": {},
        "retention_scope_schema": retention.RETENTION_SCOPE_SCHEMA,
        "retention_scope_sha256": "e" * 64,
        "schema": operator.MAINTENANCE_APPLY_JOURNAL_SCHEMA,
        "transaction_id": "0" * 64,
    }
    receipt = operator._result_receipt(  # noqa: SLF001
        plan=plan,
        journal=journal,
        candidates=effective,
        authority_bindings_sha256="1" * 64,
        allow_review_device_rebind=True,
    )
    assert receipt["maintenance_candidate_identity_sha256s"] == [
        hashlib.sha256(_canonical(accepted)).hexdigest()
    ]


@pytest.mark.parametrize("recovery_point", ("pre_first_journal", "between_batches"))
def test_fresh_maintenance_batch_accepts_boot_local_root_rebind(
    recovery_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portable = _portable_candidate(1)

    def effectful_plan(*, device: int, mount_id: int) -> dict[str, Any]:
        plan = _plan_with_cycle_authority(
            device=device,
            mount_id=mount_id,
            maintenance=True,
        )
        target = {
            **{
                name: item
                for name, item in portable.items()
                if name not in {"collection", "portable_inventory_sha256"}
            },
            "decision": "delete_candidate",
            "device": device,
            "inventory_sha256": "d" * 64,
            "mount_id": mount_id,
            "reason": "retirable_release",
        }
        core = {
            **{name: item for name, item in plan.items() if name != "plan_sha256"},
            "apply_authority": True,
            "targets": [target],
        }
        return {
            **core,
            "plan_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
        }

    accepted = effectful_plan(device=101, mount_id=201)
    generated = effectful_plan(device=102, mount_id=202)
    portable_values = [portable]
    portable_sha256 = operator._portable_identity_set_sha256(  # noqa: SLF001
        portable_values
    )
    ordinal = 0 if recovery_point == "pre_first_journal" else 1
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=tmp_path / "accepted.json",
        accepted_root_plan_sha256=str(accepted["plan_sha256"]),
        reviewed_full_candidate_set_sha256=portable_sha256,
        retention_epoch_sha256="6" * 64,
        batch_ordinal=ordinal,
        previous_receipt_sha256="" if ordinal == 0 else "7" * 64,
    )
    scope = SimpleNamespace(
        backup_root=Path("/srv/friday/backups"),
        inventory_roots=(Path("/srv/friday/releases"),),
        backup_inventory_roots=(),
        receipt=accepted["retention_scope"],
    )
    inputs = {
        "activation_journal": Path("/srv/friday/state/activation.json"),
        "backup_inventory_roots": (),
        "backup_root": Path("/srv/friday/backups"),
        "canonical_evidence_roots": (),
        "inventory_roots": (Path("/srv/friday/releases"),),
        "reviewed_scratch_targets": (),
        "unit_journal": Path("/srv/friday/state/unit.json"),
    }
    seed = {
        "backup_targets": generated["backup_targets"],
        "classification_status": "scope_seed",
        "plan_sha256": "8" * 64,
        "targets": generated["targets"],
    }
    planned = iter((seed, generated))
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: (
            {"maintenance": True} if value.get("schema") == retention.MAINTENANCE_PLAN_SCHEMA else None
        ),
    )
    monkeypatch.setattr(
        retention,
        "load_retention_scope_authority",
        lambda **_kwargs: scope,
    )
    monkeypatch.setattr(
        retention,
        "build_retention_authority_bindings",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        retention,
        "plan_release_artifact_retention",
        lambda **_kwargs: next(planned),
    )
    monkeypatch.setattr(
        retention,
        "_build_maintenance_open_inventory",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        operator,
        "_portable_reviewed_identity",
        lambda *_args, **_kwargs: dict(portable),
    )
    monkeypatch.setattr(
        operator,
        "_fresh_maintenance_portable_identities",
        lambda **_kwargs: (portable,),
    )
    monkeypatch.setattr(
        operator,
        "_maintenance_cycle_portable_identities",
        lambda *_args, **_kwargs: (portable,),
    )
    monkeypatch.setattr(
        operator,
        "_load_accepted_root_plan",
        lambda *_args, **_kwargs: accepted,
    )
    reviewed = accepted if ordinal == 0 else {"plan_sha256": "9" * 64}

    result = operator._fresh_plan_for_cycle(  # noqa: SLF001
        reviewed=reviewed,
        inputs=inputs,
        state_dir=tmp_path,
        cycle_context=cycle,
        maintenance_recovery={},
    )

    assert result == generated
    assert result["schema"] == retention.MAINTENANCE_PLAN_SCHEMA
    assert len(operator._candidate_records(result)) == 1  # noqa: SLF001


def test_zero_candidate_maintenance_journal_resume_accepts_boot_local_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    reviewed = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=False,
    )
    fresh = _plan_with_cycle_authority(
        device=102,
        mount_id=202,
        maintenance=False,
    )
    durable = tmp_path / "terminal.json"
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=tmp_path / "accepted.json",
        accepted_root_plan_sha256=str(accepted["plan_sha256"]),
        reviewed_full_candidate_set_sha256="a" * 64,
        retention_epoch_sha256="b" * 64,
        batch_ordinal=2,
        previous_receipt_sha256="c" * 64,
    )
    journal = {
        **cycle,
        "phase": "applying",
        "schema": operator.MAINTENANCE_APPLY_JOURNAL_SCHEMA,
    }
    monkeypatch.setattr(
        operator,
        "_resume_plan_from_state",
        lambda *_args, **_kwargs: (reviewed, (durable, 101, 71), journal),
    )
    monkeypatch.setattr(operator, "_plan_inputs", lambda _plan, **_kwargs: {})
    monkeypatch.setattr(
        operator,
        "_live_authority_reauthenticate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(operator, "_candidate_records", lambda _plan: ())
    monkeypatch.setattr(operator, "_fresh_plan_for_cycle", lambda **_kwargs: fresh)
    monkeypatch.setattr(
        operator,
        "_maintenance_applied_paths_before_batch",
        lambda *_args, **_kwargs: frozenset(),
    )
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: (
            {"maintenance": True} if value.get("schema") == retention.MAINTENANCE_PLAN_SCHEMA else None
        ),
    )
    monkeypatch.setattr(
        operator,
        "_load_accepted_root_plan",
        lambda *_args, **_kwargs: accepted,
    )
    monkeypatch.setattr(
        operator,
        "_require_live_maintenance_recovery",
        lambda **_kwargs: ({}, {}),
    )

    resumed, _durable, loaded, _inputs = operator._resume_plan_after_live_authority(  # noqa: SLF001
        tmp_path,
        expected_plan_sha256=str(reviewed["plan_sha256"]),
        guard=lambda: None,
        maintenance_recovery={},
    )

    assert resumed is reviewed
    assert loaded is journal


def test_durable_plan_device_rebind_authenticates_two_link_stage_before_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(mode=0o700, parents=True)
    digest = "d" * 64
    path = plan_dir / f"plan-{digest}.json"
    staged = plan_dir / f".{path.name}.new"
    reviewed = {"plan_sha256": digest}
    path.write_bytes(_canonical(reviewed) + b"\n")
    path.chmod(0o400)
    os.link(path, staged)
    status = path.stat()
    journal = {
        "durable_plan": {
            "device": int(status.st_dev) + 1,
            "inode": int(status.st_ino),
            "path": str(path),
            "sha256": digest,
        },
        "plan_sha256": digest,
    }
    observed: list[tuple[bool, bool]] = []
    monkeypatch.setattr(operator, "_load_journal", lambda _path: journal)
    monkeypatch.setattr(
        retention,
        "_open_absolute_directory_chain",
        lambda *_args, **_kwargs: (
            os.open(plan_dir, os.O_RDONLY | os.O_DIRECTORY),
            (),
            (),
        ),
    )
    monkeypatch.setattr(
        retention,
        "_require_pinned_directory",
        lambda *_args, **_kwargs: None,
    )

    def provenance(
        _state_dir: Path,
        _journal: dict[str, Any],
        **kwargs: Any,
    ) -> bool:
        observed.append(
            (
                bool(kwargs["allow_recoverable_two_link"]),
                staged.exists(),
            )
        )
        return True

    monkeypatch.setattr(operator, "_maintenance_resume_provenance", provenance)
    monkeypatch.setattr(operator, "_read_plan", lambda *_args, **_kwargs: reviewed)

    resumed, _durable, _journal = operator._resume_plan_from_state(  # noqa: SLF001
        state_dir,
        expected_plan_sha256=digest,
        guard=lambda: None,
    )

    assert resumed is reviewed
    assert observed == [(True, True)]
    assert not staged.exists()
    assert path.stat().st_nlink == 1


@pytest.mark.parametrize("tamper", ("body", "mode", "journal_inode"))
def test_durable_plan_two_link_tamper_is_rejected_without_stage_cleanup(
    tamper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(mode=0o700, parents=True)
    digest = "c" * 64
    reviewed = {"plan_sha256": digest}
    path = plan_dir / f"plan-{digest}.json"
    staged = plan_dir / f".{path.name}.new"
    path.write_bytes(b"wrong-plan-body\n" if tamper == "body" else _canonical(reviewed) + b"\n")
    path.chmod(0o600 if tamper == "mode" else 0o400)
    os.link(path, staged)
    status = path.stat()
    journal = {
        "durable_plan": {
            "device": int(status.st_dev),
            "inode": int(status.st_ino) + (1 if tamper == "journal_inode" else 0),
            "path": str(path),
            "sha256": digest,
        },
        "plan_sha256": digest,
    }
    monkeypatch.setattr(operator, "_load_journal", lambda _path: journal)
    monkeypatch.setattr(operator, "_read_plan", lambda *_args, **_kwargs: reviewed)
    before = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_apply_resume_authority_missing$",
    ):
        operator._resume_plan_from_state(  # noqa: SLF001
            state_dir,
            expected_plan_sha256=digest,
            guard=lambda: None,
        )

    assert staged.exists()
    assert os.path.samefile(path, staged)
    assert path.stat().st_nlink == 2
    assert (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) == before


def test_linked_plan_and_residual_stages_authenticate_before_cleanup(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    plan_dir = state_dir / operator.APPLY_PLAN_DIRECTORY
    plan_dir.mkdir(mode=0o700)
    plan = {"plan_sha256": "b" * 64, "value": 1}
    plan_path = plan_dir / f"plan-{plan['plan_sha256']}.json"
    plan_stage = plan_dir / f".{plan_path.name}.new"
    plan_path.write_bytes(b"wrong-plan\n")
    plan_path.chmod(0o400)
    os.link(plan_path, plan_stage)

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_apply_plan_changed$",
    ):
        operator._persist_reviewed_plan(  # noqa: SLF001
            state_dir,
            plan,
            guard=lambda: None,
            allow_incomplete_stage_repair=True,
        )
    assert plan_stage.exists()
    assert os.path.samefile(plan_path, plan_stage)
    assert plan_path.stat().st_nlink == 2

    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "member.bin").write_bytes(b"payload")
    snapshot = retention._snapshot(source)  # noqa: SLF001
    transaction_id = "a" * 64
    object_dir = state_dir / operator.OBJECT_AUTHORITY_DIRECTORY
    object_dir.mkdir(mode=0o700)
    residual_path = object_dir / f"objects-{transaction_id}-000000.bin"
    residual_stage = object_dir / f".{residual_path.name}.crash.new"
    payload = operator._residual_authority_payload(snapshot)  # noqa: SLF001
    residual_path.write_bytes(b"x" * len(payload))
    residual_path.chmod(0o600)
    os.link(residual_path, residual_stage)

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_apply_residual_authority_invalid$",
    ):
        operator._persist_residual_authority(  # noqa: SLF001
            state_dir,
            transaction_id,
            0,
            snapshot,
            guard=lambda: None,
        )
    assert residual_stage.exists()
    assert os.path.samefile(residual_path, residual_stage)
    assert residual_path.stat().st_nlink == 2


def test_between_batch_chain_accepts_boot_local_root_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    applied_plan = _plan_with_cycle_authority(
        device=102,
        mount_id=202,
        maintenance=True,
    )
    portable = _portable_candidate(1)
    candidate = {"path": str(portable["path"])}
    identity_sha256 = hashlib.sha256(_canonical(portable)).hexdigest()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=tmp_path / "accepted.json",
        accepted_root_plan_sha256=str(accepted["plan_sha256"]),
        reviewed_full_candidate_set_sha256=operator._portable_identity_set_sha256(  # noqa: SLF001
            [portable]
        ),
        retention_epoch_sha256="6" * 64,
        batch_ordinal=0,
        previous_receipt_sha256="",
    )
    receipt = {
        **cycle,
        "batch_ordinal": 0,
        "previous_receipt_sha256": "",
    }
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: (
            {"maintenance": True} if value.get("schema") == retention.MAINTENANCE_PLAN_SCHEMA else None
        ),
    )
    monkeypatch.setattr(
        operator,
        "_candidate_records",
        lambda value: (candidate,) if value is applied_plan else (),
    )
    monkeypatch.setattr(
        operator,
        "_maintenance_cycle_portable_identities",
        lambda *_args, **_kwargs: (portable,),
    )
    monkeypatch.setattr(
        operator,
        "_validate_apply_receipt_plan",
        lambda *_args, **_kwargs: (applied_plan, (candidate,)),
    )
    monkeypatch.setattr(
        operator,
        "_candidate_identity_digests",
        lambda *_args, **_kwargs: {identity_sha256},
    )
    monkeypatch.setattr(
        operator,
        "_validated_maintenance_plan_portable_identities",
        lambda *_args, **_kwargs: (portable,),
    )

    assert operator._applied_cycle_identities(  # noqa: SLF001
        tmp_path,
        latest_receipt=receipt,
        accepted_root=accepted,
        cycle_context=cycle,
        maintenance_recovery={},
    ) == {identity_sha256}

    other = _portable_candidate(2)
    other_sha256 = hashlib.sha256(_canonical(other)).hexdigest()
    monkeypatch.setattr(
        operator,
        "_maintenance_cycle_portable_identities",
        lambda *_args, **_kwargs: (portable, other),
    )
    monkeypatch.setattr(
        operator,
        "_candidate_identity_digests",
        lambda *_args, **_kwargs: {other_sha256},
    )
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_convergence_chain_invalid$",
    ):
        operator._applied_cycle_identities(  # noqa: SLF001
            tmp_path,
            latest_receipt=receipt,
            accepted_root=accepted,
            cycle_context=cycle,
            maintenance_recovery={},
        )


def test_applied_terminal_chain_accepts_boot_local_root_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = _plan_with_cycle_authority(
        device=101,
        mount_id=201,
        maintenance=True,
    )
    batch_plan = _plan_with_cycle_authority(
        device=102,
        mount_id=202,
        maintenance=True,
    )
    terminal_plan = _plan_with_cycle_authority(
        device=103,
        mount_id=203,
        maintenance=False,
    )
    portable = _portable_candidate(1)
    identity_sha256 = hashlib.sha256(_canonical(portable)).hexdigest()
    reviewed_sha256 = hashlib.sha256(_canonical([identity_sha256])).hexdigest()
    cycle = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=tmp_path / "accepted.json",
        accepted_root_plan_sha256=str(accepted["plan_sha256"]),
        reviewed_full_candidate_set_sha256=reviewed_sha256,
        retention_epoch_sha256="6" * 64,
        batch_ordinal=1,
        previous_receipt_sha256="7" * 64,
    )
    batch_receipt = {
        **cycle,
        "admission_status": "nonterminal",
        "authority_bindings_sha256": batch_plan["authority_bindings"]["bindings_sha256"],
        "batch_ordinal": 0,
        "previous_receipt_sha256": "",
        "privileged_probe_role": "global_premount_effect_authority",
        "receipt_sha256": "8" * 64,
        "retention_scope_schema": retention.RETENTION_SCOPE_SCHEMA,
        "retention_scope_sha256": "c" * 64,
        "schema": operator.MAINTENANCE_APPLY_RECEIPT_SCHEMA,
    }
    terminal_receipt = {
        **cycle,
        "admission_status": "release_admissible",
        "authority_bindings_sha256": terminal_plan["authority_bindings"]["bindings_sha256"],
        "batch_ordinal": 1,
        "previous_receipt_sha256": batch_receipt["receipt_sha256"],
        "privileged_probe_role": "diagnostic_prerequisite",
        "receipt_sha256": "9" * 64,
        "retention_scope_schema": retention.RETENTION_SCOPE_SCHEMA,
        "retention_scope_sha256": "c" * 64,
        "schema": operator.APPLY_RECEIPT_SCHEMA,
        "universal_absence_proof": False,
    }
    journal = {
        **cycle,
        "phase": "applied",
        "plan_sha256": terminal_plan["plan_sha256"],
        "receipt_sha256": terminal_receipt["receipt_sha256"],
        "schema": operator.APPLY_JOURNAL_SCHEMA,
        "transaction_id": "a" * 64,
    }
    durable = tmp_path / "terminal.json"
    monkeypatch.setattr(operator, "_load_journal", lambda _path: journal)
    monkeypatch.setattr(
        operator,
        "_resume_plan_from_state",
        lambda *_args, **_kwargs: (
            terminal_plan,
            (durable, 103, 71),
            journal,
        ),
    )
    monkeypatch.setattr(
        operator,
        "_candidate_records",
        lambda value: () if value is terminal_plan else ({"candidate": True},),
    )
    monkeypatch.setattr(
        operator,
        "_validate_journal_contract",
        lambda *_args, **_kwargs: journal,
    )
    monkeypatch.setattr(
        operator,
        "_result_receipt",
        lambda **_kwargs: terminal_receipt,
    )
    monkeypatch.setattr(
        operator,
        "_read_apply_receipt",
        lambda *_args, **_kwargs: terminal_receipt,
    )
    monkeypatch.setattr(
        operator,
        "_load_accepted_root_plan",
        lambda *_args, **_kwargs: accepted,
    )
    monkeypatch.setattr(operator, "_preflight_reviewed_root", lambda _plan: None)
    monkeypatch.setattr(
        operator,
        "_maintenance_plan_authority",
        lambda value: (
            {"maintenance": True} if value.get("schema") == retention.MAINTENANCE_PLAN_SCHEMA else None
        ),
    )

    def validate_receipt(
        _state_dir: Path,
        receipt: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[dict[str, bool], ...]]:
        if receipt is terminal_receipt:
            return terminal_plan, ()
        return batch_plan, ({"candidate": True},)

    monkeypatch.setattr(operator, "_validate_apply_receipt_plan", validate_receipt)
    monkeypatch.setattr(
        operator,
        "_candidate_identity_digests",
        lambda plan, *_args, **_kwargs: set() if plan is terminal_plan else {identity_sha256},
    )
    monkeypatch.setattr(
        operator,
        "_find_apply_receipt_by_sha256",
        lambda *_args, **_kwargs: batch_receipt,
    )
    monkeypatch.setattr(
        operator,
        "_applied_cycle_identities",
        lambda *_args, **_kwargs: {identity_sha256},
    )

    terminal = operator._validated_terminal_chain(  # noqa: SLF001
        tmp_path,
        retention_epoch_sha256="6" * 64,
        guard=lambda: None,
    )

    assert terminal is not None
    assert terminal["terminal_apply_receipt_sha256"] == "9" * 64


def test_restore_quarantine_guard_failure_before_fchmod_is_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    source = parent / "candidate"
    source.mkdir(mode=0o755)
    quarantine_name = ".friday-retention-q-v1-test"
    quarantine = parent / quarantine_name
    source.rename(quarantine)
    quarantine.chmod(0o700)
    quarantine_status = quarantine.stat()
    monkeypatch.setattr(
        operator,
        "_root_descriptor",
        lambda _candidate: (
            os.open(parent, os.O_RDONLY | os.O_DIRECTORY),
            (),
            (),
        ),
    )
    monkeypatch.setattr(retention, "_descriptor_mount_id", lambda _fd: 301)
    restore_guards = 0

    def fail_before_restore_mode() -> None:
        nonlocal restore_guards
        restore_guards += 1
        if restore_guards == 2:
            raise RuntimeError("maintenance authority drift")

    with pytest.raises(RuntimeError, match="^maintenance authority drift$"):
        operator._restore_quarantine(  # noqa: SLF001
            {
                "device": quarantine_status.st_dev,
                "inode": quarantine_status.st_ino,
                "mode": stat.S_IFDIR | 0o755,
                "mount_id": 301,
                "path": str(source),
            },
            quarantine_name,
            guard=fail_before_restore_mode,
        )
    assert stat.S_IMODE(quarantine.stat().st_mode) == 0o700
    assert not source.exists()


def test_unlink_regular_guard_failure_before_chmod_is_non_mutating(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    member = parent / "member.bin"
    member.write_bytes(b"payload")
    member.chmod(0o400)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = os.stat(member.name, dir_fd=directory_fd, follow_symlinks=False)
        with pytest.raises(RuntimeError, match="^maintenance authority drift$"):
            operator._unlink_regular_with_lease(  # noqa: SLF001
                directory_fd,
                member.name,
                before,
                root_mount_id=301,
                byte_counter=[0],
                guard=lambda: (_ for _ in ()).throw(RuntimeError("maintenance authority drift")),
            )
    finally:
        os.close(directory_fd)
    assert stat.S_IMODE(member.stat().st_mode) == 0o400


def test_post_quarantine_maintenance_probe_preserves_true_open_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / ".friday-retention-q-v1-test"
    recovery = _recovery_binding(
        accepted_root_plan_sha256="d" * 64,
        reviewed_candidate_set_sha256="e" * 64,
        boot="1",
    )
    projection = {
        "authority_sha256": recovery["maintenance_authority_sha256"],
        "boot_id_sha256": recovery["maintenance_boot_id_sha256"],
        "executing_initrd_sha256": recovery["executing_initrd_sha256"],
        "namespace_epoch_sha256": recovery["maintenance_namespace_epoch_sha256"],
        "premount_receipt_sha256": recovery["maintenance_premount_receipt_sha256"],
        "process_epoch_sha256": recovery["maintenance_process_epoch_sha256"],
        "request_file_sha256": recovery["request_file_sha256"],
        "target_receipt_sha256": "f" * 64,
        "transaction_id": recovery["transaction_id"],
    }

    class MaintenanceAuthority:
        def projection(self) -> dict[str, Any]:
            return dict(projection)

    snapshots = iter(
        (
            SimpleNamespace(open_paths=()),
            SimpleNamespace(open_paths=(target,)),
        )
    )
    maintenance_calls: list[tuple[Path, ...]] = []

    def maintenance_inventory(*, target_paths: tuple[Path, ...]) -> Any:
        maintenance_calls.append(target_paths)
        return SimpleNamespace(
            authority=MaintenanceAuthority(),
            snapshot=next(snapshots),
        )

    monkeypatch.setattr(
        retention,
        "_build_maintenance_open_inventory",
        maintenance_inventory,
    )
    monkeypatch.setattr(
        retention,
        "_validated_maintenance_effect_authority",
        lambda value, **_kwargs: value,
    )
    monkeypatch.setattr(
        retention,
        "build_complete_open_inventory",
        lambda **_kwargs: pytest.fail("maintenance must not use ordinary observer"),
    )
    guards: list[str] = []

    clear = operator._post_quarantine_open_inventory(  # noqa: SLF001
        (target,),
        maintenance_recovery=recovery,
        guard=lambda: guards.append("checked"),
    )
    opened = operator._post_quarantine_open_inventory(  # noqa: SLF001
        (target,),
        maintenance_recovery=recovery,
        guard=lambda: guards.append("checked"),
    )

    assert clear.open_paths == ()
    assert opened.open_paths == (target,)
    assert maintenance_calls == [(target,), (target,)]
    assert guards == ["checked", "checked"]
