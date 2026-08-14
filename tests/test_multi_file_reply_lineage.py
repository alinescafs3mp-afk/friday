from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import replace
from typing import Any

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


def _signed_headers(
    secret: str,
    method: str,
    path: str,
    body: bytes,
    user: str,
) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": user,
        "X-Friday-Chat": "5001",
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            secret,
            timestamp=timestamp,
            method=method,
            path=path,
            external_user_id=user,
            chat_id="5001",
            nonce=nonce,
            body=body,
        ),
    }


def _bridge_call(
    client: TestClient,
    settings: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    user: str,
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
        headers=_signed_headers(settings.telegram_bridge_secret, method, path, body, user),
    )


def _upload_text_file(
    client: TestClient,
    app: Any,
    settings: Any,
    *,
    external_user: str,
    uploader: str,
    label: str,
    body: str,
) -> str:
    source_ref = f"telegram-file:{label}"
    response = _bridge_call(
        client,
        settings,
        "POST",
        "/api/chat",
        {
            "message": "",
            "source_ref": f"telegram-update:multi-lineage-upload-{label}",
            "telegram_message_id": 10_000 + int(hashlib.sha256(label.encode("utf-8")).hexdigest()[:8], 16),
            "telegram_user": {"id": int(external_user), "first_name": label},
            "document": {
                "filename": f"{label}.txt",
                "mime_type": "text/plain",
                "media_kind": "document",
                "source_ref": source_ref,
                "file_unique_id": f"UNIQUE-{label}",
                "content_base64": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            },
        },
        user=external_user,
    )
    assert response.status_code == 200, response.text
    raw_id = app.state.storage.resolve_owned_file_source_ref(
        LEGACY_OWNER_USER_ID,
        uploader,
        source_ref,
    )
    assert raw_id
    return str(raw_id)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata_json")
    if isinstance(value, str):
        parsed = json.loads(value)
        assert isinstance(parsed, dict)
        return parsed
    assert isinstance(value, dict)
    return value


