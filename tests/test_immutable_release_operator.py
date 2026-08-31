"""Fail-closed contracts for the wheel-only immutable release operator."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import venv
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import friday
import tools.immutable_release_operator as operator
from tools import release_artifact_retention_operator as retention_apply
from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import expected_effect_capability_snapshot
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
    AssistPromotionQualityBasis,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, canonical_sha256
from friday.orchestration.supervisor_effect_maturity import (
    SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256,
    build_read_only_maturity_artifact,
)
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
)
from friday.orchestration.supervisor_promotion_evidence_producer import (
    SupervisorPromotionOperatorAttestation,
    build_supervisor_assist_promotion_evidence,
    build_supervisor_canary_promotion_evidence,
    build_supervisor_promotion_bundle_payload,
    canonical_json_file_bytes,
    load_accepted_supervisor_production_baseline,
    load_canonical_supervisor_latency_budget,
)
from friday.orchestration.supervisor_representative_window_attestation import (
    REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
    REPRESENTATIVE_WINDOW_AUTHORITY,
    REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
    representative_window_sha256,
)
from friday.storage import SCHEMA_VERSION


def _release(tmp_path: Path, name: str, *, schema: int, commit: str) -> operator.ReleaseIdentity:
    return operator.ReleaseIdentity(
        root=tmp_path / name,
        commit=commit,
        version="0.205.1",
        tree_manifest_sha256=hashlib.sha256(name.encode()).hexdigest(),
        max_schema=schema,
    )


def _rewrite_signed_journal(
    path: Path,
    mutate: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="ascii"))
    core = {key: value for key, value in payload.items() if key != "journal_sha256"}
    mutate(core)
    signed = {
        **core,
        "journal_sha256": hashlib.sha256(operator._canonical_json(core)).hexdigest(),  # noqa: SLF001
    }
    path.chmod(0o600)
    path.write_bytes(operator._canonical_json(signed) + b"\n")  # noqa: SLF001
    path.chmod(0o600)
    return core


class FakePort:
    def __init__(
        self,
        *,
        fail: str = "",
        backup_schema: int = 33,
        memory_vault_mode: str = "disabled",
        staged_config_transition: str = "obsidian_enable",
        engineer_lifecycle_required: bool = False,
        engineer_lifecycle_provisioned: bool = False,
    ) -> None:
        self.fail = fail
        self.memory_vault_mode = memory_vault_mode
        self.staged_config_transition = staged_config_transition
        self.engineer_lifecycle_required = engineer_lifecycle_required
        self.engineer_lifecycle_provisioned = engineer_lifecycle_provisioned
        self.failure_injected = False
        self.events: list[str] = []
        self.active: operator.ReleaseIdentity | None = None
        self.leases_held = False
        self.obsidian_mode = "enabled"
        self.predecessor_env_sha256 = ""
        self.canonical_env_sha256 = ""
        self.next_env_file = Path("/private-state/next.env")
        self.next_env_file_sha256 = ""
        self.backup = operator.DatabaseBackup(
            schema_version=backup_schema,
            receipt_sha256="b" * 64,
            inbox_receipt_sha256="d" * 64,
            opaque="exact-db-wal-inbox",
        )

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail == name and not self.failure_injected:
            self.failure_injected = True
            raise RuntimeError("synthetic failure")

    def activation_policy_receipt(self) -> dict[str, str]:
        return {
            "memory_vault_cutover_phase": (
                "phase_b_body_free" if self.memory_vault_mode == "disabled" else "phase_a_full_owner_bridge"
            ),
            "memory_vault_mode": self.memory_vault_mode,
        }

    def engineer_store_lifecycle_required(self) -> bool:
        return self.engineer_lifecycle_required

    def validate_engineer_recovery_contour(
        self,
        releases: tuple[operator.ReleaseIdentity, ...],
    ) -> None:
        del releases

    def engineer_store_lifecycle_provisioned(self) -> bool:
        return self.engineer_lifecycle_provisioned

    def provision_engineer_store(self, release: operator.ReleaseIdentity) -> None:
        self._event(f"provision_engineer:{release.root.name}")

    def verify_release(
        self,
        release: operator.ReleaseIdentity,
        *,
        use_predecessor_config: bool = False,
    ) -> None:
        del use_predecessor_config
        self._event(f"verify:{release.root.name}")

    def verify_units(self, candidate: operator.ReleaseIdentity) -> None:
        self._event(f"units:{candidate.root.name}")

    def verify_active_anchor(
        self,
        previous: operator.ReleaseIdentity,
        candidate: operator.ReleaseIdentity,
    ) -> None:
        del candidate
        self._event(f"active_anchor:{previous.root.name}")

    def stop_bridge(self) -> None:
        self._event("stop_bridge")

    def stop_backend(self) -> None:
        self._event("stop_backend")

    def services_inactive(self) -> bool:
        self._event("inactive")
        return True

    def writer_leases_held(self) -> bool:
        self._event("leases")
        return self.leases_held

    def acquire_writer_leases(self) -> None:
        self._event("acquire_leases")
        self.leases_held = True

    def release_writer_leases(self) -> None:
        self._event("release_leases")
        self.leases_held = False

    def validate_staged_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None:
        assert transition == self.staged_config_transition
        assert len(predecessor_env_sha256) == 64
        assert len(next_env_file_sha256) == 64
        if self.canonical_env_sha256:
            assert self.canonical_env_sha256 == predecessor_env_sha256
        self.predecessor_env_sha256 = predecessor_env_sha256
        self.canonical_env_sha256 = predecessor_env_sha256
        self.next_env_file = next_env_file
        self.next_env_file_sha256 = next_env_file_sha256
        self._event("validate_staged_config")

    def activate_staged_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None:
        assert transition == self.staged_config_transition
        assert self.canonical_env_sha256 in {"", predecessor_env_sha256, next_env_file_sha256}
        self._event("activate_staged_config")
        self.predecessor_env_sha256 = predecessor_env_sha256
        self.canonical_env_sha256 = next_env_file_sha256
        self.next_env_file = next_env_file
        self.next_env_file_sha256 = next_env_file_sha256
        self.obsidian_mode = "enabled"

    def select_predecessor_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None:
        assert transition == self.staged_config_transition
        assert self.canonical_env_sha256 in {"", predecessor_env_sha256}
        self.predecessor_env_sha256 = predecessor_env_sha256
        self.canonical_env_sha256 = predecessor_env_sha256
        self.next_env_file = next_env_file
        self.next_env_file_sha256 = next_env_file_sha256
        self.obsidian_mode = "disabled"
        self._event("select_predecessor_config")

    def backup_database(self, release: operator.ReleaseIdentity) -> operator.DatabaseBackup:
        del release
        self._event("backup_db_wal_inbox")
        return self.backup

    def offline_migrate(
        self,
        release: operator.ReleaseIdentity,
        backup: operator.DatabaseBackup,
    ) -> None:
        assert backup is self.backup
        self._event(f"offline_migrate:{release.root.name}")

    def repair_file_aliases(
        self,
        release: operator.ReleaseIdentity,
        backup: operator.DatabaseBackup,
    ) -> dict[str, object]:
        assert backup is self.backup
        self._event(f"repair_aliases:{release.root.name}")
        core: dict[str, object] = {
            "schema": operator.ALIAS_REPAIR_RECEIPT_SCHEMA,
            "status": "not_requested",
            "applied_count": 0,
            "plan_sha256": "0" * 64,
            "backup_manifest_sha256": "0" * 64,
            "backup_database_sha256": "0" * 64,
            "backup_inbox_sha256": "0" * 64,
            "pre_apply_database_sha256": "0" * 64,
            "writer_quiescence_sha256": "0" * 64,
        }
        return {
            **core,
            "receipt_sha256": hashlib.sha256(operator._canonical_json(core)).hexdigest(),  # noqa: SLF001
        }

    def switch_anchor(self, release: operator.ReleaseIdentity) -> None:
        self._event(f"anchor:{release.root.name}")
        self.active = release

    def start_backend(self, release: operator.ReleaseIdentity) -> None:
        self._event(f"start_backend:{release.root.name}")

    def accept_backend(self, release: operator.ReleaseIdentity) -> None:
        self._event(f"accept_backend:{release.root.name}")

    def start_bridge(self, release: operator.ReleaseIdentity) -> None:
        self._event(f"start_bridge:{release.root.name}")

    def accept_bridge(self, release: operator.ReleaseIdentity) -> None:
        self._event(f"accept_bridge:{release.root.name}")

    def restore_database(
        self,
        backup: operator.DatabaseBackup,
        release: operator.ReleaseIdentity,
    ) -> None:
        del release
        assert backup is self.backup
        self._event("restore_exact_db_wal_inbox")


class MemoryJournal:
    def __init__(
        self,
        *,
        prebackup_config_transition: str = "",
        predecessor_env_sha256: str = "",
        next_env_file: Path | None = None,
        next_env_file_sha256: str = "",
    ) -> None:
        self.events: list[str] = []
        self.state: dict[str, object] = {}
        self.prebackup_config_transition = prebackup_config_transition
        self.predecessor_env_sha256 = predecessor_env_sha256
        self.next_env_file = next_env_file
        self.next_env_file_sha256 = next_env_file_sha256

    def begin(self, *, candidate, previous, fallback) -> None:
        self.events.append("prepared")
        self.state = {
            "phase": "prepared",
            "candidate": candidate,
            "previous": previous,
            "fallback": fallback,
            "backup": None,
            "database_mutation_possible": False,
            "network_writer_uncertain": False,
            "engineer_provision_committed": False,
            "writer_target": "",
            "prebackup_config_transition": self.prebackup_config_transition,
            "predecessor_env_sha256": self.predecessor_env_sha256,
            "next_env_file": str(self.next_env_file) if self.next_env_file is not None else "",
            "next_env_file_sha256": self.next_env_file_sha256,
        }

    def record(
        self,
        phase: str,
        *,
        backup=None,
        database_mutation_possible: bool = False,
        network_writer_uncertain: bool = False,
        writer_target: str = "",
        terminal_receipt_sha256: str = "",
        staged_transition_validation_sha256: str = "",
    ) -> None:
        self.events.append(phase)
        self.state["phase"] = phase
        if backup is not None:
            self.state["backup"] = backup
        self.state["database_mutation_possible"] = bool(
            self.state.get("database_mutation_possible") or database_mutation_possible
        )
        self.state["network_writer_uncertain"] = bool(
            self.state.get("network_writer_uncertain") or network_writer_uncertain
        )
        if phase == "provision_committed":
            self.state["engineer_provision_committed"] = True
        if writer_target:
            self.state["writer_target"] = writer_target
        self.state["terminal_receipt_sha256"] = terminal_receipt_sha256
        if staged_transition_validation_sha256:
            self.state["staged_transition_validation_sha256"] = staged_transition_validation_sha256

    def load(self):
        return dict(self.state)

    def release_identities(self):
        return self.state["candidate"], self.state["previous"], self.state["fallback"]

    def database_backup(self):
        return self.state.get("backup")


@dataclass(frozen=True)
class Releases:
    candidate: operator.ReleaseIdentity
    previous: operator.ReleaseIdentity
    fallback: operator.ReleaseIdentity


@pytest.fixture
def releases(tmp_path: Path) -> Releases:
    return Releases(
        candidate=replace(
            _release(tmp_path, "candidate", schema=34, commit="c" * 40),
            venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
        ),
        previous=_release(
            tmp_path,
            "clean-schema33",
            schema=33,
            commit="98ce0150e84db5069a2688985b1c5a21dd1b6afa",
        ),
        fallback=replace(
            _release(tmp_path, "schema34-fallback", schema=34, commit="f" * 40),
            venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
        ),
    )


def test_units_share_one_atomic_anchor_and_bound_restart_storm(tmp_path: Path) -> None:
    anchor = tmp_path / "current-release"
    units = operator.render_units(
        anchor=anchor,
        env_file=tmp_path / "private.env",
        friday_home=tmp_path / "home",
    )
    assert set(units) == {"friday-backend.service", "friday-bridge.service"}
    for unit in units.values():
        assert f"ExecStart={anchor}/venv/bin/python -I -B -m friday.cli" in unit
        assert "StartLimitBurst=3" in unit
        assert "KillMode=control-group" in unit
        assert "UMask=0077" in unit
        assert "UnsetEnvironment=PYTHONPATH" in unit
        assert "candidate" not in unit


def test_unit_security_dropins_disable_implicit_userns_and_isolate_tmpdir(
    tmp_path: Path,
) -> None:
    port = _systemd_test_port(tmp_path)
    observed: dict[str, bytes] = {}

    for unit in (port.config.backend_unit, port.config.bridge_unit):
        dropins = dict(operator._expected_unit_dropins(port.config, unit))  # noqa: SLF001
        security = dropins[port.config.unit_dir / f"{unit}.d/security.conf"]
        runtime_name = operator._unit_runtime_directory_name(unit)  # noqa: SLF001
        tmp_directory = Path("/run/user") / str(os.geteuid()) / runtime_name
        aggregate_limits = (
            "TasksMax=512\nMemoryMax=12G\nMemorySwapMax=0\n" if unit == port.config.backend_unit else ""
        )
        assert (
            security
            == (
                "[Service]\n"
                "LimitCORE=0\n"
                f"{aggregate_limits}"
                "PrivateTmp=false\n"
                "PrivateUsers=false\n"
                f"RuntimeDirectory={runtime_name}\n"
                "RuntimeDirectoryMode=0700\n"
                "RuntimeDirectoryPreserve=no\n"
                f"Environment=TMPDIR={tmp_directory}\n"
            ).encode()
        )
        assert b"%t" not in security
        observed[unit] = security

    assert observed[port.config.backend_unit] != observed[port.config.bridge_unit]


def test_backend_startup_requires_effective_aggregate_compiler_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _systemd_test_port(tmp_path)
    cgroup_root = tmp_path / "cgroup"
    backend_cgroup = cgroup_root / "user.slice" / "friday-backend.service"
    backend_cgroup.mkdir(parents=True)
    (backend_cgroup / "memory.swap.max").write_text("0\n", encoding="ascii")
    monkeypatch.setattr(operator, "_CGROUP_ROOT", cgroup_root)
    values = {
        "--property=TasksMax": b"512\n",
        "--property=MemoryMax": b"12884901888\n",
        "--property=MemorySwapMax": b"0\n",
        "--property=ControlGroup": b"/user.slice/friday-backend.service\n",
    }

    def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        selected = next((values[item] for item in arguments if item in values), b"")
        return subprocess.CompletedProcess(arguments, 0, selected, b"")

    monkeypatch.setattr(port, "_systemctl", systemctl)
    port._verify_backend_resource_limits()  # noqa: SLF001

    values["--property=MemoryMax"] = b"infinity\n"
    with pytest.raises(operator.ReleaseFailure, match="backend_resource_boundary_unavailable"):
        port._verify_backend_resource_limits()  # noqa: SLF001

    values["--property=MemoryMax"] = b"12884901888\n"
    values["--property=MemorySwapMax"] = b"infinity\n"
    with pytest.raises(operator.ReleaseFailure, match="backend_resource_boundary_unavailable"):
        port._verify_backend_resource_limits()  # noqa: SLF001

    values["--property=MemorySwapMax"] = b"0\n"
    (backend_cgroup / "memory.swap.max").write_text("max\n", encoding="ascii")
    with pytest.raises(operator.ReleaseFailure, match="backend_resource_boundary_unavailable"):
        port._verify_backend_resource_limits()  # noqa: SLF001


def test_backend_cgroup_swap_reader_rejects_traversal_and_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "memory.swap.max").write_text("0\n", encoding="ascii")
    (cgroup_root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(operator, "_CGROUP_ROOT", cgroup_root)

    assert (
        operator.SystemdActivationPort._read_cgroup_v2_leaf(  # noqa: SLF001
            b"/../outside", "memory.swap.max"
        )
        is None
    )
    assert (
        operator.SystemdActivationPort._read_cgroup_v2_leaf(  # noqa: SLF001
            b"/escape", "memory.swap.max"
        )
        is None
    )


@pytest.mark.parametrize("predecessor", ["private-tmp", "recovery", "pre-aggregate", "current"])
def test_unit_surface_admits_only_known_security_predecessors(
    tmp_path: Path,
    predecessor: str,
) -> None:
    tmp_path.chmod(0o700)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir(mode=0o700)
    database = (
        "[Service]\n"
        f"Environment=FRIDAY_DATABASE_PATH={tmp_path / 'state.sqlite3'}\n"
        "Environment=FRIDAY_DATABASE_MUST_EXIST=1\n"
        f"ExecStartPre=/usr/bin/test -s {tmp_path / 'state.sqlite3'}\n"
    ).encode()
    for unit in operator._RUNTIME_UNIT_NAMES:  # noqa: SLF001
        dropin_directory = unit_dir / f"{unit}.d"
        dropin_directory.mkdir(mode=0o700)
        (dropin_directory / "database.conf").write_bytes(database)
        if unit == "friday-bridge.service":
            (dropin_directory / "dependency.conf").write_bytes(
                b"[Unit]\nWants=friday-backend.service\nAfter=friday-backend.service\n"
            )
        security = {
            "private-tmp": operator._LEGACY_PRIVATE_TMP_SECURITY,  # noqa: SLF001
            "recovery": operator._RECOVERY_PRIVATE_TMP_SECURITY,  # noqa: SLF001
            "pre-aggregate": operator._pre_aggregate_unit_security_dropin(unit),  # noqa: SLF001
            "current": operator._unit_security_dropin(unit),  # noqa: SLF001
        }[predecessor]
        (dropin_directory / "security.conf").write_bytes(security)
        for path in dropin_directory.iterdir():
            path.chmod(0o600)

    target, _current = operator._candidate_unit_surface(  # noqa: SLF001
        unit_dir,
        {unit: f"candidate:{unit}".encode() for unit in operator._RUNTIME_UNIT_NAMES},  # noqa: SLF001
    )

    assert tuple(target) == operator._UNIT_SURFACE_KEYS  # noqa: SLF001
    for unit in operator._RUNTIME_UNIT_NAMES:  # noqa: SLF001
        assert target[f"{unit}.d/security.conf"] == operator._unit_security_dropin(unit)  # noqa: SLF001


def test_unit_surface_rejects_unknown_security_before_convergence(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir(mode=0o700)
    database = (
        "[Service]\n"
        f"Environment=FRIDAY_DATABASE_PATH={tmp_path / 'state.sqlite3'}\n"
        "Environment=FRIDAY_DATABASE_MUST_EXIST=1\n"
        f"ExecStartPre=/usr/bin/test -s {tmp_path / 'state.sqlite3'}\n"
    ).encode()
    for unit in operator._RUNTIME_UNIT_NAMES:  # noqa: SLF001
        dropin_directory = unit_dir / f"{unit}.d"
        dropin_directory.mkdir(mode=0o700)
        (dropin_directory / "database.conf").write_bytes(database)
        if unit == "friday-bridge.service":
            (dropin_directory / "dependency.conf").write_bytes(
                b"[Unit]\nWants=friday-backend.service\nAfter=friday-backend.service\n"
            )
        (dropin_directory / "security.conf").write_bytes(
            b"[Service]\nLimitCORE=0\nPrivateTmp=false\nEnvironment=TMPDIR=/tmp\n"
        )
        for path in dropin_directory.iterdir():
            path.chmod(0o600)

    with pytest.raises(operator.ReleaseFailure, match="^installed_security_dropin_invalid$"):
        operator._candidate_unit_surface(  # noqa: SLF001
            unit_dir,
            {unit: f"candidate:{unit}".encode() for unit in operator._RUNTIME_UNIT_NAMES},  # noqa: SLF001
        )


def test_tree_manifest_records_final_artifacts_mode_and_detects_mode_drift(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    artifacts = release_root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "immutable-release.json").write_text("{}\n", encoding="ascii")
    (release_root / "venv").mkdir()
    (release_root / "venv" / "payload").write_text("installed wheel only", encoding="ascii")
    for path in (artifacts / "immutable-release.json", release_root / "venv" / "payload"):
        path.chmod(0o400)
    artifacts.chmod(0o500)
    (release_root / "venv").chmod(0o500)
    release_root.chmod(0o500)
    manifest = artifacts / "release-tree.sha256"
    artifacts.chmod(0o700)
    manifest.write_text(
        "\n".join(operator._manifest_entries(release_root, mode_overrides={"artifacts": 0o500}))  # noqa: SLF001
        + "\n",
        encoding="ascii",
    )
    manifest.chmod(0o400)
    artifacts.chmod(0o500)
    release = operator.ReleaseIdentity(
        release_root,
        "a" * 40,
        "0.205.1",
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
        34,
    )
    operator.verify_release_tree(release)
    artifacts.chmod(0o700)
    with pytest.raises(operator.ReleaseFailure, match="release_tree_changed"):
        operator.verify_release_tree(release)


def test_atomic_anchor_switches_one_shared_symlink_without_touching_release(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir(mode=0o500)
    new.mkdir(mode=0o500)
    anchor = tmp_path / "current"
    anchor.symlink_to(old, target_is_directory=True)
    release = operator.ReleaseIdentity(new, "a" * 40, "0.205.1", "b" * 64, 34)
    operator._atomic_anchor(anchor, release)  # noqa: SLF001
    assert anchor.is_symlink()
    assert anchor.resolve(strict=True) == new.resolve(strict=True)
    assert old.is_dir() and new.is_dir()


def test_activation_is_backup_then_offline_migration_then_backend_first(
    releases: Releases,
) -> None:
    port = FakePort()
    receipt = operator.activate_release(
        port,
        MemoryJournal(),
        candidate=releases.candidate,
        previous=releases.previous,
        schema_capable_fallback=releases.fallback,
    )
    assert receipt["status"] == "clear"
    contour = [
        "stop_bridge",
        "stop_backend",
        "inactive",
        "acquire_leases",
        "leases",
        "backup_db_wal_inbox",
        "offline_migrate:candidate",
        "leases",
        "repair_aliases:candidate",
        "leases",
        "anchor:candidate",
        "release_leases",
        "start_backend:candidate",
        "accept_backend:candidate",
        "start_bridge:candidate",
        "accept_bridge:candidate",
    ]
    assert port.events[-len(contour) :] == contour


def test_activation_allows_a_legacy_previous_but_requires_new_candidate_and_fallback(
    releases: Releases,
) -> None:
    assert releases.previous.venv_relocation_contract == ""
    receipt = operator.activate_release(
        FakePort(),
        MemoryJournal(),
        candidate=releases.candidate,
        previous=releases.previous,
        schema_capable_fallback=releases.fallback,
    )
    assert receipt["status"] == "clear"


@pytest.mark.parametrize(
    ("role", "code"),
    [
        ("candidate", "candidate_venv_relocation_contract_missing"),
        ("fallback", "fallback_venv_relocation_contract_missing"),
    ],
)
def test_activation_rejects_legacy_schema_capable_roles_before_side_effects(
    releases: Releases,
    role: str,
    code: str,
) -> None:
    candidate = releases.candidate
    fallback = releases.fallback
    if role == "candidate":
        candidate = replace(candidate, venv_relocation_contract="")
    else:
        fallback = replace(fallback, venv_relocation_contract="")
    port = FakePort()
    journal = MemoryJournal()
    with pytest.raises(operator.ReleaseFailure, match=f"^{code}$"):
        operator.activate_release(
            port,
            journal,
            candidate=candidate,
            previous=releases.previous,
            schema_capable_fallback=fallback,
        )
    assert port.events == []
    assert journal.events == []


def test_failure_before_network_writer_restores_exact_backup_and_clean_schema33_anchor(
    releases: Releases,
) -> None:
    port = FakePort(fail="anchor:candidate")
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            MemoryJournal(),
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )
    assert "restore_exact_db_wal_inbox" in port.events
    assert "anchor:clean-schema33" in port.events
    assert port.active is releases.previous


@pytest.mark.parametrize("failure_event", ["start_backend:candidate", "accept_backend:candidate"])
def test_backend_start_uncertainty_never_restores_backup_or_runs_schema33(
    releases: Releases,
    failure_event: str,
) -> None:
    port = FakePort(fail=failure_event)
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            MemoryJournal(),
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "anchor:schema34-fallback" in port.events
    assert port.active is releases.fallback


@pytest.mark.parametrize(
    "failure_event",
    ["stop_bridge", "stop_backend", "inactive", "acquire_leases", "leases", "backup_db_wal_inbox"],
)
def test_every_prebackup_failure_restarts_clean_previous_without_database_restore(
    releases: Releases,
    failure_event: str,
) -> None:
    port = FakePort(fail=failure_event)
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            MemoryJournal(),
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "anchor:clean-schema33" in port.events
    assert "start_backend:clean-schema33" in port.events
    assert "start_bridge:clean-schema33" in port.events
    assert port.active is releases.previous


def test_obsidian_enable_prebackup_failure_keeps_disabled_env_before_previous_acceptance(
    releases: Releases,
) -> None:
    class ModeBoundPort(FakePort):
        def accept_backend(self, release: operator.ReleaseIdentity) -> None:
            if release is releases.previous:
                assert self.obsidian_mode == "disabled"
                assert self.predecessor_env_sha256 == "7" * 64
            super().accept_backend(release)

    port = ModeBoundPort(fail="backup_db_wal_inbox")
    journal = MemoryJournal(
        prebackup_config_transition="obsidian_enable",
        predecessor_env_sha256="7" * 64,
        next_env_file=Path("/private-state/next.env"),
        next_env_file_sha256="9" * 64,
    )
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )

    assert "activate_staged_config" not in port.events
    assert port.events.index("select_predecessor_config") < port.events.index("start_backend:clean-schema33")
    assert port.canonical_env_sha256 == "7" * 64
    assert journal.state["backup"] is None
    assert journal.state["database_mutation_possible"] is False
    assert journal.state["writer_target"] == "previous"
    assert port.active is releases.previous


def test_obsidian_enable_prebackup_recovery_keeps_disabled_env_before_restart(
    releases: Releases,
) -> None:
    class ModeBoundPort(FakePort):
        def accept_backend(self, release: operator.ReleaseIdentity) -> None:
            if release is releases.previous:
                assert self.obsidian_mode == "disabled"
                assert self.predecessor_env_sha256 == "8" * 64
            super().accept_backend(release)

    journal = MemoryJournal(
        prebackup_config_transition="obsidian_enable",
        predecessor_env_sha256="8" * 64,
        next_env_file=Path("/private-state/next.env"),
        next_env_file_sha256="a" * 64,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    journal.record("bridge_stop_attempted")
    journal.record("backend_stop_attempted")
    port = ModeBoundPort()

    receipt = operator.recover_interrupted_activation(port, journal)

    assert receipt["status"] == "recovered"
    assert "activate_staged_config" not in port.events
    assert port.events.index("select_predecessor_config") < port.events.index("start_backend:clean-schema33")
    assert port.canonical_env_sha256 == "8" * 64
    assert journal.state["phase"] == "recovered"
    assert journal.state["backup"] is None
    assert journal.state["database_mutation_possible"] is False
    assert journal.state["writer_target"] == "previous"
    assert port.active is releases.previous


def test_obsidian_enable_activates_staged_env_only_after_verified_backup(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="obsidian_enable",
        predecessor_env_sha256="1" * 64,
        next_env_file=Path("/private-state/next.env"),
        next_env_file_sha256="2" * 64,
    )

    class BoundaryPort(FakePort):
        def backup_database(self, release: operator.ReleaseIdentity) -> operator.DatabaseBackup:
            assert self.canonical_env_sha256 == "1" * 64
            assert journal.state["phase"] == "leases_acquired"
            return super().backup_database(release)

        def offline_migrate(
            self,
            release: operator.ReleaseIdentity,
            backup: operator.DatabaseBackup,
        ) -> None:
            assert self.canonical_env_sha256 == "2" * 64
            assert journal.state["phase"] == "migration_attempted"
            super().offline_migrate(release, backup)

    port = BoundaryPort()
    receipt = operator.activate_release(
        port,
        journal,
        candidate=releases.candidate,
        previous=releases.previous,
        schema_capable_fallback=releases.fallback,
    )

    assert receipt["status"] == "clear"
    assert journal.events.index("backup_complete") < journal.events.index("environment_swap_attempted")
    assert journal.events.index("environment_swap_attempted") < journal.events.index("environment_active")
    assert journal.events.index("environment_active") < journal.events.index("migration_attempted")
    assert port.events.index("backup_db_wal_inbox") < port.events.index("activate_staged_config")
    assert port.canonical_env_sha256 == "2" * 64


@pytest.mark.parametrize(
    ("phase", "canonical_sha256"),
    [
        ("backup_complete", "3" * 64),
        ("environment_swap_attempted", "3" * 64),
        ("environment_swap_attempted", "4" * 64),
    ],
)
def test_obsidian_postbackup_recovery_converges_staged_env_before_writer_restart(
    releases: Releases,
    phase: str,
    canonical_sha256: str,
) -> None:
    next_env_file = Path("/private-state/next.env")
    journal = MemoryJournal(
        prebackup_config_transition="obsidian_enable",
        predecessor_env_sha256="3" * 64,
        next_env_file=next_env_file,
        next_env_file_sha256="4" * 64,
    )
    capable_previous = replace(
        releases.previous,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=capable_previous,
        fallback=releases.fallback,
    )
    journal.record("backup_complete", backup=FakePort().backup)
    if phase == "environment_swap_attempted":
        journal.record(phase, backup=journal.database_backup())
    port = FakePort()
    port.predecessor_env_sha256 = "3" * 64
    port.canonical_env_sha256 = canonical_sha256
    port.next_env_file = next_env_file
    port.next_env_file_sha256 = "4" * 64

    receipt = operator.recover_interrupted_activation(port, journal)

    assert receipt["status"] == "recovered"
    assert port.canonical_env_sha256 == "4" * 64
    assert port.events.index("activate_staged_config") < port.events.index("start_backend:clean-schema33")
    assert port.active is capable_previous


def test_secondary_shadow_prebackup_rollback_keeps_disabled_environment(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="secondary_shadow_enable",
        predecessor_env_sha256="5" * 64,
        next_env_file=Path("/private-state/secondary-shadow.env"),
        next_env_file_sha256="6" * 64,
    )
    port = FakePort(
        fail="backup_db_wal_inbox",
        staged_config_transition="secondary_shadow_enable",
    )

    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )

    assert "activate_staged_config" not in port.events
    assert port.canonical_env_sha256 == "5" * 64
    assert journal.state["backup"] is None
    assert port.active is releases.previous


def test_secondary_shadow_postbackup_recovery_converges_to_enabled_environment(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="secondary_shadow_enable",
        predecessor_env_sha256="7" * 64,
        next_env_file=Path("/private-state/secondary-shadow.env"),
        next_env_file_sha256="8" * 64,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    journal.record("backup_complete", backup=FakePort().backup)
    port = FakePort(staged_config_transition="secondary_shadow_enable")
    port.predecessor_env_sha256 = "7" * 64
    port.canonical_env_sha256 = "7" * 64

    receipt = operator.recover_interrupted_activation(port, journal)

    assert receipt["status"] == "recovered"
    assert port.canonical_env_sha256 == "8" * 64
    assert port.events.index("activate_staged_config") < port.events.index("start_backend:schema34-fallback")
    assert port.active is releases.fallback


def test_secondary_shadow_disable_prebackup_rollback_keeps_enabled_environment(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="secondary_shadow_disable",
        predecessor_env_sha256="9" * 64,
        next_env_file=Path("/private-state/secondary-shadow-disabled.env"),
        next_env_file_sha256="a" * 64,
    )
    port = FakePort(
        fail="backup_db_wal_inbox",
        staged_config_transition="secondary_shadow_disable",
    )

    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )

    assert "activate_staged_config" not in port.events
    assert port.canonical_env_sha256 == "9" * 64
    assert journal.state["backup"] is None
    assert port.active is releases.previous


def test_secondary_shadow_disable_postbackup_recovery_converges_to_disabled_environment(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="secondary_shadow_disable",
        predecessor_env_sha256="b" * 64,
        next_env_file=Path("/private-state/secondary-shadow-disabled.env"),
        next_env_file_sha256="c" * 64,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    journal.record("backup_complete", backup=FakePort().backup)
    port = FakePort(staged_config_transition="secondary_shadow_disable")
    port.predecessor_env_sha256 = "b" * 64
    port.canonical_env_sha256 = "b" * 64

    receipt = operator.recover_interrupted_activation(port, journal)

    assert receipt["status"] == "recovered"
    assert port.canonical_env_sha256 == "c" * 64
    assert port.events.index("activate_staged_config") < port.events.index("start_backend:schema34-fallback")
    assert port.active is releases.fallback


def test_failure_after_bridge_start_never_runs_schema33_and_uses_schema34_fallback(
    releases: Releases,
) -> None:
    port = FakePort(fail="accept_bridge:candidate")
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            MemoryJournal(),
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "anchor:clean-schema33" not in port.events
    assert "anchor:schema34-fallback" in port.events
    assert port.active is releases.fallback


def _engineer_lifecycle_releases(releases: Releases) -> Releases:
    capable = {
        "max_schema": operator.ENGINEER_COMMAND_LIFECYCLE_MIN_SCHEMA,
        "obsidian_cutover_contract": operator.OBSIDIAN_CUTOVER_CONTRACT,
        "engineer_command_lifecycle_contract": operator.ENGINEER_COMMAND_LIFECYCLE_CONTRACT,
    }
    return Releases(
        candidate=replace(releases.candidate, version="0.207.66", **capable),
        previous=replace(
            releases.previous,
            version="0.207.65",
            max_schema=operator.ENGINEER_COMMAND_LIFECYCLE_MIN_SCHEMA,
            obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
        ),
        fallback=replace(releases.fallback, version="0.207.66rc0", **capable),
    )


@pytest.mark.parametrize(
    ("role", "code"),
    [
        ("candidate", "candidate_engineer_lifecycle_contract_missing"),
        ("fallback", "fallback_engineer_lifecycle_contract_missing"),
    ],
)
def test_engineer_provision_requires_two_distinct_lifecycle_capable_artifacts_before_stop(
    releases: Releases,
    role: str,
    code: str,
) -> None:
    capable = _engineer_lifecycle_releases(releases)
    candidate = capable.candidate
    fallback = capable.fallback
    if role == "candidate":
        candidate = replace(candidate, engineer_command_lifecycle_contract="")
    else:
        fallback = replace(fallback, engineer_command_lifecycle_contract="")
    port = FakePort(engineer_lifecycle_required=True, backup_schema=46)
    journal = MemoryJournal()
    with pytest.raises(operator.ReleaseFailure, match=f"^{code}$"):
        operator.activate_release(
            port,
            journal,
            candidate=candidate,
            previous=capable.previous,
            schema_capable_fallback=fallback,
        )
    assert port.events == []
    assert journal.events == []


def test_preprovisioned_engineer_authority_rejects_pre_lifecycle_previous_before_stop(
    releases: Releases,
) -> None:
    capable = _engineer_lifecycle_releases(releases)
    port = FakePort(
        engineer_lifecycle_required=True,
        engineer_lifecycle_provisioned=True,
        backup_schema=46,
    )
    journal = MemoryJournal()
    with pytest.raises(
        operator.ReleaseFailure,
        match="previous_engineer_lifecycle_contract_missing",
    ):
        operator.activate_release(
            port,
            journal,
            candidate=capable.candidate,
            previous=capable.previous,
            schema_capable_fallback=capable.fallback,
        )
    assert port.events == []
    assert journal.events == []


def test_engineer_recovery_contour_failure_occurs_before_journal_or_service_stop(
    releases: Releases,
) -> None:
    class OverlapPort(FakePort):
        def validate_engineer_recovery_contour(
            self,
            releases: tuple[operator.ReleaseIdentity, ...],
        ) -> None:
            del releases
            self._event("validate_engineer_contour")
            raise operator.ReleaseFailure("engineer_recovery_contour_overlap")

    port = OverlapPort(engineer_lifecycle_required=True, backup_schema=46)
    journal = MemoryJournal()
    with pytest.raises(
        operator.ReleaseFailure,
        match="engineer_recovery_contour_overlap",
    ):
        operator.activate_release(
            port,
            journal,
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )
    assert port.events == ["validate_engineer_contour"]
    assert journal.events == []


def test_engineer_failure_before_write_ahead_boundary_restores_pre_lifecycle_set(
    releases: Releases,
) -> None:
    capable = _engineer_lifecycle_releases(releases)

    class PreBoundaryJournal(MemoryJournal):
        def record(self, phase: str, **kwargs: object) -> None:
            if phase == "provision_committed":
                raise RuntimeError("synthetic failure before durable boundary")
            super().record(phase, **kwargs)

    port = FakePort(
        engineer_lifecycle_required=True,
        backup_schema=46,
    )
    journal = PreBoundaryJournal()
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=capable.candidate,
            previous=capable.previous,
            schema_capable_fallback=capable.fallback,
        )
    assert journal.events.index("provision_attempted") < journal.events.index("rollback_restore_attempted")
    assert journal.state["engineer_provision_committed"] is False
    assert "provision_engineer:candidate" not in port.events
    assert "restore_exact_db_wal_inbox" in port.events
    assert port.active is capable.previous


def test_engineer_failure_during_provision_converges_with_capable_fallback(
    releases: Releases,
) -> None:
    capable = _engineer_lifecycle_releases(releases)
    port = FakePort(
        fail="provision_engineer:candidate",
        engineer_lifecycle_required=True,
        backup_schema=46,
    )
    journal = MemoryJournal()
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=capable.candidate,
            previous=capable.previous,
            schema_capable_fallback=capable.fallback,
        )
    assert journal.state["engineer_provision_committed"] is True
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "provision_engineer:schema34-fallback" in port.events
    assert port.active is capable.fallback


@pytest.mark.parametrize("failure_point", ["after_boundary", "anchor:candidate"])
def test_durable_engineer_provision_boundary_never_starts_pre_lifecycle_previous(
    releases: Releases,
    failure_point: str,
) -> None:
    capable = _engineer_lifecycle_releases(releases)

    class BoundaryJournal(MemoryJournal):
        def record(self, phase: str, **kwargs: object) -> None:
            super().record(phase, **kwargs)
            if phase == "provision_committed" and failure_point == "after_boundary":
                raise RuntimeError("crash after durable journal replace")

    port = FakePort(
        fail=("anchor:candidate" if failure_point == "anchor:candidate" else ""),
        engineer_lifecycle_required=True,
        backup_schema=46,
    )
    journal = BoundaryJournal()
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=capable.candidate,
            previous=capable.previous,
            schema_capable_fallback=capable.fallback,
        )
    assert journal.state["engineer_provision_committed"] is True
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "anchor:clean-schema33" not in port.events
    assert "provision_engineer:schema34-fallback" in port.events
    assert port.active is capable.fallback


@pytest.mark.parametrize(
    ("phase", "committed", "expected_target", "restore_expected"),
    [
        ("provision_attempted", False, "previous", True),
        ("provision_committed", True, "fallback", False),
    ],
)
def test_interrupted_engineer_provision_recovers_across_exact_one_way_boundary(
    releases: Releases,
    phase: str,
    committed: bool,
    expected_target: str,
    restore_expected: bool,
) -> None:
    capable = _engineer_lifecycle_releases(releases)
    journal = MemoryJournal()
    journal.begin(
        candidate=capable.candidate,
        previous=capable.previous,
        fallback=capable.fallback,
    )
    for current in (
        "bridge_stop_attempted",
        "backend_stop_attempted",
        "writers_quiesced",
        "leases_acquired",
        "backup_complete",
        "migration_attempted",
        "alias_repair_attempted",
        phase,
    ):
        journal.record(
            current,
            backup=(FakePort(backup_schema=46).backup if current == "backup_complete" else None),
            database_mutation_possible=current in {"migration_attempted", "alias_repair_attempted", phase},
        )
    assert journal.state["engineer_provision_committed"] is committed
    port = FakePort(engineer_lifecycle_required=True, backup_schema=46)
    stored_backup = journal.database_backup()
    assert isinstance(stored_backup, operator.DatabaseBackup)
    port.backup = stored_backup
    receipt = operator.recover_interrupted_activation(port, journal)
    expected = capable.previous if expected_target == "previous" else capable.fallback
    assert port.active is expected
    assert ("restore_exact_db_wal_inbox" in port.events) is restore_expected
    assert ("provision_engineer:schema34-fallback" in port.events) is committed
    assert receipt["engineer_provision_committed"] is committed
    assert receipt["backup_restored"] is restore_expected


def test_known_mutated_live_release_is_forbidden_as_rollback(
    tmp_path: Path,
    releases: Releases,
) -> None:
    corrupt = _release(
        tmp_path,
        "corrupt",
        schema=33,
        commit="8345179af57a71cc6a64916c275cce5627abfd63",
    )
    port = FakePort()
    with pytest.raises(operator.ReleaseFailure, match="forbidden_corrupt_rollback_release"):
        operator.activate_release(
            port,
            MemoryJournal(),
            candidate=releases.candidate,
            previous=corrupt,
            schema_capable_fallback=releases.fallback,
        )
    assert "stop_bridge" not in port.events


def test_exact_database_and_inbox_backup_restores_schema33_bytes_before_bridge(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    database = tmp_path / "friday.sqlite3"
    inbox = tmp_path / "telegram.sqlite3"
    main = sqlite3.connect(database)
    main.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    main.execute("INSERT INTO schema_meta VALUES('schema_version','33')")
    main.execute("CREATE TABLE durable(value TEXT NOT NULL)")
    main.execute("INSERT INTO durable VALUES('before')")
    main.commit()
    main.close()
    incoming = sqlite3.connect(inbox)
    incoming.execute("CREATE TABLE queue(value TEXT NOT NULL)")
    incoming.execute("INSERT INTO queue VALUES('pending-before')")
    incoming.commit()
    incoming.close()
    database.chmod(0o600)
    inbox.chmod(0o600)
    health_ca = tmp_path / "health-ca.pem"
    health_ca.write_text("synthetic test CA", encoding="ascii")
    health_ca.chmod(0o600)
    config = operator.SystemdConfig(
        anchor=tmp_path / "anchor",
        env_file=tmp_path / "env",
        env_file_sha256="0" * 64,
        friday_home=tmp_path,
        unit_dir=tmp_path / "units",
        database=database,
        inbox_database=inbox,
        backup_dir=tmp_path / "backups",
        state_dir=tmp_path / "state",
        health_ca=health_ca,
        health_ca_sha256=hashlib.sha256(health_ca.read_bytes()).hexdigest(),
    )
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    journal_state = tmp_path / "journal-state"
    journal_state.mkdir(mode=0o700)
    journal_path = journal_state / "immutable-release-activation.v1.json"
    config_identity = operator._systemd_config_identity(config)  # noqa: SLF001
    journal = operator.DurableActivationJournal(
        journal_path,
        backup_root=config.backup_dir,
        config_identity_sha256=config_identity,
    )
    candidate = _release(tmp_path, "journal-candidate", schema=34, commit="c" * 40)
    previous = _release(tmp_path, "journal-previous", schema=33, commit="a" * 40)
    fallback = _release(tmp_path, "journal-fallback", schema=34, commit="f" * 40)
    journal.begin(candidate=candidate, previous=previous, fallback=fallback)
    for phase in (
        "bridge_stop_attempted",
        "backend_stop_attempted",
        "writers_quiesced",
        "leases_acquired",
        "backup_complete",
    ):
        journal.record(phase, backup=backup if phase == "backup_complete" else None)
    journal.record("migration_attempted", backup=backup, database_mutation_possible=True)
    recovered_backup = operator.DurableActivationJournal(
        journal_path,
        backup_root=config.backup_dir,
        config_identity_sha256=config_identity,
    ).database_backup()
    assert recovered_backup is not None
    main = sqlite3.connect(database)
    main.execute("UPDATE durable SET value='migrated'")
    main.commit()
    main.close()
    incoming = sqlite3.connect(inbox)
    incoming.execute("DELETE FROM queue")
    incoming.commit()
    incoming.close()
    database.chmod(0o600)
    inbox.chmod(0o600)
    operator._restore_exact_sqlite_backup(config, recovered_backup)  # noqa: SLF001
    main = sqlite3.connect(database)
    incoming = sqlite3.connect(inbox)
    try:
        assert main.execute("SELECT value FROM durable").fetchone()[0] == "before"
        assert incoming.execute("SELECT value FROM queue").fetchone()[0] == "pending-before"
    finally:
        main.close()
        incoming.close()
    journal_payload = json.loads(journal_path.read_text(encoding="ascii"))
    journal_payload["backup"]["receipt_sha256"] = "e" * 64
    journal_core = {key: value for key, value in journal_payload.items() if key != "journal_sha256"}
    journal_payload["journal_sha256"] = hashlib.sha256(
        operator._canonical_json(journal_core)  # noqa: SLF001
    ).hexdigest()
    journal_path.write_text(json.dumps(journal_payload), encoding="ascii")
    journal_path.chmod(0o600)
    with pytest.raises(operator.ReleaseFailure, match="backup_receipt_mismatch"):
        operator.DurableActivationJournal(
            journal_path,
            backup_root=config.backup_dir,
            config_identity_sha256=config_identity,
        ).database_backup()


def _engineer_recovery_config(tmp_path: Path) -> operator.SystemdConfig:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database = tmp_path / "friday.sqlite3"
    inbox = state / "telegram-inbox.sqlite3"
    main = sqlite3.connect(database)
    main.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    main.execute("INSERT INTO schema_meta VALUES('schema_version','46')")
    main.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    main.execute("INSERT INTO marker VALUES('before')")
    main.commit()
    main.close()
    incoming = sqlite3.connect(inbox)
    incoming.execute("CREATE TABLE queue(value TEXT NOT NULL)")
    incoming.execute("INSERT INTO queue VALUES('pending-before')")
    incoming.commit()
    incoming.close()
    database.chmod(0o600)
    inbox.chmod(0o600)
    health_ca = tmp_path / "health-ca.pem"
    health_ca.write_text("synthetic test CA", encoding="ascii")
    health_ca.chmod(0o600)
    return operator.SystemdConfig(
        anchor=tmp_path / "anchor",
        env_file=tmp_path / "env",
        env_file_sha256="0" * 64,
        friday_home=tmp_path,
        unit_dir=tmp_path / "units",
        database=database,
        inbox_database=inbox,
        backup_dir=tmp_path / "backups",
        state_dir=state,
        health_ca=health_ca,
        health_ca_sha256=hashlib.sha256(health_ca.read_bytes()).hexdigest(),
    )


def _provision_test_engineer_store(
    config: operator.SystemdConfig,
) -> tuple[Path, Path, bytes]:
    from friday.organs.engineer.command.store import CommandJobStore

    root = config.friday_home / "data" / "engineer-command"
    root.parent.mkdir(mode=0o700)
    key = config.friday_home / "data" / "engineer-command.key"
    key_bytes = b"k" * 32
    key.write_bytes(key_bytes)
    key.chmod(0o600)
    lifecycle_key = b"l" * 32
    store = CommandJobStore.provision(
        root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=config.state_dir,
    )
    store.close()
    job = root / "jobs" / "job-a"
    job.mkdir(mode=0o700)
    result = job / "private-result-do-not-log.txt"
    result.write_bytes(b"sealed-result-before")
    result.chmod(0o600)
    workbench = root / "workbenches" / "scope-a"
    workbench.mkdir(mode=0o700)
    source = workbench / "source.bin"
    source.write_bytes(b"source-before")
    source.chmod(0o600)
    return root, key, lifecycle_key


@pytest.mark.parametrize(
    "overlap",
    [
        "backup_inside_store",
        "backup_ancestor_of_store",
        "main_database_inside_store",
        "inbox_inside_store",
        "release_root_is_store",
        "key_hardlinks_main_database",
        "key_hardlinks_state_recovery",
        "backup_nested_symlink_to_engineer_artifact",
        "release_nested_symlink_to_engineer_artifact",
        "main_wal_symlink_to_engineer_artifact",
        "inbox_journal_symlink_to_engineer_artifact",
        "backup_root_symlink_to_store",
    ],
)
def test_engineer_recovery_contour_rejects_every_namespace_or_inode_overlap_pre_stop(
    tmp_path: Path,
    overlap: str,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, key, _lifecycle_key = _provision_test_engineer_store(config)
    release_roots = (
        tmp_path / "releases" / "candidate",
        tmp_path / "releases" / "previous",
        tmp_path / "releases" / "fallback",
    )
    if overlap == "backup_inside_store":
        config = replace(config, backup_dir=root / "recursive-backups")
    elif overlap == "backup_ancestor_of_store":
        config = replace(config, backup_dir=root.parent)
    elif overlap == "main_database_inside_store":
        config = replace(config, database=root / "kernel.sqlite")
    elif overlap == "inbox_inside_store":
        config = replace(config, inbox_database=root / "inbox.sqlite3")
    elif overlap == "release_root_is_store":
        release_roots = (root, *release_roots[1:])
    elif overlap == "key_hardlinks_main_database":
        key.unlink()
        os.link(config.database, key)
    elif overlap == "key_hardlinks_state_recovery":
        recovery = config.state_dir / "immutable-release-activation.v1.json"
        recovery.write_bytes(b"private-recovery-state")
        recovery.chmod(0o600)
        key.unlink()
        os.link(recovery, key)
    elif overlap == "backup_nested_symlink_to_engineer_artifact":
        config.backup_dir.mkdir(mode=0o700)
        (config.backup_dir / "nested-secret-alias").symlink_to(
            root / "jobs" / "job-a" / "private-result-do-not-log.txt"
        )
    elif overlap == "release_nested_symlink_to_engineer_artifact":
        release_roots[0].mkdir(parents=True)
        (release_roots[0] / "nested-secret-alias").symlink_to(
            root / "jobs" / "job-a" / "private-result-do-not-log.txt"
        )
    elif overlap == "main_wal_symlink_to_engineer_artifact":
        Path(f"{config.database}-wal").symlink_to(root / "jobs" / "job-a" / "private-result-do-not-log.txt")
    elif overlap == "inbox_journal_symlink_to_engineer_artifact":
        Path(f"{config.inbox_database}-journal").symlink_to(
            root / "jobs" / "job-a" / "private-result-do-not-log.txt"
        )
    else:
        config.backup_dir.symlink_to(root, target_is_directory=True)

    with pytest.raises(
        operator.ReleaseFailure,
        match="engineer_recovery_contour_(overlap|invalid|inode_alias)",
    ):
        operator._validate_engineer_recovery_contour(  # noqa: SLF001
            config,
            release_roots,
        )


def test_engineer_recovery_set_restores_authenticated_store_in_place_without_secret_journal(
    tmp_path: Path,
) -> None:
    from friday.organs.engineer.command.store import CommandJobStore

    config = _engineer_recovery_config(tmp_path)
    root, key, lifecycle_key = _provision_test_engineer_store(config)
    database_inode = (root / "kernel.sqlite").stat().st_ino
    anchor_before = (config.state_dir / "engineer-command-store.anchor.json").read_bytes()
    result = root / "jobs" / "job-a" / "private-result-do-not-log.txt"
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001

    journal = operator.DurableActivationJournal(
        config.state_dir / "immutable-release-activation.v1.json",
        backup_root=config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(config),  # noqa: SLF001
    )
    journal.begin(
        candidate=_release(tmp_path, "engineer-journal-candidate", schema=46, commit="c" * 40),
        previous=_release(tmp_path, "engineer-journal-previous", schema=46, commit="a" * 40),
        fallback=_release(tmp_path, "engineer-journal-fallback", schema=46, commit="f" * 40),
    )
    for phase in (
        "bridge_stop_attempted",
        "backend_stop_attempted",
        "writers_quiesced",
        "leases_acquired",
        "backup_complete",
    ):
        journal.record(phase, backup=backup if phase == "backup_complete" else None)
    journal_bytes = journal.path.read_bytes()
    assert b"private-result-do-not-log" not in journal_bytes
    assert b"sealed-result-before" not in journal_bytes
    assert b"k" * 32 not in journal_bytes

    runtime = CommandJobStore.open_runtime(
        root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=config.state_dir,
    )
    with runtime.transaction():
        pass
    runtime.close()
    result.write_bytes(b"changed")
    result.chmod(0o600)
    extra = root / "jobs" / "job-a" / "extra.bin"
    extra.write_bytes(b"extra")
    extra.chmod(0o600)
    key.write_bytes(b"z" * 32)
    key.chmod(0o600)

    operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert (root / "kernel.sqlite").stat().st_ino == database_inode
    assert result.read_bytes() == b"sealed-result-before"
    assert not extra.exists()
    assert key.read_bytes() == b"k" * 32
    assert (config.state_dir / "engineer-command-store.anchor.json").read_bytes() == anchor_before
    assert (root / "kernel.lock").read_bytes() == b""
    assert (root / "kernel.lease").read_bytes() == b""
    reopened = CommandJobStore.open_runtime(
        root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=config.state_dir,
    )
    reopened.assert_lifecycle_ready()
    reopened.close()


def test_engineer_recovery_set_covers_every_lifecycle_crash_artifact(
    tmp_path: Path,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    _provision_test_engineer_store(config)
    names = (
        "engineer-command-store.anchor.json",
        "engineer-command-store.bootstrap.json",
        "engineer-command-store.pending.json",
        "engineer-command-store.committed.json",
    )
    for position, name in enumerate(names, start=1):
        path = config.state_dir / name
        if name != "engineer-command-store.anchor.json":
            path.write_bytes(f"private-crash-artifact-{position}".encode())
            path.chmod(0o600)
    before = {name: (config.state_dir / name).read_bytes() for name in names}
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    for position, name in enumerate(names):
        path = config.state_dir / name
        if position % 2:
            path.unlink()
        else:
            path.write_bytes(b"drift")
            path.chmod(0o600)

    operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert {name: (config.state_dir / name).read_bytes() for name in names} == before


def test_engineer_restore_replay_cleans_manifest_bound_secret_staging_exhaustively(
    tmp_path: Path,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, key, _lifecycle_key = _provision_test_engineer_store(config)
    result = root / "jobs" / "job-a" / "private-result-do-not-log.txt"
    anchor = config.state_dir / "engineer-command-store.anchor.json"
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    assert payload.engineer is not None
    token = payload.engineer.manifest_sha256
    stages = (
        operator._engineer_restore_stage_path(key, token),  # noqa: SLF001
        operator._engineer_restore_stage_path(anchor, token),  # noqa: SLF001
        operator._engineer_restore_stage_path(result, token),  # noqa: SLF001
    )
    assert stages[0] == operator._engineer_restore_stage_path(key, token)  # noqa: SLF001
    assert stages[0] != operator._engineer_restore_stage_path(key, "e" * 64)  # noqa: SLF001
    for position, stage in enumerate(stages, start=1):
        stage.write_bytes(f"partial-secret-crash-{position}".encode())
        stage.chmod(0o600)
    key.write_bytes(b"z" * 32)
    key.chmod(0o600)
    result.write_bytes(b"changed-after-crash")
    result.chmod(0o600)
    anchor.write_bytes(b"changed-anchor")
    anchor.chmod(0o600)

    operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert key.read_bytes() == b"k" * 32
    assert result.read_bytes() == b"sealed-result-before"
    assert operator._engineer_restore_staging_paths(config) == ()  # noqa: SLF001
    assert not any(stage.exists() or stage.is_symlink() for stage in stages)
    for directory in (key.parent, config.state_dir, result.parent):
        assert not any(
            operator._ENGINEER_RESTORE_STAGE_RE.fullmatch(path.name)  # noqa: SLF001
            for path in directory.iterdir()
        )


def test_engineer_restore_refuses_unbound_secret_stage_before_any_live_mutation(
    tmp_path: Path,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, key, _lifecycle_key = _provision_test_engineer_store(config)
    result = root / "jobs" / "job-a" / "private-result-do-not-log.txt"
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    unbound = operator._engineer_restore_stage_path(key, "f" * 64)  # noqa: SLF001
    unbound.write_bytes(b"unbound-secret-residue")
    unbound.chmod(0o600)
    key.write_bytes(b"z" * 32)
    key.chmod(0o600)
    result.write_bytes(b"changed-after-backup")
    result.chmod(0o600)

    with pytest.raises(operator.ReleaseFailure, match="engineer_restore_staging_unbound"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert key.read_bytes() == b"z" * 32
    assert result.read_bytes() == b"changed-after-backup"
    assert unbound.read_bytes() == b"unbound-secret-residue"


def test_engineer_restore_ephemeral_race_rejects_hardlinked_replacement_before_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    target = root / "kernel.lease"
    unrelated = tmp_path / "unrelated-private-file"
    unrelated.write_bytes(b"must-not-be-truncated-or-repermissioned")
    unrelated.chmod(0o640)
    original_mode = stat.S_IMODE(unrelated.stat().st_mode)
    original_open = operator.os.open
    injected = False

    def raced_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if not injected and path == target.name and dir_fd is not None:
            injected = True
            target.unlink()
            os.link(unrelated, target)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(operator.os, "open", raced_open)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_path_drift"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert injected is True
    assert unrelated.read_bytes() == b"must-not-be-truncated-or-repermissioned"
    assert stat.S_IMODE(unrelated.stat().st_mode) == original_mode == 0o640


def test_engineer_restore_missing_ephemeral_uses_exclusive_nontruncating_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    target = root / "kernel.lease"
    target.unlink()
    original_open = operator.os.open
    observed_flags: list[int] = []

    def inspect_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == target.name and dir_fd is not None:
            observed_flags.append(flags)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(operator.os, "open", inspect_open)
    operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    mutating_flags = [flags for flags in observed_flags if flags & os.O_RDWR]
    assert len(mutating_flags) == 1
    assert mutating_flags[0] & os.O_EXCL
    assert not mutating_flags[0] & os.O_TRUNC
    assert target.read_bytes() == b""
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_engineer_exact_restore_ephemeral_rejects_real_store_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    victim = tmp_path / "ephemeral-store-victim"
    victim.mkdir(mode=0o700)
    victim_target = victim / "kernel.lease"
    victim_target.write_bytes(b"must-not-be-truncated")
    victim_target.chmod(0o600)
    moved = tmp_path / "engineer-store-before-ephemeral-swap"
    original_restore = operator._restore_ephemeral_engineer_file  # noqa: SLF001
    injected = False

    def raced_restore(
        target: Path,
        *,
        mode: int,
        expected_present: bool,
        contained: bool,
        pinned_parent: tuple[int, operator._EngineerDirectoryAncestry] | None = None,  # noqa: SLF001
    ) -> None:
        nonlocal injected
        if not injected:
            root.rename(moved)
            victim.rename(root)
            injected = True
        original_restore(
            target,
            mode=mode,
            expected_present=expected_present,
            contained=contained,
            pinned_parent=pinned_parent,
        )

    monkeypatch.setattr(operator, "_restore_ephemeral_engineer_file", raced_restore)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_path_drift"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert injected is True
    assert (root / "kernel.lease").read_bytes() == b"must-not-be-truncated"


def test_engineer_exact_restore_binds_store_seen_by_live_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    victim = tmp_path / "post-scan-store-victim"
    shutil.copytree(root, victim)
    victim_result = victim / "jobs" / "job-a" / "private-result-do-not-log.txt"
    victim_result.write_bytes(b"must-survive-post-scan-swap")
    victim_result.chmod(0o600)
    moved = tmp_path / "engineer-store-seen-by-scan"
    original_scan = operator._scan_engineer_artifacts  # noqa: SLF001
    injected = False

    def raced_scan(
        candidate_config: operator.SystemdConfig,
        *,
        destination: Path | None,
    ) -> dict[str, object]:
        nonlocal injected
        observed = original_scan(candidate_config, destination=destination)
        if destination is None and not injected:
            root.rename(moved)
            victim.rename(root)
            injected = True
        return observed

    monkeypatch.setattr(operator, "_scan_engineer_artifacts", raced_scan)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_path_drift"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert injected is True
    assert (root / "jobs" / "job-a" / "private-result-do-not-log.txt").read_bytes() == (
        b"must-survive-post-scan-swap"
    )


def test_engineer_staging_cleanup_opens_fifo_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    assert payload.engineer is not None
    manifest = operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        payload.engineer,
    )
    target = root / "jobs" / "job-a" / "private-result-do-not-log.txt"
    stage = operator._engineer_restore_stage_path(  # noqa: SLF001
        target,
        payload.engineer.manifest_sha256,
    )
    os.mkfifo(stage, mode=0o600)
    original_open = operator.os.open
    inspected = False

    def inspect_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal inspected
        if path == stage.name and dir_fd is not None:
            inspected = True
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(operator.os, "open", inspect_open)
    with pytest.raises(operator.ReleaseFailure, match="engineer_restore_staging_invalid"):
        operator._cleanup_engineer_restore_staging(  # noqa: SLF001
            config,
            manifest=manifest,
            manifest_sha256=payload.engineer.manifest_sha256,
        )

    assert inspected is True


def test_engineer_backup_byte_reauthentication_does_not_mutate_backup_namespace(
    tmp_path: Path,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    assert payload.engineer is not None
    before = config.backup_dir.stat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_uid,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    before_names = {path.name for path in config.backup_dir.iterdir()}

    operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        payload.engineer,
        verify_sqlite_integrity=False,
    )

    after = config.backup_dir.stat()
    assert (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_uid,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == before_identity
    assert {path.name for path in config.backup_dir.iterdir()} == before_names


@pytest.mark.parametrize("surface", ["key", "state", "nested_store"])
def test_engineer_restore_publish_pins_every_parent_against_namespace_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "sealed-backup"
    source.write_bytes(b"secret-backup")
    source.chmod(0o600)
    if surface == "key":
        parent = tmp_path / "data"
        contained = False
        name = "engineer-command.key"
    elif surface == "state":
        parent = tmp_path / "state"
        contained = False
        name = "engineer-command-store.anchor.json"
    else:
        store = tmp_path / "store"
        jobs = store / "jobs"
        parent = jobs / "job-a"
        contained = True
        name = "result.bin"
        parent.mkdir(parents=True, mode=0o700)
        for directory in (store, jobs, parent):
            directory.chmod(0o700)
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    target = parent / name
    target.write_bytes(b"live-target")
    target.chmod(0o600)
    victim = tmp_path / f"victim-{surface}"
    victim.mkdir(mode=0o700)
    victim_target = victim / name
    victim_target.write_bytes(b"must-survive")
    victim_target.chmod(0o600)
    moved = parent.with_name(f"{parent.name}-moved")
    original_replace = operator.os.replace
    injected = False

    def raced_replace(
        source_name: str,
        destination_name: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        if not injected and destination_name == target.name and dst_dir_fd is not None:
            injected = True
            parent.rename(moved)
            parent.symlink_to(victim, target_is_directory=True)
        original_replace(
            source_name,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(operator.os, "replace", raced_replace)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_path_drift"):
        operator._restore_private_engineer_file(  # noqa: SLF001
            source,
            target,
            mode=0o600,
            staging_manifest_sha256="a" * 64,
            contained=contained,
        )

    assert injected is True
    assert victim_target.read_bytes() == b"must-survive"
    assert target.resolve(strict=True) == victim_target.resolve(strict=True)


def test_engineer_staging_cleanup_pins_parent_before_unlinkat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    assert payload.engineer is not None
    manifest = operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        payload.engineer,
    )
    parent = root / "jobs" / "job-a"
    target = parent / "private-result-do-not-log.txt"
    stage = operator._engineer_restore_stage_path(  # noqa: SLF001
        target,
        payload.engineer.manifest_sha256,
    )
    stage.write_bytes(b"authorized-crash-residue")
    stage.chmod(0o600)
    victim = tmp_path / "cleanup-victim"
    victim.mkdir(mode=0o700)
    victim_stage = victim / stage.name
    victim_stage.write_bytes(b"must-survive")
    victim_stage.chmod(0o600)
    moved = parent.with_name("job-a-moved")
    original_unlink = operator.os.unlink
    injected = False

    def raced_unlink(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        if not injected and path == stage.name and dir_fd is not None:
            injected = True
            parent.rename(moved)
            parent.symlink_to(victim, target_is_directory=True)
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(operator.os, "unlink", raced_unlink)
    with pytest.raises(operator.ReleaseFailure, match="engineer_restore_staging_changed"):
        operator._cleanup_engineer_restore_staging(  # noqa: SLF001
            config,
            manifest=manifest,
            manifest_sha256=payload.engineer.manifest_sha256,
        )

    assert injected is True
    assert victim_stage.read_bytes() == b"must-survive"


def test_engineer_private_tree_removal_pins_parent_before_unlinkat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    target = parent / "obsolete.bin"
    target.write_bytes(b"owned-obsolete")
    target.chmod(0o600)
    victim = tmp_path / "remove-victim"
    victim.mkdir(mode=0o700)
    victim_target = victim / target.name
    victim_target.write_bytes(b"must-survive")
    victim_target.chmod(0o600)
    moved = parent.with_name("private-parent-moved")
    original_unlink = operator.os.unlink
    injected = False

    def raced_unlink(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        if not injected and path == target.name and dir_fd is not None:
            injected = True
            parent.rename(moved)
            parent.symlink_to(victim, target_is_directory=True)
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(operator.os, "unlink", raced_unlink)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_path_drift"):
        operator._remove_private_engineer_tree(target)  # noqa: SLF001

    assert injected is True
    assert victim_target.read_bytes() == b"must-survive"


def test_engineer_restore_publish_rejects_late_displaced_target_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "sealed-backup"
    source.write_bytes(b"secret-backup")
    source.chmod(0o600)
    parent = tmp_path / "data"
    parent.mkdir(mode=0o700)
    target = parent / "engineer-command.key"
    target.write_bytes(b"old-live-secret")
    target.chmod(0o600)
    alias = tmp_path / "late-old-secret-alias"
    original_replace = operator.os.replace
    injected = False

    def raced_replace(
        source_name: str,
        destination_name: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        if not injected and destination_name == target.name and dst_dir_fd is not None:
            os.link(target, alias)
            injected = True
        original_replace(
            source_name,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(operator.os, "replace", raced_replace)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_target_changed"):
        operator._restore_private_engineer_file(  # noqa: SLF001
            source,
            target,
            mode=0o600,
            staging_manifest_sha256="a" * 64,
        )

    assert injected is True
    assert target.read_bytes() == b"secret-backup"
    assert alias.read_bytes() == b"old-live-secret"
    assert alias.stat().st_nlink == 1


def test_engineer_staging_cleanup_binds_parent_seen_during_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    assert payload.engineer is not None
    manifest = operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        payload.engineer,
    )
    parent = root / "jobs" / "job-a"
    target = parent / "private-result-do-not-log.txt"
    stage = operator._engineer_restore_stage_path(  # noqa: SLF001
        target,
        payload.engineer.manifest_sha256,
    )
    stage.write_bytes(b"authorized-crash-residue")
    stage.chmod(0o600)
    victim = tmp_path / "cleanup-real-directory-victim"
    victim.mkdir(mode=0o700)
    victim_stage = victim / stage.name
    victim_stage.write_bytes(b"must-survive")
    victim_stage.chmod(0o600)
    moved = tmp_path / "enumerated-parent-moved"
    original_inventory = operator._engineer_restore_staging_inventory  # noqa: SLF001
    injected = False

    def raced_inventory(
        candidate_config: operator.SystemdConfig,
    ) -> tuple[operator._EngineerRestoreStagingObservation, ...]:  # noqa: SLF001
        nonlocal injected
        observed = original_inventory(candidate_config)
        if not injected:
            parent.rename(moved)
            victim.rename(parent)
            injected = True
        return observed

    monkeypatch.setattr(operator, "_engineer_restore_staging_inventory", raced_inventory)
    with pytest.raises(operator.ReleaseFailure, match="engineer_restore_staging_changed"):
        operator._cleanup_engineer_restore_staging(  # noqa: SLF001
            config,
            manifest=manifest,
            manifest_sha256=payload.engineer.manifest_sha256,
        )

    assert injected is True
    assert (parent / stage.name).read_bytes() == b"must-survive"
    assert (moved / stage.name).read_bytes() == b"authorized-crash-residue"


def test_engineer_exact_restore_directory_chmod_uses_pinned_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    target = root / "jobs" / "job-a"
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    victim = tmp_path / "directory-chmod-victim"
    victim.mkdir(mode=0o711)
    victim_mode = stat.S_IMODE(victim.stat().st_mode)
    moved = tmp_path / "job-a-before-chmod-swap"
    original_fchmod = operator.os.fchmod
    injected = False

    def raced_fchmod(descriptor: int, mode: int) -> None:
        nonlocal injected
        opened = os.fstat(descriptor)
        if not injected and (opened.st_dev, opened.st_ino) == target_identity:
            target.rename(moved)
            victim.rename(target)
            injected = True
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(operator.os, "fchmod", raced_fchmod)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_restore_path_drift"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001

    assert injected is True
    assert stat.S_IMODE(target.stat().st_mode) == victim_mode


def test_durable_replace_path_swap_never_chmods_the_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private-state"
    parent.mkdir(mode=0o700)
    destination = parent / "state.json"
    destination.write_bytes(b"old")
    destination.chmod(0o600)
    unrelated = parent / "unrelated"
    unrelated.write_bytes(b"must-not-be-repermissioned")
    unrelated.chmod(0o640)
    published = parent / "published-before-race"
    original_replace = operator.os.replace
    injected = False

    def raced_replace(source: object, target: object) -> None:
        nonlocal injected
        original_replace(source, target)
        if not injected and Path(target) == destination:
            injected = True
            original_replace(destination, published)
            os.link(unrelated, destination)

    monkeypatch.setattr(operator.os, "replace", raced_replace)
    with pytest.raises(operator.ReleaseFailure, match="durable_state_path_changed"):
        operator._replace_private_durable(destination, b"new\n")  # noqa: SLF001

    assert injected is True
    assert unrelated.read_bytes() == b"must-not-be-repermissioned"
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o640
    assert published.read_bytes() == b"new\n"


def test_exact_operator_backup_binds_existing_lifecycle_authority_and_reverifies_restore(
    tmp_path: Path,
) -> None:
    from friday.organs.engineer.command_tools import (
        open_engineer_command_backup_authority,
        provision_engineer_command_store,
    )

    config = _engineer_recovery_config(tmp_path)
    data = config.friday_home / "data"
    data.mkdir(mode=0o700)
    root = data / "engineer-command"
    key = data / "engineer-command.key"
    key.write_bytes(b"m" * 32)
    key.chmod(0o600)
    settings = SimpleNamespace(
        engineer_command_enabled=True,
        engineer_command_key_file=key,
        engineer_command_store_dir=root,
        state_dir=config.state_dir,
    )
    assert provision_engineer_command_store(settings) == {"status": "provisioned"}

    with pytest.raises(operator.ReleaseFailure, match="engineer_store_backup_authority_required"):
        operator._exact_sqlite_backup(  # noqa: SLF001
            config,
            require_engineer_authority=True,
        )
    unbound = operator._exact_sqlite_backup(config)  # noqa: SLF001
    connection = sqlite3.connect(config.database)
    connection.execute("UPDATE marker SET value='must-survive-unbound-refusal'")
    connection.commit()
    connection.close()
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_backup_authority_required"):
        operator._restore_exact_sqlite_backup(  # noqa: SLF001
            config,
            unbound,
            require_engineer_authority=True,
            engineer_authority_verify=lambda _proof, _digest: None,
        )
    connection = sqlite3.connect(config.database)
    assert connection.execute("SELECT value FROM marker").fetchone()[0] == ("must-survive-unbound-refusal")
    connection.execute("UPDATE marker SET value='before'")
    connection.commit()
    connection.close()

    def snapshot() -> object:
        with open_engineer_command_backup_authority(settings) as authority:
            return authority.backup_authority_snapshot()

    def attest(digest: str) -> object:
        with open_engineer_command_backup_authority(settings) as authority:
            before = authority.backup_authority_snapshot()
            evidence = authority.attest_main_database_backup(digest)
            verified = authority.verify_main_database_backup_authority(evidence, digest)
            after = authority.backup_authority_snapshot()
            return {
                "after": after,
                "before": before,
                "evidence": evidence,
                "verified": verified,
            }

    backup = operator._exact_sqlite_backup(  # noqa: SLF001
        config,
        require_engineer_authority=True,
        engineer_authority_snapshot=snapshot,
        engineer_authority_attest=attest,
    )
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    private_manifest = json.loads((payload.directory / "engineer-manifest.json").read_text(encoding="ascii"))
    evidence = private_manifest["engineer_command_ledger_authority"]
    assert evidence["quiescent"] is True
    assert (
        evidence["database_sha256"]
        == hashlib.sha256((payload.directory / "database.sqlite3").read_bytes()).hexdigest()
    )

    anchor = config.state_dir / "engineer-command-store.anchor.json"
    anchor.write_bytes(b"corrupt-live-anchor")
    anchor.chmod(0o600)
    connection = sqlite3.connect(config.database)
    connection.execute("UPDATE marker SET value='changed-after-backup'")
    connection.commit()
    connection.close()

    def verify(proof: Mapping[str, object], digest: str) -> object:
        with open_engineer_command_backup_authority(settings) as authority:
            before = authority.backup_authority_snapshot()
            verified = authority.verify_main_database_backup_authority(proof, digest)
            after = authority.backup_authority_snapshot()
            return {"after": after, "before": before, "verified": verified}

    operator._restore_exact_sqlite_backup(  # noqa: SLF001
        config,
        backup,
        require_engineer_authority=True,
        engineer_authority_verify=verify,
    )
    connection = sqlite3.connect(config.database)
    try:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "before"
    finally:
        connection.close()


@pytest.mark.parametrize("mutation", ["symlink", "fifo", "hardlink", "database_inode"])
def test_engineer_restore_rejects_unsafe_live_path_or_database_identity_before_main_restore(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, _key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    connection = sqlite3.connect(config.database)
    connection.execute("UPDATE marker SET value='must-survive-refusal'")
    connection.commit()
    connection.close()
    target = root / "jobs" / "unsafe"
    if mutation == "symlink":
        target.symlink_to("/tmp")
    elif mutation == "fifo":
        os.mkfifo(target, 0o600)
    elif mutation == "hardlink":
        os.link(root / "jobs" / "job-a" / "private-result-do-not-log.txt", target)
    else:
        replacement = root / ".kernel.sqlite.replaced"
        shutil.copy2(root / "kernel.sqlite", replacement)
        replacement.chmod(0o600)
        os.replace(replacement, root / "kernel.sqlite")
    with pytest.raises(
        operator.ReleaseFailure,
        match="engineer_store_(artifact_invalid|database_identity_changed)",
    ):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001
    connection = sqlite3.connect(config.database)
    try:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "must-survive-refusal"
    finally:
        connection.close()


def test_engineer_restore_rejects_partial_private_backup_before_live_mutation(
    tmp_path: Path,
) -> None:
    config = _engineer_recovery_config(tmp_path)
    root, key, _lifecycle_key = _provision_test_engineer_store(config)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    (
        payload.directory / "engineer-recovery" / "store" / "jobs" / "job-a" / "private-result-do-not-log.txt"
    ).unlink()
    key.write_bytes(b"q" * 32)
    key.chmod(0o600)
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_backup"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001
    assert key.read_bytes() == b"q" * 32
    assert (root / "kernel.sqlite").is_file()


def _obsidian_cutover_config(tmp_path: Path) -> operator.SystemdConfig:
    port = _systemd_test_port(tmp_path)
    connection = sqlite3.connect(port.config.database)
    try:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta VALUES('schema_version','35')")
        connection.execute("INSERT INTO marker VALUES('database-before')")
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(port.config.inbox_database)
    try:
        connection.execute("INSERT INTO marker VALUES('inbox-before')")
        connection.commit()
    finally:
        connection.close()
    port.config.database.chmod(0o600)
    port.config.inbox_database.chmod(0o600)
    root = operator._obsidian_root(port.config)  # noqa: SLF001
    (root / "notes" / "empty").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (root / "notes").chmod(0o755)
    (root / "notes" / "empty").chmod(0o755)
    (root / "notes" / "entry.md").write_text("before\n", encoding="utf-8")
    (root / "notes" / "entry.md").chmod(0o644)
    return port.config


def test_obsidian_root_is_restored_exactly_with_database_and_inbox(tmp_path: Path) -> None:
    config = _obsidian_cutover_config(tmp_path)
    root = operator._obsidian_root(config)  # noqa: SLF001
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    assert payload.obsidian is not None and payload.obsidian.present

    connection = sqlite3.connect(config.database)
    connection.execute("UPDATE marker SET value='database-after'")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(config.inbox_database)
    connection.execute("UPDATE marker SET value='inbox-after'")
    connection.commit()
    connection.close()
    (root / "notes" / "entry.md").write_text("after\n", encoding="utf-8")
    (root / "notes" / "extra.md").write_text("extra\n", encoding="utf-8")
    (root / "notes" / "extra.md").chmod(0o600)

    operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001
    connection = sqlite3.connect(config.database)
    assert connection.execute("SELECT value FROM marker").fetchone()[0] == "database-before"
    connection.close()
    connection = sqlite3.connect(config.inbox_database)
    assert connection.execute("SELECT value FROM marker").fetchone()[0] == "inbox-before"
    connection.close()
    assert (root / "notes" / "entry.md").read_text(encoding="utf-8") == "before\n"
    assert stat.S_IMODE((root / "notes").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "notes" / "entry.md").stat().st_mode) == 0o644
    assert (root / "notes" / "empty").is_dir()
    assert not (root / "notes" / "extra.md").exists()


def test_exact_backup_rejects_main_database_sidecar_symlink_before_checkpoint(
    tmp_path: Path,
) -> None:
    config = _obsidian_cutover_config(tmp_path)
    target = tmp_path / "sidecar-target"
    target.write_bytes(b"must remain untouched")
    target.chmod(0o600)
    Path(f"{config.database}-wal").symlink_to(target)

    with pytest.raises(operator.ReleaseFailure, match="backup_secondary_product_sidecar_invalid"):
        operator._exact_sqlite_backup(config)  # noqa: SLF001

    assert target.read_bytes() == b"must remain untouched"


def test_exact_backup_rejects_active_secondary_product_witness(tmp_path: Path) -> None:
    config = _obsidian_cutover_config(tmp_path)
    connection = sqlite3.connect(config.database)
    try:
        connection.execute("CREATE TABLE raw_objects(source_ref TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO raw_objects VALUES(?)",
            ("secondary-product-witness:assist:" + "a" * 32,),
        )
        connection.commit()
    finally:
        connection.close()
    config.database.chmod(0o600)

    with pytest.raises(operator.ReleaseFailure, match="backup_active_secondary_product_witness"):
        operator._exact_sqlite_backup(config)  # noqa: SLF001


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_obsidian_snapshot_rejects_unsafe_tree_entries(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    config = _obsidian_cutover_config(tmp_path)
    root = operator._obsidian_root(config)  # noqa: SLF001
    source = root / "notes" / "entry.md"
    if unsafe_kind == "symlink":
        (root / "unsafe").symlink_to(source)
    elif unsafe_kind == "hardlink":
        os.link(source, root / "unsafe")
    else:
        os.mkfifo(root / "unsafe", mode=0o600)
    with pytest.raises(operator.ReleaseFailure, match="obsidian_backup_source_invalid"):
        operator._exact_sqlite_backup(config)  # noqa: SLF001


def test_obsidian_snapshot_rejects_tree_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _obsidian_cutover_config(tmp_path)
    root = operator._obsidian_root(config)  # noqa: SLF001
    original = operator._capture_obsidian_tree  # noqa: SLF001
    captures = 0

    def capture(*args, **kwargs):
        nonlocal captures
        result = original(*args, **kwargs)
        captures += 1
        if captures == 1:
            (root / "notes" / "entry.md").write_text("drift\n", encoding="utf-8")
            (root / "notes" / "entry.md").chmod(0o600)
        return result

    monkeypatch.setattr(operator, "_capture_obsidian_tree", capture)
    with pytest.raises(operator.ReleaseFailure, match="obsidian_backup_source_changed"):
        operator._exact_sqlite_backup(config)  # noqa: SLF001


def test_obsidian_private_manifest_enforces_the_restore_size_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _systemd_test_port(tmp_path).config
    manifest = {
        "schema": "friday.immutable-cutover-obsidian-root.v1",
        "present": False,
        "root": None,
        "directories": [],
        "files": [],
    }
    encoded = operator._canonical_json(manifest) + b"\n"  # noqa: SLF001
    monkeypatch.setattr(
        operator,
        "_capture_obsidian_tree",
        lambda *_args, **_kwargs: (manifest, ()),
    )

    accepted = tmp_path / "accepted-boundary"
    accepted.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "MAX_EXACT_MANIFEST_BYTES", len(encoded))
    descriptor = operator._snapshot_obsidian_root(config, accepted)  # noqa: SLF001
    assert descriptor.present is False
    assert (accepted / "obsidian-manifest.json").read_bytes() == encoded

    rejected = tmp_path / "rejected-boundary"
    rejected.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "MAX_EXACT_MANIFEST_BYTES", len(encoded) - 1)
    with pytest.raises(operator.ReleaseFailure, match="obsidian_backup_manifest_bound_exceeded"):
        operator._snapshot_obsidian_root(config, rejected)  # noqa: SLF001
    assert not (rejected / "obsidian-manifest.json").exists()


def test_obsidian_snapshot_self_verifies_before_backup_can_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _obsidian_cutover_config(tmp_path)
    real_verify = operator._verify_obsidian_backup  # noqa: SLF001
    verification_count = 0

    def tamper_then_verify(directory, descriptor):
        nonlocal verification_count
        verification_count += 1
        copied_note = directory / "obsidian-root" / "notes" / "entry.md"
        copied_note.chmod(0o600)
        copied_note.write_text("tampered before return\n", encoding="utf-8")
        return real_verify(directory, descriptor)

    monkeypatch.setattr(operator, "_verify_obsidian_backup", tamper_then_verify)
    with pytest.raises(operator.ReleaseFailure, match="obsidian_backup_manifest_mismatch"):
        operator._exact_sqlite_backup(config)  # noqa: SLF001
    assert verification_count == 1


def test_tampered_obsidian_backup_is_rejected_before_any_live_restore(tmp_path: Path) -> None:
    config = _obsidian_cutover_config(tmp_path)
    root = operator._obsidian_root(config)  # noqa: SLF001
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    connection = sqlite3.connect(config.database)
    connection.execute("UPDATE marker SET value='live-database'")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(config.inbox_database)
    connection.execute("UPDATE marker SET value='live-inbox'")
    connection.commit()
    connection.close()
    live_root = root / "notes" / "entry.md"
    live_root.write_text("live-root\n", encoding="utf-8")
    live_root.chmod(0o600)
    database_before = config.database.read_bytes()
    inbox_before = config.inbox_database.read_bytes()
    root_before = live_root.read_bytes()
    backup_note = payload.directory / "obsidian-root" / "notes" / "entry.md"
    backup_note.chmod(0o600)
    backup_note.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(operator.ReleaseFailure, match="obsidian_backup_manifest_mismatch"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001
    assert config.database.read_bytes() == database_before
    assert config.inbox_database.read_bytes() == inbox_before
    assert live_root.read_bytes() == root_before


def test_obsidian_restore_replays_after_crash_between_quarantine_and_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _obsidian_cutover_config(tmp_path)
    root = operator._obsidian_root(config)  # noqa: SLF001
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    (root / "notes" / "entry.md").write_text("changed\n", encoding="utf-8")
    (root / "notes" / "entry.md").chmod(0o600)
    original_replace = operator.os.replace
    crashed = False

    def replace_once(source, destination):
        nonlocal crashed
        original_replace(source, destination)
        if Path(source) == root and str(destination).endswith(".old") and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after quarantine")

    monkeypatch.setattr(operator.os, "replace", replace_once)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001
    assert not root.exists()
    operator._restore_exact_sqlite_backup(config, backup)  # noqa: SLF001
    assert (root / "notes" / "entry.md").read_text(encoding="utf-8") == "before\n"


def _album_payload(message_id: int, *, payload_marker: str = "") -> str:
    return json.dumps(
        {
            "update_marker": payload_marker,
            "message": {
                "message_id": message_id,
                "media_group_id": "private-synthetic-group",
                "chat": {"id": 7001},
                "from": {"id": 8001},
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _album_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE updates(
               update_id INTEGER PRIMARY KEY,payload_json TEXT NOT NULL,attempts INTEGER NOT NULL,
               last_error TEXT NOT NULL,backend_response_json TEXT,status TEXT NOT NULL,
               ordering_key TEXT NOT NULL,next_attempt_at REAL NOT NULL,failed_at REAL
           )"""
    )
    update_ids = (101, 102)
    message_ids = (11, 12)
    for update_id, message_id in zip(update_ids, message_ids, strict=True):
        conn.execute(
            "INSERT INTO updates VALUES(?,?,0,'PermanentUpdateError',NULL,'dead_letter','chat:7001',0,1)",
            (update_id, _album_payload(message_id)),
        )
    conn.commit()
    monkeypatch.setattr(operator, "HISTORICAL_ALBUM_UPDATE_IDS", update_ids)
    monkeypatch.setattr(operator, "_ALBUM_MESSAGE_IDS", message_ids)
    items = []
    for row in conn.execute("SELECT * FROM updates ORDER BY update_id"):
        payload = json.loads(row["payload_json"])
        message = payload["message"]
        items.append(
            {
                "attempts": 0,
                "backend_response_absent": True,
                "chat_id": 7001,
                "last_error": "PermanentUpdateError",
                "media_group_id": "private-synthetic-group",
                "message_id": message["message_id"],
                "ordering_key": "chat:7001",
                "payload_sha256": hashlib.sha256(row["payload_json"].encode()).hexdigest(),
                "sender_id": 8001,
                "status": "dead_letter",
                "update_id": row["update_id"],
            }
        )
    digest = hashlib.sha256(
        operator._canonical_json({"schema": operator.ALBUM_RECOVERY_SCHEMA, "items": items})  # noqa: SLF001
    ).hexdigest()
    monkeypatch.setattr(operator, "HISTORICAL_ALBUM_PLAN_SHA256", digest)
    return conn


def test_historical_album_recovery_is_exact_cas_and_public_receipt_is_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _album_db(monkeypatch)
    receipt = operator.recover_historical_album(
        conn,
        v2_binary_live=lambda: True,
        verified_backup=lambda: "a" * 64,
        bridge_lease_held=lambda: True,
    )
    assert receipt["status"] == "pending"
    assert receipt["reset_count"] == 2
    assert [row[0] for row in conn.execute("SELECT status FROM updates ORDER BY update_id")] == [
        "pending",
        "pending",
    ]
    public = json.dumps(receipt, sort_keys=True)
    assert "7001" not in public
    assert "8001" not in public
    assert "private-synthetic-group" not in public
    with pytest.raises(operator.ReleaseFailure):
        operator.recover_historical_album(
            conn,
            v2_binary_live=lambda: True,
            verified_backup=lambda: "a" * 64,
            bridge_lease_held=lambda: True,
        )


@pytest.mark.parametrize("mutation", ["payload", "id", "status"])
def test_historical_album_identity_or_status_drift_never_reaches_backup(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    conn = _album_db(monkeypatch)
    if mutation == "payload":
        conn.execute(
            "UPDATE updates SET payload_json=? WHERE update_id=101", (_album_payload(11, payload_marker="x"),)
        )
    elif mutation == "id":
        conn.execute("UPDATE updates SET update_id=999 WHERE update_id=101")
    else:
        conn.execute("UPDATE updates SET status='pending' WHERE update_id=101")
    backup_calls = 0

    def backup() -> str:
        nonlocal backup_calls
        backup_calls += 1
        return "a" * 64

    with pytest.raises(operator.ReleaseFailure):
        operator.recover_historical_album(
            conn,
            v2_binary_live=lambda: True,
            verified_backup=backup,
            bridge_lease_held=lambda: True,
        )
    assert backup_calls == 0


def test_album_reset_requires_live_v2_and_bridge_quiescence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _album_db(monkeypatch)
    with pytest.raises(operator.ReleaseFailure, match="not_live"):
        operator.recover_historical_album(
            conn,
            v2_binary_live=lambda: False,
            verified_backup=lambda: "a" * 64,
            bridge_lease_held=lambda: True,
        )
    with pytest.raises(operator.ReleaseFailure, match="not_quiesced"):
        operator.recover_historical_album(
            conn,
            v2_binary_live=lambda: True,
            verified_backup=lambda: "a" * 64,
            bridge_lease_held=lambda: False,
        )


def _album_live_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[operator.SystemdActivationPort, operator.ReleaseIdentity, dict[str, object]]:
    port = _systemd_test_port(tmp_path)
    memory = _album_db(monkeypatch)
    disk = sqlite3.connect(port.config.inbox_database)
    try:
        memory.backup(disk)
    finally:
        disk.close()
        memory.close()
    port.config.inbox_database.chmod(0o600)
    release_root = tmp_path / "candidate"
    release_root.mkdir(mode=0o700)
    release = operator.ReleaseIdentity(release_root, "c" * 40, "0.206.0", "d" * 64, 34)
    port.config.anchor.symlink_to(release_root, target_is_directory=True)
    activation = operator.DurableActivationJournal(
        port.config.state_dir / "immutable-release-activation.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(port.config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(port.config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            port.config
        ),
        alias_claim_count=len(port.config.alias_claim_manifests),
        memory_vault_mode=port.config.memory_vault_mode,
        obsidian_mode=port.config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(port.config),  # noqa: SLF001
    )
    activation.begin(
        candidate=release,
        previous=operator.ReleaseIdentity(tmp_path / "rc1", "a" * 40, "0.206.0rc1", "b" * 64, 34),
        fallback=operator.ReleaseIdentity(tmp_path / "rc1", "a" * 40, "0.206.0rc1", "b" * 64, 34),
    )

    def accept_phase_b(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "e" * 64

    _rewrite_signed_journal(activation.path, accept_phase_b)
    state: dict[str, object] = {
        "bridge_active": True,
        "completion_mode": "complete",
        "events": [],
    }

    def event(name: str) -> None:
        events = state["events"]
        assert isinstance(events, list)
        events.append(name)

    monkeypatch.setattr(port, "verify_release", lambda _release: event("verify_release"))
    monkeypatch.setattr(port, "accept_backend", lambda _release: event("accept_backend"))

    def advance_album_completion() -> None:
        connection = sqlite3.connect(port.config.inbox_database)
        try:
            placeholders = ",".join("?" for _ in operator.HISTORICAL_ALBUM_UPDATE_IDS)
            pending = connection.execute(
                f"SELECT count(*) FROM updates WHERE update_id IN ({placeholders}) "  # nosec B608
                "AND status='pending'",
                operator.HISTORICAL_ALBUM_UPDATE_IDS,
            ).fetchone()[0]
            if pending != len(operator.HISTORICAL_ALBUM_UPDATE_IDS):
                return
            if state["completion_mode"] == "complete":
                connection.execute(
                    f"DELETE FROM updates WHERE update_id IN ({placeholders})",  # nosec B608
                    operator.HISTORICAL_ALBUM_UPDATE_IDS,
                )
            elif state["completion_mode"] == "partial":
                connection.execute(
                    "DELETE FROM updates WHERE update_id=?",
                    (operator.HISTORICAL_ALBUM_UPDATE_IDS[0],),
                )
            elif state["completion_mode"] == "dead_letter":
                connection.execute(
                    f"UPDATE updates SET status='dead_letter' "  # nosec B608
                    f"WHERE update_id IN ({placeholders})",
                    operator.HISTORICAL_ALBUM_UPDATE_IDS,
                )
            connection.commit()
        finally:
            connection.close()

    def accept_bridge(_release: operator.ReleaseIdentity) -> None:
        event("accept_bridge")
        if state["bridge_active"] is not True:
            raise operator.ReleaseFailure("synthetic_bridge_inactive")
        advance_album_completion()

    def stop_bridge() -> None:
        event("stop_bridge")
        state["bridge_active"] = False

    def start_bridge(_release: operator.ReleaseIdentity) -> None:
        event("start_bridge")
        state["bridge_active"] = True

    def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        assert arguments[0] == "is-active"
        active = b"active\n" if state["bridge_active"] is True else b"inactive\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=active, stderr=b"")

    monkeypatch.setattr(port, "accept_bridge", accept_bridge)
    monkeypatch.setattr(port, "stop_bridge", stop_bridge)
    monkeypatch.setattr(port, "start_bridge", start_bridge)
    monkeypatch.setattr(port, "_systemctl", systemctl)
    return port, release, state


def test_album_recovery_requires_accepted_clear_phase_b_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, release, state = _album_live_port(tmp_path, monkeypatch)
    activation_path = port.config.state_dir / "immutable-release-activation.v1.json"

    port.config = replace(port.config, memory_vault_mode="full_owner")
    with pytest.raises(operator.ReleaseFailure, match="requires_body_free_phase"):
        port.recover_historical_album_live(release)
    port.config = replace(port.config, memory_vault_mode="disabled")
    assert state["events"] == []

    def make_nonterminal(core: dict[str, object]) -> None:
        core["phase"] = "prepared"
        core["terminal_receipt_sha256"] = ""

    _rewrite_signed_journal(activation_path, make_nonterminal)
    with pytest.raises(operator.ReleaseFailure, match="requires_clear_phase_b"):
        port.recover_historical_album_live(release)
    assert state["events"] == []

    def make_wrong_candidate(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "e" * 64
        candidate = dict(core["candidate"])  # type: ignore[arg-type]
        candidate["commit"] = "9" * 40
        candidate["root"] = str(tmp_path / "wrong-final")
        candidate["tree_manifest_sha256"] = "8" * 64
        core["candidate"] = candidate

    _rewrite_signed_journal(activation_path, make_wrong_candidate)
    with pytest.raises(operator.ReleaseFailure, match="phase_b_candidate_mismatch"):
        port.recover_historical_album_live(release)
    assert state["events"] == []


def test_album_recovery_resumes_when_journal_says_bridge_was_already_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, release, state = _album_live_port(tmp_path, monkeypatch)
    journal = operator.DurableAlbumRecoveryJournal(
        port.config.state_dir / "historical-album-recovery.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(port.config),  # noqa: SLF001
    )
    journal.begin_or_resume(release)
    journal.record("bridge_stop_attempted")
    state["bridge_active"] = False
    receipt = port.recover_historical_album_live(release)
    assert receipt["status"] == "clear"
    assert journal.load()["phase"] == "complete"
    events = state["events"]
    assert isinstance(events, list)
    assert events[:3] == ["verify_release", "accept_backend", "stop_bridge"]
    assert events.count("start_bridge") == 1
    assert events[-1] == "accept_bridge"
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            journal.path,
            backup_root=port.config.backup_dir,
            config_identity_sha256="8" * 64,
        ).load()


def test_album_recovery_reconciles_commit_before_journal_record_without_second_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, release, state = _album_live_port(tmp_path, monkeypatch)
    original_record = operator.DurableAlbumRecoveryJournal.record
    crashed = False

    def crash_after_cas(self, phase: str, **kwargs: object) -> None:
        nonlocal crashed
        if phase == "cas_complete" and not crashed:
            crashed = True
            raise RuntimeError("synthetic kill after sqlite commit")
        original_record(self, phase, **kwargs)

    monkeypatch.setattr(operator.DurableAlbumRecoveryJournal, "record", crash_after_cas)
    with pytest.raises(operator.ReleaseFailure, match="historical_album_recovery_failed"):
        port.recover_historical_album_live(release)
    journal = operator.DurableAlbumRecoveryJournal(
        port.config.state_dir / "historical-album-recovery.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(port.config),  # noqa: SLF001
    )
    assert journal.load()["phase"] == "cas_attempted"
    assert state["bridge_active"] is False
    connection = sqlite3.connect(port.config.inbox_database)
    try:
        assert connection.execute("SELECT count(*) FROM updates WHERE status='pending'").fetchone()[0] == 2
    finally:
        connection.close()
    receipt = port.recover_historical_album_live(release)
    assert receipt["reset_count"] == 2
    assert journal.load()["phase"] == "complete"
    assert len(list(port.config.backup_dir.glob("historical-album-inbox-*"))) == 1
    assert state["bridge_active"] is True


def test_album_recovery_stays_pending_until_all_exact_rows_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, release, state = _album_live_port(tmp_path, monkeypatch)
    state["completion_mode"] = "pending"
    monkeypatch.setattr(operator, "HISTORICAL_ALBUM_COMPLETION_TIMEOUT_SEC", 0.0)

    with pytest.raises(operator.ReleaseFailure, match="historical_album_completion_pending"):
        port.recover_historical_album_live(release)

    journal = operator.DurableAlbumRecoveryJournal(
        port.config.state_dir / "historical-album-recovery.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(port.config),  # noqa: SLF001
    )
    pending = journal.load()
    assert pending["phase"] == "bridge_accepted"
    assert pending["completion_receipt_sha256"] == ""
    connection = sqlite3.connect(port.config.inbox_database)
    try:
        assert connection.execute("SELECT count(*) FROM updates").fetchone()[0] == 2
    finally:
        connection.close()

    state["completion_mode"] = "complete"
    monkeypatch.setattr(operator, "HISTORICAL_ALBUM_COMPLETION_TIMEOUT_SEC", 1.0)
    receipt = port.recover_historical_album_live(release)
    assert receipt["status"] == "clear"
    assert receipt["completed_update_count"] == 2
    assert journal.load()["phase"] == "complete"


@pytest.mark.parametrize(
    ("completion_mode", "failure_code"),
    [
        ("partial", "historical_album_completion_partial"),
        ("dead_letter", "historical_album_completion_dead_lettered"),
    ],
)
def test_album_recovery_never_clears_partial_or_dead_lettered_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion_mode: str,
    failure_code: str,
) -> None:
    port, release, state = _album_live_port(tmp_path, monkeypatch)
    state["completion_mode"] = completion_mode

    with pytest.raises(operator.ReleaseFailure, match=failure_code):
        port.recover_historical_album_live(release)

    journal = operator.DurableAlbumRecoveryJournal(
        port.config.state_dir / "historical-album-recovery.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(port.config),  # noqa: SLF001
    )
    assert journal.load()["phase"] == "bridge_accepted"
    assert journal.load()["completion_receipt_sha256"] == ""


def test_album_completion_journal_crash_resumes_from_durable_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, release, _state = _album_live_port(tmp_path, monkeypatch)
    original_record = operator.DurableAlbumRecoveryJournal.record
    crashed = False

    def crash_before_completion_record(self, phase: str, **kwargs: object) -> None:
        nonlocal crashed
        if phase == "complete" and not crashed:
            crashed = True
            raise RuntimeError("synthetic kill before completion journal fsync")
        original_record(self, phase, **kwargs)

    monkeypatch.setattr(
        operator.DurableAlbumRecoveryJournal,
        "record",
        crash_before_completion_record,
    )
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_completion_not_durable"):
        port.recover_historical_album_live(release)

    journal = operator.DurableAlbumRecoveryJournal(
        port.config.state_dir / "historical-album-recovery.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(port.config),  # noqa: SLF001
    )
    assert journal.load()["phase"] == "bridge_accepted"
    connection = sqlite3.connect(port.config.inbox_database)
    try:
        assert connection.execute("SELECT count(*) FROM updates").fetchone()[0] == 0
    finally:
        connection.close()

    resumed = port.recover_historical_album_live(release)
    replayed = port.recover_historical_album_live(release)
    assert resumed == replayed
    assert resumed["status"] == "clear"
    assert resumed["completed_update_count"] == 2
    assert journal.load()["phase"] == "complete"


def _systemd_test_port(tmp_path: Path) -> operator.SystemdActivationPort:
    tmp_path.chmod(0o700)
    friday_home = tmp_path / "friday-home"
    state = friday_home / "data" / "state"
    state.mkdir(parents=True, mode=0o700)
    friday_home.chmod(0o700)
    (friday_home / "data").chmod(0o700)
    database = state / "friday.sqlite3"
    inbox = state / "telegram-inbox.sqlite3"
    for path in (database, inbox):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.commit()
        connection.close()
        path.chmod(0o600)
    env_file = friday_home / ".env.local"
    env_file.write_text("FRIDAY_PROFILE=production\n", encoding="ascii")
    env_file.chmod(0o600)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir(mode=0o700)
    health_ca = friday_home / "health-ca.pem"
    health_ca.write_text("synthetic-test-ca", encoding="ascii")
    health_ca.chmod(0o600)
    return operator.SystemdActivationPort(
        operator.SystemdConfig(
            anchor=tmp_path / "current-release",
            env_file=env_file,
            env_file_sha256=hashlib.sha256(env_file.read_bytes()).hexdigest(),
            friday_home=friday_home,
            unit_dir=unit_dir,
            database=database,
            inbox_database=inbox,
            backup_dir=friday_home / "backups",
            state_dir=state,
            health_ca=health_ca,
            health_ca_sha256=hashlib.sha256(health_ca.read_bytes()).hexdigest(),
        )
    )


def test_engineer_settings_children_pin_exact_database_with_dual_legacy_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _systemd_test_port(tmp_path)
    legacy_database = port.config.state_dir / "jericho.sqlite3"
    connection = sqlite3.connect(legacy_database)
    connection.execute("CREATE TABLE legacy_marker(value TEXT)")
    connection.commit()
    connection.close()
    legacy_database.chmod(0o600)
    release = operator.ReleaseIdentity(
        tmp_path / "engineer-capable-release",
        "c" * 40,
        "0.207.66",
        "d" * 64,
        operator.ENGINEER_COMMAND_LIFECYCLE_MIN_SCHEMA,
        engineer_command_lifecycle_contract=operator.ENGINEER_COMMAND_LIFECYCLE_CONTRACT,
    )
    observed: list[dict[str, str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        observed.append(dict(environment))
        stdout = (
            b'{"status":"provisioned"}\n'
            if "provision_engineer_command_store" in command[4]
            else b'{"snapshot":{}}\n'
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(legacy_database))
    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "0")
    monkeypatch.setenv("JERICHO_DATABASE_PATH", str(legacy_database))
    monkeypatch.setattr(operator.subprocess, "run", run)
    monkeypatch.setattr(port, "writer_leases_held", lambda: True)
    monkeypatch.setattr(port, "_release_engineer_store_locks", lambda: None)
    monkeypatch.setattr(port, "_acquire_engineer_store_locks", lambda: None)
    monkeypatch.setattr(operator.fcntl, "flock", lambda *_args: None)

    port.provision_engineer_store(release)
    kernel_lock = port.config.state_dir / "kernel.lock"
    with kernel_lock.open("w+b") as lock_stream:
        port._engineer_locks = [  # noqa: SLF001
            (lock_stream.fileno(), kernel_lock, (0, 0))
        ]
        assert (
            port._run_engineer_backup_authority(  # noqa: SLF001
                release,
                action="snapshot",
            )
            == {}
        )

    assert len(observed) == 2
    for environment in observed:
        assert environment["FRIDAY_ENV_FILE"] == str(port.config.env_file)
        assert environment["FRIDAY_HOME"] == str(port.config.friday_home)
        assert environment["FRIDAY_DATABASE_PATH"] == str(port.config.database)
        assert environment["FRIDAY_DATABASE_MUST_EXIST"] == "1"
        assert "JERICHO_DATABASE_PATH" not in environment


def _engineer_mode_enabled_environment(predecessor: bytes) -> bytes:
    replacements = (
        (b"FRIDAY_ENGINEER_MODE_ENABLED=0\n", b"FRIDAY_ENGINEER_MODE_ENABLED=1\n"),
        (
            b"FRIDAY_HOST_ALLOWED_CIDRS=\n",
            b"FRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n",
        ),
    )
    target = predecessor
    missing: list[bytes] = []
    for disabled, enabled in replacements:
        if disabled in target:
            assert target.count(disabled) == 1
            target = target.replace(disabled, enabled, 1)
        else:
            missing.append(enabled)
    if missing:
        if target and not target.endswith((b"\n", b"\r")):
            target += b"\n"
        target += b"".join(missing)
    return target


def _engineer_command_enabled_environment(predecessor: bytes) -> bytes:
    disabled = b"FRIDAY_ENGINEER_COMMAND_ENABLED=0\n"
    enabled = b"FRIDAY_ENGINEER_COMMAND_ENABLED=1\n"
    if disabled in predecessor:
        assert predecessor.count(disabled) == 1
        return predecessor.replace(disabled, enabled, 1)
    return predecessor + (b"" if predecessor.endswith(b"\n") else b"\n") + enabled


def test_systemd_engineer_lifecycle_preflight_binds_private_default_store_and_key(
    tmp_path: Path,
) -> None:
    base = _systemd_test_port(tmp_path)
    assert base.engineer_store_lifecycle_required() is False
    store = base.config.friday_home / "data" / "engineer-command"
    store.mkdir(mode=0o700)
    key = base.config.friday_home / "data" / "engineer-command.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    # Feature disablement is not deletion: valid dormant residue still makes
    # the lifecycle cutover and capable fallback mandatory.
    assert base.engineer_store_lifecycle_required() is True
    enabled = _engineer_command_enabled_environment(base.config.env_file.read_bytes())
    base.config.env_file.write_bytes(enabled)
    base.config.env_file.chmod(0o600)
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            env_file_sha256=hashlib.sha256(enabled).hexdigest(),
        )
    )
    assert port.engineer_store_lifecycle_required() is True

    drifted = enabled + b"FRIDAY_ENGINEER_COMMAND_STORE_DIR=/tmp/not-the-store\n"
    base.config.env_file.write_bytes(drifted)
    base.config.env_file.chmod(0o600)
    drifted_port = operator.SystemdActivationPort(
        replace(
            base.config,
            env_file_sha256=hashlib.sha256(drifted).hexdigest(),
        )
    )
    with pytest.raises(operator.ReleaseFailure, match="engineer_store_environment_invalid"):
        drifted_port.engineer_store_lifecycle_required()


def test_engineer_lock_hardlink_is_rejected_before_chmod_of_unrelated_inode(
    tmp_path: Path,
) -> None:
    port = _systemd_test_port(tmp_path)
    store = port.config.friday_home / "data" / "engineer-command"
    store.mkdir(mode=0o700)
    unrelated = tmp_path / "unrelated-private-file"
    unrelated.write_bytes(b"must-not-be-repermissioned")
    unrelated.chmod(0o640)
    os.link(unrelated, store / "kernel.lease")
    mode_before = stat.S_IMODE(unrelated.stat().st_mode)

    with pytest.raises(operator.ReleaseFailure, match="engineer_store_lock_invalid"):
        port._acquire_engineer_store_locks()  # noqa: SLF001

    assert stat.S_IMODE(unrelated.stat().st_mode) == mode_before == 0o640
    assert unrelated.read_bytes() == b"must-not-be-repermissioned"
    assert port._engineer_locks == []  # noqa: SLF001


@pytest.mark.parametrize(
    "predecessor",
    [
        b"FRIDAY_PROFILE=production\nFRIDAY_ENGINEER_MODE_ENABLED=1\n",
        (
            b"# preserve\r\n"
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\n"
            b"FRIDAY_ENGINEER_COMMAND_ENABLED=0\n"
            b"FRIDAY_SECONDARY_LLM_ENABLED=1\n"
        ),
    ],
)
def test_engineer_command_enable_accepts_only_the_exact_runner_switch(
    predecessor: bytes,
) -> None:
    target = _engineer_command_enabled_environment(predecessor)
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "engineer_command_enable",
        predecessor,
        target,
    )
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "engineer_command_enable",
        None,
        target,
    )


@pytest.mark.parametrize(
    ("predecessor", "target", "failure"),
    [
        (
            b"FRIDAY_ENGINEER_MODE_ENABLED=0\n",
            b"FRIDAY_ENGINEER_MODE_ENABLED=0\nFRIDAY_ENGINEER_COMMAND_ENABLED=1\n",
            "engineer_command_engineer_mode_not_enabled",
        ),
        (
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\nFRIDAY_ENGINEER_COMMAND_ENABLED=1\n",
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\nFRIDAY_ENGINEER_COMMAND_ENABLED=1\n",
            "engineer_command_predecessor_not_disabled",
        ),
        (
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\n",
            (b"FRIDAY_ENGINEER_MODE_ENABLED=1\nFRIDAY_PROFILE=changed\nFRIDAY_ENGINEER_COMMAND_ENABLED=1\n"),
            "engineer_command_unrelated_environment_changed",
        ),
        (
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\n",
            (
                b"FRIDAY_ENGINEER_MODE_ENABLED=1\n"
                b"FRIDAY_ENGINEER_COMMAND_ENABLED=1\n"
                b"FRIDAY_ENGINEER_COMMAND_ENABLED=1\n"
            ),
            "engineer_command_environment_invalid",
        ),
    ],
)
def test_engineer_command_enable_rejects_unsafe_environment_changes(
    predecessor: bytes,
    target: bytes,
    failure: str,
) -> None:
    with pytest.raises(operator.ReleaseFailure, match=failure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "engineer_command_enable",
            predecessor,
            target,
        )


@pytest.mark.parametrize(
    "predecessor",
    [
        b"FRIDAY_PROFILE=production\n",
        b"FRIDAY_PROFILE=production",
        (
            b"# exact operator bytes stay here\r\n"
            b"FRIDAY_ENGINEER_MODE_ENABLED=0\n"
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\r\n"
            b"FRIDAY_SECONDARY_LLM_ENABLED=0\n"
            b"FRIDAY_HOST_ALLOWED_CIDRS=\n"
        ),
    ],
)
def test_engineer_mode_enable_accepts_only_the_exact_canonical_additions(
    predecessor: bytes,
) -> None:
    target = _engineer_mode_enabled_environment(predecessor)

    operator._validate_staged_environment_transition(  # noqa: SLF001
        "engineer_mode_enable",
        predecessor,
        target,
    )
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "engineer_mode_enable",
        None,
        target,
    )

    assert target.count(b"FRIDAY_ENGINEER_MODE_ENABLED=1\n") == 1
    assert target.count(b"FRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n") == 1
    assert b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\r\n" in target or (
        b"FRIDAY_SEMANTIC_SUPERVISOR_MODE" not in predecessor
    )
    assert b"FRIDAY_SECONDARY_LLM_ENABLED=0\n" in target or (
        b"FRIDAY_SECONDARY_LLM_ENABLED" not in predecessor
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unrelated",
        "semantic",
        "secondary",
        "wrong_cidr",
        "public_network",
        "duplicate",
        "engineer_crlf",
    ],
)
def test_engineer_mode_enable_rejects_every_extra_byte_change(mutation: str) -> None:
    predecessor = (
        b"# retain exactly\r\n"
        b"FRIDAY_PROFILE=production\n"
        b"FRIDAY_ENGINEER_MODE_ENABLED=0\n"
        b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\n"
        b"FRIDAY_SECONDARY_LLM_ENABLED=0\n"
        b"FRIDAY_HOST_ALLOWED_CIDRS=\n"
    )
    target = _engineer_mode_enabled_environment(predecessor)
    if mutation == "unrelated":
        target = target.replace(b"FRIDAY_PROFILE=production\n", b"FRIDAY_PROFILE=owner\n")
    elif mutation == "semantic":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=shadow\n",
        )
    elif mutation == "secondary":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_ENABLED=0\n",
            b"FRIDAY_SECONDARY_LLM_ENABLED=1\n",
        )
    elif mutation == "wrong_cidr":
        target = target.replace(b"192.168.1.0/24", b"192.168.0.0/16")
    elif mutation == "public_network":
        target += b"FRIDAY_HOST_PUBLIC_NETWORK_ENABLED=1\n"
    elif mutation == "duplicate":
        target += b"FRIDAY_ENGINEER_MODE_ENABLED=1\n"
    else:
        assert mutation == "engineer_crlf"
        target = target.replace(
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\n",
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\r\n",
        )

    with pytest.raises(operator.ReleaseFailure, match="engineer_mode_"):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "engineer_mode_enable",
            predecessor,
            target,
        )


@pytest.mark.parametrize(
    ("predecessor", "target", "failure"),
    [
        (
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\n",
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\nFRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n",
            "engineer_mode_predecessor_not_disabled",
        ),
        (
            b"FRIDAY_ENGINEER_MODE_ENABLED=0\nFRIDAY_HOST_ALLOWED_CIDRS=10.0.0.0/8\n",
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\nFRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n",
            "engineer_mode_predecessor_not_disabled",
        ),
        (
            b"export FRIDAY_ENGINEER_MODE_ENABLED=0\n",
            b"FRIDAY_ENGINEER_MODE_ENABLED=1\nFRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n",
            "engineer_mode_predecessor_not_disabled",
        ),
        (
            b"JERICHO_ENGINEER_MODE_ENABLED=0\n",
            (
                b"JERICHO_ENGINEER_MODE_ENABLED=0\n"
                b"FRIDAY_ENGINEER_MODE_ENABLED=1\n"
                b"FRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n"
            ),
            "engineer_mode_environment_invalid",
        ),
    ],
)
def test_engineer_mode_enable_rejects_noncanonical_or_pre_authorized_predecessor(
    predecessor: bytes,
    target: bytes,
    failure: str,
) -> None:
    with pytest.raises(operator.ReleaseFailure, match=failure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "engineer_mode_enable",
            predecessor,
            target,
        )


def test_systemd_engineer_mode_transition_is_idempotent_and_recovery_selects_exact_env(
    tmp_path: Path,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = (
        b"# preserve this mixed ending\r\n"
        b"FRIDAY_PROFILE=production\n"
        b"FRIDAY_ENGINEER_MODE_ENABLED=0\n"
        b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\n"
        b"FRIDAY_SECONDARY_LLM_ENABLED=0\n"
        b"FRIDAY_HOST_ALLOWED_CIDRS=\n"
    )
    base.config.env_file.write_bytes(predecessor)
    base.config.env_file.chmod(0o600)
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    target = _engineer_mode_enabled_environment(predecessor)
    target_sha256 = hashlib.sha256(target).hexdigest()
    staged = base.config.state_dir / "engineer-mode.env"
    staged.write_bytes(target)
    staged.chmod(0o600)
    current = replace(base.config, env_file_sha256=predecessor_sha256)
    configured = replace(
        current,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="engineer_mode_enable",
    )
    descriptor = ("engineer_mode_enable", predecessor_sha256, staged, target_sha256)

    prebackup_state: dict[str, object] = {
        "prebackup_config_transition": "engineer_mode_enable",
        "predecessor_env_sha256": predecessor_sha256,
        "next_env_file": str(staged),
        "next_env_file_sha256": target_sha256,
        "phase": "bridge_stop_attempted",
        "backup": None,
        "database_mutation_possible": False,
        "writer_target": "",
    }
    recovered = operator._activation_recovery_systemd_config(current, prebackup_state)  # noqa: SLF001
    recovery_port = operator.SystemdActivationPort(recovered)
    recovery_port.select_predecessor_config_transition(*descriptor)
    assert recovery_port.config.env_file.read_bytes() == predecessor
    assert recovery_port.config.env_file_sha256 == predecessor_sha256
    assert staged.read_bytes() == target

    port = operator.SystemdActivationPort(configured)
    port.validate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == target
    assert port.config.env_file_sha256 == target_sha256
    assert port.config.staged_config_transition == ""
    assert not staged.exists()


def test_engineer_mode_enable_prebackup_rollback_keeps_predecessor_env(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="engineer_mode_enable",
        predecessor_env_sha256="1" * 64,
        next_env_file=Path("/private-state/engineer-mode.env"),
        next_env_file_sha256="2" * 64,
    )
    port = FakePort(
        fail="backup_db_wal_inbox",
        staged_config_transition="engineer_mode_enable",
    )

    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            journal,
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )

    assert "activate_staged_config" not in port.events
    assert port.canonical_env_sha256 == "1" * 64
    assert port.events.index("select_predecessor_config") < port.events.index("start_backend:clean-schema33")
    assert port.active is releases.previous


def test_engineer_mode_enable_postbackup_recovery_converges_to_target_env(
    releases: Releases,
) -> None:
    journal = MemoryJournal(
        prebackup_config_transition="engineer_mode_enable",
        predecessor_env_sha256="3" * 64,
        next_env_file=Path("/private-state/engineer-mode.env"),
        next_env_file_sha256="4" * 64,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    journal.record("backup_complete", backup=FakePort().backup)
    port = FakePort(staged_config_transition="engineer_mode_enable")
    port.predecessor_env_sha256 = "3" * 64
    port.canonical_env_sha256 = "3" * 64

    receipt = operator.recover_interrupted_activation(port, journal)

    assert receipt["status"] == "recovered"
    assert port.canonical_env_sha256 == "4" * 64
    assert port.events.index("activate_staged_config") < port.events.index("start_backend:schema34-fallback")
    assert port.active is releases.fallback


@pytest.mark.parametrize("terminal_phase", ["rolled_back", "recovered"])
def test_engineer_mode_enable_accepts_exact_postbackup_terminal_current_release(
    tmp_path: Path,
    releases: Releases,
    terminal_phase: str,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    target_bytes = _engineer_mode_enabled_environment(predecessor)
    target_sha256 = hashlib.sha256(target_bytes).hexdigest()
    staged = base.config.state_dir / "engineer-mode.env"
    staged.write_bytes(target_bytes)
    staged.chmod(0o600)
    staged_config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="engineer_mode_enable",
    )
    target = operator._activation_target_config(staged_config)  # noqa: SLF001
    prior, _backup = _durable_postbackup_terminal(
        base.config,
        candidate=releases.previous,
        current=releases.fallback,
        terminal_phase=terminal_phase,
    )
    next_journal = operator.DurableActivationJournal(
        prior.path,
        backup_root=target.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=(
            operator._activation_transition_predecessor_identity(  # noqa: SLF001
                target,
                base.config.env_file_sha256,
                "engineer_mode_enable",
            )
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=base.config.env_file_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="engineer_mode_enable",
    )

    next_journal.begin(
        candidate=releases.candidate,
        previous=releases.fallback,
        fallback=releases.fallback,
    )

    prepared = next_journal.load()
    assert prepared["phase"] == "prepared"
    assert prepared["prebackup_config_transition"] == "engineer_mode_enable"
    assert prepared["predecessor_env_sha256"] == base.config.env_file_sha256
    assert prepared["next_env_file_sha256"] == target_sha256


def _secondary_shadow_environment(
    predecessor: bytes,
    ca_file: Path,
    *,
    overrides: dict[str, str | None] | None = None,
) -> bytes:
    values: dict[str, str] = {
        "FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC": "0.10",
        "FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT": "0",
        "FRIDAY_SECONDARY_LLM_API_KEY": "a" * 64,
        "FRIDAY_SECONDARY_LLM_BASE_URL": "https://192.168.1.35:8443/v1",
        "FRIDAY_SECONDARY_LLM_CALL_BUDGET_SEC": "15.0",
        "FRIDAY_SECONDARY_LLM_CA_FILE": str(ca_file),
        "FRIDAY_SECONDARY_LLM_CONNECT_TIMEOUT_SEC": "1.0",
        "FRIDAY_SECONDARY_LLM_COOLDOWN_SEC": "60",
        "FRIDAY_SECONDARY_LLM_ENABLED": "1",
        "FRIDAY_SECONDARY_LLM_HEALTH_INTERVAL_SEC": "30",
        "FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY": "1",
        "FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS": "4096",
        "FRIDAY_SECONDARY_LLM_MODE": "shadow",
        "FRIDAY_SECONDARY_LLM_MODEL": (
            "friday-secondary-gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
        ),
        "FRIDAY_SECONDARY_LLM_PROFILE": (
            "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
        ),
        "FRIDAY_SECONDARY_LLM_READ_TIMEOUT_SEC": "12.0",
        "FRIDAY_SECONDARY_LLM_WORKLOADS": "extract",
    }
    for key, value in (overrides or {}).items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return predecessor + b"".join(f"{key}={value}\n".encode("ascii") for key, value in sorted(values.items()))


def _secondary_shadow_stage(
    base: operator.SystemdActivationPort,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overrides: dict[str, str | None] | None = None,
    unrelated: bytes | None = None,
    ca_file: Path | None = None,
) -> tuple[Path, bytes, str]:
    ca = ca_file or (base.config.friday_home / "secondary-ca.pem")
    if ca_file is None:
        ca.write_bytes(b"synthetic-secondary-ca\n")
        ca.chmod(0o600)
    if not ca.is_symlink():
        monkeypatch.setattr(
            operator,
            "_SECONDARY_FINALIST_CA_SHA256",
            hashlib.sha256(ca.read_bytes()).hexdigest(),
        )
    predecessor = base.config.env_file.read_bytes()
    target = _secondary_shadow_environment(
        predecessor if unrelated is None else unrelated,
        ca,
        overrides=overrides,
    )
    staged = base.config.state_dir / "secondary-shadow.env"
    staged.write_bytes(target)
    staged.chmod(0o600)
    return staged, target, hashlib.sha256(target).hexdigest()


def _secondary_shadow_enabled_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
    overrides: dict[str, str | None] | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    base = _systemd_test_port(tmp_path)
    ca = base.config.friday_home / "secondary-ca.pem"
    ca.write_bytes(b"synthetic-secondary-ca\n")
    ca.chmod(0o600)
    monkeypatch.setattr(
        operator,
        "_SECONDARY_FINALIST_CA_SHA256",
        hashlib.sha256(ca.read_bytes()).hexdigest(),
    )
    enabled = _secondary_shadow_environment(
        base.config.env_file.read_bytes() if unrelated is None else unrelated,
        ca,
        overrides=overrides,
    )
    base.config.env_file.write_bytes(enabled)
    base.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                base.config,
                env_file_sha256=hashlib.sha256(enabled).hexdigest(),
            )
        ),
        enabled,
    )


def _secondary_shadow_disable_stage(
    base: operator.SystemdActivationPort,
    *,
    target: bytes | None = None,
) -> tuple[Path, bytes, str]:
    disabled = (
        target
        if target is not None
        else _secondary_shadow_disabled_environment(base.config.env_file.read_bytes())
    )
    staged = base.config.state_dir / "secondary-shadow-disabled.env"
    staged.write_bytes(disabled)
    staged.chmod(0o600)
    return staged, disabled, hashlib.sha256(disabled).hexdigest()


def _secondary_shadow_disabled_environment(enabled: bytes) -> bytes:
    source = b"FRIDAY_SECONDARY_LLM_ENABLED=1\n"
    assert enabled.count(source) == 1
    return enabled.replace(source, b"FRIDAY_SECONDARY_LLM_ENABLED=0\n", 1)


def _secondary_private_shadow_environment(shadow: bytes) -> bytes:
    allow_private = b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n"
    assert shadow.count(allow_private) == 1
    assert shadow.count(b"FRIDAY_SECONDARY_LLM_MODE=shadow\n") == 1
    return shadow.replace(
        allow_private,
        b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1\n",
        1,
    )


def _secondary_shadow_to_private_shadow_stage(
    base: operator.SystemdActivationPort,
    *,
    target: bytes | None = None,
) -> tuple[Path, bytes, str]:
    private_shadow = (
        target
        if target is not None
        else _secondary_private_shadow_environment(base.config.env_file.read_bytes())
    )
    staged = base.config.state_dir / "secondary-private-shadow.env"
    staged.write_bytes(private_shadow)
    staged.chmod(0o600)
    return staged, private_shadow, hashlib.sha256(private_shadow).hexdigest()


def _secondary_private_shadow_enabled_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    shadow_port, shadow = _secondary_shadow_enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    private_shadow = _secondary_private_shadow_environment(shadow)
    shadow_port.config.env_file.write_bytes(private_shadow)
    shadow_port.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                shadow_port.config,
                env_file_sha256=hashlib.sha256(private_shadow).hexdigest(),
            )
        ),
        private_shadow,
    )


def _secondary_assist_environment(private_shadow: bytes) -> bytes:
    shadow_mode = b"FRIDAY_SECONDARY_LLM_MODE=shadow\n"
    assert private_shadow.count(b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1\n") == 1
    assert private_shadow.count(shadow_mode) == 1
    return private_shadow.replace(shadow_mode, b"FRIDAY_SECONDARY_LLM_MODE=assist\n", 1)


def _secondary_assist_disabled_environment(assist: bytes) -> bytes:
    source = b"FRIDAY_SECONDARY_LLM_ENABLED=1\n"
    assert assist.count(source) == 1
    return assist.replace(source, b"FRIDAY_SECONDARY_LLM_ENABLED=0\n", 1)


def _secondary_shadow_to_assist_stage(
    base: operator.SystemdActivationPort,
    *,
    target: bytes | None = None,
) -> tuple[Path, bytes, str]:
    assist = (
        target if target is not None else _secondary_assist_environment(base.config.env_file.read_bytes())
    )
    staged = base.config.state_dir / "secondary-assist.env"
    staged.write_bytes(assist)
    staged.chmod(0o600)
    return staged, assist, hashlib.sha256(assist).hexdigest()


def _secondary_assist_enabled_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    shadow_port, private_shadow = _secondary_private_shadow_enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    assist = _secondary_assist_environment(private_shadow)
    shadow_port.config.env_file.write_bytes(assist)
    shadow_port.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                shadow_port.config,
                env_file_sha256=hashlib.sha256(assist).hexdigest(),
            )
        ),
        assist,
    )


def _secondary_document_map_environment(assist: bytes, *, mode: str) -> bytes:
    assert mode in {"shadow", "assist"}
    values, unrelated = operator._secondary_environment_view(assist)  # noqa: SLF001
    assert values["FRIDAY_SECONDARY_LLM_MODE"] == "assist"
    assert values["FRIDAY_SECONDARY_LLM_WORKLOADS"] == "extract"
    assert "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE" not in values
    values["FRIDAY_SECONDARY_LLM_WORKLOADS"] = "document_map,extract"
    values["FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE"] = mode
    return operator._canonical_secondary_environment(unrelated, values)  # noqa: SLF001


def _secondary_document_map_shadow_enabled_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    assist_port, assist = _secondary_assist_enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    shadow = _secondary_document_map_environment(assist, mode="shadow")
    assist_port.config.env_file.write_bytes(shadow)
    assist_port.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                assist_port.config,
                env_file_sha256=hashlib.sha256(shadow).hexdigest(),
            )
        ),
        shadow,
    )


def _secondary_document_map_assist_enabled_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    shadow_port, shadow = _secondary_document_map_shadow_enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    assist = shadow.replace(
        b"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=shadow\n",
        b"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=assist\n",
        1,
    )
    shadow_port.config.env_file.write_bytes(assist)
    shadow_port.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                shadow_port.config,
                env_file_sha256=hashlib.sha256(assist).hexdigest(),
            )
        ),
        assist,
    )


def _semantic_supervisor_values(mode: str) -> dict[str, str]:
    assert mode in {"off", "shadow"}
    return {
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE": "off",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "0" if mode == "shadow" else "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": mode,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "0",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": (
            "compare_archive_with_current_web,compare_current_file_with_current_web"
            if mode == "shadow"
            else ""
        ),
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
    }


def _semantic_supervisor_environment(current: bytes, *, mode: str) -> bytes:
    secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        current
    )
    _current_values, unrelated, _current = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    semantic = b"".join(
        f"{key}={value}\n".encode("ascii") for key, value in sorted(_semantic_supervisor_values(mode).items())
    )
    assert secondary == b"".join(
        f"{key}={value}\n".encode("ascii") for key, value in sorted(secondary_values.items())
    )
    return unrelated + semantic + secondary


def _semantic_effect_environment(
    current: bytes,
    *,
    mode: str,
    evidence_file: Path | None = None,
) -> bytes:
    assert mode in {"off", "shadow"}
    secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        current
    )
    values, unrelated, _semantic = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    values.update(operator._SEMANTIC_EFFECT_OFF_EXACT_VALUES)  # noqa: SLF001
    if mode == "shadow":
        assert evidence_file is not None
        evidence = evidence_file.read_bytes()
        values.update(
            {
                "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE": str(evidence_file),
                "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256": hashlib.sha256(evidence).hexdigest(),
                "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE": "shadow",
            }
        )
    semantic = operator._canonical_environment_values(values)  # noqa: SLF001
    assert secondary == operator._canonical_environment_values(secondary_values)  # noqa: SLF001
    return unrelated + semantic + secondary


def _semantic_supervisor_legacy_environment(current: bytes, *, mode: str) -> bytes:
    assert mode in {"off", "shadow"}
    secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        current
    )
    _current_values, unrelated, _current = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    values = (
        operator._SEMANTIC_SUPERVISOR_LEGACY_OFF_EXACT_VALUES  # noqa: SLF001
        if mode == "off"
        else operator._SEMANTIC_SUPERVISOR_LEGACY_SHADOW_EXACT_VALUES  # noqa: SLF001
    )
    semantic = b"".join(f"{key}={value}\n".encode() for key, value in sorted(values.items()))
    assert secondary == b"".join(
        f"{key}={value}\n".encode() for key, value in sorted(secondary_values.items())
    )
    return unrelated + semantic + secondary


def _semantic_supervisor_pre_latency_environment(current: bytes, *, mode: str) -> bytes:
    assert mode in {"off", "shadow"}
    secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        current
    )
    _current_values, unrelated, _current = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    values = (
        operator._SEMANTIC_SUPERVISOR_PRE_LATENCY_OFF_EXACT_VALUES  # noqa: SLF001
        if mode == "off"
        else operator._SEMANTIC_SUPERVISOR_PRE_LATENCY_SHADOW_EXACT_VALUES  # noqa: SLF001
    )
    semantic = b"".join(f"{key}={value}\n".encode() for key, value in sorted(values.items()))
    assert secondary == b"".join(
        f"{key}={value}\n".encode() for key, value in sorted(secondary_values.items())
    )
    return unrelated + semantic + secondary


def _semantic_supervisor_promotion_baseline_raw(
    *,
    precursor: str,
    canary_observations: int = 0,
    canary_evidence_sha256: str | None = None,
) -> bytes:
    def metric(
        stage: str,
        *,
        observations: int,
        complete: int,
        failures: dict[str, int],
        latency_total: int,
        latency_max: int,
        window: str,
    ) -> dict[str, object]:
        completion_counts = (
            {} if observations == 0 else {"complete": complete, "failed": observations - complete}
        )
        return {
            "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
            "stage": stage,
            "observation_count": observations,
            "completion_counts": completion_counts,
            "complete_count": complete,
            "failure_class_counts": failures,
            "latency_observation_count": observations,
            "latency_total_ms": latency_total,
            "latency_max_ms": latency_max,
            "window_sha256": window * 64,
        }

    shadow = metric(
        "shadow",
        observations=20,
        complete=8,
        failures={"capability:source_unavailable": 5, "none:none": 15},
        latency_total=20_000,
        latency_max=1_500,
        window="1",
    )
    assist = metric(
        "assist",
        observations=20,
        complete=12,
        failures={"none:none": 20},
        latency_total=20_000,
        latency_max=1_500,
        window="4",
    )
    canary = metric(
        "canary",
        observations=canary_observations,
        complete=canary_observations,
        failures=({} if canary_observations == 0 else {"none:none": canary_observations}),
        latency_total=canary_observations * 800,
        latency_max=800 if canary_observations else 0,
        window="5",
    )
    report: dict[str, object] = {
        "schema": SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
        "evidence": {
            "kind": SUPERVISOR_PRODUCTION_BASELINE_KIND,
            "body_free": True,
            "production_acceptance": False,
            "acceptance_authority": "operator_review_required",
            "representative_window_attested": False,
            "promotion_authority": False,
        },
        "sample": {
            "limit": 100,
            "turn_traces": 40,
            "joined_supervisor_events": 20,
            "promoted_product_events": 20 + canary_observations,
            "malformed_turn_traces": 0,
            "malformed_joined_events": 0,
            "malformed_promoted_product_events": 0,
            "duplicate_turn_trace_digests": 0,
            "duplicate_shadow_product_events": 0,
            "duplicate_promoted_product_events": 0,
            "unmatched_shadow_product_events": 0,
            "unmatched_promoted_product_events": 0,
        },
        "primary_baseline": {
            "intent_counts": {"dialogue": 40},
            "playbook_counts": {"dialogue": 40},
            "completion_counts": {"complete": 20, "failed": 20},
            "publication_counts": {"assistant_committed": 40},
            "failure_counts": {"none:none": 40},
            "authority_rechecked_count": 40,
            "partial_coverage_count": 0,
            "state_restored_count": 0,
        },
        "supervisor_join": {
            "task_counts": {"compare_current_file_with_current_web": 20},
            "skip_counts": {"none": 20},
            "parse_counts": {"parsed": 20},
            "policy_reason_counts": {"admitted": 20},
            "planner_latency_bucket_counts": {"250_999ms": 20},
            "actual_completion_counts": {"complete": 20},
            "actual_publication_counts": {"assistant_committed": 20},
            "actual_capability_outcome_counts": {},
            "invoked_count": 20,
            "admitted_count": 20,
            "final_authority_rechecked_count": 20,
            "state_restored_count": 0,
            "retry_occurred_count": 0,
        },
        "product_windows": {
            "shadow_readiness": {
                "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
                "mode": "shadow",
                "production_joined": True,
                "actual_promoted_execution": False,
                "quality_claim": "documented_baseline_failure_only",
                "observation_count": 20,
                "joined_trace_count": 20,
                "baseline": shadow,
                "readiness_observation_count": 20,
                "call_rate_observation_count": 20,
                "supervisor_invocation_count": 20,
                "unnecessary_supervisor_invocation_count": 0,
                "user_visible_observation_count": 20,
                "user_visible_regression_count": 0,
                "readiness_witness_sha256": "3" * 64,
            },
            "promoted_execution": {
                "assist": {
                    "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
                    "mode": "assist",
                    "production_joined": True,
                    "actual_promoted_execution": True,
                    "observation_count": 20,
                    "joined_trace_count": 20,
                    "promotion_evidence_count": 1,
                    "promotion_evidence_sha256": precursor,
                    "promoted": assist,
                    "call_rate_observation_count": 20,
                    "supervisor_invocation_count": 20,
                    "unnecessary_supervisor_invocation_count": 0,
                    "user_visible_observation_count": 20,
                    "user_visible_regression_count": 0,
                    "product_window_sha256": "6" * 64,
                },
                "canary": {
                    "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
                    "mode": "canary",
                    "production_joined": True,
                    "actual_promoted_execution": True,
                    "observation_count": canary_observations,
                    "joined_trace_count": canary_observations,
                    "promotion_evidence_count": (1 if canary_evidence_sha256 is not None else 0),
                    "promotion_evidence_sha256": canary_evidence_sha256,
                    "promoted": canary,
                    "call_rate_observation_count": canary_observations,
                    "supervisor_invocation_count": canary_observations,
                    "unnecessary_supervisor_invocation_count": 0,
                    "user_visible_observation_count": canary_observations,
                    "user_visible_regression_count": 0,
                    "product_window_sha256": "7" * 64,
                },
            },
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return canonical_json_file_bytes(report)


def _semantic_supervisor_promoted_values(
    *,
    mode: str,
    evidence_file: Path,
    source_sha256: str = "b" * 64,
    registry_sha256: str = "c" * 64,
    actors: tuple[str, ...] = (),
    precursor_assist_evidence_sha256: str | None = None,
    quality_basis: AssistPromotionQualityBasis = (AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT),
) -> dict[str, str]:
    assert mode in {"assist", "canary"}
    budget_file = evidence_file.with_name(f"{evidence_file.stem}-latency-budget.json")
    budget_payload = {
        "schema": "friday.semantic-supervisor-latency-budget-document.v1",
        "budget_id": "current-file-web-user-visible-latency-v1",
        "task_class": "compare_current_file_with_current_web",
        "target_mode": mode,
        "source_revision_sha256": source_sha256,
        "latency_measurement": "committed_turn_trace.budget.latency_ms",
        "maximum_user_visible_latency_ms": 2_500,
    }
    budget = canonical_json_file_bytes(budget_payload)
    budget_file.write_bytes(budget)
    budget_file.chmod(0o600)
    budget_sha256 = hashlib.sha256(budget).hexdigest()
    precursor = precursor_assist_evidence_sha256 or "d" * 64
    baseline_raw = _semantic_supervisor_promotion_baseline_raw(precursor=precursor)
    baseline = load_accepted_supervisor_production_baseline(
        baseline_raw,
        expected_file_sha256=hashlib.sha256(baseline_raw).hexdigest(),
    )
    accepted_budget = load_canonical_supervisor_latency_budget(
        budget,
        expected_file_sha256=budget_sha256,
    )
    target_mode = SupervisorMode(mode)
    attestation = SupervisorPromotionOperatorAttestation(
        target_mode=target_mode,
        baseline_file_sha256=baseline.file_sha256,
        baseline_report_sha256=baseline.report_sha256,
        latency_budget_file_sha256=accepted_budget.document_sha256,
        source_revision_sha256=source_sha256,
        registry_binding_sha256=registry_sha256,
        representative_window_attested=True,
        primary_fallback_proven=True,
        laptop_unavailable_fallback_proven=True,
        final_authority_recheck_proven=True,
        primary_publication_owner_proven=True,
        zero_hidden_owners_attested=True,
        zero_duplicate_capabilities_attested=True,
        zero_duplicate_effects_attested=True,
        zero_duplicate_publications_attested=True,
        zero_false_completion_regressions_attested=True,
        precursor_assist_promotion_evidence_sha256=(
            precursor if target_mode is SupervisorMode.CANARY else None
        ),
        quality_basis=(quality_basis if target_mode is SupervisorMode.CANARY else None),
    )
    if target_mode is SupervisorMode.ASSIST:
        promoted = build_supervisor_assist_promotion_evidence(
            evidence_id="operator_assist_fixture",
            baseline=baseline,
            budget=accepted_budget,
            attestation=attestation,
            documented_failure_class_id="capability:source_unavailable",
            documented_failure_class_sha256="2" * 64,
        )
    else:
        promoted = build_supervisor_canary_promotion_evidence(
            evidence_id="operator_canary_fixture",
            baseline=baseline,
            budget=accepted_budget,
            attestation=attestation,
            documented_failure_class_id=(
                "capability:source_unavailable"
                if quality_basis is AssistPromotionQualityBasis.DOCUMENTED_FAILURE_CLASS_REMOVAL
                else None
            ),
            documented_failure_class_sha256=(
                "e" * 64
                if quality_basis is AssistPromotionQualityBasis.DOCUMENTED_FAILURE_CLASS_REMOVAL
                else None
            ),
        )
    lookup_token = "7" * 64
    observed_mode = SupervisorMode.SHADOW if target_mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
    observed_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(observed_mode)
    representative_window_sha256_value = (
        baseline.shadow_readiness.readiness_witness_sha256
        if target_mode is SupervisorMode.ASSIST
        else baseline.assist_execution.product_window_sha256
    )
    joined_trace_count = (
        baseline.shadow_readiness.joined_trace_count
        if target_mode is SupervisorMode.ASSIST
        else baseline.assist_execution.joined_trace_count
    )
    server_attestation: dict[str, object] = {
        "schema": REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
        "attestation_id": "sswindow_" + "6" * 32,
        "authority": REPRESENTATIVE_WINDOW_AUTHORITY,
        "target_mode": mode,
        "observed_mode": observed_mode.value,
        "baseline_file_sha256": baseline.file_sha256,
        "baseline_report_sha256": baseline.report_sha256,
        "latency_budget_file_sha256": accepted_budget.document_sha256,
        "latency_budget_document_sha256": accepted_budget.document_sha256,
        "latency_budget_target_mode": mode,
        "latency_budget_source_revision_sha256": source_sha256,
        "maximum_user_visible_latency_ms": 2_500,
        "precursor_assist_promotion_evidence_sha256": (
            precursor if target_mode is SupervisorMode.CANARY else None
        ),
        "source_revision_sha256": source_sha256,
        "registry_binding_sha256": registry_sha256,
        "primary_pid": 100,
        "primary_process_epoch_sha256": "5" * 64,
        "primary_backend_version": "test",
        "requested_mode": observed_mode.value,
        "observed_release_commit": "4" * 40,
        "observed_release_metadata_sha256": "3" * 64,
        "observed_release_tree_sha256": "2" * 64,
        "observed_registry_binding_sha256": registry_sha256,
        "supervisor_policy_id": observed_policy.policy_id,
        "supervisor_policy_sha256": observed_policy.policy_sha256,
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        "observer_runner_sha256": "1" * 64,
        "sample_limit": 100,
        "turn_trace_count": 40,
        "joined_trace_count": joined_trace_count,
        "representative_window_sha256": representative_window_sha256_value,
        "server_recomputed": True,
        "representative_window_attested": True,
        "synthetic_authority": False,
        "lookup_token_sha256": hashlib.sha256(lookup_token.encode("ascii")).hexdigest(),
        "state_version": 1,
        "issued_at": 1_000,
        "expires_at": 1_500,
        "signature": "9" * 64,
    }
    representative_window_issue: dict[str, object] = {
        "schema": REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
        "status": "unused",
        "server_attestation": server_attestation,
        "server_attestation_sha256": representative_window_sha256(server_attestation),
        "attestation_lookup_token": lookup_token,
        "lookup_token_sha256": server_attestation["lookup_token_sha256"],
        "state_version": 1,
    }
    evidence = canonical_json_file_bytes(
        build_supervisor_promotion_bundle_payload(
            baseline_raw=baseline_raw,
            budget=accepted_budget,
            attestation=attestation,
            representative_window_issue=representative_window_issue,
            evidence=promoted,
        )
    )
    evidence_file.write_bytes(evidence)
    evidence_file.chmod(0o600)
    return {
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE": "off",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": mode,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS": ",".join(actors),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE": str(evidence_file),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256": hashlib.sha256(evidence).hexdigest(),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE": str(budget_file),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256": budget_sha256,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256": registry_sha256,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256": source_sha256,
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": "compare_current_file_with_current_web",
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
    }


def _semantic_supervisor_promoted_payload(
    tmp_path: Path,
    *,
    mode: str,
    quality_basis: AssistPromotionQualityBasis = (AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT),
) -> tuple[Path, dict[str, str], dict[str, object]]:
    evidence_file = tmp_path / f"accepted-{mode}-evidence.json"
    evidence_file.write_bytes(b"placeholder")
    evidence_file.chmod(0o600)
    values = _semantic_supervisor_promoted_values(
        mode=mode,
        evidence_file=evidence_file,
        actors=(("d" * 64,) if mode == "canary" else ()),
        quality_basis=quality_basis,
    )
    bundle = json.loads(evidence_file.read_text(encoding="ascii"))
    assert isinstance(bundle, dict)
    payload = bundle["promotion_evidence"]
    assert isinstance(payload, dict)
    return evidence_file, values, payload


def test_semantic_supervisor_operator_uses_current_product_sample_policy() -> None:
    from friday.orchestration.supervisor_assist_promotion import (
        SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
        SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
    )

    assert operator._SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS == (  # noqa: SLF001
        SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
    )
    assert operator._SEMANTIC_SUPERVISOR_PROMOTION_POLICY_SHA256 == (  # noqa: SLF001
        SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256
    )


@pytest.mark.parametrize(
    ("mode", "expected_requested_mode", "expected_policy_id"),
    (
        ("assist", "shadow", operator._SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID),  # noqa: SLF001
        ("canary", "assist", operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID),  # noqa: SLF001
    ),
)
def test_semantic_supervisor_operator_accepts_mode_bound_live_predecessor(
    tmp_path: Path,
    mode: str,
    expected_requested_mode: str,
    expected_policy_id: str,
) -> None:
    evidence_file, values, _payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode=mode,
    )
    bundle = json.loads(evidence_file.read_text(encoding="ascii"))
    server = bundle["representative_window_issue"]["server_attestation"]
    assert server["requested_mode"] == expected_requested_mode
    assert server["supervisor_policy_id"] == expected_policy_id

    operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
        values,
        mode=mode,
        invalid_code="mode_bound_live_predecessor_invalid",
    )


@pytest.mark.parametrize(
    ("mode", "wrong_policy_id", "wrong_policy_sha256"),
    (
        (
            "assist",
            operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID,  # noqa: SLF001
            operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256,  # noqa: SLF001
        ),
        (
            "canary",
            operator._SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID,  # noqa: SLF001
            operator._SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256,  # noqa: SLF001
        ),
    ),
)
def test_semantic_supervisor_operator_rejects_mixed_live_mode_policy_identity(
    tmp_path: Path,
    mode: str,
    wrong_policy_id: str,
    wrong_policy_sha256: str,
) -> None:
    evidence_file, values, _payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode=mode,
    )
    bundle = json.loads(evidence_file.read_text(encoding="ascii"))
    issue = bundle["representative_window_issue"]
    server = issue["server_attestation"]
    server["supervisor_policy_id"] = wrong_policy_id
    server["supervisor_policy_sha256"] = wrong_policy_sha256
    issue["server_attestation_sha256"] = representative_window_sha256(server)
    raw = canonical_json_file_bytes(bundle)
    evidence_file.write_bytes(raw)
    evidence_file.chmod(0o600)
    values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256"] = hashlib.sha256(raw).hexdigest()

    with pytest.raises(operator.ReleaseFailure, match="mixed_live_identity_invalid"):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode=mode,
            invalid_code="mixed_live_identity_invalid",
        )


def _semantic_effect_maturity_file(
    tmp_path: Path,
    *,
    stem: str = "effect-maturity",
    effect_registry_sha256: str = operator._SEMANTIC_EFFECT_EXPECTED_REGISTRY_BINDING_SHA256,
) -> Path:
    bundle_file = tmp_path / f"{stem}-canary-bundle.json"
    values = _semantic_supervisor_promoted_values(
        mode="canary",
        evidence_file=bundle_file,
    )
    bundle_raw = bundle_file.read_bytes()
    bundle = json.loads(bundle_raw)
    promotion_evidence = bundle["promotion_evidence"]
    assert isinstance(promotion_evidence, dict)
    mature_baseline_raw = _semantic_supervisor_promotion_baseline_raw(
        precursor="d" * 64,
        canary_observations=SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
        canary_evidence_sha256=canonical_sha256(promotion_evidence),
    )
    budget_file = Path(values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE"])
    budget_raw = budget_file.read_bytes()
    artifact = build_read_only_maturity_artifact(
        production_baseline_raw=mature_baseline_raw,
        expected_production_baseline_file_sha256=hashlib.sha256(mature_baseline_raw).hexdigest(),
        canary_promotion_bundle_raw=bundle_raw,
        expected_canary_promotion_bundle_file_sha256=hashlib.sha256(bundle_raw).hexdigest(),
        canary_budget_raw=budget_raw,
        expected_canary_budget_file_sha256=hashlib.sha256(budget_raw).hexdigest(),
        expected_source_revision_sha256="b" * 64,
        expected_registry_binding_sha256="c" * 64,
        expected_effect_registry_binding_sha256=effect_registry_sha256,
    )
    evidence_file = tmp_path / f"{stem}.json"
    evidence_file.write_bytes(artifact)
    evidence_file.chmod(0o600)
    return evidence_file


def _semantic_supervisor_set_payload_path(
    payload: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> None:
    current = payload
    for key in path[:-1]:
        child = current[key]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


def _semantic_supervisor_write_promoted_payload(
    evidence_file: Path,
    values: dict[str, str],
    payload: dict[str, object],
) -> None:
    bundle = json.loads(evidence_file.read_text(encoding="ascii"))
    assert isinstance(bundle, dict)
    bundle["promotion_evidence"] = payload
    raw = canonical_json_file_bytes(bundle)
    evidence_file.write_bytes(raw)
    evidence_file.chmod(0o600)
    values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256"] = hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("artifact", ("evidence", "budget"))
@pytest.mark.parametrize("mode", (0o500, 0o700))
def test_semantic_supervisor_promoted_artifacts_use_exact_shared_file_modes(
    tmp_path: Path,
    artifact: str,
    mode: int,
) -> None:
    evidence_file, values, _payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode="assist",
    )
    target = (
        evidence_file
        if artifact == "evidence"
        else Path(values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE"])
    )
    target.chmod(mode)

    with pytest.raises(operator.ReleaseFailure, match="promotion_mode_invalid"):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode="assist",
            invalid_code="promotion_mode_invalid",
        )


def test_semantic_supervisor_standalone_forged_evidence_is_not_a_bundle(
    tmp_path: Path,
) -> None:
    evidence_file, values, payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode="assist",
    )
    forged = canonical_json_file_bytes(payload)
    evidence_file.write_bytes(forged)
    evidence_file.chmod(0o600)
    values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256"] = hashlib.sha256(forged).hexdigest()

    with pytest.raises(operator.ReleaseFailure, match="forged_evidence_invalid"):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode="assist",
            invalid_code="forged_evidence_invalid",
        )


def test_semantic_supervisor_live_window_is_consumed_only_after_exact_source_rechecks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, shadow = _semantic_supervisor_shadow_port(
        tmp_path,
        monkeypatch,
        unrelated=b"FRIDAY_API_TOKEN=" + b"t" * 32 + b"\nFRIDAY_PROFILE=production\n",
    )
    evidence_file = tmp_path / "representative-window-assist.json"
    target = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=evidence_file,
        source_sha256="b" * 64,
    )
    staged, _target, staged_sha256 = _semantic_supervisor_stage(
        base,
        mode="assist",
        target=target,
    )
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=staged_sha256,
            staged_config_transition="semantic_supervisor_shadow_to_assist",
        )
    )
    consumed: list[dict[str, object]] = []

    def consume(request: Mapping[str, object], **_kwargs: object) -> None:
        consumed.append(dict(request))

    monkeypatch.setattr(
        port,
        "_consume_semantic_supervisor_representative_window_attestation",
        consume,
    )
    wrong_candidate = replace(
        _release(tmp_path, "window-wrong", schema=45, commit="a" * 40),
        tree_manifest_sha256="f" * 64,
    )
    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_supervisor_candidate_source_identity_mismatch",
    ):
        port._validate_semantic_supervisor_representative_window_gate(  # noqa: SLF001
            wrong_candidate
        )
    assert consumed == []

    candidate = replace(wrong_candidate, tree_manifest_sha256="b" * 64)
    port._validate_semantic_supervisor_representative_window_gate(candidate)  # noqa: SLF001
    assert len(consumed) == 1
    assert consumed[0]["source_revision_sha256"] == candidate.tree_manifest_sha256
    assert consumed[0]["registry_binding_sha256"] == "c" * 64


def test_semantic_supervisor_window_drift_after_consume_fails_before_quiesce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, shadow = _semantic_supervisor_shadow_port(
        tmp_path,
        monkeypatch,
        unrelated=b"FRIDAY_API_TOKEN=" + b"t" * 32 + b"\nFRIDAY_PROFILE=production\n",
    )
    evidence_file = tmp_path / "representative-window-drift.json"
    target = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=evidence_file,
        source_sha256="b" * 64,
    )
    staged, _target, staged_sha256 = _semantic_supervisor_stage(
        base,
        mode="assist",
        target=target,
    )
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=staged_sha256,
            staged_config_transition="semantic_supervisor_shadow_to_assist",
        )
    )

    def consume(*_args: object, **_kwargs: object) -> None:
        evidence_file.write_bytes(evidence_file.read_bytes() + b" ")

    monkeypatch.setattr(
        port,
        "_consume_semantic_supervisor_representative_window_attestation",
        consume,
    )
    candidate = replace(
        _release(tmp_path, "window-drift", schema=45, commit="a" * 40),
        tree_manifest_sha256="b" * 64,
    )
    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_supervisor_representative_window_identity_changed",
    ):
        port._validate_semantic_supervisor_representative_window_gate(candidate)  # noqa: SLF001


def _semantic_supervisor_promoted_environment(
    current: bytes,
    *,
    mode: str,
    evidence_file: Path,
    source_sha256: str = "b" * 64,
    registry_sha256: str = "c" * 64,
    actors: tuple[str, ...] = (),
) -> bytes:
    secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        current
    )
    current_values, unrelated, _current = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    precursor: str | None = None
    if mode == "canary" and current_values.get("FRIDAY_SEMANTIC_SUPERVISOR_MODE") == "assist":
        predecessor_path = Path(current_values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE"])
        predecessor_payload = json.loads(predecessor_path.read_text(encoding="ascii"))
        assert isinstance(predecessor_payload, dict)
        predecessor_evidence = predecessor_payload["promotion_evidence"]
        assert isinstance(predecessor_evidence, dict)
        precursor = hashlib.sha256(
            operator._canonical_json(predecessor_evidence)  # noqa: SLF001
        ).hexdigest()
    values = _semantic_supervisor_promoted_values(
        mode=mode,
        evidence_file=evidence_file,
        source_sha256=source_sha256,
        registry_sha256=registry_sha256,
        actors=actors,
        precursor_assist_evidence_sha256=precursor,
    )
    semantic = b"".join(f"{key}={value}\n".encode("ascii") for key, value in sorted(values.items()))
    assert secondary == b"".join(
        f"{key}={value}\n".encode("ascii") for key, value in sorted(secondary_values.items())
    )
    return unrelated + semantic + secondary


def _semantic_supervisor_off_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    accepted_port, accepted = _secondary_document_map_assist_enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    off = _semantic_supervisor_environment(accepted, mode="off")
    accepted_port.config.env_file.write_bytes(off)
    accepted_port.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                accepted_port.config,
                env_file_sha256=hashlib.sha256(off).hexdigest(),
            )
        ),
        off,
    )


def _semantic_supervisor_shadow_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unrelated: bytes | None = None,
) -> tuple[operator.SystemdActivationPort, bytes]:
    off_port, off = _semantic_supervisor_off_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    shadow = _semantic_supervisor_environment(off, mode="shadow")
    off_port.config.env_file.write_bytes(shadow)
    off_port.config.env_file.chmod(0o600)
    return (
        operator.SystemdActivationPort(
            replace(
                off_port.config,
                env_file_sha256=hashlib.sha256(shadow).hexdigest(),
            )
        ),
        shadow,
    )


def _semantic_supervisor_stage(
    base: operator.SystemdActivationPort,
    *,
    mode: str,
    target: bytes | None = None,
) -> tuple[Path, bytes, str]:
    staged_target = target or _semantic_supervisor_environment(
        base.config.env_file.read_bytes(),
        mode=mode,
    )
    staged = base.config.state_dir / f"semantic-supervisor-{mode}.env"
    staged.write_bytes(staged_target)
    staged.chmod(0o600)
    return staged, staged_target, hashlib.sha256(staged_target).hexdigest()


def _semantic_supervisor_health_payload(mode: str) -> dict[str, object]:
    assert mode in {"off", "shadow", "assist", "canary"}
    installed = mode != "off"
    effective_mode = "shadow" if installed else "off"
    policy_id = (
        operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID  # noqa: SLF001
        if mode in {"assist", "canary"}
        else operator._SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID  # noqa: SLF001
    )
    policy_sha256 = (
        operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256  # noqa: SLF001
        if mode in {"assist", "canary"}
        else operator._SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256  # noqa: SLF001
    )
    if mode in {"assist", "canary"}:
        semantic: dict[str, object] = {
            "schema": "friday.semantic-supervisor-assist-controller-status.v1",
            "installed": True,
            "role": "durable_read_only_assist",
            "requested_mode": mode,
            "effective_mode": "off",
            "promotion_admitted": False,
            "max_review_rounds": 1,
            "promotion_attempt_total": 0,
            "promotion_evaluation_total": 0,
            "promotion_admitted_total": 0,
            "active_tasks": 0,
            "retained_active_graphs": 0,
            "fallback_total": 0,
            "invoked_total": 0,
            "publication_total": 0,
            "terminal_publication_total": 0,
            "event_success_total": 0,
            "event_failure_total": 0,
            "ordinary_event_success_total": 0,
            "ordinary_event_failure_total": 0,
            "ownership_uncertain_total": 0,
            "fallback_reasons": {},
            "runtime_owner": "durable_graph_after_admission",
            "publication_owner": "primary",
            "tools_allowed": False,
            "effects_allowed": False,
            "closed": False,
            "scheduler": {
                "state": "probing",
                "available": False,
                "workload": "plan_candidate",
                "policy_id": policy_id,
                "policy_sha256": policy_sha256,
                "workload_available": True,
                "runtime_available": False,
                "closed_reason": "admitted",
                "circuit_retry_after_sec": 0.0,
            },
        }
    else:
        semantic = {
            "schema": "friday.semantic-supervisor-shadow-runtime.v1",
            "installed": installed,
            "role": "discarded_advisory_shadow",
            "requested_mode": mode,
            "effective_mode": effective_mode,
            "promotion_admitted": False,
            "runtime_owner": "unchanged",
            "publication_owner": "primary",
            "tools_allowed": False,
            "effects_allowed": False,
            "execution_allowed": False,
        }
        if installed:
            semantic.update(
                {
                    "policy_id": policy_id,
                    "policy_sha256": policy_sha256,
                    "accepted_profile_id": operator._SECONDARY_FINALIST_PROFILE_ID,  # noqa: SLF001
                    "max_pending": 4,
                }
            )
    if mode in {"assist", "canary"}:
        semantic["activation"] = {
            "schema": "friday.supervisor-assist-activation-status.v1",
            "configured": True,
            "reason": "material_loaded_not_accepted",
            "requested_mode": mode,
            "source_revision_loaded": True,
            "registry_binding_loaded": True,
            "scheduler_projection_loaded": True,
            "scheduler_runtime_available": False,
            "evidence_loaded": True,
            "evidence_authority": "production_joined",
            "operator_gate_enabled": True,
            "canary_actor_binding_count": 0 if mode == "assist" else 2,
            "promotion_admitted": False,
            "evidence_accepted": False,
            "acceptance_authority": "none",
            "body_free": True,
        }
    return {
        "semantic_supervisor": semantic,
        "secondary": {
            "schema": "friday.optional-secondary-health.v1",
            "role": "optional_advisory",
            "enabled": True,
            "configured": True,
            "mode": "assist",
            "state": "probing",
            "available": False,
            "semantic_supervisor": {
                "workload": "plan_candidate",
                "requested_mode": mode,
                "effective_mode": effective_mode,
                "policy_id": policy_id,
                "policy_sha256": policy_sha256,
                "workload_available": installed,
                "runtime_available": False,
                "closed_reason": "admitted" if installed else "mode_off",
            },
        },
    }


def test_semantic_supervisor_health_identity_requires_installed_closed_shadow_seam() -> None:
    shadow = _semantic_supervisor_health_payload("shadow")
    assert operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        shadow,
        expected_mode="shadow",
    )
    for path, replacement in (
        (("semantic_supervisor", "installed"), False),
        (("semantic_supervisor", "accepted_profile_id"), "wrong-profile"),
        (("secondary", "semantic_supervisor", "workload_available"), False),
        (("secondary", "semantic_supervisor", "policy_sha256"), "0" * 64),
    ):
        mutated = json.loads(json.dumps(shadow))
        target = mutated
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
            mutated,
            expected_mode="shadow",
        )

    off = _semantic_supervisor_health_payload("off")
    assert operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        off,
        expected_mode="off",
    )
    off["semantic_supervisor"]["effective_mode"] = "shadow"  # type: ignore[index]
    assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        off,
        expected_mode="off",
    )


@pytest.mark.parametrize("mode", ["assist", "canary"])
def test_semantic_supervisor_promoted_health_binds_loaded_activation_material(mode: str) -> None:
    payload = _semantic_supervisor_health_payload(mode)
    assert operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        payload,
        expected_mode=mode,
    )
    semantic = payload["semantic_supervisor"]
    assert isinstance(semantic, dict)
    activation = semantic["activation"]
    assert isinstance(activation, dict)
    mutations = (
        ("configured", False),
        ("requested_mode", "shadow"),
        ("source_revision_loaded", False),
        ("evidence_loaded", False),
        ("evidence_authority", "self_reported"),
        ("operator_gate_enabled", False),
        ("promotion_admitted", True),
        ("body_free", False),
    )
    for key, value in mutations:
        mutated = json.loads(json.dumps(payload))
        mutated["semantic_supervisor"]["activation"][key] = value
        assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
            mutated,
            expected_mode=mode,
        )

    wrong_count = json.loads(json.dumps(payload))
    wrong_count["semantic_supervisor"]["activation"]["canary_actor_binding_count"] = (
        1 if mode == "assist" else 0
    )
    assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        wrong_count,
        expected_mode=mode,
    )

    for key, value in (
        ("schema", "friday.semantic-supervisor-shadow-runtime.v1"),
        ("role", "discarded_advisory_shadow"),
        ("max_review_rounds", 0),
    ):
        mutated = json.loads(json.dumps(payload))
        mutated["semantic_supervisor"][key] = value
        assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
            mutated,
            expected_mode=mode,
        )

    for key in ("ordinary_event_success_total", "ordinary_event_failure_total"):
        for invalid in (-1, True):
            mutated = json.loads(json.dumps(payload))
            mutated["semantic_supervisor"][key] = invalid
            assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
                mutated,
                expected_mode=mode,
            )

    discarded = json.loads(json.dumps(payload))
    discarded["semantic_supervisor"] = {
        "schema": "friday.semantic-supervisor-shadow-runtime.v1",
        "installed": True,
        "role": "discarded_advisory_shadow",
        "requested_mode": mode,
        "effective_mode": "shadow",
        "promotion_admitted": False,
        "policy_id": operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID,  # noqa: SLF001
        "policy_sha256": operator._SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256,  # noqa: SLF001
        "accepted_profile_id": operator._SECONDARY_FINALIST_PROFILE_ID,  # noqa: SLF001
        "runtime_owner": "unchanged",
        "publication_owner": "primary",
        "tools_allowed": False,
        "effects_allowed": False,
        "execution_allowed": False,
        "max_pending": 4,
        "activation": activation,
    }
    assert not operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        discarded,
        expected_mode=mode,
    )


def _secondary_document_map_stage(
    base: operator.SystemdActivationPort,
    *,
    mode: str,
    target: bytes | None = None,
) -> tuple[Path, bytes, str]:
    target = target or _secondary_document_map_environment(base.config.env_file.read_bytes(), mode=mode)
    staged = base.config.state_dir / f"secondary-document-map-{mode}.env"
    staged.write_bytes(target)
    staged.chmod(0o600)
    return staged, target, hashlib.sha256(target).hexdigest()


def _secondary_assist_to_disabled_stage(
    base: operator.SystemdActivationPort,
    *,
    target: bytes | None = None,
) -> tuple[Path, bytes, str]:
    disabled = (
        target
        if target is not None
        else _secondary_assist_disabled_environment(base.config.env_file.read_bytes())
    )
    staged = base.config.state_dir / "secondary-assist-disabled.env"
    staged.write_bytes(disabled)
    staged.chmod(0o600)
    return staged, disabled, hashlib.sha256(disabled).hexdigest()


def test_systemd_port_round_trips_exact_semantic_supervisor_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = b"# byte-exact operator state\r\nFRIDAY_PROFILE=production\n"
    base, off = _semantic_supervisor_off_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    staged, shadow, shadow_sha256 = _semantic_supervisor_stage(base, mode="shadow")
    enable_config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=shadow_sha256,
        staged_config_transition="semantic_supervisor_shadow_enable",
    )
    assert operator._secondary_rollout_receipt_stage(enable_config) is None  # noqa: SLF001
    port = operator.SystemdActivationPort(enable_config)
    enable = (
        "semantic_supervisor_shadow_enable",
        base.config.env_file_sha256,
        staged,
        shadow_sha256,
    )
    port.validate_staged_config_transition(*enable)
    port.select_predecessor_config_transition(*enable)
    assert port.config.env_file.read_bytes() == off
    port.activate_staged_config_transition(*enable)
    port.activate_staged_config_transition(*enable)
    assert port.config.env_file.read_bytes() == shadow
    assert not staged.exists()

    secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        shadow
    )
    semantic_values, preserved, semantic = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    assert semantic_values == _semantic_supervisor_values("shadow")
    assert set(semantic_values) == {
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS",
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
    }
    assert preserved == unrelated
    assert semantic == b"".join(
        f"{key}={value}\n".encode("ascii") for key, value in sorted(semantic_values.items())
    )
    off_secondary, _off_nonsecondary, off_secondary_bytes = operator._secondary_environment_parts(  # noqa: SLF001
        off
    )
    assert secondary_values == off_secondary
    assert secondary == off_secondary_bytes
    assert secondary_values["FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT"] == "1"
    assert secondary_values["FRIDAY_SECONDARY_LLM_MODE"] == "assist"
    assert secondary_values["FRIDAY_SECONDARY_LLM_WORKLOADS"] == "document_map,extract"
    assert secondary_values["FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE"] == "assist"

    disabled_stage, disabled, disabled_sha256 = _semantic_supervisor_stage(port, mode="off")
    disable_config = replace(
        port.config,
        next_env_file=disabled_stage,
        next_env_file_sha256=disabled_sha256,
        staged_config_transition="semantic_supervisor_shadow_disable",
    )
    assert operator._secondary_rollout_receipt_stage(disable_config) is None  # noqa: SLF001
    disabling = operator.SystemdActivationPort(disable_config)
    disable = (
        "semantic_supervisor_shadow_disable",
        port.config.env_file_sha256,
        disabled_stage,
        disabled_sha256,
    )
    disabling.validate_staged_config_transition(*disable)
    disabling.activate_staged_config_transition(*disable)
    disabling.activate_staged_config_transition(*disable)
    assert disabled == off
    assert disabling.config.env_file.read_bytes() == off
    assert not disabled_stage.exists()


def test_systemd_port_round_trips_exact_semantic_supervisor_assist_and_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    assist_evidence = base.config.state_dir / "future-assist-promotion-evidence.json"
    assist_evidence.write_bytes(b'{"fixture":"body-free-assist-evidence"}\n')
    assist_evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=assist_evidence,
    )
    staged, _target, assist_sha256 = _semantic_supervisor_stage(
        base,
        mode="assist",
        target=assist,
    )
    promoting = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=assist_sha256,
            staged_config_transition="semantic_supervisor_shadow_to_assist",
        )
    )
    descriptor = (
        "semantic_supervisor_shadow_to_assist",
        base.config.env_file_sha256,
        staged,
        assist_sha256,
    )
    assert promoting._expected_semantic_health_mode() == "shadow"  # noqa: SLF001
    promoting.validate_staged_config_transition(*descriptor)
    promoting.activate_staged_config_transition(*descriptor)
    assert promoting.config.env_file.read_bytes() == assist
    assert promoting._expected_semantic_health_mode() == "assist"  # noqa: SLF001

    canary_evidence = base.config.state_dir / "future-canary-promotion-evidence.json"
    canary_evidence.write_bytes(b'{"fixture":"body-free-canary-evidence"}\n')
    canary_evidence.chmod(0o600)
    canary = _semantic_supervisor_promoted_environment(
        assist,
        mode="canary",
        evidence_file=canary_evidence,
        actors=("d" * 64, "e" * 64),
    )
    staged_canary, _target, canary_sha256 = _semantic_supervisor_stage(
        promoting,
        mode="canary",
        target=canary,
    )
    canary_port = operator.SystemdActivationPort(
        replace(
            promoting.config,
            next_env_file=staged_canary,
            next_env_file_sha256=canary_sha256,
            staged_config_transition="semantic_supervisor_assist_to_canary",
        )
    )
    canary_descriptor = (
        "semantic_supervisor_assist_to_canary",
        promoting.config.env_file_sha256,
        staged_canary,
        canary_sha256,
    )
    canary_port.validate_staged_config_transition(*canary_descriptor)
    canary_port.activate_staged_config_transition(*canary_descriptor)
    assert canary_port.config.env_file.read_bytes() == canary
    assert canary_port._expected_semantic_health_mode() == "canary"  # noqa: SLF001

    staged_assist, _target, rollback_assist_sha256 = _semantic_supervisor_stage(
        canary_port,
        mode="assist",
        target=assist,
    )
    assist_port = operator.SystemdActivationPort(
        replace(
            canary_port.config,
            next_env_file=staged_assist,
            next_env_file_sha256=rollback_assist_sha256,
            staged_config_transition="semantic_supervisor_canary_to_assist",
        )
    )
    assist_descriptor = (
        "semantic_supervisor_canary_to_assist",
        canary_port.config.env_file_sha256,
        staged_assist,
        rollback_assist_sha256,
    )
    assist_port.validate_staged_config_transition(*assist_descriptor)
    assist_port.activate_staged_config_transition(*assist_descriptor)
    assert assist_port.config.env_file.read_bytes() == assist

    staged_shadow, _target, rollback_shadow_sha256 = _semantic_supervisor_stage(
        assist_port,
        mode="shadow",
        target=shadow,
    )
    shadow_port = operator.SystemdActivationPort(
        replace(
            assist_port.config,
            next_env_file=staged_shadow,
            next_env_file_sha256=rollback_shadow_sha256,
            staged_config_transition="semantic_supervisor_assist_to_shadow",
        )
    )
    shadow_descriptor = (
        "semantic_supervisor_assist_to_shadow",
        assist_port.config.env_file_sha256,
        staged_shadow,
        rollback_shadow_sha256,
    )
    shadow_port.validate_staged_config_transition(*shadow_descriptor)
    shadow_port.activate_staged_config_transition(*shadow_descriptor)
    assert shadow_port.config.env_file.read_bytes() == shadow
    assert shadow_port._expected_semantic_health_mode() == "shadow"  # noqa: SLF001


def test_assist_to_canary_binds_canonical_predecessor_evidence_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    assist_evidence = tmp_path / "assist-provenance.json"
    assist_evidence.write_bytes(b"placeholder")
    assist_evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=assist_evidence,
    )
    assist_raw = assist_evidence.read_bytes()
    predecessor_bundle = json.loads(assist_raw)
    assert isinstance(predecessor_bundle, dict)
    predecessor_payload = predecessor_bundle["promotion_evidence"]
    assert isinstance(predecessor_payload, dict)
    predecessor_canonical_sha256 = hashlib.sha256(
        operator._canonical_json(predecessor_payload)  # noqa: SLF001
    ).hexdigest()
    assert predecessor_canonical_sha256 != hashlib.sha256(assist_raw).hexdigest()

    canary_evidence = tmp_path / "canary-provenance.json"
    canary_evidence.write_bytes(b"placeholder")
    canary_evidence.chmod(0o600)
    canary = _semantic_supervisor_promoted_environment(
        assist,
        mode="canary",
        evidence_file=canary_evidence,
        actors=("d" * 64,),
    )
    canary_bundle = json.loads(canary_evidence.read_text(encoding="ascii"))
    assert isinstance(canary_bundle, dict)
    canary_payload = canary_bundle["promotion_evidence"]
    assert isinstance(canary_payload, dict)
    assert canary_payload["precursor_assist_promotion_evidence_sha256"] == (predecessor_canonical_sha256)
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_assist_to_canary",
        assist,
        canary,
    )
    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_assist_to_canary",
            None,
            canary,
        )

    old_canary_file_sha256 = hashlib.sha256(canary_evidence.read_bytes()).hexdigest()
    canary_payload["precursor_assist_promotion_evidence_sha256"] = "f" * 64
    canary_bundle["promotion_evidence"] = canary_payload
    corrupted_raw = canonical_json_file_bytes(canary_bundle)
    canary_evidence.write_bytes(corrupted_raw)
    corrupted_file_sha256 = hashlib.sha256(corrupted_raw).hexdigest()
    corrupted = canary.replace(
        f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256={old_canary_file_sha256}\n".encode(),
        f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256={corrupted_file_sha256}\n".encode(),
        1,
    )
    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_assist_to_canary",
            assist,
            corrupted,
        )


@pytest.mark.parametrize(
    ("source_sha256", "registry_sha256"),
    (("f" * 64, "c" * 64), ("b" * 64, "e" * 64)),
)
def test_assist_to_canary_rejects_source_or_registry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_sha256: str,
    registry_sha256: str,
) -> None:
    _base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    assist_evidence = tmp_path / "identity-assist-bundle.json"
    assist_evidence.write_bytes(b"placeholder")
    assist_evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=assist_evidence,
    )
    canary_evidence = tmp_path / "identity-canary-bundle.json"
    canary_evidence.write_bytes(b"placeholder")
    canary_evidence.chmod(0o600)
    canary = _semantic_supervisor_promoted_environment(
        assist,
        mode="canary",
        evidence_file=canary_evidence,
        source_sha256=source_sha256,
        registry_sha256=registry_sha256,
        actors=("d" * 64,),
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_supervisor_promotion_identity_drift",
    ):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_assist_to_canary",
            assist,
            canary,
        )


@pytest.mark.parametrize(
    ("phase", "staged_present"),
    (
        ("environment_swap_attempted", True),
        ("environment_swap_attempted", False),
        ("environment_active", False),
    ),
)
def test_assist_to_canary_recovers_after_each_environment_swap_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    staged_present: bool,
) -> None:
    base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    assist_evidence = tmp_path / "recovery-assist-bundle.json"
    assist_evidence.write_bytes(b"placeholder")
    assist_evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=assist_evidence,
    )
    base.config.env_file.write_bytes(assist)
    base.config.env_file.chmod(0o600)
    assist_sha256 = hashlib.sha256(assist).hexdigest()
    assist_port = operator.SystemdActivationPort(replace(base.config, env_file_sha256=assist_sha256))
    canary_evidence = tmp_path / "recovery-canary-bundle.json"
    canary_evidence.write_bytes(b"placeholder")
    canary_evidence.chmod(0o600)
    canary = _semantic_supervisor_promoted_environment(
        assist,
        mode="canary",
        evidence_file=canary_evidence,
        actors=("d" * 64,),
    )
    staged, _target, canary_sha256 = _semantic_supervisor_stage(
        assist_port,
        mode="canary",
        target=canary,
    )
    descriptor = (
        "semantic_supervisor_assist_to_canary",
        assist_sha256,
        staged,
        canary_sha256,
    )
    prevalidated = operator.SystemdActivationPort(
        replace(
            assist_port.config,
            next_env_file=staged,
            next_env_file_sha256=canary_sha256,
            staged_config_transition=descriptor[0],
        )
    )
    prevalidated.validate_staged_config_transition(*descriptor)
    validation_sha256 = operator._staged_transition_validation_sha256(  # noqa: SLF001
        *descriptor
    )

    assist_port.config.env_file.write_bytes(canary)
    assist_port.config.env_file.chmod(0o600)
    if not staged_present:
        staged.unlink()
    state: dict[str, object] = {
        "phase": phase,
        "prebackup_config_transition": descriptor[0],
        "predecessor_env_sha256": descriptor[1],
        "next_env_file": str(descriptor[2]),
        "next_env_file_sha256": descriptor[3],
        "backup": {"durable": True},
        "database_mutation_possible": False,
        "writer_target": "",
        "staged_transition_validation_sha256": validation_sha256,
    }
    recovery_config = operator._activation_recovery_systemd_config(  # noqa: SLF001
        replace(assist_port.config, env_file_sha256=canary_sha256),
        state,
    )
    replay = operator.SystemdActivationPort(recovery_config)
    replay.activate_staged_config_transition(*descriptor)

    assert replay.config.env_file.read_bytes() == canary
    assert not staged.exists()


def test_assist_to_canary_recovery_rejects_absent_or_forged_validation_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    assist_evidence = tmp_path / "receipt-assist-bundle.json"
    assist_evidence.write_bytes(b"placeholder")
    assist_evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=assist_evidence,
    )
    base.config.env_file.write_bytes(assist)
    base.config.env_file.chmod(0o600)
    assist_sha256 = hashlib.sha256(assist).hexdigest()
    canary_evidence = tmp_path / "receipt-canary-bundle.json"
    canary_evidence.write_bytes(b"placeholder")
    canary_evidence.chmod(0o600)
    canary = _semantic_supervisor_promoted_environment(
        assist,
        mode="canary",
        evidence_file=canary_evidence,
        actors=("d" * 64,),
    )
    staged = base.config.state_dir / "receipt-canary.env"
    staged.write_bytes(canary)
    staged.chmod(0o600)
    canary_sha256 = hashlib.sha256(canary).hexdigest()
    base.config.env_file.write_bytes(canary)
    state: dict[str, object] = {
        "phase": "environment_swap_attempted",
        "prebackup_config_transition": "semantic_supervisor_assist_to_canary",
        "predecessor_env_sha256": assist_sha256,
        "next_env_file": str(staged),
        "next_env_file_sha256": canary_sha256,
        "backup": {"durable": True},
        "database_mutation_possible": False,
        "writer_target": "",
    }
    current = replace(base.config, env_file_sha256=canary_sha256)

    for invalid in (None, "f" * 64):
        candidate = dict(state)
        if invalid is not None:
            candidate["staged_transition_validation_sha256"] = invalid
        with pytest.raises(operator.ReleaseFailure, match="environment_file_changed"):
            operator._activation_recovery_systemd_config(  # noqa: SLF001
                current,
                candidate,
            )


def test_assist_to_canary_persists_validation_before_first_writer_stop(
    releases: Releases,
) -> None:
    transition = "semantic_supervisor_assist_to_canary"
    predecessor_sha256 = "1" * 64
    next_env_file = Path("/private-state/semantic-supervisor-canary.env")
    next_sha256 = "2" * 64
    journal = MemoryJournal(
        prebackup_config_transition=transition,
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=next_sha256,
    )
    expected = operator._staged_transition_validation_sha256(  # noqa: SLF001
        transition,
        predecessor_sha256,
        next_env_file,
        next_sha256,
    )

    class ReceiptBoundaryPort(FakePort):
        def stop_bridge(self) -> None:
            assert journal.state["phase"] == "bridge_stop_attempted"
            assert journal.state["staged_transition_validation_sha256"] == expected
            super().stop_bridge()

    receipt = operator.activate_release(
        ReceiptBoundaryPort(staged_config_transition=transition),
        journal,
        candidate=releases.candidate,
        previous=releases.previous,
        schema_capable_fallback=releases.fallback,
    )

    assert receipt["status"] == "clear"
    assert journal.state["staged_transition_validation_sha256"] == expected


@pytest.mark.parametrize(
    "mutation",
    [
        "gate_disabled",
        "review_round",
        "task_widened",
        "evidence_digest_mismatch",
        "latency_budget_digest_mismatch",
        "source_digest_noncanonical",
        "assist_actor_present",
        "unknown_key",
        "legacy_key",
        "duplicate_key",
        "quoted_value",
    ],
)
def test_semantic_supervisor_assist_rejects_noncanonical_or_drifted_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    evidence = tmp_path / "future-assist-evidence.json"
    evidence.write_bytes(b'{"fixture":"assist-evidence"}\n')
    evidence.chmod(0o600)
    target = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=evidence,
    )
    target_values, _unrelated, _secondary = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        operator._secondary_environment_parts(target)[1]  # noqa: SLF001
    )
    latency_budget_sha256 = target_values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256"]
    replacements = {
        "gate_disabled": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED=1\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED=0\n",
        ),
        "review_round": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=1\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=0\n",
        ),
        "task_widened": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS="
            b"compare_archive_with_current_web,compare_current_file_with_current_web\n",
        ),
        "evidence_digest_mismatch": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256="
            + hashlib.sha256(evidence.read_bytes()).hexdigest().encode()
            + b"\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256=" + b"f" * 64 + b"\n",
        ),
        "latency_budget_digest_mismatch": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256="
            + latency_budget_sha256.encode()
            + b"\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256=" + b"f" * 64 + b"\n",
        ),
        "source_digest_noncanonical": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256=" + b"b" * 64 + b"\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256=" + b"B" * 64 + b"\n",
        ),
        "assist_actor_present": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS=\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS=" + b"d" * 64 + b"\n",
        ),
        "unknown_key": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_OWNER=primary\n"
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
        ),
        "legacy_key": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
            b"JERICHO_SEMANTIC_SUPERVISOR_MODE=assist\n"
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
        ),
        "duplicate_key": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=assist\n"
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
        ),
        "quoted_value": (
            f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE={evidence}\n".encode(),
            f'FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE="{evidence}"\n'.encode(),
        ),
    }
    old, new = replacements[mutation]
    mutated = target.replace(old, new, 1)
    assert mutated != target
    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_shadow_to_assist",
            shadow,
            mutated,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "budget_extra_key",
        "budget_target_mode",
        "budget_source_revision",
        "budget_value_not_bound_by_evidence",
        "evidence_budget_digest",
        "old_evidence_schema",
    ),
)
def test_semantic_supervisor_operator_binds_exact_budget_document_to_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    evidence_file = tmp_path / "accepted-assist-evidence.json"
    evidence_file.write_bytes(b"placeholder")
    evidence_file.chmod(0o600)
    target = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=evidence_file,
    )
    _secondary_values, nonsecondary, _secondary = operator._secondary_environment_parts(  # noqa: SLF001
        target
    )
    values, _unrelated, _semantic = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    budget_file = Path(values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE"])
    old_budget_sha = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256"]
    old_evidence_sha = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256"]
    budget_payload = json.loads(budget_file.read_text(encoding="ascii"))
    evidence_bundle = json.loads(evidence_file.read_text(encoding="ascii"))
    evidence_payload = evidence_bundle["promotion_evidence"]
    assert isinstance(evidence_payload, dict)

    if mutation == "budget_extra_key":
        budget_payload["body"] = "not accepted"
    elif mutation == "budget_target_mode":
        budget_payload["target_mode"] = "canary"
    elif mutation == "budget_source_revision":
        budget_payload["source_revision_sha256"] = "f" * 64
    elif mutation == "budget_value_not_bound_by_evidence":
        budget_payload["maximum_user_visible_latency_ms"] = 2_400
    elif mutation == "evidence_budget_digest":
        evidence_payload["product_evidence"]["latency_budget_sha256"] = "f" * 64
    else:
        evidence_payload["schema"] = "friday.supervisor-assist-promotion.v4"

    if mutation.startswith("budget_"):
        budget_raw = json.dumps(
            budget_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        budget_file.write_bytes(budget_raw)
        budget_file.chmod(0o600)
        new_budget_sha = hashlib.sha256(budget_raw).hexdigest()
        target = target.replace(
            f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256={old_budget_sha}\n".encode(),
            f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256={new_budget_sha}\n".encode(),
            1,
        )
        if mutation == "budget_value_not_bound_by_evidence":
            evidence_payload["product_evidence"]["latency_budget_sha256"] = new_budget_sha
    if mutation in {
        "budget_value_not_bound_by_evidence",
        "evidence_budget_digest",
        "old_evidence_schema",
    }:
        evidence_bundle["promotion_evidence"] = evidence_payload
        evidence_raw = canonical_json_file_bytes(evidence_bundle)
        evidence_file.write_bytes(evidence_raw)
        evidence_file.chmod(0o600)
        new_evidence_sha = hashlib.sha256(evidence_raw).hexdigest()
        target = target.replace(
            f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256={old_evidence_sha}\n".encode(),
            f"FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256={new_evidence_sha}\n".encode(),
            1,
        )

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_shadow_to_assist",
            shadow,
            target,
        )


@pytest.mark.parametrize(
    ("mode", "path", "value"),
    (
        ("assist", ("authority",), "isolated_live_protocol"),
        ("assist", ("observed_mode",), "assist"),
        ("canary", ("observed_mode",), "shadow"),
        ("assist", ("source_revision_sha256",), "f" * 64),
        ("assist", ("registry_binding_sha256",), "f" * 64),
        ("assist", ("baseline_file_sha256",), "F" * 64),
        ("assist", ("baseline_report_sha256",), None),
        ("assist", ("operator_attestation_sha256",), "short"),
        ("assist", ("precursor_assist_promotion_evidence_sha256",), "d" * 64),
        ("canary", ("precursor_assist_promotion_evidence_sha256",), None),
        ("assist", ("promotion_policy_sha256",), "f" * 64),
        ("assist", ("observed_policy_id",), "gptoss20b-semantic-supervisor-v2"),
        ("canary", ("observed_policy_id",), "gptoss20b-semantic-supervisor-v1"),
        ("assist", ("observed_policy_sha256",), "f" * 64),
        ("assist", ("target_policy_id",), "gptoss20b-semantic-supervisor-v1"),
        ("assist", ("target_policy_sha256",), "f" * 64),
        ("assist", ("runtime_profile_id",), "wrong-profile"),
        ("assist", ("runtime_profile_manifest_sha256",), "f" * 64),
        ("assist", ("evidence_id",), "Not_Canonical"),
        ("assist", ("max_steps",), 6.0),
        ("assist", ("max_review_rounds",), True),
        ("assist", ("observation_count",), 19),
        ("assist", ("joined_trace_count",), 19),
        ("assist", ("representative_window_attested",), False),
        ("assist", ("primary_fallback_proven",), 1),
        ("assist", ("laptop_unavailable_fallback_proven",), False),
        ("assist", ("final_authority_recheck_proven",), False),
        ("assist", ("primary_publication_owner_proven",), False),
        ("assist", ("hidden_owner_count",), 1),
        ("assist", ("hidden_owner_count",), -1),
        ("assist", ("duplicate_capability_count",), True),
        ("assist", ("duplicate_effect_count",), 1),
        ("assist", ("duplicate_publication_count",), 1),
        ("assist", ("false_completion_regression_count",), 1),
        ("assist", ("product_evidence", "baseline_observation_count"), 19),
        ("assist", ("product_evidence", "baseline_complete_count"), 21),
        ("assist", ("product_evidence", "baseline_failure_class_count"), 0),
        ("assist", ("product_evidence", "readiness_observation_count"), 19),
        ("assist", ("product_evidence", "readiness_observation_count"), 21),
        ("assist", ("product_evidence", "call_rate_observation_count"), 19),
        ("assist", ("product_evidence", "user_visible_observation_count"), 19),
        ("assist", ("product_evidence", "supervisor_invocation_count"), 21),
        ("assist", ("product_evidence", "unnecessary_supervisor_invocation_count"), 1),
        ("assist", ("product_evidence", "user_visible_regression_count"), 1),
        ("assist", ("product_evidence", "latency_max_ms"), 2_501),
        ("assist", ("product_evidence", "latency_total_ms"), 30_001),
        ("assist", ("product_evidence", "documented_failure_class_id"), "none"),
        ("assist", ("product_evidence", "documented_failure_class_id"), "Invalid"),
        ("assist", ("product_evidence", "documented_failure_class_sha256"), "F" * 64),
        ("assist", ("product_evidence", "baseline_observation_count"), True),
        (
            "assist",
            ("product_evidence", "schema"),
            "friday.supervisor-assist-outcome-evidence.v2",
        ),
        ("canary", ("product_evidence", "baseline_observation_count"), 19),
        ("canary", ("product_evidence", "promoted_observation_count"), 19),
        ("canary", ("product_evidence", "promoted_observation_count"), 21),
        ("canary", ("product_evidence", "promoted_complete_count"), 8),
        ("canary", ("product_evidence", "promoted_complete_count"), 21),
        ("canary", ("product_evidence", "baseline_failure_class_count"), 21),
        ("canary", ("product_evidence", "latency_observation_count"), 19),
        ("canary", ("product_evidence", "call_rate_observation_count"), 19),
        ("canary", ("product_evidence", "user_visible_observation_count"), 19),
        ("canary", ("product_evidence", "unnecessary_supervisor_invocation_count"), 1),
        ("canary", ("product_evidence", "user_visible_regression_count"), 1),
        ("canary", ("product_evidence", "promoted_window_sha256"), "1" * 64),
        ("canary", ("product_evidence", "latency_total_ms"), 30_001),
    ),
)
def test_semantic_supervisor_operator_rejects_non_live_promotion_evidence(
    tmp_path: Path,
    mode: str,
    path: tuple[str, ...],
    value: object,
) -> None:
    evidence_file, values, payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode=mode,
    )
    _semantic_supervisor_set_payload_path(payload, path, value)
    _semantic_supervisor_write_promoted_payload(evidence_file, values, payload)

    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_supervisor_live_evidence_invalid",
    ):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode=mode,
            invalid_code="semantic_supervisor_live_evidence_invalid",
        )


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("baseline_failure_class_count", 0),
        ("promoted_failure_class_count", 1),
        ("documented_failure_class_sha256", None),
        ("documented_failure_class_id", "none"),
    ),
)
def test_semantic_supervisor_operator_requires_exact_failure_removal_claim(
    tmp_path: Path,
    mutation: str,
    value: object,
) -> None:
    evidence_file, values, payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode="canary",
        quality_basis=AssistPromotionQualityBasis.DOCUMENTED_FAILURE_CLASS_REMOVAL,
    )
    product = payload["product_evidence"]
    assert isinstance(product, dict)
    product[mutation] = value
    _semantic_supervisor_write_promoted_payload(evidence_file, values, payload)

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode="canary",
            invalid_code="semantic_supervisor_failure_removal_invalid",
        )


def test_semantic_supervisor_operator_accepts_exact_failure_removal_claim(
    tmp_path: Path,
) -> None:
    _evidence_file, values, _payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode="canary",
        quality_basis=AssistPromotionQualityBasis.DOCUMENTED_FAILURE_CLASS_REMOVAL,
    )

    operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
        values,
        mode="canary",
        invalid_code="semantic_supervisor_failure_removal_invalid",
    )


@pytest.mark.parametrize("location", ("outer", "product"))
def test_semantic_supervisor_operator_rejects_unknown_evidence_keys(
    tmp_path: Path,
    location: str,
) -> None:
    evidence_file, values, payload = _semantic_supervisor_promoted_payload(
        tmp_path,
        mode="assist",
    )
    target = payload if location == "outer" else payload["product_evidence"]
    assert isinstance(target, dict)
    target["private_body"] = "forbidden"
    _semantic_supervisor_write_promoted_payload(evidence_file, values, payload)

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode="assist",
            invalid_code="semantic_supervisor_unknown_evidence_key",
        )


@pytest.mark.parametrize(
    "raw",
    (
        b'{"schema":"first","schema":"second"}',
        b'{"value":NaN}',
    ),
)
def test_semantic_supervisor_closed_json_maps_adversarial_input_to_release_failure(
    raw: bytes,
) -> None:
    with pytest.raises(operator.ReleaseFailure, match="semantic_supervisor_json_invalid"):
        operator._semantic_supervisor_closed_json(  # noqa: SLF001
            raw,
            invalid_code="semantic_supervisor_json_invalid",
        )


def test_semantic_supervisor_closed_json_maps_recursion_to_release_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("bounded parser recursion")

    monkeypatch.setattr(operator.json, "loads", recursion_error)
    with pytest.raises(operator.ReleaseFailure, match="semantic_supervisor_json_invalid"):
        operator._semantic_supervisor_closed_json(  # noqa: SLF001
            b"{}",
            invalid_code="semantic_supervisor_json_invalid",
        )


@pytest.mark.parametrize("actors", [(), ("e" * 64, "d" * 64), ("D" * 64,)])
def test_semantic_supervisor_canary_requires_a_canonical_actor_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actors: tuple[str, ...],
) -> None:
    _base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    assist_evidence = tmp_path / "future-assist-evidence.json"
    assist_evidence.write_bytes(b'{"fixture":"assist"}\n')
    assist_evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        shadow,
        mode="assist",
        evidence_file=assist_evidence,
    )
    canary_evidence = tmp_path / "future-canary-evidence.json"
    canary_evidence.write_bytes(b'{"fixture":"canary"}\n')
    canary_evidence.chmod(0o600)
    canary = _semantic_supervisor_promoted_environment(
        assist,
        mode="canary",
        evidence_file=canary_evidence,
        actors=actors,
    )
    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_assist_to_canary",
            assist,
            canary,
        )


def test_semantic_supervisor_exact_legacy_p1_blocks_have_a_closed_upgrade_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, canonical_off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    legacy_off = _semantic_supervisor_legacy_environment(canonical_off, mode="off")
    canonical_shadow = _semantic_supervisor_environment(legacy_off, mode="shadow")
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_enable",
        legacy_off,
        canonical_shadow,
    )

    legacy_shadow = _semantic_supervisor_legacy_environment(canonical_shadow, mode="shadow")
    evidence = tmp_path / "future-assist-evidence.json"
    evidence.write_bytes(b'{"fixture":"assist"}\n')
    evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        legacy_shadow,
        mode="assist",
        evidence_file=evidence,
    )
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_to_assist",
        legacy_shadow,
        assist,
    )
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_disable",
        legacy_shadow,
        canonical_off,
    )


def test_semantic_supervisor_pre_latency_shadow_has_an_exact_upgrade_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, canonical_off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    pre_latency_off = _semantic_supervisor_pre_latency_environment(canonical_off, mode="off")
    canonical_shadow = _semantic_supervisor_environment(pre_latency_off, mode="shadow")
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_enable",
        pre_latency_off,
        canonical_shadow,
    )

    pre_latency_shadow = _semantic_supervisor_pre_latency_environment(
        canonical_shadow,
        mode="shadow",
    )
    evidence = tmp_path / "pre-latency-assist-evidence.json"
    evidence.write_bytes(b"placeholder")
    evidence.chmod(0o600)
    assist = _semantic_supervisor_promoted_environment(
        pre_latency_shadow,
        mode="assist",
        evidence_file=evidence,
    )
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_to_assist",
        pre_latency_shadow,
        assist,
    )
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_disable",
        pre_latency_shadow,
        canonical_off,
    )


def test_semantic_supervisor_enable_accepts_only_exact_legacy_implicit_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = b"# legacy accepted release\r\nFRIDAY_PROFILE=production\n"
    base, predecessor = _secondary_document_map_assist_enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    values, nonsecondary, _secondary = operator._secondary_environment_parts(  # noqa: SLF001
        predecessor
    )
    semantic_values, preserved, semantic = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    assert semantic_values == {}
    assert semantic == b""
    assert preserved == unrelated
    staged, shadow, shadow_sha256 = _semantic_supervisor_stage(base, mode="shadow")
    config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=shadow_sha256,
        staged_config_transition="semantic_supervisor_shadow_enable",
    )
    port = operator.SystemdActivationPort(config)
    descriptor = (
        "semantic_supervisor_shadow_enable",
        base.config.env_file_sha256,
        staged,
        shadow_sha256,
    )

    assert port._expected_semantic_health_mode() == "off"  # noqa: SLF001
    assert operator._semantic_supervisor_health_identity_matches(  # noqa: SLF001
        _semantic_supervisor_health_payload("off"),
        expected_mode="off",
    )
    port.validate_staged_config_transition(*descriptor)
    port.select_predecessor_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == predecessor
    assert (
        operator._canonical_environment_values(values)
        == (  # noqa: SLF001
            operator._secondary_environment_parts(predecessor)[2]  # noqa: SLF001
        )
    )

    port.activate_staged_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == shadow
    assert port._expected_semantic_health_mode() == "shadow"  # noqa: SLF001


def test_semantic_supervisor_enable_canonicalizes_secondary_before_later_owner_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, predecessor = _secondary_document_map_assist_enabled_port(tmp_path, monkeypatch)
    predecessor += b"FRIDAY_ENGINEER_MODE_ENABLED=1\n\nFRIDAY_ENGINEER_COMMAND_ENABLED=1\n"
    target = _semantic_supervisor_environment(predecessor, mode="shadow")

    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_shadow_enable",
        predecessor,
        target,
    )
    _values, nonsecondary, secondary = operator._secondary_environment_parts(target)  # noqa: SLF001
    assert target == nonsecondary + secondary
    assert b"FRIDAY_ENGINEER_MODE_ENABLED=1\n" in nonsecondary
    assert b"FRIDAY_ENGINEER_COMMAND_ENABLED=1\n" in nonsecondary


def test_env_example_semantic_eof_rewrites_to_accepted_canonical_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, accepted = _secondary_document_map_assist_enabled_port(tmp_path, monkeypatch)
    accepted_values, _accepted_unrelated, _accepted_secondary = operator._secondary_environment_parts(
        accepted
    )  # noqa: SLF001
    template = (Path(__file__).resolve().parents[1] / ".env.example").read_bytes()
    _placeholder_values, template_nonsecondary, _placeholder_secondary = (
        operator._secondary_environment_parts(template)  # noqa: SLF001
    )
    rewritten = operator._canonical_secondary_environment(  # noqa: SLF001
        template_nonsecondary,
        accepted_values,
    )

    unrelated, rewritten_secondary = operator._validate_semantic_supervisor_environment(  # noqa: SLF001
        rewritten,
        exact_values=operator._SEMANTIC_SUPERVISOR_OFF_EXACT_VALUES,  # noqa: SLF001
        invalid_code="template_layout_invalid",
    )
    assert rewritten_secondary == accepted_values
    assert rewritten == (
        unrelated
        + operator._canonical_environment_values(  # noqa: SLF001
            operator._SEMANTIC_SUPERVISOR_OFF_EXACT_VALUES  # noqa: SLF001
        )
        + operator._canonical_environment_values(accepted_values)  # noqa: SLF001
    )


@pytest.mark.parametrize(
    "mutation",
    ["partial", "unknown", "reordered_off", "secondary_drift"],
)
def test_semantic_supervisor_enable_rejects_inexact_legacy_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    base, predecessor = _secondary_document_map_assist_enabled_port(tmp_path, monkeypatch)
    target = _semantic_supervisor_environment(predecessor, mode="shadow")
    _values, unrelated, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        predecessor
    )
    if mutation == "partial":
        predecessor = unrelated + b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\n" + secondary
    elif mutation == "unknown":
        predecessor = unrelated + b"FRIDAY_SEMANTIC_SUPERVISOR_OWNER=primary\n" + secondary
    elif mutation == "reordered_off":
        off = _semantic_supervisor_environment(predecessor, mode="off")
        first = b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=1\n"
        second = b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=6\n"
        predecessor = off.replace(first + second, second + first, 1)
    else:
        assert mutation == "secondary_drift"
        predecessor = predecessor.replace(
            b"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=assist\n",
            b"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=shadow\n",
            1,
        )

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_shadow_enable",
            predecessor,
            target,
        )


def test_backend_acceptance_rejects_uninstalled_semantic_enable_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    staged, _shadow, shadow_sha256 = _semantic_supervisor_stage(base, mode="shadow")
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=shadow_sha256,
            staged_config_transition="semantic_supervisor_shadow_enable",
        )
    )
    transition = (
        "semantic_supervisor_shadow_enable",
        base.config.env_file_sha256,
        staged,
        shadow_sha256,
    )
    port.activate_staged_config_transition(*transition)
    candidate = replace(
        _release(tmp_path, "semantic-health-candidate", schema=42, commit="a" * 40),
        memory_vault_mode_contract=operator.MEMORY_VAULT_MODE_CONTRACT,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    current_payload = {
        "status": "ok",
        "version": candidate.version,
        "memory_vault": {
            "mode": "disabled",
            "body_free_mode": True,
            "body_projection_enabled": False,
        },
        "obsidian": {
            "mode": "disabled",
            "root_sha256": operator._obsidian_root_sha256(port.config),  # noqa: SLF001
        },
        **_semantic_supervisor_health_payload("shadow"),
    }
    current_payload["semantic_supervisor"]["installed"] = False  # type: ignore[index]

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return port.config.health_url

        def read(self, _limit: int) -> bytes:
            return json.dumps(current_payload, separators=(",", ":")).encode("ascii")

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(operator.ssl, "create_default_context", lambda **_kwargs: SimpleNamespace())
    monkeypatch.setattr(operator.urllib.request, "build_opener", lambda *_handlers: Opener())
    monkeypatch.setattr(operator.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(port, "_wait_process", lambda *_args: 1)
    ticks = iter((0.0, 0.0, 421.0))
    monkeypatch.setattr(operator.time, "monotonic", lambda: next(ticks))
    with pytest.raises(operator.ReleaseFailure, match="backend_health_identity_timeout"):
        port.accept_backend(candidate)

    current_payload.clear()
    current_payload.update(
        {
            "status": "ok",
            "version": candidate.version,
            "memory_vault": {
                "mode": "disabled",
                "body_free_mode": True,
                "body_projection_enabled": False,
            },
            "obsidian": {
                "mode": "disabled",
                "root_sha256": operator._obsidian_root_sha256(port.config),  # noqa: SLF001
            },
            **_semantic_supervisor_health_payload("shadow"),
        }
    )
    monkeypatch.setattr(operator.time, "monotonic", lambda: 0.0)
    port.accept_backend(candidate)

    rollback = replace(
        _release(tmp_path, "semantic-health-rollback", schema=42, commit="b" * 40),
        memory_vault_mode_contract=operator.MEMORY_VAULT_MODE_CONTRACT,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    current_payload["version"] = rollback.version
    current_payload.pop("semantic_supervisor")
    current_payload.pop("secondary")
    ticks = iter((0.0, 0.0, 421.0))
    monkeypatch.setattr(operator.time, "monotonic", lambda: next(ticks))
    with pytest.raises(operator.ReleaseFailure, match="backend_health_identity_timeout"):
        port.accept_backend(rollback)

    current_payload.update(_semantic_supervisor_health_payload("shadow"))
    monkeypatch.setattr(operator.time, "monotonic", lambda: 0.0)
    port.accept_backend(rollback)


@pytest.mark.parametrize(
    "mutation",
    [
        "assist_mode",
        "one_task",
        "reversed_tasks",
        "max_steps",
        "review_round",
        "timeout_literal",
        "authority_key",
        "reordered",
        "duplicate",
        "unrelated",
        "public_text",
        "wrong_profile",
        "api_key_drift",
    ],
)
def test_systemd_port_rejects_nonexact_semantic_supervisor_shadow_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    target = _semantic_supervisor_environment(off, mode="shadow")
    if mutation == "assist_mode":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=shadow\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=assist\n",
        )
    elif mutation == "one_task":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS="
            b"compare_archive_with_current_web,compare_current_file_with_current_web\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_archive_with_current_web\n",
        )
    elif mutation == "reversed_tasks":
        target = target.replace(
            b"compare_archive_with_current_web,compare_current_file_with_current_web",
            b"compare_current_file_with_current_web,compare_archive_with_current_web",
        )
    elif mutation == "max_steps":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=6\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=5\n",
        )
    elif mutation == "review_round":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=0\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=1\n",
        )
    elif mutation == "timeout_literal":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC=12\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC=12.0\n",
        )
    elif mutation == "authority_key":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_PUBLICATION_ALLOWED=1\n"
            b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n",
        )
    elif mutation == "reordered":
        first = b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=0\n"
        second = b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=6\n"
        target = target.replace(first + second, second + first)
    elif mutation == "duplicate":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=shadow\nFRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n",
        )
    elif mutation == "unrelated":
        target = target.replace(b"FRIDAY_PROFILE=production\n", b"FRIDAY_PROFILE=staging\n")
    elif mutation == "public_text":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1\n",
            b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n",
        )
    elif mutation == "wrong_profile":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_PROFILE="
            b"gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f\n",
            b"FRIDAY_SECONDARY_LLM_PROFILE=wrong\n",
        )
    else:
        assert mutation == "api_key_drift"
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"a" * 64,
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"b" * 64,
        )
    staged, _target, target_sha256 = _semantic_supervisor_stage(
        base,
        mode="shadow",
        target=target,
    )

    with pytest.raises(operator.ReleaseFailure):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="semantic_supervisor_shadow_enable",
            )
        )
    assert base.config.env_file.read_bytes() == off


@pytest.mark.parametrize(
    ("side", "mutation"),
    [
        ("predecessor", "mode"),
        ("predecessor", "tasks"),
        ("predecessor", "review"),
        ("predecessor", "timeout"),
        ("target", "mode"),
        ("target", "tasks"),
        ("target", "review"),
        ("target", "timeout"),
    ],
)
def test_semantic_supervisor_disable_requires_exact_shadow_to_exact_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    mutation: str,
) -> None:
    base, shadow = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
    off = _semantic_supervisor_environment(shadow, mode="off")
    source, target = shadow, off
    replacements = {
        "mode": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=shadow\n"
            if side == "predecessor"
            else b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=off\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MODE=canary\n",
        ),
        "tasks": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS="
            + (
                b"compare_archive_with_current_web,compare_current_file_with_current_web"
                if side == "predecessor"
                else b""
            )
            + b"\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_TASKS=compare_current_file_with_current_web\n",
        ),
        "review": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS="
            + (b"0" if side == "predecessor" else b"1")
            + b"\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=9\n",
        ),
        "timeout": (
            b"FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC=12\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC=12.0\n",
        ),
    }
    old, new = replacements[mutation]
    if side == "predecessor":
        source = source.replace(old, new)
    else:
        target = target.replace(old, new)

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_shadow_disable",
            source,
            target,
        )


def test_nonsemantic_transition_cannot_change_semantic_supervisor_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    shadow = _semantic_supervisor_environment(off, mode="shadow")

    with pytest.raises(
        operator.ReleaseFailure,
        match="nonsemantic_transition_changed_semantic_supervisor_environment",
    ):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "obsidian_enable",
            off,
            shadow,
        )


def test_semantic_effect_shadow_transition_is_exact_reversible_and_evidence_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(tmp_path)
    shadow = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )

    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_effect_shadow_enable",
        off,
        shadow,
    )
    disabled = _semantic_effect_environment(shadow, mode="off")
    evidence.unlink()
    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_effect_shadow_disable",
        shadow,
        disabled,
    )
    assert disabled == off


def test_semantic_effect_shadow_preflight_rejects_unknown_effect_registry_before_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(
        tmp_path,
        effect_registry_sha256="f" * 64,
    )
    shadow = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_effect_shadow_environment_invalid",
    ):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_effect_shadow_enable",
            off,
            shadow,
        )


def test_semantic_effect_shadow_enable_upgrades_exact_pre_effect_off_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, current = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    _secondary_values, nonsecondary, secondary = operator._secondary_environment_parts(  # noqa: SLF001
        current
    )
    _values, unrelated, _semantic = operator._semantic_supervisor_environment_parts(  # noqa: SLF001
        nonsecondary
    )
    predecessor = (
        unrelated
        + operator._canonical_environment_values(  # noqa: SLF001
            operator._SEMANTIC_SUPERVISOR_PRE_EFFECT_OFF_EXACT_VALUES  # noqa: SLF001
        )
        + secondary
    )
    evidence = _semantic_effect_maturity_file(tmp_path)
    target = _semantic_effect_environment(
        predecessor,
        mode="shadow",
        evidence_file=evidence,
    )

    operator._validate_staged_environment_transition(  # noqa: SLF001
        "semantic_supervisor_effect_shadow_enable",
        predecessor,
        target,
    )
    _base.config.env_file.write_bytes(predecessor)
    _base.config.env_file.chmod(0o600)
    staged = _base.config.state_dir / "legacy-effect-shadow.env"
    staged.write_bytes(target)
    staged.chmod(0o600)
    port = operator.SystemdActivationPort(
        replace(
            _base.config,
            env_file_sha256=hashlib.sha256(predecessor).hexdigest(),
            next_env_file=staged,
            next_env_file_sha256=hashlib.sha256(target).hexdigest(),
            staged_config_transition="semantic_supervisor_effect_shadow_enable",
        )
    )
    assert port._expected_semantic_effect_health_mode() == ""  # noqa: SLF001


def test_systemd_port_stages_semantic_effect_shadow_with_exact_health_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(tmp_path)
    shadow = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )
    staged = base.config.state_dir / "semantic-effect-shadow.env"
    staged.write_bytes(shadow)
    staged.chmod(0o600)
    shadow_sha256 = hashlib.sha256(shadow).hexdigest()
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=shadow_sha256,
            staged_config_transition="semantic_supervisor_effect_shadow_enable",
        )
    )
    descriptor = (
        "semantic_supervisor_effect_shadow_enable",
        base.config.env_file_sha256,
        staged,
        shadow_sha256,
    )

    assert port._expected_semantic_health_mode() == ""  # noqa: SLF001
    assert port._expected_semantic_effect_health_mode() == "off"  # noqa: SLF001
    assert port._expected_semantic_effect_health() == ("off", None)  # noqa: SLF001
    port.validate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == shadow
    assert port._expected_semantic_effect_health_mode() == "shadow"  # noqa: SLF001
    mode, identity = port._expected_semantic_effect_health()  # noqa: SLF001
    artifact = json.loads(evidence.read_bytes())
    assert mode == "shadow"
    assert identity == operator._SemanticEffectMaturityIdentity(  # noqa: SLF001
        evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
        maturity_facts_sha256=canonical_sha256(artifact["maturity"]),
        source_revision_sha256="b" * 64,
        registry_binding_sha256="c" * 64,
        effect_registry_binding_sha256=(operator._SEMANTIC_EFFECT_EXPECTED_REGISTRY_BINDING_SHA256),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_evidence",
        "digest_mismatch",
        "public_evidence",
        "semantic_drift",
        "secondary_drift",
        "unrelated_drift",
        "reordered",
    ],
)
def test_semantic_effect_shadow_enable_rejects_every_unbound_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(tmp_path)
    evidence_sha256 = hashlib.sha256(evidence.read_bytes()).hexdigest()
    target = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )
    if mutation == "missing_evidence":
        evidence.unlink()
    elif mutation == "digest_mismatch":
        target = target.replace(
            evidence_sha256.encode(),
            b"f" * 64,
            1,
        )
    elif mutation == "public_evidence":
        evidence.chmod(0o644)
    elif mutation == "semantic_drift":
        target = target.replace(
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=6\n",
            b"FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=5\n",
            1,
        )
    elif mutation == "secondary_drift":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=assist\n",
            b"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=shadow\n",
            1,
        )
    elif mutation == "unrelated_drift":
        target = target.replace(
            b"FRIDAY_PROFILE=production\n",
            b"FRIDAY_PROFILE=staging\n",
            1,
        )
    else:
        assert mutation == "reordered"
        first = b"FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE=" + str(evidence).encode() + b"\n"
        second = (
            b"FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256="
            + hashlib.sha256(evidence.read_bytes()).hexdigest().encode()
            + b"\n"
        )
        target = target.replace(first + second, second + first, 1)

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_effect_shadow_enable",
            off,
            target,
        )


@pytest.mark.parametrize("mutation", ["schema", "facts", "noncanonical"])
def test_semantic_effect_shadow_preflight_rebuilds_canonical_maturity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(tmp_path)
    payload = json.loads(evidence.read_bytes())
    if mutation == "schema":
        payload["schema"] = "friday.semantic-supervisor-read-only-maturity-artifact.v0"
    elif mutation == "facts":
        payload["maturity"]["hidden_owner_count"] = 1
    else:
        assert mutation == "noncanonical"
    if mutation != "noncanonical":
        payload.pop("artifact_payload_sha256")
        payload["artifact_payload_sha256"] = canonical_sha256(payload)
        candidate = canonical_json_file_bytes(payload)
    else:
        candidate = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    evidence.write_bytes(candidate)
    evidence.chmod(0o600)
    target = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_effect_shadow_environment_invalid",
    ):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_effect_shadow_enable",
            off,
            target,
        )


@pytest.mark.parametrize("mutation", ["incomplete", "failed"])
def test_semantic_effect_shadow_preflight_rejects_noncomplete_canary_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(tmp_path)
    artifact = json.loads(evidence.read_bytes())
    baseline = artifact["production_baseline"]
    promoted = baseline["product_windows"]["promoted_execution"]["canary"]["promoted"]
    if mutation == "incomplete":
        promoted["complete_count"] = 19
        promoted["completion_counts"] = {"complete": 19, "failed": 1}
    else:
        promoted["failure_class_counts"] = {
            "capability:source_unavailable": 1,
            "none:none": 19,
        }
    baseline.pop("report_sha256")
    baseline["report_sha256"] = canonical_sha256(baseline)
    baseline_raw = canonical_json_file_bytes(baseline)
    artifact["maturity"]["production_baseline_file_sha256"] = hashlib.sha256(baseline_raw).hexdigest()
    artifact["maturity"]["production_baseline_report_sha256"] = baseline["report_sha256"]
    artifact.pop("artifact_payload_sha256")
    artifact["artifact_payload_sha256"] = canonical_sha256(artifact)
    evidence.write_bytes(canonical_json_file_bytes(artifact))
    evidence.chmod(0o600)
    target = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="semantic_effect_shadow_environment_invalid",
    ):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_effect_shadow_enable",
            off,
            target,
        )


def test_existing_transitions_cannot_smuggle_semantic_effect_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    evidence = _semantic_effect_maturity_file(tmp_path)
    effect_shadow = _semantic_effect_environment(
        off,
        mode="shadow",
        evidence_file=evidence,
    )
    supervisor_shadow = _semantic_supervisor_environment(off, mode="shadow")
    supervisor_and_effect_shadow = _semantic_effect_environment(
        supervisor_shadow,
        mode="shadow",
        evidence_file=evidence,
    )

    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "semantic_supervisor_shadow_enable",
            off,
            supervisor_and_effect_shadow,
        )
    with pytest.raises(
        operator.ReleaseFailure,
        match="nonsemantic_transition_changed_semantic_supervisor_environment",
    ):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "obsidian_enable",
            off,
            effect_shadow,
        )
    disabled = off.replace(
        b"FRIDAY_SECONDARY_LLM_ENABLED=1\n",
        b"FRIDAY_SECONDARY_LLM_ENABLED=0\n",
        1,
    )
    effect_and_secondary = _semantic_effect_environment(
        disabled,
        mode="shadow",
        evidence_file=evidence,
    )
    with pytest.raises(operator.ReleaseFailure):
        operator._validate_staged_environment_transition(  # noqa: SLF001
            "secondary_assist_to_disabled",
            off,
            effect_and_secondary,
        )


def test_semantic_effect_health_contract_is_exact_and_inert() -> None:
    assert (  # noqa: SLF001
        expected_effect_capability_snapshot().digest_hex()
    ) == operator._SEMANTIC_EFFECT_EXPECTED_REGISTRY_BINDING_SHA256
    assert operator._SEMANTIC_EFFECT_POLICY_ID == (  # noqa: SLF001
        semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID
    )
    assert operator._SEMANTIC_EFFECT_POLICY_SHA256 == (  # noqa: SLF001
        semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256
    )
    assert operator._SEMANTIC_EFFECT_MATURITY_POLICY_SHA256 == (  # noqa: SLF001
        SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256
    )
    identity = operator._SemanticEffectMaturityIdentity(  # noqa: SLF001
        evidence_sha256="a" * 64,
        maturity_facts_sha256="b" * 64,
        source_revision_sha256="c" * 64,
        registry_binding_sha256="d" * 64,
        effect_registry_binding_sha256="f" * 64,
    )
    payload = {
        "semantic_supervisor_effect": {
            "schema": "friday.semantic-supervisor-effect-shadow-health.v1",
            "installed": True,
            "requested_mode": "shadow",
            "effective_mode": "shadow",
            "maturity_accepted": True,
            "policy_id": semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
            "policy_sha256": (semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256),
            "evidence_sha256": identity.evidence_sha256,
            "maturity_facts_sha256": identity.maturity_facts_sha256,
            "source_revision_sha256": identity.source_revision_sha256,
            "registry_binding_sha256": identity.registry_binding_sha256,
            "effect_registry_binding_sha256": identity.effect_registry_binding_sha256,
            "execution_authorized": False,
            "publication_authorized": False,
        }
    }
    assert operator._semantic_effect_health_identity_matches(  # noqa: SLF001
        payload,
        expected_mode="shadow",
        expected_identity=identity,
    )
    for key, value in (
        ("execution_authorized", True),
        ("publication_authorized", True),
        ("maturity_accepted", False),
        ("effective_mode", "off"),
        ("evidence_sha256", "e" * 64),
        ("maturity_facts_sha256", "e" * 64),
        ("source_revision_sha256", "e" * 64),
        ("registry_binding_sha256", "e" * 64),
        ("effect_registry_binding_sha256", "e" * 64),
    ):
        mutated = json.loads(json.dumps(payload))
        mutated["semantic_supervisor_effect"][key] = value
        assert not operator._semantic_effect_health_identity_matches(  # noqa: SLF001
            mutated,
            expected_mode="shadow",
            expected_identity=identity,
        )
    payload["semantic_supervisor_effect"].update(
        {
            "installed": False,
            "requested_mode": "off",
            "effective_mode": "off",
            "maturity_accepted": False,
            "evidence_sha256": "",
            "maturity_facts_sha256": "",
            "source_revision_sha256": "",
            "registry_binding_sha256": "",
            "effect_registry_binding_sha256": "",
        }
    )
    assert operator._semantic_effect_health_identity_matches(  # noqa: SLF001
        payload,
        expected_mode="off",
    )


def test_existing_secondary_disable_preserves_canonical_semantic_supervisor_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
    disabled = off.replace(
        b"FRIDAY_SECONDARY_LLM_ENABLED=1\n",
        b"FRIDAY_SECONDARY_LLM_ENABLED=0\n",
        1,
    )

    operator._validate_staged_environment_transition(  # noqa: SLF001
        "secondary_assist_to_disabled",
        off,
        disabled,
    )
    assert operator._semantic_supervisor_environment_bytes(disabled) == (  # noqa: SLF001
        operator._semantic_supervisor_environment_bytes(off)  # noqa: SLF001
    )


def _durable_postbackup_terminal(
    config: operator.SystemdConfig,
    *,
    candidate: operator.ReleaseIdentity,
    current: operator.ReleaseIdentity,
    terminal_phase: str,
) -> tuple[operator.DurableActivationJournal, operator.DatabaseBackup]:
    connection = sqlite3.connect(config.database)
    try:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta VALUES('schema_version','34')")
        connection.commit()
    finally:
        connection.close()
    config.database.chmod(0o600)
    backup = operator._exact_sqlite_backup(config)  # noqa: SLF001
    journal = operator.DurableActivationJournal(
        config.state_dir / "immutable-release-activation.v1.json",
        backup_root=config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(config),  # noqa: SLF001
        obsidian_mode=config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(config),  # noqa: SLF001
    )
    journal.begin(candidate=candidate, previous=current, fallback=current)
    for phase in (
        "bridge_stop_attempted",
        "backend_stop_attempted",
        "writers_quiesced",
        "leases_acquired",
        "backup_complete",
    ):
        journal.record(phase, backup=backup if phase == "backup_complete" else None)
    if terminal_phase == "rolled_back":
        journal.record("migration_attempted", database_mutation_possible=True)
        journal.record("rollback_stop_attempted", database_mutation_possible=True)
        journal.record("rollback_anchor_attempted", database_mutation_possible=True)
        journal.record(
            "rollback_backend_start_attempted",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="fallback",
        )
        journal.record(
            "rollback_backend_accepted",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="fallback",
        )
        journal.record(
            "rollback_bridge_start_attempted",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="fallback",
        )
        journal.record(
            "rolled_back",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="fallback",
            terminal_receipt_sha256="d" * 64,
        )
    else:
        assert terminal_phase == "recovered"
        journal.record("recovery_stop_attempted", database_mutation_possible=True)
        journal.record("recovery_restore_attempted", database_mutation_possible=True)
        journal.record("recovery_anchor_attempted", database_mutation_possible=True)
        journal.record(
            "recovery_backend_start_attempted",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="previous",
        )
        journal.record(
            "recovery_backend_accepted",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="previous",
        )
        journal.record(
            "recovery_bridge_start_attempted",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="previous",
        )
        journal.record(
            "recovered",
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="previous",
            terminal_receipt_sha256="e" * 64,
        )
    return journal, backup


def _secondary_staged_transition_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> tuple[operator.SystemdConfig, Path, str, operator.SystemdConfig]:
    if transition == "secondary_shadow_enable":
        base = _systemd_test_port(tmp_path)
        staged, _target, target_sha256 = _secondary_shadow_stage(base, monkeypatch)
    elif transition == "secondary_shadow_disable":
        base, _enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
        staged, _target, target_sha256 = _secondary_shadow_disable_stage(base)
    elif transition == "secondary_shadow_to_private_shadow":
        base, _enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
        staged, _target, target_sha256 = _secondary_shadow_to_private_shadow_stage(base)
    elif transition == "secondary_shadow_to_assist":
        base, _enabled = _secondary_private_shadow_enabled_port(tmp_path, monkeypatch)
        staged, _target, target_sha256 = _secondary_shadow_to_assist_stage(base)
    elif transition == "secondary_assist_enable_document_map_shadow":
        base, _enabled = _secondary_assist_enabled_port(tmp_path, monkeypatch)
        staged, _target, target_sha256 = _secondary_document_map_stage(base, mode="shadow")
    else:
        assert transition == "secondary_assist_to_disabled"
        base, _enabled = _secondary_assist_enabled_port(tmp_path, monkeypatch)
        staged, _target, target_sha256 = _secondary_assist_to_disabled_stage(base)
    staged_config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )
    return (
        base.config,
        staged,
        target_sha256,
        operator._activation_target_config(staged_config),  # noqa: SLF001
    )


def _semantic_supervisor_staged_transition_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    *,
    legacy_implicit_off: bool = False,
) -> tuple[operator.SystemdConfig, Path, bytes, str]:
    if transition in {
        "semantic_supervisor_effect_shadow_enable",
        "semantic_supervisor_effect_shadow_disable",
    }:
        assert not legacy_implicit_off
        base, off = _semantic_supervisor_off_port(tmp_path, monkeypatch)
        evidence = _semantic_effect_maturity_file(tmp_path, stem=f"{transition}-maturity")
        shadow = _semantic_effect_environment(
            off,
            mode="shadow",
            evidence_file=evidence,
        )
        if transition == "semantic_supervisor_effect_shadow_enable":
            target = shadow
        else:
            base.config.env_file.write_bytes(shadow)
            base.config.env_file.chmod(0o600)
            base = operator.SystemdActivationPort(
                replace(
                    base.config,
                    env_file_sha256=hashlib.sha256(shadow).hexdigest(),
                )
            )
            target = off
        staged = base.config.state_dir / f"{transition}.env"
        staged.write_bytes(target)
        staged.chmod(0o600)
        return base.config, staged, target, hashlib.sha256(target).hexdigest()
    if transition == "semantic_supervisor_shadow_enable":
        base, _predecessor = (
            _secondary_document_map_assist_enabled_port(tmp_path, monkeypatch)
            if legacy_implicit_off
            else _semantic_supervisor_off_port(tmp_path, monkeypatch)
        )
        mode = "shadow"
    else:
        assert transition == "semantic_supervisor_shadow_disable"
        assert not legacy_implicit_off
        base, _predecessor = _semantic_supervisor_shadow_port(tmp_path, monkeypatch)
        mode = "off"
    staged, target, target_sha256 = _semantic_supervisor_stage(base, mode=mode)
    return base.config, staged, target, target_sha256


@pytest.mark.parametrize(
    ("transition", "legacy_implicit_off"),
    [
        ("semantic_supervisor_shadow_enable", False),
        ("semantic_supervisor_shadow_enable", True),
        ("semantic_supervisor_shadow_disable", False),
        ("semantic_supervisor_effect_shadow_enable", False),
        ("semantic_supervisor_effect_shadow_disable", False),
    ],
)
@pytest.mark.parametrize("interruption", ["before_replace", "after_replace", "after_unlink"])
def test_systemd_port_replays_semantic_supervisor_transition_after_each_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    legacy_implicit_off: bool,
    interruption: str,
) -> None:
    current_config, staged, target, target_sha256 = _semantic_supervisor_staged_transition_case(
        tmp_path,
        monkeypatch,
        transition,
        legacy_implicit_off=legacy_implicit_off,
    )
    predecessor = current_config.env_file.read_bytes()
    staged_config = replace(
        current_config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )
    descriptor = (transition, current_config.env_file_sha256, staged, target_sha256)
    port = operator.SystemdActivationPort(staged_config)
    durable_replace = operator._replace_private_durable  # noqa: SLF001
    fsync_directory = operator._fsync_directory  # noqa: SLF001

    def interrupt_replace(path: Path, value: bytes) -> None:
        if interruption == "after_replace":
            durable_replace(path, value)
        raise RuntimeError("synthetic interruption")

    def interrupt_after_unlink(path: Path) -> None:
        if path == staged.parent and not staged.exists():
            raise RuntimeError("synthetic interruption")
        fsync_directory(path)

    if interruption == "after_unlink":
        monkeypatch.setattr(operator, "_fsync_directory", interrupt_after_unlink)
    else:
        monkeypatch.setattr(operator, "_replace_private_durable", interrupt_replace)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        port.activate_staged_config_transition(*descriptor)

    assert current_config.env_file.read_bytes() == (
        predecessor if interruption == "before_replace" else target
    )
    assert staged.exists() is (interruption != "after_unlink")
    monkeypatch.setattr(operator, "_replace_private_durable", durable_replace)
    monkeypatch.setattr(operator, "_fsync_directory", fsync_directory)
    resumed = operator.SystemdActivationPort(staged_config)
    resumed.activate_staged_config_transition(*descriptor)
    assert current_config.env_file.read_bytes() == target
    assert not staged.exists()


def test_systemd_port_activates_exact_secondary_finalist_shadow_without_journaling_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    staged, target, target_sha256 = _secondary_shadow_stage(base, monkeypatch)
    config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_enable",
    )
    port = operator.SystemdActivationPort(config)
    descriptor = ("secondary_shadow_enable", predecessor_sha256, staged, target_sha256)
    target_config = operator._activation_target_config(config)  # noqa: SLF001
    journal_path = base.config.state_dir / "immutable-release-activation.v1.json"
    journal = operator.DurableActivationJournal(
        journal_path,
        backup_root=base.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(target_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            target_config
        ),
        obsidian_mode=target_config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target_config),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_enable",
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    journal_raw = journal_path.read_bytes()
    assert ("a" * 64).encode("ascii") not in journal_raw
    assert b"secondary_shadow_enable" in journal_raw

    port.validate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)

    assert port.config.env_file.read_bytes() == target
    assert port.config.staged_config_transition == ""
    assert not staged.exists()


@pytest.mark.parametrize("private_shadow", [False, True])
def test_systemd_port_disables_exact_secondary_finalist_shadow_with_one_admission_bit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
    private_shadow: bool,
) -> None:
    unrelated = (
        b"# operator-owned bytes stay exact\r\nFRIDAY_PROFILE=production\nFRIDAY_OBSIDIAN_ENABLED=1\r\n"
    )
    enabled_port = (
        _secondary_private_shadow_enabled_port if private_shadow else _secondary_shadow_enabled_port
    )
    base, enabled = enabled_port(
        tmp_path,
        monkeypatch,
        unrelated=unrelated,
    )
    predecessor_sha256 = hashlib.sha256(enabled).hexdigest()
    disabled = _secondary_shadow_disabled_environment(enabled)
    staged, target, target_sha256 = _secondary_shadow_disable_stage(base, target=disabled)
    config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_disable",
    )
    port = operator.SystemdActivationPort(config)
    descriptor = ("secondary_shadow_disable", predecessor_sha256, staged, target_sha256)
    target_config = operator._activation_target_config(config)  # noqa: SLF001
    journal_path = base.config.state_dir / "immutable-release-activation.v1.json"
    journal = operator.DurableActivationJournal(
        journal_path,
        backup_root=base.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(target_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            target_config
        ),
        obsidian_mode=target_config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target_config),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_disable",
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )

    journal_raw = journal_path.read_bytes()
    assert ("a" * 64).encode("ascii") not in journal_raw
    assert str(base.config.friday_home / "secondary-ca.pem").encode() not in journal_raw
    assert b"secondary_shadow_disable" in journal_raw
    port.validate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)

    assert port.config.env_file.read_bytes() == target
    enabled_values, enabled_unrelated = operator._secondary_environment_view(enabled)  # noqa: SLF001
    target_values, target_unrelated = operator._secondary_environment_view(target)  # noqa: SLF001
    assert target_values == {**enabled_values, "FRIDAY_SECONDARY_LLM_ENABLED": "0"}
    assert target_values["FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT"] == ("1" if private_shadow else "0")
    assert enabled_unrelated == target_unrelated == unrelated
    assert target == enabled.replace(
        b"FRIDAY_SECONDARY_LLM_ENABLED=1\n",
        b"FRIDAY_SECONDARY_LLM_ENABLED=0\n",
        1,
    )
    assert ("a" * 64).encode() in target
    assert b"FRIDAY_OBSIDIAN_ENABLED=1\r\n" in target
    assert port.config.staged_config_transition == ""
    assert not staged.exists()


def test_systemd_port_rejects_direct_public_shadow_to_assist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, public_shadow = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    assist = _secondary_assist_environment(_secondary_private_shadow_environment(public_shadow))
    staged, _target, target_sha256 = _secondary_shadow_to_assist_stage(base, target=assist)

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_shadow_to_assist_predecessor_not_private_shadow",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_to_assist",
            )
        )
    assert base.config.env_file.read_bytes() == public_shadow


@pytest.mark.parametrize(
    ("transition", "target_mutation", "failure"),
    [
        (
            "secondary_shadow_to_private_shadow",
            "mode",
            "secondary_shadow_to_private_shadow_environment_invalid",
        ),
        (
            "secondary_shadow_to_assist",
            "private_bit",
            "secondary_shadow_to_assist_environment_invalid",
        ),
    ],
)
def test_systemd_port_rejects_secondary_staged_transition_that_changes_two_policy_bits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    target_mutation: str,
    failure: str,
) -> None:
    if transition == "secondary_shadow_to_private_shadow":
        base, public_shadow = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
        target = _secondary_private_shadow_environment(public_shadow).replace(
            b"FRIDAY_SECONDARY_LLM_MODE=shadow\n",
            b"FRIDAY_SECONDARY_LLM_MODE=assist\n",
        )
        staged, _target, target_sha256 = _secondary_shadow_to_private_shadow_stage(base, target=target)
    else:
        assert target_mutation == "private_bit"
        base, private_shadow = _secondary_private_shadow_enabled_port(tmp_path, monkeypatch)
        target = _secondary_assist_environment(private_shadow).replace(
            b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1\n",
            b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n",
        )
        staged, _target, target_sha256 = _secondary_shadow_to_assist_stage(base, target=target)

    with pytest.raises(operator.ReleaseFailure, match=failure):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition=transition,
            )
        )


@pytest.mark.parametrize(
    "transition",
    [
        "secondary_shadow_to_private_shadow",
        "secondary_shadow_to_assist",
        "secondary_assist_to_disabled",
        "secondary_assist_enable_document_map_shadow",
    ],
)
def test_systemd_port_applies_exact_secondary_assist_transitions_without_journaling_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
    transition: str,
) -> None:
    unrelated = b"# operator-owned bytes stay exact\r\nFRIDAY_PROFILE=production\n"
    if transition == "secondary_shadow_to_private_shadow":
        base, predecessor = _secondary_shadow_enabled_port(
            tmp_path,
            monkeypatch,
            unrelated=unrelated,
        )
        staged, target, target_sha256 = _secondary_shadow_to_private_shadow_stage(base)
    elif transition == "secondary_shadow_to_assist":
        base, predecessor = _secondary_private_shadow_enabled_port(
            tmp_path,
            monkeypatch,
            unrelated=unrelated,
        )
        staged, target, target_sha256 = _secondary_shadow_to_assist_stage(base)
    elif transition == "secondary_assist_enable_document_map_shadow":
        base, predecessor = _secondary_assist_enabled_port(
            tmp_path,
            monkeypatch,
            unrelated=unrelated,
        )
        staged, target, target_sha256 = _secondary_document_map_stage(base, mode="shadow")
    else:
        base, predecessor = _secondary_assist_enabled_port(
            tmp_path,
            monkeypatch,
            unrelated=unrelated,
        )
        staged, target, target_sha256 = _secondary_assist_to_disabled_stage(base)
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    staged_config = replace(
        base.config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )
    target_config = operator._activation_target_config(staged_config)  # noqa: SLF001
    port = operator.SystemdActivationPort(staged_config)
    journal_path = base.config.state_dir / "immutable-release-activation.v1.json"
    journal = operator.DurableActivationJournal(
        journal_path,
        backup_root=base.config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(target_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            target_config
        ),
        obsidian_mode=target_config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target_config),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )

    journal_raw = journal_path.read_bytes()
    assert ("a" * 64).encode("ascii") not in journal_raw
    assert str(base.config.friday_home / "secondary-ca.pem").encode() not in journal_raw
    assert transition.encode("ascii") in journal_raw
    descriptor = (transition, predecessor_sha256, staged, target_sha256)
    port.validate_staged_config_transition(*descriptor)
    port.select_predecessor_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == predecessor
    assert port.config.env_file_sha256 == predecessor_sha256
    port.activate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)

    assert port.config.env_file.read_bytes() == target
    predecessor_values, predecessor_unrelated = operator._secondary_environment_view(  # noqa: SLF001
        predecessor
    )
    target_values, target_unrelated = operator._secondary_environment_view(target)  # noqa: SLF001
    assert predecessor_unrelated == target_unrelated == unrelated
    assert target_values["FRIDAY_SECONDARY_LLM_API_KEY"] == predecessor_values["FRIDAY_SECONDARY_LLM_API_KEY"]
    assert target_values["FRIDAY_SECONDARY_LLM_CA_FILE"] == predecessor_values["FRIDAY_SECONDARY_LLM_CA_FILE"]
    if transition == "secondary_shadow_to_private_shadow":
        assert target_values == {
            **predecessor_values,
            "FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT": "1",
        }
    elif transition == "secondary_shadow_to_assist":
        assert target_values == {
            **predecessor_values,
            "FRIDAY_SECONDARY_LLM_MODE": "assist",
        }
    elif transition == "secondary_assist_enable_document_map_shadow":
        assert target_values == {
            **predecessor_values,
            "FRIDAY_SECONDARY_LLM_WORKLOADS": "document_map,extract",
            "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE": "shadow",
        }
    else:
        assert target_values == {
            **predecessor_values,
            "FRIDAY_SECONDARY_LLM_ENABLED": "0",
        }
    assert not staged.exists()


@pytest.mark.parametrize(
    "transition",
    [
        "secondary_shadow_to_private_shadow",
        "secondary_shadow_to_assist",
        "secondary_assist_to_disabled",
        "secondary_assist_enable_document_map_shadow",
    ],
)
@pytest.mark.parametrize("mutation", ["api_key", "ca_path", "unrelated", "reordered"])
def test_systemd_port_rejects_secondary_assist_transition_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    mutation: str,
) -> None:
    if transition == "secondary_shadow_to_private_shadow":
        base, _predecessor = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
        target = _secondary_private_shadow_environment(base.config.env_file.read_bytes())
        stage = _secondary_shadow_to_private_shadow_stage
    elif transition == "secondary_shadow_to_assist":
        base, _predecessor = _secondary_private_shadow_enabled_port(tmp_path, monkeypatch)
        target = _secondary_assist_environment(base.config.env_file.read_bytes())
        stage = _secondary_shadow_to_assist_stage
    elif transition == "secondary_assist_enable_document_map_shadow":
        base, _predecessor = _secondary_assist_enabled_port(tmp_path, monkeypatch)
        target = _secondary_document_map_environment(base.config.env_file.read_bytes(), mode="shadow")

        def stage(
            current: operator.SystemdActivationPort,
            *,
            target: bytes,
        ) -> tuple[Path, bytes, str]:
            return _secondary_document_map_stage(current, mode="shadow", target=target)

    else:
        base, _predecessor = _secondary_assist_enabled_port(tmp_path, monkeypatch)
        target = _secondary_assist_disabled_environment(base.config.env_file.read_bytes())
        stage = _secondary_assist_to_disabled_stage
    if mutation == "api_key":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"a" * 64,
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"b" * 64,
        )
    elif mutation == "ca_path":
        current_ca = base.config.friday_home / "secondary-ca.pem"
        alternate_ca = base.config.friday_home / "alternate-secondary-ca.pem"
        alternate_ca.write_bytes(current_ca.read_bytes())
        alternate_ca.chmod(0o600)
        target = target.replace(str(current_ca).encode(), str(alternate_ca).encode())
    elif mutation == "unrelated":
        target = target.replace(b"FRIDAY_PROFILE=production\n", b"FRIDAY_PROFILE=staging\n")
    else:
        assert mutation == "reordered"
        first = b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n"
        second = target[target.index(first) + len(first) :].splitlines(keepends=True)[0]
        target = target.replace(first + second, second + first)
    staged, _target, target_sha256 = stage(base, target=target)

    with pytest.raises(operator.ReleaseFailure):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition=transition,
            )
        )


def test_document_map_cannot_skip_its_discarded_shadow_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, assist = _secondary_assist_enabled_port(tmp_path, monkeypatch)
    direct_assist = _secondary_document_map_environment(assist, mode="assist")
    staged, _target, target_sha256 = _secondary_document_map_stage(
        base,
        mode="shadow",
        target=direct_assist,
    )

    with pytest.raises(operator.ReleaseFailure, match="secondary_document_map_shadow_environment_invalid"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_assist_enable_document_map_shadow",
            )
        )

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_document_map_assist_predecessor_not_shadow",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_document_map_shadow_to_assist",
            )
        )


def test_assist_disable_preserves_document_map_shadow_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, shadow = _secondary_document_map_shadow_enabled_port(tmp_path, monkeypatch)
    enabled = shadow
    disabled = _secondary_assist_disabled_environment(enabled)
    staged, _target, target_sha256 = _secondary_assist_to_disabled_stage(base, target=disabled)

    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=target_sha256,
            staged_config_transition="secondary_assist_to_disabled",
        )
    )
    port.activate_staged_config_transition(
        "secondary_assist_to_disabled",
        base.config.env_file_sha256,
        staged,
        target_sha256,
    )
    values, _unrelated = operator._secondary_environment_view(port.config.env_file.read_bytes())  # noqa: SLF001
    assert values["FRIDAY_SECONDARY_LLM_ENABLED"] == "0"
    assert values["FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE"] == "shadow"
    assert values["FRIDAY_SECONDARY_LLM_WORKLOADS"] == "document_map,extract"


@pytest.mark.parametrize(
    "transition",
    [
        "secondary_shadow_to_private_shadow",
        "secondary_shadow_to_assist",
        "secondary_assist_to_disabled",
        "secondary_assist_enable_document_map_shadow",
    ],
)
@pytest.mark.parametrize("interruption", ["before_replace", "after_replace", "after_unlink"])
def test_systemd_port_replays_secondary_assist_transition_after_each_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    interruption: str,
) -> None:
    current_config, staged, target_sha256, _target_config = _secondary_staged_transition_case(
        tmp_path,
        monkeypatch,
        transition,
    )
    predecessor = current_config.env_file.read_bytes()
    target = staged.read_bytes()
    staged_config = replace(
        current_config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )
    port = operator.SystemdActivationPort(staged_config)
    descriptor = (transition, current_config.env_file_sha256, staged, target_sha256)
    durable_replace = operator._replace_private_durable  # noqa: SLF001
    fsync_directory = operator._fsync_directory  # noqa: SLF001

    def interrupt_replace(path: Path, value: bytes) -> None:
        if interruption == "after_replace":
            durable_replace(path, value)
        raise RuntimeError("synthetic interruption")

    def interrupt_after_unlink(path: Path) -> None:
        if path == staged.parent and not staged.exists():
            raise RuntimeError("synthetic interruption")
        fsync_directory(path)

    if interruption == "after_unlink":
        monkeypatch.setattr(operator, "_fsync_directory", interrupt_after_unlink)
    else:
        monkeypatch.setattr(operator, "_replace_private_durable", interrupt_replace)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        port.activate_staged_config_transition(*descriptor)

    assert current_config.env_file.read_bytes() == (
        predecessor if interruption == "before_replace" else target
    )
    assert staged.exists() is (interruption != "after_unlink")
    monkeypatch.setattr(operator, "_replace_private_durable", durable_replace)
    monkeypatch.setattr(operator, "_fsync_directory", fsync_directory)
    resumed = operator.SystemdActivationPort(staged_config)
    resumed.activate_staged_config_transition(*descriptor)
    assert current_config.env_file.read_bytes() == target
    assert not staged.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "enabled",
        "mode",
        "missing_profile",
        "api_key",
        "ca_file",
        "profile",
        "reordered",
        "unknown",
        "duplicate",
        "unicode",
        "legacy",
    ],
)
def test_systemd_port_rejects_noncanonical_secondary_shadow_disable_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    base, enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    target = _secondary_shadow_disabled_environment(enabled)
    if mutation == "enabled":
        target = target.replace(b"FRIDAY_SECONDARY_LLM_ENABLED=0\n", b"FRIDAY_SECONDARY_LLM_ENABLED=1\n")
    elif mutation == "mode":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_MODE=shadow\n", b"FRIDAY_SECONDARY_LLM_MODE=disabled\n"
        )
    elif mutation == "missing_profile":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_PROFILE="
            b"gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f\n",
            b"",
        )
    elif mutation == "api_key":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"a" * 64, b"FRIDAY_SECONDARY_LLM_API_KEY=short"
        )
    elif mutation == "ca_file":
        target = target.replace(
            f"FRIDAY_SECONDARY_LLM_CA_FILE={base.config.friday_home / 'secondary-ca.pem'}".encode(),
            f"FRIDAY_SECONDARY_LLM_CA_FILE={base.config.friday_home / 'other-ca.pem'}".encode(),
        )
    elif mutation == "profile":
        target = target.replace(
            b"FRIDAY_SECONDARY_LLM_PROFILE="
            b"gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f",
            b"FRIDAY_SECONDARY_LLM_PROFILE=wrong",
        )
    elif mutation == "reordered":
        first = b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n"
        second = b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n"
        target = target.replace(first + second, second + first)
    elif mutation == "unknown":
        target += b"FRIDAY_SECONDARY_LLM_UNKNOWN=1\n"
    elif mutation == "duplicate":
        target += b"FRIDAY_SECONDARY_LLM_ENABLED=1\n"
    elif mutation == "unicode":
        target += "\u00a0FRIDAY_SECONDARY_LLM_API_KEY=hidden\n".encode()
    else:
        assert mutation == "legacy"
        target += b"JERICHO_SECONDARY_LLM_ENABLED=1\n"
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=target)

    with pytest.raises(operator.ReleaseFailure):
        operator.SystemdActivationPort(
            replace(
                base.config,
                env_file_sha256=hashlib.sha256(enabled).hexdigest(),
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )
    assert base.config.env_file.read_bytes() == enabled


def test_systemd_port_rejects_secondary_shadow_disable_unrelated_environment_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    target = _secondary_shadow_disabled_environment(enabled).replace(
        b"FRIDAY_PROFILE=production\n",
        b"FRIDAY_PROFILE=staging\n",
    )
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=target)

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_shadow_unrelated_environment_changed",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                env_file_sha256=hashlib.sha256(enabled).hexdigest(),
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


@pytest.mark.parametrize("private_shadow", [False, True])
def test_systemd_port_rejects_secondary_shadow_disable_that_changes_private_bit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_shadow: bool,
) -> None:
    enabled_port = (
        _secondary_private_shadow_enabled_port if private_shadow else _secondary_shadow_enabled_port
    )
    base, enabled = enabled_port(tmp_path, monkeypatch)
    source = (
        b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1\n"
        if private_shadow
        else b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n"
    )
    replacement = (
        b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n"
        if private_shadow
        else b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1\n"
    )
    target = _secondary_shadow_disabled_environment(enabled).replace(source, replacement)
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=target)

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_shadow_disable_predecessor_not_enabled",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FRIDAY_SECONDARY_LLM_MODE", "assist"),
        ("FRIDAY_SECONDARY_LLM_API_KEY", "short"),
        ("FRIDAY_SECONDARY_LLM_MODEL", "friday-secondary-wrong"),
        ("FRIDAY_SECONDARY_LLM_PROFILE", None),
        ("FRIDAY_SECONDARY_LLM_UNKNOWN", "1"),
    ],
)
def test_systemd_port_rejects_secondary_shadow_disable_from_nonexact_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str | None,
) -> None:
    base, enabled = _secondary_shadow_enabled_port(
        tmp_path,
        monkeypatch,
        overrides={key: value},
    )
    ca = base.config.friday_home / "secondary-ca.pem"
    target = _secondary_shadow_environment(
        b"FRIDAY_PROFILE=production\n",
        ca,
        overrides={"FRIDAY_SECONDARY_LLM_ENABLED": "0"},
    )
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=target)

    with pytest.raises(
        operator.ReleaseFailure,
        match=(
            "secondary_shadow_environment_invalid"
            if key == "FRIDAY_SECONDARY_LLM_UNKNOWN"
            else "secondary_shadow_disable_predecessor_not_enabled"
        ),
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                env_file_sha256=hashlib.sha256(enabled).hexdigest(),
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


def test_systemd_port_rejects_secondary_shadow_disable_from_already_disabled_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, disabled = _secondary_shadow_enabled_port(
        tmp_path,
        monkeypatch,
        overrides={"FRIDAY_SECONDARY_LLM_ENABLED": "0"},
    )
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=disabled)

    with pytest.raises(operator.ReleaseFailure, match="staged_environment_identity_invalid"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


@pytest.mark.parametrize(
    "source_suffix",
    [
        b"FRIDAY_SECONDARY_LLM_MODE=assist\n",
        "\u00a0FRIDAY_SECONDARY_LLM_MODE=assist\n".encode(),
        b"JERICHO_SECONDARY_LLM_ENABLED=1\n",
    ],
)
def test_systemd_port_rejects_hidden_duplicate_or_legacy_secondary_disable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_suffix: bytes,
) -> None:
    base, enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    forged = enabled + source_suffix
    base.config.env_file.write_bytes(forged)
    base.config.env_file.chmod(0o600)
    base = operator.SystemdActivationPort(
        replace(
            base.config,
            env_file_sha256=hashlib.sha256(forged).hexdigest(),
        )
    )
    target = _secondary_shadow_environment(
        b"FRIDAY_PROFILE=production\n",
        base.config.friday_home / "secondary-ca.pem",
        overrides={"FRIDAY_SECONDARY_LLM_ENABLED": "0"},
    )
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=target)

    with pytest.raises(operator.ReleaseFailure):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


def test_systemd_port_rejects_reordered_secondary_shadow_disable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    first = b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n"
    second = b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n"
    reordered = enabled.replace(first + second, second + first)
    base.config.env_file.write_bytes(reordered)
    base.config.env_file.chmod(0o600)
    base = operator.SystemdActivationPort(
        replace(base.config, env_file_sha256=hashlib.sha256(reordered).hexdigest())
    )
    target = _secondary_shadow_disabled_environment(enabled)
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base, target=target)

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_shadow_disable_predecessor_not_enabled",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


def test_systemd_port_rejects_secondary_shadow_disable_after_source_ca_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _enabled = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    (base.config.friday_home / "secondary-ca.pem").write_bytes(b"drifted-secondary-ca\n")
    staged, _target, target_sha256 = _secondary_shadow_disable_stage(base)

    with pytest.raises(operator.ReleaseFailure, match="secondary_shadow_ca_digest_mismatch"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_disable",
            )
        )


@pytest.mark.parametrize("private_shadow", [False, True])
@pytest.mark.parametrize("interruption", ["before_replace", "after_replace", "after_unlink"])
def test_systemd_port_resumes_secondary_shadow_disable_after_each_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
    private_shadow: bool,
) -> None:
    enabled_port = (
        _secondary_private_shadow_enabled_port if private_shadow else _secondary_shadow_enabled_port
    )
    base, enabled = enabled_port(tmp_path, monkeypatch)
    predecessor_sha256 = hashlib.sha256(enabled).hexdigest()
    staged, disabled, target_sha256 = _secondary_shadow_disable_stage(base)
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=target_sha256,
            staged_config_transition="secondary_shadow_disable",
        )
    )
    descriptor = ("secondary_shadow_disable", predecessor_sha256, staged, target_sha256)
    durable_replace = operator._replace_private_durable  # noqa: SLF001
    fsync_directory = operator._fsync_directory  # noqa: SLF001

    def interrupt_replace(path: Path, value: bytes) -> None:
        if interruption == "after_replace":
            durable_replace(path, value)
        raise RuntimeError("synthetic interruption")

    def interrupt_after_unlink(path: Path) -> None:
        if path == staged.parent and not staged.exists():
            raise RuntimeError("synthetic interruption")
        fsync_directory(path)

    if interruption == "after_unlink":
        monkeypatch.setattr(operator, "_fsync_directory", interrupt_after_unlink)
    else:
        monkeypatch.setattr(operator, "_replace_private_durable", interrupt_replace)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        port.activate_staged_config_transition(*descriptor)

    assert port.config.env_file.read_bytes() == (enabled if interruption == "before_replace" else disabled)
    assert staged.exists() is (interruption != "after_unlink")
    monkeypatch.setattr(operator, "_replace_private_durable", durable_replace)
    monkeypatch.setattr(operator, "_fsync_directory", fsync_directory)
    port.activate_staged_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == disabled
    values, _unrelated = operator._secondary_environment_view(disabled)  # noqa: SLF001
    assert values["FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT"] == ("1" if private_shadow else "0")
    assert port.config.env_file_sha256 == target_sha256
    assert not staged.exists()


def test_systemd_port_accepts_standard_explicit_disabled_secondary_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes() + (
        b"FRIDAY_SECONDARY_LLM_ENABLED=0\n"
        b"FRIDAY_SECONDARY_LLM_MODE=disabled\n"
        b"FRIDAY_SECONDARY_LLM_BASE_URL=\n"
        b"FRIDAY_SECONDARY_LLM_MODEL=\n"
        b"FRIDAY_SECONDARY_LLM_API_KEY=\n"
        b"FRIDAY_SECONDARY_LLM_CA_FILE=\n"
        b"FRIDAY_SECONDARY_LLM_PROFILE=\n"
    )
    base.config.env_file.write_bytes(predecessor)
    base.config.env_file.chmod(0o600)
    base = operator.SystemdActivationPort(
        replace(
            base.config,
            env_file_sha256=hashlib.sha256(predecessor).hexdigest(),
        )
    )
    staged, _target, target_sha256 = _secondary_shadow_stage(
        base,
        monkeypatch,
        unrelated=b"FRIDAY_PROFILE=production\n",
    )

    port = operator.SystemdActivationPort(
        replace(
            base.config,
            next_env_file=staged,
            next_env_file_sha256=target_sha256,
            staged_config_transition="secondary_shadow_enable",
        )
    )
    port.validate_staged_config_transition(
        "secondary_shadow_enable",
        base.config.env_file_sha256,
        staged,
        target_sha256,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FRIDAY_SECONDARY_LLM_ENABLED", "0"),
        ("FRIDAY_SECONDARY_LLM_MODE", "assist"),
        ("FRIDAY_SECONDARY_LLM_BASE_URL", "https://192.168.1.35:9443/v1"),
        ("FRIDAY_SECONDARY_LLM_MODEL", "friday-secondary-wrong"),
        ("FRIDAY_SECONDARY_LLM_API_KEY", "short"),
        ("FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS", "8192"),
        ("FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY", "2"),
        ("FRIDAY_SECONDARY_LLM_PROFILE", "gptoss20b-wrong"),
        ("FRIDAY_SECONDARY_LLM_WORKLOADS", "extract,summarize"),
        ("FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT", "1"),
        ("FRIDAY_SECONDARY_LLM_PROFILE", None),
        ("FRIDAY_SECONDARY_LLM_UNKNOWN", "1"),
    ],
)
def test_systemd_port_rejects_nonexact_secondary_shadow_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str | None,
) -> None:
    base = _systemd_test_port(tmp_path)
    staged, _target, target_sha256 = _secondary_shadow_stage(
        base,
        monkeypatch,
        overrides={key: value},
    )

    with pytest.raises(operator.ReleaseFailure, match="secondary_shadow_environment_invalid"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_enable",
            )
        )


def test_systemd_port_rejects_reordered_secondary_shadow_enable_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    staged, target, _target_sha256 = _secondary_shadow_stage(base, monkeypatch)
    first = b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n"
    second = b"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0\n"
    reordered = target.replace(first + second, second + first)
    staged.write_bytes(reordered)
    staged.chmod(0o600)

    with pytest.raises(operator.ReleaseFailure, match="secondary_shadow_environment_invalid"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=hashlib.sha256(reordered).hexdigest(),
                staged_config_transition="secondary_shadow_enable",
            )
        )


def test_systemd_port_rejects_secondary_shadow_unrelated_environment_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    staged, _target, target_sha256 = _secondary_shadow_stage(
        base,
        monkeypatch,
        unrelated=b"FRIDAY_PROFILE=staging\n",
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_shadow_unrelated_environment_changed",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_enable",
            )
        )


def test_systemd_port_rejects_secondary_shadow_from_enabled_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes() + b"FRIDAY_SECONDARY_LLM_ENABLED=1\n"
    base.config.env_file.write_bytes(predecessor)
    base.config.env_file.chmod(0o600)
    base_config = replace(
        base.config,
        env_file_sha256=hashlib.sha256(predecessor).hexdigest(),
    )
    base = operator.SystemdActivationPort(base_config)
    staged, _target, target_sha256 = _secondary_shadow_stage(
        base,
        monkeypatch,
        unrelated=b"FRIDAY_PROFILE=production\n",
    )

    with pytest.raises(operator.ReleaseFailure, match="secondary_shadow_predecessor_not_disabled"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_enable",
            )
        )


@pytest.mark.parametrize(
    "hidden_assignment",
    [
        "\u00a0FRIDAY_SECONDARY_LLM_MODE=assist\n".encode(),
        b"\vFRIDAY_SECONDARY_LLM_MODE=assist\n",
        b"\fFRIDAY_SECONDARY_LLM_MODE=assist\n",
        "FRIDAY_SECONDARY_LLM_MODE\u00a0=assist\n".encode(),
        "FRIDAY_PROFILE=production\u2028FRIDAY_SECONDARY_LLM_MODE=assist\n".encode(),
    ],
)
def test_systemd_port_rejects_runtime_effective_noncanonical_secondary_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hidden_assignment: bytes,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes() + hidden_assignment
    base.config.env_file.write_bytes(predecessor)
    base.config.env_file.chmod(0o600)
    base = operator.SystemdActivationPort(
        replace(
            base.config,
            env_file_sha256=hashlib.sha256(predecessor).hexdigest(),
        )
    )
    staged, _target, target_sha256 = _secondary_shadow_stage(base, monkeypatch)

    with pytest.raises(operator.ReleaseFailure, match="secondary_shadow_environment_invalid"):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_enable",
            )
        )


@pytest.mark.parametrize(
    "legacy_assignment",
    [
        b"JERICHO_SECONDARY_LLM_ENABLED=1\n",
        b"JERICHO_SECONDARY_LLM_BASE_URL=https://192.168.1.35:8443/v1\n",
        "\u00a0JERICHO_SECONDARY_LLM_MODE=assist\n".encode(),
    ],
)
def test_systemd_port_rejects_every_runtime_effective_legacy_secondary_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_assignment: bytes,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes() + legacy_assignment
    base.config.env_file.write_bytes(predecessor)
    base.config.env_file.chmod(0o600)
    base = operator.SystemdActivationPort(
        replace(
            base.config,
            env_file_sha256=hashlib.sha256(predecessor).hexdigest(),
        )
    )
    staged, _target, target_sha256 = _secondary_shadow_stage(base, monkeypatch)

    with pytest.raises(
        operator.ReleaseFailure,
        match="secondary_shadow_legacy_environment_forbidden",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_enable",
            )
        )


@pytest.mark.parametrize("ca_failure", ["digest", "symlink", "relative"])
def test_systemd_port_rejects_unsafe_secondary_shadow_ca(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ca_failure: str,
) -> None:
    base = _systemd_test_port(tmp_path)
    real_ca = base.config.friday_home / "real-secondary-ca.pem"
    real_ca.write_bytes(b"synthetic-secondary-ca\n")
    real_ca.chmod(0o600)
    ca_file = real_ca
    if ca_failure == "symlink":
        ca_file = base.config.friday_home / "linked-secondary-ca.pem"
        ca_file.symlink_to(real_ca)
    staged, _target, target_sha256 = _secondary_shadow_stage(
        base,
        monkeypatch,
        ca_file=ca_file,
        overrides=(
            {"FRIDAY_SECONDARY_LLM_CA_FILE": "relative-secondary-ca.pem"}
            if ca_failure == "relative"
            else None
        ),
    )
    if ca_failure == "digest":
        monkeypatch.setattr(operator, "_SECONDARY_FINALIST_CA_SHA256", "f" * 64)

    with pytest.raises(
        operator.ReleaseFailure,
        match=(
            "secondary_shadow_ca_digest_mismatch" if ca_failure == "digest" else "secondary_shadow_ca_invalid"
        ),
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition="secondary_shadow_enable",
            )
        )


def test_systemd_port_stages_then_activates_environment_idempotently(tmp_path: Path) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    enabled = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    enabled_sha256 = hashlib.sha256(enabled).hexdigest()
    staged = base.config.state_dir / "next.env"
    staged.write_bytes(enabled)
    staged.chmod(0o600)
    operator._obsidian_root(base.config).mkdir(mode=0o700)  # noqa: SLF001
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            obsidian_mode="enabled",
            next_env_file=staged,
            next_env_file_sha256=enabled_sha256,
        )
    )

    descriptor = ("obsidian_enable", predecessor_sha256, staged, enabled_sha256)
    port.validate_staged_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == predecessor
    port.activate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)

    assert port.config.env_file.read_bytes() == enabled
    assert port.config.env_file_sha256 == enabled_sha256
    assert port.config.obsidian_mode == "enabled"
    assert port.config.next_env_file is None
    assert not staged.exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_obsidian_enable_preserves_an_existing_secondary_block_byte_for_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    base, predecessor = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    target = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    target_sha256 = hashlib.sha256(target).hexdigest()
    staged = base.config.state_dir / "obsidian-with-secondary.env"
    staged.write_bytes(target)
    staged.chmod(0o600)
    operator._obsidian_root(base.config).mkdir(mode=0o700)  # noqa: SLF001
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            obsidian_mode="enabled",
            next_env_file=staged,
            next_env_file_sha256=target_sha256,
            staged_config_transition="obsidian_enable" if explicit else "",
        )
    )

    descriptor = ("obsidian_enable", base.config.env_file_sha256, staged, target_sha256)
    port.validate_staged_config_transition(*descriptor)
    port.activate_staged_config_transition(*descriptor)

    assert port.config.env_file.read_bytes() == target
    assert not staged.exists()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("mutation", ["private", "assist", "api_key", "line_ending"])
def test_obsidian_enable_cannot_carry_any_secondary_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    mutation: str,
) -> None:
    base, predecessor = _secondary_shadow_enabled_port(tmp_path, monkeypatch)
    if mutation == "private":
        target = _secondary_private_shadow_environment(predecessor)
    elif mutation == "assist":
        target = _secondary_assist_environment(_secondary_private_shadow_environment(predecessor))
    elif mutation == "api_key":
        target = predecessor.replace(
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"a" * 64,
            b"FRIDAY_SECONDARY_LLM_API_KEY=" + b"b" * 64,
        )
    else:
        assert mutation == "line_ending"
        target = predecessor.replace(
            b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\n",
            b"FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10\r\n",
            1,
        )
    target += b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    staged = base.config.state_dir / "obsidian-secondary-bypass.env"
    staged.write_bytes(target)
    staged.chmod(0o600)
    operator._obsidian_root(base.config).mkdir(mode=0o700)  # noqa: SLF001

    with pytest.raises(
        operator.ReleaseFailure,
        match="nonsecondary_transition_changed_secondary_environment",
    ):
        operator.SystemdActivationPort(
            replace(
                base.config,
                obsidian_mode="enabled",
                next_env_file=staged,
                next_env_file_sha256=hashlib.sha256(target).hexdigest(),
                staged_config_transition="obsidian_enable" if explicit else "",
            )
        )


@pytest.mark.parametrize("interruption", ["before_replace", "after_replace"])
def test_systemd_port_resumes_environment_activation_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    enabled = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    enabled_sha256 = hashlib.sha256(enabled).hexdigest()
    staged = base.config.state_dir / "next.env"
    staged.write_bytes(enabled)
    staged.chmod(0o600)
    operator._obsidian_root(base.config).mkdir(mode=0o700)  # noqa: SLF001
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            obsidian_mode="enabled",
            next_env_file=staged,
            next_env_file_sha256=enabled_sha256,
        )
    )
    descriptor = ("obsidian_enable", predecessor_sha256, staged, enabled_sha256)
    durable_replace = operator._replace_private_durable  # noqa: SLF001

    def interrupt(path: Path, value: bytes) -> None:
        if interruption == "after_replace":
            durable_replace(path, value)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(operator, "_replace_private_durable", interrupt)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        port.activate_staged_config_transition(*descriptor)
    expected_after_interruption = enabled if interruption == "after_replace" else predecessor
    assert port.config.env_file.read_bytes() == expected_after_interruption
    assert staged.read_bytes() == enabled

    monkeypatch.setattr(operator, "_replace_private_durable", durable_replace)
    port.activate_staged_config_transition(*descriptor)
    assert port.config.env_file.read_bytes() == enabled
    assert port.config.env_file_sha256 == enabled_sha256
    assert not staged.exists()


def test_systemd_port_selects_predecessor_without_replacing_canonical_env(tmp_path: Path) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    enabled = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    enabled_sha256 = hashlib.sha256(enabled).hexdigest()
    staged = base.config.state_dir / "next.env"
    staged.write_bytes(enabled)
    staged.chmod(0o600)
    operator._obsidian_root(base.config).mkdir(mode=0o700)  # noqa: SLF001
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            obsidian_mode="enabled",
            next_env_file=staged,
            next_env_file_sha256=enabled_sha256,
        )
    )

    port.select_predecessor_config_transition("obsidian_enable", predecessor_sha256, staged, enabled_sha256)

    assert port.config.env_file.read_bytes() == predecessor
    assert port.config.env_file_sha256 == predecessor_sha256
    assert port.config.obsidian_mode == "disabled"
    assert staged.read_bytes() == enabled


@pytest.mark.parametrize("tamper", ["staged", "current"])
def test_systemd_port_rejects_unbound_staged_environment(
    tmp_path: Path,
    tamper: str,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    enabled = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    enabled_sha256 = hashlib.sha256(enabled).hexdigest()
    staged = base.config.state_dir / "next.env"
    staged.write_bytes(enabled)
    staged.chmod(0o600)
    operator._obsidian_root(base.config).mkdir(mode=0o700)  # noqa: SLF001
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            obsidian_mode="enabled",
            next_env_file=staged,
            next_env_file_sha256=enabled_sha256,
        )
    )
    target = staged if tamper == "staged" else port.config.env_file
    target.write_bytes(b"unbound environment\n")
    target.chmod(0o600)

    with pytest.raises(
        operator.ReleaseFailure,
        match=(
            "next_environment_file_digest_mismatch"
            if tamper == "staged"
            else "staged_canonical_environment_changed"
        ),
    ):
        port.activate_staged_config_transition("obsidian_enable", predecessor_sha256, staged, enabled_sha256)


def test_systemd_release_verification_uses_staged_target_and_canonical_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    predecessor = base.config.env_file.read_bytes()
    enabled = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    staged = base.config.state_dir / "next.env"
    staged.write_bytes(enabled)
    staged.chmod(0o600)
    root = operator._obsidian_root(base.config)  # noqa: SLF001
    root.mkdir(mode=0o700)
    port = operator.SystemdActivationPort(
        replace(
            base.config,
            obsidian_mode="enabled",
            next_env_file=staged,
            next_env_file_sha256=hashlib.sha256(enabled).hexdigest(),
        )
    )
    candidate = operator.ReleaseIdentity(
        tmp_path / "schema35-candidate",
        "a" * 40,
        "0.207.0",
        "b" * 64,
        35,
        operator.MEMORY_VAULT_MODE_CONTRACT,
        operator.VENV_RELOCATION_CONTRACT,
        operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    predecessor_release = replace(
        candidate,
        root=tmp_path / "schema34-previous",
        commit="c" * 40,
        version="0.206.0",
        tree_manifest_sha256="d" * 64,
        max_schema=34,
        obsidian_cutover_contract="",
    )
    monkeypatch.setattr(operator, "verify_release_tree", lambda _release: None)
    monkeypatch.setattr(operator, "installed_surface_smoke", lambda _release: "e" * 64)
    observed_env_files: list[Path] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed_env_files.append(Path(command[5]))
        receipt = {
            "memory_vault_mode": command[10],
            "obsidian_mode": command[11],
            "obsidian_root_sha256": hashlib.sha256(command[12].encode()).hexdigest(),
            "status": "clear",
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=operator._canonical_json(receipt) + b"\n",  # noqa: SLF001
            stderr=b"",
        )

    monkeypatch.setattr(operator.subprocess, "run", run)
    port.verify_release(candidate)
    port.verify_release(predecessor_release, use_predecessor_config=True)

    assert observed_env_files == [staged, base.config.env_file]
    assert port.config.env_file.read_bytes() == predecessor


def test_manager_units_are_exact_anchor_fragments_and_database_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _systemd_test_port(tmp_path)
    candidate_root = tmp_path / "candidate"
    artifacts = candidate_root / "artifacts"
    artifacts.mkdir(parents=True)
    units = operator.render_units(
        anchor=port.config.anchor,
        env_file=port.config.env_file,
        friday_home=port.config.friday_home,
    )
    for name, content in units.items():
        (artifacts / name).write_text(content, encoding="utf-8")
        (port.config.unit_dir / name).write_text(content, encoding="utf-8")
        (port.config.unit_dir / name).chmod(0o600)
        for path, dropin_content in operator._expected_unit_dropins(port.config, name):  # noqa: SLF001
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            path.write_bytes(dropin_content)
            path.chmod(0o600)
    candidate = operator.ReleaseIdentity(candidate_root, "c" * 40, "0.206.0", "d" * 64, 34)
    environment = {
        unit: (
            f"FRIDAY_HOME={port.config.friday_home} "
            f"FRIDAY_DATABASE_PATH={port.config.database} FRIDAY_DATABASE_MUST_EXIST=1 "
            f"TMPDIR={operator._unit_runtime_tmp_directory(unit)}"  # noqa: SLF001
        ).encode()
        for unit in units
    }
    extra_exec = b""
    extra_manager_dropin = ""
    security_properties = {
        "LimitCORE": b"0\n",
        "MemorySwapMax": b"0\n",
        "PrivateTmp": b"no\n",
        "PrivateUsers": b"no\n",
        "RuntimeDirectoryMode": b"0700\n",
        "RuntimeDirectoryPreserve": b"no\n",
    }
    runtime_directories = {
        unit: operator._unit_runtime_directory_name(unit)  # noqa: SLF001
        for unit in units
    }

    def exec_record(argv: list[str]) -> bytes:
        joined = " ".join(argv)
        return (
            f"{{ path={argv[0]} ; argv[]={joined} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
        ).encode()

    def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        stdout = b""
        if "--property=ExecStart" in arguments:
            role = "server" if port.config.backend_unit in arguments else "telegram-bridge"
            stdout = exec_record(
                [
                    str(port.config.anchor / "venv/bin/python"),
                    "-I",
                    "-B",
                    "-m",
                    "friday.cli",
                    "--env-file",
                    str(port.config.env_file),
                    role,
                ]
            )
        elif "--property=ExecStartPre" in arguments:
            stdout = exec_record(["/usr/bin/test", "-s", str(port.config.database)])
        elif any(
            f"--property={name}" in arguments
            for name in ("ExecCondition", "ExecStartPost", "ExecReload", "ExecStop", "ExecStopPost")
        ):
            stdout = extra_exec
        elif "--property=FragmentPath" in arguments:
            stdout = str(port.config.unit_dir / arguments[1]).encode() + b"\n"
        elif "--property=DropInPaths" in arguments:
            values = [
                str(path)
                for path, _content in operator._expected_unit_dropins(  # noqa: SLF001
                    port.config,
                    arguments[1],
                )
            ]
            if extra_manager_dropin:
                values.append(extra_manager_dropin)
            stdout = " ".join(values).encode()
        elif "--property=Environment" in arguments:
            stdout = environment[arguments[1]]
        elif "--property=KillMode" in arguments:
            stdout = b"control-group\n"
        elif "--property=UMask" in arguments:
            stdout = b"0077\n"
        elif "--property=UnitFileState" in arguments:
            stdout = b"enabled\n"
        elif "--property=RuntimeDirectory" in arguments:
            stdout = runtime_directories[arguments[1]].encode() + b"\n"
        elif any(f"--property={name}" in arguments for name in security_properties):
            property_name = next(name for name in security_properties if f"--property={name}" in arguments)
            stdout = security_properties[property_name]
        elif "--property=UnsetEnvironment" in arguments:
            stdout = b"PYTHONPATH\n"
        elif "--property=WorkingDirectory" in arguments:
            stdout = str(port.config.friday_home).encode() + b"\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(port, "_systemctl", systemctl)
    port.verify_units(candidate)
    backend = port.config.backend_unit
    environment[backend] = environment[backend].replace(
        b"FRIDAY_DATABASE_MUST_EXIST=1",
        b"FRIDAY_DATABASE_MUST_EXIST=0",
    )
    with pytest.raises(operator.ReleaseFailure, match="manager_environment"):
        port.verify_units(candidate)
    environment[backend] = environment[backend].replace(
        b"FRIDAY_DATABASE_MUST_EXIST=0",
        b"FRIDAY_DATABASE_MUST_EXIST=1",
    )
    for property_name, invalid in (
        ("MemorySwapMax", b"infinity\n"),
        ("PrivateTmp", b"yes\n"),
        ("PrivateUsers", b"yes\n"),
        ("RuntimeDirectoryMode", b"0755\n"),
        ("RuntimeDirectoryPreserve", b"yes\n"),
    ):
        valid = security_properties[property_name]
        security_properties[property_name] = invalid
        with pytest.raises(operator.ReleaseFailure, match="manager_property"):
            port.verify_units(candidate)
        security_properties[property_name] = valid
    expected_runtime = runtime_directories[backend]
    runtime_directories[backend] = "shared-tmp"
    with pytest.raises(operator.ReleaseFailure, match="manager_property"):
        port.verify_units(candidate)
    runtime_directories[backend] = expected_runtime
    expected_environment = environment[backend]
    environment[backend] = expected_environment.replace(
        str(operator._unit_runtime_tmp_directory(backend)).encode(),  # noqa: SLF001
        b"/tmp",
    )
    with pytest.raises(operator.ReleaseFailure, match="manager_environment"):
        port.verify_units(candidate)
    environment[backend] = expected_environment
    extra_exec = exec_record(["/bin/sh", "-c", "false"])
    with pytest.raises(operator.ReleaseFailure, match="manager_extra_exec"):
        port.verify_units(candidate)
    extra_exec = b""
    unexpected = port.config.unit_dir / f"{port.config.backend_unit}.d/99-unexpected.conf"
    unexpected.write_text("[Service]\nExecStartPre=/bin/false\n", encoding="ascii")
    with pytest.raises(operator.ReleaseFailure, match="dropin_set"):
        port.verify_units(candidate)
    unexpected.unlink()
    extra_manager_dropin = str(tmp_path / "runtime-injected.conf")
    with pytest.raises(operator.ReleaseFailure, match="manager_dropins"):
        port.verify_units(candidate)


def test_runtime_queue_path_is_structurally_and_candidate_settings_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _systemd_test_port(tmp_path)
    unrelated = port.config.state_dir / "unrelated-private.sqlite3"
    connection = sqlite3.connect(unrelated)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.commit()
    connection.close()
    unrelated.chmod(0o600)
    with pytest.raises(operator.ReleaseFailure, match="inbox_database_not_runtime_queue"):
        operator.SystemdActivationPort(replace(port.config, inbox_database=unrelated))

    release = operator.ReleaseIdentity(
        tmp_path / "candidate",
        "c" * 40,
        "0.206.0",
        "d" * 64,
        34,
        operator.MEMORY_VAULT_MODE_CONTRACT,
    )
    observed: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(operator, "verify_release_tree", lambda _release: None)
    monkeypatch.setattr(operator, "installed_surface_smoke", lambda _release: "e" * 64)

    def run(command, **kwargs):
        observed.append((list(command), dict(kwargs["env"])))
        root_sha256 = hashlib.sha256(str(operator._obsidian_root(port.config)).encode()).hexdigest()  # noqa: SLF001
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                json.dumps(
                    {
                        "memory_vault_mode": "disabled",
                        "obsidian_mode": "disabled",
                        "obsidian_root_sha256": root_sha256,
                        "status": "clear",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            ),
            stderr=b"",
        )

    monkeypatch.setattr(operator.subprocess, "run", run)
    port.verify_release(release)
    assert observed[0][0][-7:] == [
        str(port.config.state_dir),
        str(port.config.database),
        str(port.config.inbox_database),
        "disabled",
        "disabled",
        str(operator._obsidian_root(port.config)),  # noqa: SLF001
        "legacy",
    ]
    assert observed[0][1]["FRIDAY_DATABASE_PATH"] == str(port.config.database)

    legacy_settings = SimpleNamespace(
        home=port.config.friday_home,
        state_dir=port.config.state_dir,
        database_path=port.config.database,
        database_must_exist=True,
        memory_vault_mode="disabled",
    )
    validation_modes: list[bool] = []
    legacy_config = ModuleType("friday.config")
    legacy_config.load_local_env_file = lambda: None  # type: ignore[attr-defined]
    legacy_config.load_settings = lambda: legacy_settings  # type: ignore[attr-defined]
    legacy_config.validate_settings = (  # type: ignore[attr-defined]
        lambda _settings, *, production: validation_modes.append(production) or []
    )
    monkeypatch.setitem(sys.modules, "friday.config", legacy_config)
    probe_script = observed[0][0][4]
    legacy_argv = ["-c", *observed[0][0][5:]]
    monkeypatch.setattr(sys, "argv", legacy_argv)
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(port.config.env_file))
    exec(compile(probe_script, "<legacy-runtime-config-probe>", "exec"), {})  # noqa: S102
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["obsidian_mode"] == "disabled"
    assert validation_modes == [True]

    enabled_legacy_argv = list(legacy_argv)
    enabled_legacy_argv[7] = "enabled"
    monkeypatch.setattr(sys, "argv", enabled_legacy_argv)
    with pytest.raises(AssertionError):
        exec(compile(probe_script, "<legacy-runtime-config-probe>", "exec"), {})  # noqa: S102


def test_memory_vault_mode_is_bound_to_candidate_config_and_health(
    tmp_path: Path,
) -> None:
    port = _systemd_test_port(tmp_path)
    disabled_identity = operator._systemd_config_identity(port.config)  # noqa: SLF001
    full_owner = replace(port.config, memory_vault_mode="full_owner")
    assert operator._systemd_config_identity(full_owner) != disabled_identity  # noqa: SLF001
    operator.SystemdActivationPort(full_owner)
    with pytest.raises(operator.ReleaseFailure, match="memory_vault_mode_invalid"):
        operator.SystemdActivationPort(replace(port.config, memory_vault_mode="typo"))

    candidate = operator.ReleaseIdentity(
        tmp_path / "candidate-vault-mode",
        "c" * 40,
        "0.206.0",
        "d" * 64,
        34,
        operator.MEMORY_VAULT_MODE_CONTRACT,
    )
    previous = replace(candidate, version="0.205.0", memory_vault_mode_contract="")
    stale_same_version = replace(candidate, memory_vault_mode_contract="")
    expected = {
        "memory_vault": {
            "mode": "disabled",
            "body_free_mode": True,
            "body_projection_enabled": False,
        }
    }
    assert operator._memory_vault_health_identity_matches(expected, candidate, "disabled")  # noqa: SLF001
    assert not operator._memory_vault_health_identity_matches({}, candidate, "disabled")  # noqa: SLF001
    assert not operator._memory_vault_health_identity_matches(expected, candidate, "full_owner")  # noqa: SLF001
    assert not operator._memory_vault_health_identity_matches({}, previous, "disabled")  # noqa: SLF001
    assert operator._memory_vault_health_identity_matches({}, previous, "full_owner")  # noqa: SLF001
    assert not operator._memory_vault_health_identity_matches({}, stale_same_version, "disabled")  # noqa: SLF001
    assert operator._release_binds_memory_vault_mode(candidate)  # noqa: SLF001
    assert not operator._release_binds_memory_vault_mode(stale_same_version)  # noqa: SLF001


def test_obsidian_health_identity_is_exact_with_only_a_legacy_disabled_omission(
    tmp_path: Path,
) -> None:
    root_sha256 = "a" * 64
    schema35 = operator.ReleaseIdentity(
        tmp_path / "schema35",
        "b" * 40,
        "0.207.0",
        "c" * 64,
        35,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    legacy_previous = operator.ReleaseIdentity(
        tmp_path / "legacy-previous",
        "d" * 40,
        "0.206.0",
        "e" * 64,
        34,
    )
    capable_schema34 = replace(
        legacy_previous,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    exact = {"obsidian": {"mode": "enabled", "root_sha256": root_sha256}}

    assert operator._obsidian_health_identity_matches(  # noqa: SLF001
        exact,
        schema35,
        "enabled",
        root_sha256,
    )
    assert not operator._obsidian_health_identity_matches(  # noqa: SLF001
        {"obsidian": {**exact["obsidian"], "root": "/private/path"}},
        schema35,
        "enabled",
        root_sha256,
    )
    assert not operator._obsidian_health_identity_matches(  # noqa: SLF001
        {"obsidian": {"mode": "disabled", "root_sha256": root_sha256}},
        schema35,
        "enabled",
        root_sha256,
    )
    assert not operator._obsidian_health_identity_matches(  # noqa: SLF001
        {"obsidian": {"mode": "enabled", "root_sha256": "f" * 64}},
        schema35,
        "enabled",
        root_sha256,
    )
    assert not operator._obsidian_health_identity_matches(  # noqa: SLF001
        {},
        schema35,
        "disabled",
        root_sha256,
    )
    assert operator._obsidian_health_identity_matches(  # noqa: SLF001
        {},
        legacy_previous,
        "disabled",
        root_sha256,
    )
    assert not operator._obsidian_health_identity_matches(  # noqa: SLF001
        {},
        legacy_previous,
        "enabled",
        root_sha256,
    )
    assert not operator._obsidian_health_identity_matches(  # noqa: SLF001
        {},
        capable_schema34,
        "disabled",
        root_sha256,
    )


def test_backend_acceptance_requires_the_exact_obsidian_health_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _systemd_test_port(tmp_path)
    schema35 = operator.ReleaseIdentity(
        tmp_path / "schema35",
        "a" * 40,
        "0.207.0",
        "b" * 64,
        35,
        memory_vault_mode_contract=operator.MEMORY_VAULT_MODE_CONTRACT,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    legacy_previous = replace(
        schema35,
        root=tmp_path / "legacy-previous",
        commit="c" * 40,
        version="0.206.0",
        tree_manifest_sha256="d" * 64,
        max_schema=34,
        obsidian_cutover_contract="",
    )
    current_payload: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return port.config.health_url

        def read(self, _limit: int) -> bytes:
            return json.dumps(current_payload, separators=(",", ":")).encode("ascii")

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    tls_context_inputs: list[dict[str, object]] = []

    def create_default_context(**kwargs: object) -> SimpleNamespace:
        tls_context_inputs.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(operator.ssl, "create_default_context", create_default_context)
    monkeypatch.setattr(operator.urllib.request, "build_opener", lambda *_handlers: Opener())
    monkeypatch.setattr(operator.time, "sleep", lambda _seconds: None)
    accepted: list[operator.ReleaseIdentity] = []
    monkeypatch.setattr(port, "_wait_process", lambda _unit, release, _role: accepted.append(release))

    def health_payload(release: operator.ReleaseIdentity, *, include_obsidian: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "ok",
            "version": release.version,
            "memory_vault": {
                "mode": "disabled",
                "body_free_mode": True,
                "body_projection_enabled": False,
            },
        }
        if include_obsidian:
            payload["obsidian"] = {
                "mode": "disabled",
                "root_sha256": operator._obsidian_root_sha256(port.config),  # noqa: SLF001
            }
        return payload

    current_payload.update(health_payload(schema35, include_obsidian=True))
    monkeypatch.setattr(operator.time, "monotonic", lambda: 0.0)
    port.accept_backend(schema35)

    current_payload.clear()
    current_payload.update(health_payload(schema35, include_obsidian=False))
    ticks = iter((0.0, 0.0, 421.0))
    monkeypatch.setattr(operator.time, "monotonic", lambda: next(ticks))
    with pytest.raises(operator.ReleaseFailure, match="backend_health_identity_timeout"):
        port.accept_backend(schema35)

    current_payload.clear()
    current_payload.update(health_payload(legacy_previous, include_obsidian=False))
    monkeypatch.setattr(operator.time, "monotonic", lambda: 0.0)
    port.accept_backend(legacy_previous)
    assert accepted == [schema35, legacy_previous]
    assert tls_context_inputs
    assert all(
        value == {"cadata": port.config.health_ca.read_text(encoding="ascii")} for value in tls_context_inputs
    )


def test_release_mode_capability_not_semver_controls_legacy_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_port = _systemd_test_port(tmp_path)
    full_owner_port = operator.SystemdActivationPort(
        replace(disabled_port.config, memory_vault_mode="full_owner")
    )
    legacy_0205 = operator.ReleaseIdentity(
        tmp_path / "legacy-0205",
        "a" * 40,
        "0.205.0",
        "1" * 64,
        33,
    )
    stale_0206 = operator.ReleaseIdentity(
        tmp_path / "stale-0206",
        "b" * 40,
        "0.206.0rc1",
        "2" * 64,
        34,
    )
    capable_0206 = replace(
        stale_0206,
        root=tmp_path / "capable-0206",
        commit="c" * 40,
        tree_manifest_sha256="3" * 64,
        memory_vault_mode_contract=operator.MEMORY_VAULT_MODE_CONTRACT,
    )
    monkeypatch.setattr(operator, "verify_release_tree", lambda _release: None)
    monkeypatch.setattr(operator, "installed_surface_smoke", lambda _release: "4" * 64)

    def run(command, **_kwargs):
        mode = "disabled" if "capable-0206" in str(command[0]) else "full_owner"
        root_sha256 = hashlib.sha256(
            str(operator._obsidian_root(disabled_port.config)).encode()  # noqa: SLF001
        ).hexdigest()
        payload = (
            json.dumps(
                {
                    "memory_vault_mode": mode,
                    "obsidian_mode": "disabled",
                    "obsidian_root_sha256": root_sha256,
                    "status": "clear",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    for legacy in (legacy_0205, stale_0206):
        with pytest.raises(operator.ReleaseFailure, match="mode_contract_missing"):
            disabled_port.verify_release(legacy)
        full_owner_port.verify_release(legacy)
    disabled_port.verify_release(capable_0206)


def test_two_cutover_mode_matrix_and_mode_aware_previous_equals_fallback(
    tmp_path: Path,
) -> None:
    legacy = operator.ReleaseIdentity(tmp_path / "legacy", "a" * 40, "0.205.0", "1" * 64, 33)
    rc0 = operator.ReleaseIdentity(
        tmp_path / "rc0",
        "b" * 40,
        "0.206.0rc0",
        "2" * 64,
        34,
        operator.MEMORY_VAULT_MODE_CONTRACT,
        operator.VENV_RELOCATION_CONTRACT,
    )
    rc1 = replace(
        rc0,
        root=tmp_path / "rc1",
        commit="c" * 40,
        version="0.206.0rc1",
        tree_manifest_sha256="3" * 64,
    )
    final = replace(
        rc1,
        root=tmp_path / "final",
        commit="d" * 40,
        version="0.206.0",
        tree_manifest_sha256="4" * 64,
    )

    phase_a = FakePort(backup_schema=33, memory_vault_mode="full_owner")
    receipt_a = operator.activate_release(
        phase_a,
        MemoryJournal(),
        candidate=rc1,
        previous=legacy,
        schema_capable_fallback=rc0,
    )
    assert receipt_a["runtime_policy"] == {
        "memory_vault_cutover_phase": "phase_a_full_owner_bridge",
        "memory_vault_mode": "full_owner",
    }

    # If Phase A rolled back to the mode-aware rc0 bridge, retrying rc1 keeps
    # previous=fallback at rc0.  This is the only honest schema-34 recovery leg.
    phase_a_retry = FakePort(backup_schema=34, memory_vault_mode="full_owner")
    receipt_a_retry = operator.activate_release(
        phase_a_retry,
        MemoryJournal(),
        candidate=rc1,
        previous=rc0,
        schema_capable_fallback=rc0,
    )
    assert receipt_a_retry["runtime_policy"] == receipt_a["runtime_policy"]

    phase_b = FakePort(backup_schema=34, memory_vault_mode="disabled")
    receipt_b = operator.activate_release(
        phase_b,
        MemoryJournal(),
        candidate=final,
        previous=rc1,
        schema_capable_fallback=rc1,
    )
    assert receipt_b["runtime_policy"] == {
        "memory_vault_cutover_phase": "phase_b_body_free",
        "memory_vault_mode": "disabled",
    }


def test_album_backup_journal_resume_accepts_an_exact_empty_wal(
    tmp_path: Path,
) -> None:
    port = _systemd_test_port(tmp_path)
    wal = Path(f"{port.config.inbox_database}-wal")
    wal.write_bytes(b"")
    wal.chmod(0o600)
    backup = operator._exact_inbox_backup(port.config)  # noqa: SLF001
    release_root = tmp_path / "candidate"
    release_root.mkdir(mode=0o700)
    release = operator.ReleaseIdentity(release_root, "c" * 40, "0.206.0", "d" * 64, 34)
    identity = operator._systemd_config_identity(port.config)  # noqa: SLF001
    journal = operator.DurableAlbumRecoveryJournal(
        port.config.state_dir / "historical-album-recovery.v1.json",
        backup_root=port.config.backup_dir,
        config_identity_sha256=identity,
    )
    journal.begin_or_resume(release)
    journal.record("bridge_stop_attempted")
    journal.record("bridge_quiesced")
    journal.record("backup_complete", backup=backup)
    recovered = operator.DurableAlbumRecoveryJournal(
        journal.path,
        backup_root=port.config.backup_dir,
        config_identity_sha256=identity,
    ).backup(port.config)
    assert recovered == backup
    assert (backup.directory / "inbox.sqlite3-wal").stat().st_size == 0


def test_process_identity_requires_anchor_argv_and_exact_release_executable(tmp_path: Path) -> None:
    port = _systemd_test_port(tmp_path)
    release_root = tmp_path / "candidate"
    python = release_root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic executable")
    python.chmod(0o500)
    port.config.anchor.symlink_to(release_root, target_is_directory=True)
    release = operator.ReleaseIdentity(release_root, "c" * 40, "0.206.0", "d" * 64, 34)
    proc_root = tmp_path / "proc"
    process = proc_root / "1234"
    process.mkdir(parents=True)
    command = [
        str(port.config.anchor / "venv/bin/python"),
        "-I",
        "-B",
        "-m",
        "friday.cli",
        "--env-file",
        str(port.config.env_file),
        "server",
    ]
    (process / "cmdline").write_bytes(b"\0".join(item.encode() for item in command) + b"\0")
    (process / "exe").symlink_to(python)
    assert port._process_matches(1234, release, "backend", proc_root=proc_root)  # noqa: SLF001
    command[0] = str(release_root / "venv/bin/python")
    (process / "cmdline").write_bytes(b"\0".join(item.encode() for item in command) + b"\0")
    assert not port._process_matches(1234, release, "backend", proc_root=proc_root)  # noqa: SLF001


def test_completed_legacy_unit_journal_starts_a_full_surface_identity(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    candidate = operator.ReleaseIdentity(
        tmp_path / "candidate",
        "c" * 40,
        "0.207.36",
        "d" * 64,
        43,
    )
    previous = operator.ReleaseIdentity(
        tmp_path / "previous",
        "a" * 40,
        "0.207.35",
        "e" * 64,
        43,
    )
    legacy_hashes = {unit: "1" * 64 for unit in operator._RUNTIME_UNIT_NAMES}  # noqa: SLF001
    transition_hashes = {unit: "2" * 64 for unit in operator._RUNTIME_UNIT_NAMES}  # noqa: SLF001
    journal = operator.DurableUnitInstallJournal(state_dir / "immutable-release-unit-install.v1.json")
    initial = journal.begin_or_resume(
        candidate=candidate,
        previous=previous,
        transition_root=tmp_path / "transition",
        candidate_unit_hashes=legacy_hashes,
        transition_unit_hashes=transition_hashes,
    )
    for phase in operator._UNIT_INSTALL_PHASES[1:-1]:  # noqa: SLF001
        journal.record(phase)
    receipt = hashlib.sha256(
        operator._canonical_json(  # noqa: SLF001
            {
                "candidate_tree_sha256": candidate.tree_manifest_sha256,
                "previous_tree_sha256": previous.tree_manifest_sha256,
                "unit_hashes": legacy_hashes,
            }
        )
    ).hexdigest()
    journal.record("complete", receipt_sha256=receipt)

    legacy_terminal = operator.DurableUnitInstallJournal(journal.path).load()
    assert legacy_terminal["phase"] == "complete"
    assert legacy_terminal["candidate_unit_hashes"] == legacy_hashes
    full_hashes = {key: "3" * 64 for key in operator._UNIT_SURFACE_KEYS}  # noqa: SLF001
    migrated = operator.DurableUnitInstallJournal(journal.path).begin_or_resume(
        candidate=candidate,
        previous=previous,
        transition_root=tmp_path / "transition",
        candidate_unit_hashes=full_hashes,
        transition_unit_hashes=transition_hashes,
    )

    assert migrated["phase"] == "prepared"
    assert migrated["candidate_unit_hashes"] == full_hashes
    assert migrated["transaction_id"] != initial["transaction_id"]

    terminal = operator.DurableUnitInstallJournal(journal.path)
    for phase in operator._UNIT_INSTALL_PHASES[1:-1]:  # noqa: SLF001
        terminal.record(phase)
    full_receipt = hashlib.sha256(
        operator._canonical_json(  # noqa: SLF001
            {
                "candidate_tree_sha256": candidate.tree_manifest_sha256,
                "previous_tree_sha256": previous.tree_manifest_sha256,
                "unit_hashes": full_hashes,
            }
        )
    ).hexdigest()
    terminal.record("complete", receipt_sha256=full_receipt)
    drifted = dict(full_hashes)
    drifted["friday-backend.service.d/security.conf"] = "4" * 64
    with pytest.raises(
        operator.ReleaseFailure,
        match="^completed_unit_install_identity_changed$",
    ):
        operator.DurableUnitInstallJournal(journal.path).begin_or_resume(
            candidate=candidate,
            previous=previous,
            transition_root=tmp_path / "transition",
            candidate_unit_hashes=drifted,
            transition_unit_hashes=transition_hashes,
        )


@pytest.mark.parametrize("crash_after", [1, 3, 7, "manager-reload"])
def test_unit_pair_crash_converges_without_exposing_mixed_runtime_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after: int | str,
) -> None:
    tmp_path.chmod(0o700)
    unit_dir = tmp_path / "units"
    state_dir = tmp_path / "state"
    transition_root = tmp_path / "live-transition"
    candidate_root = tmp_path / "candidate"
    previous_root = tmp_path / "previous"
    for directory in (unit_dir, state_dir, transition_root, candidate_root, previous_root):
        directory.mkdir(mode=0o700)
    artifacts = candidate_root / "artifacts"
    artifacts.mkdir(mode=0o700)
    anchor = tmp_path / "current-release"
    env_file = tmp_path / ".env.local"
    env_file.write_text("FRIDAY_PROFILE=production\n", encoding="ascii")
    units = operator.render_units(anchor=anchor, env_file=env_file, friday_home=tmp_path)
    transition_hashes: dict[str, str] = {}
    manager_argv: dict[str, tuple[str, ...]] = {}
    for name, content in units.items():
        (artifacts / name).write_text(content, encoding="utf-8")
        role = "server" if name == "friday-backend.service" else "telegram-bridge"
        direct = content.replace(
            f"ExecStart={anchor}/venv/bin/python",
            f"ExecStart={transition_root}/venv/bin/python",
        )
        installed = unit_dir / name
        installed.write_text(direct, encoding="utf-8")
        transition_hashes[name] = hashlib.sha256(installed.read_bytes()).hexdigest()
        manager_argv[name] = (
            str(transition_root / "venv/bin/python"),
            "-I",
            "-B",
            "-m",
            "friday.cli",
            "--env-file",
            str(env_file),
            role,
        )
        dropin_directory = unit_dir / f"{name}.d"
        dropin_directory.mkdir(mode=0o700)
        database = (
            "[Service]\n"
            f"Environment=FRIDAY_DATABASE_PATH={state_dir / 'jericho.sqlite3'}\n"
            "Environment=FRIDAY_DATABASE_MUST_EXIST=1\n"
            f"ExecStartPre=/usr/bin/test -s {state_dir / 'jericho.sqlite3'}\n"
        )
        (dropin_directory / "database.conf").write_text(database, encoding="utf-8")
        if name == "friday-bridge.service":
            (dropin_directory / "dependency.conf").write_text(
                "[Unit]\nWants=friday-backend.service\nAfter=friday-backend.service\n",
                encoding="utf-8",
            )
        (dropin_directory / "security.conf").write_bytes(
            operator._pre_aggregate_unit_security_dropin(name)  # noqa: SLF001
        )
        for dropin in dropin_directory.iterdir():
            dropin.chmod(0o644)
    candidate = operator.ReleaseIdentity(
        candidate_root,
        "c" * 40,
        "0.206.0",
        "d" * 64,
        34,
        venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
    )
    previous = operator.ReleaseIdentity(previous_root, "a" * 40, "0.206.0rc1", "e" * 64, 34)

    def record(argv: tuple[str, ...]) -> bytes:
        return (
            f"{{ path={argv[0]} ; argv[]={' '.join(argv)} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
        ).encode()

    replacements = 0
    reload_crashed = False

    def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        nonlocal reload_crashed
        del check
        if arguments[0] == "is-enabled":
            return subprocess.CompletedProcess(arguments, 0, stdout=b"enabled\n", stderr=b"")
        if arguments[0] == "daemon-reload":
            if crash_after == "manager-reload" and not reload_crashed:
                reload_crashed = True
                raise RuntimeError("synthetic power loss during manager reload")
            for name in units:
                manager_argv[name] = operator._unit_exec_argv(  # noqa: SLF001
                    (unit_dir / name).read_bytes(),
                    code="test",
                )
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        name = arguments[1]
        stdout = b""
        if "--property=ExecStart" in arguments:
            stdout = record(manager_argv[name])
        elif "--property=FragmentPath" in arguments:
            stdout = str(unit_dir / name).encode() + b"\n"
        elif "--property=DropInPaths" in arguments:
            stdout = " ".join(
                str(unit_dir / f"{name}.d" / dropin)
                for dropin in operator._UNIT_DROPIN_NAMES[name]  # noqa: SLF001
            ).encode()
        elif "--property=Environment" in arguments:
            stdout = (
                f"FRIDAY_HOME={tmp_path} "
                f"FRIDAY_DATABASE_PATH={state_dir / 'jericho.sqlite3'} "
                "FRIDAY_DATABASE_MUST_EXIST=1 "
                f"TMPDIR={operator._unit_runtime_tmp_directory(name)}\n"  # noqa: SLF001
            ).encode()
        elif "--property=LimitCORE" in arguments or "--property=MemorySwapMax" in arguments:
            stdout = b"0\n"
        elif "--property=PrivateTmp" in arguments or "--property=PrivateUsers" in arguments:
            stdout = b"no\n"
        elif "--property=RuntimeDirectory" in arguments:
            stdout = operator._unit_runtime_directory_name(name).encode() + b"\n"  # noqa: SLF001
        elif "--property=RuntimeDirectoryMode" in arguments:
            stdout = b"0700\n"
        elif "--property=RuntimeDirectoryPreserve" in arguments:
            stdout = b"no\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(operator, "_run_systemctl", systemctl)
    journal = operator.DurableUnitInstallJournal(state_dir / "immutable-release-unit-install.v1.json")
    original_replace = operator._replace_unit_file  # noqa: SLF001

    def crash_after_first(destination: Path, content: bytes) -> None:
        nonlocal replacements
        original_replace(destination, content)
        replacements += 1
        if isinstance(crash_after, int) and replacements == crash_after:
            raise RuntimeError("synthetic power loss")

    monkeypatch.setattr(operator, "_replace_unit_file", crash_after_first)
    with pytest.raises(RuntimeError, match="power loss"):
        operator.install_units(
            candidate,
            previous,
            unit_dir=unit_dir,
            anchor=anchor,
            transition_runtime_root=transition_root,
            transition_unit_hashes=transition_hashes,
            journal=journal,
        )
    expected_phase = "units_converged" if crash_after == "manager-reload" else "transition_anchor_active"
    assert journal.load()["phase"] == expected_phase
    assert anchor.resolve(strict=True) == transition_root
    for name in units:
        argv = operator._unit_exec_argv((unit_dir / name).read_bytes(), code="test")  # noqa: SLF001
        assert operator._unit_effective_root_is(  # noqa: SLF001
            argv,
            expected=operator._unit_exec_argv(  # noqa: SLF001
                (artifacts / name).read_bytes(),
                code="test",
            ),
            anchor=anchor,
            transition_root=transition_root,
        )

    monkeypatch.setattr(operator, "_replace_unit_file", original_replace)
    hashes = operator.install_units(
        candidate,
        previous,
        unit_dir=unit_dir,
        anchor=anchor,
        transition_runtime_root=transition_root,
        transition_unit_hashes=transition_hashes,
        journal=operator.DurableUnitInstallJournal(journal.path),
    )
    assert journal.load()["phase"] == "complete"
    assert anchor.resolve(strict=True) == previous_root
    assert set(hashes) == set(operator._UNIT_SURFACE_KEYS)  # noqa: SLF001
    assert hashes == {
        key: hashlib.sha256(operator._unit_surface_path(unit_dir, key).read_bytes()).hexdigest()  # noqa: SLF001
        for key in operator._UNIT_SURFACE_KEYS  # noqa: SLF001
    }


def test_unit_install_rejects_a_legacy_candidate_before_systemd_or_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = operator.ReleaseIdentity(
        tmp_path / "legacy-candidate",
        "c" * 40,
        "0.206.0rc1",
        "d" * 64,
        34,
    )
    previous = replace(candidate, root=tmp_path / "previous", commit="a" * 40)
    monkeypatch.setattr(
        operator,
        "_run_systemctl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("systemd side effect")),
    )
    with pytest.raises(
        operator.ReleaseFailure,
        match="^unit_candidate_venv_relocation_contract_missing$",
    ):
        operator.install_units(
            candidate,
            previous,
            unit_dir=tmp_path / "units",
            anchor=tmp_path / "anchor",
            transition_runtime_root=tmp_path / "transition",
            transition_unit_hashes={},
            journal=object(),  # type: ignore[arg-type]
        )


def test_candidate_bound_operator_rejects_a_legacy_executor_before_file_checks(
    tmp_path: Path,
) -> None:
    legacy = operator.ReleaseIdentity(
        tmp_path / "legacy-executor",
        "c" * 40,
        "0.206.0rc1",
        "d" * 64,
        34,
    )
    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_release_venv_relocation_contract_missing$",
    ):
        operator._require_candidate_bound_operator(legacy)  # noqa: SLF001


def test_alias_receipt_cannot_smuggle_private_fields_and_rolls_back_before_network(
    releases: Releases,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakePort()
    original = port.repair_file_aliases

    def invalid(release, backup):
        return {**original(release, backup), "private_identity": "must-not-escape"}

    monkeypatch.setattr(port, "repair_file_aliases", invalid)
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            port,
            MemoryJournal(),
            candidate=releases.candidate,
            previous=releases.previous,
            schema_capable_fallback=releases.fallback,
        )
    assert "restore_exact_db_wal_inbox" in port.events
    assert "start_backend:candidate" not in port.events
    assert port.active is releases.previous


def test_cli_help_is_owned_by_argparse_not_wrapped_as_internal_failure() -> None:
    with pytest.raises(SystemExit) as raised:
        operator.main(["--help"])
    assert raised.value.code == 0


def test_health_redirect_handler_never_follows_location() -> None:
    handler = operator._NoRedirect()  # noqa: SLF001
    assert handler.redirect_request(None, None, 302, "found", None, "https://example.invalid") is None


@pytest.mark.parametrize(
    "build_receipt_profile",
    [
        operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER,
        operator.BUILD_RECEIPT_PROFILE_P0H_RETENTION,
    ],
)
def test_build_parser_binds_all_manifest_digests_into_build_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_receipt_profile: str,
) -> None:
    captured: list[operator.BuildSpec] = []
    product_runner = (
        Path(__file__).parents[1] / "deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py"
    )

    def build(spec: operator.BuildSpec) -> operator.ReleaseIdentity:
        captured.append(spec)
        return operator.ReleaseIdentity(tmp_path / "release", "a" * 40, "0.206.0", "b" * 64, 34)

    monkeypatch.setattr(operator, "build_release", build)
    arguments = operator.build_parser().parse_args(
        [
            "build",
            "--commit",
            "a" * 40,
            "--version",
            "0.206.0",
            "--wheel",
            str(tmp_path / "friday.whl"),
            "--wheel-sha256",
            "1" * 64,
            "--runtime-lock",
            str(tmp_path / "runtime.lock"),
            "--runtime-lock-sha256",
            "2" * 64,
            "--wheelhouse",
            str(tmp_path / "wheelhouse"),
            "--wheelhouse-manifest",
            str(tmp_path / "wheelhouse.sha256"),
            "--wheelhouse-manifest-sha256",
            "3" * 64,
            "--releases-root",
            str(tmp_path / "releases"),
            "--anchor",
            str(tmp_path / "current"),
            "--env-file",
            str(tmp_path / ".env.local"),
            "--friday-home",
            str(tmp_path / "home"),
            "--state-dir",
            str(tmp_path / "state"),
            "--base-python",
            str(tmp_path / "python"),
            "--base-python-sha256",
            "4" * 64,
            "--alias-tool",
            str(tmp_path / "backfill.py"),
            "--alias-tool-sha256",
            "5" * 64,
            "--alias-dependency",
            str(tmp_path / "dependency.py"),
            "--alias-dependency-sha256",
            "6" * 64,
            "--secondary-product-runner",
            str(product_runner),
            "--secondary-product-runner-sha256",
            hashlib.sha256(product_runner.read_bytes()).hexdigest(),
            "--release-retention-toolchain-manifest-sha256",
            "7" * 64,
            "--build-receipt-profile",
            build_receipt_profile,
            "--max-schema",
            "34",
        ]
    )
    receipt = operator._run_cli(arguments)  # noqa: SLF001
    assert receipt["status"] == "clear"
    assert len(captured) == 1
    assert captured[0].runtime_lock_sha256 == "2" * 64
    assert captured[0].wheelhouse_manifest_sha256 == "3" * 64
    assert captured[0].state_dir == tmp_path / "state"
    assert captured[0].secondary_product_runner == product_runner
    assert (
        captured[0].secondary_product_runner_sha256 == hashlib.sha256(product_runner.read_bytes()).hexdigest()
    )
    assert captured[0].release_retention_toolchain_manifest_sha256 == "7" * 64
    assert captured[0].build_receipt_profile == build_receipt_profile


def test_build_release_blocks_on_unfinished_retention_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    friday_home = tmp_path / "friday-home"
    state_dir = friday_home / "data/state"
    releases_root = friday_home / "wheel-only-releases"
    for path in (friday_home, friday_home / "data", state_dir, releases_root):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setattr(
        retention_apply,
        "_load_journal",
        lambda path: {"phase": "prepared"} if path == state_dir / retention_apply.APPLY_JOURNAL_NAME else None,
    )
    monkeypatch.setattr(
        operator,
        "_build_release_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("build must not start")),
    )
    spec = operator.BuildSpec(
        commit="a" * 40,
        version="0.207.85",
        wheel=tmp_path / "candidate.whl",
        wheel_sha256="1" * 64,
        runtime_lock=tmp_path / "runtime.lock",
        runtime_lock_sha256="2" * 64,
        wheelhouse=tmp_path / "wheelhouse",
        wheelhouse_manifest=tmp_path / "wheelhouse.sha256",
        wheelhouse_manifest_sha256="3" * 64,
        releases_root=releases_root,
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        friday_home=friday_home,
        state_dir=state_dir,
        base_python=tmp_path / "python",
        base_python_sha256="4" * 64,
        alias_tool=tmp_path / "backfill.py",
        alias_tool_sha256="5" * 64,
        alias_dependency=tmp_path / "dependency.py",
        alias_dependency_sha256="6" * 64,
        secondary_product_runner=tmp_path / "live_failure_battery.py",
        secondary_product_runner_sha256="7" * 64,
        release_retention_toolchain_manifest_sha256="8" * 64,
        build_receipt_profile=operator.BUILD_RECEIPT_PROFILE_P0H_RETENTION,
        max_schema=50,
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^unfinished_retention_apply_requires_recovery$",
    ):
        operator.build_release(spec)


def test_activate_cli_blocks_on_unfinished_retention_apply_before_release_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    friday_home = tmp_path / "friday-home"
    state_dir = friday_home / "data/state"
    unit_dir = tmp_path / "units"
    for path in (friday_home, friday_home / "data", state_dir, unit_dir):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setattr(
        retention_apply,
        "_load_journal",
        lambda path: {"phase": "applying"} if path == state_dir / retention_apply.APPLY_JOURNAL_NAME else None,
    )
    monkeypatch.setattr(
        operator,
        "load_release_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("release loading must not start")),
    )
    arguments = operator.build_parser().parse_args(
        [
            "activate",
            "--candidate",
            str(tmp_path / "candidate"),
            "--candidate-tree-sha256",
            "3" * 64,
            "--previous",
            str(tmp_path / "previous"),
            "--previous-tree-sha256",
            "4" * 64,
            "--schema-capable-fallback",
            str(tmp_path / "fallback"),
            "--schema-capable-fallback-tree-sha256",
            "5" * 64,
            "--anchor",
            str(friday_home / "current-release"),
            "--env-file",
            str(friday_home / ".env.local"),
            "--env-file-sha256",
            "1" * 64,
            "--friday-home",
            str(friday_home),
            "--unit-dir",
            str(unit_dir),
            "--database",
            str(state_dir / "friday.sqlite3"),
            "--inbox-database",
            str(state_dir / "telegram-inbox.sqlite3"),
            "--backup-dir",
            str(friday_home / "backups"),
            "--state-dir",
            str(state_dir),
            "--health-ca",
            str(tmp_path / "health-ca.pem"),
            "--health-ca-sha256",
            "2" * 64,
        ]
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^unfinished_retention_apply_requires_recovery$",
    ):
        operator._run_cli(arguments)  # noqa: SLF001


def _write_synthetic_wheelhouse(tmp_path: Path) -> tuple[Path, Path]:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(mode=0o700)
    filenames = [
        "anyio-4.14.2-py3-none-any.whl",
        *(filename for _name, _version, filename in operator.BOOTSTRAP_WHEELS),
    ]
    for filename in filenames:
        (wheelhouse / filename).write_bytes(f"synthetic:{filename}".encode())
    manifest = tmp_path / "wheelhouse.sha256"
    _rewrite_synthetic_wheelhouse_manifest(wheelhouse, manifest)
    return wheelhouse, manifest


def _rewrite_synthetic_wheelhouse_manifest(wheelhouse: Path, manifest: Path) -> None:
    entries = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(wheelhouse.iterdir())
    ]
    manifest.write_text("\n".join(entries) + "\n", encoding="ascii")


def _write_relocation_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, tuple[Path, ...], Path, Path]:
    releases = tmp_path / "releases"
    releases.mkdir(mode=0o700)
    commit = "a" * 40
    staging = releases / f".{commit}.fixture"
    target = releases / commit
    venv.EnvBuilder(with_pip=False, symlinks=False).create(staging / "venv")
    python = staging / "venv/bin/python"
    entrypoints: list[Path] = []
    for name in ("friday", "jericho", "pip", "future-console"):
        path = staging / "venv/bin" / name
        if name == "pip":
            body = (
                "import sys\n"
                f"print('pip {operator.BOOTSTRAP_WHEELS[0][1]} from ' + sys.prefix + "
                "'/lib/python/site-packages/pip (python synthetic)')\n"
            )
        else:
            body = f"import sys\nprint({name!r} + ' ' + sys.executable)\n"
        path.write_bytes(b"#!" + os.fsencode(str(python)) + b"\n" + body.encode("ascii"))
        path.chmod(0o755)
        entrypoints.append(path)
    site = next((staging / "venv/lib").glob("python*/site-packages"))
    dist_info = site / "synthetic-1.0.dist-info"
    dist_info.mkdir()
    pycache = site / "synthetic/__pycache__"
    pycache.mkdir(parents=True)
    cached = pycache / "module.cpython-314.pyc"
    cached.write_bytes(b"generated cache")
    record = dist_info / "RECORD"
    rows = [
        [
            os.path.relpath(path, site).replace(os.sep, "/"),
            operator._record_digest(path),  # noqa: SLF001
            str(path.stat().st_size),
        ]
        for path in entrypoints
    ]
    rows.extend(
        [
            ["synthetic/__pycache__/module.cpython-314.pyc", "", ""],
            ["synthetic-1.0.dist-info/RECORD", "", ""],
        ]
    )
    operator._write_record_rows(record, rows)  # noqa: SLF001
    return staging, target, tuple(entrypoints), record, pycache


def _remove_fixture_pycache(pycache: Path) -> None:
    for child in pycache.iterdir():
        child.unlink()
    pycache.rmdir()


def _release_retention_toolchain_manifest_sha256() -> str:
    sources = operator._read_release_retention_toolchain_sources(  # noqa: SLF001
        Path(operator.__file__).resolve(strict=True)
    )
    manifest = operator._release_retention_toolchain_manifest_bytes(sources)  # noqa: SLF001
    return hashlib.sha256(manifest).hexdigest()


def test_release_retention_toolchain_receipt_pair_preserves_historical_v1() -> None:
    assert operator._release_retention_toolchain_receipt_identity({}) == ("", "")  # noqa: SLF001
    digest = "a" * 64
    assert operator._release_retention_toolchain_receipt_identity(  # noqa: SLF001
        {
            "release_retention_toolchain_contract": (operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT),
            "release_retention_toolchain_manifest_sha256": digest,
        }
    ) == (operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT, digest)


@pytest.mark.parametrize(
    "metadata",
    [
        {"release_retention_toolchain_contract": operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT},
        {"release_retention_toolchain_manifest_sha256": "a" * 64},
        {
            "release_retention_toolchain_contract": "unrecognized-retention-toolchain-v1",
            "release_retention_toolchain_manifest_sha256": "a" * 64,
        },
        {
            "release_retention_toolchain_contract": (operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT),
            "release_retention_toolchain_manifest_sha256": "not-a-digest",
        },
    ],
    ids=["contract-only", "digest-only", "unknown-contract", "invalid-digest"],
)
def test_release_retention_toolchain_receipt_pair_fails_closed(
    metadata: dict[str, str],
) -> None:
    with pytest.raises(operator.ReleaseFailure, match="^release_metadata_invalid$"):
        operator._release_retention_toolchain_receipt_identity(metadata)  # noqa: SLF001


def _synthetic_sealed_retention_toolchain(
    tmp_path: Path,
    *,
    supplied_sources: Mapping[str, bytes] | None = None,
) -> tuple[Path, str]:
    root = tmp_path / "release"
    artifacts = root / "artifacts"
    tools = root / operator._RELEASE_RETENTION_TOOLCHAIN_ROOT / "tools"  # noqa: SLF001
    tools.mkdir(parents=True)
    sources = dict(supplied_sources or {})
    if not sources:
        sources = {
            module: f"synthetic:{module}\n".encode("ascii")
            for module in operator._RELEASE_RETENTION_TOOLCHAIN_PACKAGE_FILES  # noqa: SLF001
        }
        sources["__init__.py"] = operator._RELEASE_RETENTION_TOOLCHAIN_PACKAGE_INIT  # noqa: SLF001
    for module, raw in sources.items():
        path = tools / module
        path.write_bytes(raw)
        path.chmod(0o400)
    operator_copy = artifacts / "immutable_release_operator.py"
    operator_copy.write_bytes(sources["immutable_release_operator.py"])
    operator_copy.chmod(0o400)
    manifest_raw = operator._release_retention_toolchain_manifest_bytes(sources)  # noqa: SLF001
    manifest = root / operator._RELEASE_RETENTION_TOOLCHAIN_MANIFEST  # noqa: SLF001
    manifest.write_bytes(manifest_raw)
    manifest.chmod(0o400)
    tools.chmod(0o500)
    tools.parent.chmod(0o500)
    return root, hashlib.sha256(manifest_raw).hexdigest()


def test_release_retention_toolchain_admission_revalidates_closed_bundle(tmp_path: Path) -> None:
    root, manifest_sha256 = _synthetic_sealed_retention_toolchain(tmp_path)
    release = operator.ReleaseIdentity(
        root=root,
        commit="a" * 40,
        version="0.207.85",
        tree_manifest_sha256="b" * 64,
        max_schema=50,
        release_retention_toolchain_contract=operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT,
        release_retention_toolchain_manifest_sha256=manifest_sha256,
        build_receipt_profile=operator.BUILD_RECEIPT_PROFILE_P0H_RETENTION,
        sealed_release_retention_toolchain_manifest_sha256=manifest_sha256,
    )
    operator._require_release_retention_toolchain(release)  # noqa: SLF001

    tools = root / operator._RELEASE_RETENTION_TOOLCHAIN_ROOT / "tools"  # noqa: SLF001
    tools.chmod(0o700)
    extra = tools / "unmanifested.py"
    extra.write_text("# must fail closed\n", encoding="ascii")
    extra.chmod(0o400)
    tools.chmod(0o500)
    with pytest.raises(
        operator.ReleaseFailure,
        match="^release_retention_toolchain_manifest_invalid$",
    ):
        operator._require_release_retention_toolchain(release)  # noqa: SLF001

    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_release_retention_toolchain_missing$",
    ):
        operator._require_release_retention_toolchain(  # noqa: SLF001
            replace(
                release,
                release_retention_toolchain_contract="",
                release_retention_toolchain_manifest_sha256="",
            )
        )


def test_reader_only_release_profile_revalidates_bundle_and_omits_receipt_pairs(
    tmp_path: Path,
) -> None:
    root, manifest_sha256 = _synthetic_sealed_retention_toolchain(tmp_path)
    release = operator.ReleaseIdentity(
        root=root,
        commit="a" * 40,
        version="0.207.85",
        tree_manifest_sha256="b" * 64,
        max_schema=50,
        build_receipt_profile=operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER,
        sealed_release_retention_toolchain_manifest_sha256=manifest_sha256,
    )

    operator._require_reader_only_release_profile(release)  # noqa: SLF001
    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_release_retention_toolchain_missing$",
    ):
        operator._require_release_retention_toolchain(release)  # noqa: SLF001
    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_reader_only_release_profile_missing$",
    ):
        operator._require_reader_only_release_profile(  # noqa: SLF001
            replace(
                release,
                operator_transaction_lock_scope_contract=(operator.OPERATOR_TRANSACTION_LOCK_SCOPE_CONTRACT),
                operator_transaction_lock_scope_sha256="c" * 64,
            )
        )


def test_release_retention_toolchain_absolute_entrypoints_import_sealed_closure(
    tmp_path: Path,
) -> None:
    sources = operator._read_release_retention_toolchain_sources(  # noqa: SLF001
        Path(operator.__file__).resolve(strict=True)
    )
    root, _manifest_sha256 = _synthetic_sealed_retention_toolchain(
        tmp_path,
        supplied_sources=sources,
    )
    tools = root / operator._RELEASE_RETENTION_TOOLCHAIN_ROOT / "tools"  # noqa: SLF001
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for module in (
        "release_dr_generation_lifecycle.py",
        "release_artifact_retention_operator.py",
    ):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-B", str(tools / module), "--help"],
            check=False,
            capture_output=True,
            cwd=tmp_path,
            env=environment,
            timeout=30,
        )
        assert result.returncode == 0, (module, result.stderr)
        assert result.stderr == b""


def _synthetic_build_spec(tmp_path: Path) -> operator.BuildSpec:
    friday_home = tmp_path / "friday-home"
    releases_root = friday_home / "wheel-only-releases"
    releases_root.mkdir(parents=True, mode=0o700)
    state_dir = friday_home / "data/state"
    state_dir.mkdir(parents=True, mode=0o700)
    wheelhouse, wheelhouse_manifest = _write_synthetic_wheelhouse(tmp_path)
    runtime_lock = tmp_path / "runtime.lock"
    runtime_lock.write_text("anyio==4.14.2\n", encoding="ascii")
    wheel = tmp_path / "friday-0.206.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic Friday wheel")
    base_python = tmp_path / "python3.14"
    base_python.write_bytes(b"synthetic Python")
    alias_tool = tmp_path / "backfill_file_alias_filenames.py"
    alias_tool.write_text("# synthetic alias tool\n", encoding="ascii")
    alias_dependency = tmp_path / "backfill_telegram_file_aliases.py"
    alias_dependency.write_text("# synthetic alias dependency\n", encoding="ascii")
    product_runner = tmp_path / "deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py"
    product_runner.parent.mkdir(parents=True)
    product_runner.write_text("# synthetic product witness runner\n", encoding="ascii")
    return operator.BuildSpec(
        commit="a" * 40,
        version="0.206.0",
        wheel=wheel,
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        runtime_lock=runtime_lock,
        runtime_lock_sha256=hashlib.sha256(runtime_lock.read_bytes()).hexdigest(),
        wheelhouse=wheelhouse,
        wheelhouse_manifest=wheelhouse_manifest,
        wheelhouse_manifest_sha256=hashlib.sha256(wheelhouse_manifest.read_bytes()).hexdigest(),
        releases_root=releases_root,
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        friday_home=friday_home,
        state_dir=state_dir,
        base_python=base_python,
        base_python_sha256=hashlib.sha256(base_python.read_bytes()).hexdigest(),
        alias_tool=alias_tool,
        alias_tool_sha256=hashlib.sha256(alias_tool.read_bytes()).hexdigest(),
        alias_dependency=alias_dependency,
        alias_dependency_sha256=hashlib.sha256(alias_dependency.read_bytes()).hexdigest(),
        secondary_product_runner=product_runner,
        secondary_product_runner_sha256=hashlib.sha256(product_runner.read_bytes()).hexdigest(),
        release_retention_toolchain_manifest_sha256=(_release_retention_toolchain_manifest_sha256()),
        build_receipt_profile=operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER,
        max_schema=34,
    )


def test_build_uses_the_shared_release_operator_transaction_lock(
    tmp_path: Path,
    isolated_operator_transaction_domain: Path,
) -> None:
    spec = _synthetic_build_spec(tmp_path)

    with (
        operator.OperatorTransactionLock(spec.state_dir / "immutable-release-operator.v1.lock"),
        pytest.raises(operator.ReleaseFailure, match="^operator_transaction_in_progress$"),
    ):
        operator.build_release(spec)


def test_build_rejects_unadmitted_retention_toolchain_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    spec = replace(
        _synthetic_build_spec(tmp_path),
        release_retention_toolchain_manifest_sha256="f" * 64,
    )
    monkeypatch.setattr(operator, "_preflight_base_python", lambda _python: None)
    staging_calls: list[object] = []
    monkeypatch.setattr(
        operator.tempfile,
        "mkdtemp",
        lambda *args, **kwargs: staging_calls.append((args, kwargs)),
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^release_retention_toolchain_manifest_digest_mismatch$",
    ):
        operator.build_release(spec)

    assert staging_calls == []
    assert list(spec.releases_root.iterdir()) == []


def test_build_rejects_unknown_receipt_profile_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    spec = replace(
        _synthetic_build_spec(tmp_path),
        build_receipt_profile="prompt-selected-compatibility",
    )
    staging_calls: list[object] = []
    monkeypatch.setattr(
        operator.tempfile,
        "mkdtemp",
        lambda *args, **kwargs: staging_calls.append((args, kwargs)),
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^release_build_receipt_profile_invalid$",
    ):
        operator.build_release(spec)

    assert staging_calls == []
    assert list(spec.releases_root.iterdir()) == []


def test_build_rejects_an_alternate_state_lock_scope_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _synthetic_build_spec(tmp_path)
    alternate = tmp_path / "alternate-state"
    alternate.mkdir(mode=0o700)
    monkeypatch.setattr(
        operator,
        "_build_release_locked",
        lambda _spec: (_ for _ in ()).throw(AssertionError("build mutation reached")),
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_transaction_state_scope_invalid$",
    ):
        operator.build_release(replace(spec, state_dir=alternate))

    assert list(alternate.iterdir()) == []


@pytest.mark.parametrize("field", ["releases_root", "anchor", "env_file"])
def test_build_rejects_every_noncanonical_home_layout_path_before_lock_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    spec = _synthetic_build_spec(tmp_path)
    monkeypatch.setattr(
        operator,
        "_build_release_locked",
        lambda _spec: (_ for _ in ()).throw(AssertionError("build mutation reached")),
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_transaction_layout_invalid$",
    ):
        operator.build_release(replace(spec, **{field: tmp_path / f"alternate-{field}"}))

    assert not (spec.state_dir / "immutable-release-operator.v1.lock").exists()


def test_two_homes_cannot_reuse_one_release_root_under_independent_build_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    canonical = _synthetic_build_spec(tmp_path)
    alternate_home = tmp_path / "alternate-home"
    alternate_state = alternate_home / "data/state"
    alternate_state.mkdir(parents=True, mode=0o700)
    mutation_calls: list[object] = []
    monkeypatch.setattr(
        operator,
        "_build_release_locked",
        lambda spec: mutation_calls.append(spec),
    )
    alternate = replace(
        canonical,
        friday_home=alternate_home,
        state_dir=alternate_state,
    )

    with (
        operator.OperatorTransactionLock(canonical.state_dir / "immutable-release-operator.v1.lock"),
        pytest.raises(
            operator.ReleaseFailure,
            match="^operator_transaction_layout_invalid$",
        ),
    ):
        operator.build_release(alternate)

    assert mutation_calls == []
    assert list(alternate_state.iterdir()) == []


def test_candidate_lock_scope_rejects_a_different_canonical_state_dir(tmp_path: Path) -> None:
    first = tmp_path / "first-home/data/state"
    second = tmp_path / "second-home/data/state"
    first.mkdir(parents=True, mode=0o700)
    second.mkdir(parents=True, mode=0o700)
    candidate = operator.ReleaseIdentity(
        tmp_path / "candidate",
        "c" * 40,
        "0.207.85",
        "d" * 64,
        50,
        venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
        operator_transaction_lock_scope_contract=(operator.OPERATOR_TRANSACTION_LOCK_SCOPE_CONTRACT),
        operator_transaction_lock_scope_sha256=(
            operator._operator_transaction_lock_scope_sha256(first)  # noqa: SLF001
        ),
    )

    with pytest.raises(operator.ReleaseFailure, match="^operator_release_lock_scope_mismatch$"):
        operator._require_candidate_bound_operator(candidate, state_dir=second)  # noqa: SLF001


def test_operator_release_layout_binds_exact_root_and_sealed_unit_paths(tmp_path: Path) -> None:
    friday_home = tmp_path / "friday-home"
    commit = "c" * 40
    release_root = friday_home / "wheel-only-releases" / commit
    artifacts = release_root / "artifacts"
    artifacts.mkdir(parents=True)
    expected_units = operator.render_units(
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        friday_home=friday_home,
    )
    for name, content in expected_units.items():
        (artifacts / name).write_text(content, encoding="utf-8")
    legacy_capability_identity = operator.ReleaseIdentity(
        release_root,
        commit,
        "0.207.84",
        "d" * 64,
        50,
    )

    operator._require_release_in_operator_layout(  # noqa: SLF001
        legacy_capability_identity,
        friday_home,
    )

    with pytest.raises(operator.ReleaseFailure, match="^operator_release_layout_mismatch$"):
        operator._require_release_in_operator_layout(  # noqa: SLF001
            replace(legacy_capability_identity, root=tmp_path / "shared-release"),
            friday_home,
        )

    (artifacts / "friday-backend.service").write_text(
        expected_units["friday-backend.service"].replace(
            str(friday_home / "current-release"),
            str(tmp_path / "foreign-anchor"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(operator.ReleaseFailure, match="^operator_release_layout_mismatch$"):
        operator._require_release_in_operator_layout(  # noqa: SLF001
            legacy_capability_identity,
            friday_home,
        )


def test_recovery_keeps_exact_legacy_fallback_exempt_from_new_candidate_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    friday_home = tmp_path / "friday-home"
    state_dir = friday_home / "data/state"
    candidate = operator.ReleaseIdentity(
        friday_home / "wheel-only-releases" / ("c" * 40),
        "c" * 40,
        "0.207.85",
        "d" * 64,
        50,
    )
    fallback = operator.ReleaseIdentity(
        tmp_path / "legacy-fallback",
        "f" * 40,
        "0.207.84",
        "e" * 64,
        50,
    )
    unrelated = replace(fallback, root=tmp_path / "unrelated", commit="a" * 40)
    bound_calls: list[tuple[Path, bool]] = []
    layout_calls: list[Path] = []
    monkeypatch.setattr(
        operator,
        "_require_candidate_bound_operator",
        lambda release, *, state_dir, require_lock_scope: bound_calls.append(
            (release.root, require_lock_scope)
        ),
    )
    monkeypatch.setattr(
        operator,
        "_require_release_in_operator_layout",
        lambda release, _home: layout_calls.append(release.root),
    )

    operator._require_recovery_executor_operator(  # noqa: SLF001
        fallback,
        candidate=candidate,
        fallback=fallback,
        state_dir=state_dir,
        friday_home=friday_home,
    )
    operator._require_recovery_executor_operator(  # noqa: SLF001
        candidate,
        candidate=candidate,
        fallback=fallback,
        state_dir=state_dir,
        friday_home=friday_home,
    )
    with pytest.raises(
        operator.ReleaseFailure,
        match="^recovery_executor_not_schema_capable_release$",
    ):
        operator._require_recovery_executor_operator(  # noqa: SLF001
            unrelated,
            candidate=candidate,
            fallback=fallback,
            state_dir=state_dir,
            friday_home=friday_home,
        )

    assert bound_calls == [(fallback.root, False), (candidate.root, True)]
    assert layout_calls == [candidate.root]


def test_relocation_rebinds_discovered_entrypoints_and_metadata_across_atomic_publish(
    tmp_path: Path,
) -> None:
    staging, target, entrypoints, record, pycache = _write_relocation_fixture(tmp_path)
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    pre = operator.ReleaseIdentity(
        staging,
        "a" * 40,
        "0.206.0rc1",
        "0" * 64,
        34,
        venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
    )
    operator._direct_console_smoke(  # noqa: SLF001
        pre,
        scratch=tmp_path,
        environment=environment,
    )
    operator._activation_smoke(  # noqa: SLF001
        physical_root=staging,
        bound_root=staging,
        require_interpreter=True,
        scratch=tmp_path,
        environment=environment,
    )
    discovered = operator._relocate_venv_generated_paths(staging, target)  # noqa: SLF001
    assert {path.name for path in discovered} == {path.name for path in entrypoints}
    final_shebang = b"#!" + os.fsencode(str(target / "venv/bin/python")) + b"\n"
    assert all(path.read_bytes().startswith(final_shebang) for path in discovered)
    _remove_fixture_pycache(pycache)
    operator._verify_relocated_venv(  # noqa: SLF001
        staging,
        bound_root=target,
        forbidden_staging_root=staging,
    )
    operator._activation_smoke(  # noqa: SLF001
        physical_root=staging,
        bound_root=target,
        require_interpreter=False,
        scratch=tmp_path,
        environment=environment,
    )
    assert os.fsencode(str(staging)) not in record.read_bytes()

    os.replace(staging, target)
    post = replace(pre, root=target)
    operator._verify_relocated_venv(  # noqa: SLF001
        target,
        bound_root=target,
        forbidden_staging_root=target.parent / f".{post.commit}.",
    )
    operator._direct_console_smoke(  # noqa: SLF001
        post,
        scratch=tmp_path,
        environment=environment,
    )
    operator._activation_smoke(  # noqa: SLF001
        physical_root=target,
        bound_root=target,
        require_interpreter=True,
        scratch=tmp_path,
        environment=environment,
    )
    assert not staging.exists()
    assert target.is_dir()


def test_release_tree_copy_verifies_against_its_authenticated_bind_mount_path(
    tmp_path: Path,
) -> None:
    staging, target, _entrypoints, _record, pycache = _write_relocation_fixture(tmp_path)
    operator._relocate_venv_generated_paths(staging, target)  # noqa: SLF001
    _remove_fixture_pycache(pycache)
    os.replace(staging, target)
    artifacts = target / "artifacts"
    artifacts.mkdir()
    (artifacts / "immutable-release.json").write_bytes(b"{}\n")
    operator._seal_release_tree(target)  # noqa: SLF001
    artifacts.chmod(0o700)
    manifest = artifacts / "release-tree.sha256"
    manifest.write_text(
        "\n".join(operator._manifest_entries(target, mode_overrides={"artifacts": 0o500}))  # noqa: SLF001
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o400)
    artifacts.chmod(0o500)

    copies = tmp_path / "copies"
    copies.mkdir(mode=0o700)
    copied_root = copies / "sealed-copy"
    shutil.copytree(target, copied_root, symlinks=True, copy_function=shutil.copy2)
    release = operator.ReleaseIdentity(
        copied_root,
        target.name,
        "0.207.90",
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
        50,
        venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^release_venv_relocation_identity_mismatch$",
    ):
        operator.verify_release_tree(release)
    operator._verify_release_tree(release, venv_bound_root=target)  # noqa: SLF001
    operator._cleanup_staging_tree(copied_root)  # noqa: SLF001
    operator._cleanup_staging_tree(target)  # noqa: SLF001
    assert not copied_root.exists()
    assert not target.exists()


@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_relocation_exhaustively_rejects_unknown_staging_path_leak(
    tmp_path: Path,
    kind: str,
) -> None:
    staging, target, _entrypoints, _record, pycache = _write_relocation_fixture(tmp_path)
    operator._relocate_venv_generated_paths(staging, target)  # noqa: SLF001
    _remove_fixture_pycache(pycache)
    stale = staging / "venv" / f"unknown-stale-{kind}"
    if kind == "regular":
        stale.write_bytes(b"prefix:" + os.fsencode(str(staging)) + b":suffix")
    else:
        stale.symlink_to(staging / "vanished-target")
    with pytest.raises(operator.ReleaseFailure, match="^release_staging_path_leaked$"):
        operator._verify_relocated_venv(  # noqa: SLF001
            staging,
            bound_root=target,
            forbidden_staging_root=staging,
        )
    assert not target.exists()


@pytest.mark.parametrize(
    "mutation",
    ["wrong_hash", "missing_owner", "blank_owner", "path_escape", "present_pycache"],
)
def test_relocation_rejects_inexact_or_missing_entrypoint_record(
    tmp_path: Path,
    mutation: str,
) -> None:
    staging, target, _entrypoints, record, pycache = _write_relocation_fixture(tmp_path)
    operator._relocate_venv_generated_paths(staging, target)  # noqa: SLF001
    if mutation != "present_pycache":
        _remove_fixture_pycache(pycache)
    rows = operator._read_record_rows(record)  # noqa: SLF001
    row = next(item for item in rows if item[0].endswith("/friday"))
    if mutation == "wrong_hash":
        row[1] = "sha256=" + "A" * 43
    elif mutation == "missing_owner":
        rows.remove(row)
    elif mutation == "blank_owner":
        row[1:] = ["", ""]
    elif mutation == "path_escape":
        row[0] = "../../../../../../etc/passwd"
    operator._write_record_rows(record, rows)  # noqa: SLF001
    expected = (
        "installed_record_mismatch|installed_entrypoint_record_owner_mismatch|"
        "installed_record_unbound_entry|installed_record_path_escape|installed_record_pycache_present"
    )
    with pytest.raises(operator.ReleaseFailure, match=expected):
        operator._verify_relocated_venv(  # noqa: SLF001
            staging,
            bound_root=target,
            forbidden_staging_root=staging,
        )


@pytest.mark.parametrize(
    "binding",
    ["shebang", "activate", "activate.csh", "activate.fish", "pyvenv"],
)
def test_relocation_rejects_non_exact_final_bindings(tmp_path: Path, binding: str) -> None:
    staging, target, _entrypoints, _record, pycache = _write_relocation_fixture(tmp_path)
    operator._relocate_venv_generated_paths(staging, target)  # noqa: SLF001
    _remove_fixture_pycache(pycache)
    if binding == "shebang":
        path = staging / "venv/bin/future-console"
        content = path.read_bytes()
        path.write_bytes(b"#!/usr/bin/python3\n" + content.split(b"\n", 1)[1])
    elif binding.startswith("activate"):
        path = staging / "venv/bin" / binding
        target_bytes = os.fsencode(str(target))
        content = path.read_bytes().replace(target_bytes, b"/wrong", 1)
        path.write_bytes(content + b"\n# compensating decoy " + target_bytes + b"\n")
    else:
        path = staging / "venv/pyvenv.cfg"
        path.write_bytes(path.read_bytes().replace(os.fsencode(str(target)), b"/wrong", 1))
    with pytest.raises(
        operator.ReleaseFailure,
        match="release_entrypoint_shebang_mismatch|release_activation_binding_mismatch|release_pyvenv_binding_mismatch",
    ):
        operator._verify_relocated_venv(  # noqa: SLF001
            staging,
            bound_root=target,
            forbidden_staging_root=staging,
        )


def test_activation_smoke_rejects_late_plain_virtual_env_override(tmp_path: Path) -> None:
    staging, target, _entrypoints, _record, pycache = _write_relocation_fixture(tmp_path)
    operator._relocate_venv_generated_paths(staging, target)  # noqa: SLF001
    _remove_fixture_pycache(pycache)
    activate = staging / "venv/bin/activate"
    activate.write_bytes(activate.read_bytes() + b"\nVIRTUAL_ENV=/wrong\nexport VIRTUAL_ENV\n")
    with pytest.raises(operator.ReleaseFailure, match="^release_activation_binding_mismatch$"):
        operator._verify_activation_bindings(staging, bound_root=target)  # noqa: SLF001

    os.replace(staging, target)
    with (
        operator._isolated_smoke_environment(target) as (scratch, environment),  # noqa: SLF001
        pytest.raises(operator.ReleaseFailure, match="^installed_activation_smoke_failed$"),
    ):
        operator._activation_smoke(  # noqa: SLF001
            physical_root=target,
            bound_root=target,
            require_interpreter=True,
            scratch=scratch,
            environment=environment,
        )


@pytest.mark.parametrize("payload", [b"exit 0", b"exec true"], ids=["exit", "exec"])
def test_activation_smoke_rejects_early_successful_exit(tmp_path: Path, payload: bytes) -> None:
    staging, _target, _entrypoints, _record, _pycache = _write_relocation_fixture(tmp_path)
    activate = staging / "venv/bin/activate"
    activate.write_bytes(activate.read_bytes() + b"\n" + payload + b"\n")
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    with pytest.raises(operator.ReleaseFailure, match="^installed_activation_smoke_failed$"):
        operator._activation_smoke(  # noqa: SLF001
            physical_root=staging,
            bound_root=staging,
            require_interpreter=True,
            scratch=tmp_path,
            environment=environment,
        )


@pytest.mark.parametrize("failure_phase", ["pre_publish", "post_publish"])
@pytest.mark.parametrize(
    "build_receipt_profile",
    [
        operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER,
        operator.BUILD_RECEIPT_PROFILE_P0H_RETENTION,
    ],
    ids=["reader-only-receipt", "p0h-pair-bearing-receipt"],
)
def test_build_smoke_failure_cleans_only_prepublication_staging_and_quarantines_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    build_receipt_profile: str,
    isolated_operator_transaction_domain: Path,
) -> None:
    spec = replace(
        _synthetic_build_spec(tmp_path),
        build_receipt_profile=build_receipt_profile,
    )
    smoke_roots: list[Path] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[1:5] == ["-I", "-B", "-m", "venv"]:
            venv_root = Path(command[-1])
            (venv_root / "bin").mkdir(parents=True)
            python = venv_root / "bin/python"
            python.write_bytes(b"synthetic interpreter")
            python.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    def smoke(release: operator.ReleaseIdentity) -> str:
        smoke_roots.append(release.root)
        should_fail = failure_phase == "pre_publish" or len(smoke_roots) == 2
        if should_fail:
            raise operator.ReleaseFailure(f"synthetic_{failure_phase}_smoke_failure")
        return "0" * 64

    monkeypatch.setattr(operator.subprocess, "run", run)
    monkeypatch.setattr(operator, "installed_surface_smoke", smoke)
    monkeypatch.setattr(operator, "_relocate_venv_generated_paths", lambda *_args: ())
    monkeypatch.setattr(operator, "_verify_relocated_venv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(operator, "_activation_smoke", lambda **_kwargs: None)
    with pytest.raises(
        operator.ReleaseFailure,
        match=f"^synthetic_{failure_phase}_smoke_failure$",
    ):
        operator.build_release(spec)
    assert len(smoke_roots) == (1 if failure_phase == "pre_publish" else 2)
    target = spec.releases_root / spec.commit
    if failure_phase == "pre_publish":
        assert not target.exists()
        assert list(spec.releases_root.iterdir()) == []
        return
    assert target.is_dir()
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o500
    assert list(spec.releases_root.iterdir()) == [target]
    manifest = target / "artifacts/release-tree.sha256"
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    release = operator.ReleaseIdentity(
        target,
        spec.commit,
        spec.version,
        digest,
        spec.max_schema,
        operator.MEMORY_VAULT_MODE_CONTRACT,
        operator.VENV_RELOCATION_CONTRACT,
    )
    operator.verify_release_tree(release)
    product_runner = target / operator._SECONDARY_PRODUCT_RUNNER_ARTIFACT  # noqa: SLF001
    assert product_runner.read_bytes() == spec.secondary_product_runner.read_bytes()
    assert stat.S_IMODE(os.lstat(product_runner).st_mode) == 0o400
    toolchain_manifest = target / operator._RELEASE_RETENTION_TOOLCHAIN_MANIFEST  # noqa: SLF001
    toolchain_manifest_raw = toolchain_manifest.read_bytes()
    metadata = json.loads((target / "artifacts/immutable-release.json").read_text(encoding="ascii"))
    historical_metadata_keys = {
        "alias_dependency_sha256",
        "alias_tool_sha256",
        "base_python_sha256",
        "bootstrap_pins",
        "bootstrap_wheel_sha256",
        "commit",
        "engineer_command_lifecycle_contract",
        "max_schema",
        "memory_vault_mode_contract",
        "obsidian_cutover_contract",
        "operator_sha256",
        "runtime_lock_sha256",
        "runtime_pin_count",
        "schema",
        "secondary_product_runner_sha256",
        "venv_relocation_contract",
        "version",
        "wheel_sha256",
        "wheelhouse_manifest_sha256",
    }
    p0h_metadata_keys = {
        "operator_transaction_lock_scope_contract",
        "operator_transaction_lock_scope_sha256",
        "release_retention_toolchain_contract",
        "release_retention_toolchain_manifest_sha256",
    }
    if build_receipt_profile == operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER:
        assert set(metadata) == historical_metadata_keys
    else:
        assert set(metadata) == historical_metadata_keys | p0h_metadata_keys
        assert metadata["operator_transaction_lock_scope_contract"] == (
            operator.OPERATOR_TRANSACTION_LOCK_SCOPE_CONTRACT
        )
        assert metadata["operator_transaction_lock_scope_sha256"] == (
            operator._operator_transaction_lock_scope_sha256(spec.state_dir)  # noqa: SLF001
        )
        assert metadata["release_retention_toolchain_contract"] == (
            operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT
        )
        assert metadata["release_retention_toolchain_manifest_sha256"] == (
            spec.release_retention_toolchain_manifest_sha256
        )
    assert hashlib.sha256(toolchain_manifest_raw).hexdigest() == (
        spec.release_retention_toolchain_manifest_sha256
    )
    toolchain_payload = json.loads(toolchain_manifest_raw)
    assert toolchain_payload["schema"] == operator.RELEASE_RETENTION_TOOLCHAIN_MANIFEST_SCHEMA
    assert toolchain_payload["contract"] == operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT
    expected_sources = operator._read_release_retention_toolchain_sources(  # noqa: SLF001
        Path(operator.__file__).resolve(strict=True)
    )
    assert [item["path"] for item in toolchain_payload["files"]] == [
        operator._release_retention_toolchain_relative_path(module).as_posix()  # noqa: SLF001
        for module in operator._RELEASE_RETENTION_TOOLCHAIN_PACKAGE_FILES  # noqa: SLF001
    ]
    for item in toolchain_payload["files"]:
        packaged = target / item["path"]
        module = packaged.name
        assert packaged.read_bytes() == expected_sources[module]
        assert item["sha256"] == hashlib.sha256(expected_sources[module]).hexdigest()
        assert item["size"] == len(expected_sources[module])
        assert stat.S_IMODE(os.lstat(packaged).st_mode) == 0o400
    loaded = operator.load_release_identity(target, expected_tree_sha256=digest)
    assert loaded.secondary_product_runner_sha256 == spec.secondary_product_runner_sha256
    assert loaded.sealed_release_retention_toolchain_manifest_sha256 == (
        spec.release_retention_toolchain_manifest_sha256
    )
    operator._require_release_in_operator_layout(loaded, spec.friday_home)  # noqa: SLF001
    if build_receipt_profile == operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER:
        assert loaded.release_retention_toolchain_contract == ""
        assert loaded.release_retention_toolchain_manifest_sha256 == ""
        assert loaded.build_receipt_profile == (operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER)
        operator._require_reader_only_release_profile(loaded)  # noqa: SLF001
        with pytest.raises(
            operator.ReleaseFailure,
            match="^operator_release_retention_toolchain_missing$",
        ):
            operator._require_release_retention_toolchain(loaded)  # noqa: SLF001
        assert loaded.operator_transaction_lock_scope_contract == ""
        assert loaded.operator_transaction_lock_scope_sha256 == ""
    else:
        assert loaded.build_receipt_profile == operator.BUILD_RECEIPT_PROFILE_P0H_RETENTION
        assert loaded.release_retention_toolchain_contract == (operator.RELEASE_RETENTION_TOOLCHAIN_CONTRACT)
        assert loaded.release_retention_toolchain_manifest_sha256 == (
            spec.release_retention_toolchain_manifest_sha256
        )
        assert loaded.operator_transaction_lock_scope_contract == (
            operator.OPERATOR_TRANSACTION_LOCK_SCOPE_CONTRACT
        )
        assert loaded.operator_transaction_lock_scope_sha256 == (
            operator._operator_transaction_lock_scope_sha256(spec.state_dir)  # noqa: SLF001
        )
        operator._require_release_retention_toolchain(loaded)  # noqa: SLF001
    with pytest.raises(operator.ReleaseFailure, match="^release_target_exists$"):
        operator.build_release(spec)
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == digest


def test_installed_surface_smoke_uses_one_hermetic_environment_and_cleans_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = replace(
        _release(tmp_path, "release", schema=34, commit="a" * 40),
        venv_relocation_contract=operator.VENV_RELOCATION_CONTRACT,
    )
    (release.root / "venv/bin").mkdir(parents=True)
    live_home = tmp_path / "live-home"
    live_state = live_home / "data/state"
    live_state.mkdir(parents=True)
    (live_state / "friday.sqlite3").write_bytes(b"live-current")
    (live_state / "jericho.sqlite3").write_bytes(b"live-legacy")
    smoke_temp_root = tmp_path / "code-owned-smoke-root"
    smoke_temp_root.mkdir(mode=0o700)
    hostile_tmp = live_home / "data/obsidian"
    hostile_tmp.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(hostile_tmp))
    monkeypatch.setattr(operator, "_SMOKE_SCRATCH_ROOT", smoke_temp_root)
    poison = {
        "HOME": str(live_home),
        "PYTHONPATH": "/sentinel/import-injection",
        "SECRET_SENTINEL": "must-not-reach-smoke",
        "FRIDAY_HOME": str(live_home),
        "JERICHO_HOME": str(live_home),
        "FRIDAY_DATA_DIR": str(live_home / "data"),
        "JERICHO_DATA_DIR": str(live_home / "legacy-data"),
        "FRIDAY_STATE_DIR": str(live_state),
        "JERICHO_STATE_DIR": str(live_home / "legacy-state"),
        "FRIDAY_DATABASE_PATH": str(live_state / "friday.sqlite3"),
        "FRIDAY_DATABASE_MUST_EXIST": "1",
        "JERICHO_DATABASE_MUST_EXIST": "1",
        "FRIDAY_ENV_FILE": str(live_home / "live.env"),
        "JERICHO_DATABASE_PATH": str(live_state / "jericho.sqlite3"),
        "JERICHO_ENV_FILE": str(live_home / "legacy.env"),
    }
    for name, value in poison.items():
        monkeypatch.setenv(name, value)
    observed: list[tuple[list[str], dict[str, object]]] = []
    receipt = b'{"memory_vault_mode_contract":"","schema":34,"status":"clear"}\n'

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append((command, kwargs))
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "PYTHONPATH" not in environment
        assert "SECRET_SENTINEL" not in environment
        assert not any("sentinel" in value for value in environment.values())
        assert not Path(environment["FRIDAY_ENV_FILE"]).exists()
        if command[1:4] == ["-I", "-B", "-c"]:
            return subprocess.CompletedProcess(command, 0, stdout=receipt, stderr=b"")
        if command[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=operator._ACTIVATION_SMOKE_RECEIPT,  # noqa: SLF001
                stderr=b"",
            )
        if command[-1] == "--version":
            output = f"pip 26.1.2 from {release.root / 'venv'}/site-packages/pip\n".encode()
        else:
            output = b"synthetic help\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    assert operator.installed_surface_smoke(release) == hashlib.sha256(receipt).hexdigest()

    assert len(observed) == 5
    first_environment = observed[0][1]["env"]
    assert isinstance(first_environment, dict)
    assert all(first_environment is call[1]["env"] for call in observed[1:])
    scratch = Path(observed[0][1]["cwd"])
    assert all(scratch == Path(call[1]["cwd"]) for call in observed[1:])
    assert scratch.parent == smoke_temp_root
    assert not scratch.is_relative_to(release.root)
    home = scratch / "home"
    data = home / "data"
    state = data / "state"
    assert first_environment == {
        "HOME": str(home),
        "FRIDAY_HOME": str(home),
        "JERICHO_HOME": str(home),
        "FRIDAY_DATA_DIR": str(data),
        "JERICHO_DATA_DIR": str(data),
        "FRIDAY_STATE_DIR": str(state),
        "JERICHO_STATE_DIR": str(state),
        "FRIDAY_DATABASE_PATH": str(state / "smoke.sqlite3"),
        "JERICHO_DATABASE_PATH": str(state / "smoke.sqlite3"),
        "FRIDAY_DATABASE_MUST_EXIST": "0",
        "JERICHO_DATABASE_MUST_EXIST": "0",
        "FRIDAY_ENV_FILE": str(home / "config/no-env-file"),
        "JERICHO_ENV_FILE": str(home / "config/no-env-file"),
        "TMPDIR": str(scratch / "tmp"),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert observed[0][0][:4] == [str(release.root / "venv/bin/python"), "-I", "-B", "-c"]
    assert [call[0] for call in observed[1:4]] == [
        [str(release.root / "venv/bin/friday"), "--help"],
        [str(release.root / "venv/bin/jericho"), "--help"],
        [str(release.root / "venv/bin/pip"), "--version"],
    ]
    assert observed[4][0][:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
    assert not scratch.exists()
    assert list(hostile_tmp.iterdir()) == []
    assert all(os.environ[name] == value for name, value in poison.items())


def test_surface_smoke_requires_host_packages_only_from_schema_43() -> None:
    script = operator._smoke_script(  # noqa: SLF001
        Path("/sealed/release"),
        "0.207.35",
        43,
        operator.OBSIDIAN_CUTOVER_CONTRACT,
    )

    assert "if expected['schema']>=43:\n import friday_host_agent, friday_package_broker" in script
    assert "friday.storage._conversations, friday.telegram_bridge)+host_modules" in script
    compile(script, "<installed-surface-smoke>", "exec")


def test_smoke_scratch_root_rejects_release_and_runtime_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "_SMOKE_SCRATCH_ROOT", release)
    with (
        pytest.raises(operator.ReleaseFailure, match="installed_surface_smoke_isolation_failed"),
        operator._isolated_smoke_environment(release),  # noqa: SLF001
    ):
        pytest.fail("overlapping smoke root must not be entered")
    assert list(release.iterdir()) == []

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    base = _systemd_test_port(runtime)
    monkeypatch.setattr(operator, "_SMOKE_SCRATCH_ROOT", base.config.state_dir)
    with pytest.raises(operator.ReleaseFailure, match="smoke_scratch_runtime_overlap"):
        operator.SystemdActivationPort(base.config)


def test_installed_surface_smoke_cleans_hermetic_root_after_probe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release(tmp_path, "release", schema=34, commit="a" * 40)
    (release.root / "venv/bin").mkdir(parents=True)
    observed_scratch: list[Path] = []
    sealed_markers: list[Path] = []
    external_marker = tmp_path / "outside-smoke"
    external_marker.write_text("must survive", encoding="ascii")
    receipt = b'{"memory_vault_mode_contract":"","schema":34,"status":"clear"}\n'

    def fail(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        scratch = Path(kwargs["cwd"])
        observed_scratch.append(scratch)
        if command[1:4] == ["-I", "-B", "-c"]:
            return subprocess.CompletedProcess(command, 0, stdout=receipt, stderr=b"")
        sealed = scratch / "sealed-subtree"
        sealed.mkdir()
        marker = sealed / "marker"
        marker.write_bytes(b"sealed")
        (sealed / "external-link").symlink_to(external_marker)
        marker.chmod(0o400)
        sealed.chmod(0o500)
        sealed_markers.append(marker)
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"closed")

    monkeypatch.setattr(operator.subprocess, "run", fail)
    with pytest.raises(operator.ReleaseFailure, match="^installed_cli_smoke_failed$"):
        operator.installed_surface_smoke(release)

    assert len(observed_scratch) == 2
    assert observed_scratch[0] == observed_scratch[1]
    assert not observed_scratch[0].exists()
    assert len(sealed_markers) == 1
    assert not sealed_markers[0].exists()
    assert external_marker.read_text(encoding="ascii") == "must survive"


def test_installed_surface_smoke_fails_closed_when_cleanup_leaves_the_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release(tmp_path, "release", schema=34, commit="a" * 40)
    (release.root / "venv/bin").mkdir(parents=True)
    receipt = b'{"memory_vault_mode_contract":"","schema":34,"status":"clear"}\n'
    observed_scratch: list[Path] = []
    real_cleanup = operator._cleanup_staging_tree  # noqa: SLF001

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed_scratch.append(Path(kwargs["cwd"]))
        if command[1:4] == ["-I", "-B", "-c"]:
            return subprocess.CompletedProcess(command, 0, stdout=receipt, stderr=b"")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    monkeypatch.setattr(operator, "_cleanup_staging_tree", lambda _root: None)
    try:
        with pytest.raises(operator.ReleaseFailure, match="^installed_surface_smoke_cleanup_failed$"):
            operator.installed_surface_smoke(release)
        assert len(observed_scratch) == 2
        assert observed_scratch[0] == observed_scratch[1]
        assert observed_scratch[0].exists()
    finally:
        for scratch in set(observed_scratch):
            real_cleanup(scratch)
    assert not observed_scratch[0].exists()


def test_actual_installed_source_smoke_ignores_a_poisoned_live_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "synthetic-release"
    venv.EnvBuilder(with_pip=False, system_site_packages=True, symlinks=False).create(release_root / "venv")
    python = release_root / "venv/bin/python"
    lookup = subprocess.run(  # noqa: S603
        [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "lookup-home"),
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    purelib = Path(lookup.stdout.strip()).resolve(strict=True)
    runtime_site = next(
        parent
        for parent in Path(pytest.__file__).resolve(strict=True).parents
        if parent.name == "site-packages"
    )
    (purelib / "runtime-dependencies.pth").write_text(f"{runtime_site}\n", encoding="ascii")
    source_root = Path(__file__).parents[1]
    for package_root in ("friday", "friday_host_agent", "friday_package_broker"):
        shutil.copytree(
            source_root / package_root,
            purelib / package_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    version = friday.__version__
    metadata = purelib / f"friday-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: friday\nVersion: {version}\n",
        encoding="ascii",
    )

    live_home = tmp_path / "live-home"
    live_state = live_home / "data/state"
    live_state.mkdir(parents=True)
    current_database = live_state / "friday.sqlite3"
    legacy_database = live_state / "jericho.sqlite3"
    current_database.write_bytes(b"live-current")
    legacy_database.write_bytes(b"live-legacy")
    live_env = live_home / "live.env"
    live_env.write_text("# deliberately empty live environment\n", encoding="ascii")
    for name, value in {
        "HOME": str(live_home),
        "PYTHONPATH": "/sentinel/import-injection",
        "FRIDAY_HOME": str(live_home),
        "JERICHO_HOME": str(live_home),
        "FRIDAY_DATA_DIR": str(live_home / "data"),
        "FRIDAY_STATE_DIR": str(live_state),
        "FRIDAY_DATABASE_PATH": "",
        "JERICHO_DATABASE_PATH": "",
        "FRIDAY_DATABASE_MUST_EXIST": "1",
        "FRIDAY_ENV_FILE": str(live_env),
    }.items():
        monkeypatch.setenv(name, value)

    release = operator.ReleaseIdentity(
        root=release_root,
        commit="a" * 40,
        version=version,
        tree_manifest_sha256="b" * 64,
        max_schema=SCHEMA_VERSION,
        memory_vault_mode_contract=operator.MEMORY_VAULT_MODE_CONTRACT,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    receipt = (f'{{"memory_vault_mode_contract":"v1","schema":{SCHEMA_VERSION},"status":"clear"}}\n').encode(
        "ascii"
    )
    assert operator.installed_surface_smoke(release) == hashlib.sha256(receipt).hexdigest()
    assert current_database.read_bytes() == b"live-current"
    assert legacy_database.read_bytes() == b"live-legacy"


def test_base_python_venv_preflight_uses_an_isolated_silent_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_python = tmp_path / "python3.14"
    observed: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    operator._preflight_base_python(base_python)  # noqa: SLF001
    assert observed == [
        (
            [str(base_python), "-I", "-B", "-c", "import venv"],
            {"check": False, "capture_output": True, "timeout": 30},
        )
    ]


def test_release_venv_is_created_without_pip_or_ensurepip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_python = tmp_path / "python3.14"
    target = tmp_path / "release" / "venv"
    observed: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    operator._create_pipless_venv(base_python, target)  # noqa: SLF001
    assert observed == [
        (
            [
                str(base_python),
                "-I",
                "-B",
                "-m",
                "venv",
                "--without-pip",
                "--copies",
                str(target),
            ],
            {
                "check": False,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "timeout": 180,
            },
        )
    ]


def test_wheelhouse_is_exactly_runtime_plus_manifest_bound_bootstrap(tmp_path: Path) -> None:
    wheelhouse, manifest = _write_synthetic_wheelhouse(tmp_path)
    expected_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert (
        operator._verify_wheelhouse(  # noqa: SLF001
            wheelhouse,
            manifest,
            {"anyio": "4.14.2"},
        )
        == expected_sha256
    )

    pip_filename = operator.BOOTSTRAP_WHEELS[0][2]
    pip_wheel = wheelhouse / pip_filename
    renamed = wheelhouse / "pip-26.1.2-1-py3-none-any.whl"
    pip_wheel.rename(renamed)
    _rewrite_synthetic_wheelhouse_manifest(wheelhouse, manifest)
    with pytest.raises(operator.ReleaseFailure, match="^wheelhouse_pin_mismatch$"):
        operator._verify_wheelhouse(wheelhouse, manifest, {"anyio": "4.14.2"})  # noqa: SLF001

    renamed.rename(pip_wheel)
    _rewrite_synthetic_wheelhouse_manifest(wheelhouse, manifest)
    pip_wheel.write_bytes(b"tampered bootstrap")
    with pytest.raises(operator.ReleaseFailure, match="^wheelhouse_manifest_mismatch$"):
        operator._verify_wheelhouse(wheelhouse, manifest, {"anyio": "4.14.2"})  # noqa: SLF001


@pytest.mark.parametrize(
    "forbidden_filename",
    [
        "setuptools-83.0.0-py3-none-any.whl",
        "wheel-0.47.0-py3-none-any.whl",
    ],
)
def test_wheelhouse_rejects_manifest_bound_build_tool_extras(
    tmp_path: Path,
    forbidden_filename: str,
) -> None:
    wheelhouse, manifest = _write_synthetic_wheelhouse(tmp_path)
    (wheelhouse / forbidden_filename).write_bytes(b"manifest-bound but not a runtime dependency")
    _rewrite_synthetic_wheelhouse_manifest(wheelhouse, manifest)

    with pytest.raises(operator.ReleaseFailure, match="^wheelhouse_pin_mismatch$"):
        operator._verify_wheelhouse(wheelhouse, manifest, {"anyio": "4.14.2"})  # noqa: SLF001


def test_bootstrap_installs_only_the_pip_wheel_into_the_pipless_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse, _manifest = _write_synthetic_wheelhouse(tmp_path)
    target_python = tmp_path / "venv" / "bin" / "python"
    observed: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    operator._bootstrap_target_pip(target_python, wheelhouse)  # noqa: SLF001
    pip_wheel = wheelhouse / operator.BOOTSTRAP_WHEELS[0][2]
    assert operator.BOOTSTRAP_WHEELS == (("pip", "26.1.2", "pip-26.1.2-py3-none-any.whl"),)
    assert observed == [
        (
            [
                str(target_python),
                "-I",
                "-B",
                f"{pip_wheel}/pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-index",
                "--no-deps",
                str(pip_wheel),
            ],
            {
                "check": False,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "timeout": 300,
            },
        )
    ]


def test_installed_surface_is_exactly_runtime_plus_pip_and_friday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_python = tmp_path / "venv" / "bin" / "python"
    observed: list[tuple[list[str], dict[str, object]]] = []
    expected = json.dumps(
        {"anyio": "4.14.2", "pip": "26.1.2"},
        ensure_ascii=True,
        separators=(",", ":"),
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.append((command, kwargs))
        script = command[-1]
        assert f"expected=json.loads({expected!r})" in script
        assert "set(actual)==set(expected)|{'friday'}" in script
        assert "setuptools" not in script
        assert '"wheel"' not in script
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    operator._installed_pin_smoke(target_python, {"anyio": "4.14.2"})  # noqa: SLF001

    assert observed == [
        (
            [str(target_python), "-I", "-B", "-c", observed[0][0][-1]],
            {"check": False, "capture_output": True, "timeout": 60},
        )
    ]


def test_pip_bootstrap_pin_matches_the_frozen_build_tool_lock() -> None:
    dev_pins = operator._runtime_pins(Path(__file__).parents[1] / "requirements-dev.lock")  # noqa: SLF001
    assert operator.BOOTSTRAP_WHEELS == (("pip", "26.1.2", "pip-26.1.2-py3-none-any.whl"),)
    assert dev_pins["pip"] == "26.1.2"


def test_missing_venv_fails_before_release_staging_or_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    friday_home = tmp_path / "friday-home"
    releases_root = friday_home / "wheel-only-releases"
    releases_root.mkdir(parents=True, mode=0o700)
    state_dir = friday_home / "data/state"
    state_dir.mkdir(parents=True, mode=0o700)
    wheelhouse, wheelhouse_manifest = _write_synthetic_wheelhouse(tmp_path)
    runtime_lock = tmp_path / "runtime.lock"
    runtime_lock.write_text("anyio==4.14.2\n", encoding="ascii")
    wheel = tmp_path / "friday-0.206.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic Friday wheel")
    base_python = tmp_path / "python3.14"
    base_python.write_bytes(b"synthetic Python")
    alias_tool = tmp_path / "backfill_file_alias_filenames.py"
    alias_tool.write_text("# synthetic alias tool\n", encoding="ascii")
    alias_dependency = tmp_path / "backfill_telegram_file_aliases.py"
    alias_dependency.write_text("# synthetic alias dependency\n", encoding="ascii")
    commit = "a" * 40

    def unavailable(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"venv missing")

    def unexpected_staging(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("release staging must not be created before the venv preflight")

    monkeypatch.setattr(operator.subprocess, "run", unavailable)
    monkeypatch.setattr(operator.tempfile, "mkdtemp", unexpected_staging)
    product_runner = (
        Path(__file__).parents[1] / "deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py"
    )
    spec = operator.BuildSpec(
        commit=commit,
        version="0.206.0",
        wheel=wheel,
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        runtime_lock=runtime_lock,
        runtime_lock_sha256=hashlib.sha256(runtime_lock.read_bytes()).hexdigest(),
        wheelhouse=wheelhouse,
        wheelhouse_manifest=wheelhouse_manifest,
        wheelhouse_manifest_sha256=hashlib.sha256(wheelhouse_manifest.read_bytes()).hexdigest(),
        releases_root=releases_root,
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        friday_home=friday_home,
        state_dir=state_dir,
        base_python=base_python,
        base_python_sha256=hashlib.sha256(base_python.read_bytes()).hexdigest(),
        alias_tool=alias_tool,
        alias_tool_sha256=hashlib.sha256(alias_tool.read_bytes()).hexdigest(),
        alias_dependency=alias_dependency,
        alias_dependency_sha256=hashlib.sha256(alias_dependency.read_bytes()).hexdigest(),
        secondary_product_runner=product_runner,
        secondary_product_runner_sha256=hashlib.sha256(product_runner.read_bytes()).hexdigest(),
        release_retention_toolchain_manifest_sha256=(_release_retention_toolchain_manifest_sha256()),
        build_receipt_profile=operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER,
        max_schema=34,
    )
    with pytest.raises(operator.ReleaseFailure, match="^base_python_venv_unavailable$"):
        operator.build_release(spec)
    assert list(releases_root.iterdir()) == []
    assert not (releases_root / commit).exists()


def test_post_seal_failure_removes_staging_and_preserves_original_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    friday_home = tmp_path / "friday-home"
    releases_root = friday_home / "wheel-only-releases"
    releases_root.mkdir(parents=True, mode=0o700)
    state_dir = friday_home / "data/state"
    state_dir.mkdir(parents=True, mode=0o700)
    wheelhouse, wheelhouse_manifest = _write_synthetic_wheelhouse(tmp_path)
    runtime_lock = tmp_path / "runtime.lock"
    runtime_lock.write_text("anyio==4.14.2\n", encoding="ascii")
    wheel = tmp_path / "friday-0.206.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic Friday wheel")
    base_python = tmp_path / "python3.14"
    base_python.write_bytes(b"synthetic Python")
    alias_tool = tmp_path / "backfill_file_alias_filenames.py"
    alias_tool.write_text("# synthetic alias tool\n", encoding="ascii")
    alias_dependency = tmp_path / "backfill_telegram_file_aliases.py"
    alias_dependency.write_text("# synthetic alias dependency\n", encoding="ascii")
    sealed_markers: list[Path] = []
    external_marker = tmp_path / "must-survive-cleanup"
    external_marker.write_text("outside staging", encoding="ascii")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[1:5] == ["-I", "-B", "-m", "venv"]:
            sealed_directory = Path(command[-1]) / "lib" / "sealed-subtree"
            sealed_directory.mkdir(parents=True)
            marker = sealed_directory / "marker"
            marker.write_text("must be removed after sealing", encoding="ascii")
            (sealed_directory / "external-link").symlink_to(external_marker)
            sealed_markers.append(marker)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    def fail_after_seal(_path: Path, _value: bytes, *, final_mode: int) -> None:
        assert final_mode == 0o400
        assert len(sealed_markers) == 1
        marker = sealed_markers[0]
        assert stat.S_IMODE(os.lstat(marker).st_mode) == 0o400
        assert stat.S_IMODE(os.lstat(marker.parent).st_mode) == 0o500
        raise operator.ReleaseFailure("synthetic_post_seal_failure")

    monkeypatch.setattr(operator.subprocess, "run", run)
    monkeypatch.setattr(operator, "installed_surface_smoke", lambda _release: "0" * 64)
    monkeypatch.setattr(operator, "_relocate_venv_generated_paths", lambda *_args: ())
    monkeypatch.setattr(operator, "_verify_relocated_venv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(operator, "_activation_smoke", lambda **_kwargs: None)
    monkeypatch.setattr(operator, "_write_private_durable", fail_after_seal)
    product_runner = (
        Path(__file__).parents[1] / "deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py"
    )
    spec = operator.BuildSpec(
        commit="a" * 40,
        version="0.206.0",
        wheel=wheel,
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        runtime_lock=runtime_lock,
        runtime_lock_sha256=hashlib.sha256(runtime_lock.read_bytes()).hexdigest(),
        wheelhouse=wheelhouse,
        wheelhouse_manifest=wheelhouse_manifest,
        wheelhouse_manifest_sha256=hashlib.sha256(wheelhouse_manifest.read_bytes()).hexdigest(),
        releases_root=releases_root,
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        friday_home=friday_home,
        state_dir=state_dir,
        base_python=base_python,
        base_python_sha256=hashlib.sha256(base_python.read_bytes()).hexdigest(),
        alias_tool=alias_tool,
        alias_tool_sha256=hashlib.sha256(alias_tool.read_bytes()).hexdigest(),
        alias_dependency=alias_dependency,
        alias_dependency_sha256=hashlib.sha256(alias_dependency.read_bytes()).hexdigest(),
        secondary_product_runner=product_runner,
        secondary_product_runner_sha256=hashlib.sha256(product_runner.read_bytes()).hexdigest(),
        release_retention_toolchain_manifest_sha256=(_release_retention_toolchain_manifest_sha256()),
        build_receipt_profile=operator.BUILD_RECEIPT_PROFILE_HISTORICAL_V1_READER,
        max_schema=34,
    )
    with pytest.raises(operator.ReleaseFailure, match="^synthetic_post_seal_failure$"):
        operator.build_release(spec)
    assert len(sealed_markers) == 1
    assert not sealed_markers[0].exists()
    assert external_marker.read_text(encoding="ascii") == "outside staging"
    assert list(releases_root.iterdir()) == []


def _crashed_journal(
    releases: Releases,
    *,
    phase: str,
    backup: operator.DatabaseBackup | None,
    database_mutation_possible: bool,
    network_writer_uncertain: bool,
    writer_target: str = "",
) -> MemoryJournal:
    journal = MemoryJournal()
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    journal.record(
        phase,
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=network_writer_uncertain,
        writer_target=writer_target,
    )
    return journal


@pytest.mark.parametrize(
    "phase",
    [
        "migration_attempted",
        "alias_repair_attempted",
        "candidate_anchor_attempted",
        "rollback_restore_attempted",
        "recovery_restore_attempted",
        "recovery_anchor_attempted",
    ],
)
def test_crash_before_any_network_writer_replays_exact_restore_then_previous(
    releases: Releases,
    phase: str,
) -> None:
    port = FakePort()
    journal = _crashed_journal(
        releases,
        phase=phase,
        backup=port.backup,
        database_mutation_possible=True,
        network_writer_uncertain=False,
    )
    receipt = operator.recover_interrupted_activation(port, journal)
    assert receipt["status"] == "recovered"
    assert receipt["backup_restored"] is True
    assert "restore_exact_db_wal_inbox" in port.events
    assert "anchor:clean-schema33" in port.events
    assert port.active is releases.previous
    assert journal.state["phase"] == "recovered"


@pytest.mark.parametrize("phase", ["backend_start_attempted", "bridge_start_attempted"])
def test_crash_after_candidate_writer_attempt_never_restores_and_uses_schema_fallback(
    releases: Releases,
    phase: str,
) -> None:
    port = FakePort()
    journal = _crashed_journal(
        releases,
        phase=phase,
        backup=port.backup,
        database_mutation_possible=True,
        network_writer_uncertain=True,
        writer_target="candidate",
    )
    receipt = operator.recover_interrupted_activation(port, journal)
    assert receipt["backup_restored"] is False
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "anchor:schema34-fallback" in port.events
    assert port.active is releases.fallback


def test_crash_after_clean_previous_writer_start_keeps_previous_without_loss(
    releases: Releases,
) -> None:
    port = FakePort()
    journal = _crashed_journal(
        releases,
        phase="rollback_backend_start_attempted",
        backup=port.backup,
        database_mutation_possible=True,
        network_writer_uncertain=True,
        writer_target="previous",
    )
    receipt = operator.recover_interrupted_activation(port, journal)
    assert receipt["backup_restored"] is False
    assert "restore_exact_db_wal_inbox" not in port.events
    assert "anchor:clean-schema33" in port.events
    assert port.active is releases.previous


@pytest.mark.parametrize(
    ("role", "code"),
    [
        ("candidate", "recovery_candidate_venv_relocation_contract_missing"),
        ("fallback", "recovery_fallback_venv_relocation_contract_missing"),
    ],
)
def test_activation_recovery_rejects_legacy_schema_capable_roles_before_side_effects(
    releases: Releases,
    role: str,
    code: str,
) -> None:
    damaged = Releases(
        candidate=(
            replace(releases.candidate, venv_relocation_contract="")
            if role == "candidate"
            else releases.candidate
        ),
        previous=releases.previous,
        fallback=(
            replace(releases.fallback, venv_relocation_contract="")
            if role == "fallback"
            else releases.fallback
        ),
    )
    port = FakePort()
    journal = _crashed_journal(
        damaged,
        phase="migration_attempted",
        backup=port.backup,
        database_mutation_possible=True,
        network_writer_uncertain=False,
    )
    with pytest.raises(operator.ReleaseFailure, match=f"^{code}$"):
        operator.recover_interrupted_activation(port, journal)
    assert port.events == []


def test_durable_journal_survives_new_controller_and_rejects_corruption(
    tmp_path: Path,
    releases: Releases,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    state.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    path = state / "immutable-release-activation.v1.json"
    first = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256="9" * 64,
    )
    first.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    first.record("bridge_stop_attempted")
    second = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256="9" * 64,
    )
    assert second.load()["phase"] == "bridge_stop_attempted"
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            path,
            backup_root=backups,
            config_identity_sha256="8" * 64,
        ).load()
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["phase"] = "backend_start_attempted"
    path.chmod(0o600)
    path.write_text(json.dumps(payload), encoding="ascii")
    path.chmod(0o600)
    with pytest.raises(operator.ReleaseFailure, match="journal_digest_mismatch"):
        second.load()


def test_activation_journal_config_identity_transition_is_terminal_and_exact(
    tmp_path: Path,
    releases: Releases,
) -> None:
    port = _systemd_test_port(tmp_path)
    legacy_env_sha256 = "1" * 64
    phase_a_config = replace(
        port.config,
        env_file_sha256="2" * 64,
        memory_vault_mode="full_owner",
        alias_claim_manifests=(tmp_path / "phase-a-alias-claim.json",),
        alias_expected_counts=(3,),
        alias_expected_plan_sha256s=("e" * 64,),
    )
    phase_b_config = replace(
        port.config,
        env_file_sha256="3" * 64,
        memory_vault_mode="disabled",
    )
    legacy_identity = operator._activation_legacy_config_identity(  # noqa: SLF001
        phase_a_config,
        legacy_env_sha256,
    )
    assert legacy_identity != operator._systemd_config_identity_v1(phase_a_config)  # noqa: SLF001
    assert operator._systemd_config_scope_identity(phase_a_config) == (  # noqa: SLF001
        operator._systemd_config_scope_identity(phase_b_config)  # noqa: SLF001
    )
    assert operator._systemd_config_identity(phase_a_config) != (  # noqa: SLF001
        operator._systemd_config_identity(phase_b_config)  # noqa: SLF001
    )
    assert operator._systemd_config_scope_identity(phase_b_config) != (  # noqa: SLF001
        operator._systemd_config_scope_identity(  # noqa: SLF001
            replace(phase_b_config, backup_dir=tmp_path / "other-backups")
        )
    )
    assert operator._systemd_config_scope_identity(phase_b_config) != (  # noqa: SLF001
        operator._systemd_config_scope_identity(  # noqa: SLF001
            replace(phase_b_config, health_ca_sha256="5" * 64)
        )
    )

    def make_journal(
        label: str,
        config: operator.SystemdConfig,
    ) -> operator.DurableActivationJournal:
        state = tmp_path / label
        backups = state / "backups"
        state.mkdir(mode=0o700)
        backups.mkdir(mode=0o700)
        journal = operator.DurableActivationJournal(
            state / "immutable-release-activation.v1.json",
            backup_root=backups,
            config_identity_sha256=operator._systemd_config_identity(config),  # noqa: SLF001
            legacy_config_identity_sha256=legacy_identity,
            config_scope_sha256=operator._systemd_config_scope_identity(config),  # noqa: SLF001
            config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                config
            ),
            alias_claim_count=len(config.alias_claim_manifests),
            memory_vault_mode=config.memory_vault_mode,
        )
        journal.begin(
            candidate=releases.candidate,
            previous=releases.previous,
            fallback=releases.fallback,
        )
        return journal

    def make_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "a" * 64

    legacy_terminal = make_journal("legacy-terminal", phase_a_config)

    def convert_to_legacy_terminal(core: dict[str, object]) -> None:
        make_terminal(core)
        core.pop("config_identity_schema")
        core.pop("config_scope_sha256")
        core.pop("config_retry_scope_sha256")
        core.pop("alias_claim_count")
        core.pop("memory_vault_mode")
        core.pop("obsidian_mode")
        core.pop("obsidian_root_sha256")
        core.pop("prebackup_config_transition")
        core.pop("predecessor_env_sha256")
        core.pop("next_env_file")
        core.pop("next_env_file_sha256")
        core.pop("engineer_provision_committed")
        core["config_identity_sha256"] = legacy_identity

    _rewrite_signed_journal(legacy_terminal.path, convert_to_legacy_terminal)
    successor = operator.DurableActivationJournal(
        legacy_terminal.path,
        backup_root=legacy_terminal.backup_root,
        config_identity_sha256=operator._systemd_config_identity(phase_a_config),  # noqa: SLF001
        legacy_config_identity_sha256=legacy_identity,
        config_scope_sha256=operator._systemd_config_scope_identity(phase_a_config),  # noqa: SLF001
        memory_vault_mode="full_owner",
    )
    successor.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    assert successor.load()["config_identity_schema"] == operator.RUNTIME_CONFIG_SCHEMA_V3
    assert successor.load()["memory_vault_mode"] == "full_owner"

    legacy_to_disabled = make_journal("legacy-to-disabled", phase_a_config)
    _rewrite_signed_journal(legacy_to_disabled.path, convert_to_legacy_terminal)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            legacy_to_disabled.path,
            backup_root=legacy_to_disabled.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_b_config),  # noqa: SLF001
            legacy_config_identity_sha256=legacy_identity,
            config_scope_sha256=operator._systemd_config_scope_identity(phase_b_config),  # noqa: SLF001
            memory_vault_mode="disabled",
        ).begin(
            candidate=releases.fallback,
            previous=releases.candidate,
            fallback=releases.candidate,
        )

    legacy_unfinished = make_journal("legacy-unfinished", phase_a_config)

    def convert_to_legacy_unfinished(core: dict[str, object]) -> None:
        core.pop("config_identity_schema")
        core.pop("config_scope_sha256")
        core.pop("config_retry_scope_sha256")
        core.pop("alias_claim_count")
        core.pop("memory_vault_mode")
        core.pop("obsidian_mode")
        core.pop("obsidian_root_sha256")
        core.pop("prebackup_config_transition")
        core.pop("predecessor_env_sha256")
        core.pop("next_env_file")
        core.pop("next_env_file_sha256")
        core.pop("engineer_provision_committed")
        core["config_identity_sha256"] = legacy_identity

    _rewrite_signed_journal(legacy_unfinished.path, convert_to_legacy_unfinished)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            legacy_unfinished.path,
            backup_root=legacy_unfinished.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_a_config),  # noqa: SLF001
            legacy_config_identity_sha256=legacy_identity,
            config_scope_sha256=operator._systemd_config_scope_identity(phase_a_config),  # noqa: SLF001
            memory_vault_mode="full_owner",
        ).begin(
            candidate=releases.candidate,
            previous=releases.previous,
            fallback=releases.fallback,
        )

    forged_terminal = make_journal("legacy-forged", phase_a_config)

    def convert_to_forged_terminal(core: dict[str, object]) -> None:
        make_terminal(core)
        core.pop("config_identity_schema")
        core.pop("config_scope_sha256")
        core.pop("config_retry_scope_sha256")
        core.pop("alias_claim_count")
        core.pop("memory_vault_mode")
        core.pop("obsidian_mode")
        core.pop("obsidian_root_sha256")
        core.pop("prebackup_config_transition")
        core.pop("predecessor_env_sha256")
        core.pop("next_env_file")
        core.pop("next_env_file_sha256")
        core.pop("engineer_provision_committed")
        core["config_identity_sha256"] = operator._activation_legacy_config_identity(  # noqa: SLF001
            phase_a_config,
            "4" * 64,
        )

    _rewrite_signed_journal(forged_terminal.path, convert_to_forged_terminal)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            forged_terminal.path,
            backup_root=forged_terminal.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_a_config),  # noqa: SLF001
            legacy_config_identity_sha256=legacy_identity,
            config_scope_sha256=operator._systemd_config_scope_identity(phase_a_config),  # noqa: SLF001
            memory_vault_mode="full_owner",
        ).begin(
            candidate=releases.candidate,
            previous=releases.previous,
            fallback=releases.fallback,
        )

    phase_a_terminal = make_journal("phase-a-terminal", phase_a_config)
    _rewrite_signed_journal(phase_a_terminal.path, make_terminal)
    phase_b = operator.DurableActivationJournal(
        phase_a_terminal.path,
        backup_root=phase_a_terminal.backup_root,
        config_identity_sha256=operator._systemd_config_identity(phase_b_config),  # noqa: SLF001
        legacy_config_identity_sha256=legacy_identity,
        config_scope_sha256=operator._systemd_config_scope_identity(phase_b_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            phase_b_config
        ),
        alias_claim_count=0,
        memory_vault_mode="disabled",
    )
    phase_b.begin(
        candidate=releases.fallback,
        previous=releases.candidate,
        fallback=releases.candidate,
    )
    assert phase_b.load()["config_identity_sha256"] == operator._systemd_config_identity(  # noqa: SLF001
        phase_b_config
    )
    assert phase_b.load()["memory_vault_mode"] == "disabled"

    phase_a_retry_config = replace(
        phase_a_config,
        alias_claim_manifests=(),
        alias_expected_counts=(),
        alias_expected_plan_sha256s=(),
    )
    assert operator._systemd_config_scope_identity(phase_a_retry_config) == (  # noqa: SLF001
        operator._systemd_config_scope_identity(phase_a_config)  # noqa: SLF001
    )
    assert operator._systemd_config_retry_scope_identity(phase_a_retry_config) == (  # noqa: SLF001
        operator._systemd_config_retry_scope_identity(phase_a_config)  # noqa: SLF001
    )
    assert operator._systemd_config_identity(phase_a_retry_config) != (  # noqa: SLF001
        operator._systemd_config_identity(phase_a_config)  # noqa: SLF001
    )

    def make_fallback_rollback(core: dict[str, object]) -> None:
        core["phase"] = "rolled_back"
        core["terminal_receipt_sha256"] = "b" * 64
        core["database_mutation_possible"] = True
        core["network_writer_uncertain"] = True
        core["writer_target"] = "fallback"

    def make_fallback_recovery(core: dict[str, object]) -> None:
        make_fallback_rollback(core)
        core["phase"] = "recovered"

    retry_after_fallback = make_journal("phase-a-retry-after-fallback", phase_a_config)
    _rewrite_signed_journal(retry_after_fallback.path, make_fallback_rollback)
    retry = operator.DurableActivationJournal(
        retry_after_fallback.path,
        backup_root=retry_after_fallback.backup_root,
        config_identity_sha256=operator._systemd_config_identity(phase_a_retry_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(phase_a_retry_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            phase_a_retry_config
        ),
        alias_claim_count=0,
        memory_vault_mode="full_owner",
    )
    retry.begin(
        candidate=releases.candidate,
        previous=releases.fallback,
        fallback=releases.fallback,
    )
    assert retry.load()["phase"] == "prepared"
    assert retry.load()["alias_claim_count"] == 0

    retry_after_recovery = make_journal("phase-a-retry-after-recovery", phase_a_config)
    _rewrite_signed_journal(retry_after_recovery.path, make_fallback_recovery)
    recovered_retry = operator.DurableActivationJournal(
        retry_after_recovery.path,
        backup_root=retry_after_recovery.backup_root,
        config_identity_sha256=operator._systemd_config_identity(phase_a_retry_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(phase_a_retry_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            phase_a_retry_config
        ),
        alias_claim_count=0,
        memory_vault_mode="full_owner",
    )
    recovered_retry.begin(
        candidate=releases.candidate,
        previous=releases.fallback,
        fallback=releases.fallback,
    )
    assert recovered_retry.load()["phase"] == "prepared"
    assert recovered_retry.load()["alias_claim_count"] == 0

    for label, config, previous, fallback, mutation in (
        (
            "changed-env",
            replace(phase_a_retry_config, env_file_sha256="f" * 64),
            releases.fallback,
            releases.fallback,
            None,
        ),
        (
            "changed-scope",
            replace(
                phase_a_retry_config,
                database=tmp_path / "phase-a-recovered-wrong-database.sqlite3",
            ),
            releases.fallback,
            releases.fallback,
            None,
        ),
        (
            "wrong-lineage",
            phase_a_retry_config,
            releases.previous,
            releases.previous,
            None,
        ),
        (
            "wrong-writer-evidence",
            phase_a_retry_config,
            releases.fallback,
            releases.fallback,
            ("writer_target", "previous"),
        ),
    ):
        invalid_recovery = make_journal(f"phase-a-recovered-{label}", phase_a_config)

        def make_invalid_recovery(
            core: dict[str, object],
            *,
            change: tuple[str, object] | None = mutation,
        ) -> None:
            make_fallback_recovery(core)
            if change is not None:
                core[change[0]] = change[1]

        _rewrite_signed_journal(invalid_recovery.path, make_invalid_recovery)
        with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
            operator.DurableActivationJournal(
                invalid_recovery.path,
                backup_root=invalid_recovery.backup_root,
                config_identity_sha256=operator._systemd_config_identity(config),  # noqa: SLF001
                config_scope_sha256=operator._systemd_config_scope_identity(config),  # noqa: SLF001
                config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                    config
                ),
                alias_claim_count=0,
                memory_vault_mode="full_owner",
            ).begin(
                candidate=releases.candidate,
                previous=previous,
                fallback=fallback,
            )

    changed_retry_env = replace(phase_a_retry_config, env_file_sha256="f" * 64)
    wrong_retry_scope = make_journal("phase-a-retry-wrong-env", phase_a_config)
    _rewrite_signed_journal(wrong_retry_scope.path, make_fallback_rollback)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            wrong_retry_scope.path,
            backup_root=wrong_retry_scope.backup_root,
            config_identity_sha256=operator._systemd_config_identity(changed_retry_env),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(changed_retry_env),  # noqa: SLF001
            config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                changed_retry_env
            ),
            alias_claim_count=0,
            memory_vault_mode="full_owner",
        ).begin(
            candidate=releases.candidate,
            previous=releases.fallback,
            fallback=releases.fallback,
        )

    changed_retry_scope = replace(
        phase_a_retry_config,
        database=tmp_path / "phase-a-retry-wrong-database.sqlite3",
    )
    wrong_retry_persistent_scope = make_journal("phase-a-retry-wrong-scope", phase_a_config)
    _rewrite_signed_journal(wrong_retry_persistent_scope.path, make_fallback_rollback)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            wrong_retry_persistent_scope.path,
            backup_root=wrong_retry_persistent_scope.backup_root,
            config_identity_sha256=operator._systemd_config_identity(changed_retry_scope),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(changed_retry_scope),  # noqa: SLF001
            config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                changed_retry_scope
            ),
            alias_claim_count=0,
            memory_vault_mode="full_owner",
        ).begin(
            candidate=releases.candidate,
            previous=releases.fallback,
            fallback=releases.fallback,
        )

    wrong_retry_lineage = make_journal("phase-a-retry-wrong-lineage", phase_a_config)
    _rewrite_signed_journal(wrong_retry_lineage.path, make_fallback_rollback)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            wrong_retry_lineage.path,
            backup_root=wrong_retry_lineage.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_a_retry_config),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(phase_a_retry_config),  # noqa: SLF001
            config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                phase_a_retry_config
            ),
            alias_claim_count=0,
            memory_vault_mode="full_owner",
        ).begin(
            candidate=releases.candidate,
            previous=releases.previous,
            fallback=releases.previous,
        )

    wrong_retry_phase = make_journal("phase-a-retry-wrong-phase", phase_a_config)
    _rewrite_signed_journal(wrong_retry_phase.path, make_terminal)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            wrong_retry_phase.path,
            backup_root=wrong_retry_phase.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_a_retry_config),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(phase_a_retry_config),  # noqa: SLF001
            config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                phase_a_retry_config
            ),
            alias_claim_count=0,
            memory_vault_mode="full_owner",
        ).begin(
            candidate=releases.candidate,
            previous=releases.fallback,
            fallback=releases.fallback,
        )

    for label, field, value in (
        ("no-db-mutation", "database_mutation_possible", False),
        ("no-network-uncertainty", "network_writer_uncertain", False),
        ("wrong-writer", "writer_target", "previous"),
    ):
        unproven_retry = make_journal(f"phase-a-retry-{label}", phase_a_config)

        def remove_evidence(
            core: dict[str, object], *, key: str = field, replacement: object = value
        ) -> None:
            make_fallback_rollback(core)
            core[key] = replacement

        _rewrite_signed_journal(unproven_retry.path, remove_evidence)
        with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
            operator.DurableActivationJournal(
                unproven_retry.path,
                backup_root=unproven_retry.backup_root,
                config_identity_sha256=operator._systemd_config_identity(phase_a_retry_config),  # noqa: SLF001
                config_scope_sha256=operator._systemd_config_scope_identity(phase_a_retry_config),  # noqa: SLF001
                config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                    phase_a_retry_config
                ),
                alias_claim_count=0,
                memory_vault_mode="full_owner",
            ).begin(
                candidate=releases.candidate,
                previous=releases.fallback,
                fallback=releases.fallback,
            )

    phase_a_unfinished = make_journal("phase-a-unfinished", phase_a_config)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            phase_a_unfinished.path,
            backup_root=phase_a_unfinished.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_b_config),  # noqa: SLF001
            legacy_config_identity_sha256=legacy_identity,
            config_scope_sha256=operator._systemd_config_scope_identity(phase_b_config),  # noqa: SLF001
            memory_vault_mode="disabled",
        ).begin(
            candidate=releases.fallback,
            previous=releases.candidate,
            fallback=releases.candidate,
        )

    wrong_lineage = make_journal("phase-a-wrong-lineage", phase_a_config)
    _rewrite_signed_journal(wrong_lineage.path, make_terminal)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            wrong_lineage.path,
            backup_root=wrong_lineage.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_b_config),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(phase_b_config),  # noqa: SLF001
            memory_vault_mode="disabled",
        ).begin(
            candidate=releases.fallback,
            previous=releases.previous,
            fallback=releases.previous,
        )

    rolled_back = make_journal("phase-a-rolled-back", phase_a_config)

    def make_rolled_back(core: dict[str, object]) -> None:
        core["phase"] = "rolled_back"
        core["terminal_receipt_sha256"] = "b" * 64

    _rewrite_signed_journal(rolled_back.path, make_rolled_back)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            rolled_back.path,
            backup_root=rolled_back.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_b_config),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(phase_b_config),  # noqa: SLF001
            memory_vault_mode="disabled",
        ).begin(
            candidate=releases.fallback,
            previous=releases.candidate,
            fallback=releases.candidate,
        )

    unrelated_config = replace(
        phase_b_config,
        database=tmp_path / "unrelated.sqlite3",
    )
    wrong_scope = make_journal("phase-a-wrong-scope", phase_a_config)
    _rewrite_signed_journal(wrong_scope.path, make_terminal)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            wrong_scope.path,
            backup_root=wrong_scope.backup_root,
            config_identity_sha256=operator._systemd_config_identity(unrelated_config),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(unrelated_config),  # noqa: SLF001
            memory_vault_mode="disabled",
        ).begin(
            candidate=releases.fallback,
            previous=releases.candidate,
            fallback=releases.candidate,
        )

    with pytest.raises(operator.ReleaseFailure, match="terminal_journal_env_digest_invalid"):
        operator._activation_legacy_config_identity(phase_a_config, "not-a-hash")  # noqa: SLF001

    forged_scope = make_journal("phase-b-forged-scope", phase_b_config)

    def replace_scope_without_identity(core: dict[str, object]) -> None:
        core["config_scope_sha256"] = "9" * 64

    _rewrite_signed_journal(forged_scope.path, replace_scope_without_identity)
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        operator.DurableActivationJournal(
            forged_scope.path,
            backup_root=forged_scope.backup_root,
            config_identity_sha256=operator._systemd_config_identity(phase_b_config),  # noqa: SLF001
            config_scope_sha256=operator._systemd_config_scope_identity(phase_b_config),  # noqa: SLF001
            memory_vault_mode="disabled",
        ).load()


def test_v2_journal_upgrade_then_exact_disabled_to_enabled_obsidian_transition(
    tmp_path: Path,
    releases: Releases,
) -> None:
    port = _systemd_test_port(tmp_path)
    disabled = port.config
    state = tmp_path / "obsidian-transition"
    backups = state / "backups"
    state.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    path = state / "immutable-release-activation.v1.json"
    old_candidate = releases.previous
    legacy = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256=operator._systemd_config_identity(disabled),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(disabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(disabled),  # noqa: SLF001
        obsidian_mode="disabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(disabled),  # noqa: SLF001
    )
    legacy.begin(
        candidate=old_candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )

    def make_v2_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "a" * 64
        core["config_identity_schema"] = operator.RUNTIME_CONFIG_SCHEMA_V2
        core["config_identity_sha256"] = operator._systemd_config_identity_v2(disabled)  # noqa: SLF001
        core.pop("obsidian_mode")
        core.pop("obsidian_root_sha256")
        core.pop("prebackup_config_transition")
        core.pop("predecessor_env_sha256")
        core.pop("next_env_file")
        core.pop("next_env_file_sha256")
        core.pop("engineer_provision_committed")

    _rewrite_signed_journal(path, make_v2_terminal)
    bootstrap = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256=operator._systemd_config_identity(disabled),  # noqa: SLF001
        legacy_v2_config_identity_sha256=operator._systemd_config_identity_v2(disabled),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(disabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(disabled),  # noqa: SLF001
        obsidian_mode="disabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(disabled),  # noqa: SLF001
    )
    bootstrap.begin(
        candidate=releases.candidate,
        previous=old_candidate,
        fallback=releases.fallback,
    )
    assert bootstrap.load()["config_identity_schema"] == operator.RUNTIME_CONFIG_SCHEMA_V3

    def make_v3_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "b" * 64

    _rewrite_signed_journal(path, make_v3_terminal)
    disabled_terminal = path.read_bytes()
    enabled = replace(disabled, env_file_sha256="e" * 64, obsidian_mode="enabled")
    next_env_file = state / "next.env"
    final_candidate = replace(
        releases.fallback,
        root=tmp_path / "obsidian-final",
        commit="d" * 40,
        tree_manifest_sha256="6" * 64,
    )

    forged = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256=operator._systemd_config_identity(enabled),  # noqa: SLF001
        transition_config_identity_sha256=operator._activation_obsidian_predecessor_identity(  # noqa: SLF001
            enabled,
            "f" * 64,
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(enabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(enabled),  # noqa: SLF001
        obsidian_mode="enabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(enabled),  # noqa: SLF001
        predecessor_env_sha256="f" * 64,
        next_env_file=next_env_file,
        next_env_file_sha256=enabled.env_file_sha256,
    )
    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        forged.begin(
            candidate=final_candidate,
            previous=releases.candidate,
            fallback=releases.candidate,
        )
    path.chmod(0o600)
    path.write_bytes(disabled_terminal)
    path.chmod(0o600)

    transition = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256=operator._systemd_config_identity(enabled),  # noqa: SLF001
        transition_config_identity_sha256=operator._activation_obsidian_predecessor_identity(  # noqa: SLF001
            enabled,
            disabled.env_file_sha256,
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(enabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(enabled),  # noqa: SLF001
        obsidian_mode="enabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(enabled),  # noqa: SLF001
        predecessor_env_sha256=disabled.env_file_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=enabled.env_file_sha256,
    )
    transition.begin(
        candidate=final_candidate,
        previous=releases.candidate,
        fallback=releases.candidate,
    )
    prepared = transition.load()
    assert prepared["obsidian_mode"] == "enabled"
    assert prepared["prebackup_config_transition"] == "obsidian_enable"
    assert prepared["predecessor_env_sha256"] == disabled.env_file_sha256
    assert prepared["next_env_file"] == str(next_env_file)
    assert prepared["next_env_file_sha256"] == enabled.env_file_sha256

    def make_prebackup_rollback(core: dict[str, object]) -> None:
        core["phase"] = "rolled_back"
        core["terminal_receipt_sha256"] = "c" * 64
        core["backup"] = None
        core["database_mutation_possible"] = False
        core["network_writer_uncertain"] = True
        core["writer_target"] = "previous"

    _rewrite_signed_journal(path, make_prebackup_rollback)
    retry = operator.DurableActivationJournal(
        path,
        backup_root=backups,
        config_identity_sha256=operator._systemd_config_identity(enabled),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(enabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(enabled),  # noqa: SLF001
        obsidian_mode="enabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(enabled),  # noqa: SLF001
        predecessor_env_sha256=disabled.env_file_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=enabled.env_file_sha256,
    )
    retry.begin(
        candidate=final_candidate,
        previous=releases.candidate,
        fallback=releases.candidate,
    )
    retried = retry.load()
    assert retried["prebackup_config_transition"] == "obsidian_enable"
    assert retried["predecessor_env_sha256"] == disabled.env_file_sha256
    assert retried["next_env_file"] == str(next_env_file)
    assert retried["next_env_file_sha256"] == enabled.env_file_sha256


def test_prebackup_recovery_reopens_staged_journal_with_canonical_env_unchanged(
    tmp_path: Path,
    releases: Releases,
) -> None:
    base = _systemd_test_port(tmp_path)
    disabled = base.config
    predecessor = disabled.env_file.read_bytes()
    predecessor_sha256 = hashlib.sha256(predecessor).hexdigest()
    path = disabled.state_dir / "immutable-release-activation.v1.json"
    prior = operator.DurableActivationJournal(
        path,
        backup_root=disabled.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(disabled),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(disabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(disabled),  # noqa: SLF001
        obsidian_mode="disabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(disabled),  # noqa: SLF001
    )
    prior.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )

    def make_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "a" * 64

    _rewrite_signed_journal(path, make_terminal)
    enabled_bytes = predecessor + b"FRIDAY_OBSIDIAN_ENABLED=1\n"
    enabled_sha256 = hashlib.sha256(enabled_bytes).hexdigest()
    next_env_file = disabled.state_dir / "next.env"
    next_env_file.write_bytes(enabled_bytes)
    next_env_file.chmod(0o600)
    operator._obsidian_root(disabled).mkdir(mode=0o700)  # noqa: SLF001
    target = replace(
        disabled,
        env_file_sha256=enabled_sha256,
        obsidian_mode="enabled",
    )
    transition = operator.DurableActivationJournal(
        path,
        backup_root=target.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=operator._activation_obsidian_predecessor_identity(  # noqa: SLF001
            target,
            predecessor_sha256,
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode="enabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=enabled_sha256,
    )
    final_candidate = replace(
        releases.fallback,
        root=tmp_path / "recovery-final",
        commit="d" * 40,
        tree_manifest_sha256="6" * 64,
    )
    transition.begin(
        candidate=final_candidate,
        previous=releases.candidate,
        fallback=releases.candidate,
    )
    transition.record("bridge_stop_attempted")
    persisted = transition.load()
    assert disabled.env_file.read_bytes() == predecessor
    recovery_args = replace(disabled, obsidian_mode="enabled")
    effective = operator._activation_recovery_systemd_config(recovery_args, persisted)  # noqa: SLF001
    assert effective.obsidian_mode == "enabled"
    assert effective.env_file_sha256 == predecessor_sha256
    assert effective.next_env_file == next_env_file
    assert effective.next_env_file_sha256 == enabled_sha256
    recovery_port = operator.SystemdActivationPort(effective)
    recovery_target = operator._activation_target_config(effective)  # noqa: SLF001
    reopened = operator.DurableActivationJournal(
        path,
        backup_root=effective.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(recovery_target),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(recovery_target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            recovery_target
        ),
        obsidian_mode="enabled",
        obsidian_root_sha256=operator._obsidian_root_sha256(effective),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=enabled_sha256,
    )
    assert reopened.load()["phase"] == "bridge_stop_attempted"
    recovery_port.select_predecessor_config_transition(
        "obsidian_enable", predecessor_sha256, next_env_file, enabled_sha256
    )
    assert recovery_port.config.obsidian_mode == "disabled"
    assert recovery_port.config.env_file.read_bytes() == predecessor


def test_secondary_shadow_terminal_transition_and_prebackup_recovery_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
) -> None:
    base = _systemd_test_port(tmp_path)
    disabled = base.config
    predecessor_sha256 = disabled.env_file_sha256
    journal_path = disabled.state_dir / "immutable-release-activation.v1.json"
    prior = operator.DurableActivationJournal(
        journal_path,
        backup_root=disabled.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(disabled),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(disabled),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            disabled
        ),
        obsidian_mode=disabled.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(disabled),  # noqa: SLF001
    )
    prior.begin(
        candidate=releases.previous,
        previous=releases.fallback,
        fallback=releases.candidate,
    )

    def make_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "b" * 64

    _rewrite_signed_journal(journal_path, make_terminal)
    staged, _target, target_sha256 = _secondary_shadow_stage(base, monkeypatch)
    staged_config = replace(
        disabled,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_enable",
    )
    target = operator._activation_target_config(staged_config)  # noqa: SLF001
    transition = operator.DurableActivationJournal(
        journal_path,
        backup_root=disabled.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=(
            operator._activation_transition_predecessor_identity(  # noqa: SLF001
                target,
                predecessor_sha256,
                "secondary_shadow_enable",
            )
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_enable",
    )
    transition.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.previous,
    )
    transition.record("bridge_stop_attempted")
    persisted = transition.load()

    effective = operator._activation_recovery_systemd_config(  # noqa: SLF001
        disabled,
        persisted,
    )
    assert effective.staged_config_transition == "secondary_shadow_enable"
    assert effective.obsidian_mode == disabled.obsidian_mode
    assert effective.next_env_file == staged
    recovery_port = operator.SystemdActivationPort(effective)
    reopened = operator.DurableActivationJournal(
        journal_path,
        backup_root=effective.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_enable",
    )
    assert reopened.load()["phase"] == "bridge_stop_attempted"
    recovery_port.select_predecessor_config_transition(
        "secondary_shadow_enable",
        predecessor_sha256,
        staged,
        target_sha256,
    )
    assert recovery_port.config.env_file.read_bytes() == disabled.env_file.read_bytes()
    assert recovery_port.config.obsidian_mode == disabled.obsidian_mode


def test_semantic_legacy_absence_terminal_handover_rolls_back_and_recovers_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
) -> None:
    base, predecessor = _secondary_document_map_assist_enabled_port(tmp_path, monkeypatch)
    current_config = base.config
    predecessor_sha256 = current_config.env_file_sha256
    journal_path = current_config.state_dir / "immutable-release-activation.v1.json"
    current = releases.fallback
    prior = operator.DurableActivationJournal(
        journal_path,
        backup_root=current_config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(current_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(current_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            current_config
        ),
        obsidian_mode=current_config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(current_config),  # noqa: SLF001
    )
    prior.begin(
        candidate=current,
        previous=releases.previous,
        fallback=releases.candidate,
    )

    def make_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "b" * 64

    _rewrite_signed_journal(journal_path, make_terminal)
    staged, _shadow, target_sha256 = _semantic_supervisor_stage(base, mode="shadow")
    staged_config = replace(
        current_config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="semantic_supervisor_shadow_enable",
    )
    target = operator._activation_target_config(staged_config)  # noqa: SLF001

    def new_journal() -> operator.DurableActivationJournal:
        return operator.DurableActivationJournal(
            journal_path,
            backup_root=target.backup_dir,
            config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
            transition_config_identity_sha256=(
                operator._activation_transition_predecessor_identity(  # noqa: SLF001
                    target,
                    predecessor_sha256,
                    "semantic_supervisor_shadow_enable",
                )
            ),
            config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
            config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
                target
            ),
            obsidian_mode=target.obsidian_mode,
            obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
            predecessor_env_sha256=predecessor_sha256,
            next_env_file=staged,
            next_env_file_sha256=target_sha256,
            staged_config_transition="semantic_supervisor_shadow_enable",
        )

    transition = new_journal()
    failed_port = FakePort(
        fail="backup_db_wal_inbox",
        staged_config_transition="semantic_supervisor_shadow_enable",
    )
    failed_port.canonical_env_sha256 = predecessor_sha256
    with pytest.raises(operator.ReleaseFailure, match="activation_failed_rolled_back"):
        operator.activate_release(
            failed_port,
            transition,
            candidate=releases.candidate,
            previous=current,
            schema_capable_fallback=current,
        )
    terminal = transition.load()
    assert terminal["phase"] == "rolled_back"
    assert terminal["backup"] is None
    assert failed_port.canonical_env_sha256 == predecessor_sha256
    assert current_config.env_file.read_bytes() == predecessor

    retry_candidate = replace(
        releases.candidate,
        root=tmp_path / "semantic-retry-candidate",
        commit="d" * 40,
        tree_manifest_sha256="e" * 64,
    )
    retry = new_journal()
    retry.begin(candidate=retry_candidate, previous=current, fallback=current)
    retry.record("bridge_stop_attempted")
    release_by_root = {release.root: release for release in (retry_candidate, current)}
    monkeypatch.setattr(
        operator,
        "load_release_identity",
        lambda root, *, expected_tree_sha256: release_by_root[Path(root)],
    )
    recovery_port = FakePort(staged_config_transition="semantic_supervisor_shadow_enable")
    recovery_port.canonical_env_sha256 = predecessor_sha256
    recovered = operator.recover_interrupted_activation(recovery_port, retry)
    assert recovered["status"] == "recovered"
    assert recovery_port.canonical_env_sha256 == predecessor_sha256
    assert retry.load()["phase"] == "recovered"
    replay = operator.recover_interrupted_activation(recovery_port, retry)
    assert replay["status"] == "already_terminal"
    assert replay["terminal_phase"] == "recovered"


@pytest.mark.parametrize("private_shadow", [False, True])
def test_secondary_shadow_disable_terminal_transition_recovery_and_replay_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
    private_shadow: bool,
) -> None:
    enabled_port = (
        _secondary_private_shadow_enabled_port if private_shadow else _secondary_shadow_enabled_port
    )
    base, enabled = enabled_port(tmp_path, monkeypatch)
    enabled_config = base.config
    predecessor_sha256 = hashlib.sha256(enabled).hexdigest()
    journal_path = enabled_config.state_dir / "immutable-release-activation.v1.json"
    prior = operator.DurableActivationJournal(
        journal_path,
        backup_root=enabled_config.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(enabled_config),  # noqa: SLF001
        config_scope_sha256=operator._systemd_config_scope_identity(enabled_config),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(  # noqa: SLF001
            enabled_config
        ),
        obsidian_mode=enabled_config.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(enabled_config),  # noqa: SLF001
    )
    prior.begin(
        candidate=releases.fallback,
        previous=releases.previous,
        fallback=releases.candidate,
    )

    def make_terminal(core: dict[str, object]) -> None:
        core["phase"] = "clear"
        core["terminal_receipt_sha256"] = "d" * 64

    _rewrite_signed_journal(journal_path, make_terminal)
    staged, _disabled, target_sha256 = _secondary_shadow_disable_stage(base)
    staged_config = replace(
        enabled_config,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_disable",
    )
    target = operator._activation_target_config(staged_config)  # noqa: SLF001
    transition = operator.DurableActivationJournal(
        journal_path,
        backup_root=target.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=(
            operator._activation_transition_predecessor_identity(  # noqa: SLF001
                target,
                predecessor_sha256,
                "secondary_shadow_disable",
            )
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=predecessor_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition="secondary_shadow_disable",
    )
    transition.begin(
        candidate=releases.candidate,
        previous=releases.fallback,
        fallback=releases.fallback,
    )
    transition.record("bridge_stop_attempted")
    persisted = transition.load()
    assert persisted["prebackup_config_transition"] == "secondary_shadow_disable"
    assert ("a" * 64).encode() not in journal_path.read_bytes()

    effective = operator._activation_recovery_systemd_config(  # noqa: SLF001
        enabled_config,
        persisted,
    )
    assert effective.env_file_sha256 == predecessor_sha256
    assert effective.next_env_file == staged
    assert effective.staged_config_transition == "secondary_shadow_disable"
    recovery_port = operator.SystemdActivationPort(effective)
    recovery_port.select_predecessor_config_transition(
        "secondary_shadow_disable",
        predecessor_sha256,
        staged,
        target_sha256,
    )
    assert recovery_port.config.env_file.read_bytes() == enabled

    release_by_root = {
        release.root: release for release in (releases.candidate, releases.previous, releases.fallback)
    }
    monkeypatch.setattr(
        operator,
        "load_release_identity",
        lambda root, *, expected_tree_sha256: release_by_root[Path(root)],
    )
    recovery_fake = FakePort(staged_config_transition="secondary_shadow_disable")
    recovered = operator.recover_interrupted_activation(recovery_fake, transition)
    assert recovered["status"] == "recovered"
    assert recovery_fake.canonical_env_sha256 == predecessor_sha256

    replay = operator.recover_interrupted_activation(recovery_fake, transition)
    assert replay["status"] == "already_terminal"
    assert replay["terminal_phase"] == "recovered"


@pytest.mark.parametrize(
    "transition",
    [
        "secondary_shadow_enable",
        "secondary_shadow_disable",
        "secondary_shadow_to_private_shadow",
        "secondary_shadow_to_assist",
        "secondary_assist_to_disabled",
        "secondary_assist_enable_document_map_shadow",
        "semantic_supervisor_shadow_enable",
        "semantic_supervisor_shadow_disable",
        "semantic_supervisor_effect_shadow_enable",
        "semantic_supervisor_effect_shadow_disable",
    ],
)
@pytest.mark.parametrize("terminal_phase", ["rolled_back", "recovered"])
def test_runtime_transition_accepts_exact_postbackup_terminal_current_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
    transition: str,
    terminal_phase: str,
) -> None:
    if transition.startswith("semantic_supervisor_"):
        current_config, staged, _target_bytes, target_sha256 = _semantic_supervisor_staged_transition_case(
            tmp_path,
            monkeypatch,
            transition,
        )
        target = operator._activation_target_config(  # noqa: SLF001
            replace(
                current_config,
                next_env_file=staged,
                next_env_file_sha256=target_sha256,
                staged_config_transition=transition,
            )
        )
    else:
        current_config, staged, target_sha256, target = _secondary_staged_transition_case(
            tmp_path,
            monkeypatch,
            transition,
        )
    prior, _backup = _durable_postbackup_terminal(
        current_config,
        candidate=releases.previous,
        current=releases.fallback,
        terminal_phase=terminal_phase,
    )
    next_journal = operator.DurableActivationJournal(
        prior.path,
        backup_root=target.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=(
            operator._activation_transition_predecessor_identity(  # noqa: SLF001
                target,
                current_config.env_file_sha256,
                transition,
            )
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=current_config.env_file_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )

    next_journal.begin(
        candidate=releases.candidate,
        previous=releases.fallback,
        fallback=releases.fallback,
    )

    prepared = next_journal.load()
    assert prepared["phase"] == "prepared"
    assert prepared["prebackup_config_transition"] == transition
    assert prepared["predecessor_env_sha256"] == current_config.env_file_sha256
    assert prepared["next_env_file_sha256"] == target_sha256


@pytest.mark.parametrize(
    "transition",
    [
        "secondary_shadow_enable",
        "secondary_shadow_disable",
        "secondary_shadow_to_private_shadow",
        "secondary_shadow_to_assist",
        "secondary_assist_to_disabled",
    ],
)
@pytest.mark.parametrize(
    "mutation",
    [
        "backup_missing",
        "database_not_mutated",
        "network_certain",
        "writer_candidate",
        "previous_not_current",
        "fallback_not_current",
    ],
)
def test_secondary_transition_rejects_inexact_postbackup_terminal_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
    transition: str,
    mutation: str,
) -> None:
    current_config, staged, target_sha256, target = _secondary_staged_transition_case(
        tmp_path,
        monkeypatch,
        transition,
    )
    prior, _backup = _durable_postbackup_terminal(
        current_config,
        candidate=releases.previous,
        current=releases.fallback,
        terminal_phase="rolled_back",
    )

    def mutate(core: dict[str, object]) -> None:
        if mutation == "backup_missing":
            core["backup"] = None
        elif mutation == "database_not_mutated":
            core["database_mutation_possible"] = False
        elif mutation == "network_certain":
            core["network_writer_uncertain"] = False
        elif mutation == "writer_candidate":
            core["writer_target"] = "candidate"
        elif mutation == "previous_not_current":
            core["previous"] = operator._journal_release(releases.previous)  # noqa: SLF001
        else:
            assert mutation == "fallback_not_current"
            core["fallback"] = operator._journal_release(releases.previous)  # noqa: SLF001

    _rewrite_signed_journal(prior.path, mutate)
    next_journal = operator.DurableActivationJournal(
        prior.path,
        backup_root=target.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=(
            operator._activation_transition_predecessor_identity(  # noqa: SLF001
                target,
                current_config.env_file_sha256,
                transition,
            )
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=current_config.env_file_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )

    with pytest.raises(operator.ReleaseFailure, match="activation_config_identity_changed"):
        next_journal.begin(
            candidate=releases.candidate,
            previous=releases.fallback,
            fallback=releases.fallback,
        )


@pytest.mark.parametrize(
    "transition",
    [
        "secondary_shadow_enable",
        "secondary_shadow_disable",
        "secondary_shadow_to_private_shadow",
        "secondary_shadow_to_assist",
        "secondary_assist_to_disabled",
    ],
)
def test_secondary_transition_revalidates_postbackup_terminal_backup_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Releases,
    transition: str,
) -> None:
    current_config, staged, target_sha256, target = _secondary_staged_transition_case(
        tmp_path,
        monkeypatch,
        transition,
    )
    prior, backup = _durable_postbackup_terminal(
        current_config,
        candidate=releases.previous,
        current=releases.fallback,
        terminal_phase="recovered",
    )
    payload = backup.opaque
    assert isinstance(payload, operator._ExactBackupPayload)  # noqa: SLF001
    backup_file = payload.directory / payload.files[0][0]
    backup_file.chmod(0o600)
    backup_file.write_bytes(backup_file.read_bytes() + b"drift")
    next_journal = operator.DurableActivationJournal(
        prior.path,
        backup_root=target.backup_dir,
        config_identity_sha256=operator._systemd_config_identity(target),  # noqa: SLF001
        transition_config_identity_sha256=(
            operator._activation_transition_predecessor_identity(  # noqa: SLF001
                target,
                current_config.env_file_sha256,
                transition,
            )
        ),
        config_scope_sha256=operator._systemd_config_scope_identity(target),  # noqa: SLF001
        config_retry_scope_sha256=operator._systemd_config_retry_scope_identity(target),  # noqa: SLF001
        obsidian_mode=target.obsidian_mode,
        obsidian_root_sha256=operator._obsidian_root_sha256(target),  # noqa: SLF001
        predecessor_env_sha256=current_config.env_file_sha256,
        next_env_file=staged,
        next_env_file_sha256=target_sha256,
        staged_config_transition=transition,
    )

    with pytest.raises(operator.ReleaseFailure, match="activation_journal_backup_changed"):
        next_journal.begin(
            candidate=releases.candidate,
            previous=releases.fallback,
            fallback=releases.fallback,
        )


def test_schema35_releases_require_exact_obsidian_cutover_capability(
    tmp_path: Path,
    releases: Releases,
) -> None:
    candidate = replace(releases.candidate, max_schema=35)
    fallback = replace(releases.fallback, max_schema=35)
    with pytest.raises(operator.ReleaseFailure, match="candidate_obsidian_cutover_contract_missing"):
        operator.activate_release(
            FakePort(backup_schema=35),
            MemoryJournal(),
            candidate=candidate,
            previous=releases.previous,
            schema_capable_fallback=fallback,
        )
    capable_candidate = replace(
        candidate,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    with pytest.raises(operator.ReleaseFailure, match="fallback_obsidian_cutover_contract_missing"):
        operator.activate_release(
            FakePort(backup_schema=35),
            MemoryJournal(),
            candidate=capable_candidate,
            previous=releases.previous,
            schema_capable_fallback=fallback,
        )


def test_enabled_candidate_runs_full_settings_validation_before_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    root = operator._obsidian_root(base.config)  # noqa: SLF001
    root.mkdir(mode=0o700)
    enabled = operator.SystemdActivationPort(
        replace(base.config, obsidian_mode="enabled", obsidian_root=root)
    )
    release = operator.ReleaseIdentity(
        tmp_path / "schema35-candidate",
        "c" * 40,
        "0.207.0",
        "d" * 64,
        35,
        operator.MEMORY_VAULT_MODE_CONTRACT,
        operator.VENV_RELOCATION_CONTRACT,
        operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    monkeypatch.setattr(operator, "verify_release_tree", lambda _release: None)
    monkeypatch.setattr(operator, "installed_surface_smoke", lambda _release: "e" * 64)
    observed_script = ""

    def reject_invalid_settings(command, **_kwargs):
        nonlocal observed_script
        observed_script = command[4]
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(operator.subprocess, "run", reject_invalid_settings)
    with pytest.raises(operator.ReleaseFailure, match="candidate_runtime_config_identity_mismatch"):
        enabled.verify_release(release)
    assert "validate_settings(settings,production=True)" in observed_script
    assert "FRIDAY_SYNCTHING_BINARY" not in observed_script


def test_album_journal_requires_exact_current_config_and_rejects_legacy(
    tmp_path: Path,
    releases: Releases,
) -> None:
    port = _systemd_test_port(tmp_path)
    disabled_identity = operator._systemd_config_identity(port.config)  # noqa: SLF001
    full_owner_identity = operator._systemd_config_identity(  # noqa: SLF001
        replace(port.config, memory_vault_mode="full_owner")
    )
    legacy_identity = operator._systemd_config_identity_v1(port.config)  # noqa: SLF001

    def make_journal(label: str, identity: str) -> operator.DurableAlbumRecoveryJournal:
        state = tmp_path / label
        backups = state / "backups"
        state.mkdir(mode=0o700)
        backups.mkdir(mode=0o700)
        journal = operator.DurableAlbumRecoveryJournal(
            state / "historical-album-recovery.v1.json",
            backup_root=backups,
            config_identity_sha256=identity,
        )
        journal.begin_or_resume(releases.candidate)
        return journal

    def make_terminal(core: dict[str, object]) -> None:
        core["phase"] = "complete"
        core["backup"] = {
            "directory": str(tmp_path / "synthetic-album-backup"),
            "receipt_sha256": "a" * 64,
        }
        core["cas_receipt_sha256"] = "b" * 64
        core["completion_receipt_sha256"] = "c" * 64

    legacy_terminal = make_journal("album-legacy-terminal", disabled_identity)

    def convert_to_legacy_terminal(core: dict[str, object]) -> None:
        make_terminal(core)
        core.pop("config_identity_schema")
        core.pop("cas_receipt_sha256")
        core.pop("completion_receipt_sha256")
        core["receipt_sha256"] = "b" * 64
        core["config_identity_sha256"] = legacy_identity

    _rewrite_signed_journal(legacy_terminal.path, convert_to_legacy_terminal)
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            legacy_terminal.path,
            backup_root=legacy_terminal.backup_root,
            config_identity_sha256=disabled_identity,
        ).begin_or_resume(releases.candidate)

    legacy_unfinished = make_journal("album-legacy-unfinished", disabled_identity)

    def convert_to_legacy_unfinished(core: dict[str, object]) -> None:
        core.pop("config_identity_schema")
        core["config_identity_sha256"] = legacy_identity

    _rewrite_signed_journal(legacy_unfinished.path, convert_to_legacy_unfinished)
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            legacy_unfinished.path,
            backup_root=legacy_unfinished.backup_root,
            config_identity_sha256=disabled_identity,
        ).begin_or_resume(releases.candidate)

    forged_terminal = make_journal("album-legacy-forged", disabled_identity)

    def convert_to_forged_terminal(core: dict[str, object]) -> None:
        make_terminal(core)
        core.pop("config_identity_schema")
        core["config_identity_sha256"] = "7" * 64

    _rewrite_signed_journal(forged_terminal.path, convert_to_forged_terminal)
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            forged_terminal.path,
            backup_root=forged_terminal.backup_root,
            config_identity_sha256=disabled_identity,
        ).begin_or_resume(releases.candidate)

    same_config_terminal = make_journal("album-same-config-terminal", disabled_identity)
    _rewrite_signed_journal(same_config_terminal.path, make_terminal)
    same_config = operator.DurableAlbumRecoveryJournal(
        same_config_terminal.path,
        backup_root=same_config_terminal.backup_root,
        config_identity_sha256=disabled_identity,
    ).begin_or_resume(releases.candidate)
    assert same_config["phase"] == "complete"
    assert same_config["config_identity_sha256"] == disabled_identity

    phase_a_terminal = make_journal("album-phase-a-terminal", full_owner_identity)
    _rewrite_signed_journal(phase_a_terminal.path, make_terminal)
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            phase_a_terminal.path,
            backup_root=phase_a_terminal.backup_root,
            config_identity_sha256=disabled_identity,
        ).begin_or_resume(releases.candidate)

    unrelated_v2_terminal = make_journal("album-unrelated-v2-terminal", "6" * 64)
    _rewrite_signed_journal(unrelated_v2_terminal.path, make_terminal)
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            unrelated_v2_terminal.path,
            backup_root=unrelated_v2_terminal.backup_root,
            config_identity_sha256=disabled_identity,
        ).begin_or_resume(releases.candidate)

    phase_a_unfinished = make_journal("album-phase-a-unfinished", full_owner_identity)
    with pytest.raises(operator.ReleaseFailure, match="album_recovery_journal_invalid"):
        operator.DurableAlbumRecoveryJournal(
            phase_a_unfinished.path,
            backup_root=phase_a_unfinished.backup_root,
            config_identity_sha256=disabled_identity,
        ).begin_or_resume(releases.candidate)


def test_terminal_journal_env_digest_is_activate_only() -> None:
    common = [
        "--anchor",
        "/runtime/current",
        "--env-file",
        "/runtime/.env",
        "--env-file-sha256",
        "1" * 64,
        "--friday-home",
        "/runtime/home",
        "--unit-dir",
        "/runtime/units",
        "--database",
        "/runtime/state/friday.sqlite3",
        "--inbox-database",
        "/runtime/state/telegram-inbox.sqlite3",
        "--backup-dir",
        "/runtime/backups",
        "--state-dir",
        "/runtime/state",
        "--health-ca",
        "/runtime/health-ca.pem",
        "--health-ca-sha256",
        "2" * 64,
    ]
    activate = operator.build_parser().parse_args(
        [
            "activate",
            "--candidate",
            "/releases/rc1",
            "--candidate-tree-sha256",
            "3" * 64,
            "--previous",
            "/releases/legacy",
            "--previous-tree-sha256",
            "4" * 64,
            "--schema-capable-fallback",
            "/releases/rc0",
            "--schema-capable-fallback-tree-sha256",
            "5" * 64,
            "--terminal-journal-env-sha256",
            "6" * 64,
            "--next-env-file",
            "/runtime/state/next.env",
            "--next-env-file-sha256",
            "8" * 64,
            "--staged-config-transition",
            "secondary_shadow_disable",
            *common,
        ]
    )
    assert activate.terminal_journal_env_sha256 == "6" * 64
    assert activate.next_env_file == Path("/runtime/state/next.env")
    assert activate.next_env_file_sha256 == "8" * 64
    assert activate.staged_config_transition == "secondary_shadow_disable"

    with pytest.raises(SystemExit):
        operator.build_parser().parse_args(
            [
                "recover-historical-album",
                "--release",
                "/releases/final",
                "--release-tree-sha256",
                "7" * 64,
                "--terminal-journal-env-sha256",
                "6" * 64,
                "--next-env-file",
                "/runtime/state/next.env",
                "--next-env-file-sha256",
                "8" * 64,
                *common,
            ]
        )


def test_operator_transaction_lock_is_process_wide_nonblocking(
    tmp_path: Path,
    isolated_operator_transaction_domain: Path,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    lock_path = state / "immutable-release-operator.v1.lock"
    with (
        operator.OperatorTransactionLock(lock_path),
        pytest.raises(operator.ReleaseFailure, match="transaction_in_progress"),
        operator.OperatorTransactionLock(lock_path),
    ):
        pytest.fail("a concurrent release controller acquired the same lock")
    with operator.OperatorTransactionLock(lock_path):
        assert lock_path.stat().st_mode & 0o077 == 0


def test_operator_transaction_lock_survives_state_directory_replacement(
    tmp_path: Path,
    isolated_operator_transaction_domain: Path,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    displaced = tmp_path / "displaced-state"
    state.mkdir(mode=0o700)
    lock_path = state / "immutable-release-operator.v1.lock"

    with operator.OperatorTransactionLock(lock_path):
        state.rename(displaced)
        state.mkdir(mode=0o700)
        with (
            pytest.raises(operator.ReleaseFailure, match="^operator_transaction_in_progress$"),
            operator.OperatorTransactionLock(lock_path),
        ):
            pytest.fail("replacement state directory escaped the lexical lock domain")


def test_operator_transaction_guard_pins_named_state_inode_through_mutation(
    tmp_path: Path,
    isolated_operator_transaction_domain: Path,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    displaced = tmp_path / "displaced"
    state.mkdir(mode=0o700)
    lock_path = state / "immutable-release-operator.v1.lock"

    with operator.OperatorTransactionLock(lock_path) as transaction:
        state.rename(displaced)
        state.mkdir(mode=0o700)
        with pytest.raises(operator.ReleaseFailure, match="^operator_transaction_lock_changed$"):
            transaction.assert_held()


def test_operator_transaction_unit_pair_uses_filesystem_not_abstract_socket_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    units = tmp_path / "units"
    state.mkdir(mode=0o700)
    units.mkdir(mode=0o700)

    def forbidden_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("abstract AF_UNIX locking is network-namespace-local")

    monkeypatch.setattr(
        operator,
        "socket",
        SimpleNamespace(socket=forbidden_socket),
        raising=False,
    )
    with operator.OperatorTransactionLock(
        state / "immutable-release-operator.v1.lock",
        unit_dir=units,
    ) as transaction:
        transaction.assert_held()
        assert transaction._runtime_descriptors  # noqa: SLF001
        assert len(transaction._runtime_descriptors) == 1  # noqa: SLF001
        assert all(
            Path(name).name == name for name, _descriptor, _identity in transaction._runtime_descriptors
        )  # noqa: SLF001


def test_operator_transaction_lock_serializes_the_fixed_systemd_unit_pair_across_homes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    tmp_path.chmod(0o700)
    first_state = tmp_path / "first-home/data/state"
    second_state = tmp_path / "second-home/data/state"
    first_state.mkdir(parents=True, mode=0o700)
    second_state.mkdir(parents=True, mode=0o700)
    shared_units = tmp_path / "shared-units"
    different_units = tmp_path / "different-units"
    shared_units.mkdir(mode=0o700)
    different_units.mkdir(mode=0o700)
    first_lock = first_state / "immutable-release-operator.v1.lock"
    second_lock = second_state / "immutable-release-operator.v1.lock"

    with operator.OperatorTransactionLock(first_lock, unit_dir=shared_units):
        for second_unit_directory in (shared_units, different_units):
            with (
                pytest.raises(
                    operator.ReleaseFailure,
                    match="^operator_transaction_in_progress$",
                ),
                operator.OperatorTransactionLock(
                    second_lock,
                    unit_dir=second_unit_directory,
                ),
            ):
                pytest.fail("a second home acquired the shared systemd unit pair")

    with operator.OperatorTransactionLock(second_lock):
        pass


def test_operator_transaction_uses_one_portable_bounded_global_lock_domain(
    tmp_path: Path,
) -> None:
    runtime_parent = operator.OperatorTransactionLock._RUNTIME_PARENT  # noqa: SLF001
    assert runtime_parent.as_posix() == "/var/tmp"
    tmp_path.chmod(0o700)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir(mode=0o1777)
    shared_tmp.chmod(0o1777)

    def transaction(ordinal: int) -> operator.OperatorTransactionLock:
        state = tmp_path / f"state-{ordinal}"
        state.mkdir(mode=0o700)
        lock = operator.OperatorTransactionLock(state / "immutable-release-operator.v1.lock")
        lock._runtime_parent = shared_tmp  # noqa: SLF001
        return lock

    first = transaction(0)
    with first:
        runtime_root = first._runtime_directory  # noqa: SLF001
        assert runtime_root is not None
        assert runtime_root.parent == shared_tmp
        assert runtime_root.stat().st_uid == os.geteuid()
        assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o700
        for ordinal in range(1, 33):
            contender = transaction(ordinal)
            with (
                pytest.raises(
                    operator.ReleaseFailure,
                    match="^operator_transaction_in_progress$",
                ),
                contender,
            ):
                pytest.fail("an ephemeral state path escaped the global domain")

    for ordinal in range(33, 65):
        with transaction(ordinal):
            pass

    entries = list(runtime_root.iterdir())
    assert [entry.name for entry in entries] == [
        operator.OperatorTransactionLock._GLOBAL_RUNTIME_LOCK_NAME  # noqa: SLF001
    ]
    assert entries[0].stat().st_nlink == 1
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600


def test_operator_transaction_runtime_domain_cannot_split_when_run_user_appears(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir(mode=0o1777)
    shared_tmp.chmod(0o1777)
    first_state = tmp_path / "first-state"
    second_state = tmp_path / "second-state"
    first_state.mkdir(mode=0o700)
    second_state.mkdir(mode=0o700)
    first = operator.OperatorTransactionLock(first_state / "immutable-release-operator.v1.lock")
    second = operator.OperatorTransactionLock(second_state / "immutable-release-operator.v1.lock")
    first._runtime_parent = shared_tmp  # noqa: SLF001
    second._runtime_parent = shared_tmp  # noqa: SLF001

    with first:
        fake_run_user = tmp_path / "run/user" / str(os.geteuid())
        fake_run_user.mkdir(parents=True, mode=0o700)
        second._primary_runtime_directory = fake_run_user  # type: ignore[attr-defined]  # noqa: SLF001
        with (
            pytest.raises(
                operator.ReleaseFailure,
                match="^operator_transaction_in_progress$",
            ),
            second,
        ):
            pytest.fail("appearance of a private /run root split the lock domain")


def test_operator_transaction_guard_pins_portable_runtime_root_inode(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir(mode=0o1777)
    shared_tmp.chmod(0o1777)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    transaction = operator.OperatorTransactionLock(state / "immutable-release-operator.v1.lock")
    transaction._runtime_parent = shared_tmp  # noqa: SLF001

    with transaction:
        runtime_root = transaction._runtime_directory  # noqa: SLF001
        assert runtime_root is not None
        displaced = shared_tmp / "displaced"
        runtime_root.rename(displaced)
        runtime_root.mkdir(mode=0o700)
        with pytest.raises(
            operator.ReleaseFailure,
            match="^operator_transaction_lock_changed$",
        ):
            transaction.assert_held()


def test_operator_transaction_rejects_preplanted_runtime_root_symlink(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir(mode=0o1777)
    shared_tmp.chmod(0o1777)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    runtime_name = (
        f"{operator.OperatorTransactionLock._RUNTIME_DIRECTORY_PREFIX}-"  # noqa: SLF001
        f"{os.geteuid()}"
    )
    (shared_tmp / runtime_name).symlink_to(outside, target_is_directory=True)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    transaction = operator.OperatorTransactionLock(state / "immutable-release-operator.v1.lock")
    transaction._runtime_parent = shared_tmp  # noqa: SLF001

    with (
        pytest.raises(
            operator.ReleaseFailure,
            match="^operator_transaction_runtime_lock_invalid$",
        ),
        transaction,
    ):
        pytest.fail("a preplanted symlink became the global lock root")


def test_operator_transaction_creation_fsync_failure_releases_global_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir(mode=0o1777)
    shared_tmp.chmod(0o1777)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def transaction() -> operator.OperatorTransactionLock:
        result = operator.OperatorTransactionLock(state / "immutable-release-operator.v1.lock")
        result._runtime_parent = shared_tmp  # noqa: SLF001
        return result

    real_fsync = operator.os.fsync
    calls = 0

    def fail_global_name_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated global-name fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(operator.os, "fsync", fail_global_name_fsync)
    with (
        pytest.raises(
            operator.ReleaseFailure,
            match="^operator_transaction_lock_invalid$",
        ),
        transaction(),
    ):
        pytest.fail("runtime fsync failure admitted a transaction")

    monkeypatch.setattr(operator.os, "fsync", real_fsync)
    with transaction():
        pass


@pytest.mark.parametrize(
    "field",
    ["state_dir", "anchor", "env_file", "database", "inbox_database"],
)
def test_runtime_rejects_every_noncanonical_home_layout_path_before_port_construction(
    tmp_path: Path,
    field: str,
) -> None:
    friday_home = tmp_path / "friday-home"
    state_dir = friday_home / "data/state"
    config = operator.SystemdConfig(
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        env_file_sha256="1" * 64,
        friday_home=friday_home,
        unit_dir=tmp_path / "units",
        database=state_dir / "friday.sqlite3",
        inbox_database=state_dir / "telegram-inbox.sqlite3",
        backup_dir=friday_home / "backups",
        state_dir=state_dir,
        health_ca=friday_home / "health-ca.pem",
        health_ca_sha256="2" * 64,
    )

    with pytest.raises(
        operator.ReleaseFailure,
        match="^operator_transaction_(?:state_scope|layout)_invalid$",
    ):
        operator._require_runtime_operator_layout(  # noqa: SLF001
            replace(config, **{field: tmp_path / f"foreign-{field}"})
        )


def test_runtime_layout_preserves_the_in_state_legacy_database_name(tmp_path: Path) -> None:
    friday_home = tmp_path / "friday-home"
    state_dir = friday_home / "data/state"
    config = operator.SystemdConfig(
        anchor=friday_home / "current-release",
        env_file=friday_home / ".env.local",
        env_file_sha256="1" * 64,
        friday_home=friday_home,
        unit_dir=tmp_path / "units",
        database=state_dir / "jericho.sqlite3",
        inbox_database=state_dir / "telegram-inbox.sqlite3",
        backup_dir=friday_home / "backups",
        state_dir=state_dir,
        health_ca=friday_home / "health-ca.pem",
        health_ca_sha256="2" * 64,
    )

    operator._require_runtime_operator_layout(config)  # noqa: SLF001


def test_recovery_journal_probe_never_creates_a_missing_backup_root(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    missing_backup_root = tmp_path / "missing-backups"

    with pytest.raises(operator.ReleaseFailure, match="^private_directory_invalid$"):
        operator.DurableActivationJournal(
            state_dir / "immutable-release-activation.v1.json",
            backup_root=missing_backup_root,
            config_identity_sha256=None,
            create_backup_root=False,
        )

    assert not missing_backup_root.exists()


def test_cli_never_constructs_a_mutating_activation_port_before_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    tmp_path.chmod(0o700)
    friday_home = tmp_path / "friday-home"
    state_dir = friday_home / "data/state"
    state_dir.mkdir(parents=True, mode=0o700)
    backup_dir = tmp_path / "must-not-be-created"
    constructor_calls: list[object] = []

    def forbidden_constructor(config: object) -> object:
        constructor_calls.append(config)
        backup_dir.mkdir()
        raise AssertionError("activation port constructed before lock")

    monkeypatch.setattr(operator, "SystemdActivationPort", forbidden_constructor)
    common = [
        "--anchor",
        str(friday_home / "current-release"),
        "--env-file",
        str(friday_home / ".env.local"),
        "--env-file-sha256",
        "1" * 64,
        "--friday-home",
        str(friday_home),
        "--unit-dir",
        str(tmp_path / "units"),
        "--database",
        str(state_dir / "friday.sqlite3"),
        "--inbox-database",
        str(state_dir / "telegram-inbox.sqlite3"),
        "--backup-dir",
        str(backup_dir),
        "--state-dir",
        str(state_dir),
        "--health-ca",
        str(tmp_path / "health-ca.pem"),
        "--health-ca-sha256",
        "2" * 64,
    ]
    commands = (
        [
            "activate",
            "--candidate",
            str(tmp_path / "candidate"),
            "--candidate-tree-sha256",
            "3" * 64,
            "--previous",
            str(tmp_path / "previous"),
            "--previous-tree-sha256",
            "4" * 64,
            "--schema-capable-fallback",
            str(tmp_path / "fallback"),
            "--schema-capable-fallback-tree-sha256",
            "5" * 64,
            *common,
        ],
        [
            "recover-historical-album",
            "--release",
            str(tmp_path / "candidate"),
            "--release-tree-sha256",
            "3" * 64,
            *common,
        ],
    )

    lock_path = state_dir / "immutable-release-operator.v1.lock"
    with operator.OperatorTransactionLock(lock_path):
        for command in commands:
            arguments = operator.build_parser().parse_args(command)
            with pytest.raises(operator.ReleaseFailure, match="^operator_transaction_in_progress$"):
                operator._run_cli(arguments)  # noqa: SLF001

    assert constructor_calls == []
    assert not backup_dir.exists()


def test_empty_two_field_v3_transition_shape_normalizes_to_no_transition(
    tmp_path: Path,
    releases: Releases,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    state.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    journal = operator.DurableActivationJournal(
        state / "immutable-release-activation.v1.json",
        backup_root=backups,
        config_identity_sha256="9" * 64,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )

    def make_two_field_v3(core: dict[str, object]) -> None:
        core.pop("next_env_file")
        core.pop("next_env_file_sha256")
        core.pop("engineer_provision_committed")

    _rewrite_signed_journal(journal.path, make_two_field_v3)
    reopened = operator.DurableActivationJournal(
        journal.path,
        backup_root=backups,
        config_identity_sha256="9" * 64,
    )
    loaded = reopened.load()

    assert loaded["prebackup_config_transition"] == ""
    assert loaded["predecessor_env_sha256"] == ""
    assert operator._staged_config_transition(loaded) is None  # noqa: SLF001


def test_durable_journal_rejects_nonmonotonic_phase_without_rewriting_state(
    tmp_path: Path,
    releases: Releases,
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    state.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    journal = operator.DurableActivationJournal(
        state / "immutable-release-activation.v1.json",
        backup_root=backups,
        config_identity_sha256="9" * 64,
    )
    journal.begin(
        candidate=releases.candidate,
        previous=releases.previous,
        fallback=releases.fallback,
    )
    original = journal.path.read_bytes()
    with pytest.raises(operator.ReleaseFailure, match="journal_transition_invalid"):
        journal.record("backend_start_attempted", network_writer_uncertain=True)
    assert journal.path.read_bytes() == original
    assert journal.load()["phase"] == "prepared"


def test_multiple_uploader_claims_are_applied_under_one_backup_and_publicly_aggregated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _systemd_test_port(tmp_path)
    claims: list[Path] = []
    for index in range(2):
        claim = tmp_path / f"claim-{index}.json"
        claim.write_text("{}", encoding="ascii")
        claim.chmod(0o600)
        claims.append(claim)
    plan_hashes = ("1" * 64, "2" * 64)
    config = replace(
        base.config,
        memory_vault_mode="full_owner",
        alias_claim_manifests=tuple(claims),
        alias_expected_counts=(2, 3),
        alias_expected_plan_sha256s=plan_hashes,
    )
    with pytest.raises(
        operator.ReleaseFailure,
        match="alias_repair_not_allowed_in_body_free_phase",
    ):
        operator.SystemdActivationPort(replace(config, memory_vault_mode="disabled"))
    port = operator.SystemdActivationPort(config)
    port._leases = [object(), object()]  # noqa: SLF001
    monkeypatch.setattr(port, "writer_leases_held", lambda: True)
    backup_directory = config.backup_dir / "immutable-cutover-test"
    backup_directory.mkdir(mode=0o700)
    manifest = backup_directory / "manifest.json"
    manifest.write_text("{}\n", encoding="ascii")
    manifest.chmod(0o600)
    backup = operator.DatabaseBackup(
        schema_version=33,
        receipt_sha256="a" * 64,
        inbox_receipt_sha256="b" * 64,
        opaque=operator._ExactBackupPayload(backup_directory, ()),  # noqa: SLF001
    )
    pre_apply = iter(("7" * 64, "8" * 64))

    def apply(_database, *, claim_manifest, expected_count, expected_plan_sha256, **_kwargs):
        index = claims.index(claim_manifest)
        evidence = {
            "applied_count": expected_count,
            "applied_plan_sha256": expected_plan_sha256,
            "backup_database_sha256": "a" * 64,
            "backup_inbox_sha256": "b" * 64,
            "backup_manifest_sha256": "c" * 64,
            "pre_apply_database_sha256": next(pre_apply),
            "writer_quiescence_sha256": "d" * 64,
        }
        plan = SimpleNamespace(
            candidate_count=expected_count,
            owner_id="alice",
            plan_sha256=expected_plan_sha256,
            tenant_id="alice",
            uploader_id=("alice", "bob")[index],
        )
        return plan, evidence

    module = SimpleNamespace(
        EXTERNAL_BACKUP_SCHEMA="external-v1",
        ExternalBackupReceipt=lambda **values: values,
        apply_plan_under_held_leases=apply,
    )
    monkeypatch.setattr(operator, "_load_candidate_alias_tool", lambda _release: module)
    receipt = port.repair_file_aliases(
        operator.ReleaseIdentity(tmp_path / "candidate", "c" * 40, "0.206.0", "e" * 64, 34),
        backup,
    )
    assert receipt["status"] == "clear"
    assert receipt["applied_count"] == 5
    assert (
        receipt["plan_sha256"]
        == hashlib.sha256(
            operator._canonical_json(list(plan_hashes))  # noqa: SLF001
        ).hexdigest()
    )
    assert set(receipt) == {
        "schema",
        "status",
        "applied_count",
        "plan_sha256",
        "backup_manifest_sha256",
        "backup_database_sha256",
        "backup_inbox_sha256",
        "pre_apply_database_sha256",
        "writer_quiescence_sha256",
        "receipt_sha256",
    }


@pytest.mark.parametrize(
    "scenario",
    (
        "private_read",
        "stable_read",
        "streaming_hash",
        "private_copy",
        "obsidian_capture_swap",
        "fsync_tree_swap",
    ),
)
def test_mutable_regular_file_primitives_reject_fifo_substitution_bounded(
    tmp_path: Path,
    isolated_operator_transaction_domain: Path,
    scenario: str,
) -> None:
    """Every mutable descriptor contour fails closed instead of opening a FIFO blocking."""

    scenario_root = tmp_path / scenario
    scenario_root.mkdir(mode=0o700)
    source_root = Path(operator.__file__).resolve(strict=True).parents[1]
    script = r"""
import os
import pathlib
import stat
import sys

sys.path.insert(0, sys.argv[1])
import tools.immutable_release_operator as operator

scenario = sys.argv[2]
root = pathlib.Path(sys.argv[3])
operator.OperatorTransactionLock._RUNTIME_PARENT = pathlib.Path(sys.argv[4])
source = root / "source"
destination = root / "destination"
state = root / "state"
state.mkdir(mode=0o700)
lock_path = state / "immutable-release-operator.v1.lock"
original_read = operator.os.read

def guarded_read(descriptor, size):
    if stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        raise SystemExit("FIFO descriptor reached read")
    return original_read(descriptor, size)

operator.os.read = guarded_read

def must_reject(action):
    try:
        with operator.OperatorTransactionLock(lock_path):
            action()
    except (OSError, operator.ReleaseFailure):
        with operator.OperatorTransactionLock(lock_path):
            pass
        return
    raise SystemExit("FIFO substitution was accepted")

if scenario in {"private_read", "stable_read", "streaming_hash", "private_copy"}:
    os.mkfifo(source, mode=0o600)
    if scenario == "private_read":
        must_reject(
            lambda: operator._read_private_regular_file(
                source,
                maximum_bytes=1024,
                code="private_fifo_accepted",
            )
        )
    elif scenario == "stable_read":
        must_reject(
            lambda: operator._read_stable_regular_file(
                source,
                maximum_bytes=1024,
                code="stable_fifo_accepted",
            )
        )
    elif scenario == "streaming_hash":
        must_reject(lambda: operator._sha256_file(source))
    else:
        must_reject(lambda: operator._copy_private(source, destination))
        if destination.exists() or destination.is_symlink():
            raise SystemExit("copy mutated its destination before source admission")
elif scenario == "obsidian_capture_swap":
    vault = root / "vault"
    vault.mkdir(mode=0o700)
    source = vault / "note.md"
    source.write_bytes(b"stable note")
    source.chmod(0o600)
    displaced = vault / "note.displaced"
    original_open = operator.os.open
    swapped = False

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        global swapped
        if path == source.name and dir_fd is not None and not swapped:
            source.rename(displaced)
            os.mkfifo(source, mode=0o600)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    operator.os.open = raced_open
    must_reject(lambda: operator._capture_obsidian_tree(vault, destination=None))
    if not swapped:
        raise SystemExit("capture race was not injected")
elif scenario == "fsync_tree_swap":
    source.write_bytes(b"stable data")
    source.chmod(0o600)
    displaced = root / "source.displaced"
    original_open = operator.os.open
    original_fsync = operator.os.fsync
    swapped = False

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        global swapped
        if pathlib.Path(path) == source and not swapped:
            source.rename(displaced)
            os.mkfifo(source, mode=0o600)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def guarded_fsync(descriptor):
        if stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise SystemExit("FIFO descriptor reached fsync")
        return original_fsync(descriptor)

    operator.os.open = raced_open
    operator.os.fsync = guarded_fsync
    must_reject(lambda: operator._fsync_tree(root))
    if not swapped:
        raise SystemExit("fsync race was not injected")
else:
    raise SystemExit("unknown scenario")
"""
    try:
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                script,
                str(source_root),
                scenario,
                str(scenario_root),
                str(isolated_operator_transaction_domain),
            ],
            check=False,
            capture_output=True,
            cwd=scenario_root,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            },
            timeout=5,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stdout == b""
        assert result.stderr == b""
    finally:
        shutil.rmtree(scenario_root)
