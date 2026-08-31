from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_index as dr_index


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _candidate(
    root: Path,
    ordinal: int,
    *,
    source_kind: str = "terminal_activation",
) -> dict[str, Any]:
    backup = _private_directory(root / f"backup-{ordinal}")
    release = _private_directory(root / f"release-{ordinal}")
    digest = lambda offset: f"{ordinal * 16 + offset:064x}"  # noqa: E731
    return {
        "allowed_rollback_tree_sha256s": [digest(6)],
        "backup_directory": str(backup),
        "backup_record_sha256": digest(1),
        "database_schema": 46,
        "database_receipt_sha256": digest(2),
        "engineer_receipt_sha256": digest(3),
        "inbox_receipt_sha256": digest(4),
        "obsidian_receipt_sha256": digest(5),
        "restore_release": {
            "commit": f"{ordinal:040x}",
            "max_schema": 50,
            "root": str(release),
            "tree_manifest_sha256": digest(6),
            "version": f"0.207.{ordinal}",
            "wheel_sha256": digest(7),
        },
        "schema": dr_index.GENERATION_CANDIDATE_SCHEMA,
        "source_kind": source_kind,
        "source_receipt_sha256": digest(8),
        "source_transaction_id": digest(9),
    }


def _authentication_receipt(candidate: dict[str, Any], ordinal: int) -> dict[str, Any]:
    status = Path(candidate["backup_directory"]).stat()
    core = {
        "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
        "activation_journal_file_sha256": f"{ordinal + 1:064x}",
        "activation_journal_sha256": f"{ordinal + 2:064x}",
        "activation_receipt_file_sha256": f"{ordinal + 3:064x}",
        "activation_receipt_sha256": candidate["source_receipt_sha256"],
        "backup_directory": {
            "device": status.st_dev,
            "inode": status.st_ino,
            "path": candidate["backup_directory"],
        },
        "backup_manifest_sha256": f"{ordinal + 4:064x}",
        "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "database_schema": candidate["database_schema"],
        "restore_operator_sha256": f"{ordinal + 5:064x}",
        "schema": dr_index.AUTHENTICATION_RECEIPT_SCHEMA,
        "source_transaction_id": candidate["source_transaction_id"],
        "status": "authenticated",
        "surface_receipts": {
            "database": candidate["database_receipt_sha256"],
            "engineer": candidate["engineer_receipt_sha256"],
            "inbox": candidate["inbox_receipt_sha256"],
            "obsidian": candidate["obsidian_receipt_sha256"],
        },
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _activation_receipt(candidate: dict[str, Any], *, candidate_tree_sha256: str) -> dict[str, Any]:
    core = {
        "alias_repair": {},
        "backend_accepted": True,
        "backup_receipt_sha256": candidate["database_receipt_sha256"],
        "bridge_accepted": True,
        "candidate_tree_sha256": candidate_tree_sha256,
        "database_schema_before": candidate["database_schema"],
        "engineer_backup_receipt_sha256": candidate["engineer_receipt_sha256"],
        "inbox_backup_receipt_sha256": candidate["inbox_receipt_sha256"],
        "obsidian_backup_receipt_sha256": candidate["obsidian_receipt_sha256"],
        "runtime_policy": {},
        "schema": release_operator.ACTIVATION_RECEIPT_SCHEMA,
        "status": "clear",
    }
    return {
        **core,
        "operator_schema": release_operator.OPERATOR_SCHEMA,
        "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }


def _v2_candidate_and_authentication(
    root: Path,
    ordinal: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = _candidate(root, ordinal)
    activation = _activation_receipt(candidate, candidate_tree_sha256=f"{ordinal + 500:064x}")
    candidate["source_receipt_sha256"] = activation["receipt_sha256"]
    status = Path(candidate["backup_directory"]).stat()
    release_record = dict(candidate["restore_release"])
    core = {
        "activation_receipt": activation,
        "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
        "activation_journal_file_sha256": f"{ordinal + 1:064x}",
        "activation_journal_sha256": f"{ordinal + 2:064x}",
        "activation_receipt_file_sha256": hashlib.sha256(_canonical(activation) + b"\n").hexdigest(),
        "activation_receipt_sha256": activation["receipt_sha256"],
        "backup_directory": {
            "device": status.st_dev,
            "inode": status.st_ino,
            "path": candidate["backup_directory"],
        },
        "backup_manifest_sha256": f"{ordinal + 4:064x}",
        "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "database_schema": candidate["database_schema"],
        "release_records": {"fallback": release_record, "previous": release_record},
        "restore_operator_sha256": f"{ordinal + 5:064x}",
        "schema": dr_index.AUTHENTICATION_RECEIPT_SCHEMA_V2,
        "source_transaction_id": candidate["source_transaction_id"],
        "status": "authenticated",
        "surface_receipts": {
            "database": candidate["database_receipt_sha256"],
            "engineer": candidate["engineer_receipt_sha256"],
            "inbox": candidate["inbox_receipt_sha256"],
            "obsidian": candidate["obsidian_receipt_sha256"],
        },
    }
    return candidate, {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _v2_rehearsal_receipt(
    candidate: dict[str, Any],
    authentication: dict[str, Any],
    authenticated: dict[str, Any],
) -> dict[str, Any]:
    receipt = _rehearsal_receipt(candidate, authentication, authenticated, 0)
    core = {
        **{key: value for key, value in receipt.items() if key != "receipt_sha256"},
        "exercised_release": authentication["release_records"]["fallback"],
        "schema": dr_index.REHEARSAL_RECEIPT_SCHEMA_V2,
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _rehearsal_receipt(
    candidate: dict[str, Any],
    authentication: dict[str, Any],
    authenticated: dict[str, Any],
    _ordinal: int,
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
        "check_count": len(dr_index.DR_REHEARSAL_CHECKS),
        "checkset_sha256": dr_index.DR_REHEARSAL_CHECKSET_SHA256,
        "database_foreign_keys_clear": True,
        "database_integrity_clear": True,
        "database_reopen_count": 2,
        "database_schema": candidate["database_schema"],
        "engineer_authority_present": True,
        "engineer_exact": True,
        "fault_boundary": "after_migration_before_provision_or_network",
        "four_surface_exact": True,
        "four_surface_sha256": hashlib.sha256(_canonical(authentication["surface_receipts"])).hexdigest(),
        "index_journal_sha256": authenticated["journal_sha256"],
        "index_revision": authenticated["revision"],
        "index_transaction_id": authenticated["transaction_id"],
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
        "schema": dr_index.REHEARSAL_RECEIPT_SCHEMA,
        "scratch_removed": True,
        "source": {key: authentication[key] for key in source_keys},
        "status": "rehearsed",
        "systemctl_call_count": 0,
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _external_receipt_ref(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "schema": str(receipt["schema"]),
        "sha256": str(receipt["receipt_sha256"]),
    }


def _external_receipt_path(
    index: dr_index.DurableDRGenerationIndex,
    kind: str,
    receipt: dict[str, Any],
) -> Path:
    return index.receipt_directory / f"{kind}-{receipt['receipt_sha256']}.json"


def _index(tmp_path: Path) -> dr_index.DurableDRGenerationIndex:
    state = _private_directory(tmp_path / "state")
    index = dr_index.DurableDRGenerationIndex(state)
    index.initialize()
    return index


@pytest.mark.parametrize("helper", ("private", "staging"))
def test_dr_index_file_fifo_swap_is_bounded(tmp_path: Path, helper: str) -> None:
    directory = _private_directory(tmp_path / "state")
    target = directory / "mutable.json"
    target.write_bytes(b"x")
    target.chmod(0o600)
    child = r"""
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tools import release_dr_generation_index as module

directory = Path(sys.argv[2])
helper = sys.argv[3]
target = directory / "mutable.json"
real_open = os.open
real_close = os.close
real_unlink = os.unlink
real_mkfifo = os.mkfifo
directory_fd = real_open(directory, os.O_RDONLY | os.O_DIRECTORY)
swapped = [False]

def swap_open(path, flags, *args, **kwargs):
    if path == target.name and kwargs.get("dir_fd") == directory_fd and not swapped[0]:
        swapped[0] = True
        real_unlink(target)
        real_mkfifo(target, 0o600)
    return real_open(path, flags, *args, **kwargs)

module.os.open = swap_open
try:
    try:
        if helper == "private":
            module._stable_private_file_at(
                directory_fd,
                target.name,
                mode=0o600,
                maximum_bytes=16,
                code="test_fifo",
            )
        else:
            module._stable_staging_file_at(
                directory_fd,
                target.name,
                maximum_bytes=16,
                code="test_fifo",
            )
    except module.DRGenerationIndexError as exc:
        if str(exc) != "test_fifo":
            raise
    else:
        raise AssertionError("FIFO substitution was accepted")
finally:
    real_close(directory_fd)
if not swapped[0]:
    raise AssertionError("FIFO substitution was not exercised")
"""

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child,
            str(Path(__file__).resolve().parents[1]),
            str(directory),
            helper,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def _advance(
    index: dr_index.DurableDRGenerationIndex,
    candidate: dict[str, Any],
    *,
    intent: str,
    ordinal: int,
) -> dict[str, Any]:
    state = index.load()
    state = index.prepare(
        intent=intent,
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, ordinal)
    state = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, state, ordinal + 1000),
        expected_journal_sha256=state["journal_sha256"],
    )
    return index.publish(expected_journal_sha256=state["journal_sha256"])


def _advance_v2(
    index: dr_index.DurableDRGenerationIndex,
    root: Path,
    ordinal: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate, authentication = _v2_candidate_and_authentication(root, ordinal)
    state = index.load()
    state = index.prepare(
        intent="rotate_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_rehearsed(
        receipt=_v2_rehearsal_receipt(candidate, authentication, state),
        expected_journal_sha256=state["journal_sha256"],
    )
    return (
        index.publish(expected_journal_sha256=state["journal_sha256"]),
        candidate,
        authentication,
    )


def _assert_restore_release_pin(
    pin: dr_index.GenerationPin,
    candidate: dict[str, Any],
) -> None:
    release = candidate["restore_release"]
    assert pin.restore_release_root == Path(release["root"])
    assert pin.restore_release_commit == release["commit"]
    assert pin.restore_release_tree_manifest_sha256 == release["tree_manifest_sha256"]
    assert pin.restore_release_wheel_sha256 == release["wheel_sha256"]
    assert pin.restore_release_max_schema == release["max_schema"]
    assert pin.restore_release_version == release["version"]


def test_bootstrap_publishes_exact_immutable_receipt_then_clear_cas(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 1)

    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    assert prepared["phase"] == "prepared"
    assert prepared["base_clear_sha256"] == initial["journal_sha256"]
    authentication_body = _authentication_receipt(candidate, 101)
    authenticated = index.record_authenticated(
        receipt=authentication_body,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsal_body = _rehearsal_receipt(candidate, authentication_body, authenticated, 102)
    rehearsed = index.record_rehearsed(
        receipt=rehearsal_body,
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    authentication_path = _external_receipt_path(index, "authentication", authentication_body)
    rehearsal_path = _external_receipt_path(index, "rehearsal", rehearsal_body)
    assert authenticated["pending"]["authentication_receipt"] == _external_receipt_ref(authentication_body)
    assert rehearsed["pending"]["rehearsal_receipt"] == _external_receipt_ref(rehearsal_body)
    assert authentication_path.read_bytes() == _canonical(authentication_body) + b"\n"
    assert rehearsal_path.read_bytes() == _canonical(rehearsal_body) + b"\n"
    assert stat.S_IMODE(authentication_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(rehearsal_path.stat().st_mode) == 0o400
    assert authentication_path.stat().st_nlink == rehearsal_path.stat().st_nlink == 1
    reference = rehearsed["pending"]["generation"]
    receipt_path = index.receipt_directory / f"{reference['generation_id']}.json"
    assert not receipt_path.exists()

    published = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])

    assert published["phase"] == "clear"
    assert published["revision"] == 4
    assert published["base_clear_sha256"] == initial["journal_sha256"]
    assert published["current"] == reference
    assert published["older"] is None
    assert published["pending"] is None
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    assert receipt["generation"]["authentication_receipt"] == _external_receipt_ref(authentication_body)
    assert receipt["generation"]["rehearsal_receipt"] == _external_receipt_ref(rehearsal_body)
    assert reference["generation_id"] == hashlib.sha256(_canonical(receipt["generation"])).hexdigest()
    receipt_core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    assert reference["receipt_sha256"] == hashlib.sha256(_canonical(receipt_core)).hexdigest()
    status = receipt_path.stat()
    assert stat.S_IMODE(status.st_mode) == 0o400
    assert status.st_nlink == 1


def test_v2_publishes_and_resolves_exact_activation_body_after_source_disappears(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    candidate, authentication = _v2_candidate_and_authentication(tmp_path, 301)
    state = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )

    activation_path = index.pending_activation_receipt_path(
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    assert activation_path is not None
    expected_raw = _canonical(authentication["activation_receipt"]) + b"\n"
    assert activation_path.read_bytes() == expected_raw
    assert activation_path.name == (f"activation-{hashlib.sha256(expected_raw).hexdigest()}.json")
    assert stat.S_IMODE(activation_path.stat().st_mode) == 0o400
    assert activation_path.stat().st_nlink == 1

    with pytest.raises(dr_index.DRGenerationIndexError, match="^rehearsal_receipt_invalid$"):
        index.record_rehearsed(
            receipt=_rehearsal_receipt(candidate, authentication, authenticated, 0),
            expected_journal_sha256=authenticated["journal_sha256"],
        )

    rehearsal_receipt = _v2_rehearsal_receipt(candidate, authentication, authenticated)
    forged = dict(rehearsal_receipt)
    forged["exercised_release"] = {
        **forged["exercised_release"],
        "root": str(_private_directory(tmp_path / "foreign-exercised")),
    }
    forged_core = {key: value for key, value in forged.items() if key != "receipt_sha256"}
    forged["receipt_sha256"] = hashlib.sha256(_canonical(forged_core)).hexdigest()
    with pytest.raises(dr_index.DRGenerationIndexError, match="^rehearsal_receipt_invalid$"):
        index.record_rehearsed(
            receipt=forged,
            expected_journal_sha256=authenticated["journal_sha256"],
        )

    rehearsed = index.record_rehearsed(
        receipt=rehearsal_receipt,
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    clear = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    assert (
        index.current_activation_receipt_path(
            expected_journal_sha256=clear["journal_sha256"],
        )
        == activation_path
    )
    pin = index.authority_snapshot().pins[0]
    assert pin.activation_receipt_path == activation_path
    assert pin.activation_receipt_file_sha256 == hashlib.sha256(expected_raw).hexdigest()


def test_activation_body_can_be_durably_published_before_index_initialization(
    tmp_path: Path,
) -> None:
    state_directory = _private_directory(tmp_path / "state")
    index = dr_index.DurableDRGenerationIndex(state_directory)
    candidate, authentication = _v2_candidate_and_authentication(tmp_path, 303)
    del candidate
    body = authentication["activation_receipt"]
    expected_raw = _canonical(body) + b"\n"
    expected_sha256 = hashlib.sha256(expected_raw).hexdigest()

    assert (
        index.load_activation_receipt_body(
            file_sha256=expected_sha256,
            missing_ok=True,
        )
        is None
    )
    assert not index.path.exists()
    path = index.publish_activation_receipt(receipt=body)

    assert not index.path.exists()
    assert path.name == f"activation-{expected_sha256}.json"
    assert path.read_bytes() == expected_raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert index.load_activation_receipt_body(file_sha256=expected_sha256) == body
    assert index.resolve_activation_receipt_path(file_sha256=expected_sha256) == path
    assert index.publish_activation_receipt(receipt=body) == path


def test_exact_legacy_receipt_mode_upgrade_is_restart_safe_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    clear = _advance(index, _candidate(tmp_path, 304), intent="bootstrap_current", ordinal=304)
    generation_path = index.receipt_directory / f"{clear['current']['generation_id']}.json"
    generation = json.loads(generation_path.read_text(encoding="ascii"))
    authentication_path = index.receipt_directory / (
        f"authentication-{generation['generation']['authentication_receipt']['sha256']}.json"
    )
    rehearsal_path = index.receipt_directory / (
        f"rehearsal-{generation['generation']['rehearsal_receipt']['sha256']}.json"
    )
    expected_files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (generation_path, authentication_path, rehearsal_path)
    }
    for path in (generation_path, authentication_path, rehearsal_path):
        path.chmod(0o600)
    unrelated = index.receipt_directory / "unrelated-legacy.json"
    unrelated.write_text("{}\n", encoding="ascii")
    unrelated.chmod(0o600)
    monkeypatch.setattr(
        dr_index,
        "_LEGACY_020790_INDEX_FILE_SHA256",
        hashlib.sha256(index.path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        dr_index,
        "_LEGACY_020790_INDEX_JOURNAL_SHA256",
        clear["journal_sha256"],
    )
    monkeypatch.setattr(
        dr_index,
        "_LEGACY_020790_GENERATION_ID",
        clear["current"]["generation_id"],
    )
    monkeypatch.setattr(
        dr_index,
        "_LEGACY_020790_GENERATION_RECEIPT_SHA256",
        clear["current"]["receipt_sha256"],
    )
    monkeypatch.setattr(dr_index, "_LEGACY_020790_RECEIPT_FILES", expected_files)

    assert index.upgrade_legacy_020790_receipt_modes() == clear
    assert index.upgrade_legacy_020790_receipt_modes() == clear
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o400
        for path in (generation_path, authentication_path, rehearsal_path)
    )
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o600


def test_v2_activation_body_collision_and_tamper_fail_closed(tmp_path: Path) -> None:
    index = _index(tmp_path)
    candidate, authentication = _v2_candidate_and_authentication(tmp_path, 302)
    state = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    activation_reference, expected_raw, _payload = dr_index.activation_receipt_evidence(authentication)
    activation_path = index.receipt_directory / (f"activation-{activation_reference['sha256']}.json")
    activation_path.write_bytes(b'{"foreign":true}\n')
    activation_path.chmod(0o400)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="^activation_receipt_publication_failed$",
    ):
        index.record_authenticated(
            receipt=authentication,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    assert activation_path.read_bytes() != expected_raw
    assert index.load()["phase"] == "prepared"

    activation_path.chmod(0o600)
    activation_path.unlink()
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    activation_path.chmod(0o600)
    activation_path.write_bytes(expected_raw + b" ")
    activation_path.chmod(0o400)
    with pytest.raises(dr_index.DRGenerationIndexError, match="^activation_receipt_invalid$"):
        index.pending_activation_receipt_path(
            expected_journal_sha256=authenticated["journal_sha256"],
        )


def test_current_generation_identity_is_exact_compact_and_detached(tmp_path: Path) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 2)
    state = index.load()
    state = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 201)
    state = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=state["journal_sha256"],
    )
    rehearsal = _rehearsal_receipt(candidate, authentication, state, 202)
    state = index.record_rehearsed(
        receipt=rehearsal,
        expected_journal_sha256=state["journal_sha256"],
    )
    clear = index.publish(expected_journal_sha256=state["journal_sha256"])

    identity = index.current_generation_identity(
        expected_journal_sha256=clear["journal_sha256"],
    )

    assert identity is not None
    assert identity.index_journal_sha256 == clear["journal_sha256"]
    assert identity.index_phase == "clear"
    assert identity.index_revision == clear["revision"]
    assert identity.generation_id == clear["current"]["generation_id"]
    assert identity.generation_receipt_sha256 == clear["current"]["receipt_sha256"]
    assert identity.candidate == candidate
    assert identity.candidate_sha256 == hashlib.sha256(_canonical(candidate)).hexdigest()
    assert identity.authentication_receipt == _external_receipt_ref(authentication)
    assert identity.rehearsal_receipt == _external_receipt_ref(rehearsal)
    assert set(identity.authentication_receipt) == {"schema", "sha256"}
    assert set(identity.rehearsal_receipt) == {"schema", "sha256"}
    assert "status" not in identity.authentication_receipt
    assert "status" not in identity.rehearsal_receipt

    identity.candidate["source_kind"] = "caller_mutation"
    durable = index.current_generation_identity(
        expected_journal_sha256=clear["journal_sha256"],
    )
    assert durable is not None
    assert durable.candidate == candidate


def test_current_generation_identity_requires_exact_index_epoch(tmp_path: Path) -> None:
    index = _index(tmp_path)
    empty = index.load()
    assert (
        index.current_generation_identity(
            expected_journal_sha256=empty["journal_sha256"],
        )
        is None
    )

    clear = _advance(
        index,
        _candidate(tmp_path, 3),
        intent="bootstrap_current",
        ordinal=300,
    )
    with pytest.raises(dr_index.DRGenerationIndexError, match="^dr_generation_cas_mismatch$"):
        index.current_generation_identity(
            expected_journal_sha256=empty["journal_sha256"],
        )
    assert index.load() == clear


def test_current_generation_identity_rejects_receipt_namespace_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    clear = _advance(
        index,
        _candidate(tmp_path, 4),
        intent="bootstrap_current",
        ordinal=400,
    )
    receipt_directory = index.receipt_directory
    displaced = tmp_path / "receipts-displaced"
    real_load_receipt = index._load_receipt  # noqa: SLF001
    calls = 0

    def swap_after_load(reference: Any, receipt_fd: int) -> dict[str, Any]:
        nonlocal calls
        payload = real_load_receipt(reference, receipt_fd)
        calls += 1
        if calls == 1:
            receipt_directory.rename(displaced)
            _private_directory(receipt_directory)
        return payload

    monkeypatch.setattr(index, "_load_receipt", swap_after_load)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.current_generation_identity(
            expected_journal_sha256=clear["journal_sha256"],
        )
    assert calls >= 1


def test_explicit_older_and_rotation_use_only_exact_inputs_not_mtime_or_globs(tmp_path: Path) -> None:
    index = _index(tmp_path)
    current = _candidate(tmp_path, 2)
    adopted_older = _candidate(tmp_path, 3, source_kind="explicit_older_adoption")
    next_current = _candidate(tmp_path, 4)
    decoy = _private_directory(tmp_path / "backup-decoy")
    os.utime(decoy, ns=(9_000_000_000, 9_000_000_000))
    os.utime(Path(adopted_older["backup_directory"]), ns=(1, 1))

    first = _advance(index, current, intent="bootstrap_current", ordinal=20)
    first_ref = first["current"]
    second = _advance(index, adopted_older, intent="fill_older", ordinal=30)
    adopted_ref = second["older"]
    assert {pin.backup_directory for pin in index.pins()} == {
        Path(current["backup_directory"]),
        Path(adopted_older["backup_directory"]),
    }
    assert decoy not in {pin.backup_directory for pin in index.pins()}

    third = _advance(index, next_current, intent="rotate_current", ordinal=40)

    assert third["current"] not in (first_ref, adopted_ref)
    assert third["older"] == first_ref
    assert adopted_ref not in (third["current"], third["older"])


def test_authority_snapshot_binds_exact_index_bytes_digest_and_pins(tmp_path: Path) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 41)
    _advance(index, candidate, intent="bootstrap_current", ordinal=410)

    snapshot = index.authority_snapshot()

    assert snapshot.index_path == index.path
    assert snapshot.index_raw == index.path.read_bytes()
    assert snapshot.index_sha256 == hashlib.sha256(snapshot.index_raw).hexdigest()
    assert [pin.role for pin in snapshot.pins] == ["current"]
    assert snapshot.pins[0].backup_directory == Path(candidate["backup_directory"])
    assert snapshot.pins[0].rehearsal_binding is not None
    assert snapshot.pins[0].rehearsal_binding["database_schema"] == candidate["database_schema"]
    assert (
        snapshot.pins[0].rehearsal_binding["allowed_rollback_tree_sha256s"]
        == (candidate["allowed_rollback_tree_sha256s"])
    )
    _assert_restore_release_pin(snapshot.pins[0], candidate)


def test_first_v2_rotation_atomically_seals_permanent_preactivation_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    legacy = _advance(index, _candidate(tmp_path, 501), intent="bootstrap_current", ordinal=501)
    legacy_ref = legacy["current"]
    candidate, authentication = _v2_candidate_and_authentication(tmp_path, 502)
    state = index.prepare(
        intent="rotate_current",
        candidate=candidate,
        expected_journal_sha256=legacy["journal_sha256"],
    )
    state = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=state["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_v2_rehearsal_receipt(candidate, authentication, state),
        expected_journal_sha256=state["journal_sha256"],
    )
    assert rehearsed["preactivation_anchor"] is None

    real_cas = index._cas_replace_locked  # noqa: SLF001
    failed = False

    def fail_before_anchor_cas(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal failed
        following = args[1]
        if not failed and following.get("preactivation_anchor") is not None:
            failed = True
            raise dr_index.DRGenerationIndexError("synthetic_anchor_cas_crash")
        return real_cas(*args, **kwargs)

    monkeypatch.setattr(index, "_cas_replace_locked", fail_before_anchor_cas)
    with pytest.raises(dr_index.DRGenerationIndexError, match="^synthetic_anchor_cas_crash$"):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    assert index.load()["preactivation_anchor"] is None
    with index._guard() as pins:  # noqa: SLF001
        current = index._load_unlocked(pins)  # noqa: SLF001
        expected_anchor = index._derive_preactivation_anchor(  # noqa: SLF001
            legacy_reference=current["current"],
            first_v2_reference=current["pending"]["generation"],
            receipt_fd=pins.receipt_fd,
        )
        exact_following = {
            **index._core_from_state(current),  # noqa: SLF001
            "current": current["pending"]["generation"],
            "older": current["current"],
            "pending": None,
            "phase": "clear",
            "preactivation_anchor": expected_anchor,
            "revision": current["revision"] + 1,
        }
        before_index = index.path.read_bytes()
        before_head = (index.head_directory / dr_index.HEAD_FENCE_NAME).read_bytes()
        for missing_anchor in (
            {key: value for key, value in exact_following.items() if key != "preactivation_anchor"},
            {**exact_following, "preactivation_anchor": None},
        ):
            with pytest.raises(
                dr_index.DRGenerationIndexError,
                match="^dr_generation_preactivation_anchor_invalid$",
            ):
                real_cas(current, missing_anchor, pins)
            assert index.path.read_bytes() == before_index
            assert (index.head_directory / dr_index.HEAD_FENCE_NAME).read_bytes() == before_head
        forged_following = {
            **index._core_from_state(current),  # noqa: SLF001
            "current": current["pending"]["generation"],
            "older": current["current"],
            "pending": None,
            "phase": "clear",
            "preactivation_anchor": expected_anchor,
            "revision": current["revision"] + 2,
        }
        with pytest.raises(
            dr_index.DRGenerationIndexError,
            match="^dr_generation_preactivation_anchor_invalid$",
        ):
            real_cas(current, forged_following, pins)
    assert index.load()["preactivation_anchor"] is None
    monkeypatch.setattr(index, "_cas_replace_locked", real_cas)

    clear = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    activation_ref, _raw, _body = dr_index.activation_receipt_evidence(authentication)
    assert clear["older"] == legacy_ref
    assert clear["preactivation_anchor"] == {
        "activation_receipt": activation_ref,
        "first_v2_generation": clear["current"],
        "legacy_generation": legacy_ref,
    }
    assert [pin.role for pin in index.pins()] == ["current", "older"]


def test_missing_anchor_field_is_bounded_to_exact_legacy_index_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    state = index.load()
    legacy_core = {
        key: value for key, value in state.items() if key not in {"journal_sha256", "preactivation_anchor"}
    }
    legacy_journal = hashlib.sha256(_canonical(legacy_core)).hexdigest()
    legacy_payload = {**legacy_core, "journal_sha256": legacy_journal}
    legacy_raw = _canonical(legacy_payload) + b"\n"
    monkeypatch.setattr(dr_index, "_LEGACY_020790_INDEX_JOURNAL_SHA256", legacy_journal)
    monkeypatch.setattr(
        dr_index,
        "_LEGACY_020790_INDEX_FILE_SHA256",
        hashlib.sha256(legacy_raw).hexdigest(),
    )
    with index._guard() as pins:  # noqa: SLF001
        decoded = index._decode_state(legacy_raw, pins.receipt_fd)  # noqa: SLF001
        assert decoded["preactivation_anchor"] is None
        monkeypatch.setattr(dr_index, "_LEGACY_020790_INDEX_FILE_SHA256", "f" * 64)
        with pytest.raises(dr_index.DRGenerationIndexError, match="^dr_generation_index_invalid$"):
            index._decode_state(legacy_raw, pins.receipt_fd)  # noqa: SLF001


def test_preactivation_anchor_is_immutable_and_projects_retain_only_roles(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    _advance(index, _candidate(tmp_path, 503), intent="bootstrap_current", ordinal=503)
    first, _candidate_first, _authentication_first = _advance_v2(index, tmp_path, 504)
    anchor = first["preactivation_anchor"]
    second, _candidate_second, _authentication_second = _advance_v2(index, tmp_path, 505)
    assert second["preactivation_anchor"] == anchor
    assert [pin.role for pin in index.pins()] == [
        "current",
        "older",
        dr_index.PREACTIVATION_LEGACY_ROLE,
    ]
    third, _candidate_third, _authentication_third = _advance_v2(index, tmp_path, 506)
    assert third["preactivation_anchor"] == anchor
    assert [pin.role for pin in index.pins()] == [
        "current",
        "older",
        dr_index.PREACTIVATION_LEGACY_ROLE,
        dr_index.PREACTIVATION_FIRST_V2_ROLE,
    ]

    with index._guard() as pins:  # noqa: SLF001
        current = index._load_unlocked(pins)  # noqa: SLF001
        following = {**index._core_from_state(current), "preactivation_anchor": None}  # noqa: SLF001
        with pytest.raises(
            dr_index.DRGenerationIndexError,
            match="^dr_generation_preactivation_anchor_immutable$",
        ):
            index._cas_replace_locked(current, following, pins)  # noqa: SLF001
    assert index.load() == third


def test_unfinished_transaction_pins_current_older_and_pending(tmp_path: Path) -> None:
    index = _index(tmp_path)
    current = _candidate(tmp_path, 5)
    older = _candidate(tmp_path, 6, source_kind="explicit_older_adoption")
    pending = _candidate(tmp_path, 7)
    _advance(index, current, intent="bootstrap_current", ordinal=50)
    clear = _advance(index, older, intent="fill_older", ordinal=60)

    prepared = index.prepare(
        intent="rotate_current",
        candidate=pending,
        expected_journal_sha256=clear["journal_sha256"],
    )
    pins = index.pins()

    assert [pin.role for pin in pins] == ["current", "older", "pending"]
    assert {pin.backup_directory for pin in pins} == {
        Path(current["backup_directory"]),
        Path(older["backup_directory"]),
        Path(pending["backup_directory"]),
    }
    for pin, candidate in zip(pins, (current, older, pending), strict=True):
        _assert_restore_release_pin(pin, candidate)
    assert pins[-1].generation_id is None
    authentication = _authentication_receipt(pending, 70)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    authenticated_pending_pin = index.pins()[-1]
    assert authenticated_pending_pin.generation_id is None
    _assert_restore_release_pin(authenticated_pending_pin, pending)
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(pending, authentication, authenticated, 71),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    pending_pin = index.pins()[-1]
    assert pending_pin.generation_id == rehearsed["pending"]["generation"]["generation_id"]
    assert pending_pin.receipt_path is not None
    assert not pending_pin.receipt_path.exists()
    _assert_restore_release_pin(pending_pin, pending)


def test_stale_cas_and_out_of_order_transitions_leave_state_unchanged(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 8)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )

    stale_authentication = _authentication_receipt(candidate, 80)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_cas_mismatch"):
        index.record_authenticated(
            receipt=stale_authentication,
            expected_journal_sha256=initial["journal_sha256"],
        )
    assert not _external_receipt_path(index, "authentication", stale_authentication).exists()
    out_of_order_rehearsal = _authentication_receipt(candidate, 81)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_transition_invalid"):
        index.record_rehearsed(
            receipt=out_of_order_rehearsal,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    assert not _external_receipt_path(index, "rehearsal", out_of_order_rehearsal).exists()
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="dr_generation_recovery_receipt_required",
    ):
        index.recover(expected_journal_sha256=prepared["journal_sha256"])
    assert index.load() == prepared


def test_crash_after_no_replace_receipt_before_state_cas_recovers_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 9)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 90)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 91),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    real_cas = index._cas_replace_locked  # noqa: SLF001

    def crash(
        _current: dict[str, Any],
        _following: dict[str, Any],
        _pins: Any,
    ) -> dict[str, Any]:
        raise OSError("simulated crash")

    monkeypatch.setattr(index, "_cas_replace_locked", crash)
    with pytest.raises(OSError, match="simulated crash"):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    reference = rehearsed["pending"]["generation"]
    receipt_path = index.receipt_directory / f"{reference['generation_id']}.json"
    assert receipt_path.is_file()
    assert index.load() == rehearsed

    monkeypatch.setattr(index, "_cas_replace_locked", real_cas)
    recovered = index.recover(expected_journal_sha256=rehearsed["journal_sha256"])
    assert recovered["phase"] == "clear"
    assert recovered["current"] == reference
    assert index.recover(expected_journal_sha256=recovered["journal_sha256"]) == recovered


def test_namespace_guard_failure_after_receipt_stops_before_index_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 92)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 920)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 921),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    generation_name = f"{rehearsed['pending']['generation']['generation_id']}.json"
    real_publish = index._publish_no_replace  # noqa: SLF001
    receipt_published = False

    def publish_then_arm(**kwargs: Any) -> None:
        nonlocal receipt_published
        real_publish(**kwargs)
        if kwargs.get("name") == generation_name:
            receipt_published = True

    cas_called = False
    real_cas = index._cas_replace_locked  # noqa: SLF001

    def observed_cas(
        current: dict[str, Any],
        following: dict[str, Any],
        pins: Any,
        namespace_guard: Any = None,
    ) -> dict[str, Any]:
        nonlocal cas_called
        cas_called = True
        return real_cas(current, following, pins, namespace_guard=namespace_guard)

    def namespace_guard() -> None:
        if receipt_published:
            raise release_operator.ReleaseFailure("operator_transaction_lock_changed")

    monkeypatch.setattr(index, "_publish_no_replace", publish_then_arm)
    monkeypatch.setattr(index, "_cas_replace_locked", observed_cas)

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^operator_transaction_lock_changed$",
    ):
        index.recover(
            expected_journal_sha256=rehearsed["journal_sha256"],
            namespace_guard=namespace_guard,
        )

    assert receipt_published is True
    assert cas_called is False
    assert index.load() == rehearsed
    assert (index.receipt_directory / generation_name).is_file()


