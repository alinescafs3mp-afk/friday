"""Ordered multi-citation file flow: explicit [K#] labels + native reply fan-out.

Independent of deictic/filename/catalog/latest restoration. Mixed uploaders
keep exact internal lineage by row role; closed failures are all-or-none.
"""

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
    InboxItem,
    InboxStatus,
    KnowledgeObject,
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


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata_json")
    if isinstance(value, str):
        parsed = json.loads(value)
        assert isinstance(parsed, dict)
        return parsed
    assert isinstance(value, dict)
    return value


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
    source_ref = f"telegram-file:mcit-{label}"
    response = _bridge_call(
        client,
        settings,
        "POST",
        "/api/chat",
        {
            "message": "",
            "source_ref": f"telegram-update:mcit-upload-{label}",
            "telegram_message_id": 20_000
            + int(hashlib.sha256(label.encode("utf-8")).hexdigest()[:8], 16) % 50_000,
            "telegram_user": {"id": int(external_user), "first_name": label},
            "document": {
                "filename": f"{label}.txt",
                "mime_type": "text/plain",
                "media_kind": "document",
                "source_ref": source_ref,
                "file_unique_id": f"MCIT-UNIQUE-{label}",
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


def _ensure_file_knowledge(
    storage: Any,
    *,
    tenant: str,
    raw_id: str,
    title: str,
    body: str,
) -> str:
    existing = storage.get_knowledge_by_raw(raw_id, tenant)
    if existing is not None:
        return str(existing["id"])
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=tenant,
        raw_object_id=raw_id,
        content=body,
        content_type="file",
        title=title,
        summary=body[:200],
    )
    storage.store_knowledge_object(knowledge)
    inbox = storage.get_inbox_by_raw(raw_id, tenant) or storage.find_inbox_by_raw(raw_id, tenant)
    if inbox is None:
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=tenant,
                raw_object_id=raw_id,
                knowledge_object_id=knowledge.id,
                status=InboxStatus.CLASSIFIED,
            )
        )
    else:
        storage.execute(
            "UPDATE inbox SET knowledge_object_id=?, status='classified' WHERE id=?",
            (knowledge.id, inbox["id"]),
        )
        storage.commit()
    return knowledge.id


def _seed_mixed_citation_corpus(client, app, scoped):
    """Owner A + JBL B + ambient owner C; returns ids and canaries."""
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

    return owner, jbl, storage, upload_ack


