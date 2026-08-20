from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.security import sign_bridge_request
from friday.telegram_bridge import MediaTooLargeError, PermanentUpdateError, TelegramBridge, TelegramConfig


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


def _telegram_item_receipt(message_id: int, source_ref: str) -> dict[str, Any]:
    return {
        "telegram_message_id": message_id,
        "source_ref_sha256": hashlib.sha256(source_ref.encode("utf-8")).hexdigest(),
    }


def _album_v2_source_ref(update_id: int, items: list[tuple[int, str]]) -> str:
    canonical = json.dumps(
        [
            {
                "telegram_message_id": message_id,
                "source_ref_sha256": hashlib.sha256(source_ref.encode()).hexdigest(),
            }
            for message_id, source_ref in items
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"telegram-album-v2:{update_id}:{hashlib.sha256(canonical.encode('ascii')).hexdigest()}"


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
        assert rows[1]["attempts"] == 1
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
            if payload.get("document_stage_only") is not True:
                return {
                    "message": "Готово",
                    "message_format": "plain",
                }
            return {
                "file_ingestions": [
                    {
                        "telegram_item_receipt": _telegram_item_receipt(
                            int(item["telegram_message_id"]),
                            str(item["source_ref"]),
                        ),
                        "telegram_stage_ready": True,
                    }
                    for item in payload["documents"]
                ],
            }
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

    assert len(backend_calls) == 2
    stage, payload = backend_calls
    assert stage["document_stage_only"] is True
    assert stage["message"] == ""
    # The durable per-item source refs own replay.  A batch-level idempotency
    # key would turn a crash after a persisted prefix into one opaque uncertain
    # response and prevent the bridge from resuming the missing siblings.
    assert "source_ref" not in stage
    assert [item["source_ref"] for item in stage["documents"]] == [
        "telegram-file:file-a",
        "telegram-file:file-b",
    ]
    assert [item["telegram_message_id"] for item in stage["documents"]] == [61, 62]
    assert payload["source_ref"] == _album_v2_source_ref(
        821,
        [(61, "telegram-file:file-a"), (62, "telegram-file:file-b")],
    )
    assert payload["message"] == "Дай один ответ по двум сканам"
    assert "document" not in payload and "documents" not in payload
    assert payload["staged_document_message_ids"] == [61, 62]


@pytest.mark.asyncio
async def test_old_poisoned_anchor_cannot_replay_over_versioned_album_final(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(102500242, 1842, "historical-a", caption="Разбери оба")
    second = _album_update(102500243, 1843, "historical-b")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    final_source_refs: list[str] = []
    sent: list[str] = []

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        message_id = int(message["message_id"])
        return {
            "filename": f"{message_id}.jpg",
            "mime_type": "image/jpeg",
            "content_base64": "QQ==",
            "source_ref": f"telegram-file:historical-{message_id}",
        }

    async def backend_json(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
        if payload.get("document_stage_only") is True:
            return {
                "file_ingestions": [
                    {
                        "telegram_item_receipt": _telegram_item_receipt(
                            int(item["telegram_message_id"]), str(item["source_ref"])
                        ),
                        "telegram_stage_ready": True,
                    }
                    for item in payload["documents"]
                ]
            }
        source_ref = str(payload["source_ref"])
        final_source_refs.append(source_ref)
        if source_ref == "telegram-update:102500242":
            return {"message": "poisoned legacy replay", "message_format": "plain"}
        return {"message": "fresh v2 album answer", "message_format": "plain"}

    async def typing(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def send(_telegram, _chat_id, message, **_kwargs):  # noqa: ANN001
        sent.append(str(message))

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(object(), object(), combined, cached_response=None)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert final_source_refs == [
        _album_v2_source_ref(
            102500242,
            [
                (1842, "telegram-file:historical-1842"),
                (1843, "telegram-file:historical-1843"),
            ],
        )
    ]
    assert sent == ["fresh v2 album answer"]


@pytest.mark.asyncio
async def test_one_oversized_album_sibling_does_not_discard_its_healthy_neighbor(
    tmp_path, monkeypatch
) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(823, 63, "too-large", caption="Разбери доступные сканы")
    second = _album_update(824, 64, "healthy")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    backend_calls: list[dict[str, Any]] = []
    sent: list[str] = []

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        descriptor = message["photo"][-1]
        if descriptor["file_id"] == "too-large":
            raise MediaTooLargeError("synthetic item limit")
        return {
            "filename": "healthy.jpg",
            "mime_type": "image/jpeg",
            "content_base64": "QQ==",
            "source_ref": "telegram-file:healthy",
        }

    async def backend_json(_client, _method, path, payload, _user, _chat):  # noqa: ANN001
        assert path == "/api/chat"
        backend_calls.append(dict(payload))
        if payload.get("document_stage_only") is True:
            item = payload["documents"][0]
            return {
                "file_ingestions": [
                    {
                        "telegram_item_receipt": _telegram_item_receipt(
                            int(item["telegram_message_id"]), str(item["source_ref"])
                        ),
                        "telegram_stage_ready": True,
                    }
                ]
            }
        return {"message": "Здоровый файл обработан", "message_format": "plain"}

    async def typing(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def send(_telegram, _chat_id, message, **_kwargs):  # noqa: ANN001
        sent.append(str(message))

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(object(), object(), combined, cached_response=None)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert len(backend_calls) == 2
    assert [item["telegram_message_id"] for item in backend_calls[0]["documents"]] == [64]
    assert backend_calls[1]["staged_document_message_ids"] == [64]
    assert len(sent) == 1
    assert "Не удалось принять 1 из 2" in sent[0]
    assert "Здоровый файл обработан" in sent[0]


@pytest.mark.asyncio
async def test_all_oversized_captionless_album_gets_one_code_owned_warning(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(825, 65, "too-large-a")
    second = _album_update(826, 66, "too-large-b")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    backend_calls: list[dict[str, Any]] = []
    sent: list[str] = []

    async def prepare(*_args, **_kwargs):
        raise MediaTooLargeError("synthetic item limit")

    async def backend_json(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
        backend_calls.append(dict(payload or {}))
        raise AssertionError("all-invalid album must not reach backend chat/model")

    async def typing(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def send(_telegram, _chat_id, message, **_kwargs):  # noqa: ANN001
        sent.append(str(message))

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(object(), object(), combined, cached_response=None)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert backend_calls == []
    assert len(sent) == 1
    assert "Не удалось принять ни одного файла из альбома" in sent[0]
    assert "Отклонено: 2" in sent[0]


@pytest.mark.asyncio
async def test_aggregate_album_413_splits_to_exact_singletons_and_keeps_one_answer(
    tmp_path, monkeypatch
) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(827, 67, "file-a", caption="Разбери оба скана")
    second = _album_update(828, 68, "file-b")
    combined = dict(first)
    combined["friday_media_group_messages"] = [first["message"], second["message"]]
    stage_sizes: list[int] = []
    final_payloads: list[dict[str, Any]] = []
    sent: list[str] = []

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        message_id = int(message["message_id"])
        return {
            "filename": f"{message_id}.jpg",
            "mime_type": "image/jpeg",
            "content_base64": "QQ==",
            "source_ref": f"telegram-file:{message_id}",
        }

    async def backend_json(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
        if payload.get("document_stage_only") is True:
            documents = payload["documents"]
            stage_sizes.append(len(documents))
            if len(documents) > 1:
                raise PermanentUpdateError("synthetic aggregate limit", status_code=413)
            item = documents[0]
            return {
                "file_ingestions": [
                    {
                        "telegram_item_receipt": _telegram_item_receipt(
                            int(item["telegram_message_id"]), str(item["source_ref"])
                        ),
                        "telegram_stage_ready": True,
                    }
                ]
            }
        final_payloads.append(dict(payload))
        return {"message": "Один итог по обоим", "message_format": "plain"}

    async def typing(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def send(_telegram, _chat_id, message, **_kwargs):  # noqa: ANN001
        sent.append(str(message))

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    try:
        await bridge._process_update(object(), object(), combined, cached_response=None)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert stage_sizes == [2, 1, 1]
    assert len(final_payloads) == 1
    assert final_payloads[0]["staged_document_message_ids"] == [67, 68]
    assert "documents" not in final_payloads[0]
    assert sent == ["Один итог по обоим"]


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


def test_backend_stages_exact_album_siblings_then_runs_one_alias_authorized_turn(
    settings, monkeypatch
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    stage_payload = {
        "message": "",
        "document_stage_only": True,
        "telegram_user": {"id": 5001, "first_name": "Alice"},
        "documents": [
            {
                "filename": "scan-one.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"FIRST-STAGED-SCAN").decode(),
                "source_ref": "telegram-file:staged-one",
                "file_unique_id": "unique-staged-one",
                "telegram_message_id": 71,
                "media_kind": "document",
            },
            {
                "filename": "scan-two.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"SECOND-STAGED-SCAN").decode(),
                "source_ref": "telegram-file:staged-two",
                "file_unique_id": "unique-staged-two",
                "telegram_message_id": 72,
                "media_kind": "document",
            },
        ],
    }
    encoded_stage = json.dumps(
        stage_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    calls: list[dict[str, Any]] = []
    with TestClient(app) as client:
        original_chat = app.state.agent.chat

        async def capture_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            calls.append({"args": args, "kwargs": kwargs})
            return await original_chat(*args, **kwargs)

        monkeypatch.setattr(app.state.agent, "chat", capture_chat)
        stage = client.post(
            "/api/chat",
            content=encoded_stage,
            headers=_signed_bridge_headers(settings, encoded_stage),
        )
        assert stage.status_code == 200, stage.text
        stage_body = stage.json()
        assert stage_body["document_stage_only"] is True
        assert [item["telegram_item_receipt"] for item in stage_body["file_ingestions"]] == [
            _telegram_item_receipt(71, "telegram-file:staged-one"),
            _telegram_item_receipt(72, "telegram-file:staged-two"),
        ]
        assert [item["telegram_stage_ready"] for item in stage_body["file_ingestions"]] == [
            True,
            True,
        ]
        assert calls == []

        # No batch idempotency record is used. Exact item source refs make a
        # crash/retry replay-only for the received prefix and still emit every
        # ordered receipt needed to resume missing siblings.
        replay = client.post(
            "/api/chat",
            content=encoded_stage,
            headers=_signed_bridge_headers(settings, encoded_stage),
        )
        assert replay.status_code == 200, replay.text
        replay_items = replay.json()["file_ingestions"]
        assert [item["telegram_item_receipt"] for item in replay_items] == [
            item["telegram_item_receipt"] for item in stage_body["file_ingestions"]
        ]
        assert [item["telegram_stage_ready"] for item in replay_items] == [True, True]
        assert [item["idempotent_replay"] for item in replay_items] == [True, True]
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 2
        assert calls == []

        final_payload = {
            "message": "Сравни эти два скана",
            "source_ref": "telegram-update:staged-final",
            "telegram_message_id": 71,
            "telegram_user": {"id": 5001, "first_name": "Alice"},
            "staged_document_message_ids": [71, 72],
        }
        encoded_final = json.dumps(
            final_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        final = client.post(
            "/api/chat",
            content=encoded_final,
            headers=_signed_bridge_headers(settings, encoded_final),
        )
        assert final.status_code == 200, final.text

        mutated_final = {
            **final_payload,
            "staged_document_message_ids": [72, 71],
        }
        encoded_mutation = json.dumps(
            mutated_final, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        conflict = client.post(
            "/api/chat",
            content=encoded_mutation,
            headers=_signed_bridge_headers(settings, encoded_mutation),
        )
        assert conflict.status_code == 409

    assert len(calls) == 1
    attachments = [dict(item) for item in calls[0]["kwargs"]["attachments"]]
    assert len(attachments) == 2
    assert all(set(item) == {"raw_object_id"} for item in attachments)
    assert len({item["raw_object_id"] for item in attachments}) == 2


def test_byte_duplicate_album_siblings_keep_ordered_proof_but_one_runtime_carrier(
    settings, monkeypatch
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    duplicated = base64.b64encode(b"IDENTICAL-TELEGRAM-PHOTO").decode()
    stage_payload = {
        "message": "",
        "document_stage_only": True,
        "telegram_user": {"id": 5001, "first_name": "Alice"},
        "documents": [
            {
                "filename": f"same-{message_id}.txt",
                "mime_type": "text/plain",
                "content_base64": duplicated,
                "source_ref": f"telegram-file:same-{message_id}",
                "telegram_message_id": message_id,
                "media_kind": "photo",
            }
            for message_id in (77, 78)
        ],
    }
    encoded_stage = json.dumps(stage_payload, sort_keys=True, separators=(",", ":")).encode()
    captured_attachments: list[list[dict[str, Any]]] = []
    with TestClient(app) as client:
        original_chat = app.state.agent.chat

        async def capture_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            captured_attachments.append([dict(item) for item in kwargs.get("attachments") or []])
            return await original_chat(*args, **kwargs)

        monkeypatch.setattr(app.state.agent, "chat", capture_chat)
        stage = client.post(
            "/api/chat",
            content=encoded_stage,
            headers=_signed_bridge_headers(settings, encoded_stage),
        )
        assert stage.status_code == 200, stage.text
        stage_items = stage.json()["file_ingestions"]
        assert [item["telegram_stage_ready"] for item in stage_items] == [True, True]
        assert [item["telegram_item_receipt"]["telegram_message_id"] for item in stage_items] == [
            77,
            78,
        ]
        raw_count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(raw_count["count"]) == 1

        final_payload = {
            "message": "Обобщи документы альбома",
            "source_ref": "telegram-update:duplicate-album-final",
            "telegram_message_id": 77,
            "telegram_user": {"id": 5001, "first_name": "Alice"},
            "staged_document_message_ids": [77, 78],
        }
        encoded_final = json.dumps(final_payload, sort_keys=True, separators=(",", ":")).encode()
        final = client.post(
            "/api/chat",
            content=encoded_final,
            headers=_signed_bridge_headers(settings, encoded_final),
        )
        assert final.status_code == 200, final.text
        final_body = final.json()
        assert final_body["staged_document_message_count"] == 2
        assert final_body["staged_duplicate_count"] == 1
        assert "одинаковые по содержимому" in final_body["grounding_warning"]

    assert len(captured_attachments) == 1
    assert len(captured_attachments[0]) == 1
    assert set(captured_attachments[0][0]) == {"raw_object_id"}


def test_stage_retains_raw_but_refuses_final_authority_when_private_alias_gate_closes(
    settings, monkeypatch
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    monkeypatch.setattr("friday.server.bind_owned_telegram_reply_aliases", lambda *_args: False)
    calls = 0

    async def forbidden_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("a denied staged alias reached the model")

    stage_payload = {
        "message": "",
        "document_stage_only": True,
        "telegram_user": {"id": 5001, "first_name": "Alice"},
        "documents": [
            {
                "filename": "private.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"PRIVATE-STAGED-SCAN").decode(),
                "source_ref": "telegram-file:private-staged",
                "telegram_message_id": 73,
                "media_kind": "document",
            }
        ],
    }
    encoded_stage = json.dumps(stage_payload, sort_keys=True, separators=(",", ":")).encode()
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.agent, "chat", forbidden_chat)
        stage = client.post(
            "/api/chat",
            content=encoded_stage,
            headers=_signed_bridge_headers(settings, encoded_stage),
        )
        assert stage.status_code == 200, stage.text
        assert stage.json()["file_ingestions"][0]["telegram_stage_ready"] is False
        assert stage.json()["file_ingestions"][0]["telegram_item_receipt"] == (
            _telegram_item_receipt(73, "telegram-file:private-staged")
        )
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 1

        final_payload = {
            "message": "Обобщи",
            "source_ref": "telegram-update:private-stage-final",
            "telegram_message_id": 73,
            "telegram_user": {"id": 5001, "first_name": "Alice"},
            "staged_document_message_ids": [73],
        }
        encoded_final = json.dumps(final_payload, sort_keys=True, separators=(",", ":")).encode()
        final = client.post(
            "/api/chat",
            content=encoded_final,
            headers=_signed_bridge_headers(settings, encoded_final),
        )
        assert final.status_code == 409
    assert calls == 0


def test_stage_source_identity_conflict_is_one_terminal_receipt_not_a_batch_409(
    settings,
) -> None:
    from friday.server import create_app

    app = create_app(settings)

    def stage_payload(documents):  # noqa: ANN001
        return {
            "message": "",
            "document_stage_only": True,
            "telegram_user": {"id": 5001, "first_name": "Alice"},
            "documents": documents,
        }

    def document(message_id: int, source_ref: str, content: bytes) -> dict[str, Any]:
        return {
            "filename": f"{message_id}.txt",
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
            "source_ref": source_ref,
            "telegram_message_id": message_id,
            "media_kind": "document",
        }

    seed = stage_payload([document(74, "telegram-file:conflicted", b"ORIGINAL")])
    encoded_seed = json.dumps(seed, sort_keys=True, separators=(",", ":")).encode()
    batch = stage_payload(
        [
            document(74, "telegram-file:conflicted", b"MUTATED"),
            document(75, "telegram-file:healthy-after-conflict", b"HEALTHY"),
        ]
    )
    encoded_batch = json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()
    with TestClient(app) as client:
        first = client.post(
            "/api/chat",
            content=encoded_seed,
            headers=_signed_bridge_headers(settings, encoded_seed),
        )
        assert first.status_code == 200, first.text
        response = client.post(
            "/api/chat",
            content=encoded_batch,
            headers=_signed_bridge_headers(settings, encoded_batch),
        )
        assert response.status_code == 200, response.text
        items = response.json()["file_ingestions"]
        assert [item["telegram_stage_ready"] for item in items] == [False, True]
        assert [item["telegram_item_receipt"] for item in items] == [
            _telegram_item_receipt(74, "telegram-file:conflicted"),
            _telegram_item_receipt(75, "telegram-file:healthy-after-conflict"),
        ]
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 2


def test_stage_rejects_an_opaque_batch_idempotency_key_before_any_file_mutation(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    payload = {
        "message": "",
        "document_stage_only": True,
        "source_ref": "telegram-album-stage:must-not-own-a-prefix",
        "telegram_user": {"id": 5001, "first_name": "Alice"},
        "documents": [
            {
                "filename": "stage.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"MUST-NOT-PERSIST").decode(),
                "source_ref": "telegram-file:stage-no-batch-key",
                "telegram_message_id": 76,
                "media_kind": "document",
            }
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
        assert [item["telegram_item_receipt"] for item in body["file_ingestions"]] == [
            _telegram_item_receipt(71, "telegram-file:scan-one"),
            _telegram_item_receipt(72, "telegram-file:scan-two"),
        ]
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
        assert [item["telegram_item_receipt"] for item in replay.json()["file_ingestions"]] == [
            _telegram_item_receipt(71, "telegram-file:scan-one"),
            _telegram_item_receipt(72, "telegram-file:scan-two"),
        ]
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


def test_album_keeps_ingesting_after_a_reply_alias_is_fail_closed(settings, monkeypatch) -> None:
    """One denied post-persist alias cannot discard the rest of a ten-photo album."""

    from friday.server import create_app

    app = create_app(settings)
    calls = 0

    def bind_alias(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal calls
        calls += 1
        return calls != 5

    monkeypatch.setattr("friday.server.bind_owned_telegram_reply_aliases", bind_alias)
    payload = {
        "message": "Разбери все десять файлов одним ответом",
        "source_ref": "telegram-update:ten-photo-album",
        "telegram_message_id": 1842,
        "telegram_user": {"id": 5001, "first_name": "Alice"},
        "documents": [
            {
                "filename": f"telegram-photo-{message_id}.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(f"PHOTO-{message_id}".encode()).decode(),
                "source_ref": f"telegram-file:album-{message_id}",
                "file_unique_id": f"unique-album-{message_id}",
                "telegram_message_id": message_id,
                "media_kind": "photo",
            }
            for message_id in range(1842, 1852)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with TestClient(app) as client:
        # Reproduce the live orphan prefix: three exact source refs already own
        # Raw rows, while the group turn itself has no completed receipt.
        prefix_payload = {
            **payload,
            "source_ref": "telegram-update:ten-photo-prefix",
            "documents": payload["documents"][:3],
        }
        prefix_encoded = json.dumps(
            prefix_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        prefix = client.post(
            "/api/chat",
            content=prefix_encoded,
            headers=_signed_bridge_headers(settings, prefix_encoded),
        )
        assert prefix.status_code == 200, prefix.text
        assert len(prefix.json()["file_ingestions"]) == 3

        response = client.post(
            "/api/chat",
            content=encoded,
            headers=_signed_bridge_headers(settings, encoded),
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["file_ingestions"]) == 10
        count = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM raw_objects WHERE content_type='file'"
        ).fetchone()
        assert int(count["count"]) == 10
    # Three replayed siblings are receipted but do not create duplicate Raw;
    # seven missing siblings are the only new durable file mutations.
    assert calls == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_mode", ["missing", "generic", "reversed", "duplicate"])
async def test_album_without_exact_per_item_receipts_is_retried_as_one_group(
    tmp_path, monkeypatch, receipt_mode
) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(851, 91, "file-a", caption="Оба скана")
    second = _album_update(852, 92, "file-b")
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)

    async def process(_telegram, _backend, update, *, cached_response):  # noqa: ANN001
        assert cached_response is None
        update = dict(update)
        update["friday_media_group_messages"] = [first["message"], second["message"]]

        async def prepare(_client, message, _carrier):  # noqa: ANN001
            return {
                "filename": f"{message['message_id']}.txt",
                "mime_type": "text/plain",
                "content_base64": "QQ==",
                "source_ref": f"telegram-file:{message['message_id']}",
            }

        async def backend_json(*_args, **_kwargs):
            first_receipt = {"telegram_item_receipt": _telegram_item_receipt(91, "telegram-file:91")}
            second_receipt = {"telegram_item_receipt": _telegram_item_receipt(92, "telegram-file:92")}
            receipts = {
                "missing": [first_receipt],
                "generic": [{}, {}],
                "reversed": [second_receipt, first_receipt],
                "duplicate": [first_receipt, first_receipt],
            }[receipt_mode]
            return {
                "message": "partial",
                "file_ingestions": [{**receipt, "telegram_stage_ready": True} for receipt in receipts],
            }

        monkeypatch.setattr(bridge, "_prepare_document", prepare)
        monkeypatch.setattr(bridge, "_backend_json", backend_json)
        await bridge._process_update(_telegram, _backend, update, cached_response=None)  # noqa: SLF001

    monkeypatch.setattr(bridge, "_process_update", process)
    bridge._inbox.store(first)  # noqa: SLF001
    bridge._inbox.store(second)  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        pending = bridge._inbox.contiguous_pending_rows("chat:5001", 851, limit=2)  # noqa: SLF001
        assert [item["update_id"] for item in pending] == [851, 852]
        assert [item["attempts"] for item in pending] == [1, 1]
        assert bridge._inbox.stats() == {"pending": 2, "dead_letter": 0}  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_partial_album_409_is_retryable_and_never_immediate_dead_letter(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(861, 101, "file-a", caption="Оба скана")
    second = _album_update(862, 102, "file-b")
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)

    async def prepare(_client, message, _carrier):  # noqa: ANN001
        return {
            "filename": f"{message['message_id']}.txt",
            "mime_type": "text/plain",
            "content_base64": "QQ==",
            "source_ref": f"telegram-file:{message['message_id']}",
        }

    async def conflict(*_args, **_kwargs):
        raise PermanentUpdateError("synthetic post-persist conflict", status_code=409)

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", conflict)
    bridge._inbox.store(first)  # noqa: SLF001
    bridge._inbox.store(second)  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        pending = bridge._inbox.contiguous_pending_rows("chat:5001", 861, limit=2)  # noqa: SLF001
        assert [item["attempts"] for item in pending] == [1, 1]
        assert bridge._inbox.stats() == {"pending": 2, "dead_letter": 0}  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_album_retry_replays_received_prefix_and_mutates_only_missing_sibling(
    tmp_path, monkeypatch
) -> None:
    bridge = _bridge(tmp_path)
    first = _album_update(863, 103, "file-a", caption="Оба скана")
    second = _album_update(864, 104, "file-b")
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)
    durable_mutations: dict[int, int] = {}
    stage_calls = 0
    final_calls = 0
    sent: list[str] = []

    async def prepare(_client, message, _carrier):  # noqa: ANN001
        message_id = int(message["message_id"])
        return {
            "filename": f"{message_id}.txt",
            "mime_type": "text/plain",
            "content_base64": "QQ==",
            "source_ref": f"telegram-file:{message_id}",
        }

    async def backend_json(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
        nonlocal final_calls, stage_calls
        if payload.get("document_stage_only") is True:
            stage_calls += 1
            documents = payload["documents"]
            if stage_calls == 1:
                first_id = int(documents[0]["telegram_message_id"])
                durable_mutations[first_id] = durable_mutations.get(first_id, 0) + 1
                raise RuntimeError("synthetic process death after one persisted sibling")
            receipts = []
            for item in documents:
                message_id = int(item["telegram_message_id"])
                if message_id not in durable_mutations:
                    durable_mutations[message_id] = 1
                receipts.append(
                    {
                        "telegram_item_receipt": _telegram_item_receipt(message_id, str(item["source_ref"])),
                        "telegram_stage_ready": True,
                    }
                )
            return {"file_ingestions": receipts}
        final_calls += 1
        return {"message": "Один итог", "message_format": "plain"}

    async def typing(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def send(_telegram, _chat_id, message, **_kwargs):  # noqa: ANN001
        sent.append(str(message))

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend_json)
    monkeypatch.setattr(bridge, "_typing_loop", typing)
    monkeypatch.setattr(bridge, "_send_message", send)
    bridge._inbox.store(first)  # noqa: SLF001
    bridge._inbox.store(second)  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001
    try:
        await bridge._run_update(  # noqa: SLF001
            object(),
            object(),
            bridge._inbox.pending()[0],  # noqa: SLF001
        )
        pending = bridge._inbox.pending(now=time.time() + 60)  # noqa: SLF001
        assert [item["attempts"] for item in pending] == [1]
        assert bridge._inbox.stats() == {"pending": 2, "dead_letter": 0}  # noqa: SLF001
        assert durable_mutations == {103: 1}

        await bridge._run_update(object(), object(), pending[0])  # noqa: SLF001
        assert bridge._inbox.stats() == {"pending": 0, "dead_letter": 0}  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert durable_mutations == {103: 1, 104: 1}
    assert stage_calls == 2 and final_calls == 1
    assert sent == ["Один итог"]


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


@pytest.mark.asyncio
async def test_duplicate_album_message_identity_is_quarantined_before_dispatch(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    monkeypatch.setattr("friday.telegram_bridge._transport._ALBUM_SETTLE_SEC", 0.0)
    bridge._inbox.store(_album_update(833, 83, "file-a", caption="Оба"))  # noqa: SLF001
    bridge._inbox.store(_album_update(834, 83, "file-b"))  # noqa: SLF001
    row = bridge._inbox.pending()[0]  # noqa: SLF001
    bridge._stopping = True  # noqa: SLF001
    dispatched = False

    async def must_not_dispatch(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True

    async def no_notice(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_process_update", must_not_dispatch)
    monkeypatch.setattr(bridge, "_notify_dead_letter", no_notice)
    try:
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        assert bridge._inbox.stats() == {"pending": 0, "dead_letter": 2}  # noqa: SLF001
        assert not dispatched
    finally:
        bridge._inbox.close()  # noqa: SLF001
