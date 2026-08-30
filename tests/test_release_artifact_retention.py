from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tools import immutable_release_operator as operator
from tools import release_artifact_retention as retention


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
    return {
        "directory": str(root),
        "files": files,
        "inbox_receipt_sha256": hashlib.sha256(
            _canonical([item for item in files if str(item["name"]).startswith("inbox")])
        ).hexdigest(),
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
    inventory = _private_directory(tmp_path / "inventory")
    backup_root = _private_directory(tmp_path / "backups")
    state = _private_directory(tmp_path / "state")
    current = _release(inventory / "current", 1)
    previous = _release(inventory / "previous", 2)
    fallback = _release(inventory / "fallback", 3)
    old = _release(inventory / "old", 4)
    (current.identity.root / "internal-artifacts-link").symlink_to("artifacts", target_is_directory=True)
    (old.identity.root / "internal-artifacts-link").symlink_to("artifacts", target_is_directory=True)
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
    _write_journal(
        activation_journal,
        _activation_core(current, previous, fallback, backup=activation_backup),
    )
    _write_journal(unit_journal, _unit_core(current, previous))

    dr_index = state / "immutable-release-dr-generations.v1.json"
    dr_index.write_bytes(b'{"authenticated":true}\n')
    dr_index.chmod(0o600)
    dr_receipts = _private_directory(state / "dr-receipts")
    pins: list[retention.DRGenerationPin] = []
    for role, directory, ordinal in (
        ("current", Path(str(activation_backup["directory"])), 1),
        ("older", Path(str(older_backup["directory"])), 2),
    ):
        generation_id = hashlib.sha256(f"generation-{ordinal}".encode()).hexdigest()
        receipt = dr_receipts / f"{generation_id}.json"
        receipt.write_bytes(_canonical({"generation_id": generation_id}) + b"\n")
        receipt.chmod(0o400)
        pins.append(
            retention.DRGenerationPin(
                role=role,
                backup_directory=directory,
                generation_id=generation_id,
                receipt_path=receipt,
                receipt_sha256=_sha256_file(receipt),
            )
        )

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
        "dr_index": dr_index,
        "dr_pins": tuple(pins),
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
    return retention.RetentionAuthorityBindings(
        activation_journal_sha256=_sha256_file(fixture["activation_journal"]),
        unit_install_journal_sha256=_sha256_file(fixture["unit_journal"]),
        dr_index_path=fixture["dr_index"],
        dr_index_sha256=dr_index_sha256 or _sha256_file(fixture["dr_index"]),
        dr_pins=fixture["dr_pins"] if dr_pins is None else dr_pins,
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
    assert plan["inventory_roots"] == [
        {
            "path": str(synthetic_inventory["inventory"]),
            "device": os.stat(synthetic_inventory["inventory"]).st_dev,
            "inode": os.stat(synthetic_inventory["inventory"]).st_ino,
            "type": "directory",
            "nlink": os.stat(synthetic_inventory["inventory"]).st_nlink,
            "uid": os.geteuid(),
        }
    ]
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
            "type",
            "nlink",
            "recursive_bytes",
            "entry_count",
            "inventory_sha256",
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


def test_pending_dr_generation_is_exact_and_retained(
    synthetic_inventory: dict[str, Any],
) -> None:
    pending_directory = _exact_backup(synthetic_inventory["backup_root"] / "pending", 4)["directory"]
    generation_id = hashlib.sha256(b"pending-generation").hexdigest()
    receipt = synthetic_inventory["dr_index"].parent / "dr-receipts" / f"{generation_id}.json"
    receipt.write_bytes(_canonical({"generation_id": generation_id}) + b"\n")
    receipt.chmod(0o400)
    pending = retention.DRGenerationPin(
        role="pending",
        backup_directory=Path(str(pending_directory)),
        generation_id=generation_id,
        receipt_path=receipt,
        receipt_sha256=_sha256_file(receipt),
    )

    plan = _plan(
        synthetic_inventory,
        authority_bindings=_bindings(
            synthetic_inventory,
            dr_pins=(*synthetic_inventory["dr_pins"], pending),
        ),
    )

    target = next(item for item in plan["backup_targets"] if Path(item["path"]).name == "pending")
    assert target["reason"] == "dr_pending_backup"
    assert target["decision"] == "retain"


@pytest.mark.parametrize("forgery", ("dr_index", "dr_receipt", "evidence"))
def test_forged_authority_digest_blocks_every_delete_candidate(
    synthetic_inventory: dict[str, Any],
    forgery: str,
) -> None:
    bindings = _bindings(synthetic_inventory)
    if forgery == "dr_index":
        bindings = _bindings(synthetic_inventory, dr_index_sha256="f" * 64)
        expected = "dr_index_invalid"
    elif forgery == "evidence":
        bindings = _bindings(synthetic_inventory, evidence_sha256="f" * 64)
        expected = "canonical_evidence_invalid"
    else:
        current, older = synthetic_inventory["dr_pins"]
        forged = retention.DRGenerationPin(
            role=current.role,
            backup_directory=current.backup_directory,
            generation_id=current.generation_id,
            receipt_path=current.receipt_path,
            receipt_sha256="f" * 64,
        )
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
    current_directory = Path(
        str(_exact_backup(synthetic_inventory["backup_root"] / "dr-current", 5)["directory"])
    )
    previous_current, older = synthetic_inventory["dr_pins"]
    replacement = retention.DRGenerationPin(
        role="current",
        backup_directory=current_directory,
        generation_id=previous_current.generation_id,
        receipt_path=previous_current.receipt_path,
        receipt_sha256=previous_current.receipt_sha256,
    )

    plan = _plan(
        synthetic_inventory,
        authority_bindings=_bindings(synthetic_inventory, dr_pins=(replacement, older)),
    )

    target = next(item for item in plan["backup_targets"] if Path(item["path"]).name == "dr-current")
    assert target["reason"] == "dr_current_backup"
    assert target["decision"] == "retain"


def test_duplicate_and_overlapping_authority_inputs_are_rejected_exactly(
    synthetic_inventory: dict[str, Any],
) -> None:
    current, older = synthetic_inventory["dr_pins"]
    duplicate_role = retention.DRGenerationPin(
        role="current",
        backup_directory=older.backup_directory,
        generation_id=older.generation_id,
        receipt_path=older.receipt_path,
        receipt_sha256=older.receipt_sha256,
    )
    with pytest.raises(retention.RetentionPlanError, match="dr_pins_invalid"):
        _plan(
            synthetic_inventory,
            authority_bindings=_bindings(
                synthetic_inventory,
                dr_pins=(current, duplicate_role, older),
            ),
        )

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
