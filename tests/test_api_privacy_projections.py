"""HTTP projections do not publish storage and parser internals by accident."""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from friday.api.projections import (
    public_chat_ingestion,
    public_conversation_message,
    public_file_record,
    public_ingestion_receipt,
)
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES
from friday.server import create_app
from friday.storage.models import InboxItem, KnowledgeObject, RawObject, new_id
from friday.telegram_bridge._callbacks import CallbacksMixin


class _SyntheticAuthorityStorage:
    def __init__(self, *, user_id: str, raw_id: str) -> None:
        self.user_id = user_id
        self.raw_id = raw_id

    def get_raw_object(self, raw_id: str, user_id: str):
        if (raw_id, user_id) == (self.raw_id, self.user_id):
            return {"id": raw_id, "user_id": user_id}
        return None

    def get_inbox_item(self, _inbox_id: str, _user_id: str):
        return None

    def get_knowledge_object(self, _knowledge_id: str, _user_id: str):
        return None


def test_ingestion_receipts_are_allowlists_even_for_adversarial_internal_results() -> None:
    sentinel = "SYNTHETIC-PROJECTION-SECRET-4f813d"
    raw_id = "raw_0123456789abcdef"
    internal = {
        "promoted": False,
        "queued_for_review": True,
        "raw_object_id": raw_id,
        "inbox_id": "inbox_private_pointer",
        "stored_path": f"/srv/private/{sentinel}/document.bin",
        "transcript_text": f"raw transcript {sentinel}",
        "knowledge_object": {"content": sentinel, "raw_object_id": raw_id},
        "suggestions": {"excerpt": sentinel},
        "extracted_entities": [{"name": sentinel}],
        "extraction": {
            "success": True,
            "text_success": False,
            "chars": 42,
            "text_truncated": True,
            "parse_deadline_reached": False,
            "parse_pages_read": 3,
            "parse_pages_truncated": True,
            "parse_total_pages": 17,
            "error": f"parser failed at /srv/private/{sentinel}",
            "vision": {"model": sentinel, "evidence": sentinel},
        },
    }

    chat_receipt = public_ingestion_receipt(internal, file=True)
    assert chat_receipt == {
        "promoted": False,
        "queued_for_review": True,
        "persisted": True,
        "extraction": {
            "success": True,
            "text_success": False,
            "chars": 42,
            "text_truncated": True,
            "parse_deadline_reached": False,
            "parse_pages_read": 3,
            "parse_pages_truncated": True,
            "parse_total_pages": 17,
        },
    }
    encoded = json.dumps(chat_receipt, ensure_ascii=False)
    assert sentinel not in encoded
    assert raw_id not in encoded
    assert "stored_path" not in encoded
    assert "transcript" not in encoded
    assert "error" not in encoded

    # A direct creation endpoint may opt into its capability-gated resource
    # handle, while the chat projection remains pointer-free.
    direct_receipt = public_ingestion_receipt(
        internal,
        file=True,
        include_resource_id=True,
        storage=_SyntheticAuthorityStorage(user_id="tenant-a", raw_id=raw_id),
        resource_user_id="tenant-a",
    )
    assert direct_receipt["raw_object_id"] == raw_id
    assert "stored_path" not in direct_receipt
    assert "raw_object_id" not in public_chat_ingestion({"file_ingestion": internal})["file_ingestion"]

    # Prefix tests are not identifier validation, and an internal result is not
    # tenant authority. Both path-shaped and foreign-but-valid handles disappear.
    malformed = public_ingestion_receipt(
        {"raw_object_id": "raw_/srv/private/secret"},
        include_resource_id=True,
        storage=_SyntheticAuthorityStorage(user_id="tenant-a", raw_id="raw_/srv/private/secret"),
        resource_user_id="tenant-a",
    )
    foreign = public_ingestion_receipt(
        {"raw_object_id": raw_id},
        include_resource_id=True,
        storage=_SyntheticAuthorityStorage(user_id="tenant-b", raw_id=raw_id),
        resource_user_id="tenant-a",
    )
    shared_storage = _SyntheticAuthorityStorage(user_id="shared-tenant", raw_id=raw_id)
    original_lookup = shared_storage.get_raw_object

    def raw_from_another_person(candidate: str, tenant: str):
        row = original_lookup(candidate, tenant)
        if row:
            row["metadata_json"] = json.dumps({"uploaded_by": "bob"})
        return row

    shared_storage.get_raw_object = raw_from_another_person  # type: ignore[method-assign]
    wrong_person = public_ingestion_receipt(
        {"raw_object_id": raw_id},
        include_resource_id=True,
        storage=shared_storage,
        resource_user_id="shared-tenant",
        resource_owner_id="alice",
    )
    assert "raw_object_id" not in malformed
    assert "raw_object_id" not in foreign
    assert "raw_object_id" not in wrong_person


