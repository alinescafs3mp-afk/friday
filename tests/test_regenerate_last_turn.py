"""POST /api/me/regenerate — replay the last user turn through agent.chat.

G15: mainstream chat products have «regenerate»; Jericho only let you retype.
Self-service, chat.use, conversation resolved like /api/chat for Telegram.
Storage cannot branch alternate answers — a new user+assistant pair is appended.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from tests.test_api_vertical_slice import _bridge_json, _bridge_request


def test_regenerate_replays_last_user_message_not_an_earlier_one(settings):
    """Tail may hold two user turns without an assistant between them.

    Walking the window from the front would re-ask the OLD question; the
    endpoint must take the LAST role=user row. Mutation: change the scan to
    `for row in recent` (forward) — this test turns red.
    """
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        first = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "первый вопрос",
                "source_ref": "telegram-update:reg1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5001},
            },
        )
        assert first.status_code == 200, first.text
        conversation_id = first.json()["conversation_id"]

        second = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "второй вопрос — именно его надо повторить",
                "source_ref": "telegram-update:reg2",
                "telegram_message_id": 2,
                "telegram_user": {"id": 5001},
            },
        )
        assert second.status_code == 200, second.text

        # Drop assistant rows so the tail is pure user+user (the failure mode).
        storage = client.app.state.storage
        with storage.transaction() as conn:
            conn.execute(
                "DELETE FROM messages WHERE conversation_id=? AND role='assistant'",
                (conversation_id,),
            )

        seen: list[str] = []

        async def _spy(user_id, message, **kwargs):
            seen.append(message)
            return {
                "conversation_id": conversation_id,
                "message": {"role": "assistant", "content": f"echo:{message}"},
                "answer": f"echo:{message}",
                "context": {"interaction_mode": "dialogue"},
            }

        client.app.state.agent.chat = AsyncMock(side_effect=_spy)  # type: ignore[method-assign]

        response = _bridge_json(
            client,
            scoped,
            "POST",
            "/api/me/regenerate",
            {},
            user="5001",
            chat="5001",
        )
        assert response.status_code == 200, response.text
        assert seen == ["второй вопрос — именно его надо повторить"]
        assert response.json()["answer"] == "echo:второй вопрос — именно его надо повторить"


def test_regenerate_calls_agent_chat_and_empty_conversation_is_400(settings):
    """Mutation: delete the agent.chat call inside /regenerate — this turns red.
    Empty channel session (no prior chat) must 400, not invent a conversation.
    """
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        empty = _bridge_json(
            client,
            scoped,
            "POST",
            "/api/me/regenerate",
            {},
            user="5001",
            chat="5001",
        )
        assert empty.status_code == 400
        assert "разговор" in empty.json()["detail"].casefold()

        seeded = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "повтори меня",
                "source_ref": "telegram-update:reg-empty",
                "telegram_message_id": 9,
                "telegram_user": {"id": 5001},
            },
        )
        assert seeded.status_code == 200, seeded.text
        conversation_id = seeded.json()["conversation_id"]

        called = {"n": 0}

        async def _count(user_id, message, **kwargs):
            called["n"] += 1
            assert message == "повтори меня"
            assert kwargs.get("conversation_id") == conversation_id
            assert kwargs.get("attachments") == []
            assert kwargs.get("ingestion_result") is None
            return {
                "conversation_id": conversation_id,
                "message": {"role": "assistant", "content": "ok"},
                "answer": "ok",
                "context": {"interaction_mode": "dialogue"},
            }

        client.app.state.agent.chat = AsyncMock(side_effect=_count)  # type: ignore[method-assign]

        ok = _bridge_json(
            client,
            scoped,
            "POST",
            "/api/me/regenerate",
            {},
            user="5001",
            chat="5001",
        )
        assert ok.status_code == 200, ok.text
        assert called["n"] == 1
        assert ok.json()["answer"] == "ok"


def test_regenerate_accepts_explicit_conversation_id(settings):
    """Non-Telegram clients pass conversation_id in the body — same as /api/chat."""
    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        chat = client.post(
            "/api/chat",
            json={"message": "token path question"},
            headers=headers,
        )
        assert chat.status_code == 200, chat.text
        conversation_id = chat.json()["conversation_id"]

        async def _spy(user_id, message, **kwargs):
            assert message == "token path question"
            assert kwargs.get("conversation_id") == conversation_id
            return {
                "conversation_id": conversation_id,
                "answer": "again",
                "context": {"interaction_mode": "dialogue"},
            }

        client.app.state.agent.chat = AsyncMock(side_effect=_spy)  # type: ignore[method-assign]
        again = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation_id},
            headers=headers,
        )
        assert again.status_code == 200, again.text
        assert again.json()["answer"] == "again"
