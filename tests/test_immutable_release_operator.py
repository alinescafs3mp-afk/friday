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
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import friday
import tools.immutable_release_operator as operator


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
    ) -> None:
        self.fail = fail
        self.memory_vault_mode = memory_vault_mode
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

    def verify_active_anchor(self, previous: operator.ReleaseIdentity) -> None:
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
        assert transition == "obsidian_enable"
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
        assert transition == "obsidian_enable"
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
        assert transition == "obsidian_enable"
        assert self.canonical_env_sha256 in {"", predecessor_env_sha256}
        self.predecessor_env_sha256 = predecessor_env_sha256
        self.canonical_env_sha256 = predecessor_env_sha256
        self.next_env_file = next_env_file
        self.next_env_file_sha256 = next_env_file_sha256
        self.obsidian_mode = "disabled"
        self._event("select_predecessor_config")

    def backup_database(self) -> operator.DatabaseBackup:
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

    def restore_database(self, backup: operator.DatabaseBackup) -> None:
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
        if writer_target:
            self.state["writer_target"] = writer_target
        self.state["terminal_receipt_sha256"] = terminal_receipt_sha256

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
        def backup_database(self) -> operator.DatabaseBackup:
            assert self.canonical_env_sha256 == "1" * 64
            assert journal.state["phase"] == "leases_acquired"
            return super().backup_database()

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
    environment = (
        f"FRIDAY_HOME={port.config.friday_home} "
        f"FRIDAY_DATABASE_PATH={port.config.database} FRIDAY_DATABASE_MUST_EXIST=1"
    ).encode()
    extra_exec = b""
    extra_manager_dropin = ""

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
            stdout = environment
        elif "--property=KillMode" in arguments:
            stdout = b"control-group\n"
        elif "--property=UMask" in arguments:
            stdout = b"0077\n"
        elif "--property=UnitFileState" in arguments:
            stdout = b"enabled\n"
        elif "--property=LimitCORE" in arguments:
            stdout = b"0\n"
        elif "--property=PrivateTmp" in arguments:
            stdout = b"yes\n"
        elif "--property=UnsetEnvironment" in arguments:
            stdout = b"PYTHONPATH\n"
        elif "--property=WorkingDirectory" in arguments:
            stdout = str(port.config.friday_home).encode() + b"\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(port, "_systemctl", systemctl)
    port.verify_units(candidate)
    environment = environment.replace(b"FRIDAY_DATABASE_MUST_EXIST=1", b"FRIDAY_DATABASE_MUST_EXIST=0")
    with pytest.raises(operator.ReleaseFailure, match="manager_environment"):
        port.verify_units(candidate)
    environment = environment.replace(b"FRIDAY_DATABASE_MUST_EXIST=0", b"FRIDAY_DATABASE_MUST_EXIST=1")
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

    monkeypatch.setattr(
        operator.ssl,
        "create_default_context",
        lambda **_kwargs: SimpleNamespace(),
    )
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


def test_unit_pair_crash_converges_without_exposing_mixed_runtime_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        if arguments[0] == "is-enabled":
            return subprocess.CompletedProcess(arguments, 0, stdout=b"enabled\n", stderr=b"")
        if arguments[0] == "daemon-reload":
            for name in units:
                manager_argv[name] = operator._unit_exec_argv(  # noqa: SLF001
                    (unit_dir / name).read_bytes(),
                    code="test",
                )
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        name = arguments[1]
        return subprocess.CompletedProcess(arguments, 0, stdout=record(manager_argv[name]), stderr=b"")

    monkeypatch.setattr(operator, "_run_systemctl", systemctl)
    journal = operator.DurableUnitInstallJournal(state_dir / "immutable-release-unit-install.v1.json")
    original_replace = operator._replace_unit_file  # noqa: SLF001
    replacements = 0

    def crash_after_first(destination: Path, content: bytes) -> None:
        nonlocal replacements
        original_replace(destination, content)
        replacements += 1
        if replacements == 1:
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
    assert journal.load()["phase"] == "transition_anchor_active"
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
    assert hashes == {name: hashlib.sha256((artifacts / name).read_bytes()).hexdigest() for name in units}


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


