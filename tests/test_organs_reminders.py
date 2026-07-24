"""Organ framework (JOP) + outbound notification channel + reminders organ.

Covers: the notification queue (enqueue/dedup, pending, ack, terminal-fail),
the bridge-only gating of the drain endpoints, the reminders organ scan
(enqueues per due event, dedups, respects quiet hours, honors the allowlist and
chat-id resolution), and the organ registry composition.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jericho.knowledge_graph import KnowledgeGraph
from jericho.organs import ServiceContext, build_registry, in_quiet_hours
from jericho.organs.reminders import RemindersOrgan, scan_reminders
from jericho.server import create_app
from jericho.storage.models import EntityType


def _today_iso(offset_days: int = 0) -> str:
    return (datetime.now(UTC).date() + timedelta(days=offset_days)).isoformat()


def _seed_telegram_user(storage, chat_id: str) -> str:
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", metadata={"chat_id": chat_id})
    return user_id


def _seed_event(storage, user_id: str, name: str, occurred_at: str) -> str:
    graph = KnowledgeGraph(storage)
    event = graph.create_entity(user_id, name, EntityType.EVENT)
    graph.set_event_time(user_id, event["id"], occurred_at)
    return event["id"]


# --- notification queue ---------------------------------------------------


def test_notification_queue_enqueue_dedup_and_lifecycle(storage):
    storage.ensure_user("alice")
    assert storage.enqueue_notification("alice", "42", "первое", kind="reminder", dedup_key="k1") is True
    # Same dedup_key is idempotent.
    assert (
        storage.enqueue_notification("alice", "42", "первое again", kind="reminder", dedup_key="k1") is False
    )
    assert storage.enqueue_notification("alice", "42", "второе", kind="reminder", dedup_key="k2") is True

    pending = storage.list_pending_notifications()
    ids = {n["id"] for n in pending}
    assert len(ids) == 2

    one = pending[0]["id"]
    storage.mark_notifications(sent_ids=[one])
    remaining = storage.list_pending_notifications()
    assert one not in {n["id"] for n in remaining}

    other = remaining[0]["id"]
    for _ in range(5):
        storage.mark_notifications(failed_ids=[other])
    # After the attempt cap the row is terminal, not endlessly retried.
    assert other not in {n["id"] for n in storage.list_pending_notifications()}
    row = storage.execute("SELECT status FROM outbound_notifications WHERE id=?", (other,)).fetchone()
    assert row["status"] == "failed"


# --- bridge-only endpoints ------------------------------------------------


def test_notification_endpoints_require_bridge_actor(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        # Owner (bearer) is not the bridge — the queue is the bridge's alone.
        assert client.get("/api/notifications/pending", headers=owner).status_code == 403
        assert client.post("/api/notifications/ack", json={}, headers=owner).status_code == 403


# --- reminders organ ------------------------------------------------------


@pytest.mark.asyncio
async def test_reminders_scan_enqueues_due_events_and_dedups(storage):
    reminders_settings = _reminders_settings()
    chat_id = "5001"  # on the conftest allowlist
    user_id = _seed_telegram_user(storage, chat_id)
    _seed_event(storage, user_id, "Запуск Orion", _today_iso(0))
    _seed_event(storage, user_id, "Дедлайн", _today_iso(1))
    _seed_event(storage, user_id, "Далёкое", _today_iso(30))  # outside lead window

    ctx = ServiceContext(
        settings=reminders_settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None
    )
    await scan_reminders(ctx)

    pending = storage.list_pending_notifications(limit=100)
    bodies = [n["body"] for n in pending]
    assert any("Запуск Orion" in b and "сегодня" in b for b in bodies)
    assert any("Дедлайн" in b and "завтра" in b for b in bodies)
    assert not any("Далёкое" in b for b in bodies)
    assert all(n["chat_id"] == chat_id for n in pending)

    # Re-running does not duplicate reminders for the same events.
    before = len(pending)
    await scan_reminders(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == before


@pytest.mark.asyncio
async def test_reminders_scan_skips_unallowlisted_chats(storage):
    reminders_settings = _reminders_settings()
    stranger = _seed_telegram_user(storage, "999999")  # not on allowlist
    _seed_event(storage, stranger, "Секрет", _today_iso(0))

    ctx = ServiceContext(
        settings=reminders_settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None
    )
    await scan_reminders(ctx)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_reminders_scan_respects_quiet_hours(storage):
    now_hour = datetime.now(UTC).hour
    # A quiet window covering the current hour blocks all enqueues.
    quiet = _reminders_settings(quiet_start=now_hour, quiet_end=(now_hour + 1) % 24)
    user_id = _seed_telegram_user(storage, "5001")
    _seed_event(storage, user_id, "Ночью", _today_iso(0))

    ctx = ServiceContext(settings=quiet, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)
    await scan_reminders(ctx)
    assert storage.list_pending_notifications(limit=100) == []


def test_quiet_hours_window_is_midnight_safe():
    assert in_quiet_hours(23, 22, 8) is True
    assert in_quiet_hours(3, 22, 8) is True
    assert in_quiet_hours(12, 22, 8) is False
    assert in_quiet_hours(5, 8, 22) is False
    assert in_quiet_hours(0, 0, 0) is False  # disabled window


# --- registry -------------------------------------------------------------


def test_registry_includes_reminders_worker(settings):
    registry = build_registry(settings)
    assert any(isinstance(o, RemindersOrgan) for o in registry.organs)
    ctx = ServiceContext(settings=settings, storage=None, kg=None, ingestion=None)
    workers = registry.workers(ctx)
    assert any(w.name == "reminders_scan" for w in workers)


def _reminders_settings(*, quiet_start: int = 0, quiet_end: int = 0):
    # Import the conftest settings via a fresh load with reminders enabled and a
    # disabled quiet window unless a test asks otherwise.
    from jericho.config import load_settings

    base = load_settings()
    return replace(
        base,
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    )
