from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tracemalloc
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tools import immutable_release_operator as operator
from tools import release_artifact_retention as retention
from tools import release_artifact_retention_operator as retention_apply


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _write_journal(path: Path, core: dict[str, Any]) -> None:
    payload = {**core, "journal_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    path.write_bytes(_canonical(payload) + b"\n")
    path.chmod(0o600)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_backup(root: Path, ordinal: int) -> dict[str, Any]:
    _private_directory(root)
    files: list[dict[str, Any]] = []
    for name in ("database.sqlite3", "inbox.sqlite3"):
        path = root / name
        path.write_bytes(f"{name}-{ordinal}".encode("ascii"))
        path.chmod(0o600)
        files.append({"name": name, "sha256": _sha256_file(path), "size": path.stat().st_size})
    files.sort(key=lambda item: str(item["name"]))
    manifest = root / "manifest.json"
    manifest.write_bytes(
        _canonical(
            {
                "schema": "friday.immutable-cutover-exact-backup.v1",
                "database_schema": 50,
                "files": files,
            }
        )
        + b"\n"
    )
    manifest.chmod(0o600)
    obsidian_manifest = root / "obsidian-manifest.json"
    obsidian_manifest.write_bytes(
        _canonical(
            {
                "directories": [],
                "files": [],
                "present": False,
                "root": None,
                "schema": "friday.immutable-cutover-obsidian-root.v1",
            }
        )
        + b"\n"
    )
    obsidian_manifest.chmod(0o400)
    engineer_manifest = root / "engineer-manifest.json"
    engineer_manifest.write_bytes(
        _canonical(
            {
                "engineer_command_ledger_authority": None,
                "entries": [],
                "entry_count": 0,
                "key_present": False,
                "schema": "friday.immutable-cutover-engineer-store.v1",
                "store_present": False,
                "total_bytes": 0,
            }
        )
        + b"\n"
    )
    engineer_manifest.chmod(0o400)
    _private_directory(root / "engineer-recovery")
    obsidian_sha256 = _sha256_file(obsidian_manifest)
    engineer_sha256 = _sha256_file(engineer_manifest)
    return {
        "directory": str(root),
        "engineer": {
            "entry_count": 0,
            "key_present": False,
            "manifest_sha256": engineer_sha256,
            "store_present": False,
            "total_bytes": 0,
        },
        "engineer_receipt_sha256": engineer_sha256,
        "files": files,
        "inbox_receipt_sha256": hashlib.sha256(
            _canonical([item for item in files if str(item["name"]).startswith("inbox")])
        ).hexdigest(),
        "obsidian": {
            "file_count": 0,
            "manifest_sha256": obsidian_sha256,
            "present": False,
            "total_bytes": 0,
        },
        "obsidian_receipt_sha256": obsidian_sha256,
        "receipt_sha256": hashlib.sha256(
            _canonical([item for item in files if str(item["name"]).startswith("database")])
        ).hexdigest(),
        "schema_version": 50,
    }


@dataclass(frozen=True)
class _Release:
    identity: operator.ReleaseIdentity
    wheel_sha256: str

    @property
    def record(self) -> dict[str, Any]:
        return {
            "commit": self.identity.commit,
            "max_schema": self.identity.max_schema,
            "root": str(self.identity.root),
            "tree_manifest_sha256": self.identity.tree_manifest_sha256,
            "version": self.identity.version,
        }


def _release(root: Path, ordinal: int) -> _Release:
    root.mkdir(mode=0o700)
    artifacts = _private_directory(root / "artifacts")
    commit = f"{ordinal:040x}"
    version = f"0.1.{ordinal}"
    wheel_sha256 = hashlib.sha256(f"wheel-{ordinal}".encode()).hexdigest()
    metadata = artifacts / "immutable-release.json"
    metadata.write_bytes(
        _canonical(
            {
                "commit": commit,
                "max_schema": 50,
                "version": version,
                "wheel_sha256": wheel_sha256,
            }
        )
        + b"\n"
    )
    metadata.chmod(0o400)
    release_operator = artifacts / "immutable_release_operator.py"
    release_operator.write_bytes(f"# operator-{ordinal}\n".encode("ascii"))
    release_operator.chmod(0o400)
    metadata_sha256 = hashlib.sha256(metadata.read_bytes()).hexdigest()
    manifest = artifacts / "release-tree.sha256"
    manifest.write_text(
        f"F 0600 {metadata_sha256} artifacts/immutable-release.json\nF 0400 {'0' * 64} venv/bin/𝜋thon\n",
        encoding="utf-8",
    )
    tree_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return _Release(
        identity=operator.ReleaseIdentity(
            root=root,
            commit=commit,
            version=version,
            tree_manifest_sha256=tree_sha256,
            max_schema=50,
        ),
        wheel_sha256=wheel_sha256,
    )


def _activation_core(
    current: _Release,
    previous: _Release,
    fallback: _Release,
    *,
    phase: str = "clear",
    backup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": operator.ACTIVATION_JOURNAL_SCHEMA,
        "transaction_id": "1" * 64,
        "phase": phase,
        "config_identity_sha256": "2" * 64,
        "candidate": current.record,
        "previous": previous.record,
        "fallback": fallback.record,
        "backup": backup,
        "database_mutation_possible": False,
        "network_writer_uncertain": False,
        "terminal_receipt_sha256": "3" * 64 if phase in {"clear", "rolled_back", "recovered"} else "",
        "writer_target": "candidate" if phase == "clear" else "",
    }


def _unit_core(current: _Release, previous: _Release, *, phase: str = "complete") -> dict[str, Any]:
    unit_hashes = {
        "friday-backend.service": "4" * 64,
        "friday-backend.service.d/database.conf": "4" * 64,
        "friday-backend.service.d/security.conf": "4" * 64,
        "friday-bridge.service": "5" * 64,
        "friday-bridge.service.d/database.conf": "5" * 64,
        "friday-bridge.service.d/dependency.conf": "5" * 64,
        "friday-bridge.service.d/security.conf": "5" * 64,
    }
    receipt = hashlib.sha256(
        _canonical(
            {
                "candidate_tree_sha256": current.identity.tree_manifest_sha256,
                "previous_tree_sha256": previous.identity.tree_manifest_sha256,
                "unit_hashes": unit_hashes,
            }
        )
    ).hexdigest()
    return {
        "schema": operator.UNIT_INSTALL_JOURNAL_SCHEMA,
        "transaction_id": "6" * 64,
        "phase": phase,
        "candidate": current.record,
        "previous": previous.record,
        "transition_root": str(previous.identity.root),
        "candidate_unit_hashes": unit_hashes,
        "transition_unit_hashes": {
            "friday-backend.service": "7" * 64,
            "friday-bridge.service": "8" * 64,
        },
        "receipt_sha256": receipt if phase == "complete" else "",
    }


@pytest.fixture
def synthetic_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(
        retention,
        "_SUPPORTED_FILESYSTEM_MAGICS",
        frozenset({retention._path_filesystem_magic(tmp_path)}),  # noqa: SLF001
    )
    inventory = _private_directory(tmp_path / "inventory")
    backup_root = _private_directory(tmp_path / "backups")
    state = _private_directory(tmp_path / "state")
    current = _release(inventory / "current", 1)
    previous = _release(inventory / "previous", 2)
    fallback = _release(inventory / "fallback", 3)
    old = _release(inventory / "old", 4)
    (current.identity.root / "internal-artifacts-link").symlink_to("artifacts", target_is_directory=True)
    unknown = _private_directory(inventory / "unknown")
    (unknown / "opaque.bin").write_bytes(b"opaque")
    external = _private_directory(tmp_path / "external")
    (inventory / "symlink").symlink_to(external, target_is_directory=True)
    hardlinked = _private_directory(inventory / "hardlinked")
    source = hardlinked / "first.bin"
    source.write_bytes(b"hardlinked")
    os.link(source, hardlinked / "second.bin")

    activation_backup = _exact_backup(backup_root / "immutable-cutover-current", 1)
    older_backup = _exact_backup(backup_root / "immutable-cutover-older", 2)
    _exact_backup(backup_root / "legacy-unpinned", 3)
    evidence_root = _private_directory(backup_root / "canonical-evidence")
    evidence_authority = evidence_root / "receipt.json"
    evidence_authority.write_bytes(b'{"canonical":true}\n')
    evidence_authority.chmod(0o600)

    activation_journal = state / "immutable-release-activation.v1.json"
    unit_journal = state / "immutable-release-unit-install.v1.json"
    retention_scope = state / retention.RETENTION_SCOPE_NAME
    retention_scope.write_bytes(
        _canonical(
            {
                "backup_inventory_roots": [str(backup_root)],
                "backup_root": str(backup_root),
                "canonical_evidence_roots": [
                    {
                        "authority_path": str(evidence_authority),
                        "authority_sha256": _sha256_file(evidence_authority),
                        "path": str(evidence_root),
                    }
                ],
                "inventory_roots": [str(inventory)],
                "schema": retention.RETENTION_SCOPE_SCHEMA,
            }
        )
        + b"\n"
    )
    retention_scope.chmod(0o600)
    _write_journal(
        activation_journal,
        _activation_core(current, previous, fallback, backup=activation_backup),
    )
    _write_journal(unit_journal, _unit_core(current, previous))

    def generation_candidate(
        backup: dict[str, Any],
        release: _Release,
        ordinal: int,
        *,
        source_kind: str = "terminal_activation",
    ) -> dict[str, Any]:
        digest = lambda label: hashlib.sha256(f"{label}-{ordinal}".encode()).hexdigest()  # noqa: E731
        return {
            "allowed_rollback_tree_sha256s": [release.record["tree_manifest_sha256"]],
            "backup_directory": str(backup["directory"]),
            "backup_record_sha256": hashlib.sha256(_canonical(backup)).hexdigest(),
            "database_schema": backup["schema_version"],
            "database_receipt_sha256": str(backup["receipt_sha256"]),
            "engineer_receipt_sha256": str(backup["engineer_receipt_sha256"]),
            "inbox_receipt_sha256": str(backup["inbox_receipt_sha256"]),
            "obsidian_receipt_sha256": str(backup["obsidian_receipt_sha256"]),
            "restore_release": {
                **release.record,
                "wheel_sha256": release.wheel_sha256,
            },
            "schema": retention.dr_index.GENERATION_CANDIDATE_SCHEMA,
            "source_kind": source_kind,
            "source_receipt_sha256": digest("source-receipt"),
            "source_transaction_id": digest("transaction"),
        }

    def authentication_receipt(candidate: dict[str, Any], ordinal: int) -> dict[str, Any]:
        digest = lambda label, value: hashlib.sha256(f"{label}-{value}".encode()).hexdigest()  # noqa: E731
        directory = Path(candidate["backup_directory"])
        status = directory.stat()
        core = {
            "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
            "activation_journal_file_sha256": digest("activation-journal-file", ordinal),
            "activation_journal_sha256": digest("activation-journal", ordinal),
            "activation_receipt_file_sha256": digest("activation-receipt-file", ordinal),
            "activation_receipt_sha256": candidate["source_receipt_sha256"],
            "backup_directory": {"device": status.st_dev, "inode": status.st_ino, "path": str(directory)},
            "backup_manifest_sha256": _sha256_file(directory / "manifest.json"),
            "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
            "database_schema": candidate["database_schema"],
            "restore_operator_sha256": _sha256_file(
                Path(candidate["restore_release"]["root"]) / "artifacts/immutable_release_operator.py"
            ),
            "schema": retention.dr_index.AUTHENTICATION_RECEIPT_SCHEMA,
            "source_transaction_id": candidate["source_transaction_id"],
            "status": "authenticated",
            "surface_receipts": {
                "database": candidate["database_receipt_sha256"],
                "engineer": candidate["engineer_receipt_sha256"],
                "inbox": candidate["inbox_receipt_sha256"],
                "obsidian": candidate["obsidian_receipt_sha256"],
            },
        }
        receipt = {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
        return receipt

    def rehearsal_receipt(
        candidate: dict[str, Any], authentication: dict[str, Any], state: dict[str, Any], _ordinal: int
    ) -> dict[str, Any]:
        restore = candidate["restore_release"]
        source_keys = (
            "activation_journal_file_sha256",
            "activation_journal_sha256",
            "activation_receipt_file_sha256",
            "activation_receipt_sha256",
            "backup_manifest_sha256",
            "restore_operator_sha256",
            "surface_receipts",
        )
        core = {
            "authentication_receipt_sha256": authentication["receipt_sha256"],
            "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
            "check_count": len(retention.dr_index.DR_REHEARSAL_CHECKS),
            "checkset_sha256": retention.dr_index.DR_REHEARSAL_CHECKSET_SHA256,
            "database_foreign_keys_clear": True,
            "database_integrity_clear": True,
            "database_reopen_count": 2,
            "database_schema": candidate["database_schema"],
            "engineer_authority_present": True,
            "engineer_exact": True,
            "fault_boundary": "after_migration_before_provision_or_network",
            "four_surface_exact": True,
            "four_surface_sha256": hashlib.sha256(_canonical(authentication["surface_receipts"])).hexdigest(),
            "index_journal_sha256": state["journal_sha256"],
            "index_revision": state["revision"],
            "index_transaction_id": state["transaction_id"],
            "inbox_foreign_keys_clear": True,
            "inbox_integrity_clear": True,
            "inbox_reopen_count": 2,
            "network_call_count": 0,
            "obsidian_exact": True,
            "production_surface_write_count": 0,
            "restore_release": {
                key: restore[key]
                for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")
            },
            "rollback_restore_observed": True,
            "rollback_tree_sha256": restore["tree_manifest_sha256"],
            "rolled_back": True,
            "schema": retention.dr_index.REHEARSAL_RECEIPT_SCHEMA,
            "scratch_removed": True,
            "source": {key: authentication[key] for key in source_keys},
            "status": "rehearsed",
            "systemctl_call_count": 0,
        }
        return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}

    generation_index = retention.dr_index.DurableDRGenerationIndex(state)
    generation_index.initialize()
    for intent, candidate, ordinal in (
        ("bootstrap_current", generation_candidate(activation_backup, current, 1), 1),
        (
            "fill_older",
            generation_candidate(
                older_backup,
                fallback,
                2,
                source_kind="explicit_older_adoption",
            ),
            2,
        ),
    ):
        generation_state = generation_index.load()
        generation_state = generation_index.prepare(
            intent=intent,
            candidate=candidate,
            expected_journal_sha256=str(generation_state["journal_sha256"]),
        )
        authentication = authentication_receipt(candidate, ordinal)
        generation_state = generation_index.record_authenticated(
            receipt=authentication,
            expected_journal_sha256=str(generation_state["journal_sha256"]),
        )
        generation_state = generation_index.record_rehearsed(
            receipt=rehearsal_receipt(candidate, authentication, generation_state, ordinal),
            expected_journal_sha256=str(generation_state["journal_sha256"]),
        )
        generation_index.publish(
            expected_journal_sha256=str(generation_state["journal_sha256"]),
        )

    pins = tuple(retention.DRGenerationPin(**vars(pin)) for pin in generation_index.authority_snapshot().pins)
    dr_index = generation_index.path

    releases = {release.identity.root: release for release in (current, previous, fallback, old)}
    calls: list[tuple[str, Path]] = []

    def load(root: Path, *, expected_tree_sha256: str) -> operator.ReleaseIdentity:
        calls.append(("load", root))
        release = releases[root]
        assert expected_tree_sha256 == release.identity.tree_manifest_sha256
        return release.identity

    def verify(identity: operator.ReleaseIdentity) -> None:
        calls.append(("verify", identity.root))
        assert identity.root in releases

    monkeypatch.setattr(retention.release_operator, "load_release_identity", load)
    monkeypatch.setattr(retention.release_operator, "verify_release_tree", verify)
    return {
        "inventory": inventory,
        "backup_root": backup_root,
        "activation_journal": activation_journal,
        "unit_journal": unit_journal,
        "retention_scope": retention_scope,
        "dr_index": dr_index,
        "dr_index_owner": generation_index,
        "dr_pins": pins,
        "generation_candidate": generation_candidate,
        "authentication_receipt": authentication_receipt,
        "rehearsal_receipt": rehearsal_receipt,
        "evidence_root": evidence_root,
        "evidence_authority": evidence_authority,
        "activation_backup": activation_backup,
        "older_backup": older_backup,
        "current": current,
        "previous": previous,
        "fallback": fallback,
        "old": old,
        "calls": calls,
    }


def _bindings(
    fixture: dict[str, Any],
    *,
    dr_pins: tuple[retention.DRGenerationPin, ...] | None = None,
    dr_index_sha256: str | None = None,
    evidence_sha256: str | None = None,
) -> retention.RetentionAuthorityBindings:
    snapshot = fixture["dr_index_owner"].authority_snapshot()
    current_pins = tuple(retention.DRGenerationPin(**vars(pin)) for pin in snapshot.pins)
    return retention.RetentionAuthorityBindings(
        activation_journal_sha256=_sha256_file(fixture["activation_journal"]),
        unit_install_journal_sha256=_sha256_file(fixture["unit_journal"]),
        dr_index_path=fixture["dr_index"],
        dr_index_sha256=dr_index_sha256 or snapshot.index_sha256,
        dr_pins=current_pins if dr_pins is None else dr_pins,
        canonical_evidence_roots=(
            retention.CanonicalEvidenceRoot(
                path=fixture["evidence_root"],
                authority_path=fixture["evidence_authority"],
                authority_sha256=evidence_sha256 or _sha256_file(fixture["evidence_authority"]),
            ),
        ),
    )


def _plan(
    fixture: dict[str, Any],
    *,
    open_paths: tuple[Path, ...] = (),
    open_identities: tuple[tuple[int, int], ...] = (),
    authority_bindings: retention.RetentionAuthorityBindings | None = None,
) -> dict[str, Any]:
    return retention.plan_release_artifact_retention(
        activation_journal=fixture["activation_journal"],
        unit_journal=fixture["unit_journal"],
        backup_root=fixture["backup_root"],
        inventory_roots=(fixture["inventory"],),
        backup_inventory_roots=(fixture["backup_root"],),
        open_inventory=retention.OpenInventorySnapshot(
            source="synthetic_test",
            complete=True,
            open_paths=open_paths,
            open_identities=open_identities,
        ),
        authority_bindings=authority_bindings or _bindings(fixture),
    )


def test_complete_closed_inventory_classifies_only_authenticated_old_release_for_deletion(
    synthetic_inventory: dict[str, Any],
) -> None:
    plan = _plan(synthetic_inventory)
    targets = {Path(item["path"]).name: item for item in plan["targets"]}

    assert plan["schema"] == retention.PLAN_SCHEMA
    assert plan["mode"] == "read_only_classification"
    assert plan["scope"] == "release_and_backup_inventory"
    assert plan["apply_authority"] is False
    assert plan["classification_status"] == "eligible"
    assert plan["block_reason"] == ""
    root = plan["inventory_roots"][0]
    assert root["path"] == str(synthetic_inventory["inventory"])
    assert root["device"] == os.stat(synthetic_inventory["inventory"]).st_dev
    assert root["inode"] == os.stat(synthetic_inventory["inventory"]).st_ino
    assert root["type"] == "directory"
    assert root["nlink"] == os.stat(synthetic_inventory["inventory"]).st_nlink
    assert root["uid"] == os.geteuid()
    assert root["mount_id"] > 0
    assert root["filesystem_magic"] in retention._SUPPORTED_FILESYSTEM_MAGICS  # noqa: SLF001
    assert len(root["writable_authority_sha256"]) == 64
    assert targets["current"]["decision"] == "retain"
    assert targets["current"]["reason"] == "current_release"
    assert targets["previous"]["reason"] == "previous_release"
    assert targets["fallback"]["reason"] == "fallback_release"
    assert targets["old"]["decision"] == "delete_candidate"
    assert targets["old"]["reason"] == "retirable_authenticated_release"
    assert targets["old"]["identity"] == {
        "commit": synthetic_inventory["old"].identity.commit,
        "max_schema": 50,
        "tree_manifest_sha256": synthetic_inventory["old"].identity.tree_manifest_sha256,
        "version": "0.1.4",
        "wheel_sha256": synthetic_inventory["old"].wheel_sha256,
    }
    assert targets["unknown"]["reason"] == "unknown_artifact"
    assert targets["symlink"]["reason"] == "symlink_artifact"
    assert targets["hardlinked"]["reason"] == "hardlinked_artifact"
    assert all(
        set(item)
        == {
            "path",
            "device",
            "inode",
            "mount_id",
            "filesystem_magic",
            "mode",
            "type",
            "nlink",
            "recursive_bytes",
            "allocated_bytes",
            "entry_count",
            "inventory_sha256",
            "writable_authority_sha256",
            "identity",
            "decision",
            "reason",
        }
        for item in plan["targets"]
    )
    assert all(
        item["inventory_sha256"] is None or len(item["inventory_sha256"]) == 64 for item in plan["targets"]
    )
    core = {key: value for key, value in plan.items() if key != "plan_sha256"}
    assert plan["plan_sha256"] == hashlib.sha256(_canonical(core)).hexdigest()
    assert {call[0] for call in synthetic_inventory["calls"]} == {"load", "verify"}


def test_backup_inventory_binds_all_closed_retention_roles(
    synthetic_inventory: dict[str, Any],
) -> None:
    plan = _plan(synthetic_inventory)
    backups = {Path(item["path"]).name: item for item in plan["backup_targets"]}

    assert plan["authority_bindings"]["status"] == "authenticated"
    assert plan["authority_bindings"]["activation_journal_sha256"] == _sha256_file(
        synthetic_inventory["activation_journal"]
    )
    assert plan["authority_bindings"]["unit_install_journal_sha256"] == _sha256_file(
        synthetic_inventory["unit_journal"]
    )
    assert backups["immutable-cutover-current"]["reason"] == "activation_backup"
    assert backups["immutable-cutover-current"]["identity"]["database_receipt_sha256"] == str(
        synthetic_inventory["activation_backup"]["receipt_sha256"]
    )
    assert backups["immutable-cutover-older"]["reason"] == "dr_older_backup"
    assert backups["canonical-evidence"]["reason"] == "canonical_evidence"
    assert backups["legacy-unpinned"]["reason"] == "legacy_or_unknown_backup"
    assert all(item["decision"] == "retain" for item in plan["backup_targets"])
    assert plan["apply_authority"] is False


def test_retention_reauthenticates_exact_backup_bytes_before_any_delete_authority(
    synthetic_inventory: dict[str, Any],
) -> None:
    database = Path(synthetic_inventory["older_backup"]["directory"]) / "database.sqlite3"
    database.write_bytes(database.read_bytes() + b"tamper")

    plan = _plan(synthetic_inventory)

    assert plan["classification_status"] == "blocked"
    assert plan["block_reason"] == "dr_pins_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])
    assert not any(item["decision"] == "delete_candidate" for item in plan["backup_targets"])


def test_receipt_swap_after_index_snapshot_cannot_grant_retention_authority(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = _bindings(synthetic_inventory)
    older = next(pin for pin in bindings.dr_pins if pin.role == "older")
    assert older.rehearsal_receipt_path is not None
    receipt_path = older.rehearsal_receipt_path
    original_snapshot = retention.dr_index.DurableDRGenerationIndex.authority_snapshot
    swapped = False

    def snapshot_then_swap(
        index: retention.dr_index.DurableDRGenerationIndex,
    ) -> retention.dr_index.DRGenerationAuthoritySnapshot:
        nonlocal swapped
        result = original_snapshot(index)
        if not swapped:
            swapped = True
            replacement = json.loads(receipt_path.read_text(encoding="ascii"))
            replacement["rollback_tree_sha256"] = "f" * 64
            core = {key: value for key, value in replacement.items() if key != "receipt_sha256"}
            replacement["receipt_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
            receipt_path.chmod(0o600)
            receipt_path.write_bytes(_canonical(replacement) + b"\n")
            receipt_path.chmod(0o400)
        return result

    monkeypatch.setattr(
        retention.dr_index.DurableDRGenerationIndex,
        "authority_snapshot",
        snapshot_then_swap,
    )
    plan = _plan(synthetic_inventory, authority_bindings=bindings)

    assert plan["classification_status"] == "blocked"
    assert plan["block_reason"] == "dr_pins_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])


def test_pending_dr_generation_is_exact_and_retained(
    synthetic_inventory: dict[str, Any],
) -> None:
    pending_backup = _exact_backup(synthetic_inventory["backup_root"] / "pending", 4)
    index = synthetic_inventory["dr_index_owner"]
    state = index.load()
    index.prepare(
        intent="rotate_current",
        candidate=synthetic_inventory["generation_candidate"](
            pending_backup,
            synthetic_inventory["old"],
            4,
        ),
        expected_journal_sha256=str(state["journal_sha256"]),
    )

    plan = _plan(synthetic_inventory)

    target = next(item for item in plan["backup_targets"] if Path(item["path"]).name == "pending")
    assert target["reason"] == "dr_pending_backup"
    assert target["decision"] == "retain"


def test_prepared_replay_coalesces_exact_current_backup_and_restore_identity(
    synthetic_inventory: dict[str, Any],
) -> None:
    index = synthetic_inventory["dr_index_owner"]
    state = index.load()
    index.prepare(
        intent="rotate_current",
        candidate=synthetic_inventory["generation_candidate"](
            synthetic_inventory["activation_backup"],
            synthetic_inventory["current"],
            1,
        ),
        expected_journal_sha256=str(state["journal_sha256"]),
    )

    plan = _plan(synthetic_inventory)

    assert plan["classification_status"] == "eligible"
    assert plan["block_reason"] == ""
    matching = [
        pin
        for pin in plan["authority_bindings"]["dr_pins"]
        if pin["backup_directory"] == str(synthetic_inventory["activation_backup"]["directory"])
    ]
    assert [pin["role"] for pin in matching] == ["current", "pending"]
    backup = next(
        item
        for item in plan["backup_targets"]
        if item["path"] == str(synthetic_inventory["activation_backup"]["directory"])
    )
    assert backup["decision"] == "retain"
    assert backup["reason"] == "activation_backup"


def test_same_backup_with_conflicting_restore_identity_fails_closed(
    synthetic_inventory: dict[str, Any],
) -> None:
    index = synthetic_inventory["dr_index_owner"]
    state = index.load()
    index.prepare(
        intent="rotate_current",
        candidate=synthetic_inventory["generation_candidate"](
            synthetic_inventory["activation_backup"],
            synthetic_inventory["old"],
            9,
        ),
        expected_journal_sha256=str(state["journal_sha256"]),
    )

    plan = _plan(synthetic_inventory)

    assert plan["classification_status"] == "blocked"
    assert plan["block_reason"] == "dr_pins_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])


def test_atomic_dr_snapshot_rejects_mixed_projection_across_index_a_b_a(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = synthetic_inventory["dr_index_owner"]
    state_a = index.authority_snapshot()
    bindings_a = _bindings(synthetic_inventory)
    pending_backup = _exact_backup(synthetic_inventory["backup_root"] / "aba-pending", 11)
    current = index.load()
    index.prepare(
        intent="rotate_current",
        candidate=synthetic_inventory["generation_candidate"](
            pending_backup,
            synthetic_inventory["old"],
            11,
        ),
        expected_journal_sha256=str(current["journal_sha256"]),
    )
    state_b = index.authority_snapshot()
    assert state_b.index_sha256 != state_a.index_sha256
    assert state_b.pins != state_a.pins

    # Restore the exact A bytes after observing B: the old hash/pins/hash
    # sequence could accept B pins between identical A digests.
    index.path.write_bytes(state_a.index_raw)
    index.path.chmod(0o600)
    assert _sha256_file(index.path) == state_a.index_sha256
    mixed_bindings = replace(
        bindings_a,
        dr_pins=tuple(retention.DRGenerationPin(**vars(pin)) for pin in state_b.pins),
    )
    legacy_pins_calls = 0

    def forbidden_split_pins(
        _index: retention.dr_index.DurableDRGenerationIndex,
    ) -> tuple[retention.dr_index.GenerationPin, ...]:
        nonlocal legacy_pins_calls
        legacy_pins_calls += 1
        return state_b.pins

    monkeypatch.setattr(
        retention.dr_index.DurableDRGenerationIndex,
        "pins",
        forbidden_split_pins,
    )

    plan = _plan(synthetic_inventory, authority_bindings=mixed_bindings)

    assert legacy_pins_calls == 0
    assert plan["classification_status"] == "blocked"
    assert plan["block_reason"] == "dr_index_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])


@pytest.mark.parametrize(
    "forgery",
    ("dr_binding", "dr_body", "dr_index", "dr_receipt", "evidence"),
)
def test_forged_authority_digest_blocks_every_delete_candidate(
    synthetic_inventory: dict[str, Any],
    forgery: str,
) -> None:
    bindings = _bindings(synthetic_inventory)
    if forgery == "dr_binding":
        current, older = synthetic_inventory["dr_pins"]
        assert current.rehearsal_binding is not None
        forged_binding = dict(current.rehearsal_binding)
        forged_binding["database_schema"] = int(forged_binding["database_schema"]) + 1
        forged = replace(current, rehearsal_binding=forged_binding)
        bindings = _bindings(synthetic_inventory, dr_pins=(forged, older))
        expected = "dr_pins_invalid"
    elif forgery == "dr_body":
        body = next(synthetic_inventory["dr_index_owner"].receipt_directory.glob("authentication-*.json"))
        body.chmod(0o600)
        body.write_bytes(body.read_bytes() + b" ")
        body.chmod(0o400)
        expected = "dr_index_invalid"
    elif forgery == "dr_index":
        bindings = _bindings(synthetic_inventory, dr_index_sha256="f" * 64)
        expected = "dr_index_invalid"
    elif forgery == "evidence":
        bindings = _bindings(synthetic_inventory, evidence_sha256="f" * 64)
        expected = "canonical_evidence_invalid"
    else:
        current, older = synthetic_inventory["dr_pins"]
        forged = replace(current, receipt_sha256="f" * 64)
        bindings = _bindings(synthetic_inventory, dr_pins=(forged, older))
        expected = "dr_pins_invalid"

    plan = _plan(synthetic_inventory, authority_bindings=bindings)

    assert plan["classification_status"] == "blocked"
    assert plan["block_reason"] == expected
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])
    assert all(item["decision"] == "retain" for item in plan["backup_targets"])


def test_stale_exact_journal_binding_blocks_after_valid_journal_replacement(
    synthetic_inventory: dict[str, Any],
) -> None:
    bindings = _bindings(synthetic_inventory)
    core = _activation_core(
        synthetic_inventory["current"],
        synthetic_inventory["previous"],
        synthetic_inventory["fallback"],
        backup=synthetic_inventory["activation_backup"],
    )
    core["transaction_id"] = "9" * 64
    _write_journal(synthetic_inventory["activation_journal"], core)

    plan = _plan(synthetic_inventory, authority_bindings=bindings)

    assert plan["block_reason"] == "activation_journal_digest_mismatch"
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])


