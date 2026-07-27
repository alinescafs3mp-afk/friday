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

from jericho.telegram_bridge import TelegramBridge, TelegramConfig


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
