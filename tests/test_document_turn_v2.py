from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.security import sign_bridge_request
from friday.telegram_bridge import PermanentUpdateError, TelegramBridge, TelegramConfig


def _bridge(tmp_path) -> TelegramBridge:  # noqa: ANN001
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _album_update(update_id: int, message_id: int, file_id: str, *, caption: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "media_group_id": "album-exact-1",
        "chat": {"id": 5001, "type": "private"},
        "from": {"id": 1001, "first_name": "Alice"},
        "photo": [
            {
                "file_id": file_id,
                "file_unique_id": f"unique-{file_id}",
                "file_size": 123,
                "width": 100,
                "height": 100,
            }
        ],
    }
    if caption:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


@pytest.mark.asyncio
async def test_durable_album_rows_become_one_owned_update(tmp_path, monkeypatch) -> None:
    """Two queue rows are removed only after one combined turn succeeds."""

    bridge = _bridge(tmp_path)
    first = _album_update(801, 41, "file-a", caption="Разбери оба скана")
    second = _album_update(802, 42, "file-b")
    seen: list[dict[str, Any]] = []

    async def process(_telegram, _backend, update, *, cached_response):  # noqa: ANN001
        assert cached_response is None
        seen.append(update)

    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)
    monkeypatch.setattr(bridge, "_process_update", process)
    bridge._inbox.store(first)  # noqa: SLF001
    bridge._inbox.store(second)  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001 - do not schedule a second dispatcher in this direct probe
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        assert bridge._inbox.stats() == {"pending": 0, "dead_letter": 0}  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert len(seen) == 1
    combined = seen[0]
    assert combined["update_id"] == 801
    assert combined["message"]["caption"] == "Разбери оба скана"
    assert [item["message_id"] for item in combined["friday_media_group_messages"]] == [41, 42]


@pytest.mark.asyncio
async def test_album_part_arriving_in_the_next_poll_joins_the_same_turn(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    seen: list[list[int]] = []

    async def process(_telegram, _backend, update, *, cached_response):  # noqa: ANN001
        assert cached_response is None
        seen.append([item["message_id"] for item in update["friday_media_group_messages"]])

    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.05)
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_MAX_WAIT_SEC", 0.3)
    monkeypatch.setattr(bridge, "_process_update", process)
    bridge._inbox.store(_album_update(806, 46, "file-a", caption="Два скана"))  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001
    task = asyncio.create_task(bridge._run_update(object(), object(), row))  # noqa: SLF001
    await asyncio.sleep(0.02)
    bridge._inbox.store(_album_update(807, 47, "file-b"))  # noqa: SLF001
    try:
        await task
        assert bridge._inbox.stats() == {"pending": 0, "dead_letter": 0}  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001
    assert seen == [[46, 47]]


