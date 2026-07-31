"""G18: /archive, /delete (with confirm), /rename in Telegram.

Backend already had archive/delete (and now rename); these tests pin that the
bridge actually calls them for the current channel conversation.
"""

from __future__ import annotations

import pytest

from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient, _media_bridge


@pytest.mark.asyncio
async def test_archive_delete_rename_commands_hit_current_conversation(tmp_path):
    """Mutation: drop the /archive branch → no POST .../current/archive, red.

    Delete must show confirm buttons first; confirm callback issues DELETE.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/me": {"actor": {"preset_key": "user"}, "user": {}},
            "/api/conversations/current/archive": {
                "conversation": {"id": "conv_1", "title": "Рабочий", "is_archived": 1}
            },
            "/api/conversations/current": {
                "conversation": {"id": "conv_1", "title": "Новое имя"}
            },
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
                    "text": "/archive",
                },
            },
            cached_response=None,
        )
        assert any(
            call["method"] == "POST" and call["path"] == "/api/conversations/current/archive"
            for call in backend.calls
        ), backend.calls
        assert any(
            "архивирован" in (payload.get("text") or "").casefold()
            for url, payload in telegram.calls
            if url.endswith("/sendMessage")
        )

        telegram.calls.clear()
        backend.calls.clear()
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/delete",
                },
            },
            cached_response=None,
        )
        assert not any(call["method"] == "DELETE" for call in backend.calls)
        cards = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert cards, telegram.calls
        keyboard = cards[-1]["reply_markup"]["inline_keyboard"][0]
        assert {button["callback_data"] for button in keyboard} == {
            "conv:delete:current",
            "conv:keep:current",
        }

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cb-del",
                    "from": user,
                    "data": "conv:delete:current",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        assert any(
            call["method"] == "DELETE" and call["path"] == "/api/conversations/current"
            for call in backend.calls
        ), backend.calls

        telegram.calls.clear()
        backend.calls.clear()
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 4,
                "message": {
                    "message_id": 12,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/rename План на квартал",
                },
            },
            cached_response=None,
        )
        rename_calls = [
            call
            for call in backend.calls
            if call["method"] == "PATCH" and call["path"] == "/api/conversations/current"
        ]
        assert rename_calls, backend.calls
        assert rename_calls[0]["body"]["title"] == "План на квартал"
        assert any(
            "переименован" in (payload.get("text") or "").casefold()
            for url, payload in telegram.calls
            if url.endswith("/sendMessage")
        )
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_delete_keep_callback_does_not_call_backend(tmp_path):
    """Mutation: treat keep as delete → unexpected DELETE call, red."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me": {"actor": {"preset_key": "user"}, "user": {}}})
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb-keep",
                    "from": user,
                    "data": "conv:keep:current",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        assert not any(call["method"] == "DELETE" for call in backend.calls)
        assert any(
            "отменено" in (payload.get("text") or "").casefold()
            for url, payload in telegram.calls
            if url.endswith("/sendMessage")
        )
    finally:
        bridge._inbox.close()
