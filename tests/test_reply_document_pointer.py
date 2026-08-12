from __future__ import annotations

import asyncio
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
import pyzipper
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.security import sign_bridge_request
from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)
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
                    "file_unique_id": "STABLE_REPLY_FILE_ABC",
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
    assert chat["body"]["reply_document_message_id"] == 101
    assert chat["body"]["reply_document_file_unique_id"] == "STABLE_REPLY_FILE_ABC"
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
    content: str | None = None,
):
    base_ref = f"telegram-file:{label}"
    namespace = hashlib.sha256(uploader.encode("utf-8")).hexdigest()[:24]
    raw_content = content if content is not None else f"content {label}"
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="upload",
        source_ref=f"uploader:{namespace}:{base_ref}" if namespaced else base_ref,
        raw_content=raw_content,
        content_type="file",
        content_hash=hashlib.sha256(raw_content.encode()).hexdigest(),
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
    body: tuple[str, ...] = ("Synthetic body.",),
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
 <office:body><office:text>"""
            + "".join(f"<text:p>{line}</text:p>" for line in body)
            + """</office:text></office:body>
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
                                    "content": "№ 17-ДСП/1\nMETA-EXPORT-1\n",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        if "metadata-export.docx" in last_user and not tool_result_after_user:
            raise AssertionError("closed direct D10 export reached the model")
        if "metadata-export.docx" in last_user:
            raise AssertionError("regular D10 export unexpectedly entered an agentic follow-up")
        if "workspace_create" in last_user and tool_result_after_user:
            raise AssertionError("exact workspace success requested a post-effect LLM call")
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

    scoped = replace(settings, verify_answers=True, shared_archive=True)
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

        async def forbidden_generic_verifier(*_args, **_kwargs):
            raise AssertionError("direct attachment export entered the generic verifier/repair path")

        monkeypatch.setattr(app.state.agent, "_verify_response", forbidden_generic_verifier)
        monkeypatch.setattr(app.state.agent, "_repair_once", forbidden_generic_verifier)
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
                    "content_base64": base64.b64encode(
                        _synthetic_metadata_odt(
                            body=(
                                "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
                                "ПРИКАЗ № 17-ДСП/1",
                                "Дата документа: 10 августа 2026 года",
                                "Контрольный маркер: META-EXPORT-1",
                                "Подписант: начальник отдела Иван Иванович Иванов",
                            )
                        )
                    ).decode("ascii"),
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
        regular_payload = regular.json()
        regular_files = regular_payload.get("files") or []
        assert regular_payload["tools_used"] == ["make_file"]
        assert make_file_before_workspace == 1
        assert len(regular_files) == 1
        assert regular_files[0]["filename"] == "metadata-export.docx"
        import docx

        document = docx.Document(
            io.BytesIO(base64.b64decode(regular_files[0]["content_base64"], validate=True))
        )
        paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
        assert paragraphs[1:] == [
            "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
            "17-ДСП/1",
            "10 августа 2026 года",
            "Иван Иванович Иванов",
        ]
        regular_model_calls = [call for call in llm.calls if "metadata-export.docx" in call["user"]]
        assert regular_model_calls == []

        workspace_prompt = (
            "Контекст проверки SYNTHETIC-D10. "
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
    assert forced == []
    assert [call for call in llm.calls if "workspace_create" in call["user"]] == []
    assert workspace.json()["tools_used"] == ["workspace_create"]
    assert workspace.json().get("files") == []
    final_user = next(row for row in reversed(rows) if row.get("role") == "user")
    final_metadata = json.loads(str(final_user.get("metadata_json") or "{}"))
    assert final_metadata["conversation_attachment_raw_ids"] == [selected_raw_id]
    assert final_metadata["attachment_origin"] == "reply_reference"


def _assert_exact_workspace_direct_effect_with_disabled_llm(
    settings,
    monkeypatch,
    *,
    workspace_prompt: str,
    expected_content: str,
) -> None:
    from friday.execution_kernel import ToolResult
    from friday.server import create_app

    scoped = replace(settings, verify_answers=True, shared_archive=True)
    app = create_app(scoped)
    llm = _D10RoutingLLM()
    llm.enabled = False
    executed: list[tuple[str, dict[str, Any]]] = []

    with TestClient(app) as client:
        app.state.agent.llm = llm
        kernel = app.state.agent.kernel
        base_definitions = kernel.get_tool_definitions

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

        async def execute(name, arguments, *, actor=None):  # noqa: ANN001, ARG001
            executed.append((str(name), dict(arguments)))
            return ToolResult(str(name), True, data={"created": True})

        monkeypatch.setattr(kernel, "get_tool_definitions", definitions)
        monkeypatch.setattr(kernel, "execute", execute)
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        app.state.storage.update_user(uploader, preset_key="owner")
        source_ref = "telegram-file:D10-DISABLED-LLM"
        source = "ПРИКАЗ № 17-ДСП/1\nКонтрольный маркер: META-EXPORT-1\n"
        _stored_reply_file(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            uploader,
            "D10-DISABLED-LLM",
            content=source,
            extra_metadata={
                "extraction_success": True,
                "extraction_chars": len(source),
                "mime_type": "text/plain",
            },
        )
        conversation = app.state.storage.create_conversation(
            uploader,
            title="disabled llm exact workspace",
        )
        workspace = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": workspace_prompt,
                "conversation_id": str(conversation["id"]),
                "source_ref": "telegram-update:d10-disabled-llm",
                "telegram_message_id": 7199,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_source_ref": source_ref,
            },
            user="1001",
        )

    assert workspace.status_code == 200, workspace.text
    assert llm.calls == []
    assert executed == [
        (
            "workspace_create",
            {"filename": "mcp-metadata.txt", "content": expected_content},
        )
    ]
    assert workspace.json()["tools_used"] == ["workspace_create"]
    assert workspace.json().get("files") == []


