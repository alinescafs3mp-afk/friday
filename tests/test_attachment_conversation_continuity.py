"""A pending upload remains evidence in its own private conversation.

Inbox review controls promotion into reusable knowledge.  It must not erase a
file from the conversation in which its uploader is still asking about it, and
conversation continuity must not become a side door into another person's file
or ambient retrieval for unrelated questions.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _attachment_body_query_surface,
    _attachment_evidence_chunks,
    _attachment_filename_mentions,
    _attachment_selector_message,
    _historical_direct_read_attachment,
    _is_document_metadata_request,
    _requested_output_filename_stem,
    _supported_direct_attachment_file_only_request,
)
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext
from friday.server import _current_turn_file_attachment
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id


def _pending_file(
    storage,
    tenant_id: str,
    uploader: str,
    text: str,
    *,
    filename: str,
    extraction_success: bool = True,
    extra_metadata: dict[str, object] | None = None,
) -> RawObject:
    storage.ensure_user(tenant_id)
    if uploader != tenant_id:
        storage.ensure_user(uploader)
    raw_id = new_id("raw")
    body = text.encode("utf-8")
    supplied_metadata = dict(extra_metadata or {})
    relative_path = str(supplied_metadata.get("stored_path") or f"{tenant_id}/{raw_id}.bin")
    stored_path = storage.settings.files_dir / relative_path
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    if not stored_path.is_file():
        stored_path.write_bytes(body)
    registered_body = stored_path.read_bytes()
    digest = hashlib.sha256(registered_body).hexdigest()
    raw = RawObject(
        id=raw_id,
        user_id=tenant_id,
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "uploaded_by": uploader,
            "extraction_success": extraction_success,
            "text_extraction_success": extraction_success,
            **supplied_metadata,
            "stored_path": relative_path,
            "sha256": digest,
            "size_bytes": len(registered_body),
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant_id,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
            suggested_action="review",
        )
    )
    return raw


def _record_upload(storage, conversation_id: str, user_id: str, raw: RawObject, caption: str) -> None:
    storage.store_message(
        conversation_id,
        user_id,
        "user",
        caption,
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "attachment_origin": "upload",
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    storage.store_message(
        conversation_id,
        user_id,
        "assistant",
        f"прочитан {caption}",
        metadata={"attachment_context_used": True},
    )


def _current_attachment(storage, raw: RawObject) -> dict[str, object]:  # noqa: ANN001
    metadata = raw.metadata_json if isinstance(raw.metadata_json, dict) else {}
    stored = storage.get_raw_object(raw.id, raw.user_id)
    assert isinstance(stored, dict)
    return _current_turn_file_attachment(
        filename=str(metadata.get("filename") or "attachment"),
        file_ingestion={
            "raw_object_id": raw.id,
            "extraction": {
                "success": True,
                "text_success": True,
                "chars": len(raw.raw_content),
            },
        },
        raw=stored,
        storage=storage,
    )


def _transient_attachment(*, filename: str, text: str) -> dict[str, object]:
    """Build the process-private no-save carrier produced by the API boundary."""

    return _current_turn_file_attachment(
        filename=filename,
        file_ingestion={
            "extraction": {
                "success": True,
                "text_success": True,
                "chars": len(text),
            }
        },
        raw={
            "raw_content": text,
            "metadata_json": {
                "filename": filename,
                "uploaded_by": "alice",
                "extraction_success": True,
                "text_extraction_success": True,
            },
        },
    )


def _patch_attachment_generation(runtime, monkeypatch):  # noqa: ANN001
    seen: list[tuple[str, list[dict]]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context
        snapshot = [dict(item) for item in (attachments or [])]
        seen.append((str(message), snapshot))
        names = [str(item.get("filename") or "attachment") for item in snapshot]
        return {"content": "Синтетический ответ: " + ", ".join(names), "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    return seen


class _EnabledButUnusedLLM:
    enabled = True
    model = "attachment-test"

    async def chat(self, messages, **kwargs):  # pragma: no cover - patched paths should own the turn
        del messages, kwargs
        raise AssertionError("unexpected direct LLM call")


def test_legacy_upload_origin_is_still_an_upload_chronology_pointer() -> None:
    raw_id = "raw_legacy_upload_pointer"
    message = {
        "role": "user",
        "metadata_json": json.dumps(
            {
                "attachment_origin": "upload",
                "conversation_attachment_raw_ids": [raw_id],
            }
        ),
    }

    assert AgentRuntime._message_uploaded_attachment_ids(message) == [raw_id]  # noqa: SLF001


@pytest.mark.parametrize(
    "message",
    [
        "Напиши документ о свойствах алюминия",
        "Напиши документ про свойства нового материала",
        "Покажи свойства алюминия в этом документе",
        "Покажи реквизиты компании в этом документе",
        "Покажи свойства алюминия в report.odt",
    ],
)
def test_document_creation_about_material_properties_is_not_metadata_navigation(message: str) -> None:
    assert not _is_document_metadata_request(message)


def test_natural_document_metadata_question_is_supported() -> None:
    assert _is_document_metadata_request("Какие метаданные у этого документа?")
    assert _is_document_metadata_request("report.odt: метаданные")


@pytest.mark.asyncio
async def test_quoted_file_pointer_owns_pronoun_metadata_without_becoming_an_upload(
    settings,
    storage,
    monkeypatch,
) -> None:
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "QUOTED-PRIVATE-BODY-MUST-NOT-REACH-MODEL",
        filename="quoted-report.odt",
        extra_metadata={
            "title": "Quoted synthetic title",
            "creator": "Quoted synthetic creator",
            "mime_type": "application/vnd.oasis.opendocument.text",
        },
    )
    conversation = storage.create_conversation("alice")
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_EnabledButUnusedLLM(),
    )

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("quoted metadata route reached model/retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    # Exact live shape: the explicit metadata/file wording used to activate the
    # ordinary history resolver after the structural Telegram pointer failed.
    message = "покажи метаданные этого файла"
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        conversation_id=conversation["id"],
        attachments=[{"raw_object_id": raw.id}],
        enable_tools=True,
        quoted_attachment_reference=True,
    )

    assert "Quoted synthetic title" in result["message"]
    assert "QUOTED-PRIVATE-BODY" not in result["message"]
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=100)
    user_row = next(item for item in rows if item.get("role") == "user" and item.get("content") == message)
    metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    assert metadata["attachment_origin"] == "reply_reference"
    assert metadata["quoted_attachment_reference"] is True
    assert metadata["conversation_attachment_raw_ids"] == [raw.id]
    assert "conversation_uploaded_raw_ids" not in metadata


@pytest.mark.asyncio
async def test_unresolved_quoted_file_pointer_does_not_drift_to_latest_upload(
    settings,
    storage,
    monkeypatch,
) -> None:
    previous = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-UPLOAD-PRIVATE-BODY",
        filename="latest-upload.odt",
        extra_metadata={"title": "Latest upload private title"},
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", previous, "previous upload")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("unresolved quoted pointer reached model/retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    message = "покажи метаданные этого файла"
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
        quoted_attachment_reference=True,
    )

    assert "не удалось открыть документ, на который вы ответили" in result["message"].casefold()
    assert "Latest upload private title" not in result["message"]
    assert "LATEST-UPLOAD-PRIVATE-BODY" not in result["message"]
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=100)
    user_row = next(item for item in rows if item.get("role") == "user" and item.get("content") == message)
    metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    assert metadata["attachment_origin"] == "reply_reference"
    assert metadata["quoted_attachment_reference"] is True
    assert "conversation_attachment_raw_ids" not in metadata
    assert "conversation_uploaded_raw_ids" not in metadata


@pytest.mark.asyncio
async def test_reply_to_assistant_selects_that_answers_file_and_records_safe_lineage(
    settings,
    storage,
    monkeypatch,
) -> None:
    selected = _pending_file(
        storage,
        "alice",
        "alice",
        "ANSWER-A-PRIVATE-BODY-MUST-NOT-REACH-MODEL",
        filename="answer-a.odt",
        extra_metadata={
            "title": "Answer A synthetic title",
            "mime_type": "application/vnd.oasis.opendocument.text",
        },
    )
    newer = _pending_file(
        storage,
        "alice",
        "alice",
        "NEWER-B-PRIVATE-BODY-MUST-NOT-REACH-MODEL",
        filename="newer-b.odt",
        extra_metadata={"title": "Newer B synthetic title"},
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", newer, "newer upload")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("assistant-reply metadata route reached model/retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    selected_pointer = _historical_direct_read_attachment(
        selected.id,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="assistant_lineage",
    )
    assert selected_pointer is not None
    message = "покажи метаданные документа из ответа"
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        conversation_id=conversation["id"],
        attachments=[selected_pointer],
        enable_tools=True,
        reply_assistant_reference=True,
    )

    assert "Answer A synthetic title" in result["message"]
    assert "Newer B synthetic title" not in result["message"]
    assert "PRIVATE-BODY" not in result["message"]
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=100)
    user_row = next(item for item in rows if item.get("role") == "user" and item.get("content") == message)
    user_metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    assert user_metadata["attachment_origin"] == "reply_assistant"
    assert user_metadata["reply_assistant_reference"] is True
    assert user_metadata["conversation_attachment_raw_ids"] == [selected.id]
    assert "conversation_uploaded_raw_ids" not in user_metadata
    assistant_metadata = json.loads(str(rows[-1].get("metadata_json") or "{}"))
    assert assistant_metadata["attachment_context_used"] is True
    assert assistant_metadata["conversation_attachment_raw_ids"] == [selected.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "reference_kwargs", "expected_origin"),
    [
        (
            "Какой контрольный код указан именно в этом документе?",
            {"quoted_attachment_reference": True},
            "reply_reference",
        ),
        (
            "Повтори контрольный код именно из источника процитированного ответа.",
            {"reply_assistant_reference": True},
            "reply_assistant",
        ),
    ],
)
async def test_exact_reply_body_query_uses_structural_pointer_without_runner_identity_token(
    settings,
    storage,
    monkeypatch,
    message,
    reference_kwargs,
    expected_origin,
) -> None:
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "Контрольный код: EXACT-REPLY-BODY-VALUE.",
        filename="exact-reply-source.odt",
    )
    conversation = storage.create_conversation("alice")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    selector_kind = (
        "assistant_lineage" if reference_kwargs.get("reply_assistant_reference") else "telegram_reply"
    )
    pointer = _historical_direct_read_attachment(
        target.id,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind=selector_kind,
    )
    assert pointer is not None
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[pointer],
        enable_tools=False,
        **reference_kwargs,
    )

    assert result["attachment_query_status"] != "not_found"
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [[target.id]]
    assert "EXACT-REPLY-BODY-VALUE" in json.dumps(seen, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=100)
    user = next(row for row in rows if row.get("role") == "user")
    metadata = json.loads(str(user.get("metadata_json") or "{}"))
    assert metadata["attachment_origin"] == expected_origin
    assert metadata["conversation_attachment_raw_ids"] == [target.id]


def test_genuine_context_check_source_line_is_never_masked_from_body_query() -> None:
    message = "Найди точную строку: Контекст проверки: DEADBEEF12345678."

    projected = _attachment_body_query_surface(message)

    assert "Контекст проверки: DEADBEEF12345678" in projected


@pytest.mark.asyncio
async def test_unresolved_reply_to_assistant_never_drifts_to_latest_upload(
    settings,
    storage,
    monkeypatch,
) -> None:
    previous = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-UPLOAD-PRIVATE-BODY",
        filename="latest-upload.odt",
        extra_metadata={"title": "Latest upload private title"},
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", previous, "previous upload")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("unresolved assistant reply reached model/retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    message = "покажи метаданные документа из ответа"
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
        reply_assistant_reference=True,
    )

    assert "вложения ответа пятницы" in result["message"].casefold()
    assert "более новый файл автоматически подставлен не будет" in result["message"].casefold()
    assert "Latest upload private title" not in result["message"]
    assert "LATEST-UPLOAD-PRIVATE-BODY" not in result["message"]
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=100)
    user_row = next(item for item in rows if item.get("role") == "user" and item.get("content") == message)
    metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    assert metadata["attachment_origin"] == "reply_assistant"
    assert metadata["reply_assistant_reference"] is True
    assert "conversation_attachment_raw_ids" not in metadata
    assert "conversation_uploaded_raw_ids" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Покажи метаданные этого документа",
        "Выведи метаданные выбранного документа",
        "Напиши метаданные synthetic-report.odt",
        "Покажи метаданные документа",
    ],
)
async def test_document_metadata_is_authorised_and_rendered_without_model_or_body(
    settings,
    storage,
    monkeypatch,
    query: str,
) -> None:
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "PRIVATE-BODY-MUST-NOT-BE-RENDERED",
        filename="synthetic-report.odt",
        extra_metadata={
            "mime_type": "application/vnd.oasis.opendocument.text",
            "size_bytes": 20_480,
            "document_date": "2026-08-11",
            "title": "Синтетический заголовок",
            "creator": "Синтетический автор",
            "creation_date": "2026-08-11T12:00:00Z",
            "keywords": ["alpha", "beta"],
            "page_count": 3,
            "stored_path": "PRIVATE-STORAGE-PATH",
            "sha256": "PRIVATE-SHA256",
            "uploaded_by_internal": "PRIVATE-UPLOADER-INTERNAL",
        },
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", raw, "synthetic upload")
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_EnabledButUnusedLLM(),
    )

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("metadata route entered retrieval/context preparation")

    async def forbidden_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("metadata route entered response generation")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)
    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
    )

    assert result["message_format"] == "plain"
    assert result["tools_used"] == []
    assert result["restored_attachment_count"] == 1
    assert "synthetic-report.odt" in result["message"]
    assert "Синтетический заголовок" in result["message"]
    assert "Синтетический автор" in result["message"]
    assert "Страницы: 3" in result["message"]
    assert "alpha, beta" in result["message"]
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in (
        "PRIVATE-BODY-MUST-NOT-BE-RENDERED",
        "PRIVATE-STORAGE-PATH",
        "PRIVATE-SHA256",
        "PRIVATE-UPLOADER-INTERNAL",
        raw.id,
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_same_turn_odt_metadata_request_selects_the_only_current_upload(
    settings,
    storage,
    monkeypatch,
) -> None:
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "PRIVATE-ODT-BODY-" * 1_024,
        filename="current-20kb.odt",
        extra_metadata={
            "mime_type": "application/vnd.oasis.opendocument.text",
            "size_bytes": 20_480,
            "title": "Текущий ODT",
            "creator": "Автор ODT",
        },
    )
    conversation = storage.create_conversation("alice")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("same-turn metadata route entered general model routing")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden)
    monkeypatch.setattr(runtime, "_document_content_details_answer", forbidden)
    result = await runtime.chat(
        "alice",
        "покажи метаданные по этому файлу",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, raw)],
        enable_tools=True,
    )

    assert result["tools_used"] == []
    assert result["context"]["llm_failed"] is False
    assert "current-20kb.odt" in result["message"]
    assert "Текущий ODT" in result["message"]
    assert "Автор ODT" in result["message"]
    assert "PRIVATE-ODT-BODY" not in result["message"]
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=10)
    assistant_metadata = json.loads(str(rows[-1].get("metadata_json") or "{}"))
    assert assistant_metadata["document_metadata_owned"] is True


@pytest.mark.asyncio
async def test_metadata_of_another_document_uses_a_newly_attached_current_pointer(
    settings,
    storage,
    monkeypatch,
) -> None:
    old = _pending_file(
        storage,
        "alice",
        "alice",
        "OLD-PRIVATE-BODY",
        filename="old.odt",
        extra_metadata={"title": "Старый заголовок"},
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-PRIVATE-BODY",
        filename="current.odt",
        extra_metadata={"title": "Текущий заголовок"},
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", old, "old upload")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("code-owned metadata route called a model seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    result = await runtime.chat(
        "alice",
        "Покажи метаданные другого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, current)],
        enable_tools=True,
    )

    assert "current.odt" in result["message"]
    assert "Текущий заголовок" in result["message"]
    assert "old.odt" not in result["message"]
    assert "Старый заголовок" not in result["message"]
    assert result["restored_attachment_count"] == 0


@pytest.mark.asyncio
async def test_metadata_of_another_document_without_a_current_pointer_fails_closed(
    settings,
    storage,
    monkeypatch,
) -> None:
    first = _pending_file(storage, "alice", "alice", "FIRST", filename="first.odt")
    second = _pending_file(storage, "alice", "alice", "SECOND", filename="second.odt")
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", first, "first upload")
    _record_upload(storage, conversation["id"], "alice", second, "second upload")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("ambiguous metadata request called a model seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    result = await runtime.chat(
        "alice",
        "Покажи метаданные другого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
    )

    assert "не удалось однозначно определить" in result["message"].casefold()
    assert "first.odt" not in result["message"]
    assert "second.odt" not in result["message"]


@pytest.mark.asyncio
async def test_legacy_odt_metadata_is_hydrated_from_authorised_header_without_raw_mutation(
    settings,
    storage,
    monkeypatch,
) -> None:
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "meta.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <office:meta>
  <dc:title>LEGACY-ODT-TITLE</dc:title>
  <dc:creator>LEGACY-ODT-CREATOR</dc:creator>
  <meta:creation-date>2024-03-04T12:30:00Z</meta:creation-date>
  <meta:document-statistic meta:page-count="7" meta:word-count="321"/>
  <meta:user-defined meta:name="private-path">PRIVATE-ODT-USER-FIELD</meta:user-defined>
 </office:meta>
</office:document-meta>""",
        )
        archive.writestr("content.xml", "PRIVATE-ODT-BODY-MUST-NOT-BE-PARSED")
    content = payload.getvalue()
    relative_path = "legacy/legacy-metadata.odt"
    stored_path = settings.files_dir / relative_path
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(content)
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "LEGACY-RAW-BODY-MUST-NOT-BE-RENDERED",
        filename="legacy-metadata.odt",
        extra_metadata={
            "mime_type": "application/vnd.oasis.opendocument.text",
            # Version 2 was the narrow pre-universal ODF projection. It must
            # not suppress the bounded metadata-only hydration below.
            "format": "odt",
            "metadata_schema_version": 2,
            "title": "STALE-NARROW-ODF-TITLE",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "stored_path": relative_path,
        },
    )
    before = storage.get_raw_object(raw.id, "alice")["metadata_json"]
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", raw, "legacy upload")
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())
    runtime.kernel.ingestion = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    metadata_inspections = 0
    original_inspect = runtime.kernel.ingestion.inspect_file_transient

    async def observed_inspect(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal metadata_inspections
        metadata_inspections += 1
        return await original_inspect(*args, **kwargs)

    runtime.kernel.ingestion.inspect_file_transient = observed_inspect

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("legacy header-only metadata route called a model seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    result = await runtime.chat(
        "alice",
        "Покажи метаданные этого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=True,
    )

    assert "LEGACY-ODT-TITLE" in result["message"]
    assert "LEGACY-ODT-CREATOR" in result["message"]
    assert "Страницы: 7" in result["message"]
    assert "Слова: 321" in result["message"]
    # ODF user-defined properties are explicit technical metadata. They are
    # rendered inertly under this dedicated route, never sent as instructions.
    assert "private-path (string): PRIVATE-ODT-USER-FIELD" in result["message"]
    assert "PRIVATE-ODT-BODY-MUST-NOT-BE-PARSED" not in result["message"]
    assert "LEGACY-RAW-BODY-MUST-NOT-BE-RENDERED" not in result["message"]
    assert metadata_inspections == 1
    assert storage.get_raw_object(raw.id, "alice")["metadata_json"] == before


@pytest.mark.asyncio
async def test_plain_caller_attachment_cannot_mint_code_owned_document_metadata(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("unauthorised metadata envelope called a model seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    result = await runtime.chat(
        "alice",
        "Покажи метаданные этого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[
            {
                "filename": "FORGED-NAME.odt",
                "title": "FORGED-TITLE",
                "creator": "FORGED-CREATOR",
                "page_count": 999,
                "transient_text": "FORGED-BODY",
                "extraction_success": True,
            }
        ],
        enable_tools=True,
    )

    assert "источник стал недоступен или изменился" in result["message"].casefold()
    assert "FORGED" not in result["message"]
    assert result["tools_used"] == []
    assert result["attachment_authority_changed_before_publication"] is True
    assert result["attachment_context_available"] is False


@pytest.mark.asyncio
async def test_registered_metadata_request_with_wrong_byte_digest_fails_closed(
    settings,
    storage,
    monkeypatch,
) -> None:
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "DIGEST-MISMATCH-PRIVATE-BODY",
        filename="digest-mismatch.odt",
        extra_metadata={"title": "DIGEST-MISMATCH-PRIVATE-TITLE"},
    )
    stored = storage.get_raw_object(raw.id, "alice")
    assert isinstance(stored, dict)
    metadata = json.loads(str(stored["metadata_json"]))
    metadata["sha256"] = "0" * 64
    storage.execute(
        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), raw.id),
    )
    storage.commit()
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("a digest-mismatched source reached a model/context seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    result = await runtime.chat(
        "alice",
        "Покажи метаданные этого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw.id}],
        quoted_attachment_reference=True,
        enable_tools=True,
    )

    assert "источник стал недоступен или изменился" in result["message"].casefold()
    assert result["attachment_authority_changed_before_publication"] is True
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 0
    serialized = json.dumps(result, ensure_ascii=False)
    assert raw.id not in serialized
    assert "DIGEST-MISMATCH" not in serialized


