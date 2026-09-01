from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import release_artifact_proc_probe as real_proc_probe
from tools import release_artifact_retention as real_retention
from tools import release_artifact_retention_maintenance as maintenance
from tools import release_artifact_retention_operator as real_operator


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@pytest.fixture(autouse=True)
def _bound_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance, "proc_probe", real_proc_probe)
    monkeypatch.setattr(maintenance, "retention", real_retention)
    monkeypatch.setattr(maintenance, "operator", real_operator)


def _profile(tmp_path: Path) -> dict[str, Any]:
    return {
        "cmdline_sha256": "1" * 64,
        "io_uring_disabled": 0,
        "kernel_config_path": str(tmp_path / "config"),
        "kernel_config_sha256": "2" * 64,
        "kernel_image_path": str(tmp_path / "kernel"),
        "kernel_image_sha256": "3" * 64,
        "kernel_release": "test-kernel",
        "kernel_version_sha256": "4" * 64,
        "ordinary_initrd_path": str(tmp_path / "initrd"),
        "ordinary_initrd_sha256": "5" * 64,
        "root_device_id": "8:1",
        "root_filesystem_uuid": "11111111-1111-1111-1111-111111111111",
    }


def _request(tmp_path: Path) -> dict[str, Any]:
    transaction_id = "a" * 64
    activation = tmp_path / "state/activation.json"
    maintenance_dir = activation.parent / "release-artifact-retention-maintenance.v1"
    reviewed = [{"path": "/reviewed", "portable_inventory_sha256": "6" * 64}]
    profile = _profile(tmp_path)
    core: dict[str, Any] = {
        "candidate_count": len(reviewed),
        "candidate_set_sha256": hashlib.sha256(_canonical(reviewed)).hexdigest(),
        "completion_output_path": str(maintenance_dir / f"completion-{transaction_id}.json"),
        "controller_sha256": "7" * 64,
        "inputs": {
            "activation_journal": str(activation),
            "backup_inventory_roots": [],
            "backup_root": str(tmp_path / "backups"),
            "canonical_evidence_roots": [],
            "inventory_roots": [str(tmp_path / "releases")],
            "reviewed_scratch_targets": [],
            "unit_journal": str(tmp_path / "state/unit.json"),
        },
        "installed_controller_path": str(maintenance.INSTALLED_CONTROLLER_PATH),
        "maintenance_cmdline_sha256": "8" * 64,
        "ordinary_profile": profile,
        "ordinary_profile_sha256": hashlib.sha256(_canonical(profile)).hexdigest(),
        "owner_uid": os.getuid(),
        "plan_output_path": str(maintenance_dir / f"plan-{transaction_id}.json"),
        "result_output_path": str(maintenance_dir / f"result-{transaction_id}.json"),
        "reviewed_candidates": reviewed,
        "scope_seed_plan_sha256": "9" * 64,
        "schema": maintenance.REQUEST_SCHEMA,
        "toolchain_manifest_sha256": "b" * 64,
        "toolchain_root": str(tmp_path / "toolchain"),
        "transaction_id": transaction_id,
    }
    return {**core, "request_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _binding(
    request: dict[str, Any],
    request_raw: bytes,
    *,
    boot: str = "c",
    authority: str = "d",
) -> maintenance.CurrentMaintenanceBinding:
    return maintenance.CurrentMaintenanceBinding(
        transaction_id=str(request["transaction_id"]),
        request_file_sha256=hashlib.sha256(request_raw).hexdigest(),
        request_sha256=str(request["request_sha256"]),
        executing_initrd_sha256="e" * 64,
        maintenance_boot_id_sha256=boot * 64,
        maintenance_authority_sha256=authority * 64,
        maintenance_premount_receipt_sha256="f" * 64,
        maintenance_namespace_epoch_sha256="1" * 64,
        maintenance_process_epoch_sha256="2" * 64,
        maintenance_target_receipt_sha256="3" * 64,
    )


def test_controller_is_separate_from_the_existing_sealed_toolchain() -> None:
    assert "release_artifact_retention_maintenance.py" not in (
        maintenance._SEALED_TOOLCHAIN_MODULES  # noqa: SLF001
    )
    assert (
        Path("/usr/libexec/friday/release_artifact_retention_maintenance.py")
        == maintenance.INSTALLED_CONTROLLER_PATH
    )
    assert maintenance.EXECUTION_ADMISSION_SCHEMA.endswith(".v2")
    assert maintenance.MAINTENANCE_RECOVERY_SCHEMA.endswith(".v1")


@pytest.mark.parametrize(
    "cmdline",
    (
        b"root=/dev/sda1",
        b"root=UUID=11111111-1111-1111-1111-111111111111 root=UUID=22222222-2222-2222-2222-222222222222",
        b"root=UUID=11111111-1111-1111-1111-11111111111A",
    ),
)
def test_ordinary_root_uuid_requires_one_canonical_uuid_token(cmdline: bytes) -> None:
    with pytest.raises(maintenance.MaintenanceError, match="^ordinary_profile_invalid$"):
        maintenance._root_filesystem_uuid(  # noqa: SLF001
            cmdline,
            code="ordinary_profile_invalid",
        )