def test_exact_workspace_direct_effect_does_not_require_enabled_llm(
    settings,
    monkeypatch,
) -> None:
    _assert_exact_workspace_direct_effect_with_disabled_llm(
        settings,
        monkeypatch,
        workspace_prompt=(
            "Используй именно workspace_create и создай в MCP outbox файл mcp-metadata.txt. "
            "Первая строка — только значение номера документа без подписи. Вторая строка — "
            "только значение контрольного маркера без подписи. Никаких других строк."
        ),
        expected_content="17-ДСП/1\nMETA-EXPORT-1\n",
    )


def test_exact_workspace_reversed_field_order_runs_directly_with_disabled_llm(
    settings,
    monkeypatch,
) -> None:
    _assert_exact_workspace_direct_effect_with_disabled_llm(
        settings,
        monkeypatch,
        workspace_prompt=(
            "Используй именно workspace_create и создай в MCP outbox файл mcp-metadata.txt. "
            "Первая строка — только значение контрольного маркера без подписи. Вторая строка — "
            "только значение номера документа без подписи. Никаких других строк."
        ),
        expected_content="META-EXPORT-1\n17-ДСП/1\n",
    )


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


@pytest.mark.parametrize(
    ("label", "content", "extra_metadata"),
    [
        (
            "AMBIGUOUS-WORKSPACE-SOURCE",
            "ПРИКАЗ № DOC-A\nНомер документа: DOC-B\nКонтрольный маркер: MARKER-A",
            {},
        ),
        (
            "PARTIAL-WORKSPACE-SOURCE",
            "ПРИКАЗ № DOC-A\nКонтрольный маркер: MARKER-A",
            {"text_truncated": True},
        ),
    ],
)
def test_exact_workspace_projection_denies_ambiguous_or_partial_source_before_effect(
    settings,
    monkeypatch,
    label: str,
    content: str,
    extra_metadata: dict,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    model_calls: list[object] = []
    kernel_calls: list[str] = []

    class ForbiddenLLM:
        enabled = True
        model = "forbidden-workspace-source-model"
        total_budget_sec = 5.0

        async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            model_calls.append((args, kwargs))
            raise AssertionError("ambiguous/partial exact source reached the model")

    with TestClient(app) as client:
        app.state.agent.llm = ForbiddenLLM()
        base_execute = app.state.agent.kernel.execute

        async def execute(name, arguments, *, actor=None):  # noqa: ANN001
            kernel_calls.append(str(name))
            return await base_execute(name, arguments, actor=actor)

        monkeypatch.setattr(app.state.agent.kernel, "execute", execute)
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        uploader = str(me.json()["actor"]["user_id"])
        app.state.storage.update_user(uploader, preset_key="owner")
        _stored_reply_file(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            uploader,
            label,
            content=content,
            extra_metadata={
                "extraction_success": True,
                "text_extraction_success": True,
                "mime_type": "text/plain",
                **extra_metadata,
            },
        )
        response = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": (
                    "Используй именно workspace_create и создай в MCP outbox файл exact.txt. "
                    "Первая строка — только значение номера документа без подписи. "
                    "Вторая строка — только значение контрольного маркера без подписи. "
                    "Никаких других строк."
                ),
                "source_ref": f"telegram-update:{label}",
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_source_ref": f"telegram-file:{label}",
            },
            user="1001",
        )

    assert response.status_code == 200, response.text
    assert response.json().get("tools_used") == []
    assert response.json().get("files") == []
    assert "не создан" in str(response.json().get("message") or "").casefold()
    assert not any(
        isinstance(call, tuple)
        and isinstance(call[1], Mapping)
        and call[1].get("tool_choice") == "workspace_create"
        for call in model_calls
    )
    assert kernel_calls == []


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


def test_server_resolves_only_live_nonignored_same_tenant_reply_file(settings, monkeypatch) -> None:
    import friday.server as server_module
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
        resolution_calls: list[tuple[str, tuple[str, ...]]] = []
        original_resolver = server_module.resolve_tenant_telegram_reply_aliases

        def observed_resolver(storage_arg, user_id, source_refs):
            resolution_calls.append((user_id, source_refs))
            return original_resolver(storage_arg, user_id, source_refs)

        monkeypatch.setattr(server_module, "resolve_tenant_telegram_reply_aliases", observed_resolver)

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
        assert resolution_calls == [(tenant, (valid_pointer,))]

    assert captured["kwargs"]["attachments"] == [{"raw_object_id": valid.id}]
    assert captured["kwargs"]["quoted_attachment_reference"] is True