@pytest.mark.parametrize("kind", ("authentication", "rehearsal"))
def test_crash_after_evidence_body_before_cas_replays_without_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 25)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    prior = prepared
    authentication = _authentication_receipt(candidate, 250)
    if kind == "rehearsal":
        prior = index.record_authenticated(
            receipt=authentication,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    body = (
        _authentication_receipt(candidate, 251)
        if kind == "authentication"
        else _rehearsal_receipt(candidate, authentication, prior, 251)
    )
    before = index.path.read_bytes()
    real_cas = index._cas_replace_locked  # noqa: SLF001

    def crash(
        _current: dict[str, Any],
        _following: dict[str, Any],
        _pins: Any,
    ) -> dict[str, Any]:
        raise OSError(f"crash after {kind} body")

    monkeypatch.setattr(index, "_cas_replace_locked", crash)
    transition = index.record_authenticated if kind == "authentication" else index.record_rehearsed
    with pytest.raises(OSError, match=f"crash after {kind} body"):
        transition(receipt=body, expected_journal_sha256=prior["journal_sha256"])

    body_path = _external_receipt_path(index, kind, body)
    body_status = body_path.stat()
    assert body_path.read_bytes() == _canonical(body) + b"\n"
    assert stat.S_IMODE(body_status.st_mode) == 0o400
    assert body_status.st_nlink == 1
    assert index.path.read_bytes() == before
    assert index.load() == prior

    monkeypatch.setattr(index, "_cas_replace_locked", real_cas)
    recovered = transition(receipt=body, expected_journal_sha256=prior["journal_sha256"])
    durable_status = body_path.stat()
    assert (durable_status.st_dev, durable_status.st_ino) == (body_status.st_dev, body_status.st_ino)
    assert recovered["pending"][f"{kind}_receipt"] == _external_receipt_ref(body)


def test_foreign_receipt_at_exact_generation_name_is_never_overwritten(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 10)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 100)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 101),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    generation_id = rehearsed["pending"]["generation"]["generation_id"]
    target = index.receipt_directory / f"{generation_id}.json"
    index_raw = index.path.read_bytes()
    target.write_bytes(b'{"foreign":true}\n')
    target.chmod(0o400)

    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="generation_receipt_invalid",
    ):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    assert target.read_bytes() == b'{"foreign":true}\n'
    assert index.path.read_bytes() == index_raw