def test_explicit_citation_order_mixed_uploaders_and_public_privacy(
    settings,
    monkeypatch,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    tenant = LEGACY_OWNER_USER_ID
    canary_a = "MCIT-OWNER-A-CANARY-9X1"
    canary_b = "MCIT-JBL-B-CANARY-3W7"
    canary_c = "MCIT-AMBIENT-C-CANARY-DO-NOT-READ"

    with TestClient(app) as client:
        canonical_chat = app.state.agent.chat
        owner, jbl, storage, upload_ack = _seed_mixed_citation_corpus(client, app, scoped)
        monkeypatch.setattr(app.state.agent, "chat", upload_ack)

        body_b = f"Материал B. Маркер: {canary_b}."
        body_a = f"Материал A. Маркер: {canary_a}."
        body_c = f"Посторонний. {canary_c}."
        raw_b = _upload_text_file(
            client, app, scoped, external_user="2002", uploader=jbl, label="JBL-B", body=body_b
        )
        raw_a = _upload_text_file(
            client, app, scoped, external_user="1001", uploader=owner, label="OWNER-A", body=body_a
        )
        raw_c = _upload_text_file(
            client, app, scoped, external_user="1001", uploader=owner, label="OWNER-C", body=body_c
        )
        ko_a = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_a, title="A", body=body_a)
        ko_b = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_b, title="B", body=body_b)
        ko_c = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_c, title="C", body=body_c)

        conversation = storage.create_conversation(owner, title="multi-citation explicit")
        storage.set_channel_conversation(owner, "telegram", "5001", conversation["id"])
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Источники: A [K1], B [K2], C [K3].",
            metadata={
                "knowledge_citations": {"K1": ko_a, "K2": ko_b, "K3": ko_c},
            },
        )

        disk_reads: list[tuple[str, str]] = []
        canonical_disk_read = runtime_module.read_authorized_file

        def observed_disk_read(*args, **kwargs):
            disk_reads.append((str(args[2]), str(kwargs.get("person_id") or "")))
            return canonical_disk_read(*args, **kwargs)

        class OrderedCitationModel:
            enabled = True
            model = "synthetic-ordered-citation"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.payloads: list[str] = []

            async def chat(self, messages, **_kwargs):
                payload = json.dumps(messages, ensure_ascii=False)
                self.payloads.append(payload)
                assert canary_b in payload
                assert canary_a in payload
                assert payload.index(canary_b) < payload.index(canary_a)
                assert canary_c not in payload
                return {
                    "content": f"Сравнение: {canary_b}; {canary_a}.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        ordered_model = OrderedCitationModel()
        monkeypatch.setattr(runtime_module, "read_authorized_file", observed_disk_read)
        monkeypatch.setattr(app.state.agent, "chat", canonical_chat)
        app.state.agent.llm = ordered_model

        reply = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K2], затем [K1]",
                "source_ref": "telegram-update:mcit-explicit-compare",
                "telegram_message_id": 21_001,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert reply.status_code == 200, reply.text
        payload = reply.json()
        assert canary_b in str(payload.get("message") or "")
        assert canary_a in str(payload.get("message") or "")
        assert canary_c not in str(payload.get("message") or "")
        assert payload["attachment_context_expected_count"] == 2
        assert payload["attachment_context_readable_count"] == 2
        assert payload["attachment_coverage_complete"] is True
        assert payload["attachment_verification_complete"] is True
        assert len(ordered_model.payloads) == 1
        assert disk_reads == [(raw_b, jbl), (raw_a, owner)] * 2

        rows = storage.get_conversation_messages(conversation["id"], user_id=owner, limit=50)
        generated = [row for row in rows if row["role"] in {"user", "assistant"}][-2:]
        assert [row["role"] for row in generated] == ["user", "assistant"]
        expected_uploader_maps = [
            {raw_b: jbl},
            {raw_b: jbl, raw_a: owner},
        ]
        for row, expected_uploaders in zip(generated, expected_uploader_maps, strict=True):
            meta = _metadata(row)
            assert meta.get("conversation_attachment_raw_ids") == [raw_b, raw_a]
            assert meta.get("conversation_attachment_uploaders") == expected_uploaders

        history = _bridge_call(
            client,
            scoped,
            "GET",
            f"/api/conversations/{conversation['id']}/messages",
            user="1001",
        )
        assert history.status_code == 200, history.text
        for public_payload in (payload, history.json()):
            encoded = json.dumps(public_payload, ensure_ascii=False)
            for raw_id in (raw_a, raw_b, raw_c):
                assert raw_id not in encoded
            assert owner not in encoded and jbl not in encoded
            assert "conversation_attachment_raw_ids" not in encoded
            assert "conversation_attachment_uploaders" not in encoded
            assert ko_a not in encoded and ko_b not in encoded


