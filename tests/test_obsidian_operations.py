from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from friday.organs.obsidian.contracts import (
    IdempotencyConflictError,
    PropertyType,
    PropertyValue,
    RevisionConflictError,
    VaultDeliveryState,
)
from friday.organs.obsidian.operations import (
    NoteSyncRequest,
    ObsidianOperationService,
    OperationCommitUncertain,
    OperationLedgerError,
    OperationTerminalError,
    _legacy_marker_row_proves_note,
    _pending_operation_paths,
    canonical_arguments_digest,
)
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.storage import FridayStorage


@dataclass
class FakeSync:
    observed: VaultDeliveryState = field(default_factory=VaultDeliveryState.local_only)
    requests: list[NoteSyncRequest] = field(default_factory=list)
    fail_scan: bool = False

    def request_scan(self, request: NoteSyncRequest) -> None:
        self.requests.append(request)
        if self.fail_scan:
            raise OSError("offline")

    def observe_delivery(self, _request: NoteSyncRequest) -> VaultDeliveryState:
        return self.observed


def _make_operations(
    storage: FridayStorage,
    tmp_path: Path,
    owner_id: str,
    *,
    sync: FakeSync | None = None,
    clock: Callable[[], date] | None = None,
) -> tuple[ObsidianOperationService, ObsidianService, dict]:
    storage.ensure_user(owner_id)
    root = tmp_path / f"vault-{owner_id}"
    selected_clock = clock or (lambda: date(2026, 8, 21))
    notes = ObsidianService(VaultStore(root), clock=selected_clock)
    bundle = storage.create_obsidian_bundle(
        owner_id,
        config_root=str(tmp_path / f"config-{owner_id}"),
        database_root=str(tmp_path / f"database-{owner_id}"),
        api_endpoint=f"unix://{tmp_path}/run-{owner_id}.sock",
        api_key_ref=f"secret:obsidian:{owner_id}",
        server_path=str(root),
        folder_id=f"friday-{owner_id}",
        setup_token_hash=hashlib.sha256(f"token:{owner_id}".encode()).hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    operations = ObsidianOperationService(
        storage,
        notes,
        owner_id=owner_id,
        sync=sync,
        clock=selected_clock,
    )
    return operations, notes, bundle


def test_create_persists_exact_state_chain_and_local_only_truth(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = FakeSync()
    operations, _, bundle = _make_operations(storage, tmp_path, "alice", sync=sync)
    transitions: list[str] = []
    original_transition = storage.transition_obsidian_operation

    def observe_transition(owner_id: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        transitions.append(state)
        return original_transition(owner_id, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", observe_transition)
    result = operations.create_note("op-create", "Projects/Friday", "private note body")

    assert transitions == ["committed", "scan_pending"]
    assert result.status == "scan_pending"
    assert result.replayed is False
    assert result.delivery == VaultDeliveryState.local_only()
    assert sync.requests == [
        NoteSyncRequest(
            owner_id="alice",
            vault_id=bundle["vault"]["id"],
            folder_id="friday-alice",
            note_path="Projects/Friday.md",
            revision=result.revision,
        )
    ]
    row = storage.get_obsidian_operation("alice", "op-create")
    assert row and row["status"] == "scan_pending"
    assert "private note body" not in row["result_json"]
    assert json.loads(row["delivery_json"]) == {
        "android_completion": None,
        "android_connected": False,
        "android_received": False,
        "local_write_complete": True,
        "obsidian_opened": False,
        "server_scan_complete": False,
    }


def test_workflow_delete_tracks_and_delivers_the_exact_syncthing_tombstone(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    delivered = VaultDeliveryState(
        local_write_complete=True,
        server_scan_complete=True,
        android_connected=True,
        android_completion=100.0,
        android_received=True,
        obsidian_opened=False,
    )
    sync = FakeSync(observed=delivered)
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice", sync=sync)
    created = notes.create_note("Scratch/Delete Me.md", "temporary")

    committed = operations.delete_note(
        "delete-delivery",
        created.path,
        expected_revision=created.revision,
    )
    refreshed = operations.refresh_delivery("delete-delivery")

    assert committed.status == "committed"
    assert refreshed.status == "delivered"
    assert refreshed.delivery == delivered
    assert sync.requests
    tombstones = [request for request in sync.requests if request.deleted]
    assert tombstones
    assert tombstones[-1] == NoteSyncRequest(
        owner_id="alice",
        vault_id=bundle["vault"]["id"],
        folder_id="friday-alice",
        note_path="Scratch/Delete Me.md",
        revision=created.revision,
        deleted=True,
    )


def test_move_delivery_waits_for_every_changed_path(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    delivered = VaultDeliveryState(
        local_write_complete=True,
        server_scan_complete=True,
        android_connected=True,
        android_completion=100.0,
        android_received=True,
        obsidian_opened=False,
    )

    @dataclass
    class PerPathSync(FakeSync):
        pending_path: str = "Notes/Search.md"

        def observe_delivery(self, request: NoteSyncRequest) -> VaultDeliveryState:
            return VaultDeliveryState.local_only() if request.note_path == self.pending_path else delivered

    sync = PerPathSync()
    operations, notes, _bundle = _make_operations(storage, tmp_path, "alice", sync=sync)
    target = notes.create_note("Projects/Friday.md", "target")
    notes.create_note("Notes/Search.md", "[[Projects/Friday]]")
    moved = operations.move_note(
        "move-delivery",
        target.path,
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )

    pending = operations.refresh_delivery(moved.operation_id)
    assert pending.status == "scan_pending"
    assert pending.delivery.android_received is False
    assert {request.note_path for request in sync.requests} == set(moved.changed_paths)
    assert any(request.note_path == target.path and request.deleted for request in sync.requests)

    sync.pending_path = ""
    completed = operations.refresh_delivery(moved.operation_id)
    assert completed.status == "delivered"
    assert completed.delivery == delivered


def test_identical_canonical_arguments_replay_without_a_second_mutation(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    first = operations.create_note(
        "same-id",
        "Canonical",
        "body",
        properties={"zeta": 2, "alpha": ["a", "b"]},
    )
    replay = operations.create_note(
        "same-id",
        "Canonical.md",
        "body",
        properties={"alpha": ["a", "b"], "zeta": 2},
    )

    assert replay.replayed is True
    assert replay.revision == first.revision
    assert len(notes.list_notes()) == 1

    with pytest.raises(IdempotencyConflictError):
        operations.create_note("same-id", "Canonical", "different body")
    assert notes.read_note("Canonical").body.count("body") == 1


def test_canonical_digest_is_order_independent_and_type_sensitive() -> None:
    first = canonical_arguments_digest(
        {
            "properties": {
                "tags": PropertyValue(PropertyType.LIST, ("a", "b")),
                "due": PropertyValue(PropertyType.DATE, date(2026, 8, 22)),
            },
            "path": "Note.md",
        }
    )
    second = canonical_arguments_digest(
        {
            "path": "Note.md",
            "properties": {
                "due": PropertyValue(PropertyType.DATE, date(2026, 8, 22)),
                "tags": PropertyValue(PropertyType.LIST, ("a", "b")),
            },
        }
    )
    text_date = canonical_arguments_digest(
        {"path": "Note.md", "properties": {"due": PropertyValue(PropertyType.TEXT, "2026-08-22")}}
    )

    assert first == second
    assert first != text_date
    assert len(first) == 64


def test_operation_id_is_normalized_before_ledger_and_filesystem_marker(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")

    first = operations.create_note("  stable-id  ", "Normalized", "one")
    replay = operations.create_note("stable-id", "Normalized", "one")

    assert first.operation_id == "stable-id"
    assert replay.replayed is True
    assert notes.read_note("Normalized").body.count("one") == 1
    assert storage.get_obsidian_operation("alice", "stable-id") is not None


def test_append_uses_expected_revision_and_terminal_conflict_replays(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("create", "Log", "first")
    appended = operations.append_note(
        "append-1",
        "Log",
        "second",
        expected_revision=created.revision,
    )
    before_conflict = notes.read_note("Log").content

    with pytest.raises(RevisionConflictError):
        operations.append_note(
            "append-stale",
            "Log",
            "must not appear",
            expected_revision=created.revision,
        )

    row = storage.get_obsidian_operation("alice", "append-stale")
    assert row and row["status"] == "conflict"
    assert json.loads(row["result_json"])["error"] == "revision_conflict"
    assert notes.read_note("Log").content == before_conflict
    assert appended.revision != created.revision
    with pytest.raises(OperationTerminalError) as terminal:
        operations.append_note(
            "append-stale",
            "Log",
            "must not appear",
            expected_revision=created.revision,
        )
    assert terminal.value.status == "conflict"


def test_prepared_append_reconciles_after_receipt_commit_failure(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("create", "Recovery", "start")
    original_transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_first_receipt(owner_id: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "append-recovery" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic SQLite interruption")
        return original_transition(owner_id, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_first_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.append_note(
            "append-recovery",
            "Recovery",
            "once only",
            expected_revision=created.revision,
        )
    prepared = storage.get_obsidian_operation("alice", "append-recovery")
    assert prepared is not None and prepared["status"] == "prepared"

    recovered = operations.append_note(
        "append-recovery",
        "Recovery",
        "once only",
        expected_revision=created.revision,
    )

    assert recovered.status == "scan_pending"
    assert recovered.replayed is True
    assert notes.read_note("Recovery").body.count("once only") == 1
    assert "<!-- friday:" not in notes.read_note("Recovery").content


def test_observe_only_create_settles_an_exact_prepared_sidecar_without_vault_write(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    receipt_store = notes._receipt_store()  # noqa: SLF001 - fault injection at the durable seam
    original_commit = receipt_store.commit
    fail_once = True

    def lose_sidecar_commit(operation_digest: str):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic sidecar commit interruption")
        return original_commit(operation_digest)

    monkeypatch.setattr(receipt_store, "commit", lose_sidecar_commit)
    with pytest.raises(OperationCommitUncertain):
        operations.create_note("create-prepared-sidecar", "Recovery/Create", "exact target")

    operation_digest = hashlib.sha256(b"create-prepared-sidecar").hexdigest()
    prepared_receipt = receipt_store.inspect(operation_digest)
    assert prepared_receipt is not None and prepared_receipt.state == "prepared"
    before = notes.read_note("Recovery/Create.md")

    def forbid_vault_write(*_args: Any, **_kwargs: Any):
        raise AssertionError("observe-only reconciliation attempted a vault write")

    monkeypatch.setattr(notes.store, "write_text", forbid_vault_write)
    reconciled = operations.reconcile_local_effect("create-prepared-sidecar")

    assert reconciled.status == "scan_pending"
    assert reconciled.replayed is True
    assert reconciled.revision == before.revision
    assert reconciled.applied is False
    assert notes.read_note("Recovery/Create.md").content == before.content
    committed_receipt = receipt_store.inspect(operation_digest)
    assert committed_receipt is not None and committed_receipt.state == "committed"
    row = storage.get_obsidian_operation("alice", "create-prepared-sidecar")
    assert row is not None
    payload = json.loads(row["result_json"])
    assert payload == {
        "applied": False,
        "created": True,
        "path": "Recovery/Create.md",
        "previous_revision": None,
        "reconciliation_proof": "sidecar_committed",
        "reconciliation_state": "settled",
        "revision": before.revision,
        "schema": "friday.obsidian-note-operation.v2",
        "side_effect_receipt_sha256": payload["side_effect_receipt_sha256"],
        "sidecar_arguments_sha256": before.revision,
    }
    assert len(payload["side_effect_receipt_sha256"]) == 64
    assert operations.get_operation("create-prepared-sidecar") == reconciled
    assert _pending_operation_paths(row) == frozenset({"Recovery/Create.md"})

    marker = f'<!-- friday:create operation="{operation_digest}" arguments="{before.revision}" -->'
    marked_content = before.content + marker + "\n"
    match = re.search(r"<!-- friday:create .*? -->", marked_content)
    assert match is not None
    assert _legacy_marker_row_proves_note(
        row,
        path=before.path,
        revision=hashlib.sha256(marked_content.encode()).hexdigest(),
        content=marked_content,
        marker=match,
    )


def test_committed_append_sidecar_remains_historical_proof_after_a_later_edit(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("history-create", "Recovery/History", "start")
    original_transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_main_receipt(owner_id: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "history-append" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic main receipt interruption")
        return original_transition(owner_id, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_main_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.append_note(
            "history-append",
            "Recovery/History.md",
            "accepted once",
            expected_revision=created.revision,
        )
    target = notes.read_note("Recovery/History.md")
    receipt_store = notes._receipt_store()  # noqa: SLF001 - inspect exact historical proof
    operation_digest = hashlib.sha256(b"history-append").hexdigest()
    sidecar = receipt_store.inspect(operation_digest)
    assert sidecar is not None and sidecar.state == "committed"

    monkeypatch.setattr(storage, "transition_obsidian_operation", original_transition)
    edited = notes.store.write_text(
        target.path,
        target.content + "\nmanual later edit\n",
        expected_revision=target.revision,
    )

    def forbid_vault_write(*_args: Any, **_kwargs: Any):
        raise AssertionError("historical reconciliation attempted a vault write")

    monkeypatch.setattr(notes.store, "write_text", forbid_vault_write)
    reconciled = operations.reconcile_local_effect("history-append")

    assert reconciled.status == "scan_pending"
    assert reconciled.revision == target.revision
    assert reconciled.previous_revision == created.revision
    assert reconciled.applied is False
    assert notes.read_note(target.path).revision == edited.revision
    assert "manual later edit" in notes.read_note(target.path).body
    row = storage.get_obsidian_operation("alice", "history-append")
    assert row is not None
    payload = json.loads(row["result_json"])
    assert payload["schema"] == "friday.obsidian-note-operation.v2"
    assert payload["reconciliation_proof"] == "sidecar_committed"
    assert payload["sidecar_arguments_sha256"] == sidecar.arguments_digest
    assert operations.get_operation("history-append") == reconciled


@pytest.mark.parametrize("sidecar_mode", ("missing", "identity_mismatch", "target_mismatch"))
def test_unproved_append_stays_uncertain_and_never_mutates_the_vault(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar_mode: str,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    notes.create_note("Recovery/Unproved.md", "base")
    current = notes.read_note("Recovery/Unproved.md")
    target_content = notes.render_append_content(current.content, "must not be replayed")
    target_revision = hashlib.sha256(target_content.encode()).hexdigest()
    operation_id = f"append-{sidecar_mode}"
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(bundle["vault"]["id"]),
        method="append",
        arguments_digest="3" * 64,
        expected_revision=current.revision,
        prepared_result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": current.path,
            "target_revision": target_revision,
            "base_revision": current.revision,
        },
    )
    if sidecar_mode != "missing":
        notes._receipt_store().prepare(  # noqa: SLF001 - deliberate corrupt-proof fixture
            operation_digest=hashlib.sha256(operation_id.encode()).hexdigest(),
            method="append",
            arguments_digest="4" * 64,
            note_path=current.path,
            base_revision=current.revision,
            target_revision=("5" * 64 if sidecar_mode == "identity_mismatch" else target_revision),
            created=False,
        )
    before = notes.read_note(current.path)

    def forbid_vault_write(*_args: Any, **_kwargs: Any):
        raise AssertionError("uncertain reconciliation attempted a vault write")

    monkeypatch.setattr(notes.store, "write_text", forbid_vault_write)
    with pytest.raises(OperationCommitUncertain):
        operations.reconcile_local_effect(operation_id)

    row = storage.get_obsidian_operation("alice", operation_id)
    assert row is not None and row["status"] == "uncertain"
    assert json.loads(row["result_json"]) == {
        "base_revision": current.revision,
        "error": "local_commit_uncertain",
        "path": current.path,
        "schema": "friday.obsidian-note-operation.v1",
        "target_revision": target_revision,
    }
    with pytest.raises(OperationCommitUncertain):
        operations.reconcile_local_effect(operation_id)
    assert notes.read_note(current.path).content == before.content


@pytest.mark.parametrize("exact", (True, False))
def test_sidecar_free_legacy_create_requires_the_exact_frozen_revision(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact: bool,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    rendered = notes.render_create_content("legacy exact bytes")
    current = notes.store.write_text("Recovery/Legacy.md", rendered, create_only=True)
    target_revision = current.revision if exact else "6" * 64
    operation_id = f"legacy-create-{'exact' if exact else 'mismatch'}"
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="7" * 64,
        prepared_result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": current.path,
            "target_revision": target_revision,
        },
    )

    def forbid_vault_write(*_args: Any, **_kwargs: Any):
        raise AssertionError("legacy observation attempted a vault write")

    monkeypatch.setattr(notes.store, "write_text", forbid_vault_write)
    if not exact:
        with pytest.raises(OperationCommitUncertain):
            operations.reconcile_local_effect(operation_id)
        row = storage.get_obsidian_operation("alice", operation_id)
        assert row is not None and row["status"] == "uncertain"
        assert notes.read_note(current.path).content == rendered
        return

    reconciled = operations.reconcile_local_effect(operation_id)
    row = storage.get_obsidian_operation("alice", operation_id)
    assert row is not None and row["status"] == "scan_pending"
    payload = json.loads(row["result_json"])
    assert reconciled.revision == current.revision
    assert payload["reconciliation_proof"] == "legacy_exact_revision"
    assert payload["sidecar_arguments_sha256"] is None
    assert operations.get_operation(operation_id) == reconciled
    assert notes.read_note(current.path).content == rendered


def test_result_reader_rejects_widened_or_inconsistent_v1_payloads(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, _notes, _ = _make_operations(storage, tmp_path, "alice")
    result = operations.create_note("strict-v1", "Strict/V1.md", "body")
    row = storage.get_obsidian_operation("alice", result.operation_id)
    assert row is not None
    payload = json.loads(row["result_json"])
    payload["private_body"] = "must never be admitted"
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE obsidian_operations SET result_json=? WHERE user_id=? AND id=?",
            (json.dumps(payload, sort_keys=True), "alice", result.operation_id),
        )
    with pytest.raises(OperationLedgerError, match="v1 fields"):
        operations.get_operation(result.operation_id)

    del payload["private_body"]
    payload["target_revision"] = "8" * 64
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE obsidian_operations SET result_json=? WHERE user_id=? AND id=?",
            (json.dumps(payload, sort_keys=True), "alice", result.operation_id),
        )
    with pytest.raises(OperationLedgerError, match="target revision"):
        operations.get_operation(result.operation_id)

    payload["target_revision"] = result.revision
    duplicate = json.dumps(payload, sort_keys=True).replace(
        '"schema":',
        '"schema":"duplicate","schema":',
        1,
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE obsidian_operations SET result_json=? WHERE user_id=? AND id=?",
            (duplicate, "alice", result.operation_id),
        )
    with pytest.raises(OperationLedgerError, match="duplicate key"):
        operations.get_operation(result.operation_id)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("private_body", "forbidden", "v2 fields"),
        ("reconciliation_state", "required", "settled"),
        ("side_effect_receipt_sha256", "9" * 64, "receipt digest"),
        ("sidecar_arguments_sha256", "a" * 64, "legacy proof shape"),
        ("created", False, "mutation shape"),
    ),
)
def test_result_reader_strictly_rejects_tampered_v2_settled_proof(
    storage: FridayStorage,
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    current = notes.store.write_text("Strict/V2.md", "exact", create_only=True)
    storage.prepare_obsidian_operation(
        "alice",
        operation_id="strict-v2",
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="b" * 64,
        prepared_result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": current.path,
            "target_revision": current.revision,
        },
    )
    operations.reconcile_local_effect("strict-v2")
    row = storage.get_obsidian_operation("alice", "strict-v2")
    assert row is not None
    payload = json.loads(row["result_json"])
    payload[field] = value
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE obsidian_operations SET result_json=? WHERE user_id=? AND id=?",
            (json.dumps(payload, sort_keys=True), "alice", "strict-v2"),
        )

    with pytest.raises(OperationLedgerError, match=message):
        operations.get_operation("strict-v2")


def test_legacy_marker_migration_cas_cleans_body_and_restarts_delivery(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    sync = FakeSync()
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice", sync=sync)
    operation_id = "legacy-create"
    operation_digest = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments_digest = hashlib.sha256(b"legacy body").hexdigest()
    marker = f'<!-- friday:create operation="{operation_digest}" arguments="{arguments_digest}" -->'
    legacy = notes.store.write_text(
        "Legacy.md",
        f"legacy body\n{marker}\n",
        create_only=True,
    )
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="a" * 64,
    )
    local = {
        "local_write_complete": True,
        "server_scan_complete": False,
        "android_connected": False,
        "android_completion": None,
        "android_received": False,
        "obsidian_opened": False,
    }
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "committed",
        result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": "Legacy.md",
            "revision": legacy.revision,
            "previous_revision": None,
            "created": True,
            "applied": True,
        },
        delivery=local,
    )
    storage.transition_obsidian_operation("alice", operation_id, "scan_pending")
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": False,
    }
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "scan_complete",
        delivery=delivered,
    )
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "delivered",
        delivery=delivered,
    )
    historical = storage.get_obsidian_operation("alice", operation_id)
    assert historical is not None

    migrated = operations.migrate_legacy_operation_markers()

    current = notes.read_note("Legacy.md")
    row = storage.get_obsidian_operation("alice", operation_id)
    cleanup_rows = [
        item for item in storage.list_obsidian_operations("alice", limit=20) if item["method"] == "replace"
    ]
    assert migrated == 1
    assert current.content == "legacy body\n"
    assert "<!-- friday:" not in current.content
    assert row == historical
    assert len(cleanup_rows) == 1
    assert cleanup_rows[0]["status"] == "scan_pending"
    assert json.loads(cleanup_rows[0]["result_json"])["revision"] == current.revision
    assert sync.requests[-1].revision == current.revision


def test_multi_marker_cleanup_is_one_new_operation_and_preserves_historical_receipts(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    first_id, second_id = "legacy-first", "legacy-second"

    def marker(operation_id: str, method: str, payload: str) -> str:
        operation = hashlib.sha256(operation_id.encode()).hexdigest()
        arguments = hashlib.sha256(payload.encode()).hexdigest()
        return f'<!-- friday:{method} operation="{operation}" arguments="{arguments}" -->'

    first_marker = marker(first_id, "create", "first")
    second_marker = marker(second_id, "append", "second")
    first = notes.store.write_text(
        "History.md",
        f"first\n{first_marker}\n",
        create_only=True,
    )
    second = notes.store.write_text(
        "History.md",
        f"first\n{first_marker}\nsecond\n{second_marker}\n",
        expected_revision=first.revision,
    )
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": False,
    }

    def delivered_row(operation_id: str, method: str, revision: str, previous: str | None) -> dict:
        storage.prepare_obsidian_operation(
            "alice",
            operation_id=operation_id,
            vault_id=str(bundle["vault"]["id"]),
            method=method,
            arguments_digest=hashlib.sha256(f"ledger:{operation_id}".encode()).hexdigest(),
            expected_revision=previous,
        )
        result = {
            "schema": "friday.obsidian-note-operation.v1",
            "path": "History.md",
            "revision": revision,
            "previous_revision": previous,
            "created": method == "create",
            "applied": True,
        }
        storage.transition_obsidian_operation(
            "alice", operation_id, "committed", result=result, delivery=delivered
        )
        storage.transition_obsidian_operation("alice", operation_id, "scan_pending")
        storage.transition_obsidian_operation("alice", operation_id, "scan_complete", delivery=delivered)
        return storage.transition_obsidian_operation("alice", operation_id, "delivered", delivery=delivered)

    first_history = delivered_row(first_id, "create", first.revision, None)
    second_history = delivered_row(second_id, "append", second.revision, first.revision)

    assert operations.migrate_legacy_operation_markers() == 1
    clean_revision = notes.read_note("History.md").revision
    stable_content = notes.read_note("History.md").content
    assert operations.migrate_legacy_operation_markers() == 0

    assert stable_content == "first\nsecond\n"
    assert notes.read_note("History.md").content == stable_content
    assert storage.get_obsidian_operation("alice", first_id) == first_history
    assert storage.get_obsidian_operation("alice", second_id) == second_history
    cleanup_rows = [
        item for item in storage.list_obsidian_operations("alice", limit=20) if item["method"] == "replace"
    ]
    assert len(cleanup_rows) == 1
    assert json.loads(cleanup_rows[0]["result_json"])["revision"] == clean_revision


def test_marker_cleanup_recovers_file_to_ledger_receipt_gap_without_second_write(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    operation_id = "legacy-gap-proof"
    operation = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments = hashlib.sha256(b"body").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    legacy = notes.store.write_text("Gap.md", f"body\n{marker}\n", create_only=True)
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="b" * 64,
    )
    result = {
        "schema": "friday.obsidian-note-operation.v1",
        "path": "Gap.md",
        "revision": legacy.revision,
        "previous_revision": None,
        "created": True,
        "applied": True,
    }
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "committed",
        result=result,
        delivery={
            "local_write_complete": True,
            "server_scan_complete": False,
            "android_connected": False,
            "android_completion": None,
            "android_received": False,
            "obsidian_opened": False,
        },
    )
    storage.transition_obsidian_operation("alice", operation_id, "scan_pending")
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": False,
    }
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "scan_complete",
        delivery=delivered,
    )
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "delivered",
        delivery=delivered,
    )
    historical = storage.get_obsidian_operation("alice", operation_id)
    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_cleanup_receipt(owner_id: str, op_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if op_id.startswith("legacy-marker-cleanup:") and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic cleanup receipt interruption")
        return transition(owner_id, op_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_cleanup_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.migrate_legacy_operation_markers()
    clean = notes.read_note("Gap.md")
    assert clean.content == "body\n"

    recovered = operations.migrate_legacy_operation_markers()
    cleanup_rows = [
        item for item in storage.list_obsidian_operations("alice", limit=20) if item["method"] == "replace"
    ]
    assert recovered == 1
    assert len(cleanup_rows) == 1
    assert cleanup_rows[0]["status"] == "scan_pending"
    assert notes.read_note("Gap.md").revision == clean.revision
    assert storage.get_obsidian_operation("alice", operation_id) == historical


def test_missing_prepared_cleanup_is_skipped_while_another_cleanup_recovers(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")

    def prepare_cleanup(path: str, proof_id: str) -> tuple[str, str]:
        operation = hashlib.sha256(proof_id.encode()).hexdigest()
        arguments_digest = hashlib.sha256(path.encode()).hexdigest()
        marker = f'<!-- friday:create operation="{operation}" arguments="{arguments_digest}" -->'
        source = notes.store.write_text(path, f"body\n{marker}\n", create_only=True)
        cleaned = "body\n"
        cleanup = {
            "path": path,
            "source_revision": source.revision,
            "target_revision": hashlib.sha256(cleaned.encode()).hexdigest(),
            "markers": [marker],
            "proof_operation_ids": [proof_id],
        }
        arguments = {"method": "legacy_marker_cleanup", **cleanup}
        digest = canonical_arguments_digest(arguments)
        cleanup_id = f"legacy-marker-cleanup:{digest}"
        storage.prepare_obsidian_operation(
            "alice",
            operation_id=cleanup_id,
            vault_id=str(bundle["vault"]["id"]),
            method="replace",
            arguments_digest=digest,
            expected_revision=source.revision,
            prepared_result={
                "schema": "friday.obsidian-note-operation.v1",
                "legacy_marker_cleanup": cleanup,
            },
        )
        return cleanup_id, source.revision

    missing_id, missing_revision = prepare_cleanup("Missing.md", "missing-proof")
    surviving_id, _ = prepare_cleanup("Surviving.md", "surviving-proof")
    notes.store.delete("Missing.md", expected_revision=missing_revision)

    assert operations.migrate_legacy_operation_markers() == 1
    assert not notes.store.exists("Missing.md")
    assert notes.read_note("Surviving.md").content == "body\n"
    missing_row = storage.get_obsidian_operation("alice", missing_id)
    surviving_row = storage.get_obsidian_operation("alice", surviving_id)
    assert missing_row is not None and missing_row["status"] == "prepared"
    assert surviving_row is not None and surviving_row["status"] == "scan_pending"

    later_id = "later-delivered-proof"
    operation = hashlib.sha256(later_id.encode()).hexdigest()
    arguments_digest = hashlib.sha256(b"later").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments_digest}" -->'
    later = notes.store.write_text("Later.md", f"later\n{marker}\n", create_only=True)
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=later_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="9" * 64,
    )
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": False,
    }
    storage.transition_obsidian_operation(
        "alice",
        later_id,
        "committed",
        result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": later.path,
            "revision": later.revision,
            "previous_revision": None,
            "created": True,
            "applied": True,
        },
        delivery=delivered,
    )
    storage.transition_obsidian_operation("alice", later_id, "scan_pending")
    storage.transition_obsidian_operation("alice", later_id, "scan_complete", delivery=delivered)
    storage.transition_obsidian_operation("alice", later_id, "delivered", delivery=delivered)

    assert operations.migrate_legacy_operation_markers() == 1
    assert notes.read_note("Later.md").content == "later\n"
    assert storage.get_obsidian_operation("alice", missing_id) == missing_row