def test_server_reply_transport_aliases_survive_file_id_churn_and_reject_conflicts(
    settings,
    monkeypatch,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    captured: list[dict[str, Any]] = []
    with TestClient(app) as client:
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        tenant = LEGACY_OWNER_USER_ID
        storage = app.state.storage
        storage.update_user(uploader, preset_key="user")

        async def chat_spy(user_id, message, **kwargs):
            captured.append({"user_id": user_id, "message": message, "kwargs": kwargs})
            conversation_id = str(kwargs.get("conversation_id") or "")
            if not conversation_id:
                conversation_id = str(storage.create_conversation(uploader, title="reply aliases")["id"])
            return {
                "conversation_id": conversation_id,
                "message": "ok",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", chat_spy)
        upload = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "",
                "source_ref": "telegram-update:reply-alias-upload",
                "telegram_message_id": 501,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "document": {
                    "filename": "reply-alias.txt",
                    "mime_type": "text/plain",
                    "media_kind": "document",
                    "source_ref": "telegram-file:ORIGINAL-FILE-ID",
                    "file_unique_id": "STABLE-UNIQUE-ID",
                    "content_base64": base64.b64encode(b"first\nlast\n").decode("ascii"),
                },
            },
            user="1001",
        )
        assert upload.status_code == 200, upload.text
        raw_id = storage.resolve_owned_file_source_ref(
            tenant,
            uploader,
            "telegram-file:ORIGINAL-FILE-ID",
        )
        assert raw_id

        reply = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "покажи его метаданные",
                "source_ref": "telegram-update:reply-alias-success",
                "telegram_message_id": 502,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_message_id": 501,
                "reply_document_file_unique_id": "STABLE-UNIQUE-ID",
                # Telegram may rotate file_id in a later reply descriptor.
                "reply_document_source_ref": "telegram-file:CHURNED-UNBOUND-ID",
            },
            user="1001",
        )
        assert reply.status_code == 200, reply.text
        assert captured[-1]["kwargs"]["attachments"] == [{"raw_object_id": raw_id}]
        assert captured[-1]["kwargs"]["quoted_attachment_reference"] is True

        conflicting = _stored_reply_file(storage, tenant, uploader, "CONFLICTING-FILE-ID")
        conflict = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "покажи его метаданные",
                "source_ref": "telegram-update:reply-alias-conflict",
                "telegram_message_id": 503,
                "telegram_user": {"id": 1001, "first_name": "Alice"},
                "reply_document_message_id": 501,
                "reply_document_file_unique_id": "STABLE-UNIQUE-ID",
                "reply_document_source_ref": "telegram-file:CONFLICTING-FILE-ID",
            },
            user="1001",
        )
        assert conflict.status_code == 200, conflict.text
        assert conflicting.id != raw_id
        assert captured[-1]["kwargs"]["attachments"] == []
        assert captured[-1]["kwargs"]["quoted_attachment_reference"] is True