def test_native_reply_uses_exact_assistant_citation_row_not_newer_decoy(
    settings,
    monkeypatch,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    tenant = LEGACY_OWNER_USER_ID
    canary_a = "MCIT-REPLY-A-CANARY-4P2"
    canary_b = "MCIT-REPLY-B-CANARY-8L5"
    decoy_canary = "MCIT-REPLY-DECOY-CANARY"

    with TestClient(app) as client:
        canonical_chat = app.state.agent.chat
        owner, jbl, storage, upload_ack = _seed_mixed_citation_corpus(client, app, scoped)
        monkeypatch.setattr(app.state.agent, "chat", upload_ack)

        body_b = f"Reply B. {canary_b}."
        body_a = f"Reply A. {canary_a}."
        body_decoy = f"Decoy file. {decoy_canary}."
        raw_b = _upload_text_file(
            client, app, scoped, external_user="2002", uploader=jbl, label="R-JBL-B", body=body_b
        )
        raw_a = _upload_text_file(
            client, app, scoped, external_user="1001", uploader=owner, label="R-OWN-A", body=body_a
        )
        raw_decoy = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="R-DECOY",
            body=body_decoy,
        )
        ko_a = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_a, title="RA", body=body_a)
        ko_b = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_b, title="RB", body=body_b)
        ko_decoy = _ensure_file_knowledge(
            storage, tenant=tenant, raw_id=raw_decoy, title="RD", body=body_decoy
        )

        conversation = storage.create_conversation(owner, title="multi-citation reply")
        storage.set_channel_conversation(owner, "telegram", "5001", conversation["id"])
        target = storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Ответ с двумя источниками [K1] и [K2].",
            metadata={"knowledge_citations": {"K1": ko_a, "K2": ko_b}},
        )
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Более новый decoy ответ [K1].",
            metadata={"knowledge_citations": {"K1": ko_decoy}},
        )

        disk_reads: list[tuple[str, str]] = []
        canonical_disk_read = runtime_module.read_authorized_file

        def observed_disk_read(*args, **kwargs):
            disk_reads.append((str(args[2]), str(kwargs.get("person_id") or "")))
            return canonical_disk_read(*args, **kwargs)

        class ReplyCitationModel:
            enabled = True
            model = "synthetic-reply-citation"
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
                    "content": f"Из цитаты: {canary_b}; {canary_a}.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        model = ReplyCitationModel()
        monkeypatch.setattr(runtime_module, "read_authorized_file", observed_disk_read)
        monkeypatch.setattr(app.state.agent, "chat", canonical_chat)
        app.state.agent.llm = model

        # Without explicit labels: emitted citation order (K1=A, K2=B) → A then B.
        # Task requires B,A when labels request that order; without labels, map order.
        # Request with labels on the replied row to pin B then A.
        reply = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K2], затем [K1]",
                "source_ref": "telegram-update:mcit-native-reply",
                "telegram_message_id": 21_101,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
                "reply_source_message_id": str(target["id"]),
            },
            user="1001",
        )
        assert reply.status_code == 200, reply.text
        payload = reply.json()
        assert canary_b in str(payload.get("message") or "")
        assert canary_a in str(payload.get("message") or "")
        assert decoy_canary not in str(payload.get("message") or "")
        assert payload["attachment_context_expected_count"] == 2
        assert payload["attachment_context_readable_count"] == 2
        assert disk_reads == [(raw_b, jbl), (raw_a, owner)] * 2
        assert len(model.payloads) == 1

        # Reply without labels: emitted order only (K1 then K2 → A then B).
        disk_reads.clear()
        model.payloads.clear()

        class EmitOrderModel:
            enabled = True
            model = "synthetic-emit-order"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.payloads: list[str] = []

            async def chat(self, messages, **_kwargs):
                payload = json.dumps(messages, ensure_ascii=False)
                self.payloads.append(payload)
                assert canary_a in payload and canary_b in payload
                assert payload.index(canary_a) < payload.index(canary_b)
                assert decoy_canary not in payload
                return {
                    "content": f"Emit order: {canary_a}; {canary_b}.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        emit_model = EmitOrderModel()
        app.state.agent.llm = emit_model
        bare_reply = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Обобщи процитированный ответ.",
                "source_ref": "telegram-update:mcit-native-reply-bare",
                "telegram_message_id": 21_102,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
                "reply_source_message_id": str(target["id"]),
            },
            user="1001",
        )
        assert bare_reply.status_code == 200, bare_reply.text
        bare_payload = bare_reply.json()
        assert decoy_canary not in str(bare_payload.get("message") or "")
        assert bare_payload["attachment_context_readable_count"] == 2
        assert disk_reads == [(raw_a, owner), (raw_b, jbl)] * 2
        assert len(emit_model.payloads) == 1