def test_migration_cleans_delivered_legacy_verification_note_without_schema(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    operation_id = "verify:obssetup_production"
    clean = "# Friday Connection Test\n\nConnection works.\n"
    operation = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments = hashlib.sha256(clean.encode()).hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    legacy = notes.store.write_text(
        "Friday Connection Test.md",
        f"{clean}{marker}\n",
        create_only=True,
    )
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(bundle["vault"]["id"]),
        method="verification_note",
        arguments_digest="c" * 64,
    )
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": True,
    }
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "committed",
        result={"path": legacy.path, "revision": legacy.revision, "applied": True},
        delivery=delivered,
    )
    storage.transition_obsidian_operation("alice", operation_id, "scan_pending")
    storage.transition_obsidian_operation("alice", operation_id, "scan_complete", delivery=delivered)
    storage.transition_obsidian_operation("alice", operation_id, "delivered", delivery=delivered)

    assert operations.migrate_legacy_operation_markers() == 1
    assert notes.read_note("Friday Connection Test.md").content == clean


def test_migration_leaves_pending_marker_revision_reachable(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    operation_id = "pending-legacy"
    operation = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments = hashlib.sha256(b"pending").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    legacy = notes.store.write_text("Pending.md", f"pending\n{marker}\n", create_only=True)
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="d" * 64,
    )
    storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "committed",
        result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": legacy.path,
            "revision": legacy.revision,
            "previous_revision": None,
            "created": True,
            "applied": True,
        },
        delivery={
            "local_write_complete": True,
            "server_scan_complete": False,
            "android_connected": False,
            "android_completion": None,
            "android_received": False,
            "obsidian_opened": False,
        },
    )
    storage.transition_obsidian_operation("alice", operation_id, "scan_pending")
    historical = storage.get_obsidian_operation("alice", operation_id)

    assert operations.migrate_legacy_operation_markers() == 0
    assert marker in notes.read_note("Pending.md").content
    assert storage.get_obsidian_operation("alice", operation_id) == historical


