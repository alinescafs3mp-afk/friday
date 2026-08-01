"""A failed ack re-sends everything the bridge just delivered.

`_drain_outbound` delivers up to twenty notifications and then reports the whole
batch in ONE ack. Delivery state lives only in a local list until that ack lands,
so a single failed ack leaves every message `pending` — and the user receives all
twenty again fifteen seconds later, and again, until an ack succeeds.

The ack is retried in place. Per-message acking would shrink the window to one
message and is deliberately not done: the bridge signs its service calls as the
owner, so they count against `telegram:user:<owner>` (30/minute by default,
shared with the owner's own messages), and twenty acks per drain would spend that
budget on bookkeeping.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig


class _Telegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if url.endswith("/sendMessage"):
            self.sent.append(str((kwargs.get("json") or {}).get("text", "")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=request)


class _Backend:
    """Pending returns two notifications; ack fails `ack_failures` times first."""

    def __init__(self, *, ack_failures: int) -> None:
        self.ack_failures = ack_failures
        self.ack_attempts = 0
        self.acked: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if "/api/notifications/pending" in url:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "notif_1", "chat_id": "5001", "body": "первое"},
                        {"id": "notif_2", "chat_id": "5001", "body": "второе"},
                    ],
                    "count": 2,
                },
                request=request,
            )
        if "/api/notifications/ack" in url:
            self.ack_attempts += 1
            if self.ack_attempts <= self.ack_failures:
                return httpx.Response(503, json={"detail": "backend restarting"}, request=request)
            # The bridge signs the exact bytes it sends, so it passes `content`,
            # not `json` — reading the raw body is what the real backend does too.
            payload = json.loads(kwargs.get("content") or b"{}")
            self.acked.extend(payload.get("sent") or [])
            return httpx.Response(200, json={"sent": 2, "failed": 0}, request=request)
        return httpx.Response(200, json={}, request=request)


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


@pytest.mark.asyncio
async def test_a_transient_ack_failure_does_not_lose_the_batch(tmp_path):
    """Two failures then success: the messages are acked, not re-delivered."""
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend(ack_failures=2)
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram.sent == ["первое", "второе"]  # delivered exactly once
    assert backend.ack_attempts == 3  # two failures, then the retry that landed
    assert backend.acked == ["notif_1", "notif_2"]


@pytest.mark.asyncio
async def test_an_unacked_batch_is_reported_loudly_not_silently(tmp_path, caplog):
    """When every retry fails, the duplicate delivery to come must be visible."""
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _Backend(ack_failures=99)
    with caplog.at_level("ERROR"):
        try:
            # The drain must return rather than raise: the messages ARE delivered,
            # and an exception here would only restart the same loop.
            await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
        finally:
            bridge._inbox.close()  # noqa: SLF001

    assert backend.ack_attempts == 3
    assert any("re-sent" in record.message for record in caplog.records), caplog.text


class _RegisteredChatBackend:
    """One pending notification, targeting a chat outside the static allowlist."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.acked: list[str] = []

    async def request(self, method: str, url: str, **kwargs):
        import httpx as _httpx

        request = _httpx.Request(method, url)
        if "/api/notifications/pending" in url:
            return _httpx.Response(
                200,
                json={"items": [{"id": "notif_1", "chat_id": self.chat_id, "body": "привет"}], "count": 1},
                request=request,
            )
        if "/api/notifications/ack" in url:
            payload = json.loads(kwargs.get("content") or b"{}")
            self.acked.extend(payload.get("sent") or [])
            return _httpx.Response(200, json={"sent": 1, "failed": 0}, request=request)
        return _httpx.Response(200, json={}, request=request)


@pytest.mark.asyncio
async def test_outbound_reaches_a_chat_open_registration_already_admitted(tmp_path):
    """The send-edge re-check (`_drain_outbound`) must recognise `registered_chats`,
    not just the static allowlist -- otherwise a self-registered account (open
    registration) would never receive a single proactive push: every organ's
    delivery routes through this exact check."""
    bridge = _bridge(tmp_path)
    bridge._inbox.remember_registered_chat(7777)  # noqa: SLF001
    telegram, backend = _Telegram(), _RegisteredChatBackend(chat_id="7777")
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram.sent == ["привет"], "push to a registered (non-allowlisted) chat was refused"
    assert backend.acked == ["notif_1"]


@pytest.mark.asyncio
async def test_outbound_still_refuses_an_unregistered_stranger(tmp_path):
    """The other half of the same check: a chat that was never admitted (neither
    statically allowed nor open-registration-registered) must still be refused at
    the send edge -- this re-check exists precisely to catch a backend bug that
    picks the wrong chat_id, and loosening it for everyone would defeat that."""
    bridge = _bridge(tmp_path)
    telegram, backend = _Telegram(), _RegisteredChatBackend(chat_id="9999")
    try:
        await bridge._drain_outbound(telegram, backend)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert telegram.sent == []
    assert backend.acked == [], "an unregistered chat received a push"