def test_chat_projection_never_publishes_internal_control_metadata() -> None:
    sentinel = "SYNTHETIC-CACHED-OUTCOME-PRIVATE-f9487c"
    projected = public_chat_ingestion(
        {
            "content": "Visible synthetic answer.",
            "interaction_trace": {
                "schema": "friday.interaction-turn-trace.v1",
                "conversation_digest": "a" * 64,
            },
            "accepted_capability_outcome": {
                "schema": "friday.accepted-capability-outcome-receipt.v1",
                "private_sentinel": sentinel,
            },
            "accepted_effect_outcome": {
                "schema": "friday.accepted-effect-outcome-receipt.v1",
                "private_sentinel": sentinel,
            },
            "accepted_archive_recall_outcome": {
                "schema": "friday.accepted-archive-recall-outcome-receipt.v1",
                "private_sentinel": sentinel,
            },
            "source_search_result_identities": {
                "raw_0123456789abcdef": sentinel,
            },
        }
    )

    assert projected == {"content": "Visible synthetic answer."}
    assert sentinel not in json.dumps(projected, ensure_ascii=False)


def test_conversation_projection_never_publishes_accepted_capability_outcome() -> None:
    sentinel = "SYNTHETIC-ACCEPTED-OUTCOME-PRIVATE-74cf81"
    projected = public_conversation_message(
        {
            "id": "msg_0123456789abcdef",
            "role": "assistant",
            "content": "Visible synthetic answer.",
            "created_at": "2026-08-23T12:00:00+00:00",
            "metadata_json": json.dumps(
                {
                    "accepted_capability_outcome": {
                        "schema": "friday.accepted-capability-outcome-receipt.v1",
                        "private_sentinel": sentinel,
                    },
                    "source_search_result_identities": {
                        "raw_0123456789abcdef": sentinel,
                    },
                }
            ),
        }
    )

    assert projected == {
        "id": "msg_0123456789abcdef",
        "role": "assistant",
        "content": "Visible synthetic answer.",
        "created_at": "2026-08-23T12:00:00+00:00",
    }
    assert sentinel not in json.dumps(projected, ensure_ascii=False)


def test_shared_owner_projection_accepts_full_bounded_metadata_without_exposing_office_index() -> None:
    raw_id = "raw_0123456789abcdef"
    sentinel = "SYNTHETIC-OFFICE-INDEX-PRIVATE-9c22b1"

    class _LargeMetadataStorage(_SyntheticAuthorityStorage):
        def __init__(self, metadata_json: str) -> None:
            super().__init__(user_id="shared-tenant", raw_id=raw_id)
            self.metadata_json = metadata_json

        def get_raw_object(self, candidate: str, tenant: str):
            row = super().get_raw_object(candidate, tenant)
            if row is not None:
                row["metadata_json"] = self.metadata_json
            return row

    metadata = json.dumps(
        {
            "uploaded_by": "alice",
            "office_structure_v1": {"private_sentinel": sentinel},
            "office_structure_attestation_v1": "a" * 64,
            "padding": "X" * 70_000,
        },
        separators=(",", ":"),
    )
    assert 65_536 < len(metadata.encode("utf-8")) < RAW_FILE_METADATA_MAX_BYTES
    projected = public_ingestion_receipt(
        {
            "raw_object_id": raw_id,
            "office_structure_v1": {"private_sentinel": sentinel},
            "office_structure_attestation_v1": "a" * 64,
        },
        include_resource_id=True,
        storage=_LargeMetadataStorage(metadata),
        resource_user_id="shared-tenant",
        resource_owner_id="alice",
    )
    assert projected["raw_object_id"] == raw_id
    encoded = json.dumps(projected, ensure_ascii=False)
    assert sentinel not in encoded
    assert "office_structure_v1" not in encoded
    assert "office_structure_attestation_v1" not in encoded

    oversized = json.dumps(
        {"uploaded_by": "alice", "padding": "X" * RAW_FILE_METADATA_MAX_BYTES},
        separators=(",", ":"),
    )
    refused = public_ingestion_receipt(
        {"raw_object_id": raw_id},
        include_resource_id=True,
        storage=_LargeMetadataStorage(oversized),
        resource_user_id="shared-tenant",
        resource_owner_id="alice",
    )
    assert "raw_object_id" not in refused


