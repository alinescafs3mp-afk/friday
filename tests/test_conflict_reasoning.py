"""Contradiction/lifecycle/recency signals must reach the model, plus dated-event
conflicts and configurable graph depth (§12).

Retrieved knowledge previously reached the LLM without any conflict, lifecycle, or
recency flag, so the model could not reason about stale or contradictory personal
facts. These tests pin the enriched context payload, a new dated-event conflict
predicate, and the graph-depth config knob.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jericho.agent_runtime import AgentRuntime
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import ActorContext
from jericho.retrieval import HybridSearcher
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, content: str, title: str) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
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


class _FakeSearcher:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, user_id, query, **kwargs):
        del user_id, query, kwargs
        return {"results": self._hits, "entity_matches": []}


class _CapturingLLM:
    enabled = True
    model = "capture"

    def __init__(self):
        self.context_payload = None

    async def chat(self, messages, **kwargs):
        del kwargs
        for item in messages:
            content = str(item.get("content") or "")
            if "JERICHO_CONTEXT_DATA" in content and "{" in content:
                self.context_payload = json.loads(content[content.index("{") :])
        return {"content": "Данные противоречат друг другу [K1] и [K2]."}


@pytest.mark.asyncio
async def test_conflict_lifecycle_and_recency_reach_the_prompt(settings, storage):
    storage.ensure_user("alice")
    old = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.5.", "Atlas IP (old)")
    new = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP (new)")
    storage.store_knowledge_conflict(
        "alice", old["id"], new["id"], conflict_type="address_mismatch", confidence=0.9
    )
    storage.update_knowledge_fields(old["id"], "alice", lifecycle_stage="deprecated")

    hits = [
        {**storage.get_knowledge_object(old["id"], "alice"), "_score": 0.9, "_entities": []},
        {**storage.get_knowledge_object(new["id"], "alice"), "_score": 0.85, "_entities": []},
    ]
    llm = _CapturingLLM()
    runtime = AgentRuntime(settings, storage, llm=llm)
    await runtime.chat(
        "alice",
        "какой IP у Atlas?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=_FakeSearcher(hits),
    )

    assert llm.context_payload is not None
    objects = {obj["citation"]: obj for obj in llm.context_payload["knowledge_objects"]}
    assert objects["K1"]["lifecycle_stage"] == "deprecated"
    assert objects["K2"]["lifecycle_stage"] == "active"
    assert objects["K1"]["updated_at"]
    # The pending conflict is surfaced on BOTH sides with the counterpart citation.
    assert objects["K1"]["conflict"]["type"] == "address_mismatch"
    assert objects["K1"]["conflict"]["with_citation"] == "K2"
    assert objects["K2"]["conflict"]["with_citation"] == "K1"


@pytest.mark.asyncio
async def test_scheduled_date_mismatch_is_a_conflict(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    await pipeline.ingest_text(
        "alice", "Запомни: конференция «DevConf» пройдёт 2024-06-12.", source_ref="date-a"
    )
    await pipeline.ingest_text(
        "alice", "Запомни: конференция «DevConf» пройдёт 2024-06-15.", source_ref="date-b"
    )
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert any(c["conflict_type"] == "scheduled_date_mismatch" for c in conflicts)


@pytest.mark.asyncio
async def test_same_date_in_different_format_is_not_a_conflict(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    await pipeline.ingest_text(
        "alice", "Запомни: конференция «DevConf» пройдёт 2024-06-12.", source_ref="fmt-a"
    )
    await pipeline.ingest_text(
        "alice", "Запомни: конференция «DevConf» пройдёт 12.06.2024.", source_ref="fmt-b"
    )
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert not any(c["conflict_type"] == "scheduled_date_mismatch" for c in conflicts)


class _FakeKG:
    def __init__(self):
        self.depth = None

    def context_for_query(self, user_id, query, *, seed_knowledge_ids, entity_limit, depth, knowledge_limit):
        del user_id, query, seed_knowledge_ids, entity_limit, knowledge_limit
        self.depth = depth
        return {"nodes": [], "relations": [], "knowledge_candidates": []}

    def search_entities(self, user_id, query, *, limit=5):
        del user_id, query, limit
        return []


@pytest.mark.asyncio
async def test_graph_depth_is_configurable_and_respects_the_relational_heuristic(storage):
    searcher = HybridSearcher(storage, graph_max_depth=3)

    relational = _FakeKG()
    await searcher.search("alice", "как связаны Orion и PostgreSQL", kg=relational)
    assert relational.depth == 3

    plain = _FakeKG()
    await searcher.search("alice", "расскажи про Orion", kg=plain)
    assert plain.depth == 1