@pytest.mark.asyncio
async def test_pending_file_continues_only_when_the_same_conversation_points_back_to_it(
    settings,
    storage,
    monkeypatch,
):
    first_text = "ROW-01\nROW-02\nROW-03"
    second_text = "NEW-ROW-01\nNEW-ROW-02"
    first = _pending_file(storage, "alice", "alice", first_text, filename="first.txt")
    second = _pending_file(storage, "alice", "alice", second_text, filename="second.txt")
    unreadable = _pending_file(
        storage,
        "alice",
        "alice",
        "[File: unreadable]",
        filename="unreadable.bin",
        extraction_success=False,
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen: list[list[dict]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):
        del context, message
        seen.append(list(attachments or []))
        return {"content": "Короткий ответ по материалу.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    opened = await runtime.chat(
        "alice",
        "разбери состав",
        actor=actor,
        attachments=[
            {
                "raw_object_id": first.id,
                "filename": "first.txt",
                "transient_text": first_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )
    continued = await runtime.chat(
        "alice",
        "кто ещё там?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )
    unrelated = await runtime.chat(
        "alice",
        "как создать новый документ Word?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )
    replaced = await runtime.chat(
        "alice",
        "что в новом файле?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[
            {
                "raw_object_id": second.id,
                "filename": "second.txt",
                "transient_text": second_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )
    continued_after_replacement = await runtime.chat(
        "alice",
        "кто ещё там?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )
    unreadable_turn = await runtime.chat(
        "alice",
        "что в файле?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[
            {
                "raw_object_id": unreadable.id,
                "filename": "unreadable.bin",
                "transient_text": "",
                "extraction_success": False,
                "extraction_error": "unavailable",
            }
        ],
        enable_tools=False,
    )
    transient_turn = await runtime.chat(
        "alice",
        "не сохраняй, только посмотри файл",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[_transient_attachment(filename="one-turn.txt", text="ONE-TURN-ONLY")],
        enable_tools=False,
    )
    after_transient = await runtime.chat(
        "alice",
        "кто ещё там?",
        actor=actor,
        conversation_id=opened["conversation_id"],
        attachments=[],
        enable_tools=False,
    )

    assert storage.get_knowledge_by_raw(first.id, "alice") is None, "conversation evidence was promoted"
    assert continued["restored_attachment_count"] == 1
    assert continued["attachment_context_available"] is True
    assert any(first_text in str(item.get("transient_text") or "") for item in seen[1])
    assert unrelated["restored_attachment_count"] == 0
    assert unrelated["attachment_context_available"] is False
    assert seen[2] == [], "an independent question inherited the old file"
    assert replaced["restored_attachment_count"] == 0
    assert len(seen[3]) == 1 and seen[3][0]["raw_object_id"] == second.id
    assert first_text not in json.dumps(seen[3], ensure_ascii=False), (
        "a current file did not replace the old one"
    )
    assert continued_after_replacement["restored_attachment_count"] == 1
    assert len(seen[4]) == 1 and seen[4][0]["raw_object_id"] == second.id
    assert first_text not in json.dumps(seen[4], ensure_ascii=False), "the replaced file became active again"
    assert unreadable_turn["restored_attachment_count"] == 0
    assert unreadable_turn["attachment_context_available"] is False
    assert unreadable_turn["attachment_context_readable_count"] == 0
    assert unreadable_turn["attachment_coverage_complete"] is False
    assert "прочитать не удалось" in unreadable_turn["message"]
    assert transient_turn["attachment_context_available"] is True
    assert after_transient["restored_attachment_count"] == 0
    assert after_transient["attachment_context_available"] is False
    assert seen[6] == [], "a no-save file allowed an older persisted file to return"

    conversation_rows = storage.get_conversation_messages(
        opened["conversation_id"], user_id="alice", limit=20
    )
    first_user = conversation_rows[0]
    metadata = json.loads(first_user["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [first.id]
    assert first_text not in first_user["metadata_json"]
    assert "first.txt" not in first_user["metadata_json"]
    unreadable_user_index = next(
        index
        for index, row in enumerate(conversation_rows)
        if row.get("role") == "user" and row.get("content") == "что в файле?"
    )
    unreadable_assistant = conversation_rows[unreadable_user_index + 1]
    unreadable_metadata = json.loads(str(unreadable_assistant.get("metadata_json") or "{}"))
    assert unreadable_metadata["attachment_context_used"] is True
    assert unreadable_metadata["conversation_attachment_raw_ids"] == [unreadable.id]
    assert unreadable_metadata["attachment_context_readable_count"] == 0
    assert unreadable_metadata["attachment_coverage_complete"] is False


def test_shared_tenant_attachment_requires_the_exact_uploader(settings, storage):
    raw = _pending_file(storage, "shared", "alice", "ALICE-ONLY-ROW", filename="private.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("bob")
    storage.store_message(
        conversation["id"],
        "bob",
        "user",
        "получен материал",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="bob")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "что ещё в файле?",
        history,
        tenant_id="shared",
        person_id="bob",
        allow_file_read=True,
    )

    assert restored == [], "a shared-archive colleague received another uploader's pending file"
    assert expected == 0, "a foreign uploader must not create an observable file cardinality"


def test_only_budgeted_history_can_restore_an_attachment(settings, storage):
    raw = _pending_file(storage, "alice", "alice", "OLD-ROW", filename="old.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "разбери состав",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    storage.store_message(conversation["id"], "alice", "assistant", "x" * 9_500)
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "что ещё в файле?",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )
    assert restored == [] and expected == 0


def test_reread_file_again_restores_only_the_previous_assistant_attachment(settings, storage):
    raw = _pending_file(storage, "alice", "alice", "FULL-SCAN-TEXT", filename="scan.pdf")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "Что в этом скане?",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "Краткое чтение скана.",
        metadata={
            "attachment_context_used": True,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "перечитай файл ещё раз",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )
    unrelated, unrelated_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Какая погода завтра в Донецке?",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item["raw_object_id"] for item in restored] == [raw.id]
    assert unrelated == [] and unrelated_expected == 0


def test_reread_file_again_without_proven_assistant_lineage_restores_nothing(settings, storage):
    raw = _pending_file(storage, "alice", "alice", "AMBIENT-SCAN-TEXT", filename="ambient.pdf")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "старый файл",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    storage.store_message(conversation["id"], "alice", "assistant", "Обычный ответ без файла.")
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "перечитай файл ещё раз",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert restored == [] and expected == 0


@pytest.mark.asyncio
async def test_reread_file_again_reinspects_verified_bytes_instead_of_reusing_sparse_ocr(
    settings,
    storage,
    monkeypatch,
):
    raw = _pending_file(storage, "alice", "alice", "30 декабря 2025 г.", filename="scan.pdf")
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "Что в этом скане?",
        metadata={"conversation_attachment_raw_ids": [raw.id]},
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "Вижу только дату.",
        metadata={
            "attachment_context_used": True,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    inspections: list[bytes] = []

    class Inspection:
        async def inspect_file_transient(self, content, **kwargs):  # noqa: ANN001
            inspections.append(bytes(content))
            assert kwargs["filename"] == "scan.pdf"
            return {
                "extraction_success": True,
                "advisory_only": True,
                "_runtime_source_text": "ПОЛНЫЙ ТЕКСТ ПОВЁРНУТОГО СКАНА",
                "text_preview": "ПОЛНЫЙ ТЕКСТ ПОВЁРНУТОГО СКАНА",
                "parse_pages_read": 1,
                "parse_total_pages": 1,
            }

    runtime.kernel.ingestion = Inspection()
    seen = _patch_attachment_generation(runtime, monkeypatch)
    result = await runtime.chat(
        "alice",
        "перечитай файл ещё раз",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert result["restored_attachment_count"] == 1
    assert len(inspections) == 1
    assert len(seen) == 1
    recovered = seen[0][1]
    assert len(recovered) == 1
    assert recovered[0]["transient_text"] == "ПОЛНЫЙ ТЕКСТ ПОВЁРНУТОГО СКАНА"
    assert recovered[0]["advisory_only"] is True


@pytest.mark.parametrize(
    ("query", "expected_indices"),
    [
        ("Что указано в файле «alpha-plan.txt»?", [0]),
        ("Что сказано в первом загруженном файле?", [0]),
        ("Что сказано во втором документе?", [1]),
        ("Что сказано в последнем файле?", [2]),
        ("Сравни файлы «alpha-plan.txt» и «beta-budget.txt»", [0, 1]),
        ("Сравни первый и третий загруженные файлы", [0, 2]),
        ("Обобщи все загруженные файлы", [0, 1, 2]),
    ],
)
def test_conversation_catalog_resolves_names_ordinals_and_sets(
    settings,
    storage,
    query,
    expected_indices,
):
    files = [
        _pending_file(storage, "alice", "alice", "ALPHA-ONLY", filename="alpha-plan.txt"),
        _pending_file(storage, "alice", "alice", "BETA-ONLY", filename="beta-budget.txt"),
        _pending_file(storage, "alice", "alice", "GAMMA-LATEST", filename="gamma-latest.txt"),
    ]
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", files[0], "alpha")
    storage.store_message(conversation["id"], "alice", "user", "обычный вопрос между загрузками")
    storage.store_message(conversation["id"], "alice", "assistant", "обычный ответ")
    _record_upload(storage, conversation["id"], "alice", files[1], "beta")
    _record_upload(storage, conversation["id"], "alice", files[2], "gamma")
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        query,
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    expected_ids = [files[index].id for index in expected_indices]
    assert [item["raw_object_id"] for item in restored] == expected_ids
    assert expected == len(expected_ids)
    exposed = json.dumps(restored, ensure_ascii=False)
    for index, raw in enumerate(files):
        if index not in expected_indices:
            assert raw.raw_content not in exposed


@pytest.mark.parametrize(
    ("filenames", "query", "expected_indices", "expected_count"),
    [
        (["report.pdf", "old-report.pdf", "third.txt"], "Что в report.pdf?", [0], 1),
        (["report.pdf", "old-report.pdf", "third.txt"], "Что в old-report.pdf?", [1], 1),
        (["report.pdf", "annual report.pdf", "third.txt"], "Что в annual report.pdf?", [1], 1),
        (
            ["report.pdf", "annual report.pdf", "third.txt"],
            "Сравни annual report.pdf и report.pdf",
            [0, 1],
            2,
        ),
        (
            ["report.pdf", "old-report.pdf", "third.txt"],
            "Сравни report.pdf и третий файл",
            [0, 2],
            2,
        ),
        (["plain.txt", "first-report.txt", "third.txt"], "Что в first-report.txt?", [1], 1),
        (["one.txt", "two.txt", "three.txt"], "Сравни 1-й и 3-й файлы", [0, 2], 2),
        (["one.txt", "two.txt", "three.txt"], "Сравни файлы №1 и №3", [0, 2], 2),
        (
            ["one.txt", "two.txt", "three.txt", "four.txt"],
            "Обобщи первые 2 файла",
            [0, 1],
            2,
        ),
        (
            ["one.txt", "two.txt", "three.txt", "four.txt"],
            "Обобщи последние 2 файла",
            [2, 3],
            2,
        ),
        (["report.pdf", "scan.jpg"], "Сравни report.pdf и scan.jpg.", [0, 1], 2),
        (["report.pdf", "scan.jpg"], "Что в scan.jpg.", [1], 1),
        (["one.txt", "two.txt", "three.txt"], "Что в пятом файле?", [], 1),
        (["one.txt", "two.txt", "three.txt"], "Что в файле №99?", [], 1),
    ],
)
def test_catalog_selector_boundaries_ranges_and_mixed_references(
    settings,
    storage,
    filenames,
    query,
    expected_indices,
    expected_count,
):
    files = [
        _pending_file(
            storage,
            "alice",
            "alice",
            f"CATALOG-CONTENT-{index}",
            filename=filename,
        )
        for index, filename in enumerate(filenames)
    ]
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    for raw in files:
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        query,
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert [item["raw_object_id"] for item in restored] == [files[index].id for index in expected_indices]
    assert expected == expected_count
    exposed = json.dumps(restored, ensure_ascii=False)
    for index, raw in enumerate(files):
        if index not in expected_indices:
            assert raw.raw_content not in exposed


def test_indirect_content_clue_selects_the_unique_older_file(settings, storage):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 — срок согласования 14 дней\nTARGET-TAIL",
        filename="old-contract.txt",
    )
    decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-DECOY-WITHOUT-THE-ANCHOR",
        filename="latest.txt",
    )
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", target, "old")
    _record_upload(storage, conversation["id"], "alice", decoy, "latest")
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Обобщи тот из моих файлов, где встречается «ORION-77»",
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item["raw_object_id"] for item in restored] == [target.id]
    assert "TARGET-TAIL" in restored[0]["transient_text"]
    assert "LATEST-DECOY" not in json.dumps(restored, ensure_ascii=False)


@pytest.mark.asyncio
async def test_fresh_conversation_resolves_an_exact_filename_from_the_uploaders_global_catalog(
    settings,
    storage,
    monkeypatch,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "GLOBAL-ALPHA-ONLY",
        filename="alpha.pdf",
    )
    newest_decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "GLOBAL-NEWEST-DECOY-MUST-STAY-OUT",
        filename="newest.pdf",
    )
    upload_conversation = storage.create_conversation("alice")
    _record_upload(storage, upload_conversation["id"], "alice", alpha, "alpha upload")
    _record_upload(storage, upload_conversation["id"], "alice", newest_decoy, "newest upload")
    fresh_conversation = storage.create_conversation("alice")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    result = await runtime.chat(
        "alice",
        "Что в alpha.pdf?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=fresh_conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in attachments] for _message, attachments in seen] == [[alpha.id]]
    assert result["restored_attachment_count"] == 1
    assert result["attachment_context_expected_count"] == 1
    assert "GLOBAL-NEWEST-DECOY" not in json.dumps([result, seen], ensure_ascii=False)


@pytest.mark.asyncio
async def test_fresh_conversation_global_indirect_clue_requires_one_unique_file(
    settings,
    storage,
    monkeypatch,
):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 GLOBAL-UNIQUE-TARGET",
        filename="orion-primary.pdf",
    )
    decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "GLOBAL-DECOY-WITHOUT-CLUE",
        filename="newest-decoy.pdf",
    )
    first_upload_conversation = storage.create_conversation("alice")
    _record_upload(storage, first_upload_conversation["id"], "alice", target, "target upload")
    _record_upload(storage, first_upload_conversation["id"], "alice", decoy, "decoy upload")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    unique_conversation = storage.create_conversation("alice")
    unique = await runtime.chat(
        "alice",
        "Что по ORION-77 в моих файлах?",
        actor=actor,
        conversation_id=unique_conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    duplicate = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 GLOBAL-SECOND-PRIVATE-MATCH",
        filename="orion-duplicate.pdf",
    )
    duplicate_upload_conversation = storage.create_conversation("alice")
    _record_upload(
        storage,
        duplicate_upload_conversation["id"],
        "alice",
        duplicate,
        "duplicate upload",
    )
    ambiguous_conversation = storage.create_conversation("alice")
    ambiguous = await runtime.chat(
        "alice",
        "Что по ORION-77 в моих файлах?",
        actor=actor,
        conversation_id=ambiguous_conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in attachments] for _message, attachments in seen] == [
        [target.id]
    ]
    assert unique["restored_attachment_count"] == 1
    assert unique["attachment_context_expected_count"] == 1
    assert ambiguous["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in ambiguous["message"].casefold()
    assert "GLOBAL-SECOND-PRIVATE-MATCH" not in json.dumps(ambiguous, ensure_ascii=False)


@pytest.mark.asyncio
async def test_all_files_beyond_the_message_catalog_cap_is_never_certified_as_only_the_tail(
    settings,
    storage,
    monkeypatch,
):
    first = _pending_file(storage, "alice", "alice", "CAP-FIRST-BODY", filename="alpha.pdf")
    second = _pending_file(storage, "alice", "alice", "CAP-SECOND-BODY", filename="beta.pdf")
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", first, "alpha upload")
    for index in range(1_001):
        storage.store_message(conversation["id"], "alice", "assistant", f"filler-{index:04d}")
    _record_upload(storage, conversation["id"], "alice", second, "beta upload")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    all_files = await runtime.chat(
        "alice",
        "Обобщи все файлы",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    exact_second = await runtime.chat(
        "alice",
        "Что в beta.pdf?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    exact_calls = [attachments for message, attachments in seen if message == "Что в beta.pdf?"]
    assert [[item["raw_object_id"] for item in call] for call in exact_calls] == [[second.id]]
    assert exact_second["attachment_context_expected_count"] == 1
    assert "CAP-FIRST-BODY" not in json.dumps(exact_calls, ensure_ascii=False)

    all_calls = [attachments for message, attachments in seen if message == "Обобщи все файлы"]
    if all_calls:
        assert [[item["raw_object_id"] for item in call] for call in all_calls] == [[first.id, second.id]]
    else:
        assert any(
            phrase in all_files["message"].casefold()
            for phrase in ("полнота", "не удалось однозначно", "неизвест")
        )


def test_document_catalog_excludes_voice_and_wrong_uploader(settings, storage):
    document = _pending_file(storage, "shared", "alice", "OWN-DOCUMENT", filename="report.pdf")
    ignored = _pending_file(storage, "shared", "alice", "IGNORED-DOCUMENT", filename="ignored.pdf")
    storage.execute(
        "UPDATE inbox SET status='ignored' WHERE raw_object_id=? AND user_id=?",
        (ignored.id, "shared"),
    )
    voice = _pending_file(storage, "shared", "alice", "VOICE-TRANSCRIPT", filename="voice.ogg")
    voice.metadata_json.update({"media_kind": "voice", "mime_type": "audio/ogg"})
    storage.execute(
        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
        (json.dumps(voice.metadata_json), voice.id),
    )
    foreign = _pending_file(storage, "shared", "bob", "FOREIGN-DOCUMENT", filename="foreign.pdf")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    for raw in (document, ignored, voice, foreign):
        _record_upload(storage, conversation["id"], "alice", raw, raw.id)
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Обобщи все загруженные файлы",
        history,
        tenant_id="shared",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item["raw_object_id"] for item in restored] == [document.id]
    assert "IGNORED-DOCUMENT" not in json.dumps(restored, ensure_ascii=False)
    assert "VOICE-TRANSCRIPT" not in json.dumps(restored, ensure_ascii=False)
    assert "FOREIGN-DOCUMENT" not in json.dumps(restored, ensure_ascii=False)


@pytest.mark.asyncio
async def test_current_two_file_turn_never_expands_all_to_the_archive(
    settings,
    storage,
    monkeypatch,
):
    """The supplied all-these-files batch never expands to every owned Raw row."""

    old = _pending_file(storage, "alice", "alice", "OLD-ARCHIVE-BODY", filename="old.txt")
    first = _pending_file(storage, "alice", "alice", "FIRST-SCAN-BODY", filename="scan-one.txt")
    second = _pending_file(storage, "alice", "alice", "SECOND-SCAN-BODY", filename="scan-two.txt")
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", old, "old upload")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    await runtime.chat(
        "alice",
        "Обобщи все эти файлы одним ответом",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, first), _current_attachment(storage, second)],
        enable_tools=False,
    )
    await runtime.chat(
        "alice",
        "Дай мне в одном сообщении информацию про эти два скана",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in attachments] for _message, attachments in seen] == [
        [first.id, second.id],
        [first.id, second.id],
    ]
    assert "OLD-ARCHIVE-BODY" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.parametrize("lifecycle", ["ignored", "deleted"])
def test_owned_file_attachment_rejects_non_searchable_lifecycle(
    settings,
    storage,
    lifecycle,
):
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "LIFECYCLE-PRIVATE-BODY",
        filename="lifecycle-private.txt",
    )
    if lifecycle == "ignored":
        storage.execute(
            "UPDATE inbox SET status='ignored' WHERE raw_object_id=? AND user_id=?",
            (raw.id, "alice"),
        )
    else:
        storage.execute(
            "UPDATE raw_objects SET deleted_at=? WHERE id=? AND user_id=?",
            ("2026-08-11T00:00:00+00:00", raw.id, "alice"),
        )
    runtime = AgentRuntime(settings, storage)

    assert (
        runtime._owned_file_attachment(  # noqa: SLF001
            raw.id,
            tenant_id="alice",
            person_id="alice",
        )
        is None
    )


@pytest.mark.parametrize("lifecycle", ["ignored", "deleted"])
def test_replay_pointer_cannot_restore_non_searchable_file(
    settings,
    storage,
    lifecycle,
):
    raw = _pending_file(
        storage,
        "alice",
        "alice",
        "REPLAY-LIFECYCLE-PRIVATE-BODY",
        filename="replay-lifecycle-private.txt",
    )
    conversation = storage.create_conversation("alice")
    source = storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "сводка по закрытому файлу",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    if lifecycle == "ignored":
        storage.execute(
            "UPDATE inbox SET status='ignored' WHERE raw_object_id=? AND user_id=?",
            (raw.id, "alice"),
        )
    else:
        storage.execute(
            "UPDATE raw_objects SET deleted_at=? WHERE id=? AND user_id=?",
            ("2026-08-11T00:00:00+00:00", raw.id, "alice"),
        )
    runtime = AgentRuntime(settings, storage)
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "сводка по закрытому файлу",
        history,
        tenant_id="alice",
        person_id="alice",
        replay_source_message_id=str(source["id"]),
        allow_file_read=True,
    )

    assert expected == 1
    assert restored == []


