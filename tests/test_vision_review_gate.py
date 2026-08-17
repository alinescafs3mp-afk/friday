"""Vision/OCR honors the review gate — §24.

Vision-ingested files previously produced a searchable KnowledgeObject before
any confirmation, "Ignore" left that KO in retrieval forever, and the
vision_review_required metadata flag was write-only. These tests pin the
inbox-first flow (no KO until confirm), confirmation building the KO from the
capped vision suggestions, ignore leaving nothing searchable, soft-deletion of
premature KOs on ignore (legacy rows), and idempotent replay of pending
uploads.
"""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from friday.agent_runtime import AgentRuntime, _advisory_vision_overview, _OwnedAttachment
from friday.config import PROFILES
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import EntityType, InboxItem, InboxStatus, new_id


class _VisionLLM:
    enabled = True
    model = "fake-qwen-vision"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {
            "content": json.dumps(
                {
                    "text": "Чек: аренда зала 5000 руб, проект Orion",
                    "pages": [
                        {
                            "asset_id": "A1",
                            "text": "Чек: аренда зала 5000 руб, проект Orion",
                        }
                    ],
                    "title": "Чек за аренду",
                    "summary": "Чек об оплате аренды зала для проекта Orion.",
                    "entities": [
                        {
                            "name": "Orion",
                            "entity_type": "project",
                            "confidence": 0.97,
                            "asset_id": "A1",
                            "evidence": "проект Orion",
                        }
                    ],
                    "evidence": [
                        {
                            "asset_id": "A1",
                            "quote": "аренда зала 5000 руб",
                            "claim": "Оплачена аренда зала",
                        }
                    ],
                    "warnings": [],
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        }


class _NoSecondVisionPass:
    enabled = True
    model = "must-not-run-after-vision"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        del messages, kwargs
        self.calls += 1
        raise AssertionError("complete vision summary triggered a second model pass")


def _png() -> bytes:
    image = Image.new("RGB", (320, 200), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


async def _ingest_image(settings, storage, *, source_ref: str):
    llm = _VisionLLM()
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        llm,
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        _png(),
        filename="receipt.png",
        mime_type="image/png",
        metadata={"uploaded_by": "alice", "language_code": "ru"},
        source_ref=source_ref,
    )
    return pipeline, result


@pytest.mark.asyncio
async def test_visual_summary_is_requested_in_user_language_and_persisted(settings, storage):
    pipeline, result = await _ingest_image(settings, storage, source_ref="vision:summary-language")
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    metadata = json.loads(raw["metadata_json"])

    assert metadata["vision"]["summary_language"] == "ru"
    prompt = json.dumps(pipeline.llm.calls[0][0], ensure_ascii=False)
    assert "in Russian" in prompt
    assert "must occur verbatim in the corresponding pages[].text" in prompt


def test_complete_language_matched_visual_summary_can_skip_second_model_call() -> None:
    summary = "На скане показан чек об оплате аренды зала для проекта Orion. Сумма составляет 5000 рублей."
    attachment = _OwnedAttachment(
        {
            "mime_type": "image/png",
            "advisory_only": True,
            "_registered_file_bytes_verified": True,
            "_advisory_vision_success": True,
            "_advisory_vision_summary": summary,
            "_advisory_vision_summary_language": "ru",
            "_advisory_vision_confidence": 0.9,
            "_advisory_vision_asset_coverage": 1.0,
            "_advisory_vision_grounded_evidence_count": 1,
            "_advisory_vision_pages_read": 1,
            "_advisory_vision_pages_total": 1,
            "_advisory_vision_pages_truncated": False,
            "_advisory_vision_deadline_reached": False,
            "_advisory_vision_text_truncated": False,
        }
    )

    assert _advisory_vision_overview("что на этом скане?", attachment) == summary
    assert _advisory_vision_overview("а суть этого скриншота опиши", attachment) == summary
    assert _advisory_vision_overview("найди регистрационный номер на этом скане", attachment) == ""

    wrong_language = _OwnedAttachment({**attachment, "_advisory_vision_summary_language": "en"})
    partial = _OwnedAttachment(
        {**attachment, "_advisory_vision_pages_read": 1, "_advisory_vision_pages_total": 2}
    )
    unverified = _OwnedAttachment({**attachment, "_registered_file_bytes_verified": False})
    assert _advisory_vision_overview("что на этом скане?", wrong_language) == ""
    assert _advisory_vision_overview("что на этом скане?", partial) == ""
    assert _advisory_vision_overview("что на этом скане?", unverified) == ""


@pytest.mark.asyncio
async def test_complete_registered_scan_overview_publishes_without_second_model_pass(
    settings, storage
) -> None:
    _pipeline, ingested = await _ingest_image(settings, storage, source_ref="vision:single-pass-chat")
    storage.ensure_user("alice", preset_key="owner")
    configured = replace(settings, profile=PROFILES["qwen36-vl"], verify_answers=False)
    authorization = AuthorizationService(storage)
    model = _NoSecondVisionPass()
    runtime = AgentRuntime(
        configured,
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=ExecutionKernel(authorization, configured),
    )

    result = await runtime.chat(
        "alice",
        "что на этом скане?",
        actor=authorization.actor_for_user("alice", source="test"),
        attachments=[{"raw_object_id": str(ingested["raw_object_id"])}],
        enable_tools=True,
    )

    assert model.calls == 0
    assert "Чек об оплате аренды зала для проекта Orion." in result["message"]
    assert result["attachment_verification_complete"] is False
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_sparse_legacy_scan_is_reinspected_once_then_publishes_complete_summary(
    settings,
    storage,
) -> None:
    _pipeline, ingested = await _ingest_image(settings, storage, source_ref="vision:legacy-refresh")
    raw_id = str(ingested["raw_object_id"])
    raw = storage.get_raw_object(raw_id, "alice")
    metadata = json.loads(raw["metadata_json"])
    metadata.pop("vision", None)
    metadata.update(
        {
            "vision_used": False,
            "vision_review_required": False,
            "extraction_success": True,
            "text_extraction_success": True,
            "extraction_chars": 19,
        }
    )
    storage.execute(
        "UPDATE raw_objects SET raw_content=?, metadata_json=? WHERE id=?",
        ("30 декабря 2025 г.", json.dumps(metadata, ensure_ascii=False), raw_id),
    )
    storage.commit()
    storage.ensure_user("alice", preset_key="owner")
    configured = replace(settings, profile=PROFILES["qwen36-vl"], verify_answers=False)
    authorization = AuthorizationService(storage)
    model = _NoSecondVisionPass()
    runtime = AgentRuntime(
        configured,
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=ExecutionKernel(authorization, configured),
    )
    inspections = 0
    summary = "На скане показан полный учебный документ с реквизитами, дисциплиной и итоговой оценкой."

    async def inspect(content, **kwargs):  # noqa: ANN001
        nonlocal inspections
        inspections += 1
        assert bytes(content) == _png()
        assert kwargs["preferred_language"] == "ru"
        return {
            "extraction_success": True,
            "advisory_only": True,
            "_runtime_source_text": "ПОЛНЫЙ ТЕКСТ УЧЕБНОГО ДОКУМЕНТА",
            "text_preview": "ПОЛНЫЙ ТЕКСТ УЧЕБНОГО ДОКУМЕНТА",
            "parse_pages_read": 1,
            "parse_total_pages": 1,
            "_advisory_vision_success": True,
            "_advisory_vision_summary": summary,
            "_advisory_vision_summary_language": "ru",
            "_advisory_vision_confidence": 0.95,
            "_advisory_vision_asset_coverage": 1.0,
            "_advisory_vision_grounded_evidence_count": 2,
            "_advisory_vision_pages_read": 1,
            "_advisory_vision_pages_total": 1,
            "_advisory_vision_pages_truncated": False,
            "_advisory_vision_deadline_reached": False,
            "_advisory_vision_text_truncated": False,
        }

    runtime.kernel.ingestion = SimpleNamespace(inspect_file_transient=inspect)
    result = await runtime.chat(
        "alice",
        "что на этом скане?",
        actor=authorization.actor_for_user("alice", source="test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=True,
    )

    assert inspections == 1
    assert model.calls == 0
    assert summary in result["message"]
    assert "30 декабря 2025 г." not in result["message"]


@pytest.mark.asyncio
async def test_confirmation_builds_ko_from_capped_vision_suggestions(settings, storage):
    # A pre-existing entity lets deferred promotion demonstrate linking; brand
    # new vision entities stay Inbox suggestions by design (no graph pollution).
    KnowledgeGraph(storage).create_entity("alice", "Orion", EntityType.PROJECT)
    pipeline, result = await _ingest_image(settings, storage, source_ref="vision:receipt-1")
    assert result["promoted"] is False
    assert result["knowledge_object"] is None

    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox["status"] == "pending" and inbox["knowledge_object_id"] is None

    reviewed = pipeline.classify_inbox_item(
        "alice", inbox["id"], InboxStatus.CLASSIFIED, promote=True, reviewed_by="alice"
    )
    ko = storage.get_knowledge_by_raw(result["raw_object_id"], "alice")
    assert ko is not None and reviewed["knowledge_object_id"] == ko["id"]
    assert ko["title"] == "Чек за аренду"  # vision suggestion wins over filename
    # The advisory confidence cap travels through deferred promotion: the
    # existing entity is linked only as a capped suggestion, never accepted.
    links = storage.list_knowledge_entity_links("alice", knowledge_object_id=ko["id"], status=None, limit=20)
    assert links
    assert all(float(link["confidence"]) <= 0.79 for link in links)
    assert all(link["status"] == "suggested" for link in links)
    # Only now does the material become searchable.
    hits = storage.search_knowledge("alice", "аренда зала")
    assert [hit["id"] for hit in hits] == [ko["id"]]


@pytest.mark.asyncio
async def test_ignored_vision_file_never_becomes_knowledge(settings, storage):
    pipeline, result = await _ingest_image(settings, storage, source_ref="vision:receipt-2")
    assert storage.search_knowledge("alice", "аренда зала") == []

    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    pipeline.classify_inbox_item("alice", inbox["id"], InboxStatus.IGNORED, reviewed_by="alice")

    assert storage.get_knowledge_by_raw(result["raw_object_id"], "alice") is None
    assert storage.search_knowledge("alice", "аренда зала") == []
    refreshed = storage.get_inbox_item(inbox["id"], "alice")
    assert refreshed["status"] == "ignored"


@pytest.mark.asyncio
async def test_ignore_soft_deletes_premature_ko(settings, storage):
    # Legacy shape: a pending inbox item already pointing at a live KO
    # (pre-fix vision ingestion or auto-promoted text sent back for review).
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    promoted = await pipeline.ingest_text(
        "alice",
        "Проект Orion переходит на PostgreSQL 16, это подтверждено командой.",
        source_ref="note:orion-db",
        force_knowledge=True,
    )
    ko_id = promoted["knowledge_object"]["id"]
    legacy = InboxItem(
        id=new_id("inbox"),
        user_id="alice",
        raw_object_id=promoted["raw_object_id"],
        knowledge_object_id=ko_id,
        status=InboxStatus.PENDING,
    )
    storage.store_inbox_item(legacy)
    assert storage.search_knowledge("alice", "PostgreSQL") != []

    pipeline.classify_inbox_item("alice", legacy.id, InboxStatus.IGNORED, reviewed_by="alice")

    ko = storage.get_knowledge_object(ko_id, "alice")
    assert ko is None or ko.get("deleted_at")  # hidden from retrieval
    assert storage.search_knowledge("alice", "PostgreSQL") == []
    refreshed = storage.get_inbox_item(legacy.id, "alice")
    assert refreshed["knowledge_object_id"] is None
    stored = storage.execute(
        "SELECT metadata_json, deleted_at FROM knowledge_objects WHERE id=?", (ko_id,)
    ).fetchone()
    assert stored["deleted_at"]
    assert json.loads(stored["metadata_json"])["ignored_from_inbox"] == legacy.id


@pytest.mark.asyncio
async def test_replay_of_pending_vision_upload_reports_review(settings, storage):
    pipeline, first = await _ingest_image(settings, storage, source_ref="vision:receipt-3")
    _, replay = await _ingest_image(settings, storage, source_ref="vision:receipt-3")
    assert replay["idempotent_replay"] is True
    assert replay["promoted"] is False
    assert replay["queued_for_review"] is True
    assert replay["inbox_id"] == storage.find_inbox_by_raw(first["raw_object_id"], "alice")["id"]