def test_migration_skips_whole_note_with_delivered_and_pending_markers(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")

    def marker(operation_id: str, method: str, payload: str) -> str:
        operation = hashlib.sha256(operation_id.encode()).hexdigest()
        arguments = hashlib.sha256(payload.encode()).hexdigest()
        return f'<!-- friday:{method} operation="{operation}" arguments="{arguments}" -->'

    delivered_id, pending_id = "mixed-delivered", "mixed-pending"
    delivered_marker = marker(delivered_id, "create", "first")
    pending_marker = marker(pending_id, "append", "second")
    first = notes.store.write_text("Mixed.md", f"first\n{delivered_marker}\n", create_only=True)
    current = notes.store.write_text(
        "Mixed.md",
        f"first\n{delivered_marker}\nsecond\n{pending_marker}\n",
        expected_revision=first.revision,
    )
    local = {
        "local_write_complete": True,
        "server_scan_complete": False,
        "android_connected": False,
        "android_completion": None,
        "android_received": False,
        "obsidian_opened": False,
    }
    complete = {
        **local,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
    }

    for operation_id, method, revision, previous in (
        (delivered_id, "create", first.revision, None),
        (pending_id, "append", current.revision, first.revision),
    ):
        storage.prepare_obsidian_operation(
            "alice",
            operation_id=operation_id,
            vault_id=str(bundle["vault"]["id"]),
            method=method,
            arguments_digest=hashlib.sha256(f"ledger:{operation_id}".encode()).hexdigest(),
            expected_revision=previous,
        )
        storage.transition_obsidian_operation(
            "alice",
            operation_id,
            "committed",
            result={
                "schema": "friday.obsidian-note-operation.v1",
                "path": "Mixed.md",
                "revision": revision,
                "previous_revision": previous,
                "created": method == "create",
                "applied": True,
            },
            delivery=local,
        )
        storage.transition_obsidian_operation("alice", operation_id, "scan_pending")
    storage.transition_obsidian_operation(
        "alice",
        delivered_id,
        "scan_complete",
        delivery=complete,
    )
    storage.transition_obsidian_operation(
        "alice",
        delivered_id,
        "delivered",
        delivery=complete,
    )
    before = notes.read_note("Mixed.md")

    assert operations.migrate_legacy_operation_markers() == 0
    after = notes.read_note("Mixed.md")
    assert after.content == before.content
    assert after.revision == before.revision == current.revision


def test_migration_skips_legacy_note_with_new_marker_free_pending_append(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    legacy_id = "legacy-before-marker-free-append"
    operation = hashlib.sha256(legacy_id.encode()).hexdigest()
    arguments = hashlib.sha256(b"legacy").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    legacy = notes.store.write_text("MixedMarkerFree.md", f"legacy\n{marker}\n", create_only=True)
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=legacy_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="e" * 64,
    )
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": False,
    }
    storage.transition_obsidian_operation(
        "alice",
        legacy_id,
        "committed",
        result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": legacy.path,
            "revision": legacy.revision,
            "previous_revision": None,
            "created": True,
            "applied": True,
        },
        delivery=delivered,
    )
    storage.transition_obsidian_operation("alice", legacy_id, "scan_pending")
    storage.transition_obsidian_operation("alice", legacy_id, "scan_complete", delivery=delivered)
    storage.transition_obsidian_operation("alice", legacy_id, "delivered", delivery=delivered)

    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_main_receipt(owner_id: str, op_id: str, state: str, **values: Any) -> dict:
        nonlocal fail_once
        if op_id == "new-marker-free-append" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic main-ledger interruption")
        return transition(owner_id, op_id, state, **values)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_main_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.append_note(
            "new-marker-free-append",
            legacy.path,
            "new text",
            expected_revision=legacy.revision,
        )
    before = notes.read_note(legacy.path)
    prepared = storage.get_obsidian_operation("alice", "new-marker-free-append")
    assert prepared is not None and prepared["status"] == "prepared"
    assert before.content.count("<!-- friday:") == 1

    assert operations.migrate_legacy_operation_markers() == 0
    replay = operations.append_note(
        "new-marker-free-append",
        legacy.path,
        "new text",
        expected_revision=legacy.revision,
    )
    assert replay.status == "scan_pending"
    assert replay.replayed is True
    assert operations.migrate_legacy_operation_markers() == 0
    after = notes.read_note(legacy.path)
    assert after.content == before.content
    assert after.revision == before.revision == replay.revision
    assert after.content.count("new text") == 1
    assert not any(row["method"] == "replace" for row in storage.list_obsidian_operations("alice", limit=20))


