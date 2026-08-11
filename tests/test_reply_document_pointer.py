from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
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
    extra_metadata: dict | None = None,
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
        content_hash=hashlib.sha256(f"content {label}".encode()).hexdigest(),
        metadata_json={
            "filename": f"{label}.docx",
            "uploaded_by": uploader,
            **(extra_metadata or {}),
        },
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


def test_server_reply_to_assistant_uses_only_that_answers_authorized_file_lineage(
    settings,
    monkeypatch,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    captured: dict[str, dict] = {}
    with TestClient(app) as client:
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        tenant = LEGACY_OWNER_USER_ID
        storage = app.state.storage
        storage.update_user(uploader, preset_key="user")
        selected = _stored_reply_file(storage, tenant, uploader, "ANSWER-A")
        selected_second = _stored_reply_file(storage, tenant, uploader, "ANSWER-A-SECOND")
        newer = _stored_reply_file(storage, tenant, uploader, "NEWER-B")
        deleted = _stored_reply_file(storage, tenant, uploader, "DELETED-C", deleted=True)
        conversation = storage.create_conversation(uploader, title="assistant lineage")
        answer_a = storage.store_message(
            conversation["id"],
            uploader,
            "assistant",
            "answer A",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [selected.id],
            },
        )
        answer_deleted = storage.store_message(
            conversation["id"],
            uploader,
            "assistant",
            "answer deleted",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [deleted.id],
            },
        )
        answer_multi = storage.store_message(
            conversation["id"],
            uploader,
            "assistant",
            "answer over two files",
            metadata={
                "attachment_context_used": True,
                # Keep the inverse of storage chronology to prove that the
                # structural order, not SQL result order, reaches Runtime.
                "conversation_attachment_raw_ids": [selected.id, selected_second.id],
            },
        )
        answer_empty = storage.store_message(
            conversation["id"],
            uploader,
            "assistant",
            "answer without a file",
            metadata={"attachment_context_used": False},
        )
        answer_forged_context = storage.store_message(
            conversation["id"],
            uploader,
            "assistant",
            "answer with unconfirmed internal pointer",
            metadata={
                "attachment_context_used": False,
                "conversation_attachment_raw_ids": [selected.id],
            },
        )
        other_conversation = storage.create_conversation(uploader, title="other own dialogue")
        other_answer = storage.store_message(
            other_conversation["id"],
            uploader,
            "assistant",
            "answer from another own dialogue",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [selected.id],
            },
        )
        storage.ensure_user("foreign-user", preset_key="user")
        foreign_conversation = storage.create_conversation("foreign-user", title="foreign")
        foreign_answer = storage.store_message(
            foreign_conversation["id"],
            "foreign-user",
            "assistant",
            "foreign answer",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [selected.id],
            },
        )
        storage.set_channel_conversation(uploader, "telegram", "5001", conversation["id"])

        async def chat_spy(user_id, message, **kwargs):
            captured[message] = {"user_id": user_id, **kwargs}
            return {
                "conversation_id": conversation["id"],
                "message": "ok",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", chat_spy)
        cases = {
            "valid": str(answer_a["id"]),
            "multi": str(answer_multi["id"]),
            "missing": "msg_missing_lineage",
            "deleted": str(answer_deleted["id"]),
            "empty": str(answer_empty["id"]),
            "unconfirmed": str(answer_forged_context["id"]),
            "other-dialogue": str(other_answer["id"]),
            "foreign": str(foreign_answer["id"]),
        }
        for index, (label, message_id) in enumerate(cases.items(), start=1):
            response = _bridge_call(
                client,
                scoped,
                "POST",
                "/api/chat",
                {
                    "message": label,
                    "source_ref": f"telegram-update:assistant-lineage-{index}",
                    "telegram_message_id": 500 + index,
                    "telegram_user": {"id": 1001, "first_name": "Alice"},
                    "reply_source_message_id": message_id,
                    # A caller list cannot compete with the structural reply.
                    "attachments": [{"raw_object_id": newer.id}],
                },
                user="1001",
            )
            assert response.status_code == 200, response.text

    assert captured["valid"]["attachments"] == [{"raw_object_id": selected.id}]
    assert captured["multi"]["attachments"] == [
        {"raw_object_id": selected.id},
        {"raw_object_id": selected_second.id},
    ]
    for label in (
        "missing",
        "deleted",
        "empty",
        "unconfirmed",
        "other-dialogue",
        "foreign",
    ):
        assert captured[label]["attachments"] == []
    assert all(row["reply_assistant_reference"] is True for row in captured.values())


def test_server_reply_to_assistant_metadata_is_end_to_end_and_publicly_redacted(
    settings,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    with TestClient(app) as client:
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        tenant = LEGACY_OWNER_USER_ID
        storage = app.state.storage
        storage.update_user(uploader, preset_key="user")
        selected = _stored_reply_file(
            storage,
            tenant,
            uploader,
            "SELECTED-ANSWER-FILE",
            extra_metadata={"title": "Selected answer synthetic title"},
        )
        newer = _stored_reply_file(
            storage,
            tenant,
            uploader,
            "NEWER-DECOY-FILE",
            extra_metadata={"title": "Newer decoy synthetic title"},
        )
        conversation = storage.create_conversation(uploader, title="assistant reply e2e")
        source_answer = storage.store_message(
            conversation["id"],
            uploader,
            "assistant",
            "prior answer over selected file",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [selected.id],
            },
        )
        storage.set_channel_conversation(uploader, "telegram", "5001", conversation["id"])

        response = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "покажи метаданные его",
                "source_ref": "telegram-update:assistant-lineage-e2e",
                "telegram_message_id": 601,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_source_message_id": str(source_answer["id"]),
                "attachments": [{"raw_object_id": newer.id}],
            },
            user="1001",
        )
        assert response.status_code == 200, response.text
        history = _bridge_call(
            client,
            scoped,
            "GET",
            f"/api/conversations/{conversation['id']}/messages",
            user="1001",
        )
        assert history.status_code == 200, history.text

        rows = storage.get_conversation_messages(
            conversation["id"],
            user_id=uploader,
            limit=100,
        )

    assert "Selected answer synthetic title" in response.json()["message"]
    assert "Newer decoy synthetic title" not in response.json()["message"]
    user_metadata = json.loads(str(rows[-2].get("metadata_json") or "{}"))
    assistant_metadata = json.loads(str(rows[-1].get("metadata_json") or "{}"))
    assert user_metadata["attachment_origin"] == "reply_assistant"
    assert user_metadata["conversation_attachment_raw_ids"] == [selected.id]
    assert "conversation_uploaded_raw_ids" not in user_metadata
    assert assistant_metadata["attachment_context_used"] is True
    assert assistant_metadata["conversation_attachment_raw_ids"] == [selected.id]
    for public_payload in (response.json(), history.json()):
        encoded = json.dumps(public_payload, ensure_ascii=False)
        assert selected.id not in encoded
        assert newer.id not in encoded
        assert "conversation_attachment_raw_ids" not in encoded


