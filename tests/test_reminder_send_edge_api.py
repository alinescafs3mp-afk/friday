from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from starlette.requests import Request

import friday.api.notifications as notifications
from friday.organs.engineer.terminal_delivery import TerminalDeliveryError

PERSON_ID = "telegram:test:5001"
CHAT_ID = "5001"
NOTIFICATION_ID = "notif_reminder_send_edge"
DEDUP_KEY = "reminder:entity_send_edge:2026-08-29"
BODY = "🔔 Напоминание: «проверить отчёт» — сегодня."


def _seed_pending_reminder(storage) -> dict[str, str]:
    storage.ensure_user(PERSON_ID, source="telegram", metadata={"chat_id": CHAT_ID})
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO outbound_notifications(
                   id,user_id,chat_id,kind,dedup_key,body,status,attempts,created_at)
               VALUES(?,?,?,?,?,?,'pending',0,?)""",
            (
                NOTIFICATION_ID,
                PERSON_ID,
                CHAT_ID,
                "reminder",
                DEDUP_KEY,
                BODY,
                "2026-08-29T09:00:00Z",
            ),
        )
    return {
        "id": NOTIFICATION_ID,
        "chat_id": CHAT_ID,
        "kind": "reminder",
        "dedup_key": DEDUP_KEY,
    }


def _state(storage, settings):
    return SimpleNamespace(storage=storage, settings=settings)


@pytest.mark.asyncio
async def test_pending_reminder_exposes_only_an_exact_claim_pointer(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _seed_pending_reminder(storage)
    row = {**pointer, "user_id": PERSON_ID, "body": BODY}
    monkeypatch.setattr(
        storage,
        "list_pending_notifications",
        lambda *, limit: [row],
    )
    state = _state(storage, settings)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/notifications/pending",
            "app": SimpleNamespace(state=state),
        }
    )
    request.state.actor = SimpleNamespace(source="telegram-bridge")

    pending = await notifications.notifications_pending(
        request,
        limit=20,
        status_messages=True,
    )

    assert pending == {"items": [pointer], "count": 1}


def test_reminder_claim_reauthorizes_and_keeps_the_exact_payload_shape(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _seed_pending_reminder(storage)
    fixed_now = datetime(2026, 8, 29, 12, 30, tzinfo=ZoneInfo(settings.local_timezone or "UTC"))
    observed: dict[str, object] = {}
    original_get_user = storage.get_user
    original_resolve_identity = storage.resolve_identity
    original_resolve_chat_id = notifications.resolve_chat_id
    original_may_push_to = notifications.may_push_to

    def get_user(user_id: str):
        assert storage.conn.in_transaction
        return original_get_user(user_id)

    def resolve_identity(source: str, external_id: str):
        assert storage.conn.in_transaction
        return original_resolve_identity(source, external_id)

    def resolve_chat_id(inner_storage, user_id: str):
        assert storage.conn.in_transaction
        return original_resolve_chat_id(inner_storage, user_id)

    def may_push_to(inner_settings, inner_storage, user_id: str, chat_id: str):
        assert storage.conn.in_transaction
        return original_may_push_to(inner_settings, inner_storage, user_id, chat_id)

    def claim(notification_id: str, **kwargs):
        assert storage.conn.in_transaction
        observed.update(notification_id=notification_id, **kwargs)
        row = storage.execute(
            """SELECT id,user_id,chat_id,kind,dedup_key,body,status
                 FROM outbound_notifications WHERE id=?""",
            (notification_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    monkeypatch.setattr(storage, "claim_reminder_notification", claim, raising=False)
    monkeypatch.setattr(storage, "get_user", get_user)
    monkeypatch.setattr(storage, "resolve_identity", resolve_identity)
    monkeypatch.setattr(notifications, "resolve_chat_id", resolve_chat_id)
    monkeypatch.setattr(notifications, "may_push_to", may_push_to)
    monkeypatch.setattr(notifications, "local_now", lambda _settings: fixed_now)

    claimed = notifications._claim_strict_notification(
        _state(storage, settings),
        NOTIFICATION_ID,
        pointer,
        status_messages=True,
    )

    assert claimed == {**pointer, "body": BODY}
    assert observed == {
        "notification_id": NOTIFICATION_ID,
        "expected_chat_id": CHAT_ID,
        "expected_dedup_key": DEDUP_KEY,
        "now": fixed_now,
        "lead_days": settings.reminders_lead_days,
    }
    assert original_resolve_identity("telegram", CHAT_ID) is None


@pytest.mark.parametrize("revocation", ["account", "chat", "push", "identity"])
def test_reminder_claim_fails_closed_on_current_authority_drift(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    pointer = _seed_pending_reminder(storage)
    claim_called = False

    def claim(_notification_id: str, **_kwargs):
        nonlocal claim_called
        claim_called = True
        return None

    monkeypatch.setattr(storage, "claim_reminder_notification", claim, raising=False)
    state = _state(storage, settings)
    if revocation == "account":
        with storage.transaction() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (PERSON_ID,))
    elif revocation == "chat":
        storage.ensure_user(PERSON_ID, source="telegram", metadata={"chat_id": "5002"})
    elif revocation == "push":
        monkeypatch.setattr(notifications, "may_push_to", lambda *_args: False)
    else:
        other = "telegram:test:other"
        storage.ensure_user(other, source="telegram", metadata={"chat_id": "5002"})
        storage.link_identity("telegram", CHAT_ID, other)

    with pytest.raises(TerminalDeliveryError, match="terminal_authorization_changed"):
        notifications._claim_strict_notification(
            state,
            NOTIFICATION_ID,
            pointer,
            status_messages=False,
        )
    assert claim_called is False


def test_reminder_claim_rejects_pointer_drift_before_storage_claim(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _seed_pending_reminder(storage)
    claim_called = False

    def claim(_notification_id: str, **_kwargs):
        nonlocal claim_called
        claim_called = True
        return None

    monkeypatch.setattr(storage, "claim_reminder_notification", claim, raising=False)
    stale_pointer = {**pointer, "dedup_key": f"{DEDUP_KEY}:stale"}

    with pytest.raises(TerminalDeliveryError, match="terminal_claim_identity_changed"):
        notifications._claim_strict_notification(
            _state(storage, settings),
            NOTIFICATION_ID,
            stale_pointer,
            status_messages=False,
        )
    assert claim_called is False


def test_reminder_retirement_commits_before_claim_reports_unavailable(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = _seed_pending_reminder(storage)

    def retire_and_decline(notification_id: str, **_kwargs):
        assert storage.conn.in_transaction
        storage.execute(
            """UPDATE outbound_notifications
                  SET status='failed', kind='undeliverable:reminder_expired', dedup_key=''
                WHERE id=? AND status='pending'""",
            (notification_id,),
        )
        return None

    monkeypatch.setattr(
        storage,
        "claim_reminder_notification",
        retire_and_decline,
        raising=False,
    )

    with pytest.raises(TerminalDeliveryError, match="terminal_notification_unavailable"):
        notifications._claim_strict_notification(
            _state(storage, replace(settings, reminders_lead_days=0)),
            NOTIFICATION_ID,
            pointer,
            status_messages=False,
        )

    retired = storage.execute(
        "SELECT status,kind,dedup_key FROM outbound_notifications WHERE id=?",
        (NOTIFICATION_ID,),
    ).fetchone()
    assert retired is not None
    assert dict(retired) == {
        "status": "failed",
        "kind": "undeliverable:reminder_expired",
        "dedup_key": "",
    }