@pytest.mark.parametrize("kind", ("authentication", "rehearsal"))
def test_foreign_evidence_at_exact_digest_name_is_never_overwritten(
    tmp_path: Path,
    kind: str,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 26)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    prior = prepared
    authentication = _authentication_receipt(candidate, 260)
    if kind == "rehearsal":
        prior = index.record_authenticated(
            receipt=authentication,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    body = (
        _authentication_receipt(candidate, 261)
        if kind == "authentication"
        else _rehearsal_receipt(candidate, authentication, prior, 261)
    )
    target = _external_receipt_path(index, kind, body)
    target.write_bytes(b'{"foreign":true}\n')
    target.chmod(0o400)
    index_raw = index.path.read_bytes()
    transition = index.record_authenticated if kind == "authentication" else index.record_rehearsed

    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match=f"{kind}_receipt_publication_failed",
    ):
        transition(receipt=body, expected_journal_sha256=prior["journal_sha256"])

    assert target.read_bytes() == b'{"foreign":true}\n'
    assert index.path.read_bytes() == index_raw


def test_committed_receipt_mode_link_or_body_drift_fails_closed(tmp_path: Path) -> None:
    index = _index(tmp_path)
    clear = _advance(index, _candidate(tmp_path, 11), intent="bootstrap_current", ordinal=110)
    reference = clear["current"]
    receipt = index.receipt_directory / f"{reference['generation_id']}.json"

    receipt.chmod(0o600)
    with pytest.raises(dr_index.DRGenerationIndexError, match="generation_receipt_invalid"):
        index.load()
    receipt.chmod(0o400)
    alias = index.receipt_directory / "forbidden-hardlink"
    os.link(receipt, alias)
    with pytest.raises(dr_index.DRGenerationIndexError, match="generation_receipt_invalid"):
        index.load()
    alias.unlink()
    raw = receipt.read_bytes()
    receipt.chmod(0o600)
    receipt.write_bytes(raw.replace(b"terminal_activation", b"explicit_older_adoption"))
    receipt.chmod(0o400)
    with pytest.raises(dr_index.DRGenerationIndexError, match="generation_receipt_invalid"):
        index.load()