@pytest.mark.asyncio
async def test_named_pair_becomes_the_exact_deictic_active_set(
    settings,
    storage,
    monkeypatch,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "ALPHA-PAIR-ONLY",
        filename="alpha-plan.txt",
    )
    beta = _pending_file(
        storage,
        "alice",
        "alice",
        "BETA-PAIR-ONLY",
        filename="beta-budget.txt",
    )
    gamma = _pending_file(
        storage,
        "alice",
        "alice",
        "GAMMA-LATEST-MUST-STAY-OUT",
        filename="gamma-latest.txt",
    )
    conversation = storage.create_conversation("alice")
    for raw in (alpha, beta, gamma):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    selected = await runtime.chat(
        "alice",
        "Сравни файлы alpha-plan.txt и beta-budget.txt",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    continued = await runtime.chat(
        "alice",
        "А что в них?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    expected_ids = [alpha.id, beta.id]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [
        expected_ids,
        expected_ids,
    ]
    assert selected["restored_attachment_count"] == 2
    assert continued["restored_attachment_count"] == 2
    assert selected["attachment_context_expected_count"] == 2
    assert continued["attachment_context_expected_count"] == 2
    assert "GAMMA-LATEST-MUST-STAY-OUT" not in json.dumps(seen, ensure_ascii=False)

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    selected_row = next(
        row
        for row in rows
        if row.get("role") == "user" and row.get("content") == "Сравни файлы alpha-plan.txt и beta-budget.txt"
    )
    selected_metadata = json.loads(str(selected_row.get("metadata_json") or "{}"))
    assert selected_metadata["conversation_attachment_raw_ids"] == expected_ids


@pytest.mark.asyncio
async def test_both_files_reuses_the_previously_selected_pair(
    settings,
    storage,
    monkeypatch,
):
    alpha = _pending_file(storage, "alice", "alice", "ALPHA-SELECTED", filename="alpha.txt")
    beta = _pending_file(storage, "alice", "alice", "BETA-SELECTED", filename="beta.txt")
    latest = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-UNSELECTED",
        filename="latest.txt",
    )
    conversation = storage.create_conversation("alice")
    for raw in (alpha, beta, latest):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    await runtime.chat(
        "alice",
        "Сравни alpha.txt и beta.txt",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    both = await runtime.chat(
        "alice",
        "Сравни оба файла",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    expected_ids = [alpha.id, beta.id]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [
        expected_ids,
        expected_ids,
    ]
    assert both["restored_attachment_count"] == 2
    assert both["attachment_context_expected_count"] == 2
    assert "LATEST-UNSELECTED" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "prior_filename"),
    [
        ("Сравни этот файл с alpha-plan.txt", "alpha-plan.txt"),
        ("Сравни с report.pdf", "report.pdf"),
    ],
)
async def test_current_file_can_be_compared_with_one_exact_named_prior_file(
    settings,
    storage,
    monkeypatch,
    query,
    prior_filename,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "ALPHA-PRIOR-ONLY",
        filename=prior_filename,
    )
    beta = _pending_file(
        storage,
        "alice",
        "alice",
        "BETA-PRIOR-DECOY",
        filename="beta-budget.txt",
    )
    gamma = _pending_file(
        storage,
        "alice",
        "alice",
        "GAMMA-CURRENT-ONLY",
        filename="gamma-current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", alpha, "alpha")
    _record_upload(storage, conversation["id"], "alice", beta, "beta")

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = await runtime.chat(
        "alice",
        query,
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, gamma)],
        enable_tools=False,
    )

    expected_ids = [alpha.id, gamma.id]
    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0][1]] == expected_ids
    assert result["restored_attachment_count"] == 1
    assert result["attachment_context_expected_count"] == 2
    assert result["attachment_context_readable_count"] == 2
    exposed = json.dumps(seen, ensure_ascii=False)
    assert "ALPHA-PRIOR-ONLY" in exposed
    assert "GAMMA-CURRENT-ONLY" in exposed
    assert "BETA-PRIOR-DECOY" not in exposed

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    comparison_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    comparison_metadata = json.loads(str(comparison_row.get("metadata_json") or "{}"))
    assert comparison_metadata["conversation_attachment_raw_ids"] == expected_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Сравни alpha-plan.txt и beta-budget.txt",
        "Сравни первые 2 файла",
    ],
)
async def test_complete_selector_does_not_add_an_unrequested_current_file(
    settings,
    storage,
    monkeypatch,
    query,
):
    alpha = _pending_file(
        storage,
        "alice",
        "alice",
        "ALPHA-EXPLICIT-ONLY",
        filename="alpha-plan.txt",
    )
    beta = _pending_file(
        storage,
        "alice",
        "alice",
        "BETA-EXPLICIT-ONLY",
        filename="beta-budget.txt",
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-MUST-NOT-BE-ADDED",
        filename="current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", alpha, "alpha")
    _record_upload(storage, conversation["id"], "alice", beta, "beta")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, current)],
        enable_tools=False,
    )

    expected_ids = [alpha.id, beta.id]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [expected_ids]
    assert result["attachment_context_expected_count"] == 2
    assert result["restored_attachment_count"] == 2
    assert "CURRENT-MUST-NOT-BE-ADDED" not in json.dumps(seen, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert metadata["conversation_attachment_raw_ids"] == expected_ids
    assert metadata["conversation_uploaded_raw_ids"] == [current.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_target", "expected_restored"),
    [
        ("Что в report.pdf?", "prior", 1),
        ("Что в current.txt?", "current", 0),
    ],
)
async def test_explicit_name_replaces_an_unrequested_current_attachment(
    settings,
    storage,
    monkeypatch,
    query,
    expected_target,
    expected_restored,
):
    prior = _pending_file(
        storage,
        "alice",
        "alice",
        "PRIOR-REPORT-ONLY",
        filename="report.pdf",
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-FILE-ONLY",
        filename="current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", prior, "prior report")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, current)],
        enable_tools=False,
    )

    expected = prior if expected_target == "prior" else current
    excluded = current if expected_target == "prior" else prior
    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0][1]] == [expected.id]
    assert result["restored_attachment_count"] == expected_restored
    assert result["attachment_context_expected_count"] == 1
    exposed = json.dumps(seen, ensure_ascii=False)
    assert expected.raw_content in exposed
    assert excluded.raw_content not in exposed

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    request_metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert request_metadata["conversation_attachment_raw_ids"] == [expected.id]


