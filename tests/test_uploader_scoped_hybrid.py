"""Exact uploader scope reaches every active HybridSearcher candidate lane."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3

import pytest

from friday import retrieval as retrieval_module
from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES, bounded_raw_file_metadata
from friday.retrieval import HybridSearcher, pack_vector
from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject, new_id


class _SyntheticEmbeddings:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.remote_enabled = True

    async def embed(self, texts, *, budget_sec=None):
        del budget_sec
        return [[1.0, 0.0, 0.0] for _ in texts]


def _settings(settings):
    return dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9/v1",
        embeddings_model="synthetic-author-scope",
        embeddings_recall_candidates=4,
        embeddings_dense_max_objects=1,
        embeddings_resident_cache=True,
        embeddings_chunk_chars=128,
        embeddings_chunk_max_per_object=4,
        embeddings_chunk_scan_multiplier=4,
    )


def _knowledge(
    storage,
    tenant: str,
    *,
    author: object,
    content: str,
    title: str,
    created_at: str,
    document_date: str = "",
) -> tuple[str, str]:
    metadata = {"uploaded_by": author}
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="synthetic",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256((title + content).encode()).hexdigest(),
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=tenant,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
        metadata_json={"document_date": document_date} if document_date else {},
        created_at=created_at,
    )
    storage.store_knowledge_object(ko)
    return ko.id, raw.id


def _vectors(storage, tenant: str, ids: list[str]) -> None:
    whole = []
    chunks = {}
    for index, ko_id in enumerate(ids):
        whole.append(
            {
                "knowledge_object_id": ko_id,
                "user_id": tenant,
                "model": "synthetic-author-scope",
                "dim": 3,
                "source_version": 1,
                "content_hash": f"whole-{index}",
                "vector": pack_vector([1.0, 0.0, 0.0]),
            }
        )
        chunks[ko_id] = [
            {
                "chunk_index": 0,
                "user_id": tenant,
                "model": "synthetic-author-scope",
                "dim": 3,
                "source_version": 1,
                "chunk_scheme": "synthetic",
                "start_char": 0,
                "end_char": 20,
                "content_hash": f"chunk-{index}",
                "vector": pack_vector([1.0, 0.0, 0.0]),
            }
        ]
    storage.upsert_knowledge_vectors(whole, chunks)


@pytest.mark.asyncio
async def test_scoped_hybrid_rescues_dense_target_before_foreign_caps_and_models(
    storage, settings, monkeypatch
):
    tenant = "shared-synthetic"
    storage.ensure_user(tenant)
    target_id, _ = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="Запись о собаке без слов пользовательского вопроса.",
        title="Целевой документ",
        created_at="2020-01-01T00:00:00Z",
    )
    foreign_ids = []
    for index in range(12):
        ko_id, _ = _knowledge(
            storage,
            tenant,
            author="foreign-author",
            content="FOREIGN_SCOPE_SENTINEL нужен совет про питомца " * 4,
            title=f"Чужой {index:02d}",
            created_at=f"2026-01-{index + 1:02d}T00:00:00Z",
        )
        foreign_ids.append(ko_id)
    _vectors(storage, tenant, [target_id, *foreign_ids])

    reranker_inputs: list[list[str]] = []

    async def reranker(_query: str, items: list[dict]):
        reranker_inputs.append([str(item["id"]) for item in items])
        forged = dict(items[0])
        forged["content"] = "FORGED_RERANK_BODY"
        forged["_rerank_score"] = 1.0
        return [forged]

    class _ForbiddenTenantCache:
        def get(self, *_args, **_kwargs):
            raise AssertionError("author-scoped search touched the tenant resident cache")

    class _ForbiddenGraph:
        def context_for_query(self, *_args, **_kwargs):
            raise AssertionError("author-scoped search touched the shared graph")

        def search_entities(self, *_args, **_kwargs):
            raise AssertionError("author-scoped search touched shared entities")

    tuned = _settings(settings)
    real_dense_scores = retrieval_module.dense_scores

    def off_loop_dense_scores(*args, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return real_dense_scores(*args, **kwargs)

    monkeypatch.setattr(retrieval_module, "dense_scores", off_loop_dense_scores)
    searcher = HybridSearcher(
        storage,
        _SyntheticEmbeddings(tuned),
        chunk_recall=False,
        reranker=reranker,
        rerank_top=8,
        rerank_confident_min=0.1,
        pool_max=2,
    )
    searcher._dense_cache = _ForbiddenTenantCache()  # noqa: SLF001 - privacy mutation seam

    def forbidden_entity_links(*_args, **_kwargs):
        raise AssertionError("author-scoped search touched shared entity names")

    searcher._entity_links_by_document = forbidden_entity_links  # type: ignore[method-assign]  # noqa: SLF001
    result = await searcher.search(
        tenant,
        "нужен совет про питомца",
        limit=4,
        uploaded_by="target-author",
        kg=_ForbiddenGraph(),
        include_entities=True,
        graph_expansion=True,
        explain=True,
        record_usage=False,
    )

    assert [item["id"] for item in result["results"]] == [target_id]
    assert reranker_inputs == [[target_id]]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "FOREIGN_SCOPE_SENTINEL" not in encoded
    assert "FORGED_RERANK_BODY" not in encoded
    assert result["strategy"]["uploader_scoped"] is True
    assert result["strategy"]["graph"] is False
    assert result["strategy"]["graph_author_scope_disabled"] is True


@pytest.mark.asyncio
async def test_scoped_chunk_lane_alone_carries_a_late_passage_before_foreign_caps(storage, settings):
    tenant = "shared-chunk-synthetic"
    storage.ensure_user(tenant)
    header = "synthetic header without matching vocabulary " * 30
    evidence = "LATE_CHUNK_EVIDENCE describes the hidden synthetic fact."
    target_id, _ = _knowledge(
        storage,
        tenant,
        author="target-author",
        content=header + evidence,
        title="Late passage",
        created_at="2020-01-01T00:00:00Z",
    )
    foreign_ids = []
    for index in range(6):
        ko_id, _ = _knowledge(
            storage,
            tenant,
            author="foreign-author",
            content="FOREIGN_SCOPE_SENTINEL нужен совет про питомца",
            title=f"Foreign chunk {index}",
            created_at=f"2026-02-{index + 1:02d}T00:00:00Z",
        )
        foreign_ids.append(ko_id)

    ids = [target_id, *foreign_ids]
    whole = []
    chunks = {}
    for ko_id in ids:
        is_target = ko_id == target_id
        whole.append(
            {
                "knowledge_object_id": ko_id,
                "user_id": tenant,
                "model": "synthetic-author-scope",
                "dim": 3,
                "source_version": 1,
                "content_hash": f"whole-{ko_id}",
                # The target's document vector cannot carry it; only its passage can.
                "vector": pack_vector([0.0, 1.0, 0.0] if is_target else [1.0, 0.0, 0.0]),
            }
        )
        start = len(header) if is_target else 0
        chunks[ko_id] = [
            {
                "chunk_index": 0,
                "user_id": tenant,
                "model": "synthetic-author-scope",
                "dim": 3,
                "source_version": 1,
                "chunk_scheme": "synthetic",
                "start_char": start,
                "end_char": start + (len(evidence) if is_target else 20),
                "content_hash": f"chunk-{ko_id}",
                "vector": pack_vector([1.0, 0.0, 0.0]),
            }
        ]
    storage.upsert_knowledge_vectors(whole, chunks)

    tuned = _settings(settings)
    result = await HybridSearcher(storage, _SyntheticEmbeddings(tuned), pool_max=1).search(
        tenant,
        "нужен совет про питомца",
        limit=2,
        uploaded_by="target-author",
        record_usage=False,
    )
    hit = next(item for item in result["results"] if item["id"] == target_id)
    assert hit["_embedding_chunk"] == 0
    assert hit["_embedding_chunk_span"] == [len(header), len(header) + len(evidence)]
    assert result["strategy"]["embeddings_chunked"] is True
    assert "FOREIGN_SCOPE_SENTINEL" not in json.dumps(result, ensure_ascii=False)

    control = await HybridSearcher(
        storage,
        _SyntheticEmbeddings(tuned),
        chunk_recall=False,
        pool_max=1,
    ).search(
        tenant,
        "нужен совет про питомца",
        limit=2,
        uploaded_by="target-author",
        record_usage=False,
    )
    assert target_id not in {item["id"] for item in control["results"]}


def test_every_storage_cap_uses_the_same_exact_uploader(storage, monkeypatch):
    tenant = "shared-storage-synthetic"
    storage.ensure_user(tenant)
    target_id, _ = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="target-only-token",
        title="Target",
        created_at="2020-01-01T00:00:00Z",
        document_date="2024-03-10",
    )
    foreign_id, _ = _knowledge(
        storage,
        tenant,
        author="foreign-author",
        content="target-only-token FOREIGN_SCOPE_SENTINEL",
        title="Foreign",
        created_at="2026-01-01T00:00:00Z",
        document_date="2024-03-11",
    )
    _vectors(storage, tenant, [target_id, foreign_id])
    monkeypatch.setattr(storage, "_WINDOW_IDS_MAX", 1)

    assert [
        row["id"]
        for row in storage.search_knowledge(tenant, "target-only-token", limit=1, uploaded_by="target-author")
    ] == [target_id]
    assert [
        row["id"] for row in storage.list_knowledge_objects(tenant, limit=1, uploaded_by="target-author")
    ] == [target_id]
    assert storage.knowledge_ids_in_window(
        tenant,
        since="2024-03-01",
        until="2024-03-31",
        uploaded_by="target-author",
    ) == {target_id}
    assert storage.count_knowledge_objects(tenant, uploaded_by="target-author") == 1
    assert storage.get_knowledge_object(foreign_id, tenant, uploaded_by="target-author") is None
    assert [
        row[0]
        for row in storage.get_user_embeddings(
            tenant,
            "synthetic-author-scope",
            3,
            limit=1,
            uploaded_by="target-author",
        )
    ] == [target_id]
    assert [
        row[0].split("#", 1)[0]
        for row in storage.get_user_chunk_embeddings(
            tenant,
            "synthetic-author-scope",
            3,
            object_limit=1,
            row_limit=4,
            uploaded_by="target-author",
        )
    ] == [target_id]
    assert (
        storage.get_chunk_spans(
            tenant,
            "synthetic-author-scope",
            [(foreign_id, 0)],
            uploaded_by="target-author",
        )
        == {}
    )
    assert storage.get_chunk_spans(
        tenant,
        "synthetic-author-scope",
        [(target_id, 0)],
        uploaded_by="target-author",
    ) == {(target_id, 0): (0, 20)}

    other_tenant = "foreign-raw-tenant"
    storage.ensure_user(other_tenant)
    _, mismatched_raw = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="mismatched-raw-tenant-needle",
        title="Mismatched Raw tenant",
        created_at="2026-01-02T00:00:00Z",
    )
    # Managed writes forbid this state. Simulate a malformed legacy/external row
    # to prove the read boundary checks both denormalised tenant authorities.
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET user_id=? WHERE id=?", (other_tenant, mismatched_raw))
    assert (
        storage.search_knowledge(
            tenant,
            "mismatched-raw-tenant-needle",
            uploaded_by="target-author",
        )
        == []
    )

    # Explicit None is the backwards-compatible path, byte/order identical to an
    # omitted scope on every vector/list method changed by this release.
    assert storage.list_knowledge_objects(tenant, limit=20) == storage.list_knowledge_objects(
        tenant, limit=20, uploaded_by=None
    )
    assert storage.get_user_embeddings(tenant, "synthetic-author-scope", 3) == storage.get_user_embeddings(
        tenant, "synthetic-author-scope", 3, uploaded_by=None
    )
    assert storage.get_user_chunk_embeddings(
        tenant, "synthetic-author-scope", 3
    ) == storage.get_user_chunk_embeddings(tenant, "synthetic-author-scope", 3, uploaded_by=None)


def test_ignored_source_cannot_fill_scoped_fts_or_dense_caps(storage):
    tenant = "shared-ignored-cap-synthetic"
    author = "target-author"
    storage.ensure_user(tenant)
    target_id, target_raw = _knowledge(
        storage,
        tenant,
        author=author,
        content="common ignored-cap marker valid source",
        title="Valid older source",
        created_at="2020-01-01T00:00:00Z",
    )
    ignored_id, ignored_raw = _knowledge(
        storage,
        tenant,
        author=author,
        content="common ignored-cap marker rejected source",
        title="Ignored newer source",
        created_at="2026-01-01T00:00:00Z",
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant,
            raw_object_id=target_raw,
            knowledge_object_id=target_id,
            status=InboxStatus.CLASSIFIED,
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant,
            raw_object_id=ignored_raw,
            knowledge_object_id=ignored_id,
            status=InboxStatus.IGNORED,
        )
    )
    _vectors(storage, tenant, [target_id, ignored_id])

    assert [
        row["id"]
        for row in storage.search_knowledge(
            tenant,
            "common ignored-cap marker",
            limit=1,
            uploaded_by=author,
        )
    ] == [target_id]
    assert [
        row[0]
        for row in storage.get_user_embeddings(
            tenant,
            "synthetic-author-scope",
            3,
            limit=1,
            uploaded_by=author,
        )
    ] == [target_id]
    assert [
        row[0].split("#", 1)[0]
        for row in storage.get_user_chunk_embeddings(
            tenant,
            "synthetic-author-scope",
            3,
            object_limit=1,
            row_limit=4,
            uploaded_by=author,
        )
    ] == [target_id]


def test_ambiguous_or_unbounded_raw_metadata_belongs_to_nobody(storage):
    tenant = "shared-malformed-synthetic"
    storage.ensure_user(tenant)

    oversized_id, oversized_raw = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="oversized-needle",
        title="Oversized",
        created_at="2026-01-01T00:00:00Z",
    )
    oversized = {
        "uploaded_by": "target-author",
        "padding": "X" * RAW_FILE_METADATA_MAX_BYTES,
    }
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (json.dumps(oversized, separators=(",", ":")), oversized_raw),
        )

    duplicate_id, duplicate_raw = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="duplicate-needle",
        title="Duplicate",
        created_at="2026-01-02T00:00:00Z",
    )
    duplicate = '{"uploaded_by":"target-author","uploaded_by":"foreign-author"}'
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET metadata_json=? WHERE id=?", (duplicate, duplicate_raw))

    blob_id, blob_raw = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="blob-needle",
        title="Blob",
        created_at="2026-01-03T00:00:00Z",
    )
    blob = sqlite3.Binary(b'{"uploaded_by":"target-author"}')
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET metadata_json=? WHERE id=?", (blob, blob_raw))

    malformed_id, malformed_raw = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="malformed-needle",
        title="Malformed",
        created_at="2026-01-04T00:00:00Z",
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET metadata_json=? WHERE id=?", ('{"uploaded_by":', malformed_raw))

    array_id, array_raw = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="array-root-needle",
        title="Array root",
        created_at="2026-01-05T00:00:00Z",
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            ('[{"uploaded_by":"target-author"}]', array_raw),
        )

    for ko_id, raw_id, needle in (
        (oversized_id, oversized_raw, "oversized-needle"),
        (duplicate_id, duplicate_raw, "duplicate-needle"),
        (blob_id, blob_raw, "blob-needle"),
        (malformed_id, malformed_raw, "malformed-needle"),
        (array_id, array_raw, "array-root-needle"),
    ):
        raw = storage.execute(
            "SELECT metadata_json FROM raw_objects WHERE id=? AND user_id=?",
            (raw_id, tenant),
        ).fetchone()
        assert raw is not None
        assert bounded_raw_file_metadata(raw["metadata_json"]) == {}
        assert (
            storage.search_knowledge(
                tenant,
                needle,
                uploaded_by="target-author",
            )
            == []
        ), ko_id

    empty_id, _ = _knowledge(
        storage,
        tenant,
        author="",
        content="empty-author-needle",
        title="Empty author",
        created_at="2026-01-06T00:00:00Z",
    )
    assert storage.search_knowledge(tenant, "empty-author-needle", uploaded_by="") == []
    assert storage.list_knowledge_objects(tenant, uploaded_by="") == []
    assert storage.list_knowledge_objects(tenant, uploaded_by="   ") == []
    assert storage.count_knowledge_objects(tenant, uploaded_by="") == 0
    assert storage.get_knowledge_object(empty_id, tenant, uploaded_by="") is None


@pytest.mark.asyncio
async def test_hostile_reranker_cannot_replace_the_scoped_set(storage, settings):
    tenant = "shared-rerank-synthetic"
    storage.ensure_user(tenant)
    first, _ = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="common synthetic first",
        title="First",
        created_at="2026-01-01T00:00:00Z",
    )
    second, _ = _knowledge(
        storage,
        tenant,
        author="target-author",
        content="common synthetic second",
        title="Second",
        created_at="2026-01-02T00:00:00Z",
    )

    async def replacement(_query: str, items: list[dict]):
        assert {item["id"] for item in items} == {first, second}
        items[0]["id"] = "ko_foreign_replacement"
        items[0]["content"] = "FOREIGN_SCOPE_SENTINEL"
        return items

    result = await HybridSearcher(
        storage,
        None,
        reranker=replacement,
        rerank_top=4,
        rerank_confident_min=0.0,
    ).search(
        tenant,
        "common synthetic",
        uploaded_by="target-author",
        record_usage=False,
    )
    assert {item["id"] for item in result["results"]} == {first, second}
    assert "FOREIGN_SCOPE_SENTINEL" not in json.dumps(result, ensure_ascii=False)
