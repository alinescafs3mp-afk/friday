"""A tenant export is an egress boundary, not a bypass around reminder privacy."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage import FridayStorage
from friday.storage._base import pack_snapshot
from friday.storage.models import (
    Entity,
    EntityResolutionCandidate,
    EntityType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationType,
    new_id,
)


def _entity(storage, user_id: str, name: str, kind: EntityType = EntityType.EVENT) -> Entity:
    entity = Entity(new_id("ent"), user_id, name, kind)
    storage.create_entity(entity)
    return entity


def _read_export(storage, user_id: str) -> dict:
    result = storage.export_user(user_id)
    return json.loads(Path(result["path"]).read_text(encoding="utf-8"))


def _mark_private_reminder(storage, entity_id: str, user_id: str, person_id: str) -> None:
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', ?, ?)""",
            (
                entity_id,
                user_id,
                "2026-08-10T09:00:00Z",
                f"reminder:{person_id}",
                "2026-08-06T00:00:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (entity_id, person_id, "2026-08-06T00:00:00Z"),
        )


def test_export_uses_current_and_historical_private_alias_identity_tokens(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    old_alias = "PRIVATE-EXPORT-HISTORICAL-ALIAS-2e90"
    private = Entity(
        new_id("ent"),
        "bob",
        "Private export alias authority",
        EntityType.EVENT,
        aliases_json=[old_alias],
    )
    storage.create_entity(private)
    private.aliases_json = ["PRIVATE-EXPORT-CURRENT-ALIAS-3f81"]
    storage.update_entity(private)
    copied_alias = unicodedata.normalize("NFD", old_alias.casefold())
    carrier_old_alias = "PRIVATE-EXPORT-CARRIER-HISTORICAL-ALIAS-84d2"
    carrier = Entity(
        new_id("ent"),
        "alice",
        "Otherwise public export carrier",
        EntityType.PROJECT,
        description=f"Copied historical alias: {copied_alias}",
        aliases_json=[carrier_old_alias],
    )
    storage.create_entity(carrier)
    carrier.aliases_json = ["PRIVATE-EXPORT-CARRIER-CURRENT-ALIAS-915b"]
    storage.update_entity(carrier)
    named_carrier = Entity(
        new_id("ent"),
        "alice",
        copied_alias,
        EntityType.PROJECT,
    )
    storage.create_entity(named_carrier)
    transitive_carrier = Entity(
        new_id("ent"),
        "alice",
        "Transitive historical alias carrier",
        EntityType.PROJECT,
        description=f"Copied carrier alias: {carrier_old_alias}",
    )
    storage.create_entity(transitive_carrier)
    raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        f"Copied historical alias: {copied_alias}",
        "text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        content=f"Copied historical alias: {copied_alias}",
        title="Otherwise public export knowledge",
    )
    storage.store_knowledge_object(knowledge)
    _mark_private_reminder(storage, private.id, "bob", "bob")

    assert storage.get_entity(carrier.id, "alice") is None
    assert storage.get_entity(named_carrier.id, "alice") is None
    assert storage.get_entity(transitive_carrier.id, "alice") is None
    assert storage.get_raw_object(raw.id, "alice") is None
    assert storage.get_knowledge_object(knowledge.id, "alice") is None
    payload = _read_export(storage, "alice")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert copied_alias not in encoded
    assert old_alias not in encoded
    assert carrier.id not in encoded
    assert named_carrier.id not in encoded
    assert transitive_carrier.id not in encoded
    assert raw.id not in encoded
    assert knowledge.id not in encoded


def test_export_keeps_dependencies_of_the_users_exact_private_alias(storage):
    storage.ensure_user("alice")
    own_alias = "ALICE-OWN-PRIVATE-ALIAS-a694"
    private = Entity(
        new_id("ent"),
        "alice",
        "Alice private export authority",
        EntityType.EVENT,
        aliases_json=[own_alias],
    )
    storage.create_entity(private)
    carrier = Entity(
        new_id("ent"),
        "alice",
        "Alice owned alias carrier",
        EntityType.PROJECT,
        description=f"Owned reminder context: {own_alias}",
    )
    storage.create_entity(carrier)
    raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        f"Owned reminder context: {own_alias}",
        "text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        content=f"Owned reminder context: {own_alias}",
        title="Alice-owned reminder knowledge",
    )
    storage.store_knowledge_object(knowledge)
    _mark_private_reminder(storage, private.id, "alice", "alice")

    payload = _read_export(storage, "alice")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert private.id in encoded
    assert carrier.id in encoded
    assert raw.id in encoded
    assert knowledge.id in encoded
    assert own_alias in encoded