def test_file_list_projection_keeps_download_handle_but_not_private_provenance() -> None:
    sentinel = "SYNTHETIC-FILE-METADATA-SECRET-93bc2a"
    projected = public_file_record(
        {
            "id": "raw_0123456789abcdef",
            "user_id": sentinel,
            "source_ref": sentinel,
            "received_at": "2026-08-06T12:00:00+00:00",
            "deleted_at": None,
        },
        {
            "filename": f"/home/private/{sentinel}/report.txt",
            "mime_type": "text/plain",
            "size_bytes": 123,
            "stored_path": f"/home/private/{sentinel}/report.txt",
            "sha256": sentinel,
            "uploaded_by": sentinel,
            "chat_id": sentinel,
            "transcription": {"text": sentinel, "model": sentinel},
            "extraction_error": sentinel,
            "promotion_assessment": {"reason": sentinel},
        },
    )

    assert projected == {
        "id": "raw_0123456789abcdef",
        "received_at": "2026-08-06T12:00:00+00:00",
        "deleted_at": None,
        "metadata": {
            "filename": "report.txt",
            "mime_type": "text/plain",
            "size_bytes": 123,
        },
    }
    assert sentinel not in json.dumps(projected, ensure_ascii=False)


def test_path_shaped_opaque_id_lookalikes_never_become_public_handles() -> None:
    sentinel = "SYNTHETIC-PATH-ID-SECRET-1f7d91"
    raw_path_id = f"raw_/home/private/{sentinel}"
    msg_path_id = f"msg_C:/private/{sentinel}"

    file_row = public_file_record(
        {"id": raw_path_id, "received_at": "2026-08-06T12:00:00+00:00"},
        {},
    )
    message = public_conversation_message(
        {
            "id": msg_path_id,
            "role": "assistant",
            "content": "safe synthetic answer",
        }
    )

    assert "id" not in file_row
    assert "id" not in message
    assert sentinel not in json.dumps([file_row, message], ensure_ascii=False)


