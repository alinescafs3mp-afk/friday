"""Merge replay is a privacy boundary, not merely an audit convenience."""

from __future__ import annotations

import json
import unicodedata

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage._graph import _bounded_merge_history_rows, _count_merge_history
from friday.storage.models import (
    EntityResolutionCandidate,
    EntityType,
    KnowledgeObject,
    RawObject,
    new_id,
)


def _knowledge(storage, user_id: str, title: str, *, entity_id: str | None = None) -> str:
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), title, "text")
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        user_id,
        raw.id,
        entity_id=entity_id,
        content=title,
        title=title,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _quarantine(storage, entity_id: str, *, person_id: str = "bob") -> None:
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, occurred_end, precision, source, updated_at)
               SELECT id, user_id, '2026-08-10T10:00:00Z', NULL, 'day', ?,
                      '2026-08-06T00:00:00Z'
                 FROM entities WHERE id=?""",
            (f"reminder:{person_id}", entity_id),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', '2026-08-06T00:00:00Z')""",
            (entity_id, person_id),
        )


def test_merge_moves_only_the_public_dependency_closure(storage) -> None:
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Public source", EntityType.PROJECT)
    target = graph.create_entity("alice", "Public target", EntityType.PROJECT)
    third = graph.create_entity("alice", "PRIVATE THIRD PARTY 9f3a", EntityType.PERSON)

    public_knowledge = _knowledge(storage, "alice", "public merge document")
    hidden_knowledge = _knowledge(
        storage,
        "alice",
        "synthetic hidden document",
        entity_id=source["id"],
    )
    public_link = graph.link_knowledge_to_entity(public_knowledge, source["id"], "alice")
    hidden_source_link = graph.link_knowledge_to_entity(hidden_knowledge, source["id"], "alice")
    graph.link_knowledge_to_entity(hidden_knowledge, third["id"], "alice")
    hidden_relation = graph.create_relation("alice", source["id"], third["id"])
    hidden_candidate = storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id="alice",
            entity_a_id=source["id"],
            entity_b_id=third["id"],
            confidence=0.91,
            resolution_method="synthetic",
            evidence_json={"knowledge_object_id": hidden_knowledge},
        )
    )
    _quarantine(storage, third["id"])

    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")

    public_row = storage.execute(
        "SELECT entity_id FROM knowledge_entity_links WHERE id=?", (public_link["id"],)
    ).fetchone()
    # The original public link is replaced by its recorded target copy.
    assert public_row is None
    assert storage.execute(
        """SELECT 1 FROM knowledge_entity_links
           WHERE user_id='alice' AND knowledge_object_id=? AND entity_id=?""",
        (public_knowledge, target["id"]),
    ).fetchone()
    hidden_link_after = storage.execute(
        "SELECT entity_id FROM knowledge_entity_links WHERE id=?", (hidden_source_link["id"],)
    ).fetchone()
    assert hidden_link_after is not None and hidden_link_after["entity_id"] == source["id"]
    assert (
        storage.execute("SELECT entity_id FROM knowledge_objects WHERE id=?", (hidden_knowledge,)).fetchone()[
            "entity_id"
        ]
        == source["id"]
    )
    relation_after = storage.execute(
        "SELECT source_entity_id, target_entity_id FROM relations WHERE id=?",
        (hidden_relation.id,),
    ).fetchone()
    assert dict(relation_after) == {
        "source_entity_id": source["id"],
        "target_entity_id": third["id"],
    }
    assert (
        storage.execute(
            "SELECT status FROM entity_resolution_candidates WHERE id=?", (hidden_candidate.id,)
        ).fetchone()["status"]
        == "suggested"
    )

    raw_history = storage.execute(
        "SELECT transfer_json FROM entity_merge_history WHERE id=?", (merged["_merge_id"],)
    ).fetchone()
    encoded_transfer = str(raw_history["transfer_json"])
    assert hidden_knowledge not in encoded_transfer
    assert hidden_source_link["id"] not in encoded_transfer
    assert hidden_relation.id not in encoded_transfer
    assert hidden_candidate.id not in encoded_transfer
    assert third["id"] not in encoded_transfer
    assert storage.get_merge_history(merged["_merge_id"], "alice") is not None


def test_later_private_relation_hides_get_list_count_and_undo_atomically(storage) -> None:
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Merge source", EntityType.PROJECT)
    target = graph.create_entity("alice", "Merge target", EntityType.PROJECT)
    third = graph.create_entity("alice", "PRIVATE RELATION ENDPOINT e21c", EntityType.PERSON)
    relation = graph.create_relation("alice", source["id"], third["id"])
    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    merge_id = merged["_merge_id"]
    _quarantine(storage, third["id"])

    assert storage.get_merge_history(merge_id, "alice") is None
    assert storage.list_merge_history("alice") == []
    assert _bounded_merge_history_rows(storage, "alice") == []
    assert _count_merge_history(storage, "alice") == 0
    with pytest.raises(ValueError, match="^Merge history entry not found$"):
        storage.unmerge_entities("alice", merge_id, undone_by="owner")

    source_after = storage.execute(
        "SELECT merged_into_id, canonical FROM entities WHERE id=?", (source["id"],)
    ).fetchone()
    assert dict(source_after) == {"merged_into_id": target["id"], "canonical": 0}
    relation_after = storage.execute(
        "SELECT source_entity_id, target_entity_id FROM relations WHERE id=?", (relation.id,)
    ).fetchone()
    assert dict(relation_after) == {
        "source_entity_id": target["id"],
        "target_entity_id": third["id"],
    }