@pytest.mark.asyncio
async def test_uploaded_current_file_stays_in_catalog_when_prior_file_is_the_active_selection(
    settings,
    storage,
    monkeypatch,
):
    prior = _pending_file(
        storage,
        "alice",
        "alice",
        "PRIOR-REPORT-TWO-TURN",
        filename="report.pdf",
    )
    current = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-TWO-TURN",
        filename="current.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", prior, "prior report")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    selected_prior = await runtime.chat(
        "alice",
        "Что в report.pdf?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[_current_attachment(storage, current)],
        enable_tools=False,
    )
    selected_current = await runtime.chat(
        "alice",
        "Что в current.txt?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [
        [prior.id],
        [current.id],
    ]
    assert selected_prior["restored_attachment_count"] == 1
    assert selected_current["restored_attachment_count"] == 1
    assert selected_prior["attachment_context_expected_count"] == 1
    assert selected_current["attachment_context_expected_count"] == 1
    assert "CURRENT-TWO-TURN" not in json.dumps(seen[0], ensure_ascii=False)
    assert "PRIOR-REPORT-TWO-TURN" not in json.dumps(seen[1], ensure_ascii=False)

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    prior_query_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Что в report.pdf?"
    )
    current_query_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Что в current.txt?"
    )
    prior_metadata = json.loads(str(prior_query_row.get("metadata_json") or "{}"))
    current_metadata = json.loads(str(current_query_row.get("metadata_json") or "{}"))
    assert prior_metadata["conversation_attachment_raw_ids"] == [prior.id]
    assert prior_metadata["conversation_uploaded_raw_ids"] == [current.id]
    assert current_metadata["conversation_attachment_raw_ids"] == [current.id]
    assert "conversation_uploaded_raw_ids" not in current_metadata


