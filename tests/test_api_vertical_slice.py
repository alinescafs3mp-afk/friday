from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.security import sign_bridge_request


def _signed_headers(secret: str, method: str, path: str, body: bytes, user: str, chat: str, *, nonce=None):
    timestamp = int(time.time())
    nonce = nonce or uuid.uuid4().hex
    return {
        "Content-Type": "application/json",
        "X-Jericho-Timestamp": str(timestamp),
        "X-Jericho-User": user,
        "X-Jericho-Chat": chat,
        "X-Jericho-Nonce": nonce,
        "X-Jericho-Signature": sign_bridge_request(
            secret,
            timestamp=timestamp,
            method=method,
            path=path,
            external_user_id=user,
            chat_id=chat,
            nonce=nonce,
            body=body,
        ),
    }


def _bridge_request(client, settings, path, payload, *, user="5001", chat="5001"):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return client.post(
        path,
        content=body,
        headers=_signed_headers(settings.telegram_bridge_secret, "POST", path, body, user, chat),
    )


def _bridge_get(client, settings, path, *, user="5001", chat="5001"):
    return client.get(
        path,
        headers=_signed_headers(settings.telegram_bridge_secret, "GET", path, b"", user, chat),
    )


def _bridge_json(client, settings, method, path, payload, *, user="5001", chat="5001"):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return client.request(
        method,
        path,
        content=body,
        headers=_signed_headers(
            settings.telegram_bridge_secret,
            method,
            path,
            body,
            user,
            chat,
        ),
    )


