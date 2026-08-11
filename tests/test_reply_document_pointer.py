from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.security import sign_bridge_request
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id
from friday.telegram_bridge import TelegramBridge, TelegramConfig


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload or {"ok": True, "result": {}}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def post(self, url, json=None, **_kwargs):
        self.calls.append((url, json))
        return _Response()


class _Backend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, method, url, *, content=None, headers=None):
        path = urlsplit(url).path
        body = json.loads(content.decode("utf-8")) if content else None
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        return _Response({"message": "ok", "message_format": "plain"})


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "reply-pointer.sqlite3"),
        )
    )


@pytest.mark.asyncio
async def test_no_caption_reply_sends_exact_file_pointer_without_redownload(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    backend = _Backend()
    update = {
        "update_id": 9101,
        "message": {
            "message_id": 102,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "text": "покажи его метаданные",
            "reply_to_message": {
                "message_id": 101,
                "document": {
                    "file_id": "REPLY_FILE_ABC",
                    "file_name": "без подписи.docx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                },
            },
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    [chat] = [call for call in backend.calls if call["path"] == "/api/chat"]
    assert chat["body"]["reply_document_source_ref"] == "telegram-file:REPLY_FILE_ABC"
    assert "document" not in chat["body"]
    assert "reply_to" not in chat["body"]
    assert not any(url.endswith("/getFile") for url, _payload in telegram.calls)


@pytest.mark.asyncio
async def test_current_media_wins_over_replied_file_pointer(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    backend = _Backend()

    async def prepared(*_args, **_kwargs):
        return {
            "filename": "current.txt",
            "mime_type": "text/plain",
            "content_base64": "Y3VycmVudA==",
            "source_ref": "telegram-file:CURRENT",
            "media_kind": "document",
        }

    monkeypatch.setattr(bridge, "_prepare_document", prepared)
    update = {
        "update_id": 9102,
        "message": {
            "message_id": 202,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "caption": "сравни с ним",
            "document": {"file_id": "CURRENT", "file_name": "current.txt"},
            "reply_to_message": {
                "message_id": 201,
                "document": {"file_id": "OLDER", "file_name": "older.txt"},
            },
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    [chat] = [call for call in backend.calls if call["path"] == "/api/chat"]
    assert chat["body"]["document"]["source_ref"] == "telegram-file:CURRENT"
    assert "reply_document_source_ref" not in chat["body"]


def _signed_headers(secret: str, method: str, path: str, body: bytes, user: str, chat: str):
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": user,
        "X-Friday-Chat": chat,
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            secret,
            timestamp=timestamp,
            method=method,
            path=path,
            external_user_id=user,
            chat_id=chat,
            nonce=nonce,
            body=body,
        ),
    }


def _bridge_call(
    client,
    settings,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    user: str = "5001",
):
    body = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if payload is not None
        else b""
    )
    return client.request(
        method,
        path,
        content=body or None,
        headers=_signed_headers(settings.telegram_bridge_secret, method, path, body, user, "5001"),
    )


def _stored_reply_file(
    storage,
    tenant: str,
    uploader: str,
    label: str,
    *,
    ignored=False,
    deleted=False,
    namespaced=True,
):
    base_ref = f"telegram-file:{label}"
    namespace = hashlib.sha256(uploader.encode("utf-8")).hexdigest()[:24]
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="upload",
        source_ref=f"uploader:{namespace}:{base_ref}" if namespaced else base_ref,
        raw_content=f"content {label}",
        content_type="file",
        metadata_json={"filename": f"{label}.docx", "uploaded_by": uploader},
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant,
            raw_object_id=raw.id,
            status=InboxStatus.IGNORED if ignored else InboxStatus.PENDING,
        )
    )
    if deleted:
        storage.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-11T00:00:00+00:00' WHERE id=?",
            (raw.id,),
        )
        storage.commit()
    return raw


def test_server_resolves_only_current_uploaders_live_nonignored_reply_file(settings, monkeypatch) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    captured: dict = {}
    with TestClient(app) as client:
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        actor = me.json()["actor"]
        uploader = str(actor["user_id"])
        tenant = LEGACY_OWNER_USER_ID
        storage = app.state.storage
        # An allowed group participant starts deliberately narrow; this test is
        # about a user who has file-read authority in the shared archive.
        storage.update_user(uploader, preset_key="user")
        valid = _stored_reply_file(storage, tenant, uploader, "VALID")
        _stored_reply_file(storage, tenant, "another-person", "FOREIGN")
        _stored_reply_file(storage, tenant, uploader, "IGNORED", ignored=True)
        _stored_reply_file(storage, tenant, uploader, "DELETED", deleted=True)
        legacy = _stored_reply_file(storage, tenant, uploader, "LEGACY", namespaced=False)

        valid_pointer = "telegram-file:VALID"
        assert storage.resolve_owned_file_source_ref(tenant, uploader, valid_pointer) == valid.id
        assert storage.resolve_owned_file_source_ref(tenant, uploader, "telegram-file:LEGACY") == legacy.id
        for label in ("FOREIGN", "IGNORED", "DELETED"):
            assert storage.resolve_owned_file_source_ref(tenant, uploader, f"telegram-file:{label}") is None
        resolution_calls: list[tuple[str, str, str]] = []
        original_resolver = storage.resolve_owned_file_source_ref

        def observed_resolver(user_id, uploaded_by, source_ref):
            resolution_calls.append((user_id, uploaded_by, source_ref))
            return original_resolver(user_id, uploaded_by, source_ref)

        monkeypatch.setattr(storage, "resolve_owned_file_source_ref", observed_resolver)

        async def chat_spy(user_id, message, **kwargs):
            captured.update(user_id=user_id, message=message, kwargs=kwargs)
            conversation = storage.create_conversation(uploader, title="reply pointer")
            return {
                "conversation_id": conversation["id"],
                "message": "ok",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", chat_spy)
        response = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "покажи его метаданные",
                "source_ref": "telegram-update:reply-pointer-1",
                "telegram_message_id": 301,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_source_ref": valid_pointer,
            },
            user="1001",
        )
        assert response.status_code == 200, response.text
        assert resolution_calls == [(tenant, uploader, valid_pointer)]

    assert captured["kwargs"]["attachments"] == [{"raw_object_id": valid.id}]
    assert captured["kwargs"]["quoted_attachment_reference"] is True