def test_export_closes_foreign_private_material_across_foreign_carriers(storage):
    for user_id in ("alice", "bob", "charlie"):
        storage.ensure_user(user_id)
    private_name = "BOB-PRIVATE-FOREIGN-CLOSURE-77d1"
    private = Entity(new_id("ent"), "bob", private_name, EntityType.EVENT)
    storage.create_entity(private)
    foreign_carrier_name = "CHARLIE-FOREIGN-CARRIER-IDENTITY-60ab"
    foreign_carrier = Entity(
        new_id("ent"),
        "charlie",
        foreign_carrier_name,
        EntityType.PROJECT,
        description=f"Copied Bob identity: {private_name}",
    )
    storage.create_entity(foreign_carrier)
    alice_carrier = Entity(
        new_id("ent"),
        "alice",
        "Alice transitive foreign carrier",
        EntityType.PROJECT,
        description=f"Copied Charlie identity: {foreign_carrier_name}",
    )
    storage.create_entity(alice_carrier)
    monitor = storage.create_monitor(
        "alice",
        f"Watch copied Charlie identity: {foreign_carrier_name}",
        chat_id="alice-safe-chat",
        created_by="alice",
    )
    _mark_private_reminder(storage, private.id, "bob", "bob")

    assert storage.get_entity(private.id, "bob") is None
    assert storage.get_entity(foreign_carrier.id, "charlie") is None
    assert storage.get_entity(alice_carrier.id, "alice") is None
    encoded = json.dumps(_read_export(storage, "alice"), ensure_ascii=False, sort_keys=True)
    assert alice_carrier.id not in encoded
    assert foreign_carrier_name not in encoded
    assert monitor["id"] not in encoded


def test_export_matches_fail_closed_current_shapes_and_version_authentication(storage):
    storage.ensure_user("alice")
    malformed_entity = _entity(storage, "alice", "Malformed current entity")
    invalid_history_entity = _entity(storage, "alice", "Invalid history entity")
    invalid_snapshot = dict(storage.get_entity(invalid_history_entity.id, "alice") or {})
    invalid_snapshot["version"] = 999
    raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        "otherwise public raw body",
        "text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        content="otherwise public knowledge",
        title="Malformed raw dependency",
    )
    storage.store_knowledge_object(knowledge)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entities SET aliases_json='{' WHERE id=?",
            (malformed_entity.id,),
        )
        conn.execute(
            "UPDATE raw_objects SET metadata_json='{' WHERE id=?",
            (raw.id,),
        )
        conn.execute(
            """INSERT INTO entity_versions(
                   id, user_id, entity_id, version, snapshot_json, created_at)
               VALUES(?, 'alice', ?, 2, ?, '2026-08-06T00:00:00Z')""",
            (
                new_id("entv"),
                invalid_history_entity.id,
                json.dumps(invalid_snapshot, ensure_ascii=False),
            ),
        )

    assert storage.get_entity(malformed_entity.id, "alice") is None
    assert storage.get_entity(invalid_history_entity.id, "alice") is None
    assert storage.get_raw_object(raw.id, "alice") is None
    assert storage.get_knowledge_object(knowledge.id, "alice") is None
    encoded = json.dumps(_read_export(storage, "alice"), ensure_ascii=False, sort_keys=True)
    for hidden_id in (
        malformed_entity.id,
        invalid_history_entity.id,
        raw.id,
        knowledge.id,
    ):
        assert hidden_id not in encoded


def test_export_excludes_malformed_raw_without_any_private_identity_tokens(storage):
    storage.ensure_user("alice")
    raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        "otherwise public raw body",
        "text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        content="otherwise public knowledge",
        title="Malformed raw dependency",
    )
    storage.store_knowledge_object(knowledge)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json='{' WHERE id=?",
            (raw.id,),
        )

    assert storage.get_raw_object(raw.id, "alice") is None
    assert storage.get_knowledge_object(knowledge.id, "alice") is None
    encoded = json.dumps(_read_export(storage, "alice"), ensure_ascii=False, sort_keys=True)
    assert raw.id not in encoded
    assert knowledge.id not in encoded


def test_export_authenticates_a_knowledge_versions_raw_identity(storage):
    storage.ensure_user("alice")
    sentinel = "INVALID-KOV-MISSING-RAW-IDENTITY-48c2"
    raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        "public raw body",
        "text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        content="public current knowledge",
        title="Public current title",
    )
    storage.store_knowledge_object(knowledge)
    invalid_snapshot = dict(storage.get_knowledge_object(knowledge.id, "alice") or {})
    invalid_snapshot.pop("raw_object_id", None)
    invalid_snapshot["content"] = sentinel
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_object_versions SET snapshot_json=? WHERE knowledge_object_id=?",
            (
                pack_snapshot(json.dumps(invalid_snapshot, ensure_ascii=False)),
                knowledge.id,
            ),
        )

    assert storage.get_knowledge_object(knowledge.id, "alice") is not None
    assert storage.list_knowledge_versions(knowledge.id, "alice") == []
    encoded = json.dumps(_read_export(storage, "alice"), ensure_ascii=False, sort_keys=True)
    assert sentinel not in encoded
    assert knowledge.id not in encoded