@pytest.mark.parametrize(
    ("phase", "kind", "error"),
    (
        ("authenticated", "authentication", "authentication_receipt_invalid"),
        ("rehearsed", "rehearsal", "rehearsal_receipt_invalid"),
        ("clear", "authentication", "authentication_receipt_invalid"),
        ("clear", "rehearsal", "rehearsal_receipt_invalid"),
    ),
)
def test_missing_evidence_body_fails_every_referencing_state_load(
    tmp_path: Path,
    phase: str,
    kind: str,
    error: str,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 23)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication_body = _authentication_receipt(candidate, 230)
    authenticated = index.record_authenticated(
        receipt=authentication_body,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsal_body = _rehearsal_receipt(candidate, authentication_body, authenticated, 231)
    state = authenticated
    if phase in {"rehearsed", "clear"}:
        state = index.record_rehearsed(
            receipt=rehearsal_body,
            expected_journal_sha256=state["journal_sha256"],
        )
    if phase == "clear":
        state = index.publish(expected_journal_sha256=state["journal_sha256"])
    assert state["phase"] == phase

    body = authentication_body if kind == "authentication" else rehearsal_body
    path = _external_receipt_path(index, kind, body)
    path.unlink()

    with pytest.raises(dr_index.DRGenerationIndexError, match=error):
        index.load()


@pytest.mark.parametrize("kind", ("authentication", "rehearsal"))
def test_evidence_body_mode_link_hash_schema_and_canonical_drift_fail_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 24)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication_body = _authentication_receipt(candidate, 240)
    authenticated = index.record_authenticated(
        receipt=authentication_body,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsal_body = _rehearsal_receipt(candidate, authentication_body, authenticated, 241)
    rehearsed = index.record_rehearsed(
        receipt=rehearsal_body,
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    clear = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    body = authentication_body if kind == "authentication" else rehearsal_body
    path = _external_receipt_path(index, kind, body)
    error = f"{kind}_receipt_invalid"
    original = path.read_bytes()

    path.chmod(0o600)
    with pytest.raises(dr_index.DRGenerationIndexError, match=error):
        index.load()
    path.chmod(0o400)
    alias = index.receipt_directory / f"forbidden-{kind}-hardlink"
    os.link(path, alias)
    with pytest.raises(dr_index.DRGenerationIndexError, match=error):
        index.load()
    alias.unlink()

    path.chmod(0o600)
    path.write_bytes(json.dumps(body, indent=2, sort_keys=True).encode("ascii") + b"\n")
    path.chmod(0o400)
    with pytest.raises(dr_index.DRGenerationIndexError, match=error):
        index.load()

    changed = dict(body)
    changed["status"] = "tampered"
    path.chmod(0o600)
    path.write_bytes(_canonical(changed) + b"\n")
    path.chmod(0o400)
    with pytest.raises(dr_index.DRGenerationIndexError, match=error):
        index.load()

    changed["schema"] = f"friday.tampered-{kind}.v1"
    changed_core = {key: value for key, value in changed.items() if key != "receipt_sha256"}
    changed["receipt_sha256"] = hashlib.sha256(_canonical(changed_core)).hexdigest()
    path.chmod(0o600)
    path.write_bytes(_canonical(changed) + b"\n")
    path.chmod(0o400)
    with pytest.raises(dr_index.DRGenerationIndexError, match=error):
        index.load()

    path.chmod(0o600)
    path.write_bytes(original)
    path.chmod(0o400)
    assert index.load() == clear


def test_index_symlink_hardlink_or_digest_drift_fails_closed(tmp_path: Path) -> None:
    index = _index(tmp_path)
    raw = index.path.read_bytes()
    alias = index.state_directory / "index-hardlink"
    os.link(index.path, alias)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_index_invalid"):
        index.load()
    alias.unlink()

    payload = json.loads(raw)
    payload["revision"] = 999
    index.path.write_bytes(_canonical(payload) + b"\n")
    index.path.chmod(0o600)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="dr_generation_head_rollback_detected",
    ):
        index.load()

    index.path.unlink()
    outside = index.state_directory / "outside"
    outside.write_bytes(raw)
    outside.chmod(0o600)
    index.path.symlink_to(outside)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_index_invalid"):
        index.load()


def test_closed_intents_and_exact_external_receipts_reject_implicit_adoption(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    adoption = _candidate(tmp_path, 12, source_kind="explicit_older_adoption")
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_intent_invalid"):
        index.prepare(
            intent="bootstrap_current",
            candidate=adoption,
            expected_journal_sha256=initial["journal_sha256"],
        )
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_intent_invalid"):
        index.prepare(
            intent="fill_older",
            candidate=_candidate(tmp_path, 13),
            expected_journal_sha256=initial["journal_sha256"],
        )

    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 14),
        expected_journal_sha256=initial["journal_sha256"],
    )
    with pytest.raises(dr_index.DRGenerationIndexError, match="authentication_receipt_invalid"):
        index.record_authenticated(
            receipt={"schema": "friday.test.v1", "sha256": "not-a-digest"},
            expected_journal_sha256=prepared["journal_sha256"],
        )
    assert index.load() == prepared


