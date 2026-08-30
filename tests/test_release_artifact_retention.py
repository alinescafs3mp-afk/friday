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
) -> dict[str, Any]:
    return {
        "schema": operator.ACTIVATION_JOURNAL_SCHEMA,
        "transaction_id": "1" * 64,
        "phase": phase,
        "config_identity_sha256": "2" * 64,
        "candidate": current.record,
        "previous": previous.record,
        "fallback": fallback.record,
        "backup": None,
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

    activation_journal = state / "immutable-release-activation.v1.json"
    unit_journal = state / "immutable-release-unit-install.v1.json"
    _write_journal(activation_journal, _activation_core(current, previous, fallback))
    _write_journal(unit_journal, _unit_core(current, previous))

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
        "current": current,
        "previous": previous,
        "fallback": fallback,
        "old": old,
        "calls": calls,
    }


def _plan(
    fixture: dict[str, Any],
    *,
    open_paths: tuple[Path, ...] = (),
    open_identities: tuple[tuple[int, int], ...] = (),
) -> dict[str, Any]:
    return retention.plan_release_artifact_retention(
        activation_journal=fixture["activation_journal"],
        unit_journal=fixture["unit_journal"],
        backup_root=fixture["backup_root"],
        inventory_roots=(fixture["inventory"],),
        open_inventory=retention.OpenInventorySnapshot(
            source="synthetic_test",
            complete=True,
            open_paths=open_paths,
            open_identities=open_identities,
        ),
    )


def test_complete_closed_inventory_classifies_only_authenticated_old_release_for_deletion(
    synthetic_inventory: dict[str, Any],
) -> None:
    plan = _plan(synthetic_inventory)
    targets = {Path(item["path"]).name: item for item in plan["targets"]}

    assert plan["schema"] == retention.PLAN_SCHEMA
    assert plan["mode"] == "read_only_classification"
    assert plan["scope"] == "wheel_release_inventory_only"
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
    ]
    assert retention.main(argv) == 0
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["block_reason"] == "open_state_ambiguous"

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
