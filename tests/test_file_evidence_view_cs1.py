"""CS2: FileEvidenceSet is the sole file-state authority."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openpyxl import Workbook

from friday.agent_runtime import (
    _ATTACHMENT_QUERY_NOT_FOUND,
    _ATTACHMENT_QUERY_UNKNOWN,
    _FILE_EVIDENCE_ATTR,
    _OWNED_SAFE_DOCUMENT_METADATA,
    _RAW_SOURCE_IDENTITY_KEY,
    _UNREADABLE_ATTACHMENT_ANSWER,
    AgentRuntime,
    FileBodyKind,
    FileEvidenceSet,
    _AttachmentHierarchyBundle,
    _bounded_attachment_projection,
    _build_file_evidence_view,
    _file_evidence_set_from_attachments,
    _file_evidence_view_of,
    _historical_direct_read_attachment,
    _historical_direct_read_authority_of,
    _OwnedAttachment,
    _projected_attachment_from_source,
    _ProjectedAttachment,
    _retain_historical_direct_read_authority,
    _stamp_file_evidence,
    _withhold_nonverifiable_attachment,
    _WorkspaceInboxAttachment,
)
from friday.agent_runtime._office_attachments import (
    OFFICE_STRUCTURE_KEY,
    is_trusted_office_attachment,
    trusted_office_attachment,
    validate_runtime_office_index,
)
from friday.documents import DocumentExtractor
from friday.execution_kernel import ExecutionKernel
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import raw_source_identity_sha256, tenant_authorized_file_snapshot_token
from friday.storage.models import RawObject, new_id


def _stamp_valid(item: Any) -> Any:
    view = _build_file_evidence_view(item)
    assert view is not None
    _stamp_file_evidence(item, view)
    return item


def _plant_stale_view(item: Any, view: Any) -> Any:
    object.__setattr__(item, _FILE_EVIDENCE_ATTR, view)
    return item


def _actor(user: str = "alice") -> ActorContext:
    return ActorContext(user_id=user, preset_key="owner", source="cs2-test")


class _NeverLLM:
    enabled = True
    model = "cs2-never"
    total_budget_sec = 1.0

    async def chat(self, *_args, **_kwargs):
        raise AssertionError("file terminal reached the model")


class _SilentLLM:
    enabled = True
    model = "cs2-silent"
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *_args, **_kwargs):
        self.calls += 1
        return {"content": "CS2-SILENT", "tool_calls": None, "_queue_wait_sec": 0.0}


def _runtime(settings, storage, llm) -> AgentRuntime:
    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, settings)
    return AgentRuntime(replace(settings, verify_answers=False), storage, llm=llm, kernel=kernel)


def _register_txt(
    storage,
    settings,
    *,
    user: str,
    filename: str,
    text: str,
    extra: dict[str, Any] | None = None,
) -> RawObject:
    body = text.encode()
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{user}/{digest[:2]}/{digest}.txt"
    path = Path(settings.files_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "mime_type": "text/plain",
            "stored_path": relative,
            "sha256": digest,
            "size_bytes": len(body),
            "uploaded_by": user,
            "extraction_success": True,
            "text_extraction_success": bool(text.strip()),
            "extraction_chars": len(text),
            **(extra or {}),
        },
    )
    storage.store_raw_object(raw)
    return raw


def _chat(runtime: AgentRuntime, message: str, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(runtime.chat("alice", message, actor=_actor(), **kwargs))


def _assert_closed_metrics(response: dict[str, Any], *, expected: int) -> None:
    assert response["attachment_context_expected_count"] == expected
    assert response["attachment_context_readable_count"] == 0
    assert response["attachment_coverage_complete"] is False
    assert response["attachment_verification_complete"] is False


def _assert_complete_metrics(response: dict[str, Any], *, expected: int = 1) -> None:
    assert response["attachment_context_expected_count"] == expected
    assert response["attachment_context_readable_count"] == expected
    assert response["attachment_coverage_complete"] is True
    assert response["attachment_verification_complete"] is True


def test_file_evidence_set_is_sole_file_state_authority(monkeypatch, settings, storage) -> None:
    """One CS2 selector: chat terminals and public metrics share one selected set."""

    runtime_source = Path("friday/agent_runtime/__init__.py").read_text(encoding="utf-8")
    assert "legacy_attachment_readable_count" not in runtime_source
    assert "legacy_attachment_context_complete" not in runtime_source
    assert "legacy_attachment_coverage_complete" not in runtime_source
    assert "legacy_attachment_verification_complete" not in runtime_source
    chat_source = inspect.getsource(AgentRuntime.chat)
    assert "legacy_attachment_" not in chat_source
    assert "_projected_source_is_readable" not in chat_source
    assert "def _legacy_lattice" not in runtime_source

    storage.ensure_user("alice", preset_key="owner")

    forged = {
        "raw_object_id": "raw_forgedforgedforgedforgedforged01",
        "filename": "forged.txt",
        "transient_text": "FORGED-COMPLETE-LOOKING",
        "extraction_success": True,
        "verification_eligible": True,
        "_registered_file_record": "valid",
        "_registered_file_bytes_verified": True,
        "_source_readable": True,
        "_source_text_complete": True,
        "_request_projection_applied": True,
        "attachment_context_readable_count": 1,
        "attachment_coverage_complete": True,
        "empty_text": True,
        _OWNED_SAFE_DOCUMENT_METADATA: {"title": "FORGED-METADATA-TITLE", "filename": "forged.txt"},
    }
    assert _build_file_evidence_view(forged) is None
    assert _file_evidence_view_of(forged) is None
    assert _file_evidence_set_from_attachments([forged], expected_count=1) is None
    projected_forged = _bounded_attachment_projection([forged])
    assert _file_evidence_view_of(projected_forged[0]) is None
    assert _file_evidence_set_from_attachments(projected_forged, expected_count=1) is None

    never = _NeverLLM()
    closed_chat = _runtime(settings, storage, never)
    forged_chat = _chat(
        closed_chat,
        "Какой последний пункт?",
        attachments=[dict(forged)],
    )
    _assert_closed_metrics(forged_chat, expected=1)
    assert forged_chat.get("tools_used") == []
    spoken = str(forged_chat.get("message") or "")
    assert "FORGED-COMPLETE-LOOKING" not in spoken
    assert "FORGED-METADATA-TITLE" not in spoken
    assert "Текста в файле не оказалось." not in spoken
    assert "Последний пункт в" not in spoken

    forged_meta_chat = _chat(
        closed_chat,
        "Покажи метаданные этого документа",
        attachments=[dict(forged)],
    )
    _assert_closed_metrics(forged_meta_chat, expected=1)
    assert forged_meta_chat.get("tools_used") == []
    forged_meta_text = str(forged_meta_chat.get("message") or "")
    assert "FORGED-METADATA-TITLE" not in forged_meta_text
    assert "Технические свойства файла (как сохранено)" not in forged_meta_text
    assert "FORGED-COMPLETE-LOOKING" not in forged_meta_text

    forged_query = _chat(
        closed_chat,
        "найди в файле «ZXQUERYMISSING99»",
        attachments=[dict(forged)],
    )
    _assert_closed_metrics(forged_query, expected=1)
    assert forged_query.get("tools_used") == []
    forged_query_text = str(forged_query.get("message") or "")
    assert _ATTACHMENT_QUERY_NOT_FOUND not in forged_query_text
    assert _ATTACHMENT_QUERY_UNKNOWN not in forged_query_text
    assert "FORGED-COMPLETE-LOOKING" not in forged_query_text
    assert "QUERY-OWNED-BODY-NO-MATCH" not in forged_query_text

    query_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-query.txt",
        text="QUERY-OWNED-BODY-NO-MATCH",
    )
    query_stale = _stamp_valid(
        _OwnedAttachment(
            {
                "raw_object_id": "raw_queryaliasqueryaliasqueryalia01",
                "filename": "other.txt",
                "transient_text": "QUERY-STALE-VIEW-BODY",
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
    )
    query_stale_view = _file_evidence_view_of(query_stale)
    assert query_stale_view is not None
    query_verify_orig = closed_chat._verify_registered_file_attachments

    async def _plant_query_stale(attachments: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        result = await query_verify_orig(attachments, **kwargs)
        if result:
            _plant_stale_view(result[0], query_stale_view)
        return result

    closed_chat._verify_registered_file_attachments = _plant_query_stale  # type: ignore[method-assign]
    query_chat = _chat(
        closed_chat,
        "найди в файле «ZXQUERYMISSING99»",
        attachments=[_OwnedAttachment({"raw_object_id": query_raw.id})],
    )
    closed_chat._verify_registered_file_attachments = query_verify_orig  # type: ignore[method-assign]
    _assert_closed_metrics(query_chat, expected=1)
    assert query_chat.get("tools_used") == []
    query_text = str(query_chat.get("message") or "")
    assert _ATTACHMENT_QUERY_NOT_FOUND not in query_text
    assert _ATTACHMENT_QUERY_UNKNOWN not in query_text
    assert "QUERY-OWNED-BODY-NO-MATCH" not in query_text
    assert "QUERY-STALE-VIEW-BODY" not in query_text

    meta_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-meta.txt",
        text="DISK-VERIFIED-METADATA-BODY",
        extra={"title": "Hydrated-Title"},
    )
    meta = _chat(
        closed_chat,
        "Покажи метаданные этого документа",
        attachments=[_OwnedAttachment({"raw_object_id": meta_raw.id})],
    )
    _assert_complete_metrics(meta)
    assert meta.get("tools_used") == []
    assert "Технические свойства файла (как сохранено)" in meta["message"]
    assert "cs2-meta.txt" in meta["message"]
    assert "DISK-VERIFIED-METADATA-BODY" not in meta["message"]

    sha_text = "CACHED-SHA-INVALID-BODY"
    sha_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-sha-invalid.txt",
        text=sha_text,
        extra={"title": "CACHED-INVALID-SHA-TITLE"},
    )
    sha_digest = hashlib.sha256(sha_text.encode()).hexdigest()
    sha_path = Path(settings.files_dir) / "alice" / sha_digest[:2] / f"{sha_digest}.txt"
    sha_path.write_bytes(b"CORRUPTED-BYTES-NOT-MATCHING-REGISTERED-SHA")
    sha_chat = _chat(
        closed_chat,
        "Покажи метаданные этого документа",
        attachments=[_OwnedAttachment({"raw_object_id": sha_raw.id})],
    )
    _assert_closed_metrics(sha_chat, expected=1)
    assert sha_chat.get("tools_used") == []
    sha_text_out = str(sha_chat.get("message") or "")
    assert "CACHED-INVALID-SHA-TITLE" not in sha_text_out
    assert "CACHED-SHA-INVALID-BODY" not in sha_text_out
    assert "Технические свойства файла (как сохранено)" not in sha_text_out

    mixed_meta = _chat(
        closed_chat,
        "Покажи метаданные этих документов",
        attachments=[
            _OwnedAttachment({"raw_object_id": meta_raw.id}),
            _OwnedAttachment({"raw_object_id": sha_raw.id}),
        ],
    )
    _assert_closed_metrics(mixed_meta, expected=2)
    assert mixed_meta.get("attachment_context_available") is False
    assert mixed_meta.get("tools_used") == []
    mixed_text = str(mixed_meta.get("message") or "")
    assert "Hydrated-Title" not in mixed_text
    assert "DISK-VERIFIED-METADATA-BODY" not in mixed_text
    assert "CACHED-INVALID-SHA-TITLE" not in mixed_text
    assert "CACHED-SHA-INVALID-BODY" not in mixed_text
    assert "Технические свойства файла (как сохранено)" not in mixed_text
    assert "cs2-meta.txt" not in mixed_text
    assert "cs2-sha-invalid.txt" not in mixed_text

    empty_raw = _register_txt(storage, settings, user="alice", filename="cs2-empty.txt", text="")
    empty = _chat(
        closed_chat,
        "что в этом файле?",
        attachments=[_OwnedAttachment({"raw_object_id": empty_raw.id})],
    )
    _assert_complete_metrics(empty)
    assert empty.get("tools_used") == []
    assert empty["message"] == "Текста в файле не оказалось."

    last_body = "1. Первый пункт\n2. Второй пункт\n3. LAST-ITEM-LITERAL-CS2"
    last_raw = _register_txt(storage, settings, user="alice", filename="cs2-last.txt", text=last_body)
    last = _chat(
        closed_chat,
        "Какой последний пункт?",
        attachments=[_OwnedAttachment({"raw_object_id": last_raw.id})],
    )
    _assert_complete_metrics(last)
    assert last.get("tools_used") == []
    assert "LAST-ITEM-LITERAL-CS2" in last["message"]
    assert last["message"].startswith("Последний пункт в")

    ql_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-quicklook.txt",
        text="QUICKLOOK-LITERAL-CS2 first line of the registered upload.",
    )
    full_review_model = _SilentLLM()
    full_review = _chat(
        _runtime(settings, storage, full_review_model),
        "Загружен документ: cs2-quicklook.txt",
        attachments=[_OwnedAttachment({"raw_object_id": ql_raw.id})],
        synthetic_document_notice=True,
    )
    _assert_complete_metrics(full_review)
    assert full_review.get("tools_used") == []
    assert full_review_model.calls == 1
    assert full_review["message"] == "CS2-SILENT"

    nosave = _stamp_valid(
        _OwnedAttachment(
            {
                "filename": "nosave.txt",
                "transient_text": "SERVER-OWNED-NO-SAVE-BODY",
                "extraction_success": True,
                "verification_eligible": True,
            }
        )
    )
    silent = _SilentLLM()
    nosave_chat = _chat(
        _runtime(settings, storage, silent),
        "что в этом файле?",
        attachments=[nosave],
    )
    _assert_complete_metrics(nosave_chat)
    nosave_set = _file_evidence_set_from_attachments([nosave], expected_count=1)
    assert nosave_set is not None
    assert nosave_set.source_readable_count == 1
    assert nosave_set.coverage_complete is True
    assert nosave_set.verification_complete is True

    workspace = _stamp_valid(
        _WorkspaceInboxAttachment(
            {
                "filename": "inbox.txt",
                "workspace_relative_path": "dept/note.txt",
                "workspace_sha256": "a" * 64,
                "workspace_source_sha256": "b" * 64,
                "transient_text": "MCP-BODY",
                "extraction_success": True,
                "verification_eligible": True,
                "_workspace_file_bytes_verified": True,
            }
        )
    )
    workspace_projected = _bounded_attachment_projection([workspace])
    assert isinstance(workspace_projected[0], _ProjectedAttachment)
    workspace_view = _file_evidence_view_of(workspace_projected[0])
    assert workspace_view is not None
    assert workspace_view.source_readable is True
    assert workspace_view.workspace_relative_path == "dept/note.txt"

    ghost_raw = "raw_ghostauthoritywithoutdisk01"
    ghost_marker = _historical_direct_read_attachment(
        ghost_raw,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="telegram_reply",
    )
    assert ghost_marker is not None
    ghost = _OwnedAttachment(
        {
            "raw_object_id": ghost_raw,
            "filename": "ghost.txt",
            "transient_text": "GHOST-BODY-MUST-NOT-RAISE",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    ghost = _retain_historical_direct_read_authority(ghost_marker, ghost)
    ghost_chat = _chat(
        closed_chat,
        "Какой последний пункт?",
        attachments=[ghost],
    )
    _assert_closed_metrics(ghost_chat, expected=1)
    assert "GHOST-BODY-MUST-NOT-RAISE" not in str(ghost_chat.get("message") or "")
    assert "Последний пункт в" not in str(ghost_chat.get("message") or "")

    stale_source = _stamp_valid(
        _OwnedAttachment(
            {
                "raw_object_id": "raw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "filename": "raw-a-empty.txt",
                "transient_text": "",
                "empty_text": True,
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
    )
    stale_view = _file_evidence_view_of(stale_source)
    assert stale_view is not None
    assert stale_view.body_kind == FileBodyKind.EMPTY
    assert stale_view.raw_id == "raw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    stale_b = _OwnedAttachment(
        {
            "raw_object_id": "raw_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "filename": "raw-b.txt",
            "transient_text": "STALE-B-EMPTY-OR-EXTRACTED",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    _plant_stale_view(stale_b, stale_view)
    assert _file_evidence_set_from_attachments([stale_b], expected_count=1) is None

    stale_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-stale-b.txt",
        text="STALE-B-BODY-MUST-NOT-LEAK",
    )
    verify_orig = closed_chat._verify_registered_file_attachments

    async def _plant_after_verify(attachments: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        result = await verify_orig(attachments, **kwargs)
        if result:
            _plant_stale_view(result[0], stale_view)
        return result

    closed_chat._verify_registered_file_attachments = _plant_after_verify  # type: ignore[method-assign]
    stale_chat = _chat(
        closed_chat,
        "Какой последний пункт?",
        attachments=[_OwnedAttachment({"raw_object_id": stale_raw.id})],
    )
    closed_chat._verify_registered_file_attachments = verify_orig  # type: ignore[method-assign]
    _assert_closed_metrics(stale_chat, expected=1)
    assert stale_chat.get("tools_used") == []
    stale_out = str(stale_chat.get("message") or "")
    assert "STALE-B-BODY-MUST-NOT-LEAK" not in stale_out
    assert "Последний пункт в" not in stale_out
    assert "Текста в файле не оказалось." not in stale_out
    assert "Технические свойства файла (как сохранено)" not in stale_out

    reply_marker = _historical_direct_read_attachment(
        last_raw.id,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="telegram_reply",
    )
    assert reply_marker is not None
    reply_owned = _OwnedAttachment({"raw_object_id": last_raw.id})
    reply_owned = _retain_historical_direct_read_authority(reply_marker, reply_owned)
    seen_historical: list[Any] = []
    owned_orig = closed_chat._owned_file_attachment

    def _spy_owned(raw_id: str, **kwargs: Any) -> Any:
        authority = kwargs.get("historical_authority")
        if authority is not None:
            seen_historical.append(authority)
        return owned_orig(raw_id, **kwargs)

    closed_chat._owned_file_attachment = _spy_owned  # type: ignore[method-assign]
    reply_chat = _chat(
        closed_chat,
        "Какой последний пункт?",
        attachments=[reply_owned],
    )
    closed_chat._owned_file_attachment = owned_orig  # type: ignore[method-assign]
    _assert_complete_metrics(reply_chat)
    assert "LAST-ITEM-LITERAL-CS2" in reply_chat["message"]
    assert seen_historical
    retained = seen_historical[-1]
    assert retained.selector_kind == "telegram_reply"
    assert retained.raw_object_id == last_raw.id
    assert retained.uploaded_by == "alice"
    assert retained.tenant_id == "alice"

    stub_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="stub.bin",
        text="[File: stub.bin]",
        extra={"extraction_success": False, "text_extraction_success": False},
    )
    withheld_chat = _chat(
        closed_chat,
        "что в этом файле?",
        attachments=[_OwnedAttachment({"raw_object_id": stub_raw.id})],
    )
    _assert_closed_metrics(withheld_chat, expected=1)
    assert withheld_chat.get("tools_used") == []
    assert "[File: stub.bin]" not in str(withheld_chat.get("message") or "")
    assert str(withheld_chat.get("message") or "") == _UNREADABLE_ATTACHMENT_ANSWER

    withhold_raw = "raw_withholdwithholdwithholdwith01"
    withhold_marker = _historical_direct_read_attachment(
        withhold_raw,
        tenant_id="tenant-test",
        uploaded_by="uploader-test",
        selector_kind="telegram_reply",
    )
    assert withhold_marker is not None
    withheld_source = _stamp_valid(
        _OwnedAttachment(
            {
                "raw_object_id": withhold_raw,
                "filename": "stub.bin",
                "transient_text": "[File: stub.bin]",
                "extraction_success": False,
                "verification_eligible": False,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
    )
    withheld_source = _retain_historical_direct_read_authority(withhold_marker, withheld_source)
    withheld = _withhold_nonverifiable_attachment(withheld_source)
    assert isinstance(withheld, _OwnedAttachment)
    assert _historical_direct_read_authority_of(withheld) is not None
    assert str(withheld.get("transient_text") or "") == ""
    withheld_view = _file_evidence_view_of(withheld)
    assert withheld_view is not None
    assert withheld_view.source_readable is False
    assert withheld_view.verification_eligible is False
    assert withheld_view is not _file_evidence_view_of(withheld_source)

    sibling_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="ok.txt",
        text="HEALTHY-SIBLING",
    )
    pair_chat = _chat(
        closed_chat,
        "суммируй эти два файла",
        attachments=[
            _OwnedAttachment({"raw_object_id": sibling_raw.id}),
            {
                "filename": "bare.txt",
                "transient_text": "UNSTAMPED-SIBLING",
                "extraction_success": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            },
        ],
    )
    _assert_closed_metrics(pair_chat, expected=2)
    assert pair_chat.get("tools_used") == []
    pair_text = str(pair_chat.get("message") or "")
    assert "HEALTHY-SIBLING" not in pair_text
    assert "UNSTAMPED-SIBLING" not in pair_text

    pair_stale_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="stale-sib.txt",
        text="STALE-PRIVATE-SIBLING-BODY",
    )
    pair_verify_orig = closed_chat._verify_registered_file_attachments

    async def _plant_second_after_verify(
        attachments: list[dict[str, Any]], **kwargs: Any
    ) -> list[dict[str, Any]]:
        result = await pair_verify_orig(attachments, **kwargs)
        if len(result) >= 2:
            first_view = _file_evidence_view_of(result[0])
            if first_view is not None:
                _plant_stale_view(result[1], first_view)
        return result

    closed_chat._verify_registered_file_attachments = _plant_second_after_verify  # type: ignore[method-assign]
    stale_pair_chat = _chat(
        closed_chat,
        "суммируй эти два файла",
        attachments=[
            _OwnedAttachment({"raw_object_id": sibling_raw.id}),
            _OwnedAttachment({"raw_object_id": pair_stale_raw.id}),
        ],
    )
    closed_chat._verify_registered_file_attachments = pair_verify_orig  # type: ignore[method-assign]
    _assert_closed_metrics(stale_pair_chat, expected=2)
    assert stale_pair_chat.get("tools_used") == []
    stale_pair_text = str(stale_pair_chat.get("message") or "")
    assert "HEALTHY-SIBLING" not in stale_pair_text
    assert "STALE-PRIVATE-SIBLING-BODY" not in stale_pair_text

    healthy = _stamp_valid(
        _OwnedAttachment(
            {
                "raw_object_id": "raw_healthyhealthyhealthyhealth01",
                "filename": "ok.txt",
                "transient_text": "HEALTHY-SIBLING",
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
    )
    unstamped_sibling = _OwnedAttachment(
        {
            "raw_object_id": "raw_unstampedunstampedunstamped01",
            "filename": "bare.txt",
            "transient_text": "UNSTAMPED-SIBLING",
            "extraction_success": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    assert _file_evidence_set_from_attachments([healthy, unstamped_sibling], expected_count=2) is None
    assert _file_evidence_set_from_attachments([healthy], expected_count=2) is None

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Name", "Role"])
    sheet.append(["Alice", "Engineer"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    extracted = DocumentExtractor().extract(buffer.getvalue(), "tiny-roster.xlsx")
    assert extracted.success is True
    index = extracted.office_structure_index
    assert isinstance(index, dict)
    assert validate_runtime_office_index(index, extracted.text) == index
    office = trusted_office_attachment(
        {
            "raw_object_id": "raw_officeofficeofficeofficeofficeo1",
            "filename": "tiny-roster.xlsx",
            "transient_text": extracted.text,
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            OFFICE_STRUCTURE_KEY: index,
        }
    )
    assert is_trusted_office_attachment(office)
    _stamp_valid(office)
    office_projected = _bounded_attachment_projection([office])
    office_set = _file_evidence_set_from_attachments(office_projected, expected_count=1)
    assert office_set is not None
    assert office_set.source_readable_count == 1
    assert office_set.coverage_complete is True
    assert office_set.verification_complete is True

    incomplete = trusted_office_attachment(
        {
            "raw_object_id": "raw_incompleteofficeprojection00001",
            "filename": "incomplete.xlsx",
            "transient_text": "Name | Role\nAlice | Engineer",
            "extraction_success": True,
            "verification_eligible": True,
            "text_truncated": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    incomplete_view = _build_file_evidence_view(incomplete)
    assert incomplete_view is not None
    assert incomplete_view.source_complete is False
    _stamp_file_evidence(incomplete, incomplete_view)
    upgraded = _projected_attachment_from_source(
        incomplete,
        {
            **incomplete,
            "text_truncated": False,
            "_office_structured": True,
            "_office_prompt_available": True,
            "_office_index_complete": True,
            "_office_prompt_complete": True,
        },
    )
    derived = _file_evidence_view_of(upgraded)
    assert derived is not None
    assert derived.source_readable is True
    assert derived.source_complete is False
    upgraded_set = _file_evidence_set_from_attachments([upgraded], expected_count=1)
    assert upgraded_set is not None
    assert upgraded_set.context_complete is True
    assert upgraded_set.coverage_complete is False
    assert upgraded_set.verification_complete is False

    hierarchy_body = ("HIERARCHY-SOURCE-LINE\n" * 2000) + "HIERARCHY-SOURCE-TAIL"
    hierarchy_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-hierarchy.txt",
        text=hierarchy_body,
        extra={"text_truncated": True},
    )
    fake_bundle = _AttachmentHierarchyBundle(
        evidence="HIERARCHY-FAKE-UPGRADE",
        source_complete=True,
        map_complete=True,
        files_total=1,
        files_readable=9,
        chunks_total=1,
        chunks_planned=1,
        chunks_mapped=1,
        source_chars_total=len(hierarchy_body),
        source_chars_planned=len(hierarchy_body),
        records_available=False,
        ordered_record_count=None,
    )

    async def _fake_hierarchy_bundle(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        return fake_bundle, True

    async def _fake_hierarchy_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "content": "HIERARCHY-FAKE-SYNTHESIS",
            "tools_used": [],
            "_attachment_hierarchy_bundle": fake_bundle,
            "_attachment_hierarchy_complete": True,
        }

    monkeypatch.setattr(closed_chat, "_build_attachment_hierarchy_bundle", _fake_hierarchy_bundle)
    monkeypatch.setattr(closed_chat, "_hierarchical_attachment_response", _fake_hierarchy_response)
    hierarchy_chat = _chat(
        closed_chat,
        "суммируй этот файл",
        attachments=[_OwnedAttachment({"raw_object_id": hierarchy_raw.id})],
    )
    assert hierarchy_chat.get("tools_used") == []
    assert hierarchy_chat["attachment_context_expected_count"] == 1
    assert hierarchy_chat["attachment_context_readable_count"] == 1
    assert hierarchy_chat["attachment_coverage_complete"] is False
    assert hierarchy_chat["attachment_verification_complete"] is False
    assert hierarchy_chat["attachment_context_readable_count"] <= 1

    downgrade_body = ("HIERARCHY-DOWNGRADE-LINE\n" * 2000) + "HIERARCHY-DOWNGRADE-TAIL"
    downgrade_raw = _register_txt(
        storage,
        settings,
        user="alice",
        filename="cs2-hierarchy-downgrade.txt",
        text=downgrade_body,
    )
    pre_hierarchy = _stamp_valid(
        _OwnedAttachment(
            {
                "raw_object_id": downgrade_raw.id,
                "filename": "cs2-hierarchy-downgrade.txt",
                "transient_text": downgrade_body,
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
    )
    pre_hierarchy_set = _file_evidence_set_from_attachments([pre_hierarchy], expected_count=1)
    assert pre_hierarchy_set is not None
    assert pre_hierarchy_set.source_readable_count == 1
    assert pre_hierarchy_set.coverage_complete is True
    assert pre_hierarchy_set.verification_complete is True
    fake_downgrade = _AttachmentHierarchyBundle(
        evidence="HIERARCHY-FAKE-DOWNGRADE",
        source_complete=False,
        map_complete=False,
        files_total=1,
        files_readable=0,
        chunks_total=1,
        chunks_planned=1,
        chunks_mapped=0,
        source_chars_total=len(downgrade_body),
        source_chars_planned=0,
        records_available=False,
        ordered_record_count=None,
    )

    async def _fake_downgrade_bundle(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        return fake_downgrade, False

    async def _fake_downgrade_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "content": "HIERARCHY-FAKE-DOWNGRADE-SYNTHESIS",
            "tools_used": [],
            "_attachment_hierarchy_bundle": fake_downgrade,
            "_attachment_hierarchy_complete": False,
        }

    monkeypatch.setattr(closed_chat, "_build_attachment_hierarchy_bundle", _fake_downgrade_bundle)
    monkeypatch.setattr(closed_chat, "_hierarchical_attachment_response", _fake_downgrade_response)
    downgrade_chat = _chat(
        closed_chat,
        "суммируй этот файл",
        attachments=[_OwnedAttachment({"raw_object_id": downgrade_raw.id})],
    )
    assert downgrade_chat.get("tools_used") == []
    assert downgrade_chat["attachment_context_expected_count"] == 1
    assert downgrade_chat["attachment_context_readable_count"] == 0
    assert downgrade_chat["attachment_coverage_complete"] is False
    assert downgrade_chat["attachment_verification_complete"] is False

    current = _stamp_valid(
        _OwnedAttachment(
            {
                "raw_object_id": "raw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "filename": "current.txt",
                "transient_text": "CURRENT-BODY-ALPHA",
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
    )
    historical = _ProjectedAttachment(
        {
            "raw_object_id": "raw_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "filename": "historical.txt",
            "transient_text": "",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            "_request_projection_applied": True,
            "_source_readable": True,
            "_source_text_complete": True,
        }
    )
    historical = _stamp_valid(historical)
    pair = _file_evidence_set_from_attachments([current, historical], expected_count=2)
    assert isinstance(pair, FileEvidenceSet)
    assert pair.source_readable_count == 2
    assert pair.context_complete is True
    assert pair.coverage_complete is True
    assert pair.verification_complete is True
    assert _file_evidence_view_of(historical).body_kind == FileBodyKind.PROJECTED
    assert _file_evidence_view_of(historical).projection_empty_no_match is True

    metadata_raw_id = "raw_0123456789abcdef"
    metadata_bytes = b"abc"
    metadata_sha256 = hashlib.sha256(metadata_bytes).hexdigest()
    metadata_raw_projection = {
        "id": metadata_raw_id,
        "user_id": "tenant-test",
        "source": "upload",
        "source_ref": "cs2:legacy-metadata",
        "content_type": "file",
        "received_at": "2026-08-14T00:00:00+00:00",
        "content_hash": metadata_sha256,
        "_raw_content": "DISK-VERIFIED-BODY",
        "_raw_metadata": "{}",
    }
    metadata_identity = raw_source_identity_sha256(metadata_raw_projection)
    metadata_snapshot_token = tenant_authorized_file_snapshot_token(
        metadata_raw_projection,
        content_sha256=metadata_sha256,
        tenant_id="tenant-test",
        storage_owner_id="tenant-test",
    )
    assert metadata_snapshot_token is not None

    original_view = _build_file_evidence_view(
        _OwnedAttachment(
            {
                "raw_object_id": metadata_raw_id,
                "filename": "legacy.doc",
                "transient_text": "DISK-VERIFIED-BODY",
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
                _RAW_SOURCE_IDENTITY_KEY: metadata_identity,
            }
        )
    )
    assert original_view is not None
    stamped = _OwnedAttachment(
        {
            "raw_object_id": metadata_raw_id,
            "filename": "legacy.doc",
            "transient_text": "DISK-VERIFIED-BODY",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            _RAW_SOURCE_IDENTITY_KEY: metadata_identity,
        }
    )
    _stamp_file_evidence(stamped, original_view)
    unstamped_canonical = _OwnedAttachment(
        {
            "raw_object_id": metadata_raw_id,
            "filename": "legacy.doc",
            "transient_text": "DISK-VERIFIED-BODY",
            "extraction_success": True,
            "_registered_file_record": "valid",
            _RAW_SOURCE_IDENTITY_KEY: metadata_identity,
        }
    )

    class _Authorized:
        content = metadata_bytes
        filename = "legacy.doc"
        mime_type = "application/msword"
        snapshot_token = metadata_snapshot_token

    async def inspect_headers(*_args, **_kwargs):
        return {"_document_metadata": {"format": "odt", "title": "Hydrated-Title"}}

    hydrate_runtime = object.__new__(AgentRuntime)
    hydrate_runtime._owned_file_attachment = (  # noqa: SLF001
        lambda *_args, **_kwargs: unstamped_canonical
    )
    hydrate_runtime.storage = object()
    hydrate_runtime.settings = SimpleNamespace(files_dir="/tmp", max_upload_bytes=1_000_000)
    hydrate_runtime.kernel = SimpleNamespace(
        ingestion=SimpleNamespace(inspect_file_transient=inspect_headers)
    )
    monkeypatch.setattr(
        "friday.agent_runtime.read_authorized_file",
        lambda *_args, **_kwargs: _Authorized(),
    )
    hydrated = asyncio.run(
        AgentRuntime._hydrate_legacy_document_metadata(
            hydrate_runtime,
            [stamped],
            tenant_id="tenant-test",
            person_id="uploader-test",
        )
    )
    assert len(hydrated) == 1
    assert hydrated[0].get("_registered_file_bytes_verified") is True
    assert _file_evidence_view_of(hydrated[0]) is original_view
    assert hydrated[0].get(_OWNED_SAFE_DOCUMENT_METADATA, {}).get("title") == "Hydrated-Title"
