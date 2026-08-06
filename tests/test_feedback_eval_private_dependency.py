"""Feedback and eval projections must follow reminder privacy retroactively."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import replace

import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import (
    Entity,
    EntityType,
    FeedbackItem,
    FeedbackType,
    KnowledgeObject,
    RawObject,
    new_id,
)


def _knowledge(storage, user_id: str, *, entity_id: str | None, title: str) -> KnowledgeObject:
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
    return knowledge


def _mark_private(storage, entity_id: str, user_id: str) -> None:
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-07T09:00:00Z', 'day', 'reminder:bob',
                      '2026-08-06T00:00:00Z')""",
            (entity_id, user_id),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', '2026-08-06T00:00:00Z')""",
            (entity_id,),
        )


def test_feedback_and_eval_rows_disappear_when_their_knowledge_becomes_private(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    entity = Entity(new_id("ent"), shared, "RETROACTIVE-PRIVATE-FEEDBACK-42ac", EntityType.EVENT)
    storage.create_entity(entity)
    private_knowledge = _knowledge(storage, shared, entity_id=entity.id, title="private target")
    public_knowledge = _knowledge(storage, shared, entity_id=None, title="public target")

    private_case = storage.add_eval_case(
        shared,
        "private eval query",
        [private_knowledge.id],
        note="private eval note",
    )
    public_case = storage.add_eval_case(shared, "public eval query", [public_knowledge.id])
    private_feedback = FeedbackItem(
        id=new_id("feedback"),
        user_id=shared,
        target_type="knowledge_object",
        target_id=private_knowledge.id,
        feedback_type=FeedbackType.ANSWER_USEFULNESS,
        score=-1.0,
        comment="private feedback comment",
        context_json={"knowledge_object_ids": [private_knowledge.id]},
    )
    storage.store_feedback(private_feedback)
    public_feedback = FeedbackItem(
        id=new_id("feedback"),
        user_id=shared,
        target_type="knowledge_object",
        target_id=public_knowledge.id,
        feedback_type=FeedbackType.ANSWER_USEFULNESS,
        score=1.0,
        comment="public feedback comment",
        context_json={"knowledge_object_ids": [public_knowledge.id]},
    )
    storage.store_feedback(public_feedback)

    _mark_private(storage, entity.id, shared)

    assert {row["id"] for row in storage.list_eval_cases(shared)} == {public_case["id"]}
    assert storage.delete_eval_case(shared, private_case["id"]) is False
    assert storage.upsert_feedback_eval_case(shared, "private eval query", [private_knowledge.id]) is False
    with pytest.raises(ValueError, match="private knowledge"):
        storage.add_eval_case(shared, "private eval query", [private_knowledge.id])

    assert (
        storage.get_feedback_for_target(
            shared,
            "knowledge_object",
            private_knowledge.id,
        )
        == []
    )
    assert (
        storage.get_feedback_state(
            shared,
            target_type="knowledge_object",
            target_id=private_knowledge.id,
        )
        == []
    )
    assert storage.count_feedback_state(shared) == 1
    assert storage.get_feedback_stats(shared) == {
        FeedbackType.ANSWER_USEFULNESS.value: {"avg_score": 1.0, "count": 1}
    }
    assert storage.get_current_feedback_stats(shared) == {
        FeedbackType.ANSWER_USEFULNESS.value: {
            "avg_score": 1.0,
            "count": 1,
            "positive": 1,
            "negative": 0,
        }
    }
    with pytest.raises(ValueError, match="private knowledge"):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=shared,
                target_type="knowledge_object",
                target_id=private_knowledge.id,
                score=1.0,
            )
        )


