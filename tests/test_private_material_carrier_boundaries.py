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


@pytest.mark.parametrize(
    "material,json_allowed,relation_allowed",
    [
        pytest.param(None, 0, 0, id="sql-null"),
        pytest.param("", 0, 0, id="empty"),
        pytest.param("{", 0, 0, id="malformed"),
        pytest.param('{"bad":}', 0, 0, id="malformed-value"),
        pytest.param("null", 0, 0, id="json-null"),
        pytest.param("[]", 0, 0, id="array"),
        pytest.param("42", 0, 0, id="number"),
        pytest.param('"text"', 0, 0, id="string"),
        pytest.param("true", 0, 0, id="boolean"),
        pytest.param("{}", 1, 1, id="empty-object"),
        pytest.param('{"note":"public"}', 1, 1, id="public"),
        pytest.param('{"origin":null}', 1, 1, id="absent-review"),
        pytest.param('{"origin":"manual"}', 1, 1, id="manual"),
        pytest.param('{"origin":"review"}', 1, 0, id="incomplete-review"),
        pytest.param('{"source":"reviewed_relation_candidate"}', 1, 0, id="incomplete-source"),
        pytest.param('{"nested":"[1]"}', 0, 1, id="opaque-nested"),
        pytest.param('{"note":"' + "a" * 53 + '"}', 1, 1, id="exact-bound"),
        pytest.param('{"note":"' + "a" * 54 + '"}', 0, 0, id="over-bound"),
        pytest.param("{" + "x" * 64, 0, 0, id="oversize-malformed"),
    ],
)
@pytest.mark.parametrize("kind", ["json", "relation"])
def test_composed_json_guards_are_total_and_preserve_review_intent(
    storage, monkeypatch, material, json_allowed, relation_allowed, kind
) -> None:
    """Flat CASE arms must retain lazy guards, NULL semantics and review denial."""
    import friday.storage._privacy as privacy

    monkeypatch.setattr(privacy, "_RELATION_PUBLIC_JSON_MAX_BYTES", 64)
    predicate = (
        privacy._not_private_bounded_json_dependency(
            "r.metadata_json", "r.user_id", max_bytes=64, reject_nested_json=True
        )
        if kind == "json"
        else privacy._not_private_relation_dependency("r")
    )
    # Values are bound, including malformed legacy JSON. The generated fragment
    # and column names are code-owned. Do not let JSON errors break another row.
    row = storage.execute(
        f"""WITH r AS (
                SELECT ? AS metadata_json, 'alice' AS user_id,
                       'source' AS source_entity_id, 'target' AS target_entity_id,
                       'related_to' AS relation_type
            ) SELECT {predicate} AS visible FROM r""",  # nosec B608
        (material,),
    ).fetchone()
    assert row["visible"] == (json_allowed if kind == "json" else relation_allowed)


@pytest.mark.parametrize("kind", ["json", "relation"])
def test_composed_json_size_guard_precedes_even_json_validation(storage, monkeypatch, kind) -> None:
    """A bounded guard must short-circuit parsing, not merely reject afterwards."""
    import friday.storage._privacy as privacy

    monkeypatch.setattr(privacy, "_RELATION_PUBLIC_JSON_MAX_BYTES", 64)
    predicate = (
        privacy._not_private_bounded_json_dependency("r.metadata_json", "r.user_id", max_bytes=64)
        if kind == "json"
        else privacy._not_private_relation_dependency("r")
    )
    calls = []

    def forbidden_validation(value):
        calls.append(value)
        raise AssertionError("oversized material reached the JSON parser")

    # Override only this fixture's connection, never the application globally.
    storage.conn.create_function("json_valid", 1, forbidden_validation)
    row = storage.execute(
        f"""WITH r AS (
                SELECT ? AS metadata_json, 'alice' AS user_id,
                       'source' AS source_entity_id, 'target' AS target_entity_id,
                       'related_to' AS relation_type
            ) SELECT {predicate} AS visible FROM r""",  # nosec B608
        ("{" + "x" * 64,),
    ).fetchone()
    assert row["visible"] == 0
    assert calls == []


def test_reviewed_replacement_is_visible_only_while_its_evidence_is_public(storage) -> None:
    """Compose real reviewed/superseding SQL even on SQLite's fixed parser stack."""
    from friday.storage._graph import _visible_superseding_relation_id
    from friday.storage._privacy import _not_private_relation_dependency

    storage.ensure_user("alice")
    private = Entity(new_id("ent"), "alice", "PRIVATE SOURCE 83c7", EntityType.EVENT)
    source = Entity(new_id("ent"), "alice", "Source", EntityType.PROJECT)
    target = Entity(new_id("ent"), "alice", "Target", EntityType.PROJECT)
    for entity in (private, source, target):
        storage.create_entity(entity)
    candidate = storage.store_relation_candidate(
        "alice",
        source.id,
        target.id,
        "related_to",
        confidence=0.8,
        evidence={"span": private.name},
    )
    assert storage.review_relation_candidate("alice", candidate["id"], "accepted", reviewed_by="alice")
    replacement = storage.execute(
        "SELECT id FROM relations WHERE user_id=? AND source_entity_id=? AND relation_type='related_to'",
        ("alice", source.id),
    ).fetchone()["id"]
    original = storage.create_relation(
        Relation(new_id("rel"), "alice", source.id, target.id, RelationType.PART_OF)
    )
    assert storage.invalidate_relation("alice", original.id, superseded_by=replacement)
    sql = f"""SELECT {_visible_superseding_relation_id("r")} AS replacement
                FROM relations r WHERE r.id=? AND r.user_id=?
                AND {_not_private_relation_dependency("r")}"""  # nosec B608
    assert storage.execute(sql, (original.id, "alice")).fetchone()["replacement"] == replacement
    assert storage.execute(sql, (original.id, "bob")).fetchone() is None
    assert storage.get_entity_relations(source.id, "alice")

    _quarantine(storage, private)
    # Neither endpoint was quarantined: the evidence alone revokes the derived
    # edge and its historical replacement pointer. A manual fact remains visible.
    assert storage.get_entity(source.id, "alice") is not None
    assert storage.get_entity(target.id, "alice") is not None
    assert storage.execute(sql, (original.id, "alice")).fetchone()["replacement"] is None
    assert all(row["id"] != replacement for row in storage.get_entity_relations(source.id, "alice"))
