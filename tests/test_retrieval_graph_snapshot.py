"""The graph that ranked search is the bounded graph search returns.

This is the retrieval/API seam of proposal 26.  Traversal may use full storage
rows internally, but neither the agent nor ``GET /api/search`` may receive that
working set, recompute a different path, or silently turn a malformed historical
date into a current graphless answer.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from friday.retrieval import HybridSearcher, _public_graph_context
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _knowledge(storage, text: str = "Atlas production notes") -> str:
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    item = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Atlas",
    )
    storage.store_knowledge_object(item)
    return item.id


class _SnapshotGraph:
    def __init__(self, knowledge_id: str) -> None:
        self.knowledge_id = knowledge_id
        self.calls: list[dict] = []

    def context_for_query(self, user_id, query, **kwargs):
        self.calls.append({"user_id": user_id, "query": query, **kwargs})
        nodes = [
            {
                "id": f"ent_{index}",
                "name": f"Entity {index}",
                "entity_type": "project",
                "_graph_depth": min(index, 4),
                "_graph_score": 1.0 / (index + 1),
                "description": f"secret node body {index}",
                "metadata_json": {"secret": index},
            }
            for index in range(15)
        ]
        relations = [
            {
                "id": f"rel_{index}",
                "source_entity_id": "ent_0",
                "target_entity_id": f"ent_{index + 1}",
                "source_name": "Entity 0",
                "target_name": f"Entity {index + 1}",
                "relation_type": "depends_on",
                "weight": 0.8,
                "valid_from": "2020-01-01",
                "valid_to": "",
                "metadata_json": {"secret": index},
                "evidence": "secret relation body",
            }
            for index in range(25)
        ]
        paths = [
            {
                "path_id": f"path_{index}",
                "root": "ent_0",
                "target": f"ent_{index + 1}",
                "score": 0.8 - index / 100,
                "entity_ids": ["ent_0", f"ent_{index + 1}"],
                "edges": [
                    {
                        "id": f"rel_{index}",
                        "from": "ent_0",
                        "to": f"ent_{index + 1}",
                        "direction": "forward",
                        "source": "ent_0",
                        "target": f"ent_{index + 1}",
                        "type": "depends_on",
                        "weight": 0.8,
                        "implicit": False,
                        "valid_from": "2020-01-01",
                        "valid_to": "",
                        "created_at": "2020-01-02T00:00:00+00:00",
                        "provenance": {
                            "kind": "reviewed_knowledge",
                            "knowledge_object_id": self.knowledge_id,
                            "secret": "raw provenance body",
                        },
                        "metadata_json": {"secret": index},
                    }
                ],
                "metadata_json": {"secret": index},
            }
            for index in range(13)
        ]
        return {
            "as_of": kwargs.get("as_of", ""),
            "temporal_basis": "valid_time",
            "roots": nodes[:1],
            "nodes": nodes,
            "entities": nodes,
            "relations": relations,
            "paths": paths,
            "paths_matched_at_least": 13,
            "paths_truncated": True,
            "knowledge_candidates": [
                {
                    "knowledge_object_id": self.knowledge_id,
                    "score": 0.9,
                    "query_matched": True,
                    "evidence": [{"path_id": "path_0"}],
                }
            ],
            "raw_document_body": "must never leave retrieval",
        }

    def search_entities(self, user_id, query, *, limit=5):
        del user_id, query, limit
        return []


@pytest.mark.asyncio
async def test_search_returns_the_same_bounded_temporal_graph_snapshot(storage):
    knowledge_id = _knowledge(storage)
    graph = _SnapshotGraph(knowledge_id)

    result = await HybridSearcher(storage, record_usage=False).search(
        "alice", "Atlas", kg=graph, as_of="2024/03/05"
    )

    assert graph.calls[0]["as_of"] == "2024-03-05"
    assert result["as_of"] == "2024-03-05"
    context = result["graph_context"]
    assert context["query"] == "Atlas"
    assert context["as_of"] == "2024-03-05"
    assert context["temporal_basis"] == "valid_time"
    assert context["expanded"] is True
    assert len(context["nodes"]) == len(context["entities"]) == 12
    assert len(context["relations"]) == 20
    assert len(context["paths"]) == 10
    assert context["paths_matched_at_least"] == 13
    assert context["paths_truncated"] is True
    assert context["paths"][0]["edges"][0]["direction"] == "forward"
    assert context["paths"][0]["edges"][0]["provenance"] == {
        "kind": "reviewed_knowledge",
        "knowledge_object_id": knowledge_id,
    }
    serialized = json.dumps(context, ensure_ascii=False)
    assert "metadata_json" not in serialized
    assert "secret" not in serialized
    assert "raw_document_body" not in serialized
    assert "knowledge_candidates" not in context
    whole_response = json.dumps(result, ensure_ascii=False)
    assert "secret" not in whole_response
    assert result["entity_matches"] == context["nodes"][:5]
    assert "metadata_json" not in json.dumps(result["entity_matches"], ensure_ascii=False)


class _LightweightGraph:
    def __init__(self) -> None:
        self.context_calls = 0

    def context_for_query(self, *_args, **_kwargs):
        self.context_calls += 1
        raise AssertionError("graph_expansion=False called the traversal")

    def search_entities(self, user_id, query, *, limit=5):
        del user_id, query, limit
        return [
            {
                "id": "ent_atlas",
                "name": "Atlas",
                "entity_type": "project",
                "metadata_json": {"secret": True},
                "description": "secret node body",
            }
        ]


def _valid_projected_path() -> dict:
    return {
        "path_id": "path-valid",
        "root": "a",
        "target": "b",
        "entity_ids": ["a", "b"],
        "edges": [
            {
                "id": "rel-valid",
                "from": "a",
                "to": "b",
                "source": "a",
                "target": "b",
                "direction": "forward",
                "type": "related_to",
            }
        ],
    }


@pytest.mark.parametrize("corruption", ["too_long", "malformed", "root", "assertion"])
def test_public_snapshot_rejects_an_incoherent_path_as_one_unit(corruption: str) -> None:
    path = _valid_projected_path()
    if corruption == "too_long":
        path["entity_ids"] = [f"e{index}" for index in range(6)]
        path["root"] = "e0"
        path["target"] = "e5"
        path["edges"] = [
            {
                "id": f"rel-{index}",
                "from": f"e{index}",
                "to": f"e{index + 1}",
                "source": f"e{index}",
                "target": f"e{index + 1}",
                "direction": "forward",
            }
            for index in range(5)
        ]
    elif corruption == "malformed":
        path["target"] = "c"
        path["entity_ids"] = ["a", "b", "c"]
        path["edges"].append({})
    elif corruption == "root":
        path["root"] = "wrong"
    else:
        path["edges"][0]["source"] = "b"
        path["edges"][0]["target"] = "a"

    snapshot = _public_graph_context(
        {"paths": [path], "paths_matched_at_least": 1},
        query="q",
        as_of="2024-01-01",
        expanded=True,
    )

    assert snapshot["paths"] == []
    assert snapshot["paths_matched_at_least"] == 1
    assert snapshot["paths_truncated"] is True


def test_public_snapshot_emits_only_bounded_strict_json_numbers() -> None:
    snapshot = _public_graph_context(
        {
            "nodes": [
                {
                    "id": "a",
                    "name": "A",
                    "_relation_count": 10**5_000,
                    "_knowledge_count": -(10**5_000),
                    "_graph_score": float("nan"),
                }
            ],
            "relations": [
                {
                    "id": "rel",
                    "weight": float("inf"),
                    "confidence": float("-inf"),
                }
            ],
        },
        query=10**5_000,  # type: ignore[arg-type]
        as_of="",
        expanded=False,
    )

    assert snapshot["query"] == ""
    assert snapshot["nodes"][0]["_relation_count"] == 1_000_000_000
    assert snapshot["nodes"][0]["_knowledge_count"] == -1_000_000_000
    assert "_graph_score" not in snapshot["nodes"][0]
    assert "weight" not in snapshot["relations"][0]
    assert "confidence" not in snapshot["relations"][0]
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)

    invalid_path = _valid_projected_path()
    invalid_path["path_id"] = 10**5_000
    invalid_path["root"] = 10**5_000
    assert (
        _public_graph_context(
            {"paths": [invalid_path]},
            query="q",
            as_of="",
            expanded=True,
        )["paths"]
        == []
    )


def test_public_path_strips_contradictory_structural_aliases() -> None:
    path = _valid_projected_path()
    path.update(
        {
            "id": "evil-path-id",
            "root_entity_id": "evil-root",
            "target_entity_id": "evil-target",
            "current_entity_id": "evil-current",
            "depth": 99,
        }
    )
    path["edges"][0].update(
        {
            "from_entity_id": "evil-from",
            "to_entity_id": "evil-to",
            "traversal_from_entity_id": "evil-traversal-from",
            "traversal_to_entity_id": "evil-traversal-to",
            "assertion_direction": "reverse",
            "traversal_direction": "reverse",
            "depth": 99,
        }
    )

    snapshot = _public_graph_context(
        {"paths": [path]},
        query="q",
        as_of="",
        expanded=True,
    )
    encoded = json.dumps(snapshot["paths"][0], ensure_ascii=False)

    for sentinel in (
        "evil-path-id",
        "evil-root",
        "evil-target",
        "evil-current",
        "evil-from",
        "evil-to",
        "evil-traversal-from",
        "evil-traversal-to",
        '"assertion_direction"',
        '"traversal_direction"',
        '"depth"',
    ):
        assert sentinel not in encoded


@pytest.mark.asyncio
async def test_graph_expansion_false_builds_a_lightweight_snapshot_without_traversal(storage):
    _knowledge(storage)
    graph = _LightweightGraph()

    result = await HybridSearcher(storage, record_usage=False).search(
        "alice",
        "Atlas",
        kg=graph,
        graph_expansion=False,
        as_of="2024-03-05",
    )

    assert graph.context_calls == 0
    assert result["graph_context"] == {
        "query": "Atlas",
        "expanded": False,
        "as_of": "2024-03-05",
        "temporal_basis": "valid_time",
        "roots": [{"id": "ent_atlas", "name": "Atlas", "entity_type": "project"}],
        "nodes": [{"id": "ent_atlas", "name": "Atlas", "entity_type": "project"}],
        "entities": [{"id": "ent_atlas", "name": "Atlas", "entity_type": "project"}],
        "relations": [],
        "paths": [],
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }


@pytest.mark.asyncio
async def test_invalid_as_of_is_refused_before_graph_enrichment(storage):
    graph = _LightweightGraph()

    with pytest.raises(ValueError, match="Некорректная дата as_of"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice", "Atlas", kg=graph, as_of="not-a-date"
        )

    assert graph.context_calls == 0


@pytest.mark.asyncio
async def test_repaired_query_is_the_one_traversed_and_published(storage, monkeypatch):
    knowledge_id = _knowledge(storage)
    graph = _SnapshotGraph(knowledge_id)
    searcher = HybridSearcher(storage, record_usage=False)
    monkeypatch.setattr(
        searcher,
        "_repair_query",
        lambda _user_id, _query: SimpleNamespace(query="Atlas", kind="test-repair", detail=""),
    )

    result = await searcher.search("alice", "QWERTY_NO_MATCH", kg=graph)

    assert graph.calls[0]["query"] == "Atlas"
    assert result["query"] == "Atlas"
    assert result["graph_context"]["query"] == "Atlas"


def test_search_api_forwards_as_of_and_rejects_an_invalid_date(settings, monkeypatch):
    app = create_app(settings)
    captured: list[dict] = []

    async def _search(user_id, query, **kwargs):
        captured.append({"user_id": user_id, "query": query, **kwargs})
        if query == "internal failure":
            raise ValueError("ranking failed")
        return {"query": query, "as_of": kwargs["as_of"], "results": [], "count": 0}

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.hybrid_searcher, "search", _search)
        valid = client.get("/api/search", params={"q": "Atlas", "as_of": "2024-03-05"}, headers=headers)
        invalid = client.get("/api/search", params={"q": "Atlas", "as_of": "not-a-date"}, headers=headers)
        internal = client.get(
            "/api/search",
            params={"q": "internal failure", "as_of": "2024-03-05"},
            headers=headers,
        )

    assert valid.status_code == 200
    assert valid.json()["as_of"] == "2024-03-05"
    assert captured[0]["as_of"] == "2024-03-05"
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "Некорректная дата as_of"}
    assert internal.status_code == 500
    assert len(captured) == 2, "invalid as_of reached the searcher before HTTP validation"