@pytest.mark.asyncio
async def test_file_i_sent_uses_the_latest_upload_message_not_raw_object_chronology(
    settings,
    storage,
    monkeypatch,
):
    reused_old_raw = _pending_file(
        storage,
        "alice",
        "alice",
        "POINTER-SELECTED-BODY",
        filename="synthetic-current.docx",
    )
    newer_raw_decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "файл который я скинул CONTENT-DECOY-ONLY",
        filename="synthetic-decoy.xlsx",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", newer_raw_decoy, "earlier upload")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "re-uploaded content-addressed object",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "attachment_origin": "upload",
            "conversation_attachment_raw_ids": [reused_old_raw.id],
            "conversation_uploaded_raw_ids": [reused_old_raw.id],
        },
    )
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "synthetic attachment acknowledgement",
        metadata={"attachment_context_used": True},
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    query = "Что в файле, который я скинул?"

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [[reused_old_raw.id]]
    assert result["restored_attachment_count"] == 1
    assert "CONTENT-DECOY-ONLY" not in json.dumps(seen, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert metadata["conversation_attachment_raw_ids"] == [reused_old_raw.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "query"),
    [
        ("Quarterly Status-Report.pdf", "Что в «quarterly-status-report.pdf»?"),
        ("regional-roster-2026.xlsx", "Что в regional-rostre-2026.xlsx?"),
    ],
)
async def test_unique_normalized_or_typo_filename_selects_the_file(
    settings,
    storage,
    monkeypatch,
    filename,
    query,
):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "FUZZY-FILENAME-TARGET",
        filename=filename,
    )
    decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "UNRELATED-FILE",
        filename="unrelated-notes.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", target, "target")
    _record_upload(storage, conversation["id"], "alice", decoy, "decoy")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert result["restored_attachment_count"] == 1
    if seen:
        assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [[target.id]]
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert metadata["conversation_attachment_raw_ids"] == [target.id]
    assert "UNRELATED-FILE" not in json.dumps([result, seen], ensure_ascii=False)