def test_shared_export_excludes_a_quarantined_reminder_and_every_dependency(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    sentinel = "BOB-PRIVATE-REMINDER-EXPORT-9f42d7"

    public_left = _entity(storage, shared, "Общий проект", EntityType.PROJECT)
    public_right = _entity(storage, shared, "Общая организация", EntityType.ORGANIZATION)
    private = _entity(storage, shared, sentinel)

    public_relation = Relation(
        new_id("rel"),
        shared,
        public_left.id,
        public_right.id,
        RelationType.RELATED_TO,
        metadata_json={"origin": "test"},
    )
    private_relation = Relation(
        new_id("rel"),
        shared,
        private.id,
        public_left.id,
        RelationType.RELATED_TO,
        metadata_json={"evidence": sentinel},
    )
    storage.create_relation(public_relation)
    storage.create_relation(private_relation)
    private_candidate = storage.store_relation_candidate(
        shared,
        private.id,
        public_right.id,
        RelationType.RELATED_TO.value,
        confidence=0.8,
        evidence={"private": sentinel},
    )
    resolution = EntityResolutionCandidate(
        new_id("er"),
        shared,
        private.id,
        public_right.id,
        0.8,
        "synthetic",
        evidence_json={"private": sentinel},
    )
    storage.store_resolution_candidate(resolution)

    raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), sentinel, "text")
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        raw.id,
        entity_id=private.id,
        content=sentinel,
        title=sentinel,
    )
    storage.store_knowledge_object(knowledge)
    link = storage.link_knowledge_entity(
        shared,
        knowledge.id,
        private.id,
        evidence={"private": sentinel},
        reviewed_by="bob",
    )
    merge_id = new_id("merge")
    request_key = "synthetic-export-request"
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_merge_history(
                   id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json,
                   transfer_json, merged_by, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'bob', ?)""",
            (
                merge_id,
                shared,
                private.id,
                public_right.id,
                json.dumps({"name": sentinel}),
                "{}",
                json.dumps({"aliases": [sentinel]}),
                json.dumps({"time_moved": [{"source": "reminder:bob", "name": sentinel}]}),
                "2026-08-06T00:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO request_idempotency(
                   user_id, request_key, request_hash, response_json, state,
                   lease_token, created_at, updated_at)
               VALUES(?, ?, 'safe-hash', ?, 'complete', '', ?, ?)""",
            (
                shared,
                request_key,
                json.dumps({"body": sentinel}),
                "2026-08-06T00:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
            ),
        )

    # Mark it private only after legacy dependencies already exist.  This is the
    # exact pre-isolation shape the startup migration must quarantine.
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (
                private.id,
                shared,
                "2026-08-07T09:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00+00:00"),
        )

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert sentinel not in encoded
    assert private.id not in encoded
    assert raw.id not in encoded
    assert knowledge.id not in encoded
    assert private_relation.id not in encoded
    assert private_candidate["id"] not in encoded
    assert resolution.id not in encoded
    assert merge_id not in encoded
    assert link["id"] not in encoded
    assert "response_json" not in payload["request_idempotency"][0]
    assert payload["request_idempotency"][0]["request_key"] == request_key

    assert {row["id"] for row in payload["entities"]} == {public_left.id, public_right.id}
    assert {row["id"] for row in payload["relations"]} == {public_relation.id}
    assert {row["relation_id"] for row in payload["relation_revisions"]} == {public_relation.id}


