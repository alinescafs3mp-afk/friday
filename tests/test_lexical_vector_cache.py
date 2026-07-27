"""Reusing a lexical vector must never mean reusing a stale one.

`lexical_vector` runs once per candidate over that candidate's full body and
dominates a search: profiled on the 342-document stand it accounted for 69 of the
85 seconds spent inside `search`, and one call on a 205 KB body costs 48 ms. The
cache that existed lived for one request, so the same document was rebuilt from
scratch for the next question and the one after that. Caching across requests
took the stand's median search from 739 ms to 323 ms with the graph off.

The whole risk of that is staleness, so the key is (id, version, updated_at):
version moves on an edit and updated_at moves on anything that touches the row.
These tests are about the second half of «faster» — that it is still correct.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.retrieval import HybridSearcher
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make_ko(storage, user_id: str, title: str, content: str) -> str:
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
    )
    storage.store_knowledge_object(ko)
    return ko.id


@pytest.mark.asyncio
async def test_an_edited_document_is_found_by_its_new_words(storage):
    storage.ensure_user("alice")
    note = _make_ko(storage, "alice", "Записка", "Дежурство караула на этой неделе.")
    searcher = HybridSearcher(storage, None, record_usage=False)

    # Warm the cache on the old text.
    first = await searcher.search("alice", "дежурство караула", limit=5)
    assert note in {item["id"] for item in first["results"]}

    storage.update_knowledge_fields(note, "alice", content="Расписание отпусков на квартал.")

    found = await searcher.search("alice", "расписание отпусков", limit=5)
    assert note in {item["id"] for item in found["results"]}, "the search answered from a stale vector"


@pytest.mark.asyncio
async def test_repeating_a_question_gives_the_same_answer(storage):
    storage.ensure_user("alice")
    for index in range(5):
        _make_ko(storage, "alice", f"Документ {index}", f"Тело документа номер {index} про дежурства.")
    searcher = HybridSearcher(storage, None, record_usage=False)

    first = await searcher.search("alice", "дежурства документ", limit=5)
    second = await searcher.search("alice", "дежурства документ", limit=5)
    assert [item["id"] for item in first["results"]] == [item["id"] for item in second["results"]]
    assert [item["_score"] for item in first["results"]] == [item["_score"] for item in second["results"]]


@pytest.mark.asyncio
async def test_the_cache_is_bounded(storage):
    """A cache that grows with the corpus is a leak with a good excuse."""
    from jericho.retrieval import _VECTOR_CACHE_MAX

    storage.ensure_user("alice")
    for index in range(12):
        _make_ko(storage, "alice", f"Заметка {index}", f"Содержимое {index} про караул и смены.")
    searcher = HybridSearcher(storage, None, record_usage=False)
    searcher._vector_cache.clear()  # noqa: SLF001

    await searcher.search("alice", "караул смены", limit=10)
    assert 0 < len(searcher._vector_cache) <= _VECTOR_CACHE_MAX  # noqa: SLF001
