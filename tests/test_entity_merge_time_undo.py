"""Unmerge reverses the temporal move without erasing later target decisions."""

from __future__ import annotations

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType


def _events(storage):
    graph = KnowledgeGraph(storage)
    storage.ensure_user("alice")
    source = graph.create_entity("alice", "Source event", EntityType.EVENT)
    target = graph.create_entity("alice", "Target event", EntityType.EVENT)
    return source, target


def test_unmerge_returns_a_merge_created_time_to_the_source(storage) -> None:
    source, target = _events(storage)
    original = storage.set_entity_time(
        source["id"],
        "alice",
        "2026-08-12T10:30:00Z",
        occurred_end="2026-08-12T11:00:00Z",
        precision="minute",
    )

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    assert storage.get_entity_time(source["id"], "alice") is None
    assert storage.get_entity_time(target["id"], "alice")["occurred_at"] == original["occurred_at"]

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")

    restored = storage.get_entity_time(source["id"], "alice")
    assert restored == original
    assert storage.get_entity_time(target["id"], "alice") is None


def test_unmerge_preserves_the_targets_own_time_and_restores_the_source(storage) -> None:
    source, target = _events(storage)
    source_time = storage.set_entity_time(source["id"], "alice", "2026-09-30")
    target_time = storage.set_entity_time(target["id"], "alice", "2026-08-12")

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    assert storage.get_entity_time(target["id"], "alice") == target_time

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")

    assert storage.get_entity_time(source["id"], "alice") == source_time
    assert storage.get_entity_time(target["id"], "alice") == target_time


def test_unmerge_keeps_a_target_time_edited_after_the_merge(storage) -> None:
    source, target = _events(storage)
    source_time = storage.set_entity_time(source["id"], "alice", "2026-08-12")
    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    later_target_time = storage.set_entity_time(target["id"], "alice", "2027-01-15")

    storage.unmerge_entities("alice", merged["_merge_id"], undone_by="owner")

    assert storage.get_entity_time(source["id"], "alice") == source_time
    assert storage.get_entity_time(target["id"], "alice") == later_target_time
