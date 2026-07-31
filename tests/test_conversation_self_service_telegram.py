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
            "/api/conversations/current": {"conversation": {"id": "conv_1", "title": "Новое имя"}},
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
            "conv:delete:current.1001",
            "conv:keep:current.1001",
        }

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cb-del",
                    "from": user,
                    "data": "conv:delete:current.1001",
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
async def test_a_different_group_member_cannot_confirm_someone_elses_delete(tmp_path):
    """Found by adversarial review: the confirm prompt used to carry no record
    of who /delete was shown to, so whichever OTHER capable account in a group
    tapped "Да, удалить" first deleted THEIR OWN current conversation, not the
    invoker's — silently, with a success message implying the invoker's
    conversation was the one removed.

    Mutation: drop the invoker-id check (accept any presser) → the DELETE call
    fires and this test goes red.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me": {"actor": {"preset_key": "user"}, "user": {}}})
    invoker = {"id": 1001, "first_name": "Alice"}
    other_member = {"id": 2002, "first_name": "Bob"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb-del-wrong-presser",
                    "from": other_member,
                    "data": f"conv:delete:current.{invoker['id']}",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        assert not any(call["method"] == "DELETE" for call in backend.calls), backend.calls
        assert any(
            "не для вас" in (payload.get("text") or "").casefold()
            for url, payload in telegram.calls
            if url.endswith("/answerCallbackQuery")
        ), telegram.calls
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
                    "data": "conv:keep:current.1001",
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
