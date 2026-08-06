"""Transitive copies of private entity identity stay outside public boundaries."""

from __future__ import annotations

import unicodedata

import pytest

from friday.api.kg import _bounded_public_entity_versions
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import (
    Entity,
    EntityResolutionCandidate,
    EntityType,
    FeedbackItem,
    FeedbackType,
    Relation,
    RelationType,
    new_id,
)


def _quarantine(storage, entity: Entity, *, person_id: str = "bob") -> None:
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-07T09:00:00Z', 'day', ?,
                      '2026-08-06T00:00:00Z')""",
            (entity.id, entity.user_id, f"reminder:{person_id}"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', '2026-08-06T00:00:00Z')""",
            (entity.id, person_id),
        )


def _private_source_and_carrier(storage) -> tuple[Entity, Entity, Entity]:
    storage.ensure_user("alice")
    private = Entity(
        new_id("ent"),
        "alice",
        "PRIVATE MATERIAL SOURCE 71b9",
        EntityType.EVENT,
    )
    copied_name = unicodedata.normalize("NFD", private.name.casefold())
    carrier = Entity(
        new_id("ent"),
        "alice",
        "Otherwise public carrier",
        EntityType.PROJECT,
        description=f"Historical identity copy: {copied_name}",
    )
    endpoint = Entity(new_id("ent"), "alice", "Public endpoint", EntityType.PROJECT)
    storage.create_entity(private)
    storage.create_entity(carrier)
    storage.create_entity(endpoint)
    return private, carrier, endpoint


def test_feedback_cannot_mutate_targets_hidden_through_a_material_carrier(storage) -> None:
    private, carrier, endpoint = _private_source_and_carrier(storage)
    relation = Relation(
        new_id("rel"),
        "alice",
        carrier.id,
        endpoint.id,
        RelationType.RELATED_TO,
    )
    storage.create_relation(relation)
    candidate = storage.store_relation_candidate(
        "alice",
        carrier.id,
        endpoint.id,
        RelationType.RELATED_TO.value,
        confidence=0.8,
        evidence={},
    )
    resolution = EntityResolutionCandidate(
        new_id("resolution"),
        "alice",
        carrier.id,
        endpoint.id,
        0.8,
        "synthetic",
    )
    storage.store_resolution_candidate(resolution)
    _quarantine(storage, private)

    assert storage.get_entity(carrier.id, "alice") is None
    targets = (
        ("entity", carrier.id),
        ("relation", relation.id),
        ("relation_candidate", str(candidate["id"])),
        ("entity_resolution_candidate", resolution.id),
    )
    for target_type, target_id in targets:
        with pytest.raises(ValueError, match="private knowledge"):
            storage.store_feedback(
                FeedbackItem(
                    new_id("feedback"),
                    "alice",
                    target_type,
                    target_id,
                    FeedbackType.GENERAL,
                    1.0,
                )
            )

    target_ids = tuple(target_id for _, target_id in targets)
    placeholders = ",".join("?" for _ in target_ids)
    feedback_rows = storage.execute(
        f"SELECT COUNT(*) AS count FROM feedback WHERE target_id IN ({placeholders})",  # nosec B608
        target_ids,
    ).fetchone()
    state_rows = storage.execute(
        f"SELECT COUNT(*) AS count FROM feedback_state WHERE target_id IN ({placeholders})",  # nosec B608
        target_ids,
    ).fetchone()
    assert int(feedback_rows["count"] if feedback_rows else -1) == 0
    assert int(state_rows["count"] if state_rows else -1) == 0


def test_entity_history_and_container_parent_follow_material_carrier_quarantine(storage) -> None:
    private, carrier, endpoint = _private_source_and_carrier(storage)
    storage.create_relation(
        Relation(
            new_id("rel"),
            "alice",
            endpoint.id,
            carrier.id,
            RelationType.PART_OF,
        )
    )
    _quarantine(storage, private)

    versions, matched, truncated = _bounded_public_entity_versions(
        storage,
        "alice",
        carrier.id,
    )
    assert versions == []
    assert matched == 0
    assert truncated is False

    containers = KnowledgeGraph(storage).list_containers("alice")
    by_id = {str(item["id"]): item for item in containers}
    assert carrier.id not in by_id
    assert endpoint.id in by_id
    assert by_id[endpoint.id]["parent_id"] is None