def test_opaque_or_hidden_transfer_material_fails_closed_before_replay(storage) -> None:
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Simple source", EntityType.PROJECT)
    target = graph.create_entity("alice", "Simple target", EntityType.PROJECT)
    private = graph.create_entity("alice", "ТАЙНЫЙ ЙОД 86b1", EntityType.PERSON)
    hidden_knowledge = _knowledge(storage, "alice", "hidden dependency", entity_id=private["id"])
    candidate = storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id="alice",
            entity_a_id=private["id"],
            entity_b_id=target["id"],
            confidence=0.8,
            resolution_method="synthetic",
        )
    )
    relation_candidate = storage.store_relation_candidate(
        "alice",
        private["id"],
        target["id"],
        "related_to",
        confidence=0.7,
    )
    merged = storage.merge_entities("alice", source["id"], target["id"], merged_by="owner")
    merge_id = merged["_merge_id"]
    baseline = dict(storage.execute("SELECT * FROM entity_merge_history WHERE id=?", (merge_id,)).fetchone())
    _quarantine(storage, private["id"])

    transfer = json.loads(baseline["transfer_json"])
    variants: list[tuple[str, str]] = []

    nested = dict(transfer)
    private_spelling = unicodedata.normalize("NFD", private["name"].lower())
    nested["opaque"] = json.dumps({"copy": private_spelling}, ensure_ascii=True)
    variants.append(("transfer_json", json.dumps(nested, ensure_ascii=False)))

    malformed_nested = dict(transfer)
    malformed_nested["opaque"] = "{not-json"
    variants.append(("transfer_json", json.dumps(malformed_nested, ensure_ascii=False)))

    hidden_primary = dict(transfer)
    hidden_primary["primary_moved"] = [hidden_knowledge]
    variants.append(("transfer_json", json.dumps(hidden_primary, ensure_ascii=False)))

    hidden_candidate = dict(transfer)
    hidden_candidate["closed_candidates"] = [candidate.id]
    variants.append(("transfer_json", json.dumps(hidden_candidate, ensure_ascii=False)))

    hidden_relation_candidate = dict(transfer)
    hidden_relation_candidate["opaque"] = {"candidate_id": relation_candidate["id"]}
    variants.append(("transfer_json", json.dumps(hidden_relation_candidate, ensure_ascii=False)))

    hidden_time = dict(transfer)
    hidden_time["time_moved"] = [
        {
            "entity_id": source["id"],
            "user_id": "alice",
            "occurred_at": "2026-08-10T10:00:00Z",
            "occurred_end": None,
            "precision": "day",
            "source": "reminder:bob",
            "updated_at": "2026-08-06T00:00:00Z",
        }
    ]
    variants.append(("transfer_json", json.dumps(hidden_time, ensure_ascii=False)))

    malformed_shape = dict(transfer)
    malformed_shape["links_moved"] = [{"private": private["id"]}]
    variants.append(("transfer_json", json.dumps(malformed_shape, ensure_ascii=False)))

    oversized_snapshot = json.loads(baseline["source_snapshot_json"])
    oversized_snapshot["description"] = "x" * 1_048_577
    variants.append(("source_snapshot_json", json.dumps(oversized_snapshot)))

    for field, value in variants:
        with storage.transaction() as conn:
            conn.execute(
                f"UPDATE entity_merge_history SET {field}=? WHERE id=?",  # nosec B608
                (value, merge_id),
            )
        assert storage.get_merge_history(merge_id, "alice") is None
        assert storage.list_merge_history("alice") == []
        assert _count_merge_history(storage, "alice") == 0
        with pytest.raises(ValueError, match="^Merge history entry not found$"):
            storage.unmerge_entities("alice", merge_id, undone_by="owner")
        assert (
            storage.execute("SELECT merged_into_id FROM entities WHERE id=?", (source["id"],)).fetchone()[
                "merged_into_id"
            ]
            == target["id"]
        )
        with storage.transaction() as conn:
            conn.execute(
                """UPDATE entity_merge_history
                   SET source_snapshot_json=?, target_before_json=?, target_after_json=?,
                       transfer_json=? WHERE id=?""",
                (
                    baseline["source_snapshot_json"],
                    baseline["target_before_json"],
                    baseline["target_after_json"],
                    baseline["transfer_json"],
                    merge_id,
                ),
            )

    assert storage.get_merge_history(merge_id, "alice") is not None
