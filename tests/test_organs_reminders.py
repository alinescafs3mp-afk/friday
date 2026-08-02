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

from friday.knowledge_graph import KnowledgeGraph
from friday.organs import ServiceContext, build_registry, in_quiet_hours
from friday.organs.reminders import RemindersOrgan, scan_reminders
from friday.server import create_app
from friday.storage.models import EntityType


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
    # Час МЕСТНЫЙ: тихие часы теперь про ночь человека, а не про UTC.
    now_hour = datetime.now().astimezone().hour
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
    from friday.config import load_settings

    base = load_settings()
    return replace(
        base,
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    )


# --- a push target must be a chat the user is alone in --------------------


@pytest.mark.asyncio
async def test_a_group_chat_is_never_a_push_target(storage):
    """A poisoned chat_id must not deliver, even though it is already on disk.

    Every proactive organ pushes to `metadata.chat_id`, and the backend used to
    overwrite it on *every* signed bridge request — including one sent in an
    allowlisted group. One message in a group therefore redirected the weekly
    digest, reminders and "on this day" into that group: the owner's own
    knowledge, read out to everyone in the room, silently and permanently.
    Repairing the write site cannot reach rows already stored, so resolution
    refuses a non-private chat id outright.
    """
    from friday.organs import resolve_chat_id

    group = "-1001234567890"
    reminders_settings = replace(
        _reminders_settings(), telegram_allowed_chat_ids=[-1001234567890], telegram_owner_chat_ids=[]
    )
    poisoned = _seed_telegram_user(storage, group)
    _seed_event(storage, poisoned, "Личное: результаты анализов", _today_iso(0))

    assert resolve_chat_id(storage, poisoned) is None

    ctx = ServiceContext(
        settings=reminders_settings, storage=storage, kg=KnowledgeGraph(storage), ingestion=None
    )
    await scan_reminders(ctx)
    assert storage.list_pending_notifications(limit=100) == []


def test_writing_from_a_group_keeps_the_private_chat_on_file(settings):
    """The group message must not overwrite a known-good private target."""
    import json
    import time
    import uuid

    from friday.security import sign_bridge_request

    group_settings = replace(settings, telegram_allowed_chat_ids=[5001, -1001234567890])
    with TestClient(create_app(group_settings)) as client:
        storage = client.app.state.storage

        def bridge_get(chat: str) -> int:
            path = "/api/me"
            timestamp = int(time.time())
            nonce = uuid.uuid4().hex
            return client.get(
                path,
                headers={
                    "X-Friday-Timestamp": str(timestamp),
                    "X-Friday-User": "5001",
                    "X-Friday-Chat": chat,
                    "X-Friday-Nonce": nonce,
                    "X-Friday-Signature": sign_bridge_request(
                        group_settings.telegram_bridge_secret,
                        timestamp=timestamp,
                        method="GET",
                        path=path,
                        external_user_id="5001",
                        chat_id=chat,
                        nonce=nonce,
                        body=b"",
                    ),
                },
            ).status_code

        assert bridge_get("5001") == 200  # private chat: id equals the sender's
        user_id = next(u["id"] for u in storage.list_users() if str(u.get("external_id") or "") == "5001")
        stored = json.loads(storage.get_user(user_id)["metadata_json"])
        assert stored["chat_id"] == "5001"

        assert bridge_get("-1001234567890") == 200  # same person, now in a group
        stored = json.loads(storage.get_user(user_id)["metadata_json"])
        assert stored["chat_id"] == "5001", "a group message overwrote the private push target"


def test_the_documented_organ_list_matches_the_registry(settings):
    """A hand-maintained list next to the thing it describes always drifts.

    `BUILTIN_ORGAN_NAMES` is the exported, documented inventory of shipped organs
    and it silently lost `sentinel` — `build_registry` had six, the constant
    named five. Pinned here so the next organ cannot be added to only one of them.
    """
    from friday.organs import BUILTIN_ORGAN_NAMES

    registered = tuple(organ.name for organ in build_registry(settings).organs)
    assert sorted(BUILTIN_ORGAN_NAMES) == sorted(registered)


def test_a_reminder_that_exhausted_its_retries_can_be_queued_again(storage):
    """Five attempts are spent in about 75 seconds, and then it was gone for good.

    There is no delay column: the bridge drains every 15 s and each failed send
    increments `attempts`, so a two-minute network outage exhausts every queued
    push. The terminal `failed` row kept its `dedup_key`, which keeps it in
    `uq_outbound_dedup` — so `enqueue_notification` refused to queue that reminder
    ever again. An event silently lost its only notification because the WAN
    blinked once.

    A `sent` row must keep its key, or the same message goes out twice.
    """
    storage.ensure_user("alice")
    assert storage.enqueue_notification("alice", "5001", "Событие завтра", dedup_key="event:1")
    assert not storage.enqueue_notification("alice", "5001", "Событие завтра", dedup_key="event:1")

    pending = storage.list_pending_notifications(limit=10)
    assert len(pending) == 1
    notif_id = pending[0]["id"]

    for _ in range(5):
        storage.mark_notifications(failed_ids=[notif_id], max_attempts=5)
    assert storage.list_pending_notifications(limit=10) == []

    # The organ's next scan can queue it again — the reminder is not lost.
    assert storage.enqueue_notification("alice", "5001", "Событие завтра", dedup_key="event:1")
    assert len(storage.list_pending_notifications(limit=10)) == 1

    # A delivered notification still suppresses a duplicate.
    delivered = storage.list_pending_notifications(limit=10)[0]["id"]
    storage.mark_notifications(sent_ids=[delivered])
    assert not storage.enqueue_notification("alice", "5001", "Событие завтра", dedup_key="event:1")
