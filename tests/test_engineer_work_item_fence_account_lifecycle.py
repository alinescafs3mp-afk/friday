from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from friday.account_deletion import (
    AccountDeletionBlocked,
    AccountDeletionConflict,
    _mark_account_deletion_history_clean,
    delete_account,
    preflight_account_deletion,
)
from friday.engineer_source_binding import canonical_engineer_source_binding_sha256
from friday.interaction_control_plane.engineer_work_item_schema import ENGINEER_WORK_ITEM_SCHEMA
from friday.organs.engineer.command import CommandError
from friday.organs.engineer.command.store import (
    CommandJobStore,
    EngineerCommandAccountInventory,
)

OWNER = "local:engineer-fence-owner"
ACTOR = "local:engineer-fence-admin"
IDEMPOTENCY_KEY = "ecmd-" + "a" * 64
WORK_ITEM_ID = "ewi_" + "b" * 32
SOURCE_BINDING = "c" * 64
COMMAND_DIGEST = "d" * 64
RETIRED_AT = "2026-08-27T10:00:00+00:00"
LIFECYCLE_KEY = b"account-deletion-ledger-key!!___"


def _external_job_payload(
    *,
    actor_id: str,
    tenant_id: str,
    digit: str,
) -> dict[str, object]:
    source_step_id = "ecstep-" + digit * 32
    source_hash = "3" * 64
    source_binding = canonical_engineer_source_binding_sha256(
        owner_id=actor_id,
        tenant_id=tenant_id,
        conversation_id="conversation",
        channel="telegram",
        source_row_id=f"source-{digit}",
        source_step_id=source_step_id,
        source_hash=source_hash,
        telegram_update_id=f"update-{digit}",
        delivery_chat_id="123",
    )
    return {
        "job_id": digit * 32,
        "actor_id": actor_id,
        "tenant_id": tenant_id,
        "conversation_id": "conversation",
        "channel": "telegram",
        "source_row_id": f"source-{digit}",
        "source_step_id": source_step_id,
        "source_binding_sha256": source_binding,
        "source_hash": source_hash,
        "telegram_update_id": f"update-{digit}",
        "isolation_profile": "host_user",
        "host_user_authorized": True,
        "idempotency_key": "ecmd-" + digit * 64,
        "command_digest": digit * 64,
        "input_manifest_sha256": "",
        "argv_sha256": "6" * 64,
        "lane": "argv",
        "origin": "model",
        "status": "admitted",
        "grant_nonce": f"grant-{digit}",
        "timeout_sec": 30,
        "max_stdout_bytes": 1024,
        "max_stderr_bytes": 1024,
        "created_at": time.time(),
        "executable_json": "{}",
        "delivery_chat_id": "123",
    }