def test_export_requires_exact_matching_owner_marker_and_reminder_source(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("alice")
    storage.ensure_user("bob")

    own = _entity(storage, "bob", "BOB-OWN-REMINDER-7b31")
    storage.set_entity_time(
        own.id,
        "bob",
        "2026-08-08T10:00:00+00:00",
        source="reminder:bob",
    )

    source_only = _entity(storage, shared, "SOURCE-ONLY-PRIVATE-a18d")
    marker_only = _entity(storage, shared, "MARKER-ONLY-PRIVATE-c39e")
    mismatch = _entity(storage, shared, "MISMATCH-PRIVATE-e52f")
    exact_owner = _entity(storage, shared, "OWNER-OWN-REMINDER-f64a")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', ?, ?)""",
            (
                source_only.id,
                shared,
                "2026-08-08T11:00:00+00:00",
                "reminder:bob",
                "2026-08-06T00:00:00+00:00",
            ),
        )
        for entity_id, person_id in (
            (marker_only.id, shared),
            (mismatch.id, "alice"),
            (exact_owner.id, shared),
        ):
            conn.execute(
                """INSERT INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, ?, 'reminder', ?)""",
                (entity_id, person_id, "2026-08-06T00:00:00+00:00"),
            )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', ?, ?)""",
            (
                mismatch.id,
                shared,
                "2026-08-08T12:00:00+00:00",
                "reminder:bob",
                "2026-08-06T00:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', ?, ?)""",
            (
                exact_owner.id,
                shared,
                "2026-08-08T13:00:00+00:00",
                f"reminder:{shared}",
                "2026-08-06T00:00:00+00:00",
            ),
        )

    shared_payload = _read_export(storage, shared)
    shared_names = {row["name"] for row in shared_payload["entities"]}
    assert "OWNER-OWN-REMINDER-f64a" in shared_names
    assert "SOURCE-ONLY-PRIVATE-a18d" not in shared_names
    assert "MARKER-ONLY-PRIVATE-c39e" not in shared_names
    assert "MISMATCH-PRIVATE-e52f" not in shared_names
    assert shared_payload["private_entity_owners"] == [
        next(row for row in shared_payload["private_entity_owners"] if row["entity_id"] == exact_owner.id)
    ]

    bob_payload = _read_export(storage, "bob")
    assert {row["id"] for row in bob_payload["entities"]} == {own.id}
    assert {row["entity_id"] for row in bob_payload["entity_time"]} == {own.id}
    assert {row["entity_id"] for row in bob_payload["private_entity_owners"]} == {own.id}


def test_export_closes_primary_link_and_inbox_knowledge_dependencies(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    private = _entity(storage, shared, "DEPENDENCY-PRIVATE-ENTITY-1ad7")

    seeded: list[tuple[str, str, str]] = []
    for seam in ("primary", "link", "inbox"):
        sentinel = f"PRIVATE-{seam.upper()}-COPY-6c2e"
        raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), sentinel, "text")
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            new_id("ko"),
            shared,
            raw.id,
            entity_id=private.id if seam == "primary" else None,
            content=sentinel,
            title=sentinel,
        )
        storage.store_knowledge_object(knowledge)
        seeded.append((sentinel, raw.id, knowledge.id))
        if seam == "link":
            storage.link_knowledge_entity(
                shared,
                knowledge.id,
                private.id,
                evidence={"private": sentinel},
            )
        elif seam == "inbox":
            with storage.transaction() as conn:
                conn.execute(
                    """INSERT INTO inbox(
                           id, user_id, raw_object_id, knowledge_object_id, status,
                           suggested_entity_id, suggestions_json, classification_notes, created_at)
                       VALUES(?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                    (
                        new_id("inbox"),
                        shared,
                        raw.id,
                        knowledge.id,
                        private.id,
                        json.dumps({"private": sentinel}),
                        sentinel,
                        "2026-08-06T00:00:00+00:00",
                    ),
                )

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (
                private.id,
                shared,
                "2026-08-09T10:00:00+00:00",
                "2026-08-06T00:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00+00:00"),
        )

    encoded = json.dumps(_read_export(storage, shared), ensure_ascii=False, sort_keys=True)
    for sentinel, raw_id, knowledge_id in seeded:
        assert sentinel not in encoded
        assert raw_id not in encoded
        assert knowledge_id not in encoded


def test_export_excludes_a_public_knowledge_object_with_a_private_historical_snapshot(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    public = _entity(storage, shared, "Public version parent", EntityType.PROJECT)
    private = _entity(storage, shared, "PRIVATE-HISTORICAL-ENTITY-71d9")
    sentinel = "PRIVATE-KOV-BODY-b82c"
    raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "public current raw", "text")
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        raw.id,
        entity_id=public.id,
        content="public current body",
        title="public current title",
    )
    storage.store_knowledge_object(knowledge)
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE knowledge_object_versions SET snapshot_json=?
                 WHERE knowledge_object_id=?""",
            (
                json.dumps(
                    {
                        "id": knowledge.id,
                        "user_id": shared,
                        "raw_object_id": raw.id,
                        "entity_id": private.id,
                        "content": sentinel,
                        "title": sentinel,
                    },
                    ensure_ascii=False,
                ),
                knowledge.id,
            ),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (private.id, shared, "2026-08-10T09:00:00Z", "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00Z"),
        )

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert public.id in {row["id"] for row in payload["entities"]}
    assert knowledge.id not in encoded
    assert raw.id not in encoded
    assert private.id not in encoded
    assert sentinel not in encoded


