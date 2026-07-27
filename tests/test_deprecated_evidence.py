"""`deprecated_weak` must weigh EVERY kind of evidence, not just lexical.

A deprecated record already pays for its stage in the blend (`lifecycle_factor`
0.36), and DATA_LIFECYCLE promises it «остаётся в поиске» with a demoted rank.
The gate, however, once looked only at FTS membership, `lex` and `grp` — while
`emb` and `fld` sat unread in its own parameter list. Two lines above,
`insufficient_evidence` had just accepted a dense cosine or a curated-field match
as full proof; then this gate deleted the object without looking at either.
`lex >= 0.25` could not save it: measured on a real corpus, p99 lexical is 0.153.

Net effect: a deprecated note recalled BY MEANING — the exact case dense recall
exists for — was unreturnable, always. These tests pin the repaired contract.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math

import pytest

from jericho.retrieval import HybridSearcher
from jericho.storage.models import KnowledgeObject, RawObject, new_id
from jericho.workers import WorkersManager

# One semantic axis per topic; the query scores ~1.0 on "pet" without sharing a
# single surface token with the document (same trick as test_chunk_recall).
_TOPICS: dict[str, tuple[str, ...]] = {
    "pet": ("пёс", "рекс", "корм", "собак", "миск", "питом"),
    "tax": ("налог", "декларац", "вычет"),
}

QUERY = "что любит мой питомец"


def _topic_vector(text: str) -> list[float]:
    lowered = text.lower()
    counts = [float(sum(lowered.count(word) for word in words)) for words in _TOPICS.values()]
    norm = math.sqrt(sum(value * value for value in counts))
    return [value / norm for value in counts] if norm else [0.0] * len(_TOPICS)


class _FakeTopicEmbeddings:
    def __init__(self, settings):
        self.settings = settings
        self.remote_enabled = True

    async def embed(self, texts):
        return [_topic_vector(text) for text in texts]


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
        summary="",
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


def _embeddings_settings(settings):
    return dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9999/v1",
        embeddings_model="test-embed",
    )


@pytest.mark.asyncio
async def test_deprecated_object_recalled_by_meaning_is_returned(storage, settings):
    """Dense evidence must be able to carry a deprecated object into the results."""
    tuned = _embeddings_settings(settings)
    fake = _FakeTopicEmbeddings(tuned)
    note = _make_ko(
        storage,
        "alice",
        "Пёс Рекс ест корм из миски. Собака обожает корм.",
        title="Записка",
    )
    storage.update_knowledge_fields(note["id"], "alice", lifecycle_stage="deprecated")

    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001

    # Preconditions of the defect: the query shares no token with the body, so
    # lexical evidence alone cannot reach the old `lex >= 0.25` bar…
    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5, explain=True)
    row = next(entry for entry in result["trace"] if entry["id"] == note["id"])
    assert row["components"]["lexical"] < 0.25
    # …while the dense channel is unambiguous.
    assert row["components"]["embedding"] > 0.9

    assert note["id"] in {hit["id"] for hit in result["results"]}, (
        f"deprecated object with dense evidence was excluded: reason={row['reason']}"
    )


@pytest.mark.asyncio
async def test_deprecated_object_without_evidence_stays_gated(storage, settings):
    """The gate itself must survive the fix: no evidence — no slot, even in-pool."""
    tuned = _embeddings_settings(settings)
    fake = _FakeTopicEmbeddings(tuned)
    note = _make_ko(
        storage,
        "alice",
        "Налоговая декларация и вычет за прошлый год.",
        title="Налоги",
    )
    storage.update_knowledge_fields(note["id"], "alice", lifecycle_stage="deprecated")

    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001

    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5, explain=True)
    assert note["id"] not in {hit["id"] for hit in result["results"]}
    row = next(entry for entry in result["trace"] if entry["id"] == note["id"])
    assert row["reason"] in {"deprecated_weak", "insufficient_evidence"}


@pytest.mark.asyncio
async def test_deprecated_ranks_below_an_active_twin(storage, settings):
    """Returning deprecated records is a demotion contract, not an equality one."""
    tuned = _embeddings_settings(settings)
    fake = _FakeTopicEmbeddings(tuned)
    active = _make_ko(storage, "alice", "Пёс Рекс ест корм из миски каждый день.", title="Живая записка")
    deprecated = _make_ko(
        storage, "alice", "Пёс Рекс ест корм из миски каждый вечер.", title="Старая записка"
    )
    storage.update_knowledge_fields(deprecated["id"], "alice", lifecycle_stage="deprecated")

    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001

    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5)
    scores = {hit["id"]: hit["_score"] for hit in result["results"]}
    assert active["id"] in scores and deprecated["id"] in scores
    assert scores[active["id"]] > scores[deprecated["id"]]