def test_stale_pending_create_and_delete_do_not_block_cleanup_of_another_path(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, bundle = _make_operations(storage, tmp_path, "alice")
    legacy_id = "production-legacy-note"
    operation = hashlib.sha256(legacy_id.encode()).hexdigest()
    arguments = hashlib.sha256(b"research").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    legacy = notes.store.write_text("Research.md", f"research\n{marker}\n", create_only=True)
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=legacy_id,
        vault_id=str(bundle["vault"]["id"]),
        method="create",
        arguments_digest="f" * 64,
    )
    delivered = {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": True,
        "android_completion": 100.0,
        "android_received": True,
        "obsidian_opened": False,
    }
    storage.transition_obsidian_operation(
        "alice",
        legacy_id,
        "committed",
        result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": legacy.path,
            "revision": legacy.revision,
            "previous_revision": None,
            "created": True,
            "applied": True,
        },
        delivery=delivered,
    )
    storage.transition_obsidian_operation("alice", legacy_id, "scan_pending")
    storage.transition_obsidian_operation("alice", legacy_id, "scan_complete", delivery=delivered)
    storage.transition_obsidian_operation("alice", legacy_id, "delivered", delivery=delivered)

    stale = operations.create_note("stale-create", "Deleted.md", "temporary")
    deleted = operations.delete_note(
        "stale-delete",
        stale.path,
        expected_revision=stale.revision,
    )
    assert stale.status == "scan_pending"
    assert deleted.status == "committed"
    assert not notes.store.exists(stale.path)

    assert operations.migrate_legacy_operation_markers() == 1
    assert notes.read_note("Research.md").content == "research\n"


