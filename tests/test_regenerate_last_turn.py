"""POST /api/me/regenerate — replay the last user turn through agent.chat.

G15: mainstream chat products have «regenerate»; Jericho only let you retype.
Self-service, chat.use, conversation resolved like /api/chat for Telegram.
Storage cannot branch alternate answers — a new user+assistant pair is appended.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
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


@pytest.mark.asyncio
async def test_concurrent_regenerate_does_not_call_agent_twice(settings):
    """G17a: double-tap /regenerate must not run agent.chat twice.

    Mutation: remove idempotency_claim from regenerate_last_turn → calls == 2
    and this assertion fails.
    """
    import asyncio

    import httpx

    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            seeded = await client.post(
                "/api/chat",
                json={"message": "вопрос для гонки regenerate"},
                headers=headers,
            )
            assert seeded.status_code == 200, seeded.text
            conversation_id = seeded.json()["conversation_id"]

            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def delayed_chat(*args, **kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return {
                    "conversation_id": conversation_id,
                    "message": {"role": "assistant", "content": "once"},
                    "answer": "once",
                    "context": {"interaction_mode": "dialogue"},
                }

            app.state.agent.chat = delayed_chat  # type: ignore[method-assign]
            payload = {"conversation_id": conversation_id}
            first_task = asyncio.create_task(
                client.post("/api/me/regenerate", json=payload, headers=headers)
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = await client.post("/api/me/regenerate", json=payload, headers=headers)
            assert second.status_code == 409, second.text
            assert second.headers.get("Retry-After") == "2"

            release.set()
            first = await first_task
            assert first.status_code == 200, first.text
            assert first.json()["answer"] == "once"

            replay = await client.post("/api/me/regenerate", json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json().get("idempotent_replay") is True
            assert calls == 1


def test_regenerate_warns_when_original_turn_had_attachments(settings):
    """G17b: lost attachment must be audible; plain text turns stay silent.

    Mutation: stop writing had_attachments in AgentRuntime.chat → notice absent.
    """
    from jericho.server import create_app
    from jericho.telegram_bridge._callbacks import CallbacksMixin

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        storage = client.app.state.storage
        # Owner user id from API token path.
        me = client.get("/api/me", headers=headers)
        assert me.status_code == 200, me.text
        user_id = me.json()["actor"]["user_id"]
        conversation = storage.create_conversation(user_id, title="attach regen")
        conversation_id = conversation["id"]
        storage.store_message(
            conversation_id,
            user_id,
            "user",
            "что в этом файле?",
            metadata={"had_attachments": True, "attachment_count": 1},
        )
        storage.store_message(
            conversation_id,
            user_id,
            "assistant",
            "в файле — таблица",
        )

        async def _spy(user_id_arg, message, **kwargs):
            assert kwargs.get("attachments") == []
            return {
                "conversation_id": conversation_id,
                "message": {"role": "assistant", "content": "угадываю без файла"},
                "answer": "угадываю без файла",
                "context": {"interaction_mode": "dialogue"},
            }

        client.app.state.agent.chat = AsyncMock(side_effect=_spy)  # type: ignore[method-assign]
        with_attach = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation_id},
            headers=headers,
        )
        assert with_attach.status_code == 200, with_attach.text
        body = with_attach.json()
        notice = str(body.get("regenerate_notice") or "")
        assert "вложен" in notice.casefold()
        assert "grounding_warning" in body
        formatted = CallbacksMixin._format_response_message(
            {
                "message": body.get("answer") or body.get("message") or "",
                "regenerate_notice": notice,
                "grounding_warning": body.get("grounding_warning"),
                "context": body.get("context") or {},
            }
        )
        assert "вложен" in formatted.casefold()

        # Plain turn: no marker, no notice.
        plain_conv = storage.create_conversation(user_id, title="plain regen")
        plain_id = plain_conv["id"]
        storage.store_message(plain_id, user_id, "user", "просто текст без файла")
        storage.store_message(plain_id, user_id, "assistant", "ок")

        async def _plain(user_id_arg, message, **kwargs):
            return {
                "conversation_id": plain_id,
                "message": {"role": "assistant", "content": "снова"},
                "answer": "снова",
                "context": {"interaction_mode": "dialogue"},
            }

        client.app.state.agent.chat = AsyncMock(side_effect=_plain)  # type: ignore[method-assign]
        plain = client.post(
            "/api/me/regenerate",
            json={"conversation_id": plain_id},
            headers=headers,
        )
        assert plain.status_code == 200, plain.text
        assert not plain.json().get("regenerate_notice")


def test_agent_chat_records_had_attachments_on_user_message(settings, storage):
    """Without this marker /regenerate cannot know a file was there."""
    import asyncio
    import json

    from jericho.agent_runtime import AgentRuntime
    from jericho.permissions import AuthorizationService

    storage.ensure_user("alice", preset_key="user")
    auth = AuthorizationService(storage)
    agent = AgentRuntime(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    async def _run():
        return await agent.chat(
            "alice",
            "посмотри файл",
            actor=actor,
            attachments=[{"filename": "x.pdf", "transient": True}],
            enable_tools=False,
        )

    asyncio.run(_run())
    rows = storage.execute(
        "SELECT metadata_json FROM messages WHERE user_id=? AND role='user'",
        ("alice",),
    ).fetchall()
    assert rows
    meta = json.loads(rows[0]["metadata_json"] or "{}")
    assert meta.get("had_attachments") is True
    assert meta.get("attachment_count") == 1
