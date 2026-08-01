"""G20: plain-text export of a conversation (HTTP + Telegram /export).

HTTP is the tenant boundary (foreign id → 404). Telegram only ships the file
via sendDocument; mutation drops that call and the bridge test turns red.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from tests.test_api_vertical_slice import _bridge_get, _bridge_request, _signed_headers
from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient, _media_bridge


def test_export_contains_messages_in_order_and_foreign_is_404(settings):
    """Self-service: own transcript has both turns chronologically; foreign → 404.

    Mutation: drop user_id from get_conversation / get_conversation_messages →
    foreign export may leak or 500 instead of 404.
    """
    tuned = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(tuned)) as client:
        first = _bridge_request(
            client,
            tuned,
            "/api/chat",
            {
                "message": "первый вопрос экспорта",
                "source_ref": "telegram-update:exp1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5001},
            },
        )
        assert first.status_code == 200, first.text
        conversation_id = first.json()["conversation_id"]

        second = _bridge_request(
            client,
            tuned,
            "/api/chat",
            {
                "message": "второй вопрос экспорта",
                "source_ref": "telegram-update:exp2",
                "telegram_message_id": 2,
                "telegram_user": {"id": 5001},
            },
        )
        assert second.status_code == 200, second.text

        # Раньше здесь переписывались тексты ответов ради детерминизма. Текст
        # сообщения чата теперь неизменяем на уровне базы (требование владельца),
        # да и нужды в этом нет: порядок проверяется по вопросам человека, они
        # заданы этим же тестом, а от ответов требуется только присутствие.
        storage = client.app.state.storage
        roles = [
            str(row["role"])
            for row in storage.execute(
                "SELECT role FROM messages WHERE conversation_id=? ORDER BY created_at ASC, rowid ASC",
                (conversation_id,),
            )
        ]
        assert "assistant" in roles, "в разговоре нет ни одного ответа для экспорта"

        path = f"/api/conversations/{conversation_id}/export"
        response = _bridge_get(client, tuned, path, user="5001", chat="5001")
        assert response.status_code == 200, response.text
        assert "text/plain" in (response.headers.get("content-type") or "")
        body = response.text
        assert "первый вопрос экспорта" in body
        assert "второй вопрос экспорта" in body
        pos_first = body.index("первый вопрос экспорта")
        pos_second = body.index("второй вопрос экспорта")
        assert pos_first < pos_second, body
        assert "user:" in body
        assert "assistant:" in body

        # Foreign conversation id under another actor → 404, not 403.
        client.get(
            path,
            headers=_signed_headers(
                tuned.telegram_bridge_secret,
                "GET",
                path,
                b"",
                "5002",
                "5002",
            ),
        )
        # 5002 may not be allowlisted — bridge auth may 403 before tenant check.
        # Use owner bearer with a conversation that belongs to telegram user:
        owner = {"Authorization": f"Bearer {tuned.api_token}"}
        # Owner has a different user_id; same conversation is not theirs.
        owner_resp = client.get(path, headers=owner)
        assert owner_resp.status_code == 404, owner_resp.text


def test_export_current_sentinel_and_truncation_note(settings):
    """current resolves like G18; full window notes truncation when cap is hit."""
    from friday.api.conversations import EXPORT_MESSAGE_LIMIT, format_conversation_export

    text = format_conversation_export(
        {"id": "conv_x", "title": "План"},
        [
            {"role": "user", "created_at": "t1", "content": "a"},
            {"role": "assistant", "created_at": "t2", "content": "b"},
        ],
        limit=2,
        truncated=True,
    )
    assert "показаны последние 2 сообщений" in text
    assert "[t1] user: a" in text
    assert "[t2] assistant: b" in text
    assert "conversation_id: conv_x" in text
    assert EXPORT_MESSAGE_LIMIT == 500

    tuned = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    with TestClient(create_app(tuned)) as client:
        chat = _bridge_request(
            client,
            tuned,
            "/api/chat",
            {
                "message": "только current",
                "source_ref": "telegram-update:exp-cur",
                "telegram_message_id": 9,
                "telegram_user": {"id": 5001},
            },
        )
        assert chat.status_code == 200, chat.text
        response = _bridge_get(
            client,
            tuned,
            "/api/conversations/current/export",
            user="5001",
            chat="5001",
        )
        assert response.status_code == 200, response.text
        assert "только current" in response.text


@pytest.mark.asyncio
async def test_export_command_calls_send_document(tmp_path):
    """Mutation: remove _send_document from /export handler → no sendDocument, red."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    export_body = (
        "# Friday conversation export\n"
        "# conversation_id: conv_1\n"
        "# title: тест\n"
        "# messages: 2\n"
        "\n"
        "[t1] user: вопрос\n"
        "[t2] assistant: ответ\n"
    )
    backend = _FakeBackendClient(
        {
            "/api/me": {"actor": {"preset_key": "user"}, "user": {}},
            "/api/conversations/current/export": export_body,
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    # Spy on the real method path through the bridge instance.
    original = bridge._send_document
    seen: list[tuple] = []

    async def _spy(client, chat_id, filename, content_bytes, *, caption=""):
        seen.append((chat_id, filename, content_bytes, caption))
        return await original(client, chat_id, filename, content_bytes, caption=caption)

    bridge._send_document = _spy  # type: ignore[method-assign]
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
                    "text": "/export",
                },
            },
            cached_response=None,
        )
        assert any(
            call["method"] == "GET" and call["path"] == "/api/conversations/current/export"
            for call in backend.calls
        ), backend.calls
        assert seen, "expected _send_document to be called"
        chat_id, filename, content_bytes, caption = seen[0]
        assert chat_id == 5001
        assert filename.endswith(".txt")
        assert "вопрос".encode() in content_bytes
        assert "ответ".encode() in content_bytes
        assert any(url.endswith("/sendDocument") for url, _ in telegram.calls), telegram.calls
    finally:
        bridge._send_document = original  # type: ignore[method-assign]
        bridge._inbox.close()
