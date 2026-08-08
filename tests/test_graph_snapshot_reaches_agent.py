"""The graph explanation used for ranking is the one shown to the model.

These tests guard the wiring added by proposal 26.  A graph implementation can
return perfectly historical, provenance-rich paths and still be unsafe if the
agent silently traverses a second, current snapshot or drops the path before it
reaches the prompt.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _graph_paths_for_prompt,
    _historical_tool_graph_context,
)
from friday.execution_kernel import ExecutionKernel, ToolResult, _memory_graph_context_for_llm
from friday.permissions import ActorContext, AuthorizationService

REQUESTED_KNOWN_AT = "2026-08-04T12:30:00+03:00"
NORMALIZED_KNOWN_AT = "2026-08-04T09:30:00.000000Z"
KNOWN_AT_FLOOR = "2026-08-01T00:00:00.000000Z"


def _known_at_status(known_at: str = NORMALIZED_KNOWN_AT) -> dict[str, Any]:
    return {
        "known_at": known_at,
        "known_at_floor": KNOWN_AT_FLOOR,
        "history_complete": True,
        "identity_basis": "current_names",
    }


def _complete_known_snapshot(**extra: Any) -> dict[str, Any]:
    return {
        "as_of": "",
        **_known_at_status(),
        "temporal_basis": "bitemporal",
        **extra,
    }


class _SnapshotGraph:
    def __init__(self, fallback: Any = None) -> None:
        self.fallback = {"paths": [{"path_id": "wrong-second-traversal"}]} if fallback is None else fallback
        self.context_calls: list[dict[str, Any]] = []

    def get_stats(self, user_id: str) -> dict[str, int]:
        return {
            "relation_count": 0,
            "pending_inbox": 0,
            "pending_resolutions": 0,
            "pending_relation_candidates": 0,
            "pending_conflicts": 0,
        }

    def context_for_query(self, user_id: str, query: str, **kwargs: Any) -> Any:
        self.context_calls.append({"user_id": user_id, "query": query, **kwargs})
        return self.fallback


class _SnapshotSearcher:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def search(self, user_id: str, query: str, **kwargs: Any) -> Any:
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
        "reviewed": True,
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


def test_prompt_names_the_complete_bitemporal_snapshot(settings, storage):
    storage.ensure_user("alice")
    context = AgentContext(
        conversation_id="conv-bitemporal-prompt",
        user_id="alice",
        search_query="кто был известен к тому моменту",
        graph_context={
            "query": "исправленный запрос",
            "as_of": "2024-06-01",
            **_known_at_status(),
            "temporal_basis": "bitemporal",
            "expanded": True,
            "paths": [],
            "paths_matched_at_least": 0,
            "paths_truncated": False,
        },
    )

    payload = _context_data(
        AgentRuntime(settings, storage)._build_initial_messages(  # noqa: SLF001
            context,
            "Что было известно?",
            None,
            tool_enabled=False,
        )
    )

    assert payload["graph_snapshot"] == {
        "query": "исправленный запрос",
        "as_of": "2024-06-01",
        "known_at": NORMALIZED_KNOWN_AT,
        "known_at_floor": KNOWN_AT_FLOOR,
        "history_complete": True,
        "identity_basis": "current_names",
        "temporal_basis": "bitemporal",
        "expanded": True,
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }


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

    forged_identity_edge = {
        **anchored,
        "provenance": {
            **anchored["provenance"],
            "reviewed": False,
            "reviewed_by": "PRIVATE REVIEWER SENTINEL",
            "created_by": "PRIVATE CREATOR SENTINEL",
        },
    }
    forged_identity = _graph_paths_for_prompt(
        {
            "paths": [
                {
                    "path_id": "forged-reviewer",
                    "root": "a",
                    "target": "b",
                    "entity_ids": ["a", "b"],
                    "edges": [forged_identity_edge],
                }
            ]
        },
        {"ko-one": "K1"},
    )
    assert forged_identity[0]["grounded"] is False
    assert "PRIVATE REVIEWER SENTINEL" not in json.dumps(forged_identity, ensure_ascii=False)
    assert "PRIVATE CREATOR SENTINEL" not in json.dumps(forged_identity, ensure_ascii=False)

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
@pytest.mark.parametrize(
    "invalid",
    ["2026-08-04", "2026-08-04T09:30:00", "не timestamp"],
)
async def test_memory_search_rejects_invalid_known_at_before_any_read(
    settings,
    storage,
    monkeypatch,
    invalid,
):
    storage.ensure_user("alice")
    searcher = _SnapshotSearcher({"results": []})
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    graph = _SnapshotGraph()
    kernel.bind_services(storage, graph, None, None, searcher=searcher)  # type: ignore[arg-type]
    touched: list[str] = []

    def status(*args, **kwargs):
        del args, kwargs
        touched.append("history")
        raise AssertionError("invalid known_at reached storage")

    monkeypatch.setattr(storage, "relation_history_status", status)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    with pytest.raises(ValueError, match="offset-aware RFC3339"):
        await kernel._memory_search(  # noqa: SLF001
            actor=actor,
            query="с кем работал Альфа",
            known_at=invalid,
        )

    assert touched == []
    assert searcher.calls == []


@pytest.mark.asyncio
async def test_memory_search_refuses_an_unavailable_known_at_before_search(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    searcher = _SnapshotSearcher({"results": []})
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, _SnapshotGraph(), None, None, searcher=searcher)  # type: ignore[arg-type]

    def status(_user_id: str, *, known_at: str = "") -> dict[str, Any]:
        assert known_at == NORMALIZED_KNOWN_AT
        raise ValueError("known_at crosses a later entity merge")

    monkeypatch.setattr(storage, "relation_history_status", status)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    with pytest.raises(ValueError, match="crosses a later entity merge"):
        await kernel._memory_search(  # noqa: SLF001
            actor=actor,
            query="с кем работал Альфа",
            known_at=REQUESTED_KNOWN_AT,
        )

    assert searcher.calls == []


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
            "temporal_basis": "valid_time",
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


@pytest.mark.asyncio
async def test_memory_search_passes_one_known_at_and_publishes_a_complete_bounded_snapshot(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    status_calls: list[str] = []

    def status(_user_id: str, *, known_at: str = "") -> dict[str, Any]:
        status_calls.append(known_at)
        return _known_at_status(known_at)

    monkeypatch.setattr(storage, "relation_history_status", status)
    searcher = _SnapshotSearcher(
        {
            "query": "исправленный запрос",
            "results": [],
            "strategy": {},
            "as_of": "",
            **_known_at_status(),
            "temporal_basis": "bitemporal",
            "graph_context": {
                "query": "исправленный запрос",
                "as_of": "",
                **_known_at_status(),
                "temporal_basis": "bitemporal",
                "expanded": True,
                "paths": [],
            },
        }
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    graph = _SnapshotGraph()
    kernel.bind_services(storage, graph, None, None, searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    payload = await kernel._memory_search(  # noqa: SLF001
        actor=actor,
        query="расскажи подробно про Альфа",
        known_at=REQUESTED_KNOWN_AT,
    )

    assert status_calls == [NORMALIZED_KNOWN_AT, NORMALIZED_KNOWN_AT]
    call = searcher.calls[0]
    assert call["known_at"] == NORMALIZED_KNOWN_AT
    assert call["graph_expansion"] is True
    assert payload["known_at"] == NORMALIZED_KNOWN_AT
    assert payload["known_at_floor"] == KNOWN_AT_FLOOR
    assert payload["history_complete"] is True
    assert payload["identity_basis"] == "current_names"
    assert payload["temporal_basis"] == "bitemporal"
    assert payload["graph_context"] == {
        "query": "исправленный запрос",
        "expanded": True,
        "as_of": "",
        "known_at": NORMALIZED_KNOWN_AT,
        "known_at_floor": KNOWN_AT_FLOOR,
        "history_complete": True,
        "identity_basis": "current_names",
        "temporal_basis": "bitemporal",
        "roots": [],
        "nodes": [],
        "entities": [],
        "relations": [],
        "paths": [],
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }


@pytest.mark.asyncio
async def test_memory_search_refuses_a_torn_snapshot_before_return(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    status_calls = 0

    def status(_user_id: str, *, known_at: str = "") -> dict[str, Any]:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 2:
            raise ValueError("known_at crosses a merge committed during search")
        return _known_at_status(known_at)

    monkeypatch.setattr(storage, "relation_history_status", status)
    searcher = _SnapshotSearcher(
        {
            "query": "Atlas",
            "results": [],
            "as_of": "",
            **_known_at_status(),
            "temporal_basis": "bitemporal",
            "graph_context": {
                "as_of": "",
                **_known_at_status(),
                "temporal_basis": "bitemporal",
                "paths": [],
            },
        }
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, _SnapshotGraph(), None, None, searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    with pytest.raises(ValueError, match="merge committed during search"):
        await kernel._memory_search(  # noqa: SLF001
            actor=actor,
            query="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )

    assert status_calls == 2
    assert len(searcher.calls) == 1


@pytest.mark.asyncio
async def test_memory_search_refuses_metadata_that_disagrees_with_storage(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": _known_at_status(known_at),
    )
    searcher = _SnapshotSearcher(
        {
            "results": [],
            "as_of": "",
            **_known_at_status(),
            "temporal_basis": "bitemporal",
            "graph_context": {
                "as_of": "",
                **_known_at_status("2026-08-04T09:29:59.000000Z"),
                "temporal_basis": "bitemporal",
                "paths": [],
            },
        }
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, _SnapshotGraph(), None, None, searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    with pytest.raises(ValueError, match="disagrees on known_at"):
        await kernel._memory_search(  # noqa: SLF001
            actor=actor,
            query="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )


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
    assert searcher.calls[0]["known_at"] == ""
    assert searcher.calls[0]["graph_expansion"] is expands


def test_graph_tools_distinguish_valid_time_from_transaction_time(settings, storage):
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    for name in ("memory_search", "entity_lookup"):
        tool = kernel._tools[name]  # noqa: SLF001
        properties = tool.parameters["properties"]
        assert properties["as_of"]["type"] == "string"
        assert "ГГГГ-ММ-ДД" in properties["as_of"]["description"]
        assert properties["known_at"]["type"] == "string"
        assert properties["known_at"]["format"] == "date-time"
        assert "RFC3339" in properties["known_at"]["description"]
        assert "transaction-time" in tool.description
        assert "valid-time" in tool.description


class _EntitySnapshotGraph:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def find_entity(self, user_id: str, name: str) -> dict[str, Any]:
        self.events.append(("find", (user_id, name)))
        return {"id": "ent-atlas", "name": "Atlas", "entity_type": "project"}

    def entity_profile(self, entity_id: str, user_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.events.append(("profile", (entity_id, user_id)))
        return {"profile": {"tags": []}, "knowledge_objects": []}

    def get_entity_graph(
        self,
        user_id: str,
        entity_id: str,
        depth: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.events.append(("graph", (user_id, entity_id, depth, kwargs)))
        return {
            "as_of": str(kwargs.get("as_of") or ""),
            "nodes": [
                {"id": "ent-atlas", "name": "Atlas"},
                {"id": "ent-person", "name": "Иван"},
            ],
            "edges": [
                {
                    "relation_type": "managed_by",
                    "source_entity_id": "ent-atlas",
                    "target_entity_id": "ent-person",
                    "valid_from": "2024-01-01",
                    "valid_to": "",
                }
            ],
            **_known_at_status(),
            "temporal_basis": "bitemporal",
        }


@pytest.mark.asyncio
async def test_entity_lookup_normalizes_known_at_before_reads_and_passes_both_axes(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    graph = _EntitySnapshotGraph()

    def status(_user_id: str, *, known_at: str = "") -> dict[str, Any]:
        graph.events.append(("status", known_at))
        return _known_at_status(known_at)

    monkeypatch.setattr(storage, "relation_history_status", status)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = await kernel._entity_lookup(  # noqa: SLF001
        actor=actor,
        name="Atlas",
        as_of="2024-06-01",
        known_at=REQUESTED_KNOWN_AT,
    )

    assert [event for event, _value in graph.events] == [
        "status",
        "find",
        "profile",
        "graph",
        "status",
    ]
    assert graph.events[0] == ("status", NORMALIZED_KNOWN_AT)
    graph_call = next(value for event, value in graph.events if event == "graph")
    assert graph_call[3]["as_of"] == "2024-06-01"
    assert graph_call[3]["known_at"] == NORMALIZED_KNOWN_AT
    assert result["known_at"] == NORMALIZED_KNOWN_AT
    assert result["known_at_floor"] == KNOWN_AT_FLOOR
    assert result["history_complete"] is True
    assert result["identity_basis"] == "current_names"
    assert result["temporal_basis"] == "bitemporal"
    assert result["relations"] == [
        {
            "type": "managed_by",
            "source": "Atlas",
            "target": "Иван",
            "valid_from": "2024-01-01",
            "valid_to": "",
        }
    ]
    assert result["relations_matched_at_least"] == 1
    assert result["relations_truncated"] is False


@pytest.mark.asyncio
async def test_entity_lookup_refuses_an_identity_change_during_its_reads(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    graph = _EntitySnapshotGraph()
    status_calls = 0

    def status(_user_id: str, *, known_at: str = "") -> dict[str, Any]:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 2:
            raise ValueError("known_at crosses a merge committed during lookup")
        return _known_at_status(known_at)

    monkeypatch.setattr(storage, "relation_history_status", status)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    with pytest.raises(ValueError, match="merge committed during lookup"):
        await kernel._entity_lookup(  # noqa: SLF001
            actor=actor,
            name="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )

    assert status_calls == 2


@pytest.mark.asyncio
async def test_entity_lookup_rejects_invalid_known_at_before_status_or_entity_reads(
    settings,
    storage,
    monkeypatch,
):
    graph = _EntitySnapshotGraph()

    def status(*args, **kwargs):
        del args, kwargs
        graph.events.append(("status", "unexpected"))
        raise AssertionError("invalid known_at reached storage")

    monkeypatch.setattr(storage, "relation_history_status", status)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    with pytest.raises(ValueError, match="offset-aware RFC3339"):
        await kernel._entity_lookup(  # noqa: SLF001
            actor=actor,
            name="Atlas",
            known_at="2026-08-04T09:30:00",
        )

    assert graph.events == []


class _HistoricalToolLLM:
    enabled = True
    total_budget_sec = 120.0
    model = "historical-tool-test"

    def __init__(self, arguments: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self.arguments = arguments or {"query": "Atlas", "known_at": REQUESTED_KNOWN_AT}

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        del messages, temperature, max_tokens
        self.calls += 1
        if self.calls == 1 and tools:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-history",
                        "function": {
                            "name": "memory_search",
                            "arguments": json.dumps(self.arguments),
                        },
                    }
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "Исторический снимок проверен.", "tool_calls": None, "_queue_wait_sec": 0.0}


def _graph_tool_schemas(*names: str) -> list[dict[str, Any]]:
    assert names
    return [
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        }
        for name in names
    ]


class _HistoricalToolKernel:
    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete

    def get_tool_definitions(self, actor, *, topic=None):
        del actor, topic
        return [
            {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "Поиск",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute(self, name, arguments, *, actor=None):
        del arguments, actor
        snapshot = {
            "query": "Atlas",
            "as_of": "",
            **_known_at_status(),
            "history_complete": self.complete,
            "temporal_basis": "bitemporal",
            "expanded": True,
            "paths": [],
            "paths_matched_at_least": 0,
            "paths_truncated": False,
            "nodes": [{"id": "ent-secret", "name": "Личное имя не хранить"}],
        }
        return ToolResult(
            name,
            True,
            data={
                "count": 0,
                "query": "Atlas",
                "as_of": "",
                **_known_at_status(),
                "history_complete": self.complete,
                "temporal_basis": "bitemporal",
                "graph_context": snapshot,
                "results": [],
            },
        )


class _GraphlessToolKernel(_HistoricalToolKernel):
    async def execute(self, name, arguments, *, actor=None):
        del arguments, actor
        return ToolResult(name, True, data={"count": 0, "query": "Atlas", "results": []})


class _IdentityCurrentToolKernel(_HistoricalToolKernel):
    def __init__(self) -> None:
        super().__init__()
        sentinel = "CURRENT_PRIVATE_SENTINEL_" + "x" * 250_000
        self.payload = {
            "count": 0,
            "query": "Atlas",
            "strategy": {"legacy_marker": "must-stay"},
            "graph_context": {
                "query": "Atlas",
                "as_of": "",
                "known_at": "",
                "temporal_basis": "valid_time",
                "paths": [],
                "nodes": [{"id": "ent-atlas", "metadata_json": {"secret": sentinel}}],
                "legacy_graph_marker": sentinel,
            },
            "results": [],
            "private": sentinel,
        }
        self.result: ToolResult | None = None

    async def execute(self, name, arguments, *, actor=None):
        del arguments, actor
        self.result = ToolResult(name, True, data=self.payload)
        return self.result


@pytest.mark.asyncio
async def test_historical_tool_snapshot_becomes_the_durable_boundary_without_graph_payload(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_HistoricalToolLLM(),
        kernel=_HistoricalToolKernel(),  # type: ignore[arg-type]
    )
    prepared: list[AgentContext] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        context = AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            search_query="Atlas",
            conversation_history=[],
            interaction_mode="dialogue",
        )
        prepared.append(context)
        return context

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    result = await runtime.chat(
        "alice",
        "Проверь сохранённый исторический снимок",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert prepared[0].graph_context["known_at"] == NORMALIZED_KNOWN_AT
    messages = storage.get_conversation_messages(result["conversation_id"], user_id="alice", limit=20)
    assistant = next(item for item in reversed(messages) if item["role"] == "assistant")
    metadata = json.loads(assistant["metadata_json"])
    assert metadata["graph_snapshot"] == {
        "as_of": "",
        "known_at": NORMALIZED_KNOWN_AT,
        "known_at_floor": KNOWN_AT_FLOOR,
        "history_complete": True,
        "identity_basis": "current_names",
        "temporal_basis": "bitemporal",
        "paths": 0,
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }
    encoded = json.dumps(metadata["graph_snapshot"], ensure_ascii=False)
    assert "Личное имя" not in encoded
    assert "nodes" not in metadata["graph_snapshot"]


@pytest.mark.asyncio
async def test_agent_loop_refuses_an_incomplete_historical_tool_snapshot(settings, storage):
    storage.ensure_user("alice")
    llm = _HistoricalToolLLM()
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,
        kernel=_HistoricalToolKernel(complete=False),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-incomplete-history",
        user_id="alice",
        interaction_mode="dialogue",
    )
    original = context.graph_context

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь снимок",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search"),
        attachments=None,
    )

    assert context.graph_context is original
    assert result["tool_evidence"] == []


@pytest.mark.asyncio
async def test_graphless_current_tool_does_not_erase_the_effective_snapshot(settings, storage):
    storage.ensure_user("alice")
    runtime = AgentRuntime(
        settings,
        storage,
        llm=_HistoricalToolLLM({"query": "Atlas"}),
        kernel=_GraphlessToolKernel(),  # type: ignore[arg-type]
    )
    original = {
        "query": "previous historical query",
        **_known_at_status(),
        "temporal_basis": "bitemporal",
        "paths": [],
    }
    context = AgentContext(
        conversation_id="conv-current-graphless",
        user_id="alice",
        interaction_mode="dialogue",
        graph_context=original,
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь ещё раз",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search"),
        attachments=None,
    )

    assert context.graph_context is original
    assert len(result["tool_evidence"]) == 1


@pytest.mark.asyncio
async def test_current_graph_tool_data_is_safe_projected_before_model_evidence(settings, storage):
    kernel = _IdentityCurrentToolKernel()
    runtime = AgentRuntime(
        settings,
        storage,
        llm=_HistoricalToolLLM({"query": "Atlas"}),
        kernel=kernel,  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-current-identity",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь текущий снимок",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search"),
        attachments=None,
    )

    assert kernel.result is not None
    assert kernel.result.data is not kernel.payload
    encoded = json.dumps(kernel.result.data, ensure_ascii=False)
    assert "CURRENT_PRIVATE_SENTINEL" not in encoded
    assert "metadata_json" not in encoded
    assert "legacy_graph_marker" not in encoded
    assert "strategy" not in kernel.result.data
    assert len(encoded) < 10_500
    assert len(result["tool_evidence"]) == 1
    assert "CURRENT_PRIVATE_SENTINEL" not in result["tool_evidence"][0]["output"]


@pytest.mark.asyncio
async def test_agent_loop_refuses_a_tool_that_drops_requested_known_at(settings, storage):
    storage.ensure_user("alice")
    runtime = AgentRuntime(
        settings,
        storage,
        llm=_HistoricalToolLLM(),
        kernel=_GraphlessToolKernel(),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-dropped-history",
        user_id="alice",
        interaction_mode="dialogue",
    )
    original = context.graph_context

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь снимок",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search"),
        attachments=None,
    )

    assert context.graph_context is original
    assert result["tool_evidence"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", ["as_of", "known_at"])
@pytest.mark.parametrize("bad_value", [None, False, 0, [], {}])
async def test_memory_search_rejects_present_non_string_temporal_axes_before_reads(
    settings,
    storage,
    monkeypatch,
    axis,
    bad_value,
):
    reads: list[str] = []
    searcher = _SnapshotSearcher({"results": []})
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: reads.append("status"),
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, _SnapshotGraph(), None, None, searcher=searcher)  # type: ignore[arg-type]
    kwargs = {axis: bad_value}

    with pytest.raises(ValueError, match=f"{axis} must be a string"):
        await kernel._memory_search(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            query="Atlas",
            **kwargs,
        )

    assert reads == []
    assert searcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", ["as_of", "known_at"])
@pytest.mark.parametrize("bad_value", [None, False, 0, [], {}])
async def test_entity_lookup_rejects_present_non_string_temporal_axes_before_reads(
    settings,
    storage,
    monkeypatch,
    axis,
    bad_value,
):
    graph = _EntitySnapshotGraph()
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: graph.events.append(("status", "unexpected")),
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]
    kwargs = {axis: bad_value}

    with pytest.raises(ValueError, match=f"{axis} must be a string"):
        await kernel._entity_lookup(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            name="Atlas",
            **kwargs,
        )

    assert graph.events == []


@pytest.mark.asyncio
async def test_current_graph_tools_do_not_read_relation_history(settings, storage, monkeypatch):
    graph = _EntitySnapshotGraph()
    searcher = _SnapshotSearcher({"results": [], "strategy": {}, "graph_context": {}})

    def history_must_not_run(*_args, **_kwargs):
        raise AssertionError("current tool read relation history")

    monkeypatch.setattr(storage, "relation_history_status", history_must_not_run)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object(), searcher=searcher)  # type: ignore[arg-type]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    await kernel._memory_search(actor=actor, query="Atlas")  # noqa: SLF001
    entity = await kernel._entity_lookup(actor=actor, name="Atlas")  # noqa: SLF001

    assert entity["found"] is True
    assert searcher.calls[0]["as_of"] == ""
    assert searcher.calls[0]["known_at"] == ""


class _MissingEntityGraph(_EntitySnapshotGraph):
    def find_entity(self, user_id: str, name: str) -> None:
        self.events.append(("find", (user_id, name)))
        return None


@pytest.mark.asyncio
async def test_entity_lookup_normalizes_year_and_missing_entity_echoes_valid_time(
    settings,
    storage,
    monkeypatch,
):
    graph = _MissingEntityGraph()
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("as_of-only lookup read relation history")
        ),
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]

    result = await kernel._entity_lookup(  # noqa: SLF001
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        name="Нет такой",
        as_of="2024",
    )

    assert result == {
        "found": False,
        "entity": None,
        "as_of": "2024-01-01",
        "known_at": "",
        "temporal_basis": "valid_time",
    }
    assert [event for event, _value in graph.events] == ["find"]


@pytest.mark.asyncio
async def test_entity_lookup_rejects_invalid_as_of_before_any_read(settings, storage, monkeypatch):
    graph = _EntitySnapshotGraph()
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda *_args, **_kwargs: graph.events.append(("status", "unexpected")),
    )
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid date"):
        await kernel._entity_lookup(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            name="Atlas",
            as_of="2024-13",
        )

    assert graph.events == []


class _ConflictingEntityGraph(_EntitySnapshotGraph):
    def find_entity(self, user_id: str, name: str) -> dict[str, Any]:
        self.events.append(("find", (user_id, name)))
        return {
            "id": "ent-atlas",
            "name": "Atlas",
            "entity_type": "project",
            "metadata_json": {"secret": "ENTITY_RAW_SECRET"},
            "internal_owner": "PROFILE_RAW_SECRET",
        }

    def entity_profile(self, entity_id: str, user_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.events.append(("profile", (entity_id, user_id)))
        return {
            "profile": {"tags": []},
            "knowledge_objects": [],
            "relations": [
                {
                    "relation_type": "current_conflict",
                    "source_name": "Atlas",
                    "target_name": f"Текущий {index}",
                    "metadata_json": {"secret": "CURRENT_RAW_SECRET"},
                }
                for index in range(20)
            ],
            "relations_matched_at_least": 20,
            "relations_truncated": False,
            "private_profile_context": "PROFILE_RAW_SECRET",
        }

    def get_entity_graph(
        self,
        user_id: str,
        entity_id: str,
        depth: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.events.append(("graph", (user_id, entity_id, depth, kwargs)))
        return {
            "as_of": str(kwargs.get("as_of") or ""),
            "temporal_basis": "valid_time",
            "nodes": [
                {"id": "ent-atlas", "name": "Atlas", "metadata_json": {"secret": "NODE"}},
                *[{"id": f"ent-{index}", "name": f"Исторический {index}"} for index in range(20)],
            ],
            "edges": [
                {
                    "relation_type": "managed_by",
                    "source_entity_id": "ent-atlas",
                    "target_entity_id": f"ent-{index}",
                    "valid_from": "2024-01-01",
                    "valid_to": "",
                    "metadata_json": {"secret": "HISTORICAL_RAW_SECRET"},
                    "provenance": {"evidence": "HISTORICAL_LONG_EVIDENCE" * 1000},
                }
                for index in range(20)
            ],
        }


@pytest.mark.asyncio
async def test_entity_lookup_returns_only_bounded_current_relations(settings, storage):
    graph = _ConflictingEntityGraph()
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]

    result = await kernel._entity_lookup(  # noqa: SLF001
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        name="Atlas",
    )

    assert len(result["relations"]) == 12
    assert result["relations_matched_at_least"] == 20
    assert result["relations_truncated"] is True
    assert result["relations"][0] == {
        "type": "current_conflict",
        "source": "Atlas",
        "target": "Текущий 0",
        "valid_from": "",
        "valid_to": "",
    }
    assert set(result["entity"]) == {"id", "name", "entity_type"}
    encoded = json.dumps(result, ensure_ascii=False)
    assert "ENTITY_RAW_SECRET" not in encoded
    assert "CURRENT_RAW_SECRET" not in encoded
    assert "PROFILE_RAW_SECRET" not in encoded
    assert "metadata_json" not in encoded


@pytest.mark.asyncio
async def test_entity_lookup_returns_only_bounded_historical_relations(settings, storage):
    graph = _ConflictingEntityGraph()
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]

    result = await kernel._entity_lookup(  # noqa: SLF001
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        name="Atlas",
        as_of="2024",
    )

    assert result["as_of"] == "2024-01-01"
    assert len(result["relations"]) == 12
    assert result["relations_matched_at_least"] == 20
    assert result["relations_truncated"] is True
    assert result["relations"][0] == {
        "type": "managed_by",
        "source": "Atlas",
        "target": "Исторический 0",
        "valid_from": "2024-01-01",
        "valid_to": "",
    }
    encoded = json.dumps(result, ensure_ascii=False)
    assert "current_conflict" not in encoded
    assert "CURRENT_RELATION_SECRET" not in encoded
    assert "CURRENT_RAW_SECRET" not in encoded
    assert "HISTORICAL_RAW_SECRET" not in encoded
    assert "HISTORICAL_LONG_EVIDENCE" not in encoded
    assert "relations_as_of" not in result


@pytest.mark.asyncio
async def test_entity_lookup_requires_exact_graph_as_of_echo(settings, storage):
    graph = _EntitySnapshotGraph()
    original = graph.get_entity_graph

    def mismatched(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        result["as_of"] = "2024-01-02"
        result["temporal_basis"] = "valid_time"
        return result

    graph.get_entity_graph = mismatched  # type: ignore[method-assign]
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="disagrees on as_of"):
        await kernel._entity_lookup(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            name="Atlas",
            as_of="2024",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["envelope", "graph_context"])
@pytest.mark.parametrize(
    "missing",
    [None, "known_at", "known_at_floor", "history_complete", "identity_basis", "temporal_basis"],
)
async def test_memory_search_rejects_nonmapping_or_incomplete_known_at_contracts(
    settings,
    storage,
    monkeypatch,
    location,
    missing,
):
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": _known_at_status(known_at),
    )
    graph_context: Any = _complete_known_snapshot(paths=[])
    response: Any = _complete_known_snapshot(
        results=[],
        strategy={},
        graph_context=graph_context,
    )
    if location == "envelope":
        if missing is None:
            response = []
        else:
            del response[missing]
    elif missing is None:
        response["graph_context"] = []
    else:
        del response["graph_context"][missing]
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(
        storage,
        _SnapshotGraph(),
        None,
        None,
        searcher=_SnapshotSearcher(response),
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="mapping|missing"):
        await kernel._memory_search(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            query="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )


@pytest.mark.asyncio
async def test_memory_search_requires_exact_boolean_history_contract(settings, storage, monkeypatch):
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": _known_at_status(known_at),
    )
    graph_context = _complete_known_snapshot(paths=[])
    response = _complete_known_snapshot(
        results=[],
        strategy={},
        graph_context=graph_context,
    )
    response["history_complete"] = 1
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(
        storage,
        _SnapshotGraph(),
        None,
        None,
        searcher=_SnapshotSearcher(response),
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="disagrees on history_complete"):
        await kernel._memory_search(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            query="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [None, "as_of", "known_at", "known_at_floor", "history_complete", "identity_basis", "temporal_basis"],
)
async def test_memory_search_rejects_nonmapping_or_incomplete_kg_fallback(
    settings,
    storage,
    monkeypatch,
    missing,
):
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": _known_at_status(known_at),
    )
    fallback: Any = _complete_known_snapshot(paths=[])
    if missing is None:
        fallback = []
    else:
        del fallback[missing]
    response = _complete_known_snapshot(results=[], strategy={})
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(
        storage,
        _SnapshotGraph(fallback),
        None,
        None,
        searcher=_SnapshotSearcher(response),
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="mapping|missing"):
        await kernel._memory_search(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            query="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )


@pytest.mark.asyncio
async def test_memory_search_refuses_temporal_success_without_any_graph_snapshot(
    settings,
    storage,
    monkeypatch,
):
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": _known_at_status(known_at),
    )
    response = _complete_known_snapshot(results=[], strategy={})
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(
        storage,
        None,
        None,
        None,
        searcher=_SnapshotSearcher(response),
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="snapshot is unavailable"):
        await kernel._memory_search(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            query="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )


@pytest.mark.asyncio
async def test_memory_search_without_searcher_or_kg_refuses_temporal_success(settings, storage):
    storage.ensure_user("alice")
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="snapshot is unavailable"):
        await kernel._memory_search(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            query="Atlas",
            as_of="2024-01-01",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [None, "known_at", "known_at_floor", "history_complete", "identity_basis", "temporal_basis"],
)
async def test_entity_lookup_rejects_nonmapping_or_incomplete_graph_contract(
    settings,
    storage,
    monkeypatch,
    missing,
):
    graph = _EntitySnapshotGraph()
    monkeypatch.setattr(
        storage,
        "relation_history_status",
        lambda _user_id, *, known_at="": _known_at_status(known_at),
    )
    response: Any = _complete_known_snapshot(nodes=[], edges=[])
    if missing is None:
        response = []
    else:
        del response[missing]
    graph.get_entity_graph = lambda *_args, **_kwargs: response  # type: ignore[method-assign]
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="mapping|missing"):
        await kernel._entity_lookup(  # noqa: SLF001
            actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
            name="Atlas",
            known_at=REQUESTED_KNOWN_AT,
        )


@pytest.mark.parametrize("location", ["envelope", "graph_context"])
@pytest.mark.parametrize(
    "missing",
    [None, "known_at", "known_at_floor", "history_complete", "identity_basis", "temporal_basis"],
)
def test_runtime_requires_complete_known_at_contract_in_both_memory_layers(location, missing):
    snapshot: Any = _complete_known_snapshot(query="Atlas", paths=[])
    payload: Any = _complete_known_snapshot(
        query="Atlas",
        results=[],
        graph_context=snapshot,
    )
    if location == "envelope":
        if missing is None:
            payload = []
        else:
            del payload[missing]
    elif missing is None:
        payload["graph_context"] = []
    else:
        del payload["graph_context"][missing]

    with pytest.raises(ValueError):
        _historical_tool_graph_context(
            "memory_search",
            payload,
            {"query": "Atlas", "known_at": REQUESTED_KNOWN_AT},
        )


@pytest.mark.parametrize(
    "location",
    ["envelope_only", "snapshot_only", "missing_graph"],
)
def test_runtime_never_falls_back_between_as_of_memory_layers(location):
    snapshot: Any = {
        "query": "Atlas",
        "as_of": "2024-01-01",
        "known_at": "",
        "temporal_basis": "valid_time",
        "paths": [],
    }
    payload: dict[str, Any] = {
        "query": "Atlas",
        "as_of": "2024-01-01",
        "known_at": "",
        "temporal_basis": "valid_time",
        "results": [],
        "graph_context": snapshot,
    }
    if location == "envelope_only":
        del snapshot["as_of"]
    elif location == "snapshot_only":
        del payload["as_of"]
    else:
        payload["graph_context"] = None

    with pytest.raises(ValueError):
        _historical_tool_graph_context(
            "memory_search",
            payload,
            {"query": "Atlas", "as_of": "2024-01-01"},
        )


@pytest.mark.parametrize(
    "missing",
    [None, "known_at", "known_at_floor", "history_complete", "identity_basis", "temporal_basis"],
)
def test_runtime_requires_complete_entity_known_at_payload(missing):
    payload: Any = _complete_known_snapshot(found=True, entity={"id": "ent-atlas"}, relations=[])
    if missing is None:
        payload = []
    else:
        del payload[missing]

    with pytest.raises(ValueError):
        _historical_tool_graph_context(
            "entity_lookup",
            payload,
            {"name": "Atlas", "known_at": REQUESTED_KNOWN_AT},
        )


@pytest.mark.parametrize("axis", ["as_of", "known_at"])
@pytest.mark.parametrize("bad_value", [None, False, 0, [], {}])
def test_runtime_rejects_present_non_string_temporal_arguments(axis, bad_value):
    with pytest.raises(ValueError, match=f"requested {axis} must be a string"):
        _historical_tool_graph_context(
            "entity_lookup",
            {"found": False, "entity": None},
            {"name": "Atlas", axis: bad_value},
        )


def _valid_time_tool_payload(tool_name: str, as_of: str) -> dict[str, Any]:
    if tool_name == "entity_lookup":
        return {
            "found": True,
            "entity": {"id": "ent-atlas", "name": "Atlas"},
            "as_of": as_of,
            "known_at": "",
            "temporal_basis": "valid_time",
            "relations": [],
        }
    snapshot = {
        "query": "Atlas",
        "as_of": as_of,
        "known_at": "",
        "temporal_basis": "valid_time",
        "paths": [],
    }
    return {
        "query": "Atlas",
        "as_of": as_of,
        "known_at": "",
        "temporal_basis": "valid_time",
        "graph_context": snapshot,
        "results": [],
    }


@pytest.mark.parametrize(
    ("tool_name", "requested", "expected"),
    [
        ("memory_search", "2024-06-01", "2024-06-01"),
        ("memory_search", "01.06.2024", "2024-06-01"),
        ("entity_lookup", "2024", "2024-01-01"),
        ("entity_lookup", "2024-06", "2024-06-01"),
        ("entity_lookup", "2024-06-01", "2024-06-01"),
    ],
)
def test_runtime_normalizes_as_of_exactly_like_each_tool_producer(
    tool_name,
    requested,
    expected,
):
    argument_name = "query" if tool_name == "memory_search" else "name"

    adopted = _historical_tool_graph_context(
        tool_name,
        _valid_time_tool_payload(tool_name, expected),
        {argument_name: "Atlas", "as_of": requested},
    )

    assert adopted is not None
    assert adopted["as_of"] == expected
    assert adopted["known_at"] == ""
    assert adopted["temporal_basis"] == "valid_time"


@pytest.mark.parametrize(
    ("tool_name", "requested"),
    [
        ("memory_search", "2024"),
        ("memory_search", "2024-06"),
        ("entity_lookup", "01.06.2024"),
    ],
)
def test_runtime_rejects_as_of_forms_unsupported_by_the_specific_tool(tool_name, requested):
    argument_name = "query" if tool_name == "memory_search" else "name"

    with pytest.raises(ValueError, match="invalid|Invalid date"):
        _historical_tool_graph_context(
            tool_name,
            _valid_time_tool_payload(tool_name, "2024-06-01"),
            {argument_name: "Atlas", "as_of": requested},
        )


@pytest.mark.parametrize(
    ("arguments", "payload"),
    [
        (
            {"query": "Atlas", "known_at": REQUESTED_KNOWN_AT},
            {
                **_complete_known_snapshot(as_of="2024-01-01", query="Atlas", paths=[]),
            },
        ),
        (
            {"query": "Atlas", "as_of": "2024-01-01"},
            {
                "query": "Atlas",
                "as_of": "2024-01-01",
                "known_at": NORMALIZED_KNOWN_AT,
                **_known_at_status(),
                "temporal_basis": "bitemporal",
                "paths": [],
            },
        ),
    ],
)
def test_runtime_requires_unrequested_axis_to_remain_empty(arguments, payload):
    data = {**payload, "graph_context": dict(payload), "results": []}
    with pytest.raises(ValueError, match="different requested"):
        _historical_tool_graph_context("memory_search", data, arguments)


def test_runtime_rejects_a_spontaneous_historical_boundary_on_current_call():
    payload = _complete_known_snapshot(query="Atlas", paths=[])
    with pytest.raises(ValueError, match="unrequested temporal"):
        _historical_tool_graph_context(
            "memory_search",
            {**payload, "graph_context": dict(payload), "results": []},
            {"query": "Atlas"},
        )


class _AsOfToolKernel(_HistoricalToolKernel):
    async def execute(self, name, arguments, *, actor=None):
        del arguments, actor
        snapshot = {
            "query": "Atlas",
            "as_of": "2024-01-01",
            "known_at": "",
            "temporal_basis": "valid_time",
            "expanded": True,
            "paths": [],
            "paths_matched_at_least": 0,
            "paths_truncated": False,
        }
        return ToolResult(
            name,
            True,
            data={
                "count": 0,
                "query": "Atlas",
                "as_of": "2024-01-01",
                "known_at": "",
                "temporal_basis": "valid_time",
                "graph_context": snapshot,
                "results": [],
            },
        )


@pytest.mark.asyncio
async def test_as_of_only_tool_snapshot_becomes_durable_graph_as_of(
    settings,
    storage,
    monkeypatch,
):
    storage.ensure_user("alice")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_HistoricalToolLLM({"query": "Atlas", "as_of": "01.01.2024"}),
        kernel=_AsOfToolKernel(),  # type: ignore[arg-type]
    )
    prepared: list[AgentContext] = []

    async def prepare(user_id, message, conversation_id, **kwargs):
        del message, kwargs
        context = AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            search_query="Atlas",
            conversation_history=[],
            interaction_mode="dialogue",
        )
        prepared.append(context)
        return context

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    result = await runtime.chat(
        "alice",
        "Проверь valid-time снимок",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert prepared[0].graph_context["as_of"] == "2024-01-01"
    assert prepared[0].graph_context["temporal_basis"] == "valid_time"
    assert result["context"]["graph_as_of"] == "2024-01-01"
    messages = storage.get_conversation_messages(result["conversation_id"], user_id="alice", limit=20)
    assistant = next(item for item in reversed(messages) if item["role"] == "assistant")
    metadata = json.loads(assistant["metadata_json"])
    assert metadata["graph_snapshot"] == {
        "as_of": "2024-01-01",
        "paths": 0,
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }


class _RecordingHistoricalLLM(_HistoricalToolLLM):
    def __init__(self, arguments: dict[str, Any] | None = None) -> None:
        super().__init__(arguments)
        self.message_snapshots: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        self.message_snapshots.append([dict(message) for message in messages])
        return await super().chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )


class _UnsafeHistoricalKernel(_HistoricalToolKernel):
    async def execute(self, name, arguments, *, actor=None):
        del arguments, actor
        snapshot = _complete_known_snapshot(
            query="Atlas",
            expanded=True,
            nodes=[{"id": "a", "name": "Atlas"}, {"id": "b", "name": "Beta"}],
            relations=[
                {
                    "id": "rel-secret",
                    "source_name": "Atlas",
                    "target_name": "Beta",
                    "relation_type": "related_to",
                    "metadata_json": {"secret": "FORBIDDEN_METADATA_SECRET"},
                    "provenance": {"evidence_text": "FORBIDDEN_EVIDENCE_SECRET" * 1000},
                }
            ],
            paths=[],
        )
        return ToolResult(
            name,
            True,
            data={
                **_complete_known_snapshot(query="Atlas"),
                "raw_metadata_json": {"secret": "FORBIDDEN_TOP_LEVEL_SECRET"},
                "graph_context": snapshot,
                "results": [
                    {
                        "id": f"ko-{index}",
                        "title": f"Документ {index}",
                        "kind": "note",
                        "excerpt": "полезная выдержка " * 200,
                        "metadata_json": {"secret": "FORBIDDEN_RESULT_SECRET"},
                    }
                    for index in range(20)
                ],
            },
        )


@pytest.mark.asyncio
async def test_historical_tool_replaces_current_prompt_and_serializes_only_safe_json(settings, storage):
    llm = _RecordingHistoricalLLM()
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,
        kernel=_UnsafeHistoricalKernel(),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-safe-historical-tool",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
        graph_context={
            "query": "Atlas current",
            "as_of": "",
            "temporal_basis": "valid_time",
            "paths": [{"path_id": "CURRENT_PATH_MUST_DISAPPEAR", "edges": []}],
        },
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь снимок",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search"),
        attachments=None,
    )

    assert len(llm.message_snapshots) >= 2
    second_call = llm.message_snapshots[1]
    envelope = next(
        str(message["content"])
        for message in second_call
        if message.get("role") == "user"
        and str(message.get("content") or "").startswith("FRIDAY_CONTEXT_DATA")
    )
    assert "CURRENT_PATH_MUST_DISAPPEAR" not in envelope
    assert NORMALIZED_KNOWN_AT in envelope
    tool_message = next(str(message["content"]) for message in second_call if message.get("role") == "tool")
    assert len(tool_message) < 12_000
    safe_data = json.loads(tool_message.split("\n", 1)[1])
    assert safe_data["graph_context"]["known_at"] == NORMALIZED_KNOWN_AT
    assert safe_data["results_truncated"] is True
    encoded = json.dumps(safe_data, ensure_ascii=False)
    for forbidden in (
        "FORBIDDEN_METADATA_SECRET",
        "FORBIDDEN_EVIDENCE_SECRET",
        "FORBIDDEN_TOP_LEVEL_SECRET",
        "FORBIDDEN_RESULT_SECRET",
        "metadata_json",
        "evidence_text",
    ):
        assert forbidden not in encoded
    assert len(result["tool_evidence"]) == 1
    assert "FORBIDDEN" not in result["tool_evidence"][0]["output"]


class _SequenceToolLLM:
    enabled = True
    total_budget_sec = 120.0
    model = "sequence-tool-test"

    def __init__(self, rounds: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.rounds = rounds
        self.round_index = 0
        self.message_snapshots: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        del temperature, max_tokens
        self.message_snapshots.append([dict(message) for message in messages])
        if tools and self.round_index < len(self.rounds):
            calls = self.rounds[self.round_index]
            self.round_index += 1
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{self.round_index}-{index}",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                    for index, (name, arguments) in enumerate(calls)
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "Границы проверены.", "tool_calls": None, "_queue_wait_sec": 0.0}


class _EchoBoundaryKernel(_HistoricalToolKernel):
    async def execute(self, name, arguments, *, actor=None):
        del actor
        marker = str(arguments.get("query") or arguments.get("name") or "Atlas")
        as_of = str(arguments.get("as_of") or "")
        if as_of == "2024":
            as_of = "2024-01-01"
        known_at = str(arguments.get("known_at") or "")
        if known_at:
            status = {
                "known_at": known_at,
                "known_at_floor": KNOWN_AT_FLOOR,
                "history_complete": True,
                "identity_basis": "current_names",
                "temporal_basis": "bitemporal",
            }
        else:
            status = {"known_at": "", "temporal_basis": "valid_time"}
        if name == "entity_lookup":
            return ToolResult(
                name,
                True,
                data={
                    "found": True,
                    "entity": {"id": "ent-atlas", "name": marker},
                    "as_of": as_of,
                    **status,
                    "relations": [],
                },
            )
        snapshot = {
            "query": marker,
            "as_of": as_of,
            **status,
            "expanded": True,
            "paths": [],
        }
        return ToolResult(
            name,
            True,
            data={
                "count": 0,
                "query": marker,
                "as_of": as_of,
                **status,
                "graph_context": snapshot,
                "results": [],
            },
        )


class _GraphlessThenBoundaryKernel(_EchoBoundaryKernel):
    def __init__(self, graphless_marker: str) -> None:
        super().__init__()
        self.graphless_marker = graphless_marker

    async def execute(self, name, arguments, *, actor=None):
        marker = str(arguments.get("query") or arguments.get("name") or "")
        if marker == self.graphless_marker:
            data = (
                {"count": 0, "query": marker, "results": []}
                if name == "memory_search"
                else {"found": False, "entity": None}
            )
            return ToolResult(name, True, data=data)
        return await super().execute(name, arguments, actor=actor)


def _boundary_tool_arguments(
    tool_name: str,
    marker: str,
    *,
    known_at: str = "",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "query" if tool_name == "memory_search" else "name": marker,
    }
    if known_at:
        arguments["known_at"] = known_at
    return arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("same_boundary", [True, False])
async def test_turn_boundary_is_shared_across_memory_and_entity_tools(
    settings,
    storage,
    same_boundary,
):
    second = NORMALIZED_KNOWN_AT if same_boundary else "2026-08-04T09:31:00.000000Z"
    llm = _SequenceToolLLM(
        [
            [
                ("memory_search", {"query": "Atlas", "known_at": NORMALIZED_KNOWN_AT}),
                ("entity_lookup", {"name": "Atlas", "known_at": second}),
            ]
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=_EchoBoundaryKernel(),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-boundary-pair",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь границу",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search", "entity_lookup"),
        attachments=None,
    )

    assert context.graph_context["known_at"] == NORMALIZED_KNOWN_AT
    assert len(result["tool_evidence"]) == (2 if same_boundary else 1)


@pytest.mark.asyncio
async def test_turn_boundary_rejects_a_different_historical_snapshot_in_a_later_round(
    settings,
    storage,
):
    llm = _SequenceToolLLM(
        [
            [("memory_search", {"query": "Atlas", "known_at": NORMALIZED_KNOWN_AT})],
            [
                (
                    "memory_search",
                    {"query": "Atlas", "known_at": "2026-08-04T09:31:00.000000Z"},
                )
            ],
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=_EchoBoundaryKernel(),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-boundary-rounds",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь две границы",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search"),
        attachments=None,
    )

    assert context.graph_context["known_at"] == NORMALIZED_KNOWN_AT
    assert len(result["tool_evidence"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("historical_tool", "current_tool"),
    [
        ("memory_search", "memory_search"),
        ("memory_search", "entity_lookup"),
        ("entity_lookup", "memory_search"),
        ("entity_lookup", "entity_lookup"),
    ],
)
async def test_current_graphful_tool_is_refused_after_historical_boundary(
    settings,
    storage,
    historical_tool,
    current_tool,
):
    llm = _SequenceToolLLM(
        [
            [
                (
                    historical_tool,
                    _boundary_tool_arguments(
                        historical_tool,
                        "HISTORICAL_FIRST_MARKER",
                        known_at=NORMALIZED_KNOWN_AT,
                    ),
                )
            ],
            [(current_tool, _boundary_tool_arguments(current_tool, "CURRENT_SECOND_MARKER"))],
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=_EchoBoundaryKernel(),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-historical-current",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Не смешивай снимки",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search", "entity_lookup"),
        attachments=None,
    )

    assert context.graph_context["known_at"] == NORMALIZED_KNOWN_AT
    assert len(result["tool_evidence"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_tool", "historical_tool"),
    [
        ("memory_search", "memory_search"),
        ("memory_search", "entity_lookup"),
        ("entity_lookup", "memory_search"),
        ("entity_lookup", "entity_lookup"),
    ],
)
async def test_historical_graph_is_refused_after_the_first_current_graph_result(
    settings,
    storage,
    current_tool,
    historical_tool,
):
    llm = _SequenceToolLLM(
        [
            [(current_tool, _boundary_tool_arguments(current_tool, "CURRENT_FIRST_MARKER"))],
            [
                (
                    historical_tool,
                    _boundary_tool_arguments(
                        historical_tool,
                        "HISTORICAL_SECOND_MARKER",
                        known_at=NORMALIZED_KNOWN_AT,
                    ),
                )
            ],
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=_EchoBoundaryKernel(),  # type: ignore[arg-type]
    )
    original = {
        "query": "INITIAL_CURRENT_CONTEXT",
        "as_of": "",
        "temporal_basis": "valid_time",
        "paths": [],
    }
    context = AgentContext(
        conversation_id="conv-current-historical",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
        graph_context=original,
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Не смешивай текущий и исторический графы",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search", "entity_lookup"),
        attachments=None,
    )

    assert context.graph_context is original
    assert len(result["tool_evidence"]) == 1
    final_tool_messages = [
        str(message.get("content") or "")
        for message in llm.message_snapshots[-1]
        if message.get("role") == "tool"
    ]
    assert len(final_tool_messages) == 2
    assert "CURRENT_FIRST_MARKER" in final_tool_messages[0]
    assert "HISTORICAL_SECOND_MARKER" not in final_tool_messages[1]
    assert NORMALIZED_KNOWN_AT not in final_tool_messages[1]
    assert "Historical graph snapshot refused: ValueError" in final_tool_messages[1]
    assert "boundary differs" not in final_tool_messages[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_tool", "second_tool"),
    [
        ("memory_search", "memory_search"),
        ("memory_search", "entity_lookup"),
        ("entity_lookup", "memory_search"),
        ("entity_lookup", "entity_lookup"),
    ],
)
async def test_two_current_graph_results_share_the_current_boundary(
    settings,
    storage,
    first_tool,
    second_tool,
):
    llm = _SequenceToolLLM(
        [
            [(first_tool, _boundary_tool_arguments(first_tool, "CURRENT_FIRST_MARKER"))],
            [(second_tool, _boundary_tool_arguments(second_tool, "CURRENT_SECOND_MARKER"))],
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=_EchoBoundaryKernel(),  # type: ignore[arg-type]
    )
    original: dict[str, Any] = {}
    context = AgentContext(
        conversation_id="conv-current-current",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
        graph_context=original,
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Текущий граф не меняется",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search", "entity_lookup"),
        attachments=None,
    )

    assert context.graph_context is original
    assert len(result["tool_evidence"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graphless_tool", "historical_tool"),
    [
        ("memory_search", "entity_lookup"),
        ("entity_lookup", "memory_search"),
    ],
)
async def test_graphless_current_result_does_not_bind_before_a_historical_graph(
    settings,
    storage,
    graphless_tool,
    historical_tool,
):
    marker = "GRAPHLESS_FIRST_MARKER"
    llm = _SequenceToolLLM(
        [
            [(graphless_tool, _boundary_tool_arguments(graphless_tool, marker))],
            [
                (
                    historical_tool,
                    _boundary_tool_arguments(
                        historical_tool,
                        "HISTORICAL_SECOND_MARKER",
                        known_at=NORMALIZED_KNOWN_AT,
                    ),
                )
            ],
        ]
    )
    runtime = AgentRuntime(
        settings,
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=_GraphlessThenBoundaryKernel(marker),  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-graphless-historical",
        user_id="alice",
        search_query="Atlas",
        interaction_mode="dialogue",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Сначала ответ без графа, потом исторический снимок",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        tools=_graph_tool_schemas("memory_search", "entity_lookup"),
        attachments=None,
    )

    assert context.graph_context["known_at"] == NORMALIZED_KNOWN_AT
    assert len(result["tool_evidence"]) == 2
