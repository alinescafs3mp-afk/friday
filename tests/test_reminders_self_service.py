"""G19: list and dismiss pending reminders (self-service).

Reminders are auto-queued from entity_time by the reminders organ into
outbound_notifications (kind=reminder, dedup_key=reminder:{entity}:{date}).
Dismiss must set status='dismissed' WITHOUT clearing dedup_key — otherwise
the next scan_reminders re-inserts the same push via INSERT OR IGNORE.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jericho.knowledge_graph import KnowledgeGraph
from jericho.organs import ServiceContext
from jericho.organs.reminders import scan_reminders
from jericho.server import create_app
from jericho.storage.models import EntityType
from tests.test_api_vertical_slice import _bridge_get, _bridge_json
from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient, _media_bridge


def _today_iso(offset_days: int = 0) -> str:
    return (datetime.now(UTC).date() + timedelta(days=offset_days)).isoformat()


def _reminders_settings(settings):
    return replace(
        settings,
        reminders_enabled=True,
        reminders_lead_days=2,
        quiet_hours_start=0,
        quiet_hours_end=0,
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[],
    )


def test_dismiss_keeps_dedup_key_so_rescan_does_not_requeue(settings):
    """Mutation: clear dedup_key on dismiss (copy discard_notifications) → red.

    After dismiss, enqueue_notification with the same dedup_key must return
    False; list_pending_notifications must not include the dismissed row.
    """
    tuned = _reminders_settings(settings)
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        user_id = "telegram:telegram:5001"
        storage.ensure_user(user_id, source="telegram", metadata={"chat_id": "5001"})
        dedup = f"reminder:entity_x:{_today_iso(0)}"
        assert storage.enqueue_notification(
            user_id,
            "5001",
            "🔔 Напоминание: «X» — сегодня.",
            kind="reminder",
            dedup_key=dedup,
        )
        rows = storage.list_pending_reminders(user_id)
        assert len(rows) == 1
        notif_id = rows[0]["id"]

        response = _bridge_json(
            client,
            tuned,
            "POST",
            f"/api/me/reminders/{notif_id}/dismiss",
            {},
            user="5001",
            chat="5001",
        )
        assert response.status_code == 200, response.text
        assert response.json()["dismissed"] is True

        row = storage.execute(
            "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
            (notif_id,),
        ).fetchone()
        assert row["status"] == "dismissed"
        # Core invariant of G19: key stays, partial unique index keeps blocking.
        assert row["dedup_key"] == dedup

        assert storage.list_pending_reminders(user_id) == []
        assert notif_id not in {n["id"] for n in storage.list_pending_notifications(limit=100)}

        # Simulate the next organ scan: same dedup_key must NOT re-queue.
        assert (
            storage.enqueue_notification(
                user_id,
                "5001",
                "🔔 Напоминание: «X» — сегодня.",
                kind="reminder",
                dedup_key=dedup,
            )
            is False
        )
        assert storage.list_pending_reminders(user_id) == []


@pytest.mark.asyncio
async def test_dismiss_blocks_scan_reminders_rescan(settings):
    """End-to-end: scan → dismiss → scan again does not re-enqueue."""
    tuned = _reminders_settings(settings)
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        user_id = "telegram:telegram:5001"
        storage.ensure_user(user_id, source="telegram", metadata={"chat_id": "5001"})
        graph = KnowledgeGraph(storage)
        event = graph.create_entity(user_id, "Запуск Orion", EntityType.EVENT)
        graph.set_event_time(user_id, event["id"], _today_iso(0))
        ctx = ServiceContext(settings=tuned, storage=storage, kg=graph, ingestion=None)
        await scan_reminders(ctx)
        pending = storage.list_pending_reminders(user_id)
        assert len(pending) == 1
        notif_id = pending[0]["id"]
        dedup = pending[0]["dedup_key"]

        assert storage.dismiss_notification(user_id, notif_id) is True
        await scan_reminders(ctx)
        assert storage.list_pending_reminders(user_id) == []
        row = storage.execute(
            "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
            (notif_id,),
        ).fetchone()
        assert row["status"] == "dismissed"
        assert row["dedup_key"] == dedup


def test_list_and_dismiss_are_tenant_scoped(settings):
    """Foreign notification_id → 404 (not 403). Own list never shows foreign rows."""
    tuned = _reminders_settings(settings)
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        alice = "telegram:telegram:5001"
        bob = "telegram:telegram:5002"
        storage.ensure_user(alice, source="telegram", metadata={"chat_id": "5001"})
        storage.ensure_user(bob, source="telegram", metadata={"chat_id": "5002"})
        assert storage.enqueue_notification(
            alice, "5001", "alice reminder", kind="reminder", dedup_key="rem:a"
        )
        assert storage.enqueue_notification(bob, "5002", "bob reminder", kind="reminder", dedup_key="rem:b")
        bob_id = storage.list_pending_reminders(bob)[0]["id"]

        response = _bridge_get(client, tuned, "/api/me/reminders", user="5001", chat="5001")
        assert response.status_code == 200, response.text
        bodies = [item["body"] for item in response.json()["items"]]
        assert bodies == ["alice reminder"]
        assert response.json()["count"] == 1

        foreign = _bridge_json(
            client,
            tuned,
            "POST",
            f"/api/me/reminders/{bob_id}/dismiss",
            {},
            user="5001",
            chat="5001",
        )
        assert foreign.status_code == 404, foreign.text
        assert storage.list_pending_reminders(bob)[0]["id"] == bob_id


def test_dismiss_notification_rejects_non_reminder_kind(settings):
    """G21: dismiss is kind-scoped like list_pending_reminders.

    Mutation: drop ``AND kind='reminder'`` from dismiss SQL → this test reds
    because a pending chronicle row of the same tenant would flip to dismissed
    and permanently block its dedup_key.
    """
    tuned = _reminders_settings(settings)
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        user_id = "telegram:telegram:5001"
        storage.ensure_user(user_id, source="telegram", metadata={"chat_id": "5001"})
        assert storage.enqueue_notification(
            user_id,
            "5001",
            "chronicle body",
            kind="chronicle",
            dedup_key="chronicle:day:1",
        )
        assert storage.enqueue_notification(
            user_id,
            "5001",
            "reminder body",
            kind="reminder",
            dedup_key="rem:own",
        )
        chronicle = storage.execute(
            "SELECT id, status, kind, dedup_key FROM outbound_notifications "
            "WHERE user_id=? AND kind='chronicle'",
            (user_id,),
        ).fetchone()
        assert chronicle is not None
        chronicle_id = chronicle["id"]
        reminder_id = storage.list_pending_reminders(user_id)[0]["id"]

        assert storage.dismiss_notification(user_id, chronicle_id) is False
        still = storage.execute(
            "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
            (chronicle_id,),
        ).fetchone()
        assert still["status"] == "pending"
        assert still["dedup_key"] == "chronicle:day:1"

        # HTTP surface mirrors storage: non-reminder id → 404, row untouched.
        http = _bridge_json(
            client,
            tuned,
            "POST",
            f"/api/me/reminders/{chronicle_id}/dismiss",
            {},
            user="5001",
            chat="5001",
        )
        assert http.status_code == 404, http.text
        after_http = storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (chronicle_id,),
        ).fetchone()
        assert after_http["status"] == "pending"

        # Same-tenant reminder still dismisses; kind filter is not a blanket deny.
        assert storage.dismiss_notification(user_id, reminder_id) is True
        rem = storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (reminder_id,),
        ).fetchone()
        assert rem["status"] == "dismissed"


@pytest.mark.asyncio
async def test_reminders_command_lists_and_dismiss_callback_hits_api(tmp_path):
    """Mutation: drop /reminders branch or remind:dismiss handler → red."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    notif_id = "notif_abc123def45678"
    backend = _FakeBackendClient(
        {
            "/api/me": {"actor": {"preset_key": "user"}, "user": {}},
            "/api/me/reminders?limit=10": {
                "count": 1,
                "items": [{"id": notif_id, "body": "🔔 Напоминание: «X» — сегодня."}],
            },
            f"/api/me/reminders/{notif_id}/dismiss": {"dismissed": True, "id": notif_id},
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/reminders",
                },
            },
            cached_response=None,
        )
        assert any(
            call["method"] == "GET" and call["path"].startswith("/api/me/reminders") for call in backend.calls
        ), backend.calls
        cards = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert any(
            (payload.get("reply_markup") or {}).get("inline_keyboard", [[{}]])[0][0].get("callback_data")
            == f"remind:dismiss:{notif_id}"
            for payload in cards
        ), cards

        backend.calls.clear()
        telegram.calls.clear()
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb-rem",
                    "from": user,
                    "data": f"remind:dismiss:{notif_id}",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        assert any(
            call["method"] == "POST" and call["path"] == f"/api/me/reminders/{notif_id}/dismiss"
            for call in backend.calls
        ), backend.calls
    finally:
        bridge._inbox.close()