def test_prepared_property_write_reconciles_by_typed_postcondition(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("create", "Metadata", "body")
    body_before_properties = notes.read_note("Metadata").body
    original_transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_first_receipt(owner_id: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "properties" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic SQLite interruption")
        return original_transition(owner_id, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_first_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.set_properties(
            "properties",
            "Metadata",
            {"status": "review", "done": False},
            expected_revision=created.revision,
        )

    recovered = operations.set_properties(
        "properties",
        "Metadata",
        {"done": False, "status": "review"},
        expected_revision=created.revision,
    )

    assert recovered.status == "scan_pending"
    assert recovered.replayed is True
    assert notes.read_note("Metadata").properties["status"].value == "review"
    assert notes.read_note("Metadata").body == body_before_properties


def test_prepared_property_write_crash_before_mutation_retries_at_exact_revision(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("create", "PreparedMetadata", "body")
    original_set_properties = notes.set_properties

    class SyntheticCrash(BaseException):
        pass

    def crash_before_write(*_args: Any, **_kwargs: Any) -> Any:
        raise SyntheticCrash

    monkeypatch.setattr(notes, "set_properties", crash_before_write)
    with pytest.raises(SyntheticCrash):
        operations.set_properties(
            "properties-before-write",
            "PreparedMetadata",
            {"status": "review"},
            expected_revision=created.revision,
        )
    prepared = storage.get_obsidian_operation("alice", "properties-before-write")
    assert prepared is not None and prepared["status"] == "prepared"

    monkeypatch.setattr(notes, "set_properties", original_set_properties)
    recovered = operations.set_properties(
        "properties-before-write",
        "PreparedMetadata",
        {"status": "review"},
        expected_revision=created.revision,
    )

    assert recovered.status == "scan_pending"
    assert recovered.replayed is True
    assert recovered.previous_revision == created.revision
    assert notes.read_note("PreparedMetadata").properties["status"].value == "review"


def test_prepared_property_write_crash_with_divergent_revision_becomes_conflict(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("create", "DivergentMetadata", "body")
    original_set_properties = notes.set_properties

    class SyntheticCrash(BaseException):
        pass

    def crash_before_write(*_args: Any, **_kwargs: Any) -> Any:
        raise SyntheticCrash

    monkeypatch.setattr(notes, "set_properties", crash_before_write)
    with pytest.raises(SyntheticCrash):
        operations.set_properties(
            "properties-divergent",
            "DivergentMetadata",
            {"status": "review"},
            expected_revision=created.revision,
        )
    monkeypatch.setattr(notes, "set_properties", original_set_properties)
    external = notes.append_note(
        "DivergentMetadata",
        "Android edit",
        operation_id="external-android-edit",
        expected_revision=created.revision,
    )

    with pytest.raises(RevisionConflictError):
        operations.set_properties(
            "properties-divergent",
            "DivergentMetadata",
            {"status": "review"},
            expected_revision=created.revision,
        )

    row = storage.get_obsidian_operation("alice", "properties-divergent")
    assert row is not None and row["status"] == "conflict"
    assert json.loads(row["result_json"])["actual_revision"] == external.revision
    current = notes.read_note("DivergentMetadata")
    assert current.revision == external.revision
    assert "status" not in current.properties


def test_prepared_property_write_without_revision_closes_as_terminal_conflict(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    operations.create_note("create", "UnversionedMetadata", "body")
    original_set_properties = notes.set_properties

    class SyntheticCrash(BaseException):
        pass

    def crash_before_write(*_args: Any, **_kwargs: Any) -> Any:
        raise SyntheticCrash

    monkeypatch.setattr(notes, "set_properties", crash_before_write)
    with pytest.raises(SyntheticCrash):
        operations.set_properties(
            "properties-without-revision",
            "UnversionedMetadata",
            {"status": "review"},
        )
    monkeypatch.setattr(notes, "set_properties", original_set_properties)

    for _ in range(2):
        with pytest.raises(OperationTerminalError, match="expected_revision_required_for_recovery"):
            operations.set_properties(
                "properties-without-revision",
                "UnversionedMetadata",
                {"status": "review"},
            )

    row = storage.get_obsidian_operation("alice", "properties-without-revision")
    assert row is not None and row["status"] == "conflict"
    assert "status" not in notes.read_note("UnversionedMetadata").properties


def test_daily_note_is_frozen_to_one_day_and_replayed_once(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")

    first = operations.daily_note("daily-op", content="Итог дня")
    replay = operations.daily_note("daily-op", date(2026, 8, 21), content="Итог дня")

    assert first.path == "Daily/2026-08-21.md"
    assert replay.replayed is True
    assert replay.revision == first.revision
    assert notes.read_note(first.path).body.count("Итог дня") == 1


def test_structured_daily_operation_is_durable_and_exactly_once(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")

    first = operations.daily_note(
        "battery-daily",
        date(2026, 8, 22),
        section="Friday",
        item="- Проверена интеграция с Obsidian",
    )
    replay = operations.daily_note(
        "battery-daily",
        date(2026, 8, 22),
        section="Friday",
        item="- Проверена интеграция с Obsidian",
    )

    assert replay.replayed is True
    assert replay.revision == first.revision
    body = notes.read_note(first.path).body
    assert body.count("## Friday") == 1
    assert body.count("- Проверена интеграция с Obsidian") == 1


@pytest.mark.parametrize("mode", ["create", "append", "section"])
def test_daily_note_recovers_file_to_main_ledger_gap_without_duplicates(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    selected = date(2026, 8, 22)
    operation_id = f"daily-gap-{mode}"
    kwargs: dict[str, Any] = {}
    expected_text: str
    if mode == "create":
        kwargs["content"] = "created once"
        expected_text = "created once"
    else:
        base = operations.daily_note("daily-gap-seed", selected, content="seed")
        kwargs["expected_revision"] = base.revision
        if mode == "append":
            kwargs["content"] = "appended once"
            expected_text = "appended once"
        else:
            kwargs.update(section="Friday", item="- section once")
            expected_text = "- section once"

    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_main_receipt(owner_id: str, op_id: str, state: str, **values: Any) -> dict:
        nonlocal fail_once
        if op_id == operation_id and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic main-ledger interruption")
        return transition(owner_id, op_id, state, **values)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_main_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.daily_note(operation_id, selected, **kwargs)
    first_content = notes.read_note("Daily/2026-08-22.md").content
    row = storage.get_obsidian_operation("alice", operation_id)
    assert row is not None and row["status"] == "prepared"

    replay = operations.daily_note(operation_id, selected, **kwargs)

    final_content = notes.read_note("Daily/2026-08-22.md").content
    assert replay.replayed is True
    assert replay.status == "scan_pending"
    assert final_content == first_content
    assert final_content.count(expected_text) == 1
    assert "<!-- friday:" not in final_content


def test_implicit_daily_day_remains_frozen_when_retry_crosses_midnight(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    current_day = [date(2026, 8, 21)]
    operations, notes, _ = _make_operations(
        storage,
        tmp_path,
        "alice",
        clock=lambda: current_day[0],
    )
    first = operations.daily_note("midnight", content="before midnight")
    current_day[0] = date(2026, 8, 22)

    replay = operations.daily_note("midnight", content="before midnight")

    assert replay.replayed is True
    assert replay.path == first.path == "Daily/2026-08-21.md"
    assert notes.read_note(first.path).body.count("before midnight") == 1
    assert not notes.store.exists("Daily/2026-08-22.md")


def test_uncertain_daily_retry_keeps_stale_revision_guard(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    initial = operations.daily_note("daily-initial", content="initial")
    original_daily_note = notes.daily_note

    def fail_before_append(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("synthetic filesystem interruption")

    monkeypatch.setattr(notes, "daily_note", fail_before_append)
    with pytest.raises(OperationCommitUncertain):
        operations.daily_note(
            "daily-stale",
            content="must not append",
            expected_revision=initial.revision,
        )
    uncertain = storage.get_obsidian_operation("alice", "daily-stale")
    assert uncertain is not None and uncertain["status"] == "uncertain"

    monkeypatch.setattr(notes, "daily_note", original_daily_note)
    external = notes.append_note(
        initial.path,
        "external edit",
        operation_id="external-edit",
        expected_revision=initial.revision,
    )
    with pytest.raises(RevisionConflictError):
        operations.daily_note(
            "daily-stale",
            content="must not append",
            expected_revision=initial.revision,
        )
    assert "must not append" not in notes.read_note(initial.path).body
    assert notes.read_note(initial.path).revision == external.revision


def test_repeated_unknown_failure_remains_uncertain(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes, _ = _make_operations(storage, tmp_path, "alice")
    created = operations.create_note("uncertain-base", "Uncertain", "base")

    def fail_append(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("unknown local outcome")

    monkeypatch.setattr(notes, "append_note", fail_append)
    for _ in range(2):
        with pytest.raises(OperationCommitUncertain):
            operations.append_note(
                "uncertain-op",
                "Uncertain",
                "effect",
                expected_revision=created.revision,
            )
        row = storage.get_obsidian_operation("alice", "uncertain-op")
        assert row is not None and row["status"] == "uncertain"


def test_scan_failure_never_erases_or_downgrades_local_commit(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    sync = FakeSync(fail_scan=True)
    operations, notes, _ = _make_operations(storage, tmp_path, "alice", sync=sync)

    result = operations.create_note("offline-create", "Offline", "saved")

    assert result.status == "scan_pending"
    assert result.delivery.local_write_complete is True
    assert result.delivery.server_scan_complete is False
    assert notes.read_note("Offline").body.startswith("saved")
    pending = storage.get_obsidian_operation("alice", "offline-create")
    assert pending is not None and pending["status"] == "scan_pending"

    replay = operations.create_note("offline-create", "Offline", "saved")
    assert replay.replayed is True
    assert len(sync.requests) == 2


def test_delivery_refresh_requires_explicit_scan_and_android_receipt(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    sync = FakeSync()
    operations, _, _ = _make_operations(storage, tmp_path, "alice", sync=sync)
    operations.create_note("delivery", "Delivery", "body")
    sync.observed = VaultDeliveryState(
        local_write_complete=True,
        server_scan_complete=True,
        android_connected=True,
        android_completion=100.0,
        android_received=False,
        obsidian_opened=False,
    )

    pending = operations.refresh_delivery("delivery")

    assert pending.status == "delivery_pending"
    assert pending.delivery.android_completion == 100.0
    assert pending.delivery.android_received is False
    sync.observed = VaultDeliveryState(
        local_write_complete=True,
        server_scan_complete=True,
        android_connected=True,
        android_completion=100.0,
        android_received=True,
        obsidian_opened=False,
    )
    delivered = operations.refresh_delivery("delivery")
    assert delivered.status == "delivered"
    assert delivered.delivery.android_received is True
    assert delivered.delivery.obsidian_opened is False


def test_adapter_cannot_claim_receipt_without_scan_evidence(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    sync = FakeSync()
    operations, _, _ = _make_operations(storage, tmp_path, "alice", sync=sync)
    operations.create_note("invalid-proof", "Proof", "body")
    sync.observed = VaultDeliveryState(
        local_write_complete=True,
        server_scan_complete=False,
        android_connected=True,
        android_completion=100.0,
        android_received=True,
        obsidian_opened=False,
    )

    with pytest.raises(OperationLedgerError, match="receipt requires server scan"):
        operations.refresh_delivery("invalid-proof")
    pending = storage.get_obsidian_operation("alice", "invalid-proof")
    assert pending is not None and pending["status"] == "scan_pending"


def test_owner_binding_isolated_read_surface_and_same_ids_per_owner(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    alice, alice_notes, _ = _make_operations(storage, tmp_path, "alice")
    bob, bob_notes, _ = _make_operations(storage, tmp_path, "bob")
    alice.create_note("shared-operation-id", "Alice", "alice secret")
    bob.create_note("shared-operation-id", "Bob", "bob secret")

    assert [item.path for item in alice.list_notes()] == ["Alice.md"]
    assert [item.path for item in bob.search_notes("bob secret")] == ["Bob.md"]
    assert alice.read_note("Alice").body.startswith("alice secret")
    alice_row = storage.get_obsidian_operation("alice", "shared-operation-id")
    bob_row = storage.get_obsidian_operation("bob", "shared-operation-id")
    assert alice_row is not None and alice_row["user_id"] == "alice"
    assert bob_row is not None and bob_row["user_id"] == "bob"
    assert {
        (str(item["id"]), str(item["user_id"]))
        for item in storage.list_obsidian_legacy_marker_candidates("alice")
    } == {("shared-operation-id", "alice")}
    assert {
        (str(item["id"]), str(item["user_id"]))
        for item in storage.list_obsidian_legacy_marker_candidates("bob")
    } == {("shared-operation-id", "bob")}

    with pytest.raises(ValueError, match="does not match"):
        ObsidianOperationService(storage, bob_notes, owner_id="alice")
    assert alice_notes.read_note("Alice").body.startswith("alice secret")

    storage.execute("DELETE FROM obsidian_vaults WHERE user_id='alice'")
    with pytest.raises(OperationLedgerError, match="no longer available"):
        alice.list_notes()