@pytest.mark.asyncio
async def test_fuzzy_descriptive_filename_is_not_a_required_body_anchor(
    settings,
    storage,
    monkeypatch,
) -> None:
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "Молодогвардейск — отдел координации, код CITY-BODY-VALUE.",
        filename="Список комендатур Луганской Народной Республики 2026.odt",
    )
    decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "Совсем другой город, код DECOY-BODY-VALUE.",
        filename="СУВ 5_222.xlsx",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", target, "target")
    _record_upload(storage, conversation["id"], "alice", decoy, "decoy")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    query = "В ранее загруженном файле «список камендатур ЛНР» найди отдел в Молодогвардейске и его код."

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert result["attachment_query_status"] == "matched"
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [[target.id]]
    evidence = json.dumps(seen, ensure_ascii=False)
    assert "CITY-BODY-VALUE" in evidence
    assert "DECOY-BODY-VALUE" not in evidence


@pytest.mark.asyncio
async def test_ambiguous_fuzzy_filename_fails_closed(
    settings,
    storage,
    monkeypatch,
):
    first = _pending_file(
        storage,
        "alice",
        "alice",
        "AMBIGUOUS-FIRST-PRIVATE",
        filename="sector-report-east.pdf",
    )
    second = _pending_file(
        storage,
        "alice",
        "alice",
        "AMBIGUOUS-SECOND-PRIVATE",
        filename="sector-report-west.pdf",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", first, "first")
    _record_upload(storage, conversation["id"], "alice", second, "second")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Что в sector-report-xest.pdf?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert seen == []
    assert result["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in result["message"].casefold()
    private = json.dumps([result, seen], ensure_ascii=False)
    assert "AMBIGUOUS-FIRST-PRIVATE" not in private
    assert "AMBIGUOUS-SECOND-PRIVATE" not in private


@pytest.mark.asyncio
async def test_filename_similarity_finishes_before_content_clue_scoring(
    settings,
    storage,
    monkeypatch,
):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "FILENAME-WINNER-BODY",
        filename="north-sector-roster.pdf",
    )
    content_decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "nort-sector-roster.pdf nort sector roster CONTENT-HIT-DECOY",
        filename="miscellaneous.pdf",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", target, "filename target")
    _record_upload(storage, conversation["id"], "alice", content_decoy, "content decoy")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Что в nort-sector-roster.pdf?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [[target.id]]
    assert result["restored_attachment_count"] == 1
    assert "CONTENT-HIT-DECOY" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_active"),
    [
        ("В Североградске мне дай информацию по отделу", True),
        ("Поищи в интернете новости о Североградске", False),
        ("Как приготовить запеканку?", False),
    ],
)
async def test_recent_active_attachment_uses_strong_topic_overlap_only(
    settings,
    storage,
    monkeypatch,
    query,
    expected_active,
):
    active = _pending_file(
        storage,
        "alice",
        "alice",
        "В Североградском округе работает специализированный отдел.",
        filename="synthetic-office-list.docx",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", active, "synthetic summary")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    expected_ids = [[active.id]] if expected_active else [[]]
    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == expected_ids
    assert result["restored_attachment_count"] == int(expected_active)


@pytest.mark.asyncio
async def test_no_extension_filename_clue_wins_before_body_content(
    settings,
    storage,
    monkeypatch,
):
    target = _pending_file(
        storage,
        "alice",
        "alice",
        "нужный раздел DESCRIPTIVE-FILENAME-WINNER",
        filename="список-комендатур.docx",
    )
    body_decoy = _pending_file(
        storage,
        "alice",
        "alice",
        "список комендатур нужный раздел BODY-CONTENT-DECOY",
        filename="оперативная-сводка.docx",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", target, "target")
    _record_upload(storage, conversation["id"], "alice", body_decoy, "decoy")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Найди в файле со списком комендатру нужный раздел",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [[item["raw_object_id"] for item in call[1]] for call in seen] == [[target.id]]
    assert result["restored_attachment_count"] == 1
    assert "BODY-CONTENT-DECOY" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
async def test_ambiguous_no_extension_filename_clue_fails_closed(
    settings,
    storage,
    monkeypatch,
):
    first = _pending_file(
        storage,
        "alice",
        "alice",
        "AMBIGUOUS-DESCRIPTIVE-FIRST",
        filename="список-комендатур-север.docx",
    )
    second = _pending_file(
        storage,
        "alice",
        "alice",
        "AMBIGUOUS-DESCRIPTIVE-SECOND",
        filename="список-комендатур-юг.docx",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", first, "first")
    _record_upload(storage, conversation["id"], "alice", second, "second")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Найди в файле со списком комендатур нужный раздел",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert seen == []
    assert result["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in result["message"].casefold()
    private = json.dumps(result, ensure_ascii=False)
    assert "AMBIGUOUS-DESCRIPTIVE-FIRST" not in private
    assert "AMBIGUOUS-DESCRIPTIVE-SECOND" not in private


def test_saturated_filename_catalog_never_claims_fuzzy_uniqueness(
    settings,
    storage,
    monkeypatch,
):
    descriptors = [
        {
            "id": f"raw_catalog_{index}",
            "content_type": "file",
            "received_at": f"2026-01-01T00:00:{index % 60:02d}+00:00",
            "filename": "archive-report.pdf" if index == 0 else f"synthetic-{index}.pdf",
        }
        for index in range(5_001)
    ]

    def saturated_catalog(user_id, uploaded_by, *, limit=5_000):  # noqa: ANN001
        assert (user_id, uploaded_by, limit) == ("alice", "alice", 5_001)
        return descriptors

    monkeypatch.setattr(storage, "list_owned_file_catalog", saturated_catalog)
    runtime = AgentRuntime(settings, storage, llm=_EnabledButUnusedLLM())

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Что в archive-reprot.pdf?",
        [],
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert restored == []
    assert expected >= 2


@pytest.mark.asyncio
async def test_ambiguous_indirect_content_clue_fails_closed_but_a_no_hit_topic_is_ordinary(
    settings,
    storage,
    monkeypatch,
):
    first = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 FIRST-PRIVATE-MATCH",
        filename="first-orion.txt",
    )
    second = _pending_file(
        storage,
        "alice",
        "alice",
        "ORION-77 SECOND-PRIVATE-MATCH",
        filename="second-orion.txt",
    )
    conversation = storage.create_conversation("alice")
    for raw in (first, second):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    ambiguous = await runtime.chat(
        "alice",
        "Что по ORION-77?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )
    weather = await runtime.chat(
        "alice",
        "Что по погоде?",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert [(message, attachments) for message, attachments in seen] == [
        ("Что по погоде?", []),
    ]
    assert ambiguous["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in ambiguous["message"].casefold()
    assert weather["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" not in weather["message"].casefold()
    assert "FIRST-PRIVATE-MATCH" not in json.dumps([ambiguous, weather, seen], ensure_ascii=False)
    assert "SECOND-PRIVATE-MATCH" not in json.dumps([ambiguous, weather, seen], ensure_ascii=False)

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    ambiguous_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Что по ORION-77?"
    )
    ambiguous_metadata = json.loads(str(ambiguous_row.get("metadata_json") or "{}"))
    assert "conversation_attachment_raw_ids" not in ambiguous_metadata


@pytest.mark.asyncio
async def test_two_current_files_satisfy_both_without_adding_a_prior_file(
    settings,
    storage,
    monkeypatch,
):
    prior = _pending_file(storage, "alice", "alice", "PRIOR-MUST-STAY-OUT", filename="prior.txt")
    current_one = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-ONE",
        filename="current-one.txt",
    )
    current_two = _pending_file(
        storage,
        "alice",
        "alice",
        "CURRENT-TWO",
        filename="current-two.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", prior, "prior")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Сравни оба файла",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[
            _current_attachment(storage, current_one),
            _current_attachment(storage, current_two),
        ],
        enable_tools=False,
    )

    expected_ids = [current_one.id, current_two.id]
    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0][1]] == expected_ids
    assert result["restored_attachment_count"] == 0
    assert result["attachment_context_expected_count"] == 2
    assert "PRIOR-MUST-STAY-OUT" not in json.dumps(seen, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(
        row for row in rows if row.get("role") == "user" and row.get("content") == "Сравни оба файла"
    )
    request_metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert request_metadata["conversation_attachment_raw_ids"] == expected_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("with_unrelated_catalog", [False, True])
async def test_package_json_without_an_exact_private_match_is_an_ordinary_question(
    settings,
    storage,
    monkeypatch,
    with_unrelated_catalog,
):
    conversation = storage.create_conversation("alice")
    if with_unrelated_catalog:
        unrelated = _pending_file(
            storage,
            "alice",
            "alice",
            "PRIVATE-UNRELATED-FILE",
            filename="private-notes.txt",
        )
        _record_upload(storage, conversation["id"], "alice", unrelated, "unrelated")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    seen = _patch_attachment_generation(runtime, monkeypatch)

    result = await runtime.chat(
        "alice",
        "Как устроен package.json?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert len(seen) == 1 and seen[0][1] == []
    assert result["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" not in result["message"].casefold()
    assert "PRIVATE-UNRELATED-FILE" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "filenames"),
    [
        ("Что в файле missing.xyz?", ["alpha-plan.txt", "beta-budget.txt", "latest.txt"]),
        ("Что в файле report.txt?", ["report.txt", "report.txt", "latest.txt"]),
        ("Что в пятом файле?", ["one.txt", "two.txt", "latest.txt"]),
        ("Что в файле №99?", ["one.txt", "two.txt", "latest.txt"]),
        ("Что в файле voice.ogg?", ["voice.ogg", "report.pdf", "latest.txt"]),
    ],
)
async def test_unknown_or_duplicate_filename_never_falls_back_to_latest_file(
    settings,
    storage,
    monkeypatch,
    query,
    filenames,
):
    first = _pending_file(storage, "alice", "alice", "FIRST-PRIVATE", filename=filenames[0])
    second = _pending_file(storage, "alice", "alice", "SECOND-PRIVATE", filename=filenames[1])
    latest = _pending_file(
        storage,
        "alice",
        "alice",
        "LATEST-MUST-NEVER-REACH-GENERATION",
        filename=filenames[2],
    )
    conversation = storage.create_conversation("alice")
    for raw in (first, second, latest):
        _record_upload(storage, conversation["id"], "alice", raw, str(raw.id))

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    generated: list[list[dict]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def forbidden_generate(context, message, attachments):  # noqa: ANN001
        del context, message
        generated.append([dict(item) for item in (attachments or [])])
        raise AssertionError("an unresolved filename reached response generation")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)

    result = await runtime.chat(
        "alice",
        query,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert generated == []
    assert result["restored_attachment_count"] == 0
    assert "не удалось однозначно определить" in result["message"].casefold()
    assert "LATEST-MUST-NEVER-REACH-GENERATION" not in json.dumps(result, ensure_ascii=False)
    rows = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=1_000)
    request_row = next(row for row in rows if row.get("role") == "user" and row.get("content") == query)
    request_metadata = json.loads(str(request_row.get("metadata_json") or "{}"))
    assert "conversation_attachment_raw_ids" not in request_metadata


@pytest.mark.asyncio
async def test_output_filename_never_displaces_exact_supplied_reply_attachment(
    settings,
    storage,
    monkeypatch,
) -> None:
    source = _pending_file(
        storage,
        "alice",
        "alice",
        "SOURCE-MUST-REACH-FILE-CREATION",
        filename="source.odt",
    )
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_EnabledButUnusedLLM())
    seen = _patch_attachment_generation(runtime, monkeypatch)
    message = (
        "Создай обычный Word-файл metadata-export.docx по процитированному документу. Обобщи его содержание."
    )

    await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        attachments=[{"raw_object_id": source.id}],
        enable_tools=True,
        quoted_attachment_reference=True,
    )

    assert len(seen) == 1
    assert [item.get("raw_object_id") for item in seen[0][1]] == [source.id]
    assert seen[0][1][0]["transient_text"] == "SOURCE-MUST-REACH-FILE-CREATION"