def _insert_retired_fence(storage) -> None:
    # A retired fence legitimately has no live Work Item.  Bypass only the
    # admission-time trigger to build that post-retirement state, then restore the
    # canonical DDL before exercising either public lifecycle.
    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER trg_engineer_work_item_command_fence_insert_guard")
        conn.execute(
            """INSERT INTO engineer_work_item_command_fences(
                   owner_id,idempotency_key,work_item_id,expected_revision,step_ordinal,
                   source_binding_sha256,command_digest,retired_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                OWNER,
                IDEMPOTENCY_KEY,
                WORK_ITEM_ID,
                1,
                1,
                SOURCE_BINDING,
                COMMAND_DIGEST,
                RETIRED_AT,
            ),
        )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)


def test_retired_command_fence_exports_but_blocks_false_external_ledger_erasure(storage) -> None:
    storage.ensure_user(ACTOR, preset_key="admin")
    storage.ensure_user(OWNER)
    _insert_retired_fence(storage)

    exported = storage.export_user(OWNER)
    export_path = Path(str(exported["path"]))
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["engineer_work_item_command_fences"] == [
        {
            "owner_id": OWNER,
            "idempotency_key": IDEMPOTENCY_KEY,
            "work_item_id": WORK_ITEM_ID,
            "expected_revision": 1,
            "step_ordinal": 1,
            "source_binding_sha256": SOURCE_BINDING,
            "command_digest": COMMAND_DIGEST,
            "retired_at": RETIRED_AT,
        }
    ]
    # The export itself is deliberately an external-artifact blocker; remove the
    # test artifact before proving the independent account-erasure lifecycle.
    export_path.unlink()

    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    plan = preflight_account_deletion(storage, OWNER, quiescence_available=True)
    assert plan["ready"] is False
    assert plan["counts"]["engineer_work_item_command_fences"] == 1
    assert "engineer_work_item_command_fences.owner_id" not in plan["unknown_scopes"]
    assert {item["code"] for item in plan["blockers"]} == {"engineer_command_history"}

    with pytest.raises(AccountDeletionBlocked):
        delete_account(
            storage,
            OWNER,
            expected_fingerprint=plan["fingerprint"],
            actor_user_id=ACTOR,
            quiescence_verified=True,
        )

    assert (
        storage.execute(
            "SELECT COUNT(*) FROM engineer_work_item_command_fences WHERE owner_id=?",
            (OWNER,),
        ).fetchone()[0]
        == 1
    )
    assert storage.get_user(OWNER) is not None
    assert storage.get_user(ACTOR) is not None


def test_external_inventory_counts_actor_tenant_fence_publication_and_output(
    tmp_path: Path,
) -> None:
    store = CommandJobStore.provision(
        tmp_path / "command-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        own_job = _external_job_payload(actor_id=OWNER, tenant_id=OWNER, digit="1")
        delegated_job = _external_job_payload(actor_id=ACTOR, tenant_id=OWNER, digit="2")
        store.insert_job(own_job)
        store.insert_job(delegated_job)
        with store.transaction():
            store.update_job(
                str(own_job["job_id"]),
                {
                    "receipt_mac": "a" * 64,
                    "stdout_sha256": "b" * 64,
                    "stdout_bytes": 7,
                },
            )
        store.create_engineer_work_item_fence(
            actor_id=OWNER,
            idempotency_key="ecmd-" + "9" * 64,
            work_item_id="ewi_" + "8" * 32,
            expected_revision=1,
            step_ordinal=1,
            source_binding_sha256="7" * 64,
            command_digest="6" * 64,
        )

        inventory = store.account_deletion_inventory(OWNER)

        assert inventory.user_id == OWNER
        assert len(inventory.store_id) == 32
        assert inventory.authority_sequence == 4
        assert inventory.jobs == 2
        assert inventory.source_slots == 2
        assert inventory.fences == 1
        assert inventory.unattributable_fences == 0
        assert inventory.publications == 2
        assert inventory.output_jobs == 1
        assert inventory.retained_roots == 3
        assert inventory.has_history is True
        unrelated = store.account_deletion_inventory("local:unrelated")
        assert unrelated.fences == 0
        assert unrelated.unattributable_fences == 1
        assert unrelated.has_history is True
    finally:
        store.close()


def test_external_only_job_blocks_account_deletion_without_a_main_work_item(
    storage,
    tmp_path: Path,
) -> None:
    storage.ensure_user(ACTOR, preset_key="admin")
    storage.ensure_user(OWNER)
    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    store = CommandJobStore.provision(
        tmp_path / "command-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        store.insert_job(_external_job_payload(actor_id=OWNER, tenant_id=OWNER, digit="1"))
        inventory = store.account_deletion_inventory(OWNER)
        plan = preflight_account_deletion(
            storage,
            OWNER,
            quiescence_available=True,
            engineer_command_inventory=inventory,
            engineer_command_inventory_required=True,
        )

        assert (
            storage.execute(
                "SELECT COUNT(*) FROM engineer_work_items WHERE owner_id=? OR tenant_id=?",
                (OWNER, OWNER),
            ).fetchone()[0]
            == 0
        )
        assert plan["ready"] is False
        assert plan["counts"]["engineer_command_external_jobs"] == 1
        assert plan["counts"]["engineer_command_external_source_slots"] == 1
        assert plan["counts"]["engineer_command_external_publications"] == 1
        assert plan["engineer_command_inventory"] == inventory.fingerprint_payload() | {
            "required": True,
            "available": True,
        }
        assert {item["code"] for item in plan["blockers"]} == {"engineer_command_history"}
    finally:
        store.close()


def test_other_actor_fence_conservatively_blocks_unattributable_tenant_deletion(
    storage,
    tmp_path: Path,
) -> None:
    storage.ensure_user(OWNER)
    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    store = CommandJobStore.provision(
        tmp_path / "command-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        store.create_engineer_work_item_fence(
            actor_id=ACTOR,
            idempotency_key="ecmd-" + "9" * 64,
            work_item_id="ewi_" + "8" * 32,
            expected_revision=1,
            step_ordinal=1,
            source_binding_sha256="7" * 64,
            command_digest="6" * 64,
        )
        inventory = store.account_deletion_inventory(OWNER)
        assert inventory.fences == 0
        assert inventory.unattributable_fences == 1

        plan = preflight_account_deletion(
            storage,
            OWNER,
            quiescence_available=True,
            engineer_command_inventory=inventory,
            engineer_command_inventory_required=True,
        )
        assert plan["ready"] is False
        assert plan["counts"]["engineer_command_external_unattributable_fences"] == 1
        assert {item["code"] for item in plan["blockers"]} == {"engineer_command_history"}
    finally:
        store.close()


def test_inactive_scope_progress_retirement_is_scoped_and_exactly_idempotent(
    tmp_path: Path,
) -> None:
    store = CommandJobStore.provision(
        tmp_path / "progress-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        payload = _external_job_payload(actor_id=OWNER, tenant_id=OWNER, digit="9")
        payload["status"] = "running"
        store.insert_job(payload)

        with pytest.raises(CommandError, match="progress_state_changed"):
            store.retire_progress_for_inactive_scope(
                "9" * 32,
                actor_id=ACTOR,
                conversation_id="conversation",
                retired_at=1_000.0,
            )
        assert (
            store._conn.execute(  # noqa: SLF001 - exact private-ledger assertion
                "SELECT retired_at FROM command_job_progress WHERE job_id=?",
                ("9" * 32,),
            ).fetchone()[0]
            is None
        )

        store.retire_progress_for_inactive_scope(
            "9" * 32,
            actor_id=OWNER,
            conversation_id="conversation",
            retired_at=1_000.0,
        )
        store.retire_progress_for_inactive_scope(
            "9" * 32,
            actor_id=OWNER,
            conversation_id="conversation",
            retired_at=1_000.0,
        )
        # A crash after the private-ledger commit loses the caller's timestamp.
        # Same-scope replay keeps the immutable first marker instead of wedging.
        store.retire_progress_for_inactive_scope(
            "9" * 32,
            actor_id=OWNER,
            conversation_id="conversation",
            retired_at=1_001.0,
        )
        row = store.read_job("9" * 32)
        assert row["status"] == "running"
        assert (
            store._conn.execute(  # noqa: SLF001 - exact private-ledger assertion
                "SELECT retired_at FROM command_job_progress WHERE job_id=?",
                ("9" * 32,),
            ).fetchone()[0]
            == 1_000.0
        )
    finally:
        store.close()


def test_delete_resnapshot_blocks_restore_skew_that_removed_the_main_work_item(
    storage,
    tmp_path: Path,
) -> None:
    storage.ensure_user(ACTOR, preset_key="admin")
    storage.ensure_user(OWNER)
    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    store = CommandJobStore.provision(
        tmp_path / "command-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        clean_inventory = store.account_deletion_inventory(OWNER)
        reviewed = preflight_account_deletion(
            storage,
            OWNER,
            quiescence_available=True,
            engineer_command_inventory=clean_inventory,
            engineer_command_inventory_required=True,
        )
        assert reviewed["ready"] is True, reviewed

        # The external monotonic authority advances while the restored/older
        # main DB still has no EWI.  The deletion path must use this fresh
        # snapshot rather than trusting the formerly clean reviewed plan.
        store.insert_job(_external_job_payload(actor_id=ACTOR, tenant_id=OWNER, digit="2"))
        restored_skew_inventory = store.account_deletion_inventory(OWNER)
        with pytest.raises(AccountDeletionBlocked) as blocked:
            delete_account(
                storage,
                OWNER,
                expected_fingerprint=reviewed["fingerprint"],
                actor_user_id=ACTOR,
                quiescence_verified=True,
                engineer_command_inventory=restored_skew_inventory,
                engineer_command_inventory_required=True,
            )

        assert {item["code"] for item in blocked.value.report["blockers"]} == {"engineer_command_history"}
        assert storage.get_user(OWNER) is not None
    finally:
        store.close()


def test_delete_fingerprint_rejects_even_an_unrelated_ledger_generation_change(
    storage,
    tmp_path: Path,
) -> None:
    storage.ensure_user(ACTOR, preset_key="admin")
    storage.ensure_user(OWNER)
    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    store = CommandJobStore.provision(
        tmp_path / "command-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        reviewed_inventory = store.account_deletion_inventory(OWNER)
        reviewed = preflight_account_deletion(
            storage,
            OWNER,
            quiescence_available=True,
            engineer_command_inventory=reviewed_inventory,
            engineer_command_inventory_required=True,
        )
        assert reviewed["ready"] is True, reviewed

        unrelated = "local:unrelated-ledger-actor"
        store.insert_job(_external_job_payload(actor_id=unrelated, tenant_id=unrelated, digit="4"))
        fresh_inventory = store.account_deletion_inventory(OWNER)
        assert fresh_inventory.has_history is False
        assert fresh_inventory.authority_sequence > reviewed_inventory.authority_sequence

        with pytest.raises(AccountDeletionConflict):
            delete_account(
                storage,
                OWNER,
                expected_fingerprint=reviewed["fingerprint"],
                actor_user_id=ACTOR,
                quiescence_verified=True,
                engineer_command_inventory=fresh_inventory,
                engineer_command_inventory_required=True,
            )
        assert storage.get_user(OWNER) is not None
    finally:
        store.close()


def test_enabled_engineer_authority_never_treats_unavailable_inventory_as_zero(storage) -> None:
    storage.ensure_user(OWNER)
    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")

    plan = preflight_account_deletion(
        storage,
        OWNER,
        quiescence_available=True,
        engineer_command_inventory=None,
        engineer_command_inventory_required=True,
    )

    assert plan["ready"] is False
    assert plan["engineer_command_inventory"] == {
        "required": True,
        "available": False,
    }
    assert {item["code"] for item in plan["blockers"]} == {"engineer_inventory_unavailable"}


def test_forged_typed_inventory_is_rejected_at_construction_and_preflight(
    storage,
    tmp_path: Path,
) -> None:
    storage.ensure_user(OWNER)
    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    store = CommandJobStore.provision(
        tmp_path / "command-store",
        lifecycle_key=LIFECYCLE_KEY,
    )
    try:
        inventory = store.account_deletion_inventory(OWNER)
    finally:
        store.close()
    with pytest.raises(ValueError, match="invalid Engineer command inventory"):
        replace(inventory, jobs=-1)

    forged = object.__new__(EngineerCommandAccountInventory)
    for name in (
        "user_id",
        "store_id",
        "authority_sequence",
        "jobs",
        "source_slots",
        "fences",
        "unattributable_fences",
        "publications",
        "output_jobs",
    ):
        object.__setattr__(forged, name, getattr(inventory, name))
    object.__setattr__(forged, "authority_sequence", -1)

    plan = preflight_account_deletion(
        storage,
        OWNER,
        quiescence_available=True,
        engineer_command_inventory=forged,
        engineer_command_inventory_required=True,
    )
    assert plan["ready"] is False
    assert plan["engineer_command_inventory"] == {
        "required": True,
        "available": False,
    }
    assert {item["code"] for item in plan["blockers"]} == {"engineer_inventory_unavailable"}


def test_admin_preflight_uses_the_organ_owned_external_ledger(
    settings,
    tmp_path: Path,
) -> None:
    import hashlib
    import hmac

    from fastapi.testclient import TestClient

    from friday.organs.engineer.command_tools import provision_engineer_command_store
    from friday.server import create_app

    target = "local:external-only-route"
    master = b"M" * 32
    key_file = tmp_path / "engineer-command.key"
    key_file.write_bytes(master)
    key_file.chmod(0o600)
    store_root = tmp_path / "engineer-command-store"
    configured = replace(
        settings,
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        engineer_command_store_dir=store_root,
        engineer_command_key_file=key_file,
        account_hard_delete_enabled=True,
    )
    provision_engineer_command_store(configured)
    lifecycle_key = hmac.new(
        master,
        b"friday-engineer-command-v1\x00store-lifecycle",
        hashlib.sha256,
    ).digest()
    external = CommandJobStore.open_runtime(
        store_root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=configured.state_dir,
    )
    try:
        external.insert_job(_external_job_payload(actor_id=target, tenant_id=target, digit="5"))
    finally:
        external.close()

    app = create_app(configured)
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user(target)
        assert _mark_account_deletion_history_clean(storage, target)
        storage.update_user(target, status="disabled")
        provider = app.state.engineer_command_account_inventory
        assert callable(provider)
        assert provider(target).jobs == 1

        response = client.get(
            f"/api/admin/users/{target}/deletion",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["ready"] is False
    assert report["counts"]["engineer_command_external_jobs"] == 1
    assert {item["code"] for item in report["blockers"]} == {"engineer_command_history"}

    dormant_app = create_app(
        replace(
            configured,
            engineer_mode_enabled=False,
            engineer_command_enabled=False,
        )
    )
    with TestClient(dormant_app) as client:
        assert dormant_app.state.engineer_command_account_inventory is None
        assert dormant_app.state.engineer_command_account_inventory_required is True
        response = client.get(
            f"/api/admin/users/{target}/deletion",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 200, response.text
    dormant_report = response.json()
    assert dormant_report["ready"] is False
    assert {item["code"] for item in dormant_report["blockers"]} == {"engineer_inventory_unavailable"}
