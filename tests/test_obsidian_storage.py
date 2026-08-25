from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.storage import FridayStorage


def _bundle(storage: FridayStorage, user_id: str, *, suffix: str = "") -> dict:
    storage.ensure_user(user_id)
    return storage.create_obsidian_bundle(
        user_id,
        config_root=f"/private/profiles/{user_id}{suffix}",
        database_root=f"/private/data/{user_id}{suffix}",
        api_endpoint=f"unix:///private/run/{user_id}{suffix}.sock",
        api_key_ref=f"secret:obsidian:{user_id}{suffix}",
        server_path=f"/private/vaults/{user_id}{suffix}",
        folder_id=f"friday-{user_id}{suffix}",
        setup_token_hash=hashlib.sha256(f"token:{user_id}{suffix}".encode()).hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
        convention={"daily_notes": "Daily/{date}.md"},
    )


def test_bundle_is_atomic_idempotent_and_tenant_scoped(storage: FridayStorage) -> None:
    alice = _bundle(storage, "alice")
    replay = _bundle(storage, "alice", suffix="-ignored")
    bob = _bundle(storage, "bob")

    assert replay == alice
    assert storage.get_obsidian_profile("alice")["id"] == alice["profile"]["id"]
    assert storage.get_obsidian_profile("bob")["id"] == bob["profile"]["id"]
    assert storage.get_obsidian_vault("alice")["id"] != bob["vault"]["id"]
    assert storage.get_obsidian_profile("nobody") is None


