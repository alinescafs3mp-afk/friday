from __future__ import annotations

import hashlib
import json
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
    original_append = notes.append_note

    def fail_before_append(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("synthetic filesystem interruption")

    monkeypatch.setattr(notes, "append_note", fail_before_append)
    with pytest.raises(OperationCommitUncertain):
        operations.daily_note(
            "daily-stale",
            content="must not append",
            expected_revision=initial.revision,
        )
    uncertain = storage.get_obsidian_operation("alice", "daily-stale")
    assert uncertain is not None and uncertain["status"] == "uncertain"

    monkeypatch.setattr(notes, "append_note", original_append)
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

    with pytest.raises(ValueError, match="does not match"):
        ObsidianOperationService(storage, bob_notes, owner_id="alice")
    assert alice_notes.read_note("Alice").body.startswith("alice secret")

    storage.execute("DELETE FROM obsidian_vaults WHERE user_id='alice'")
    with pytest.raises(OperationLedgerError, match="no longer available"):
        alice.list_notes()
