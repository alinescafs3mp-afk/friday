"""Atomic storage fence for one person-owned reminder send attempt."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.reminder_schedule import reminder_due_state
from friday.storage import init_storage
from friday.storage.models import EntityType

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "event,expected",
    [
        (
            {
                "occurred_at": "2026-08-29",
                "source": "reminder:alice",
                "description": "friday-reminder-clock:12:01",
            },
            "early",
        ),
        (
            {
                "occurred_at": "2026-08-29",
                "source": "reminder:alice",
                "description": "friday-reminder-clock:11:59",
            },
            "due",
        ),
        ({"occurred_at": "2026-08-21", "source": "reminder:alice"}, "expired"),
        ({"occurred_at": "2026-08-31", "source": "document"}, "early"),
        ({"occurred_at": "2026-08-28", "source": "document"}, "expired"),
        (
            {
                "occurred_at": "2026-08-29",
                "source": "reminder:alice",
                "description": "friday-reminder-clock:25:99",
            },
            "invalid",
        ),
        ({"occurred_at": "not-a-date", "source": "reminder:alice"}, "invalid"),
    ],
)
def test_reminder_due_window_is_closed(event: dict, expected: str) -> None:
    assert reminder_due_state(event, NOW, lead_days=1) == expected


def _queued_reminder(
    storage,
    *,
    occurred_at: str,
    user_id: str = "alice",
    chat_id: str = "5001",
    source: str | None = None,
) -> dict:
    storage.ensure_user(user_id, metadata={"chat_id": chat_id})
    graph = KnowledgeGraph(storage)
    event = graph.create_entity(
        user_id,
        f"opaque reminder {occurred_at}",
        EntityType.EVENT,
        deduplicate=False,
    )
    graph.set_event_time(
        user_id,
        event["id"],
        occurred_at,
        source=source or f"reminder:{user_id}",
    )
    stored_time = storage.execute(
        "SELECT occurred_at FROM entity_time WHERE entity_id=?",
        (event["id"],),
    ).fetchone()["occurred_at"]
    dedup_key = f"reminder:{event['id']}:{stored_time}"
    body = "bounded reminder body"
    assert storage.enqueue_notification(
        user_id,
        chat_id,
        body,
        kind="reminder",
        dedup_key=dedup_key,
    )
    row = storage.execute(
        """SELECT id, user_id, chat_id, kind, dedup_key, status, attempts
             FROM outbound_notifications WHERE dedup_key=?""",
        (dedup_key,),
    ).fetchone()
    return {**dict(row), "entity_id": event["id"], "body": body}


def _claim(storage, pointer: dict, *, now: datetime = NOW) -> dict | None:
    return storage.claim_reminder_notification(
        pointer["id"],
        expected_chat_id=pointer["chat_id"],
        expected_dedup_key=pointer["dedup_key"],
        now=now,
        lead_days=1,
    )


def test_two_storage_workers_get_one_due_reminder_body(settings, storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    second = init_storage(settings)
    barrier = Barrier(2)

    def claim(instance):
        barrier.wait(timeout=5)
        return _claim(instance, pointer)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (storage, second)))
    finally:
        second.close()

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0] == {
        "id": pointer["id"],
        "chat_id": pointer["chat_id"],
        "kind": "reminder",
        "dedup_key": pointer["dedup_key"],
        "body": pointer["body"],
    }
    durable = storage.execute(
        "SELECT status, attempts FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (durable["status"], durable["attempts"]) == ("uncertain", 0)


def test_pointer_drift_neither_exposes_body_nor_mutates_pending(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())

    assert (
        storage.claim_reminder_notification(
            pointer["id"],
            expected_chat_id="5002",
            expected_dedup_key=pointer["dedup_key"],
            now=NOW,
            lead_days=1,
        )
        is None
    )
    assert (
        storage.claim_reminder_notification(
            pointer["id"],
            expected_chat_id=pointer["chat_id"],
            expected_dedup_key="reminder:another:event",
            now=NOW,
            lead_days=1,
        )
        is None
    )
    assert (
        storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (pointer["id"],),
        ).fetchone()["status"]
        == "pending"
    )


def test_due_document_event_keeps_ordinary_owner_delivery(storage) -> None:
    pointer = _queued_reminder(
        storage,
        occurred_at=(NOW.date() + timedelta(days=1)).isoformat(),
        source="document",
    )

    claimed = _claim(storage, pointer)
    assert claimed is not None
    assert claimed["body"] == pointer["body"]


def test_shared_document_event_cannot_be_claimed_for_a_participant(settings) -> None:
    shared = init_storage(replace(settings, shared_archive=True))
    try:
        shared.ensure_user(LEGACY_OWNER_USER_ID)
        shared.ensure_user("alice")
        graph = KnowledgeGraph(shared)
        event = graph.create_entity(
            LEGACY_OWNER_USER_ID,
            "shared document event",
            EntityType.EVENT,
            deduplicate=False,
        )
        occurred_at = (NOW.date() + timedelta(days=1)).isoformat()
        graph.set_event_time(LEGACY_OWNER_USER_ID, event["id"], occurred_at, source="document")
        dedup_key = f"reminder:{event['id']}:{occurred_at}"
        assert shared.enqueue_notification(
            "alice",
            "5001",
            "shared document reminder",
            kind="reminder",
            dedup_key=dedup_key,
        )
        forged = shared.execute(
            "SELECT id, chat_id, dedup_key FROM outbound_notifications WHERE chat_id='5001'",
        ).fetchone()

        assert (
            shared.claim_reminder_notification(
                forged["id"],
                expected_chat_id=forged["chat_id"],
                expected_dedup_key=forged["dedup_key"],
                now=NOW,
                lead_days=1,
            )
            is None
        )
        assert (
            shared.execute(
                "SELECT status FROM outbound_notifications WHERE id=?",
                (forged["id"],),
            ).fetchone()["status"]
            == "failed"
        )
    finally:
        shared.close()


def test_early_pointer_is_retired_and_releases_its_future_slot(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=(NOW.date() + timedelta(days=1)).isoformat())

    assert _claim(storage, pointer) is None
    retired = storage.execute(
        "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retired["status"], retired["dedup_key"]) == ("failed", "")
    assert storage.enqueue_notification(
        pointer["user_id"],
        pointer["chat_id"],
        pointer["body"],
        kind="reminder",
        dedup_key=pointer["dedup_key"],
    )


def test_expired_pointer_is_retired_without_releasing_its_slot(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=(NOW.date() - timedelta(days=8)).isoformat())

    assert _claim(storage, pointer) is None
    retired = storage.execute(
        "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retired["status"], retired["dedup_key"]) == ("failed", pointer["dedup_key"])
    assert not storage.enqueue_notification(
        pointer["user_id"],
        pointer["chat_id"],
        pointer["body"],
        kind="reminder",
        dedup_key=pointer["dedup_key"],
    )


def test_invalid_schedule_is_retired_without_releasing_its_slot(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    invalid_key = f"reminder:{pointer['entity_id']}:not-a-date"
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_time SET occurred_at='not-a-date' WHERE entity_id=?",
            (pointer["entity_id"],),
        )
        conn.execute(
            "UPDATE outbound_notifications SET dedup_key=? WHERE id=?",
            (invalid_key, pointer["id"]),
        )
    pointer["dedup_key"] = invalid_key

    assert _claim(storage, pointer) is None
    retired = storage.execute(
        "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retired["status"], retired["dedup_key"]) == ("failed", pointer["dedup_key"])


def test_source_drift_retires_without_returning_body(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_time SET source='reminder:bob' WHERE entity_id=?",
            (pointer["entity_id"],),
        )

    assert _claim(storage, pointer) is None
    retired = storage.execute(
        "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retired["status"], retired["dedup_key"]) == ("failed", pointer["dedup_key"])


def test_private_owner_drift_retires_without_returning_body(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    with storage.transaction() as conn:
        conn.execute(
            "DELETE FROM private_entity_owners WHERE entity_id=?",
            (pointer["entity_id"],),
        )

    assert _claim(storage, pointer) is None
    retired = storage.execute(
        "SELECT status, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retired["status"], retired["dedup_key"]) == ("failed", pointer["dedup_key"])


@pytest.mark.parametrize("outcome", ["sent", "failed", "uncertain"])
def test_pending_reminder_cannot_be_settled_without_send_edge_claim(storage, outcome: str) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())

    states = storage.acknowledge_notifications(**{f"{outcome}_ids": [pointer["id"]]})

    assert states["pending"] == [pointer["id"]]
    durable = storage.execute(
        "SELECT status, attempts, sent_at FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (durable["status"], durable["attempts"], durable["sent_at"]) == ("pending", 0, None)


@pytest.mark.parametrize("outcome", ["sent", "failed"])
def test_ordinary_pending_notification_ack_behavior_is_unchanged(storage, outcome: str) -> None:
    storage.ensure_user("alice")
    assert storage.enqueue_notification("alice", "5001", "ordinary notification")
    notification_id = storage.list_pending_notifications()[0]["id"]

    states = storage.acknowledge_notifications(**{f"{outcome}_ids": [notification_id]})

    expected = "sent" if outcome == "sent" else "pending"
    assert states[expected] == [notification_id]
    durable = storage.execute(
        "SELECT status, attempts FROM outbound_notifications WHERE id=?",
        (notification_id,),
    ).fetchone()
    assert (durable["status"], durable["attempts"]) == (expected, int(outcome == "failed"))


def test_sent_ack_settles_a_claimed_reminder(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    assert _claim(storage, pointer) is not None

    states = storage.acknowledge_notifications(sent_ids=[pointer["id"]])
    assert states["sent"] == [pointer["id"]]
    assert (
        storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (pointer["id"],),
        ).fetchone()["status"]
        == "sent"
    )


def test_sent_ack_remains_provable_after_claimed_source_drift(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    assert _claim(storage, pointer) is not None
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_time SET source='reminder:bob' WHERE entity_id=?",
            (pointer["entity_id"],),
        )

    states = storage.acknowledge_notifications(sent_ids=[pointer["id"]])
    assert states["sent"] == [pointer["id"]]
    assert (
        storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (pointer["id"],),
        ).fetchone()["status"]
        == "sent"
    )


def test_failed_ack_after_claimed_source_drift_stays_uncertain(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    assert _claim(storage, pointer) is not None
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_time SET source='reminder:bob' WHERE entity_id=?",
            (pointer["entity_id"],),
        )

    states = storage.acknowledge_notifications(failed_ids=[pointer["id"]])
    assert states["uncertain"] == [pointer["id"]]
    durable = storage.execute(
        "SELECT status, attempts, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (durable["status"], durable["attempts"], durable["dedup_key"]) == (
        "uncertain",
        0,
        pointer["dedup_key"],
    )


def test_proven_failure_reopens_then_terminally_retires_a_claim(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    assert _claim(storage, pointer) is not None

    first = storage.acknowledge_notifications(failed_ids=[pointer["id"]], max_attempts=2)
    assert first["pending"] == [pointer["id"]]
    retryable = storage.execute(
        "SELECT status, attempts, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retryable["status"], retryable["attempts"], retryable["dedup_key"]) == (
        "pending",
        1,
        pointer["dedup_key"],
    )

    assert _claim(storage, pointer) is not None
    terminal = storage.acknowledge_notifications(failed_ids=[pointer["id"]], max_attempts=2)
    assert terminal["failed"] == [pointer["id"]]
    retired = storage.execute(
        "SELECT status, attempts, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (retired["status"], retired["attempts"], retired["dedup_key"]) == (
        "failed",
        2,
        pointer["dedup_key"],
    )
    assert not storage.enqueue_notification(
        pointer["user_id"],
        pointer["chat_id"],
        pointer["body"],
        kind="reminder",
        dedup_key=pointer["dedup_key"],
    )


def test_ambiguous_ack_keeps_the_claim_uncertain_and_unclaimable(storage) -> None:
    pointer = _queued_reminder(storage, occurred_at=NOW.date().isoformat())
    assert _claim(storage, pointer) is not None

    states = storage.acknowledge_notifications(uncertain_ids=[pointer["id"]])
    assert states["uncertain"] == [pointer["id"]]
    assert _claim(storage, pointer) is None
    durable = storage.execute(
        "SELECT status, attempts, dedup_key FROM outbound_notifications WHERE id=?",
        (pointer["id"],),
    ).fetchone()
    assert (durable["status"], durable["attempts"], durable["dedup_key"]) == (
        "uncertain",
        0,
        pointer["dedup_key"],
    )
