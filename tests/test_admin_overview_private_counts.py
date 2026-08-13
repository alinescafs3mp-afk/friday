"""Administrative aggregates cannot reveal quarantined reminder dependencies."""

from __future__ import annotations

import json
import unicodedata

import pytest

from friday.storage.models import (
    Entity,
    EntityType,
    FeedbackItem,
    FeedbackType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationType,
    new_id,
)


@pytest.mark.asyncio
async def test_overview_and_database_diagnostics_count_only_public_graph_rows(settings, storage, monkeypatch):
    from friday.admin_api._overview import overview
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import ActorContext, AuthorizationService

    storage.ensure_user("alice", preset_key="owner")
    public = Entity(new_id("ent"), "alice", "Public entity", EntityType.PROJECT)
    private = Entity(new_id("ent"), "alice", "PRIVATE-ADMIN-COUNT-8d4a", EntityType.EVENT)
    copied_private_name = unicodedata.normalize("NFD", private.name.casefold())
    carrier = Entity(
        new_id("ent"),
        "alice",
        "Otherwise public carrier",
        EntityType.PROJECT,
        description=f"Historical copy: {copied_private_name}",
    )
    copied_carrier_name = unicodedata.normalize("NFD", carrier.name.casefold())
    storage.create_entity(public)
    storage.create_entity(private)
    storage.create_entity(carrier)
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), "private source", "text")
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        entity_id=private.id,
        content="private source",
        title="private source",
    )
    storage.store_knowledge_object(knowledge)
    public_raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        "public source",
        "text",
    )
    storage.store_raw_object(public_raw)
    versioned_public = KnowledgeObject(
        new_id("ko"),
        "alice",
        public_raw.id,
        content=f"Historical transitive copy: {copied_carrier_name}",
        title="Historical copy",
    )
    storage.store_knowledge_object(versioned_public)
    versioned_public.content = "Current public body"
    versioned_public.title = "Current public title"
    storage.update_knowledge_object(versioned_public)
    inbox_id = new_id("inbox")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO inbox(
                   id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json,
                   classification_notes, created_at)
               VALUES(?, 'alice', ?, ?, 'pending', ?, '[]', '{}', '', ?)""",
            (inbox_id, raw.id, knowledge.id, private.id, "2026-08-06T00:00:00Z"),
        )
    storage.create_relation(
        Relation(
            new_id("rel"),
            "alice",
            private.id,
            public.id,
            RelationType.RELATED_TO,
        )
    )
    storage.create_relation(
        Relation(
            new_id("rel"),
            "alice",
            carrier.id,
            public.id,
            RelationType.RELATED_TO,
        )
    )
    storage.store_relation_candidate(
        "alice",
        private.id,
        public.id,
        RelationType.RELATED_TO.value,
        confidence=0.8,
        evidence={"knowledge_object_id": knowledge.id},
    )
    storage.store_relation_candidate(
        "alice",
        carrier.id,
        public.id,
        RelationType.RELATED_TO.value,
        confidence=0.8,
        evidence={},
    )
    assert storage.enqueue_notification(
        "alice",
        "5001",
        f"Chronicle copied {copied_private_name}",
        kind="chronicle",
        dedup_key="chronicle:private-admin-count",
    )
    storage.store_feedback(
        FeedbackItem(
            id=new_id("feedback"),
            user_id="alice",
            target_type="classification",
            target_id=raw.id,
            feedback_type=FeedbackType.CLASSIFICATION,
            score=-1.0,
        )
    )
    # Напоминание принадлежит ДРУГОМУ человеку в этом же арендаторе — то есть
    # ровно тому случаю, ради которого §30 и писался: общий архив, где чужое
    # личное не должно показываться. Собственное напоминание владельца арендатора
    # с 0.198.0 своих же носителей не прячет (§76), и с `person_id='alice'` эта
    # проба мерила бы уже не приватность, а послабление.
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, 'alice', '2026-08-07T09:00:00Z', 'day', 'reminder:bob', ?)""",
            (private.id, "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-06T00:00:00Z"),
        )

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")

    class _Request:
        def __init__(self) -> None:
            self.app = type(
                "App",
                (),
                {
                    "state": type(
                        "S",
                        (),
                        {
                            "storage": storage,
                            "auth_service": auth,
                            "kg": graph,
                            "settings": settings,
                        },
                    )()
                },
            )()
            self.state = type("RS", (), {"actor": actor})()

    # Full diagnostics remains an explicit operator action.  The landing page is
    # forbidden from invoking it: on a real corpus it checks the whole database
    # and decodes every historical knowledge snapshot.
    database = storage.diagnostics()

    def refuse_full_diagnostics():
        raise AssertionError("the overview ran full database diagnostics")

    monkeypatch.setattr(storage, "diagnostics", refuse_full_diagnostics)
    result = await overview(_Request())
    expected_zero = {"inbox", "relations", "relation_candidates", "feedback", "feedback_state"}
    assert result["counts"]["entities"] == 1
    assert result["counts"]["raw_objects"] == 1
    assert result["counts"]["knowledge_objects"] == 1
    assert result["pending_inbox"] == 0
    assert {key: result["counts"][key] for key in expected_zero} == {key: 0 for key in expected_zero}
    assert result["database"]["integrity_check"] == "not_run"
    assert result["database"]["ok"] is None
    assert "counts" not in result["database"]
    assert database["counts"]["entities"] == 1
    assert database["counts"]["raw_objects"] == 1
    assert database["counts"]["knowledge_objects"] == 1
    assert {key: database["counts"][key] for key in expected_zero} == {key: 0 for key in expected_zero}
    assert database["counts"]["knowledge_usage"] == 0
    assert database["inbox_pending"] == 0
    assert len(storage.list_knowledge_versions(versioned_public.id, "alice")) == 1
    assert database["versions_rows"] == 1
    assert database["outbound_pending"] == 0
    assert private.id not in json.dumps(result, ensure_ascii=False)