def _synthetic_metadata_odt(
    *,
    transport_marker: bytes = b"",
    title: str = "Canonical alias title",
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.comment = transport_marker
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr(
            "content.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>Synthetic body.</text:p></office:text></office:body>
</office:document-content>""",
        )
        archive.writestr(
            "meta.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <office:meta><dc:title>"""
            + title
            + """</dc:title></office:meta>
</office:document-meta>""",
        )
    return payload.getvalue()


class _D10RoutingLLM:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages,
        *,
        temperature=None,
        max_tokens=None,
        tools=None,
        tool_choice=None,
        **_kwargs,
    ):
        del temperature, max_tokens
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, Mapping)
        }
        last_user = next(
            (
                str(item.get("content") or "")
                for item in reversed(messages)
                if isinstance(item, Mapping) and str(item.get("role") or "") == "user"
            ),
            "",
        )
        last_user_index = max(
            (
                index
                for index, item in enumerate(messages)
                if isinstance(item, Mapping) and str(item.get("role") or "") == "user"
            ),
            default=-1,
        )
        tool_result_after_user = any(
            isinstance(item, Mapping) and str(item.get("role") or "") == "tool"
            for item in messages[last_user_index + 1 :]
        )
        self.calls.append(
            {
                "user": last_user,
                "tool_choice": tool_choice,
                "offered": offered,
            }
        )
        common = {"_queue_wait_sec": 0.0, "_offered_tool_names": sorted(offered)}
        if any(
            "FRIDAY_DOCUMENT_DETAIL_DATA" in str(item.get("content") or "")
            for item in messages
            if isinstance(item, Mapping)
        ):
            return {**common, "content": '{"details":[]}', "tool_calls": None}
        if tool_choice == "workspace_create":
            return {
                **common,
                "content": "",
                "tool_calls": [
                    {
                        "id": "d10-workspace-create",
                        "function": {
                            "name": "workspace_create",
                            "arguments": json.dumps(
                                {
                                    "filename": "model-must-not-choose.txt",
                                    "content": "17-ДСП/1\nMETA-EXPORT-1\n",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        if "metadata-export.docx" in last_user and not tool_result_after_user:
            return {
                **common,
                "content": "",
                "tool_calls": [
                    {
                        "id": "d10-regular-make-file",
                        "function": {
                            "name": "make_file",
                            "arguments": json.dumps(
                                {
                                    "kind": "docx",
                                    "title": "metadata export",
                                    "blocks": [
                                        {"kind": "text", "text": "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ"},
                                        {"kind": "text", "text": "17-ДСП/1"},
                                        {"kind": "text", "text": "10 августа 2026 года"},
                                        {"kind": "text", "text": "Иван Иванович Иванов"},
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        if "metadata-export.docx" in last_user:
            return {**common, "content": "Файл отправлен в этот чат.", "tool_calls": None}
        if "workspace_create" in last_user and tool_result_after_user:
            return {
                **common,
                "content": "Файл mcp-metadata.txt во внешнем MCP outbox создан.",
                "tool_calls": None,
            }
        return {**common, "content": "Синтетический ответ.", "tool_calls": None}


def _workspace_tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic routing contract",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_exact_d10_three_turn_api_keeps_reply_source_and_forces_only_workspace_effect(
    settings,
    monkeypatch,
) -> None:
    from friday.agent_runtime import AgentContext
    from friday.execution_kernel import ToolResult
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    llm = _D10RoutingLLM()
    executed: list[tuple[str, dict[str, Any]]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        workspace = "workspace_create" in str(message)
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=str(kwargs.get("person_id") or user_id),
            conversation_history=list(kwargs.get("prior_history") or []),
            interaction_mode=str(kwargs.get("interaction_mode") or "dialogue"),
            search_query=str(message),
            outward_verdict=("материал", None),
            # Exercise the exact production seam: a structural stage may have
            # consumed the model remainder, but it did not consume the explicit
            # current-user MCP authority.
            structural_answer="Синтетический структурный этап завершён." if workspace else "",
            open_remainder="",
            remainder_known=workspace,
        )

    with TestClient(app) as client:
        app.state.agent.llm = llm
        kernel = app.state.agent.kernel
        base_definitions = kernel.get_tool_definitions
        base_execute = kernel.execute

        def definitions(actor, topic=None):  # noqa: ANN001
            result = list(base_definitions(actor, topic=topic))
            names = {
                str((item.get("function") or {}).get("name") or "")
                for item in result
                if isinstance(item, Mapping)
            }
            if "workspace_create" not in names:
                result.append(_workspace_tool_schema("workspace_create"))
            return result

        async def execute(name, arguments, *, actor=None):  # noqa: ANN001
            executed.append((str(name), dict(arguments)))
            if name == "workspace_create":
                return ToolResult(name, True, data={"created": True})
            return await base_execute(name, arguments, actor=actor)

        monkeypatch.setattr(kernel, "get_tool_definitions", definitions)
        monkeypatch.setattr(kernel, "execute", execute)
        monkeypatch.setattr(app.state.agent, "_prepare_context", prepare)
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        app.state.storage.update_user(uploader, preset_key="owner")
        source_ref = "telegram-file:D10-ROUTING-SOURCE"
        upload = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": (
                    "Покажи все технические метаданные контейнера и все видимые реквизиты этого документа."
                ),
                "source_ref": "telegram-update:d10-routing-1",
                "telegram_message_id": 7101,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "document": {
                    "filename": "d10-routing.odt",
                    "mime_type": "application/vnd.oasis.opendocument.text",
                    "media_kind": "document",
                    "source_ref": source_ref,
                    "content_base64": base64.b64encode(_synthetic_metadata_odt()).decode("ascii"),
                },
            },
            user="1001",
        )
        assert upload.status_code == 200, upload.text
        conversation_id = str(upload.json()["conversation_id"])
        selected_raw_id = app.state.storage.resolve_owned_file_source_ref(
            LEGACY_OWNER_USER_ID,
            uploader,
            source_ref,
        )
        assert selected_raw_id

        # A second authorised file makes the old broad ordinal match observable:
        # it must not replace the structural reply target with catalog ambiguity.
        decoy = _stored_reply_file(app.state.storage, LEGACY_OWNER_USER_ID, uploader, "D10-DECOY")
        app.state.storage.store_message(
            conversation_id,
            uploader,
            "user",
            "synthetic prior decoy upload",
            metadata={
                "had_attachments": True,
                "attachment_count": 1,
                "conversation_attachment_raw_ids": [decoy.id],
                "conversation_uploaded_raw_ids": [decoy.id],
            },
        )
        app.state.storage.store_message(
            conversation_id,
            uploader,
            "assistant",
            "synthetic decoy acknowledgement",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [decoy.id],
            },
        )

        regular = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": (
                    "Создай обычный Word-файл metadata-export.docx по процитированному документу. "
                    "Включи ровно четыре строки: гриф, номер документа, видимую дату документа "
                    "и подписанта из предыдущего ответа."
                ),
                "conversation_id": conversation_id,
                "source_ref": "telegram-update:d10-routing-2",
                "telegram_message_id": 7102,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_source_ref": source_ref,
            },
            user="1001",
        )
        assert regular.status_code == 200, regular.text
        make_file_before_workspace = sum(name == "make_file" for name, _arguments in executed)

        workspace_prompt = (
            "Используй именно workspace_create и создай в MCP outbox файл mcp-metadata.txt. "
            "Первая строка — только значение номера документа без подписи. Вторая строка — "
            "только значение контрольного маркера без подписи. Никаких других строк."
        )
        workspace = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": workspace_prompt,
                "conversation_id": conversation_id,
                "source_ref": "telegram-update:d10-routing-3",
                "telegram_message_id": 7103,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_source_ref": source_ref,
            },
            user="1001",
        )
        assert workspace.status_code == 200, workspace.text
        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=uploader,
            limit=100,
        )

    workspace_calls = [arguments for name, arguments in executed if name == "workspace_create"]
    assert workspace_calls == [{"filename": "mcp-metadata.txt", "content": "17-ДСП/1\nMETA-EXPORT-1\n"}]
    assert sum(name == "make_file" for name, _arguments in executed) == make_file_before_workspace
    forced = [call for call in llm.calls if call["tool_choice"] == "workspace_create"]
    assert len(forced) == 1
    assert "workspace_create" in forced[0]["offered"]
    assert workspace.json()["tools_used"] == ["workspace_create"]
    assert workspace.json().get("files") == []
    final_user = next(row for row in reversed(rows) if row.get("role") == "user")
    final_metadata = json.loads(str(final_user.get("metadata_json") or "{}"))
    assert final_metadata["conversation_attachment_raw_ids"] == [selected_raw_id]
    assert final_metadata["attachment_origin"] == "reply_reference"


def test_unresolved_or_foreign_workspace_reply_pointer_has_no_effect_or_late_file(
    settings,
    monkeypatch,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    executed: list[str] = []

    async def forbidden_late_file(*_args, **_kwargs):
        raise AssertionError("unresolved structural reply fell through to late make_file")

    prompt = (
        "Используй именно workspace_create и создай в MCP outbox файл mcp-metadata.txt. "
        "Первая строка — номер документа."
    )
    with TestClient(app) as client:
        base_execute = app.state.agent.kernel.execute

        async def execute(name, arguments, *, actor=None):  # noqa: ANN001
            executed.append(str(name))
            return await base_execute(name, arguments, actor=actor)

        monkeypatch.setattr(app.state.agent.kernel, "execute", execute)
        monkeypatch.setattr(app.state.agent, "_file_for_a_request_that_wanted_one", forbidden_late_file)
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        app.state.storage.update_user(uploader, preset_key="owner")
        _stored_reply_file(app.state.storage, LEGACY_OWNER_USER_ID, "foreign-uploader", "FOREIGN-D10")
        for index, pointer in enumerate(
            ("telegram-file:MISSING-D10", "telegram-file:FOREIGN-D10"),
            start=1,
        ):
            response = _bridge_call(
                client,
                scoped,
                "POST",
                "/api/chat",
                {
                    "message": prompt,
                    "source_ref": f"telegram-update:d10-denied-{index}",
                    "telegram_message_id": 7200 + index,
                    "telegram_user": {"id": 1001, "first_name": "Alice"},
                    "reply_document_source_ref": pointer,
                },
                user="1001",
            )
            assert response.status_code == 200, response.text
            assert response.json().get("files") == []
            assert response.json().get("tools_used") == []

    assert executed == []


def test_workspace_output_line_preserves_authorized_second_document_selector(
    settings,
    storage,
) -> None:
    from friday.agent_runtime import (
        AgentRuntime,
        _attachment_reference_kind,
        _workspace_reply_attachment_selector_message,
    )

    tenant = "workspace-selector-tenant"
    uploader = "workspace-selector-uploader"
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user(uploader, preset_key="user")
    reply_source = _stored_reply_file(storage, tenant, uploader, "WORKSPACE-REPLY-FIRST")
    selected_second = _stored_reply_file(storage, tenant, uploader, "WORKSPACE-SELECTED-SECOND")
    prompt = "Используй workspace_create и создай out.txt. Первая строка — значение из второго документа."
    selector = _workspace_reply_attachment_selector_message(prompt)
    runtime = AgentRuntime(settings, storage)

    restored, expected = runtime._resolve_conversation_attachment_reference(  # noqa: SLF001
        selector,
        [],
        tenant_id=tenant,
        person_id=uploader,
        already_supplied_count=0,
        reference_kind=_attachment_reference_kind(selector),
        additional_raw_ids=(reply_source.id,),
    )

    assert expected == 1
    assert [str(item.get("raw_object_id") or "") for item in restored] == [selected_second.id]


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


def test_content_dedup_binds_fresh_telegram_ref_to_canonical_odt_for_reply_metadata(
    settings,
    monkeypatch,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    payloads = [
        _synthetic_metadata_odt(transport_marker=b"first transport"),
        _synthetic_metadata_odt(transport_marker=b"resaved transport"),
    ]
    assert payloads[0] != payloads[1]

    async def upload_ack(user_id, _message, **kwargs):
        del user_id
        conversation_id = str(kwargs.get("conversation_id") or "")
        if not conversation_id:
            conversation_id = str(app.state.storage.create_conversation(uploader)["id"])
        return {
            "conversation_id": conversation_id,
            "message": "accepted",
            "context": {"interaction_mode": "dialogue"},
        }

    with TestClient(app) as client:
        canonical_chat = app.state.agent.chat
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        app.state.storage.update_user(uploader, preset_key="user")
        monkeypatch.setattr(app.state.agent, "chat", upload_ack)

        raw_ids: list[str] = []
        for index, file_ref in enumerate(("ALIAS-ODT-A", "ALIAS-ODT-B"), start=1):
            encoded = base64.b64encode(payloads[index - 1]).decode("ascii")
            response = _bridge_call(
                client,
                scoped,
                "POST",
                "/api/chat",
                {
                    "message": "",
                    "source_ref": f"telegram-update:alias-upload-{index}",
                    "telegram_message_id": 400 + index,
                    "telegram_user": {"id": 1001, "first_name": "Alice"},
                    "document": {
                        "filename": "canonical-alias.odt",
                        "mime_type": "application/vnd.oasis.opendocument.text",
                        "media_kind": "document",
                        "source_ref": f"telegram-file:{file_ref}",
                        "content_base64": encoded,
                    },
                },
                user="1001",
            )
            assert response.status_code == 200, response.text
            resolved = app.state.storage.resolve_owned_file_source_ref(
                LEGACY_OWNER_USER_ID,
                uploader,
                f"telegram-file:{file_ref}",
            )
            assert resolved is not None
            raw_ids.append(resolved)

        assert raw_ids[0] == raw_ids[1]
        assert (
            app.state.storage.resolve_owned_file_source_ref(
                LEGACY_OWNER_USER_ID,
                uploader,
                "telegram-file:ALIAS-ODT-B",
            )
            == raw_ids[0]
        )

        monkeypatch.setattr(app.state.agent, "chat", canonical_chat)
        metadata = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "покажи метаданные этого файла",
                "source_ref": "telegram-update:alias-metadata",
                "telegram_message_id": 403,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_source_ref": "telegram-file:ALIAS-ODT-B",
            },
            user="1001",
        )
        assert metadata.status_code == 200, metadata.text
        assert "Canonical alias title" in metadata.json()["message"]


@pytest.mark.anyio
async def test_same_odt_text_with_changed_technical_metadata_keeps_second_reply_target(
    settings,
    storage,
) -> None:
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    tenant = "synthetic-tenant"
    uploader = "synthetic-uploader"
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user(uploader, preset_key="user")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    first_ref = "telegram-file:METADATA-A"
    second_ref = "telegram-file:METADATA-B"

    first = await pipeline.ingest_file(
        tenant,
        None,
        _synthetic_metadata_odt(transport_marker=b"transport-a", title="Synthetic title A"),
        filename="same-body.odt",
        mime_type="application/vnd.oasis.opendocument.text",
        metadata={"uploaded_by": uploader},
        source_ref=first_ref,
    )
    second = await pipeline.ingest_file(
        tenant,
        None,
        _synthetic_metadata_odt(transport_marker=b"transport-b", title="Synthetic title B"),
        filename="same-body-resaved.odt",
        mime_type="application/vnd.oasis.opendocument.text",
        metadata={"uploaded_by": uploader},
        source_ref=second_ref,
    )

    first_raw_id = str(first.get("raw_object_id") or "")
    second_raw_id = str(second.get("raw_object_id") or "")
    assert first_raw_id and second_raw_id and first_raw_id != second_raw_id
    assert storage.resolve_owned_file_source_ref(tenant, uploader, first_ref) == first_raw_id
    assert storage.resolve_owned_file_source_ref(tenant, uploader, second_ref) == second_raw_id
    second_raw = storage.get_raw_object(second_raw_id, tenant)
    assert second_raw is not None
    second_metadata = json.loads(str(second_raw.get("metadata_json") or "{}"))
    assert second_metadata["title"] == "Synthetic title B"
    assert second_metadata["title"] != "Synthetic title A"