def test_authenticated_api_and_telegram_vertical_slice(settings):
    from jericho.server import create_app

    app = create_app(settings)
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/me", headers={"Authorization": "Bearer wrong"}).status_code == 401
        owner = client.get("/api/me", headers=owner_headers)
        assert owner.status_code == 200
        assert owner.json()["actor"]["preset_key"] == "owner"
        overview = client.get("/api/admin/overview", headers=owner_headers)
        assert overview.status_code == 200

        first = _bridge_request(
            client,
            settings,
            "/api/chat",
            {
                "message": "Запомни: Project Alpha launches in September.",
                "force_knowledge": True,
                "source_ref": "telegram-update:1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 1001, "first_name": "Alice", "username": "alice"},
            },
        )
        assert first.status_code == 200, first.text
        first_data = first.json()
        assert first_data["ingestion"]["promoted"] is True
        conversation_id = first_data["conversation_id"]

        replay = _bridge_request(
            client,
            settings,
            "/api/chat",
            {
                "message": "Запомни: Project Alpha launches in September.",
                "force_knowledge": True,
                "source_ref": "telegram-update:1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
            },
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["conversation_id"] == conversation_id

        second = _bridge_request(
            client,
            settings,
            "/api/chat",
            {
                "message": "Что известно про Alpha?",
                "source_ref": "telegram-update:2",
                "telegram_message_id": 2,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
            },
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == conversation_id

        alice_knowledge = _bridge_get(client, settings, "/api/knowledge", user="5001", chat="5001")
        bob_knowledge = _bridge_get(client, settings, "/api/knowledge", user="5002", chat="5002")
        assert alice_knowledge.status_code == 200
        assert alice_knowledge.json()["count"] >= 1
        assert bob_knowledge.status_code == 200
        assert bob_knowledge.json()["count"] == 0

        reset = _bridge_request(
            client,
            settings,
            "/api/conversations/channel/reset",
            {"channel": "telegram", "channel_id": "5001", "telegram_user": {"id": 1001}},
        )
        assert reset.status_code == 200
        third = _bridge_request(
            client,
            settings,
            "/api/chat",
            {
                "message": "Новый разговор",
                "source_ref": "telegram-update:3",
                "telegram_message_id": 3,
                "telegram_user": {"id": 1001},
            },
        )
        assert third.status_code == 200
        assert third.json()["conversation_id"] != conversation_id

        body = b"{}"
        bad_headers = _signed_headers(
            settings.telegram_bridge_secret,
            "POST",
            "/api/chat",
            body,
            "1001",
            "5001",
        )
        bad_headers["X-Jericho-Signature"] = "0" * 64
        assert client.post("/api/chat", content=body, headers=bad_headers).status_code == 401

        users = client.get("/api/admin/users", headers=owner_headers)
        ids = {user["id"] for user in users.json()["items"]}
        assert "telegram:telegram:5001" in ids
        assert "telegram:telegram:5002" in ids
        audit = client.get("/api/admin/audit", headers=owner_headers)
        assert audit.status_code == 200
        assert "Content-Security-Policy" in client.get("/admin/").headers


def test_bridge_denies_chat_outside_allowlist(settings):
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        # An allowed chat authenticates.
        allowed = _bridge_get(client, scoped, "/api/me", user="1001", chat="5001")
        assert allowed.status_code == 200
        # A chat outside the effective allowlist is denied with 403 (so the bridge
        # dead-letters instead of retrying) and never registers a user.
        denied = _bridge_get(client, scoped, "/api/me", user="9999", chat="9999")
        assert denied.status_code == 403
        users = client.get("/api/admin/users", headers={"Authorization": f"Bearer {scoped.api_token}"})
        ids = {user["id"] for user in users.json()["items"]}
        assert "telegram:telegram:9999" not in ids


def test_bridge_owner_chat_ids_are_allowed(settings):
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[], telegram_owner_chat_ids=[5001])
    with TestClient(create_app(scoped)) as client:
        assert _bridge_get(client, scoped, "/api/me", user="1001", chat="5001").status_code == 200
        assert _bridge_get(client, scoped, "/api/me", user="1002", chat="6002").status_code == 403


def test_bridge_nonce_replay_is_rejected(settings):
    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        body = json.dumps(
            {"telegram_user": {"id": 5001}}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        headers = _signed_headers(settings.telegram_bridge_secret, "GET", "/api/me", body, "5001", "5001")
        first = client.request("GET", "/api/me", content=body, headers=headers)
        assert first.status_code == 200
        # Replaying the exact same signed request (identical nonce) is rejected.
        replay = client.request("GET", "/api/me", content=body, headers=headers)
        assert replay.status_code == 403


def test_bridge_request_without_nonce_is_rejected(settings):
    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        body = b"{}"
        headers = _signed_headers(settings.telegram_bridge_secret, "GET", "/api/me", body, "5001", "5001")
        del headers["X-Jericho-Nonce"]
        assert client.request("GET", "/api/me", content=body, headers=headers).status_code == 401


def test_rate_limit_from_authentication_middleware_returns_429(settings):
    from jericho.server import create_app

    limited_settings = replace(settings, api_user_rate_limit_per_minute=1)
    app = create_app(limited_settings)
    owner_headers = {"Authorization": f"Bearer {limited_settings.api_token}"}
    with TestClient(app) as client:
        assert client.get("/api/me", headers=owner_headers).status_code == 200
        limited = client.get("/api/me", headers=owner_headers)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "API rate limit exceeded"}
        assert limited.headers["X-Content-Type-Options"] == "nosniff"


def test_admin_delegation_cannot_escalate_to_owner(settings):
    from jericho.server import create_app

    app = create_app(settings)
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    admin_user_id = "telegram:telegram:9001"
    with TestClient(app) as client:
        created = client.post(
            "/api/admin/users",
            json={
                "id": admin_user_id,
                "display_name": "Delegated administrator",
                "preset_key": "admin",
            },
            headers=owner_headers,
        )
        assert created.status_code == 200, created.text

        owner_preset = _bridge_json(
            client,
            settings,
            "POST",
            f"/api/admin/users/{admin_user_id}/preset",
            {"preset_key": "owner"},
            user="9001",
            chat="9001",
        )
        assert owner_preset.status_code == 403

        owner_mutation = _bridge_json(
            client,
            settings,
            "PATCH",
            f"/api/admin/users/{LEGACY_OWNER_USER_ID}",
            {"status": "disabled"},
            user="9001",
            chat="9001",
        )
        assert owner_mutation.status_code == 403

        code_override = _bridge_json(
            client,
            settings,
            "PUT",
            f"/api/admin/users/{admin_user_id}/permissions/code.run",
            {"effect": "allow"},
            user="9001",
            chat="9001",
        )
        assert code_override.status_code == 403

        unsafe_preset = _bridge_json(
            client,
            settings,
            "POST",
            "/api/admin/presets",
            {
                "preset_key": "executor",
                "name": "Executor",
                "capabilities": ["knowledge.read", "code.run"],
            },
            user="9001",
            chat="9001",
        )
        assert unsafe_preset.status_code == 403

        safe_preset = _bridge_json(
            client,
            settings,
            "POST",
            "/api/admin/presets",
            {
                "preset_key": "knowledge_curator",
                "name": "Knowledge curator",
                "capabilities": ["knowledge.read", "knowledge.edit", "kg.write"],
            },
            user="9001",
            chat="9001",
        )
        assert safe_preset.status_code == 200, safe_preset.text

        owner_grant = client.put(
            f"/api/admin/users/{admin_user_id}/permissions/code.run",
            json={"effect": "allow"},
            headers=owner_headers,
        )
        assert owner_grant.status_code == 200, owner_grant.text

        still_not_owner = _bridge_json(
            client,
            settings,
            "POST",
            f"/api/admin/users/{admin_user_id}/preset",
            {"preset_key": "owner"},
            user="9001",
            chat="9001",
        )
        assert still_not_owner.status_code == 403


def test_rate_limit_from_authentication_middleware_is_http_429(settings):
    from dataclasses import replace

    from jericho.server import create_app

    app = create_app(replace(settings, api_user_rate_limit_per_minute=1))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        assert client.get("/api/me", headers=headers).status_code == 200
        limited = client.get("/api/me", headers=headers)
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        assert "rate limit" in limited.json()["detail"].casefold()


def test_document_chat_replay_is_idempotent_before_file_side_effects(settings):
    import base64

    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Сохрани приложенный документ",
        "force_knowledge": True,
        "document": {
            "filename": "note.txt",
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(b"one durable file").decode("ascii"),
            "source_ref": "api-document:one",
        },
    }
    with TestClient(app) as client:
        first = client.post("/api/chat", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        files_after_first = client.get("/api/files", headers=headers).json()["items"]
        assert len(files_after_first) == 1

        replay = client.post("/api/chat", json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        files_after_replay = client.get("/api/files", headers=headers).json()["items"]
        assert len(files_after_replay) == 1


def test_document_only_chat_does_not_promote_its_synthetic_acknowledgement(settings):
    import base64

    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "document": {
            "filename": "architecture.txt",
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(
                b"Project Orion uses PostgreSQL 16 for durable storage."
            ).decode("ascii"),
            "source_ref": "api-document:without-caption",
        }
    }
    with TestClient(app) as client:
        response = client.post("/api/chat", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ingestion"] == {
            "promoted": False,
            "queued_for_review": False,
            "action": "transient",
            "category": "system_notice",
            "reason": "synthetic document acknowledgement; file ingestion handled separately",
            "synthetic": True,
        }
        assert body["file_ingestion"]["promoted"] is True
        raw_rows = app.state.storage.execute(
            "SELECT content_type, source_ref FROM raw_objects ORDER BY received_at"
        ).fetchall()
        assert [(row["content_type"], row["source_ref"]) for row in raw_rows] == [
            ("file", "api-document:without-caption")
        ]
        inbox_count = app.state.storage.execute("SELECT COUNT(*) AS count FROM inbox").fetchone()
        assert int(inbox_count["count"]) == 1


def test_maturity_workflows_are_reachable_through_signed_and_admin_apis(settings):
    from jericho.server import create_app

    app = create_app(settings)
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    user_id = "telegram:telegram:5001"
    with TestClient(app) as client:
        mode = _bridge_json(
            client,
            settings,
            "POST",
            "/api/conversations/channel/mode",
            {
                "channel": "telegram",
                "channel_id": "5001",
                "mode": "research",
                "telegram_user": {"id": 1001, "first_name": "Alice"},
            },
        )
        assert mode.status_code == 200, mode.text
        assert mode.json()["mode"] == "research"

        channel_status = _bridge_get(client, settings, "/api/kg/stats")
        assert channel_status.status_code == 200, channel_status.text
        assert channel_status.json()["interaction_mode"] == "research"

        invalid_mode = _bridge_json(
            client,
            settings,
            "POST",
            "/api/conversations/channel/mode",
            {"channel": "telegram", "channel_id": "5001", "mode": "unsafe-autonomy"},
        )
        assert invalid_mode.status_code == 400

        research_answer = _bridge_request(
            client,
            settings,
            "/api/chat",
            {
                "message": "Исследуй варианты обновления PostgreSQL 16 и отдели факты от рекомендаций.",
                "source_ref": "telegram-update:research-1",
                "telegram_message_id": 101,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
            },
        )
        assert research_answer.status_code == 200, research_answer.text
        answer = research_answer.json()
        assert answer["context"]["interaction_mode"] == "research"
        assert answer["message_id"]

        candidate = _bridge_json(
            client,
            settings,
            "POST",
            "/api/research/candidates",
            {"message_id": answer["message_id"]},
        )
        assert candidate.status_code == 200, candidate.text
        assert candidate.json()["queued_for_review"] is True
        assert candidate.json()["promoted"] is False
        replay = _bridge_json(
            client,
            settings,
            "POST",
            "/api/research/candidates",
            {"message_id": answer["message_id"]},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        feedback = _bridge_json(
            client,
            settings,
            "POST",
            "/api/feedback",
            {
                "target_type": "answer",
                "target_id": answer["message_id"],
                "feedback_type": "answer_usefulness",
                "score": -1,
                "context": {"channel": "telegram"},
            },
        )
        assert feedback.status_code == 200, feedback.text

        relation_ingest = _bridge_json(
            client,
            settings,
            "POST",
            "/api/ingest",
            {
                "content": "Запомни: проект Orion использует PostgreSQL 16.",
                "source_ref": "api:relation:orion",
                "force_knowledge": True,
            },
        )
        assert relation_ingest.status_code == 200
        relation_candidates = client.get(
            f"/api/admin/relation-candidates?user_id={user_id}&status=suggested",
            headers=owner_headers,
        )
        assert relation_candidates.status_code == 200
        relation_items = relation_candidates.json()["items"]
        assert relation_items and relation_items[0]["relation_type"] == "uses"
        reviewed_relation = client.post(
            f"/api/admin/relation-candidates/{relation_items[0]['id']}/review",
            json={"user_id": user_id, "status": "accepted"},
            headers=owner_headers,
        )
        assert reviewed_relation.status_code == 200
        assert reviewed_relation.json()["item"]["status"] == "accepted"

        for suffix, ip in (("old", "10.0.0.5"), ("new", "10.0.0.7")):
            response = _bridge_json(
                client,
                settings,
                "POST",
                "/api/ingest",
                {
                    "content": f"Запомни: сервер Atlas имеет IP {ip}.",
                    "source_ref": f"api:conflict:{suffix}",
                    "force_knowledge": True,
                },
            )
            assert response.status_code == 200
        conflicts = client.get(
            f"/api/admin/conflicts?user_id={user_id}&status=suggested",
            headers=owner_headers,
        )
        assert conflicts.status_code == 200
        conflict_items = conflicts.json()["items"]
        assert conflict_items and conflict_items[0]["conflict_type"] == "address_mismatch"
        reviewed_conflict = client.post(
            f"/api/admin/conflicts/{conflict_items[0]['id']}/review",
            json={"user_id": user_id, "status": "confirmed", "resolution_note": "reviewed"},
            headers=owner_headers,
        )
        assert reviewed_conflict.status_code == 200
        assert reviewed_conflict.json()["item"]["status"] == "confirmed"

        quality = client.get(f"/api/admin/quality?user_id={user_id}", headers=owner_headers)
        assert quality.status_code == 200, quality.text
        quality_data = quality.json()
        assert quality_data["feedback"]["current_count"] >= 1
        assert quality_data["review_pressure"]["pending_inbox"] >= 1
        assert "pending_relation_candidates" in quality_data["graph"]

        inbox_id = candidate.json()["inbox_id"]
        bulk = client.post(
            "/api/admin/inbox/bulk",
            json={
                "user_id": user_id,
                "inbox_ids": [inbox_id],
                "status": "ignored",
                "promote": False,
            },
            headers=owner_headers,
        )
        assert bulk.status_code == 200, bulk.text
        assert len(bulk.json()["changed"]) == 1


def test_knowledge_work_result_can_only_enter_memory_through_inbox(settings):
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        mode = _bridge_json(
            client,
            settings,
            "POST",
            "/api/conversations/channel/mode",
            {
                "channel": "telegram",
                "channel_id": "7002",
                "mode": "knowledge_work",
                "telegram_user": {"id": 7001, "first_name": "Worker"},
            },
            user="7002",
            chat="7002",
        )
        assert mode.status_code == 200

        answer = _bridge_request(
            client,
            settings,
            "/api/chat",
            {
                "message": "Собери структурированную карточку проекта Orion.",
                "source_ref": "telegram-update:work-1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 7001, "first_name": "Worker"},
            },
            user="7002",
            chat="7002",
        )
        assert answer.status_code == 200, answer.text
        payload = answer.json()
        assert payload["context"]["interaction_mode"] == "knowledge_work"
        assert payload["context"]["can_queue_to_inbox"] is True

        queued = _bridge_json(
            client,
            settings,
            "POST",
            "/api/assistant/candidates",
            {"message_id": payload["message_id"]},
            user="7002",
            chat="7002",
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["queued_for_review"] is True
        assert queued.json()["promoted"] is False

        replay = _bridge_json(
            client,
            settings,
            "POST",
            "/api/assistant/candidates",
            {"message_id": payload["message_id"]},
            user="7002",
            chat="7002",
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        wrong_endpoint = _bridge_json(
            client,
            settings,
            "POST",
            "/api/research/candidates",
            {"message_id": payload["message_id"]},
            user="7002",
            chat="7002",
        )
        assert wrong_endpoint.status_code == 409

        raw_row = client.app.state.storage.execute(
            "SELECT user_id FROM raw_objects WHERE id=?",
            (queued.json()["raw_object_id"],),
        ).fetchone()
        assert raw_row is not None
        user_id = str(raw_row["user_id"])
        assert client.app.state.storage.count_knowledge_objects(user_id) == 0
        work_item = client.app.state.storage.find_inbox_by_raw(
            queued.json()["raw_object_id"],
            user_id,
        )
        assert work_item and work_item["id"] == queued.json()["inbox_id"]
        assert work_item["status"] == "pending"


def test_admin_bulk_graph_review_is_bounded_and_reports_partial_failures(settings):
    from jericho.server import create_app
    from jericho.storage.models import EntityType

    app = create_app(settings)
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        storage = client.app.state.storage
        graph = client.app.state.kg
        storage.ensure_user("graph-admin-user")
        source = graph.create_entity("graph-admin-user", "Orion", EntityType.PROJECT)
        target_a = graph.create_entity("graph-admin-user", "PostgreSQL", EntityType.CONCEPT)
        target_b = graph.create_entity("graph-admin-user", "Redis", EntityType.CONCEPT)
        first = storage.store_relation_candidate(
            "graph-admin-user",
            source["id"],
            target_a["id"],
            "uses",
            confidence=0.76,
            evidence={"source": "test"},
        )
        second = storage.store_relation_candidate(
            "graph-admin-user",
            source["id"],
            target_b["id"],
            "uses",
            confidence=0.68,
            evidence={"source": "test"},
        )

        response = client.post(
            "/api/admin/relation-candidates/bulk-review",
            json={
                "user_id": "graph-admin-user",
                "candidate_ids": [first["id"], second["id"], "missing"],
                "status": "accepted",
            },
            headers=owner_headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["changed_count"] == 2
        assert payload["skipped"] == [{"id": "missing", "reason": "not_found"}]
        assert len(storage.get_entity_relations(source["id"], "graph-admin-user")) == 2

        terminal = client.post(
            "/api/admin/relation-candidates/bulk-review",
            json={
                "user_id": "graph-admin-user",
                "candidate_ids": [first["id"]],
                "status": "rejected",
            },
            headers=owner_headers,
        )
        assert terminal.status_code == 200
        assert terminal.json()["changed_count"] == 0
        assert "already accepted" in terminal.json()["skipped"][0]["reason"]

        oversized = client.post(
            "/api/admin/relation-candidates/bulk-review",
            json={
                "user_id": "graph-admin-user",
                "candidate_ids": [f"candidate-{index}" for index in range(201)],
                "status": "rejected",
            },
            headers=owner_headers,
        )
        assert oversized.status_code == 400


def test_group_chat_members_are_provisioned_with_least_privilege(settings):
    """Allowlisting a GROUP chat used to hand every participant the full 'user' preset.

    Tenant isolation keeps the owner's knowledge private, so the exposure was never
    exfiltration — it was spending the owner's resources: web search and fetch, file
    upload and background missions. A new account in a non-private chat is now 'guest'
    (read and chat only), which keeps the chat working instead of locking anyone out.
    """
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001, 9001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        owner = {"Authorization": f"Bearer {scoped.api_token}"}
        # A private chat: in Telegram its id equals the sender's.
        assert _bridge_get(client, scoped, "/api/me", user="5001", chat="5001").status_code == 200
        # A group: the chat id is not the sender's.
        assert _bridge_get(client, scoped, "/api/me", user="1234", chat="9001").status_code == 200

        presets = {
            row["id"]: row["preset_key"]
            for row in client.get("/api/admin/users", headers=owner).json()["items"]
        }
        assert presets["telegram:telegram:5001"] == "user"
        assert presets["telegram:telegram:1234"] == "guest"

    # The opt-out restores the previous behaviour for operators who want it.
    opened = replace(scoped, telegram_group_members_full_access=True)
    with TestClient(create_app(opened)) as client:
        assert _bridge_get(client, opened, "/api/me", user="4321", chat="9001").status_code == 200
        owner = {"Authorization": f"Bearer {opened.api_token}"}
        presets = {
            row["id"]: row["preset_key"]
            for row in client.get("/api/admin/users", headers=owner).json()["items"]
        }
        assert presets["telegram:telegram:4321"] == "user"


def test_an_existing_account_is_never_downgraded_by_a_group_message(settings):
    """ensure_user does not rewrite preset_key, so writing in a group must not strip
    an established member (or the owner) of their capabilities."""
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001, 9001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        owner = {"Authorization": f"Bearer {scoped.api_token}"}
        assert _bridge_get(client, scoped, "/api/me", user="5001", chat="5001").status_code == 200
        # The same person now writes in a group chat.
        assert _bridge_get(client, scoped, "/api/me", user="5001", chat="9001").status_code == 200
        presets = {
            row["id"]: row["preset_key"]
            for row in client.get("/api/admin/users", headers=owner).json()["items"]
        }
        assert presets["telegram:telegram:5001"] == "user"


def test_open_registration_off_still_denies_a_stranger(settings):
    """The feature defaults off: an unlisted private chat gets nothing, exactly
    as before it existed. This is the safety net for every install that never
    touches JERICHO_TELEGRAM_OPEN_REGISTRATION."""
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    assert scoped.telegram_open_registration is False
    with TestClient(create_app(scoped)) as client:
        response = _bridge_get(client, scoped, "/api/me", user="7777", chat="7777")
        assert response.status_code == 403


def test_open_registration_provisions_a_stranger_with_the_newcomer_preset(settings):
    """A private chat outside the allowlist is admitted only when the flag is on,
    and gets 'newcomer' -- not the full 'user' preset a statically allowlisted
    private chat gets. Mutation this test must catch: dropping the
    `chat_is_allowlisted` distinction in server.py would hand 'user' here too.
    """
    from jericho.server import NEWCOMER_PRESET_CAPABILITIES, create_app

    scoped = replace(
        settings,
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[],
        telegram_open_registration=True,
    )
    app = create_app(scoped)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {scoped.api_token}"}
        # Statically allowlisted private chat: unaffected, still 'user'.
        assert _bridge_get(client, scoped, "/api/me", user="5001", chat="5001").status_code == 200
        # A stranger's private chat: admitted, but narrower.
        response = _bridge_get(client, scoped, "/api/me", user="7777", chat="7777")
        assert response.status_code == 200

        presets = {
            row["id"]: row["preset_key"]
            for row in client.get("/api/admin/users", headers=owner).json()["items"]
        }
        assert presets["telegram:telegram:5001"] == "user"
        assert presets["telegram:telegram:7777"] == "newcomer"

        # Ни миссий, ни выполнения кода, ни админки — проверяется ВЫДАННОЕ, то
        # есть то, что реально лежит в базе под ключом «newcomer», а не значение
        # модульной константы. Прежняя редакция утверждала про саму константу, и
        # потому оставалась зелёной, даже если под тем же ключом в базу записан
        # набор с `admin.*` и `code.run`: ассерты о константе к выданным правам
        # отношения не имеют.
        granted = set((app.state.storage.get_custom_preset("newcomer") or {}).get("capabilities") or [])
        assert granted, "пресет «newcomer» не записан в базу вовсе"
        assert "missions.create" not in granted
        assert "code.run" not in granted
        assert not any(item.startswith("admin.") for item in granted)
        assert "web.search" in granted
        assert "files.upload" in granted
        assert granted == set(NEWCOMER_PRESET_CAPABILITIES), (
            "выданный набор разошёлся с константой — значит константа больше ничем не управляет"
        )


def test_open_registration_does_not_widen_a_group_chat(settings):
    """Open registration is about strangers writing in PRIVATE, not about groups.
    An unlisted group must still be silently refused -- the feature must not
    become a second, wider allowlist by accident."""
    from jericho.server import create_app

    scoped = replace(
        settings,
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[],
        telegram_open_registration=True,
    )
    with TestClient(create_app(scoped)) as client:
        # chat != user: this is a group, per the same signal the rest of the
        # bridge uses to tell private from group chats.
        response = _bridge_get(client, scoped, "/api/me", user="1234", chat="9001")
        assert response.status_code == 403


def test_open_registration_never_downgrades_a_returning_newcomer(settings):
    """A returning self-registered account must keep its preset on the second
    message, not be silently re-evaluated or upgraded/downgraded."""
    from jericho.server import create_app

    scoped = replace(
        settings,
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[],
        telegram_open_registration=True,
    )
    with TestClient(create_app(scoped)) as client:
        owner = {"Authorization": f"Bearer {scoped.api_token}"}
        assert _bridge_get(client, scoped, "/api/me", user="7777", chat="7777").status_code == 200
        assert _bridge_get(client, scoped, "/api/me", user="7777", chat="7777").status_code == 200
        presets = {
            row["id"]: row["preset_key"]
            for row in client.get("/api/admin/users", headers=owner).json()["items"]
        }
        assert presets["telegram:telegram:7777"] == "newcomer"


def test_open_registration_notifies_owner_about_a_new_newcomer(settings):
    """Self-registration must not be silent: the owner chat gets one outbound
    notification naming the newcomer. Mutation this must catch: drop the
    enqueue in server.py after ensure_user — this assertion goes red.
    """
    from jericho.server import create_app

    owner_chat = 5001
    scoped = replace(
        settings,
        telegram_allowed_chat_ids=[owner_chat],
        telegram_owner_chat_ids=[owner_chat],
        telegram_open_registration=True,
    )
    with TestClient(create_app(scoped)) as client:
        storage = client.app.state.storage
        response = _bridge_json(
            client,
            scoped,
            "GET",
            "/api/me",
            {
                "telegram_user": {
                    "id": 7777,
                    "first_name": "New",
                    "last_name": "Comer",
                    "username": "newcomer_bot",
                }
            },
            user="7777",
            chat="7777",
        )
        assert response.status_code == 200, response.text

        rows = storage.execute(
            "SELECT chat_id, body, kind, dedup_key FROM outbound_notifications WHERE kind='onboarding'"
        ).fetchall()
        assert len(rows) == 1, rows
        row = dict(rows[0])
        assert str(row["chat_id"]) == str(owner_chat)
        assert row["dedup_key"] == f"onboarding:telegram:telegram:7777:{owner_chat}"
        assert "самозарегистрировался" in row["body"]
        assert "New Comer" in row["body"]
        assert "newcomer_bot" in row["body"]
        # Message content must never leak into the owner push.
        assert "/api/me" not in row["body"]

        # Returning newcomer: still one row (dedup + existing-is-not-None).
        assert (
            _bridge_json(
                client,
                scoped,
                "GET",
                "/api/me",
                {
                    "telegram_user": {
                        "id": 7777,
                        "first_name": "New",
                        "last_name": "Comer",
                        "username": "newcomer_bot",
                    }
                },
                user="7777",
                chat="7777",
            ).status_code
            == 200
        )
        again = storage.execute(
            "SELECT COUNT(*) AS n FROM outbound_notifications WHERE kind='onboarding'"
        ).fetchone()
        assert int(again["n"]) == 1


def test_custom_instructions_are_self_service_and_reflected_in_me(settings):
    """PATCH /api/me/instructions writes into the actor's OWN metadata_json --
    there is no user_id parameter to take, so a cross-tenant write is
    structurally impossible, not merely gated. GET /api/me shows what was set,
    and the endpoint is reachable by every preset that has chat.use (not just
    'user'), because it is a personal preference, not an admin action."""
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        # A private chat, statically allowlisted, gets 'user' -- has chat.use.
        set_response = _bridge_json(
            client,
            scoped,
            "PATCH",
            "/api/me/instructions",
            {"instructions": "  отвечай  коротко  "},
            user="5001",
            chat="5001",
        )
        assert set_response.status_code == 200
        # Collapsed whitespace -- the same normalization the backend applies
        # before storing, not the raw string echoed back.
        assert set_response.json()["custom_instructions"] == "отвечай коротко"

        me = _bridge_get(client, scoped, "/api/me", user="5001", chat="5001")
        import json as _json

        metadata = _json.loads(me.json()["user"]["metadata_json"])
        assert metadata["custom_instructions"] == "отвечай коротко"

        # Setting empty text clears it rather than storing an empty string.
        clear_response = _bridge_json(
            client, scoped, "PATCH", "/api/me/instructions", {"instructions": ""}, user="5001", chat="5001"
        )
        assert clear_response.json()["custom_instructions"] == ""
        me_after = _bridge_get(client, scoped, "/api/me", user="5001", chat="5001")
        metadata_after = _json.loads(me_after.json()["user"]["metadata_json"])
        assert "custom_instructions" not in metadata_after


def test_custom_instructions_are_capped(settings):
    """500 chars, matching the project's convention for short free-text fields
    (reason[:500] appears throughout the codebase) -- long enough for a real
    preference, short enough that it cannot become a second system prompt."""
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        long_text = "x" * 900
        response = _bridge_json(
            client,
            scoped,
            "PATCH",
            "/api/me/instructions",
            {"instructions": long_text},
            user="5001",
            chat="5001",
        )
        assert len(response.json()["custom_instructions"]) == 500


def test_narrowing_the_newcomer_preset_in_code_reaches_a_database_that_already_has_it(settings):
    """Константа обязана управлять правами новичка и ПОСЛЕ первой саморегистрации.

    Пресет писался в базу один раз, под охраной `if not preset_exists(...)`, и
    дальше жил своей жизнью: сузить права новичка правкой кода было нельзя — в
    боевой базе остался бы прежний набор. Это единственный пресет, который
    выдаётся человеку с улицы автоматически, поэтому цена расхождения прямая.

    Мутация: вернуть охрану `if not auth_service.preset_exists("newcomer")` —
    тест обязан покраснеть.
    """
    from jericho.server import create_app

    scoped = replace(
        settings,
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[],
        telegram_open_registration=True,
    )
    app = create_app(scoped)
    with TestClient(app) as client:
        assert _bridge_get(client, scoped, "/api/me", user="7777", chat="7777").status_code == 200
        storage = app.state.storage

        # База «из прошлого»: под тем же ключом лежит набор шире нынешней константы.
        storage.upsert_custom_preset(
            "newcomer",
            "Новичок (авторегистрация)",
            {"chat.use", "knowledge.read", "code.run"},
            description="устаревший набор",
            created_by="system",
        )
        assert "code.run" in set(storage.get_custom_preset("newcomer")["capabilities"])

        # Следующий новичок — и набор снова тот, что объявлен в коде.
        assert _bridge_get(client, scoped, "/api/me", user="7778", chat="7778").status_code == 200
        granted = set(storage.get_custom_preset("newcomer")["capabilities"])
        assert "code.run" not in granted, "правка константы не доехала до существующей базы"

        # И расхождение не проходит молча: оно записано.
        events = storage.list_events(event_type="presets.newcomer_synced", limit=5)
        assert events, "набор заменён без следа — оператор не узнает, что правку затёрли"
