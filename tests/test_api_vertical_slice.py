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


def _bridge_request(client, settings, path, payload, *, user="1001", chat="5001"):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return client.post(
        path,
        content=body,
        headers=_signed_headers(settings.telegram_bridge_secret, "POST", path, body, user, chat),
    )


def _bridge_get(client, settings, path, *, user="1001", chat="5001"):
    return client.get(
        path,
        headers=_signed_headers(settings.telegram_bridge_secret, "GET", path, b"", user, chat),
    )


def _bridge_json(client, settings, method, path, payload, *, user="1001", chat="5001"):
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

        alice_knowledge = _bridge_get(client, settings, "/api/knowledge", user="1001", chat="5001")
        bob_knowledge = _bridge_get(client, settings, "/api/knowledge", user="1002", chat="5002")
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
        assert "telegram:telegram:1001" in ids
        assert "telegram:telegram:1002" in ids
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
    user_id = "telegram:telegram:1001"
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
            user="7001",
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
            user="7001",
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
            user="7001",
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
            user="7001",
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
            user="7001",
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