def test_profile_file_hashing_streams_without_materializing_whole_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "large-profile-artifact"
    raw = b"profile-block" * (2 << 17)
    path.write_bytes(raw)
    path.chmod(0o600)
    monkeypatch.setattr(
        maintenance,
        "_stable_file",
        lambda *_args, **_kwargs: pytest.fail("profile hashing must stream"),
    )

    assert (
        maintenance._file_sha256(path, code="profile_invalid")  # noqa: SLF001
        == hashlib.sha256(raw).hexdigest()
    )
    alias = tmp_path / "profile-alias"
    alias.symlink_to(path)
    with pytest.raises(maintenance.MaintenanceError, match="^profile_invalid$"):
        maintenance._file_sha256(alias, code="profile_invalid")  # noqa: SLF001


def test_installed_controller_contract_is_fixed_root_owned_0555(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def digest(path: Path, **kwargs: object) -> str:
        observed.update(path=path, **kwargs)
        return "7" * 64

    monkeypatch.setattr(
        maintenance,
        "__file__",
        str(maintenance.INSTALLED_CONTROLLER_PATH),
    )
    monkeypatch.setattr(maintenance, "_file_sha256", digest)

    maintenance._authenticate_installed_controller(  # noqa: SLF001
        {
            "controller_sha256": "7" * 64,
            "installed_controller_path": str(maintenance.INSTALLED_CONTROLLER_PATH),
        }
    )

    assert observed == {
        "allowed_modes": frozenset({0o555}),
        "code": "maintenance_controller_invalid",
        "expected_uid": 0,
        "path": maintenance.INSTALLED_CONTROLLER_PATH,
    }


def test_review_request_is_canonical_portable_and_binds_the_separate_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    activation = state / "activation.json"
    unit = state / "unit.json"
    toolchain = tmp_path / "toolchain"
    output = state / "review.json"
    candidates = [{"path": "/exact", "portable_inventory_sha256": "3" * 64}]
    profile = _profile(tmp_path)
    monkeypatch.setattr(maintenance, "_bind_toolchain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        maintenance,
        "_scope_seed",
        lambda **_kwargs: (
            {"classification_status": "scope_seed", "plan_sha256": "4" * 64},
            {
                "activation_journal": str(activation),
                "backup_inventory_roots": [],
                "backup_root": str(tmp_path / "backups"),
                "canonical_evidence_roots": [],
                "inventory_roots": [str(tmp_path / "releases")],
                "reviewed_scratch_targets": [],
                "unit_journal": str(unit),
            },
        ),
    )
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan: candidates,
    )
    monkeypatch.setattr(maintenance, "_ordinary_profile", lambda **_kwargs: profile)
    monkeypatch.setattr(
        maintenance,
        "_proc_bytes",
        lambda *_args, **_kwargs: b"root=UUID=ordinary\n",
    )

    def digest(path: Path, **_kwargs: object) -> str:
        return "5" * 64 if path.name == "manifest.json" else "6" * 64

    monkeypatch.setattr(maintenance, "_file_sha256", digest)

    result = maintenance.create_review_request(
        activation_journal=activation,
        unit_journal=unit,
        kernel_image=tmp_path / "kernel",
        kernel_config=tmp_path / "config",
        ordinary_initrd=tmp_path / "initrd",
        toolchain_root=toolchain,
        output=output,
    )

    assert output.read_bytes() == _canonical(result) + b"\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert result["controller_sha256"] == "6" * 64
    assert result["installed_controller_path"] == str(maintenance.INSTALLED_CONTROLLER_PATH)
    assert result["toolchain_manifest_sha256"] == "5" * 64
    assert "toolchain_controller_sha256" not in result
    assert "boot_id_sha256" not in result["ordinary_profile"]
    assert result["reviewed_candidates"] == candidates
    assert result["candidate_set_sha256"] == hashlib.sha256(_canonical(candidates)).hexdigest()


def test_portable_candidate_identity_changes_only_boot_local_mount_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = (
        (
            ".",
            11,
            22,
            stat.S_IFDIR | 0o700,
            2,
            os.getuid(),
            0,
            1,
            2,
            101,
            os.getgid(),
            0,
        ),
    )
    exact = {
        "allocated_bytes": 0,
        "collection": "targets",
        "device": 11,
        "entry_count": 1,
        "filesystem_magic": real_retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "identity": {"nested_device": 919, "nested_mount_id": 929},
        "inode": 22,
        "inventory_sha256": hashlib.sha256(_canonical(records)).hexdigest(),
        "mode": stat.S_IFDIR | 0o700,
        "mount_id": 101,
        "nlink": 2,
        "path": str(tmp_path / "candidate"),
        "recursive_bytes": 0,
        "type": "directory",
        "writable_authority_sha256": "a" * 64,
    }
    snapshots = [SimpleNamespace(records=records)]
    monkeypatch.setattr(
        real_operator,
        "_reviewed_candidate_identities",
        lambda _plan: (dict(exact),),
    )
    monkeypatch.setattr(real_retention, "_snapshot", lambda _path: snapshots[0])

    before_reboot = maintenance._candidate_review_identities({})  # noqa: SLF001
    remounted = tuple(
        tuple(44 if index == 1 else 202 if index == 9 else value for index, value in enumerate(row))
        for row in records
    )
    exact["device"] = 44
    exact["mount_id"] = 202
    exact["inventory_sha256"] = hashlib.sha256(_canonical(remounted)).hexdigest()
    snapshots[0] = SimpleNamespace(records=remounted)

    assert maintenance._candidate_review_identities({}) == before_reboot  # noqa: SLF001
    assert before_reboot[0]["identity"] == {
        "nested_device": 919,
        "nested_mount_id": 929,
    }


