"""Corrupt graph edges must never cross the knowledge tenant boundary.

The product writer validates both endpoints before creating a link.  Old imports,
manual recovery, or a damaged database can still contain a row whose ``user_id``
and entity belong to one owner while ``knowledge_object_id`` belongs to another.
Every read surface therefore has to enforce the same composite ownership relation;
the foreign-key on the bare object id is not enough.
"""

from __future__ import annotations

import hashlib

from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id, utc_now


def _document(
    storage,
    user_id: str,
    title: str,
    *,
    tag: str,
    document_date: str,
) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="synthetic-test",
        source_ref=new_id("src"),
        raw_content=f"Synthetic body for {title}",
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}:{title}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=f"Synthetic body for {title}",
        content_type="text",
        title=title,
        tags_json=[tag],
        metadata_json={"document_date": document_date},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _corrupt_graph(storage) -> dict[str, str]:
    owner = "synthetic-link-owner"
    foreign_owner = "synthetic-link-foreign-owner"
    storage.ensure_user(owner)
    storage.ensure_user(foreign_owner)

    entity = Entity(
        id=new_id("ent"),
        user_id=owner,
        name="Synthetic tenant boundary project",
        entity_type=EntityType.PROJECT,
    )
    storage.create_entity(entity)
    own_knowledge = _document(
        storage,
        owner,
        "OWNER_VISIBLE_DOCUMENT",
        tag="owner-visible-tag",
        document_date="2026-01-02",
    )
    foreign_knowledge = _document(
        storage,
        foreign_owner,
        "FOREIGN_KNOWLEDGE_SENTINEL",
        tag="foreign-knowledge-sentinel-tag",
        document_date="2099-12-31",
    )
    storage.link_knowledge_entity(owner, own_knowledge, entity.id, status="accepted")

    # Public writers reject this shape. Direct SQL represents an old/imported or
    # externally corrupted edge and proves that every read remains defensive.
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO knowledge_entity_links(
                   id, user_id, knowledge_object_id, entity_id, status,
                   confidence, evidence_json, created_at, reviewed_at, reviewed_by
               ) VALUES(?, ?, ?, ?, 'accepted', 1.0, '{}', ?, NULL, NULL)""",
            (new_id("kel"), owner, foreign_knowledge, entity.id, utc_now()),
        )

    return {
        "owner": owner,
        "entity_id": entity.id,
        "entity_name": entity.name,
        "own_knowledge": own_knowledge,
        "foreign_knowledge": foreign_knowledge,
    }


def test_cross_owner_link_is_absent_from_entity_browse_counts(storage) -> None:
    graph = _corrupt_graph(storage)
    owner = graph["owner"]
    entity_id = graph["entity_id"]
    entity_name = graph["entity_name"]

    containers = storage.list_container_entities(owner, (EntityType.PROJECT.value,))
    activity = storage.list_entities_by_activity(
        owner,
        types=(EntityType.PROJECT.value,),
        limit=10,
    )

    assert containers == [
        {
            "id": entity_id,
            "name": entity_name,
            "entity_type": EntityType.PROJECT.value,
            "knowledge_count": 1,
        }
    ]
    assert activity == [
        {
            "id": entity_id,
            "name": entity_name,
            "entity_type": EntityType.PROJECT.value,
            "knowledge_count": 1,
        }
    ]


def test_cross_owner_link_is_absent_from_link_map_and_lineage_impact(storage) -> None:
    graph = _corrupt_graph(storage)
    owner = graph["owner"]
    own_knowledge = graph["own_knowledge"]
    foreign_knowledge = graph["foreign_knowledge"]

    by_document = storage.list_knowledge_entity_links_for([own_knowledge, foreign_knowledge])

    assert by_document == {own_knowledge: [graph["entity_name"]]}
    assert storage.knowledge_impact(owner, own_knowledge) == {
        "entities_confirmed": 1,
        "entities_without_another_source": 1,
    }


def test_cross_owner_link_is_absent_from_entity_document_projections(storage) -> None:
    graph = _corrupt_graph(storage)
    owner = graph["owner"]
    entity_id = graph["entity_id"]
    own_knowledge = graph["own_knowledge"]

    refs = storage.list_entity_knowledge_refs(owner, entity_id, limit=10)
    batched_refs = storage.list_entities_knowledge_refs(owner, [entity_id], limit=10)
    full_rows = storage.get_entity_knowledge(owner, entity_id, limit=10)
    cards = storage.get_entity_knowledge_cards(owner, entity_id, limit=10)

    assert storage.count_entity_knowledge(owner, entity_id) == 1
    assert [row["id"] for row in refs] == [own_knowledge]
    assert [row["id"] for row in batched_refs[entity_id]] == [own_knowledge]
    assert [row["id"] for row in full_rows] == [own_knowledge]
    assert [row["id"] for row in cards] == [own_knowledge]


def test_cross_owner_link_is_absent_from_entity_aggregate(storage) -> None:
    graph = _corrupt_graph(storage)
    summary = storage.entity_knowledge_summary(graph["owner"], graph["entity_id"])

    assert summary == {
        "tags": ["owner-visible-tag"],
        "tags_matched_at_least": 1,
        "tags_truncated": False,
        "document_date_range": {"earliest": "2026-01-02", "latest": "2026-01-02"},
        "documents_without_own_date": 0,
        "total": 1,
    }
