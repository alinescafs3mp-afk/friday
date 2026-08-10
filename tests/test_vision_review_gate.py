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

import pytest
from PIL import Image

from friday.config import PROFILES
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType, InboxItem, InboxStatus, new_id


class _VisionLLM:
    enabled = True
    model = "fake-qwen-vision"

    async def chat(self, messages, **kwargs):
        return {
            "content": json.dumps(
                {
                    "text": "Чек: аренда зала 5000 руб, проект Orion",
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


def _png() -> bytes:
    image = Image.new("RGB", (320, 200), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


async def _ingest_image(settings, storage, *, source_ref: str):
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        _VisionLLM(),
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        _png(),
        filename="receipt.png",
        mime_type="image/png",
        source_ref=source_ref,
    )
    return pipeline, result


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
