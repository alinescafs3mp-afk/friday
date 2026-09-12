"""User mission start and research queue through actual HTTP/storage services."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Mission, MissionTask, RawObject, new_id
from tests.test_api_tokens import _issue

ALICE = "actions092-alice"
BOB = "actions092-bob"
BODY = "Research result\nAn ordinary observation remains a proposal for review."
STAMP = "2026-01-01T00:00:00+00:00"
DATA = ("raw_objects", "inbox", "knowledge_objects", "entities", "knowledge_entity_links", "messages")
MISSION_TABLES = ("missions", "mission_tasks")


def _rows(store, table):
    assert table in (*DATA, *MISSION_TABLES, "audit_log", "request_idempotency")
    return [dict(row) for row in store.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


def _state(ctx):
    return {
        table: _rows(ctx["store"], table)
        for table in (*DATA, *MISSION_TABLES, "audit_log", "request_idempotency")
    }


def _bounded_time(value, start):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None and start.replace(microsecond=0) <= parsed <= datetime.now(UTC)


def _assert_refusal_state(ctx, before, response, entered):
    after = _state(ctx)
    assert entered == []
    assert all(after[k] == before[k] for k in before if k != "audit_log")
    if response.status_code != 401:
        assert after["audit_log"] == before["audit_log"]
        return
    # HTTP authentication failure is itself a required audit event.
    assert len(after["audit_log"]) == len(before["audit_log"]) + 1
    assert after["audit_log"][:-1] == before["audit_log"]
    audit = after["audit_log"][-1]
    assert audit["action"] == "auth.failed" and audit["user_id"] == "anonymous"
    assert audit["target_type"] == "auth" and audit["before_json"] is None
    assert audit["request_id"] == response.headers["x-request-id"]
    text = json.dumps(audit)
    assert "PRIVATE_GOAL_" not in text and "PRIVATE_OTHER_RESEARCH" not in text
    assert "jrc_actions092_" not in text and BODY not in text


def _mission(ctx, author, *, status="paused", tenant=None):
    store = ctx["store"]
    tenant = tenant or ctx["tenant"]
    mid = new_id("mis")
    store.create_mission(
        Mission(
            id=mid,
            user_id=tenant,
            goal="PRIVATE_GOAL_" + author,
            title="Controlled task",
            status="paused",
            created_by=author,
            error="old blocked reason",
            budget_seconds=120,
            budget_tool_calls=2,
            budget_retries=1,
        )
    )
    tasks = [
        MissionTask(
            id=new_id("mtask"),
            mission_id=mid,
            user_id=tenant,
            seq=i,
            instruction="PRIVATE_INSTRUCTION_" + str(i),
            depends_on_json=[1] if i == 2 else [],
        )
        for i in (1, 2)
    ]
    store.set_mission_plan(mid, tenant, tasks, plan_summary="retained plan", status=status)
    return mid


def _view(row, tasks):
    return {
        **row,
        "tasks": [
            {
                **task,
                "depends_on": json.loads(task["depends_on_json"]),
                "tools_used": json.loads(task["tools_used_json"]),
            }
            for task in tasks
        ],
    }


@pytest.fixture
def action_http(settings, request):
    shared = getattr(request, "param", True)
    current = replace(settings, shared_archive=shared, autonomy_enabled=True, workers_enabled=False)
    assert not current.llm_enabled and not current.embeddings_enabled
    with TestClient(create_app(current), raise_server_exceptions=False) as client:
        store = client.app.state.storage
        headers = {"owner": {"Authorization": f"Bearer {current.api_token}"}, "anonymous": {}}
        for name, person in (("alice", ALICE), ("bob", BOB)):
            secret = "jrc_actions092_" + name
            _issue(store, person, "user", secret)
            headers[name] = {"Authorization": "Bearer " + secret}
        tenant = LEGACY_OWNER_USER_ID if shared else ALICE
        messages = {}
        for name, person in (("alice", ALICE), ("bob", BOB)):
            conversation = store.create_conversation(person, "Personal research")
            messages[name] = store.store_message(
                conversation["id"],
                person,
                "assistant",
                BODY if name == "alice" else "PRIVATE_OTHER_RESEARCH",
                metadata={
                    "interaction_mode": "research",
                    "tools_used": ["web_search"],
                    "knowledge_object_ids": [],
                    "knowledge_citations": {},
                },
            )
        for person in (tenant, BOB):
            store.store_raw_object(
                RawObject(
                    id=new_id("raw"),
                    user_id=person,
                    source="text",
                    source_ref="research-answer:" + messages["alice"]["id"],
                    raw_content=BODY,
                    content_type="text",
                    received_at=STAMP,
                    created_at=STAMP,
                )
            )
        ctx = {
            "client": client,
            "store": store,
            "settings": current,
            "tenant": tenant,
            "headers": headers,
            "messages": messages,
            "shared": shared,
        }
        ctx["decoy"] = _mission(ctx, BOB)
        yield ctx


def _start(ctx, mid, role="alice"):
    return ctx["client"].post(f"/api/missions/{mid}/start", headers=ctx["headers"][role])


def _assert_start(ctx, mid, actor):
    before = _state(ctx)
    old = next(row for row in before["missions"] if row["id"] == mid)
    tasks = [row for row in before["mission_tasks"] if row["mission_id"] == mid]
    start = datetime.now(UTC)
    response = _start(ctx, mid, "owner" if actor == LEGACY_OWNER_USER_ID else "alice")
    assert response.status_code == 200, response.text
    persisted = ctx["store"].get_mission(mid, ctx["tenant"])
    assert persisted is not None
    _bounded_time(persisted["updated_at"], start)
    expected = {
        **old,
        "status": "ready",
        "error": "",
        "version": old["version"] + 1,
        "updated_at": persisted["updated_at"],
    }
    assert persisted == expected and response.json() == {"mission": _view(expected, tasks)}
    after = _state(ctx)
    assert after["missions"] == [expected if row["id"] == mid else row for row in before["missions"]]
    assert after["mission_tasks"] == before["mission_tasks"]
    assert all(after[table] == before[table] for table in (*DATA, "request_idempotency"))
    assert len(after["audit_log"]) == len(before["audit_log"]) + 1
    assert after["audit_log"][:-1] == before["audit_log"]
    audit = after["audit_log"][-1]
    assert (
        audit["action"] == "mission.start" and audit["target_type"] == "mission" and audit["target_id"] == mid
    )
    # The actor is the person who started the mission, including a shared archive.
    assert audit["user_id"] == actor
    assert "PRIVATE_GOAL_" not in json.dumps(audit) and "PRIVATE_INSTRUCTION_" not in json.dumps(audit)
    repeat = _start(ctx, mid, "owner" if actor == LEGACY_OWNER_USER_ID else "alice")
    assert repeat.status_code == 200 and repeat.json() == response.json() and _state(ctx) == after


@pytest.mark.parametrize("action_http", [False, True], indirect=True)
@pytest.mark.parametrize("author_kind", ["person", "agent"])
def test_mission_start_exact_transition_author_audit_and_repeat(action_http, author_kind):
    ctx = action_http
    author = ALICE if author_kind == "person" else "agent:" + ALICE
    _assert_start(ctx, _mission(ctx, author), ALICE)


def test_mission_start_owner_can_control_another_author_in_shared_archive(action_http):
    ctx = action_http
    assert ctx["shared"]
    _assert_start(ctx, _mission(ctx, BOB), LEGACY_OWNER_USER_ID)


@pytest.mark.parametrize("status", ["ready", "running", "completed", "failed", "cancelled"])
def test_mission_start_preserves_already_active_and_terminal_states(action_http, status):
    ctx = action_http
    mid = _mission(ctx, ALICE, status=status)
    before = _state(ctx)
    row = next(row for row in before["missions"] if row["id"] == mid)
    tasks = [row for row in before["mission_tasks"] if row["mission_id"] == mid]
    response = _start(ctx, mid)
    assert response.status_code == 200 and response.json() == {"mission": _view(row, tasks)}
    assert _state(ctx) == before


def test_mission_start_autonomy_disabled_blocks_without_running_a_task(action_http):
    ctx = action_http
    ctx["client"].app.state.executive.settings = replace(ctx["settings"], autonomy_enabled=False)
    mid = _mission(ctx, ALICE)
    before = _state(ctx)
    old = next(row for row in before["missions"] if row["id"] == mid)
    start = datetime.now(UTC)
    response = _start(ctx, mid)
    assert response.status_code == 200, response.text
    after = _state(ctx)
    changed = next(row for row in after["missions"] if row["id"] == mid)
    _bounded_time(changed["updated_at"], start)
    expected = {
        **old,
        "status": "blocked",
        "version": old["version"] + 1,
        "updated_at": changed["updated_at"],
    }
    tasks = [row for row in before["mission_tasks"] if row["mission_id"] == mid]
    assert response.json() == {"mission": _view(expected, tasks)}
    assert after["missions"] == [expected if row["id"] == mid else row for row in before["missions"]]
    assert all(after[k] == before[k] for k in before if k != "missions")


@pytest.mark.parametrize(
    "refusal", ["anonymous", "capability", "foreign-author", "foreign-tenant", "missing"]
)
def test_mission_start_refuses_before_executor_mutation(action_http, monkeypatch, refusal):
    ctx = action_http
    mid = _mission(
        ctx,
        BOB if refusal == "foreign-author" else ALICE,
        tenant=BOB if refusal == "foreign-tenant" else None,
    )
    role, status = "alice", 403
    if refusal == "anonymous":
        role, status = "anonymous", 401
    elif refusal == "capability":
        ctx["store"].set_permission_override(ALICE, "missions.control", "deny")
    elif refusal == "missing":
        mid, status = "mis_" + "0" * 16, 404
    elif refusal == "foreign-tenant":
        status = 404
    entered = []

    async def forbidden(*args, **kwargs):
        entered.append(True)
        raise AssertionError("refused HTTP request reached the executive mutation")

    monkeypatch.setattr(ctx["client"].app.state.executive, "start_mission", forbidden)
    before = _state(ctx)
    response = _start(ctx, mid, role)
    assert response.status_code == status, response.text
    if status == 404:
        assert response.json() == {"detail": "Mission not found"}
    elif refusal == "foreign-author":
        assert response.json() == {
            "detail": "Это чужая миссия. Запустить или остановить её может тот, кто её завёл, или владелец архива."
        }
    else:
        assert "detail" in response.json()
    _assert_refusal_state(ctx, before, response, entered)


@pytest.mark.parametrize("action_http", [False, True], indirect=True)
def test_research_queue_exact_provenance_review_only_and_repeat(action_http):
    ctx = action_http
    message = ctx["messages"]["alice"]
    before = _state(ctx)
    response = ctx["client"].post(
        "/api/research/candidates", headers=ctx["headers"]["alice"], json={"message_id": message["id"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "idempotent_replay",
        "promoted",
        "queued_for_review",
        "action",
        "candidate_type",
        "reason",
        "raw_object_id",
        "inbox_id",
        "suggestions",
    }
    for key, value in {
        "idempotent_replay": False,
        "promoted": False,
        "queued_for_review": True,
        "action": "review",
        "candidate_type": "research",
        "reason": "research synthesis requires explicit review before long-term storage",
    }.items():
        assert body[key] == value
    assert re.fullmatch(r"raw_[0-9a-f]{16}", body["raw_object_id"])
    assert re.fullmatch(r"inbox_[0-9a-f]{16}", body["inbox_id"])
    after = _state(ctx)
    raw = next(row for row in after["raw_objects"] if row["id"] == body["raw_object_id"])
    inbox = next(row for row in after["inbox"] if row["id"] == body["inbox_id"])
    assert after["raw_objects"] == [*before["raw_objects"], raw]
    assert after["inbox"] == [*before["inbox"], inbox]
    assert raw["user_id"] == ctx["tenant"] and raw["source"] == "research"
    assert raw["source_ref"] == "research-answer:" + message["id"]
    assert raw["raw_content"] == BODY and raw["content_hash"] == hashlib.sha256(BODY.encode()).hexdigest()
    meta = json.loads(raw["metadata_json"])
    assert (
        meta["assistant_message_id"] == message["id"]
        and meta["conversation_id"] == message["conversation_id"]
    )
    assert (
        meta["tools_used"] == ["web_search"]
        and meta["knowledge_object_ids"] == []
        and meta["knowledge_citations"] == {}
    )
    assert (
        meta["agent_candidate"] is True
        and meta["review_only"] is True
        and meta["candidate_type"] == "research"
    )
    assert meta["interaction_mode"] == "research" and meta["promotion_assessment"]["action"] == "review"
    assert inbox["user_id"] == ctx["tenant"] and inbox["raw_object_id"] == raw["id"]
    assert inbox["knowledge_object_id"] is None and inbox["status"] == "pending"
    suggestions = body["suggestions"]
    assert set(suggestions) == {
        "title",
        "summary",
        "tags",
        "importance",
        "quality_score",
        "knowledge_kind",
        "entities",
        "metadata",
    }
    assert suggestions["title"] == "Research result"
    assert suggestions["summary"] == "Research result An ordinary observation remains a proposal for review."
    assert json.loads(inbox["suggestions_json"]) == suggestions
    assert json.loads(inbox["suggested_tags_json"]) == suggestions["tags"]
    assert 0 <= inbox["promotion_score"] <= 0.78 and inbox["quality_score"] == suggestions["quality_score"]
    assert all(after[k] == before[k] for k in before if k not in ("raw_objects", "inbox"))
    replay = ctx["client"].post(
        "/api/research/candidates", headers=ctx["headers"]["alice"], json={"message_id": message["id"]}
    )
    expected = {key: value for key, value in body.items() if key != "suggestions"}
    expected.update(
        idempotent_replay=True,
        reason="такое же предложение уже лежит во входящих и ждёт разбора; новой записи не создано",
    )
    assert replay.status_code == 200 and replay.json() == expected and _state(ctx) == after


@pytest.mark.parametrize(
    "refusal",
    [
        "anonymous",
        "capability",
        "missing-id",
        "missing-row",
        "foreign",
        "user-message",
        "dialogue",
        "knowledge-work",
    ],
)
def test_research_queue_refuses_before_ingestion_without_effects(action_http, monkeypatch, refusal):
    ctx = action_http
    message = ctx["messages"]["alice"]
    body, role = {"message_id": message["id"]}, "alice"
    if refusal == "anonymous":
        role, status, detail = "anonymous", 401, None
    elif refusal == "capability":
        ctx["store"].set_permission_override(ALICE, "knowledge.create", "deny")
        status, detail = 403, None
    elif refusal == "missing-id":
        body, status, detail = {}, 400, "message_id is required"
    elif refusal in ("missing-row", "foreign"):
        body = {"message_id": ctx["messages"]["bob"]["id"] if refusal == "foreign" else "msg_" + "0" * 16}
        status, detail = 404, "Assistant message not found"
    else:
        mode = "knowledge_work" if refusal == "knowledge-work" else "dialogue"
        added = ctx["store"].store_message(
            message["conversation_id"],
            ALICE,
            "user" if refusal == "user-message" else "assistant",
            "other answer",
            metadata={"interaction_mode": mode},
        )
        body = {"message_id": added["id"]}
        status = 404 if refusal == "user-message" else 409
        detail = (
            "Assistant message not found"
            if status == 404
            else "Only research-mode answers can be queued here"
            if refusal == "knowledge-work"
            else "Only knowledge_work or research answers can be queued"
        )
    entered = []

    async def forbidden(*args, **kwargs):
        entered.append(True)
        raise AssertionError("refused candidate request reached ingestion")

    monkeypatch.setattr(ctx["client"].app.state.ingestion, "queue_research_candidate", forbidden)
    before = _state(ctx)
    response = ctx["client"].post("/api/research/candidates", headers=ctx["headers"][role], json=body)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail} if detail is not None else "detail" in response.json()
    _assert_refusal_state(ctx, before, response, entered)
