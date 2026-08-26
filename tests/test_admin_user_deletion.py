"""Permanent account deletion is a proved cascade, not a bigger Disable button."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import threading
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.account_deletion import (
    AccountDeletionConflict,
    _account_files_directory,
    _mark_account_deletion_history_clean,
    _obsidian_account_directory,
    delete_account,
    preflight_account_deletion,
)
from friday.account_gate import AccountActivityGate, AccountGateClosed
from friday.host_control.jobs import HostJobStore
from friday.memory import VaultAccountWriteBlocked, _safe_component
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage import (
    DeletedAccountError,
    deleted_account_tombstone_key,
    deleted_identity_tombstone_key,
)
from friday.storage.models import (
    AuditEntry,
    Entity,
    EntityResolutionCandidate,
    KnowledgeObject,
    RawObject,
    Relation,
    new_id,
    utc_now,
)


def _headers(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture
def hard_delete_settings(settings):
    """Explicit test-only opt-in; production has no equivalent escape hatch."""

    return replace(
        settings,
        account_hard_delete_enabled=True,
        memory_vault_mode="full_owner",
    )


def _create_user(client: TestClient, settings, user_id: str, *, preset: str = "user") -> None:
    response = client.post(
        "/api/admin/users",
        headers=_headers(settings),
        json={"id": user_id, "display_name": user_id.title(), "preset_key": preset},
    )
    assert response.status_code == 200, response.text


def _disable_user(client: TestClient, settings, user_id: str) -> None:
    response = client.patch(
        f"/api/admin/users/{user_id}", headers=_headers(settings), json={"status": "disabled"}
    )
    assert response.status_code == 200, response.text


def _verified_plan(storage, user_id: str) -> dict:
    """Synthetic clean-history proof for direct storage fixtures."""

    with suppress(ValueError):
        _mark_account_deletion_history_clean(storage, user_id)
    return preflight_account_deletion(storage, user_id, quiescence_available=True)


def _put_text_knowledge(storage, user_id: str, marker: str) -> tuple[str, str]:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=marker,
        content_type="text",
        content_hash=hashlib.sha256(marker.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=marker,
        title="Deletion fixture",
        summary=marker,
    )
    storage.store_knowledge_object(knowledge)
    return raw.id, knowledge.id


def test_disabled_account_is_erased_with_access_sessions_data_and_audit(hard_delete_settings) -> None:
    settings = hard_delete_settings
    target = "local:delete-me"
    neighbour = "local:keep-me"
    marker = "ONLY-DELETED-ACCOUNT-MARKER-8F4E"
    neighbour_marker = "NEIGHBOUR-MUST-SURVIVE-913A"

    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        _create_user(client, settings, neighbour)

        storage.link_identity("sso", "555001", target, linked_by=LEGACY_OWNER_USER_ID)
        storage.set_permission_override(target, "chat.use", "deny")
        storage.create_api_token(target, hashlib.sha256(b"target-token").hexdigest(), label="old")
        storage.idempotency_store(target, "request-1", {"message": marker})
        raw_id, knowledge_id = _put_text_knowledge(storage, target, marker)
        storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Temporary entity"))
        storage.kv_set(f"eval:last_run:{target}", "{}")

        neighbour_conversation = storage.create_conversation(neighbour, "Keep")
        neighbour_message = storage.store_message(
            neighbour_conversation["id"], neighbour, "user", neighbour_marker
        )
        neighbour_raw, neighbour_knowledge = _put_text_knowledge(storage, neighbour, neighbour_marker)
        storage.update_user(neighbour, metadata_json={"supervisor_id": target, "note": "preserve"})
        _disable_user(client, settings, target)

        preflight = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings))
        assert preflight.status_code == 200, preflight.text
        plan = preflight.json()
        assert plan["ready"] is True, plan
        assert plan["counts"]["user_identities"] == 1
        assert plan["counts"]["knowledge_objects"] == 1
        assert plan["affected_other_rows"]["supervisor_links_removed"] == 1
        assert plan["identity_tombstones_planned"] == 1

        deleted_response = client.request(
            "DELETE",
            f"/api/admin/users/{target}",
            headers=_headers(settings),
            json={"confirmation": target, "fingerprint": plan["fingerprint"]},
        )
        assert deleted_response.status_code == 200, deleted_response.text
        outcome = deleted_response.json()
        assert outcome["status"] == "deleted"
        assert outcome["deleted_rows"] == plan["planned_delete_rows"]
        assert outcome["supervisor_links_removed"] == 1
        assert outcome["tombstoned"] is True
        assert outcome["identity_tombstones_created"] == 1
        assert outcome["retained"]["audit_log"] == plan["retained"]["audit_log"] + 1

        assert storage.get_user(target) is None
        assert storage.resolve_identity("sso", "555001") is None
        assert storage.get_raw_object(raw_id, target) is None
        assert storage.get_knowledge_object(knowledge_id, target) is None
        assert (
            storage.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (target,)).fetchone()[0] == 0
        )
        assert storage.execute("PRAGMA foreign_key_check").fetchall() == []

        # Cross-tenant proof: not merely row counts, but the exact neighbour ids.
        assert storage.get_user(neighbour) is not None
        assert storage.get_message(neighbour_message["id"], neighbour) is not None
        assert storage.get_raw_object(neighbour_raw, neighbour) is not None
        assert storage.get_knowledge_object(neighbour_knowledge, neighbour) is not None
        neighbour_meta = storage.get_user(neighbour)["metadata_json"]
        assert "supervisor_id" not in neighbour_meta
        assert "preserve" in neighbour_meta

        tombstone = storage.kv_get(deleted_account_tombstone_key(target))
        assert tombstone is not None
        assert storage.kv_get(deleted_identity_tombstone_key("sso", "555001")) is not None
        with pytest.raises(DeletedAccountError):
            storage.ensure_user(target, source="telegram")
        with pytest.raises(DeletedAccountError):
            storage.link_identity("sso", "555001", neighbour, linked_by=LEGACY_OWNER_USER_ID)
        with pytest.raises(DeletedAccountError):
            storage.ensure_user(
                "local:replacement-login",
                source="sso",
                external_id="555001",
            )
        assert storage.get_user("local:replacement-login") is None
        with pytest.raises(DeletedAccountError):
            storage.kv_set(f"workers:lifecycle:{target}", '{"late":true}')
        with pytest.raises(DeletedAccountError):
            storage.record_event(
                "graph.entities_pruned",
                {"user_ids": [target], "names": [marker]},
            )

        target_vault_dir = storage.settings.memory_vault_dir / "users" / _safe_component(target)
        with pytest.raises(VaultAccountWriteBlocked):
            client.app.state.memory_vault.sync_object(
                {
                    "id": "ko-stale-after-delete",
                    "user_id": target,
                    "title": "Must not return",
                    "content": marker,
                }
            )
        assert not target_vault_dir.exists()
        assert client.portal.call(client.app.state.account_activity_gate.is_closed, target) is True

        recreate = client.post(
            "/api/admin/users",
            headers=_headers(settings),
            json={"id": target, "display_name": "Must not return", "preset_key": "user"},
        )
        assert recreate.status_code == 409

        audit = [
            row
            for row in storage.list_audit_log(None, limit=100)
            if row["action"] == "admin.user.delete" and row["target_id"] == target
        ]
        assert len(audit) == 1


def test_confirmation_and_fingerprint_are_both_fail_closed(hard_delete_settings) -> None:
    settings = hard_delete_settings
    target = "local:stale-delete"
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        _disable_user(client, settings, target)
        plan = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings)).json()
        assert plan["ready"] is True
        verified_plan = _verified_plan(storage, target)
        assert verified_plan["ready"] is True

        mistyped = client.request(
            "DELETE",
            f"/api/admin/users/{target}",
            headers=_headers(settings),
            json={"confirmation": target.upper(), "fingerprint": plan["fingerprint"]},
        )
        assert mistyped.status_code == 400
        assert storage.get_user(target) is not None

        # A token appeared after the administrator reviewed the counts.  The old
        # fingerprint cannot authorize a different deletion plan.
        token = storage.create_api_token(target, hashlib.sha256(b"late-token").hexdigest())
        stale = client.request(
            "DELETE",
            f"/api/admin/users/{target}",
            headers=_headers(settings),
            json={"confirmation": target, "fingerprint": plan["fingerprint"]},
        )
        assert stale.status_code == 409
        assert storage.get_user(target) is not None
        assert storage.get_api_token(token["id"]) is not None
        assert storage.kv_get(deleted_account_tombstone_key(target)) is None
        with pytest.raises(AccountDeletionConflict, match="изменилась"):
            delete_account(
                storage,
                target,
                expected_fingerprint=verified_plan["fingerprint"],
                actor_user_id=LEGACY_OWNER_USER_ID,
                quiescence_verified=True,
            )


def test_delete_waits_for_cli_export_or_backup_contour(hard_delete_settings) -> None:
    settings = hard_delete_settings
    from friday.diagnostics.runtime_lease import ProcessLease

    target = "local:cli-snapshot-race"
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        _disable_user(client, settings, target)
        plan = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings)).json()
        assert plan["ready"] is True

        with ProcessLease(
            settings.state_dir / "account-deletion.lock",
            protocol="friday.account-deletion.v1",
        ):
            blocked = client.request(
                "DELETE",
                f"/api/admin/users/{target}",
                headers=_headers(settings),
                json={"confirmation": target, "fingerprint": plan["fingerprint"]},
            )

        assert blocked.status_code == 409
        assert "экспорта" in blocked.json()["detail"]
        assert storage.get_user(target) is not None
        assert storage.kv_get(deleted_account_tombstone_key(target)) is None

        deleted = client.request(
            "DELETE",
            f"/api/admin/users/{target}",
            headers=_headers(settings),
            json={"confirmation": target, "fingerprint": plan["fingerprint"]},
        )
        assert deleted.status_code == 200, deleted.text
        assert storage.get_user(target) is None


def test_preflight_names_missing_worker_off_maintenance_proof(hard_delete_settings) -> None:
    settings = hard_delete_settings
    target = "local:maintenance-required"
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        _disable_user(client, settings, target)
        plan = preflight_account_deletion(storage, target, quiescence_available=False)
        assert plan["ready"] is False
        assert {item["code"] for item in plan["blockers"]} == {"quiescence_unavailable"}

        online = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings)).json()
        assert online["ready"] is True


def test_global_delete_gate_drains_other_admin_requests_and_reopens_traffic() -> None:
    async def scenario() -> None:
        gate = AccountActivityGate()
        entered = asyncio.Event()
        release = asyncio.Event()
        target = "local:global-drain-target"

        async def stale_other_admin_request() -> None:
            async with gate.hold("local:other-admin"):
                entered.set()
                await release.wait()

        stale = asyncio.create_task(stale_other_admin_request())
        await entered.wait()
        async with gate.hold(LEGACY_OWNER_USER_ID) as delete_token:

            async def release_after_global_close() -> None:
                while not await gate.is_closed(target):
                    await asyncio.sleep(0)
                with pytest.raises(AccountGateClosed):
                    async with gate.hold("local:late-admin"):
                        pass
                release.set()

            releaser = asyncio.create_task(release_after_global_close())
            lease = await gate.close_world_and_drain(
                target,
                exclude_token=delete_token,
                timeout=1.0,
            )
            assert stale.done()
            await lease.commit()
            await releaser
        await stale

        async with gate.hold("local:late-admin"):
            pass
        with pytest.raises(AccountGateClosed):
            async with gate.hold(target):
                pass

    asyncio.run(scenario())


def test_cancelled_global_drain_reopens_every_admission() -> None:
    async def scenario() -> None:
        gate = AccountActivityGate()
        target = "local:cancelled-drain-target"
        async with (
            gate.hold("local:slow-writer"),
            gate.hold(LEGACY_OWNER_USER_ID) as delete_token,
        ):
            drain = asyncio.create_task(
                gate.close_world_and_drain(
                    target,
                    exclude_token=delete_token,
                    timeout=10.0,
                )
            )
            while not await gate.is_closed(target):
                await asyncio.sleep(0)
            drain.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain

            assert await gate.is_closed(target) is False
            async with gate.hold(target):
                pass
            async with gate.hold("local:new-request"):
                pass

    asyncio.run(scenario())


def test_cancelled_postcommit_cleanup_still_reopens_global_traffic() -> None:
    async def scenario() -> None:
        gate = AccountActivityGate()
        target = "local:cancelled-postcommit-target"
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        original_finish = gate._finish_drain

        async def delayed_finish(user_id: str, *, keep_target_closed: bool) -> None:
            cleanup_started.set()
            await allow_cleanup.wait()
            await original_finish(user_id, keep_target_closed=keep_target_closed)

        gate._finish_drain = delayed_finish  # type: ignore[method-assign]
        async with gate.hold(LEGACY_OWNER_USER_ID) as delete_token:
            lease = await gate.close_world_and_drain(
                target,
                exclude_token=delete_token,
                timeout=1.0,
            )
            commit = asyncio.create_task(lease.commit())
            await cleanup_started.wait()
            commit.cancel()
            with pytest.raises(asyncio.CancelledError):
                await commit
            allow_cleanup.set()
            while await gate.is_closed("local:new-request"):
                await asyncio.sleep(0)

            async with gate.hold("local:new-request"):
                pass
            assert await gate.is_closed(target) is True

    asyncio.run(scenario())


def test_http_offload_is_registered_before_executor_start_and_survives_await_cancellation() -> None:
    from friday.workers._blocking import current_activity, in_flight, run_blocking

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        occupying = threading.Event()
        release_occupying = threading.Event()
        offload_finished = threading.Event()

        def occupy_only_thread() -> None:
            occupying.set()
            release_occupying.wait(5)

        first = loop.run_in_executor(None, occupy_only_thread)
        while not occupying.is_set():
            await asyncio.sleep(0)
        activity_token = current_activity.set("http")
        try:
            queued = asyncio.create_task(run_blocking(offload_finished.set))
            while in_flight("http") != 1:
                await asyncio.sleep(0)
            assert offload_finished.is_set() is False
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert in_flight("http") == 1

            release_occupying.set()
            await first
            while in_flight("http"):
                await asyncio.sleep(0)
            assert offload_finished.is_set() is True
        finally:
            current_activity.reset(activity_token)
            release_occupying.set()
            executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_delete_drains_cancelled_http_export_thread_then_rechecks_artifact(
    hard_delete_settings,
) -> None:
    settings = hard_delete_settings
    from friday.workers._blocking import _tracked

    target = "local:orphan-http-export"
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        _disable_user(client, settings, target)
        plan = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings)).json()
        assert plan["ready"] is True

        orphan_started = threading.Event()
        release_orphan = threading.Event()
        exported: dict[str, str] = {}

        def stale_export() -> None:
            orphan_started.set()
            release_orphan.wait(5)
            exported.update(storage.export_user(target))

        orphan = threading.Thread(target=lambda: _tracked("http", stale_export))
        orphan.start()
        assert orphan_started.wait(5)

        response_box: dict[str, object] = {}

        def request_delete() -> None:
            response_box["response"] = client.request(
                "DELETE",
                f"/api/admin/users/{target}",
                headers=_headers(settings),
                json={"confirmation": target, "fingerprint": plan["fingerprint"]},
            )

        deleting = threading.Thread(target=request_delete)
        deleting.start()
        while not client.portal.call(client.app.state.account_activity_gate.is_closed, target):
            deleting.join(timeout=0.01)
            assert deleting.is_alive(), "delete escaped the physical offload drain"
        assert storage.get_user(target) is not None

        release_orphan.set()
        orphan.join(timeout=10)
        deleting.join(timeout=10)
        assert not orphan.is_alive()
        assert not deleting.is_alive()
        response = response_box["response"]
        assert response.status_code == 409
        assert storage.get_user(target) is not None
        assert Path(exported["path"]).is_file()


def test_http_routes_have_no_untracked_executor_escape() -> None:
    root = Path(__file__).resolve().parents[1] / "friday"
    offenders: list[str] = []
    for package in (root / "admin_api", root / "api"):
        for path in package.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if (
                "asyncio.to_thread(" in source
                or ".run_in_executor(" in source
                or "to_thread.run_sync(" in source
            ):
                offenders.append(str(path.relative_to(root.parent)))
    assert offenders == []


def test_shared_tenant_authorship_blocks_without_touching_either_account(
    hard_delete_settings,
) -> None:
    settings = hard_delete_settings
    target = "local:shared-author"
    tenant = "local:shared-tenant"
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        _create_user(client, settings, tenant)
        raw = RawObject(
            id=new_id("raw"),
            user_id=tenant,
            source="upload",
            source_ref=new_id("src"),
            raw_content="shared text",
            content_type="text",
            metadata_json={"requested_by": target},
        )
        storage.store_raw_object(raw)
        _disable_user(client, settings, target)

        response = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings))
        assert response.status_code == 200
        plan = response.json()
        assert plan["ready"] is False
        assert plan["shared_owned"]["shared_uploads"] == 1
        assert {item["code"] for item in plan["blockers"]} == {
            "cross_account_json_references",
            "shared_owned_data",
        }

        refused = client.request(
            "DELETE",
            f"/api/admin/users/{target}",
            headers=_headers(settings),
            json={"confirmation": target, "fingerprint": plan["fingerprint"]},
        )
        assert refused.status_code == 409
        assert storage.get_user(target) is not None
        assert storage.get_user(tenant) is not None
        assert storage.get_raw_object(raw.id, tenant) is not None


def test_file_source_alias_account_axes_are_counted_once_and_deleted_without_raw(storage) -> None:
    actor = "local:file-alias-delete-admin"
    target = "local:file-alias-delete-target"
    survivor = "local:file-alias-delete-survivor"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.ensure_user(survivor)
    raw = RawObject(
        id=new_id("raw"),
        user_id=survivor,
        source="upload",
        source_ref="telegram-update:surviving-canonical-file",
        raw_content="[File: survivor.txt]",
        content_type="file",
        metadata_json={"filename": "survivor.txt", "uploaded_by": survivor},
    )
    storage.store_raw_object(raw)
    now = utc_now()
    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,created_at
               ) VALUES(?,?,?,?,?)""",
            (
                (target, target, "telegram-file:TARGET-BOTH-AXES", raw.id, now),
                (survivor, target, "telegram-file:TARGET-UPLOADER-AXIS", raw.id, now),
                (survivor, survivor, "telegram-file:SURVIVOR-ALIAS", raw.id, now),
            ),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is True, plan
    assert plan["counts"]["file_source_aliases"] == 2
    outcome = delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )

    assert outcome["deleted"]["file_source_aliases"] == 2
    assert outcome["deleted_rows"] == plan["planned_delete_rows"]
    assert storage.get_raw_object(raw.id, survivor) is not None
    survivor_aliases = storage.execute(
        "SELECT user_id,uploaded_by,source_ref FROM file_source_aliases ORDER BY source_ref"
    ).fetchall()
    assert [tuple(row) for row in survivor_aliases] == [(survivor, survivor, "telegram-file:SURVIVOR-ALIAS")]


