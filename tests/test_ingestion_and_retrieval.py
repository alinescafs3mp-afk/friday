from __future__ import annotations

import pytest

from jericho.ingestion import IngestionPipeline, _extract_entities
from jericho.knowledge_graph import KnowledgeGraph
from jericho.retrieval import HybridSearcher, best_snippet


def test_entity_extraction_uses_boundaries_and_explicit_markers():
    entities = _extract_entities(
        "Project Alpha is led by Ivan Petrov and Maria Sidorova at company Google. "
        "The conference called DevFest takes place in San Francisco."
    )
    by_name = {item["name"]: item for item in entities}
    assert "Alpha" in by_name
    assert "Ivan Petrov" in by_name
    assert "Maria Sidorova" in by_name
    assert "Google" in by_name
    assert "DevFest" in by_name
    assert "San Francisco" in by_name
    assert "Alpha is" not in by_name
    assert "cal" not in by_name


@pytest.mark.asyncio
async def test_vertical_ingestion_graph_retrieval_and_idempotency(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)
    text = (
        "Project Alpha is an important knowledge management project. "
        "The company Google supports the conference called DevFest in San Francisco."
    )
    first = await pipeline.ingest_text(
        "alice",
        text,
        source="telegram",
        source_ref="message-42",
        force_knowledge=True,
    )
    assert first["promoted"] is True
    ko = first["knowledge_object"]
    assert ko["raw_object_id"] == first["raw_object_id"]
    assert first["graph_links"]
    assert storage.get_inbox_item(first["inbox_id"], "alice") is not None
    assert graph.get_stats("alice")["knowledge_object_count"] == 1

    replay = await pipeline.ingest_text(
        "alice",
        text,
        source="telegram",
        source_ref="message-42",
        force_knowledge=True,
    )
    assert replay["idempotent_replay"] is True
    assert replay["raw_object_id"] == first["raw_object_id"]
    assert graph.get_stats("alice")["knowledge_object_count"] == 1

    searcher = HybridSearcher(storage)
    result = await searcher.search("alice", "DevFest knowledge", kg=graph)
    assert result["count"] >= 1
    assert result["results"][0]["id"] == ko["id"]
    assert result["strategy"]["feedback"] is True
    assert await searcher.search("bob", "DevFest", kg=graph) == {
        "query": "DevFest",
        "results": [],
        "count": 0,
        "entity_matches": [],
        "strategy": {
            "fts": True,
            "lexical": True,
            "embeddings": False,
            "feedback": True,
            "graph": True,
        },
    }


def test_best_snippet_returns_query_matched_passage_not_head():
    head = "нейтральный вводный текст без ключевых слов. " * 12  # ~530 chars, no query terms
    fact = "IP сервера Atlas равен 10.0.0.7 в дата-центре Москвы."
    text = head + fact + " " + ("прочий хвост. " * 40)

    snippet = best_snippet("IP сервера Atlas", text, max_chars=200)

    # The matched passage is surfaced, not the (irrelevant) document head.
    assert "Atlas" in snippet and "10.0.0.7" in snippet
    assert snippet.startswith("…")  # a middle window, not the head
    assert len(snippet) <= 202  # max_chars + the two ellipses


def test_best_snippet_short_text_and_no_match_fallbacks():
    assert best_snippet("что угодно", "короткий текст") == "короткий текст"
    long_unmatched = "ааааа " * 200
    fallback = best_snippet("zzz", long_unmatched, max_chars=100)
    assert fallback.startswith("а") and fallback.endswith("…") and len(fallback) <= 101