def test_build_parser_binds_both_manifest_digests_into_build_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[operator.BuildSpec] = []

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
            "--max-schema",
            "34",
        ]
    )
    receipt = operator._run_cli(arguments)  # noqa: SLF001
    assert receipt["status"] == "clear"
    assert len(captured) == 1
    assert captured[0].runtime_lock_sha256 == "2" * 64
    assert captured[0].wheelhouse_manifest_sha256 == "3" * 64


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


def _synthetic_build_spec(tmp_path: Path) -> operator.BuildSpec:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(mode=0o700)
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
        anchor=tmp_path / "current-release",
        env_file=tmp_path / ".env.local",
        friday_home=tmp_path / "friday-home",
        base_python=base_python,
        base_python_sha256=hashlib.sha256(base_python.read_bytes()).hexdigest(),
        alias_tool=alias_tool,
        alias_tool_sha256=hashlib.sha256(alias_tool.read_bytes()).hexdigest(),
        alias_dependency=alias_dependency,
        alias_dependency_sha256=hashlib.sha256(alias_dependency.read_bytes()).hexdigest(),
        max_schema=34,
    )


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
def test_build_smoke_failure_cleans_only_prepublication_staging_and_quarantines_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    spec = _synthetic_build_spec(tmp_path)
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
        Path(entry).resolve(strict=True) for entry in sys.path if Path(entry).name == "site-packages"
    )
    (purelib / "runtime-dependencies.pth").write_text(f"{runtime_site}\n", encoding="ascii")
    shutil.copytree(
        Path(__file__).parents[1] / "friday",
        purelib / "friday",
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
        max_schema=36,
        memory_vault_mode_contract=operator.MEMORY_VAULT_MODE_CONTRACT,
        obsidian_cutover_contract=operator.OBSIDIAN_CUTOVER_CONTRACT,
    )
    receipt = b'{"memory_vault_mode_contract":"v1","schema":36,"status":"clear"}\n'
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
) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(mode=0o700)
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
        anchor=tmp_path / "current-release",
        env_file=tmp_path / ".env.local",
        friday_home=tmp_path / "friday-home",
        base_python=base_python,
        base_python_sha256=hashlib.sha256(base_python.read_bytes()).hexdigest(),
        alias_tool=alias_tool,
        alias_tool_sha256=hashlib.sha256(alias_tool.read_bytes()).hexdigest(),
        alias_dependency=alias_dependency,
        alias_dependency_sha256=hashlib.sha256(alias_dependency.read_bytes()).hexdigest(),
        max_schema=34,
    )
    with pytest.raises(operator.ReleaseFailure, match="^base_python_venv_unavailable$"):
        operator.build_release(spec)
    assert list(releases_root.iterdir()) == []
    assert not (releases_root / commit).exists()


def test_post_seal_failure_removes_staging_and_preserves_original_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(mode=0o700)
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
        anchor=tmp_path / "current-release",
        env_file=tmp_path / ".env.local",
        friday_home=tmp_path / "friday-home",
        base_python=base_python,
        base_python_sha256=hashlib.sha256(base_python.read_bytes()).hexdigest(),
        alias_tool=alias_tool,
        alias_tool_sha256=hashlib.sha256(alias_tool.read_bytes()).hexdigest(),
        alias_dependency=alias_dependency,
        alias_dependency_sha256=hashlib.sha256(alias_dependency.read_bytes()).hexdigest(),
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
            *common,
        ]
    )
    assert activate.terminal_journal_env_sha256 == "6" * 64
    assert activate.next_env_file == Path("/runtime/state/next.env")
    assert activate.next_env_file_sha256 == "8" * 64

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


def test_operator_transaction_lock_is_process_wide_nonblocking(tmp_path: Path) -> None:
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