def test_export_excludes_an_entity_whose_version_points_at_a_private_target(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    public = _entity(storage, shared, "Public entity with old merge pointer", EntityType.PROJECT)
    private = _entity(storage, shared, "PRIVATE-ENTITY-VERSION-TARGET-49ae")
    sentinel = "PRIVATE-ENTITY-VERSION-BODY-55c1"
    public_snapshot = dict(storage.get_entity(public.id, shared) or {})
    public_snapshot["merged_into_id"] = private.id
    public_snapshot["description"] = sentinel
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_versions SET snapshot_json=? WHERE entity_id=?",
            (json.dumps(public_snapshot, ensure_ascii=False), public.id),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (private.id, shared, "2026-08-10T10:00:00Z", "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00Z"),
        )

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert public.id not in encoded
    assert private.id not in encoded
    assert sentinel not in encoded


def test_export_rejects_merge_history_with_a_hidden_nested_dependency(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    source = _entity(storage, shared, "Public merge source", EntityType.PROJECT)
    target = _entity(storage, shared, "Public merge target", EntityType.PROJECT)
    private = _entity(storage, shared, "PRIVATE-MERGE-THIRD-PARTY-e4f6")
    sentinels = {
        "source": "PRIVATE-MERGE-SOURCE-SNAPSHOT-b7f4",
        "before": "PRIVATE-MERGE-TARGET-BEFORE-05d8",
        "after": "PRIVATE-MERGE-TARGET-AFTER-b2c9",
        "transfer": "PRIVATE-MERGE-TRANSFER-802e",
    }
    source_snapshot = dict(storage.get_entity(source.id, shared) or {})
    target_before = dict(storage.get_entity(target.id, shared) or {})
    target_after = dict(target_before)
    source_snapshot["description"] = sentinels["source"]
    target_before["description"] = sentinels["before"]
    target_after["description"] = sentinels["after"]
    merge_id = new_id("merge")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_merge_history(
                   id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json,
                   transfer_json, merged_by, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                merge_id,
                shared,
                source.id,
                target.id,
                json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps(target_before, ensure_ascii=False),
                json.dumps(target_after, ensure_ascii=False),
                json.dumps(
                    {
                        "relations": [
                            {
                                "original": {
                                    "source_entity_id": source.id,
                                    "target_entity_id": private.id,
                                    "metadata_json": sentinels["transfer"],
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                shared,
                "2026-08-06T00:00:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (private.id, shared, "2026-08-10T11:00:00Z", "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00Z"),
        )

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert {source.id, target.id} <= {row["id"] for row in payload["entities"]}
    assert merge_id not in encoded
    assert private.id not in encoded
    for sentinel in sentinels.values():
        assert sentinel not in encoded


def test_export_rejects_embedded_private_material_with_public_outer_keys(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    left = _entity(storage, shared, "Public evidence left", EntityType.PROJECT)
    right = _entity(storage, shared, "Public evidence right", EntityType.ORGANIZATION)
    private = _entity(storage, "bob", "ЛИЧНОЕ-НАПОМИНАНИЕ-ИЗ-EVIDENCE-a93f")
    storage.set_entity_time(
        private.id,
        "bob",
        "2026-08-11T09:00:00Z",
        source="reminder:bob",
    )
    evidence = {"entity_id": private.id, "name": private.name}
    relation = Relation(
        new_id("rel"),
        shared,
        left.id,
        right.id,
        RelationType.RELATED_TO,
        metadata_json=evidence,
    )
    storage.create_relation(relation)

    linked_raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "safe linked raw", "text")
    storage.store_raw_object(linked_raw)
    linked_knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        linked_raw.id,
        content="safe linked body",
        title="safe linked title",
    )
    storage.store_knowledge_object(linked_knowledge)
    link = storage.link_knowledge_entity(
        shared,
        linked_knowledge.id,
        left.id,
        evidence=evidence,
    )
    escaped_evidence = json.dumps({"name": private.name}, ensure_ascii=True)
    with storage.transaction() as conn:
        # The private token exists only after json.loads: raw substring scans do
        # not see Cyrillic represented as \uXXXX escapes.
        conn.execute("UPDATE relations SET metadata_json=? WHERE id=?", (escaped_evidence, relation.id))
        conn.execute(
            "UPDATE knowledge_entity_links SET evidence_json=? WHERE id=?",
            (escaped_evidence, link["id"]),
        )

    inbox_raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "safe inbox raw", "text")
    storage.store_raw_object(inbox_raw)
    inbox_id = new_id("inbox")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO inbox(
                   id, user_id, raw_object_id, status, suggestions_json,
                   classification_notes, created_at)
               VALUES(?, ?, ?, 'pending', ?, ?, ?)""",
            (
                inbox_id,
                shared,
                inbox_raw.id,
                escaped_evidence,
                private.name,
                "2026-08-06T00:00:00Z",
            ),
        )

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert {left.id, right.id} <= {row["id"] for row in payload["entities"]}
    for hidden in (
        private.id,
        private.name,
        relation.id,
        linked_raw.id,
        linked_knowledge.id,
        link["id"],
        inbox_raw.id,
        inbox_id,
    ):
        assert hidden not in encoded


def test_startup_keeps_a_reminder_shared_when_only_a_packed_knowledge_version_depends_on_it(
    settings,
):
    shared = LEGACY_OWNER_USER_ID
    store = FridayStorage(settings)
    reminder = _entity(store, shared, "PRIVATE-PACKED-KOV-REMINDER-4be7")
    store.ensure_user("bob")
    raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "public raw", "text")
    store.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        raw.id,
        content="public current body",
        title="public current title",
    )
    store.store_knowledge_object(knowledge)
    old_private_body = "PRIVATE-PACKED-KOV-BODY-63cf"
    with store.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_object_versions SET snapshot_json=? WHERE knowledge_object_id=?",
            (
                pack_snapshot(
                    json.dumps(
                        {
                            "id": knowledge.id,
                            "user_id": shared,
                            "raw_object_id": raw.id,
                            "entity_id": reminder.id,
                            "content": old_private_body,
                        },
                        ensure_ascii=False,
                    )
                ),
                knowledge.id,
            ),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (reminder.id, shared, "2026-08-12T09:00:00Z", "2026-08-06T00:00:00Z"),
        )
    store.close(final=True)

    reopened = FridayStorage(settings)
    try:
        row = reopened.execute("SELECT user_id FROM entities WHERE id=?", (reminder.id,)).fetchone()
        assert row is not None and row["user_id"] == shared
        marker = reopened.execute(
            "SELECT person_id FROM private_entity_owners WHERE entity_id=?", (reminder.id,)
        ).fetchone()
        assert marker is not None and marker["person_id"] == "bob"
        encoded = json.dumps(_read_export(reopened, shared), ensure_ascii=False, sort_keys=True)
        assert reminder.id not in encoded
        assert knowledge.id not in encoded
        assert raw.id not in encoded
        assert old_private_body not in encoded
    finally:
        reopened.close(final=True)


def test_startup_keeps_a_reminder_shared_when_inbox_json_is_its_only_dependency(settings):
    shared = LEGACY_OWNER_USER_ID
    store = FridayStorage(settings)
    reminder = _entity(store, shared, "PRIVATE-INBOX-ONLY-REMINDER-9f5a")
    store.ensure_user("bob")
    raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "public inbox raw", "text")
    store.store_raw_object(raw)
    inbox_id = new_id("inbox")
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO inbox(
                   id, user_id, raw_object_id, status, suggested_entity_id,
                   suggestions_json, classification_notes, created_at)
               VALUES(?, ?, ?, 'pending', NULL, ?, ?, ?)""",
            (
                inbox_id,
                shared,
                raw.id,
                json.dumps({"candidate": {"id": reminder.id, "name": reminder.name}}, ensure_ascii=False),
                reminder.name,
                "2026-08-06T00:00:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (reminder.id, shared, "2026-08-12T10:00:00Z", "2026-08-06T00:00:00Z"),
        )
    store.close(final=True)

    reopened = FridayStorage(settings)
    try:
        row = reopened.execute("SELECT user_id FROM entities WHERE id=?", (reminder.id,)).fetchone()
        assert row is not None and row["user_id"] == shared
        encoded = json.dumps(_read_export(reopened, shared), ensure_ascii=False, sort_keys=True)
        assert reminder.id not in encoded
        assert reminder.name not in encoded
        assert inbox_id not in encoded
        assert raw.id not in encoded
    finally:
        reopened.close(final=True)


def test_startup_securely_invalidates_pre_privacy_idempotency_responses(settings):
    store = FridayStorage(settings)
    store.ensure_user("alice")
    sentinel = "PRIVATE-LEGACY-IDEMPOTENCY-RESPONSE-24ea"
    with store.transaction() as conn:
        conn.execute("DELETE FROM schema_meta WHERE key='idempotency_response_privacy'")
        conn.execute(
            """INSERT INTO request_idempotency(
                   user_id, request_key, request_hash, response_json, state,
                   lease_token, created_at, updated_at)
               VALUES('alice', 'legacy-complete', '', ?, 'complete', '', ?, ?)""",
            (
                json.dumps({"response": sentinel}, ensure_ascii=False),
                "2026-08-06T00:00:00Z",
                "2026-08-06T00:00:00Z",
            ),
        )
        conn.execute(
            """INSERT INTO request_idempotency(
                   user_id, request_key, request_hash, response_json, state,
                   lease_token, created_at, updated_at)
               VALUES('alice', 'active-pending', '', '{}', 'pending', 'lease-safe', ?, ?)""",
            ("2026-08-06T00:00:00Z", "2026-08-06T00:00:00Z"),
        )
    store.close(final=True)

    reopened = FridayStorage(settings)
    try:
        assert reopened.idempotency_get("alice", "legacy-complete") is None
        pending = reopened.execute(
            "SELECT state, lease_token FROM request_idempotency WHERE request_key='active-pending'"
        ).fetchone()
        assert pending is not None
        assert (pending["state"], pending["lease_token"]) == ("pending", "lease-safe")
        marker = reopened.execute(
            "SELECT value FROM schema_meta WHERE key='idempotency_response_privacy'"
        ).fetchone()
        assert marker is not None and marker["value"] == "v1"
    finally:
        reopened.close(final=True)

    secret = sentinel.encode("utf-8")
    database = Path(settings.database_path)
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if candidate.exists():
            assert secret not in candidate.read_bytes()


def test_current_idempotency_responses_survive_ordinary_reopen_after_privacy_marker(settings):
    store = FridayStorage(settings)
    store.ensure_user("alice")
    store.idempotency_store("alice", "current-response", {"status": "current-safe"})
    store.close(final=True)

    reopened = FridayStorage(settings)
    try:
        assert reopened.idempotency_get("alice", "current-response") == {"status": "current-safe"}
    finally:
        reopened.close(final=True)


def test_startup_treats_a_private_name_inside_packed_history_as_a_dependency(settings):
    shared = LEGACY_OWNER_USER_ID
    store = FridayStorage(settings)
    reminder = _entity(store, shared, "СЕКРЕТНОЕ НАПОМИНАНИЕ ТОЛЬКО В ВЕРСИИ 82d4")
    store.ensure_user("bob")
    raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "public current raw", "text")
    store.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        raw.id,
        content="public current body",
        title="public current title",
    )
    store.store_knowledge_object(knowledge)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_object_versions SET snapshot_json=? WHERE knowledge_object_id=?",
            (
                pack_snapshot(
                    json.dumps(
                        {
                            "id": knowledge.id,
                            "user_id": shared,
                            "raw_object_id": raw.id,
                            "entity_id": None,
                            "content": "historical public-looking body",
                            "metadata_json": json.dumps(
                                {"historical_copy": reminder.name},
                                ensure_ascii=True,
                            ),
                        },
                        ensure_ascii=False,
                    )
                ),
                knowledge.id,
            ),
        )
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, ?, 'day', 'reminder:bob', ?)""",
            (reminder.id, shared, "2026-08-13T09:00:00Z", "2026-08-06T00:00:00Z"),
        )
    store.close(final=True)

    reopened = FridayStorage(settings)
    try:
        entity_row = reopened.execute("SELECT user_id FROM entities WHERE id=?", (reminder.id,)).fetchone()
        assert entity_row is not None and entity_row["user_id"] == shared
        marker = reopened.execute(
            "SELECT person_id FROM private_entity_owners WHERE entity_id=?", (reminder.id,)
        ).fetchone()
        assert marker is not None and marker["person_id"] == "bob"
        encoded = json.dumps(_read_export(reopened, shared), ensure_ascii=False, sort_keys=True)
        assert reminder.id not in encoded
        assert reminder.name not in encoded
        assert knowledge.id not in encoded
    finally:
        reopened.close(final=True)


def test_startup_decodes_merge_reminder_provenance_independent_of_json_spacing(settings):
    shared = LEGACY_OWNER_USER_ID
    store = FridayStorage(settings)
    store.ensure_user(shared, preset_key="owner")
    store.ensure_user("bob")
    source = _entity(store, shared, "PRIVATE MERGED SOURCE 3c72")
    target = _entity(store, shared, "Public-looking target", EntityType.PROJECT)
    target_snapshot = dict(store.get_entity(target.id, shared) or {})
    source_snapshot = dict(store.get_entity(source.id, shared) or {})
    target_snapshot["aliases_json"] = json.dumps([source.name], ensure_ascii=False)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE entities SET aliases_json=? WHERE id=?",
            (target_snapshot["aliases_json"], target.id),
        )
        conn.execute(
            """INSERT INTO entity_merge_history(
                   id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json,
                   transfer_json, merged_by, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("merge"),
                shared,
                source.id,
                target.id,
                json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps(target_snapshot, ensure_ascii=False),
                json.dumps(target_snapshot, ensure_ascii=False),
                '{"time_moved":[{"source" : "reminder:bob"}]}',
                shared,
                "2026-08-06T00:00:00Z",
            ),
        )
    store.close(final=True)

    reopened = FridayStorage(settings)
    try:
        markers = reopened.execute(
            """SELECT entity_id, person_id FROM private_entity_owners
                 WHERE entity_id IN (?, ?)""",
            (source.id, target.id),
        ).fetchall()
        assert {row["entity_id"] for row in markers} == {source.id, target.id}
        assert {row["person_id"] for row in markers} == {"bob"}
        encoded = json.dumps(_read_export(reopened, shared), ensure_ascii=False, sort_keys=True)
        assert source.id not in encoded
        assert target.id not in encoded
        assert source.name not in encoded
    finally:
        reopened.close(final=True)


