"""Acknowledging a button press must not be able to undo the press.

`_process_callback_query` runs the backend action first and answers the callback
second — correct order for the user (the toast should say what happened). But a
failed `answerCallbackQuery` used to raise, which marks the whole update
RETRYABLE, and the retry runs the backend action again. A callback id expires
after a while and Telegram then answers 400 «query is too old», so a single 👍
could re-POST `/api/feedback` on every retry for as long as the update was kept.

The visible damage was modest — `feedback_state` is upserted per (user, target,
type), so the rating itself does not drift — but the append-only audit table
filled with duplicates, and an action that already succeeded must not be replayed
because a toast failed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig


class _FlakyTelegram:
    """Every call succeeds except answerCallbackQuery, which 400s like an expired id."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(url)
        request = httpx.Request("POST", url)
        if url.endswith("/answerCallbackQuery"):
            return httpx.Response(400, json={"ok": False, "description": "query is too old"}, request=request)
        return httpx.Response(200, json={"ok": True, "result": {}}, request=request)


class _CountingBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(f"{method} {url}")
        request = httpx.Request(method, url)
        return httpx.Response(200, json={"feedback": {"id": "feedback_1"}}, request=request)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


@pytest.mark.asyncio
async def test_a_failed_ack_does_not_replay_the_action(tmp_path):
    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    telegram = _FlakyTelegram()
    backend = _CountingBackend()
    callback = {
        "id": "callback-expired",
        "from": {"id": 1001, "first_name": "Alice"},
        "data": "feedback:up:msg_777",
        "message": {"message_id": 99, "chat": {"id": 5001}},
    }
    try:
        # The contract: this returns rather than raising, so the update is
        # settled and never handed back to the retry loop.
        await bridge._process_callback_query(telegram, backend, callback)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    feedback_calls = [call for call in backend.calls if call.endswith("/api/feedback")]
    assert len(feedback_calls) == 1, f"the action ran {len(feedback_calls)} times"
    # The ack really was attempted and really did fail — otherwise this test
    # would pass without touching the behaviour it is about.
    assert any(url.endswith("/answerCallbackQuery") for url in telegram.calls)
