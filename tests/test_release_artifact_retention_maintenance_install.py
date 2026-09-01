from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import signal
import stat
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tools import release_artifact_retention_maintenance_install as host_install

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/release-retention/install-one-shot-maintenance-boot.sh"
UNINSTALLER = ROOT / "deploy/release-retention/uninstall-one-shot-maintenance-boot.sh"
PROC_HELPER_SOURCE = ROOT / "tools/release_artifact_proc_probe.py"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class InstallCase:
    root: Path
    source: Path
    request: Path
    request_sha256: str
    transaction_id: str
    owner: str
    source_digests: dict[str, str]
    ordinary_initrd: Path
    ordinary_initrd_raw: bytes
    ordinary_policy: Path
    ordinary_policy_raw: bytes
    privileged_helper: Path
    privileged_helper_raw: bytes

    def install_argv(self) -> tuple[str, ...]:
        return (
            "--source-directory",
            str(self.source),
            "--request",
            str(self.request),
            "--expected-request-sha256",
            self.request_sha256,
            "--owner-user",
            self.owner,
            "--expected-launcher-source-sha256",
            self.source_digests["launcher"],
            "--expected-module-sha256",
            self.source_digests["module"],
            "--expected-hook-sha256",
            self.source_digests["hook"],
            "--expected-runner-sha256",
            self.source_digests["runner"],
            "--expected-proc-probe-sha256",
            self.source_digests["proc_probe"],
            "--root",
            str(self.root),
        )

    def remove_argv(self) -> tuple[str, ...]:
        return (
            "--transaction-id",
            self.transaction_id,
            "--expected-request-sha256",
            self.request_sha256,
            "--root",
            str(self.root),
        )


def _write(path: Path, raw: bytes, *, mode: int) -> None:
    missing: list[Path] = []
    cursor = path.parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        directory.chmod(0o755)
    path.write_bytes(raw)
    path.chmod(mode)


def _case(
    base: Path,
    *,
    transaction_character: str = "a",
    root: Path | None = None,
) -> InstallCase:
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    fake_root = root or base / "root"
    if not fake_root.exists():
        fake_root.mkdir(mode=0o700)
    privileged_helper = fake_root / host_install.PRIVILEGED_PROC_HELPER_PATH[1:]
    privileged_helper_raw = PROC_HELPER_SOURCE.read_bytes()
    privileged_lock = fake_root / host_install.PRIVILEGED_PROC_INSTALL_LOCK_PATH[1:]
    if root is None:
        _write(privileged_helper, privileged_helper_raw, mode=0o755)
        _write(privileged_lock, b"", mode=0o600)
    else:
        assert privileged_helper.read_bytes() == privileged_helper_raw
        assert stat.S_IMODE(privileged_helper.stat().st_mode) == 0o755
        assert privileged_lock.read_bytes() == b""
        assert stat.S_IMODE(privileged_lock.stat().st_mode) == 0o600
    ordinary_policy = fake_root / "etc/sudoers.d/friday-retention-probe"
    python = Path("/usr/bin/python3").resolve(strict=True)
    ordinary_policy_raw = (
        f"#{os.geteuid()} ALL=(root) NOPASSWD: {python} -I -B -S "
        "/usr/libexec/friday/release_artifact_proc_probe.py privileged-target-probe\n"
    ).encode("ascii")
    if root is None:
        _write(ordinary_policy, ordinary_policy_raw, mode=0o440)
    else:
        assert ordinary_policy.read_bytes() == ordinary_policy_raw
        assert stat.S_IMODE(ordinary_policy.stat().st_mode) == 0o440
    source = base / "source"
    source.mkdir(mode=0o700)
    launcher = b"""\
.section .text
.global _start
.type _start,@function
_start:
    mov $60,%rax
    xor %rdi,%rdi
    syscall
.section .note.GNU-stack,"",@progbits
"""
    controller = b"#!/usr/bin/env python3\nraise SystemExit(0)\n"
    module = b"#!/bin/bash\ncheck() { return 255; }\n"
    hook = b"#!/bin/sh\nexit 0\n"
    runner = b"#!/bin/sh\nexit 0\n"
    payloads = {
        "controller": controller,
        "launcher": launcher,
        "module": module,
        "hook": hook,
        "runner": runner,
    }
    names = {
        "controller": "release_artifact_retention_maintenance.py",
        "launcher": "friday-retention-maintenance-launcher.S",
        "module": "module-setup.sh",
        "hook": "friday-retention-maintenance-hook.sh",
        "runner": "friday-retention-maintenance-runner.sh",
    }
    for role, raw in payloads.items():
        _write(source / names[role], raw, mode=0o555 if role != "launcher" else 0o444)

    toolchain = base / "toolchain"
    manifest = toolchain / "manifest.json"
    _write(manifest, b'{"schema":"sealed-test-toolchain"}\n', mode=0o400)
    kernel = base / "kernel"
    config = base / "config"
    ordinary_initrd = base / "ordinary-initrd"
    kernel_raw = b"ordinary-kernel"
    config_raw = b"ordinary-config"
    ordinary_initrd_raw = b"ordinary-initrd-must-never-change"
    for path, raw in (
        (kernel, kernel_raw),
        (config, config_raw),
        (ordinary_initrd, ordinary_initrd_raw),
    ):
        _write(path, raw, mode=0o400)
    profile = {
        "cmdline_sha256": "1" * 64,
        "io_uring_disabled": 0,
        "kernel_config_path": str(config),
        "kernel_config_sha256": _sha(config_raw),
        "kernel_image_path": str(kernel),
        "kernel_image_sha256": _sha(kernel_raw),
        "kernel_release": os.uname().release,
        "kernel_version_sha256": _sha(os.uname().version.encode("utf-8")),
        "ordinary_initrd_path": str(ordinary_initrd),
        "ordinary_initrd_sha256": _sha(ordinary_initrd_raw),
        "root_device_id": "8:1",
        "root_filesystem_uuid": "11111111-1111-1111-1111-111111111111",
    }
    transaction = transaction_character * 64
    candidates = [{"identity": "c" * 64, "path": "/reviewed/candidate"}]
    state = base / "state"
    state.mkdir(mode=0o700)
    core: dict[str, Any] = {
        "candidate_count": len(candidates),
        "candidate_set_sha256": _sha(_canonical(candidates)),
        "completion_output_path": str(state / "completion.json"),
        "controller_sha256": _sha(controller),
        "inputs": {},
        "installed_controller_path": ("/usr/libexec/friday/release_artifact_retention_maintenance.py"),
        "maintenance_cmdline_sha256": "2" * 64,
        "ordinary_profile": profile,
        "ordinary_profile_sha256": _sha(_canonical(profile)),
        "owner_uid": os.geteuid(),
        "plan_output_path": str(state / "plan.json"),
        "result_output_path": str(state / "result.json"),
        "reviewed_candidates": candidates,
        "schema": host_install.REQUEST_SCHEMA,
        "scope_seed_plan_sha256": "3" * 64,
        "toolchain_manifest_sha256": _sha(manifest.read_bytes()),
        "toolchain_root": str(toolchain),
        "transaction_id": transaction,
    }
    request = {**core, "request_sha256": _sha(_canonical(core))}
    request_path = base / "request.json"
    _write(request_path, _canonical(request) + b"\n", mode=0o600)
    return InstallCase(
        root=fake_root,
        source=source,
        request=request_path,
        request_sha256=str(request["request_sha256"]),
        transaction_id=transaction,
        owner=pwd.getpwuid(os.geteuid()).pw_name,
        source_digests={
            **{role: _sha(raw) for role, raw in payloads.items()},
            "proc_probe": _sha(privileged_helper_raw),
        },
        ordinary_initrd=ordinary_initrd,
        ordinary_initrd_raw=ordinary_initrd_raw,
        ordinary_policy=ordinary_policy,
        ordinary_policy_raw=ordinary_policy_raw,
        privileged_helper=privileged_helper,
        privileged_helper_raw=privileged_helper_raw,
    )