def test_distinct_current_dr_generation_has_its_own_closed_reason(
    synthetic_inventory: dict[str, Any],
) -> None:
    current_backup = _exact_backup(synthetic_inventory["backup_root"] / "dr-current", 5)
    index = synthetic_inventory["dr_index_owner"]
    state = index.load()
    state = index.prepare(
        intent="rotate_current",
        candidate=(
            candidate := synthetic_inventory["generation_candidate"](
                current_backup,
                synthetic_inventory["old"],
                5,
            )
        ),
        expected_journal_sha256=str(state["journal_sha256"]),
    )
    authentication = synthetic_inventory["authentication_receipt"](candidate, 5)
    state = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=str(state["journal_sha256"]),
    )
    state = index.record_rehearsed(
        receipt=synthetic_inventory["rehearsal_receipt"](candidate, authentication, state, 5),
        expected_journal_sha256=str(state["journal_sha256"]),
    )
    index.publish(expected_journal_sha256=str(state["journal_sha256"]))

    plan = _plan(synthetic_inventory)

    target = next(item for item in plan["backup_targets"] if Path(item["path"]).name == "dr-current")
    assert target["reason"] == "dr_current_backup"
    assert target["decision"] == "retain"
    restore_release = next(item for item in plan["targets"] if Path(item["path"]).name == "old")
    assert restore_release["reason"] == "dr_restore_release"
    assert restore_release["decision"] == "retain"


