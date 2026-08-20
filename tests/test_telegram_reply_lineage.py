"""Telegram replies carry only durable, chat-scoped backend message lineage."""

from __future__ import annotations

import json

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge._queue import _UpdateInbox


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def test_reply_context_is_chat_scoped_ttl_cleaned_and_bounded(tmp_path) -> None:
    inbox = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    try:
        for index in range(1, 4):
            inbox.remember_outbound_reply_context(
                5001,
                7000 + index,
                f"msg_backend_{index}",
                max_rows=2,
            )

        assert inbox.outbound_reply_source_message_id(5001, 7001) == ""
        assert inbox.outbound_reply_source_message_id(5001, 7002) == "msg_backend_2"
        assert inbox.outbound_reply_source_message_id(9999, 7002) == ""
        assert inbox.outbound_reply_source_message_id(5001, 7003) == "msg_backend_3"

        inbox._conn.execute(  # noqa: SLF001 - isolated expiry regression
            "UPDATE outbound_reply_context SET expires_at=0 WHERE telegram_message_id=7002"
        )
        inbox._conn.commit()  # noqa: SLF001
        assert inbox.outbound_reply_source_message_id(5001, 7002) == ""

        columns = {
            str(row["name"])
            for row in inbox._conn.execute("PRAGMA table_info(outbound_reply_context)").fetchall()  # noqa: SLF001
        }
        assert columns == {
            "chat_id",
            "telegram_message_id",
            "backend_message_id",
            "created_at",
            "expires_at",
        }
        database_dump = "\n".join(inbox._conn.iterdump())  # noqa: SLF001
        assert "raw_object_id" not in database_dump
        assert "conversation_attachment" not in database_dump
        assert "answer text must not be stored" not in database_dump
    finally:
        inbox.close()


@pytest.mark.asyncio
async def test_every_sent_answer_chunk_maps_to_same_backend_message(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    sent_ids = iter((8101, 8102, 8103))

    async def post(_client, _payload, _chunk):  # noqa: ANN001
        telegram_message_id = next(sent_ids)
        request = httpx.Request("POST", "https://api.telegram.test/sendMessage")
        return httpx.Response(
            200,
            request=request,
            json={"ok": True, "result": {"message_id": telegram_message_id}},
        )

    monkeypatch.setattr(bridge, "_post_message_chunk", post)
    try:
        await bridge._send_message(  # noqa: SLF001
            object(),
            5001,
            "A" * 5000,
            text_format="plain",
            reply_source_message_id="msg_backend_exact",
        )

        rows = bridge._inbox._conn.execute(  # noqa: SLF001
            """SELECT telegram_message_id, backend_message_id
               FROM outbound_reply_context ORDER BY telegram_message_id"""
        ).fetchall()
        assert len(rows) >= 2
        assert {str(row["backend_message_id"]) for row in rows} == {"msg_backend_exact"}
        assert all(
            bridge._inbox.outbound_reply_source_message_id(5001, int(row["telegram_message_id"]))
            == "msg_backend_exact"
            for row in rows
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cached_backend_response_still_maps_retried_chunks(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)

    async def post(
        _client,
        _payload,
        _chunk,
        *,
        resume_key=None,
        chunk_number=None,
    ):  # noqa: ANN001
        assert resume_key == 8150
        assert chunk_number == 1
        assert bridge._inbox.begin_answer_chunk_delivery(resume_key, chunk_number) is not None
        request = httpx.Request("POST", "https://api.telegram.test/sendMessage")
        return httpx.Response(
            200,
            request=request,
            json={"ok": True, "result": {"message_id": 8151}},
        )

    monkeypatch.setattr(bridge, "_post_message_chunk", post)
    update = {
        "update_id": 8150,
        "message": {
            "message_id": 8150,
            "chat": {"id": 5001, "type": "private"},
            "from": {"id": 1001},
            "text": "Повтори доставку",
        },
    }
    cached = {"message_id": "msg_cached_answer", "message": "Кешированный ответ"}

    try:
        assert bridge._inbox.store(update) is True  # noqa: SLF001
        await bridge._process_update(  # noqa: SLF001
            object(),
            object(),
            update,
            cached_response=cached,
        )
        assert (
            bridge._inbox.outbound_reply_source_message_id(5001, 8151)  # noqa: SLF001
            == "msg_cached_answer"
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_incoming_reply_emits_only_opaque_backend_message_id(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    captured: dict = {}
    bridge._inbox.remember_outbound_reply_context(  # noqa: SLF001
        5001,
        8201,
        "msg_backend_prior_answer",
    )

    async def backend(_client, method, path, payload, _user, _chat):  # noqa: ANN001
        assert method == "POST" and path == "/api/chat"
        captured.update(payload)
        return {"message_id": "msg_backend_current", "message": "Принято"}

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_send_message", send)
    update = {
        "update_id": 8202,
        "message": {
            "message_id": 8202,
            "chat": {"id": 5001, "type": "private"},
            "from": {"id": 1001},
            "text": "А откуда это взято?",
            "reply_to_message": {
                "message_id": 8201,
                "from": {"id": 999, "is_bot": True},
                "text": "Старый ответ Пятницы",
            },
        },
    }

    try:
        await bridge._process_update(object(), object(), update, cached_response=None)  # noqa: SLF001

        assert captured["reply_source_message_id"] == "msg_backend_prior_answer"
        assert captured["reply_to"] == "Старый ответ Пятницы"
        serialized = json.dumps(captured, ensure_ascii=False, sort_keys=True)
        assert "raw_object_id" not in serialized
        assert "conversation_attachment" not in serialized
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_reply_mapping_never_crosses_chat_boundary(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    captured: dict = {}
    bridge._inbox.remember_outbound_reply_context(5002, 8301, "msg_other_chat")  # noqa: SLF001

    async def backend(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
        captured.update(payload)
        return {"message_id": "msg_current", "message": "Принято"}

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_send_message", send)
    update = {
        "update_id": 8302,
        "message": {
            "message_id": 8302,
            "chat": {"id": 5001, "type": "private"},
            "from": {"id": 1001},
            "text": "Продолжи",
            "reply_to_message": {
                "message_id": 8301,
                "from": {"id": 999, "is_bot": True},
                "text": "Чужой chat id",
            },
        },
    }

    try:
        await bridge._process_update(object(), object(), update, cached_response=None)  # noqa: SLF001
        assert "reply_source_message_id" not in captured
    finally:
        bridge._inbox.close()  # noqa: SLF001