def test_bundle_rejects_an_incomplete_existing_aggregate(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")
    storage.execute("DELETE FROM obsidian_onboarding_sessions WHERE user_id='alice'")

    with pytest.raises(sqlite3.IntegrityError, match="incomplete"):
        _bundle(storage, "alice")

    assert storage.get_obsidian_profile("alice")["id"] == aggregate["profile"]["id"]
    assert storage.get_obsidian_onboarding("alice") is None


def test_profile_inventory_and_creation_are_bounded(storage: FridayStorage) -> None:
    _bundle(storage, "alice")
    storage.ensure_user("bob")
    with pytest.raises(ValueError, match="profile limit reached"):
        storage.create_obsidian_bundle(
            "bob",
            config_root="/private/profiles/bob",
            database_root="/private/data/bob",
            api_endpoint="unix:///private/run/bob.sock",
            api_key_ref="secret:obsidian:bob",
            server_path="/private/vaults/bob",
            folder_id="friday-bob",
            setup_token_hash=hashlib.sha256(b"token:bob").hexdigest(),
            expires_at="2030-01-01T00:00:00+00:00",
            max_profiles=1,
        )

    assert [row["user_id"] for row in storage.list_obsidian_profiles(limit=1)] == ["alice"]
    assert storage.get_obsidian_profile("bob") is None


def test_onboarding_state_machine_and_device_binding(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")

    with pytest.raises(ValueError, match="invalid.*transition"):
        storage.transition_obsidian_onboarding("alice", "ready")

    session = storage.transition_obsidian_onboarding(
        "alice", "awaiting_device_id_handoff", device_id_presented=True
    )
    assert session["device_id_presented_at"]
    storage.transition_obsidian_onboarding("alice", "awaiting_android_device")
    candidates = storage.record_obsidian_pairing_candidates(
        "alice",
        [
            {"syncthing_device_id": "aaaa-bbbb", "display_name": "Pixel"},
            {"syncthing_device_id": "cccc-dddd", "display_name": "Tablet"},
        ],
    )
    assert all(candidate["id"].startswith("obscand_") for candidate in candidates)
    storage.transition_obsidian_onboarding("alice", "multiple_pending_devices")
    selected = storage.select_obsidian_pairing_candidate("alice", candidates[0]["id"])
    assert selected["state"] == "selected"
    assert [item["id"] for item in storage.list_obsidian_pairing_candidates("alice")] == [candidates[0]["id"]]
    device = storage.bind_obsidian_android_device(
        "alice", syncthing_device_id="aaaa-bbbb", display_name="Pixel"
    )
    assert device["syncthing_device_id"] == "AAAA-BBBB"
    assert storage.get_obsidian_vault("alice")["android_device_id"] == device["id"]
    assert (
        storage.bind_obsidian_android_device(
            "alice", syncthing_device_id="AAAA-BBBB", display_name="ignored"
        )["id"]
        == device["id"]
    )
    with pytest.raises(ValueError, match="different Android"):
        storage.bind_obsidian_android_device("alice", syncthing_device_id="CCCC-DDDD")

    session = storage.transition_obsidian_onboarding(
        "alice", "android_device_detected", pending_device_id=device["syncthing_device_id"]
    )
    assert session["pending_device_id"] == "AAAA-BBBB"
    assert aggregate["vault"]["id"] == storage.get_obsidian_vault("alice")["id"]


def test_reappearing_pairing_candidate_returns_to_pending(storage: FridayStorage) -> None:
    _bundle(storage, "alice")
    storage.transition_obsidian_onboarding("alice", "awaiting_device_id_handoff")
    storage.transition_obsidian_onboarding("alice", "awaiting_android_device")
    first = storage.record_obsidian_pairing_candidates(
        "alice", [{"syncthing_device_id": "AAAA-BBBB", "display_name": "Pixel"}]
    )[0]
    assert storage.record_obsidian_pairing_candidates("alice", []) == []

    reappeared = storage.record_obsidian_pairing_candidates(
        "alice", [{"syncthing_device_id": "AAAA-BBBB", "display_name": "Pixel again"}]
    )
    assert len(reappeared) == 1
    assert reappeared[0]["id"] == first["id"]
    assert reappeared[0]["state"] == "pending"
    assert reappeared[0]["display_name"] == "Pixel again"


def test_unicode_vault_alias_is_normalized_owner_scoped_and_bounded(storage: FridayStorage) -> None:
    _bundle(storage, "alice")
    _bundle(storage, "bob")
    updated = storage.update_obsidian_vault_alias("alice", "  Личныи\u0306 Vault  ")
    assert updated["android_vault_name"] == "Личный Vault"
    assert storage.get_obsidian_vault("bob")["android_vault_name"] == "Friday"
    for unsafe in ("", "bad/name", "bad\\name", "bad\x00name", "x" * 101):
        with pytest.raises((TypeError, ValueError)):
            storage.update_obsidian_vault_alias("alice", unsafe)


def test_ready_session_and_vault_are_finalized_atomically(storage: FridayStorage) -> None:
    _bundle(storage, "alice")
    for state in (
        "awaiting_device_id_handoff",
        "awaiting_android_device",
        "android_device_detected",
        "offering_folder",
        "awaiting_android_folder_acceptance",
        "initial_sync",
        "awaiting_obsidian_vault_registration",
    ):
        storage.transition_obsidian_onboarding("alice", state)
    storage.transition_obsidian_onboarding("alice", "round_trip_verification", obsidian_opened=True)
    storage.update_obsidian_vault("alice", state="verifying")

    finalized = storage.finalize_obsidian_onboarding("alice")
    assert finalized["state"] == "ready"
    assert storage.get_obsidian_vault("alice")["state"] == "ready"


def test_setup_token_rotation_and_consumption_are_hash_only_and_one_shot(
    storage: FridayStorage,
) -> None:
    _bundle(storage, "alice")
    raw_token = "a private setup capability"
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    storage.rotate_obsidian_setup_token(
        "alice", setup_token_hash=digest, expires_at="2030-01-01T00:00:00+00:00"
    )
    storage.update_obsidian_profile(
        "alice", state="running", server_device_id="AAAA-BBBB", syncthing_version="2.0.0"
    )

    database_text = " ".join(
        str(item[0]) for item in storage.execute("SELECT setup_token_hash FROM obsidian_onboarding_sessions")
    )
    assert raw_token not in database_text
    resolved = storage.consume_obsidian_setup_token(digest, now="2026-08-21T00:00:00+00:00")
    assert resolved and resolved["server_device_id"] == "AAAA-BBBB"
    assert storage.consume_obsidian_setup_token(digest, now="2026-08-21T00:00:01+00:00") is None


def test_operation_ledger_replays_only_identical_arguments(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")
    bob = _bundle(storage, "bob")
    digest = hashlib.sha256(b'{"path":"Notes/A.md"}').hexdigest()

    prepared, created = storage.prepare_obsidian_operation(
        "alice",
        operation_id="op-client-1",
        vault_id=aggregate["vault"]["id"],
        method="create",
        arguments_digest=digest,
        expected_revision="0" * 64,
    )
    replay, replay_created = storage.prepare_obsidian_operation(
        "alice",
        operation_id="op-client-1",
        vault_id=aggregate["vault"]["id"],
        method="create",
        arguments_digest=digest,
        expected_revision="0" * 64,
    )
    assert created is True and replay_created is False
    assert replay == prepared

    with pytest.raises(ValueError, match="different arguments"):
        storage.prepare_obsidian_operation(
            "alice",
            operation_id="op-client-1",
            vault_id=aggregate["vault"]["id"],
            method="append",
            arguments_digest=digest,
            expected_revision="0" * 64,
        )
    with pytest.raises(ValueError, match="different arguments"):
        storage.prepare_obsidian_operation(
            "alice",
            operation_id="op-client-1",
            vault_id=aggregate["vault"]["id"],
            method="create",
            arguments_digest=digest,
            expected_revision="0" * 64,
            work_item_id="another-work-item",
        )
    assert storage.get_obsidian_operation("bob", "op-client-1") is None
    bob_operation, bob_created = storage.prepare_obsidian_operation(
        "bob",
        operation_id="op-client-1",
        vault_id=bob["vault"]["id"],
        method="append",
        arguments_digest=digest,
    )
    assert bob_created is True and bob_operation["user_id"] == "bob"

    committed = storage.transition_obsidian_operation(
        "alice", "op-client-1", "committed", result={"revision": "1" * 64}
    )
    assert committed["status"] == "committed"
    assert storage.list_pending_obsidian_operations()[0]["id"] == "op-client-1"
    with pytest.raises(ValueError, match="invalid.*transition"):
        storage.transition_obsidian_operation("alice", "op-client-1", "delivered")


def test_prepared_operation_is_pending_and_can_reconcile_directly(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")
    prepared, _created = storage.prepare_obsidian_operation(
        "alice",
        operation_id="op-prepared-reconcile",
        vault_id=aggregate["vault"]["id"],
        method="create",
        arguments_digest=hashlib.sha256(b"prepared-reconcile").hexdigest(),
        prepared_result={"reconciliation_state": "required"},
    )

    assert prepared["status"] == "prepared"
    assert {row["id"] for row in storage.list_pending_obsidian_operations()} == {"op-prepared-reconcile"}

    reconciled = storage.transition_obsidian_operation(
        "alice",
        "op-prepared-reconcile",
        "reconciled",
        result={"reconciliation_state": "settled"},
    )
    assert reconciled["status"] == "reconciled"
    assert json.loads(reconciled["result_json"]) == {"reconciliation_state": "settled"}


@pytest.mark.parametrize(
    ("accepted_state", "transitions"),
    [
        ("committed", ("committed",)),
        ("scan_pending", ("committed", "scan_pending")),
        ("scan_complete", ("committed", "scan_complete")),
        ("delivery_pending", ("committed", "scan_pending", "delivery_pending")),
        ("delivered", ("committed", "scan_complete", "delivered")),
        ("reconciled", ("reconciled",)),
    ],
)
def test_accepted_operation_result_is_immutable(
    storage: FridayStorage,
    accepted_state: str,
    transitions: tuple[str, ...],
) -> None:
    aggregate = _bundle(storage, "alice")
    operation_id = f"op-immutable-{accepted_state}"
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=aggregate["vault"]["id"],
        method="create",
        arguments_digest=hashlib.sha256(operation_id.encode()).hexdigest(),
    )
    accepted_result = {"path": "Notes/A.md", "revision": "1" * 64}
    for state in transitions:
        storage.transition_obsidian_operation(
            "alice",
            operation_id,
            state,
            result=accepted_result if state == transitions[0] else None,
        )

    identical = storage.transition_obsidian_operation(
        "alice",
        operation_id,
        accepted_state,
        result={"revision": "1" * 64, "path": "Notes/A.md"},
    )
    assert json.loads(identical["result_json"]) == accepted_result

    with pytest.raises(ValueError, match="result is immutable"):
        storage.transition_obsidian_operation(
            "alice",
            operation_id,
            accepted_state,
            result={"path": "Notes/A.md", "revision": "2" * 64},
        )
    unchanged = storage.get_obsidian_operation("alice", operation_id)
    assert unchanged is not None
    assert unchanged["status"] == accepted_state
    assert json.loads(unchanged["result_json"]) == accepted_result


def test_accepted_result_survives_delivery_update_and_transition(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")
    operation_id = "op-delivery-only"
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=aggregate["vault"]["id"],
        method="create",
        arguments_digest=hashlib.sha256(operation_id.encode()).hexdigest(),
    )
    accepted_result = {"revision": "1" * 64}
    storage.transition_obsidian_operation("alice", operation_id, "committed", result=accepted_result)
    updated = storage.transition_obsidian_operation(
        "alice", operation_id, "committed", delivery={"server_scan_complete": False}
    )
    assert json.loads(updated["result_json"]) == accepted_result
    advanced = storage.transition_obsidian_operation(
        "alice", operation_id, "scan_pending", result={"revision": "1" * 64}
    )
    assert json.loads(advanced["result_json"]) == accepted_result

    with pytest.raises(ValueError, match="result is immutable"):
        storage.transition_obsidian_operation(
            "alice", operation_id, "scan_complete", result={"revision": "2" * 64}
        )
    unchanged = storage.get_obsidian_operation("alice", operation_id)
    assert unchanged is not None
    assert unchanged["status"] == "scan_pending"


def test_uncertain_operation_can_replace_result_when_reconciled(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")
    operation_id = "op-uncertain-reconciled"
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=aggregate["vault"]["id"],
        method="append",
        arguments_digest=hashlib.sha256(operation_id.encode()).hexdigest(),
        prepared_result={"reconciliation_state": "required"},
    )
    storage.transition_obsidian_operation(
        "alice", operation_id, "uncertain", result={"error": "commit outcome unknown"}
    )

    reconciled = storage.transition_obsidian_operation(
        "alice",
        operation_id,
        "reconciled",
        result={"reconciliation_state": "settled", "revision": "1" * 64},
    )
    assert reconciled["status"] == "reconciled"
    assert json.loads(reconciled["result_json"]) == {
        "reconciliation_state": "settled",
        "revision": "1" * 64,
    }


def test_conflicts_are_preserved_and_upserted_per_vault(storage: FridayStorage) -> None:
    aggregate = _bundle(storage, "alice")
    first = storage.record_obsidian_conflict(
        "alice",
        vault_id=aggregate["vault"]["id"],
        canonical_path="Notes/A.md",
        conflict_path="Notes/A.sync-conflict-20260821.md",
    )
    second = storage.record_obsidian_conflict(
        "alice",
        vault_id=aggregate["vault"]["id"],
        canonical_path="Notes/Renamed.md",
        conflict_path="Notes/A.sync-conflict-20260821.md",
    )

    assert second["id"] == first["id"]
    assert second["canonical_path"] == "Notes/Renamed.md"
    assert storage.list_obsidian_conflicts("alice") == [second]
    assert storage.list_obsidian_conflicts("bob") == []


def test_user_export_contains_only_the_owners_obsidian_state(storage: FridayStorage) -> None:
    alice = _bundle(storage, "alice")
    bob = _bundle(storage, "bob")
    storage.prepare_obsidian_operation(
        "alice",
        operation_id="alice-export-operation",
        vault_id=alice["vault"]["id"],
        method="create",
        arguments_digest=hashlib.sha256(b"alice-export-operation").hexdigest(),
    )
    storage.record_obsidian_conflict(
        "alice",
        vault_id=alice["vault"]["id"],
        canonical_path="Notes/A.md",
        conflict_path="Notes/A.sync-conflict.md",
    )

    exported = storage.export_user("alice")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))

    for table in (
        "obsidian_sync_profiles",
        "obsidian_vaults",
        "obsidian_onboarding_sessions",
        "obsidian_operations",
        "obsidian_conflicts",
    ):
        assert payload[table]
        assert {row["user_id"] for row in payload[table]} == {"alice"}
    assert bob["profile"]["id"] not in Path(exported["path"]).read_text(encoding="utf-8")


def test_current_schema_rejects_tampered_obsidian_contract(settings, tmp_path) -> None:
    database = tmp_path / "tampered.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP INDEX uq_obsidian_profile_user")

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Obsidian state"):
            reopened.execute("SELECT 1")
    finally:
        reopened.close()


def test_composite_tenant_links_reject_cross_owner_references(storage: FridayStorage) -> None:
    _bundle(storage, "alice")
    bob = _bundle(storage, "bob")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        storage.execute(
            """INSERT INTO obsidian_pairing_candidates(
                   id, user_id, session_id, syncthing_device_id, display_name,
                   short_suffix, state, detected_at, expires_at, updated_at
               ) VALUES('cross-owner', 'alice', ?, 'EEEE-FFFF', '', 'FFFF',
                        'pending', '2026-08-21T00:00:00+00:00',
                        '2030-01-01T00:00:00+00:00', '2026-08-21T00:00:00+00:00')""",
            (bob["session"]["id"],),
        )