def test_multi_citation_closed_failure_matrix(
    settings,
    monkeypatch,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday.server import create_app

    scoped = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(scoped)
    tenant = LEGACY_OWNER_USER_ID
    canary_a = "MCIT-NEG-A-CANARY-1"
    canary_b = "MCIT-NEG-B-CANARY-2"
    decoy_canary = "MCIT-NEG-DECOY"

    with TestClient(app) as client:
        canonical_chat = app.state.agent.chat
        owner, jbl, storage, upload_ack = _seed_mixed_citation_corpus(client, app, scoped)
        monkeypatch.setattr(app.state.agent, "chat", upload_ack)

        body_b = f"Neg B. {canary_b}."
        body_a = f"Neg A. {canary_a}."
        body_decoy = f"Neg decoy. {decoy_canary}."
        raw_b = _upload_text_file(
            client, app, scoped, external_user="2002", uploader=jbl, label="N-JBL-B", body=body_b
        )
        raw_a = _upload_text_file(
            client, app, scoped, external_user="1001", uploader=owner, label="N-OWN-A", body=body_a
        )
        raw_decoy = _upload_text_file(
            client,
            app,
            scoped,
            external_user="1001",
            uploader=owner,
            label="N-DECOY",
            body=body_decoy,
        )
        ko_a = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_a, title="NA", body=body_a)
        ko_b = _ensure_file_knowledge(storage, tenant=tenant, raw_id=raw_b, title="NB", body=body_b)
        ko_decoy = _ensure_file_knowledge(
            storage, tenant=tenant, raw_id=raw_decoy, title="ND", body=body_decoy
        )

        conversation = storage.create_conversation(owner, title="multi-citation negatives")
        storage.set_channel_conversation(owner, "telegram", "5001", conversation["id"])
        healthy_source = storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Healthy citations [K1][K2].",
            metadata={"knowledge_citations": {"K1": ko_a, "K2": ko_b, "K3": ko_decoy}},
        )

        class NeverModel:
            enabled = True
            model = "never-on-closed-multi-citation"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, _messages, **_kwargs):
                self.calls += 1
                raise AssertionError("closed multi-citation reached the model")

        never_model = NeverModel()
        monkeypatch.setattr(app.state.agent, "chat", canonical_chat)
        app.state.agent.llm = never_model

        disk_reads: list[tuple[str, str]] = []
        canonical_disk_read = runtime_module.read_authorized_file

        def observed_disk_read(*args, **kwargs):
            disk_reads.append((str(args[2]), str(kwargs.get("person_id") or "")))
            return canonical_disk_read(*args, **kwargs)

        monkeypatch.setattr(runtime_module, "read_authorized_file", observed_disk_read)

        def assert_closed(response, *, allow_disk: bool = False) -> None:
            assert response.status_code == 200, response.text
            body = response.json()
            assert body.get("attachment_context_readable_count", 0) == 0
            text = str(body.get("message") or "")
            assert canary_a not in text
            assert canary_b not in text
            assert decoy_canary not in text
            assert never_model.calls == 0
            if not allow_disk:
                assert disk_reads == []

        # 1) unknown label
        disk_reads.clear()
        unknown = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K9], затем [K1]",
                "source_ref": "telegram-update:mcit-neg-unknown",
                "telegram_message_id": 22_001,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert_closed(unknown)

        # 2) deleted KO
        dead_ko = KnowledgeObject(
            id=new_id("ko"),
            user_id=tenant,
            raw_object_id=raw_a,
            content=body_a,
            content_type="file",
            title="dead",
        )
        storage.store_knowledge_object(dead_ko)
        storage.execute(
            "UPDATE knowledge_objects SET deleted_at='2026-08-12T00:00:00+00:00' WHERE id=?",
            (dead_ko.id,),
        )
        storage.commit()
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "deleted ko map",
            metadata={"knowledge_citations": {"K1": dead_ko.id, "K2": ko_b}},
        )
        disk_reads.clear()
        deleted_ko = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K1], затем [K2]",
                "source_ref": "telegram-update:mcit-neg-del-ko",
                "telegram_message_id": 22_002,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert_closed(deleted_ko)

        # 3) deleted Raw (soft)
        storage.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-12T00:00:00+00:00' WHERE id=?",
            (raw_a,),
        )
        storage.commit()
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "deleted raw map",
            metadata={"knowledge_citations": {"K1": ko_a, "K2": ko_b}},
        )
        disk_reads.clear()
        deleted_raw = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K1], затем [K2]",
                "source_ref": "telegram-update:mcit-neg-del-raw",
                "telegram_message_id": 22_003,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert_closed(deleted_raw)
        # restore raw_a for later cases that re-upload? better use fresh raws
        storage.execute(
            "UPDATE raw_objects SET deleted_at=NULL WHERE id=?",
            (raw_a,),
        )
        storage.commit()

        # 4) An exact explicit citation remains a typed direct-read authority
        # even when the cited Inbox row is ignored. This exception is confined
        # to the exact K1/K2 selector; ambient/latest restoration still excludes
        # ignored material.
        storage.execute(
            "UPDATE inbox SET status='ignored' WHERE user_id=? AND raw_object_id=?",
            (tenant, raw_b),
        )
        storage.commit()
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "ignored map",
            metadata={"knowledge_citations": {"K1": ko_a, "K2": ko_b}},
        )
        disk_reads.clear()

        class IgnoredCitationModel:
            enabled = True
            model = "explicit-ignored-citation"
            total_budget_sec = 5.0

            def __init__(self) -> None:
                self.calls = 0

            async def chat(self, messages, **_kwargs):
                projected = json.dumps(messages, ensure_ascii=False)
                self.calls += 1
                assert canary_a in projected and canary_b in projected
                assert projected.index(canary_a) < projected.index(canary_b)
                assert decoy_canary not in projected
                return {
                    "content": f"Exact ignored citations: {canary_a}; {canary_b}.",
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }

        ignored_model = IgnoredCitationModel()
        app.state.agent.llm = ignored_model
        ignored = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K1], затем [K2]",
                "source_ref": "telegram-update:mcit-neg-ignored",
                "telegram_message_id": 22_004,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        assert ignored.status_code == 200, ignored.text
        ignored_payload = ignored.json()
        ignored_text = str(ignored_payload.get("message") or "")
        assert canary_a in ignored_text and canary_b in ignored_text
        assert decoy_canary not in ignored_text
        assert ignored_payload["attachment_context_expected_count"] == 2
        assert ignored_payload["attachment_context_readable_count"] == 2
        assert ignored_payload["attachment_coverage_complete"] is True
        assert ignored_payload["attachment_verification_complete"] is True
        assert ignored_model.calls == 1
        assert disk_reads == [(raw_a, owner), (raw_b, jbl)] * 2
        ignored_public = json.dumps(ignored_payload, ensure_ascii=False)
        assert raw_a not in ignored_public and raw_b not in ignored_public and raw_decoy not in ignored_public
        assert "conversation_attachment_raw_ids" not in ignored_public
        assert "conversation_attachment_uploaders" not in ignored_public
        app.state.agent.llm = never_model
        storage.execute(
            "UPDATE inbox SET status='classified' WHERE user_id=? AND raw_object_id=?",
            (tenant, raw_b),
        )
        storage.commit()

        # 5) disk-SHA mismatch on one member
        raw_row = storage.get_raw_object(raw_a, tenant)
        assert raw_row is not None
        meta = raw_row.get("metadata_json")
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert isinstance(meta, dict)
        broken = dict(meta)
        broken["sha256"] = "0" * 64
        storage.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (json.dumps(broken, ensure_ascii=False, sort_keys=True), raw_a),
        )
        storage.commit()
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "sha mismatch map",
            metadata={"knowledge_citations": {"K1": ko_a, "K2": ko_b}},
        )
        disk_reads.clear()
        sha_mismatch = _bridge_call(
            client,
            scoped,
            "POST",
            "/api/chat",
            {
                "message": "Сравни [K1], затем [K2]",
                "source_ref": "telegram-update:mcit-neg-sha",
                "telegram_message_id": 22_005,
                "telegram_user": {"id": 1001, "first_name": "Owner"},
            },
            user="1001",
        )
        # verification may touch disk for the healthy sibling or failed member;
        # model and sibling canaries must still be absent.
        assert_closed(sha_mismatch, allow_disk=True)
        storage.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False, sort_keys=True), raw_a),
        )
        storage.commit()

        # 6) denied files.read
        storage.set_permission_override(owner, "files.read", "deny")
        try:
            disk_reads.clear()
            denied_read = _bridge_call(
                client,
                scoped,
                "POST",
                "/api/chat",
                {
                    "message": "Сравни [K2], затем [K1]",
                    "source_ref": "telegram-update:mcit-neg-files-read",
                    "telegram_message_id": 22_006,
                    "telegram_user": {"id": 1001, "first_name": "Owner"},
                },
                user="1001",
            )
            assert_closed(denied_read)
        finally:
            storage.set_permission_override(owner, "files.read", None)

        # 7) denied admin.all_data.read for JBL member (owner loses cross-uploader)
        storage.set_permission_override(owner, "admin.all_data.read", "deny")
        try:
            disk_reads.clear()
            denied_admin = _bridge_call(
                client,
                scoped,
                "POST",
                "/api/chat",
                {
                    "message": "Сравни [K2], затем [K1]",
                    "source_ref": "telegram-update:mcit-neg-admin",
                    "telegram_message_id": 22_007,
                    "telegram_user": {"id": 1001, "first_name": "Owner"},
                    "reply_source_message_id": str(healthy_source["id"]),
                },
                user="1001",
            )
            assert_closed(denied_admin)
        finally:
            storage.set_permission_override(owner, "admin.all_data.read", None)

        # 8) malformed citation-like tokens close the selector (no catalog/latest)
        storage.store_message(
            conversation["id"],
            owner,
            "assistant",
            "Healthy for malformed matrix [K1][K2].",
            metadata={"knowledge_citations": {"K1": ko_a, "K2": ko_b, "K3": ko_decoy}},
        )
        malformed_tokens = (
            "[K0]",
            "[K100]",
            "[K1,,K2]",
            "[K1",
            "[K1] и [K0]",
        )
        for index, token in enumerate(malformed_tokens):
            disk_reads.clear()
            never_model.calls = 0
            malformed = _bridge_call(
                client,
                scoped,
                "POST",
                "/api/chat",
                {
                    "message": f"Сравни {token}",
                    "source_ref": f"telegram-update:mcit-neg-malformed-{index}",
                    "telegram_message_id": 22_100 + index,
                    "telegram_user": {"id": 1001, "first_name": "Owner"},
                },
                user="1001",
            )
            assert_closed(malformed)
