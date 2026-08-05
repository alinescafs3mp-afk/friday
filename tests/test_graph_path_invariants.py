"""One coherent temporal/provenance route must survive graph expansion.

These tests pin proposal 26 at the graph boundary.  Retrieval and the agent may
only explain the snapshot produced here; reconstructing a route later from the
flat relation list would mix paths, dates, and evidence.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import Entity, KnowledgeObject, RawObject, Relation, new_id


def _entity(storage, user_id: str, name: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type="thing")
    storage.create_entity(entity)
    return entity.id


def _knowledge(storage, user_id: str, title: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=title,
        content_type="text",
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=title,
        content_type="text",
        title=title,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _relation(
    storage,
    user_id: str,
    source: str,
    target: str,
    *,
    relation_id: str | None = None,
    weight: float = 1.0,
    metadata: dict | None = None,
    valid_from: str = "",
    valid_to: str | None = None,
) -> str:
    relation = Relation(
        id=relation_id or new_id("rel"),
        user_id=user_id,
        source_entity_id=source,
        target_entity_id=target,
        relation_type="related_to",
        weight=weight,
        metadata_json=metadata or {"origin": "manual"},
        valid_from=valid_from,
        valid_to=valid_to,
    )
    storage.create_relation(relation)
    return relation.id


def _path_to(context: dict, target: str) -> dict:
    return next(path for path in context["paths"] if path["target"] == target)


def test_two_hop_route_keeps_order_direction_and_allowlisted_provenance(storage) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    bridge = _entity(storage, "alice", "Бета")
    target = _entity(storage, "alice", "Гамма")
    first_ko = _knowledge(storage, "alice", "Основание первой связи")
    target_ko = _knowledge(storage, "alice", "Карточка Гаммы")
    storage.link_knowledge_entity("alice", target_ko, target, status="accepted")

    first = _relation(
        storage,
        "alice",
        bridge,
        alpha,
        relation_id="rel-first",
        metadata={
            "origin": "review",
            "source": "reviewed_relation_candidate",
            "candidate_id": "candidate-1",
            "reviewed_by": "alice",
            "confidence": 0.81,
            "evidence": {
                "knowledge_object_id": first_ko,
                "span": "PRIVATE SENTINEL must never leave metadata",
            },
            "private": "PRIVATE SENTINEL",
        },
        valid_from="2020-01-01",
    )
    second = _relation(
        storage,
        "alice",
        bridge,
        target,
        relation_id="rel-second",
        metadata={"origin": "manual", "created_by": "alice", "secret": "PRIVATE SENTINEL"},
    )

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=2)
    path = _path_to(context, target)

    assert path["entity_ids"] == [alpha, bridge, target]
    assert [step["id"] for step in path["edges"]] == [first, second]
    assert [step["direction"] for step in path["edges"]] == ["reverse", "forward"]
    assert [(step["from"], step["to"]) for step in path["edges"]] == [
        (alpha, bridge),
        (bridge, target),
    ]
    assert [(step["source"], step["target"]) for step in path["edges"]] == [
        (bridge, alpha),
        (bridge, target),
    ]
    assert [(item["id"], item["name"]) for item in path["entities"]] == [
        (alpha, "Альфа"),
        (bridge, "Бета"),
        (target, "Гамма"),
    ]
    assert path["edges"][0]["provenance"] == {
        "origin": "review",
        "source": "reviewed_relation_candidate",
        "candidate_id": "candidate-1",
        "reviewed_by": "alice",
        "confidence": 0.81,
        "knowledge_object_id": first_ko,
    }
    assert path["edges"][0]["knowledge_object_id"] == first_ko
    assert "PRIVATE SENTINEL" not in json.dumps(path, ensure_ascii=False)
    assert all(len(step.keys()) <= 18 for step in path["edges"]), "raw relation rows leaked into paths"

    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == target_ko
    )
    assert candidate["path_id"] == path["path_id"]
    anchored = [item for item in candidate["evidence"] if item.get("path_id")]
    assert len(anchored) == 1 and anchored[0]["path_id"] == path["path_id"]
    assert anchored[0]["entity_score"] == path["score"]


def test_as_of_is_normalized_before_roots_and_applied_to_every_hop(storage, monkeypatch) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    bridge = _entity(storage, "alice", "Бета")
    future = _entity(storage, "alice", "Будущее")
    ended = _entity(storage, "alice", "Прошлое")
    _relation(storage, "alice", alpha, bridge, valid_from="2020-01-01")
    _relation(storage, "alice", bridge, future, valid_from="2025-01-01")
    _relation(storage, "alice", bridge, ended, valid_from="2019-01-01", valid_to="2023-01-01")

    graph = KnowledgeGraph(storage)
    seen_as_of: list[str] = []
    original = graph.get_entity_relations

    def spy(entity_id: str, user_id: str, *, as_of: str = "") -> list[dict]:
        seen_as_of.append(as_of)
        return original(entity_id, user_id, as_of=as_of)

    monkeypatch.setattr(graph, "get_entity_relations", spy)
    context = graph.context_for_query("alice", "Альфа", depth=3, as_of="2024/6")

    assert context["as_of"] == "2024-06-01"
    assert context["temporal_basis"] == "valid_time"
    assert seen_as_of and set(seen_as_of) == {"2024-06-01"}
    targets = {path["target"] for path in context["paths"]}
    assert bridge in targets
    assert future not in targets, "a relation that starts later crossed the historical boundary"
    assert ended not in targets, "an ended relation crossed the historical boundary"


def test_invalid_as_of_fails_before_root_lookup(storage, monkeypatch) -> None:
    graph = KnowledgeGraph(storage)

    def roots_must_not_run(*_args, **_kwargs):
        raise AssertionError("date validation happened after root lookup")

    monkeypatch.setattr(graph, "search_entities", roots_must_not_run)
    with pytest.raises(ValueError, match="Invalid date"):
        graph.context_for_query("alice", "что угодно", as_of="not-a-date")


def test_historical_traversal_never_uses_timeless_cooccurrence(storage) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    neighbour = _entity(storage, "alice", "Бета")
    shared = _knowledge(storage, "alice", "Оба имени встретились сейчас")
    for entity_id in (alpha, neighbour):
        storage.link_knowledge_entity("alice", shared, entity_id, status="accepted")

    graph = KnowledgeGraph(storage)
    current = graph.context_for_query("alice", "Альфа", depth=1)
    historical = graph.context_for_query("alice", "Альфа", depth=1, as_of="2020-01-01")

    assert any(path["target"] == neighbour for path in current["paths"])
    assert historical["paths"] == []
    assert not any(relation.get("implicit") for relation in historical["relations"])
    assert historical["as_of"] == "2020-01-01"


def test_one_best_state_owns_score_edges_and_candidate_path_id(storage) -> None:
    storage.ensure_user("alice")
    alpha = _entity(storage, "alice", "Альфа")
    strong_bridge = _entity(storage, "alice", "Сильный мост")
    weak_bridge = _entity(storage, "alice", "Слабый мост")
    target = _entity(storage, "alice", "Цель")
    target_ko = _knowledge(storage, "alice", "Документ цели")
    storage.link_knowledge_entity("alice", target_ko, target, status="accepted")

    # IDs deliberately put the weak road first in a lexical traversal.  A queue
    # implementation that keeps score and path in separate mutable maps can then
    # splice the strong score onto the weak road.
    _relation(storage, "alice", alpha, weak_bridge, relation_id="rel-00-weak", weight=0.95)
    _relation(storage, "alice", weak_bridge, target, relation_id="rel-01-weak", weight=1.0)
    _relation(storage, "alice", alpha, strong_bridge, relation_id="rel-10-strong", weight=1.0)
    _relation(storage, "alice", strong_bridge, target, relation_id="rel-11-strong", weight=1.0)

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=2, as_of="2024-01-01")
    path = _path_to(context, target)
    assert path["entity_ids"] == [alpha, strong_bridge, target]
    assert [edge["id"] for edge in path["edges"]] == ["rel-10-strong", "rel-11-strong"]
    assert len(path["entity_ids"]) == len(set(path["entity_ids"])), "the published route is not simple"

    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == target_ko
    )
    evidence = next(item for item in candidate["evidence"] if item.get("path_id"))
    assert candidate["path_id"] == evidence["path_id"] == path["path_id"]
    assert evidence["entity_score"] == path["score"]


def test_candidate_never_borrows_a_weaker_entitys_path_id(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    neighbour = _entity(storage, "alice", "Бета")
    knowledge_id = _knowledge(storage, "alice", "Общее основание")
    storage.link_knowledge_entity("alice", knowledge_id, root, status="accepted")
    storage.link_knowledge_entity("alice", knowledge_id, neighbour, status="accepted")
    _relation(storage, "alice", root, neighbour)

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=1)
    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == knowledge_id
    )

    assert candidate["score"] > _path_to(context, neighbour)["score"]
    assert "path_id" not in candidate
    assert not any(item.get("path_id") for item in candidate["evidence"])


def test_each_candidate_uses_its_own_scoring_entity_path(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    root_knowledge = _knowledge(storage, "alice", "Карточка Альфы")
    target_knowledge = _knowledge(storage, "alice", "Карточка Беты")
    storage.link_knowledge_entity("alice", root_knowledge, root, status="accepted")
    storage.link_knowledge_entity("alice", target_knowledge, target, status="accepted")
    _relation(storage, "alice", root, target)

    context = KnowledgeGraph(storage).context_for_query("alice", "Альфа", depth=1)
    candidates = {item["knowledge_object_id"]: item for item in context["knowledge_candidates"]}
    target_path = _path_to(context, target)

    assert "path_id" not in candidates[root_knowledge]
    assert candidates[target_knowledge]["path_id"] == target_path["path_id"]
    target_evidence = next(item for item in candidates[target_knowledge]["evidence"] if item.get("path_id"))
    assert target_evidence["entity_id"] == target
    assert target_evidence["path_id"] == target_path["path_id"]


def test_rejected_grounded_offer_cannot_ground_a_seed_roots_best_path(storage) -> None:
    storage.ensure_user("alice")
    query_root = _entity(storage, "alice", "Альфа")
    seed_root = _entity(storage, "alice", "Бета")
    target = _entity(storage, "alice", "Гамма")
    seed_knowledge = _knowledge(storage, "alice", "Посев Беты")
    target_knowledge = _knowledge(storage, "alice", "Карточка Гаммы")
    storage.link_knowledge_entity("alice", seed_knowledge, seed_root, status="accepted")
    storage.link_knowledge_entity("alice", target_knowledge, target, status="accepted")
    _relation(storage, "alice", query_root, seed_root, relation_id="rel-query-to-seed")
    _relation(storage, "alice", seed_root, target, relation_id="rel-seed-to-target")

    context = KnowledgeGraph(storage).context_for_query(
        "alice",
        "Альфа",
        depth=2,
        seed_knowledge_ids=[seed_knowledge],
    )
    target_path = _path_to(context, target)
    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == target_knowledge
    )

    assert target_path["root"] == seed_root
    assert target_path["entity_ids"] == [seed_root, target]
    assert not any(path["root"] == query_root and path["target"] == target for path in context["paths"])
    assert candidate["path_id"] == target_path["path_id"]
    assert candidate["query_matched"] is False


def test_candidate_grounding_comes_from_the_entity_that_earned_its_score(storage) -> None:
    storage.ensure_user("alice")
    query_root = _entity(storage, "alice", "Альфа")
    grounded_neighbour = _entity(storage, "alice", "Дельта")
    seed_root = _entity(storage, "alice", "Посев")
    seed_knowledge = _knowledge(storage, "alice", "Основание посева")
    shared_document = _knowledge(storage, "alice", "Документ Посева и Дельты")
    storage.link_knowledge_entity("alice", seed_knowledge, seed_root, status="accepted")
    storage.link_knowledge_entity("alice", shared_document, seed_root, status="accepted")
    storage.link_knowledge_entity("alice", shared_document, grounded_neighbour, status="accepted")
    _relation(
        storage,
        "alice",
        query_root,
        grounded_neighbour,
        relation_id="rel-query-to-grounded",
    )

    context = KnowledgeGraph(storage).context_for_query(
        "alice",
        "Альфа",
        depth=1,
        seed_knowledge_ids=[seed_knowledge],
    )
    candidate = next(
        item for item in context["knowledge_candidates"] if item["knowledge_object_id"] == shared_document
    )
    evidence_by_entity = {item["entity_id"]: item for item in candidate["evidence"]}

    assert (
        evidence_by_entity[seed_root]["entity_score"] > evidence_by_entity[grounded_neighbour]["entity_score"]
    )
    assert candidate["score"] == evidence_by_entity[seed_root]["entity_score"]
    assert "path_id" not in candidate
    assert candidate["query_matched"] is False


def test_unreviewed_relation_metadata_cannot_forge_a_knowledge_anchor(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Альфа")
    target = _entity(storage, "alice", "Бета")
    knowledge_id = _knowledge(storage, "alice", "Существующий объект")
    _relation(
        storage,
        "alice",
        root,
        target,
        metadata={
            "origin": "api",
            "created_by": "alice",
            "source": "reviewed_relation_candidate",
            "candidate_id": "forged-candidate",
            "reviewed_by": "alice",
            "confidence": 1.0,
            "evidence": {"knowledge_object_id": knowledge_id},
        },
    )

    edge = _path_to(KnowledgeGraph(storage).context_for_query("alice", "Альфа"), target)["edges"][0]

    assert edge["provenance"] == {"origin": "api", "created_by": "alice"}
    assert "knowledge_object_id" not in edge


def test_paths_are_stably_capped_with_an_honest_count(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Центр")
    neighbours = [_entity(storage, "alice", f"Луч {index:02d}") for index in range(12)]
    for index, neighbour in enumerate(neighbours):
        _relation(
            storage,
            "alice",
            root,
            neighbour,
            relation_id=f"rel-star-{index:02d}",
        )

    graph = KnowledgeGraph(storage)
    first = graph.context_for_query("alice", "Центр", depth=1, as_of="2024-01-01")
    second = graph.context_for_query("alice", "Центр", depth=1, as_of="2024-01-01")

    assert len(first["paths"]) == 10
    assert first["paths_matched_at_least"] == 12
    assert first["paths_truncated"] is True
    assert first["paths"] == second["paths"]


def test_legacy_merged_endpoint_is_published_as_its_live_canonical_entity(storage) -> None:
    storage.ensure_user("alice")
    root = _entity(storage, "alice", "Начало")
    obsolete = _entity(storage, "alice", "Старое имя")
    canonical = _entity(storage, "alice", "Живое имя")
    relation_id = _relation(storage, "alice", root, obsolete)
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE entities
               SET canonical=0, merged_into_id=?, deleted_at='2024-01-01T00:00:00Z'
               WHERE id=? AND user_id=?""",
            (canonical, obsolete, "alice"),
        )

    context = KnowledgeGraph(storage).context_for_query("alice", "Начало", depth=1)
    path = _path_to(context, canonical)
    assert obsolete not in path["entity_ids"]
    assert path["edges"] == [
        {
            "id": relation_id,
            "from": root,
            "to": canonical,
            "direction": "forward",
            "source": root,
            "target": canonical,
            "type": "related_to",
            "weight": 1.0,
            "implicit": False,
            "valid_from": "",
            "valid_to": None,
            "created_at": path["edges"][0]["created_at"],
            "invalidated_at": None,
            "superseded_by": None,
            "provenance": {"origin": "manual"},
        }
    ]