def _run(
    script: Path,
    arguments: tuple[str, ...],
    *,
    fail_after: str = "",
    boot_identity: str = "boot-a",
    root_device_id: str = "8:1",
    root_filesystem_uuid: str = "11111111-1111-1111-1111-111111111111",
    timeout: int = 60,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "FRIDAY_RETENTION_MAINTENANCE_INSTALL_TEST_MODE": "1",
        "FRIDAY_RETENTION_MAINTENANCE_FAIL_AFTER": fail_after,
        "FRIDAY_RETENTION_MAINTENANCE_TEST_BOOT_ID": boot_identity,
        "FRIDAY_RETENTION_MAINTENANCE_TEST_RESOLVED_ROOT_DEVICE_ID": root_device_id,
        "FRIDAY_RETENTION_MAINTENANCE_TEST_RESOLVED_ROOT_FILESYSTEM_UUID": (root_filesystem_uuid),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    return subprocess.run(  # noqa: S603
        [str(script), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        timeout=timeout,
    )


def _journal(case: InstallCase) -> dict[str, Any]:
    path = case.root / host_install.JOURNAL_PATH[1:]
    raw = path.read_bytes()
    value = json.loads(raw)
    assert raw == _canonical(value) + b"\n"
    assert value["journal_sha256"] == _sha(
        _canonical({name: item for name, item in value.items() if name != "journal_sha256"})
    )
    assert "boot_id" not in raw.decode("ascii")
    return value


def _assert_removed(case: InstallCase) -> None:
    value = _journal(case)
    assert value["phase"] == "removed"
    prefix = case.root / "usr/libexec/friday"
    assert not (prefix / "release_artifact_retention_maintenance.py").exists()
    assert not (prefix / "release_artifact_retention_maintenance_launcher").exists()
    assert not (case.root / f"boot/friday-retention-maintenance-{case.transaction_id}.img").exists()
    assert case.ordinary_initrd.read_bytes() == case.ordinary_initrd_raw
    assert case.ordinary_policy.read_bytes() == case.ordinary_policy_raw
    assert case.privileged_helper.read_bytes() == case.privileged_helper_raw
    ordinary_lock = case.root / host_install.PRIVILEGED_PROC_INSTALL_LOCK_PATH[1:]
    assert ordinary_lock.read_bytes() == b""
    assert stat.S_IMODE(ordinary_lock.stat().st_mode) == 0o600
    assert not (case.root / host_install.MAINTENANCE_POLICY_PATH[1:]).exists()


def test_live_profile_uses_fresh_uuid_resolution_not_review_dev_t(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = "a" * 64
    root_uuid = "11111111-1111-1111-1111-111111111111"
    cmdline = f"quiet root=UUID={root_uuid}".encode("ascii")
    evidence = {
        Path("/proc/cmdline"): cmdline + b"\n",
        Path("/proc/sys/kernel/io_uring_disabled"): b"0\n",
        Path(f"/proc/{os.getpid()}/mountinfo"): (b"1 0 259:1 / / rw - ext4 /dev/root rw\n"),
    }

    def external_file(path: Path, **_kwargs: Any) -> host_install.FileEvidence:
        raw = evidence[path]
        return host_install.FileEvidence(
            sha256=_sha(raw),
            mode=0o444,
            size=len(raw),
            raw=raw,
        )

    resolved: list[tuple[str, str]] = []

    def resolve(value: str, *, code: str) -> str:
        resolved.append((value, code))
        return "259:1"

    monkeypatch.setattr(host_install, "_external_file", external_file)
    monkeypatch.setattr(host_install, "_resolve_unique_root_device_id", resolve)

    current = host_install._validate_live_ordinary_profile(  # noqa: SLF001
        {
            "cmdline_sha256": _sha(cmdline),
            "io_uring_disabled": 0,
            "root_device_id": "8:1",
            "root_filesystem_uuid": root_uuid,
        },
        transaction_id=transaction_id,
        maintenance_cmdline_sha256=_sha(cmdline + b" rd.friday.retention=" + transaction_id.encode("ascii")),
    )

    assert current == "259:1"
    assert resolved == [(root_uuid, "maintenance_install_profile_invalid")]


def test_journal_exists_before_every_component_and_install_is_never_armed(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="install_prepared")
    assert crashed.returncode == 86
    assert _journal(case)["phase"] == "install_prepared"
    assert not (case.root / "usr/libexec/friday/release_artifact_retention_maintenance.py").exists()
    assert not (case.root / "usr/lib/dracut/modules.d/99friday-retention-maintenance").exists()
    assert not (case.root / "boot").exists()
    assert case.ordinary_initrd.read_bytes() == case.ordinary_initrd_raw

    installed = _run(
        INSTALLER,
        case.install_argv(),
        boot_identity="a-different-ordinary-boot",
    )
    assert installed.returncode == 0, installed.stderr
    assert b"installed_not_armed" in installed.stdout
    installed_journal = _journal(case)
    assert installed_journal["phase"] == "installed_not_armed"
    assert installed_journal["ordinary_root_device_id"] == "8:1"
    assert installed_journal["ordinary_root_filesystem_uuid"] == "11111111-1111-1111-1111-111111111111"
    assert installed_journal["artifact_set"]["components"]["controller"] == {
        "mode": 0o555,
        "stage": (
            f"/usr/libexec/friday/.release_artifact_retention_maintenance.py.{case.transaction_id}.new"
        ),
        "target": "/usr/libexec/friday/release_artifact_retention_maintenance.py",
    }
    controller = case.root / "usr/libexec/friday/release_artifact_retention_maintenance.py"
    assert controller.read_bytes() == (case.source / "release_artifact_retention_maintenance.py").read_bytes()
    assert stat.S_IMODE(controller.stat().st_mode) == 0o555
    assert controller.stat().st_uid == os.geteuid()
    image_authority = json.loads(
        (
            case.root
            / "usr/libexec/friday"
            / f"release_artifact_retention_maintenance_image-{case.transaction_id}.v1.json"
        ).read_bytes()
    )
    assert image_authority["controller_path"] == (
        "/usr/libexec/friday/release_artifact_retention_maintenance.py"
    )
    assert image_authority["controller_sha256"] == case.source_digests["controller"]
    python = Path("/usr/bin/python3").resolve(strict=True)
    policy_raw = (
        f"#{os.geteuid()} ALL=(root) NOPASSWD: {python} -I -B -S "
        "/usr/libexec/friday/release_artifact_proc_probe.py maintenance-target-probe\n"
    ).encode("ascii")
    policy = case.root / host_install.MAINTENANCE_POLICY_PATH[1:]
    assert policy.read_bytes() == policy_raw
    assert stat.S_IMODE(policy.stat().st_mode) == 0o440
    assert installed_journal["maintenance_policy_sha256"] == _sha(policy_raw)
    assert installed_journal["privileged_proc_helper_sha256"] == case.source_digests["proc_probe"]
    assert installed_journal["artifact_set"]["maintenance_policy"] == {
        "mode": 0o440,
        "stage": (f"/etc/sudoers.d/.friday-retention-maintenance-probe.{case.transaction_id}.new"),
        "target": host_install.MAINTENANCE_POLICY_PATH,
    }
    assert case.ordinary_policy.read_bytes() == case.ordinary_policy_raw
    assert case.privileged_helper.read_bytes() == case.privileged_helper_raw
    assert image_authority["ordinary_root_device_id"] == "8:1"
    assert image_authority["ordinary_root_filesystem_uuid"] == "11111111-1111-1111-1111-111111111111"
    installed_config = case.root / host_install.Layout(case.transaction_id).config_dir[1:]
    assert (installed_config / "ordinary-root-device-id").read_bytes() == b"8:1\n"
    assert (installed_config / "ordinary-root-filesystem-uuid").read_bytes() == (
        b"11111111-1111-1111-1111-111111111111\n"
    )
    assert not (case.source.parent / "toolchain/tools/release_artifact_retention_maintenance.py").exists()
    assert not (case.root / "etc/default").exists()
    assert not (case.root / "etc/sysctl.d").exists()
    assert case.ordinary_initrd.read_bytes() == case.ordinary_initrd_raw

    removed = _run(UNINSTALLER, case.remove_argv(), boot_identity="yet-another-boot")
    assert removed.returncode == 0, removed.stderr
    _assert_removed(case)


@pytest.mark.parametrize("phase", host_install.INSTALL_PHASES)
@pytest.mark.parametrize("recovery", ("resume", "remove"))
def test_every_install_phase_is_cross_boot_resumable_or_removable(
    tmp_path: Path,
    phase: str,
    recovery: str,
) -> None:
    case = _case(tmp_path)
    crashed = _run(
        INSTALLER,
        case.install_argv(),
        fail_after=phase,
        boot_identity="boot-one",
        root_device_id="8:1",
    )
    assert crashed.returncode == 86
    crashed_journal = _journal(case)
    assert crashed_journal["phase"] == phase
    assert crashed_journal["ordinary_root_device_id"] == "8:1"
    if recovery == "resume":
        resumed = _run(
            INSTALLER,
            case.install_argv(),
            boot_identity="boot-two",
            root_device_id="259:1",
        )
        assert resumed.returncode == 0, resumed.stderr
        assert _journal(case)["phase"] == "installed_not_armed"
    removed = _run(
        UNINSTALLER,
        case.remove_argv(),
        boot_identity="boot-three",
        root_device_id="259:1",
    )
    assert removed.returncode == 0, removed.stderr
    _assert_removed(case)


def test_cross_boot_dev_t_rebind_still_requires_the_reviewed_uuid(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)

    refused = _run(
        INSTALLER,
        case.install_argv(),
        root_device_id="259:1",
        root_filesystem_uuid="22222222-2222-2222-2222-222222222222",
    )

    assert refused.returncode == 2
    assert not (case.root / host_install.JOURNAL_PATH[1:]).exists()


@pytest.mark.parametrize("phase", host_install.REMOVE_PHASES)
def test_every_remove_phase_is_cross_boot_resumable(
    tmp_path: Path,
    phase: str,
) -> None:
    case = _case(tmp_path)
    installed = _run(INSTALLER, case.install_argv())
    assert installed.returncode == 0, installed.stderr
    crashed = _run(
        UNINSTALLER,
        case.remove_argv(),
        fail_after=phase,
        boot_identity="remove-boot-one",
    )
    assert crashed.returncode == 86
    assert _journal(case)["phase"] == phase
    resumed = _run(
        UNINSTALLER,
        case.remove_argv(),
        boot_identity="remove-boot-two",
    )
    assert resumed.returncode == 0, resumed.stderr
    _assert_removed(case)


@pytest.mark.parametrize("fault", ("effect:policy:stage", "effect:policy:publish"))
@pytest.mark.parametrize("recovery", ("resume", "remove"))
def test_policy_publication_effect_is_crash_resumable_or_removable(
    tmp_path: Path,
    fault: str,
    recovery: str,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after=fault)
    assert crashed.returncode == 86
    assert _journal(case)["phase"] == "policy_publishing"
    policy = case.root / host_install.MAINTENANCE_POLICY_PATH[1:]
    if fault == "effect:policy:stage":
        assert not policy.exists()
    else:
        assert policy.exists()
    if recovery == "resume":
        resumed = _run(INSTALLER, case.install_argv(), boot_identity="policy-resume")
        assert resumed.returncode == 0, resumed.stderr
        assert _journal(case)["phase"] == "installed_not_armed"
    removed = _run(UNINSTALLER, case.remove_argv(), boot_identity="policy-remove")
    assert removed.returncode == 0, removed.stderr
    _assert_removed(case)


def test_torn_journaled_policy_stage_is_rebuilt_before_publication(tmp_path: Path) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="policy_publishing")
    assert crashed.returncode == 86
    layout = host_install.Layout(case.transaction_id)
    stage = case.root / layout.maintenance_policy_stage[1:]
    _write(stage, b"torn-policy", mode=0o440)

    resumed = _run(INSTALLER, case.install_argv(), boot_identity="policy-stage-repair")
    assert resumed.returncode == 0, resumed.stderr
    assert not stage.exists()
    assert _journal(case)["phase"] == "installed_not_armed"
    assert _run(UNINSTALLER, case.remove_argv()).returncode == 0
    _assert_removed(case)


def test_policy_revocation_is_first_and_never_republished_after_crash(tmp_path: Path) -> None:
    case = _case(tmp_path)
    assert _run(INSTALLER, case.install_argv()).returncode == 0
    policy = case.root / host_install.MAINTENANCE_POLICY_PATH[1:]
    controller = case.root / "usr/libexec/friday/release_artifact_retention_maintenance.py"
    initrd = case.root / f"boot/friday-retention-maintenance-{case.transaction_id}.img"

    crashed = _run(
        UNINSTALLER,
        case.remove_argv(),
        fail_after="effect:remove:policy",
    )
    assert crashed.returncode == 86
    assert _journal(case)["phase"] == "policy_revoking"
    assert not policy.exists()
    assert controller.exists()
    assert initrd.exists()
    assert case.ordinary_policy.read_bytes() == case.ordinary_policy_raw
    assert _run(INSTALLER, case.install_argv()).returncode == 2
    assert not policy.exists()

    resumed = _run(UNINSTALLER, case.remove_argv(), boot_identity="revocation-resume")
    assert resumed.returncode == 0, resumed.stderr
    _assert_removed(case)


@pytest.mark.parametrize("substitution", ("mode", "symlink"))
def test_substituted_policy_fails_closed_before_other_removal(
    tmp_path: Path,
    substitution: str,
) -> None:
    case = _case(tmp_path)
    assert _run(INSTALLER, case.install_argv()).returncode == 0
    policy = case.root / host_install.MAINTENANCE_POLICY_PATH[1:]
    victim = tmp_path / "policy-substitution-victim"
    victim.write_bytes(b"must-survive")
    if substitution == "mode":
        policy.chmod(0o600)
    else:
        policy.unlink()
        policy.symlink_to(victim)
    controller = case.root / "usr/libexec/friday/release_artifact_retention_maintenance.py"
    initrd = case.root / f"boot/friday-retention-maintenance-{case.transaction_id}.img"

    refused = _run(UNINSTALLER, case.remove_argv())
    assert refused.returncode == 2
    assert _journal(case)["phase"] == "policy_revoking"
    if substitution == "mode":
        assert stat.S_IMODE(policy.stat().st_mode) == 0o600
    else:
        assert policy.is_symlink()
    assert victim.read_bytes() == b"must-survive"
    assert controller.exists()
    assert initrd.exists()
    assert case.ordinary_policy.read_bytes() == case.ordinary_policy_raw


def test_helper_substitution_before_policy_publish_never_grants_authority(tmp_path: Path) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="policy_publishing")
    assert crashed.returncode == 86
    case.privileged_helper.chmod(0o600)

    refused = _run(INSTALLER, case.install_argv())
    assert refused.returncode == 2
    assert not (case.root / host_install.MAINTENANCE_POLICY_PATH[1:]).exists()
    removed = _run(UNINSTALLER, case.remove_argv())
    assert removed.returncode == 0, removed.stderr
    case.privileged_helper.chmod(0o755)
    _assert_removed(case)


def test_unjournaled_maintenance_policy_is_fenced_without_mutation(tmp_path: Path) -> None:
    case = _case(tmp_path)
    policy = case.root / host_install.MAINTENANCE_POLICY_PATH[1:]
    foreign = b"# foreign-maintenance-policy\n"
    _write(policy, foreign, mode=0o440)

    refused = _run(INSTALLER, case.install_argv())
    assert refused.returncode == 2
    assert policy.read_bytes() == foreign
    assert case.ordinary_policy.read_bytes() == case.ordinary_policy_raw
    assert not (case.root / host_install.JOURNAL_PATH[1:]).exists()


def test_ordinary_proc_install_lock_excludes_policy_transaction(tmp_path: Path) -> None:
    case = _case(tmp_path)
    lock_path = case.root / host_install.PRIVILEGED_PROC_INSTALL_LOCK_PATH[1:]
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = _run(INSTALLER, case.install_argv())
        assert refused.returncode == 2
        assert not (case.root / host_install.MAINTENANCE_POLICY_PATH[1:]).exists()
        assert not (case.root / host_install.JOURNAL_PATH[1:]).exists()
    finally:
        os.close(descriptor)

    installed = _run(INSTALLER, case.install_argv())
    assert installed.returncode == 0, installed.stderr
    assert _run(UNINSTALLER, case.remove_argv()).returncode == 0
    _assert_removed(case)


def test_incomplete_transaction_fences_distinct_request_without_mutation(
    tmp_path: Path,
) -> None:
    first = _case(tmp_path / "first")
    crashed = _run(INSTALLER, first.install_argv(), fail_after="payloads_staged")
    assert crashed.returncode == 86
    before = (first.root / host_install.JOURNAL_PATH[1:]).read_bytes()
    second = _case(
        tmp_path / "second",
        transaction_character="b",
        root=first.root,
    )
    blocked = _run(INSTALLER, second.install_argv(), boot_identity="different-boot")
    assert blocked.returncode == 2
    assert (first.root / host_install.JOURNAL_PATH[1:]).read_bytes() == before
    assert _journal(first)["transaction_id"] == first.transaction_id
    removed = _run(UNINSTALLER, first.remove_argv())
    assert removed.returncode == 0, removed.stderr
    _assert_removed(first)


def test_partial_publish_and_atomic_journal_stage_replay_exactly(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    partial = _run(
        INSTALLER,
        case.install_argv(),
        fail_after="effect:publish:controller",
    )
    assert partial.returncode == 86
    assert _journal(case)["phase"] == "components_publishing"
    resumed = _run(
        INSTALLER,
        case.install_argv(),
        fail_after="journal_stage:components_published",
    )
    assert resumed.returncode == 86
    assert _journal(case)["phase"] == "components_publishing"
    assert (case.root / host_install.JOURNAL_STAGE_PATH[1:]).exists()
    completed = _run(INSTALLER, case.install_argv(), boot_identity="new-boot")
    assert completed.returncode == 0, completed.stderr
    assert not (case.root / host_install.JOURNAL_STAGE_PATH[1:]).exists()
    assert _journal(case)["phase"] == "installed_not_armed"
    assert _run(UNINSTALLER, case.remove_argv()).returncode == 0


@pytest.mark.parametrize("has_current", (False, True))
def test_torn_private_journal_stage_is_discarded_and_replayed(
    tmp_path: Path,
    has_current: bool,
) -> None:
    case = _case(tmp_path)
    if has_current:
        crashed = _run(INSTALLER, case.install_argv(), fail_after="payloads_staged")
        assert crashed.returncode == 86
    stage = case.root / host_install.JOURNAL_STAGE_PATH[1:]
    _write(stage, b'{"torn":', mode=0o400)
    resumed = _run(INSTALLER, case.install_argv(), boot_identity="post-power-loss")
    assert resumed.returncode == 0, resumed.stderr
    assert not stage.exists()
    assert _journal(case)["phase"] == "installed_not_armed"
    assert _run(UNINSTALLER, case.remove_argv()).returncode == 0


def test_torn_journal_stage_with_alias_is_fenced_without_deleting_either_name(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    stage = case.root / host_install.JOURNAL_STAGE_PATH[1:]
    _write(stage, b'{"torn":', mode=0o400)
    alias = tmp_path / "journal-stage-alias"
    os.link(stage, alias)
    refused = _run(INSTALLER, case.install_argv())
    assert refused.returncode == 2
    assert stage.exists()
    assert alias.exists()
    assert stage.stat().st_ino == alias.stat().st_ino
    assert not (case.root / host_install.JOURNAL_PATH[1:]).exists()


def test_initial_journal_two_link_publication_recovers_without_losing_current(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="install_prepared")
    assert crashed.returncode == 86
    current = case.root / host_install.JOURNAL_PATH[1:]
    stage = case.root / host_install.JOURNAL_STAGE_PATH[1:]
    current_raw = current.read_bytes()
    current_inode = current.stat().st_ino
    os.link(current, stage)
    assert current.stat().st_nlink == 2
    assert stage.stat().st_ino == current_inode

    with host_install.RootFS(
        case.root,
        uid=os.geteuid(),
        gid=os.getegid(),
    ) as root:
        host_install._recover_journal_stage(  # noqa: SLF001
            root,
            expected_transaction=case.transaction_id,
        )

    assert not stage.exists()
    assert current.read_bytes() == current_raw
    assert current.stat().st_ino == current_inode
    assert current.stat().st_nlink == 1

    resumed = _run(INSTALLER, case.install_argv(), boot_identity="post-link-crash")
    assert resumed.returncode == 0, resumed.stderr
    assert _journal(case)["phase"] == "installed_not_armed"
    assert _run(UNINSTALLER, case.remove_argv()).returncode == 0


def test_journaled_private_partial_payload_is_repaired_from_bound_source(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="install_prepared")
    assert crashed.returncode == 86
    layout = host_install.Layout(case.transaction_id)
    controller_stage = case.root / layout.component_stage("controller")[1:]
    _write(controller_stage, b"partial-controller-write", mode=0o555)
    resumed = _run(INSTALLER, case.install_argv())
    assert resumed.returncode == 0, resumed.stderr
    controller = case.root / layout.controller[1:]
    assert controller.read_bytes() == (case.source / "release_artifact_retention_maintenance.py").read_bytes()
    assert _run(UNINSTALLER, case.remove_argv()).returncode == 0


def test_remove_cleans_journaled_partial_compile_output_before_payload_binding(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="install_prepared")
    assert crashed.returncode == 86
    layout = host_install.Layout(case.transaction_id)
    launcher_stage = case.root / layout.component_stage("launcher")[1:]
    _write(launcher_stage, b"partial-linker-output", mode=0o700)
    removed = _run(UNINSTALLER, case.remove_argv())
    assert removed.returncode == 0, removed.stderr
    assert not launcher_stage.exists()
    _assert_removed(case)


def test_malformed_journal_and_substituted_component_fail_without_following(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    assert _run(INSTALLER, case.install_argv()).returncode == 0
    journal_path = case.root / host_install.JOURNAL_PATH[1:]
    original = journal_path.read_bytes()
    journal_path.chmod(0o600)
    journal_path.write_bytes(original[:-2] + b',"phase":"removed"}\n')
    journal_path.chmod(0o400)
    rejected = _run(UNINSTALLER, case.remove_argv())
    assert rejected.returncode == 2
    initrd = case.root / f"boot/friday-retention-maintenance-{case.transaction_id}.img"
    assert initrd.exists()

    journal_path.chmod(0o600)
    journal_path.write_bytes(original)
    journal_path.chmod(0o400)
    hook = case.root / "usr/libexec/friday/release_artifact_retention_maintenance_hook.sh"
    victim = tmp_path / "victim"
    victim.write_bytes(b"must-survive")
    hook.unlink()
    hook.symlink_to(victim)
    refused = _run(UNINSTALLER, case.remove_argv())
    assert refused.returncode == 2
    assert victim.read_bytes() == b"must-survive"
    assert hook.is_symlink()
    assert _journal(case)["phase"] == "components_removing"


def test_removed_transaction_cannot_replay_but_a_new_identity_can_start(
    tmp_path: Path,
) -> None:
    first = _case(tmp_path / "first")
    assert _run(INSTALLER, first.install_argv()).returncode == 0
    assert _run(UNINSTALLER, first.remove_argv()).returncode == 0
    replay = _run(INSTALLER, first.install_argv())
    assert replay.returncode == 2

    second = _case(
        tmp_path / "second",
        transaction_character="b",
        root=first.root,
    )
    installed = _run(INSTALLER, second.install_argv())
    assert installed.returncode == 0, installed.stderr
    assert _journal(second)["transaction_id"] == second.transaction_id
    assert _run(UNINSTALLER, second.remove_argv()).returncode == 0
    _assert_removed(second)
    historical_replay = _run(INSTALLER, first.install_argv())
    assert historical_replay.returncode == 2
    assert _journal(second)["transaction_id"] == second.transaction_id


def test_wrong_transaction_cannot_take_over_incomplete_removal_authority(
    tmp_path: Path,
) -> None:
    first = _case(tmp_path / "first")
    crashed = _run(INSTALLER, first.install_argv(), fail_after="payloads_staged")
    assert crashed.returncode == 86
    before = (first.root / host_install.JOURNAL_PATH[1:]).read_bytes()
    second = _case(
        tmp_path / "second",
        transaction_character="b",
        root=first.root,
    )
    refused = _run(UNINSTALLER, second.remove_argv())
    assert refused.returncode == 2
    assert (first.root / host_install.JOURNAL_PATH[1:]).read_bytes() == before
    removed = _run(UNINSTALLER, first.remove_argv())
    assert removed.returncode == 0, removed.stderr
    _assert_removed(first)


def test_durable_rollover_stage_fences_a_third_transaction_and_resumes_exactly(
    tmp_path: Path,
) -> None:
    first = _case(tmp_path / "first")
    assert _run(INSTALLER, first.install_argv()).returncode == 0
    assert _run(UNINSTALLER, first.remove_argv()).returncode == 0
    second = _case(
        tmp_path / "second",
        transaction_character="b",
        root=first.root,
    )
    staged = _run(
        INSTALLER,
        second.install_argv(),
        fail_after="journal_stage:install_prepared",
    )
    assert staged.returncode == 86
    journal_before = (first.root / host_install.JOURNAL_PATH[1:]).read_bytes()
    stage_path = first.root / host_install.JOURNAL_STAGE_PATH[1:]
    stage_before = stage_path.read_bytes()

    third = _case(
        tmp_path / "third",
        transaction_character="c",
        root=first.root,
    )
    refused = _run(INSTALLER, third.install_argv())
    assert refused.returncode == 2
    assert (first.root / host_install.JOURNAL_PATH[1:]).read_bytes() == journal_before
    assert stage_path.read_bytes() == stage_before

    resumed = _run(INSTALLER, second.install_argv(), boot_identity="rollover-resume")
    assert resumed.returncode == 0, resumed.stderr
    assert _journal(second)["transaction_id"] == second.transaction_id
    assert first.transaction_id in _journal(second)["retired_transaction_ids"]
    assert _run(UNINSTALLER, second.remove_argv()).returncode == 0


def test_torn_config_stage_is_structurally_removable_from_journaled_name(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    crashed = _run(
        INSTALLER,
        case.install_argv(),
        fail_after="config_publishing",
    )
    assert crashed.returncode == 86
    layout = host_install.Layout(case.transaction_id)
    config_dir = case.root / layout.config_dir[1:]
    config_dir.mkdir(mode=0o700)
    torn = case.root / layout.config_stage("controller-path")[1:]
    _write(torn, b"short-before-power-loss", mode=0o400)

    removed = _run(UNINSTALLER, case.remove_argv(), boot_identity="remove-after-loss")
    assert removed.returncode == 0, removed.stderr
    _assert_removed(case)


def test_transient_assembler_object_mode_is_removable_before_digest_binding(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="install_prepared")
    assert crashed.returncode == 86
    layout = host_install.Layout(case.transaction_id)
    partial_object = case.root / layout.launcher_object_stage[1:]
    _write(partial_object, b"partial-as-output", mode=0o600)

    removed = _run(UNINSTALLER, case.remove_argv())
    assert removed.returncode == 0, removed.stderr
    _assert_removed(case)


@pytest.mark.parametrize("recovery", ("resume", "remove"))
def test_journaled_dracut_tree_is_nofollow_recoverable(
    tmp_path: Path,
    recovery: str,
) -> None:
    case = _case(tmp_path)
    crashed = _run(INSTALLER, case.install_argv(), fail_after="initrd_building")
    assert crashed.returncode == 86
    layout = host_install.Layout(case.transaction_id)
    private = case.root / layout.dracut_tmp_dir[1:]
    nested = private / "dracut.dABCD12/initramfs/usr/lib"
    nested.mkdir(parents=True, mode=0o750)
    private.chmod(0o700)
    (nested / "partial").write_bytes(b"partial-initramfs")
    fifo = private / "dracut.dABCD12/build.fifo"
    os.mkfifo(fifo, 0o600)
    victim = tmp_path / "outside-victim"
    victim.write_bytes(b"must-survive")
    (private / "dracut.dABCD12/outside-link").symlink_to(victim)

    if recovery == "resume":
        resumed = _run(INSTALLER, case.install_argv(), boot_identity="resume-boot")
        assert resumed.returncode == 0, resumed.stderr
        assert _journal(case)["phase"] == "installed_not_armed"
    removed = _run(UNINSTALLER, case.remove_argv(), boot_identity="remove-boot")
    assert removed.returncode == 0, removed.stderr
    assert victim.read_bytes() == b"must-survive"
    _assert_removed(case)


def test_unjournaled_dracut_tree_fences_a_new_transaction(tmp_path: Path) -> None:
    case = _case(tmp_path)
    layout = host_install.Layout(case.transaction_id)
    residue = case.root / layout.dracut_tmp_dir[1:]
    residue.mkdir(parents=True, mode=0o700)
    residue.chmod(0o700)
    (residue / "foreign").write_bytes(b"not-this-transaction")

    refused = _run(INSTALLER, case.install_argv())
    assert refused.returncode == 2
    assert residue.exists()
    assert not (case.root / host_install.JOURNAL_PATH[1:]).exists()


def test_residue_scan_propagates_a_substituted_bootstrapped_directory(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    with host_install.RootFS(
        case.root,
        uid=os.geteuid(),
        gid=os.getegid(),
    ) as root:
        host_install._bootstrap(root)  # noqa: SLF001
        friday = case.root / "usr/libexec/friday"
        moved = case.root / "usr/libexec/friday-moved"
        friday.rename(moved)
        friday.symlink_to(moved, target_is_directory=True)
        with pytest.raises(host_install.MaintenanceInstallError):
            host_install._known_residue_names(root)  # noqa: SLF001


def test_list_dir_opens_once_and_closes_its_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    with host_install.RootFS(
        case.root,
        uid=os.geteuid(),
        gid=os.getegid(),
    ) as root:
        host_install._bootstrap(root)  # noqa: SLF001
        original = root.open_dir
        opened: list[int] = []

        def observed_open_dir(
            logical: str,
            *,
            create: bool = False,
            final_mode: int | None = None,
        ) -> int:
            descriptor = original(
                logical,
                create=create,
                final_mode=final_mode,
            )
            opened.append(descriptor)
            return descriptor

        root.ensure_dir("/usr/libexec/friday/list-dir-empty", mode=0o755)
        monkeypatch.setattr(root, "open_dir", observed_open_dir)
        assert root.list_dir("/usr/libexec/friday/list-dir-empty") == ()
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])


def test_private_tree_cleanup_rejects_same_device_mount_id_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    layout = host_install.Layout(case.transaction_id)
    with host_install.RootFS(
        case.root,
        uid=os.geteuid(),
        gid=os.getegid(),
    ) as root:
        host_install._bootstrap(root)  # noqa: SLF001
        root.ensure_dir(layout.dracut_tmp_dir, mode=0o700)
        nested = f"{layout.dracut_tmp_dir}/same-device-bind"
        root.ensure_dir(nested, mode=0o700)
        root.write_new(f"{nested}/must-remain", b"bound", mode=0o400)
        mount_ids = iter((17, 17, 23))
        monkeypatch.setattr(
            host_install,
            "_descriptor_mount_id",
            lambda _descriptor: next(mount_ids),
        )

        with pytest.raises(host_install.MaintenanceInstallError):
            root.remove_private_tree(layout.dracut_tmp_dir, mode=0o700)
        assert root.status(f"{nested}/must-remain") is not None


@pytest.mark.parametrize("surface", ("request", "journal"))
def test_fifo_authority_is_rejected_without_blocking(
    tmp_path: Path,
    surface: str,
) -> None:
    case = _case(tmp_path)
    if surface == "request":
        case.request.unlink()
        os.mkfifo(case.request, 0o600)
    else:
        journal = case.root / host_install.JOURNAL_PATH[1:]
        journal.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        os.mkfifo(journal, 0o400)

    refused = _run(INSTALLER, case.install_argv(), timeout=3)
    assert refused.returncode == 2


def test_external_regular_reader_sets_nonblocking_on_the_race_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "authority"
    _write(source, b"authority", mode=0o400)
    original_open = host_install.os.open
    observed: list[int] = []

    def recording_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == source.name and dir_fd is not None:
            observed.append(flags)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(host_install.os, "open", recording_open)
    evidence = host_install._external_file(  # noqa: SLF001
        source,
        maximum=1024,
        code="test_invalid",
    )
    assert evidence.sha256 == _sha(b"authority")
    assert observed
    assert observed[-1] & os.O_NONBLOCK


def test_exec_child_holds_transaction_lock_after_parent_sigkill(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    started = tmp_path / "builder.started"
    finished = tmp_path / "builder.finished"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child_pid = os.fork()
    if child_pid == 0:
        try:
            code = (
                "from pathlib import Path; import time; "
                f"Path({str(started)!r}).write_text('started'); "
                "time.sleep(1.0); "
                f"Path({str(finished)!r}).write_text('finished')"
            )
            result = host_install._run_host_tool(  # noqa: SLF001
                ["/usr/bin/python3", "-c", code],
                environment={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                timeout=10,
                lock_fd=lock_fd,
            )
            os._exit(result.returncode)
        except BaseException:  # noqa: BLE001
            os._exit(91)

    os.close(lock_fd)
    reaped = False
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        os.kill(child_pid, signal.SIGKILL)
        os.waitpid(child_pid, 0)
        reaped = True

        contender = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        pytest.fail("exec child did not release inherited transaction lock")
                    time.sleep(0.01)
            assert finished.exists()
        finally:
            os.close(contender)
    finally:
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