def test_user_and_admin_file_lists_apply_the_projection_at_the_http_boundary(settings) -> None:
    sentinel = "SYNTHETIC-STORED-FILE-SECRET-1d9a03"
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        raw = app.state.storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=LEGACY_OWNER_USER_ID,
                source="upload",
                source_ref=sentinel,
                raw_content="synthetic file placeholder",
                content_type="file",
                metadata_json={
                    "filename": "safe.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 17,
                    "stored_path": f"/home/private/{sentinel}/safe.txt",
                    "uploaded_by": sentinel,
                    "transcription": {"text": sentinel},
                    "extraction_error": sentinel,
                },
            )
        )
        own = client.get("/api/files", headers=headers)
        admin = client.get(
            "/api/admin/files",
            params={"user_id": LEGACY_OWNER_USER_ID},
            headers=headers,
        )

    assert own.status_code == 200, own.text
    assert admin.status_code == 200, admin.text
    for payload in (own.json(), admin.json()):
        item = next(candidate for candidate in payload["items"] if candidate["id"] == raw.id)
        assert item["metadata"] == {
            "filename": "safe.txt",
            "mime_type": "text/plain",
            "size_bytes": 17,
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        assert sentinel not in encoded
        assert "stored_path" not in encoded
        assert "transcription" not in encoded


def test_self_and_admin_conversation_messages_never_return_raw_metadata(settings) -> None:
    sentinel = "SYNTHETIC-MESSAGE-METADATA-SECRET-6ac291"
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        storage = app.state.storage
        user_id = LEGACY_OWNER_USER_ID
        storage.ensure_user(user_id)

        raw = storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=user_id,
                source="test",
                source_ref="privacy-projection:knowledge",
                raw_content="Synthetic source",
                content_type="text",
            )
        )
        knowledge = storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id=user_id,
                raw_object_id=raw.id,
                content="Synthetic source",
                title="Safe synthetic title",
                summary="Synthetic source",
            )
        )
        conversation = storage.create_conversation(user_id, "Projection test")
        storage.store_message(
            conversation["id"],
            user_id,
            "assistant",
            "Visible synthetic answer.",
            metadata={
                "answer_grounded": True,
                "answer_mode": "personal_knowledge",
                "knowledge_hits": 1,
                "retrieval_confidence": 0.84,
                "search_query": "safe synthetic query",
                "verification_status": "passed",
                "verified": True,
                "accepted_capability_outcome": {
                    "schema": "friday.accepted-capability-outcome-receipt.v1",
                    "outcome_sha256": sentinel,
                },
                "knowledge_citations": {
                    "K1": knowledge.id,
                    "K2": raw.id,
                    "K3": f"ko_/home/private/{sentinel}",
                    sentinel: f"/home/private/{sentinel}",
                },
                "conversation_attachment_raw_ids": [raw.id],
                "transient_text": sentinel,
                "stored_path": f"/home/private/{sentinel}/document.txt",
                "nested": {"raw_object_id": raw.id, "excerpt": sentinel},
                "retrieval_trace": [
                    {
                        "id": knowledge.id,
                        "title": f"/home/private/{sentinel}",
                        "score": 0.84,
                        "status": "returned",
                    },
                    {
                        "id": raw.id,
                        "title": sentinel,
                        "score": 1.0,
                        "status": "returned",
                    },
                    {
                        "id": f"ko_/home/private/{sentinel}",
                        "title": sentinel,
                        "score": 1.0,
                        "status": "returned",
                    },
                ],
            },
        )
        storage.set_channel_conversation(user_id, "api", "projection-channel", conversation["id"])

        own = client.get(
            f"/api/conversations/{conversation['id']}/messages",
            headers=headers,
        )
        admin = client.get(
            f"/api/admin/conversations/{conversation['id']}/messages",
            params={"user_id": user_id},
            headers=headers,
        )
        why = client.get(
            "/api/conversations/channel/why",
            params={"channel": "api", "channel_id": "projection-channel"},
            headers=headers,
        )

    assert own.status_code == 200, own.text
    own_item = own.json()["items"][0]
    assert own_item == {
        "id": own_item["id"],
        "role": "assistant",
        "content": "Visible synthetic answer.",
        "created_at": own_item["created_at"],
    }
    assert "metadata_json" not in own_item

    assert admin.status_code == 200, admin.text
    admin_item = admin.json()["items"][0]
    assert "metadata_json" not in admin_item
    assert admin_item["insights"] == {
        "answer_grounded": True,
        "verification_status": "passed",
        "verified": True,
        "citations": [
            {
                "label": "K1",
                "knowledge_id": knowledge.id,
                "title": "Safe synthetic title",
            }
        ],
    }
    assert why.status_code == 200, why.text
    assert why.json()["citations"] == {"K1": knowledge.id}
    assert why.json()["trace"] == [
        {
            "id": knowledge.id,
            "title": "Safe synthetic title",
            "score": 0.84,
            "status": "returned",
        }
    ]
    for payload in (own.json(), admin.json(), why.json()):
        encoded = json.dumps(payload, ensure_ascii=False)
        assert sentinel not in encoded
        assert raw.id not in encoded
        assert "/home/private/" not in encoded
        assert "accepted_capability_outcome" not in encoded


