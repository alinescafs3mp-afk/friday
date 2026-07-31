"""The graph traversal must not turn graph width into SQL query count."""

from __future__ import annotations

import hashlib
from typing import Any

from jericho.knowledge_graph import KnowledgeGraph
from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _entity(storage, user_id: str, name: str) -> str:
    entity = Entity(
        id=new_id("ent"),
        user_id=user_id,
        name=name,
        entity_type=EntityType.CONCEPT,
    )
    storage.create_entity(entity)
    return entity.id


def _knowledge(storage, user_id: str, title: str, *, entity_id: str | None = None) -> str:
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
        entity_id=entity_id,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _link(storage, user_id: str, knowledge_id: str, entity_id: str, confidence: float) -> None:
    storage.link_knowledge_entity(
        user_id,
        knowledge_id,
        entity_id,
        status="accepted",
        confidence=confidence,
    )


def test_batch_projection_is_identical_to_one_entity_calls(storage):
    storage.ensure_user("owner")
    linked = _entity(storage, "owner", "Linked")
    legacy = _entity(storage, "owner", "Legacy")
    empty = _entity(storage, "owner", "Empty")
    storage.ensure_user("other")
    foreign = _entity(storage, "other", "Foreign")
    foreign_knowledge = _knowledge(storage, "other", "must stay tenant-isolated")
    _link(storage, "other", foreign_knowledge, foreign, 0.99)

    linked_ids: list[str] = []
    for index, confidence in enumerate((0.91, 0.82, 0.73)):
        knowledge_id = _knowledge(storage, "owner", f"linked-{index}")
        storage.execute(
            "UPDATE knowledge_objects SET importance=?, updated_at=? WHERE id=?",
            (0.9 - index / 10, f"2026-01-0{index + 1}T00:00:00Z", knowledge_id),
        )
        _link(storage, "owner", knowledge_id, linked, confidence)
        linked_ids.append(knowledge_id)
    # A direct legacy association is ignored when accepted link rows exist.
    _knowledge(storage, "owner", "linked-fallback-must-not-leak", entity_id=linked)
    legacy_ids: list[str] = []
    for index in range(3):
        knowledge_id = _knowledge(storage, "owner", f"legacy-{index}", entity_id=legacy)
        storage.execute(
            "UPDATE knowledge_objects SET importance=?, updated_at=? WHERE id=?",
            (0.7 - index / 10, f"2026-02-0{index + 1}T00:00:00Z", knowledge_id),
        )
        legacy_ids.append(knowledge_id)
    storage.commit()

    entity_ids = [linked, legacy, empty, foreign]
    expected = {
        entity_id: storage.list_entity_knowledge_refs("owner", entity_id, limit=2) for entity_id in entity_ids
    }
    actual = storage.list_entities_knowledge_refs(
        "owner",
        [linked, legacy, empty, foreign, linked],
        limit=2,
    )

    assert actual == expected
    assert [item["id"] for item in actual[linked]] == linked_ids[:2]
    assert [item["id"] for item in actual[legacy]] == legacy_ids[:2]
    assert actual[empty] == []
    assert actual[foreign] == []


def test_one_wide_frontier_uses_one_projection_query_and_keeps_output(storage, monkeypatch):
    storage.ensure_user("owner")
    root = _entity(storage, "owner", "Atlas")
    neighbours = [_entity(storage, "owner", f"Node {index:02d}") for index in range(25)]
    shared = _knowledge(storage, "owner", "Shared roster")
    _link(storage, "owner", shared, root, 0.99)
    for index, entity_id in enumerate(neighbours):
        _link(storage, "owner", shared, entity_id, 0.80 + index / 1000)
        own = _knowledge(storage, "owner", f"Node evidence {index:02d}")
        _link(storage, "owner", own, entity_id, 0.90)

    graph = KnowledgeGraph(storage)
    single = storage.list_entity_knowledge_refs
    real_batch = storage.list_entities_knowledge_refs
    legacy_calls: list[str] = []

    def legacy_batch(
        user_id: str, entity_ids: list[str], *, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        result = {}
        for entity_id in entity_ids:
            legacy_calls.append(entity_id)
            result[entity_id] = single(user_id, entity_id, limit=limit)
        return result

    monkeypatch.setattr(storage, "list_entities_knowledge_refs", legacy_batch)
    expected = graph.context_for_query("owner", "Atlas", depth=1, knowledge_limit=50)
    assert len(set(legacy_calls)) == 26

    batch_sizes: list[int] = []

    def watched_batch(
        user_id: str, entity_ids: list[str], *, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]:
        batch_sizes.append(len(entity_ids))
        return real_batch(user_id, entity_ids, limit=limit)

    def forbidden_single(*_args, **_kwargs):
        raise AssertionError("the traversal fell back to one projection query per entity")

    monkeypatch.setattr(storage, "list_entities_knowledge_refs", watched_batch)
    monkeypatch.setattr(storage, "list_entity_knowledge_refs", forbidden_single)
    actual = graph.context_for_query("owner", "Atlas", depth=1, knowledge_limit=50)

    assert actual == expected
    assert batch_sizes == [1, 25]
