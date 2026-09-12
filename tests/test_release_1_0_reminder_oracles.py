"""Exact deterministic oracles for reminder self-service and Telegram rendering.

Every HTTP observation crosses the real FastAPI authentication and storage path.
Telegram observations invoke the production command/callback branches but stop at
controlled in-process transports.  They therefore prove the adapter contract and
must never be credited as a live Telegram sender round-trip.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.organs import ServiceContext
from friday.organs.reminders import scan_reminders
from friday.server import create_app
from friday.storage.models import Entity, EntityType
from friday.telegram_bridge import TelegramBridge, TelegramConfig
from tests.test_api_vertical_slice import _bridge_get, _bridge_json

_ALICE_EXTERNAL = "5001"
_ALICE = "telegram:telegram:5001"
_BOB_EXTERNAL = "5002"
_BOB = "telegram:telegram:5002"
_MIGRATED_ALLOWED_CHAT = "5003"
_MISSING_ID = "ent_r10_missing_reminder_01"
_UNRELATED_BUTTON = {"text": "Оставить", "callback_data": "doc:show:ko_r10_other"}
_FIXED_NOW = datetime(2035, 9, 8, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))


@dataclass(frozen=True)
class _ReminderHTTP:
    client: TestClient
    settings: Any
    storage: Any
    graph: KnowledgeGraph
    now: datetime
    today: date


@pytest.fixture
def reminder_http(settings, monkeypatch) -> Any:
    import friday.organs.reminders as reminder_organ
    import friday.server as server_module

    monkeypatch.setattr(server_module, "local_now", lambda _settings: _FIXED_NOW)
    monkeypatch.setattr(reminder_organ, "local_now", lambda _settings: _FIXED_NOW)
    tuned = replace(
        settings,
        shared_archive=True,
        reminders_enabled=True,
        reminders_lead_days=2,
        quiet_hours_start=0,
        quiet_hours_end=0,
        telegram_allowed_chat_ids=[5001, 5002, int(_MIGRATED_ALLOWED_CHAT)],
        telegram_owner_chat_ids=[],
    )
    assert tuned.llm_enabled is False
    assert tuned.embeddings_enabled is False
    assert tuned.workers_enabled is False
    app = create_app(tuned)
    with TestClient(app) as client:
        storage = app.state.storage
        for person, external, name in (
            (_ALICE, _ALICE_EXTERNAL, "Alice"),
            (_BOB, _BOB_EXTERNAL, "Bob"),
        ):
            storage.ensure_user(
                person,
                source="telegram",
                external_id=external,
                display_name=name,
                preset_key="user",
                metadata={"chat_id": external},
            )
        yield _ReminderHTTP(
            client=client,
            settings=tuned,
            storage=storage,
            graph=KnowledgeGraph(storage),
            now=_FIXED_NOW,
            today=_FIXED_NOW.date(),
        )


def _seed_event(ctx: _ReminderHTTP, person: str, name: str, offset_days: int) -> dict[str, str]:
    event = ctx.graph.create_entity(person, name, EntityType.EVENT, deduplicate=False)
    occurred_at = (ctx.today + timedelta(days=offset_days)).isoformat()
    ctx.graph.set_event_time(
        person,
        event["id"],
        occurred_at,
        source=f"reminder:{person}",
    )
    return {
        "id": str(event["id"]),
        "name": name,
        "occurred_at": occurred_at,
        "dedup_key": f"reminder:{event['id']}:{occurred_at}",
        "person": person,
    }


def _enqueue(ctx: _ReminderHTTP, event: dict[str, str], *, state: str = "pending") -> dict[str, Any]:
    chat_id = _ALICE_EXTERNAL if event["person"] == _ALICE else _BOB_EXTERNAL
    assert ctx.storage.enqueue_notification(
        event["person"],
        chat_id,
        f"queued:{event['name']}",
        kind="reminder",
        dedup_key=event["dedup_key"],
    )
    row = ctx.storage.execute(
        "SELECT * FROM outbound_notifications WHERE user_id=? AND dedup_key=?",
        (event["person"], event["dedup_key"]),
    ).fetchone()
    assert row is not None
    if state == "sent":
        claimed = ctx.storage.claim_reminder_notification(
            row["id"],
            expected_chat_id=chat_id,
            expected_dedup_key=event["dedup_key"],
            now=ctx.now,
            lead_days=ctx.settings.reminders_lead_days,
        )
        assert claimed is not None
        ctx.storage.mark_notifications(sent_ids=[row["id"]])
    elif state == "dismissed":
        assert ctx.storage.silence_reminder(event["person"], event["dedup_key"], chat_id=chat_id)
    elif state != "pending":
        raise AssertionError("unsupported fixture state")
    stored = ctx.storage.execute("SELECT * FROM outbound_notifications WHERE id=?", (row["id"],)).fetchone()
    assert stored is not None
    return dict(stored)


def _when(ctx: _ReminderHTTP, event: dict[str, str]) -> str:
    if event["occurred_at"] == ctx.today.isoformat():
        return "сегодня"
    if event["occurred_at"] == (ctx.today + timedelta(days=1)).isoformat():
        return "завтра"
    return event["occurred_at"]


def _expected_item(ctx: _ReminderHTTP, event: dict[str, str], *, state: str = "new") -> dict[str, str]:
    return {
        "id": event["id"],
        "body": f"🔔 «{event['name']}» — {_when(ctx, event)}.",
        "dedup_key": event["dedup_key"],
        "occurred_at": event["occurred_at"],
        "state": state,
    }


def _assert_list(response: httpx.Response, expected: list[dict[str, str]]) -> None:
    assert response.status_code == 200, "reminder_list_status"
    assert response.json() == {"count": len(expected), "items": expected}, "reminder_list_exact"


def _relevant_snapshot(storage: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        "notifications": [
            dict(row)
            for row in storage.execute("SELECT * FROM outbound_notifications ORDER BY id").fetchall()
        ],
        "times": [
            dict(row) for row in storage.execute("SELECT * FROM entity_time ORDER BY entity_id").fetchall()
        ],
        "owners": [
            dict(row)
            for row in storage.execute("SELECT * FROM private_entity_owners ORDER BY entity_id").fetchall()
        ],
    }


def _assert_dismissed(ctx: _ReminderHTTP, event: dict[str, str]) -> None:
    rows = ctx.storage.execute(
        """SELECT user_id,chat_id,kind,dedup_key,status
             FROM outbound_notifications WHERE user_id=? AND dedup_key=?""",
        (event["person"], event["dedup_key"]),
    ).fetchall()
    assert len(rows) == 1, "reminder_dismiss_persistence"
    assert dict(rows[0]) == {
        "user_id": event["person"],
        "chat_id": _ALICE_EXTERNAL if event["person"] == _ALICE else _BOB_EXTERNAL,
        "kind": "reminder",
        "dedup_key": event["dedup_key"],
        "status": "dismissed",
    }, "reminder_dismiss_persistence"


def _scan(ctx: _ReminderHTTP) -> None:
    asyncio.run(
        scan_reminders(
            ServiceContext(
                settings=ctx.settings,
                storage=ctx.storage,
                kg=ctx.graph,
                ingestion=None,
            )
        )
    )


def test_get_reminders_has_exact_person_order_window_and_durable_state(reminder_http) -> None:
    ctx = reminder_http
    _seed_event(ctx, _ALICE, "R10 Past", -1)
    alpha = _seed_event(ctx, _ALICE, "r10 alpha", 0)
    zulu = _seed_event(ctx, _ALICE, "R10 Zulu", 0)
    bravo = _seed_event(ctx, _ALICE, "R10 Bravo", 0)
    tomorrow = _seed_event(ctx, _ALICE, "R10 Tomorrow", 1)
    edge = _seed_event(ctx, _ALICE, "R10 Window Edge", 2)
    dismissed = _seed_event(ctx, _ALICE, "R10 Dismissed", 1)
    _seed_event(ctx, _ALICE, "R10 Beyond Window", 3)
    foreign = _seed_event(ctx, _BOB, "R10 Foreign Private", 0)
    _enqueue(ctx, alpha, state="pending")
    _enqueue(ctx, bravo, state="sent")
    _enqueue(ctx, dismissed, state="dismissed")

    expected = [
        _expected_item(ctx, alpha, state="pending"),
        _expected_item(ctx, bravo, state="sent"),
        _expected_item(ctx, zulu),
        _expected_item(ctx, tomorrow),
        _expected_item(ctx, edge),
    ]
    _assert_list(
        _bridge_get(
            ctx.client,
            ctx.settings,
            "/api/me/reminders?limit=100",
            user=_ALICE_EXTERNAL,
            chat=_ALICE_EXTERNAL,
        ),
        expected,
    )
    _assert_list(
        _bridge_get(
            ctx.client,
            ctx.settings,
            "/api/me/reminders?limit=100",
            user=_BOB_EXTERNAL,
            chat=_BOB_EXTERNAL,
        ),
        [_expected_item(ctx, foreign)],
    )
    for limit in (0, 101):
        denied = _bridge_get(
            ctx.client,
            ctx.settings,
            f"/api/me/reminders?limit={limit}",
            user=_ALICE_EXTERNAL,
            chat=_ALICE_EXTERNAL,
        )
        assert denied.status_code == 422


def test_get_reminders_refuses_anonymous_without_observing_or_mutating_private_rows(
    reminder_http,
) -> None:
    ctx = reminder_http
    own = _seed_event(ctx, _ALICE, "R10 Own Private Canary", 0)
    foreign = _seed_event(ctx, _BOB, "R10 Foreign Private Canary", 0)
    _enqueue(ctx, own)
    _enqueue(ctx, foreign)
    before = _relevant_snapshot(ctx.storage)

    response = ctx.client.get("/api/me/reminders")

    assert response.status_code == 401
    assert own["name"] not in response.text and foreign["name"] not in response.text
    assert _relevant_snapshot(ctx.storage) == before


def test_dismissed_head_does_not_consume_the_active_http_limit(reminder_http) -> None:
    """The public limit counts visible reminders, not hidden tombstones."""

    ctx = reminder_http
    dismissed = _seed_event(ctx, _ALICE, "R10 A Dismissed Head", 0)
    active = _seed_event(ctx, _ALICE, "R10 B Active Tail", 0)
    _enqueue(ctx, dismissed, state="dismissed")

    response = _bridge_get(
        ctx.client,
        ctx.settings,
        "/api/me/reminders?limit=1",
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )

    _assert_list(response, [_expected_item(ctx, active)])


def test_dismiss_accepts_queued_id_and_prescan_entity_then_blocks_every_rescan(
    reminder_http,
) -> None:
    ctx = reminder_http
    queued_event = _seed_event(ctx, _ALICE, "R10 Queued Dismiss", 0)
    _scan(ctx)
    queued = ctx.storage.execute(
        "SELECT * FROM outbound_notifications WHERE user_id=? AND dedup_key=?",
        (_ALICE, queued_event["dedup_key"]),
    ).fetchone()
    assert queued is not None and queued["status"] == "pending"

    queued_response = _bridge_json(
        ctx.client,
        ctx.settings,
        "POST",
        f"/api/me/reminders/{queued['id']}/dismiss",
        {},
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    assert queued_response.status_code == 200
    assert queued_response.json() == {"dismissed": True, "id": queued["id"]}
    _assert_dismissed(ctx, queued_event)

    prescan_event = _seed_event(ctx, _ALICE, "R10 Prescan Dismiss", 0)
    assert ctx.storage.reminder_states(_ALICE, [prescan_event["dedup_key"]]) == {}
    prescan_response = _bridge_json(
        ctx.client,
        ctx.settings,
        "POST",
        f"/api/me/reminders/{prescan_event['id']}/dismiss",
        {},
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    assert prescan_response.status_code == 200
    assert prescan_response.json() == {"dismissed": True, "id": prescan_event["id"]}
    _assert_dismissed(ctx, prescan_event)

    before_rescan = _relevant_snapshot(ctx.storage)
    _scan(ctx)
    assert _relevant_snapshot(ctx.storage) == before_rescan
    assert ctx.storage.list_pending_reminders(_ALICE) == []
    _assert_list(
        _bridge_get(
            ctx.client,
            ctx.settings,
            "/api/me/reminders",
            user=_ALICE_EXTERNAL,
            chat=_ALICE_EXTERNAL,
        ),
        [],
    )


@pytest.mark.parametrize("delivery_state", ["uncertain-event-id", "failed-event-id"])
def test_every_current_listed_delivery_edge_state_remains_dismissible(reminder_http, delivery_state) -> None:
    """Every current event-ID row offered by /reminders remains actionable."""

    ctx = reminder_http
    event = _seed_event(ctx, _ALICE, f"R10 {delivery_state}", 0)
    row = _enqueue(ctx, event)
    claimed = ctx.storage.claim_reminder_notification(
        row["id"],
        expected_chat_id=_ALICE_EXTERNAL,
        expected_dedup_key=event["dedup_key"],
        now=ctx.now,
        lead_days=ctx.settings.reminders_lead_days,
    )
    assert claimed is not None
    if delivery_state == "failed-event-id":
        states = ctx.storage.acknowledge_notifications(failed_ids=[row["id"]], max_attempts=1)
        assert states["failed"] == [row["id"]]
        expected_state = "failed"
    else:
        expected_state = "uncertain"
    target = event["id"]
    listed = _bridge_get(
        ctx.client,
        ctx.settings,
        "/api/me/reminders",
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    _assert_list(listed, [_expected_item(ctx, event, state=expected_state)])

    response = _bridge_json(
        ctx.client,
        ctx.settings,
        "POST",
        f"/api/me/reminders/{target}/dismiss",
        {},
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )

    assert response.status_code == 200, "listed_reminder_must_be_dismissible"
    assert response.json() == {"dismissed": True, "id": target}
    _assert_dismissed(ctx, event)


@pytest.mark.asyncio
async def test_legacy_sent_queue_id_button_remains_dismissible_after_delivery(
    reminder_http, tmp_path
) -> None:
    """A stale G19 Telegram button keeps working after its queue row was sent.

    The shipped legacy list rendered a pending queue-row ID, then the bridge
    transitioned that row to sent.  The current list below independently proves
    that new buttons use the event ID; only the callback replays the old ID.
    """

    ctx = reminder_http
    event = _seed_event(ctx, _ALICE, "R10 Legacy Sent Queue", 0)
    row = _enqueue(ctx, event, state="sent")
    legacy_target = str(row["id"])
    assert legacy_target != event["id"]
    listed = _bridge_get(
        ctx.client,
        ctx.settings,
        "/api/me/reminders",
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    _assert_list(listed, [_expected_item(ctx, event, state="sent")])
    assert listed.json()["items"][0]["id"] == event["id"], "current_list_uses_event_id"

    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(
            telegram,
            backend,
            _callback_update(51, legacy_target),
            cached_response=None,
        )
        assert backend.calls == [
            {
                "method": "POST",
                "path": f"/api/me/reminders/{legacy_target}/dismiss",
                "status": 200,
                "body": {"telegram_user": {"id": 5001, "first_name": "Alice"}},
            }
        ], "legacy_sent_queue_callback_status"
        callbacks = [payload for url, payload in telegram.calls if url.endswith("/answerCallbackQuery")]
        assert callbacks == [{"callback_query_id": "callback-51", "text": "Снято", "show_alert": False}]
        _assert_reminder_markup_retired(telegram.calls, update_id=51)
        _assert_dismissed(ctx, event)
    finally:
        bridge._inbox.close()


def test_dismissal_tombstone_survives_storage_delivery_chat_metadata_transition(
    reminder_http,
) -> None:
    """Storage-transition robustness only; this is not an entrance-path claim.

    The destination is separately allowlisted and belongs to neither test
    person.  Direct metadata setup models an already-authorized migration; HTTP
    and Telegram identity binding are intentionally outside this node's credit.
    """

    ctx = reminder_http
    event = _seed_event(ctx, _ALICE, "R10 Rebound Chat", 0)
    response = _bridge_json(
        ctx.client,
        ctx.settings,
        "POST",
        f"/api/me/reminders/{event['id']}/dismiss",
        {},
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    assert response.status_code == 200
    _assert_dismissed(ctx, event)
    assert int(_MIGRATED_ALLOWED_CHAT) in ctx.settings.telegram_effective_allowed_chat_ids
    ctx.storage.ensure_user(_ALICE, source="", metadata={"chat_id": _MIGRATED_ALLOWED_CHAT})

    _scan(ctx)

    rows = [
        dict(row)
        for row in ctx.storage.execute(
            """SELECT user_id,chat_id,kind,dedup_key,status
                 FROM outbound_notifications WHERE user_id=? AND dedup_key=?
                 ORDER BY id""",
            (_ALICE, event["dedup_key"]),
        ).fetchall()
    ]
    assert rows == [
        {
            "user_id": _ALICE,
            "chat_id": _ALICE_EXTERNAL,
            "kind": "reminder",
            "dedup_key": event["dedup_key"],
            "status": "dismissed",
        }
    ], "dismissed_reminder_requeued_after_chat_change"


def test_reminder_list_preserves_an_exact_personal_clock(reminder_http) -> None:
    """Self-service text retains the clock used by the delivery formatter."""

    ctx = reminder_http
    event = ctx.graph.create_entity(
        _ALICE,
        "R10 Exact Clock",
        EntityType.EVENT,
        description="friday-reminder-clock:15:00",
        deduplicate=False,
    )
    occurred_at = (ctx.today + timedelta(days=1)).isoformat()
    ctx.graph.set_event_time(
        _ALICE,
        event["id"],
        occurred_at,
        source=f"reminder:{_ALICE}",
    )
    expected = {
        "id": event["id"],
        "body": "🔔 «R10 Exact Clock» — завтра в 15:00.",
        "dedup_key": f"reminder:{event['id']}:{occurred_at}",
        "occurred_at": occurred_at,
        "state": "new",
    }

    _assert_list(
        _bridge_get(
            ctx.client,
            ctx.settings,
            "/api/me/reminders",
            user=_ALICE_EXTERNAL,
            chat=_ALICE_EXTERNAL,
        ),
        [expected],
    )


@pytest.mark.parametrize("target_kind", ["foreign-event", "foreign-queue", "missing", "anonymous"])
def test_dismiss_refusals_have_no_cross_person_effect(reminder_http, target_kind) -> None:
    ctx = reminder_http
    foreign = _seed_event(ctx, _BOB, "R10 Foreign Dismiss Canary", 0)
    foreign_row = _enqueue(ctx, foreign)
    own = _seed_event(ctx, _ALICE, "R10 Anonymous Dismiss Canary", 0)
    target = {
        "foreign-event": foreign["id"],
        "foreign-queue": str(foreign_row["id"]),
        "missing": _MISSING_ID,
        "anonymous": own["id"],
    }[target_kind]
    before = _relevant_snapshot(ctx.storage)

    path = f"/api/me/reminders/{target}/dismiss"
    if target_kind == "anonymous":
        response = ctx.client.post(path, json={})
        expected_status = 401
    else:
        response = _bridge_json(
            ctx.client,
            ctx.settings,
            "POST",
            path,
            {},
            user=_ALICE_EXTERNAL,
            chat=_ALICE_EXTERNAL,
        )
        expected_status = 404

    assert response.status_code == expected_status
    assert foreign["name"] not in response.text
    assert foreign["dedup_key"] not in response.text
    assert _relevant_snapshot(ctx.storage) == before


@pytest.mark.parametrize("fault", ["order", "foreign-id", "state"])
def test_list_oracle_rejects_mutated_actual_http_output(reminder_http, monkeypatch, fault) -> None:
    ctx = reminder_http
    first = _seed_event(ctx, _ALICE, "R10 First", 0)
    second = _seed_event(ctx, _ALICE, "R10 Second", 1)
    foreign = _seed_event(ctx, _BOB, "R10 HTTP Foreign", 0)
    expected = [_expected_item(ctx, first), _expected_item(ctx, second)]
    original = TestClient.request

    def damaged_request(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if method.upper() != "GET" or str(url).split("?", 1)[0] != "/api/me/reminders":
            return response
        body = copy.deepcopy(response.json())
        if fault == "order":
            body["items"].reverse()
        elif fault == "foreign-id":
            body["items"][0]["id"] = foreign["id"]
        else:
            body["items"][0]["state"] = "dismissed"
        return httpx.Response(response.status_code, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", damaged_request)
    response = _bridge_get(
        ctx.client,
        ctx.settings,
        "/api/me/reminders?limit=100",
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    with pytest.raises(AssertionError, match="reminder_list_exact"):
        _assert_list(response, expected)


@pytest.mark.parametrize("fault", ["queued-write-missing", "prescan-write-missing"])
def test_dismiss_oracle_rejects_http_success_without_persisted_effect(
    reminder_http, monkeypatch, fault
) -> None:
    ctx = reminder_http
    event = _seed_event(ctx, _ALICE, f"R10 {fault}", 0)
    if fault == "queued-write-missing":
        row = _enqueue(ctx, event)
        target = str(row["id"])
        monkeypatch.setattr(ctx.storage, "dismiss_notification", lambda *_args, **_kwargs: True)
    else:
        target = event["id"]
        monkeypatch.setattr(ctx.storage, "silence_reminder", lambda *_args, **_kwargs: True)

    response = _bridge_json(
        ctx.client,
        ctx.settings,
        "POST",
        f"/api/me/reminders/{target}/dismiss",
        {},
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )
    assert response.status_code == 200 and response.json()["dismissed"] is True
    with pytest.raises(AssertionError, match="reminder_dismiss_persistence"):
        _assert_dismissed(ctx, event)


class _TransportResponse:
    status_code = 200
    headers: dict[str, str] = {}
    text = '{"ok":true}'

    def json(self) -> dict[str, Any]:
        return {"ok": True, "result": {"message_id": 9001}}

    def raise_for_status(self) -> None:
        return None


class _RecordingTelegram:
    def __init__(self, fault: str = "", *, drop_text_contains: str = "") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.dropped: list[tuple[str, dict[str, Any]]] = []
        self.fault = fault
        self.drop_text_contains = drop_text_contains

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> _TransportResponse:
        payload = copy.deepcopy(json if json is not None else kwargs)
        if self.fault and url.endswith("/sendMessage") and payload.get("reply_markup"):
            if self.fault == "button-missing":
                payload.pop("reply_markup", None)
            elif self.fault == "button-foreign":
                payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] = (
                    f"remind:dismiss:{_MISSING_ID}"
                )
        if (
            self.drop_text_contains
            and url.endswith("/sendMessage")
            and self.drop_text_contains in str(payload.get("text") or "")
        ):
            self.dropped.append((url, payload))
            return _TransportResponse()
        self.calls.append((url, payload))
        return _TransportResponse()


class _ForwardBackend:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        response = self.client.request(method, target, content=content, headers=headers)
        self.calls.append(
            {
                "method": method.upper(),
                "path": target,
                "status": response.status_code,
                "body": json.loads(content) if content else None,
            }
        )
        return response


def _bridge(ctx: _ReminderHTTP, tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:controlled",
            bridge_secret=ctx.settings.telegram_bridge_secret,
            backend_url="http://friday.test",
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram-reminder-oracle.sqlite3"),
        )
    )


def _message_update(update_id: int, *, chat_id: int = 5001) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Alice"},
            "text": "/reminders",
        },
    }


def _callback_data_update(update_id: int, data: str, *, chat_id: int = 5001) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": chat_id, "first_name": "Alice"},
            "data": data,
            "message": {
                "message_id": update_id + 200,
                "chat": {"id": chat_id, "type": "private"},
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Снять", "callback_data": data}],
                        [copy.deepcopy(_UNRELATED_BUTTON)],
                    ]
                },
            },
        },
    }


def _callback_update(update_id: int, target: str, *, chat_id: int = 5001) -> dict[str, Any]:
    return _callback_data_update(update_id, f"remind:dismiss:{target}", chat_id=chat_id)


def _assert_telegram_cards(calls: list[tuple[str, dict[str, Any]]], expected: list[dict[str, str]]) -> None:
    messages = [payload for url, payload in calls if url.endswith("/sendMessage")]
    assert len(messages) == len(expected) + 1, "telegram_reminder_message_count"
    assert messages[0]["text"] == (
        f"Предстоящие напоминания: {len(expected)}. «Снять» отменяет одно; повторный скан его не вернёт."
    ), "telegram_reminder_heading"
    observed = []
    for payload in messages[1:]:
        keyboard = (payload.get("reply_markup") or {}).get("inline_keyboard")
        assert keyboard and len(keyboard) == 1 and len(keyboard[0]) == 1, "telegram_reminder_button_shape"
        button = keyboard[0][0]
        observed.append((payload["text"], button.get("text"), button.get("callback_data")))
    wanted = [(item["body"], "Снять", f"remind:dismiss:{item['id']}") for item in expected]
    assert observed == wanted, "telegram_reminder_exact_cards"


def _assert_reminder_markup_retired(
    calls: list[tuple[str, dict[str, Any]]], *, update_id: int, chat_id: int = 5001
) -> None:
    edits = [payload for url, payload in calls if url.endswith("/editMessageReplyMarkup")]
    assert edits == [
        {
            "chat_id": chat_id,
            "message_id": update_id + 200,
            "reply_markup": {"inline_keyboard": [[_UNRELATED_BUTTON]]},
        }
    ], "telegram_reminder_markup_retirement"


def _seed_long_id_reminder(ctx: _ReminderHTTP) -> dict[str, str]:
    long_id = "ent_" + ("a" * 46)
    assert len(f"remind:dismiss:{long_id}".encode()) == 65
    ctx.storage.create_entity(
        Entity(
            id=long_id,
            user_id=_ALICE,
            name="R10 Long Reminder Identifier",
            entity_type=EntityType.EVENT,
        )
    )
    ctx.graph.set_event_time(
        _ALICE,
        long_id,
        ctx.today.isoformat(),
        source=f"reminder:{_ALICE}",
    )
    return {
        "id": long_id,
        "name": "R10 Long Reminder Identifier",
        "occurred_at": ctx.today.isoformat(),
        "dedup_key": f"reminder:{long_id}:{ctx.today.isoformat()}",
        "person": _ALICE,
    }


def _observe_long_id_cards(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    long_item: dict[str, str],
    sibling_item: dict[str, str],
) -> dict[str, Any]:
    messages = [payload for url, payload in calls if url.endswith("/sendMessage")]
    heading = "Предстоящие напоминания: 2. «Снять» отменяет одно; повторный скан его не вернёт."
    assert sum(payload.get("text") == heading for payload in messages) == 1, "telegram_reminder_heading"
    long_cards = [payload for payload in messages if long_item["body"] in str(payload.get("text") or "")]
    assert len(long_cards) == 1, "telegram_long_reminder_body_visible"
    sibling_cards = [
        payload for payload in messages if sibling_item["body"] in str(payload.get("text") or "")
    ]
    assert len(sibling_cards) == 1, "telegram_valid_sibling_body_visible"

    def callback_buttons(payload: dict[str, Any]) -> list[dict[str, Any]]:
        keyboard = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
        return [
            button
            for row in keyboard
            if isinstance(row, list)
            for button in row
            if isinstance(button, dict) and "callback_data" in button
        ]

    sibling_callback = f"remind:dismiss:{sibling_item['id']}"
    assert callback_buttons(sibling_cards[0]) == [{"text": "Снять", "callback_data": sibling_callback}], (
        "telegram_valid_sibling_action"
    )
    long_buttons = callback_buttons(long_cards[0])
    assert len(long_buttons) <= 1, "telegram_long_reminder_action_count"
    if long_buttons:
        assert long_buttons[0].get("text") == "Снять", "telegram_long_reminder_action_kind"
    callbacks = [str(button["callback_data"]) for payload in messages for button in callback_buttons(payload)]
    return {
        "callbacks": callbacks,
        "sibling_callback": sibling_callback,
        "long_callback": str(long_buttons[0]["callback_data"]) if long_buttons else None,
    }


@pytest.mark.asyncio
async def test_telegram_reminder_buttons_never_exceed_the_transport_limit(reminder_http, tmp_path) -> None:
    """Long body stays visible; every offered action is byte-safe and real."""

    ctx = reminder_http
    long_event = _seed_long_id_reminder(ctx)
    sibling = _seed_event(ctx, _ALICE, "R10 Valid Reminder Sibling", 0)
    long_item = _expected_item(ctx, long_event)
    sibling_item = _expected_item(ctx, sibling)
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(telegram, backend, _message_update(91), cached_response=None)
        observed = _observe_long_id_cards(
            telegram.calls,
            long_item=long_item,
            sibling_item=sibling_item,
        )

        telegram.calls.clear()
        backend.calls.clear()
        await bridge._process_update(
            telegram,
            backend,
            _callback_data_update(92, observed["sibling_callback"]),
            cached_response=None,
        )
        assert backend.calls == [
            {
                "method": "POST",
                "path": f"/api/me/reminders/{sibling['id']}/dismiss",
                "status": 200,
                "body": {"telegram_user": {"id": 5001, "first_name": "Alice"}},
            }
        ], "telegram_valid_sibling_action"
        _assert_dismissed(ctx, sibling)
        assert ctx.storage.reminder_states(_ALICE, [long_event["dedup_key"]]) == {}

        assert all(len(value.encode()) <= 64 for value in observed["callbacks"]), (
            "telegram_callback_data_limit"
        )
        if observed["long_callback"] is not None:
            telegram.calls.clear()
            backend.calls.clear()
            await bridge._process_update(
                telegram,
                backend,
                _callback_data_update(93, observed["long_callback"]),
                cached_response=None,
            )
            assert len(backend.calls) == 1 and backend.calls[0]["status"] == 200, (
                "telegram_long_reminder_action_works"
            )
            _assert_dismissed(ctx, long_event)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_telegram_long_reminder_oracle_rejects_actual_dropped_body(reminder_http, tmp_path) -> None:
    ctx = reminder_http
    long_event = _seed_long_id_reminder(ctx)
    sibling = _seed_event(ctx, _ALICE, "R10 Valid Reminder Sibling", 0)
    long_item = _expected_item(ctx, long_event)
    sibling_item = _expected_item(ctx, sibling)
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram(drop_text_contains=long_item["body"])
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(telegram, backend, _message_update(94), cached_response=None)
        assert len(telegram.dropped) == 1
        assert long_item["body"] in str(telegram.dropped[0][1].get("text") or "")
        with pytest.raises(AssertionError, match="telegram_long_reminder_body_visible"):
            _observe_long_id_cards(
                telegram.calls,
                long_item=long_item,
                sibling_item=sibling_item,
            )
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_telegram_reminders_actual_command_renders_own_ids_and_dismisses_via_http(
    reminder_http, tmp_path
) -> None:
    ctx = reminder_http
    first = _seed_event(ctx, _ALICE, "R10 Telegram Alpha", 0)
    second = _seed_event(ctx, _ALICE, "R10 Telegram Beta", 1)
    foreign = _seed_event(ctx, _BOB, "R10 Telegram Foreign Canary", 0)
    expected = [_expected_item(ctx, first), _expected_item(ctx, second)]
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(telegram, backend, _message_update(101), cached_response=None)
        _assert_telegram_cards(telegram.calls, expected)
        assert backend.calls == [
            {
                "method": "GET",
                "path": "/api/me/reminders?limit=10",
                "status": 200,
                "body": {"telegram_user": {"id": 5001, "first_name": "Alice"}},
            }
        ]
        encoded = json.dumps(telegram.calls, ensure_ascii=False)
        assert foreign["id"] not in encoded and foreign["name"] not in encoded

        telegram.calls.clear()
        await bridge._process_update(
            telegram,
            backend,
            _callback_update(102, first["id"]),
            cached_response=None,
        )
        assert backend.calls[-1]["method"] == "POST"
        assert backend.calls[-1]["path"] == f"/api/me/reminders/{first['id']}/dismiss"
        assert backend.calls[-1]["status"] == 200
        _assert_dismissed(ctx, first)
        callbacks = [payload for url, payload in telegram.calls if url.endswith("/answerCallbackQuery")]
        assert callbacks == [{"callback_query_id": "callback-102", "text": "Снято", "show_alert": False}]
        _assert_reminder_markup_retired(telegram.calls, update_id=102)
        assert any(
            url.endswith("/sendMessage") and payload.get("text") == "Напоминание снято."
            for url, payload in telegram.calls
        )
    finally:
        bridge._inbox.close()


@pytest.mark.parametrize("target_kind", ["foreign", "missing"])
@pytest.mark.asyncio
async def test_telegram_reminder_callback_refuses_unowned_ids_without_cross_person_effect(
    reminder_http, tmp_path, target_kind
) -> None:
    ctx = reminder_http
    foreign = _seed_event(ctx, _BOB, "R10 Telegram Refusal Canary", 0)
    _enqueue(ctx, foreign)
    target = foreign["id"] if target_kind == "foreign" else _MISSING_ID
    before = _relevant_snapshot(ctx.storage)
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(
            telegram,
            backend,
            _callback_update(201, target),
            cached_response=None,
        )
        assert backend.calls == [
            {
                "method": "POST",
                "path": f"/api/me/reminders/{target}/dismiss",
                "status": 404,
                "body": {"telegram_user": {"id": 5001, "first_name": "Alice"}},
            }
        ]
        callbacks = [payload for url, payload in telegram.calls if url.endswith("/answerCallbackQuery")]
        assert callbacks == [
            {
                "callback_query_id": "callback-201",
                "text": "Действие уже недоступно",
                "show_alert": True,
            }
        ]
        _assert_reminder_markup_retired(telegram.calls, update_id=201)
        assert not any(
            url.endswith("/sendMessage") and payload.get("text") == "Напоминание снято."
            for url, payload in telegram.calls
        )
        assert _relevant_snapshot(ctx.storage) == before
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_telegram_reminder_callback_preserves_backend_forbidden_refusal(
    reminder_http, tmp_path
) -> None:
    ctx = reminder_http
    own = _seed_event(ctx, _ALICE, "R10 Telegram Forbidden", 0)
    ctx.storage.upsert_custom_preset(
        "r10_no_chat",
        "No chat",
        set(),
        created_by=_ALICE,
    )
    ctx.storage.update_user(_ALICE, preset_key="r10_no_chat")
    before = _relevant_snapshot(ctx.storage)
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(
            telegram,
            backend,
            _callback_update(251, own["id"]),
            cached_response=None,
        )
        assert backend.calls == [
            {
                "method": "POST",
                "path": f"/api/me/reminders/{own['id']}/dismiss",
                "status": 403,
                "body": {"telegram_user": {"id": 5001, "first_name": "Alice"}},
            }
        ]
        callbacks = [payload for url, payload in telegram.calls if url.endswith("/answerCallbackQuery")]
        assert callbacks == [
            {
                "callback_query_id": "callback-251",
                "text": "Смотреть объекты вам сейчас не разрешено — попросите доступ у владельца.",
                "show_alert": True,
            }
        ]
        _assert_reminder_markup_retired(telegram.calls, update_id=251)
        assert not any(
            url.endswith("/sendMessage") and payload.get("text") == "Напоминание снято."
            for url, payload in telegram.calls
        )
        assert _relevant_snapshot(ctx.storage) == before
    finally:
        bridge._inbox.close()


@pytest.mark.parametrize("update_kind", ["command", "callback"])
@pytest.mark.asyncio
async def test_telegram_reminders_refuse_an_unallowed_chat_before_backend(
    reminder_http, tmp_path, update_kind
) -> None:
    ctx = reminder_http
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    update = (
        _message_update(301, chat_id=5999)
        if update_kind == "command"
        else _callback_update(301, _MISSING_ID, chat_id=5999)
    )
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
        assert backend.calls == []
        if update_kind == "command":
            assert telegram.calls == []
        else:
            assert telegram.calls == [
                (
                    "https://api.telegram.org/bot123:controlled/answerCallbackQuery",
                    {
                        "callback_query_id": "callback-301",
                        "text": "Действие недоступно",
                        "show_alert": True,
                    },
                )
            ]
    finally:
        bridge._inbox.close()


@pytest.mark.parametrize("fault", ["button-missing", "button-foreign"])
@pytest.mark.asyncio
async def test_telegram_reminder_oracle_rejects_mutated_transport_output(
    reminder_http, tmp_path, fault
) -> None:
    ctx = reminder_http
    own = _seed_event(ctx, _ALICE, "R10 Telegram Output", 0)
    expected = [_expected_item(ctx, own)]
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram(fault)
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(telegram, backend, _message_update(401), cached_response=None)
        with pytest.raises(
            AssertionError,
            match="telegram_reminder_(button_shape|exact_cards)",
        ):
            _assert_telegram_cards(telegram.calls, expected)
    finally:
        bridge._inbox.close()


def test_visible_reminder_limit_skips_a_run_of_dismissed_rows(reminder_http) -> None:
    """The HTTP limit is applied to visible reminders, even after a hidden prefix."""

    ctx = reminder_http
    for index in range(12):
        hidden = _seed_event(ctx, _ALICE, f"R10 A Hidden {index:02d}", 0)
        _enqueue(ctx, hidden, state="dismissed")
    first = _seed_event(ctx, _ALICE, "R10 Y Visible First", 0)
    second = _seed_event(ctx, _ALICE, "R10 Z Visible Second", 0)

    response = _bridge_get(
        ctx.client,
        ctx.settings,
        "/api/me/reminders?limit=2",
        user=_ALICE_EXTERNAL,
        chat=_ALICE_EXTERNAL,
    )

    _assert_list(response, [_expected_item(ctx, first), _expected_item(ctx, second)])


def test_reminder_visibility_filter_is_opt_in_person_scoped_and_privacy_safe(
    reminder_http,
) -> None:
    """Only an eligible tombstone owned by this person hides a timeline event."""

    from friday.storage._graph import _bounded_visible_timeline_event_rows

    ctx = reminder_http
    own = _seed_event(ctx, _ALICE, "R10 Visibility Canary", 0)
    foreign_private = _seed_event(ctx, _BOB, "R10 Foreign Privacy Canary", 0)
    with ctx.storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO outbound_notifications(
                   id,user_id,chat_id,kind,dedup_key,body,status,attempts,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (
                    "notif_r10_foreign_person_shadow",
                    _BOB,
                    _BOB_EXTERNAL,
                    "reminder",
                    own["dedup_key"],
                    "foreign person shadow",
                    "dismissed",
                    0,
                    ctx.now.isoformat(),
                ),
                (
                    "notif_r10_private_dependency_shadow",
                    _ALICE,
                    "5999",
                    "reminder",
                    own["dedup_key"],
                    f"private dependency {foreign_private['id']}",
                    "dismissed",
                    0,
                    ctx.now.isoformat(),
                ),
            ],
        )
    query = {
        "start": ctx.today.isoformat(),
        "end": ctx.today.isoformat(),
        "limit": 2,
    }
    visible = _bounded_visible_timeline_event_rows(
        ctx.storage,
        _ALICE,
        _ALICE,
        exclude_dismissed_reminders=True,
        **query,
    )
    assert [row["entity_id"] for row in visible] == [own["id"]]

    with ctx.storage.transaction() as conn:
        conn.execute(
            "DELETE FROM outbound_notifications WHERE id IN (?,?)",
            ("notif_r10_foreign_person_shadow", "notif_r10_private_dependency_shadow"),
        )
    _enqueue(ctx, own, state="dismissed")

    generic = _bounded_visible_timeline_event_rows(ctx.storage, _ALICE, _ALICE, **query)
    filtered = _bounded_visible_timeline_event_rows(
        ctx.storage,
        _ALICE,
        _ALICE,
        exclude_dismissed_reminders=True,
        **query,
    )
    assert [row["entity_id"] for row in generic] == [own["id"]]
    assert filtered == []


@pytest.mark.asyncio
async def test_telegram_reminder_callback_at_exact_transport_limit_stays_actionable(
    reminder_http, tmp_path
) -> None:
    """A 64-byte callback remains present and reaches the real dismiss route."""

    ctx = reminder_http
    entity_id = "ent_" + ("b" * 45)
    callback_data = f"remind:dismiss:{entity_id}"
    assert len(callback_data.encode()) == 64
    ctx.storage.create_entity(
        Entity(
            id=entity_id,
            user_id=_ALICE,
            name="R10 Exact Callback Boundary",
            entity_type=EntityType.EVENT,
        )
    )
    ctx.graph.set_event_time(
        _ALICE,
        entity_id,
        ctx.today.isoformat(),
        source=f"reminder:{_ALICE}",
    )
    event = {
        "id": entity_id,
        "name": "R10 Exact Callback Boundary",
        "occurred_at": ctx.today.isoformat(),
        "dedup_key": f"reminder:{entity_id}:{ctx.today.isoformat()}",
        "person": _ALICE,
    }
    expected = _expected_item(ctx, event)
    bridge = _bridge(ctx, tmp_path)
    telegram = _RecordingTelegram()
    backend = _ForwardBackend(ctx.client)
    try:
        await bridge._process_update(telegram, backend, _message_update(1201), cached_response=None)
        _assert_telegram_cards(telegram.calls, [expected])
        messages = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        button = messages[1]["reply_markup"]["inline_keyboard"][0][0]
        assert button["callback_data"] == callback_data

        telegram.calls.clear()
        backend.calls.clear()
        await bridge._process_update(
            telegram,
            backend,
            _callback_data_update(1202, callback_data),
            cached_response=None,
        )
        assert backend.calls == [
            {
                "method": "POST",
                "path": f"/api/me/reminders/{entity_id}/dismiss",
                "status": 200,
                "body": {"telegram_user": {"id": 5001, "first_name": "Alice"}},
            }
        ]
        _assert_dismissed(ctx, event)
    finally:
        bridge._inbox.close()
