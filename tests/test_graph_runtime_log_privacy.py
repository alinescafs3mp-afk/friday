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
        audit_before = [
            dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
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

        def assert_entity_state(response, marker):
            expected = {
                "id": entity_id,
                "name": f"Имя {marker}",
                "entity_type": "person",
                "description": f"Описание {marker}",
            }
            public = response.json()["entity"]
            assert {key: public[key] for key in expected} == expected, "entity_http_projection"
            assert public["aliases"] == [f"Псевдоним {marker}"], "entity_http_aliases"
            assert "metadata" not in public and "metadata_json" not in public
            persisted = storage.get_entity(entity_id, LEGACY_OWNER_USER_ID)
            assert persisted is not None, "entity_http_persistence"
            assert {key: persisted[key] for key in expected} == expected, "entity_http_persistence"
            assert persisted["user_id"] == LEGACY_OWNER_USER_ID
            assert json.loads(persisted["aliases_json"]) == [f"Псевдоним {marker}"]
            assert json.loads(persisted["metadata_json"])["private"] == marker
            assert not persisted["deleted_at"] and int(persisted["canonical"]) == 1

        assert_entity_state(created, first)
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
        assert_entity_state(changed, second)
        deleted = client.delete(f"/api/kg/entities/{entity_id}", headers=headers)
        assert deleted.status_code == 200 and deleted.json() == {"status": "soft_deleted"}
        assert storage.get_entity(entity_id, LEGACY_OWNER_USER_ID)["deleted_at"], "entity_http_delete"
        undeleted = client.post(f"/api/kg/entities/{entity_id}/undelete", headers=headers)
        assert undeleted.status_code == 200
        assert_entity_state(undeleted, second)
        restored = client.post(
            f"/api/kg/entities/{entity_id}/restore",
            json={"version": 1},
            headers=headers,
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored_from_version"] == 1
        assert_entity_state(restored, first)

        audit_after = [
            dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        assert audit_after[: len(audit_before)] == audit_before, "entity_http_audit_prefix"
        entity_audit = audit_after[len(audit_before) :]
        assert [
            (row["action"], row["user_id"], row["target_type"], row["target_id"]) for row in entity_audit
        ] == [
            ("entity." + action, LEGACY_OWNER_USER_ID, "entity", entity_id)
            for action in ("create", "update", "delete", "undelete", "restore")
        ], "entity_http_audit_sequence"
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

    import hashlib as _hashlib
    import hmac as _hmac
    from datetime import UTC, datetime

    sentinel = "SYNTHETIC_PRIVATE_MERGE_SURFACE_SENTINEL"
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        foreign_id = "tenant-foreign-admin-id"
        guest_id = "tenant-guest-merge-id"
        user_id = "tenant-user-merge-id"
        noread_id = "tenant-noread-merge-id"
        foreign_secret = "foreign-admin-" + "F" * 32
        guest_secret = "guest-merge-" + "G" * 32
        user_secret = "user-merge-" + "U" * 32
        noread_secret = "noread-merge-" + "N" * 32

        def _sql_row(table: str, row_id: str) -> dict[str, Any]:
            allowed = {
                "entity_resolution_candidates": "entity_resolution_candidates",
                "entities": "entities",
                "entity_merge_history": "entity_merge_history",
                "relations": "relations",
                "knowledge_entity_links": "knowledge_entity_links",
            }
            row = storage.execute(
                f"SELECT * FROM {allowed[table]} WHERE id=?",
                (row_id,),
            ).fetchone()
            assert row is not None, f"{table} {row_id} missing"
            return dict(row)

        def _sql_rows(table: str) -> list[dict[str, Any]]:
            allowed = {
                "entity_resolution_candidates": "entity_resolution_candidates",
                "entities": "entities",
                "entity_merge_history": "entity_merge_history",
                "relations": "relations",
                "knowledge_entity_links": "knowledge_entity_links",
            }
            rows = storage.execute(f"SELECT * FROM {allowed[table]} ORDER BY rowid ASC").fetchall()
            return [dict(row) for row in rows]

        def _audit_ordered() -> list[dict[str, Any]]:
            rows = storage.execute(
                "SELECT id, user_id, action, target_type, target_id, before_json, "
                "after_json, ip_address, request_id, created_at "
                "FROM audit_log ORDER BY rowid ASC"
            ).fetchall()
            return [dict(row) for row in rows]

        def _audit_known(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "action": row["action"],
                "user_id": row["user_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
            }

        def _assert_audit_delta(before, after, expected):
            assert after[: len(before)] == before
            delta = after[len(before) :]
            observed = [_audit_known(row) for row in delta]
            assert observed == expected, observed
            return delta

        def _real_history() -> list[dict[str, Any]]:
            return [
                row
                for row in _sql_rows("entity_merge_history")
                if not str(row["id"]).startswith("merge-private-page-")
            ]

        def _selected_state() -> dict[str, Any]:
            return {
                "candidates": _sql_rows("entity_resolution_candidates"),
                "entities": _sql_rows("entities"),
                "relations": _sql_rows("relations"),
                "links": _sql_rows("knowledge_entity_links"),
                "history": _sql_rows("entity_merge_history"),
            }

        def _headers(target: str, preset: str, secret: str) -> dict[str, str]:
            storage.ensure_user(target, source="api-token", display_name=target, preset_key=preset)
            storage.update_user(target, preset_key=preset)
            storage.create_api_token(
                target,
                _hashlib.sha256(secret.encode()).hexdigest(),
                label="resolution-merge-http",
                created_by="test",
            )
            return {"Authorization": f"Bearer {secret}"}

        def _resolution_card(
            candidate_id: str,
            *,
            knowledge_a: int,
            relation_a: int,
            knowledge_b: int = 0,
            relation_b: int = 0,
        ) -> dict[str, Any]:
            cand = _sql_row("entity_resolution_candidates", candidate_id)
            left = _sql_row("entities", cand["entity_a_id"])
            right = _sql_row("entities", cand["entity_b_id"])
            confidence = float(cand["confidence"])
            if confidence >= 0.95:
                recommendation = "strong_merge_candidate"
            elif confidence >= 0.78:
                recommendation = "compare_context"
            else:
                recommendation = "manual_review"
            return {
                "id": cand["id"],
                "entity_a_id": cand["entity_a_id"],
                "entity_b_id": cand["entity_b_id"],
                "confidence": confidence,
                "resolution_method": "synthetic",
                "status": cand["status"],
                "created_at": str(cand["created_at"] or ""),
                "resolved_at": str(cand["resolved_at"] or ""),
                "entity_a": {
                    "id": left["id"],
                    "name": left["name"],
                    "entity_type": "person",
                    "knowledge_count": knowledge_a,
                    "relation_count": relation_a,
                },
                "entity_b": {
                    "id": right["id"],
                    "name": right["name"],
                    "entity_type": "person",
                    "knowledge_count": knowledge_b,
                    "relation_count": relation_b,
                },
                "recommendation": recommendation,
            }

        def _kg_list_envelope(
            items: list[dict[str, Any]],
            *,
            limit: int,
            offset: int,
            total: int,
            matched_at_least: int,
        ) -> dict[str, Any]:
            return {
                "items": items,
                "count": len(items),
                "total": total,
                "limit": limit,
                "offset": offset,
                "matched_at_least": matched_at_least,
                "truncated": offset + len(items) < total,
            }

        def _pending_envelope(items: list[dict[str, Any]], *, total: int) -> dict[str, Any]:
            return {
                "items": items,
                "count": len(items),
                "total": total,
                "matched_at_least": total,
                "truncated": total > len(items),
            }

        def _admin_list_envelope(
            items: list[dict[str, Any]],
            *,
            limit: int,
            offset: int,
            total: int,
            matched_at_least: int,
        ) -> dict[str, Any]:
            body = _kg_list_envelope(
                items,
                limit=limit,
                offset=offset,
                total=total,
                matched_at_least=matched_at_least,
            )
            return {"user_id": LEGACY_OWNER_USER_ID, **body}

        def _history_card(merge_id: str) -> dict[str, Any]:
            raw = _sql_row("entity_merge_history", merge_id)
            transfer = json.loads(raw["transfer_json"] or "{}")
            transfer_bytes = len(str(raw["transfer_json"] or "").encode("utf-8"))

            def _count(field: str) -> int:
                value = transfer.get(field)
                return len(value) if isinstance(value, list) else 0

            return {
                "id": raw["id"],
                "source_entity_id": raw["source_entity_id"],
                "target_entity_id": raw["target_entity_id"],
                "created_at": str(raw["created_at"] or ""),
                "undone_at": str(raw["undone_at"] or ""),
                "undoable": bool(transfer) and not raw.get("undone_at"),
                "transfer_bytes": transfer_bytes,
                "links_moved_count": _count("links_moved"),
                "links_suppressed_count": _count("links_suppressed"),
                "relations_count": _count("relations"),
                "candidates_closed_count": _count("closed_candidates"),
            }

        def _history_envelope(items: list[dict[str, Any]], *, admin: bool) -> dict[str, Any]:
            body = {
                "items": items,
                "count": len(items),
                "total": len(items),
                "matched_at_least": len(items),
                "truncated": False,
            }
            if admin:
                return {"user_id": LEGACY_OWNER_USER_ID, **body}
            return body

        def _private_needles(*extra: str) -> tuple[str, ...]:
            return (
                sentinel,
                "FOREIGN-PRIVATE",
                foreign_secret,
                guest_secret,
                user_secret,
                noread_secret,
                settings.api_token,
                "evidence_json",
                "snapshot_json",
                "transfer_json",
                "source_snapshot",
                *extra,
            )

        def _scan_needles(payload: object) -> None:
            if payload is None:
                blob = ""
            elif isinstance(payload, str):
                blob = payload
            else:
                blob = json.dumps(payload, ensure_ascii=False, default=str)
            for needle in _private_needles():
                assert needle not in blob, needle

        def _observe_http(call, expected_delta, *, mutate: bool = False):
            before_state = _selected_state()
            before_audit = _audit_ordered()
            response = call()
            delta = _assert_audit_delta(before_audit, _audit_ordered(), expected_delta)
            if not mutate:
                assert _selected_state() == before_state
            _scan_needles(response.text)
            for row in delta:
                _scan_needles(row)
            return response

        def _parse_after(row: dict[str, Any]) -> dict[str, Any]:
            raw = row.get("after_json")
            if raw in (None, ""):
                return {}
            if isinstance(raw, dict):
                return raw
            return json.loads(raw)

        def _parse_json_field(raw: object) -> dict[str, Any] | None:
            if raw in (None, ""):
                return None
            if isinstance(raw, dict):
                return raw
            return json.loads(str(raw))

        def _privacy_key() -> bytes:
            row = storage.execute(
                "SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'"
            ).fetchone()
            text = str(row["value"] if row is not None else "")
            assert len(text) == 64
            return bytes.fromhex(text)

        def _opaque_target(target_type: str, raw_id: str) -> str:
            domain = f"target:{target_type}"
            digest = _hmac.new(
                _privacy_key(),
                f"{domain}\0{raw_id}".encode(),
                _hashlib.sha256,
            ).hexdigest()[:24]
            return f"{target_type}:ref:{digest}"

        def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
            return {row["id"]: row for row in rows}

        def _opaque_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [row for row in rows if str(row["id"]).startswith("merge-private-page-")]

        def _public_person(entity_id: str, name: str) -> dict[str, Any]:
            return {
                "id": entity_id,
                "name": name,
                "entity_type": "person",
                "knowledge_count": 0,
            }

        def _parse_aware_utc(value: object) -> datetime:
            text = str(value or "")
            assert text not in ("", "None"), value
            if text.endswith("Z"):
                parsed = datetime.fromisoformat(text[:-1] + "+00:00")
            else:
                parsed = datetime.fromisoformat(text)
            assert parsed.tzinfo is not None, value
            return parsed.astimezone(UTC)

        def _parse_merge_ts(value: object) -> datetime:
            text = str(value or "")
            assert text.endswith("Z") and "." in text, value
            return _parse_aware_utc(text)

        def _parse_candidate_r(value: object) -> datetime:
            text = str(value or "")
            assert "+00:00" in text and "Z" not in text, value
            parsed = _parse_aware_utc(text)
            assert parsed.microsecond == 0, value
            return parsed

        def _floor_seconds(value: datetime) -> datetime:
            return value.replace(microsecond=0)

        def _in_call_window(parsed: datetime, start: datetime, end: datetime) -> None:
            assert start <= parsed <= end, (parsed.isoformat(), start.isoformat(), end.isoformat())

        def _time_authority() -> dict[str, Any]:
            row = storage.execute(
                """SELECT
                       (SELECT recorded_at FROM relation_revisions
                         ORDER BY event_seq DESC LIMIT 1) AS latest_relation_recorded_at,
                       (SELECT created_at FROM entity_versions
                         ORDER BY created_at DESC, rowid DESC
                         LIMIT 1) AS latest_entity_version_at,
                       (SELECT created_at FROM entity_merge_history
                         ORDER BY created_at DESC, rowid DESC
                         LIMIT 1) AS latest_merge_created_at,
                       (SELECT undone_at FROM entity_merge_history
                         WHERE undone_at IS NOT NULL
                         ORDER BY undone_at DESC, rowid DESC
                         LIMIT 1) AS latest_merge_undone_at,
                       (SELECT value FROM schema_meta
                         WHERE key='relation_history_complete_from') AS history_floor,
                       (SELECT observed_at FROM relation_revision_context
                         WHERE singleton=1) AS observed_at"""
            ).fetchone()
            return dict(row) if row is not None else {}

        def _assert_authority_not_ahead(authority: dict[str, Any], call_start: datetime) -> None:
            for key, raw in authority.items():
                if not raw:
                    continue
                parsed = _parse_aware_utc(raw)
                assert parsed <= call_start, (key, raw, call_start.isoformat())

        def _row_with(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
            out = dict(row)
            out.update(changes)
            return out

        def _json_roundtrip(row: dict[str, Any]) -> dict[str, Any]:
            return json.loads(json.dumps(row, ensure_ascii=False))

        def _bound_version(after: object, before: object) -> None:
            assert int(after) == int(before) + 1

        def _assert_table_preserved(
            before_rows: list[dict[str, Any]],
            after_rows: list[dict[str, Any]],
            *,
            permitted: dict[str, set[str]] | None = None,
            allow_new: set[str] | None = None,
            allow_removed: set[str] | None = None,
        ) -> None:
            permitted = permitted or {}
            allow_new = set(allow_new or ())
            allow_removed = set(allow_removed or ())
            before = _by_id(before_rows)
            after = _by_id(after_rows)
            assert set(after) - set(before) == allow_new
            assert set(before) - set(after) == allow_removed
            for row_id, brow in before.items():
                if row_id in allow_removed:
                    continue
                arow = after[row_id]
                allowed = permitted.get(row_id, set())
                for key, value in brow.items():
                    if key in allowed:
                        continue
                    assert arow[key] == value, (row_id, key, value, arow.get(key))

        def _closed_merge_after(
            row: dict[str, Any],
            *,
            merge_id: str,
            source_id: str,
            target_id: str,
            undone_at: str | None = None,
            source_fields: int | None = None,
        ) -> dict[str, Any]:
            assert row.get("before_json") in (None, "")
            parsed = _parse_json_field(row.get("after_json"))
            expected: dict[str, Any] = {
                "merge_id": merge_id,
                "private_fields_count": 1,
                "private_items_count": 13,
                "source_entity_id": source_id,
                "target_entity_id": target_id,
            }
            if source_fields is not None:
                expected["source_fields"] = source_fields
            if undone_at is not None:
                expected["undone_at"] = undone_at
            assert parsed == expected
            return parsed

        def _observe_mutation(call, expected_known):
            before_state = _selected_state()
            before_audit = _audit_ordered()
            authority = _time_authority()
            call_start = datetime.now(UTC)
            _assert_authority_not_ahead(authority, call_start)
            response = call()
            call_end = datetime.now(UTC)
            delta = _assert_audit_delta(before_audit, _audit_ordered(), expected_known)
            _scan_needles(response.text)
            for row in delta:
                _scan_needles(row)
            window = {"start": call_start, "end": call_end, "authority": authority}
            return response, before_state, delta, window

        def _moved_link(
            before_links: list[dict[str, Any]],
            after_links: list[dict[str, Any]],
            *,
            knowledge_id: str,
            from_entity: str,
            to_entity: str,
        ) -> None:
            assert len(before_links) == 1
            assert len(after_links) == 1
            blink = before_links[0]
            alink = after_links[0]
            assert blink["knowledge_object_id"] == knowledge_id
            assert alink["knowledge_object_id"] == knowledge_id
            assert blink["entity_id"] == from_entity
            assert alink["entity_id"] == to_entity
            for key, value in blink.items():
                if key in {"id", "entity_id"}:
                    continue
                assert alink[key] == value

        foreign = _headers(foreign_id, "admin", foreign_secret)
        guest = _headers(guest_id, "guest", guest_secret)
        user = _headers(user_id, "user", user_secret)
        noread = _headers(noread_id, "user", noread_secret)
        storage.set_permission_override(noread_id, "kg.read", "deny")

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
        anchor = kg.create_entity(
            LEGACY_OWNER_USER_ID,
            "Якорь связи",
            EntityType.CONCEPT,
        )
        relation = kg.create_relation(
            LEGACY_OWNER_USER_ID,
            first["id"],
            anchor["id"],
            RelationType.RELATED_TO,
        )
        raw = RawObject(
            id=new_id("raw"),
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref=new_id("src"),
            raw_content="private-link-body " + sentinel,
            content_type="text",
            content_hash=_hashlib.sha256(b"link-body").hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=raw.id,
            title="Связанный документ",
            content="private-link-body " + sentinel,
            content_type="text",
        )
        storage.store_knowledge_object(knowledge)
        storage.link_knowledge_entity(LEGACY_OWNER_USER_ID, knowledge.id, first["id"], status="accepted")
        personal = EntityResolutionCandidate(
            id="er-private-surface",
            user_id=LEGACY_OWNER_USER_ID,
            entity_a_id=first["id"],
            entity_b_id=second["id"],
            confidence=0.97,
            resolution_method="synthetic",
            evidence_json={"private": sentinel + "e" * 200_000},
        )
        storage.store_resolution_candidate(personal)
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
        foreign_left = kg.create_entity(foreign_id, "Чужая левая", EntityType.PERSON)
        foreign_right = kg.create_entity(foreign_id, "Чужая правая", EntityType.PERSON)
        foreign_candidate = EntityResolutionCandidate(
            id="er-foreign-surface",
            user_id=foreign_id,
            entity_a_id=foreign_left["id"],
            entity_b_id=foreign_right["id"],
            confidence=0.91,
            resolution_method="synthetic",
            evidence_json={"private": "FOREIGN-PRIVATE"},
        )
        storage.store_resolution_candidate(foreign_candidate)

        original_first = _sql_row("entities", first["id"])
        original_second = _sql_row("entities", second["id"])
        original_third = _sql_row("entities", third["id"])
        original_fourth = _sql_row("entities", fourth["id"])
        original_personal = _sql_row("entity_resolution_candidates", personal.id)
        original_admin_candidate = _sql_row("entity_resolution_candidates", admin_candidate.id)
        original_relation = _sql_row("relations", relation.id)
        original_links = _sql_rows("knowledge_entity_links")
        assert json.loads(original_first["aliases_json"] or "[]") == ["x" * 9000 + sentinel]
        assert json.loads(original_second["aliases_json"] or "[]") == []
        assert json.loads(original_third["aliases_json"] or "[]") == []
        assert json.loads(original_fourth["aliases_json"] or "[]") == []
        assert original_second["aliases_json"] == "[]"
        assert original_fourth["aliases_json"] == "[]"
        assert original_first["name"] == "Первая карточка"
        assert original_second["name"] == "Вторая карточка"
        assert original_third["name"] == "Третья карточка"
        assert original_fourth["name"] == "Четвёртая карточка"
        expected_personal_target_aliases = json.dumps(
            ["x" * 9000 + sentinel, "Первая карточка"],
            ensure_ascii=False,
        )
        expected_admin_target_aliases = json.dumps(["Третья карточка"], ensure_ascii=False)
        assert json.loads(expected_personal_target_aliases) == [
            "x" * 9000 + sentinel,
            "Первая карточка",
        ]
        assert json.loads(expected_admin_target_aliases) == ["Третья карточка"]
        four_ids = (first["id"], second["id"], third["id"], fourth["id"])
        original_entity_time = [
            dict(row)
            for row in storage.execute(
                "SELECT * FROM entity_time WHERE entity_id IN (?,?,?,?)",
                four_ids,
            ).fetchall()
        ]
        original_primary_knowledge = [
            dict(row)
            for row in storage.execute(
                "SELECT * FROM knowledge_objects WHERE entity_id IN (?,?,?,?)",
                four_ids,
            ).fetchall()
        ]

        def _non_reminder_times(entity_id: str) -> list[dict[str, Any]]:
            return [
                row
                for row in original_entity_time
                if row["entity_id"] == entity_id and not str(row.get("source") or "").startswith("reminder:")
            ]

        assert _non_reminder_times(first["id"]) == []
        assert _non_reminder_times(third["id"]) == []
        expected_personal_primary_moved = [
            str(row["id"]) for row in original_primary_knowledge if row["entity_id"] == first["id"]
        ]
        expected_admin_primary_moved = [
            str(row["id"]) for row in original_primary_knowledge if row["entity_id"] == third["id"]
        ]
        assert expected_admin_primary_moved == []
        assert len(original_links) == 1
        assert original_links[0]["entity_id"] == first["id"]

        personal_card = _resolution_card(personal.id, knowledge_a=1, relation_a=1)
        admin_card = _resolution_card(admin_candidate.id, knowledge_a=0, relation_a=0)
        foreign_card = _resolution_card(foreign_candidate.id, knowledge_a=0, relation_a=0)
        assert personal_card["confidence"] == 0.97
        assert admin_card["confidence"] == 0.96
        assert foreign_card["confidence"] == 0.91
        assert personal_card["entity_a"]["name"] == "Первая карточка"
        assert personal_card["entity_b"]["name"] == "Вторая карточка"
        assert admin_card["entity_a"]["name"] == "Третья карточка"
        assert admin_card["entity_b"]["name"] == "Четвёртая карточка"
        assert personal_card["recommendation"] == "strong_merge_candidate"
        assert admin_card["recommendation"] == "strong_merge_candidate"
        assert foreign_card["recommendation"] == "compare_context"

        owner_list = _observe_http(
            lambda: client.get("/api/kg/resolutions?status=suggested", headers=owner),
            [],
        )
        assert owner_list.status_code == 200, owner_list.text
        assert owner_list.json() == _kg_list_envelope(
            [personal_card, admin_card],
            limit=100,
            offset=0,
            total=2,
            matched_at_least=2,
        )

        owner_pending = _observe_http(
            lambda: client.get("/api/kg/resolutions/pending", headers=owner),
            [],
        )
        assert owner_pending.status_code == 200, owner_pending.text
        assert owner_pending.json() == _pending_envelope([personal_card, admin_card], total=2)
        assert "limit" not in owner_pending.json()
        assert "offset" not in owner_pending.json()

        owner_admin_list = _observe_http(
            lambda: client.get(
                f"/api/admin/resolutions?user_id={LEGACY_OWNER_USER_ID}&status=suggested",
                headers=owner,
            ),
            [],
        )
        assert owner_admin_list.status_code == 200, owner_admin_list.text
        assert owner_admin_list.json() == _admin_list_envelope(
            [personal_card, admin_card],
            limit=100,
            offset=0,
            total=2,
            matched_at_least=2,
        )

        owner_limit1 = _observe_http(
            lambda: client.get(
                "/api/kg/resolutions?status=suggested&limit=1&offset=0",
                headers=owner,
            ),
            [],
        )
        assert owner_limit1.status_code == 200, owner_limit1.text
        assert owner_limit1.json() == _kg_list_envelope(
            [personal_card],
            limit=1,
            offset=0,
            total=2,
            matched_at_least=2,
        )

        owner_page2 = _observe_http(
            lambda: client.get(
                "/api/kg/resolutions?status=suggested&limit=1&offset=1",
                headers=owner,
            ),
            [],
        )
        assert owner_page2.status_code == 200, owner_page2.text
        assert owner_page2.json() == _kg_list_envelope(
            [admin_card],
            limit=1,
            offset=1,
            total=2,
            matched_at_least=1,
        )

        foreign_own = _observe_http(
            lambda: client.get("/api/kg/resolutions?status=suggested", headers=foreign),
            [],
        )
        assert foreign_own.status_code == 200, foreign_own.text
        assert foreign_own.json() == _kg_list_envelope(
            [foreign_card],
            limit=100,
            offset=0,
            total=1,
            matched_at_least=1,
        )
        assert personal.id not in {item["id"] for item in foreign_own.json()["items"]}

        foreign_admin = _observe_http(
            lambda: client.get(
                f"/api/admin/resolutions?user_id={LEGACY_OWNER_USER_ID}&status=suggested",
                headers=foreign,
            ),
            [
                {
                    "action": "admin.resolutions.read",
                    "user_id": foreign_id,
                    "target_type": "user",
                    "target_id": LEGACY_OWNER_USER_ID,
                }
            ],
        )
        assert foreign_admin.status_code == 200, foreign_admin.text
        assert foreign_admin.json() == _admin_list_envelope(
            [personal_card, admin_card],
            limit=100,
            offset=0,
            total=2,
            matched_at_least=2,
        )

        foreign_bad_status = _observe_http(
            lambda: client.get(
                f"/api/admin/resolutions?user_id={LEGACY_OWNER_USER_ID}&status=not-a-status",
                headers=foreign,
            ),
            [
                {
                    "action": "admin.resolutions.read",
                    "user_id": foreign_id,
                    "target_type": "user",
                    "target_id": LEGACY_OWNER_USER_ID,
                }
            ],
        )
        assert foreign_bad_status.status_code == 400, foreign_bad_status.text
        assert foreign_bad_status.json() == {"detail": "Недопустимый статус объединения"}

        owner_admin_bad = _observe_http(
            lambda: client.get(
                f"/api/admin/resolutions?user_id={LEGACY_OWNER_USER_ID}&status=not-a-status",
                headers=owner,
            ),
            [],
        )
        assert owner_admin_bad.status_code == 400
        assert owner_admin_bad.json() == {"detail": "Недопустимый статус объединения"}

        owner_kg_bad = _observe_http(
            lambda: client.get("/api/kg/resolutions?status=not-a-status", headers=owner),
            [],
        )
        assert owner_kg_bad.status_code == 400
        assert owner_kg_bad.json() == {"detail": "Недопустимый статус объединения"}

        anon = _observe_http(
            lambda: client.get("/api/kg/resolutions"),
            [
                {
                    "action": "auth.failed",
                    "user_id": "anonymous",
                    "target_type": "auth",
                    "target_id": "invalid_credentials",
                }
            ],
        )
        assert anon.status_code == 401
        assert anon.json() == {"detail": "Missing authentication"}
        anon_delta = _audit_ordered()[-1]
        assert anon_delta["before_json"] is None
        assert json.loads(anon_delta["after_json"]) == {
            "method_chars": 3,
            "path_chars": 19,
            "reason": "invalid_credentials",
            "status_present": True,
        }

        noread_list = _observe_http(
            lambda: client.get("/api/kg/resolutions?status=suggested", headers=noread),
            [],
        )
        assert noread_list.status_code == 403
        assert noread_list.json() == {"detail": "Access denied for kg.read (explicit_deny)"}

        guest_list = _observe_http(
            lambda: client.get("/api/kg/resolutions?status=suggested", headers=guest),
            [],
        )
        assert guest_list.status_code == 200
        assert guest_list.json() == _kg_list_envelope([], limit=100, offset=0, total=0, matched_at_least=0)

        user_list = _observe_http(
            lambda: client.get("/api/kg/resolutions?status=suggested", headers=user),
            [],
        )
        assert user_list.status_code == 200
        assert user_list.json() == _kg_list_envelope([], limit=100, offset=0, total=0, matched_at_least=0)

        guest_admin = _observe_http(
            lambda: client.get(
                f"/api/admin/resolutions?user_id={LEGACY_OWNER_USER_ID}&status=suggested",
                headers=guest,
            ),
            [],
        )
        assert guest_admin.status_code == 403
        assert guest_admin.json() == {"detail": "Access denied for admin.all_data.read (default_deny)"}

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
        owner_detect = _observe_http(
            lambda: client.post("/api/kg/resolutions/detect", headers=owner),
            [],
        )
        assert owner_detect.status_code == 200, owner_detect.text
        assert "stopped_at" not in owner_detect.text
        assert owner_detect.json()["scan"] == {
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 2,
            "keys_examined": 1,
            "keys_pending": 1,
            "suggested": 0,
            "pending_total": 2,
            "sweeps": 0,
            "partial": True,
            "resumed": False,
            "complete": False,
        }
        assert owner_detect.json()["items"] == [personal_card, admin_card]
        assert owner_detect.json()["count"] == 2
        assert owner_detect.json()["total"] == 2

        admin_detect = _observe_http(
            lambda: client.post(
                "/api/admin/resolutions/detect",
                json={"user_id": LEGACY_OWNER_USER_ID},
                headers=owner,
            ),
            [
                {
                    "action": "admin.entity_resolution.detect",
                    "user_id": LEGACY_OWNER_USER_ID,
                    "target_type": "user",
                    "target_id": LEGACY_OWNER_USER_ID,
                }
            ],
        )
        assert admin_detect.status_code == 200, admin_detect.text
        assert "stopped_at" not in admin_detect.text
        assert admin_detect.json() == {
            "user_id": LEGACY_OWNER_USER_ID,
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 2,
            "keys_examined": 1,
            "keys_pending": 1,
            "suggested": 0,
            "pending_total": 2,
            "sweeps": 0,
            "partial": True,
            "resumed": False,
            "complete": False,
        }
        detect_after = _parse_after(_audit_ordered()[-1])
        assert detect_after == {"private_fields_count": 11}

        accepted, before_accept, accept_delta, accept_window = _observe_mutation(
            lambda: client.post(
                f"/api/kg/resolutions/{personal.id}/accept",
                json={"target_entity_id": second["id"]},
                headers=owner,
            ),
            [
                {
                    "action": "entity.merge",
                    "user_id": LEGACY_OWNER_USER_ID,
                    "target_type": "resolution",
                    "target_id": _opaque_target("resolution", personal.id),
                }
            ],
        )
        assert accepted.status_code == 200, accepted.text
        after_accept = _selected_state()
        new_history = [
            row
            for row in after_accept["history"]
            if row["id"] not in {item["id"] for item in before_accept["history"]}
        ]
        assert len(new_history) == 1
        merge_row = new_history[0]
        merge_id = merge_row["id"]
        assert merge_row["source_entity_id"] == first["id"]
        assert merge_row["target_entity_id"] == second["id"]
        assert merge_row["merged_by"] == LEGACY_OWNER_USER_ID
        assert merge_row["user_id"] == LEGACY_OWNER_USER_ID
        assert merge_row["undone_at"] is None
        assert merge_row["undone_by"] is None
        merge_ts = merge_row["created_at"]
        merge_dt = _parse_merge_ts(merge_ts)
        _in_call_window(merge_dt, accept_window["start"], accept_window["end"])
        transfer = json.loads(merge_row["transfer_json"])
        assert set(transfer) == {
            "relation_transfer_version",
            "links_moved",
            "links_suppressed",
            "primary_moved",
            "relations",
            "relation_reference_rewrites",
            "closed_candidates",
            "time_moved",
            "time_target_created",
        }
        assert transfer["relation_transfer_version"] == 2
        assert transfer["links_suppressed"] == []
        assert transfer["primary_moved"] == expected_personal_primary_moved
        assert transfer["relation_reference_rewrites"] == []
        assert transfer["time_moved"] == []
        assert transfer["time_target_created"] is False
        assert transfer["closed_candidates"] == [personal.id]
        assert len(transfer["links_moved"]) == 1
        assert len(transfer["relations"]) == 1
        assert accepted.json() == {
            "result": {
                "merge_id": merge_id,
                "source_entity_id": first["id"],
                "target_entity_id": second["id"],
                "merged_into": _public_person(second["id"], "Вторая карточка"),
            }
        }
        assert len(accept_delta) == 1
        _closed_merge_after(
            accept_delta[0],
            merge_id=merge_id,
            source_id=first["id"],
            target_id=second["id"],
        )

        before_ents = _by_id(before_accept["entities"])
        after_ents = _by_id(after_accept["entities"])
        assert before_ents[first["id"]] == original_first
        assert before_ents[second["id"]] == original_second
        personal_sql = _by_id(after_accept["candidates"])[personal.id]
        first_sql = after_ents[first["id"]]
        second_sql = after_ents[second["id"]]
        expected_personal_merged = _row_with(
            original_personal,
            status="merged",
            resolved_by=LEGACY_OWNER_USER_ID,
            resolved_at=personal_sql["resolved_at"],
        )
        _assert_table_preserved(
            before_accept["candidates"],
            after_accept["candidates"],
            permitted={personal.id: {"status", "resolved_by", "resolved_at"}},
        )
        assert personal_sql["status"] == "merged"
        assert personal_sql["resolved_by"] == LEGACY_OWNER_USER_ID
        resolved_dt = _parse_candidate_r(personal_sql["resolved_at"])
        assert _floor_seconds(accept_window["start"]) <= resolved_dt <= _floor_seconds(accept_window["end"])
        assert personal_sql == expected_personal_merged
        expected_first_merged = _row_with(
            original_first,
            canonical=0,
            merged_into_id=second["id"],
            deleted_at=merge_ts,
            updated_at=merge_ts,
        )
        expected_second_merged = _row_with(
            original_second,
            aliases_json=expected_personal_target_aliases,
            version=int(original_second["version"]) + 1,
            updated_at=merge_ts,
        )
        assert json.loads(second_sql["aliases_json"]) == [
            "x" * 9000 + sentinel,
            "Первая карточка",
        ]
        assert second_sql["aliases_json"] == expected_personal_target_aliases
        assert first_sql == expected_first_merged
        assert second_sql == expected_second_merged
        _assert_table_preserved(
            before_accept["entities"],
            after_accept["entities"],
            permitted={
                first["id"]: {"canonical", "merged_into_id", "deleted_at", "updated_at"},
                second["id"]: {"updated_at", "version", "aliases_json"},
            },
        )
        assert int(first_sql["canonical"]) == 0
        assert first_sql["merged_into_id"] == second["id"]
        assert first_sql["deleted_at"] == merge_ts
        assert first_sql["updated_at"] == merge_ts
        assert int(first_sql["version"]) == int(original_first["version"])
        assert first_sql["aliases_json"] == original_first["aliases_json"]
        assert first_sql["metadata_json"] == original_first["metadata_json"]
        assert first_sql["name"] == "Первая карточка"
        assert int(second_sql["canonical"]) == 1
        assert second_sql["merged_into_id"] in (None, "")
        _bound_version(second_sql["version"], original_second["version"])
        assert second_sql["updated_at"] == merge_ts
        for foreign_entity_id in (
            foreign_left["id"],
            foreign_right["id"],
            third["id"],
            fourth["id"],
            anchor["id"],
        ):
            assert after_ents[foreign_entity_id] == before_ents[foreign_entity_id]
        expected_relation_moved = _row_with(original_relation, source_entity_id=second["id"])
        rel_sql = _by_id(after_accept["relations"])[relation.id]
        assert rel_sql == expected_relation_moved
        _assert_table_preserved(
            before_accept["relations"],
            after_accept["relations"],
            permitted={relation.id: {"source_entity_id"}},
        )
        assert rel_sql["source_entity_id"] == second["id"]
        assert rel_sql["target_entity_id"] == anchor["id"]
        _moved_link(
            before_accept["links"],
            after_accept["links"],
            knowledge_id=knowledge.id,
            from_entity=first["id"],
            to_entity=second["id"],
        )
        assert len(after_accept["links"]) == 1
        new_target_link = after_accept["links"][0]
        assert new_target_link["id"] != original_links[0]["id"]
        assert transfer["links_moved"] == [
            {**_json_roundtrip(original_links[0]), "target_link_id": new_target_link["id"]}
        ]
        assert transfer["relations"] == [
            {
                "original": _json_roundtrip(original_relation),
                "fate": "moved",
                "rewritten": {
                    "id": relation.id,
                    "source_entity_id": second["id"],
                    "target_entity_id": anchor["id"],
                },
            }
        ]
        source_snapshot = json.loads(merge_row["source_snapshot_json"])
        target_before_snapshot = json.loads(merge_row["target_before_json"])
        target_after_snapshot = json.loads(merge_row["target_after_json"])
        assert source_snapshot == _json_roundtrip(original_first)
        assert target_before_snapshot == _json_roundtrip(original_second)
        assert target_after_snapshot == _json_roundtrip(expected_second_merged)
        assert merge_row["user_id"] == LEGACY_OWNER_USER_ID
        _assert_table_preserved(
            before_accept["history"],
            after_accept["history"],
            allow_new={merge_id},
        )
        assert _opaque_history(after_accept["history"]) == _opaque_history(before_accept["history"])
        admin_still = _by_id(after_accept["candidates"])[admin_candidate.id]
        foreign_still = _by_id(after_accept["candidates"])[foreign_candidate.id]
        assert admin_still == original_admin_candidate
        assert admin_still["status"] == "suggested"
        assert foreign_still["status"] == "suggested"
        assert int(after_ents[third["id"]]["canonical"]) == 1
        assert int(after_ents[fourth["id"]]["canonical"]) == 1

        replay_accept = _observe_http(
            lambda: client.post(
                f"/api/kg/resolutions/{personal.id}/accept",
                json={"target_entity_id": second["id"]},
                headers=owner,
            ),
            [],
        )
        assert replay_accept.status_code == 400, replay_accept.text
        assert replay_accept.json() == {
            "detail": "Resolution candidate was not found or is no longer pending"
        }
        assert _sql_row("entity_resolution_candidates", personal.id)["status"] == "merged"

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
                                "closed_candidates": [personal.id],
                            }
                        ),
                        "owner",
                        f"2026-01-01T00:00:{index % 60:02}Z",
                    )
                    for index in range(205)
                ],
            )

        history_card = _history_card(merge_id)
        assert history_card["id"] == merge_id
        assert history_card["source_entity_id"] == first["id"]
        assert history_card["target_entity_id"] == second["id"]
        assert history_card["undoable"] is True
        assert history_card["undone_at"] == ""
        assert history_card["links_moved_count"] == 1
        assert history_card["relations_count"] == 1
        assert history_card["candidates_closed_count"] == 1
        owner_merges = _observe_http(
            lambda: client.get("/api/kg/merges", params={"limit": 100}, headers=owner),
            [],
        )
        assert owner_merges.status_code == 200, owner_merges.text
        assert owner_merges.json() == _history_envelope([history_card], admin=False)
        assert owner_merges.json()["items"] == [history_card]
        admin_merges = _observe_http(
            lambda: client.get(
                "/api/admin/merges",
                params={"user_id": LEGACY_OWNER_USER_ID, "limit": 500},
                headers=owner,
            ),
            [],
        )
        assert admin_merges.status_code == 200, admin_merges.text
        assert admin_merges.json() == _history_envelope([history_card], admin=True)
        assert admin_merges.json()["items"] == [history_card]
        foreign_merges = _observe_http(
            lambda: client.get(
                "/api/admin/merges",
                params={"user_id": LEGACY_OWNER_USER_ID, "limit": 50},
                headers=foreign,
            ),
            [
                {
                    "action": "admin.merges.read",
                    "user_id": foreign_id,
                    "target_type": "user",
                    "target_id": LEGACY_OWNER_USER_ID,
                }
            ],
        )
        assert foreign_merges.status_code == 200
        assert foreign_merges.json() == _history_envelope([history_card], admin=True)
        assert foreign_merges.json()["items"] == [history_card]

        undone, before_undo, undo_delta, undo_window = _observe_mutation(
            lambda: client.post(f"/api/kg/merges/{merge_id}/undo", headers=owner),
            [
                {
                    "action": "entity.unmerge",
                    "user_id": LEGACY_OWNER_USER_ID,
                    "target_type": "merge",
                    "target_id": merge_id,
                }
            ],
        )
        assert undone.status_code == 200, undone.text
        after_undo = _selected_state()
        restored_hist = _by_id(after_undo["history"])[merge_id]
        undone_at = restored_hist["undone_at"]
        undo_dt = _parse_merge_ts(undone_at)
        _in_call_window(undo_dt, undo_window["start"], undo_window["end"])
        assert undo_dt > merge_dt
        assert restored_hist["undone_by"] == LEGACY_OWNER_USER_ID
        assert undone.json() == {
            "result": {
                "merge_id": merge_id,
                "source_entity_id": first["id"],
                "target_entity_id": second["id"],
                "source": _public_person(first["id"], "Первая карточка"),
                "target": _public_person(second["id"], "Вторая карточка"),
                "undone_at": undone_at,
            }
        }
        assert len(undo_delta) == 1
        _closed_merge_after(
            undo_delta[0],
            merge_id=merge_id,
            source_id=first["id"],
            target_id=second["id"],
            undone_at=undone_at,
            source_fields=13,
        )
        parsed_undo_audit = _parse_json_field(undo_delta[0].get("after_json")) or {}
        assert parsed_undo_audit.get("undone_at") == undone_at
        assert _history_card(merge_id)["undone_at"] == undone_at
        assert _history_card(merge_id)["undoable"] is False
        expected_hist_undone = _row_with(
            merge_row,
            undone_at=undone_at,
            undone_by=LEGACY_OWNER_USER_ID,
        )
        assert restored_hist == expected_hist_undone

        restored_personal = _by_id(after_undo["candidates"])[personal.id]
        restored_first = _by_id(after_undo["entities"])[first["id"]]
        restored_second = _by_id(after_undo["entities"])[second["id"]]
        assert restored_personal["resolved_at"] is None
        assert restored_personal["resolved_by"] is None
        expected_personal_restored = _row_with(original_personal)
        assert restored_personal == expected_personal_restored
        _assert_table_preserved(
            before_undo["candidates"],
            after_undo["candidates"],
            permitted={personal.id: {"status", "resolved_by", "resolved_at"}},
        )
        assert restored_personal["status"] == "suggested"
        assert restored_personal["resolved_by"] in (None, "")
        assert restored_personal["resolved_at"] in (None, "")
        assert (
            restored_personal["evidence_json"]
            == _by_id(before_accept["candidates"])[personal.id]["evidence_json"]
        )
        expected_first_restored = _row_with(original_first, updated_at=undone_at)
        expected_second_restored = _row_with(
            original_second,
            version=int(original_second["version"]) + 2,
            updated_at=undone_at,
        )
        assert restored_first == expected_first_restored
        assert restored_second == expected_second_restored
        _assert_table_preserved(
            before_undo["entities"],
            after_undo["entities"],
            permitted={
                first["id"]: {
                    "canonical",
                    "merged_into_id",
                    "deleted_at",
                    "updated_at",
                    "aliases_json",
                    "metadata_json",
                },
                second["id"]: {"updated_at", "version", "aliases_json"},
            },
        )
        assert restored_second["aliases_json"] == original_second["aliases_json"] == "[]"
        assert restored_first["aliases_json"] == original_first["aliases_json"]
        assert restored_first["metadata_json"] == original_first["metadata_json"]
        assert restored_first["name"] == original_first["name"] == "Первая карточка"
        assert restored_first["description"] == original_first["description"]
        assert int(restored_first["canonical"]) == 1
        assert restored_first["merged_into_id"] in (None, "")
        assert restored_first["deleted_at"] in (None, "")
        assert restored_first["updated_at"] == undone_at
        assert int(restored_first["version"]) == int(original_first["version"])
        assert restored_second["aliases_json"] == original_second["aliases_json"]
        assert restored_second["metadata_json"] == original_second["metadata_json"]
        assert restored_second["name"] == original_second["name"]
        assert int(restored_second["canonical"]) == 1
        assert restored_second["merged_into_id"] in (None, "")
        _bound_version(
            restored_second["version"],
            _by_id(before_undo["entities"])[second["id"]]["version"],
        )
        assert _by_id(after_undo["relations"])[relation.id] == original_relation
        _assert_table_preserved(
            before_undo["relations"],
            after_undo["relations"],
            permitted={relation.id: {"source_entity_id"}},
        )
        assert _by_id(after_undo["relations"])[relation.id]["source_entity_id"] == first["id"]
        assert after_undo["links"] == before_accept["links"] == original_links
        orig_link = before_accept["links"][0]
        restored_link = after_undo["links"][0]
        assert restored_link["id"] == orig_link["id"]
        for key, value in orig_link.items():
            assert restored_link[key] == value
        assert len(_opaque_history(after_undo["history"])) == 205
        assert _opaque_history(after_undo["history"]) == _opaque_history(before_undo["history"])
        _assert_table_preserved(
            before_undo["history"],
            after_undo["history"],
            permitted={merge_id: {"undone_at", "undone_by"}},
        )
        for foreign_entity_id in (
            foreign_left["id"],
            foreign_right["id"],
            third["id"],
            fourth["id"],
            anchor["id"],
        ):
            assert (
                _by_id(after_undo["entities"])[foreign_entity_id]
                == _by_id(before_undo["entities"])[foreign_entity_id]
            )
        assert _by_id(after_undo["candidates"])[admin_candidate.id] == original_admin_candidate
        assert _by_id(after_undo["candidates"])[admin_candidate.id]["status"] == "suggested"
        assert _by_id(after_undo["entities"])[third["id"]]["name"] == "Третья карточка"
        assert _by_id(after_undo["entities"])[fourth["id"]]["name"] == "Четвёртая карточка"

        replay_undo = _observe_http(
            lambda: client.post(f"/api/kg/merges/{merge_id}/undo", headers=owner),
            [],
        )
        assert replay_undo.status_code == 400, replay_undo.text
        assert replay_undo.json() == {"detail": "Merge has already been undone"}
        assert _sql_row("entity_merge_history", merge_id)["undone_at"] == undone_at

        user_accept = _observe_http(
            lambda: client.post(
                f"/api/kg/resolutions/{admin_candidate.id}/accept",
                json={"target_entity_id": fourth["id"]},
                headers=user,
            ),
            [],
        )
        assert user_accept.status_code == 403
        assert user_accept.json() == {"detail": "Access denied for kg.merge (default_deny)"}

        guest_accept = _observe_http(
            lambda: client.post(
                f"/api/kg/resolutions/{admin_candidate.id}/accept",
                json={"target_entity_id": fourth["id"]},
                headers=guest,
            ),
            [],
        )
        assert guest_accept.status_code == 403
        assert guest_accept.json() == {"detail": "Access denied for kg.merge (default_deny)"}

        foreign_kg_accept = _observe_http(
            lambda: client.post(
                f"/api/kg/resolutions/{admin_candidate.id}/accept",
                json={"target_entity_id": fourth["id"]},
                headers=foreign,
            ),
            [],
        )
        assert foreign_kg_accept.status_code == 400
        assert foreign_kg_accept.json() == {
            "detail": "Resolution candidate was not found or is no longer pending"
        }

        foreign_admin_accept = _observe_http(
            lambda: client.post(
                f"/api/admin/resolutions/{admin_candidate.id}/accept",
                json={
                    "user_id": LEGACY_OWNER_USER_ID,
                    "target_entity_id": fourth["id"],
                },
                headers=foreign,
            ),
            [],
        )
        assert foreign_admin_accept.status_code == 403
        assert foreign_admin_accept.json() == {
            "detail": "Только владелец может изменять учётную запись владельца"
        }

        anon_accept = _observe_http(
            lambda: client.post(
                f"/api/kg/resolutions/{admin_candidate.id}/accept",
                json={"target_entity_id": fourth["id"]},
            ),
            [
                {
                    "action": "auth.failed",
                    "user_id": "anonymous",
                    "target_type": "auth",
                    "target_id": "invalid_credentials",
                }
            ],
        )
        assert anon_accept.status_code == 401
        assert anon_accept.json() == {"detail": "Missing authentication"}
        anon_accept_delta = _audit_ordered()[-1]
        assert json.loads(anon_accept_delta["after_json"]) == {
            "method_chars": 4,
            "path_chars": len(f"/api/kg/resolutions/{admin_candidate.id}/accept"),
            "reason": "invalid_credentials",
            "status_present": True,
        }

        assert _sql_row("entity_resolution_candidates", admin_candidate.id)["status"] == "suggested"

        admin_accepted, before_admin, admin_delta, admin_window = _observe_mutation(
            lambda: client.post(
                f"/api/admin/resolutions/{admin_candidate.id}/accept",
                json={"user_id": LEGACY_OWNER_USER_ID, "target_entity_id": fourth["id"]},
                headers=owner,
            ),
            [
                {
                    "action": "admin.entity.merge",
                    "user_id": LEGACY_OWNER_USER_ID,
                    "target_type": "resolution",
                    "target_id": _opaque_target("resolution", admin_candidate.id),
                }
            ],
        )
        assert admin_accepted.status_code == 200, admin_accepted.text
        after_admin = _selected_state()
        new_admin_history = [
            row
            for row in after_admin["history"]
            if row["id"] not in {item["id"] for item in before_admin["history"]}
        ]
        assert len(new_admin_history) == 1
        admin_merge_row = new_admin_history[0]
        admin_merge_id = admin_merge_row["id"]
        assert admin_merge_row["source_entity_id"] == third["id"]
        assert admin_merge_row["target_entity_id"] == fourth["id"]
        assert admin_merge_row["merged_by"] == LEGACY_OWNER_USER_ID
        assert admin_merge_row["user_id"] == LEGACY_OWNER_USER_ID
        assert admin_merge_row["undone_at"] is None
        assert admin_merge_row["undone_by"] is None
        merge_ts_admin = admin_merge_row["created_at"]
        merge_dt_admin = _parse_merge_ts(merge_ts_admin)
        _in_call_window(merge_dt_admin, admin_window["start"], admin_window["end"])
        admin_transfer = json.loads(admin_merge_row["transfer_json"])
        assert set(admin_transfer) == {
            "relation_transfer_version",
            "links_moved",
            "links_suppressed",
            "primary_moved",
            "relations",
            "relation_reference_rewrites",
            "closed_candidates",
            "time_moved",
            "time_target_created",
        }
        assert admin_transfer["relation_transfer_version"] == 2
        assert admin_transfer["links_moved"] == []
        assert admin_transfer["links_suppressed"] == []
        assert admin_transfer["primary_moved"] == expected_admin_primary_moved
        assert admin_transfer["relations"] == []
        assert admin_transfer["relation_reference_rewrites"] == []
        assert admin_transfer["closed_candidates"] == [admin_candidate.id]
        assert admin_transfer["time_moved"] == []
        assert admin_transfer["time_target_created"] is False
        assert admin_accepted.json() == {
            "entity": {
                "merge_id": admin_merge_id,
                "source_entity_id": third["id"],
                "target_entity_id": fourth["id"],
                "merged_into": _public_person(fourth["id"], "Четвёртая карточка"),
            }
        }
        assert len(admin_delta) == 1
        _closed_merge_after(
            admin_delta[0],
            merge_id=admin_merge_id,
            source_id=third["id"],
            target_id=fourth["id"],
        )
        before_admin_ents = _by_id(before_admin["entities"])
        after_admin_ents = _by_id(after_admin["entities"])
        assert before_admin_ents[third["id"]] == original_third
        assert before_admin_ents[fourth["id"]] == original_fourth
        assert _by_id(before_admin["candidates"])[admin_candidate.id] == original_admin_candidate
        admin_sql = _by_id(after_admin["candidates"])[admin_candidate.id]
        third_sql = after_admin_ents[third["id"]]
        fourth_sql = after_admin_ents[fourth["id"]]
        expected_admin_merged = _row_with(
            original_admin_candidate,
            status="merged",
            resolved_by=LEGACY_OWNER_USER_ID,
            resolved_at=admin_sql["resolved_at"],
        )
        _assert_table_preserved(
            before_admin["candidates"],
            after_admin["candidates"],
            permitted={admin_candidate.id: {"status", "resolved_by", "resolved_at"}},
        )
        assert admin_sql["status"] == "merged"
        assert admin_sql["resolved_by"] == LEGACY_OWNER_USER_ID
        admin_resolved_dt = _parse_candidate_r(admin_sql["resolved_at"])
        assert (
            _floor_seconds(admin_window["start"]) <= admin_resolved_dt <= _floor_seconds(admin_window["end"])
        )
        assert admin_sql == expected_admin_merged
        expected_third_merged = _row_with(
            original_third,
            canonical=0,
            merged_into_id=fourth["id"],
            deleted_at=merge_ts_admin,
            updated_at=merge_ts_admin,
        )
        expected_fourth_merged = _row_with(
            original_fourth,
            aliases_json=expected_admin_target_aliases,
            version=int(original_fourth["version"]) + 1,
            updated_at=merge_ts_admin,
        )
        assert json.loads(fourth_sql["aliases_json"]) == ["Третья карточка"]
        assert fourth_sql["aliases_json"] == expected_admin_target_aliases
        assert third_sql == expected_third_merged
        assert fourth_sql == expected_fourth_merged
        _assert_table_preserved(
            before_admin["entities"],
            after_admin["entities"],
            permitted={
                third["id"]: {"canonical", "merged_into_id", "deleted_at", "updated_at"},
                fourth["id"]: {"updated_at", "version", "aliases_json"},
            },
        )
        assert third_sql["merged_into_id"] == fourth["id"]
        assert int(third_sql["canonical"]) == 0
        assert int(fourth_sql["canonical"]) == 1
        assert third_sql["aliases_json"] == original_third["aliases_json"]
        assert third_sql["metadata_json"] == original_third["metadata_json"]
        assert third_sql["deleted_at"] == merge_ts_admin
        assert third_sql["updated_at"] == merge_ts_admin
        assert fourth_sql["updated_at"] == merge_ts_admin
        _bound_version(fourth_sql["version"], original_fourth["version"])
        source_snapshot_admin = json.loads(admin_merge_row["source_snapshot_json"])
        target_before_snapshot_admin = json.loads(admin_merge_row["target_before_json"])
        target_after_snapshot_admin = json.loads(admin_merge_row["target_after_json"])
        assert source_snapshot_admin == _json_roundtrip(original_third)
        assert target_before_snapshot_admin == _json_roundtrip(original_fourth)
        assert target_after_snapshot_admin == _json_roundtrip(expected_fourth_merged)
        assert _opaque_history(after_admin["history"]) == _opaque_history(before_admin["history"])
        assert len(_opaque_history(after_admin["history"])) == 205
        _assert_table_preserved(
            before_admin["history"],
            after_admin["history"],
            allow_new={admin_merge_id},
        )
        _assert_table_preserved(before_admin["relations"], after_admin["relations"])
        _assert_table_preserved(before_admin["links"], after_admin["links"])
        assert _by_id(after_admin["candidates"])[personal.id] == original_personal
        assert _by_id(after_admin["candidates"])[personal.id]["status"] == "suggested"
        assert _by_id(after_admin["entities"])[first["id"]]["aliases_json"] == original_first["aliases_json"]
        assert _by_id(after_admin["entities"])[second["id"]]["name"] == original_second["name"]
        for foreign_entity_id in (foreign_left["id"], foreign_right["id"], anchor["id"]):
            assert after_admin_ents[foreign_entity_id] == before_admin_ents[foreign_entity_id]

        replay_admin_accept = _observe_http(
            lambda: client.post(
                f"/api/admin/resolutions/{admin_candidate.id}/accept",
                json={"user_id": LEGACY_OWNER_USER_ID, "target_entity_id": fourth["id"]},
                headers=owner,
            ),
            [],
        )
        assert replay_admin_accept.status_code == 400
        assert replay_admin_accept.json() == {
            "detail": "Resolution candidate was not found or is no longer pending"
        }

        admin_undone, before_admin_undo, admin_undo_delta, admin_undo_window = _observe_mutation(
            lambda: client.post(
                f"/api/admin/merges/{admin_merge_id}/undo",
                json={"user_id": LEGACY_OWNER_USER_ID},
                headers=owner,
            ),
            [
                {
                    "action": "admin.entity.unmerge",
                    "user_id": LEGACY_OWNER_USER_ID,
                    "target_type": "merge",
                    "target_id": admin_merge_id,
                }
            ],
        )
        assert admin_undone.status_code == 200, admin_undone.text
        after_admin_undo = _selected_state()
        admin_hist = _by_id(after_admin_undo["history"])[admin_merge_id]
        admin_undone_at = admin_hist["undone_at"]
        admin_undo_dt = _parse_merge_ts(admin_undone_at)
        _in_call_window(admin_undo_dt, admin_undo_window["start"], admin_undo_window["end"])
        assert admin_undo_dt > merge_dt_admin
        assert admin_hist["undone_by"] == LEGACY_OWNER_USER_ID
        assert admin_undone.json() == {
            "result": {
                "merge_id": admin_merge_id,
                "source_entity_id": third["id"],
                "target_entity_id": fourth["id"],
                "source": _public_person(third["id"], "Третья карточка"),
                "target": _public_person(fourth["id"], "Четвёртая карточка"),
                "undone_at": admin_undone_at,
            }
        }
        assert len(admin_undo_delta) == 1
        _closed_merge_after(
            admin_undo_delta[0],
            merge_id=admin_merge_id,
            source_id=third["id"],
            target_id=fourth["id"],
            undone_at=admin_undone_at,
            source_fields=13,
        )
        parsed_admin_undo_audit = _parse_json_field(admin_undo_delta[0].get("after_json")) or {}
        assert parsed_admin_undo_audit.get("undone_at") == admin_undone_at
        assert _history_card(admin_merge_id)["undone_at"] == admin_undone_at
        expected_admin_hist_undone = _row_with(
            admin_merge_row,
            undone_at=admin_undone_at,
            undone_by=LEGACY_OWNER_USER_ID,
        )
        assert admin_hist == expected_admin_hist_undone
        restored_admin = _by_id(after_admin_undo["candidates"])[admin_candidate.id]
        restored_third = _by_id(after_admin_undo["entities"])[third["id"]]
        restored_fourth = _by_id(after_admin_undo["entities"])[fourth["id"]]
        assert restored_admin["resolved_at"] is None
        assert restored_admin["resolved_by"] is None
        expected_admin_restored = _row_with(original_admin_candidate)
        assert restored_admin == expected_admin_restored
        _assert_table_preserved(
            before_admin_undo["candidates"],
            after_admin_undo["candidates"],
            permitted={admin_candidate.id: {"status", "resolved_by", "resolved_at"}},
        )
        assert restored_admin["status"] == "suggested"
        assert restored_admin["resolved_by"] in (None, "")
        assert restored_admin["resolved_at"] in (None, "")
        expected_third_restored = _row_with(original_third, updated_at=admin_undone_at)
        expected_fourth_restored = _row_with(
            original_fourth,
            version=int(original_fourth["version"]) + 2,
            updated_at=admin_undone_at,
        )
        assert restored_third == expected_third_restored
        assert restored_fourth == expected_fourth_restored
        assert restored_fourth["aliases_json"] == original_fourth["aliases_json"] == "[]"
        _assert_table_preserved(
            before_admin_undo["entities"],
            after_admin_undo["entities"],
            permitted={
                third["id"]: {"canonical", "merged_into_id", "deleted_at", "updated_at"},
                fourth["id"]: {"updated_at", "version", "aliases_json"},
            },
        )
        assert restored_third["aliases_json"] == original_third["aliases_json"]
        assert restored_third["metadata_json"] == original_third["metadata_json"]
        assert restored_third["name"] == original_third["name"] == "Третья карточка"
        assert int(restored_third["canonical"]) == 1
        assert restored_third["merged_into_id"] in (None, "")
        assert restored_third["deleted_at"] in (None, "")
        assert restored_third["updated_at"] == admin_undone_at
        assert int(restored_third["version"]) == int(original_third["version"])
        assert restored_fourth["name"] == original_fourth["name"]
        assert int(restored_fourth["canonical"]) == 1
        assert restored_fourth["updated_at"] == admin_undone_at
        _bound_version(
            restored_fourth["version"],
            _by_id(before_admin_undo["entities"])[fourth["id"]]["version"],
        )
        assert _opaque_history(after_admin_undo["history"]) == _opaque_history(before_admin_undo["history"])
        assert len(_opaque_history(after_admin_undo["history"])) == 205
        _assert_table_preserved(
            before_admin_undo["history"],
            after_admin_undo["history"],
            permitted={admin_merge_id: {"undone_at", "undone_by"}},
        )
        _assert_table_preserved(before_admin_undo["relations"], after_admin_undo["relations"])
        _assert_table_preserved(before_admin_undo["links"], after_admin_undo["links"])
        assert _by_id(after_admin_undo["history"])[merge_id]["undone_at"] == undone_at
        assert (
            _by_id(after_admin_undo["entities"])[first["id"]]["aliases_json"]
            == original_first["aliases_json"]
        )

        replay_admin_undo = _observe_http(
            lambda: client.post(
                f"/api/admin/merges/{admin_merge_id}/undo",
                json={"user_id": LEGACY_OWNER_USER_ID},
                headers=owner,
            ),
            [],
        )
        assert replay_admin_undo.status_code == 400
        assert replay_admin_undo.json() == {"detail": "Merge has already been undone"}

        limit0 = _observe_http(
            lambda: client.get("/api/kg/resolutions?limit=0", headers=owner),
            [],
        )
        assert limit0.status_code == 422
        assert limit0.json() == {
            "detail": [
                {
                    "type": "greater_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be greater than or equal to 1",
                    "input": "0",
                    "ctx": {"ge": 1},
                }
            ]
        }

        limit501 = _observe_http(
            lambda: client.get("/api/kg/resolutions?limit=501", headers=owner),
            [],
        )
        assert limit501.status_code == 422
        assert limit501.json() == {
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be less than or equal to 500",
                    "input": "501",
                    "ctx": {"le": 500},
                }
            ]
        }

        merges0 = _observe_http(
            lambda: client.get("/api/kg/merges?limit=0", headers=owner),
            [],
        )
        assert merges0.status_code == 422
        assert merges0.json() == {
            "detail": [
                {
                    "type": "greater_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be greater than or equal to 1",
                    "input": "0",
                    "ctx": {"ge": 1},
                }
            ]
        }

        merges101 = _observe_http(
            lambda: client.get("/api/kg/merges?limit=101", headers=owner),
            [],
        )
        assert merges101.status_code == 422
        assert merges101.json() == {
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be less than or equal to 100",
                    "input": "101",
                    "ctx": {"le": 100},
                }
            ]
        }

        for row in _audit_ordered():
            _scan_needles(row)
        _scan_needles(json.dumps(_audit_ordered(), ensure_ascii=False))


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