def test_current_review_inputs_rebinds_scratch_only_through_full_portable_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scratch"
    mode = stat.S_IFDIR | 0o700
    old_record = (".", 101, 31, mode, 2, os.getuid(), 0, 0, 0, 201, os.getgid(), 0)
    current_record = (".", 102, 31, mode, 2, os.getuid(), 0, 0, 0, 202, os.getgid(), 0)
    current_snapshot = SimpleNamespace(records=(current_record,))
    current_exact_sha256 = hashlib.sha256(_canonical(current_snapshot.records)).hexdigest()
    portable_sha256 = real_operator._portable_inventory_sha256(  # noqa: SLF001
        current_snapshot
    )
    accepted = {
        "allocated_bytes": 0,
        "collection": "targets",
        "entry_count": 1,
        "filesystem_magic": real_retention._EXT4_SUPER_MAGIC,  # noqa: SLF001
        "identity": {
            "allocated_bytes": 0,
            "contour": real_retention._SCRATCH_CONTOUR,  # noqa: SLF001
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
        "type": "directory",
        "writable_authority_sha256": "a" * 64,
    }
    inputs = {
        "activation_journal": tmp_path / "activation.json",
        "reviewed_scratch_targets": (
            real_retention.ReviewedScratchTarget(
                path=path,
                inventory_sha256=hashlib.sha256(_canonical((old_record,))).hexdigest(),
            ),
        ),
        "unit_journal": tmp_path / "unit.json",
    }
    observed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(real_retention, "_snapshot", lambda _path: current_snapshot)

    def scope_seed(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        observed.append(tuple(kwargs["reviewed_scratch_targets"]))
        return {"classification_status": "scope_seed"}, {}

    monkeypatch.setattr(maintenance, "_scope_seed", scope_seed)
    projected = [dict(accepted)]
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan, **_kwargs: projected,
    )

    current, _seed = maintenance._current_review_inputs(  # noqa: SLF001
        inputs,
        reviewed_candidates=[accepted],
    )

    assert current["reviewed_scratch_targets"][0].inventory_sha256 == current_exact_sha256
    assert observed[0][0].inventory_sha256 == current_exact_sha256

    projected[0] = {**accepted, "inode": 32}
    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_review_changed$",
    ):
        maintenance._current_review_inputs(  # noqa: SLF001
            inputs,
            reviewed_candidates=[accepted],
        )


def test_execution_admission_is_boot_neutral_and_create_only(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    request = _request(tmp_path)
    path = tmp_path / "admission.json"
    request_file_sha256 = "1" * 64
    first = maintenance._execution_admission(  # noqa: SLF001
        path=path,
        request=request,
        request_file_sha256=request_file_sha256,
        executing_initrd_sha256="2" * 64,
        plan_sha256="3" * 64,
        create=True,
    )

    assert set(first) == {
        "admission_sha256",
        "executing_initrd_sha256",
        "plan_sha256",
        "request_file_sha256",
        "request_sha256",
        "reviewed_candidate_set_sha256",
        "schema",
        "transaction_id",
    }
    assert not any("boot" in name or "authority" in name for name in first)
    assert (
        maintenance._execution_admission(  # noqa: SLF001
            path=path,
            request=request,
            request_file_sha256=request_file_sha256,
            executing_initrd_sha256="2" * 64,
            plan_sha256="3" * 64,
            create=False,
        )
        == first
    )


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("request_file_sha256", "4" * 64),
        ("executing_initrd_sha256", "5" * 64),
        ("plan_sha256", "6" * 64),
        ("request_sha256", "7" * 64),
        ("candidate_set_sha256", "8" * 64),
        ("transaction_id", "9" * 64),
    ),
)
def test_execution_admission_rejects_stable_identity_drift(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    tmp_path.chmod(0o700)
    request = _request(tmp_path)
    path = tmp_path / "admission.json"
    values = {
        "request_file_sha256": "1" * 64,
        "executing_initrd_sha256": "2" * 64,
        "plan_sha256": "3" * 64,
    }
    maintenance._execution_admission(  # noqa: SLF001
        path=path,
        request=request,
        create=True,
        **values,
    )
    if field in values:
        values[field] = changed
    else:
        request[field] = changed

    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_execution_admission_invalid$",
    ):
        maintenance._execution_admission(  # noqa: SLF001
            path=path,
            request=request,
            create=False,
            **values,
        )