def test_code_owned_receipt_contract_rejects_self_hashed_foreign_and_cross_candidate(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 140)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    foreign_core = {"schema": "friday.test-authentication.v1", "status": "passed"}
    foreign = {
        **foreign_core,
        "receipt_sha256": hashlib.sha256(_canonical(foreign_core)).hexdigest(),
    }
    with pytest.raises(dr_index.DRGenerationIndexError, match="^authentication_receipt_invalid$"):
        index.record_authenticated(
            receipt=foreign,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    crossed = _authentication_receipt(_candidate(tmp_path, 141), 1400)
    with pytest.raises(dr_index.DRGenerationIndexError, match="^authentication_receipt_invalid$"):
        index.record_authenticated(
            receipt=crossed,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    assert index.load() == prepared


def test_rehearsal_contract_rejects_stale_index_epoch_and_noncanonical_checkset(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 142)
    state = index.load()
    state = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 1420)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=state["journal_sha256"],
    )
    for field, value in (
        ("index_transaction_id", "f" * 64),
        ("index_revision", authenticated["revision"] - 1),
        ("index_journal_sha256", "f" * 64),
        ("checkset_sha256", "f" * 64),
        ("four_surface_sha256", "f" * 64),
        ("database_schema", candidate["database_schema"] + 1),
        ("rollback_tree_sha256", "f" * 64),
    ):
        receipt = _rehearsal_receipt(candidate, authentication, authenticated, 1421)
        receipt[field] = value
        core = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
        receipt["receipt_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
        with pytest.raises(dr_index.DRGenerationIndexError, match="^rehearsal_receipt_invalid$"):
            index.record_rehearsed(
                receipt=receipt,
                expected_journal_sha256=authenticated["journal_sha256"],
            )
    assert index.load() == authenticated


def test_published_generation_revalidates_rehearsal_against_durable_exact_binding(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 225)
    clear = _advance(
        index,
        candidate,
        intent="bootstrap_current",
        ordinal=2250,
    )
    assert dr_index.DurableDRGenerationIndex(index.state_directory).load() == clear
    reference = clear["current"]
    receipt_path = index.receipt_directory / f"{reference['generation_id']}.json"
    generation = json.loads(receipt_path.read_bytes())["generation"]
    binding = generation["rehearsal_binding"]
    authentication_path = index.receipt_directory / (
        f"authentication-{generation['authentication_receipt']['sha256']}.json"
    )
    rehearsal_path = index.receipt_directory / (f"rehearsal-{generation['rehearsal_receipt']['sha256']}.json")
    authentication = json.loads(authentication_path.read_bytes())
    rehearsal = json.loads(rehearsal_path.read_bytes())

    assert binding == {
        "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
        "authentication_receipt_sha256": authentication["receipt_sha256"],
        "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "database_schema": candidate["database_schema"],
        "index_journal_sha256": rehearsal["index_journal_sha256"],
        "index_revision": rehearsal["index_revision"],
        "index_transaction_id": rehearsal["index_transaction_id"],
        "schema": dr_index.REHEARSAL_BINDING_SCHEMA,
    }
    for field, value in (
        ("index_transaction_id", "f" * 64),
        ("index_revision", rehearsal["index_revision"] + 1),
        ("index_journal_sha256", "f" * 64),
        ("database_schema", candidate["database_schema"] + 1),
        ("rollback_tree_sha256", "f" * 64),
    ):
        forged = dict(rehearsal)
        forged[field] = value
        forged_core = {key: item for key, item in forged.items() if key != "receipt_sha256"}
        forged["receipt_sha256"] = hashlib.sha256(_canonical(forged_core)).hexdigest()
        with pytest.raises(
            dr_index.DRGenerationIndexError,
            match="^rehearsal_receipt_invalid$",
        ):
            dr_index.validate_rehearsal_receipt(
                forged,
                candidate=candidate,
                authentication_receipt=authentication,
                index_transaction_id=binding["index_transaction_id"],
                index_revision=binding["index_revision"],
                index_journal_sha256=binding["index_journal_sha256"],
            )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("database_schema", 47),
        ("allowed_rollback_tree_sha256s", ["f" * 64]),
        ("source_transaction_id", "f" * 64),
    ),
)
def test_authentication_contract_exactly_binds_schema_rollback_and_source(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    candidate = _candidate(tmp_path, 226)
    receipt = _authentication_receipt(candidate, 2260)
    receipt[field] = value
    core = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()

    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="^authentication_receipt_invalid$",
    ):
        dr_index.validate_authentication_receipt(receipt, candidate=candidate)


