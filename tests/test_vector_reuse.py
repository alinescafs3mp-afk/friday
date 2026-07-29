"""The same text, the same model — the vector already exists somewhere.

`get_reusable_vectors` asked "has THIS object embedded this text before", which
covers a re-index and nothing else. A real archive is full of the same text twice:
one folder of 342 working documents held 13 groups of byte-identical files, 29
objects, each paying the embedding service for a vector that was already stored.
Importing a folder that was imported before is the same case taken to the limit —
every object a duplicate, and the whole import re-embedded from scratch.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.dedup import pack_vector
from jericho.storage.models import KnowledgeObject, RawObject, new_id

MODEL = "test-embed"


def _store(storage, user_id: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"), user_id=user_id, raw_object_id=raw.id, content=text, title=text[:40]
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_vector_is_found_by_its_text_not_by_its_owner(storage):
    storage.ensure_user("alice")
    first = _store(storage, "alice", "Ведомость на выдачу имущества, экземпляр первый.")
    text = "Ведомость на выдачу имущества, экземпляр первый."
    storage.upsert_knowledge_vectors(
        [
            {
                "knowledge_object_id": first,
                "user_id": "alice",
                "model": MODEL,
                "dim": 3,
                "source_version": 1,
                "content_hash": _digest(text),
                "vector": pack_vector([0.1, 0.2, 0.3]),
                "chunk_scheme": "v1:1200:200:64",
            }
        ]
    )

    found = storage.get_vectors_by_content_hash([_digest(text)], MODEL)
    assert _digest(text) in found

    # The same lookup from a DIFFERENT object's point of view still finds it — that
    # is the whole point, and `get_reusable_vectors` keyed by object does not.
    second = _store(storage, "alice", text)
    assert not storage.get_reusable_vectors([second], MODEL).get(second)
    assert _digest(text) in storage.get_vectors_by_content_hash([_digest(text)], MODEL)


def test_a_different_model_never_shares_a_vector(storage):
    """Same text under another model is a different vector; sharing it would be silent."""
    storage.ensure_user("alice")
    text = "Приказ об утверждении графика дежурств."
    ko = _store(storage, "alice", text)
    storage.upsert_knowledge_vectors(
        [
            {
                "knowledge_object_id": ko,
                "user_id": "alice",
                "model": MODEL,
                "dim": 3,
                "source_version": 1,
                "content_hash": _digest(text),
                "vector": pack_vector([0.4, 0.5, 0.6]),
                "chunk_scheme": "v1:1200:200:64",
            }
        ]
    )
    assert storage.get_vectors_by_content_hash([_digest(text)], "another-model") == {}


def test_an_unknown_text_returns_nothing_rather_than_something_close(storage):
    storage.ensure_user("alice")
    assert storage.get_vectors_by_content_hash([_digest("никогда не встречался")], MODEL) == {}
    assert storage.get_vectors_by_content_hash([], MODEL) == {}


@pytest.mark.asyncio
async def test_the_indexer_does_not_pay_twice_for_the_same_text(settings, storage):
    """Through the worker, counting what the embedding service is actually asked for.

    The unit tests above prove the lookup works; only this one fails when the indexer
    stops calling it. The first version of this change shipped with the wiring
    untested — the mutation that removed the call left every other test green.
    """
    from dataclasses import replace

    from jericho.workers import WorkersManager

    calls: list[list[str]] = []

    class _CountingBackend:
        remote_enabled = True

        async def embed(self, texts, *, budget_sec=None):
            calls.append(list(texts))
            return [[0.1, 0.2, 0.3] for _ in texts]

    tuned = replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9/v1",
        embeddings_model=MODEL,
    )
    storage.ensure_user("alice")
    text = "Ведомость на выдачу имущества, экземпляр первый, страница первая."
    _store(storage, "alice", text)
    manager = WorkersManager(tuned, storage, None, None, embeddings=_CountingBackend())

    await manager._embeddings_index_all()  # noqa: SLF001
    first_round = sum(len(batch) for batch in calls)
    assert first_round, "nothing was embedded at all"

    # The same text arrives again as a different object — a file copied to a second
    # folder, or the same folder imported twice.
    calls.clear()
    _store(storage, "alice", text)
    await manager._embeddings_index_all()  # noqa: SLF001

    assert sum(len(batch) for batch in calls) == 0, (
        f"the service was asked for {sum(len(b) for b in calls)} vectors that already existed"
    )