def test_recovery_mapping_combines_stable_admission_with_fresh_boot_only(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    first_binding = _binding(request, request_raw, boot="3", authority="4")
    admission = maintenance._admission_value(  # noqa: SLF001
        request=request,
        request_file_sha256=first_binding.request_file_sha256,
        executing_initrd_sha256=first_binding.executing_initrd_sha256,
        plan_sha256="5" * 64,
    )

    first = maintenance._maintenance_recovery(  # noqa: SLF001
        admission=admission,
        binding=first_binding,
    )
    second_binding = replace(
        first_binding,
        maintenance_boot_id_sha256="6" * 64,
        maintenance_authority_sha256="7" * 64,
        maintenance_premount_receipt_sha256="8" * 64,
        maintenance_namespace_epoch_sha256="9" * 64,
        maintenance_process_epoch_sha256="a" * 64,
        maintenance_target_receipt_sha256="b" * 64,
    )
    second = maintenance._maintenance_recovery(  # noqa: SLF001
        admission=admission,
        binding=second_binding,
    )

    stable = {
        "executing_initrd_sha256",
        "plan_sha256",
        "request_file_sha256",
        "request_sha256",
        "reviewed_candidate_set_sha256",
        "transaction_id",
    }
    assert {name: first[name] for name in stable} == {name: second[name] for name in stable}
    assert first["maintenance_boot_id_sha256"] != second["maintenance_boot_id_sha256"]
    assert first["maintenance_authority_sha256"] != second["maintenance_authority_sha256"]
    assert first["recovery_sha256"] != second["recovery_sha256"]
    assert (
        first["recovery_sha256"]
        == hashlib.sha256(
            _canonical({name: value for name, value in first.items() if name != "recovery_sha256"})
        ).hexdigest()
    )
    assert set(first) == stable | {
        "maintenance_authority_sha256",
        "maintenance_boot_id_sha256",
        "maintenance_namespace_epoch_sha256",
        "maintenance_premount_receipt_sha256",
        "maintenance_process_epoch_sha256",
        "maintenance_target_receipt_sha256",
        "recovery_sha256",
        "schema",
    }


@pytest.mark.parametrize(
    "drift",
    ("transaction", "request_file", "request", "initrd"),
)
def test_recovery_mapping_rejects_every_stable_binding_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    binding = _binding(request, request_raw)
    admission = maintenance._admission_value(  # noqa: SLF001
        request=request,
        request_file_sha256=binding.request_file_sha256,
        executing_initrd_sha256=binding.executing_initrd_sha256,
        plan_sha256="3" * 64,
    )
    changed = replace(
        binding,
        **{
            {
                "transaction": "transaction_id",
                "request_file": "request_file_sha256",
                "request": "request_sha256",
                "initrd": "executing_initrd_sha256",
            }[drift]: "0" * 64,
        },
    )

    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_execution_admission_invalid$",
    ):
        maintenance._maintenance_recovery(  # noqa: SLF001
            admission=admission,
            binding=changed,
        )


def _effect_authority(
    request: dict[str, Any],
    request_raw: bytes,
    *,
    boot_id_sha256: str,
) -> real_retention._MaintenanceEffectAuthority:  # noqa: SLF001
    return real_retention._MaintenanceEffectAuthority(  # noqa: SLF001
        authority_sha256="1" * 64,
        boot_id_sha256=boot_id_sha256,
        executing_initrd_sha256="2" * 64,
        namespace_epoch_sha256="3" * 64,
        premount_receipt_sha256="4" * 64,
        process_epoch_sha256="5" * 64,
        request_file_sha256=hashlib.sha256(request_raw).hexdigest(),
        target_receipt_sha256="7" * 64,
        transaction_id=request["transaction_id"],
        _seal=real_retention._MAINTENANCE_AUTHORITY_SEAL,  # noqa: SLF001
    )


def _maintenance_target_receipt(
    request: dict[str, Any],
    request_raw: bytes,
) -> dict[str, Any]:
    return {
        "authority_file_sha256": "1" * 64,
        "boot_id_sha256": "6" * 64,
        "executing_initrd_sha256": "2" * 64,
        "namespace_epoch_sha256": "3" * 64,
        "premount_receipt_sha256": "4" * 64,
        "process_epoch_sha256": "5" * 64,
        "receipt_sha256": "8" * 64,
        "referenced_target_ids": [],
        "request_file_sha256": hashlib.sha256(request_raw).hexdigest(),
        "schema": real_proc_probe.MAINTENANCE_TARGET_RECEIPT_SCHEMA,
        "transaction_id": request["transaction_id"],
    }


