"""Exact current-upload replay may refresh stale extraction without rewriting Raw."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.file_evidence import current_turn_file_reference_of
from friday.permissions import ActorContext
from friday.server import _current_turn_file_attachment
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id


class _UnusedLLM:
    enabled = True
    model = "current-upload-reinspection-test"

    async def chat(self, messages, **kwargs):  # pragma: no cover - generation is patched
        del messages, kwargs
        raise AssertionError("unexpected direct model call")


def _sparse_registered_pdf(storage) -> RawObject:  # noqa: ANN001
    storage.ensure_user("alice")
    raw_id = new_id("raw")
    content = b"%PDF-current-upload-identical-bytes"
    digest = hashlib.sha256(content).hexdigest()
    relative_path = f"alice/{raw_id}.pdf"
    stored_path = storage.settings.files_dir / relative_path
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(content)
    raw = RawObject(
        id=raw_id,
        user_id="alice",
        source="upload",
        source_ref=f"telegram-file:{raw_id}",
        raw_content="30 декабря 2025 г.",
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": "scan.pdf",
            "mime_type": "application/pdf",
            "uploaded_by": "alice",
            "stored_path": relative_path,
            "sha256": digest,
            "size_bytes": len(content),
            "extraction_receipt_version": 1,
            "extraction_success": True,
            "text_extraction_success": True,
            "extraction_chars": 19,
            "parse_pages_read": 1,
            "parse_total_pages": 1,
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id="alice",
            raw_object_id=raw_id,
            status=InboxStatus.PENDING,
            suggested_action="review",
        )
    )
    return raw


def _replay_attachment(storage, raw: RawObject) -> dict[str, object]:  # noqa: ANN001
    stored = storage.get_raw_object(raw.id, "alice")
    assert isinstance(stored, dict)
    return _current_turn_file_attachment(
        filename="scan.pdf",
        file_ingestion={
            "idempotent_replay": True,
            "raw_object_id": raw.id,
            "extraction": {
                "success": True,
                "text_success": True,
                "chars": 19,
                "parse_pages_read": 1,
                "parse_total_pages": 1,
            },
        },
        raw=stored,
        storage=storage,
        tenant_id="alice",
        uploaded_by="alice",
    )


def _patch_generation(runtime: AgentRuntime, monkeypatch) -> list[list[dict]]:  # noqa: ANN001
    seen: list[list[dict]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        seen.append([dict(item) for item in (attachments or [])])
        return {"content": "Синтетическая сводка по файлу.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    return seen


@pytest.mark.asyncio
async def test_sparse_identical_current_upload_reinspects_exact_bytes_without_mutating_raw(
    settings,
    storage,
    monkeypatch,
) -> None:
    raw = _sparse_registered_pdf(storage)
    attachment = _replay_attachment(storage, raw)
    token = current_turn_file_reference_of(attachment)
    assert token is not None and token.reinspect_current_upload is True
    before = storage.get_raw_object(raw.id, "alice")

    inspections: list[bytes] = []

    async def inspect(content, **kwargs):  # noqa: ANN001
        inspections.append(bytes(content))
        assert kwargs["filename"] == "scan.pdf"
        assert kwargs["preferred_language"] == "ru"
        return {
            "extraction_success": True,
            "advisory_only": True,
            "_runtime_source_text": "ПОЛНЫЙ ТЕКСТ ПОВТОРНО ОСМОТРЕННОГО СКАНА",
            "text_preview": "ПОЛНЫЙ ТЕКСТ ПОВТОРНО ОСМОТРЕННОГО СКАНА",
            "parse_pages_read": 1,
            "parse_total_pages": 1,
        }

    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_UnusedLLM())
    runtime.kernel.ingestion = SimpleNamespace(inspect_file_transient=inspect)
    seen = _patch_generation(runtime, monkeypatch)
    conversation = storage.create_conversation("alice")

    await runtime.chat(
        "alice",
        "Что написано в этом документе?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        conversation_id=conversation["id"],
        attachments=[attachment],
        enable_tools=False,
    )

    assert inspections == [b"%PDF-current-upload-identical-bytes"]
    assert len(seen) == 1
    assert seen[0][0]["transient_text"] == "ПОЛНЫЙ ТЕКСТ ПОВТОРНО ОСМОТРЕННОГО СКАНА", repr(seen[0][0])
    after = storage.get_raw_object(raw.id, "alice")
    assert before is not None and after is not None
    assert after["raw_content"] == before["raw_content"]
    assert after["content_hash"] == before["content_hash"]
    assert json.loads(after["metadata_json"]) == json.loads(before["metadata_json"])


@pytest.mark.asyncio
async def test_plain_json_or_historical_sparse_pointer_cannot_force_reinspection(
    settings,
    storage,
    monkeypatch,
) -> None:
    raw = _sparse_registered_pdf(storage)
    private_current = _replay_attachment(storage, raw)
    copied_json = dict(private_current)
    assert current_turn_file_reference_of(copied_json) is None

    async def forbidden_inspection(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("ordinary historical/JSON carrier forced parser work")

    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_UnusedLLM())
    runtime.kernel.ingestion = SimpleNamespace(inspect_file_transient=forbidden_inspection)
    seen = _patch_generation(runtime, monkeypatch)
    conversation = storage.create_conversation("alice")

    await runtime.chat(
        "alice",
        "Какая дата указана в этом документе?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        conversation_id=conversation["id"],
        attachments=[copied_json],
        enable_tools=False,
    )

    assert len(seen) == 1
    assert seen[0][0]["transient_text"] == "30 декабря 2025 г."


def test_healthy_duplicate_does_not_request_current_upload_reinspection(storage) -> None:  # noqa: ANN001
    raw = _sparse_registered_pdf(storage)
    stored = storage.get_raw_object(raw.id, "alice")
    assert isinstance(stored, dict)
    metadata = json.loads(stored["metadata_json"])
    metadata["extraction_chars"] = 500
    metadata["text_truncated"] = True
    metadata["parse_pages_truncated"] = True
    metadata["parse_pages_read"] = 3
    metadata["parse_total_pages"] = 10
    stored["raw_content"] = "Полный проверенный текст. " * 24
    stored["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    attachment = _current_turn_file_attachment(
        filename="scan.pdf",
        file_ingestion={
            "idempotent_replay": True,
            "raw_object_id": raw.id,
            "extraction": {"success": True, "text_success": True, "chars": 500},
        },
        raw=stored,
        tenant_id="alice",
        uploaded_by="alice",
    )

    token = current_turn_file_reference_of(attachment)
    assert token is not None
    assert token.reinspect_current_upload is False


def test_exact_empty_text_duplicate_is_not_reparsed() -> None:
    raw_id = "raw_0000000000000001"
    attachment = _current_turn_file_attachment(
        filename="empty.txt",
        file_ingestion={
            "idempotent_replay": True,
            "raw_object_id": raw_id,
            "extraction": {"success": True, "text_success": False, "chars": 0},
        },
        raw={
            "id": raw_id,
            "user_id": "alice",
            "source": "upload",
            "source_ref": "telegram-file:empty",
            "content_type": "file",
            "content_hash": hashlib.sha256(b"").hexdigest(),
            "raw_content": "[document: empty.txt; type=text/plain; size=0]",
            "metadata_json": {
                "filename": "empty.txt",
                "mime_type": "text/plain",
                "uploaded_by": "alice",
                "extraction_receipt_version": 1,
                "extraction_success": True,
                "text_extraction_success": False,
                "extraction_chars": 0,
            },
        },
        tenant_id="alice",
        uploaded_by="alice",
    )

    token = current_turn_file_reference_of(attachment)
    assert token is not None
    assert token.reinspect_current_upload is False


def test_failed_duplicate_requests_current_upload_reinspection() -> None:
    raw_id = "raw_0000000000000002"
    attachment = _current_turn_file_attachment(
        filename="legacy.doc",
        file_ingestion={
            "idempotent_replay": True,
            "raw_object_id": raw_id,
            "extraction": {"success": False, "text_success": False, "chars": 0},
        },
        raw={
            "id": raw_id,
            "user_id": "alice",
            "source": "upload",
            "source_ref": "telegram-file:failed",
            "content_type": "file",
            "content_hash": hashlib.sha256(b"legacy-doc").hexdigest(),
            "raw_content": "[document: legacy.doc; type=application/msword; size=10]",
            "metadata_json": {
                "filename": "legacy.doc",
                "mime_type": "application/msword",
                "uploaded_by": "alice",
                "extraction_receipt_version": 1,
                "extraction_success": False,
                "text_extraction_success": False,
                "extraction_chars": 0,
            },
        },
        tenant_id="alice",
        uploaded_by="alice",
    )

    token = current_turn_file_reference_of(attachment)
    assert token is not None
    assert token.reinspect_current_upload is True


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("legacy.doc", "application/msword"),
        ("legacy.fh11", "application/octet-stream"),
        ("neutral.bin", "application/vnd.sun.xml.draw"),
    ],
)
def test_sparse_legacy_office_duplicate_requests_current_upload_reinspection(
    filename: str,
    mime_type: str,
) -> None:
    raw_id = "raw_0000000000000003"
    attachment = _current_turn_file_attachment(
        filename=filename,
        file_ingestion={
            "idempotent_replay": True,
            "raw_object_id": raw_id,
            "extraction": {"success": True, "text_success": True, "chars": 12},
        },
        raw={
            "id": raw_id,
            "user_id": "alice",
            "source": "upload",
            "source_ref": "telegram-file:sparse-office",
            "content_type": "file",
            "content_hash": hashlib.sha256(b"legacy-doc").hexdigest(),
            "raw_content": "Старый огрызок",
            "metadata_json": {
                "filename": filename,
                "mime_type": mime_type,
                "uploaded_by": "alice",
                "extraction_receipt_version": 1,
                "extraction_success": True,
                "text_extraction_success": True,
                "extraction_chars": 12,
            },
        },
        tenant_id="alice",
        uploaded_by="alice",
    )

    token = current_turn_file_reference_of(attachment)
    assert token is not None
    assert token.reinspect_current_upload is True
