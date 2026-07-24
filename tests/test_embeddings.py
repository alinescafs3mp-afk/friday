"""Dense recall over persisted vectors — the §5 embeddings path.

The historical embedding code only re-ranked the FTS/recency pool and never stored
a vector, so a semantically relevant note that shared no lexical tokens and was not
recent could not be recalled. These tests pin the new contract: vectors are indexed
and persisted by a worker, and query-time dense recall unions corpus vectors into
the candidate pool and rescues lexically-disjoint matches through the evidence gate.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from jericho.retrieval import EmbeddingBackend, HybridSearcher, pack_vector
from jericho.storage.models import KnowledgeObject, RawObject, new_id
from jericho.workers import WorkersManager


def _make_ko(storage, user_id: str, content: str, *, title: str) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


def _concept_vector(text: str) -> list[float]:
    """Toy 'semantic' embedding: maps meaning, not surface tokens, to an axis."""
    lowered = text.lower()
    if "пёс" in lowered or "питом" in lowered or "собак" in lowered:
        return [1.0, 0.0, 0.0]
    if "акци" in lowered or "портфел" in lowered:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


class _FakeEmbeddings:
    """Stands in for EmbeddingBackend without touching the network."""

    def __init__(self, settings):
        self.settings = settings
        self.remote_enabled = True
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [_concept_vector(text) for text in texts]


def _embeddings_settings(settings):
    return dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9999/v1",
        embeddings_model="test-embed",
        embeddings_recall_candidates=40,
        embeddings_index_batch=64,
    )


# --- storage index bookkeeping -------------------------------------------


def test_missing_embedding_query_tracks_new_model_and_version(storage):
    ko = _make_ko(storage, "alice", "Мой пёс Рекс любит мяч", title="Пёс")
    assert [row["id"] for row in storage.list_knowledge_missing_embedding("test-embed")] == [ko["id"]]

    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko["id"],
                "user_id": "alice",
                "model": "test-embed",
                "dim": 3,
                "source_version": ko["version"],
                "content_hash": "h",
                "vector": pack_vector([1.0, 0.0, 0.0]),
            }
        ]
    )
    assert storage.count_knowledge_embeddings("alice") == 1
    assert storage.list_knowledge_missing_embedding("test-embed") == []
    # A different active model invalidates the stored vector.
    assert [row["id"] for row in storage.list_knowledge_missing_embedding("other-model")] == [ko["id"]]

    # A content update bumps the object version, so the vector becomes stale.
    storage.update_knowledge_fields(ko["id"], "alice", content="Совсем другой текст про кошку")
    assert [row["id"] for row in storage.list_knowledge_missing_embedding("test-embed")] == [ko["id"]]


def test_stored_vectors_are_scoped_by_model_dim_and_exclude_deleted(storage):
    ko = _make_ko(storage, "alice", "Портфель акций", title="Финансы")
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko["id"],
                "user_id": "alice",
                "model": "test-embed",
                "dim": 3,
                "source_version": ko["version"],
                "content_hash": "h",
                "vector": pack_vector([0.0, 1.0, 0.0]),
            }
        ]
    )
    assert len(storage.get_user_embeddings("alice", "test-embed", 3)) == 1
    assert storage.get_user_embeddings("alice", "test-embed", 384) == []
    assert storage.get_user_embeddings("alice", "wrong-model", 3) == []

    storage.soft_delete_knowledge_object(ko["id"], "alice")
    assert storage.get_user_embeddings("alice", "test-embed", 3) == []


# --- query-time dense recall ---------------------------------------------


@pytest.mark.asyncio
async def test_dense_recall_unions_corpus_vector_into_empty_pool(storage, settings):
    tuned = _embeddings_settings(settings)
    ko = _make_ko(storage, "alice", "Мой пёс Рекс любит мяч", title="Пёс")
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko["id"],
                "user_id": "alice",
                "model": "test-embed",
                "dim": 3,
                "source_version": ko["version"],
                "content_hash": "h",
                "vector": pack_vector([1.0, 0.0, 0.0]),
            }
        ]
    )
    searcher = HybridSearcher(storage, _FakeEmbeddings(tuned))
    pool: dict[str, dict] = {}
    scores = await searcher._dense_recall("alice", "нужен совет про питомца", pool)  # noqa: SLF001
    # The object was NOT in the pool; dense recall pulled it in from the corpus.
    assert ko["id"] in pool
    assert scores[ko["id"]] > 0.99


@pytest.mark.asyncio
async def test_search_rescues_lexically_disjoint_but_semantic_match(storage, settings):
    tuned = _embeddings_settings(settings)
    fake = _FakeEmbeddings(tuned)
    pet = _make_ko(storage, "alice", "Мой пёс Рекс любит мяч", title="Пёс")
    _make_ko(storage, "alice", "Портфель акций заметно вырос", title="Финансы")

    # Populate the index exactly as the worker would.
    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_embeddings("alice") == 2

    query = "нужен совет про питомца"
    with_dense = await HybridSearcher(storage, fake).search("alice", query, limit=10)
    ids = [item["id"] for item in with_dense["results"]]
    assert pet["id"] in ids
    top = next(item for item in with_dense["results"] if item["id"] == pet["id"])
    assert top["_embedding_score"] > 0.99

    # Control: with embeddings off, the lexically-disjoint note is filtered out.
    without_dense = await HybridSearcher(storage, None).search("alice", query, limit=10)
    assert pet["id"] not in [item["id"] for item in without_dense["results"]]


def _index_ko(storage, user_id, text, title):
    ko = _make_ko(storage, user_id, text, title=title)
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko["id"],
                "user_id": user_id,
                "model": "test-embed",
                "dim": 3,
                "source_version": ko["version"],
                "content_hash": "h",
                "vector": pack_vector(_concept_vector(text)),
            }
        ]
    )
    return ko


def test_get_user_embeddings_respects_limit(storage):
    for i, text in enumerate(("первый", "второй", "третий")):
        _index_ko(storage, "alice", text, f"K{i}")
    assert len(storage.get_user_embeddings("alice", "test-embed", 3)) == 3
    assert len(storage.get_user_embeddings("alice", "test-embed", 3, limit=2)) == 2


@pytest.mark.asyncio
async def test_dense_recall_caps_the_scan_and_flags_it(storage, settings):
    # The pure-Python cosine scan is the retrieval scaling ceiling; the guard caps
    # how many vectors are scored and makes the truncation visible, not silent.
    tuned = dataclasses.replace(_embeddings_settings(settings), embeddings_dense_max_objects=1)
    _index_ko(storage, "alice", "Мой пёс Рекс любит мяч", "Пёс")
    _index_ko(storage, "alice", "Портфель акций вырос", "Финансы")

    searcher = HybridSearcher(storage, _FakeEmbeddings(tuned))
    meta: dict = {}
    await searcher._dense_recall("alice", "нужен совет про питомца", {}, meta=meta)  # noqa: SLF001
    assert meta["dense_scanned"] == 1  # only the newest vector was scored, not both
    assert meta["dense_capped"] is True

    # The cap is surfaced in the search strategy (visible in the explain-trace).
    result = await searcher.search("alice", "нужен совет про питомца", limit=5)
    assert result["strategy"].get("embeddings_capped") is True


@pytest.mark.asyncio
async def test_dense_recall_falls_back_to_pool_when_index_empty(storage, settings):
    tuned = _embeddings_settings(settings)
    ko = _make_ko(storage, "alice", "Мой пёс Рекс любит мяч", title="Пёс")
    searcher = HybridSearcher(storage, _FakeEmbeddings(tuned))
    pool = {ko["id"]: ko}
    # No vectors indexed yet: fall back to scoring the existing pool directly.
    scores = await searcher._dense_recall("alice", "питомец", pool)  # noqa: SLF001
    assert scores[ko["id"]] > 0.99


# --- indexer worker -------------------------------------------------------


@pytest.mark.asyncio
async def test_indexer_embeds_then_is_idempotent_until_content_changes(storage, settings):
    tuned = _embeddings_settings(settings)
    fake = _FakeEmbeddings(tuned)
    ko = _make_ko(storage, "alice", "Мой пёс Рекс", title="Пёс")
    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)

    await manager._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_embeddings() == 1
    first_call_count = len(fake.calls)

    # Nothing stale: a second cycle does no embedding work.
    await manager._embeddings_index_all()  # noqa: SLF001
    assert len(fake.calls) == first_call_count

    # A content change re-stales the vector and the next cycle re-embeds it.
    storage.update_knowledge_fields(ko["id"], "alice", content="Теперь про кошку")
    await manager._embeddings_index_all()  # noqa: SLF001
    assert len(fake.calls) == first_call_count + 1
    assert storage.count_knowledge_embeddings() == 1


@pytest.mark.asyncio
async def test_indexer_is_disabled_when_backend_not_configured(storage, settings):
    # Default settings: embeddings disabled -> the real backend is not remote_enabled.
    backend = EmbeddingBackend(settings)
    assert backend.remote_enabled is False
    manager = WorkersManager(settings, storage, None, None, embeddings=backend)
    _make_ko(storage, "alice", "Мой пёс Рекс", title="Пёс")
    await manager._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_embeddings() == 0
