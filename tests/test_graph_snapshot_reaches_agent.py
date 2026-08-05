"""The graph explanation used for ranking is the one shown to the model.

These tests guard the wiring added by proposal 26.  A graph implementation can
return perfectly historical, provenance-rich paths and still be unsafe if the
agent silently traverses a second, current snapshot or drops the path before it
reaches the prompt.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, _graph_paths_for_prompt
from friday.execution_kernel import ExecutionKernel, _memory_graph_context_for_llm
from friday.permissions import ActorContext, AuthorizationService


class _SnapshotGraph:
    def __init__(self, fallback: dict[str, Any] | None = None) -> None:
        self.fallback = fallback or {"paths": [{"path_id": "wrong-second-traversal"}]}
        self.context_calls: list[dict[str, Any]] = []

    def get_stats(self, user_id: str) -> dict[str, int]:
        return {
            "relation_count": 0,
            "pending_inbox": 0,
            "pending_resolutions": 0,
            "pending_relation_candidates": 0,
            "pending_conflicts": 0,
        }

    def context_for_query(self, user_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        self.context_calls.append({"user_id": user_id, "query": query, **kwargs})
        return self.fallback


class _SnapshotSearcher:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def search(self, user_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"user_id": user_id, "query": query, **kwargs})
        return self.response


@pytest.mark.asyncio
async def test_prepare_context_reuses_the_effective_graph_snapshot(settings, storage):
    storage.ensure_user("alice")
    snapshot = {
        "query": "исправленная строка поиска",
        "as_of": "2024-06-01",
        "temporal_basis": "valid_time",
        "expanded": True,
        "roots": [{"id": "ent-a", "name": "Альфа"}],
        "entities": [{"id": "ent-a", "name": "Альфа"}],
        "nodes": [{"id": "ent-a", "name": "Альфа"}],
        "relations": [],
        "paths": [],
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }
    entity_matches = [{"id": "ent-a", "name": "Альфа", "entity_type": "project"}]
    searcher = _SnapshotSearcher(
        {
            "results": [],
            "entity_matches": entity_matches,
            "strategy": {"query_repaired": "vocabulary"},
            "graph_context": snapshot,
        }
    )
    graph = _SnapshotGraph()

    context = await AgentRuntime(settings, storage)._prepare_context(  # noqa: SLF001
        "alice",
        "С кем работал проект Альфа в июне 2024 года?",
        "conv-snapshot",
        prior_history=[],
        kg=graph,
        searcher=searcher,
        interaction_mode="knowledge_work",
    )

    assert graph.context_calls == [], "agent traversed a second, potentially current graph snapshot"
    assert context.graph_context is snapshot, "the exact ranking snapshot was replaced or reconstructed"
    assert context.entity_hits == entity_matches, (
        "entity matches are a separate retrieval result and were lost"
    )
    assert context.graph_context["query"] == "исправленная строка поиска"
    assert context.graph_context["as_of"] == "2024-06-01"


@pytest.mark.asyncio
async def test_prepare_context_keeps_a_legacy_searcher_fallback(settings, storage):
    storage.ensure_user("alice")
    fallback = {"roots": [{"id": "legacy", "name": "Legacy"}], "paths": []}
    graph = _SnapshotGraph(fallback)
    searcher = _SnapshotSearcher({"results": [], "entity_matches": [], "strategy": {}})

    context = await AgentRuntime(settings, storage)._prepare_context(  # noqa: SLF001
        "alice",
        "Расскажи про проект Legacy подробно",
        "conv-legacy",
        prior_history=[],
        kg=graph,
        searcher=searcher,
        interaction_mode="knowledge_work",
    )

    assert len(graph.context_calls) == 1
    assert context.graph_context == fallback
    assert context.entity_hits == fallback["roots"]


def _context_data(messages: list[dict[str, Any]]) -> dict[str, Any]:
    envelope = next(
        str(message["content"])
        for message in messages
        if message.get("role") == "user"
        and str(message.get("content") or "").startswith("FRIDAY_CONTEXT_DATA")
    )
    return json.loads(envelope.split("\n", 1)[1])


def _edge(
    number: int,
    *,
    direction: str,
    knowledge_object_id: str = "",
    implicit: bool = False,
    from_id: str | None = None,
    to_id: str | None = None,
    source_id: str | None = None,
    target_id: str | None = None,
    origin: str = "review",
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "origin": origin,
        "source": "reviewed_relation_candidate",
        "candidate_id": f"candidate-{number}",
        "reviewed_by": "owner",
        "created_by": "ingestion",
        "confidence": 0.81,
        "metadata_json": {"secret": "must-not-reach-the-model"},
        "evidence_text": "a long raw quotation must not reach the model",
    }
    if knowledge_object_id:
        provenance["knowledge_object_id"] = knowledge_object_id
    return {
        "id": f"rel-{number}",
        "from": from_id or f"ent-{number}",
        "to": to_id or f"ent-{number + 1}",
        "direction": direction,
        "source": source_id or "canonical-source",
        "target": target_id or "canonical-target",
        "type": "member_of",
        "weight": 0.875,
        "implicit": implicit,
        "valid_from": "2024-01-01",
        "valid_to": "2024-12-31",
        "created_at": "2026-07-01T12:00:00+00:00",
        "invalidated_at": "2026-07-02T12:00:00+00:00",
        "superseded_by": "rel-new",
        "provenance": provenance,
    }


def test_prompt_gets_bounded_temporal_paths_with_existing_k_anchors(settings, storage):
    storage.ensure_user("alice")
    context = AgentContext(
        conversation_id="conv-path",
        user_id="alice",
        search_query="кто с кем работал",
        answer_mode="personal_knowledge",
        knowledge_hits=[
            {
                "id": "ko-one",
                "raw_object_id": "raw-one",
                "title": "Первое основание",
                "content": "Первое основание связи",
                "_score": 0.9,
            },
            {
                "id": "ko-two",
                "raw_object_id": "raw-two",
                "title": "Второе основание",
                "content": "Второе основание связи",
                "_score": 0.8,
            },
        ],
        graph_context={
            "query": "исправленный эффективный запрос",
            "as_of": "2024-06-01",
            "temporal_basis": "valid_time",
            "expanded": True,
            "paths_matched_at_least": 8,
            "paths_truncated": True,
            "nodes": [
                {"id": "ent-a", "name": "Альфа", "entity_type": "project"},
                {"id": "ent-b", "name": "Бета", "entity_type": "person"},
                {"id": "ent-c", "name": "Гамма", "entity_type": "organization"},
                {"id": "ent-d", "name": "Дельта", "entity_type": "project"},
            ],
            "paths": [
                {
                    "path_id": "path-grounded",
                    "root": "ent-a",
                    "target": "ent-c",
                    "score": 0.55,
                    "entity_ids": ["ent-a", "ent-b", "ent-c"],
                    "edges": [
                        _edge(
                            1,
                            direction="reverse",
                            knowledge_object_id="ko-one",
                            from_id="ent-a",
                            to_id="ent-b",
                            source_id="ent-b",
                            target_id="ent-a",
                        ),
                        _edge(
                            2,
                            direction="forward",
                            knowledge_object_id="ko-two",
                            from_id="ent-b",
                            to_id="ent-c",
                            source_id="ent-b",
                            target_id="ent-c",
                        ),
                    ],
                },
                {
                    "path_id": "path-unanchored",
                    "root": "ent-a",
                    "target": "ent-d",
                    "score": 0.4,
                    "entity_ids": ["ent-a", "ent-d"],
                    "edges": [
                        _edge(
                            3,
                            direction="forward",
                            knowledge_object_id="ko-one",
                            from_id="ent-a",
                            to_id="ent-d",
                            source_id="ent-a",
                            target_id="ent-d",
                            origin="api",
                        )
                    ],
                },
                *[
                    {
                        "path_id": f"path-extra-{index}",
                        "root": "ent-a",
                        "target": f"ent-extra-{index}",
                        "score": 0.1,
                        "entity_ids": ["ent-a", f"ent-extra-{index}"],
                        "edges": [_edge(10 + index, direction="forward")],
                    }
                    for index in range(6)
                ],
            ],
        },
    )

    messages = AgentRuntime(settings, storage)._build_initial_messages(  # noqa: SLF001
        context,
        "Кто с кем работал на тот момент?",
        None,
        tool_enabled=False,
    )
    payload = _context_data(messages)

    assert payload["graph_snapshot"] == {
        "as_of": "2024-06-01",
        "expanded": True,
        "paths_matched_at_least": 8,
        "paths_truncated": True,
        "query": "исправленный эффективный запрос",
        "temporal_basis": "valid_time",
    }
    assert len(payload["graph_paths"]) == 6
    grounded, unanchored = payload["graph_paths"][:2]
    assert grounded["grounded"] is True
    assert [step["direction"] for step in grounded["steps"]] == ["reverse", "forward"]
    assert grounded["steps"][0]["source"] == {
        "id": "ent-b",
        "name": "Бета",
        "entity_type": "person",
    }
    assert grounded["steps"][0]["valid_from"] == "2024-01-01"
    assert grounded["steps"][0]["created_at"].startswith("2026-07-01")
    assert grounded["steps"][0]["weight"] == 0.875
    assert grounded["steps"][0]["provenance"]["citation"] == "K1"
    assert grounded["steps"][0]["provenance"]["source"] == "reviewed_relation_candidate"
    assert grounded["steps"][0]["provenance"]["confidence"] == 0.81
    assert grounded["steps"][1]["provenance"]["citation"] == "K2"
    assert unanchored["grounded"] is False
    assert "metadata_json" not in grounded["steps"][0]["provenance"]
    assert "evidence_text" not in grounded["steps"][0]["provenance"]
    assert "[G" not in json.dumps(payload, ensure_ascii=False)


def test_malformed_or_oversized_prompt_paths_fail_closed() -> None:
    anchored = _edge(
        1,
        direction="forward",
        knowledge_object_id="ko-one",
        from_id="a",
        to_id="b",
        source_id="a",
        target_id="b",
    )
    malformed = _graph_paths_for_prompt(
        {
            "paths": [
                {
                    "path_id": "partial",
                    "root": "a",
                    "target": "c",
                    "entity_ids": ["a", "b", "c"],
                    "edges": [anchored, "corrupt"],
                }
            ]
        },
        {"ko-one": "K1"},
    )
    assert malformed[0]["grounded"] is False

    wrong_root = _graph_paths_for_prompt(
        {
            "paths": [
                {
                    "path_id": "wrong-root",
                    "root": "elsewhere",
                    "target": "b",
                    "entity_ids": ["a", "b"],
                    "edges": [anchored],
                }
            ]
        },
        {"ko-one": "K1"},
    )
    assert wrong_root[0]["grounded"] is False

    reversed_assertion = {**anchored, "source": "b", "target": "a"}
    wrong_assertion = _graph_paths_for_prompt(
        {
            "paths": [
                {
                    "path_id": "wrong-assertion",
                    "root": "a",
                    "target": "b",
                    "entity_ids": ["a", "b"],
                    "edges": [reversed_assertion],
                }
            ]
        },
        {"ko-one": "K1"},
    )
    assert wrong_assertion[0]["grounded"] is False

    long = "x" * 1_000
    raw_paths = []
    for path_index in range(6):
        entity_ids = [f"p{path_index}-{step}" for step in range(5)]
        edges = []
        for step_index in range(4):
            edge = _edge(
                path_index * 10 + step_index,
                direction="forward",
                knowledge_object_id="ko-one",
                from_id=entity_ids[step_index],
                to_id=entity_ids[step_index + 1],
            )
            edge.update(
                {
                    "id": long,
                    "type": long,
                    "valid_from": long,
                    "valid_to": long,
                    "created_at": long,
                    "invalidated_at": long,
                    "superseded_by": long,
                }
            )
            edge["provenance"].update({"candidate_id": long, "reviewed_by": long, "created_by": long})
            edges.append(edge)
        raw_paths.append(
            {
                "path_id": long,
                "root": entity_ids[0],
                "target": entity_ids[-1],
                "entity_ids": entity_ids,
                "edges": edges,
            }
        )

    bounded = _graph_paths_for_prompt({"paths": raw_paths}, {"ko-one": "K1"})
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 8_000
    assert len(bounded) < len(raw_paths)


def test_path_local_entity_labels_survive_the_global_node_cap() -> None:
    roots = [{"id": f"root-{index}", "name": f"Root {index}", "entity_type": "project"} for index in range(8)]
    paths = []
    for index in range(6):
        target_id = f"target-{index}"
        paths.append(
            {
                "path_id": f"path-{index}",
                "root": "root-0",
                "target": target_id,
                "entity_ids": ["root-0", target_id],
                "entities": [
                    roots[0],
                    {
                        "id": target_id,
                        "name": f"Target {index}",
                        "entity_type": "person",
                        "metadata_json": {"secret": True},
                    },
                ],
                "edges": [
                    _edge(
                        index,
                        direction="forward",
                        from_id="root-0",
                        to_id=target_id,
                    )
                ],
            }
        )

    rendered = _graph_paths_for_prompt({"nodes": roots, "paths": paths}, {})

    assert rendered[-1]["target"] == {
        "id": "target-5",
        "name": "Target 5",
        "entity_type": "person",
    }
    assert rendered[-1]["steps"][0]["to"] == rendered[-1]["target"]
    assert "metadata_json" not in json.dumps(rendered, ensure_ascii=False)


def test_prompt_graph_numbers_are_strict_json_safe() -> None:
    edge = _edge(
        1,
        direction="forward",
        from_id="a",
        to_id="b",
        source_id="a",
        target_id="b",
    )
    edge["weight"] = 10**5_000
    rendered = _graph_paths_for_prompt(
        {
            "paths": [
                {
                    "path_id": "numeric",
                    "root": "a",
                    "target": "b",
                    "score": 10**5_000,
                    "entity_ids": ["a", "b"],
                    "edges": [edge],
                }
            ]
        },
        {},
    )

    assert "score" not in rendered[0]
    assert "weight" not in rendered[0]["steps"][0]
    json.dumps(rendered, ensure_ascii=False, allow_nan=False)

    huge = 10**5_000
    edge["id"] = huge
    malformed_text = _graph_paths_for_prompt(
        {
            "paths": [
                {
                    "path_id": huge,
                    "root": huge,
                    "target": "b",
                    "entity_ids": ["a", "b"],
                    "edges": [edge],
                }
            ]
        },
        {},
    )
    assert malformed_text[0]["grounded"] is False
    json.dumps(malformed_text, ensure_ascii=False, allow_nan=False)


@pytest.mark.asyncio
async def test_memory_search_rejects_invalid_as_of_before_search(settings, storage):
    storage.ensure_user("alice")
    searcher = _SnapshotSearcher({"results": []})
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    graph = _SnapshotGraph()
    kernel.bind_services(storage, graph, None, None, searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    refused = await kernel._memory_search(  # noqa: SLF001
        actor=actor,
        query="с кем работал Альфа",
        as_of="31 февраля 2024",
    )

    assert refused["empty_because"] == "as_of_unparsed"
    assert refused["results"] == []
    assert searcher.calls == [], "invalid temporal input silently degraded to a current search"


@pytest.mark.asyncio
async def test_memory_search_passes_normalized_as_of_and_a_bounded_graph_snapshot(settings, storage):
    storage.ensure_user("alice")
    graph_context = {
        "query": "расскажи подробно про проект Альфа",
        "as_of": "2024-06-01",
        "temporal_basis": "valid_time",
        "expanded": True,
        "roots": [],
        "entities": [],
        "nodes": [],
        "relations": [],
        "paths_matched_at_least": 12,
        "paths_truncated": True,
        "paths": [
            {
                "path_id": f"path-{index}",
                "root": "a",
                "target": "b",
                "score": 0.5,
                "grounded": True,
                "entity_ids": ["a", "b"],
                "edges": [
                    {
                        **_edge(
                            index,
                            direction="forward",
                            knowledge_object_id="ko-one",
                            from_id="a",
                            to_id="b",
                            source_id="a",
                            target_id="b",
                        ),
                        "grounded": True,
                    }
                ],
            }
            for index in range(12)
        ],
    }
    searcher = _SnapshotSearcher(
        {
            "results": [{"id": "ko-one", "title": "Основание", "content": "связь"}],
            "strategy": {},
            "as_of": "2024-06-01",
            "graph_context": graph_context,
        }
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    graph = _SnapshotGraph()
    kernel.bind_services(storage, graph, None, None, searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    payload = await kernel._memory_search(  # noqa: SLF001
        actor=actor,
        query="расскажи подробно про проект Альфа",
        as_of="01.06.2024",
    )

    call = searcher.calls[0]
    assert call["as_of"] == "2024-06-01"
    assert call["kg"] is graph
    assert call["graph_expansion"] is True
    assert payload["as_of"] == "2024-06-01"
    snapshot = payload["graph_context"]
    assert snapshot["query"] == "расскажи подробно про проект Альфа"
    assert snapshot["as_of"] == "2024-06-01"
    assert snapshot["temporal_basis"] == "valid_time"
    assert snapshot["expanded"] is True
    assert snapshot["paths"]
    assert len(snapshot["paths"]) <= 6
    assert snapshot["paths_matched_at_least"] == 12
    assert snapshot["paths_truncated"] is True
    first_path = snapshot["paths"][0]
    assert first_path["path_id"] == "path-0"
    first_edge = first_path["edges"][0]
    assert first_edge["direction"] == "forward"
    assert first_edge["valid_from"] == "2024-01-01"
    assert first_edge["provenance"]["knowledge_object_id"] == "ko-one"
    assert first_edge["provenance"]["source"] == "reviewed_relation_candidate"
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert "metadata_json" not in encoded
    assert "evidence_text" not in encoded
    assert '"grounded"' not in encoded
    assert len(encoded) <= 3_200


def test_memory_graph_snapshot_bounds_pathological_counts() -> None:
    bounded = _memory_graph_context_for_llm(
        {
            "paths": [],
            "paths_matched_at_least": 10**3_500,
            "temporal_basis": "valid_time",
        },
        query="с кем работал Альфа",
        as_of="2024-06-01",
    )

    assert bounded["paths_matched_at_least"] == 1_000_000_000
    encoded = json.dumps(bounded, ensure_ascii=False)
    assert len(encoded) <= 3_200
    assert json.loads(encoded) == bounded

    huge = 10**5_000
    text_bounded = _memory_graph_context_for_llm(
        {"query": huge},
        query=huge,  # type: ignore[arg-type]
        as_of="",
    )
    assert text_bounded["query"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expands"),
    [
        ("с кем работал проект Альфа", True),
        ("расскажи подробно про проект Альфа", False),
    ],
)
async def test_memory_search_expands_a_current_graph_only_for_relational_queries(
    settings,
    storage,
    query,
    expands,
):
    storage.ensure_user("alice")
    searcher = _SnapshotSearcher({"results": [], "strategy": {}, "graph_context": {}})
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    graph = _SnapshotGraph()
    kernel.bind_services(storage, graph, None, None, searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    await kernel._memory_search(actor=actor, query=query)  # noqa: SLF001

    assert searcher.calls[0]["kg"] is graph
    assert searcher.calls[0]["as_of"] == ""
    assert searcher.calls[0]["graph_expansion"] is expands


def test_memory_search_publishes_as_of_in_its_tool_spec(settings, storage):
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    properties = kernel._tools["memory_search"].parameters["properties"]  # noqa: SLF001

    assert properties["as_of"]["type"] == "string"
    assert "ГГГГ-ММ-ДД" in properties["as_of"]["description"]
