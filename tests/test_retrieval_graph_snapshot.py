"""The graph that ranked search is the bounded graph search returns.

This is the retrieval/API seam of proposal 26.  Traversal may use full storage
rows internally, but neither the agent nor ``GET /api/search`` may receive that
working set, recompute a different path, or silently turn a malformed historical
date into a current graphless answer.

Every probe here declares ``graph_expansion=True`` out loud.  The parameter used to
default to ``True``, so these tests exercised the graph channel by SILENCE — and so
did three production roads that never meant to.  Since proposal 40 the default is
the measured ordinary behaviour (no expansion), and asking for the graph is an
explicit act.  A probe about the graph seam must therefore say so.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from friday.retrieval import HybridSearcher, _public_graph_context
from friday.server import create_app
from friday.storage.models import (
    Entity,
    KnowledgeObject,
    RawObject,
    RelationHistorySnapshotError,
    new_id,
)


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
    def __init__(
        self,
        knowledge_id: str,
        *,
        candidate_path_id: object | None = "path_0",
        candidate_query_matched: object = True,
        candidate_score: float = 0.9,
        path_implicit: object | None = False,
    ) -> None:
        self.knowledge_id = knowledge_id
        self.candidate_path_id = candidate_path_id
        self.candidate_query_matched = candidate_query_matched
        self.candidate_score = candidate_score
        self.path_implicit = path_implicit
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
                        **({"implicit": self.path_implicit} if self.path_implicit is not None else {}),
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
            "known_at": kwargs.get("known_at", ""),
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
            "temporal_basis": "bitemporal" if kwargs.get("known_at") else "valid_time",
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
                    "score": self.candidate_score,
                    "query_matched": self.candidate_query_matched,
                    "evidence": [{"path_id": "path_0"}],
                    **({"path_id": self.candidate_path_id} if self.candidate_path_id is not None else {}),
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
        "alice", "Atlas", graph_expansion=True, kg=graph, as_of="2024/03/05"
    )

    assert graph.calls[0]["as_of"] == "2024-03-05"
    assert result["as_of"] == "2024-03-05"
    context = result["graph_context"]
    assert context["query"] == "Atlas"
    assert context["as_of"] == "2024-03-05"
    assert context["known_at"] == ""
    assert context["history_complete"] is True
    assert context["identity_basis"] == "current_names"
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


async def test_temporal_explicit_path_is_not_vetoed_by_text_only_reranking(storage):
    """An accepted explicit temporal path is stronger than a text-only veto.

    Mutation: dropping the temporal guard makes the current query bypass the
    reranker too; dropping the published-path guard makes the pathless query do
    so. Both controls below must remain empty.
    """

    knowledge_id = _knowledge(storage, "Atlas archive fact")
    rerank_calls = 0

    async def reject_everything(_query, items):
        nonlocal rerank_calls
        rerank_calls += 1
        return [{**item, "_rerank_score": 0.001} for item in items]

    searcher = HybridSearcher(
        storage,
        record_usage=False,
        reranker=reject_everything,
        rerank_top=20,
        rerank_confident_min=0.10,
    )

    temporal = await searcher.search(
        "alice",
        "Atlas",
        graph_expansion=True,
        kg=_SnapshotGraph(knowledge_id),
        as_of="2024-03-05",
    )
    assert [item["id"] for item in temporal["results"]] == [knowledge_id]
    assert rerank_calls == 0

    current = await searcher.search("alice", "Atlas", graph_expansion=True, kg=_SnapshotGraph(knowledge_id))
    assert current["results"] == []
    assert rerank_calls == 1

    untrusted_candidates = (
        _SnapshotGraph(knowledge_id, candidate_path_id=None),
        _SnapshotGraph(knowledge_id, candidate_path_id="path-not-published"),
        _SnapshotGraph(knowledge_id, candidate_path_id=True),
        _SnapshotGraph(knowledge_id, candidate_query_matched="truthy-but-not-attested"),
        _SnapshotGraph(knowledge_id, path_implicit=True),
        _SnapshotGraph(knowledge_id, path_implicit=None),
    )
    for graph in untrusted_candidates:
        untrusted = await searcher.search(
            "alice",
            "Atlas",
            graph_expansion=True,
            kg=graph,
            as_of="2024-03-05",
        )
        assert untrusted["results"] == []
    assert rerank_calls == 1 + len(untrusted_candidates)


async def test_an_excluded_temporal_path_cannot_disable_reranking_for_other_results(storage):
    """The bypass belongs to a returned candidate, never a discarded graph row."""

    protected_id = _knowledge(storage, "BRK.B protected historical row")
    _knowledge(storage, "BRK.A eligible current row")
    rerank_calls = 0

    async def reject_everything(_query, items):
        nonlocal rerank_calls
        rerank_calls += 1
        return [{**item, "_rerank_score": 0.001} for item in items]

    searcher = HybridSearcher(
        storage,
        record_usage=False,
        reranker=reject_everything,
        rerank_top=20,
        rerank_confident_min=0.10,
    )
    result = await searcher.search(
        "alice",
        "BRK.A",
        graph_expansion=True,
        kg=_SnapshotGraph(protected_id),
        as_of="2024-03-05",
    )

    assert result["results"] == []
    assert rerank_calls == 1


async def test_a_protected_tail_cannot_disable_reranking_for_the_head(storage):
    """A path beyond rerank_top cannot be vetoed, so it grants no global bypass."""

    protected_id = _knowledge(storage, "historical graph-only tail")
    eligible_id = _knowledge(storage, "Atlas eligible lexical head")
    rerank_calls = 0

    async def reject_head(_query, items):
        nonlocal rerank_calls
        rerank_calls += 1
        return [{**item, "_rerank_score": 0.001} for item in items]

    searcher = HybridSearcher(
        storage,
        record_usage=False,
        reranker=reject_head,
        rerank_top=1,
        rerank_confident_min=0.10,
    )
    result = await searcher.search(
        "alice",
        "Atlas eligible",
        graph_expansion=True,
        kg=_SnapshotGraph(protected_id, candidate_score=0.21),
        as_of="2024-03-05",
        limit=2,
    )

    assert rerank_calls == 1
    assert [item["id"] for item in result["results"]] == [protected_id]
    assert eligible_id not in {item["id"] for item in result["results"]}


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


def test_public_snapshot_drops_reviewer_identity_even_from_a_malicious_upstream() -> None:
    path = _valid_projected_path()
    path["edges"][0]["provenance"] = {
        "origin": "review",
        "source": "reviewed_relation_candidate",
        "candidate_id": "candidate-visible",
        "reviewed": True,
        "reviewed_by": "PRIVATE REVIEWER SENTINEL",
        "created_by": "PRIVATE CREATOR SENTINEL",
        "invalidated_reason": "PRIVATE INVALIDATION REASON SENTINEL",
    }
    path["edges"][0]["invalidated_reason"] = "PRIVATE INVALIDATION REASON SENTINEL"
    snapshot = _public_graph_context(
        {
            "relations": [
                {
                    "id": "rel-visible",
                    "reviewed_by": "PRIVATE RELATION REVIEWER SENTINEL",
                    "invalidated_reason": "PRIVATE INVALIDATION REASON SENTINEL",
                }
            ],
            "paths": [path],
        },
        query="q",
        as_of="",
        expanded=True,
    )

    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert "PRIVATE REVIEWER SENTINEL" not in encoded
    assert "PRIVATE CREATOR SENTINEL" not in encoded
    assert "PRIVATE RELATION REVIEWER SENTINEL" not in encoded
    assert "PRIVATE INVALIDATION REASON SENTINEL" not in encoded
    assert snapshot["paths"][0]["edges"][0]["provenance"]["reviewed"] is True


@pytest.mark.asyncio
async def test_quarantined_entity_link_cannot_rank_or_decorate_a_shared_document(storage) -> None:
    knowledge_id = _knowledge(storage, "Atlas public document")
    sentinel = "PRIVATE_ALICE_REMINDER_SENTINEL"
    entity = Entity(
        id="ent-private-retrieval",
        user_id="alice",
        name=sentinel,
        entity_type="event",
    )
    storage.create_entity(entity)
    storage.link_knowledge_entity(
        "alice",
        knowledge_id,
        entity.id,
        status="accepted",
        evidence={"private": sentinel},
    )
    searcher = HybridSearcher(storage, record_usage=False)
    assert (
        searcher._entity_links_by_document("alice", [knowledge_id])[knowledge_id][0][  # noqa: SLF001
            "name"
        ]
        == sentinel
    )

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (entity.id, "person-alice", "2026-08-05T00:00:00Z"),
        )

    assert searcher._entity_links_by_document("alice", [knowledge_id]) == {}  # noqa: SLF001
    result = await searcher.search("alice", "Atlas")
    encoded = json.dumps(result, ensure_ascii=False)
    assert sentinel not in encoded
    assert entity.id not in encoded
    assert result["results"] == []


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
async def test_candidate_graph_evidence_is_allowlisted_and_structurally_bounded(storage):
    knowledge_id = _knowledge(storage)
    secret = "PRIVATE-CANDIDATE-EVIDENCE-" + ("x" * 250_000)

    class _EvidenceGraph(_SnapshotGraph):
        def context_for_query(self, user_id, query, **kwargs):
            result = super().context_for_query(user_id, query, **kwargs)
            result["knowledge_candidates"][0]["evidence"] = [
                {
                    "entity_id": f"entity-{index}",
                    "entity_name": "N" * 500,
                    "link_confidence": 0.75,
                    "entity_score": 0.5,
                    "path_id": f"path-{index}",
                    "metadata_json": {"secret": secret},
                    "excerpt": secret,
                }
                for index in range(30)
            ]
            return result

    result = await HybridSearcher(storage, record_usage=False).search(
        "alice",
        "Atlas",
        graph_expansion=True,
        kg=_EvidenceGraph(knowledge_id),
    )

    document = result["results"][0]
    assert len(document["_graph_evidence"]) == 12
    assert document["_graph_evidence_matched_at_least"] == 30
    assert document["_graph_evidence_truncated"] is True
    assert all(len(item["entity_name"]) == 240 for item in document["_graph_evidence"])
    encoded = json.dumps(document["_graph_evidence"], ensure_ascii=False)
    assert "metadata_json" not in encoded
    assert "excerpt" not in encoded
    assert "PRIVATE-CANDIDATE-EVIDENCE" not in encoded


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
        "known_at": "",
        "known_at_floor": "",
        "history_complete": True,
        "identity_basis": "current_names",
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
            "alice", "Atlas", graph_expansion=True, kg=graph, as_of="not-a-date"
        )

    assert graph.context_calls == 0


@pytest.mark.asyncio
async def test_known_at_is_normalized_before_candidates_and_reaches_the_same_snapshot(
    storage,
    monkeypatch,
):
    knowledge_id = _knowledge(storage)
    graph = _SnapshotGraph(knowledge_id)
    expected = "2026-08-04T09:30:00.000000Z"
    status_calls: list[str] = []

    def _history_status(user_id: str, *, known_at: str = "") -> dict:
        assert user_id == "alice"
        status_calls.append(known_at)
        return {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        }

    monkeypatch.setattr(storage, "relation_history_status", _history_status)
    result = await HybridSearcher(storage, record_usage=False).search(
        "alice",
        "Atlas",
        graph_expansion=True,
        kg=graph,
        known_at="2026-08-04T12:30:00+03:00",
    )

    assert status_calls == [expected, expected]
    assert graph.calls[0]["known_at"] == expected
    assert result["known_at"] == expected
    assert result["known_at_floor"] == "2026-08-01T00:00:00.000000Z"
    assert result["history_complete"] is True
    assert result["identity_basis"] == "current_names"
    assert result["temporal_basis"] == "bitemporal"
    assert result["graph_context"]["known_at"] == expected
    assert result["graph_context"]["temporal_basis"] == "bitemporal"


@pytest.mark.asyncio
async def test_invalid_known_at_is_refused_before_history_or_candidate_reads(storage, monkeypatch):
    graph = _LightweightGraph()
    touched: list[str] = []
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: touched.append("history"),
    )
    monkeypatch.setattr(
        storage,
        "search_knowledge",
        lambda *_args, **_kwargs: touched.append("candidates"),
    )

    with pytest.raises(ValueError, match="Некорректный timestamp known_at"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            graph_expansion=True,
            kg=graph,
            known_at="2026-08-04T12:30:00",
        )

    assert touched == []
    assert graph.context_calls == 0


@pytest.mark.asyncio
async def test_current_search_never_depends_on_relation_history(storage, monkeypatch):
    _knowledge(storage)
    graph = _LightweightGraph()

    def _forbid_history(*_args, **_kwargs):
        raise AssertionError("current search read relation history")

    monkeypatch.setattr(storage, "relation_history_status", _forbid_history)

    result = await HybridSearcher(storage, record_usage=False).search(
        "alice",
        "Atlas",
        kg=graph,
        graph_expansion=False,
        as_of="2024-03-05",
    )

    assert result["known_at"] == ""
    assert result["known_at_floor"] == ""
    assert result["history_complete"] is True
    assert result["identity_basis"] == "current_names"
    assert result["temporal_basis"] == "valid_time"
    assert result["graph_context"]["known_at"] == ""
    assert result["graph_context"]["known_at_floor"] == ""


@pytest.mark.asyncio
async def test_history_floor_refusal_precedes_every_candidate_read(storage, monkeypatch):
    touched: list[str] = []

    def _refuse_history(_user_id: str, *, known_at: str = "") -> dict:
        touched.append(f"history:{known_at}")
        raise RelationHistorySnapshotError("known_at precedes complete relation history")

    def _candidate_read(*_args, **_kwargs):
        touched.append("candidate")
        raise AssertionError("candidate read happened before relation-history refusal")

    monkeypatch.setattr(storage, "relation_history_status", _refuse_history)
    monkeypatch.setattr(storage, "knowledge_ids_in_window", _candidate_read)
    monkeypatch.setattr(storage, "search_knowledge", _candidate_read)
    monkeypatch.setattr(storage, "list_knowledge_objects", _candidate_read)

    with pytest.raises(RelationHistorySnapshotError, match="precedes complete"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            known_at="2026-08-04T12:30:00+03:00",
            graph_expansion=False,
        )

    assert touched == ["history:2026-08-04T09:30:00.000000Z"]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("known_at", ""),
        ("known_at_floor", ""),
        ("known_at_floor", "2026-08-04T10:00:00.000000Z"),
        ("history_complete", 1),
        ("identity_basis", "historical_names"),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_history_attestation_is_refused_before_candidate_reads(
    storage,
    monkeypatch,
    field,
    bad_value,
):
    expected = "2026-08-04T09:30:00.000000Z"
    touched: list[str] = []
    status_calls: list[str] = []

    def _status(_user_id, *, known_at=""):
        status_calls.append(known_at)
        status = {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        }
        status[field] = bad_value
        return status

    def _candidate_read(*_args, **_kwargs):
        touched.append("candidate")
        raise AssertionError("candidate read happened before history attestation")

    monkeypatch.setattr(storage, "relation_history_status", _status)
    monkeypatch.setattr(storage, "knowledge_ids_in_window", _candidate_read)

    with pytest.raises(ValueError, match="relation-history|storage changed"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            known_at="2026-08-04T12:30:00+03:00",
        )

    assert status_calls == [expected]
    assert touched == []


@pytest.mark.asyncio
async def test_temporal_graph_refusal_is_not_swallowed_as_optional_enrichment(storage, monkeypatch):
    _knowledge(storage)
    expected = "2026-08-04T09:30:00.000000Z"
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        },
    )

    class _RefusingGraph(_SnapshotGraph):
        def context_for_query(self, user_id, query, **kwargs):
            assert kwargs["known_at"] == expected
            raise ValueError("known_at пересекает merge")

    with pytest.raises(ValueError, match="пересекает merge"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            graph_expansion=True,
            kg=_RefusingGraph("unused"),
            known_at="2026-08-04T12:30:00+03:00",
        )


@pytest.mark.parametrize(
    "boundaries",
    [
        {"as_of": "2024-03-05"},
        {"known_at": "2026-08-04T12:30:00+03:00"},
    ],
)
@pytest.mark.asyncio
async def test_any_temporal_traversal_failure_is_fail_closed(storage, monkeypatch, boundaries):
    knowledge_id = _knowledge(storage)
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        },
    )

    class _BrokenGraph(_SnapshotGraph):
        def context_for_query(self, *_args, **_kwargs):
            raise RuntimeError("synthetic traversal outage")

    with pytest.raises(RuntimeError, match="synthetic traversal outage"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            graph_expansion=True,
            kg=_BrokenGraph(knowledge_id),
            **boundaries,
        )


@pytest.mark.asyncio
async def test_current_traversal_failure_keeps_the_legacy_graphless_fallback(storage):
    knowledge_id = _knowledge(storage)

    class _BrokenGraph(_SnapshotGraph):
        def context_for_query(self, *_args, **_kwargs):
            raise RuntimeError("synthetic current traversal outage")

    result = await HybridSearcher(storage, record_usage=False).search(
        "alice",
        "Atlas",
        graph_expansion=True,
        kg=_BrokenGraph(knowledge_id),
    )

    assert result["count"] == 1
    assert result["graph_context"]["expanded"] is False
    assert result["graph_context"]["paths"] == []


@pytest.mark.asyncio
async def test_non_mapping_current_traversal_is_not_reported_as_expanded(storage):
    knowledge_id = _knowledge(storage)

    class _MalformedGraph(_SnapshotGraph):
        def context_for_query(self, *_args, **_kwargs):
            return []

    result = await HybridSearcher(storage, record_usage=False).search(
        "alice",
        "Atlas",
        graph_expansion=True,
        kg=_MalformedGraph(knowledge_id),
    )

    assert result["count"] == 1
    assert result["graph_context"]["expanded"] is False


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("known_at", ""),
        ("as_of", "2024-03-06"),
        ("known_at_floor", ""),
        ("history_complete", 1),
        ("identity_basis", "historical_names"),
        ("temporal_basis", "valid_time"),
    ],
)
@pytest.mark.asyncio
async def test_temporal_graph_must_echo_every_attested_boundary(
    storage,
    monkeypatch,
    field,
    bad_value,
):
    knowledge_id = _knowledge(storage)
    expected_known_at = "2026-08-04T09:30:00.000000Z"
    expected_floor = "2026-08-01T00:00:00.000000Z"

    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": {
            "known_at": known_at,
            "known_at_floor": expected_floor,
            "history_complete": True,
            "identity_basis": "current_names",
        },
    )

    class _CorruptEchoGraph(_SnapshotGraph):
        def context_for_query(self, user_id, query, **kwargs):
            result = super().context_for_query(user_id, query, **kwargs)
            result[field] = bad_value
            return result

    graph = _CorruptEchoGraph(knowledge_id)
    with pytest.raises(ValueError, match="graph traversal"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            graph_expansion=True,
            kg=graph,
            as_of="2024-03-05",
            known_at="2026-08-04T12:30:00+03:00",
        )

    assert graph.calls[0]["known_at"] == expected_known_at


@pytest.mark.asyncio
async def test_known_at_postflight_rejects_an_identity_change_during_search(storage, monkeypatch):
    _knowledge(storage)
    expected = "2026-08-04T09:30:00.000000Z"
    status_calls: list[str] = []
    candidate_reads: list[str] = []
    real_search = storage.search_knowledge

    def _history_status(_user_id: str, *, known_at: str = "") -> dict:
        status_calls.append(known_at)
        if len(status_calls) == 2:
            raise ValueError("known_at crosses a later entity merge")
        return {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        }

    def _search(*args, **kwargs):
        candidate_reads.append("read")
        return real_search(*args, **kwargs)

    monkeypatch.setattr(storage, "relation_history_status", _history_status)
    monkeypatch.setattr(storage, "search_knowledge", _search)

    with pytest.raises(ValueError, match="later entity merge"):
        await HybridSearcher(storage, record_usage=False).search(
            "alice",
            "Atlas",
            known_at="2026-08-04T12:30:00+03:00",
            graph_expansion=False,
        )

    assert status_calls == [expected, expected]
    assert candidate_reads, "test did not place the identity change after candidate reads"


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
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        },
    )

    result = await searcher.search(
        "alice",
        "QWERTY_NO_MATCH",
        graph_expansion=True,
        kg=graph,
        known_at="2026-08-04T12:30:00+03:00",
    )

    assert graph.calls[0]["query"] == "Atlas"
    assert result["query"] == "Atlas"
    assert result["graph_context"]["query"] == "Atlas"
    assert graph.calls[0]["known_at"] == "2026-08-04T09:30:00.000000Z"
    assert result["graph_context"]["known_at"] == "2026-08-04T09:30:00.000000Z"


def test_search_api_forwards_temporal_boundaries_and_rejects_invalid_input(settings, monkeypatch):
    app = create_app(settings)
    captured: list[dict] = []
    status_calls: list[str] = []

    async def _search(user_id, query, **kwargs):
        captured.append({"user_id": user_id, "query": query, **kwargs})
        if query == "internal failure":
            raise ValueError("ranking failed")
        return {
            "query": query,
            "as_of": kwargs["as_of"],
            "known_at": kwargs["known_at"],
            "results": [],
            "count": 0,
        }

    def _history_status(_user_id: str, *, known_at: str = "") -> dict:
        status_calls.append(known_at)
        if known_at == "2026-08-03T09:30:00.000000Z":
            raise RelationHistorySnapshotError("known_at precedes complete relation history")
        return {
            "known_at": known_at,
            "known_at_floor": "2026-08-04T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        }

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.hybrid_searcher, "search", _search)
        monkeypatch.setattr(app.state.storage, "relation_history_status", _history_status)
        valid = client.get("/api/search", params={"q": "Atlas", "as_of": "2024-03-05"}, headers=headers)
        invalid = client.get("/api/search", params={"q": "Atlas", "as_of": "not-a-date"}, headers=headers)
        known = client.get(
            "/api/search",
            params={"q": "Atlas", "known_at": "2026-08-04T12:30:00+03:00"},
            headers=headers,
        )
        invalid_known = client.get(
            "/api/search",
            params={"q": "Atlas", "known_at": "2026-08-04T12:30:00"},
            headers=headers,
        )
        before_floor = client.get(
            "/api/search",
            params={"q": "Atlas", "known_at": "2026-08-03T12:30:00+03:00"},
            headers=headers,
        )
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
    assert known.status_code == 200
    assert known.json()["known_at"] == "2026-08-04T09:30:00.000000Z"
    assert captured[1]["known_at"] == "2026-08-04T09:30:00.000000Z"
    assert invalid_known.status_code == 400
    assert invalid_known.json() == {"detail": "Некорректная граница known_at"}
    assert before_floor.status_code == 400
    assert before_floor.json() == {"detail": "Исторический снимок графа недоступен или неполон"}
    assert status_calls == [
        "2026-08-04T09:30:00.000000Z",
        "2026-08-03T09:30:00.000000Z",
    ]
    assert internal.status_code == 500
    assert len(captured) == 3, "invalid temporal input reached the searcher before HTTP validation"


def test_search_api_maps_only_typed_postflight_snapshot_refusal_to_400(settings, monkeypatch):
    app = create_app(settings)

    def _history_status(_user_id: str, *, known_at: str = "") -> dict:
        return {
            "known_at": known_at,
            "known_at_floor": "2026-08-01T00:00:00.000000Z",
            "history_complete": True,
            "identity_basis": "current_names",
        }

    async def _search(_user_id: str, query: str, **_kwargs):
        if query == "snapshot race":
            raise RelationHistorySnapshotError("known_at crosses a later entity merge")
        raise ValueError("ranking invariant failed")

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.storage, "relation_history_status", _history_status)
        monkeypatch.setattr(app.state.hybrid_searcher, "search", _search)
        race = client.get(
            "/api/search",
            params={"q": "snapshot race", "known_at": "2026-08-04T12:30:00+03:00"},
            headers=headers,
        )
        invariant = client.get(
            "/api/search",
            params={"q": "ranking bug", "known_at": "2026-08-04T12:30:00+03:00"},
            headers=headers,
        )

    assert race.status_code == 400
    assert race.json() == {"detail": "Исторический снимок недоступен после изменения слияния сущностей"}
    assert invariant.status_code == 500