def test_owner_structural_reply_reads_active_shared_uploaders_registered_file(
    settings,
    monkeypatch,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    with TestClient(app) as client:
        canonical_chat = app.state.agent.chat
        owner_me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        sender_me = _bridge_call(client, scoped, "GET", "/api/me", user="2002")
        assert owner_me.status_code == 200, owner_me.text
        assert sender_me.status_code == 200, sender_me.text
        owner = str(owner_me.json()["actor"]["user_id"])
        sender = str(sender_me.json()["actor"]["user_id"])
        assert owner != sender
        app.state.storage.update_user(owner, preset_key="owner")
        app.state.storage.update_user(sender, preset_key="user")

        async def upload_ack(user_id, _message, **kwargs):
            del user_id
            conversation_id = str(kwargs.get("conversation_id") or "")
            if not conversation_id:
                conversation_id = str(app.state.storage.create_conversation(sender)["id"])
            return {
                "conversation_id": conversation_id,
                "message": "accepted",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", upload_ack)
        upload = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "",
                "source_ref": "telegram-update:shared-reply-upload",
                "telegram_message_id": 8801,
                "telegram_user": {"id": 2002, "first_name": "Sender"},
                "document": {
                    "filename": "shared-reply.odt",
                    "mime_type": "application/vnd.oasis.opendocument.text",
                    "media_kind": "document",
                    "source_ref": "telegram-file:SHARED-ORIGINAL-ID",
                    "file_unique_id": "SHARED-STABLE-ID",
                    "content_base64": base64.b64encode(
                        _synthetic_metadata_odt(
                            title="Shared sender title",
                            body=("Контрольный маркер: SHARED-REPLY-BODY-MARKER",),
                        )
                    ).decode("ascii"),
                },
            },
            user="2002",
        )
        assert upload.status_code == 200, upload.text
        raw_id = app.state.storage.resolve_owned_file_source_ref(
            LEGACY_OWNER_USER_ID,
            sender,
            "telegram-file:SHARED-ORIGINAL-ID",
        )
        assert raw_id

        disk_read_people: list[str] = []
        canonical_disk_read = runtime_module.read_authorized_file

        def observed_disk_read(*args, **kwargs):
            disk_read_people.append(str(kwargs.get("person_id") or ""))
            return canonical_disk_read(*args, **kwargs)

        class BodyModel:
            enabled = True
            model = "synthetic-shared-reply-body"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.payloads: list[str] = []

            async def chat(self, messages, **_kwargs):
                payload = json.dumps(messages, ensure_ascii=False)
                self.payloads.append(payload)
                assert "SHARED-REPLY-BODY-MARKER" in payload
                return {
                    "content": "Ответ из SHARED-REPLY-BODY-MARKER.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        body_model = BodyModel()
        monkeypatch.setattr(runtime_module, "read_authorized_file", observed_disk_read)
        monkeypatch.setattr(app.state.agent, "chat", canonical_chat)
        app.state.agent.llm = body_model
        reply = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Скажи, какой контрольный маркер указан в этом файле?",
                "source_ref": "telegram-update:shared-reply-read",
                "telegram_message_id": 8802,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
                "reply_document_message_id": 8801,
                "reply_document_file_unique_id": "SHARED-STABLE-ID",
                "reply_document_source_ref": "telegram-file:SHARED-CHURNED-ID",
            },
            user="1001",
        )
        assert reply.status_code == 200, reply.text
        reply_payload = reply.json()
        assert "SHARED-REPLY-BODY-MARKER" in str(reply_payload.get("message") or "")
        assert reply_payload["attachment_context_readable_count"] == 1
        assert reply_payload["attachment_coverage_complete"] is True
        assert reply_payload["attachment_verification_complete"] is True
        assert disk_read_people == [sender]

        follow_up = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "А подробнее по нему?",
                "source_ref": "telegram-update:shared-reply-follow-up",
                "telegram_message_id": 8803,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert follow_up.status_code == 200, follow_up.text
        follow_up_payload = follow_up.json()
        assert follow_up_payload["attachment_context_readable_count"] == 1
        assert follow_up_payload["attachment_coverage_complete"] is True
        assert follow_up_payload["attachment_verification_complete"] is True
        assert follow_up_payload["restored_attachment_count"] == 1
        assert disk_read_people == [sender, sender]

        history = app.state.storage.get_conversation_messages(
            str(reply_payload["conversation_id"]),
            user_id=owner,
        )
        denied, denied_expected = app.state.agent._restore_conversation_attachments(  # noqa: SLF001
            "А подробнее по нему?",
            history[:2],
            tenant_id=LEGACY_OWNER_USER_ID,
            person_id=owner,
            allow_file_read=False,
        )
        assert denied == []
        assert denied_expected == 1
        assert disk_read_people == [sender, sender]

        forged = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "покажи метаданные этого файла",
                "source_ref": "telegram-update:shared-reply-forged",
                "telegram_message_id": 8804,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
                "attachments": [{"raw_object_id": raw_id}],
            },
            user="1001",
        )
        assert forged.status_code == 200, forged.text
        assert forged.json()["attachment_context_readable_count"] == 0
        assert disk_read_people == [sender, sender]


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