def test_maintenance_adapter_rebuilds_both_target_indexes_and_carries_exact_output_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request_raw)
    receipt = _maintenance_target_receipt(request, request_raw)
    target_receipt_sha256 = "7" * 64
    calls: list[str] = []
    exact = real_retention._require_exact_target_index  # noqa: SLF001

    def require_exact(index: Any, seeds: Any, *, code: str) -> None:
        calls.append(code)
        exact(index, seeds, code=code)

    monkeypatch.setattr(
        real_retention,
        "_run_maintenance_target_probe",
        lambda _index: (receipt, "9" * 64, target_receipt_sha256),
    )
    monkeypatch.setattr(real_retention, "_require_exact_target_index", require_exact)

    authority = real_retention.build_maintenance_effect_authority(
        target_path=request_path,
    )
    inventory = real_retention._build_maintenance_open_inventory(  # noqa: SLF001
        target_paths=(request_path,),
    )

    assert authority.target_receipt_sha256 == target_receipt_sha256
    assert isinstance(
        inventory,
        real_retention._MaintenanceOpenInventorySnapshot,  # noqa: SLF001
    )
    assert inventory.authority.target_receipt_sha256 == target_receipt_sha256
    assert calls == [
        "maintenance_effect_authority_invalid",
        "open_state_ambiguous",
    ]


def test_exact_target_index_rebuild_rejects_post_probe_tree_drift(tmp_path: Path) -> None:
    target = tmp_path / "candidate"
    target.mkdir()
    body = target / "body"
    body.write_bytes(b"before")
    index, seeds = real_retention._target_probe_index((target,))  # noqa: SLF001

    body.write_bytes(b"after-with-different-size")

    with pytest.raises(real_retention.RetentionPlanError, match="^open_state_ambiguous$"):
        real_retention._require_exact_target_index(  # noqa: SLF001
            index,
            seeds,
            code="open_state_ambiguous",
        )


def test_final_eligible_recheck_includes_maintenance_target_index() -> None:
    assert real_retention._MAINTENANCE_OPEN_SOURCE in (  # noqa: SLF001
        real_retention._FINAL_TARGET_INDEX_RECHECK_SOURCES  # noqa: SLF001
    )


def test_current_binding_requires_a_fresh_live_boot_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request_raw)
    boot_id_sha256 = "6" * 64
    authority = _effect_authority(
        request,
        request_raw,
        boot_id_sha256=boot_id_sha256,
    )
    monkeypatch.setattr(
        real_retention,
        "build_maintenance_effect_authority",
        lambda **_kwargs: authority,
    )
    monkeypatch.setattr(
        maintenance,
        "_boot_id_sha256",
        lambda **_kwargs: boot_id_sha256,
    )

    result = maintenance._current_maintenance_binding(  # noqa: SLF001
        request_path=request_path,
        request=request,
        request_raw=request_raw,
    )

    assert result.maintenance_boot_id_sha256 == boot_id_sha256
    assert result.request_file_sha256 == hashlib.sha256(request_raw).hexdigest()
    assert result.executing_initrd_sha256 == "2" * 64


@pytest.mark.parametrize(
    "drift",
    (
        "missing_epoch",
        "missing_target_receipt",
        "stale_boot",
        "unsealed",
        "wrong_request",
        "wrong_authority",
    ),
)
def test_current_binding_rejects_malformed_or_stale_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request_raw)
    boot_id_sha256 = "6" * 64
    authority = _effect_authority(
        request,
        request_raw,
        boot_id_sha256=boot_id_sha256,
    )
    if drift == "missing_epoch":
        values = vars(authority).copy()
        del values["namespace_epoch_sha256"]
        authority = SimpleNamespace(**values)
    elif drift == "missing_target_receipt":
        values = vars(authority).copy()
        del values["target_receipt_sha256"]
        authority = SimpleNamespace(**values)
    elif drift == "stale_boot":
        authority = replace(authority, boot_id_sha256="8" * 64)
    elif drift == "unsealed":
        authority = replace(authority, _seal=None)
    elif drift == "wrong_request":
        authority = replace(authority, request_file_sha256="9" * 64)
    else:
        authority = replace(authority, authority_sha256="ordinary_no_delete")
    monkeypatch.setattr(
        real_retention,
        "build_maintenance_effect_authority",
        lambda **_kwargs: authority,
    )
    monkeypatch.setattr(
        maintenance,
        "_boot_id_sha256",
        lambda **_kwargs: boot_id_sha256,
    )

    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_boot_authority_invalid$",
    ):
        maintenance._current_maintenance_binding(  # noqa: SLF001
            request_path=request_path,
            request=request,
            request_raw=request_raw,
        )