def test_persistent_head_fence_blocks_a_b_a_and_explicitly_repairs_forward(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    state_a = index.load()
    state_b = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 143),
        expected_journal_sha256=state_a["journal_sha256"],
    )
    index.path.write_bytes(_canonical(state_a) + b"\n")
    index.path.chmod(0o600)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="^dr_generation_head_rollback_detected$",
    ):
        index.authority_snapshot()
    assert index.initialize() == state_b
    assert index.load() == state_b


def test_head_fence_backup_skew_never_accepts_newer_mutable_projection(tmp_path: Path) -> None:
    index = _index(tmp_path)
    head_path = index.head_directory / dr_index.HEAD_FENCE_NAME
    older_head = head_path.read_bytes()
    state = index.load()
    index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 144),
        expected_journal_sha256=state["journal_sha256"],
    )
    head_path.write_bytes(older_head)
    head_path.chmod(0o600)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="^dr_generation_head_rollback_detected$",
    ):
        index.load()
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="^dr_generation_head_backup_skew$",
    ):
        index.initialize()


def test_authoritative_head_is_bounded_across_long_rotation(tmp_path: Path) -> None:
    index = _index(tmp_path)
    _advance(
        index,
        _candidate(tmp_path, 145),
        intent="bootstrap_current",
        ordinal=1450,
    )
    for ordinal in range(146, 178):
        _advance(
            index,
            _candidate(tmp_path, ordinal),
            intent="rotate_current",
            ordinal=ordinal * 10,
        )

    entries = list(index.head_directory.iterdir())
    assert [entry.name for entry in entries] == [dr_index.HEAD_FENCE_NAME]
    assert entries[0].stat().st_size <= dr_index.MAX_HEAD_FENCE_BYTES
    assert index.load()["revision"] == 33 * 4


