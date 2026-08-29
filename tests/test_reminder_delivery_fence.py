"""At-most-once Telegram delivery for a claimed durable reminder."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig, _UpdateInbox
from friday.telegram_bridge import _transport as bridge_transport


class _HardCrash(BaseException):
    pass


def _reminder_item() -> dict[str, Any]:
    return {
        "id": "notif_reminder_fence_1",
        "chat_id": "5001",
        "kind": "reminder",
        "dedup_key": "reminder:event_fence_1:2026-08-29",
        "body": "🔔 Пора проверить отчёт.",
    }


def _empty_states() -> dict[str, list[str]]:
    return {
        "sent": [],
        "failed": [],
        "uncertain": [],
        "pending": [],
        "dismissed": [],
        "missing": [],
        "unconfirmed": [],
    }


class _StatefulBackend:
    """Minimal pending -> uncertain claim and proof-bearing ACK state machine."""

    def __init__(self, item: dict[str, Any], *, ack_response_losses: int = 0) -> None:
        self.item = dict(item)
        self.status = "pending"
        self.claim_calls = 0
        self.ack_attempts = 0
        self.ack_response_losses = ack_response_losses
        self.acks: list[dict[str, Any]] = []

    @property
    def pointer(self) -> dict[str, Any]:
        return {key: self.item[key] for key in ("id", "chat_id", "kind", "dedup_key")}

    def _state_ids(self, requested: list[str]) -> dict[str, list[str]]:
        states = _empty_states()
        for notification_id in dict.fromkeys(requested):
            if notification_id != self.item["id"]:
                states["missing"].append(notification_id)
            elif self.status in states:
                states[self.status].append(notification_id)
            else:
                states["unconfirmed"].append(notification_id)
        return states

    def _apply_ack(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        notification_id = str(self.item["id"])
        sent = payload.get("sent") or []
        failed = payload.get("failed") or []
        uncertain = payload.get("uncertain") or []
        if notification_id in sent and self.status == "uncertain":
            self.status = "sent"
        elif notification_id in failed and self.status == "uncertain":
            self.status = "pending"
        requested = [str(value) for values in (sent, failed, uncertain) for value in values]
        return self._state_ids(requested)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/api/notifications/pending" in url:
            items = [self.pointer] if self.status == "pending" else []
            return httpx.Response(
                200,
                json={"items": items, "count": len(items)},
                request=request,
            )
        if url.endswith(f"/api/notifications/{self.item['id']}/claim"):
            self.claim_calls += 1
            payload = json.loads(kwargs.get("content") or b"{}")
            assert payload == {**self.pointer, "status_messages": True}
            if self.status != "pending":
                return httpx.Response(404, json={"detail": "unavailable"}, request=request)
            # This is the backend's durable pre-effect boundary.  The response
            # can be lost or the bridge can die from this point onward without
            # making the body claimable a second time.
            self.status = "uncertain"
            return httpx.Response(200, json={"item": self.item}, request=request)
        if url.endswith("/api/notifications/ack"):
            self.ack_attempts += 1
            payload = json.loads(kwargs.get("content") or b"{}")
            assert set(payload) in ({"sent", "failed"}, {"sent", "failed", "uncertain"})
            states = self._apply_ack(payload)
            self.acks.append(payload)
            if self.ack_response_losses > 0:
                self.ack_response_losses -= 1
                raise httpx.ReadTimeout("committed ACK response lost", request=request)
            return httpx.Response(200, json={"state_ids": states}, request=request)
        return httpx.Response(404, request=request)


class _Telegram:
    def __init__(self, *, failure: str = "") -> None:
        self.failure = failure
        self.attempts = 0
        self.accepted: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/sendMessage")
        self.attempts += 1
        request = httpx.Request("POST", url)
        if self.failure == "connect":
            raise httpx.ConnectError("not connected", request=request)
        self.accepted.append(str((kwargs.get("json") or {}).get("text") or ""))
        if self.failure == "read_timeout":
            raise httpx.ReadTimeout("accepted response lost", request=request)
        if self.failure == "hard_crash":
            raise _HardCrash
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 77}},
            request=request,
        )


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            max_document_bytes=1024,
        )
    )


@pytest.fixture(autouse=True)
def _no_transport_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(bridge_transport.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_success_claims_once_sends_once_and_settles_sent(tmp_path) -> None:
    item = _reminder_item()
    backend = _StatefulBackend(item)
    telegram = _Telegram()
    bridge = _bridge(tmp_path)
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001

        assert telegram.accepted == [item["body"]]
        assert backend.status == "sent"
        assert backend.claim_calls == 1
        assert backend.acks == [{"sent": [item["id"]], "failed": []}]
        assert bridge._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_read_timeout_after_acceptance_is_uncertain_and_never_replayed(tmp_path) -> None:
    item = _reminder_item()
    backend = _StatefulBackend(item)
    first = _bridge(tmp_path)
    telegram = _Telegram(failure="read_timeout")
    await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert telegram.accepted == [item["body"]]
    assert backend.status == "uncertain"
    assert backend.acks == [{"sent": [], "failed": [], "uncertain": [item["id"]]}]
    first._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    replay = _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.attempts == 0
        assert backend.claim_calls == 1
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_hard_process_death_after_possible_acceptance_never_replays(tmp_path) -> None:
    item = _reminder_item()
    backend = _StatefulBackend(item)
    first = _bridge(tmp_path)
    telegram = _Telegram(failure="hard_crash")
    with pytest.raises(_HardCrash):
        await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert telegram.accepted == [item["body"]]
    assert backend.status == "uncertain"
    assert backend.acks == []
    first._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    replay = _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.attempts == 0
        assert backend.claim_calls == 1
        assert backend.status == "uncertain"
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_connect_error_is_proven_pre_accept_and_retries_once(tmp_path) -> None:
    item = _reminder_item()
    backend = _StatefulBackend(item)
    bridge = _bridge(tmp_path)
    rejected = _Telegram(failure="connect")
    try:
        await bridge._drain_outbound(rejected, backend)  # noqa: SLF001
        assert rejected.attempts == 1
        assert rejected.accepted == []
        assert backend.status == "pending"
        assert backend.acks == [{"sent": [], "failed": [item["id"]]}]

        delivered = _Telegram()
        await bridge._drain_outbound(delivered, backend)  # noqa: SLF001
        assert delivered.accepted == [item["body"]]
        assert backend.claim_calls == 2
        assert backend.status == "sent"
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_reconciled_reminder_outcome_is_complete_in_one_local_commit(tmp_path) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = _UpdateInbox(path)
    first.remember_notification_delivery_reconciled_outcome("notif_atomic", "sent")
    assert first.notification_delivery_outcomes() == {"notif_atomic": "sent"}
    assert first.notification_delivery_reconciled_outcomes() == {"notif_atomic": "sent"}
    first.remember_notification_delivery_reconciled_outcome("notif_atomic", "uncertain")
    assert first.notification_delivery_reconciled_outcomes() == {"notif_atomic": "uncertain"}
    first.close()

    restarted = _UpdateInbox(path)
    try:
        assert restarted.notification_delivery_outcomes() == {"notif_atomic": "uncertain"}
        assert restarted.notification_delivery_reconciled_outcomes() == {"notif_atomic": "uncertain"}
        restarted.remember_notification_delivery_reconciled_outcome("notif_atomic", "sent")
        assert restarted.notification_delivery_outcomes() == {"notif_atomic": "uncertain"}
        assert restarted.notification_delivery_reconciled_outcomes() == {"notif_atomic": "uncertain"}
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_crash_after_atomic_reminder_outcome_reacks_without_resend(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _reminder_item()
    backend = _StatefulBackend(item)
    first = _bridge(tmp_path)
    telegram = _Telegram()
    persist = first._inbox.remember_notification_delivery_reconciled_outcome  # noqa: SLF001

    def crash_after_commit(notification_id: str, outcome: str) -> None:
        persist(notification_id, outcome)
        raise _HardCrash

    monkeypatch.setattr(
        first._inbox,  # noqa: SLF001
        "remember_notification_delivery_reconciled_outcome",
        crash_after_commit,
    )
    with pytest.raises(_HardCrash):
        await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert telegram.accepted == [item["body"]]
    assert backend.status == "uncertain"
    assert backend.acks == []
    assert first._inbox.notification_delivery_reconciled_outcomes() == {  # noqa: SLF001
        item["id"]: "sent"
    }
    first._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    replay = _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.attempts == 0
        assert backend.claim_calls == 1
        assert backend.status == "sent"
        assert backend.acks == [{"sent": [item["id"]], "failed": []}]
        assert restarted._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        restarted._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_lost_ack_reacks_off_page_after_restart_without_resend(tmp_path) -> None:
    item = _reminder_item()
    backend = _StatefulBackend(item, ack_response_losses=3)
    first = _bridge(tmp_path)
    telegram = _Telegram()
    await first._drain_outbound(telegram, backend)  # noqa: SLF001
    assert telegram.accepted == [item["body"]]
    assert backend.status == "sent"
    assert backend.claim_calls == 1
    assert backend.ack_attempts == 3
    assert first._inbox.notification_delivery_reconciled_outcomes() == {  # noqa: SLF001
        item["id"]: "sent"
    }
    first._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    replay = _Telegram()
    try:
        await restarted._drain_outbound(replay, backend)  # noqa: SLF001
        assert replay.attempts == 0
        assert backend.claim_calls == 1
        assert backend.ack_attempts == 4
        assert backend.acks[-1] == {"sent": [item["id"]], "failed": []}
        assert backend.status == "sent"
        assert restarted._inbox.notification_delivery_ids() == set()  # noqa: SLF001
    finally:
        restarted._inbox.close()  # noqa: SLF001
