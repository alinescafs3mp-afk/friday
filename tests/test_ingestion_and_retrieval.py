from __future__ import annotations

import pytest

from jericho.ingestion import IngestionPipeline, _extract_entities
from jericho.knowledge_graph import KnowledgeGraph
from jericho.retrieval import HybridSearcher


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