def test_same_backup_path_cannot_be_republished_as_a_distinct_generation(tmp_path: Path) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 15)
    clear = _advance(index, candidate, intent="bootstrap_current", ordinal=150)
    forged_generation = _candidate(tmp_path, 16)
    forged_generation["backup_directory"] = candidate["backup_directory"]
    forged_authentication = _authentication_receipt(forged_generation, 160)
    prepared = index.prepare(
        intent="rotate_current",
        candidate=forged_generation,
        expected_journal_sha256=clear["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=forged_authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )

    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_duplicate_slot"):
        index.record_rehearsed(
            receipt=_rehearsal_receipt(
                forged_generation,
                forged_authentication,
                authenticated,
                161,
            ),
            expected_journal_sha256=authenticated["journal_sha256"],
        )
    assert index.load() == authenticated


def test_bootstrap_crash_after_atomic_noreplace_has_one_link_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    index = dr_index.DurableDRGenerationIndex(state)
    real_rename = dr_index._rename_noreplace  # noqa: SLF001

    def rename_then_crash(directory_fd: int, source: str, destination: str) -> None:
        real_rename(directory_fd, source, destination)
        if destination == dr_index.INDEX_NAME:
            raise OSError("crash after atomic bootstrap rename")

    monkeypatch.setattr(dr_index, "_rename_noreplace", rename_then_crash)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="dr_generation_index_initialization_failed",
    ):
        index.initialize()

    status = index.path.stat()
    assert status.st_nlink == 1
    assert stat.S_IMODE(status.st_mode) == 0o600
    monkeypatch.setattr(dr_index, "_rename_noreplace", real_rename)
    assert index.initialize()["phase"] == "clear"


def test_receipt_crash_after_atomic_noreplace_recovers_without_two_link_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 17)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 170)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 171),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    reference = rehearsed["pending"]["generation"]
    receipt_name = f"{reference['generation_id']}.json"
    real_rename = dr_index._rename_noreplace  # noqa: SLF001

    def rename_then_crash(directory_fd: int, source: str, destination: str) -> None:
        real_rename(directory_fd, source, destination)
        if destination == receipt_name:
            raise OSError("crash after atomic receipt rename")

    monkeypatch.setattr(dr_index, "_rename_noreplace", rename_then_crash)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="generation_receipt_publication_failed",
    ):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    receipt_path = index.receipt_directory / receipt_name
    assert receipt_path.stat().st_nlink == 1
    assert index.load() == rehearsed

    monkeypatch.setattr(dr_index, "_rename_noreplace", real_rename)
    recovered = index.recover(expected_journal_sha256=rehearsed["journal_sha256"])
    assert recovered["current"] == reference
    assert receipt_path.stat().st_nlink == 1


def test_authorized_partial_receipt_staging_is_recovered_without_scanning(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 20)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 200)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 201),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    pending = rehearsed["pending"]
    _reference, receipt_raw = dr_index._generation_receipt(  # noqa: SLF001
        {
            "authentication_receipt": pending["authentication_receipt"],
            "candidate": pending["candidate"],
            "rehearsal_binding": dr_index._rehearsal_binding(  # noqa: SLF001
                candidate=pending["candidate"],
                authentication_receipt=pending["authentication_receipt"],
                index_transaction_id=authenticated["transaction_id"],
                index_revision=authenticated["revision"],
                index_journal_sha256=authenticated["journal_sha256"],
            ),
            "rehearsal_receipt": pending["rehearsal_receipt"],
            "schema": dr_index.GENERATION_SCHEMA,
        }
    )
    generation_id = pending["generation"]["generation_id"]
    receipt_name = f"{generation_id}.json"
    staging = index.receipt_directory / f".{receipt_name}.{hashlib.sha256(receipt_raw).hexdigest()}.new"
    staging.write_bytes(receipt_raw[: len(receipt_raw) // 2])
    staging.chmod(0o600)

    clear = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])

    assert clear["phase"] == "clear"
    assert not staging.exists()
    assert (index.receipt_directory / receipt_name).stat().st_nlink == 1


def test_state_directory_swap_during_load_cannot_redirect_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    original_state = index.state_directory
    displaced_state = tmp_path / "state-displaced"
    real_read = dr_index._stable_private_file_at  # noqa: SLF001
    swapped = False

    def swap_then_read(
        directory_fd: int,
        name: str,
        *,
        mode: int,
        maximum_bytes: int,
        code: str,
    ) -> bytes:
        nonlocal swapped
        if name == dr_index.INDEX_NAME and not swapped:
            swapped = True
            original_state.rename(displaced_state)
            _private_directory(original_state)
            _private_directory(original_state / dr_index.RECEIPT_DIRECTORY_NAME)
            (original_state / "replacement-marker").write_bytes(b"replacement")
        return real_read(
            directory_fd,
            name,
            mode=mode,
            maximum_bytes=maximum_bytes,
            code=code,
        )

    monkeypatch.setattr(dr_index, "_stable_private_file_at", swap_then_read)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.load()

    assert not (original_state / dr_index.INDEX_NAME).exists()
    assert (original_state / "replacement-marker").read_bytes() == b"replacement"
    assert (displaced_state / dr_index.INDEX_NAME).is_file()


def test_state_directory_swap_during_cas_never_writes_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 18)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    original_state = index.state_directory
    displaced_state = tmp_path / "state-displaced"
    real_replace = dr_index._replace_private_durable_at  # noqa: SLF001
    swapped = False

    def swap_then_replace(directory_fd: int, name: str, raw: bytes, *, code: str) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            original_state.rename(displaced_state)
            _private_directory(original_state)
            _private_directory(original_state / dr_index.RECEIPT_DIRECTORY_NAME)
            (original_state / "replacement-marker").write_bytes(b"replacement")
        real_replace(directory_fd, name, raw, code=code)

    monkeypatch.setattr(dr_index, "_replace_private_durable_at", swap_then_replace)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.record_authenticated(
            receipt=_authentication_receipt(candidate, 180),
            expected_journal_sha256=prepared["journal_sha256"],
        )

    assert not (original_state / dr_index.INDEX_NAME).exists()
    assert (original_state / "replacement-marker").read_bytes() == b"replacement"
    displaced_payload = json.loads((displaced_state / dr_index.INDEX_NAME).read_text(encoding="ascii"))
    assert displaced_payload["phase"] == "authenticated"


def test_receipt_directory_swap_during_publish_never_writes_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 19)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 190)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 191),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    state_raw = index.path.read_bytes()
    displaced_receipts = index.state_directory / "receipts-displaced"
    real_rename = dr_index._rename_noreplace  # noqa: SLF001
    swapped = False

    def swap_then_rename(directory_fd: int, source: str, destination: str) -> None:
        nonlocal swapped
        if destination != dr_index.INDEX_NAME and not swapped:
            swapped = True
            index.receipt_directory.rename(displaced_receipts)
            _private_directory(index.receipt_directory)
            (index.receipt_directory / "replacement-marker").write_bytes(b"replacement")
        real_rename(directory_fd, source, destination)

    monkeypatch.setattr(dr_index, "_rename_noreplace", swap_then_rename)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])

    generation_id = rehearsed["pending"]["generation"]["generation_id"]
    assert index.path.read_bytes() == state_raw
    assert not (index.receipt_directory / f"{generation_id}.json").exists()
    assert (index.receipt_directory / "replacement-marker").read_bytes() == b"replacement"
    assert (displaced_receipts / f"{generation_id}.json").stat().st_nlink == 1