@pytest.mark.asyncio
async def test_album_transient_failure_keeps_every_part_behind_one_anchor(tmp_path, monkeypatch) -> None:
    """A failed combined turn can retry, but no trailing part can split off."""

    bridge = _bridge(tmp_path)
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic transport failure")

    monkeypatch.setattr(bridge, "_process_update", fail)
    bridge._inbox.store(_album_update(811, 51, "file-a", caption="Оба"))  # noqa: SLF001
    bridge._inbox.store(_album_update(812, 52, "file-b"))  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        rows = bridge._inbox.contiguous_pending_rows("chat:5001", 811, limit=3)  # noqa: SLF001
        assert [item["update_id"] for item in rows] == [811, 812]
        assert rows[0]["attempts"] == 1
        assert rows[1]["attempts"] == 0
        assert bridge._inbox.pending(now=time.time() + 60)[0]["update_id"] == 811  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_bridge_sends_one_ordered_documents_payload_for_an_album(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(821, 61, "file-a", caption="Дай один ответ по двум сканам")
    second = _album_update(822, 62, "file-b")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    backend_calls: list[dict[str, Any]] = []

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        descriptor = message["photo"][-1]
        return {
            "filename": f"{descriptor['file_id']}.jpg",
            "mime_type": "image/jpeg",
            "content_base64": base64.b64encode(descriptor["file_id"].encode()).decode(),
            "source_ref": f"telegram-file:{descriptor['file_id']}",
            "file_unique_id": descriptor["file_unique_id"],
            "media_kind": "photo",
        }

    async def backend_json(_client, method, path, payload, _user, _chat):  # noqa: ANN001
        if path == "/api/chat":
            backend_calls.append(dict(payload))
            return {"message": "Готово", "message_format": "plain"}
        return {}

    async def typing(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(object(), object(), combined, cached_response=None)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert len(backend_calls) == 1
    payload = backend_calls[0]
    assert payload["source_ref"] == "telegram-update:821"
    assert payload["message"] == "Дай один ответ по двум сканам"
    assert "document" not in payload
    assert [item["source_ref"] for item in payload["documents"]] == [
        "telegram-file:file-a",
        "telegram-file:file-b",
    ]
    assert [item["telegram_message_id"] for item in payload["documents"]] == [61, 62]


@pytest.mark.asyncio
async def test_a_pre_v2_cached_single_file_answer_cannot_retire_an_album(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(825, 65, "file-a", caption="Оба скана")
    second = _album_update(826, 66, "file-b")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    try:
        with pytest.raises(PermanentUpdateError, match="complete album"):
            await bridge._process_update(  # noqa: SLF001
                object(),
                object(),
                combined,
                cached_response={"message": "Старый ответ только по первому файлу"},
            )
    finally:
        bridge._inbox.close()  # noqa: SLF001


def _signed_bridge_headers(settings, body: bytes) -> dict[str, str]:  # noqa: ANN001
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": "5001",
        "X-Friday-Chat": "5001",
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=timestamp,
            method="POST",
            path="/api/chat",
            external_user_id="5001",
            chat_id="5001",
            nonce=nonce,
            body=body,
        ),
    }


def test_backend_ingests_two_documents_as_one_idempotent_turn(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    payload = {
        "message": "Сравни эти два скана и ответь одним сообщением",
        "source_ref": "telegram-update:901",
        "telegram_message_id": 71,
        "telegram_user": {"id": 5001, "first_name": "Alice"},
        "documents": [
            {
                "filename": "scan-one.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"FIRST-SCAN-CONTENT").decode(),
                "source_ref": "telegram-file:scan-one",
                "file_unique_id": "unique-scan-one",
                "telegram_message_id": 71,
                "media_kind": "document",
            },
            {
                "filename": "scan-two.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"SECOND-SCAN-CONTENT").decode(),
                "source_ref": "telegram-file:scan-two",
                "file_unique_id": "unique-scan-two",
                "telegram_message_id": 72,
                "media_kind": "document",
            },
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with TestClient(app) as client:
        first = client.post("/api/chat", content=encoded, headers=_signed_bridge_headers(settings, encoded))
        assert first.status_code == 200, first.text
        body = first.json()
        assert len(body["file_ingestions"]) == 2
        assert "raw_object_id" not in json.dumps(body["file_ingestions"], ensure_ascii=False)
        rows = app.state.storage.execute(
            "SELECT source_ref FROM raw_objects WHERE content_type='file' ORDER BY source_ref"
        ).fetchall()
        assert [row["source_ref"] for row in rows] == [
            "telegram-file:scan-one",
            "telegram-file:scan-two",
        ]

        replay = client.post("/api/chat", content=encoded, headers=_signed_bridge_headers(settings, encoded))
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 2

        reordered_payload = {**payload, "documents": list(reversed(payload["documents"]))}
        reordered = json.dumps(
            reordered_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        conflict = client.post(
            "/api/chat",
            content=reordered,
            headers=_signed_bridge_headers(settings, reordered),
        )
        assert conflict.status_code == 409


def test_backend_rejects_ambiguous_document_shapes_before_persistence(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    payload = {
        "message": "ambiguous",
        "document": {"content_base64": "QQ=="},
        "documents": [{"content_base64": "Qg=="}],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            content=encoded,
            headers=_signed_bridge_headers(settings, encoded),
        )
        assert response.status_code == 400
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 0


def test_backend_rejects_a_bad_later_batch_item_before_the_first_can_persist(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    payload = {
        "message": "process both",
        "source_ref": "telegram-update:bad-batch",
        "documents": [
            {
                "filename": "safe.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"must-not-persist").decode(),
            },
            {
                "filename": "later.zip",
                "mime_type": "application/zip",
                "content_base64": base64.b64encode(b"PK\x03\x04").decode(),
            },
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            content=encoded,
            headers=_signed_bridge_headers(settings, encoded),
        )
        assert response.status_code == 400
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 0


@pytest.mark.asyncio
async def test_conflicting_album_captions_are_quarantined_together(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)
    bridge._inbox.store(_album_update(831, 81, "file-a", caption="Первый запрос"))  # noqa: SLF001
    bridge._inbox.store(_album_update(832, 82, "file-b", caption="Другой запрос"))  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001

    async def no_notice(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_notify_dead_letter", no_notice)
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        assert bridge._inbox.stats() == {"pending": 0, "dead_letter": 2}  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001
