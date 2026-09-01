from __future__ import annotations

import dataclasses
import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import release_artifact_proc_probe as probe
from tools import release_artifact_retention as retention
from tools import release_artifact_retention_maintenance_install as maintenance_install

ROOT = Path(__file__).resolve().parents[1]
BOOT = ROOT / "deploy/release-retention/maintenance-boot"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _with_receipt_digest(core: dict[str, Any]) -> dict[str, Any]:
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _premount_fixture() -> tuple[dict[str, Any], bytes]:
    boot = "1" * 64
    process = "2" * 64
    mount_namespace = "3" * 64
    mountinfo = "4" * 64
    namespace = hashlib.sha256(
        (f"friday-maintenance-namespace-v1:{mount_namespace}:{mountinfo}:{process}").encode("ascii")
    ).hexdigest()
    value = _with_receipt_digest(
        {
            "boot_id_sha256": boot,
            "cmdline_sha256": "5" * 64,
            "executing_initrd_sha256": "6" * 64,
            "io_uring_disabled": 2,
            "maintenance_cmdline_sha256": "b" * 64,
            "mount_namespace_sha256": mount_namespace,
            "mountinfo_sha256": mountinfo,
            "namespace_epoch_sha256": namespace,
            "nsfs_pins_absent": True,
            "only_pid1_userspace_task": True,
            "pid1_starttime_sha256": "7" * 64,
            "process_epoch_sha256": process,
            "request_file_sha256": "8" * 64,
            "root_device_sha256": "9" * 64,
            "root_device_unmounted": True,
            "schema": probe.MAINTENANCE_PREMOUNT_RECEIPT_SCHEMA,
            "single_mount_namespace": True,
            "transaction_id": "a" * 64,
        }
    )
    return value, _canonical(value) + b"\n"


def test_premount_receipt_is_canonical_duplicate_free_and_finite() -> None:
    value, raw = _premount_fixture()

    assert probe.parse_maintenance_premount_receipt_bytes(raw) == value
    assert probe.canonical_maintenance_premount_receipt_bytes(value) == raw

    duplicate = raw[:-2] + b',"transaction_id":"' + b"b" * 64 + b'"}\n'
    with pytest.raises(probe.ProcProbeInputError, match="premount_receipt_invalid"):
        probe.parse_maintenance_premount_receipt_bytes(duplicate)
    nonfinite = raw.replace(b'"io_uring_disabled":2', b'"io_uring_disabled":NaN')
    with pytest.raises(probe.ProcProbeInputError, match="premount_receipt_invalid"):
        probe.parse_maintenance_premount_receipt_bytes(nonfinite)
    with pytest.raises(probe.ProcProbeInputError, match="premount_receipt_invalid"):
        probe.parse_maintenance_premount_receipt_bytes(raw + b" " * probe.MAX_MAINTENANCE_AUTHORITY_BYTES)