def _prepare_existing_plan(
    tmp_path: Path,
    request: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    plan_path = Path(str(request["plan_output_path"]))
    plan_path.parent.mkdir(mode=0o700, parents=True)
    plan = {"plan_sha256": "b" * 64}
    plan_path.write_bytes(_canonical(plan) + b"\n")
    plan_path.chmod(0o600)
    return plan_path, plan


def test_execute_reuses_admission_across_boots_but_refreshes_operator_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    plan_path, plan = _prepare_existing_plan(tmp_path, request)
    activation = Path(str(request["inputs"]["activation_journal"]))
    reviewed = request["reviewed_candidates"]
    bindings = iter(
        (
            _binding(request, request_raw, boot="c", authority="d"),
            _binding(request, request_raw, boot="e", authority="f"),
        )
    )
    consumed: list[dict[str, Any]] = []
    review_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(
        maintenance,
        "_inputs",
        lambda _value: {"activation_journal": activation},
    )
    monkeypatch.setattr(
        maintenance,
        "_current_maintenance_binding",
        lambda **_kwargs: next(bindings),
    )
    monkeypatch.setattr(real_operator, "_read_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan, **kwargs: review_calls.append(dict(kwargs)) or reviewed,
    )
    monkeypatch.setattr(
        maintenance,
        "_current_review_inputs",
        lambda value, **_kwargs: (dict(value), {}),
    )
    monkeypatch.setattr(
        maintenance,
        "_stored_plan_request_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(maintenance, "MAX_CONVERGENCE_PASSES", 1)

    def converge(**kwargs: Any) -> dict[str, Any]:
        recovery = dict(kwargs["_maintenance_recovery"])
        consumed.append(recovery)
        return {
            "maintenance_recovery_sha256": recovery["recovery_sha256"],
            "status": "in_progress",
        }

    monkeypatch.setattr(real_operator, "converge_retention_cycle", converge)
    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_transaction_in_progress$",
    ):
        maintenance.execute_request(
            request_path=tmp_path / "request.json",
            expected_request_sha256=str(request["request_sha256"]),
        )
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan, **_kwargs: pytest.fail(
            "an admitted restart must not re-inventory already-effected paths"
        ),
    )
    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_transaction_in_progress$",
    ):
        maintenance.execute_request(
            request_path=tmp_path / "request.json",
            expected_request_sha256=str(request["request_sha256"]),
        )

    admission_path = maintenance._execution_admission_path(plan_path)  # noqa: SLF001
    admission = json.loads(admission_path.read_text(encoding="ascii"))
    assert "maintenance_boot_id_sha256" not in admission
    assert consumed[0]["maintenance_boot_id_sha256"] == "c" * 64
    assert consumed[1]["maintenance_boot_id_sha256"] == "e" * 64
    assert consumed[0]["plan_sha256"] == consumed[1]["plan_sha256"] == plan["plan_sha256"]
    assert consumed[0]["recovery_sha256"] != consumed[1]["recovery_sha256"]
    assert review_calls == [{"allow_boot_rebind": True}]


def test_execute_records_the_boot_that_actually_converged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    _plan_path, plan = _prepare_existing_plan(tmp_path, request)
    activation = Path(str(request["inputs"]["activation_journal"]))
    binding = _binding(request, request_raw, boot="c", authority="d")
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(
        maintenance,
        "_inputs",
        lambda _value: {"activation_journal": activation},
    )
    monkeypatch.setattr(
        maintenance,
        "_current_maintenance_binding",
        lambda **_kwargs: binding,
    )
    monkeypatch.setattr(real_operator, "_read_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan, **_kwargs: request["reviewed_candidates"],
    )
    monkeypatch.setattr(
        maintenance,
        "_current_review_inputs",
        lambda value, **_kwargs: (dict(value), {}),
    )
    monkeypatch.setattr(
        maintenance,
        "_stored_plan_request_binding",
        lambda *_args, **_kwargs: None,
    )

    recorded_recovery: dict[str, Any] = {}

    def converge(**kwargs: Any) -> dict[str, Any]:
        recovery = kwargs["_maintenance_recovery"]
        recorded_recovery.update(recovery)
        return {
            "receipt_sha256": "e" * 64,
            "status": "converged",
        }

    monkeypatch.setattr(real_operator, "converge_retention_cycle", converge)
    monkeypatch.setattr(
        real_operator,
        "_current_activation_receipt_path",
        lambda _state_dir: tmp_path / "activation-receipt.json",
    )
    monkeypatch.setattr(
        real_operator,
        "_maintenance_convergence_authority_for_state",
        lambda **_kwargs: {"maintenance_recovery": dict(recorded_recovery)},
    )

    result = maintenance.execute_request(
        request_path=tmp_path / "request.json",
        expected_request_sha256=str(request["request_sha256"]),
    )

    assert result["converged_maintenance_boot_id_sha256"] == "c" * 64
    assert result["executing_initrd_sha256"] == binding.executing_initrd_sha256
    assert result["convergence_receipt_sha256"] == "e" * 64
    assert Path(str(request["result_output_path"])).read_bytes() == _canonical(result) + b"\n"


def test_execute_rejects_an_operator_that_does_not_consume_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    _plan_path, plan = _prepare_existing_plan(tmp_path, request)
    activation = Path(str(request["inputs"]["activation_journal"]))
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(
        maintenance,
        "_inputs",
        lambda _value: {"activation_journal": activation},
    )
    monkeypatch.setattr(
        maintenance,
        "_current_maintenance_binding",
        lambda **_kwargs: _binding(request, request_raw),
    )
    monkeypatch.setattr(real_operator, "_read_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan, **_kwargs: request["reviewed_candidates"],
    )
    monkeypatch.setattr(
        maintenance,
        "_current_review_inputs",
        lambda value, **_kwargs: (dict(value), {}),
    )
    monkeypatch.setattr(
        maintenance,
        "_stored_plan_request_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        real_operator,
        "converge_retention_cycle",
        lambda **_kwargs: {"status": "in_progress"},
    )

    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_recovery_not_consumed$",
    ):
        maintenance.execute_request(
            request_path=tmp_path / "request.json",
            expected_request_sha256=str(request["request_sha256"]),
        )