def test_feedback_and_eval_reject_nested_escaped_or_uninspectable_private_material(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    entity = Entity(new_id("ent"), shared, "ESCAPED-PRIVATE-NAME-7c11", EntityType.EVENT)
    storage.create_entity(entity)
    private_knowledge = _knowledge(storage, shared, entity_id=entity.id, title="private")
    _mark_private(storage, entity.id, shared)
    conversation = storage.create_conversation(shared, "nested feedback")
    message = storage.store_message(conversation["id"], shared, "assistant", "public answer")

    nested = json.dumps(
        {"copy": json.dumps({"knowledge_object_id": private_knowledge.id})},
        ensure_ascii=True,
    )
    with pytest.raises(ValueError, match="private knowledge"):
        storage.add_eval_case(shared, nested, ["ko_missing"])
    with pytest.raises(ValueError, match="private knowledge"):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=shared,
                target_type="answer",
                target_id=str(message["id"]),
                comment=nested,
                context_json={},
            )
        )

    malformed_eval_id = new_id("eval")
    malformed_feedback_id = new_id("feedback")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO eval_cases(
                   id, user_id, query, expected_ids_json, note, source, created_at)
               VALUES(?, ?, 'legacy malformed', '[', '', 'manual', ?)""",
            (malformed_eval_id, shared, "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO feedback(
                   id, user_id, target_type, target_id, feedback_type,
                   score, comment, context_json, created_at)
               VALUES(?, ?, 'answer', 'legacy-answer', 'general', 1, '', '{', ?)""",
            (malformed_feedback_id, shared, "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO feedback_state(
                   user_id, target_type, target_id, feedback_type, score,
                   comment, context_json, feedback_id, updated_at)
               VALUES(?, 'answer', 'legacy-answer', 'general', 1, '', '{', ?, ?)""",
            (shared, malformed_feedback_id, "2026-08-06T00:00:00Z"),
        )

    assert storage.list_eval_cases(shared) == []
    assert storage.get_feedback_for_target(shared, "answer", "legacy-answer") == []
    assert storage.get_feedback_state(shared) == []
    assert storage.count_feedback_state(shared) == 0
    assert storage.get_feedback_stats(shared) == {}
    assert storage.get_current_feedback_stats(shared) == {}


def test_classification_feedback_follows_its_raw_dependency_and_missing_matches_hidden(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    entity = Entity(new_id("ent"), shared, "PRIVATE-RAW-FEEDBACK-5a81", EntityType.EVENT)
    storage.create_entity(entity)
    knowledge = _knowledge(storage, shared, entity_id=entity.id, title="raw feedback dependency")
    raw_id = knowledge.raw_object_id
    stored = FeedbackItem(
        id=new_id("feedback"),
        user_id=shared,
        target_type="classification",
        target_id=raw_id,
        feedback_type=FeedbackType.CLASSIFICATION,
        score=-1.0,
    )
    storage.store_feedback(stored)
    _mark_private(storage, entity.id, shared)

    assert storage.get_feedback_for_target(shared, "classification", raw_id) == []
    assert storage.get_feedback_state(shared) == []
    assert storage.count_feedback_state(shared) == 0
    assert storage.get_feedback_stats(shared) == {}
    with pytest.raises(ValueError, match="private knowledge"):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=shared,
                target_type="classification",
                target_id=raw_id,
                feedback_type=FeedbackType.CLASSIFICATION,
                score=1.0,
            )
        )
    with pytest.raises(ValueError, match="private knowledge"):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=shared,
                target_type="classification",
                target_id="raw-does-not-exist",
                feedback_type=FeedbackType.CLASSIFICATION,
                score=1.0,
            )
        )


def test_feedback_private_token_matching_never_crosses_a_tenant(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    bob_entity = Entity(new_id("ent"), "bob", "BOB-PRIVATE-NAME-911e", EntityType.EVENT)
    storage.create_entity(bob_entity)
    _mark_private(storage, bob_entity.id, "bob")
    conversation = storage.create_conversation("alice", "cross tenant feedback")
    message = storage.store_message(conversation["id"], "alice", "assistant", "public answer")
    alice_feedback = FeedbackItem(
        id=new_id("feedback"),
        user_id="alice",
        target_type="answer",
        target_id=str(message["id"]),
        feedback_type=FeedbackType.GENERAL,
        score=1.0,
        comment=bob_entity.name,
    )

    storage.store_feedback(alice_feedback)

    assert storage.count_feedback_state("alice") == 1
    assert (
        storage.get_feedback_for_target("alice", "answer", str(message["id"]))[0]["comment"]
        == bob_entity.name
    )


def test_feedback_and_eval_reject_current_and_historical_private_aliases(storage):
    storage.ensure_user("alice")
    old_alias = "PRIVATE-HISTORICAL-ALIAS-5d91"
    current_alias = "PRIVATE-CURRENT-ALIAS-7a20"
    private = Entity(
        new_id("ent"),
        "alice",
        "Private alias authority",
        EntityType.EVENT,
        aliases_json=[old_alias],
    )
    storage.create_entity(private)
    private.aliases_json = [current_alias]
    storage.update_entity(private)
    public_raw = RawObject(
        new_id("raw"),
        "alice",
        "test",
        new_id("ref"),
        "public feedback target",
        "text",
    )
    storage.store_raw_object(public_raw)
    _mark_private(storage, private.id, "alice")
    copied_aliases = (
        unicodedata.normalize("NFD", old_alias.casefold()),
        unicodedata.normalize("NFD", current_alias.casefold()),
    )

    for copied_alias in copied_aliases:
        with pytest.raises(ValueError, match="private knowledge"):
            storage.store_feedback(
                FeedbackItem(
                    new_id("feedback"),
                    "alice",
                    "classification",
                    public_raw.id,
                    FeedbackType.CLASSIFICATION,
                    1.0,
                    comment=f"Copied alias: {copied_alias}",
                )
            )
        with pytest.raises(ValueError, match="private knowledge"):
            storage.add_eval_case(
                "alice",
                f"Copied alias: {copied_alias}",
                ["ko_missing"],
            )
    rows = storage.execute(
        "SELECT COUNT(*) AS count FROM feedback WHERE target_id=?",
        (public_raw.id,),
    ).fetchone()
    assert int(rows["count"] if rows else -1) == 0


def test_private_feedback_token_lookup_scales_with_hidden_identities(storage):
    """Feedback scans the sparse quarantine, never every public entity state."""

    from friday.storage._core import _private_identity_tokens_json
    from friday.storage._feedback import _private_feedback_tokens

    storage.ensure_user("alice")
    historical_alias = unicodedata.normalize("NFD", "ИСТОРИЯ ЁЛКА FEEDBACK 71C9")
    current_alias = unicodedata.normalize("NFD", "ТЕКУЩАЯ ЁЛКА FEEDBACK 82D0")
    private = Entity(
        new_id("ent"),
        "alice",
        "Private feedback identity",
        EntityType.EVENT,
        aliases_json=[historical_alias],
    )
    storage.create_entity(private)
    private.aliases_json = [current_alias]
    storage.update_entity(private)
    _mark_private(storage, private.id, "alice")

    calls = 0

    def counted_identity_tokens(name: object, aliases: object) -> str:
        nonlocal calls
        calls += 1
        return _private_identity_tokens_json(name, aliases)

    storage.conn.create_function(
        "jericho_private_identity_tokens",
        2,
        counted_identity_tokens,
        deterministic=True,
    )
    exact, names = _private_feedback_tokens(storage.conn, "alice")
    sparse_calls = calls
    assert private.id in exact
    assert unicodedata.normalize("NFC", historical_alias).casefold() in names
    assert unicodedata.normalize("NFC", current_alias).casefold() in names
    assert sparse_calls > 0

    for number in range(24):
        storage.create_entity(
            Entity(
                new_id("ent"),
                "alice",
                f"Independent feedback decoy {number}",
                EntityType.CONCEPT,
            )
        )
    calls = 0
    assert _private_feedback_tokens(storage.conn, "alice") == (exact, names)
    assert calls == sparse_calls


def test_eval_source_and_feedback_type_fields_cannot_carry_private_material(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.ensure_user(shared, preset_key="owner")
    storage.ensure_user("bob")
    entity = Entity(new_id("ent"), shared, "PRIVATE-FIELD-COPY-e10b", EntityType.EVENT)
    storage.create_entity(entity)
    _mark_private(storage, entity.id, shared)

    with pytest.raises(ValueError, match="source"):
        storage.add_eval_case(shared, "public query", ["ko_missing"], source=entity.name)
    with pytest.raises(ValueError, match="private knowledge"):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=shared,
                target_type=entity.name,
                target_id="public-target",
                feedback_type=FeedbackType.GENERAL,
                score=1.0,
            )
        )

    eval_id = new_id("eval")
    feedback_id = new_id("feedback")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO eval_cases(
                   id, user_id, query, expected_ids_json, note, source, created_at)
               VALUES(?, ?, 'public query', '["ko_missing"]', '', ?, ?)""",
            (eval_id, shared, entity.name, "2026-08-06T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO feedback(
                   id, user_id, target_type, target_id, feedback_type,
                   score, comment, context_json, created_at)
               VALUES(?, ?, 'answer', 'public-answer', ?, 1, '', '{}', ?)""",
            (feedback_id, shared, entity.name, "2026-08-06T00:00:00Z"),
        )
    assert storage.list_eval_cases(shared) == []
    assert storage.get_feedback_for_target(shared, "answer", "public-answer") == []
    assert storage.get_feedback_stats(shared) == {}


def test_shared_feedback_maps_personal_messages_to_archive_knowledge_without_cross_person_tokens(storage):
    shared = LEGACY_OWNER_USER_ID
    storage.settings = replace(storage.settings, shared_archive=True)
    for user_id in (shared, "alice", "bob"):
        storage.ensure_user(user_id, preset_key="owner")
    public_knowledge = _knowledge(storage, shared, entity_id=None, title="shared public knowledge")
    alice_conversation = storage.create_conversation("alice", "personal chat")
    alice_message = storage.store_message(
        alice_conversation["id"],
        "alice",
        "assistant",
        "shared answer",
        metadata={"knowledge_object_ids": [public_knowledge.id]},
    )

    alice_private = Entity(new_id("ent"), "alice", "ALICE-PRIVATE-FEEDBACK-a11c", EntityType.EVENT)
    storage.create_entity(alice_private)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, 'alice', '2026-08-07T09:00:00Z', 'day', 'reminder:alice',
                      '2026-08-06T00:00:00Z')""",
            (alice_private.id,),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'alice', 'reminder', '2026-08-06T00:00:00Z')""",
            (alice_private.id,),
        )
    with pytest.raises(ValueError, match="private knowledge"):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id="alice",
                target_type="answer",
                target_id=str(alice_message["id"]),
                feedback_type=FeedbackType.ANSWER_USEFULNESS,
                score=1.0,
                comment=alice_private.name,
            )
        )

    bob_legacy = Entity(new_id("ent"), shared, "BOB-PRIVATE-COLLISION-b20d", EntityType.EVENT)
    storage.create_entity(bob_legacy)
    _mark_private(storage, bob_legacy.id, shared)
    accepted = FeedbackItem(
        id=new_id("feedback"),
        user_id="alice",
        target_type="answer",
        target_id=str(alice_message["id"]),
        feedback_type=FeedbackType.ANSWER_USEFULNESS,
        score=1.0,
        comment=bob_legacy.name,
        context_json={"knowledge_object_ids": [public_knowledge.id]},
    )
    storage.store_feedback(accepted)
    assert storage.count_feedback_state("alice") == 1
    usage = storage.get_knowledge_usage(shared, [public_knowledge.id])[public_knowledge.id]
    assert usage["positive_feedback_count"] == 1

    bob_private = Entity(new_id("ent"), "bob", "BOB-PRIVATE-EVAL-e31f", EntityType.EVENT)
    storage.create_entity(bob_private)
    _mark_private(storage, bob_private.id, "bob")
    with pytest.raises(ValueError, match="private knowledge"):
        storage.add_eval_case(shared, bob_private.name, [public_knowledge.id])


def test_answer_feedback_requires_an_owned_assistant_message(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    conversation = storage.create_conversation("bob", "Bob chat")
    bob_message = storage.store_message(conversation["id"], "bob", "assistant", "Bob answer")

    for target_id in (str(bob_message["id"]), "message-does-not-exist"):
        with pytest.raises(ValueError, match="private knowledge"):
            storage.store_feedback(
                FeedbackItem(
                    id=new_id("feedback"),
                    user_id="alice",
                    target_type="answer",
                    target_id=target_id,
                    feedback_type=FeedbackType.GENERAL,
                    score=1.0,
                )
            )