def test_http_intake_and_chat_project_before_serialising_or_caching(settings) -> None:
    sentinel = "SYNTHETIC-HTTP-INGESTION-SECRET-20d4a8"
    raw_id = new_id("raw")
    app = create_app(settings)

    async def adversarial_ingest_file(*_args, **_kwargs):
        return {
            "promoted": False,
            "queued_for_review": True,
            "raw_object_id": raw_id,
            "stored_path": f"/var/lib/private/{sentinel}/file.txt",
            "transcript_text": f"transcript {sentinel}",
            "knowledge_object": {"content": sentinel},
            "extraction": {
                "success": True,
                "text_success": True,
                "chars": 10,
                "error": f"/var/lib/private/{sentinel}",
                "vision": {"evidence": sentinel},
            },
        }

    async def quiet_chat(*_args, **_kwargs):
        return {"conversation_id": "conv_synthetic", "message": "ok"}

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    encoded_document = base64.b64encode(b"synthetic bytes").decode("ascii")

    with TestClient(app) as client:
        app.state.storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id=LEGACY_OWNER_USER_ID,
                source="test",
                source_ref="projection-authority:file",
                raw_content="synthetic file placeholder",
                content_type="file",
                metadata_json={"filename": "synthetic.txt", "uploaded_by": LEGACY_OWNER_USER_ID},
            )
        )
        app.state.ingestion.ingest_file = adversarial_ingest_file
        app.state.agent.chat = quiet_chat
        uploaded = client.post(
            "/api/files",
            files={"file": ("synthetic.txt", b"synthetic bytes", "text/plain")},
            headers=headers,
        )
        chatted = client.post(
            "/api/chat",
            json={
                "message": "Inspect this synthetic document.",
                "document": {
                    "filename": "synthetic.txt",
                    "mime_type": "text/plain",
                    "content_base64": encoded_document,
                },
            },
            headers=headers,
        )

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["raw_object_id"] == raw_id
    assert chatted.status_code == 200, chatted.text
    assert "raw_object_id" not in chatted.json()["file_ingestion"]
    for response in (uploaded, chatted):
        assert sentinel not in response.text
        assert "stored_path" not in response.text
        assert "transcript_text" not in response.text
        assert '"error"' not in response.text


def test_chat_keeps_only_an_owned_inbox_handle_and_revalidates_it_on_replay(settings) -> None:
    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        storage = app.state.storage
        raw = storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=LEGACY_OWNER_USER_ID,
                source="test",
                source_ref="projection-authority:chat",
                raw_content="synthetic review candidate",
                content_type="text",
            )
        )
        inbox = storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=LEGACY_OWNER_USER_ID,
                raw_object_id=raw.id,
            )
        )

        async def review_ingest(*_args, **_kwargs):
            return {
                "promoted": False,
                "queued_for_review": True,
                "raw_object_id": raw.id,
                "inbox_id": inbox.id,
            }

        async def quiet_chat(*_args, **_kwargs):
            return {
                "conversation_id": "conv_synthetic",
                "message_id": "msg_0123456789abcdef",
                "message": "ok",
            }

        request = {
            "message": "synthetic candidate for review",
            "source_ref": "projection-authority:replay",
        }
        app.state.ingestion.ingest_text = review_ingest
        app.state.agent.chat = quiet_chat
        first = client.post("/api/chat", json=request, headers=headers)
        replay = client.post("/api/chat", json=request, headers=headers)

        # The cached public response is re-authorized on every replay. If the
        # resource is no longer visible to this tenant, its handle is not echoed.
        storage.execute("DELETE FROM inbox WHERE id=?", (inbox.id,))
        storage.commit()
        after_removal = client.post("/api/chat", json=request, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    for payload in (first.json(), replay.json()):
        assert payload["ingestion"]["inbox_id"] == inbox.id
        assert "raw_object_id" not in payload["ingestion"]
        markup = CallbacksMixin._response_reply_markup(payload, external_user_id="4242")
        assert markup is not None
        encoded = json.dumps(markup, ensure_ascii=False)
        assert f"inbox:promote:{inbox.id}" in encoded
        assert f"inbox:ignore:{inbox.id}" in encoded
    assert replay.json()["idempotent_replay"] is True
    assert after_removal.status_code == 200, after_removal.text
    assert "inbox_id" not in after_removal.json()["ingestion"]