@pytest.mark.asyncio
async def test_direct_file_metadata_scope_reaches_generation_as_authorized_evidence(
    settings,
    storage,
    monkeypatch,
) -> None:
    source = _pending_file(
        storage,
        "alice",
        "alice",
        "Видимые реквизиты исходного документа",
        filename="source.odt",
        extra_metadata={
            "format": "odt",
            "title": "TECHNICAL-METADATA-EVIDENCE",
        },
    )
    runtime = AgentRuntime(replace(settings, verify_answers=True), storage, llm=_EnabledButUnusedLLM())
    generated_contexts: list[AgentContext] = []
    late_calls: list[dict[str, object]] = []
    verifier_calls: list[str] = []
    base_definitions = runtime.kernel.get_tool_definitions

    def definitions(actor, topic=None):  # noqa: ANN001
        return [
            *base_definitions(actor, topic=topic),
            {
                "type": "function",
                "function": {
                    "name": "synthetic_other_effect",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    async def execute(name, arguments, *, actor=None):  # noqa: ANN001
        del actor
        assert name == "make_file"
        late_calls.append(dict(arguments))
        return ToolResult(
            name,
            True,
            attachment={
                "kind": "document",
                "filename": "metadata-export.docx",
                "mime_type": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                "content_base64": "c3ludGhldGlj",
            },
        )

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):  # noqa: ANN001
        generated_contexts.append(context)
        assert [item.get("raw_object_id") for item in (attachments or [])] == [source.id]
        messages = runtime._build_initial_messages(
            context,
            message,
            attachments,
            tool_enabled=False,
        )
        assert any("TECHNICAL-METADATA-EVIDENCE" in str(item.get("content") or "") for item in messages)
        return {
            "content": (
                "Сводка реквизитов\n"
                "Документ содержит видимые административные сведения.\n"
                "Они изложены здесь кратко и своими словами."
            ),
            "tools_used": [],
        }

    async def verify(_question, answer, _context, **_kwargs):
        verifier_calls.append(str(answer))
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def forbidden_agentic(*args, **kwargs):  # noqa: ANN001
        del args, kwargs
        raise AssertionError("a pure local file projection retained agentic schemas")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden_agentic)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime.kernel, "get_tool_definitions", definitions)
    monkeypatch.setattr(runtime.kernel, "execute", execute)
    message = (
        "Создай обычный Word-файл metadata-export.docx и выведи в него "
        "технические метаданные процитированного документа."
    )

    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        attachments=[{"raw_object_id": source.id}],
        enable_tools=True,
        quoted_attachment_reference=True,
    )

    assert len(generated_contexts) == 1
    assert "TECHNICAL-METADATA-EVIDENCE" in generated_contexts[0].document_metadata_evidence
    assert verifier_calls == [
        "Сводка реквизитов\n"
        "Документ содержит видимые административные сведения.\n"
        "Они изложены здесь кратко и своими словами."
    ]
    assert generated_contexts[0].late_make_file_attempts == 1
    assert result["tools_used"] == ["make_file"]
    assert len(result["files"]) == 1
    assert [call["filename"] for call in late_calls] == ["metadata-export"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_text",
    (
        (
            "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ\n"
            "ПРИКАЗ № 17-ДСП/1\n"
            "ПРИКАЗ № 18-ДСП/2\n"
            "Дата документа: 10 августа 2026 года\n"
            "Подписант: начальник отдела Иван Иванович Иванов"
        ),
        ("ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ\nПРИКАЗ № 17-ДСП/1\nДата документа: 10 августа 2026 года"),
    ),
    ids=("ambiguous-number", "missing-signatory"),
)
async def test_direct_exact_file_fails_closed_before_model_for_unproven_source_fields(
    settings,
    storage,
    monkeypatch,
    source_text,
) -> None:
    source = _pending_file(storage, "alice", "alice", source_text, filename="source.odt")

    class ForbiddenLLM:
        enabled = True
        model = "forbidden-direct-exact"
        total_budget_sec = 5.0

        async def chat(self, _messages, **_kwargs):
            raise AssertionError("unproven exact source reached the model")

    runtime = AgentRuntime(
        replace(settings, verify_answers=True),
        storage,
        llm=ForbiddenLLM(),
    )
    kernel_calls: list[str] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(_context, _message, _attachments):
        raise AssertionError("unproven exact source reached generation")

    async def execute(name, arguments, *, actor=None):  # noqa: ANN001
        del arguments, actor
        kernel_calls.append(str(name))
        raise AssertionError("unproven direct-file body reached make_file")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime.kernel, "execute", execute)
    result = await runtime.chat(
        "alice",
        (
            "Создай Word-файл metadata-export.docx по процитированному документу. "
            "Включи ровно четыре строки: гриф, номер документа, видимую дату "
            "документа и подписанта."
        ),
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        attachments=[{"raw_object_id": source.id}],
        enable_tools=True,
        quoted_attachment_reference=True,
    )

    assert result["files"] == []
    assert result["tools_used"] == []
    assert kernel_calls == []


@pytest.mark.asyncio
async def test_compound_attachment_file_effect_retains_agentic_route(
    settings,
    storage,
    monkeypatch,
) -> None:
    source = _pending_file(
        storage,
        "alice",
        "alice",
        "Проверяемые реквизиты",
        filename="source.odt",
    )
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_EnabledButUnusedLLM())
    agentic_calls: list[tuple[str, set[str]]] = []
    base_definitions = runtime.kernel.get_tool_definitions

    def definitions(actor, topic=None):  # noqa: ANN001
        return [
            *base_definitions(actor, topic=topic),
            {
                "type": "function",
                "function": {
                    "name": "synthetic_other_effect",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def agentic(context, message, actor, tools, attachments, **kwargs):  # noqa: ANN001
        del context, actor, attachments, kwargs
        agentic_calls.append(
            (
                str(message),
                {
                    str((item.get("function") or {}).get("name") or "")
                    for item in tools
                    if isinstance(item, dict)
                },
            )
        )
        return {"content": "Составной запрос остался на agentic-маршруте.", "tools_used": []}

    async def forbidden_generate(*args, **kwargs):  # noqa: ANN001
        del args, kwargs
        raise AssertionError("a compound effect was projected as a tool-free file turn")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_agentic_loop", agentic)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)
    monkeypatch.setattr(runtime.kernel, "get_tool_definitions", definitions)
    message = (
        "Создай Word-файл metadata-export.docx по процитированному документу "
        "и напомни мне об этом завтра в 09:00."
    )

    assert _supported_direct_attachment_file_only_request(message) is False
    await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge"),
        attachments=[{"raw_object_id": source.id}],
        enable_tools=True,
        quoted_attachment_reference=True,
    )

    assert len(agentic_calls) == 1
    assert agentic_calls[0][1]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Сохрани результат в export.txt", ()),
        ("Сохрани как export.txt", ()),
        ("Создай итоговый файл как export.txt", ()),
        ("Создай файл с именем export.txt", ()),
        ("Прочитай данные из report.txt", ("report.txt",)),
        ("Сохрани изменения в report.txt", ("report.txt",)),
        ("Сохрани отчёт по данным в report.txt", ("report.txt",)),
        ("Сохрани данные из report.txt в export.txt", ("report.txt",)),
        ("Сохрани содержимое report.txt в export.txt", ("report.txt",)),
        ("Save data from report.txt to export.txt", ("report.txt",)),
        ("Создай export.docx по данным report.docx", ("report.docx",)),
        ("Create export.docx from report.docx", ("report.docx",)),
        ("Create export.docx using report.docx", ("report.docx",)),
        (
            "Сравни a.docx и b.docx и сохрани результат в out.docx.",
            ("a.docx", "b.docx"),
        ),
        (
            "Compare a.docx and b.docx and save the result to out.docx.",
            ("a.docx", "b.docx"),
        ),
        (
            "Объедини a.docx и b.docx и создай Word-файл out.docx.",
            ("a.docx", "b.docx"),
        ),
        (
            "Merge a.docx and b.docx and create Word file out.docx.",
            ("a.docx", "b.docx"),
        ),
        (
            "Combine a.docx and b.docx into out.docx.",
            ("a.docx", "b.docx"),
        ),
        (
            "Используя report.txt, сохрани результат в export.txt",
            ("report.txt",),
        ),
    ],
)
def test_direct_output_filename_masking_is_local_to_each_span(
    message: str,
    expected: tuple[str, ...],
) -> None:
    assert _attachment_filename_mentions(_attachment_selector_message(message)) == expected