def test_duplicate_and_overlapping_authority_inputs_are_rejected_exactly(
    synthetic_inventory: dict[str, Any],
) -> None:
    current, older = synthetic_inventory["dr_pins"]
    duplicate_role = replace(older, role="current")
    forged = _plan(
        synthetic_inventory,
        authority_bindings=_bindings(
            synthetic_inventory,
            dr_pins=(current, duplicate_role, older),
        ),
    )
    assert forged["block_reason"] == "dr_pins_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in forged["targets"])

    with pytest.raises(retention.RetentionPlanError, match="inventory_roots_duplicate"):
        retention.plan_release_artifact_retention(
            activation_journal=synthetic_inventory["activation_journal"],
            unit_journal=synthetic_inventory["unit_journal"],
            backup_root=synthetic_inventory["backup_root"],
            inventory_roots=(synthetic_inventory["inventory"],),
            backup_inventory_roots=(
                synthetic_inventory["backup_root"],
                synthetic_inventory["backup_root"],
            ),
        )

    nested = _private_directory(synthetic_inventory["evidence_root"] / "nested")
    nested_authority = nested / "receipt.json"
    nested_authority.write_bytes(b'{"nested":true}\n')
    nested_authority.chmod(0o600)
    bindings = _bindings(synthetic_inventory)
    overlapping = retention.RetentionAuthorityBindings(
        activation_journal_sha256=bindings.activation_journal_sha256,
        unit_install_journal_sha256=bindings.unit_install_journal_sha256,
        dr_index_path=bindings.dr_index_path,
        dr_index_sha256=bindings.dr_index_sha256,
        dr_pins=bindings.dr_pins,
        canonical_evidence_roots=(
            *bindings.canonical_evidence_roots,
            retention.CanonicalEvidenceRoot(
                path=nested,
                authority_path=nested_authority,
                authority_sha256=_sha256_file(nested_authority),
            ),
        ),
    )
    with pytest.raises(retention.RetentionPlanError, match="canonical_evidence_invalid"):
        _plan(synthetic_inventory, authority_bindings=overlapping)