def test_execute_reuses_an_authenticated_published_result_without_new_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    _plan_path, plan = _prepare_existing_plan(tmp_path, request)
    result_path = Path(str(request["result_output_path"]))
    result_path.write_bytes(b"published-result\n")
    result_path.chmod(0o400)
    result = {
        "convergence_receipt_sha256": "c" * 64,
        "maintenance_recovery_sha256": "d" * 64,
        "plan_sha256": plan["plan_sha256"],
        "result_sha256": "e" * 64,
    }
    activation = Path(str(request["inputs"]["activation_journal"]))
    authenticated: list[dict[str, Any]] = []
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(
        maintenance,
        "_inputs",
        lambda _value: {"activation_journal": activation},
    )
    monkeypatch.setattr(real_operator, "_read_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        maintenance,
        "_candidate_review_identities",
        lambda _plan, **_kwargs: request["reviewed_candidates"],
    )
    monkeypatch.setattr(
        maintenance,
        "_current_review_inputs",
        lambda value, **_kwargs: (dict(value), {}),
    )
    monkeypatch.setattr(
        maintenance,
        "_stored_plan_request_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        maintenance,
        "_validate_result",
        lambda *_args, **_kwargs: dict(result),
    )
    monkeypatch.setattr(
        maintenance,
        "_validate_result_convergence",
        lambda value, **_kwargs: authenticated.append(dict(value)) or {},
    )
    monkeypatch.setattr(
        maintenance,
        "_current_maintenance_binding",
        lambda **_kwargs: pytest.fail("a completed transaction cannot reacquire effect authority"),
    )

    assert (
        maintenance.execute_request(
            request_path=tmp_path / "request.json",
            expected_request_sha256=str(request["request_sha256"]),
        )
        == result
    )
    assert authenticated == [result]


