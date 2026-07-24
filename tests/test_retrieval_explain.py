"""Retrieval explain-trace — §11: a ranker you can trust is one you can inspect.

Every candidate is already scored; these tests pin that `search(explain=True)`
surfaces the full ranked set — returned (with rank + per-signal breakdown) and
discarded (with the exact reason the ranker applied) — plus the admin endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.retrieval import HybridSearcher
from jericho.server import create_app


async def _seed(settings, storage) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    await pipeline.ingest_text(
        "alice",
        "Сервер Atlas работает в дата-центре Москвы и обслуживает продакшн.",
        source_ref="atlas",
        force_knowledge=True,
    )
    await pipeline.ingest_text(
        "alice",
        "Рецепт борща: свёкла, капуста, картофель и немного лимона.",
        source_ref="borscht",
        force_knowledge=True,
    )


@pytest.mark.asyncio
async def test_explain_trace_reports_returned_and_discarded(settings, storage):
    await _seed(settings, storage)
    searcher = HybridSearcher(storage)

    result = await searcher.search("alice", "Сервер Москва", limit=1, explain=True)
    assert "trace" in result
    trace = result["trace"]
    assert trace

    returned = [row for row in trace if row["status"] == "returned"]
    assert returned and returned[0]["rank"] == 0
    assert "Atlas" in returned[0]["title"]
    # The per-signal breakdown is exposed for the returned hit.
    components = returned[0]["components"]
    assert {"lexical", "field", "graph", "lifecycle_factor"} <= set(components)

    # The unrelated note is in the recall pool but discarded with a concrete reason.
    discarded = [row for row in trace if row["status"] == "discarded"]
    assert discarded
    assert any(row["reason"] == "insufficient_evidence" for row in discarded)
    # Discarded candidates still carry a score and components (the "why").
    assert all(isinstance(row["score"], float) and row["components"] for row in discarded)


@pytest.mark.asyncio
async def test_plain_search_has_no_trace_key(settings, storage):
    await _seed(settings, storage)
    searcher = HybridSearcher(storage)
    result = await searcher.search("alice", "Сервер Москва", limit=5)
    assert "trace" not in result  # trace is opt-in; normal callers are unaffected


def test_retrieval_explain_endpoint(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        for content in (
            "Сервер Atlas работает в дата-центре Москвы.",
            "Рецепт борща со свёклой и капустой.",
        ):
            resp = client.post(
                "/api/ingest", json={"content": content, "force_knowledge": True}, headers=owner
            )
            assert resp.status_code == 200, resp.text

        response = client.get(
            "/api/admin/retrieval/explain",
            params={"q": "Сервер Москва", "user_id": LEGACY_OWNER_USER_ID, "limit": 1},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["query"] and body["returned"] >= 1
        assert body["candidates"] >= body["returned"]
        trace = body["trace"]
        assert any(row["status"] == "returned" for row in trace)
        assert trace[0]["components"]  # signal breakdown available to the admin