def test_purgeable_list_never_materializes_a_quarantined_knowledge_title(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    private = _entity(storage, shared, "PRIVATE PURGEABLE ENTITY 4f51")
    sentinel = "PRIVATE-PURGEABLE-TITLE-f85b"
    raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), sentinel, "text")
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(new_id("ko"), shared, raw.id, content=sentinel, title=sentinel)
    storage.store_knowledge_object(knowledge)
    storage.link_knowledge_entity(shared, knowledge.id, private.id)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET deleted_at='2020-01-01T00:00:00Z' WHERE id=?",
            (knowledge.id,),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00Z"),
        )

    assert storage.list_purgeable_knowledge(shared, older_than_days=0) == []
    assert storage.list_purgeable_knowledge(None, older_than_days=0) == []


def test_tenant_export_does_not_include_another_persons_monitor_query_or_chat(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    private_query = "PRIVATE-BOB-MONITOR-QUERY-7d32"
    private_chat = "PRIVATE-BOB-MONITOR-CHAT-51af"
    private_monitor = storage.create_monitor(
        shared,
        private_query,
        chat_id=private_chat,
        created_by="bob",
    )
    owner_monitor = storage.create_monitor(
        shared,
        "owner-safe-monitor",
        chat_id="owner-safe-chat",
        created_by=shared,
    )

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert private_monitor["id"] not in encoded
    assert private_query not in encoded
    assert private_chat not in encoded
    assert {row["id"] for row in payload["monitors"]} == {owner_monitor["id"]}


def test_export_requires_exact_tenant_identity_in_every_merge_snapshot(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    source = _entity(storage, shared, "Public merge identity source", EntityType.PROJECT)
    target = _entity(storage, shared, "Public merge identity target", EntityType.PROJECT)
    source_snapshot = dict(storage.get_entity(source.id, shared) or {})
    target_snapshot = dict(storage.get_entity(target.id, shared) or {})
    source_snapshot["user_id"] = "bob"
    merge_id = new_id("merge")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_merge_history(
                   id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json,
                   transfer_json, merged_by, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
            (
                merge_id,
                shared,
                source.id,
                target.id,
                json.dumps(source_snapshot, ensure_ascii=False),
                json.dumps(target_snapshot, ensure_ascii=False),
                json.dumps(target_snapshot, ensure_ascii=False),
                shared,
                "2026-08-06T00:00:00Z",
            ),
        )

    payload = _read_export(storage, shared)
    assert {source.id, target.id} <= {row["id"] for row in payload["entities"]}
    assert merge_id not in {row["id"] for row in payload["entity_merge_history"]}


def test_export_reaches_a_fixed_point_after_a_hidden_knowledge_id_is_discovered(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    private = _entity(storage, shared, "PRIVATE-FIXED-POINT-ENTITY-38ac")

    private_raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "private body", "text")
    storage.store_raw_object(private_raw)
    private_knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        private_raw.id,
        entity_id=private.id,
        content="private body",
        title="private title",
    )
    storage.store_knowledge_object(private_knowledge)

    copied_raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), "public body", "text")
    storage.store_raw_object(copied_raw)
    copied_inbox_id = new_id("inbox")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO inbox(
                   id, user_id, raw_object_id, status, suggestions_json,
                   suggested_tags_json, classification_notes, created_at)
               VALUES(?, ?, ?, 'pending', ?, '[]', 'public notes', ?)""",
            (
                copied_inbox_id,
                shared,
                copied_raw.id,
                json.dumps({"opaque_copy": private_knowledge.id}),
                "2026-08-06T00:00:00Z",
            ),
        )
    _mark_private_reminder(storage, private.id, shared, "bob")

    payload = _read_export(storage, shared)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert private_knowledge.id not in encoded
    assert copied_inbox_id not in encoded
    assert copied_raw.id not in encoded


def test_export_matches_private_names_case_insensitively_and_unicode_normalized(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    private_name = "Секрётный Ёж"
    private = _entity(storage, shared, private_name)
    copied_name = unicodedata.normalize("NFD", private_name.casefold())
    copied_raw = RawObject(new_id("raw"), shared, "test", new_id("ref"), copied_name, "text")
    storage.store_raw_object(copied_raw)
    copied_knowledge = KnowledgeObject(
        new_id("ko"),
        shared,
        copied_raw.id,
        content=copied_name,
        title="apparently public",
    )
    storage.store_knowledge_object(copied_knowledge)
    _mark_private_reminder(storage, private.id, shared, "bob")

    encoded = json.dumps(_read_export(storage, shared), ensure_ascii=False, sort_keys=True)
    assert private.id not in encoded
    assert copied_raw.id not in encoded
    assert copied_knowledge.id not in encoded
    assert copied_name not in encoded