def test_missing_reply_alias_recovers_exact_bytes_once_and_replays_without_redownload(
    settings,
    monkeypatch,
) -> None:
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    recovered_bytes = b"exact historical telegram bytes\n"
    archive_password = "exact-reply-archive-password"
    archive_buffer = io.BytesIO()
    with pyzipper.AESZipFile(
        archive_buffer,
        mode="w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(archive_password.encode("utf-8"))
        archive.writestr("note.txt", "synthetic exact reply archive body")
    archive_bytes = archive_buffer.getvalue()
    model_calls: list[dict[str, Any]] = []
    ingest_calls: list[bytes] = []

    async def chat_spy(user_id, message, **kwargs):  # noqa: ANN001
        model_calls.append({"user_id": user_id, "message": message, **kwargs})
        conversation_id = str(kwargs.get("conversation_id") or "")
        if not conversation_id:
            person_id = str(kwargs.get("actor").own_id)
            conversation_id = str(
                app.state.storage.create_conversation(person_id, title="recovered reply")["id"]
            )
        return {
            "conversation_id": conversation_id,
            "message": "ok",
            "context": {"interaction_mode": "dialogue"},
        }

    source_ref = "telegram-update:reply-exact-byte-recovery"
    file_ref = "telegram-file:HISTORICAL-CHURNED-FILE"
    unique_id = "HISTORICAL-STABLE-UNIQUE"
    message_id = 7301
    base_payload = {
        "message": "покажи метаданные этого файла",
        "source_ref": source_ref,
        "telegram_message_id": 7302,
        "telegram_user": {"id": 1001, "first_name": "Alice"},
        "reply_document_source_ref": file_ref,
        "reply_document_message_id": message_id,
        "reply_document_file_unique_id": unique_id,
    }

    with TestClient(app) as client:
        canonical_ingest = app.state.ingestion.ingest_file

        async def ingest_spy(*args, **kwargs):  # noqa: ANN002, ANN003
            ingest_calls.append(bytes(args[2]))
            assert kwargs.get("exact_byte_identity_only") is True
            return await canonical_ingest(*args, **kwargs)

        monkeypatch.setattr(app.state.agent, "chat", chat_spy)
        monkeypatch.setattr(app.state.ingestion, "ingest_file", ingest_spy)
        me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        assert me.status_code == 200, me.text
        uploader = str(me.json()["actor"]["user_id"])
        app.state.storage.update_user(uploader, preset_key="user")
        decoy = _stored_reply_file(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            uploader,
            "same-visible-name",
            content="different decoy bytes",
        )
        before_raw_count = int(
            app.state.storage.execute(
                "SELECT COUNT(*) AS n FROM raw_objects WHERE user_id=?",
                (LEGACY_OWNER_USER_ID,),
            ).fetchone()["n"]
        )
        before_message_count = int(
            app.state.storage.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        )
        before_conversation_count = int(
            app.state.storage.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        )
        before_user_count = int(app.state.storage.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

        phase_one = _bridge_call(client, scoped, "POST", "/api/chat", base_payload, user="1001")
        assert phase_one.status_code == 200, phase_one.text
        assert phase_one.json() == {"reply_media_recovery_required": True}
        assert model_calls == []
        assert ingest_calls == []
        assert (
            int(
                app.state.storage.execute(
                    "SELECT COUNT(*) AS n FROM raw_objects WHERE user_id=?",
                    (LEGACY_OWNER_USER_ID,),
                ).fetchone()["n"]
            )
            == before_raw_count
        )
        assert int(app.state.storage.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]) == (
            before_message_count
        )
        assert (
            int(app.state.storage.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"])
            == before_conversation_count
        )
        assert int(app.state.storage.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]) == (
            before_user_count
        )
        assert (
            app.state.storage.execute(
                "SELECT 1 FROM request_idempotency WHERE user_id=? AND request_key=?",
                (uploader, source_ref),
            ).fetchone()
            is None
        )

        recovered_payload = {
            **base_payload,
            "reply_document_recovery": {
                "filename": "same-visible-name.txt",
                "mime_type": "text/plain",
                "media_kind": "document",
                "source_ref": file_ref,
                "file_unique_id": unique_id,
                "content_base64": base64.b64encode(recovered_bytes).decode("ascii"),
            },
        }
        phase_two = _bridge_call(client, scoped, "POST", "/api/chat", recovered_payload, user="1001")
        assert phase_two.status_code == 200, phase_two.text
        assert phase_two.json()["message"] == "ok"
        assert len(model_calls) == 1
        assert ingest_calls == [recovered_bytes]
        attachment = model_calls[0]["attachments"]
        assert len(attachment) == 1
        recovered_raw_id = str(attachment[0].get("raw_object_id") or "")
        assert recovered_raw_id and recovered_raw_id != decoy.id
        raw = app.state.storage.get_raw_object(recovered_raw_id, LEGACY_OWNER_USER_ID)
        assert raw is not None
        metadata = json.loads(str(raw.get("metadata_json") or "{}"))
        stored = scoped.files_dir / str(metadata["stored_path"])
        assert stored.read_bytes() == recovered_bytes
        assert metadata["size_bytes"] == len(recovered_bytes)
        assert metadata["sha256"] == hashlib.sha256(recovered_bytes).hexdigest()
        message_ref = f"telegram-message:5001:{message_id}"
        refs = (message_ref, f"telegram-unique:{unique_id}", file_ref)
        from friday.storage._intake import resolve_tenant_telegram_reply_aliases

        assert resolve_tenant_telegram_reply_aliases(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            refs,
        ) == (recovered_raw_id, uploader)

        replay = _bridge_call(client, scoped, "POST", "/api/chat", base_payload, user="1001")
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert len(model_calls) == 1
        assert ingest_calls == [recovered_bytes]

        from friday.storage._intake import (
            TELEGRAM_REPLY_BLOCKED,
            bind_owned_telegram_reply_recovery_aliases,
            resolve_tenant_telegram_reply_alias_state,
        )

        for label, flags in (
            ("blocked-private", {}),
            ("blocked-deleted", {"deleted": True}),
            ("blocked-ignored", {"ignored": True}),
        ):
            blocked_raw = _stored_reply_file(
                app.state.storage,
                LEGACY_OWNER_USER_ID,
                uploader,
                label,
                **flags,
            )
            if label == "blocked-private":
                private_knowledge = KnowledgeObject(
                    id=new_id("ko"),
                    user_id=LEGACY_OWNER_USER_ID,
                    raw_object_id=blocked_raw.id,
                    content=blocked_raw.raw_content,
                    content_type="text",
                    title="private reply dependency",
                )
                app.state.storage.store_knowledge_object(private_knowledge)
                private_entity = Entity(
                    id=new_id("ent"),
                    user_id=LEGACY_OWNER_USER_ID,
                    name=f"Private reply entity {blocked_raw.id}",
                    entity_type=EntityType.EVENT,
                )
                app.state.storage.create_entity(private_entity)
                app.state.storage.link_knowledge_entity(
                    LEGACY_OWNER_USER_ID,
                    private_knowledge.id,
                    private_entity.id,
                    status="accepted",
                )
                with app.state.storage.transaction() as connection:
                    connection.execute(
                        """INSERT INTO private_entity_owners(
                               entity_id, person_id, privacy_kind, created_at)
                           VALUES(?, ?, 'reminder', '2026-08-12T00:00:00+00:00')""",
                        (private_entity.id, "another-person"),
                    )
            blocked_ref = f"telegram-unique:{label.upper()}"
            app.state.storage.execute(
                """INSERT INTO file_source_aliases(
                       user_id, uploaded_by, source_ref, raw_object_id, created_at
                   ) VALUES(?, ?, ?, ?, '2026-08-12T00:00:00+00:00')""",
                (LEGACY_OWNER_USER_ID, uploader, blocked_ref, blocked_raw.id),
            )
            app.state.storage.commit()
            assert (
                resolve_tenant_telegram_reply_alias_state(
                    app.state.storage,
                    LEGACY_OWNER_USER_ID,
                    (blocked_ref,),
                ).status
                == TELEGRAM_REPLY_BLOCKED
            )
            assert (
                bind_owned_telegram_reply_recovery_aliases(
                    app.state.storage,
                    LEGACY_OWNER_USER_ID,
                    uploader,
                    blocked_raw.id,
                    (
                        f"telegram-message:5001:{7400 + len(label)}",
                        f"telegram-file:BIND-{label.upper()}",
                    ),
                )
                is False
            )

        conflicting = _stored_reply_file(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            uploader,
            "blocked-conflict-a",
        )
        conflict_ref = "telegram-unique:BLOCKED-CONFLICT"
        app.state.storage.execute(
            """INSERT INTO file_source_aliases(
                   user_id, uploaded_by, source_ref, raw_object_id, created_at
               ) VALUES(?, ?, ?, ?, '2026-08-12T00:00:00+00:00')""",
            (LEGACY_OWNER_USER_ID, uploader, conflict_ref, conflicting.id),
        )
        app.state.storage.commit()
        assert (
            resolve_tenant_telegram_reply_alias_state(
                app.state.storage,
                LEGACY_OWNER_USER_ID,
                (message_ref, conflict_ref),
            ).status
            == TELEGRAM_REPLY_BLOCKED
        )
        atomic_candidate = _stored_reply_file(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            uploader,
            "atomic-candidate",
        )
        atomic_new_refs = (
            "telegram-message:5001:7991",
            conflict_ref,
            "telegram-file:ATOMIC-NEW-FILE",
        )
        alias_count_before = int(
            app.state.storage.execute("SELECT COUNT(*) AS n FROM file_source_aliases").fetchone()["n"]
        )
        from friday.storage import SourceReferenceConflictError

        with pytest.raises(SourceReferenceConflictError):
            bind_owned_telegram_reply_recovery_aliases(
                app.state.storage,
                LEGACY_OWNER_USER_ID,
                uploader,
                atomic_candidate.id,
                atomic_new_refs,
            )
        assert (
            int(app.state.storage.execute("SELECT COUNT(*) AS n FROM file_source_aliases").fetchone()["n"])
            == alias_count_before
        )
        assert (
            app.state.storage.execute(
                "SELECT 1 FROM file_source_aliases WHERE user_id=? AND source_ref IN (?, ?)",
                (LEGACY_OWNER_USER_ID, atomic_new_refs[0], atomic_new_refs[2]),
            ).fetchone()
            is None
        )

        archive_source_ref = "telegram-update:reply-exact-byte-archive"
        archive_file_ref = "telegram-file:HISTORICAL-ENCRYPTED-ARCHIVE"
        archive_unique_id = "HISTORICAL-ENCRYPTED-UNIQUE"
        archive_message_id = 8301
        archive_base_payload = {
            "message": "покажи содержимое этого архива",
            "source_ref": archive_source_ref,
            "telegram_message_id": 8302,
            "telegram_user": {"id": 1001, "first_name": "Alice"},
            "reply_document_source_ref": archive_file_ref,
            "reply_document_message_id": archive_message_id,
            "reply_document_file_unique_id": archive_unique_id,
        }
        archive_recovery_payload = {
            **archive_base_payload,
            "reply_document_recovery": {
                "filename": "historical-protected.zip",
                "mime_type": "application/zip",
                "media_kind": "document",
                "source_ref": archive_file_ref,
                "file_unique_id": archive_unique_id,
                "content_base64": base64.b64encode(archive_bytes).decode("ascii"),
            },
        }
        archive_raw_before = int(
            app.state.storage.execute(
                "SELECT COUNT(*) AS n FROM raw_objects WHERE user_id=?",
                (LEGACY_OWNER_USER_ID,),
            ).fetchone()["n"]
        )
        archive_ingest_before = len(ingest_calls)
        archive_model_before = len(model_calls)

        archive_phase_one = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            archive_base_payload,
            user="1001",
        )
        assert archive_phase_one.status_code == 200, archive_phase_one.text
        assert archive_phase_one.json() == {"reply_media_recovery_required": True}
        for password, challenge_key in (
            (None, "archive_password_required"),
            ("wrong-exact-reply-password", "archive_password_invalid"),
        ):
            attempt = dict(archive_recovery_payload)
            if password is not None:
                attempt["archive_password"] = password
            challenge = _bridge_call(client, scoped, "POST", "/api/chat", attempt, user="1001")
            assert challenge.status_code == 200, challenge.text
            assert challenge.json()[challenge_key] is True
            assert len(model_calls) == archive_model_before
            assert (
                int(
                    app.state.storage.execute(
                        "SELECT COUNT(*) AS n FROM raw_objects WHERE user_id=?",
                        (LEGACY_OWNER_USER_ID,),
                    ).fetchone()["n"]
                )
                == archive_raw_before
            )
            assert (
                app.state.storage.execute(
                    "SELECT 1 FROM file_source_aliases WHERE user_id=? AND source_ref=?",
                    (LEGACY_OWNER_USER_ID, archive_file_ref),
                ).fetchone()
                is None
            )
            assert (
                app.state.storage.execute(
                    "SELECT 1 FROM request_idempotency WHERE user_id=? AND request_key=?",
                    (uploader, archive_source_ref),
                ).fetchone()
                is None
            )

        archive_correct = {
            **archive_recovery_payload,
            "archive_password": archive_password,
        }
        archive_success = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            archive_correct,
            user="1001",
        )
        assert archive_success.status_code == 200, archive_success.text
        assert archive_success.json()["message"] == "ok"
        assert len(model_calls) == archive_model_before + 1
        assert ingest_calls[archive_ingest_before:] == [archive_bytes, archive_bytes, archive_bytes]
        archive_refs = (
            f"telegram-message:5001:{archive_message_id}",
            f"telegram-unique:{archive_unique_id}",
            archive_file_ref,
        )
        resolved_archive = resolve_tenant_telegram_reply_aliases(
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            archive_refs,
        )
        assert resolved_archive is not None
        archive_raw_id, archive_uploader = resolved_archive
        assert archive_uploader == uploader
        archive_raw = app.state.storage.get_raw_object(archive_raw_id, LEGACY_OWNER_USER_ID)
        assert archive_raw is not None
        archive_metadata = json.loads(str(archive_raw.get("metadata_json") or "{}"))
        archive_stored = scoped.files_dir / str(archive_metadata["stored_path"])
        assert archive_stored.read_bytes() == archive_bytes
        assert archive_metadata["size_bytes"] == len(archive_bytes)
        assert archive_metadata["sha256"] == hashlib.sha256(archive_bytes).hexdigest()
        archive_replay = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            archive_base_payload,
            user="1001",
        )
        assert archive_replay.status_code == 200, archive_replay.text
        assert archive_replay.json()["idempotent_replay"] is True
        assert ingest_calls[archive_ingest_before:] == [archive_bytes, archive_bytes, archive_bytes]

    class _RecoveryDownload:
        headers = {"Content-Length": str(len(recovered_bytes))}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield recovered_bytes[:7]
            yield recovered_bytes[7:]

    class _RecoveryTelegram(_Telegram):
        async def post(self, url, json=None, **_kwargs):
            self.calls.append((url, json))
            if url.endswith("/getFile"):
                return _Response({"ok": True, "result": {"file_path": "documents/recovered.bin"}})
            return _Response()

        def stream(self, method, url):  # noqa: ANN001
            assert method == "GET"
            assert url.endswith("/documents/recovered.bin")
            return _RecoveryDownload()

    class _RecoveryBackend(_Backend):
        async def request(self, method, url, *, content=None, headers=None):
            path = urlsplit(url).path
            body = json.loads(content.decode("utf-8")) if content else None
            self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
            if len(self.calls) == 1:
                return _Response({"reply_media_recovery_required": True})
            assert body["reply_document_source_ref"] == file_ref
            descriptor = body["reply_document_recovery"]
            assert descriptor["source_ref"] == file_ref
            assert descriptor["file_unique_id"] == unique_id
            assert base64.b64decode(descriptor["content_base64"], validate=True) == recovered_bytes
            return _Response({"message": "ok", "message_format": "plain"})

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(scoped.data_dir / "reply-recovery-bridge.sqlite3"),
        )
    )
    telegram = _RecoveryTelegram()
    backend = _RecoveryBackend()
    update = {
        "update_id": 7303,
        "message": {
            "message_id": 7302,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "text": "покажи метаданные этого файла",
            "reply_to_message": {
                "message_id": message_id,
                "document": {
                    "file_id": file_ref.removeprefix("telegram-file:"),
                    "file_unique_id": unique_id,
                    "file_name": "same-visible-name.txt",
                    "mime_type": "text/plain",
                    "file_size": len(recovered_bytes),
                },
            },
        },
    }
    try:
        asyncio.run(bridge._process_update(telegram, backend, update, cached_response=None))
    finally:
        bridge._inbox.close()
    assert len([call for call in backend.calls if call["path"] == "/api/chat"]) == 2
    assert len([url for url, _payload in telegram.calls if url.endswith("/getFile")]) == 1

    class _ArchiveRecoveryDownload:
        headers = {"Content-Length": str(len(archive_bytes))}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield archive_bytes

    class _ArchiveRecoveryTelegram(_Telegram):
        async def post(self, url, json=None, **_kwargs):
            self.calls.append((url, json))
            if url.endswith("/getFile"):
                return _Response({"ok": True, "result": {"file_path": "documents/protected.zip"}})
            return _Response()

        def stream(self, method, url):  # noqa: ANN001
            assert method == "GET"
            assert url.endswith("/documents/protected.zip")
            return _ArchiveRecoveryDownload()

    class _ArchiveRecoveryBackend(_Backend):
        async def request(self, method, url, *, content=None, headers=None):
            path = urlsplit(url).path
            body = json.loads(content.decode("utf-8")) if content else None
            self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
            assert "document" not in body
            if "reply_document_recovery" not in body:
                return _Response({"reply_media_recovery_required": True})
            descriptor = body["reply_document_recovery"]
            assert descriptor["source_ref"] == "telegram-file:HISTORICAL-PROTECTED-ARCHIVE"
            assert descriptor["file_unique_id"] == "HISTORICAL-PROTECTED-UNIQUE"
            assert base64.b64decode(descriptor["content_base64"], validate=True) == archive_bytes
            if body.get("archive_password") is None:
                return _Response({"archive_password_required": True})
            if body["archive_password"] != archive_password:
                return _Response({"archive_password_invalid": True})
            return _Response({"message": "ok", "message_format": "plain"})

    archive_bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(scoped.data_dir / "reply-recovery-archive-bridge.sqlite3"),
        )
    )
    archive_telegram = _ArchiveRecoveryTelegram()
    archive_backend = _ArchiveRecoveryBackend()
    archive_reply_message_id = 8401
    archive_update = {
        "update_id": 8403,
        "message": {
            "message_id": 8402,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "text": "покажи содержимое этого архива",
            "reply_to_message": {
                "message_id": archive_reply_message_id,
                "document": {
                    "file_id": "HISTORICAL-PROTECTED-ARCHIVE",
                    "file_unique_id": "HISTORICAL-PROTECTED-UNIQUE",
                    "file_name": "historical-protected.zip",
                    "mime_type": "application/zip",
                    "file_size": len(archive_bytes),
                },
            },
        },
    }
    try:
        asyncio.run(
            archive_bridge._process_update(  # noqa: SLF001
                archive_telegram,
                archive_backend,
                archive_update,
                cached_response=None,
            )
        )
        pending = archive_bridge._inbox.archive_password_challenge(5001, 1001)  # noqa: SLF001
        assert pending is not None and pending["reply_recovery"] is True
        assert pending["reply_document_message_id"] == archive_reply_message_id
        assert pending["reply_document_source_ref"] == "telegram-file:HISTORICAL-PROTECTED-ARCHIVE"
        assert pending["reply_document_file_unique_id"] == "HISTORICAL-PROTECTED-UNIQUE"

        for update_id, password in (
            (8404, "wrong-exact-reply-password"),
            (8405, archive_password),
        ):
            followup = {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "chat": {"id": 5001},
                    "from": {"id": 1001, "first_name": "Alice"},
                    "text": password,
                },
            }
            safe_followup = archive_bridge._sanitize_update_before_store(followup)  # noqa: SLF001
            asyncio.run(
                archive_bridge._process_update(  # noqa: SLF001
                    archive_telegram,
                    archive_backend,
                    safe_followup,
                    cached_response=None,
                )
            )
            pending = archive_bridge._inbox.archive_password_challenge(5001, 1001)  # noqa: SLF001
            if password == archive_password:
                assert pending is None
            else:
                assert pending is not None and pending["reply_recovery"] is True

        chat_bodies = [call["body"] for call in archive_backend.calls if call["path"] == "/api/chat"]
        assert len(chat_bodies) == 6
        for phase_one_body in chat_bodies[::2]:
            assert "document" not in phase_one_body
            assert "reply_document_recovery" not in phase_one_body
            assert phase_one_body["reply_document_source_ref"] == "telegram-file:HISTORICAL-PROTECTED-ARCHIVE"
            assert phase_one_body["reply_document_message_id"] == archive_reply_message_id
            assert phase_one_body["reply_document_file_unique_id"] == "HISTORICAL-PROTECTED-UNIQUE"
        assert [body.get("archive_password") for body in chat_bodies[1::2]] == [
            None,
            "wrong-exact-reply-password",
            archive_password,
        ]
        assert all("document" not in body for body in chat_bodies[1::2])
        assert all("reply_document_recovery" in body for body in chat_bodies[1::2])
        assert len([url for url, _payload in archive_telegram.calls if url.endswith("/getFile")]) == 3
        durable_dump = "\n".join(archive_bridge._inbox._conn.iterdump())  # noqa: SLF001
        assert archive_password not in durable_dump
        assert "wrong-exact-reply-password" not in durable_dump
    finally:
        archive_bridge._archive_passwords.clear()  # noqa: SLF001
        archive_bridge._inbox.close()  # noqa: SLF001