@pytest.mark.parametrize(
    ("message", "expected_stem"),
    [
        ("Создай export.docx по данным report.docx", "export"),
        ("Create export.docx from report.docx", "export"),
        ("Сравни a.docx и b.docx и сохрани результат в out.docx.", "out"),
        ("Compare a.docx and b.docx and save the result to out.docx.", "out"),
        ("Объедини a.docx и b.docx и создай Word-файл out.docx.", "out"),
        ("Merge a.docx and b.docx and create Word file out.docx.", "out"),
        ("Combine a.docx and b.docx into out.docx.", "out"),
    ],
)
def test_mixed_input_output_roles_keep_one_supported_output_name(
    message: str,
    expected_stem: str,
) -> None:
    assert _requested_output_filename_stem(message, kind="docx") == (expected_stem, True)


@pytest.mark.asyncio
async def test_output_destination_filename_never_restores_an_older_same_named_file(
    settings,
    storage,
    monkeypatch,
) -> None:
    old_export = _pending_file(
        storage,
        "alice",
        "alice",
        "OLD-EXPORT-MUST-NOT-BECOME-INPUT",
        filename="export.txt",
    )
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", old_export, "old export")
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_EnabledButUnusedLLM())
    seen = _patch_attachment_generation(runtime, monkeypatch)

    async def no_late_file(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", no_late_file)
    await runtime.chat(
        "alice",
        "Сохрани результат в export.txt",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[],
        enable_tools=False,
    )

    assert len(seen) == 1
    assert seen[0][1] == []
    assert "OLD-EXPORT-MUST-NOT-BECOME-INPUT" not in json.dumps(seen, ensure_ascii=False)


@pytest.mark.parametrize(
    ("source_name", "message"),
    [
        (
            "open-source.docx",
            "Открой open-source.docx и создай Word-файл open-export.docx по нему.",
        ),
        (
            "using-source.docx",
            "Используя using-source.docx, создай Word-файл using-export.docx.",
        ),
        (
            "from-source.docx",
            "Сохрани данные из from-source.docx в from-export.docx.",
        ),
        (
            "content-source.docx",
            "Сохрани содержимое content-source.docx в content-export.docx.",
        ),
        (
            "english-source.docx",
            "Save data from english-source.docx to english-export.docx.",
        ),
    ],
)
def test_direct_file_creation_still_honours_explicit_input_filename_selector(
    settings,
    storage,
    source_name: str,
    message: str,
) -> None:
    selected = _pending_file(storage, "alice", "alice", "SELECTED-INPUT", filename=source_name)
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", selected, "selected input")
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_EnabledButUnusedLLM())
    history = storage.get_conversation_messages(conversation["id"], user_id="alice", limit=100)

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        _attachment_selector_message(message),
        history,
        tenant_id="alice",
        person_id="alice",
        allow_file_read=True,
    )

    assert expected == 1
    assert [item["raw_object_id"] for item in restored] == [selected.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Открой report.docx и создай Word-файл export.docx по нему.",
        "Создай Word-файл export.docx по данным report.docx.",
        "Объедини report.docx и current.odt и создай Word-файл export.docx.",
    ],
)
async def test_ambiguous_input_filename_on_file_creation_never_falls_back_to_current(
    settings,
    storage,
    monkeypatch,
    message: str,
) -> None:
    first = _pending_file(storage, "alice", "alice", "FIRST-REPORT", filename="report.docx")
    second = _pending_file(storage, "alice", "alice", "SECOND-REPORT", filename="report.docx")
    current = _pending_file(storage, "alice", "alice", "CURRENT-MUST-NOT-WIN", filename="current.odt")
    conversation = storage.create_conversation("alice")
    _record_upload(storage, conversation["id"], "alice", first, "first report")
    _record_upload(storage, conversation["id"], "alice", second, "second report")
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_EnabledButUnusedLLM())

    async def forbidden_generate(*_args, **_kwargs):
        raise AssertionError("ambiguous input filename reached generation")

    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        attachments=[{"raw_object_id": current.id}],
        enable_tools=True,
    )

    assert "не удалось однозначно определить" in result["message"].casefold()
    assert "CURRENT-MUST-NOT-WIN" not in json.dumps(result, ensure_ascii=False)


def test_exact_replay_restores_a_caption_for_regenerate(settings, storage):
    raw = _pending_file(storage, "alice", "alice", "REPLAY-ROW", filename="replay.txt")
    runtime = AgentRuntime(settings, storage)
    conversation = storage.create_conversation("alice")
    source = storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "сводка по составу",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    restored, expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "сводка по составу",
        history,
        tenant_id="alice",
        person_id="alice",
        replay_source_message_id=str(source["id"]),
        allow_file_read=True,
    )

    assert expected == 1
    assert len(restored) == 1 and restored[0]["raw_object_id"] == raw.id


class _Judge:
    enabled = True
    model = "attachment-judge"

    def __init__(self, answer: str):
        self.answer = answer
        self.messages = []

    async def chat(self, messages, **kwargs):
        del kwargs
        self.messages = messages
        return {"content": self.answer}


@pytest.mark.asyncio
async def test_attachment_chunks_reach_cardinality_verifier_and_repair(settings, storage):
    rows = "\n".join(f"POSITION-{number:02d}: PERSON-{number:02d}" for number in range(1, 17))
    chunks = _attachment_evidence_chunks(
        [{"filename": "positions.txt", "transient_text": rows, "extraction_success": True}]
    )
    assert 1 <= len(chunks) <= 6
    combined = "\n".join(chunk["output"] for chunk in chunks)
    assert "POSITION-01" in combined and "POSITION-16" in combined

    judge = _Judge('{"ok": true, "request_satisfied": true, "score": 1.0, "issues": []}')
    runtime = AgentRuntime(settings, storage, llm=judge)
    verdict = await runtime._verify_response(  # noqa: SLF001
        "перечисли все позиции и посчитай их",
        "В документе 16 позиций.",
        AgentContext(conversation_id="conv", user_id="alice"),
        tool_evidence=[*chunks, {"tool": "web_search", "output": "TOOL-EVIDENCE-SENTINEL"}],
    )
    assert verdict["status"] == "passed"
    judge_system = "\n".join(str(item.get("content") or "") for item in judge.messages)
    assert "количество позиций" in judge_system
    assert "POSITION-16" in judge_system
    assert "TOOL-EVIDENCE-SENTINEL" in judge_system, "attachment chunks displaced real tool evidence"

    repair = _Judge("Исправленный полный ответ, в котором перечислены все шестнадцать отдельных позиций.")
    runtime.llm = repair
    fixed = await runtime._repair_once(  # noqa: SLF001
        "перечисли все позиции",
        "В документе десять позиций, перечислены не все.",
        AgentContext(conversation_id="conv", user_id="alice"),
        {"status": "failed", "issues": ["пропущены позиции"]},
        tool_evidence=[*chunks, {"tool": "web_search", "output": "TOOL-EVIDENCE-SENTINEL"}],
    )
    repair_prompt = "\n".join(str(item.get("content") or "") for item in repair.messages)
    assert fixed.startswith("Исправленный")
    assert "POSITION-16" in repair_prompt
    assert "число позиций" in repair_prompt
    assert "TOOL-EVIDENCE-SENTINEL" in repair_prompt


@pytest.mark.asyncio
async def test_short_attachment_answer_is_verified_without_persisting_file_text(
    settings,
    storage,
    monkeypatch,
):
    private_text = "PRIVATE-ROW-SENTINEL-16"
    raw = _pending_file(storage, "alice", "alice", private_text, filename="private.txt")
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=10_000),
        storage,
        llm=_EnabledButUnusedLLM(),
    )
    captured: dict[str, object] = {}

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id="alice")

    async def generate(context, message, attachments):
        del context, message, attachments
        return {"content": "Их 16.", "tools_used": []}

    async def verify(query, response, context, *, tool_evidence=None):
        del query, response, context
        captured["evidence"] = list(tool_evidence or [])
        return {
            "status": "passed",
            "ok": True,
            "score": 1.0,
            "issues": [f"quoted {private_text}"],
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    result = await runtime.chat(
        "alice",
        "сколько позиций в файле?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "private.txt",
                "transient_text": private_text,
                "extraction_success": True,
            }
        ],
        enable_tools=False,
    )

    assert result["verification_status"] == "passed", "the minimum-length gate hid file evidence"
    evidence = json.dumps(captured.get("evidence"), ensure_ascii=False)
    assert private_text in evidence
    messages = storage.get_conversation_messages(result["conversation_id"], user_id="alice")
    for message in messages:
        assert private_text not in str(message.get("metadata_json") or "")
    assistant_metadata = json.loads(messages[-1]["metadata_json"])
    assert assistant_metadata["verification"]["issues"] == ["attachment_verification_note"]
    assert private_text not in json.dumps(result, ensure_ascii=False)
