"""An identifier written at the end of a sentence must still be the same identifier.

The retrieval tokenizer lets ``. _ + # -`` continue a token so that ``file.txt``,
``BRK.A`` and ``scale_factor`` survive as units. That is right, but it also meant a
token ending a sentence kept the full stop — and since ``_identifier_coverage`` drops
any candidate whose query identifiers are not all present as whole tokens, a document
was unreachable by the exact identifier it contained.

Found by the retrieval bench, then reduced to two documents: FTS returned the hit and
``HybridSearcher`` returned nothing, reporting ``identifier_mismatch``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from jericho.knowledge_graph import KnowledgeGraph
from jericho.retrieval import HybridSearcher, tokens_of
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=f"sha256:{new_id('x')}",
            raw_content=content,
            content_type="text/plain",
            content_hash=new_id("h") * 2,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    stored = storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            entity_id=None,
            title=title,
            summary=content[:120],
            content=content,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    return stored.id


@pytest.mark.parametrize(
    "text,expected",
    [
        ("конец предложения.", ["конец", "предложения"]),
        ("подъём autovacuum_vacuum_scale_factor.", ["подъём", "autovacuum_vacuum_scale_factor"]),
        ("версия 1.2.3.", ["версия", "1.2.3"]),
        ("тире- и точка.", ["тире", "и", "точка"]),
        # Inside a token these characters are meaning, not punctuation.
        ("файл.txt и BRK.A", ["файл.txt", "и", "BRK.A"]),
        # C++ and C# genuinely end in those characters, so they are never stripped.
        ("C++ и C#", ["C++", "и", "C#"]),
    ],
)
def test_trailing_punctuation_is_not_part_of_a_token(text, expected):
    assert tokens_of(text) == expected


def test_an_identifier_ending_a_sentence_is_still_found(settings, storage):
    """The defect, end to end: FTS had the hit and the blend discarded it."""
    storage.ensure_user("alice", source="upload")
    target = _store(
        storage,
        "pg-vacuum.md",
        "Помог VACUUM FULL и подъём autovacuum_vacuum_scale_factor. Партиционировать по месяцам.",
    )
    _store(storage, "kazan.md", "Попробовать эчпочмак и дойти до Свияжска.")

    query = "autovacuum_vacuum_scale_factor"
    assert storage.search_knowledge("alice", query, limit=10), "precondition: FTS must match"

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(searcher.search("alice", query, limit=10, kg=KnowledgeGraph(storage)))["results"]

    assert [hit["id"] for hit in results] == [target]


def test_a_query_identifier_must_still_match_a_whole_token(settings, storage):
    """The discrimination the rule exists for is preserved.

    ``scale_factor`` is a substring of ``autovacuum_vacuum_scale_factor``, not the same
    identifier — the same reason a query for ``BRK.A`` must not be satisfied by
    ``XBRK.A``. Stripping trailing punctuation must not quietly turn identifier
    matching into substring matching.
    """
    storage.ensure_user("alice", source="upload")
    _store(storage, "pg-vacuum.md", "Подъём autovacuum_vacuum_scale_factor. Готово.")

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(searcher.search("alice", "scale_factor", limit=10, kg=KnowledgeGraph(storage)))[
        "results"
    ]

    assert results == []
