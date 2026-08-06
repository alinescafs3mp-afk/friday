"""Review policy honours the prompt's "Inbox before canonical" invariant.

By default (`unless_explicit`), heuristic auto-promotion of substantial material
is queued as a pending Inbox suggestion. Explicit `assessed` compatibility mode
still creates a canonical Knowledge Object immediately, while explicit saves
(/note, "запомни", force_knowledge) still promote directly, since the user already
decided. These tests pin both behaviours and the idempotent replay path.

What the policy covers is tested in `test_review_gate_is_uniform.py`: this file is
about the text path's own semantics, that one about the four entry points that
used to answer this question differently from each other.
"""

from __future__ import annotations

import dataclasses

import pytest

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph

_AUTO_PROMOTE_FACT = "Сервер Atlas работает на Ubuntu 24.04."


def _strict(settings):
    return dataclasses.replace(settings, ingestion_review_policy="unless_explicit")


def _assessed(settings):
    return dataclasses.replace(settings, ingestion_review_policy="assessed")


@pytest.mark.asyncio
async def test_strict_review_downgrades_auto_promotion_to_inbox(settings, storage):
    pipeline = IngestionPipeline(_strict(settings), storage, KnowledgeGraph(storage))

    result = await pipeline.ingest_text("alice", _AUTO_PROMOTE_FACT, source_ref="strict-fact:1")

    assert result["assessed_action"] == "promote"
    assert result["action"] == "review"
    assert result["strict_review"] is True
    assert result["promoted"] is False
    assert result["queued_for_review"] is True
    # A pending Inbox suggestion, not a canonical Knowledge Object.
    assert storage.get_inbox_item(result["inbox_id"], "alice")["knowledge_object_id"] is None
    assert storage.count_knowledge_objects("alice") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "force_knowledge"),
    [
        ("Запомни: синтетический проект Alpha использует PostgreSQL 16.", False),
        ("Синтетическая инфраструктурная заметка о кластере Beta.", True),
    ],
    ids=("explicit-remember", "force-knowledge-note"),
)
async def test_strict_review_still_honours_explicit_saves(settings, storage, content, force_knowledge):
    pipeline = IngestionPipeline(_strict(settings), storage, KnowledgeGraph(storage))

    result = await pipeline.ingest_text(
        "alice",
        content,
        source_ref=f"strict-explicit:{int(force_knowledge)}",
        force_knowledge=force_knowledge,
    )
    assert result["action"] == "promote"
    assert result["promoted"] is True

    assert storage.count_knowledge_objects("alice") == 1


@pytest.mark.asyncio
async def test_assessed_compatibility_mode_auto_promotes_without_review(settings, storage):
    pipeline = IngestionPipeline(_assessed(settings), storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_text("alice", _AUTO_PROMOTE_FACT, source_ref="default-fact:1")
    assert result["action"] == "promote"
    assert result["promoted"] is True
    assert storage.count_knowledge_objects("alice") == 1


@pytest.mark.asyncio
async def test_strict_downgraded_source_replays_without_false_in_progress(settings, storage):
    pipeline = IngestionPipeline(_strict(settings), storage, KnowledgeGraph(storage))
    first = await pipeline.ingest_text("alice", _AUTO_PROMOTE_FACT, source_ref="strict-replay:1")
    assert first["strict_review"] is True

    # A retry of the same source_ref must replay cleanly — the earlier bug raised a
    # false "in progress" error because a promote-labelled raw had no Knowledge Object.
    replay = await pipeline.ingest_text("alice", _AUTO_PROMOTE_FACT, source_ref="strict-replay:1")
    assert replay["idempotent_replay"] is True
    assert replay["promoted"] is False
    assert replay["action"] == "review"
    assert replay["inbox_id"] == first["inbox_id"]
    assert storage.count_knowledge_objects("alice") == 0


@pytest.mark.asyncio
async def test_concurrent_inbox_promotion_creates_exactly_one_ko(settings, storage):
    # Inbox before canonical, EXACTLY once: several approvals racing on one item
    # (Admin UI + Telegram + a worker) must not each mint a KO from one Raw Object.
    import threading

    from friday.storage.models import InboxStatus

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    seeded = await pipeline.ingest_text(
        "alice",
        "Проект Orion переходит на PostgreSQL 16, подтверждено командой инфраструктуры.",
        source_ref="race:orion",
        force_review=True,
    )
    inbox_id = seeded["inbox_id"]
    raw_id = seeded["raw_object_id"]
    assert storage.get_inbox_item(inbox_id, "alice")["knowledge_object_id"] is None

    barrier = threading.Barrier(4)
    errors: list = []

    def approve():
        try:
            barrier.wait(timeout=5)
            pipeline.classify_inbox_item(
                "alice", inbox_id, InboxStatus.CLASSIFIED, promote=True, reviewed_by="alice"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=approve) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10)

    assert not errors, errors
    # Exactly one LIVE canonical KO from one Raw Object; race losers soft-delete
    # their orphan (hidden from retrieval, provenance kept).
    count = storage.execute(
        "SELECT COUNT(*) AS n FROM knowledge_objects "
        "WHERE raw_object_id=? AND user_id=? AND deleted_at IS NULL",
        (raw_id, "alice"),
    ).fetchone()["n"]
    assert count == 1, f"expected exactly one live KO, got {count}"
    ko = storage.get_knowledge_by_raw(raw_id, "alice")
    assert ko is not None and ko.get("deleted_at") is None
    assert storage.get_inbox_item(inbox_id, "alice")["knowledge_object_id"] == ko["id"]