def test_cross_account_identity_attribution_blocks_without_revoking_neighbour(storage) -> None:
    target = "local:identity-link-author"
    neighbour = "local:identity-link-owner"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    storage.link_identity("telegram", "667788", neighbour, linked_by=target)
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_references"]["identity_links_created"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_references"}
    assert storage.resolve_identity("telegram", "667788") == neighbour
    assert storage.get_user(target) is not None


def test_cross_tenant_non_fk_object_reference_is_reported_before_delete(storage) -> None:
    target = "local:entity-reference-owner"
    neighbour = "local:entity-reference-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    entity = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Target entity"))
    _raw_id, knowledge_id = _put_text_knowledge(storage, neighbour, "neighbour knowledge")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET entity_id=? WHERE id=? AND user_id=?",
            (entity.id, knowledge_id, neighbour),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_object_references"]["non_fk"]["knowledge_entity"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}
    assert storage.get_entity(entity.id, target) is not None
    assert storage.execute(
        "SELECT 1 FROM knowledge_objects WHERE id=? AND user_id=?", (knowledge_id, neighbour)
    ).fetchone()


def test_cross_tenant_append_only_revision_endpoint_is_reported(storage) -> None:
    target = "local:revision-endpoint-owner"
    neighbour = "local:revision-endpoint-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    target_entity = storage.create_entity(
        Entity(id=new_id("ent"), user_id=target, name="Historical target endpoint")
    )
    neighbour_entity = storage.create_entity(
        Entity(id=new_id("ent"), user_id=neighbour, name="Neighbour endpoint")
    )
    relation_id = new_id("rel")
    now = utc_now()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO relations(
                   id,user_id,source_entity_id,target_entity_id,relation_type,weight,
                   metadata_json,created_at,valid_from)
               VALUES(?,?,?,?, 'related_to',1.0,'{}',?,'')""",
            (relation_id, neighbour, target_entity.id, neighbour_entity.id, now),
        )
        conn.execute("DELETE FROM relations WHERE id=?", (relation_id,))
    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (relation_id,)).fetchone() is None
    assert storage.execute("SELECT 1 FROM relation_revisions WHERE relation_id=?", (relation_id,)).fetchone()
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_object_references"]["non_fk"]["relation_revision_endpoints"] >= 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}


def test_cross_tenant_feedback_candidate_targets_are_reported(storage) -> None:
    target = "local:feedback-candidate-owner"
    neighbour = "local:feedback-candidate-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    source = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Candidate A"))
    destination = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Candidate B"))
    candidate_id = new_id("relc")
    feedback_id = new_id("fb")
    now = utc_now()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO relation_candidates(
                   id,user_id,source_entity_id,target_entity_id,relation_type,
                   confidence,evidence_json,status,created_at)
               VALUES(?,?,?,?, 'related_to',0.5,'{}','suggested',?)""",
            (candidate_id, target, source.id, destination.id, now),
        )
        conn.execute(
            """INSERT INTO feedback(
                   id,user_id,target_type,target_id,feedback_type,score,comment,context_json,created_at)
               VALUES(?,?, 'relation_candidate',?,'general',0.0,'','{}',?)""",
            (feedback_id, neighbour, candidate_id, now),
        )
        conn.execute(
            """INSERT INTO feedback_state(
                   user_id,target_type,target_id,feedback_type,score,comment,
                   context_json,feedback_id,updated_at)
               VALUES(?, 'relation_candidate',?,'general',0.0,'','{}',?,?)""",
            (neighbour, candidate_id, feedback_id, now),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    refs = plan["cross_account_object_references"]["non_fk"]
    assert refs["feedback_targets"] == 1
    assert refs["feedback_state_targets"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}


def test_cross_tenant_eval_case_expected_ids_and_malformed_shapes_are_reported(storage) -> None:
    target = "local:eval-target-owner"
    neighbour = "local:eval-case-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    _raw_id, knowledge_id = _put_text_knowledge(storage, target, "eval target material")
    now = utc_now()
    with storage.transaction() as conn:
        for index, expected_ids in enumerate(
            (json.dumps([knowledge_id]), '{"broken":', json.dumps({"id": knowledge_id}))
        ):
            conn.execute(
                """INSERT INTO eval_cases(
                       id,user_id,query,expected_ids_json,note,source,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (new_id("eval"), neighbour, f"case {index}", expected_ids, "", "legacy", now),
            )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    non_fk = plan["cross_account_object_references"]["non_fk"]
    assert non_fk["eval_case_expected_ids"] == 3
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}
    assert storage.get_knowledge_object(knowledge_id, target) is not None


def test_cross_tenant_approval_payload_object_id_is_reported(storage) -> None:
    target = "local:approval-object-owner"
    neighbour = "local:approval-object-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    source = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Candidate A"))
    destination = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Candidate B"))
    candidate = EntityResolutionCandidate(
        id=new_id("er"),
        user_id=target,
        entity_a_id=source.id,
        entity_b_id=destination.id,
        confidence=0.9,
        resolution_method="test",
    )
    storage.store_resolution_candidate(candidate)
    approval = storage.create_action_approval(
        neighbour,
        tool="entity_merge_decide",
        payload={"candidate_id": candidate.id, "decision": "accept"},
        requested_by=neighbour,
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    non_fk = plan["cross_account_object_references"]["non_fk"]
    assert non_fk["structural_json:action_approvals.payload_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}
    assert storage.get_action_approval(approval["id"], neighbour) is not None
    assert storage.get_resolution_candidate(candidate.id, target) is not None


def test_cross_tenant_incoming_fk_is_reported_in_preflight_not_as_delete_500(storage) -> None:
    target = "local:raw-reference-owner"
    neighbour = "local:raw-reference-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    target_raw = RawObject(
        id=new_id("raw"),
        user_id=target,
        source="test",
        source_ref=new_id("src"),
        raw_content="target raw",
        content_type="text",
    )
    storage.store_raw_object(target_raw)
    _neighbour_raw, neighbour_knowledge = _put_text_knowledge(storage, neighbour, "neighbour")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET raw_object_id=? WHERE id=? AND user_id=?",
            (target_raw.id, neighbour_knowledge, neighbour),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    foreign = plan["cross_account_object_references"]["foreign_keys"]
    assert foreign["knowledge_objects.raw_object_id->raw_objects.id"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}
    assert storage.get_raw_object(target_raw.id, target) is not None


def test_future_incoming_fk_without_an_owner_column_is_fail_closed(storage) -> None:
    target = "local:future-fk-owner"
    storage.ensure_user(target)
    target_raw = RawObject(
        id=new_id("raw"),
        user_id=target,
        source="test",
        source_ref=new_id("src"),
        raw_content="future reference target",
        content_type="text",
    )
    storage.store_raw_object(target_raw)
    with storage.transaction() as conn:
        conn.execute(
            """CREATE TABLE future_raw_reference(
                   id TEXT PRIMARY KEY,
                   raw_reference TEXT NOT NULL REFERENCES raw_objects(id))"""
        )
        conn.execute(
            "INSERT INTO future_raw_reference(id,raw_reference) VALUES('future-ref',?)",
            (target_raw.id,),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    foreign = plan["cross_account_object_references"]["foreign_keys"]
    assert foreign["future_raw_reference.raw_reference->raw_objects.id"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}


def test_cross_tenant_json_provenance_and_versions_are_declared(storage) -> None:
    target = "local:json-reviewer"
    neighbour = "local:json-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    entity = storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id=neighbour,
            name="Reviewed entity",
            metadata_json={"accepted_by": target},
        )
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_json_references"]["entities.metadata_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}
    assert storage.get_entity(entity.id, neighbour) is not None


def test_cross_tenant_json_provenance_in_an_object_key_is_declared(storage) -> None:
    target = "local:json-key-reviewer"
    neighbour = "local:json-key-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    entity = storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id=neighbour,
            name="Keyed reviewer map",
            metadata_json={target: {"decision": "accepted"}},
        )
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_json_references"]["entities.metadata_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}
    assert storage.get_entity(entity.id, neighbour) is not None


def test_malformed_cross_tenant_json_fails_closed(storage) -> None:
    target = "local:malformed-json-reviewer"
    neighbour = "local:malformed-json-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    entity = storage.create_entity(Entity(id=new_id("ent"), user_id=neighbour, name="Legacy row"))
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entities SET metadata_json=? WHERE id=? AND user_id=?",
            ('{"accepted_by":"' + target + '"', entity.id, neighbour),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_json_references"]["entities.metadata_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}


def test_only_plain_target_supervisor_link_is_the_removable_json_exception(storage) -> None:
    target = "local:supervisor-shape-reviewer"
    object_neighbour = "local:object-supervisor-neighbour"
    agent_neighbour = "local:agent-supervisor-neighbour"
    for user_id in (target, object_neighbour, agent_neighbour):
        storage.ensure_user(user_id)
    storage.update_user(
        object_neighbour,
        metadata_json={"supervisor_id": {"accepted_by": target}},
    )
    storage.update_user(
        agent_neighbour,
        metadata_json={"supervisor_id": f"agent:{target}"},
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["affected_other_rows"]["supervisor_links_removed"] == 0
    assert plan["cross_account_json_references"]["users.metadata_json"] == 2
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}


def test_numeric_supervisor_value_uses_the_same_runtime_coercion_and_is_removed(storage) -> None:
    from friday.oversight_scope import supervisor_of

    actor = "local:numeric-supervisor-admin"
    target = "123"
    neighbour = "local:numeric-supervisor-neighbour"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    storage.update_user(neighbour, metadata_json={"supervisor_id": 123, "keep": "yes"})
    assert supervisor_of(storage, neighbour) == target
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is True, plan
    assert plan["affected_other_rows"]["supervisor_links_removed"] == 1
    outcome = delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )
    assert outcome["supervisor_links_removed"] == 1
    metadata = json.loads(str(storage.get_user(neighbour)["metadata_json"]))
    assert "supervisor_id" not in metadata
    assert metadata["keep"] == "yes"


def test_compressed_cross_tenant_version_provenance_is_declared(storage) -> None:
    target = "local:compressed-version-reviewer"
    neighbour = "local:compressed-version-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    raw = RawObject(
        id=new_id("raw"),
        user_id=neighbour,
        source="test",
        source_ref=new_id("src"),
        raw_content="versioned body",
        content_type="text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=neighbour,
        raw_object_id=raw.id,
        content="versioned body",
        title="v1",
        metadata_json={"accepted_by": target},
    )
    storage.store_knowledge_object(knowledge)
    for version in range(2, 7):
        assert storage.update_knowledge_fields(
            knowledge.id,
            neighbour,
            title=f"v{version}",
            metadata_json={},
        )
    oldest = storage.execute(
        """SELECT typeof(snapshot_json) AS storage_type
             FROM knowledge_object_versions
            WHERE knowledge_object_id=? AND version=1""",
        (knowledge.id,),
    ).fetchone()
    assert oldest is not None and oldest["storage_type"] == "blob"
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_json_references"]["knowledge_object_versions.snapshot_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}


def test_compressed_cross_tenant_version_structural_id_is_declared(storage) -> None:
    target = "local:compressed-object-owner"
    neighbour = "local:compressed-object-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    _target_raw, target_knowledge_id = _put_text_knowledge(storage, target, "target body")
    raw = RawObject(
        id=new_id("raw"),
        user_id=neighbour,
        source="test",
        source_ref=new_id("src"),
        raw_content="neighbour body",
        content_type="text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=neighbour,
        raw_object_id=raw.id,
        content="neighbour body",
        title="v1",
        metadata_json={"related": target_knowledge_id},
    )
    storage.store_knowledge_object(knowledge)
    for version in range(2, 7):
        assert storage.update_knowledge_fields(
            knowledge.id,
            neighbour,
            title=f"v{version}",
            metadata_json={},
        )
    oldest = storage.execute(
        """SELECT typeof(snapshot_json) AS storage_type
             FROM knowledge_object_versions
            WHERE knowledge_object_id=? AND version=1""",
        (knowledge.id,),
    ).fetchone()
    assert oldest is not None and oldest["storage_type"] == "blob"
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    non_fk = plan["cross_account_object_references"]["non_fk"]
    assert non_fk["structural_json:knowledge_object_versions.snapshot_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}


def test_runtime_event_user_reference_is_retained_and_blocks_claim_of_erasure(storage) -> None:
    target = "local:event-history-user"
    storage.ensure_user(target)
    event_id = storage.record_event("embeddings.reindex_requested", {"user_id": target})
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["runtime_event_references"] == 1
    assert plan["counts"]["runtime_events_retained"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"runtime_event_history"}
    assert storage.list_events(limit=10)[0]["id"] == event_id


def test_runtime_event_structural_object_reference_blocks_claim_of_erasure(storage) -> None:
    target = "local:event-object-owner"
    storage.ensure_user(target)
    _raw_id, knowledge_id = _put_text_knowledge(storage, target, "event object body")
    event_id = storage.record_event("knowledge.reviewed", {"knowledge_object_id": knowledge_id})
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    non_fk = plan["cross_account_object_references"]["non_fk"]
    assert non_fk["structural_json:runtime_events.payload"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_object_references"}
    assert storage.list_events(limit=10)[0]["id"] == event_id


def test_oversight_audit_marker_blocks_untraceable_cross_account_chat_derivative(storage) -> None:
    target = "local:oversight-source"
    neighbour = "local:oversight-reader"
    marker = "TARGET-DERIVATIVE-IN-NEIGHBOUR-CHAT"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    _put_text_knowledge(storage, target, marker)
    conversation = storage.create_conversation(neighbour, "Oversight result")
    derivative = storage.store_message(conversation["id"], neighbour, "assistant", marker)
    storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id=neighbour,
            action="tool.user_activity",
            target_type="user",
            target_id=target,
            after_json={"content": "full"},
        )
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["cross_account_chat_derivatives"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_chat_derivatives"}
    assert storage.get_message(derivative["id"], neighbour) is not None


def test_unscoped_personal_runtime_event_during_account_lifetime_blocks(storage) -> None:
    target = "local:legacy-unscoped-event-user"
    storage.ensure_user(target)
    created_at = str(storage.get_user(target)["created_at"])
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_events(id,event_type,payload,created_at) VALUES(?,?,?,?)",
            (
                new_id("evt"),
                "graph.entities_pruned",
                json.dumps({"names": ["Sensitive Person"]}),
                created_at,
            ),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["runtime_event_references"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"runtime_event_history"}


def test_malformed_runtime_event_during_account_lifetime_fails_closed(storage) -> None:
    target = "local:malformed-runtime-event-user"
    storage.ensure_user(target)
    created_at = str(storage.get_user(target)["created_at"])
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_events(id,event_type,payload,created_at) VALUES(?,?,?,?)",
            (new_id("evt"), "legacy.unknown", '{"user_id":', created_at),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["runtime_event_references"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"runtime_event_history"}


def test_unscoped_personal_runtime_event_before_account_birth_does_not_poison_clean_path(storage) -> None:
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_events(id,event_type,payload,created_at) VALUES(?,?,?,?)",
            (
                new_id("evt"),
                "graph.entities_pruned",
                json.dumps({"names": ["Historical Person"]}),
                "2000-01-01T00:00:00+00:00",
            ),
        )
    target = "local:post-journal-clean-user"
    storage.ensure_user(target)
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is True, plan


def test_immutable_chat_history_is_reported_instead_of_bypassed(hard_delete_settings) -> None:
    settings = hard_delete_settings
    target = "local:chat-history-delete"
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        _create_user(client, settings, target)
        conversation = storage.create_conversation(target, "Kept by policy")
        message = storage.store_message(conversation["id"], target, "user", "said once")
        _disable_user(client, settings, target)

        response = client.get(f"/api/admin/users/{target}/deletion", headers=_headers(settings))
        assert response.status_code == 200
        plan = response.json()
        assert plan["ready"] is False
        assert plan["counts"]["conversations"] == 1
        assert plan["counts"]["messages"] == 1
        assert {item["code"] for item in plan["blockers"]} == {
            "chat_history",
        }
        assert storage.get_message(message["id"], target) is not None


def test_audit_failure_rolls_back_every_account_mutation(storage, monkeypatch) -> None:
    target = "local:rollback-delete"
    actor = "local:deleting-admin"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.link_identity("sso", "rollback-identity", target, linked_by=actor)
    storage.update_user(target, status="disabled")
    plan = _verified_plan(storage, target)
    assert plan["ready"] is True

    def fail_audit(_entry) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(storage, "log_audit", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        delete_account(
            storage,
            target,
            expected_fingerprint=plan["fingerprint"],
            actor_user_id=actor,
            quiescence_verified=True,
        )

    assert storage.get_user(target) is not None
    assert storage.resolve_identity("sso", "rollback-identity") == target
    assert storage.kv_get(deleted_account_tombstone_key(target)) is None
    assert storage.kv_get(deleted_identity_tombstone_key("sso", "rollback-identity")) is None
    assert storage.execute("PRAGMA foreign_key_check").fetchall() == []


def test_merge_transfer_json_provenance_is_not_left_in_another_tenant(storage) -> None:
    target = "local:merge-provenance-reviewer"
    neighbour = "local:merge-provenance-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    source = storage.create_entity(Entity(id=new_id("ent"), user_id=neighbour, name="Source"))
    destination = storage.create_entity(Entity(id=new_id("ent"), user_id=neighbour, name="Destination"))
    now = utc_now()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_merge_history(
                   id,user_id,source_entity_id,target_entity_id,source_snapshot_json,
                   target_before_json,target_after_json,transfer_json,merged_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("merge"),
                neighbour,
                source.id,
                destination.id,
                "{}",
                "{}",
                "{}",
                '{"suppressed_link":{"reviewed_by":"' + target + '"}}',
                neighbour,
                now,
            ),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_json_references"]["entity_merge_history.transfer_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}


def test_merge_snapshot_nested_json_string_provenance_is_declared(storage) -> None:
    target = "local:nested-merge-reviewer"
    neighbour = "local:nested-merge-neighbour"
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    source = storage.create_entity(Entity(id=new_id("ent"), user_id=neighbour, name="Nested source"))
    destination = storage.create_entity(
        Entity(id=new_id("ent"), user_id=neighbour, name="Nested destination")
    )
    nested_snapshot = json.dumps(
        {"metadata_json": json.dumps({"accepted_by": target}, ensure_ascii=False)},
        ensure_ascii=False,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_merge_history(
                   id,user_id,source_entity_id,target_entity_id,source_snapshot_json,
                   target_before_json,target_after_json,transfer_json,merged_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("merge"),
                neighbour,
                source.id,
                destination.id,
                nested_snapshot,
                "{}",
                "{}",
                "{}",
                neighbour,
                utc_now(),
            ),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["cross_account_json_references"]["entity_merge_history.source_snapshot_json"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}


def test_nonempty_account_directory_blocks_without_reading_or_removing_it(storage) -> None:
    target = "local:file-directory-delete"
    storage.ensure_user(target)
    storage.update_user(target, status="disabled")
    files_dir = _account_files_directory(storage, target)
    files_dir.mkdir(parents=True)
    marker = files_dir / "must-survive.txt"
    marker.write_text("file body is not part of preflight", encoding="utf-8")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {"file_directory"}
    assert marker.read_text(encoding="utf-8") == "file body is not part of preflight"
    assert storage.get_user(target) is not None


def test_nonempty_obsidian_directory_blocks_without_reading_or_removing_it(storage) -> None:
    target = "local:obsidian-directory-delete"
    storage.ensure_user(target)
    storage.update_user(target, status="disabled")
    obsidian_dir = _obsidian_account_directory(storage, target)
    obsidian_dir.mkdir(parents=True)
    marker = obsidian_dir / "must-survive.md"
    marker.write_text("Obsidian material is outside the SQLite cascade", encoding="utf-8")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {"obsidian_directory"}
    assert marker.read_text(encoding="utf-8") == "Obsidian material is outside the SQLite cascade"
    assert storage.get_user(target) is not None


def test_obsidian_rows_are_counted_deleted_and_empty_owner_directory_is_removed(storage) -> None:
    actor = "local:obsidian-delete-admin"
    target = "local:obsidian-delete-target"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    account_dir = _obsidian_account_directory(storage, target)
    account_dir.mkdir(parents=True)
    bundle = storage.create_obsidian_bundle(
        target,
        config_root=str(account_dir / "profile"),
        database_root=str(account_dir / "database"),
        api_endpoint="unix:///tmp/obsidian-delete-target.sock",
        api_key_ref="secret:obsidian:delete-target",
        server_path=str(account_dir / "vault"),
        folder_id="friday-delete-target",
        setup_token_hash=hashlib.sha256(b"obsidian-delete-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
        convention={},
    )
    storage.transition_obsidian_onboarding(target, "awaiting_device_id_handoff")
    storage.transition_obsidian_onboarding(target, "awaiting_android_device")
    storage.record_obsidian_pairing_candidates(
        target,
        [{"syncthing_device_id": "AAAA-BBBB", "display_name": "Pixel"}],
    )
    storage.bind_obsidian_android_device(
        target,
        syncthing_device_id="AAAA-BBBB",
        display_name="Pixel",
    )
    storage.prepare_obsidian_operation(
        target,
        operation_id="delete-operation",
        vault_id=bundle["vault"]["id"],
        method="create",
        arguments_digest=hashlib.sha256(b"delete-operation").hexdigest(),
    )
    storage.record_obsidian_conflict(
        target,
        vault_id=bundle["vault"]["id"],
        canonical_path="Notes/A.md",
        conflict_path="Notes/A.sync-conflict.md",
    )
    binding = storage.upsert_obsidian_note_binding(
        target,
        vault_id=bundle["vault"]["id"],
        integration_id="obnote-delete-target",
        current_path="Notes/Delete.md",
        current_revision="a" * 64,
        origin="user",
    )
    storage.upsert_obsidian_note_index(
        target,
        binding_id=binding["id"],
        revision="a" * 64,
        body_text="delete lifecycle",
    )
    storage.replace_obsidian_note_links(
        target,
        binding_id=binding["id"],
        revision="a" * 64,
        links=[
            {
                "kind": "wikilink",
                "target_text": "Notes/Delete",
                "resolved_binding_id": binding["id"],
            }
        ],
    )
    candidate_set = storage.create_obsidian_candidate_set(
        target,
        vault_id=bundle["vault"]["id"],
        query={"text": "delete"},
        candidates=[{"binding_id": binding["id"]}],
        expires_at="2030-01-01T00:00:00+00:00",
    )
    storage.select_obsidian_candidate(target, candidate_set["id"], 1)
    storage.upsert_obsidian_active_frame(
        target,
        vault_id=bundle["vault"]["id"],
        frame_id="delete-frame",
        candidate_set_id=candidate_set["id"],
        expires_at="2030-01-01T00:00:00+00:00",
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is True, plan
    for table in (
        "obsidian_sync_profiles",
        "obsidian_android_devices",
        "obsidian_vaults",
        "obsidian_onboarding_sessions",
        "obsidian_pairing_candidates",
        "obsidian_operations",
        "obsidian_conflicts",
        "obsidian_note_bindings",
        "obsidian_note_index",
        "obsidian_note_links",
        "obsidian_candidate_sets",
        "obsidian_candidate_set_items",
        "obsidian_active_frames",
    ):
        assert plan["counts"][table] == 1

    outcome = delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )

    assert outcome["status"] == "deleted"
    assert not account_dir.exists()
    for table in (
        "obsidian_sync_profiles",
        "obsidian_android_devices",
        "obsidian_vaults",
        "obsidian_onboarding_sessions",
        "obsidian_pairing_candidates",
        "obsidian_operations",
        "obsidian_conflicts",
        "obsidian_note_bindings",
        "obsidian_note_index",
        "obsidian_note_links",
        "obsidian_candidate_sets",
        "obsidian_candidate_set_items",
        "obsidian_active_frames",
    ):
        remaining = storage.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?',  # nosec B608 - closed test table tuple
            (target,),
        ).fetchone()[0]
        assert remaining == 0


def test_existing_user_export_is_an_external_artifact_blocker(storage) -> None:
    target = "local:exported-delete"
    storage.ensure_user(target)
    assert _mark_account_deletion_history_clean(storage, target)
    export = storage.export_user(target)
    storage.update_user(target, status="disabled")

    plan = preflight_account_deletion(storage, target, quiescence_available=True)

    assert plan["ready"] is False
    assert plan["counts"]["exports_retained"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"export_artifacts"}
    assert storage.get_user(target) is not None
    assert Path(export["path"]).is_file()


def test_file_row_without_a_stored_path_still_blocks_deletion(storage) -> None:
    target = "local:legacy-file-delete"
    storage.ensure_user(target)
    raw = RawObject(
        id=new_id("raw"),
        user_id=target,
        source="legacy",
        source_ref="legacy-file-without-path",
        raw_content="legacy preview",
        content_type="file",
        metadata_json={},
    )
    storage.store_raw_object(raw)
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["counts"]["raw_objects"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"stored_files"}
    assert storage.get_raw_object(raw.id, target) is not None


def test_telegram_identity_requires_external_queue_coordination(storage) -> None:
    target = "local:telegram-delete"
    storage.ensure_user(target)
    storage.link_identity("telegram", "992211", target, linked_by=LEGACY_OWNER_USER_ID)
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {
        "external_history_unverified",
        "external_identity_state",
    }
    assert storage.resolve_identity("telegram", "992211") == target


def test_unlinked_telegram_history_remains_a_durable_deletion_blocker(storage) -> None:
    target = "local:historical-telegram-delete"
    storage.ensure_user(target)
    assert _mark_account_deletion_history_clean(storage, target)
    storage.link_identity("telegram", "992212", target, linked_by=LEGACY_OWNER_USER_ID)
    assert storage.unlink_identity("telegram", "992212")
    storage.update_user(target, metadata_json={}, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {
        "external_history_unverified",
        "external_identity_state",
    }
    assert storage.get_user(target) is not None


def test_database_restore_invalidates_clean_history_proof_for_external_state(storage) -> None:
    from friday.diagnostics.runtime_lease import ProcessLease

    target = "local:restore-history-ambiguous"
    storage.ensure_user(target)
    assert _mark_account_deletion_history_clean(storage, target)
    backup = storage.create_backup(label="deletion-history-proof")

    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        restored = storage.restore_backup(backup["database"], safety_label="deletion-history-test")

    assert restored["invalidated_deletion_eligibility"] >= 1
    storage.update_user(target, status="disabled")
    plan = preflight_account_deletion(storage, target, quiescence_available=True)
    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {"external_history_unverified"}


def test_chat_id_history_cannot_be_hidden_by_a_later_metadata_clear(storage) -> None:
    target = "local:historical-chat-id-delete"
    storage.ensure_user(target)
    assert _mark_account_deletion_history_clean(storage, target)
    storage.update_user(target, metadata_json={"chat_id": "887766"})
    storage.update_user(target, metadata_json={}, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {
        "external_history_unverified",
        "external_identity_state",
    }


def test_canonical_identity_collision_blocks_without_revoking_survivor(storage) -> None:
    target = "local:identity-collision-target"
    survivor = "local:identity-collision-survivor"
    storage.ensure_user(target)
    storage.ensure_user(survivor)
    now = utc_now()
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO user_identities(source,external_id,user_id,linked_by,created_at)
               VALUES('Telegram','42',?,?,?),('telegram','42',?,?,?)""",
            (target, LEGACY_OWNER_USER_ID, now, survivor, LEGACY_OWNER_USER_ID, now),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {
        "external_history_unverified",
        "external_identity_state",
        "identity_collision",
    }
    with pytest.raises(ValueError, match="different accounts"):
        storage.resolve_identity("telegram", "42")
    survivor_row = storage.execute(
        """SELECT user_id FROM user_identities
           WHERE source='telegram' AND external_id='42'"""
    ).fetchone()
    assert survivor_row is not None and survivor_row["user_id"] == survivor
    assert storage.kv_get(deleted_identity_tombstone_key("telegram", "42")) is None


def test_same_account_identity_variants_share_one_transactional_tombstone(storage) -> None:
    actor = "local:canonical-identity-admin"
    target = "local:canonical-identity-target"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    now = utc_now()
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO user_identities(source,external_id,user_id,linked_by,created_at)
               VALUES('SSO','77',?,?,?),('sso','77',?,?,?)""",
            (target, actor, now, target, actor, now),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)
    assert plan["ready"] is True, plan
    assert plan["identity_tombstones_planned"] == 1
    outcome = delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )

    assert outcome["identity_tombstones_created"] == 1
    assert storage.resolve_identity("SSO", " 77 ") is None
    assert storage.resolve_identity("sso", "77") is None
    assert storage.kv_get(deleted_identity_tombstone_key("sso", "77")) is not None


def test_shared_private_reminder_markers_and_legacy_source_block(storage) -> None:
    target = "local:shared-reminder-person"
    tenant = "local:shared-reminder-tenant"
    storage.ensure_user(target)
    storage.ensure_user(tenant)
    entity = storage.create_entity(Entity(id=new_id("ent"), user_id=tenant, name="Private reminder"))
    now = utc_now()
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO private_entity_owners(entity_id,person_id,privacy_kind,created_at) "
            "VALUES(?,?,'reminder',?)",
            (entity.id, target, now),
        )
        conn.execute(
            "INSERT INTO entity_time(entity_id,user_id,occurred_at,precision,source,updated_at) "
            "VALUES(?,?,?,'day',?,?)",
            (entity.id, tenant, now, f"reminder:{target}", now),
        )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["shared_owned"]["shared_private_entities"] == 1
    assert plan["shared_owned"]["shared_legacy_reminders"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"shared_owned_data"}
    assert storage.get_user(tenant) is not None


def test_uncertain_external_action_state_blocks_and_survives(storage) -> None:
    target = "local:uncertain-action-delete"
    storage.ensure_user(target)
    approval = storage.create_action_approval(
        target,
        tool="external_mutation",
        payload={"operation": "test"},
        requested_by=target,
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE action_approvals SET status='uncertain' WHERE id=?", (approval["id"],))
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {"active_operations"}
    assert storage.get_action_approval(approval["id"], target) is not None


def test_runtime_key_boundaries_never_delete_a_neighbour_or_global_state(storage) -> None:
    actor = "local:runtime-admin"
    target = "team:alice"
    neighbour = "alice"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    storage.kv_set("eval:last_run:team:alice", "target")
    storage.kv_set("quota:web:alice:2026-08-09", "neighbour")
    storage.kv_set("workers:last_backup", "global")
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)
    assert plan["ready"] is True, plan
    delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )

    assert storage.kv_get("eval:last_run:team:alice") is None
    assert storage.kv_get("quota:web:alice:2026-08-09") == "neighbour"
    assert storage.kv_get("workers:last_backup") == "global"

    ambiguous = "team:bob"
    storage.ensure_user(ambiguous)
    storage.kv_set("quota:team:bob:2026-08-09", "belongs to a different format")
    storage.update_user(ambiguous, status="disabled")
    blocked = _verified_plan(storage, ambiguous)
    assert blocked["ready"] is False
    assert {item["code"] for item in blocked["blockers"]} == {"unknown_runtime_scope"}
    assert storage.kv_get("quota:team:bob:2026-08-09") is not None


def test_ambiguous_bare_and_graph_runtime_keys_are_never_claimed(storage) -> None:
    global_named_user = "workers:last_backup"
    storage.ensure_user(global_named_user)
    storage.kv_set(global_named_user, "global backup state")
    storage.update_user(global_named_user, status="disabled")
    global_plan = _verified_plan(storage, global_named_user)
    assert global_plan["ready"] is False
    assert {item["code"] for item in global_plan["blockers"]} == {"unknown_runtime_scope"}
    assert storage.kv_get(global_named_user) == "global backup state"

    bob = "bob"
    cursor_shaped_user = "candidate:00000003:bob:x"
    storage.ensure_user(bob)
    storage.ensure_user(cursor_shaped_user)
    ambiguous_key = "graph:mention_backfill:candidate:00000003:bob:x"
    storage.kv_set(ambiguous_key, "ambiguous graph state")
    storage.update_user(bob, status="disabled")
    bob_plan = _verified_plan(storage, bob)
    assert bob_plan["ready"] is False
    assert {item["code"] for item in bob_plan["blockers"]} == {"unknown_runtime_scope"}
    assert storage.kv_get(ambiguous_key) == "ambiguous graph state"


def test_preflight_rejects_an_unclassified_future_user_table(storage) -> None:
    target = "local:new-schema-delete"
    storage.ensure_user(target)
    storage.update_user(target, status="disabled")
    with storage.transaction() as conn:
        conn.execute("CREATE TABLE future_user_material(id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        conn.execute("INSERT INTO future_user_material(id,user_id) VALUES('future-1',?)", (target,))
        conn.execute(
            "CREATE TABLE future_account_material(id TEXT PRIMARY KEY, account_id TEXT REFERENCES users(id))"
        )
        conn.execute("INSERT INTO future_account_material(id,account_id) VALUES('future-2',?)", (target,))
        conn.execute("CREATE TABLE future_owner_material(id TEXT PRIMARY KEY, owner_id TEXT)")
        conn.execute("INSERT INTO future_owner_material(id,owner_id) VALUES('future-3',?)", (target,))

    plan = _verified_plan(storage, target)
    assert plan["ready"] is False
    assert plan["unknown_scopes"] == [
        "future_account_material.account_id",
        "future_owner_material.owner_id",
        "future_user_material.user_id",
    ]
    assert {item["code"] for item in plan["blockers"]} == {"unknown_user_scope"}
    assert storage.get_user(target) is not None


def test_relation_history_is_named_as_an_immutable_blocker(storage) -> None:
    target = "local:relation-history-delete"
    storage.ensure_user(target)
    source = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Source"))
    destination = storage.create_entity(Entity(id=new_id("ent"), user_id=target, name="Destination"))
    storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id=target,
            source_entity_id=source.id,
            target_entity_id=destination.id,
        )
    )
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)
    assert plan["ready"] is False
    assert plan["counts"]["relations"] == 1
    assert plan["counts"]["relation_revisions"] >= 1
    assert {item["code"] for item in plan["blockers"]} == {"relation_history"}


def test_host_action_history_is_named_as_an_immutable_blocker(storage) -> None:
    target = "local:host-action-history-delete"
    storage.ensure_user(target)
    digest = hashlib.sha256(b"host-action-account-deletion-fixture").hexdigest()
    job, created = HostJobStore(storage).create_or_get(
        user_id=target,
        actor_own_id=target,
        conversation_id=None,
        source_message_id=None,
        host_agent_id="local-user-agent",
        capability_id="network.nmap.scan",
        adapter_id="network.nmap",
        adapter_version=1,
        action_id="discover",
        normalized_arguments={"targets": ["192.0.2.0/24"]},
        plan={"schema_version": 1, "plan_digest_input": digest},
        plan_digest=digest,
        risk_class="network_observe",
        authorization_basis="explicit_current_user_request",
        idempotency_key="account-deletion-fixture",
        continuation={"kind": "none"},
    )
    assert created is True
    storage.update_user(target, status="disabled")

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert plan["counts"]["host_action_jobs"] == 1
    assert plan["counts"]["host_action_events"] == 1
    assert {item["code"] for item in plan["blockers"]} == {"host_action_history"}
    assert HostJobStore(storage).get(job["id"], user_id=target, actor_own_id=target) is not None


def test_transactional_service_rechecks_self_owner_and_system_protections(storage) -> None:
    actor = "local:direct-delete-admin"
    storage.ensure_user(actor, preset_key="admin")
    targets = (
        ("local:direct-self", {"actor": "self"}),
        (LEGACY_OWNER_USER_ID, {"preset_key": "user"}),
        ("local:direct-owner-preset", {"preset_key": "owner"}),
        ("local:direct-system", {"source": "system"}),
        ("local:direct-system-meta", {"metadata": {"system_account": True}}),
    )
    for target, options in targets:
        storage.ensure_user(
            target,
            source="local",
            preset_key=str(options.get("preset_key") or "user"),
            metadata=options.get("metadata"),
        )
        assert _mark_account_deletion_history_clean(storage, target)
        if options.get("source"):
            with storage.transaction() as conn:
                conn.execute("UPDATE users SET source=? WHERE id=?", (options["source"], target))
        storage.update_user(target, status="disabled")
        plan = preflight_account_deletion(storage, target, quiescence_available=True)
        assert plan["ready"] is True, (target, plan)
        deleting_actor = target if options.get("actor") == "self" else actor
        with pytest.raises(AccountDeletionConflict):
            delete_account(
                storage,
                target,
                expected_fingerprint=plan["fingerprint"],
                actor_user_id=deleting_actor,
                quiescence_verified=True,
            )
        assert storage.get_user(target) is not None
        assert storage.kv_get(deleted_account_tombstone_key(target)) is None


def test_retained_audit_history_cannot_recreate_account_during_migration(storage) -> None:
    actor = "local:migration-delete-admin"
    target = "local:migration-delete-target"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id=target,
            action="admin.user.update",
            target_type="user",
            target_id=target,
            before_json={"status": "active"},
            after_json={"status": "disabled"},
        )
    )
    storage.update_user(target, status="disabled")
    plan = _verified_plan(storage, target)
    assert plan["ready"] is True, plan
    delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )

    with storage.transaction() as conn:
        storage._migrate_legacy_schema(conn)

    assert storage.get_user(target) is None
    assert storage.kv_get(deleted_account_tombstone_key(target)) is not None


def test_transaction_rejects_an_administrator_revoked_after_preflight(storage) -> None:
    actor = "local:revoked-delete-admin"
    target = "local:revoked-delete-target"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.update_user(target, status="disabled")
    plan = _verified_plan(storage, target)
    assert plan["ready"] is True
    storage.update_user(actor, status="disabled")

    with pytest.raises(AccountDeletionConflict, match="Полномочия администратора"):
        delete_account(
            storage,
            target,
            expected_fingerprint=plan["fingerprint"],
            actor_user_id=actor,
            quiescence_verified=True,
        )

    assert storage.get_user(target) is not None
    assert storage.kv_get(deleted_account_tombstone_key(target)) is None


def test_unsupported_legacy_user_id_is_a_named_preflight_blocker(storage) -> None:
    now = utc_now()
    target = "legacy id with spaces"
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO users(id,source,external_id,display_name,username,preset_key,status,
                   metadata_json,created_at,updated_at,last_seen_at)
               VALUES(?,'legacy','','','','user','disabled','{}',?,?,?)""",
            (target, now, now, now),
        )

    plan = _verified_plan(storage, target)

    assert plan["ready"] is False
    assert {item["code"] for item in plan["blockers"]} == {"unsupported_legacy_id"}
    assert storage.get_user(target) is not None


def test_hard_delete_is_code_owned_disabled_even_if_an_env_escape_is_attempted(
    settings,
    monkeypatch,
) -> None:
    from friday.config import load_settings

    assert settings.account_hard_delete_enabled is False
    monkeypatch.setenv("FRIDAY_ACCOUNT_HARD_DELETE_ENABLED", "1")

    assert load_settings().account_hard_delete_enabled is False


def test_disabled_hard_delete_surface_cannot_mutate_an_account(settings) -> None:
    disabled_settings = settings
    target = "local:quarantined-hard-delete"
    with TestClient(create_app(disabled_settings)) as client:
        storage = client.app.state.storage
        _create_user(client, disabled_settings, target)
        _disable_user(client, disabled_settings, target)

        users = client.get("/api/admin/users", headers=_headers(disabled_settings))
        assert users.status_code == 200
        assert users.json()["hard_delete_enabled"] is False

        preflight = client.get(
            f"/api/admin/users/{target}/deletion",
            headers=_headers(disabled_settings),
        )
        assert preflight.status_code == 503
        refused = client.request(
            "DELETE",
            f"/api/admin/users/{target}",
            headers=_headers(disabled_settings),
            json={"confirmation": target, "fingerprint": "0" * 64},
        )
        assert refused.status_code == 503
        assert storage.get_user(target) is not None
        assert storage.kv_get(deleted_account_tombstone_key(target)) is None


def test_delete_routes_require_management_and_protect_self_owner_and_system(
    hard_delete_settings,
) -> None:
    settings = hard_delete_settings
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.storage
        owner = _headers(settings)

        own = client.get(f"/api/admin/users/{LEGACY_OWNER_USER_ID}/deletion", headers=owner)
        assert own.status_code == 409
        assert "текущего администратора" in own.json()["detail"]

        _create_user(client, settings, "local:owner-preset", preset="owner")
        protected_owner = client.get("/api/admin/users/local:owner-preset/deletion", headers=owner)
        assert protected_owner.status_code == 403

        storage.ensure_user("local:system-account", source="system")
        storage.update_user("local:system-account", status="disabled")
        protected_system = client.get("/api/admin/users/local:system-account/deletion", headers=owner)
        assert protected_system.status_code == 403

        _create_user(client, settings, "local:self-admin", preset="admin")
        admin_secret = "jrc_" + "S" * 43
        storage.create_api_token("local:self-admin", hashlib.sha256(admin_secret.encode()).hexdigest())
        self_response = client.get(
            "/api/admin/users/local:self-admin/deletion",
            headers={"Authorization": f"Bearer {admin_secret}"},
        )
        assert self_response.status_code == 409
        assert "текущего администратора" in self_response.json()["detail"]

        _create_user(client, settings, "local:tooled-target")
        storage.set_permission_override("local:tooled-target", "code.run", "allow")
        _disable_user(client, settings, "local:tooled-target")
        undelegable = client.get(
            "/api/admin/users/local:tooled-target/deletion",
            headers={"Authorization": f"Bearer {admin_secret}"},
        )
        assert undelegable.status_code == 403
        assert storage.get_user("local:tooled-target") is not None

        _create_user(client, settings, "local:ordinary")
        ordinary_secret = "jrc_" + "U" * 43
        storage.create_api_token("local:ordinary", hashlib.sha256(ordinary_secret.encode()).hexdigest())
        denied = client.get(
            "/api/admin/users/local:self-admin/deletion",
            headers={"Authorization": f"Bearer {ordinary_secret}"},
        )
        assert denied.status_code == 403


def test_unknown_and_mutating_cli_commands_require_the_backend_process_lease(settings, monkeypatch) -> None:
    from friday import cli
    from friday.diagnostics import runtime_lease

    attempted: list[str] = []

    class RefusingLease:
        def __init__(self, path, *, protocol: str) -> None:  # noqa: ANN001
            self.path = path
            expected = {
                "account-deletion.lock": "friday.account-deletion.v1",
                "backend.lock": "friday.backend.v1",
            }
            assert protocol == expected[path.name]

        def __enter__(self):
            attempted.append(self.path.name)
            if self.path.name == "backend.lock":
                raise runtime_lease.RuntimeLeaseError("backend already owns the lease")
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
            return None

    monkeypatch.setattr("friday.config.load_settings", lambda: settings)
    monkeypatch.setattr(runtime_lease, "ProcessLease", RefusingLease)
    called: list[str] = []
    for command in ("prune-entities", "future-maintenance-mutator"):
        args = argparse.Namespace(
            command=command,
            handler=lambda _args, value=command: called.append(value),
        )
        with pytest.raises(runtime_lease.RuntimeLeaseError):
            cli._run_cli_handler(args)

    assert attempted == [
        "account-deletion.lock",
        "backend.lock",
        "account-deletion.lock",
        "backend.lock",
    ]
    assert called == []

    export_args = argparse.Namespace(
        command="export-user",
        handler=lambda _args: called.append("export-user"),
    )
    assert cli._run_cli_handler(export_args) == 0
    assert attempted[-1] == "account-deletion.lock"
    assert called == ["export-user"]


def test_admin_ui_requires_typed_id_and_keeps_disable_separate() -> None:
    from friday.admin_ui import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "actions.setUserStatus" in source
    assert "actions.deleteUserDialog" in source
    assert "!state.hardDeleteEnabled" in source
    assert "users.hard_delete_enabled===true" in source
    assert "Безвозвратное удаление временно недоступно" in source
    assert "deleteUserConfirmation" in source
    assert "if(typed!==id)" in source
    assert "confirmation:typed,fingerprint:plan.fingerprint" in source
    assert "Удалить учётную запись" in source