def test_evidence_authority_parent_swap_is_detected_and_fails_closed(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = synthetic_inventory["evidence_root"]
    authority_name = synthetic_inventory["evidence_authority"].name
    displaced = evidence_root.with_name("canonical-evidence-displaced")
    replacement_after = evidence_root.with_name("canonical-evidence-racer")
    raw = synthetic_inventory["evidence_authority"].read_bytes()
    real_require = retention._require_pinned_directory  # noqa: SLF001
    swapped = False

    def swap_after_pin(
        descriptor: int,
        parts: tuple[str, ...],
        identities: tuple[tuple[int, int], ...],
        *,
        code: str = "output_path_invalid",
        private: bool = True,
    ) -> os.stat_result:
        nonlocal swapped
        status = real_require(
            descriptor,
            parts,
            identities,
            code=code,
            private=private,
        )
        if code == "canonical_evidence_invalid" and not swapped:
            evidence_root.rename(displaced)
            _private_directory(evidence_root)
            replacement_authority = evidence_root / authority_name
            replacement_authority.write_bytes(raw)
            replacement_authority.chmod(0o600)
            swapped = True
        return status

    monkeypatch.setattr(retention, "_require_pinned_directory", swap_after_pin)
    try:
        plan = _plan(synthetic_inventory)
    finally:
        if swapped:
            evidence_root.rename(replacement_after)
            displaced.rename(evidence_root)

    assert plan["block_reason"] == "canonical_evidence_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])


def test_plan_is_deterministic_and_never_mutates_inventory(synthetic_inventory: dict[str, Any]) -> None:
    before = sorted(
        str(path.relative_to(synthetic_inventory["inventory"]))
        for path in synthetic_inventory["inventory"].rglob("*")
    )
    activation_before = synthetic_inventory["activation_journal"].read_bytes()
    unit_before = synthetic_inventory["unit_journal"].read_bytes()

    first = _plan(synthetic_inventory)
    second = _plan(synthetic_inventory)

    assert first == second
    assert before == sorted(
        str(path.relative_to(synthetic_inventory["inventory"]))
        for path in synthetic_inventory["inventory"].rglob("*")
    )
    assert synthetic_inventory["activation_journal"].read_bytes() == activation_before
    assert synthetic_inventory["unit_journal"].read_bytes() == unit_before


def test_plan_reauthenticates_backup_without_engineer_scratch_mutation(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = operator.DurableActivationJournal.database_backup
    integrity_modes: list[bool] = []

    def observe(
        journal: operator.DurableActivationJournal,
        *,
        verify_engineer_sqlite_integrity: bool = True,
    ) -> operator.DatabaseBackup | None:
        integrity_modes.append(verify_engineer_sqlite_integrity)
        return original(
            journal,
            verify_engineer_sqlite_integrity=verify_engineer_sqlite_integrity,
        )

    monkeypatch.setattr(operator.DurableActivationJournal, "database_backup", observe)

    _plan(synthetic_inventory)

    assert integrity_modes == [False, False]


def test_open_reference_and_incomplete_default_both_fail_closed(synthetic_inventory: dict[str, Any]) -> None:
    open_plan = _plan(synthetic_inventory, open_paths=(synthetic_inventory["old"].identity.root,))
    open_old = next(item for item in open_plan["targets"] if Path(item["path"]).name == "old")
    assert open_old["decision"] == "retain"
    assert open_old["reason"] == "open_reference"

    metadata_status = os.stat(synthetic_inventory["old"].identity.root / "artifacts/immutable-release.json")
    identity_plan = _plan(
        synthetic_inventory,
        open_identities=((metadata_status.st_dev, metadata_status.st_ino),),
    )
    identity_old = next(item for item in identity_plan["targets"] if Path(item["path"]).name == "old")
    assert identity_old["decision"] == "retain"
    assert identity_old["reason"] == "open_reference"

    default_plan = retention.plan_release_artifact_retention(
        activation_journal=synthetic_inventory["activation_journal"],
        unit_journal=synthetic_inventory["unit_journal"],
        backup_root=synthetic_inventory["backup_root"],
        inventory_roots=(synthetic_inventory["inventory"],),
        backup_inventory_roots=(synthetic_inventory["backup_root"],),
        authority_bindings=_bindings(synthetic_inventory),
    )
    assert default_plan["classification_status"] == "blocked"
    assert default_plan["block_reason"] == "open_state_ambiguous"
    assert not any(item["decision"] == "delete_candidate" for item in default_plan["targets"])
    default_targets = {Path(item["path"]).name: item for item in default_plan["targets"]}
    assert default_targets["current"]["reason"] == "current_release"
    assert default_plan["open_inventory"] == {
        "schema": retention.OPEN_INVENTORY_SCHEMA,
        "source": "unavailable",
        "complete": False,
        "open_path_count": 0,
        "open_identity_count": 0,
        "authority_sha256": "",
        "target_index_sha256": "",
        "process_epoch_sha256": "",
        "observation_role": "diagnostic_prerequisite",
        "universal_absence_proof": False,
        "snapshot_sha256": default_plan["open_inventory"]["snapshot_sha256"],
    }


@pytest.mark.parametrize(
    ("journal", "phase", "reason"),
    (
        ("activation", "prepared", "activation_not_clear"),
        ("unit", "manager_reloaded", "unit_install_not_complete"),
    ),
)
def test_unfinished_journal_retains_every_clean_release(
    synthetic_inventory: dict[str, Any],
    journal: str,
    phase: str,
    reason: str,
) -> None:
    if journal == "activation":
        _write_journal(
            synthetic_inventory["activation_journal"],
            _activation_core(
                synthetic_inventory["current"],
                synthetic_inventory["previous"],
                synthetic_inventory["fallback"],
                phase=phase,
            ),
        )
    else:
        _write_journal(
            synthetic_inventory["unit_journal"],
            _unit_core(synthetic_inventory["current"], synthetic_inventory["previous"], phase=phase),
        )

    plan = _plan(synthetic_inventory)

    assert plan["classification_status"] == "blocked"
    assert plan["block_reason"] == reason
    assert not any(item["decision"] == "delete_candidate" for item in plan["targets"])


def test_corrupt_journal_digest_and_cross_journal_identity_both_retain(
    synthetic_inventory: dict[str, Any],
) -> None:
    raw = json.loads(synthetic_inventory["activation_journal"].read_text(encoding="ascii"))
    raw["journal_sha256"] = "0" * 64
    synthetic_inventory["activation_journal"].write_bytes(_canonical(raw) + b"\n")
    synthetic_inventory["activation_journal"].chmod(0o600)
    corrupt = _plan(synthetic_inventory)
    assert corrupt["block_reason"] == "activation_journal_invalid"
    assert not any(item["decision"] == "delete_candidate" for item in corrupt["targets"])

    _write_journal(
        synthetic_inventory["activation_journal"],
        _activation_core(
            synthetic_inventory["current"],
            synthetic_inventory["previous"],
            synthetic_inventory["fallback"],
        ),
    )
    _write_journal(
        synthetic_inventory["unit_journal"],
        _unit_core(synthetic_inventory["fallback"], synthetic_inventory["previous"]),
    )
    mismatch = _plan(synthetic_inventory)
    assert mismatch["block_reason"] == "journal_identity_mismatch"
    assert not any(item["decision"] == "delete_candidate" for item in mismatch["targets"])


def test_tampered_metadata_and_authenticated_hardlinks_are_never_candidates(
    synthetic_inventory: dict[str, Any],
) -> None:
    old_root = synthetic_inventory["old"].identity.root
    metadata = old_root / "artifacts/immutable-release.json"
    original = metadata.read_bytes()
    metadata.chmod(0o600)
    metadata.write_bytes(original.replace(synthetic_inventory["old"].wheel_sha256.encode(), b"f" * 64))
    tampered = _plan(synthetic_inventory)
    tampered_old = next(item for item in tampered["targets"] if Path(item["path"]).name == "old")
    assert tampered_old["decision"] == "retain"
    assert tampered_old["reason"] == "malformed_release"

    metadata.write_bytes(original)
    source = old_root / "hardlink-source"
    source.write_bytes(b"sealed-looking")
    os.link(source, old_root / "hardlink-alias")
    hardlinked = _plan(synthetic_inventory)
    hardlinked_old = next(item for item in hardlinked["targets"] if Path(item["path"]).name == "old")
    assert hardlinked_old["decision"] == "retain"
    assert hardlinked_old["reason"] == "hardlinked_artifact"


def test_cli_stdout_is_read_only_and_explicit_output_is_atomic(
    synthetic_inventory: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    argv = [
        "--activation-journal",
        str(synthetic_inventory["activation_journal"]),
        "--unit-journal",
        str(synthetic_inventory["unit_journal"]),
        "--backup-root",
        str(synthetic_inventory["backup_root"]),
        "--inventory-root",
        str(synthetic_inventory["inventory"]),
        "--backup-inventory-root",
        str(synthetic_inventory["backup_root"]),
    ]
    assert retention.main(argv) == 0
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["block_reason"] == "retention_authority_unbound"

    output_parent = _private_directory(tmp_path / "output")
    output = output_parent / "plan.json"
    assert retention.main([*argv, "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    assert json.loads(output.read_text(encoding="ascii"))["schema"] == retention.PLAN_SCHEMA
    assert stat_mode(output) == 0o600
    assert not list(output_parent.glob(".*.new"))


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def test_inventory_root_symlink_and_forged_complete_cli_controls_are_unavailable(
    synthetic_inventory: dict[str, Any],
    tmp_path: Path,
) -> None:
    alias = tmp_path / "inventory-alias"
    alias.symlink_to(synthetic_inventory["inventory"], target_is_directory=True)
    with pytest.raises(retention.RetentionPlanError, match="inventory_root_invalid"):
        retention.plan_release_artifact_retention(
            activation_journal=synthetic_inventory["activation_journal"],
            unit_journal=synthetic_inventory["unit_journal"],
            backup_root=synthetic_inventory["backup_root"],
            inventory_roots=(alias,),
        )

    parser = retention._parser()  # noqa: SLF001 - prove CLI has no self-attestation rail
    with pytest.raises(SystemExit):
        parser.parse_args(["--open-inventory-complete"])


def test_atomic_output_never_overwrites_a_raced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_parent = _private_directory(tmp_path / "atomic-output")
    output = output_parent / "plan.json"
    real_link = os.link

    def race_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_dir_fd)
        try:
            os.write(descriptor, b"racer\n")
        finally:
            os.close(descriptor)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(retention.os, "link", race_link)
    with pytest.raises(retention.RetentionPlanError, match="output_exists"):
        retention._write_atomic(output, b"planner\n")  # noqa: SLF001 - exact no-replace boundary

    assert output.read_bytes() == b"racer\n"
    assert not list(output_parent.glob(".*.new"))


def test_atomic_output_removes_its_publication_when_parent_revalidation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_parent = _private_directory(tmp_path / "revalidation-output")
    output = output_parent / "plan.json"
    real_require = retention._require_pinned_directory  # noqa: SLF001
    calls = 0

    def fail_after_publish(
        descriptor: int,
        parts: tuple[str, ...],
        identities: tuple[tuple[int, int], ...],
    ) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise retention.RetentionPlanError("output_path_invalid")
        return real_require(descriptor, parts, identities)

    monkeypatch.setattr(retention, "_require_pinned_directory", fail_after_publish)
    with pytest.raises(retention.RetentionPlanError, match="output_path_invalid"):
        retention._write_atomic(output, b"planner\n")  # noqa: SLF001

    assert not output.exists()
    assert not list(output_parent.glob(".*.new"))


def test_atomic_output_detects_and_preserves_a_post_publish_racer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_parent = _private_directory(tmp_path / "post-publish-race")
    output = output_parent / "plan.json"
    real_require = retention._require_pinned_directory  # noqa: SLF001
    calls = 0

    def replace_after_publish(
        descriptor: int,
        parts: tuple[str, ...],
        identities: tuple[tuple[int, int], ...],
    ) -> os.stat_result:
        nonlocal calls
        calls += 1
        status = real_require(descriptor, parts, identities)
        if calls == 3:
            output.unlink()
            output.write_bytes(b"racer\n")
        return status

    monkeypatch.setattr(retention, "_require_pinned_directory", replace_after_publish)
    with pytest.raises(retention.RetentionPlanError, match="output_publish_raced"):
        retention._write_atomic(output, b"planner\n")  # noqa: SLF001

    assert output.read_bytes() == b"racer\n"
    assert not list(output_parent.glob(".*.new"))


def _eligible_plan(
    fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    runtime_parent = fixture["activation_journal"].parent.parent / "operator-runtime"
    runtime_parent.mkdir(mode=0o1700, exist_ok=True)
    runtime_parent.chmod(0o1700)
    monkeypatch.setattr(operator.OperatorTransactionLock, "_RUNTIME_PARENT", runtime_parent)
    monkeypatch.setattr(
        retention,
        "build_complete_open_inventory",
        lambda **_kwargs: retention.OpenInventorySnapshot(
            source="code_owned_fd_inventory_v1",
            complete=True,
        ),
    )
    # Exercise the downstream apply contour as if a future fully validated
    # writer receipt pair were present.  The production v1 contract remains
    # fail-closed and is covered independently below.
    _enable_complete_delete_evidence(monkeypatch)
    return retention.build_eligible_retention_plan(
        activation_journal=fixture["activation_journal"],
        unit_journal=fixture["unit_journal"],
        backup_root=fixture["backup_root"],
        inventory_roots=(fixture["inventory"],),
        backup_inventory_roots=(fixture["backup_root"],),
        canonical_evidence_roots=(
            retention.CanonicalEvidenceRoot(
                path=fixture["evidence_root"],
                authority_path=fixture["evidence_authority"],
                authority_sha256=_sha256_file(fixture["evidence_authority"]),
            ),
        ),
    )


def _enable_complete_delete_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        retention,
        "_full_rollback_release_evidence_complete",
        lambda _authentication, _rehearsal: True,
    )


def test_legacy_v1_dr_evidence_never_grants_destructive_authority(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed = False

    def unexpected_probe(**_kwargs: Any) -> retention.OpenInventorySnapshot:
        nonlocal probed
        probed = True
        raise AssertionError("legacy v1 authority must fail before the open-file probe")

    monkeypatch.setattr(retention, "build_complete_open_inventory", unexpected_probe)
    scope = retention.load_retention_scope_authority(
        activation_journal=synthetic_inventory["activation_journal"]
    )
    with pytest.raises(
        retention.RetentionPlanError,
        match="^dr_rollback_release_evidence_incomplete$",
    ):
        retention.build_eligible_retention_plan(
            activation_journal=synthetic_inventory["activation_journal"],
            unit_journal=synthetic_inventory["unit_journal"],
            backup_root=synthetic_inventory["backup_root"],
            inventory_roots=(synthetic_inventory["inventory"],),
            backup_inventory_roots=(synthetic_inventory["backup_root"],),
            canonical_evidence_roots=scope.canonical_evidence_roots,
        )

    read_only = _plan(synthetic_inventory)
    assert read_only["classification_status"] == "eligible"
    assert read_only["apply_authority"] is False
    assert read_only["block_reason"] == ""
    assert any(
        item["decision"] == "delete_candidate"
        for key in ("targets", "backup_targets")
        for item in read_only[key]
    )
    assert probed is False


def _plan_file(plan: dict[str, Any], path: Path) -> Path:
    path.write_bytes(_canonical(plan) + b"\n")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("reader", "hardlinked"),
    (
        ("reviewed_plan", False),
        ("apply_journal", False),
        ("resume_plan", True),
        ("retention_scope", False),
        ("streaming_auth", False),
    ),
)
def test_metadata_fifo_swap_is_bounded_for_plan_journal_and_two_link_resume(
    tmp_path: Path,
    reader: str,
    hardlinked: bool,
) -> None:
    repository = Path(retention.__file__).resolve().parents[1]
    target = tmp_path / (retention.RETENTION_SCOPE_NAME if reader == "retention_scope" else "reviewed.json")
    target.write_bytes(b"{}\n")
    target.chmod(0o600)
    if hardlinked:
        os.link(target, tmp_path / "reviewed-stage.json")
    child = r"""
import os
import pathlib
import sys

repository = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
reader = sys.argv[3]
sys.path.insert(0, str(repository))
from tools import release_artifact_retention as retention
from tools import release_artifact_retention_operator as retention_apply

real_open = os.open
swapped = False
target_opens = 0

def swapping_open(path, flags, *args, **kwargs):
    global swapped, target_opens
    if path == target.name or path == target:
        target_opens += 1
    swap_at = 2 if reader == "retention_scope" else 1
    if not swapped and target_opens == swap_at:
        os.unlink(target)
        os.mkfifo(target, 0o600)
        swapped = True
    return real_open(path, flags, *args, **kwargs)

retention.os.open = swapping_open
try:
    if reader == "apply_journal":
        retention_apply._load_journal(target)
    elif reader == "retention_scope":
        retention.load_retention_scope_authority(
            activation_journal=target.parent / "activation.json",
        )
    elif reader == "streaming_auth":
        retention._stable_file_sha256_streaming(
            target,
            expected_size=3,
            private=True,
            code="streaming_auth_invalid",
        )
    else:
        retention_apply._read_plan(
            target,
            expected_sha256="a" * 64,
            allow_recoverable_two_link=reader == "resume_plan",
        )
except (retention.RetentionPlanError, retention_apply.RetentionApplyError):
    if swapped:
        raise SystemExit(0)
raise SystemExit(3)
"""
    completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed test interpreter and program
        [sys.executable, "-I", "-B", "-c", child, str(repository), str(target), reader],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        timeout=3,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_stale_v1_executable_plan_is_rejected_before_durable_apply_state(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    production_evidence_check = retention._full_rollback_release_evidence_complete  # noqa: SLF001
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    monkeypatch.setattr(
        retention,
        "_full_rollback_release_evidence_complete",
        production_evidence_check,
    )
    plan_path = _plan_file(plan, tmp_path / "legacy-v1-plan.json")
    state = synthetic_inventory["activation_journal"].parent
    durable_plan_directory = state / retention_apply.APPLY_PLAN_DIRECTORY
    journal = state / retention_apply.APPLY_JOURNAL_NAME

    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_dr_rollback_release_evidence_incomplete$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert not durable_plan_directory.exists()
    assert not journal.exists()
    assert all(
        Path(item["path"]).exists()
        for key in ("targets", "backup_targets")
        for item in plan[key]
        if item["decision"] == "delete_candidate"
    )


def test_stale_v1_resume_does_not_repair_two_link_plan_before_rejection(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_evidence_check = retention._full_rollback_release_evidence_complete  # noqa: SLF001
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    state = synthetic_inventory["activation_journal"].parent
    durable = retention_apply._persist_reviewed_plan(  # noqa: SLF001
        state,
        plan,
        guard=lambda: None,
        allow_incomplete_stage_repair=True,
    )
    candidates = retention_apply._candidate_records(plan)  # noqa: SLF001
    journal_path = state / retention_apply.APPLY_JOURNAL_NAME
    journal = retention_apply._new_journal(  # noqa: SLF001
        plan,
        candidates,
        durable_plan=durable,
        filesystem_before=retention_apply._filesystem_free_evidence(  # noqa: SLF001
            candidates,
            guard=lambda: None,
        ),
    )
    retention_apply._write_journal(journal_path, journal, guard=lambda: None)  # noqa: SLF001
    staged = durable[0].with_name(f".{durable[0].name}.new")
    os.link(durable[0], staged)
    plan_bytes = durable[0].read_bytes()
    journal_bytes = journal_path.read_bytes()
    target_snapshots = {
        Path(str(candidate["path"])): retention._snapshot(Path(str(candidate["path"]))).records  # noqa: SLF001
        for candidate in candidates
    }
    assert os.lstat(durable[0]).st_nlink == 2
    monkeypatch.setattr(
        retention,
        "_full_rollback_release_evidence_complete",
        production_evidence_check,
    )

    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_dr_rollback_release_evidence_incomplete$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=None,
            expected_plan_sha256=plan["plan_sha256"],
            state_dir=state,
        )

    assert durable[0].read_bytes() == plan_bytes
    assert staged.read_bytes() == plan_bytes
    assert os.path.samefile(durable[0], staged)
    assert os.lstat(durable[0]).st_nlink == 2
    assert journal_path.read_bytes() == journal_bytes
    assert {
        path: retention._snapshot(path).records  # noqa: SLF001
        for path in target_snapshots
    } == target_snapshots


def _scope_body(fixture: dict[str, Any]) -> dict[str, Any]:
    return json.loads(fixture["retention_scope"].read_text(encoding="ascii"))


def _write_scope(fixture: dict[str, Any], body: dict[str, Any]) -> None:
    fixture["retention_scope"].write_bytes(_canonical(body) + b"\n")
    fixture["retention_scope"].chmod(0o600)


def test_eligible_plan_binds_only_the_complete_code_owned_retention_scope(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    scope = retention.load_retention_scope_authority(
        activation_journal=synthetic_inventory["activation_journal"]
    )

    assert plan["retention_scope"] == scope.receipt
    assert len(plan["retention_scope"]["file_sha256"]) == 64
    with pytest.raises(retention.RetentionPlanError, match="^retention_scope_mismatch$"):
        retention.build_eligible_retention_plan(
            activation_journal=synthetic_inventory["activation_journal"],
            unit_journal=synthetic_inventory["unit_journal"],
            backup_root=synthetic_inventory["backup_root"],
            inventory_roots=(),
            backup_inventory_roots=(synthetic_inventory["backup_root"],),
            canonical_evidence_roots=scope.canonical_evidence_roots,
        )
    with pytest.raises(retention.RetentionPlanError, match="^retention_scope_mismatch$"):
        retention.build_eligible_retention_plan(
            activation_journal=synthetic_inventory["activation_journal"],
            unit_journal=synthetic_inventory["unit_journal"],
            backup_root=synthetic_inventory["backup_root"],
            inventory_roots=(synthetic_inventory["inventory"],),
            backup_inventory_roots=(synthetic_inventory["backup_root"],),
            canonical_evidence_roots=(
                retention.CanonicalEvidenceRoot(
                    path=synthetic_inventory["evidence_root"],
                    authority_path=synthetic_inventory["evidence_authority"],
                    authority_sha256="f" * 64,
                ),
            ),
        )


def test_retention_scope_registry_requires_canonical_private_exact_file(
    synthetic_inventory: dict[str, Any],
) -> None:
    scope_path = synthetic_inventory["retention_scope"]
    scope_path.write_text(json.dumps(_scope_body(synthetic_inventory), indent=2), encoding="ascii")
    scope_path.chmod(0o600)
    with pytest.raises(retention.RetentionPlanError, match="^retention_scope_invalid$"):
        retention.load_retention_scope_authority(activation_journal=synthetic_inventory["activation_journal"])

    _write_scope(synthetic_inventory, _scope_body(synthetic_inventory))
    scope_path.chmod(0o640)
    with pytest.raises(retention.RetentionPlanError, match="^retention_scope_invalid$"):
        retention.load_retention_scope_authority(activation_journal=synthetic_inventory["activation_journal"])


def test_apply_rejects_retention_scope_inode_or_body_drift_before_mutation(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    plan_path = _plan_file(plan, tmp_path / "scope-plan.json")
    candidate_paths = tuple(
        Path(item["path"])
        for key in ("targets", "backup_targets")
        for item in plan[key]
        if item["decision"] == "delete_candidate"
    )
    original = synthetic_inventory["retention_scope"].read_bytes()
    replacement = synthetic_inventory["retention_scope"].with_suffix(".replacement")
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    os.replace(replacement, synthetic_inventory["retention_scope"])

    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_retention_scope_changed$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert all(path.exists() for path in candidate_paths)

    body = _scope_body(synthetic_inventory)
    body["backup_root"] = str(tmp_path / "different")
    _write_scope(synthetic_inventory, body)
    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_retention_scope_changed$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert all(path.exists() for path in candidate_paths)


def test_eligible_cli_builds_code_owned_authority_and_classifies_exact_backup(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    backups = {Path(item["path"]).name: item for item in plan["backup_targets"]}

    assert plan["mode"] == "eligible_classification"
    assert plan["apply_authority"] is True
    assert backups["legacy-unpinned"]["decision"] == "delete_candidate"
    assert backups["legacy-unpinned"]["reason"] == "retirable_authenticated_backup"
    assert {item["role"] for item in plan["authority_bindings"]["dr_pins"]} >= {
        "current",
        "older",
    }

    argv = [
        "--activation-journal",
        str(synthetic_inventory["activation_journal"]),
        "--unit-journal",
        str(synthetic_inventory["unit_journal"]),
        "--backup-root",
        str(synthetic_inventory["backup_root"]),
        "--inventory-root",
        str(synthetic_inventory["inventory"]),
        "--backup-inventory-root",
        str(synthetic_inventory["backup_root"]),
        "--eligible",
        "--evidence-authority",
        str(synthetic_inventory["evidence_root"]),
        str(synthetic_inventory["evidence_authority"]),
        _sha256_file(synthetic_inventory["evidence_authority"]),
    ]
    assert retention.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["plan_sha256"] == plan["plan_sha256"]


def test_privileged_target_probe_marks_only_exact_referenced_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "body").write_bytes(b"first")
    (second / "body").write_bytes(b"second")

    def observe(index: retention.proc_probe.TargetIndex) -> tuple[dict[str, Any], str]:
        target_id = next(target.target_id for target in index.targets if target.roots == (second,))
        core = {
            "authority": "code_owned_privileged_host_proc_v1",
            "implementation_sha256": "1" * 64,
            "observation_sha256": "2" * 64,
            "observer_euid": 0,
            "process_epoch_sha256": "3" * 64,
            "referenced_target_ids": [target_id],
            "schema": retention.proc_probe.PRIVILEGED_RECEIPT_SCHEMA,
            "scope_identity_sha256": "4" * 64,
            "status": "referenced",
            "target_count": len(index.targets),
            "target_index_sha256": index.sha256,
            "task_count": 2,
            "tgid_count": 2,
        }
        receipt = {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
        return receipt, "5" * 64

    monkeypatch.setattr(retention, "_run_privileged_target_probe", observe)
    inventory = retention.build_complete_open_inventory(target_paths=(first, second))

    assert inventory.source == "code_owned_privileged_target_diagnostic_v1"
    assert inventory.open_paths == (second,)
    assert len(inventory.target_index_sha256) == 64
    assert len(inventory.authority_sha256) == 64


def test_candidate_probe_scope_does_not_index_more_than_a_million_retained_unknowns(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = synthetic_inventory["old"].identity.root
    calls = 0
    observed_probe_paths: tuple[Path, ...] = ()

    def plans(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if kwargs.get("_scope_seed") is True:

            def retained_then_candidate():
                for _ in range(1_000_001):
                    yield {"decision": "retain", "path": "/retained/unknown"}
                yield {"decision": "delete_candidate", "path": str(candidate)}

            return {
                "classification_status": "scope_seed",
                "block_reason": "",
                "plan_sha256": "a" * 64,
                "targets": retained_then_candidate(),
                "backup_targets": (),
            }
        return {
            "classification_status": "eligible",
            "apply_authority": True,
            "block_reason": "",
            "targets": ({"decision": "delete_candidate", "path": str(candidate)},),
            "backup_targets": (),
        }

    def inventory(*, target_paths: tuple[Path, ...]) -> retention.OpenInventorySnapshot:
        nonlocal observed_probe_paths
        observed_probe_paths = target_paths
        return retention.OpenInventorySnapshot(source="code_owned_fd_inventory_v1", complete=True)

    monkeypatch.setattr(retention, "plan_release_artifact_retention", plans)
    monkeypatch.setattr(retention, "build_complete_open_inventory", inventory)
    result = retention.build_eligible_retention_plan(
        activation_journal=synthetic_inventory["activation_journal"],
        unit_journal=synthetic_inventory["unit_journal"],
        backup_root=synthetic_inventory["backup_root"],
        inventory_roots=(synthetic_inventory["inventory"],),
        backup_inventory_roots=(synthetic_inventory["backup_root"],),
        canonical_evidence_roots=(
            retention.CanonicalEvidenceRoot(
                path=synthetic_inventory["evidence_root"],
                authority_path=synthetic_inventory["evidence_authority"],
                authority_sha256=_sha256_file(synthetic_inventory["evidence_authority"]),
            ),
        ),
    )

    assert result["classification_status"] == "eligible"
    assert calls == 2
    assert observed_probe_paths == (candidate,)


def test_zero_candidate_scope_cannot_race_into_unprobed_delete_candidate(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = synthetic_inventory["old"].identity.root

    def plans(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("_scope_seed") is True:
            return {
                "classification_status": "scope_seed",
                "block_reason": "",
                "plan_sha256": "a" * 64,
                "targets": ({"decision": "retain", "path": str(candidate)},),
                "backup_targets": (),
            }
        return {
            "classification_status": "eligible",
            "apply_authority": True,
            "block_reason": "",
            "targets": ({"decision": "delete_candidate", "path": str(candidate)},),
            "backup_targets": (),
        }

    monkeypatch.setattr(retention, "plan_release_artifact_retention", plans)
    with pytest.raises(retention.RetentionPlanError, match="^open_state_ambiguous$"):
        retention.build_eligible_retention_plan(
            activation_journal=synthetic_inventory["activation_journal"],
            unit_journal=synthetic_inventory["unit_journal"],
            backup_root=synthetic_inventory["backup_root"],
            inventory_roots=(synthetic_inventory["inventory"],),
            backup_inventory_roots=(synthetic_inventory["backup_root"],),
            canonical_evidence_roots=(
                retention.CanonicalEvidenceRoot(
                    path=synthetic_inventory["evidence_root"],
                    authority_path=synthetic_inventory["evidence_authority"],
                    authority_sha256=_sha256_file(synthetic_inventory["evidence_authority"]),
                ),
            ),
        )


def test_explicit_reviewed_scratch_requires_exact_symlink_free_non_git_tree(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = synthetic_inventory["inventory"] / "operator-reviewed-build"
    scratch.mkdir()
    (scratch / "wheel.whl").write_bytes(b"exact disposable bytes")
    observation = retention._observe_target(scratch)  # noqa: SLF001
    assert observation.inventory_sha256 is not None
    runtime_parent = synthetic_inventory["activation_journal"].parent.parent / "operator-runtime-2"
    runtime_parent.mkdir(mode=0o1700)
    monkeypatch.setattr(operator.OperatorTransactionLock, "_RUNTIME_PARENT", runtime_parent)
    monkeypatch.setattr(
        retention,
        "build_complete_open_inventory",
        lambda **_kwargs: retention.OpenInventorySnapshot(
            source="code_owned_fd_inventory_v1",
            complete=True,
        ),
    )
    _enable_complete_delete_evidence(monkeypatch)
    plan = retention.build_eligible_retention_plan(
        activation_journal=synthetic_inventory["activation_journal"],
        unit_journal=synthetic_inventory["unit_journal"],
        backup_root=synthetic_inventory["backup_root"],
        inventory_roots=(synthetic_inventory["inventory"],),
        backup_inventory_roots=(synthetic_inventory["backup_root"],),
        canonical_evidence_roots=(
            retention.CanonicalEvidenceRoot(
                path=synthetic_inventory["evidence_root"],
                authority_path=synthetic_inventory["evidence_authority"],
                authority_sha256=_sha256_file(synthetic_inventory["evidence_authority"]),
            ),
        ),
        reviewed_scratch_targets=(retention.ReviewedScratchTarget(scratch, observation.inventory_sha256),),
    )
    target = next(item for item in plan["targets"] if item["path"] == str(scratch))
    assert target["decision"] == "delete_candidate"
    assert target["reason"] == "retirable_reviewed_scratch"

    nested = scratch / "nested"
    nested.mkdir()
    (nested / "link").symlink_to(scratch / "wheel.whl")
    changed = retention._observe_target(scratch)  # noqa: SLF001
    assert changed.has_symlink
    assert changed.inventory_sha256 is not None
    blocked = retention.plan_release_artifact_retention(
        activation_journal=synthetic_inventory["activation_journal"],
        unit_journal=synthetic_inventory["unit_journal"],
        backup_root=synthetic_inventory["backup_root"],
        inventory_roots=(synthetic_inventory["inventory"],),
        backup_inventory_roots=(synthetic_inventory["backup_root"],),
        reviewed_scratch_targets=(retention.ReviewedScratchTarget(scratch, changed.inventory_sha256),),
        open_inventory=retention.OpenInventorySnapshot(source="synthetic_test", complete=True),
        authority_bindings=_bindings(synthetic_inventory),
        executable=True,
    )
    changed_target = next(item for item in blocked["targets"] if item["path"] == str(scratch))
    assert changed_target["decision"] == "retain"
    assert changed_target["reason"] == "symlink_artifact"

    (nested / "link").unlink()
    nested.rmdir()
    (scratch / ".git").write_text("gitdir: /review-required\n", encoding="ascii")
    git_observation = retention._observe_target(scratch)  # noqa: SLF001
    assert git_observation.inventory_sha256 is not None
    git_blocked = retention.plan_release_artifact_retention(
        activation_journal=synthetic_inventory["activation_journal"],
        unit_journal=synthetic_inventory["unit_journal"],
        backup_root=synthetic_inventory["backup_root"],
        inventory_roots=(synthetic_inventory["inventory"],),
        backup_inventory_roots=(synthetic_inventory["backup_root"],),
        reviewed_scratch_targets=(
            retention.ReviewedScratchTarget(scratch, git_observation.inventory_sha256),
        ),
        open_inventory=retention.OpenInventorySnapshot(source="synthetic_test", complete=True),
        authority_bindings=_bindings(synthetic_inventory),
        executable=True,
    )
    git_target = next(item for item in git_blocked["targets"] if item["path"] == str(scratch))
    assert git_target["decision"] == "retain"
    assert git_target["reason"] == "reviewed_scratch_invalid"


def test_nested_expected_name_symlink_is_never_backup_or_apply_candidate(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = synthetic_inventory["backup_root"] / "legacy-unpinned"
    database = backup / "database.sqlite3"
    outside = backup.parent / "outside.sqlite3"
    database.replace(outside)
    database.symlink_to(outside)

    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    record = next(item for item in plan["backup_targets"] if item["path"] == str(backup))
    assert record["decision"] == "retain"
    assert record["reason"] == "symlink_artifact"

    old = next(item for item in plan["targets"] if Path(item["path"]).name == "old")
    old_root = Path(old["path"])
    metadata = old_root / "artifacts/immutable-release.json"
    held = old_root / "artifacts/immutable-release.held"
    metadata.replace(held)
    metadata.symlink_to(held)
    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_target_raced$",
    ):
        retention_apply._candidate_matches_observation(old, old_root)  # noqa: SLF001


def test_immutable_inode_flags_make_a_tree_unsafe_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _private_directory(tmp_path / "candidate")
    (candidate / "body.bin").write_bytes(b"body")
    assert not retention._snapshot(candidate).has_special  # noqa: SLF001

    monkeypatch.setattr(
        retention,
        "_descriptor_inode_flags",
        lambda _descriptor: retention._FS_IMMUTABLE_FL,  # noqa: SLF001
    )
    immutable = retention._snapshot(candidate)  # noqa: SLF001
    assert immutable.has_special
    assert all(record[-1] == retention._FS_IMMUTABLE_FL for record in immutable.records)  # noqa: SLF001


def test_reviewed_scratch_apply_resumes_after_rename_crash(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch = synthetic_inventory["inventory"] / "reviewed-wheel-scratch"
    scratch.mkdir()
    (scratch / "friday.whl").write_bytes(b"reviewed")
    observation = retention._observe_target(scratch)  # noqa: SLF001
    assert observation.inventory_sha256 is not None
    runtime_parent = synthetic_inventory["activation_journal"].parent.parent / "operator-runtime-3"
    runtime_parent.mkdir(mode=0o1700)
    monkeypatch.setattr(operator.OperatorTransactionLock, "_RUNTIME_PARENT", runtime_parent)
    monkeypatch.setattr(
        retention,
        "build_complete_open_inventory",
        lambda **_kwargs: retention.OpenInventorySnapshot(
            source="code_owned_fd_inventory_v1",
            complete=True,
        ),
    )
    _enable_complete_delete_evidence(monkeypatch)
    plan = retention.build_eligible_retention_plan(
        activation_journal=synthetic_inventory["activation_journal"],
        unit_journal=synthetic_inventory["unit_journal"],
        backup_root=synthetic_inventory["backup_root"],
        inventory_roots=(synthetic_inventory["inventory"],),
        backup_inventory_roots=(synthetic_inventory["backup_root"],),
        canonical_evidence_roots=(
            retention.CanonicalEvidenceRoot(
                path=synthetic_inventory["evidence_root"],
                authority_path=synthetic_inventory["evidence_authority"],
                authority_sha256=_sha256_file(synthetic_inventory["evidence_authority"]),
            ),
        ),
        reviewed_scratch_targets=(retention.ReviewedScratchTarget(scratch, observation.inventory_sha256),),
    )
    plan_path = _plan_file(plan, tmp_path / "scratch-plan.json")
    crashed = False

    def crash(point: str) -> None:
        nonlocal crashed
        if point == "after_rename" and not crashed:
            crashed = True
            raise retention_apply._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(retention_apply, "_fault", crash)
    with pytest.raises(retention_apply._InjectedCrash):  # noqa: SLF001
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )
    monkeypatch.setattr(retention_apply, "_fault", lambda _point: None)
    receipt = retention_apply.apply_retention_plan(
        plan_path=plan_path,
        expected_plan_sha256=plan["plan_sha256"],
    )

    assert receipt["status"] == "applied"
    assert not scratch.exists()


def test_exact_backup_discovery_streams_large_and_zero_members_with_bounded_memory(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "large-exact-backup"
    record = _exact_backup(backup, 90)
    database = backup / "database.sqlite3"
    inbox = backup / "inbox.sqlite3"
    with database.open("r+b") as stream:
        stream.truncate(64 << 20)
    with inbox.open("r+b") as stream:
        stream.truncate(0)

    def stream_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()

    files = []
    for item in record["files"]:
        path = backup / item["name"]
        files.append(
            {
                "name": item["name"],
                "sha256": stream_digest(path),
                "size": path.stat().st_size,
            }
        )
    manifest = {
        "database_schema": 50,
        "files": sorted(files, key=lambda item: item["name"]),
        "schema": "friday.immutable-cutover-exact-backup.v1",
    }
    (backup / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    (backup / "manifest.json").chmod(0o600)

    tracemalloc.start()
    discovered = retention._discover_exact_backup(backup)  # noqa: SLF001
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert discovered is not None and discovered.get("invalid") is not True
    assert peak < 8 << 20


def test_snapshot_keeps_logical_and_allocated_sparse_bytes_distinct(tmp_path: Path) -> None:
    root = _private_directory(tmp_path / "sparse")
    sparse = root / "large.bin"
    with sparse.open("wb") as stream:
        stream.truncate(128 << 20)

    snapshot = retention._snapshot(root)  # noqa: SLF001
    assert snapshot.total_bytes == 128 << 20
    assert 0 <= snapshot.total_allocated_bytes < snapshot.total_bytes


def _private_legacy_worktree(tmp_path: Path, inventory: Path) -> tuple[Path, Path]:
    repository = tmp_path / "legacy-repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "friday@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Friday"],
        check=True,
    )
    (repository / "tracked.txt").write_text("legacy\n", encoding="ascii")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "legacy"], check=True)
    worktree = inventory / "legacy-worktree"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
    for root in (repository / ".git", worktree):
        for parent, directories, files in os.walk(root):
            Path(parent).chmod(0o700)
            for name in directories:
                (Path(parent) / name).chmod(0o700)
            for name in files:
                (Path(parent) / name).chmod(0o600)
    git_dir = Path((worktree / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
    return worktree, git_dir


def test_registered_detached_clean_legacy_worktree_is_eligible_only_when_exact(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree, _git_dir = _private_legacy_worktree(
        tmp_path,
        synthetic_inventory["inventory"],
    )

    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    target = next(item for item in plan["targets"] if item["path"] == str(worktree))
    assert target["decision"] == "retain"
    assert target["reason"] == "registered_legacy_requires_secondary_root"

    (worktree / "tracked.txt").write_text("dirty\n", encoding="ascii")
    blocked = _eligible_plan(synthetic_inventory, monkeypatch)
    dirty = next(item for item in blocked["targets"] if item["path"] == str(worktree))
    assert dirty["decision"] == "retain"


def test_apply_rejects_unreviewed_digest_before_any_mutation(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    plan_path = _plan_file(plan, tmp_path / "retention-plan.json")

    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_plan_digest_mismatch$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256="f" * 64,
        )

    assert synthetic_inventory["old"].identity.root.exists()
    assert not (
        synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_JOURNAL_NAME
    ).exists()


@pytest.mark.parametrize("launch", ("direct", "module"))
def test_apply_cli_failure_is_import_safe_canonical_and_body_free(
    tmp_path: Path,
    launch: str,
) -> None:
    repository = Path(retention_apply.__file__).resolve().parents[1]
    tool = repository / "tools/release_artifact_retention_operator.py"
    private_plan = tmp_path / "private-plan-path.json"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    if launch == "direct":
        command = [sys.executable, str(tool)]
    else:
        environment["PYTHONPATH"] = str(repository)
        command = [sys.executable, "-m", "tools.release_artifact_retention_operator"]

    completed = subprocess.run(  # noqa: S603
        [
            *command,
            "apply",
            "--plan",
            str(private_plan),
            "--expected-plan-sha256",
            "a" * 64,
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        timeout=10,
    )
    expected = {
        "failure_code": "retention_apply_plan_invalid",
        "schema": retention_apply.APPLY_RECEIPT_SCHEMA,
        "status": "failed_closed",
    }

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == _canonical(expected) + b"\n"
    assert b"Traceback" not in completed.stderr
    assert os.fsencode(private_plan) not in completed.stderr


@pytest.mark.parametrize(
    "crash_point",
    (
        "before_rename",
        "after_rename",
        "before_delete",
        "after_delete",
        "during_delete",
        "before_receipt_publish",
        "after_receipt_publish",
    ),
)
def test_apply_crash_boundaries_resume_idempotently_and_reauthenticate(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_point: str,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    plan_path = _plan_file(plan, tmp_path / "retention-plan.json")
    calls = 0

    def crash(point: str) -> None:
        nonlocal calls
        if crash_point == "during_delete":
            if point != "before_unlink_entry":
                return
            calls += 1
            if calls != 3:
                return
        elif point != crash_point:
            return
        raise retention_apply._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(retention_apply, "_fault", crash)
    with pytest.raises(retention_apply._InjectedCrash):  # noqa: SLF001
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )

    monkeypatch.setattr(retention_apply, "_fault", lambda _point: None)
    receipt = retention_apply.apply_retention_plan(
        plan_path=plan_path,
        expected_plan_sha256=plan["plan_sha256"],
    )
    repeated = retention_apply.apply_retention_plan(
        plan_path=plan_path,
        expected_plan_sha256=plan["plan_sha256"],
    )

    assert receipt == repeated
    assert receipt["status"] == "applied"
    assert receipt["post_apply_reauthenticated"] is True
    assert receipt["pre_delete_authenticated_allocated_bytes"] >= 0
    assert isinstance(receipt["statvfs_available_delta_bytes"], int)
    assert receipt["allocated_bytes_are_not_exact_physical_attribution"] is True
    assert receipt["deleted_candidate_count"] == 2
    assert receipt["retention_scope_schema"] == retention.RETENTION_SCOPE_SCHEMA
    assert receipt["retention_scope_sha256"] == plan["retention_scope"]["file_sha256"]
    assert not synthetic_inventory["old"].identity.root.exists()
    assert not (synthetic_inventory["backup_root"] / "legacy-unpinned").exists()
    assert not list(synthetic_inventory["inventory"].glob(".friday-retention-q-v1-*"))
    assert not list(synthetic_inventory["backup_root"].glob(".friday-retention-q-v1-*"))
    receipt_files = list(
        (synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_RECEIPT_DIRECTORY).glob(
            "receipt-*.json"
        )
    )
    assert len(receipt_files) == 1
    assert stat_mode(receipt_files[0]) == 0o400
    assert not list(
        (synthetic_inventory["activation_journal"].parent / retention_apply.OBJECT_AUTHORITY_DIRECTORY).glob(
            "objects-*.bin"
        )
    )
    journal = json.loads(
        (synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_JOURNAL_NAME).read_text(
            encoding="ascii"
        )
    )
    assert journal["retention_scope_schema"] == retention.RETENTION_SCOPE_SCHEMA
    assert journal["retention_scope_sha256"] == plan["retention_scope"]["file_sha256"]


def test_post_rename_open_reference_restores_exact_source(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    plan_path = _plan_file(plan, tmp_path / "retention-plan.json")
    ordinary_inventory = retention.build_complete_open_inventory
    quarantine_probes: list[tuple[Path, ...]] = []

    def referenced_quarantine(*, target_paths: tuple[Path, ...]) -> retention.OpenInventorySnapshot:
        if any(path.name.startswith(".friday-retention-q-v1-") for path in target_paths):
            quarantine_probes.append(target_paths)
            return retention.OpenInventorySnapshot(
                source="code_owned_fd_inventory_v1",
                complete=True,
                open_paths=target_paths,
            )
        return ordinary_inventory(target_paths=target_paths)

    monkeypatch.setattr(retention, "build_complete_open_inventory", referenced_quarantine)

    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_open_reference$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert len(quarantine_probes) == 1
    assert len(quarantine_probes[0]) == 2
    assert synthetic_inventory["old"].identity.root.is_dir()
    assert (synthetic_inventory["backup_root"] / "legacy-unpinned").is_dir()
    assert not list(synthetic_inventory["inventory"].glob(".friday-retention-q-v1-*"))
    assert not list(synthetic_inventory["backup_root"].glob(".friday-retention-q-v1-*"))


def test_partial_delete_resume_rejects_inode_laundering_against_immutable_object_authority(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    plan_path = _plan_file(plan, tmp_path / "retention-plan.json")
    unlink_boundaries = 0

    def crash(point: str) -> None:
        nonlocal unlink_boundaries
        if point != "before_unlink_entry":
            return
        unlink_boundaries += 1
        if unlink_boundaries == 2:
            raise retention_apply._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(retention_apply, "_fault", crash)
    with pytest.raises(retention_apply._InjectedCrash):  # noqa: SLF001
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )

    journal = json.loads(
        (synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_JOURNAL_NAME).read_text(
            encoding="ascii"
        )
    )
    candidate_by_sha = {
        hashlib.sha256(_canonical(item)).hexdigest(): item
        for key in ("targets", "backup_targets")
        for item in plan[key]
        if item["decision"] == "delete_candidate"
    }
    partial, candidate, quarantine = next(
        (entry, item, path)
        for entry in journal["entries"]
        if entry["status"] == "deleting"
        for item in (candidate_by_sha[entry["candidate_sha256"]],)
        for path in (Path(item["path"]).parent / entry["quarantine_name"],)
        if path.exists() and retention._snapshot(path).entry_count < item["entry_count"]  # noqa: SLF001
    )
    remaining = next(path for path in sorted(quarantine.rglob("*")) if path.is_file())
    body = remaining.read_bytes()
    mode = stat_mode(remaining)
    remaining.unlink()
    remaining.write_bytes(body)
    remaining.chmod(mode)

    monkeypatch.setattr(retention_apply, "_fault", lambda _point: None)
    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_partial_state_invalid$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert quarantine.is_dir()
    assert remaining.is_file()


def test_registered_legacy_worktree_is_not_an_apply_mutation_root(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worktree, git_dir = _private_legacy_worktree(
        tmp_path,
        synthetic_inventory["inventory"],
    )
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    target = next(item for item in plan["targets"] if item["path"] == str(worktree))

    assert target["decision"] == "retain"
    assert target["reason"] == "registered_legacy_requires_secondary_root"
    assert worktree.is_dir()
    assert git_dir.is_dir()


def test_durable_plan_and_receipt_partial_stages_recover_without_weakening_bound_state(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "state")
    plan = {"plan_sha256": "a" * 64, "value": 1}
    plan_directory = _private_directory(state / retention_apply.APPLY_PLAN_DIRECTORY)
    plan_stage = plan_directory / f".plan-{'a' * 64}.json.new"
    plan_stage.write_bytes((_canonical(plan) + b"\n")[:17])
    plan_stage.chmod(0o600)

    durable = retention_apply._persist_reviewed_plan(  # noqa: SLF001
        state,
        plan,
        guard=lambda: None,
        allow_incomplete_stage_repair=True,
    )
    assert durable[0].read_bytes() == _canonical(plan) + b"\n"
    assert stat_mode(durable[0]) == 0o400

    blocked_plan = {"plan_sha256": "b" * 64, "value": 2}
    blocked_stage = plan_directory / f".plan-{'b' * 64}.json.new"
    blocked_stage.write_bytes(b"partial")
    blocked_stage.chmod(0o600)
    with pytest.raises(retention_apply.RetentionApplyError, match="retention_apply_plan_changed"):
        retention_apply._persist_reviewed_plan(  # noqa: SLF001
            state,
            blocked_plan,
            guard=lambda: None,
            allow_incomplete_stage_repair=False,
        )
    assert blocked_stage.read_bytes() == b"partial"

    receipt_core = {
        "schema": retention_apply.APPLY_RECEIPT_SCHEMA,
        "status": "applied",
        "transaction_id": "c" * 64,
    }
    receipt = retention_apply._receipt_with_digest(receipt_core)  # noqa: SLF001
    receipt_directory = _private_directory(state / retention_apply.APPLY_RECEIPT_DIRECTORY)
    receipt_stage = receipt_directory / f".receipt-{'c' * 64}.json.new"
    receipt_stage.write_bytes((_canonical(receipt) + b"\n")[:11])
    receipt_stage.chmod(0o600)

    assert (
        retention_apply._publish_receipt(  # noqa: SLF001
            state,
            receipt,
            guard=lambda: None,
        )
        == receipt
    )
    published = receipt_directory / f"receipt-{'c' * 64}.json"
    assert published.read_bytes() == _canonical(receipt) + b"\n"
    assert stat_mode(published) == 0o400


def test_rename_and_restore_never_replace_an_unreviewed_racing_destination(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    candidates = retention_apply._candidate_records(plan)  # noqa: SLF001
    first = candidates[0]
    plan_path = _plan_file(plan, tmp_path / "retention-plan.json")
    original_rename = retention_apply._rename_noreplace  # noqa: SLF001
    injected = False

    def race(source_fd: int, source: str, target_fd: int, target: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.mkdir(target, 0o700, dir_fd=target_fd)
        original_rename(source_fd, source, target_fd, target)

    monkeypatch.setattr(retention_apply, "_rename_noreplace", race)
    with pytest.raises(retention_apply.RetentionApplyError, match="retention_apply_target_raced"):
        retention_apply.apply_retention_plan(
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert Path(first["path"]).is_dir()

    monkeypatch.setattr(retention_apply, "_rename_noreplace", original_rename)
    quarantine_name = ".friday-retention-q-v1-restore-race"
    source = Path(first["path"])
    os.rename(source, source.parent / quarantine_name)
    injected = False
    monkeypatch.setattr(retention_apply, "_rename_noreplace", race)
    with pytest.raises(retention_apply.RetentionApplyError, match="retention_apply_restore_blocked"):
        retention_apply._restore_quarantine(  # noqa: SLF001
            first,
            quarantine_name,
            guard=lambda: None,
        )
    assert source.is_dir()
    assert (source.parent / quarantine_name).is_dir()


def test_immutable_receipts_survive_next_plan_journal_reuse(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _eligible_plan(synthetic_inventory, monkeypatch)
    first_path = _plan_file(first, tmp_path / "first-plan.json")
    first_receipt = retention_apply.apply_retention_plan(
        plan_path=first_path,
        expected_plan_sha256=first["plan_sha256"],
    )
    receipt_directory = (
        synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_RECEIPT_DIRECTORY
    )
    first_file = receipt_directory / f"receipt-{first_receipt['transaction_id']}.json"
    first_raw = first_file.read_bytes()

    second = _eligible_plan(synthetic_inventory, monkeypatch)
    assert second["plan_sha256"] != first["plan_sha256"]
    second_path = _plan_file(second, tmp_path / "second-plan.json")
    second_receipt = retention_apply.apply_retention_plan(
        plan_path=second_path,
        expected_plan_sha256=second["plan_sha256"],
    )

    assert second_receipt["transaction_id"] != first_receipt["transaction_id"]
    assert first_file.read_bytes() == first_raw
    assert len(list(receipt_directory.glob("receipt-*.json"))) == 2


def test_rollover_cleans_only_exact_prior_residual_authority_after_cleanup_crash(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _eligible_plan(synthetic_inventory, monkeypatch)
    first_path = _plan_file(first, tmp_path / "first-plan.json")

    def crash(point: str) -> None:
        if point == "after_applied_journal_before_cleanup":
            raise retention_apply._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(retention_apply, "_fault", crash)
    with pytest.raises(retention_apply._InjectedCrash):  # noqa: SLF001
        retention_apply.apply_retention_plan(
            plan_path=first_path,
            expected_plan_sha256=first["plan_sha256"],
        )

    state = synthetic_inventory["activation_journal"].parent
    authority_directory = state / retention_apply.OBJECT_AUTHORITY_DIRECTORY
    prior_manifests = tuple(authority_directory.glob("objects-*.bin"))
    assert prior_manifests
    unrelated = authority_directory / "unrelated-immutable.bin"
    unrelated.write_bytes(b"unrelated\n")
    unrelated.chmod(0o400)

    monkeypatch.setattr(retention_apply, "_fault", lambda _point: None)
    second = _eligible_plan(synthetic_inventory, monkeypatch)
    assert second["plan_sha256"] != first["plan_sha256"]
    second_path = _plan_file(second, tmp_path / "second-plan.json")
    receipt = retention_apply.apply_retention_plan(
        plan_path=second_path,
        expected_plan_sha256=second["plan_sha256"],
    )

    assert receipt["status"] == "applied"
    assert all(not path.exists() for path in prior_manifests)
    assert unrelated.read_bytes() == b"unrelated\n"
    assert stat_mode(unrelated) == 0o400


def test_plan_namespace_and_preexisting_quarantine_are_rejected_before_mutation(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    inside = _plan_file(plan, synthetic_inventory["old"].identity.root / "reviewed-plan.json")
    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_plan_namespace_invalid$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=inside,
            expected_plan_sha256=plan["plan_sha256"],
        )
    inside.unlink()

    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    outside = _plan_file(plan, tmp_path / "outside-plan.json")
    candidates = retention_apply._candidate_records(plan)  # noqa: SLF001
    initial = retention_apply._new_journal(  # noqa: SLF001
        plan,
        candidates,
        durable_plan=(tmp_path / "durable-plan.json", 1, 1),
        filesystem_before=[],
    )
    entry = initial["entries"][0]
    collision = Path(candidates[0]["path"]).parent / entry["quarantine_name"]
    collision.mkdir(mode=0o700)
    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_quarantine_collision$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=outside,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert synthetic_inventory["old"].identity.root.exists()
    assert not (
        synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_JOURNAL_NAME
    ).exists()


@pytest.mark.parametrize("crash_point", ("before_rename", "after_rename"))
def test_resume_rejects_plan_relocated_inside_pending_mutation_namespace(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_point: str,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    original = _plan_file(plan, tmp_path / "reviewed-plan.json")

    def crash(point: str) -> None:
        if point == crash_point:
            raise retention_apply._InjectedCrash  # noqa: SLF001

    monkeypatch.setattr(retention_apply, "_fault", crash)
    with pytest.raises(retention_apply._InjectedCrash):  # noqa: SLF001
        retention_apply.apply_retention_plan(
            plan_path=original,
            expected_plan_sha256=plan["plan_sha256"],
        )

    journal_path = synthetic_inventory["activation_journal"].parent / retention_apply.APPLY_JOURNAL_NAME
    journal_before = journal_path.read_bytes()
    journal = json.loads(journal_before)
    candidates = retention_apply._candidate_records(plan)  # noqa: SLF001
    if crash_point == "before_rename":
        index = next(
            index
            for index, entry in enumerate(journal["entries"])
            if entry["status"] == "renaming" and Path(candidates[index]["path"]).exists()
        )
        relocation_root = Path(candidates[index]["path"])
    else:
        index = next(index for index, entry in enumerate(journal["entries"]) if entry["status"] == "renaming")
        source = Path(candidates[index]["path"])
        relocation_root = source.parent / journal["entries"][index]["quarantine_name"]
    relocated = relocation_root / "copied-reviewed-plan.json"
    relocated.write_bytes(original.read_bytes())
    relocated.chmod(0o600)

    monkeypatch.setattr(retention_apply, "_fault", lambda _point: None)
    with pytest.raises(
        retention_apply.RetentionApplyError,
        match="^retention_apply_plan_namespace_invalid$",
    ):
        retention_apply.apply_retention_plan(
            plan_path=relocated,
            expected_plan_sha256=plan["plan_sha256"],
        )

    assert relocated.exists()
    assert journal_path.read_bytes() == journal_before


@pytest.mark.parametrize("surface", ("runtime", "state", "local"))
def test_operator_lock_displacement_stops_before_candidate_mutation(
    synthetic_inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    plan = _eligible_plan(synthetic_inventory, monkeypatch)
    plan_path = _plan_file(plan, tmp_path / "retention-plan.json")
    state = synthetic_inventory["activation_journal"].parent
    runtime_parent = operator.OperatorTransactionLock._RUNTIME_PARENT  # noqa: SLF001
    displaced = state.with_name(f"{state.name}-displaced")

    def displace(point: str) -> None:
        if point != "before_rename":
            return
        monkeypatch.setattr(retention_apply, "_fault", lambda _point: None)
        if surface == "runtime":
            lock_path = (
                runtime_parent
                / f"friday-immutable-release-operator-v1-{os.geteuid()}"
                / "friday-immutable-release-operator-global-v1.lock"
            )
            lock_path.unlink()
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)
        elif surface == "local":
            lock_path = state / "immutable-release-operator.v1.lock"
            lock_path.unlink()
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)
        else:
            state.rename(displaced)
            state.mkdir(mode=0o700)

    monkeypatch.setattr(retention_apply, "_fault", displace)
    try:
        with pytest.raises(
            retention_apply.RetentionApplyError,
            match="^retention_apply_operator_lock_failed$",
        ):
            retention_apply.apply_retention_plan(
                plan_path=plan_path,
                expected_plan_sha256=plan["plan_sha256"],
            )
    finally:
        if surface == "state" and displaced.exists():
            state.rmdir()
            displaced.rename(state)

    assert synthetic_inventory["old"].identity.root.exists()
    assert not list(synthetic_inventory["inventory"].glob(".friday-retention-q-v1-*"))
