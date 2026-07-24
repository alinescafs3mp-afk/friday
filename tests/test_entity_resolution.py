from __future__ import annotations

from jericho.knowledge_graph import KnowledgeGraph
from jericho.storage.models import EntityResolutionCandidate, EntityType, KnowledgeObject, RawObject, new_id


def _knowledge(storage, user_id: str):
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), "Alpha context", "text")
    storage.store_raw_object(raw)
    ko = KnowledgeObject(new_id("ko"), user_id, raw.id, content="Alpha context", title="Alpha")
    storage.store_knowledge_object(ko)
    return ko


def test_rejected_candidate_stays_rejected(storage):
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "Project Alpha", EntityType.PROJECT, aliases=["Alpha"])
    graph.create_entity("alice", "Alpha Project", EntityType.PROJECT)
    candidates = graph.resolver.detect_duplicates("alice", min_confidence=0.25)
    assert candidates
    candidate = candidates[0]
    assert graph.resolver.reject_resolution(candidate.id, "alice", resolved_by="owner") is True
    assert graph.resolver.detect_duplicates("alice", min_confidence=0.25) == []
    stored = storage.get_resolution_candidate(candidate.id, "alice")
    assert stored and stored["status"] == "rejected"


def test_merge_preserves_links_relations_aliases_and_history(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Alpha Project", EntityType.PROJECT, aliases=["A Project"])
    target = graph.create_entity("alice", "Project Alpha", EntityType.PROJECT, aliases=["Alpha"])
    person = graph.create_entity("alice", "Ivan Petrov", EntityType.PERSON)
    ko = _knowledge(storage, "alice")
    graph.link_knowledge_to_entity(ko.id, source["id"], "alice")
    graph.create_relation("alice", source["id"], person["id"])
    candidate = EntityResolutionCandidate(
        id=new_id("er"),
        user_id="alice",
        entity_a_id=source["id"],
        entity_b_id=target["id"],
        confidence=0.9,
        resolution_method="test",
    )
    storage.store_resolution_candidate(candidate)

    merged = graph.resolver.accept_resolution(
        candidate.id,
        "alice",
        target_entity_id=target["id"],
        resolved_by="owner",
    )
    assert merged["target_entity_id"] == target["id"]
    old = storage.get_entity(source["id"], "alice")
    current = storage.get_entity(target["id"], "alice")
    assert old and old["merged_into_id"] == target["id"] and old["canonical"] == 0
    assert current and "Alpha Project" in current["aliases_json"]
    links = storage.list_knowledge_entity_links("alice", entity_id=target["id"])
    assert any(link["knowledge_object_id"] == ko.id for link in links)
    relations = storage.get_entity_relations(target["id"], "alice")
    assert any(rel["target_entity_id"] == person["id"] for rel in relations)
    history = storage.list_merge_history("alice")
    assert history and history[0]["source_entity_id"] == source["id"]


def test_compact_identifiers_are_exact_match_only(storage):
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "BRK.A", EntityType.OTHER)
    graph.create_entity("alice", "BRK.B", EntityType.OTHER)
    graph.create_entity("alice", "BRNQ26", EntityType.OTHER)
    graph.create_entity("alice", "BRNQ27", EntityType.OTHER)

    assert graph.resolver.detect_duplicates("alice", min_confidence=0.0) == []
    assert storage.find_entity_by_name("alice", "BRK.A")["name"] == "BRK.A"
    assert storage.find_entity_by_name("alice", "BRK.B")["name"] == "BRK.B"


def test_accepted_relation_candidate_is_not_reopened_by_rediscovery(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Orion", EntityType.PROJECT)
    target = graph.create_entity("alice", "PostgreSQL", EntityType.CONCEPT)
    candidate = storage.store_relation_candidate(
        "alice",
        source["id"],
        target["id"],
        "uses",
        confidence=0.7,
        evidence={"source": "first-pass"},
    )
    accepted = graph.review_relation_candidate(
        "alice",
        candidate["id"],
        "accepted",
        reviewed_by="alice",
    )
    assert accepted and accepted["status"] == "accepted"

    rediscovered = storage.store_relation_candidate(
        "alice",
        source["id"],
        target["id"],
        "uses",
        confidence=0.79,
        evidence={"source": "later-pass"},
    )

    assert rediscovered["id"] == candidate["id"]
    assert rediscovered["status"] == "accepted"
    assert len(storage.get_entity_relations(source["id"], "alice")) == 1


def test_pending_resolutions_are_enriched_for_review(storage):
    graph = KnowledgeGraph(storage)
    left = graph.create_entity("alice", "Ivan Petrov", EntityType.PERSON)
    right = graph.create_entity("alice", "Ivan Petroff", EntityType.PERSON)
    assert left["id"] != right["id"]
    ko = _knowledge(storage, "alice")
    graph.link_knowledge_to_entity(ko.id, left["id"], "alice")
    candidate = EntityResolutionCandidate(
        id=new_id("er"),
        user_id="alice",
        entity_a_id=left["id"],
        entity_b_id=right["id"],
        confidence=0.96,
        resolution_method="test",
    )
    storage.store_resolution_candidate(candidate)

    pending = graph.resolver.get_pending_resolutions("alice")
    assert len(pending) == 1
    item = pending[0]
    assert item["id"] == candidate.id
    assert item["entity_a"]["name"] == "Ivan Petrov"
    assert item["entity_b"]["name"] == "Ivan Petroff"
    assert item["entity_a"]["knowledge_count"] == 1
    assert item["recommendation"] == "strong_merge_candidate"
