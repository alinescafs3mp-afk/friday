"""Graph/runtime privacy boundaries stay bounded before logs or Python payloads."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService
from friday.server import create_app
from friday.storage._base import pack_snapshot
from friday.storage._graph import _bounded_entity_by_id
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

_PROJECT = Path(__file__).resolve().parents[1]
_LOG_PRIVACY_SOURCES = (
    _PROJECT / "friday" / "agent_runtime" / "__init__.py",
    _PROJECT / "friday" / "execution_kernel" / "__init__.py",
    _PROJECT / "friday" / "storage" / "_graph.py",
)
_SENSITIVE_LOG_NAMES = frozenset(
    {
        "display_name",
        "exc",
        "filename",
        "message",
        "moment",
        "proposed",
        "query",
        "reason",
        "rest",
        "url",
        "user_id",
        "what",
        "when",
    }
)


def _is_logger_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "LOGGER"
    )


def _contains_raw_sensitive_value(node: ast.AST) -> bool:
    """Counts/booleans/classes are safe; raw content-bearing values are not."""

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"bool", "len", "type"}
    ):
        return False
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    ):
        return False
    if isinstance(node, ast.Name) and node.id in _SENSITIVE_LOG_NAMES:
        return True
    if isinstance(node, ast.Attribute) and node.attr in _SENSITIVE_LOG_NAMES:
        return True
    return any(_contains_raw_sensitive_value(child) for child in ast.iter_child_nodes(node))


def test_content_processing_loggers_cannot_emit_tracebacks_or_raw_private_values() -> None:
    offenders: list[str] = []
    for path in _LOG_PRIVACY_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_logger_call(node):
                continue
            assert isinstance(node.func, ast.Attribute)
            if node.func.attr == "exception":
                offenders.append(f"{path.name}:{node.lineno}: LOGGER.exception")
            for keyword in node.keywords:
                if keyword.arg == "exc_info" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value in {False, None}
                ):
                    offenders.append(f"{path.name}:{node.lineno}: exc_info")
            for argument in node.args[1:]:
                if _contains_raw_sensitive_value(argument):
                    offenders.append(f"{path.name}:{node.lineno}: raw private log argument")
    assert offenders == []


def _kernel(settings, storage, *, ingestion: Any = object()) -> ExecutionKernel:
    storage.ensure_user("alice", preset_key="owner")
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, KnowledgeGraph(storage), object(), ingestion)  # type: ignore[arg-type]
    return kernel


@pytest.mark.asyncio
async def test_agent_runtime_logs_only_the_presence_of_a_private_remainder(
    settings,
    storage,
    caplog,
    monkeypatch,
) -> None:
    sentinel = "SYNTHETIC_PRIVATE_REMAINDER_SENTINEL_" + "r" * 20_000
    runtime = AgentRuntime(settings, storage)

    async def private_remainder(*_args: Any, **_kwargs: Any) -> str:
        return sentinel

    monkeypatch.setattr(runtime, "_remainder_after", private_remainder)
    monkeypatch.setattr(runtime, "_served_model_name", lambda: "synthetic-local-model")
    context = AgentContext(conversation_id="conv-log-privacy", user_id="alice")
    with caplog.at_level(logging.INFO, logger="friday.agent_runtime"):
        await runtime._say_what_i_am_if_asked("Какая ты модель и что ещё?", context)  # noqa: SLF001

    assert context.open_remainder == sentinel
    assert caplog.records
    assert all(sentinel not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_tool_failure_message_log_and_audit_drop_the_exception_payload(
    settings,
    storage,
    caplog,
) -> None:
    sentinel = "SYNTHETIC_PRIVATE_TOOL_EXCEPTION_SENTINEL_" + "x" * 20_000
    kernel = _kernel(settings, storage)

    async def fail_after_start(**_kwargs: Any) -> None:
        raise ValueError(sentinel)

    kernel._tools["memory_search"].handler = fail_after_start  # noqa: SLF001
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    with caplog.at_level(logging.WARNING, logger="friday.execution_kernel"):
        result = await kernel.execute(
            "memory_search",
            {"query": sentinel},
            actor=actor,
            execution_scope="internal",
        )

    assert result.success is False
    assert sentinel not in result.to_llm_message()
    assert sentinel not in json.dumps(storage.list_audit_log(limit=20), ensure_ascii=False)
    assert caplog.records
    assert all(sentinel not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_web_capture_failure_logs_neither_url_nor_exception(
    settings,
    storage,
    caplog,
) -> None:
    sentinel = "SYNTHETIC_PRIVATE_WEB_CAPTURE_SENTINEL_" + "w" * 5_000

    class FailingIngestion:
        async def ingest_text(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(sentinel)

    kernel = _kernel(settings, storage, ingestion=FailingIngestion())
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    # Stay inside the public-source URL bound so the test reaches the intended
    # ingestion-exception logger. Oversized URLs are rejected before ingestion.
    url_secret = sentinel[:96]
    url = f"https://synthetic.example.com/private/{url_secret}?token={url_secret}"
    with caplog.at_level(logging.WARNING, logger="friday.execution_kernel"):
        captured = await kernel._capture_web_sources(  # noqa: SLF001
            actor,
            sentinel,
            {
                "sources": [
                    {
                        "url": url,
                        "title": sentinel,
                        "text": "x" * 500,
                        "text_length": 500,
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "requested_sources": 1,
                "completed_sources": 1,
                "timed_out_sources": 0,
                "failed_sources": 0,
                "search_timed_out": False,
            },
        )

    assert captured == []
    assert caplog.records
    assert all(sentinel not in record.getMessage() for record in caplog.records)
    assert all(url_secret not in record.getMessage() for record in caplog.records)
    assert all("synthetic.example.com" not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_approved_failure_persists_only_the_exception_class(
    settings,
    storage,
    caplog,
) -> None:
    from tests.test_dangerous_tools_need_a_person import _candidate

    sentinel = "SYNTHETIC_PRIVATE_APPROVAL_FAILURE_SENTINEL_" + "a" * 20_000
    candidate_id = _candidate(storage, "alice")
    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    requested = await kernel.execute(
        "entity_merge_decide",
        {"candidate_id": candidate_id, "decision": "accept"},
        actor=actor,
    )
    approval_id = str(requested.data["approval_id"])
    storage.decide_action_approval(
        approval_id,
        "alice",
        decision="approve",
        decided_by="alice",
    )

    async def fail_after_approval(**_kwargs: Any) -> None:
        raise RuntimeError(sentinel)

    kernel._tools["entity_merge_decide"].handler = fail_after_approval  # noqa: SLF001
    with caplog.at_level(logging.WARNING, logger="friday.execution_kernel"):
        result = await kernel.execute_approved(approval_id, actor=actor)

    assert result.success is False
    assert sentinel not in result.to_llm_message()
    approval = storage.get_action_approval(approval_id, "alice")
    assert approval["error"] == "RuntimeError"
    assert sentinel not in json.dumps(approval, ensure_ascii=False)
    assert all(sentinel not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_graph_traversal_drops_large_entity_and_relation_blobs_before_publication(storage) -> None:
    sentinel = "SYNTHETIC_PRIVATE_GRAPH_BLOB_SENTINEL_" + "g" * 100_000
    storage.ensure_user("alice")
    root = Entity(
        id=new_id("ent"),
        user_id="alice",
        name="Корень",
        entity_type=EntityType.PROJECT,
        description=sentinel,
        metadata_json={"private": sentinel},
    )
    target = Entity(
        id=new_id("ent"),
        user_id="alice",
        name="Цель",
        entity_type=EntityType.OTHER,
        metadata_json={"private": sentinel},
    )
    storage.create_entity(root)
    storage.create_entity(target)
    storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id="alice",
            source_entity_id=root.id,
            target_entity_id=target.id,
            relation_type=RelationType.RELATED_TO,
            metadata_json={"origin": "manual", "private": sentinel},
        )
    )
    boundary_row = storage.execute(
        "SELECT recorded_at FROM relation_revisions WHERE user_id=? ORDER BY event_seq DESC LIMIT 1",
        ("alice",),
    ).fetchone()
    assert boundary_row is not None

    snapshots = (
        storage.get_entity_graph("alice", root.id, depth=1),
        storage.get_entity_graph(
            "alice",
            root.id,
            depth=1,
            known_at=str(boundary_row["recorded_at"]),
        ),
        KnowledgeGraph(storage).context_for_query("alice", "Корень", depth=1),
    )
    for snapshot in snapshots:
        encoded = json.dumps(snapshot, ensure_ascii=False)
        assert sentinel not in encoded
        assert len(encoded) < 100_000


def test_quarantining_relation_evidence_hides_fact_queue_history_and_review(storage) -> None:
    """A relation grounded in a now-private KO is itself a private derived copy."""

    user_id = "alice"
    sentinel = "PRIVATE RELATION EVIDENCE SENTINEL"
    storage.ensure_user(user_id)
    endpoints = [
        Entity(id=f"ent-public-relation-{index}", user_id=user_id, name=f"Public {index}")
        for index in range(3)
    ]
    hidden = Entity(
        id="ent-private-relation-source",
        user_id=user_id,
        name=sentinel,
        entity_type=EntityType.EVENT,
    )
    for entity in (*endpoints, hidden):
        storage.create_entity(entity)
    raw = RawObject(
        id="raw-private-relation-source",
        user_id=user_id,
        source="test",
        source_ref="relation-source",
        raw_content=sentinel,
        content_type="text",
        content_hash=hashlib.sha256(sentinel.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id="ko-private-relation-source",
        user_id=user_id,
        raw_object_id=raw.id,
        content=sentinel,
        content_type="text",
        title=sentinel,
        metadata_json={"document_date": "2024-03-15"},
    )
    storage.store_knowledge_object(knowledge)
    for entity in (*endpoints, hidden):
        storage.link_knowledge_entity(user_id, knowledge.id, entity.id, status="accepted")

    accepted = storage.store_relation_candidate(
        user_id,
        endpoints[0].id,
        endpoints[1].id,
        "related_to",
        confidence=0.8,
        evidence={"knowledge_object_id": knowledge.id, "excerpt": sentinel},
    )
    pending = storage.store_relation_candidate(
        user_id,
        endpoints[0].id,
        endpoints[2].id,
        "related_to",
        confidence=0.7,
        evidence={"knowledge_object_id": knowledge.id, "excerpt": sentinel},
    )
    storage.review_relation_candidate(
        user_id,
        str(accepted["id"]),
        "accepted",
        reviewed_by="alice",
    )
    revision = storage.execute(
        "SELECT recorded_at FROM relation_revisions ORDER BY event_seq DESC LIMIT 1"
    ).fetchone()
    assert revision is not None

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "bob", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_knowledge_object(knowledge.id, user_id) is None
    assert storage.count_relation_candidates(user_id, status="suggested") == 0
    assert storage.list_relation_candidates(user_id, status="suggested") == []
    assert storage.get_relation_candidate(user_id, str(pending["id"])) is None
    assert (
        storage.review_relation_candidate(
            user_id,
            str(pending["id"]),
            "accepted",
            reviewed_by="alice",
        )
        is None
    )
    assert storage.get_entity_relations(endpoints[0].id, user_id) == []
    assert storage.count_entity_relations(endpoints[0].id, user_id) == 0
    assert storage.list_relation_changes_in_range(user_id) == []
    assert storage.count_relation_changes_in_range(user_id) == 0
    assert (
        storage.get_entity_relations(
            endpoints[0].id,
            user_id,
            known_at=str(revision["recorded_at"]),
        )
        == []
    )
    payloads = (
        storage.get_entity_graph(user_id, endpoints[0].id, depth=1),
        storage.graph_overview(user_id),
        storage.graph_overview(user_id, known_at=str(revision["recorded_at"])),
    )
    encoded = json.dumps(payloads, ensure_ascii=False)
    assert knowledge.id not in encoded
    assert sentinel not in encoded

    # Removing the grounding row must not reclassify reviewed metadata as an
    # explicit/manual fact.  Review intent is durable in the relation signature;
    # a broken signature fails closed in current and historical reads.
    with storage.transaction() as conn:
        conn.execute(
            "DELETE FROM relation_candidates WHERE id=? AND user_id=?",
            (str(accepted["id"]), user_id),
        )
    assert storage.get_entity_relations(endpoints[0].id, user_id) == []
    assert (
        storage.get_entity_relations(
            endpoints[0].id,
            user_id,
            known_at=str(revision["recorded_at"]),
        )
        == []
    )


def test_unicode_equivalent_private_name_invalidates_relation_candidate(storage) -> None:
    """NFD/lowercase evidence is still a copy of an NFC private identity."""

    user_id = "alice"
    private_name = "СЕКРЁТНЫЙ ЁЖ"
    copied_name = unicodedata.normalize("NFD", private_name.casefold())
    storage.ensure_user(user_id)
    source = Entity(id="ent-unicode-source", user_id=user_id, name="Public source")
    target = Entity(id="ent-unicode-target", user_id=user_id, name="Public target")
    hidden = Entity(
        id="ent-unicode-private",
        user_id=user_id,
        name=private_name,
        entity_type=EntityType.EVENT,
    )
    for entity in (source, target, hidden):
        storage.create_entity(entity)
    candidate = storage.store_relation_candidate(
        user_id,
        source.id,
        target.id,
        "related_to",
        confidence=0.8,
        evidence={"excerpt": copied_name},
    )
    assert storage.get_relation_candidate(user_id, str(candidate["id"])) is not None

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "bob", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_relation_candidate(user_id, str(candidate["id"])) is None
    assert storage.list_relation_candidates(user_id, status="suggested") == []
    assert storage.count_relation_candidates(user_id, status="suggested") == 0
    assert (
        storage.review_relation_candidate(
            user_id,
            str(candidate["id"]),
            "accepted",
            reviewed_by=user_id,
        )
        is None
    )


def test_packed_knowledge_history_cannot_repeat_a_private_name(storage) -> None:
    """Decoded historical text is checked even when its structural ids are public."""

    user_id = "alice"
    sentinel = "СЕКРЁТНЫЙ ЁЖ VERSION SENTINEL"
    copied_name = unicodedata.normalize("NFD", sentinel.casefold())
    storage.ensure_user(user_id)
    raw = RawObject(
        id="raw-public-version-source",
        user_id=user_id,
        source="test",
        source_ref="version-source",
        raw_content="Public source",
        content_type="text",
        content_hash=hashlib.sha256(b"Public source").hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id="ko-public-current-private-history",
        user_id=user_id,
        raw_object_id=raw.id,
        content=f"Historical body copied {copied_name}",
        content_type="text",
        title="Historical public title",
        metadata_json={"wrapped": copied_name},
    )
    storage.store_knowledge_object(knowledge)
    storage.update_knowledge_fields(
        knowledge.id,
        user_id,
        content="Current public body",
        title="Current public title",
        metadata_json={},
    )
    hidden = Entity(
        id="ent-private-version-name",
        user_id=user_id,
        name=sentinel,
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(hidden)
    with storage.transaction() as conn:
        old = conn.execute(
            """SELECT id, snapshot_json FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND version=1""",
            (knowledge.id,),
        ).fetchone()
        assert old is not None
        conn.execute(
            "UPDATE knowledge_object_versions SET snapshot_json=? WHERE id=?",
            (pack_snapshot(str(old["snapshot_json"])), str(old["id"])),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "bob", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_knowledge_object(knowledge.id, user_id) is not None
    versions = storage.list_knowledge_versions(knowledge.id, user_id)
    encoded = json.dumps(versions, ensure_ascii=False)
    assert sentinel not in encoded
    assert copied_name not in encoded
    assert {int(item["version"]) for item in versions} == {2}


def test_private_knowledge_usage_is_neither_readable_mutable_nor_counted_by_admin(
    settings,
) -> None:
    """Usage is derived state and follows the KO's live privacy boundary."""

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        user_id = LEGACY_OWNER_USER_ID
        sentinel = "PRIVATE USAGE SENTINEL"
        hidden = Entity(
            id="ent-private-usage-source",
            user_id=user_id,
            name=sentinel,
            entity_type=EntityType.EVENT,
        )
        storage.create_entity(hidden)
        raw = RawObject(
            id="raw-private-usage-source",
            user_id=user_id,
            source="test",
            source_ref="private-usage-source",
            raw_content=sentinel,
            content_type="text",
            content_hash=hashlib.sha256(sentinel.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id="ko-private-usage-source",
            user_id=user_id,
            raw_object_id=raw.id,
            content=sentinel,
            content_type="text",
            title=sentinel,
        )
        storage.store_knowledge_object(knowledge)
        storage.link_knowledge_entity(user_id, knowledge.id, hidden.id, status="accepted")
        assert (
            storage.record_knowledge_usage(
                user_id,
                [knowledge.id],
                retrieved=True,
                used_in_answer=True,
            )
            == 1
        )
        assert storage.get_knowledge_usage(user_id, [knowledge.id])[knowledge.id]["answer_count"] == 1

        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, ?, 'reminder', ?)""",
                (hidden.id, "person-private", "2026-08-05T00:00:00Z"),
            )
        before = dict(
            storage.execute(
                "SELECT * FROM knowledge_usage WHERE user_id=? AND knowledge_object_id=?",
                (user_id, knowledge.id),
            ).fetchone()
        )
        assert (
            storage.record_knowledge_usage(
                user_id,
                [knowledge.id],
                retrieved=True,
                used_in_answer=True,
            )
            == 0
        )
        assert storage.get_knowledge_usage(user_id, [knowledge.id]) == {}
        after = dict(
            storage.execute(
                "SELECT * FROM knowledge_usage WHERE user_id=? AND knowledge_object_id=?",
                (user_id, knowledge.id),
            ).fetchone()
        )
        assert after == before

        response = client.get(
            "/api/admin/quality",
            params={"user_id": user_id},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["usage"] == {
            "tracked": 0,
            "retrievals": 0,
            "answers": 0,
            "positive": 0,
            "negative": 0,
        }
        assert knowledge.id not in response.text
        assert sentinel not in response.text


def test_queued_private_text_only_reaches_the_exact_reminder_owner(storage) -> None:
    """A restart/drain cannot send stale derived text after reminder quarantine."""

    sentinel = "СЕКРЁТНЫЙ ЁЖ QUEUED REMINDER SENTINEL"
    copied_name = unicodedata.normalize("NFD", sentinel.casefold())
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    event = Entity(
        id="ent-private-queued-reminder",
        user_id="bob",
        name=sentinel,
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(event)
    assert storage.enqueue_notification(
        "alice",
        "5001",
        f"Chronicle copied {copied_name}",
        kind="chronicle",
        dedup_key="chronicle:private-copy",
    )
    assert storage.enqueue_notification(
        "alice",
        "5001",
        copied_name,
        kind="reminder",
        dedup_key=f"reminder:{event.id}:2026-08-06",
    )
    assert storage.enqueue_notification(
        "bob",
        "5002",
        copied_name,
        kind="reminder",
        dedup_key=f"reminder:{event.id}:2026-08-07",
    )
    storage.set_entity_time(
        event.id,
        "bob",
        "2026-08-07",
        source="reminder:bob",
    )

    pending = storage.list_pending_notifications(limit=100)
    assert {(item["user_id"], item["kind"]) for item in pending} == {("bob", "reminder")}
    assert storage.list_pending_reminders("alice", limit=100) == []
    own = storage.list_pending_reminders("bob", limit=100)
    assert len(own) == 1 and own[0]["body"] == copied_name
    alice_reminder_key = f"reminder:{event.id}:2026-08-06"
    hidden_rows = storage.execute(
        """SELECT id, kind, dedup_key, status, attempts, sent_at
             FROM outbound_notifications
            WHERE user_id='alice'
            ORDER BY kind"""
    ).fetchall()
    hidden_before = {str(row["kind"]): dict(row) for row in hidden_rows}
    assert set(hidden_before) == {"chronicle", "reminder"}
    assert storage.reminder_states("alice", [alice_reminder_key]) == {}
    assert storage.reminder_states(
        "bob",
        [f"reminder:{event.id}:2026-08-07"],
    ) == {f"reminder:{event.id}:2026-08-07": "pending"}
    assert not storage.dismiss_notification("alice", hidden_before["reminder"]["id"])
    assert not storage.silence_reminder("alice", alice_reminder_key, chat_id="5001")
    assert (
        storage.discard_notifications(
            [hidden_before["chronicle"]["id"], hidden_before["reminder"]["id"]],
            reason="privacy-regression",
        )
        == 0
    )
    storage.mark_notifications(
        sent_ids=[hidden_before["chronicle"]["id"]],
        failed_ids=[hidden_before["reminder"]["id"]],
        max_attempts=1,
    )
    hidden_after = {
        str(row["kind"]): dict(row)
        for row in storage.execute(
            """SELECT id, kind, dedup_key, status, attempts, sent_at
                 FROM outbound_notifications
                WHERE user_id='alice'
                ORDER BY kind"""
        ).fetchall()
    }
    assert hidden_after == hidden_before
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_time SET source='reminder:alice' WHERE entity_id=?",
            (event.id,),
        )
    assert storage.list_pending_reminders("bob", limit=100) == []
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE entity_time SET source='reminder:bob' WHERE entity_id=?",
            (event.id,),
        )
        conn.execute("DELETE FROM private_entity_owners WHERE entity_id=?", (event.id,))
    assert storage.list_pending_reminders("bob", limit=100) == []
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (event.id, "bob", "2026-08-05T00:00:00Z"),
        )
        conn.execute("DELETE FROM entity_time WHERE entity_id=?", (event.id,))
    assert storage.list_pending_reminders("bob", limit=100) == []
    assert not storage.enqueue_notification(
        "alice",
        "5001",
        f"Reflection copied {sentinel}",
        kind="reflection",
        dedup_key="reflection:private-copy",
    )
    rejected = storage.execute(
        "SELECT 1 FROM outbound_notifications WHERE dedup_key='reflection:private-copy'"
    ).fetchone()
    assert rejected is None


def test_truncated_entity_warning_does_not_log_the_tenant_identifier(storage, caplog) -> None:
    sentinel = "SYNTHETIC_PRIVATE_TENANT_SENTINEL"
    storage.ensure_user(sentinel)
    for index in range(6):
        storage.create_entity(
            Entity(
                id=new_id("ent"),
                user_id=sentinel,
                name=f"Сущность {index}",
            )
        )

    with caplog.at_level(logging.WARNING, logger="friday.storage"):
        rows = storage.list_entities(sentinel, limit=4)

    assert len(rows) == 4
    assert caplog.records
    assert all(sentinel not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_entity_browse_search_and_alias_tail_are_bounded_before_python(settings, monkeypatch) -> None:
    """Public graph browsing cannot turn one tenant into an unbounded payload."""

    sentinel = "SYNTHETIC_PRIVATE_ENTITY_LIST_SENTINEL"
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        aliases_page_fetches: list[int] = []
        observed_sql: list[str] = []
        real_execute = storage.execute

        class _MeasuredCursor:
            def __init__(self, cursor: Any) -> None:
                self._cursor = cursor

            def fetchall(self) -> Any:
                rows = self._cursor.fetchall()
                aliases_page_fetches.append(len(rows))
                return rows

            def __getattr__(self, name: str) -> Any:
                return getattr(self._cursor, name)

        def measured_execute(sql: str, params: Any = ()) -> Any:
            observed_sql.append(" ".join(sql.lower().split()))
            cursor = real_execute(sql, params)
            if "e.aliases_json NOT IN ('[]', '', 'null')" in sql:
                return _MeasuredCursor(cursor)
            return cursor

        for index in range(260):
            aliases = [f"Псевдоним {index:03}", "x" * 7_000]
            if index == 0:
                aliases = ["x" * 9_000 + sentinel]
            storage.create_entity(
                Entity(
                    id=f"ent-private-list-{index:04}",
                    user_id=LEGACY_OWNER_USER_ID,
                    name=f"Общий проект {index:04}",
                    entity_type=EntityType.PROJECT,
                    aliases_json=aliases,
                    description=("d" * 500 + sentinel + "z" * 20_000) if index == 0 else "",
                    metadata_json={"private": sentinel + "m" * 20_000},
                )
            )

        monkeypatch.setattr(storage, "execute", measured_execute)
        tail = storage.find_entity_by_alias(
            LEGACY_OWNER_USER_ID,
            "Псевдоним 259",
            limit=1,
        )
        assert [item["id"] for item in tail] == ["ent-private-list-0259"]
        assert aliases_page_fetches and max(aliases_page_fetches) <= 256

        headers = {"Authorization": f"Bearer {settings.api_token}"}
        listed = client.get("/api/kg/entities", params={"limit": 5000}, headers=headers)
        assert listed.status_code == 200, listed.text
        listing = listed.json()
        assert listing["count"] == 200
        assert listing["total"] == 260
        assert listing["matched_at_least"] == 260
        assert listing["truncated"] is True
        assert sentinel not in listed.text
        assert all(
            not ({"user_id", "metadata_json", "normalized_name", "canonical", "merged_into_id"} & item.keys())
            for item in listing["items"]
        )
        admin_listed = client.get(
            "/api/admin/entities",
            params={"user_id": LEGACY_OWNER_USER_ID, "limit": 5000},
            headers=headers,
        )
        assert admin_listed.status_code == 200, admin_listed.text
        assert admin_listed.json()["count"] == 200
        assert admin_listed.json()["total"] == 260
        assert admin_listed.json()["truncated"] is True
        assert sentinel not in admin_listed.text

        searched = client.get(
            "/api/kg/entities",
            params={"q": "Общий проект", "limit": 5000, "entity_type": "project"},
            headers=headers,
        )
        assert searched.status_code == 200, searched.text
        search_page = searched.json()
        assert search_page["count"] == 25
        assert search_page["matched_at_least"] == 26
        assert search_page["truncated"] is True
        assert sentinel not in searched.text

        observed_sql.clear()
        direct = client.get("/api/kg/entities/ent-private-list-0000", headers=headers)
        assert direct.status_code == 200, direct.text
        assert sentinel not in direct.text
        raw_entity_reads = [statement for statement in observed_sql if "select * from entities" in statement]
        assert raw_entity_reads == []
        bounded_row = _bounded_entity_by_id(
            storage,
            "ent-private-list-0000",
            LEGACY_OWNER_USER_ID,
        )
        assert bounded_row is not None
        assert bounded_row["metadata_json"] == "{}"
        assert len(str(bounded_row["description"])) == 500


def test_broad_entity_search_keeps_only_top_k_cards_alive(storage, monkeypatch) -> None:
    """Mutation gate for append-all-then-sort on broad token matches."""

    import friday.knowledge_graph as graph_module

    class _TrackedEntity(dict[str, Any]):
        alive = 0
        peak = 0

        def __init__(self, index: int) -> None:
            super().__init__(
                id=f"ent-topk-{index:05}",
                user_id="alice",
                name=f"Общий проект {index:05}",
                entity_type="project",
                aliases_json="[]",
                description="",
                metadata_json="{}",
                canonical=1,
                merged_into_id=None,
                version=1,
                created_at="",
                updated_at="",
                deleted_at=None,
            )
            type(self).alive += 1
            type(self).peak = max(type(self).peak, type(self).alive)

        def __del__(self) -> None:
            type(self).alive -= 1

    def corpus(*_args: Any, **_kwargs: Any) -> Any:
        for index in range(5_000):
            yield _TrackedEntity(index)

    graph = KnowledgeGraph(storage)
    monkeypatch.setattr(graph, "match_mentions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(graph_module, "_iter_entities_for_graph_search", corpus)
    found = graph.search_entities("alice", "Общий проект", limit=25)

    assert len(found) == 25
    assert _TrackedEntity.peak <= 30


def test_entity_audit_retains_no_content_or_content_hash_after_hard_purge(settings) -> None:
    """Append-only audit survives deletion, so it may retain shape but no PII trace."""

    first = "SYNTHETIC_PRIVATE_ENTITY_AUDIT_FIRST"
    second = "SYNTHETIC_PRIVATE_ENTITY_AUDIT_SECOND"
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        created = client.post(
            "/api/kg/entities",
            json={
                "name": f"Имя {first}",
                "entity_type": "person",
                "aliases": [f"Псевдоним {first}"],
                "description": f"Описание {first}",
                "metadata": {"private": first},
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        entity_id = created.json()["entity"]["id"]
        changed = client.patch(
            f"/api/kg/entities/{entity_id}",
            json={
                "name": f"Имя {second}",
                "aliases": [f"Псевдоним {second}"],
                "description": f"Описание {second}",
                "metadata": {"private": second},
            },
            headers=headers,
        )
        assert changed.status_code == 200, changed.text
        assert client.delete(f"/api/kg/entities/{entity_id}", headers=headers).status_code == 200
        assert client.post(f"/api/kg/entities/{entity_id}/undelete", headers=headers).status_code == 200
        restored = client.post(
            f"/api/kg/entities/{entity_id}/restore",
            json={"version": 1},
            headers=headers,
        )
        assert restored.status_code == 200, restored.text

        entity_audit = [
            row
            for row in storage.list_audit_log(limit=100)
            if str(row.get("action") or "").startswith("entity.")
        ]
        assert entity_audit
        encoded = json.dumps(entity_audit, ensure_ascii=False)
        assert first not in encoded and second not in encoded
        assert "sha256" not in encoded
        assert "changed_fields" in encoded

        # Simulate the eventual hard-purge boundary. The append-only audit row
        # remains, so the same no-content assertion must still hold afterwards.
        with storage.transaction() as conn:
            conn.execute("DELETE FROM entity_versions WHERE entity_id=?", (entity_id,))
            conn.execute("DELETE FROM entities WHERE id=? AND user_id=?", (entity_id, LEGACY_OWNER_USER_ID))
        assert storage.get_entity(entity_id, LEGACY_OWNER_USER_ID) is None
        after_purge = json.dumps(storage.list_audit_log(limit=100), ensure_ascii=False)
        assert first not in after_purge and second not in after_purge
        assert "sha256" not in after_purge


def test_container_page_preserves_a_parent_just_beyond_the_page(storage) -> None:
    """A displayed child does not become a false root merely because its parent is item 201."""

    storage.ensure_user("alice")
    child = Entity(
        id="ent-container-child",
        user_id="alice",
        name="A child",
        entity_type=EntityType.PROJECT,
    )
    parent = Entity(
        id="ent-container-parent",
        user_id="alice",
        name="Z parent",
        entity_type=EntityType.PROJECT,
    )
    storage.create_entity(child)
    for index in range(199):
        storage.create_entity(
            Entity(
                id=f"ent-container-fill-{index:03}",
                user_id="alice",
                name=f"B filler {index:03}",
                entity_type=EntityType.COLLECTION,
            )
        )
    storage.create_entity(parent)
    storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id="alice",
            source_entity_id=child.id,
            target_entity_id=parent.id,
            relation_type=RelationType.PART_OF,
        )
    )

    page = KnowledgeGraph(storage).list_containers("alice", limit=200)
    by_id = {str(item["id"]): item for item in page}
    assert len(page) == 200
    assert parent.id not in by_id
    assert by_id[child.id]["parent_id"] == parent.id
    assert page.matched_at_least == 201
    assert page.truncated is True


def test_container_parent_link_cannot_publish_a_quarantined_private_target(storage) -> None:
    storage.ensure_user("alice")
    child = Entity(
        id="ent-public-container-child",
        user_id="alice",
        name="Public child",
        entity_type=EntityType.PROJECT,
    )
    parent = Entity(
        id="ent-private-container-parent",
        user_id="alice",
        name="PRIVATE PARENT SENTINEL",
        entity_type=EntityType.PROJECT,
    )
    storage.create_entity(child)
    storage.create_entity(parent)
    storage.create_relation(
        Relation(
            id="rel-private-parent",
            user_id="alice",
            source_entity_id=child.id,
            target_entity_id=parent.id,
            relation_type=RelationType.PART_OF,
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (parent.id, "person-alice", "2026-08-05T00:00:00Z"),
        )

    page = KnowledgeGraph(storage).list_containers("alice")
    encoded = json.dumps(page, ensure_ascii=False)
    by_id = {str(item["id"]): item for item in page}
    assert child.id in by_id
    assert by_id[child.id]["parent_id"] is None
    assert parent.id not in encoded
    assert "PRIVATE PARENT SENTINEL" not in encoded


def test_current_entity_copies_follow_a_later_private_identity_quarantine(storage) -> None:
    """Aliases/descriptions/metadata are dependencies, not independent public facts."""

    user_id = "alice"
    sentinel = "СЕКРЁТНЫЙ ЁЖ ENTITY MATERIAL SENTINEL"
    copied_name = unicodedata.normalize("NFD", sentinel.casefold())
    storage.ensure_user(user_id)
    hidden = Entity(
        id="ent-private-material-source",
        user_id=user_id,
        name=sentinel,
        entity_type=EntityType.EVENT,
    )
    copied = Entity(
        id="ent-public-material-copy",
        user_id=user_id,
        name="Public carrier",
        entity_type=EntityType.EVENT,
        aliases_json=[f"Alias of {copied_name}"],
        description=f"Copied identity {copied_name}",
        metadata_json={"copied": {"identity": copied_name}},
    )
    storage.create_entity(hidden)
    storage.create_entity(copied)
    assert storage.get_entity(copied.id, user_id) is not None
    storage.set_entity_time(copied.id, user_id, "2026-08-06", source="document")
    time_before = dict(
        storage.execute(
            "SELECT * FROM entity_time WHERE entity_id=? AND user_id=?",
            (copied.id, user_id),
        ).fetchone()
    )

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "person-private", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_entity(hidden.id, user_id) is None
    assert storage.get_entity(copied.id, user_id) is None
    assert _bounded_entity_by_id(storage, copied.id, user_id) is None
    assert storage.list_entities(user_id) == []
    assert storage.count_entities(user_id) == 0
    assert storage.find_entity_by_name(user_id, "Public carrier") is None
    assert storage.find_entities_by_normalized_names(user_id, ["Public carrier"]) == []
    assert KnowledgeGraph(storage).search_entities(user_id, sentinel, limit=10) == []
    assert storage.get_entity_time(copied.id, user_id) is None
    assert not storage.delete_entity_time(copied.id, user_id)
    assert not storage.delete_entity_time(copied.id)
    with pytest.raises(ValueError, match="Event entity not found"):
        storage.set_entity_time(copied.id, user_id, "2026-08-07", source="document")
    time_after = dict(
        storage.execute(
            "SELECT * FROM entity_time WHERE entity_id=? AND user_id=?",
            (copied.id, user_id),
        ).fetchone()
    )
    assert time_after == time_before
    overview = storage.graph_overview(user_id)
    assert overview["nodes"] == [] and overview["total"] == 0

    with pytest.raises(ValueError, match="private graph material"):
        storage.create_entity(
            Entity(
                id="ent-rejected-private-material-copy",
                user_id=user_id,
                name="Rejected carrier",
                metadata_json={"copied": sentinel},
            )
        )
    assert (
        storage.execute("SELECT 1 FROM entities WHERE id='ent-rejected-private-material-copy'").fetchone()
        is None
    )

    # Current cleanup alone cannot retire the authenticated v1 copy.  Export and
    # generic reads use the same durable fixed point, so the entity remains hidden
    # until a dedicated privacy-safe history-retirement operation exists.
    stored_before = dict(storage.execute("SELECT * FROM entities WHERE id=?", (copied.id,)).fetchone())
    with pytest.raises(ValueError, match="private graph material"):
        storage.update_entity(
            Entity(
                id=copied.id,
                user_id=user_id,
                name="Clean carrier",
                entity_type=EntityType.PROJECT,
                aliases_json=[],
                description="",
                metadata_json={},
                version=1,
                created_at=copied.created_at,
            )
        )
    stored_after = dict(storage.execute("SELECT * FROM entities WHERE id=?", (copied.id,)).fetchone())
    assert stored_after == stored_before
    assert storage.list_entity_versions(copied.id, user_id) == []
    with pytest.raises(LookupError, match="Version 1 not found"):
        storage.restore_entity_version(copied.id, user_id, 1, reviewed_by=user_id)


def test_current_raw_and_knowledge_material_follow_transitive_quarantine(storage) -> None:
    """Current bodies, JSON keys, links and mutations share one privacy closure."""

    user_id = "alice"
    private_name = "СЕКРЁТНЫЙ ЁЖ CURRENT MATERIAL"
    private_copy = unicodedata.normalize("NFD", private_name.casefold())
    carrier_name = "Transitive carrier identity"
    carrier_copy = unicodedata.normalize("NFD", carrier_name.casefold())
    storage.ensure_user(user_id)
    hidden = storage.create_entity(
        Entity(
            id="ent-current-material-private",
            user_id=user_id,
            name=private_name,
            entity_type=EntityType.EVENT,
        )
    )
    carrier = storage.create_entity(
        Entity(
            id="ent-current-material-carrier",
            user_id=user_id,
            name=carrier_name,
            description=private_copy,
        )
    )

    def make_raw(raw_id: str, content: str, *, metadata: dict[str, Any] | None = None) -> RawObject:
        raw = RawObject(
            id=raw_id,
            user_id=user_id,
            source="test",
            source_ref=f"source:{raw_id}",
            raw_content=content,
            content_type="text",
            metadata_json=metadata or {},
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        return raw

    raw_direct = make_raw("raw-current-private-copy", private_copy)
    raw_json = make_raw(
        "raw-current-private-json-key",
        "Public raw body",
        metadata={carrier_copy: 1},
    )
    raw_direct_ko = make_raw("raw-current-private-ko-source", "Public KO source")
    raw_json_ko = make_raw("raw-current-private-ko-json-source", "Public JSON KO source")
    raw_linked_ko = make_raw("raw-current-private-ko-link-source", "Public linked KO source")
    raw_clean_ko = make_raw("raw-current-public-ko-source", "Public mutation source")

    direct_ko = KnowledgeObject(
        id="ko-current-private-copy",
        user_id=user_id,
        raw_object_id=raw_direct_ko.id,
        content=private_copy,
        content_type="text",
        title="Direct copied KO",
    )
    json_ko = KnowledgeObject(
        id="ko-current-private-json-key",
        user_id=user_id,
        raw_object_id=raw_json_ko.id,
        content="Public JSON KO body",
        content_type="text",
        title="JSON copied KO",
        metadata_json={carrier_copy: 1},
    )
    linked_ko = KnowledgeObject(
        id="ko-current-private-carrier-link",
        user_id=user_id,
        raw_object_id=raw_linked_ko.id,
        content="Public linked KO body",
        content_type="text",
        title="Linked carrier KO",
    )
    clean_ko = KnowledgeObject(
        id="ko-current-public-mutation",
        user_id=user_id,
        raw_object_id=raw_clean_ko.id,
        content="Public stable body",
        content_type="text",
        title="Public stable KO",
    )
    for knowledge in (direct_ko, json_ko, linked_ko, clean_ko):
        storage.store_knowledge_object(knowledge)
    storage.link_knowledge_entity(user_id, linked_ko.id, carrier.id, status="accepted")

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "bob", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_entity(carrier.id, user_id) is None
    for raw_id in (raw_direct.id, raw_json.id, raw_direct_ko.id, raw_json_ko.id, raw_linked_ko.id):
        assert storage.get_raw_object(raw_id, user_id) is None
    for knowledge_id in (direct_ko.id, json_ko.id, linked_ko.id):
        assert storage.get_knowledge_object(knowledge_id, user_id) is None
    assert storage.get_knowledge_object(clean_ko.id, user_id) is not None
    assert storage.list_knowledge_entity_links(user_id, knowledge_object_id=linked_ko.id) == []
    assert not storage.set_document_date(direct_ko.id, user_id, "2026-08-05")

    rejected_raw = RawObject(
        id="raw-rejected-current-private-copy",
        user_id=user_id,
        source="test",
        source_ref="source:rejected-private-copy",
        raw_content=private_copy,
        content_type="text",
    )
    with pytest.raises(ValueError, match="private graph material"):
        storage.store_raw_object(rejected_raw)
    assert storage.execute("SELECT 1 FROM raw_objects WHERE id=?", (rejected_raw.id,)).fetchone() is None

    rejected_ko = KnowledgeObject(
        id="ko-rejected-current-private-copy",
        user_id=user_id,
        raw_object_id=raw_clean_ko.id,
        content=private_copy,
        content_type="text",
        title="Rejected copied KO",
    )
    with pytest.raises(ValueError, match="private graph material"):
        storage.store_knowledge_object(rejected_ko)
    assert storage.execute("SELECT 1 FROM knowledge_objects WHERE id=?", (rejected_ko.id,)).fetchone() is None

    clean_before = dict(
        storage.execute("SELECT * FROM knowledge_objects WHERE id=?", (clean_ko.id,)).fetchone()
    )
    with pytest.raises(ValueError, match="private graph material"):
        storage.update_knowledge_fields(clean_ko.id, user_id, content=private_copy)
    clean_after = dict(
        storage.execute("SELECT * FROM knowledge_objects WHERE id=?", (clean_ko.id,)).fetchone()
    )
    assert clean_after == clean_before

    remediated = storage.update_knowledge_fields(
        direct_ko.id,
        user_id,
        content="Remediated public body",
    )
    assert remediated is not None and remediated["content"] == "Remediated public body"
    versions = storage.list_knowledge_versions(direct_ko.id, user_id)
    assert {int(item["version"]) for item in versions} == {2}
    assert private_copy not in json.dumps(versions, ensure_ascii=False)


def test_private_graph_neighbours_cannot_influence_public_duplicate_proposals(storage) -> None:
    storage.ensure_user("alice")
    left = Entity(
        id="ent-public-duplicate-left",
        user_id="alice",
        name="Alpha Node",
        entity_type=EntityType.PROJECT,
    )
    right = Entity(
        id="ent-public-duplicate-right",
        user_id="alice",
        name="Alfa Node",
        entity_type=EntityType.PROJECT,
    )
    hidden = Entity(
        id="ent-private-duplicate-neighbour",
        user_id="alice",
        name="PRIVATE DUPLICATE NEIGHBOUR SENTINEL",
        entity_type=EntityType.EVENT,
    )
    for entity in (left, right, hidden):
        storage.create_entity(entity)
    for index, source in enumerate((left, right)):
        storage.create_relation(
            Relation(
                id=f"rel-private-duplicate-neighbour-{index}",
                user_id="alice",
                source_entity_id=source.id,
                target_entity_id=hidden.id,
                relation_type=RelationType.RELATED_TO,
            )
        )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "person-alice", "2026-08-05T00:00:00Z"),
        )

    candidates = storage.find_duplicate_candidates("alice", min_confidence=0.0)
    candidate = next(
        item for item in candidates if {item.entity_a_id, item.entity_b_id} == {left.id, right.id}
    )
    assert candidate.evidence_json["shared_graph_neighbours"] == 0.0
    encoded = json.dumps([item.to_row() for item in candidates], ensure_ascii=False)
    assert hidden.id not in encoded
    assert "PRIVATE DUPLICATE NEIGHBOUR SENTINEL" not in encoded


def test_stored_duplicate_score_is_invalidated_when_its_graph_inputs_become_private(storage) -> None:
    """A stale scalar score cannot authorize a merge after its evidence is quarantined."""

    user_id = "alice"
    storage.ensure_user(user_id)
    left = Entity(
        id="ent-stale-resolution-left",
        user_id=user_id,
        name="Alpha Node",
        entity_type=EntityType.PROJECT,
    )
    right = Entity(
        id="ent-stale-resolution-right",
        user_id=user_id,
        name="Alfa Node",
        entity_type=EntityType.PROJECT,
    )
    hidden = Entity(
        id="ent-stale-resolution-private-input",
        user_id=user_id,
        name="PRIVATE STALE RESOLUTION INPUT",
        entity_type=EntityType.EVENT,
    )
    for entity in (left, right, hidden):
        storage.create_entity(entity)
    for index, source in enumerate((left, right)):
        storage.create_relation(
            Relation(
                id=f"rel-stale-resolution-input-{index}",
                user_id=user_id,
                source_entity_id=source.id,
                target_entity_id=hidden.id,
                relation_type=RelationType.RELATED_TO,
            )
        )
    raw = RawObject(
        id="raw-stale-resolution-input",
        user_id=user_id,
        source="test",
        source_ref="stale-resolution-input",
        raw_content="Shared public context",
        content_type="text",
        content_hash=hashlib.sha256(b"Shared public context").hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id="ko-stale-resolution-input",
        user_id=user_id,
        raw_object_id=raw.id,
        content="Shared public context",
        content_type="text",
        title="Shared public context",
    )
    storage.store_knowledge_object(knowledge)
    for entity in (left, right, hidden):
        storage.link_knowledge_entity(user_id, knowledge.id, entity.id, status="accepted")

    generated = next(
        item
        for item in storage.find_duplicate_candidates(user_id, min_confidence=0.0)
        if {item.entity_a_id, item.entity_b_id} == {left.id, right.id}
    )
    assert hidden.id in generated.evidence_json["graph_neighbour_entity_ids"]
    assert knowledge.id in generated.evidence_json["knowledge_object_ids"]
    stored = storage.store_resolution_candidate(generated)
    assert storage.get_resolution_candidate(stored.id, user_id) is not None
    assert storage.count_resolution_candidates(user_id) == 1

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "person-private", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_resolution_candidate(stored.id, user_id) is None
    assert storage.list_resolution_candidates(user_id) == []
    assert storage.count_resolution_candidates(user_id) == 0
    with pytest.raises(ValueError, match="private graph material"):
        storage.store_resolution_candidate(generated)
    with pytest.raises(ValueError, match="not found|no longer pending"):
        KnowledgeGraph(storage).resolver.accept_resolution(
            stored.id,
            user_id,
            target_entity_id=right.id,
        )
    endpoints = storage.execute(
        "SELECT id, canonical, merged_into_id FROM entities WHERE id IN (?, ?) ORDER BY id",
        (left.id, right.id),
    ).fetchall()
    assert all(bool(row["canonical"]) and row["merged_into_id"] is None for row in endpoints)


def test_public_knowledge_link_mutations_and_lineage_never_publish_evidence_or_reviewer(
    settings,
) -> None:
    sentinel = "PRIVATE KNOWLEDGE LINK SENTINEL"
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        graph = app.state.kg
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        raw = RawObject(
            id=new_id("raw"),
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref=new_id("source"),
            raw_content="Public document",
            content_type="text",
            content_hash=hashlib.sha256(b"Public document").hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=raw.id,
            content="Public document",
            content_type="text",
            title="Public title",
        )
        storage.store_knowledge_object(knowledge)
        public_entity = graph.create_entity(
            LEGACY_OWNER_USER_ID,
            "Public entity",
            EntityType.PROJECT,
        )
        private_entity = graph.create_entity(
            LEGACY_OWNER_USER_ID,
            sentinel,
            EntityType.EVENT,
        )
        private_raw = RawObject(
            id=new_id("raw"),
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref=new_id("source"),
            raw_content=sentinel,
            content_type="text",
            content_hash=hashlib.sha256(sentinel.encode()).hexdigest(),
        )
        storage.store_raw_object(private_raw)
        private_knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=private_raw.id,
            content=sentinel,
            content_type="text",
            title=sentinel,
        )
        storage.store_knowledge_object(private_knowledge)
        storage.link_knowledge_entity(
            LEGACY_OWNER_USER_ID,
            private_knowledge.id,
            private_entity["id"],
            status="accepted",
            evidence={"private": sentinel},
        )
        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, ?, 'reminder', ?)""",
                (private_entity["id"], "person-private", "2026-08-05T00:00:00Z"),
            )

        created = client.post(
            "/api/kg/link",
            json={
                "knowledge_object_id": knowledge.id,
                "entity_id": public_entity["id"],
                "status": "accepted",
                "evidence": {"private": sentinel + "x" * 200_000},
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        card = created.json()["link"]
        assert set(card) == {
            "id",
            "knowledge_object_id",
            "entity_id",
            "status",
            "confidence",
            "created_at",
            "reviewed_at",
            "entity_name",
            "entity_type",
            "knowledge_title",
            "knowledge_lifecycle",
            "evidence",
        }
        assert card["evidence"]["present"] is True
        assert card["evidence"]["bytes"] > 200_000
        assert sentinel not in created.text
        assert "evidence_json" not in created.text
        assert "reviewed_by" not in created.text
        assert "user_id" not in created.text

        lineage = client.get(f"/api/knowledge/{knowledge.id}", headers=headers)
        assert lineage.status_code == 200, lineage.text
        encoded_links = json.dumps(lineage.json()["entity_links"], ensure_ascii=False)
        assert sentinel not in encoded_links
        assert private_entity["id"] not in encoded_links
        assert "evidence_json" not in encoded_links
        assert "reviewed_by" not in encoded_links
        assert lineage.json()["entity_link_counts"]["accepted"] == 1

        denied = client.post(
            "/api/kg/link",
            json={
                "knowledge_object_id": private_knowledge.id,
                "entity_id": public_entity["id"],
                "status": "accepted",
            },
            headers=headers,
        )
        assert denied.status_code == 400
        assert client.get(f"/api/knowledge/{private_knowledge.id}", headers=headers).status_code == 404
        public_page = client.get("/api/knowledge", headers=headers)
        assert public_page.status_code == 200
        assert private_knowledge.id not in public_page.text
        assert sentinel not in public_page.text
        assert storage.search_knowledge(LEGACY_OWNER_USER_ID, sentinel) == []
        assert storage.search_raw_objects(LEGACY_OWNER_USER_ID, sentinel) == []
        assert storage.get_raw_object(private_raw.id, LEGACY_OWNER_USER_ID) is None
        assert (
            storage.list_knowledge_objects(
                LEGACY_OWNER_USER_ID,
                entity_id=private_entity["id"],
            )
            == []
        )
        assert (
            storage.count_filtered_knowledge_objects(
                LEGACY_OWNER_USER_ID,
                entity_id=private_entity["id"],
            )
            == 0
        )


def test_resolution_and_merge_surfaces_never_publish_snapshots_or_evidence(
    settings,
    monkeypatch,
) -> None:
    """Owner/admin parity for the graph's most content-heavy review records."""

    sentinel = "SYNTHETIC_PRIVATE_MERGE_SURFACE_SENTINEL"
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        first = kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Первая карточка",
            EntityType.PERSON,
            aliases=["x" * 9_000 + sentinel],
            description="d" * 500 + sentinel + "d" * 20_000,
            metadata={"private": sentinel + "m" * 200_000},
        )
        second = kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Вторая карточка",
            EntityType.PERSON,
            metadata={"private": sentinel + "n" * 200_000},
        )
        candidate = EntityResolutionCandidate(
            id="er-private-surface",
            user_id=LEGACY_OWNER_USER_ID,
            entity_a_id=first["id"],
            entity_b_id=second["id"],
            confidence=0.97,
            resolution_method="synthetic",
            evidence_json={"private": sentinel + "e" * 200_000},
        )
        storage.store_resolution_candidate(candidate)

        for path in (
            "/api/kg/resolutions?status=suggested",
            "/api/kg/resolutions/pending",
            f"/api/admin/resolutions?user_id={LEGACY_OWNER_USER_ID}&status=suggested",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, response.text
            assert sentinel not in response.text
            assert "evidence_json" not in response.text and '"evidence"' not in response.text
            item = response.json()["items"][0]
            assert set(item) <= {
                "id",
                "entity_a_id",
                "entity_b_id",
                "confidence",
                "resolution_method",
                "status",
                "created_at",
                "resolved_at",
                "entity_a",
                "entity_b",
                "recommendation",
            }

        private_report = {
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 2,
            "keys_examined": 1,
            "keys_pending": 1,
            "partial": True,
            "stopped_at": [4, [sentinel]],
            "private": sentinel,
            "sweeps": 0,
            "resumed": False,
            "complete": False,
        }
        monkeypatch.setattr(
            storage,
            "sweep_entity_duplicates",
            lambda *_args, **_kwargs: ([], dict(private_report)),
        )
        owner_detect = client.post("/api/kg/resolutions/detect", headers=headers)
        admin_detect = client.post(
            "/api/admin/resolutions/detect",
            json={"user_id": LEGACY_OWNER_USER_ID},
            headers=headers,
        )
        for response in (owner_detect, admin_detect):
            assert response.status_code == 200, response.text
            assert sentinel not in response.text
            assert "stopped_at" not in response.text and '"private"' not in response.text

        accepted = client.post(
            f"/api/kg/resolutions/{candidate.id}/accept",
            json={"target_entity_id": second["id"]},
            headers=headers,
        )
        assert accepted.status_code == 200, accepted.text
        assert sentinel not in accepted.text
        result = accepted.json()["result"]
        merge_id = result["merge_id"]
        assert set(result) == {
            "merge_id",
            "source_entity_id",
            "target_entity_id",
            "merged_into",
        }
        assert set(result["merged_into"]) <= {
            "id",
            "name",
            "entity_type",
            "knowledge_count",
        }

        oversized = sentinel + "t" * 200_000
        with storage.transaction() as conn:
            conn.executemany(
                """INSERT INTO entity_merge_history(
                       id, user_id, source_entity_id, target_entity_id,
                       source_snapshot_json, target_before_json, target_after_json,
                       transfer_json, merged_by, created_at, undone_at, undone_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                [
                    (
                        f"merge-private-page-{index:03}",
                        LEGACY_OWNER_USER_ID,
                        first["id"],
                        second["id"],
                        json.dumps({"private": oversized if index == 0 else sentinel}),
                        json.dumps({"private": sentinel}),
                        json.dumps({"private": sentinel}),
                        json.dumps(
                            {
                                "private": oversized if index == 0 else sentinel,
                                "links_moved": [{"private": sentinel}],
                                "links_suppressed": [],
                                "relations": [{"private": sentinel}],
                                "closed_candidates": [candidate.id],
                            }
                        ),
                        "owner",
                        f"2026-01-01T00:00:{index % 60:02}Z",
                    )
                    for index in range(205)
                ],
            )

        owner_merges = client.get("/api/kg/merges", params={"limit": 100}, headers=headers)
        admin_merges = client.get(
            "/api/admin/merges",
            params={"user_id": LEGACY_OWNER_USER_ID, "limit": 500},
            headers=headers,
        )
        assert owner_merges.status_code == 200, owner_merges.text
        assert admin_merges.status_code == 200, admin_merges.text
        # The 205 hand-written rows above are deliberately opaque/corrupt: their
        # snapshots have no tenant identity and their transfer entries have no
        # replay grammar.  Cards must fail closed just like get/count/undo.
        assert owner_merges.json()["count"] == 1
        assert owner_merges.json()["total"] == 1
        assert owner_merges.json()["truncated"] is False
        assert admin_merges.json()["count"] == 1
        assert admin_merges.json()["total"] == 1
        assert admin_merges.json()["truncated"] is False
        for response in (owner_merges, admin_merges):
            assert sentinel not in response.text
            assert "snapshot" not in response.text and "transfer_json" not in response.text
            assert all(
                set(item)
                == {
                    "id",
                    "source_entity_id",
                    "target_entity_id",
                    "created_at",
                    "undone_at",
                    "undoable",
                    "transfer_bytes",
                    "links_moved_count",
                    "links_suppressed_count",
                    "relations_count",
                    "candidates_closed_count",
                }
                for item in response.json()["items"]
            )

        undone = client.post(f"/api/kg/merges/{merge_id}/undo", headers=headers)
        assert undone.status_code == 200, undone.text
        assert sentinel not in undone.text
        assert set(undone.json()["result"]["source"]) <= {
            "id",
            "name",
            "entity_type",
            "knowledge_count",
        }
        assert set(undone.json()["result"]["target"]) <= {
            "id",
            "name",
            "entity_type",
            "knowledge_count",
        }

        third = kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Третья карточка",
            EntityType.PERSON,
            metadata={"private": sentinel + "q" * 20_000},
        )
        fourth = kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Четвёртая карточка",
            EntityType.PERSON,
            metadata={"private": sentinel + "r" * 20_000},
        )
        admin_candidate = EntityResolutionCandidate(
            id="er-private-admin-surface",
            user_id=LEGACY_OWNER_USER_ID,
            entity_a_id=third["id"],
            entity_b_id=fourth["id"],
            confidence=0.96,
            resolution_method="synthetic",
            evidence_json={"private": sentinel + "s" * 20_000},
        )
        storage.store_resolution_candidate(admin_candidate)
        admin_accepted = client.post(
            f"/api/admin/resolutions/{admin_candidate.id}/accept",
            json={"user_id": LEGACY_OWNER_USER_ID, "target_entity_id": fourth["id"]},
            headers=headers,
        )
        assert admin_accepted.status_code == 200, admin_accepted.text
        assert sentinel not in admin_accepted.text
        admin_merge_id = admin_accepted.json()["entity"]["merge_id"]
        admin_undone = client.post(
            f"/api/admin/merges/{admin_merge_id}/undo",
            json={"user_id": LEGACY_OWNER_USER_ID},
            headers=headers,
        )
        assert admin_undone.status_code == 200, admin_undone.text
        assert sentinel not in admin_undone.text

        audit = json.dumps(storage.list_audit_log(limit=200), ensure_ascii=False)
        assert sentinel not in audit
        assert "snapshot_json" not in audit and "transfer_json" not in audit


@pytest.mark.asyncio
async def test_duplicate_and_merge_tools_keep_private_graph_payloads_from_the_model(
    settings,
    storage,
    monkeypatch,
) -> None:
    sentinel = "SYNTHETIC_PRIVATE_MERGE_TOOL_SENTINEL"
    kernel = _kernel(settings, storage)
    _, kg, _, _ = kernel._require_services()  # noqa: SLF001
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    monkeypatch.setattr(
        storage,
        "sweep_entity_duplicates",
        lambda *_args, **_kwargs: (
            [],
            {
                "entities": 2,
                "pairs_examined": 1,
                "keys_total": 2,
                "keys_examined": 1,
                "keys_pending": 1,
                "partial": True,
                "stopped_at": [4, [sentinel]],
                "private": sentinel,
                "sweeps": 0,
                "resumed": False,
                "complete": False,
            },
        ),
    )
    duplicate_result = await kernel._resolve_duplicates(actor=actor)  # noqa: SLF001
    assert sentinel not in json.dumps(duplicate_result, ensure_ascii=False)

    first = kg.create_entity(
        "alice",
        "Первая",
        EntityType.PERSON,
        description="d" * 500 + sentinel,
        metadata={"private": sentinel + "m" * 50_000},
    )
    second = kg.create_entity(
        "alice",
        "Вторая",
        EntityType.PERSON,
        metadata={"private": sentinel + "n" * 50_000},
    )
    candidate = EntityResolutionCandidate(
        id="er-private-tool",
        user_id="alice",
        entity_a_id=first["id"],
        entity_b_id=second["id"],
        confidence=0.99,
        resolution_method="synthetic",
        evidence_json={"private": sentinel + "e" * 50_000},
    )
    storage.store_resolution_candidate(candidate)
    merged = await kernel._entity_merge_decide(  # noqa: SLF001
        actor=actor,
        candidate_id=candidate.id,
        decision="accept",
        target_entity_id=second["id"],
    )
    assert sentinel not in json.dumps(merged, ensure_ascii=False)
    merge_id = str(merged["result"]["merge_id"])
    undone = await kernel._entity_merge_undo(actor=actor, merge_id=merge_id)  # noqa: SLF001
    assert sentinel not in json.dumps(undone, ensure_ascii=False)
