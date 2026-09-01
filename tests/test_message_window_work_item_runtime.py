"""Runtime contract for the first durable ``RecallConversation`` continuation.

The tests use a temporary SQLite database and the real execution kernel.  No
network, model inference, Docker or production state is involved.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import friday.agent_runtime as agent_runtime
from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.interaction_control_plane.runtime_trace import load_trace_namespace_key
from friday.interaction_control_plane.turn_trace import (
    ContinuationKind,
    TraceIdentifierDomain,
    TurnTrace,
    WorkRelation,
    derive_trace_identifier,
)
from friday.interaction_control_plane.work_item_contract import (
    RecallConversationWorkItem,
    RecallMessageRole,
    WorkState,
    WorkTransition,
)
from friday.interaction_control_plane.work_item_store import (
    WorkItemConflictError,
    cancel_recall_conversation_work_item_in_transaction,
    expire_recall_conversation_work_item_in_transaction,
)
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.capability_outcome import load_accepted_capability_outcome_receipt
from friday.permissions import AuthorizationService
from friday.storage import FridayStorage
from friday.storage._conversations import store_message_in_transaction
from friday.web_surfer import WebSurfer

OWNER = "message-work-owner"
FOREIGN = "message-work-foreign"
INITIAL_PROMPT = "Выведи всю переписку за 22 августа"
FOLLOWUP = "А за 21 августа?"
LOCAL_NOW = datetime(2026, 8, 23, 12, 0)
CLARIFICATION = (
    "Не удалось восстановить активный запрос переписки. Повторите полный запрос с нужным периодом."
)


class _ForbiddenModel:
    enabled = True
    model = "forbidden-message-work-model"
    total_budget_sec = 360.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.calls += 1
        raise AssertionError("RecallConversation canary called a model")


class _LegacyModel:
    enabled = True
    model = "synthetic-message-work-legacy-model"
    total_budget_sec = 360.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.calls += 1
        return {
            "content": "Обычный маршрут без продолжения Work Item.",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _stack(
    settings: Any,
    storage: Any,
    *,
    model: Any,
    user_id: str = OWNER,
    timezone_name: str = "Europe/Moscow",
    local_now: datetime = LOCAL_NOW,
) -> SimpleNamespace:
    storage.ensure_user(OWNER, preset_key="user")
    storage.ensure_user(FOREIGN, preset_key="user")
    tuned = replace(
        settings,
        verify_answers=False,
        local_timezone=timezone_name,
    )
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, tuned)
    kernel.bind_services(
        storage,
        graph,
        WebSurfer(tuned),
        IngestionPipeline(tuned, storage, graph),
    )
    runtime = AgentRuntime(tuned, storage, llm=model, kernel=kernel)
    runtime._local_now = lambda: local_now  # type: ignore[method-assign]  # noqa: SLF001
    return SimpleNamespace(
        kernel=kernel,
        model=model,
        runtime=runtime,
        actor=authorization.actor_for_user(user_id, source="message-work-runtime-test"),
    )


def _set_message_time(storage: Any, message_id: str, stamp: str) -> None:
    storage.execute("UPDATE messages SET created_at=? WHERE id=?", (stamp, message_id))
    storage.conn.commit()


def _seed_two_days(storage: Any, conversation_id: str) -> None:
    for role, body, stamp in (
        ("user", "USER-DAY-21", "2026-08-21T08:00:00+00:00"),
        ("assistant", "ASSISTANT-DAY-21", "2026-08-21T09:00:00+00:00"),
        ("user", "USER-DAY-22", "2026-08-22T08:00:00+00:00"),
        ("assistant", "ASSISTANT-DAY-22", "2026-08-22T09:00:00+00:00"),
        ("user", "USER-DAY-23", "2026-08-22T22:00:00+00:00"),
        ("assistant", "ASSISTANT-DAY-23", "2026-08-22T23:00:00+00:00"),
    ):
        row = storage.store_message(conversation_id, OWNER, role, body)
        _set_message_time(storage, str(row["id"]), stamp)


def _record_message_search(kernel: ExecutionKernel) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = kernel.execute

    async def recording_execute(  # noqa: ANN202
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",  # noqa: ANN001
    ):
        if name == "message_search":
            assert execution_scope == "internal"
            calls.append(dict(arguments))
        return await original(name, arguments, actor=actor, execution_scope=execution_scope)

    kernel.execute = recording_execute  # type: ignore[assignment]
    return calls


def _record_tool_calls(kernel: ExecutionKernel) -> list[str]:
    calls: list[str] = []
    original = kernel.execute

    async def recording_execute(  # noqa: ANN202
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",  # noqa: ANN001
    ):
        calls.append(str(name))
        return await original(name, arguments, actor=actor, execution_scope=execution_scope)

    kernel.execute = recording_execute  # type: ignore[assignment]
    return calls


def _work_items(
    storage: Any, conversation_id: str, *, user_id: str = OWNER
) -> list[RecallConversationWorkItem]:
    rows = storage.execute(
        "SELECT * FROM work_items WHERE user_id=? AND conversation_id=? ORDER BY rowid",
        (user_id, conversation_id),
    ).fetchall()
    return [RecallConversationWorkItem.from_storage_row(dict(row)) for row in rows]


def _only_work_item(storage: Any, conversation_id: str) -> RecallConversationWorkItem:
    items = _work_items(storage, conversation_id)
    assert len(items) == 1
    return items[0]


def _stored_receipt(storage: Any, message_id: str, *, user_id: str = OWNER):  # noqa: ANN202
    row = storage.get_message(message_id, user_id)
    assert row is not None
    return load_accepted_capability_outcome_receipt(str(row["metadata_json"]))


def _stored_trace(
    storage: Any,
    message_id: str,
    *,
    user_id: str = OWNER,
) -> tuple[TurnTrace, str]:
    row = storage.get_message(message_id, user_id)
    assert row is not None
    metadata_json = str(row["metadata_json"])
    metadata = json.loads(metadata_json)
    return TurnTrace.parse(metadata["interaction_trace"]), metadata_json


def _expected_work_digest(storage: Any, work_item_id: str) -> str:
    return derive_trace_identifier(
        domain=TraceIdentifierDomain.WORK_ITEM,
        raw_identifier=work_item_id,
        namespace_key=load_trace_namespace_key(storage),
    )


async def _create_active_work(
    settings: Any, storage: Any
) -> tuple[str, dict[str, Any], RecallConversationWorkItem]:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "durable recall")
    conversation_id = str(conversation["id"])
    _seed_two_days(storage, conversation_id)
    calls = _record_message_search(stack.kernel)

    reply = await stack.runtime.chat(
        OWNER,
        INITIAL_PROMPT,
        actor=stack.actor,
        conversation_id=conversation_id,
    )

    assert model.calls == 0
    assert len(calls) == 1
    assert calls[0]["promoted_timezone_name"] == "Europe/Moscow"
    assert "role" not in calls[0]
    assert "USER-DAY-22" in reply["message"]
    assert "ASSISTANT-DAY-22" in reply["message"]
    item = _only_work_item(storage, conversation_id)
    return conversation_id, reply, item


@pytest.mark.asyncio
async def test_initial_exact_window_creates_one_receipt_bound_active_work_item(
    settings: Any,
    storage: Any,
) -> None:
    conversation_id, reply, item = await _create_active_work(settings, storage)
    receipt = _stored_receipt(storage, str(reply["message_id"]))
    trace, metadata_json = _stored_trace(storage, str(reply["message_id"]))

    assert item.state is WorkState.ACTIVE
    assert item.transition is WorkTransition.CREATED
    assert item.revision == 1
    assert item.conversation_id == conversation_id
    assert item.anchor_assistant_message_id == reply["message_id"]
    assert item.active_frame.role is RecallMessageRole.ANY
    assert item.active_frame.timezone_name == "Europe/Moscow"
    assert item.active_frame.since_utc == "2026-08-21T21:00:00+00:00"
    assert item.active_frame.until_utc == "2026-08-22T21:00:00+00:00"
    assert item.accepted_plan_sha256 == receipt.outcome.plan_sha256
    assert item.accepted_outcome_sha256 == receipt.outcome_sha256
    assert trace.work_relation is WorkRelation.NEW
    assert trace.continuation is ContinuationKind.NONE
    assert trace.work_item_digest == _expected_work_digest(storage, item.id)
    assert item.id not in metadata_json


@pytest.mark.asyncio
async def test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update(
    settings: Any,
    storage: Any,
) -> None:
    conversation_id, _initial_reply, initial = await _create_active_work(settings, storage)
    model = _ForbiddenModel()
    reopened = FridayStorage(
        replace(
            settings,
            database_path=storage.settings.database_path,
            database_must_exist=True,
        )
    )
    try:
        restarted = _stack(settings, reopened, model=model)
        calls = _record_message_search(restarted.kernel)

        reply = await restarted.runtime.chat(
            OWNER,
            FOLLOWUP,
            actor=restarted.actor,
            conversation_id=conversation_id,
        )
    finally:
        reopened.close(final=True)

    assert model.calls == 0
    assert len(calls) == 1
    assert calls[0]["promoted_timezone_name"] == "Europe/Moscow"
    assert "role" not in calls[0]
    assert calls[0]["since"] == "2026-08-20T21:00:00+00:00"
    assert calls[0]["until"] == "2026-08-21T21:00:00+00:00"
    assert "USER-DAY-21" in reply["message"]
    assert "ASSISTANT-DAY-21" in reply["message"]

    updated = _only_work_item(storage, conversation_id)
    receipt = _stored_receipt(storage, str(reply["message_id"]))
    trace, metadata_json = _stored_trace(storage, str(reply["message_id"]))
    assert updated.id == initial.id
    assert updated.revision == initial.revision + 1
    assert updated.transition is WorkTransition.CONSTRAINT_UPDATED
    assert updated.active_frame.role is initial.active_frame.role is RecallMessageRole.ANY
    assert updated.active_frame.timezone_name == initial.active_frame.timezone_name == "Europe/Moscow"
    assert updated.active_frame.since_utc == "2026-08-20T21:00:00+00:00"
    assert updated.active_frame.until_utc == "2026-08-21T21:00:00+00:00"
    assert updated.anchor_assistant_message_id == reply["message_id"]
    assert updated.accepted_plan_sha256 == receipt.outcome.plan_sha256
    assert updated.accepted_outcome_sha256 == receipt.outcome_sha256
    assert trace.work_relation is WorkRelation.CONTINUED
    assert trace.continuation is ContinuationKind.CONSTRAINT_UPDATE
    assert trace.work_item_digest == _expected_work_digest(storage, initial.id)
    assert initial.id not in metadata_json


@pytest.mark.asyncio
async def test_relative_followup_uses_retained_frame_timezone(
    settings: Any,
    storage: Any,
) -> None:
    conversation_id, _initial_reply, initial = await _create_active_work(settings, storage)
    model = _ForbiddenModel()
    restarted = _stack(
        settings,
        storage,
        model=model,
        # Settings changed to Los Angeles after the initial Moscow request. At
        # this instant LA is still Aug 22 while the retained frame is Aug 23.
        timezone_name="America/Los_Angeles",
        local_now=datetime(2026, 8, 22, 16, 30),
    )
    calls = _record_message_search(restarted.kernel)

    reply = await restarted.runtime.chat(
        OWNER,
        "А вчера?",
        actor=restarted.actor,
        conversation_id=conversation_id,
    )

    assert model.calls == 0
    assert len(calls) == 1
    assert calls[0]["promoted_timezone_name"] == "Europe/Moscow"
    assert calls[0]["since"] == "2026-08-21T21:00:00+00:00"
    assert calls[0]["until"] == "2026-08-22T21:00:00+00:00"
    assert "USER-DAY-22" in reply["message"]
    assert _only_work_item(storage, conversation_id).id == initial.id


@pytest.mark.asyncio
async def test_explicit_date_reference_is_recognized_before_current_zone_resolution(
    settings: Any,
    storage: Any,
) -> None:
    conversation_id, _initial_reply, initial = await _create_active_work(settings, storage)
    model = _ForbiddenModel()
    restarted = _stack(
        settings,
        storage,
        model=model,
        # In LA Aug 23 is still in the future; in the retained Moscow frame it is today.
        timezone_name="America/Los_Angeles",
        local_now=datetime(2026, 8, 22, 16, 30),
    )
    calls = _record_message_search(restarted.kernel)

    reply = await restarted.runtime.chat(
        OWNER,
        "А за 23 августа?",
        actor=restarted.actor,
        conversation_id=conversation_id,
    )

    assert model.calls == 0
    assert len(calls) == 1
    assert calls[0]["promoted_timezone_name"] == "Europe/Moscow"
    assert calls[0]["since"] == "2026-08-22T21:00:00+00:00"
    assert calls[0]["until"] == "2026-08-23T21:00:00+00:00"
    assert "USER-DAY-23" in reply["message"]
    assert "ASSISTANT-DAY-23" in reply["message"]
    updated = _only_work_item(storage, conversation_id)
    assert updated.id == initial.id
    assert updated.active_frame.timezone_name == "Europe/Moscow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_case",
    [
        "intervening",
        "expired",
        "cancelled",
        "other_conversation",
        "foreign_owner",
        "compound",
        "reply_text",
        "reply_metadata",
        "replay",
    ],
)
async def test_non_immediate_or_non_temporal_continuations_never_read_messages(
    settings: Any,
    storage: Any,
    blocked_case: str,
) -> None:
    conversation_id, _reply, initial = await _create_active_work(settings, storage)
    target_user = OWNER
    target_conversation = conversation_id
    prompt = FOLLOWUP
    kwargs: dict[str, Any] = {}

    if blocked_case == "intervening":
        storage.store_message(conversation_id, OWNER, "user", "INTERVENING-USER")
        storage.store_message(conversation_id, OWNER, "assistant", "INTERVENING-ASSISTANT")
    elif blocked_case == "expired":
        due = datetime.fromisoformat(initial.expires_at) + timedelta(seconds=1)
        with storage.transaction() as conn:
            expire_recall_conversation_work_item_in_transaction(
                conn,
                work_item_id=initial.id,
                user_id=OWNER,
                conversation_id=conversation_id,
                expected_revision=initial.revision,
                now=due.isoformat(),
            )
    elif blocked_case == "cancelled":
        with storage.transaction() as conn:
            cancel_recall_conversation_work_item_in_transaction(
                conn,
                work_item_id=initial.id,
                user_id=OWNER,
                conversation_id=conversation_id,
                expected_revision=initial.revision,
                now=(datetime.fromisoformat(initial.updated_at) + timedelta(seconds=1)).isoformat(),
            )
    elif blocked_case == "other_conversation":
        target_conversation = str(storage.create_conversation(OWNER, "no active work")["id"])
    elif blocked_case == "foreign_owner":
        target_user = FOREIGN
        target_conversation = str(storage.create_conversation(FOREIGN, "foreign work scope")["id"])
    elif blocked_case == "compound":
        prompt = "А за 21 августа и создай заметку?"
    elif blocked_case == "reply_text":
        prompt = "Да, за 21 августа"
    elif blocked_case == "reply_metadata":
        kwargs["reply_to"] = "цитата из предыдущего ответа"
    elif blocked_case == "replay":
        kwargs["replay_source_message_id"] = initial.anchor_user_message_id

    model = _LegacyModel()
    stack = _stack(settings, storage, model=model, user_id=target_user)
    calls = _record_tool_calls(stack.kernel)
    reply = await stack.runtime.chat(
        target_user,
        prompt,
        actor=stack.actor,
        conversation_id=target_conversation,
        **kwargs,
    )

    assert calls == []
    if blocked_case in {
        "intervening",
        "expired",
        "cancelled",
        "other_conversation",
        "foreign_owner",
        "reply_metadata",
        "replay",
    }:
        assert model.calls == 0
        assert reply["message"] == CLARIFICATION
    else:
        assert model.calls > 0
        assert reply["message"] == "Обычный маршрут без продолжения Work Item."
    retained = _only_work_item(storage, conversation_id)
    assert retained.id == initial.id
    assert retained.active_frame.since_utc != "2026-08-20T21:00:00+00:00"
    assert retained.active_frame.until_utc != "2026-08-21T21:00:00+00:00"


@pytest.mark.asyncio
async def test_post_boundary_admission_race_returns_atomic_clarification_without_execution(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, _reply, initial = await _create_active_work(settings, storage)
    model = _LegacyModel()
    stack = _stack(settings, storage, model=model)
    calls = _record_tool_calls(stack.kernel)
    real_get = agent_runtime.get_current_recall_conversation_work_item_in_transaction

    def race_after_boundary(conn, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if kwargs.get("boundary_user_message_id") is not None:
            store_message_in_transaction(
                conn,
                conversation_id,
                OWNER,
                "assistant",
                "CONCURRENT-LATER-ANSWER",
                reply_to=str(kwargs["boundary_user_message_id"]),
            )
            return None
        return real_get(conn, **kwargs)

    monkeypatch.setattr(
        agent_runtime,
        "get_current_recall_conversation_work_item_in_transaction",
        race_after_boundary,
    )

    reply = await stack.runtime.chat(
        OWNER,
        FOLLOWUP,
        actor=stack.actor,
        conversation_id=conversation_id,
    )

    assert model.calls == 0
    assert calls == []
    assert reply["message"] == CLARIFICATION
    stored = storage.get_message(str(reply["message_id"]), OWNER)
    assert stored is not None
    assert stored["content"] == CLARIFICATION
    later = storage.execute(
        """SELECT content FROM messages
             WHERE user_id=? AND conversation_id=? AND content='CONCURRENT-LATER-ANSWER'""",
        (OWNER, conversation_id),
    ).fetchone()
    assert later is not None
    assert _only_work_item(storage, conversation_id) == initial


@pytest.mark.asyncio
async def test_initial_assistant_receipt_and_work_item_creation_roll_back_together(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation_id = str(storage.create_conversation(OWNER, "initial atomicity")["id"])
    _seed_two_days(storage, conversation_id)
    real_create = agent_runtime.create_recall_conversation_work_item_in_transaction

    def fail_after_create(conn, **kwargs):  # noqa: ANN001, ANN003, ANN202
        created = real_create(conn, **kwargs)
        assert conn.in_transaction and created.revision == 1
        raise RuntimeError("injected failure after Work Item creation")

    monkeypatch.setattr(
        agent_runtime,
        "create_recall_conversation_work_item_in_transaction",
        fail_after_create,
    )

    with pytest.raises(RuntimeError, match="after Work Item creation"):
        await stack.runtime.chat(
            OWNER,
            INITIAL_PROMPT,
            actor=stack.actor,
            conversation_id=conversation_id,
        )

    assert _work_items(storage, conversation_id) == []
    boundary = storage.execute(
        "SELECT id FROM messages WHERE user_id=? AND conversation_id=? AND content=?",
        (OWNER, conversation_id, INITIAL_PROMPT),
    ).fetchone()
    assert boundary is not None
    assistant = storage.execute(
        "SELECT 1 FROM messages WHERE user_id=? AND conversation_id=? AND reply_to=?",
        (OWNER, conversation_id, str(boundary["id"])),
    ).fetchone()
    assert assistant is None


@pytest.mark.asyncio
async def test_continuation_assistant_receipt_and_constraint_cas_roll_back_together(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, _reply, initial = await _create_active_work(settings, storage)
    model = _ForbiddenModel()
    restarted = _stack(settings, storage, model=model)
    real_cas = agent_runtime.cas_update_recall_conversation_constraints_in_transaction
    seen_expected_revisions: list[int] = []

    def fail_after_cas(conn, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen_expected_revisions.append(int(kwargs["expected_revision"]))
        updated = real_cas(conn, **kwargs)
        assert conn.in_transaction and updated.revision == initial.revision + 1
        raise WorkItemConflictError("injected post-CAS publication race")

    monkeypatch.setattr(
        agent_runtime,
        "cas_update_recall_conversation_constraints_in_transaction",
        fail_after_cas,
    )

    with pytest.raises(WorkItemConflictError, match="post-CAS"):
        await restarted.runtime.chat(
            OWNER,
            FOLLOWUP,
            actor=restarted.actor,
            conversation_id=conversation_id,
        )

    assert seen_expected_revisions == [initial.revision]
    retained = _only_work_item(storage, conversation_id)
    assert retained == initial
    boundary = storage.execute(
        """SELECT id FROM messages
             WHERE user_id=? AND conversation_id=? AND content=?
             ORDER BY rowid DESC LIMIT 1""",
        (OWNER, conversation_id, FOLLOWUP),
    ).fetchone()
    assert boundary is not None
    assistant = storage.execute(
        "SELECT 1 FROM messages WHERE user_id=? AND conversation_id=? AND reply_to=?",
        (OWNER, conversation_id, str(boundary["id"])),
    ).fetchone()
    assert assistant is None