@pytest.mark.parametrize(
    "field",
    (
        "process_epoch_sha256",
        "namespace_epoch_sha256",
        "mountinfo_sha256",
        "mount_namespace_sha256",
        "root_device_unmounted",
        "nsfs_pins_absent",
        "only_pid1_userspace_task",
        "single_mount_namespace",
        "io_uring_disabled",
    ),
)
def test_premount_receipt_rejects_proof_drift(field: str) -> None:
    value, _raw = _premount_fixture()
    changed = dict(value)
    changed[field] = False if isinstance(changed[field], bool) else "f" * 64
    if field == "io_uring_disabled":
        changed[field] = 1
    core = {name: item for name, item in changed.items() if name != "receipt_sha256"}
    changed["receipt_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()

    with pytest.raises(probe.ProcProbeInputError, match="premount_receipt_invalid"):
        probe.canonical_maintenance_premount_receipt_bytes(changed)


def test_maintenance_binding_is_frozen_and_contains_only_stable_plus_fresh_fields() -> None:
    fields = {
        "transaction_id",
        "request_file_sha256",
        "executing_initrd_sha256",
        "boot_id_sha256",
        "authority_file_sha256",
        "premount_receipt_sha256",
        "process_epoch_sha256",
        "namespace_epoch_sha256",
    }
    assert {field.name for field in dataclasses.fields(probe.MaintenancePremountAuthority)} == fields
    binding = probe.MaintenancePremountAuthority(**{name: "a" * 64 for name in fields})
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.boot_id_sha256 = "b" * 64  # type: ignore[misc]


def _live_authority_fixture() -> tuple[dict[Path, bytes], dict[str, Any]]:
    premount, premount_raw = _premount_fixture()
    transaction = str(premount["transaction_id"])
    initrd = str(premount["executing_initrd_sha256"])
    cmdline = (
        f"root=/dev/exact rd.friday.retention={transaction} "
        f"rd.friday.retention.initrd_sha256={initrd} "
        f"rdinit={probe.MAINTENANCE_RDINIT_PATH} retain_initrd "
        "sysctl.kernel.io_uring_disabled=2"
    ).encode("ascii")
    template = f"root=/dev/exact rd.friday.retention={transaction}".encode("ascii")
    premount["cmdline_sha256"] = hashlib.sha256(cmdline).hexdigest()
    premount["maintenance_cmdline_sha256"] = hashlib.sha256(template).hexdigest()
    premount_core = {name: item for name, item in premount.items() if name != "receipt_sha256"}
    premount = _with_receipt_digest(premount_core)
    premount_raw = _canonical(premount) + b"\n"
    request_path = Path("/var/lib/friday-retention/maintenance/request.json")
    authority = _with_receipt_digest(
        {
            "authority": probe.MAINTENANCE_PREMOUNT_AUTHORITY,
            "boot_id_sha256": premount["boot_id_sha256"],
            "cmdline_sha256": premount["cmdline_sha256"],
            "executing_initrd_sha256": initrd,
            "io_uring_disabled": 2,
            "maintenance_cmdline_sha256": hashlib.sha256(template).hexdigest(),
            "mount_namespace_sha256": premount["mount_namespace_sha256"],
            "namespace_epoch_sha256": premount["namespace_epoch_sha256"],
            "ordinary_workloads_started": False,
            "premount_receipt_path": str(probe.MAINTENANCE_PREMOUNT_RECEIPT_PATH),
            "premount_receipt_sha256": hashlib.sha256(premount_raw).hexdigest(),
            "process_epoch_sha256": premount["process_epoch_sha256"],
            "rdinit_path": probe.MAINTENANCE_RDINIT_PATH,
            "request_file_sha256": premount["request_file_sha256"],
            "request_path": str(request_path),
            "request_sha256": "b" * 64,
            "root_device_sha256": premount["root_device_sha256"],
            "root_device_unmounted": True,
            "schema": probe.MAINTENANCE_PREMOUNT_AUTHORITY_SCHEMA,
            "transaction_id": transaction,
        }
    )
    authority_raw = _canonical(authority) + b"\n"
    return (
        {
            probe.MAINTENANCE_AUTHORITY_PATH: authority_raw,
            probe.MAINTENANCE_PREMOUNT_RECEIPT_PATH: premount_raw,
        },
        {
            "authority": authority,
            "cmdline": cmdline,
            "pid1_starttime_sha256": premount["pid1_starttime_sha256"],
            "request_path": request_path,
        },
    )


def test_live_authority_binds_exact_initrd_boot_request_and_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files, values = _live_authority_fixture()
    authority = values["authority"]
    accumulated_paths: list[Path] = []

    def body_reader(path: Path, **_kwargs: object) -> bytes:
        accumulated_paths.append(path)
        return files[path]

    monkeypatch.setattr(
        probe,
        "_stable_maintenance_file_bytes",
        body_reader,
    )
    monkeypatch.setattr(
        probe,
        "_stable_kernel_bytes",
        lambda path, **_kwargs: {
            Path("/proc/sys/kernel/random/boot_id"): b"12345678-1234-1234-1234-123456789abc\n",
            Path("/proc/cmdline"): values["cmdline"] + b"\n",
            Path("/proc/sys/kernel/io_uring_disabled"): b"2\n",
        }[path],
    )
    authority["boot_id_sha256"] = hashlib.sha256(b"12345678-1234-1234-1234-123456789abc").hexdigest()
    premount = json.loads(files[probe.MAINTENANCE_PREMOUNT_RECEIPT_PATH])
    premount["boot_id_sha256"] = authority["boot_id_sha256"]
    premount_core = {name: item for name, item in premount.items() if name != "receipt_sha256"}
    premount = _with_receipt_digest(premount_core)
    files[probe.MAINTENANCE_PREMOUNT_RECEIPT_PATH] = _canonical(premount) + b"\n"
    authority["premount_receipt_sha256"] = hashlib.sha256(
        files[probe.MAINTENANCE_PREMOUNT_RECEIPT_PATH]
    ).hexdigest()
    authority_core = {name: item for name, item in authority.items() if name != "receipt_sha256"}
    files[probe.MAINTENANCE_AUTHORITY_PATH] = _canonical(_with_receipt_digest(authority_core)) + b"\n"
    streamed_paths: list[tuple[Path, int]] = []

    def stream_digest(path: Path, *, maximum: int, **_kwargs: object) -> str:
        streamed_paths.append((path, maximum))
        return (
            authority["executing_initrd_sha256"]
            if path == Path("/sys/firmware/initrd")
            else authority["request_file_sha256"]
        )

    monkeypatch.setattr(probe, "_stable_file_sha256", stream_digest)
    monkeypatch.setattr(
        probe,
        "_namespace_identity_sha256",
        lambda _path: authority["mount_namespace_sha256"],
    )
    monkeypatch.setattr(
        probe,
        "_pid1_starttime_sha256",
        lambda: values["pid1_starttime_sha256"],
    )

    binding = probe.maintenance_premount_authority()

    assert binding.transaction_id == authority["transaction_id"]
    assert Path("/sys/firmware/initrd") not in accumulated_paths
    assert accumulated_paths == [
        probe.MAINTENANCE_AUTHORITY_PATH,
        probe.MAINTENANCE_PREMOUNT_RECEIPT_PATH,
    ]
    assert (Path("/sys/firmware/initrd"), probe.MAX_EXECUTING_INITRD_BYTES) in streamed_paths
    assert binding.executing_initrd_sha256 == authority["executing_initrd_sha256"]
    assert (
        binding.authority_file_sha256 == hashlib.sha256(files[probe.MAINTENANCE_AUTHORITY_PATH]).hexdigest()
    )


@pytest.mark.parametrize(
    "bad_cmdline",
    (
        b"rd.friday.retention=" + b"a" * 64,
        b"rd.friday.retention=" + b"a" * 64 + b" retain_initrd retain_initrd",
        b"rd.friday.retention=" + b"a" * 64 + b" rdinit=/init",
        b"rd.friday.retention=" + b"a" * 64 + b" sysctl.kernel.io_uring_disabled=1",
    ),
)
def test_cmdline_requires_exact_rdinit_retain_initrd_initrd_digest_and_io_uring(
    bad_cmdline: bytes,
) -> None:
    with pytest.raises(probe.ProcProbeInputError, match="maintenance_authority_invalid"):
        probe._validate_maintenance_cmdline(  # noqa: SLF001
            bad_cmdline,
            transaction_id="a" * 64,
            executing_initrd_sha256="b" * 64,
            maintenance_cmdline_sha256="c" * 64,
        )


def test_cmdline_projection_preserves_all_reviewed_ordinary_tokens() -> None:
    transaction = "a" * 64
    initrd = "b" * 64
    template = f"root=/dev/exact quiet rd.friday.retention={transaction}"
    actual = (
        f"{template} rd.friday.retention.initrd_sha256={initrd} "
        f"rdinit={probe.MAINTENANCE_RDINIT_PATH} retain_initrd "
        "sysctl.kernel.io_uring_disabled=2"
    ).encode("ascii")
    expected = hashlib.sha256(template.encode("ascii")).hexdigest()

    probe._validate_maintenance_cmdline(  # noqa: SLF001
        actual,
        transaction_id=transaction,
        executing_initrd_sha256=initrd,
        maintenance_cmdline_sha256=expected,
    )
    with pytest.raises(probe.ProcProbeInputError, match="maintenance_authority_invalid"):
        probe._validate_maintenance_cmdline(  # noqa: SLF001
            actual.replace(b" quiet ", b" debug "),
            transaction_id=transaction,
            executing_initrd_sha256=initrd,
            maintenance_cmdline_sha256=expected,
        )


def _target_index() -> probe.TargetIndex:
    return probe.build_target_index(
        (
            probe.ProbeTarget(
                "artifact-one",
                (Path("/exact"),),
                (probe.ObjectKey(1, 2, stat.S_IFREG),),
            ),
        )
    )


def _target_receipt(
    index: probe.TargetIndex,
    binding: probe.MaintenancePremountAuthority,
) -> dict[str, Any]:
    core = {
        "authority": probe.MAINTENANCE_TARGET_AUTHORITY,
        "authority_file_sha256": binding.authority_file_sha256,
        "boot_id_sha256": binding.boot_id_sha256,
        "executing_initrd_sha256": binding.executing_initrd_sha256,
        "host_scope_authority_sha256": "1" * 64,
        "implementation_sha256": "2" * 64,
        "kernel_epoch_sha256": "3" * 64,
        "namespace_epoch_sha256": binding.namespace_epoch_sha256,
        "observation_sha256": "4" * 64,
        "observer_euid": 0,
        "observer_namespace_epoch_sha256": "5" * 64,
        "observer_process_epoch_sha256": "6" * 64,
        "premount_receipt_sha256": binding.premount_receipt_sha256,
        "process_epoch_sha256": binding.process_epoch_sha256,
        "referenced_target_ids": [],
        "request_file_sha256": binding.request_file_sha256,
        "schema": probe.MAINTENANCE_TARGET_RECEIPT_SCHEMA,
        "scope_identity_sha256": "7" * 64,
        "status": "clear",
        "target_count": 1,
        "target_index_sha256": index.sha256,
        "task_count": 2,
        "tgid_count": 2,
        "transaction_id": binding.transaction_id,
    }
    return _with_receipt_digest(core)


def test_target_receipt_carries_and_validates_all_eight_authority_fields() -> None:
    index = _target_index()
    names = {field.name for field in dataclasses.fields(probe.MaintenancePremountAuthority)}
    binding = probe.MaintenancePremountAuthority(**{name: "a" * 64 for name in names})
    receipt = _target_receipt(index, binding)

    assert (
        probe.canonical_maintenance_target_receipt_bytes(
            receipt,
            expected_target_index=index,
            expected_implementation_sha256="2" * 64,
            expected_host_scope_authority_sha256="1" * 64,
            expected_authority=binding,
        )
        == _canonical(receipt) + b"\n"
    )
    assert (
        probe.parse_maintenance_target_receipt_bytes(
            _canonical(receipt) + b"\n",
            expected_target_index=index,
            expected_implementation_sha256="2" * 64,
            expected_host_scope_authority_sha256="1" * 64,
            expected_authority=binding,
        )
        == receipt
    )
    duplicate = _canonical(receipt)[:-1] + b',"transaction_id":"' + b"b" * 64 + b'"}\n'
    with pytest.raises(probe.ProcProbeInputError, match="maintenance_probe_receipt_invalid"):
        probe.parse_maintenance_target_receipt_bytes(
            duplicate,
            expected_target_index=index,
            expected_implementation_sha256="2" * 64,
            expected_host_scope_authority_sha256="1" * 64,
            expected_authority=binding,
        )
    for field in names:
        forged = dict(receipt, **{field: "b" * 64})
        core = {name: item for name, item in forged.items() if name != "receipt_sha256"}
        forged["receipt_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
        with pytest.raises(probe.ProcProbeInputError, match="maintenance_probe_receipt_invalid"):
            probe.canonical_maintenance_target_receipt_bytes(
                forged,
                expected_target_index=index,
                expected_implementation_sha256="2" * 64,
                expected_host_scope_authority_sha256="1" * 64,
                expected_authority=binding,
            )


def test_retention_adapter_invokes_explicit_maintenance_command_and_binds_stdout_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _target_index()
    binding = probe.MaintenancePremountAuthority(
        **{field.name: "a" * 64 for field in dataclasses.fields(probe.MaintenancePremountAuthority)}
    )
    receipt = _target_receipt(index, binding)
    raw = _canonical(receipt) + b"\n"
    commands: list[list[str]] = []

    def root_owned(path: Path, *, setuid: bool = False) -> tuple[Path, str]:
        del setuid
        digest = "2" * 64 if path == retention.PRIVILEGED_PROC_HELPER else "8" * 64
        return path, digest

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    monkeypatch.setattr(retention, "_root_owned_file_sha256", root_owned)
    monkeypatch.setattr(retention, "_stable_file_sha256", lambda *_args, **_kwargs: "2" * 64)
    monkeypatch.setattr(retention, "PRIVILEGED_SCOPE_AUTHORITY_SHA256", "1" * 64)
    monkeypatch.setattr(retention.subprocess, "run", run)

    observed, _transport_sha256, output_sha256 = retention._run_maintenance_target_probe(index)  # noqa: SLF001

    assert observed == receipt
    assert commands == [
        [
            "/usr/bin/sudo",
            "-n",
            "--",
            "/usr/bin/python3",
            "-I",
            "-B",
            "-S",
            str(retention.PRIVILEGED_PROC_HELPER),
            "maintenance-target-probe",
        ]
    ]
    assert output_sha256 == hashlib.sha256(raw).hexdigest()


def test_ordinary_privileged_v3_contract_shape_is_unchanged() -> None:
    assert probe.PRIVILEGED_RECEIPT_SCHEMA == "friday.release-artifact-privileged-proc-receipt.v3"
    assert "maintenance" not in probe.privileged_target_reference_receipt.__doc__.lower()


def test_explicit_cli_keeps_ordinary_v3_and_maintenance_dispatch_disjoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_privileged_main", lambda: 17)
    monkeypatch.setattr(probe, "_maintenance_main", lambda: 23)
    monkeypatch.setattr(
        probe,
        "maintenance_boot_requested",
        lambda: pytest.fail("explicit CLI dispatch must not inspect boot routing state"),
    )
    assert probe.main(["privileged-target-probe"]) == 17
    assert probe.main(["maintenance-target-probe"]) == 23


def test_sudoers_install_surface_grants_only_both_exact_probe_commands() -> None:
    deploy = ROOT / "deploy/release-retention"
    template_path = deploy / "friday-retention-probe.sudoers.in"
    template_raw = template_path.read_bytes()
    template = template_raw.decode("ascii")
    installer = (deploy / "install-privileged-proc-probe.sh").read_text()
    lines = [line for line in template.splitlines() if line]
    assert len(lines) == 1
    assert lines[0].endswith("release_artifact_proc_probe.py privileged-target-probe")
    assert "NOPASSWD:" in lines[0] and "*" not in lines[0]
    assert installer.count("release_artifact_proc_probe.py privileged-target-probe\\n") == 2
    assert "maintenance-target-probe" not in installer
    assert f"TEMPLATE_SHA256={hashlib.sha256(template_raw).hexdigest()}" in installer

    maintenance_policy = maintenance_install._maintenance_policy_payload(  # noqa: SLF001
        owner_uid=1234,
        python="/usr/bin/python3",
    ).decode("ascii")
    assert maintenance_policy.count("\n") == 1
    assert maintenance_policy.endswith("release_artifact_proc_probe.py maintenance-target-probe\n")
    assert "privileged-target-probe" not in maintenance_policy
    assert "NOPASSWD:" in maintenance_policy and "*" not in maintenance_policy


def test_boot_assets_encode_first_pid1_global_premount_fail_closed_rules() -> None:
    launcher = (BOOT / "friday-retention-maintenance-launcher.S").read_text()
    runner = (BOOT / "friday-retention-maintenance-runner.sh").read_text()
    hook = (BOOT / "friday-retention-maintenance-hook.sh").read_text()
    module = (BOOT / "module-setup.sh").read_text()

    assert "mov $39, %rax" in launcher
    assert "cmp $1, %rax" in launcher
    assert "mov $272, %rax" not in launcher
    assert runner.startswith("#!/bin/sh\nset -eu\n")
    assert "set -efu" not in runner
    assert "unshare" not in runner
    assert "unshare" not in module
    assert "rd.friday.retention.initrd_sha256=" in runner
    # One token advances the counter once and is admitted; a duplicate advances
    # it twice and is rejected by the exact cardinality fence below.
    assert runner.count("initrd_count=$((initrd_count + 1))") == 1
    assert '[ "$initrd_count" -eq 1 ] || fail' in runner
    assert 'rdinit="$RDINIT"' in runner
    assert "retain_initrd" in runner
    assert "sysctl.kernel.io_uring_disabled=2" in runner
    sysctl_write = "printf '%s\\n' 2 > /proc/sys/kernel/io_uring_disabled"
    assert sysctl_write in runner
    assert runner.index(sysctl_write) < runner.index("\ncapture_premount_fixed_point\n")
    assert "digest /sys/firmware/initrd" in runner
    assert "/proc/[0-9]*" in runner and "/task/[0-9]*" in runner
    assert "nsfs:" in runner and "mnt:\\[" in runner
    assert '!= "$ORDINARY_ROOT_DEVICE_ID"' in runner
    assert "ordinary-root-filesystem-uuid" in runner
    assert "root-block-module-chain" in runner
    assert "load_root_block_modules" in runner
    assert "verify_root_block_module_chain" in runner
    assert runner.count('case "$observed_modules" in') == 1
    assert "module_deadline=$((module_started_at + 60))" in runner
    assert "blkid -p -c /dev/null" in runner
    assert "blkid -U" not in runner
    assert 'match_count" -le 1' in runner
    assert "timeout -s KILL 2 blkid" in runner
    assert "deadline=$((started_at + 120))" in runner
    assert 'mount -t ext4 -o rw "$ROOT_DEVICE_NODE" "$SYSROOT"' in runner
    assert 'echo "base kernel-modules"' in module
    assert "bind_root_block_authority" in module
    assert "maintenance_build_fail" in module
    assert "dfatal" in module and "exit 1" in module
    assert "get_maj_min" in module
    assert "hostonly='' instmods" in module
    assert "timeout" in module and "blkid" in module and "modprobe" in module
    assert "exit 0" not in hook
    assert "rdinit_authority_missing" in hook


def test_module_build_rebinds_review_dev_t_to_unique_current_uuid_device() -> None:
    module = (BOOT / "module-setup.sh").read_text()

    assert "reviewed_root_device_id=$CONFIG_VALUE" in module
    assert "root_device_id=$matched_device_id" in module
    assert '[[ $matched_device_id == "$root_device_id" ]]' not in module
    assert '[[ $matched_device_id == "$reviewed_root_device_id" ]]' not in module
    assert module.index("root_device_id=$matched_device_id") < module.index(
        "root_path=/dev/block/$root_device_id"
    )
    assert "((device_count <= 4096))" in module
    assert "((device_count > 0 && match_count == 1))" in module
    assert "scan_deadline=$((SECONDS + 120))" in module
    assert "((SECONDS < scan_deadline))" in module
    assert '[[ $block_inventory_after == "$block_inventory" ]]' in module
    assert "blkid -p -c /dev/null -s UUID" in module
    assert "[[ $root_type == ext4 ]]" in module
    assert '"/sys/dev/block/$root_device_id"/slaves/*' in module
    assert module.index("root_device_id=$matched_device_id") < module.index(
        "hostonly='' instmods \"${exact_modules[@]}\""
    )