def test_state_lock_serializes_two_writers_across_receipt_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_a = _index(tmp_path)
    index_b = dr_index.DurableDRGenerationIndex(index_a.state_directory)
    initial = index_a.load()
    candidate = _candidate(tmp_path, 21)
    prepared = index_a.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    real_replace = dr_index._replace_private_durable_at  # noqa: SLF001
    a_at_replace = threading.Event()
    allow_a_replace = threading.Event()
    b_started = threading.Event()
    b_done = threading.Event()
    outcomes: dict[str, object] = {}
    authentication_a = _authentication_receipt(candidate, 210)
    authentication_b = _authentication_receipt(candidate, 211)

    def gated_replace(directory_fd: int, name: str, raw: bytes, *, code: str) -> None:
        if threading.current_thread().name == "dr-writer-a":
            a_at_replace.set()
            if not allow_a_replace.wait(timeout=5):
                raise AssertionError("writer A gate timeout")
        real_replace(directory_fd, name, raw, code=code)

    monkeypatch.setattr(dr_index, "_replace_private_durable_at", gated_replace)

    def writer_a() -> None:
        try:
            outcomes["a"] = index_a.record_authenticated(
                receipt=authentication_a,
                expected_journal_sha256=prepared["journal_sha256"],
            )
        except BaseException as exc:  # noqa: BLE001 - exact concurrent outcome under test.
            outcomes["a"] = exc

    def writer_b() -> None:
        b_started.set()
        try:
            outcomes["b"] = index_b.record_authenticated(
                receipt=authentication_b,
                expected_journal_sha256=prepared["journal_sha256"],
            )
        except BaseException as exc:  # noqa: BLE001 - exact concurrent outcome under test.
            outcomes["b"] = exc
        finally:
            b_done.set()

    thread_a = threading.Thread(target=writer_a, name="dr-writer-a")
    thread_a.start()
    assert a_at_replace.wait(timeout=5)
    displaced_receipts = index_a.state_directory / "receipt-lock-domain-old"
    index_a.receipt_directory.rename(displaced_receipts)
    _private_directory(index_a.receipt_directory)

    thread_b = threading.Thread(target=writer_b, name="dr-writer-b")
    thread_b.start()
    assert b_started.wait(timeout=5)
    assert not b_done.wait(timeout=0.1), "writer B escaped the pinned state-directory lock"
    allow_a_replace.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    assert isinstance(outcomes["a"], dr_index.DRGenerationIndexError)
    assert "dr_generation_directory_changed" in str(outcomes["a"])
    assert isinstance(outcomes["b"], dr_index.DRGenerationIndexError)
    assert "authentication_receipt_invalid" in str(outcomes["b"])
    with pytest.raises(dr_index.DRGenerationIndexError, match="authentication_receipt_invalid"):
        index_b.load()
    durable = json.loads(index_b.path.read_text(encoding="ascii"))
    assert durable["phase"] == "authenticated"
    assert durable["revision"] == prepared["revision"] + 1
    assert durable["pending"]["authentication_receipt"] == _external_receipt_ref(authentication_a)
    displaced_body = displaced_receipts / f"authentication-{authentication_a['receipt_sha256']}.json"
    assert displaced_body.read_bytes() == _canonical(authentication_a) + b"\n"


@pytest.mark.parametrize(
    ("boundary", "staging_mode"),
    (("content_fsync_0600", 0o600), ("metadata_fsync_0400", 0o400)),
)
def test_evidence_body_staging_crash_boundaries_replay_after_ordered_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    staging_mode: int,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 27)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    body = _authentication_receipt(candidate, 270)
    body_raw = _canonical(body) + b"\n"
    receipt_name = f"authentication-{body['receipt_sha256']}.json"
    staging = index.receipt_directory / f".{receipt_name}.{hashlib.sha256(body_raw).hexdigest()}.new"
    real_fsync = os.fsync
    crashed = False

    def crash_at_boundary(descriptor: int) -> None:
        nonlocal crashed
        status = os.fstat(descriptor)
        mode = stat.S_IMODE(status.st_mode)
        real_fsync(descriptor)
        should_crash = (
            boundary == "content_fsync_0600" and stat.S_ISREG(status.st_mode) and mode == 0o600
        ) or (boundary == "metadata_fsync_0400" and stat.S_ISREG(status.st_mode) and mode == 0o400)
        if should_crash and not crashed:
            crashed = True
            raise OSError(f"crash at {boundary}")

    monkeypatch.setattr(dr_index.os, "fsync", crash_at_boundary)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="authentication_receipt_publication_failed",
    ):
        index.record_authenticated(
            receipt=body,
            expected_journal_sha256=prepared["journal_sha256"],
        )
    assert crashed
    assert staging.read_bytes() == body_raw
    assert stat.S_IMODE(staging.stat().st_mode) == staging_mode
    assert staging.stat().st_nlink == 1
    assert index.path.read_bytes() == _canonical(prepared) + b"\n"

    monkeypatch.setattr(dr_index.os, "fsync", real_fsync)
    authenticated = index.record_authenticated(
        receipt=body,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    durable = index.receipt_directory / receipt_name
    assert authenticated["phase"] == "authenticated"
    assert not staging.exists()
    assert durable.read_bytes() == body_raw
    assert durable.stat().st_nlink == 1
    assert stat.S_IMODE(durable.stat().st_mode) == 0o400


@pytest.mark.parametrize(
    ("boundary", "staging_mode"),
    (("content_fsync_0600", 0o600), ("metadata_fsync_0400", 0o400)),
)
def test_receipt_staging_crash_boundaries_recover_after_ordered_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    staging_mode: int,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 22)
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, 220)
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, authenticated, 221),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    pending = rehearsed["pending"]
    _reference, receipt_raw = dr_index._generation_receipt(  # noqa: SLF001
        {
            "authentication_receipt": pending["authentication_receipt"],
            "candidate": pending["candidate"],
            "rehearsal_binding": dr_index._rehearsal_binding(  # noqa: SLF001
                candidate=pending["candidate"],
                authentication_receipt=pending["authentication_receipt"],
                index_transaction_id=authenticated["transaction_id"],
                index_revision=authenticated["revision"],
                index_journal_sha256=authenticated["journal_sha256"],
            ),
            "rehearsal_receipt": pending["rehearsal_receipt"],
            "schema": dr_index.GENERATION_SCHEMA,
        }
    )
    generation_id = pending["generation"]["generation_id"]
    receipt_name = f"{generation_id}.json"
    staging = index.receipt_directory / f".{receipt_name}.{hashlib.sha256(receipt_raw).hexdigest()}.new"
    real_fsync = os.fsync
    crashed = False

    def crash_at_boundary(descriptor: int) -> None:
        nonlocal crashed
        status = os.fstat(descriptor)
        mode = stat.S_IMODE(status.st_mode)
        real_fsync(descriptor)
        should_crash = (
            boundary == "content_fsync_0600" and stat.S_ISREG(status.st_mode) and mode == 0o600
        ) or (boundary == "metadata_fsync_0400" and stat.S_ISREG(status.st_mode) and mode == 0o400)
        if should_crash and not crashed:
            crashed = True
            raise OSError(f"crash at {boundary}")

    monkeypatch.setattr(dr_index.os, "fsync", crash_at_boundary)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="generation_receipt_publication_failed",
    ):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    assert crashed
    assert staging.read_bytes() == receipt_raw
    assert stat.S_IMODE(staging.stat().st_mode) == staging_mode
    assert staging.stat().st_nlink == 1

    monkeypatch.setattr(dr_index.os, "fsync", real_fsync)
    clear = index.recover(expected_journal_sha256=rehearsed["journal_sha256"])
    receipt = index.receipt_directory / receipt_name
    assert clear["phase"] == "clear"
    assert not staging.exists()
    assert receipt.stat().st_nlink == 1
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o400