def test_mixed_uploader_two_file_reply_regenerate_and_closed_failures(
    settings,
    monkeypatch,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    tenant = LEGACY_OWNER_USER_ID
    canary_b = "EXACT-JBL-B-CANARY-7Q9"
    canary_a = "EXACT-OWNER-A-CANARY-4M2"
    decoy_canary = "AMBIENT-DECOY-CANARY-DO-NOT-READ"
    ignored_canary = "IGNORED-FILE-CANARY"

    with TestClient(app) as client:
        canonical_chat = app.state.agent.chat
        owner_me = _bridge_call(client, scoped, "GET", "/api/me", user="1001")
        jbl_me = _bridge_call(client, scoped, "GET", "/api/me", user="2002")
        assert owner_me.status_code == 200, owner_me.text
        assert jbl_me.status_code == 200, jbl_me.text
        owner = str(owner_me.json()["actor"]["user_id"])
        jbl = str(jbl_me.json()["actor"]["user_id"])
        assert owner != jbl
        storage = app.state.storage
        storage.update_user(owner, preset_key="owner")
        storage.update_user(jbl, preset_key="user")

        async def upload_ack(_user_id, _message, **kwargs):
            actor = kwargs["actor"]
            conversation_id = str(kwargs.get("conversation_id") or "")
            if not conversation_id:
                conversation_id = str(storage.create_conversation(actor.own_id)["id"])
            return {
                "conversation_id": conversation_id,
                "message": "accepted",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.agent, "chat", upload_ack)
        raw_b = _upload_text_file(
            client,
            app,
            scoped,
            external_user="2002",
            uploader=jbl,
            label="JBL-B",
            body=f"Материал B. Контрольный маркер: {canary_b}.",
        )
        raw_a = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="OWNER-A",
            body=f"Материал A. Контрольный маркер: {canary_a}.",
        )
        raw_decoy = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="OWNER-C-DECOY",
            body=f"Посторонний материал. {decoy_canary}.",
        )
        raw_deleted = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="OWNER-DELETED",
            body="DELETED-FILE-CANARY",
        )
        raw_ignored = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="OWNER-IGNORED",
            body=ignored_canary,
        )
        raw_private = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="OWNER-PRIVATE",
            body="PRIVATE-FILE-CANARY",
        )

        conversation = storage.create_conversation(owner, title="mixed uploader lineage")
        source_answer = storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Предыдущий ответ использовал два материала.",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [raw_b, raw_a],
                "conversation_attachment_uploaders": {raw_b: jbl},
            },
        )
        storage.set_channel_conversation(owner, "telegram", "5001", conversation["id"])

        disk_reads: list[tuple[str, str]] = []
        canonical_disk_read = runtime_module.read_authorized_file

        def observed_disk_read(*args, **kwargs):
            disk_reads.append((str(args[2]), str(kwargs.get("person_id") or "")))
            return canonical_disk_read(*args, **kwargs)

        class OrderedTwoFileModel:
            enabled = True
            model = "synthetic-ordered-two-file"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.payloads: list[str] = []

            async def chat(self, messages, **_kwargs):
                payload = json.dumps(messages, ensure_ascii=False)
                self.payloads.append(payload)
                assert canary_b in payload
                assert canary_a in payload
                assert payload.index(canary_b) < payload.index(canary_a)
                assert decoy_canary not in payload
                return {
                    "content": f"Совместная сводка: {canary_b}; {canary_a}.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        ordered_model = OrderedTwoFileModel()
        monkeypatch.setattr(runtime_module, "read_authorized_file", observed_disk_read)
        monkeypatch.setattr(app.state.agent, "chat", canonical_chat)
        app.state.agent.llm = ordered_model

        reply = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": (
                    "Сделай совместное краткое обобщение материалов в процитированном ответе "
                    "и назови их контрольные маркеры."
                ),
                "source_ref": "telegram-update:mixed-two-file-reply",
                "telegram_message_id": 11_001,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
                "reply_source_message_id": str(source_answer["id"]),
                "attachments": [{"raw_object_id": raw_decoy}],
            },
            user="1001",
        )
        assert reply.status_code == 200, reply.text
        reply_payload = reply.json()
        assert canary_b in str(reply_payload.get("message") or "")
        assert canary_a in str(reply_payload.get("message") or "")
        assert decoy_canary not in str(reply_payload.get("message") or "")
        assert reply_payload["attachment_context_expected_count"] == 2
        assert reply_payload["attachment_context_readable_count"] == 2
        assert reply_payload["attachment_coverage_complete"] is True
        assert reply_payload["attachment_verification_complete"] is True

        regenerated = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/me/regenerate",
            {"conversation_id": conversation["id"]},
            user="1001",
        )
        assert regenerated.status_code == 200, regenerated.text
        regenerated_payload = regenerated.json()
        assert regenerated_payload["attachment_context_expected_count"] == 2
        assert regenerated_payload["attachment_context_readable_count"] == 2
        assert regenerated_payload["attachment_coverage_complete"] is True
        assert regenerated_payload["attachment_verification_complete"] is True

        follow_up = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Что ещё указано в них? Снова назови контрольные маркеры.",
                "source_ref": "telegram-update:mixed-two-file-follow-up",
                "telegram_message_id": 11_002,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert follow_up.status_code == 200, follow_up.text
        follow_up_payload = follow_up.json()
        assert follow_up_payload["attachment_context_expected_count"] == 2
        assert follow_up_payload["attachment_context_readable_count"] == 2
        assert follow_up_payload["attachment_coverage_complete"] is True
        assert follow_up_payload["attachment_verification_complete"] is True
        assert follow_up_payload["restored_attachment_count"] == 2

        assert len(ordered_model.payloads) == 3
        # The first native-reply publication performs an additional exact
        # re-read before committing its derived answer.
        assert disk_reads == [(raw_b, jbl), (raw_a, owner)] * 4

        rows = storage.get_conversation_messages(
            conversation["id"],
            user_id=owner,
            limit=100,
        )
        generated = rows[-6:]
        assert [row["role"] for row in generated] == ["user", "assistant"] * 3
        expected_uploader_maps = [
            {raw_b: jbl},
            {raw_b: jbl, raw_a: owner},
            {raw_b: jbl},
            {raw_b: jbl},
            {raw_b: jbl},
            {raw_b: jbl},
        ]
        for row, expected_uploaders in zip(generated, expected_uploader_maps, strict=True):
            metadata = _metadata(row)
            assert metadata["conversation_attachment_raw_ids"] == [raw_b, raw_a]
            assert metadata["conversation_attachment_uploaders"] == expected_uploaders
            assert "conversation_uploaded_raw_ids" not in metadata
            if row["role"] == "assistant":
                assert metadata["attachment_context_used"] is True

        history = _bridge_call(
            client,
            scoped,
            "GET",
            f"/api/conversations/{conversation['id']}/messages",
            user="1001",
        )
        assert history.status_code == 200, history.text
        for public_payload in (
            reply_payload,
            regenerated_payload,
            follow_up_payload,
            history.json(),
        ):
            encoded = json.dumps(public_payload, ensure_ascii=False)
            for raw_id in (raw_b, raw_a, raw_decoy):
                assert raw_id not in encoded
            assert owner not in encoded and jbl not in encoded
            assert "conversation_attachment_raw_ids" not in encoded
            assert "conversation_attachment_uploaders" not in encoded

        storage.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-12T00:00:00+00:00' WHERE id=?",
            (raw_deleted,),
        )
        storage.execute(
            "UPDATE inbox SET status='ignored' WHERE user_id=? AND raw_object_id=?",
            (tenant, raw_ignored),
        )
        storage.commit()

        private_knowledge = storage.get_knowledge_by_raw(raw_private, tenant)
        if private_knowledge is None:
            stored_private = storage.get_raw_object(raw_private, tenant)
            assert stored_private is not None
            created_private = KnowledgeObject(
                id=new_id("ko"),
                user_id=tenant,
                raw_object_id=raw_private,
                content=str(stored_private.get("raw_content") or ""),
                content_type="text",
                title="private multi-lineage dependency",
            )
            storage.store_knowledge_object(created_private)
            private_knowledge_id = created_private.id
        else:
            private_knowledge_id = str(private_knowledge["id"])
        private_entity = Entity(
            id=new_id("ent"),
            user_id=tenant,
            name=f"Private multi-lineage entity {raw_private}",
            entity_type=EntityType.EVENT,
        )
        storage.create_entity(private_entity)
        storage.link_knowledge_entity(
            tenant,
            private_knowledge_id,
            private_entity.id,
            status="accepted",
        )
        with storage.transaction() as connection:
            connection.execute(
                """INSERT INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, ?, 'reminder', '2026-08-12T00:00:00+00:00')""",
                (private_entity.id, jbl),
            )

        foreign_tenant = "foreign-multi-lineage-tenant"
        storage.ensure_user(foreign_tenant, preset_key="user")
        foreign_body = "FOREIGN-TENANT-FILE-CANARY"
        foreign_raw = RawObject(
            id=new_id("raw"),
            user_id=foreign_tenant,
            source="upload",
            source_ref="foreign:multi-lineage",
            raw_content=foreign_body,
            content_type="file",
            content_hash=hashlib.sha256(foreign_body.encode()).hexdigest(),
            metadata_json={"filename": "foreign.txt", "uploaded_by": owner},
        )
        storage.store_raw_object(foreign_raw)
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=foreign_tenant,
                raw_object_id=foreign_raw.id,
                status=InboxStatus.PENDING,
            )
        )

        # A native reply to one exact assistant lineage is a typed historical
        # direct-read authority, including for an ignored Inbox member. Ambient
        # and implicit restoration still exclude that member.
        ignored_source = storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Exact ignored lineage",
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [raw_a, raw_ignored],
            },
        )

        class IgnoredLineageModel:
            enabled = True
            model = "exact-ignored-assistant-lineage"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, messages, **_kwargs):
                projected = json.dumps(messages, ensure_ascii=False)
                self.calls += 1
                assert canary_a in projected and ignored_canary in projected
                assert projected.index(canary_a) < projected.index(ignored_canary)
                assert canary_b not in projected and decoy_canary not in projected
                return {
                    "content": f"Exact ignored lineage: {canary_a}; {ignored_canary}.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        ignored_model = IgnoredLineageModel()
        app.state.agent.llm = ignored_model
        disk_reads.clear()
        ignored = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Обобщи оба файла из процитированного ответа.",
                "source_ref": "telegram-update:exact-ignored-multi-lineage",
                "telegram_message_id": 11_003,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
                "reply_source_message_id": str(ignored_source["id"]),
                "attachments": [{"raw_object_id": raw_decoy}],
            },
            user="1001",
        )
        assert ignored.status_code == 200, ignored.text
        ignored_payload = ignored.json()
        ignored_text = str(ignored_payload.get("message") or "")
        assert canary_a in ignored_text and ignored_canary in ignored_text
        assert canary_b not in ignored_text and decoy_canary not in ignored_text
        assert ignored_payload["attachment_context_expected_count"] == 2
        assert ignored_payload["attachment_context_readable_count"] == 2
        assert ignored_payload["attachment_coverage_complete"] is True
        assert ignored_payload["attachment_verification_complete"] is True
        assert ignored_model.calls == 1
        assert disk_reads == [(raw_a, owner), (raw_ignored, owner)] * 2
        ignored_history = _bridge_call(
            client,
            scoped,
            "GET",
            f"/api/conversations/{conversation['id']}/messages",
            user="1001",
        )
        assert ignored_history.status_code == 200, ignored_history.text
        for public_payload in (ignored_payload, ignored_history.json()):
            encoded = json.dumps(public_payload, ensure_ascii=False)
            for raw_id in (raw_a, raw_b, raw_ignored, raw_decoy):
                assert raw_id not in encoded
            assert owner not in encoded and jbl not in encoded
            assert "conversation_attachment_raw_ids" not in encoded
            assert "conversation_attachment_uploaders" not in encoded

        class NeverModel:
            enabled = True
            model = "never-on-closed-multi-lineage"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, _messages, **_kwargs):
                self.calls += 1
                raise AssertionError("closed multi-file lineage reached the model")

        never_model = NeverModel()
        app.state.agent.llm = never_model
        disk_reads.clear()
        malformed_extra_raw = new_id("raw")
        closed_cases = (
            (
                "files-read-denied",
                {
                    "attachment_context_used": True,
                    "conversation_attachment_raw_ids": [raw_b, raw_a],
                    "conversation_attachment_uploaders": {raw_b: jbl},
                },
                True,
            ),
            (
                "malformed-uploader-map",
                {
                    "attachment_context_used": True,
                    "conversation_attachment_raw_ids": [raw_b, raw_a],
                    "conversation_attachment_uploaders": {
                        raw_b: jbl,
                        malformed_extra_raw: owner,
                    },
                },
                False,
            ),
            (
                "deleted-member",
                {
                    "attachment_context_used": True,
                    "conversation_attachment_raw_ids": [raw_a, raw_deleted],
                },
                False,
            ),
            (
                "private-member",
                {
                    "attachment_context_used": True,
                    "conversation_attachment_raw_ids": [raw_a, raw_private],
                },
                False,
            ),
            (
                "foreign-tenant-member",
                {
                    "attachment_context_used": True,
                    "conversation_attachment_raw_ids": [raw_a, foreign_raw.id],
                },
                False,
            ),
        )
        for index, (label, lineage, deny_file_read) in enumerate(closed_cases, start=1):
            closed_source = storage.store_message(
                conversation["id"],
                owner,
                "assistant",
                f"Closed source {label}",
                metadata=lineage,
            )
            if deny_file_read:
                storage.set_permission_override(owner, "files.read", "deny")
            try:
                closed = _bridge_call(
                    client,
                    scoped,
                    "POST",
                    "/api/chat",
                    {
                        "message": "Обобщи оба файла из процитированного ответа.",
                        "source_ref": f"telegram-update:closed-multi-lineage-{label}",
                        "telegram_message_id": 12_000 + index,
                        "telegram_user": {"id": 1001, "first_name": "Owner"},
                        "reply_source_message_id": str(closed_source["id"]),
                        "attachments": [{"raw_object_id": raw_decoy}],
                    },
                    user="1001",
                )
            finally:
                if deny_file_read:
                    storage.set_permission_override(owner, "files.read", None)
            assert closed.status_code == 200, closed.text
            closed_payload = closed.json()
            assert closed_payload["attachment_context_readable_count"] == 0
            assert canary_a not in str(closed_payload.get("message") or "")
            assert canary_b not in str(closed_payload.get("message") or "")
            assert decoy_canary not in str(closed_payload.get("message") or "")
            assert never_model.calls == 0
            assert disk_reads == []