@pytest.mark.parametrize(
    "drift",
    ("exact", "accepted_root", "receipt", "recovery"),
)
def test_result_reauthenticates_the_exact_durable_terminal_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    activation = tmp_path / "state/activation.json"
    result = {
        "convergence_receipt_sha256": "1" * 64,
        "maintenance_recovery_sha256": "2" * 64,
        "plan_sha256": "3" * 64,
    }
    convergence = {
        "accepted_root_plan_sha256": result["plan_sha256"],
        "receipt_sha256": result["convergence_receipt_sha256"],
        "status": "converged",
    }
    changed = {
        "accepted_root": ("accepted_root_plan_sha256", "4" * 64),
        "receipt": ("receipt_sha256", "5" * 64),
    }
    if drift not in {"exact", "recovery"}:
        name, value = changed[drift]
        convergence[name] = value
    observed: dict[str, Any] = {}
    monkeypatch.setattr(
        real_operator,
        "_current_activation_receipt_path",
        lambda state_dir: state_dir / "current-activation.json",
    )

    def converged(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        if drift == "recovery":
            raise real_operator.RetentionApplyError("retention_maintenance_recovery_invalid")
        return convergence

    monkeypatch.setattr(real_operator, "_converged_receipt_for_state", converged)

    if drift == "exact":
        assert (
            maintenance._validate_result_convergence(  # noqa: SLF001
                result,
                activation_journal=activation,
            )
            == convergence
        )
    else:
        with pytest.raises(
            maintenance.MaintenanceError,
            match="^maintenance_result_invalid$",
        ):
            maintenance._validate_result_convergence(  # noqa: SLF001
                result,
                activation_journal=activation,
            )
    assert observed == {
        "activation_receipt": activation.parent / "current-activation.json",
        "maintenance_recovery_sha256": result["maintenance_recovery_sha256"],
        "state_dir": activation.parent,
    }


@pytest.mark.parametrize("uuid_mapping_matches", (True, False))
def test_completion_rebinds_review_dev_t_to_the_same_unique_uuid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uuid_mapping_matches: bool,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    profile = request["ordinary_profile"]
    result = {
        "converged_maintenance_boot_id_sha256": "a" * 64,
        "result_sha256": "b" * 64,
    }
    ordinary_boot = "c" * 64
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(maintenance.os, "geteuid", lambda: request["owner_uid"])
    monkeypatch.setattr(
        maintenance,
        "_validate_result",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        maintenance,
        "_validate_result_convergence",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        maintenance,
        "_inputs",
        lambda _value: {"activation_journal": Path(str(request["inputs"]["activation_journal"]))},
    )
    monkeypatch.setattr(
        maintenance,
        "_validate_result_convergence",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        maintenance,
        "proc_probe",
        SimpleNamespace(MAINTENANCE_AUTHORITY_PATH=tmp_path / "absent"),
    )
    monkeypatch.setattr(
        maintenance,
        "_boot_id_sha256",
        lambda **_kwargs: ordinary_boot,
    )
    ordinary_cmdline = f"root=UUID={profile['root_filesystem_uuid']}".encode("ascii")
    monkeypatch.setattr(
        maintenance,
        "_proc_bytes",
        lambda path, **_kwargs: {
            Path("/proc/cmdline"): ordinary_cmdline,
            Path("/proc/sys/kernel/io_uring_disabled"): b"0\n",
        }[path],
    )
    profile["cmdline_sha256"] = hashlib.sha256(ordinary_cmdline).hexdigest()
    monkeypatch.setattr(
        maintenance.os,
        "uname",
        lambda: SimpleNamespace(release=profile["kernel_release"], version="ordinary"),
    )
    profile["kernel_version_sha256"] = hashlib.sha256(b"ordinary").hexdigest()
    monkeypatch.setattr(
        maintenance,
        "_file_sha256",
        lambda path, **_kwargs: {
            profile["kernel_image_path"]: profile["kernel_image_sha256"],
            profile["kernel_config_path"]: profile["kernel_config_sha256"],
            profile["ordinary_initrd_path"]: profile["ordinary_initrd_sha256"],
        }[str(path)],
    )
    current_root_device_id = "259:1"
    assert current_root_device_id != profile["root_device_id"]
    monkeypatch.setattr(maintenance, "_root_device_id", lambda: current_root_device_id)
    mapped_uuids: list[tuple[str, str]] = []

    def uuid_device_id(value: str, *, code: str) -> str:
        mapped_uuids.append((value, code))
        return current_root_device_id if uuid_mapping_matches else "8:2"

    monkeypatch.setattr(maintenance, "_root_uuid_device_id", uuid_device_id)
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        maintenance,
        "_write_no_replace",
        lambda _path, value, **_kwargs: published.append(dict(value)),
    )

    if not uuid_mapping_matches:
        with pytest.raises(
            maintenance.MaintenanceError,
            match="^ordinary_profile_not_restored$",
        ):
            maintenance.finalize_request(
                request_path=tmp_path / "request",
                expected_request_sha256=str(request["request_sha256"]),
                result_path=Path(str(request["result_output_path"])),
            )
        assert published == []
        assert mapped_uuids == [(str(profile["root_filesystem_uuid"]), "ordinary_profile_not_restored")]
        return

    completion = maintenance.finalize_request(
        request_path=tmp_path / "request",
        expected_request_sha256=str(request["request_sha256"]),
        result_path=Path(str(request["result_output_path"])),
    )

    assert completion["status"] == "complete_after_ordinary_reboot"
    assert completion["ordinary_boot_id_sha256"] == ordinary_boot
    assert "review_boot_id_sha256" not in completion
    assert published == [completion]
    assert mapped_uuids == [(str(profile["root_filesystem_uuid"]), "ordinary_profile_not_restored")]


def test_completion_accepts_only_the_request_bound_result_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(maintenance.os, "geteuid", lambda: request["owner_uid"])
    monkeypatch.setattr(
        maintenance,
        "_validate_result",
        lambda *_args, **_kwargs: pytest.fail("an unbound result must not be read"),
    )

    with pytest.raises(
        maintenance.MaintenanceError,
        match="^maintenance_result_invalid$",
    ):
        maintenance.finalize_request(
            request_path=tmp_path / "request",
            expected_request_sha256=str(request["request_sha256"]),
            result_path=tmp_path / "unbound-result.json",
        )


def test_completion_rejects_the_final_maintenance_boot_even_with_matching_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request_raw = _canonical(request) + b"\n"
    final_boot = "a" * 64
    monkeypatch.setattr(
        maintenance,
        "_load_request",
        lambda *_args, **_kwargs: (request, request_raw),
    )
    monkeypatch.setattr(
        maintenance,
        "_authenticate_installed_controller",
        lambda _request: None,
    )
    monkeypatch.setattr(maintenance.os, "geteuid", lambda: request["owner_uid"])
    monkeypatch.setattr(
        maintenance,
        "_validate_result",
        lambda *_args, **_kwargs: {
            "converged_maintenance_boot_id_sha256": final_boot,
            "result_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        maintenance,
        "_validate_result_convergence",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        maintenance,
        "_inputs",
        lambda _value: {"activation_journal": Path(str(request["inputs"]["activation_journal"]))},
    )
    monkeypatch.setattr(
        maintenance,
        "_validate_result_convergence",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        maintenance,
        "proc_probe",
        SimpleNamespace(MAINTENANCE_AUTHORITY_PATH=tmp_path / "absent"),
    )
    monkeypatch.setattr(
        maintenance,
        "_boot_id_sha256",
        lambda **_kwargs: final_boot,
    )

    with pytest.raises(
        maintenance.MaintenanceError,
        match="^ordinary_profile_not_restored$",
    ):
        maintenance.finalize_request(
            request_path=tmp_path / "request",
            expected_request_sha256=str(request["request_sha256"]),
            result_path=Path(str(request["result_output_path"])),
        )
