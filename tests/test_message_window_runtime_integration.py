"""Full-runtime acceptance for the promoted exact current-chat window.

The lane is intentionally narrow.  These tests use a temporary SQLite database
and the real execution kernel, but no model, network, Docker, or production
service.  They pin admission, same-transaction currentness, typed completion and
the durable/public receipt boundary rather than merely testing the standalone
message-window dataclasses.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import friday.agent_runtime as agent_runtime
from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.capability_outcome import (
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.message_window_outcome import (
    MESSAGE_WINDOW_DENIED_RESPONSE,
    MESSAGE_WINDOW_EMPTY_RESPONSE,
    MESSAGE_WINDOW_UNAVAILABLE_RESPONSE,
    MessageWindowOutcomeError,
)
from friday.permissions import AuthorizationService
from friday.telegram_bridge._callbacks import CallbacksMixin
from friday.web_surfer import WebSurfer

OWNER = "message-window-owner"
FOREIGN = "message-window-foreign"
PROMPT = "Выведи всю переписку за 22 августа"
WINDOW_DAY = "2026-08-22"


class _ForbiddenModel:
    enabled = True
    model = "forbidden-message-window-model"
    total_budget_sec = 360.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.calls += 1
        raise AssertionError("promoted exact message window called a model")


class _LegacyModel:
    enabled = True
    model = "synthetic-legacy-message-window-model"
    total_budget_sec = 360.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.calls += 1
        return {
            "content": "Обычный непродвинутый маршрут.",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _stack(settings: Any, storage: Any, *, model: Any) -> SimpleNamespace:
    storage.ensure_user(OWNER, preset_key="user")
    storage.ensure_user(FOREIGN, preset_key="user")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        WebSurfer(settings),
        IngestionPipeline(settings, storage, graph),
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    return SimpleNamespace(
        authorization=authorization,
        kernel=kernel,
        model=model,
        runtime=runtime,
        actor=authorization.actor_for_user(OWNER, source="message-window-test"),
    )


def _at_window_time(storage: Any, message_id: str, index: int) -> None:
    storage.execute(
        "UPDATE messages SET created_at=? WHERE id=?",
        (f"{WINDOW_DAY}T08:{index:02d}:00+00:00", message_id),
    )
    storage.conn.commit()


def _historical_messages(
    storage: Any,
    conversation_id: str,
    count: int,
    *,
    prefix: str = "EXACT-BODY",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        row = storage.store_message(
            conversation_id,
            OWNER,
            role,
            f"{prefix}-{index + 1:02d}",
        )
        _at_window_time(storage, str(row["id"]), index)
        rows.append(row)
    return rows


def _foreign_canaries(storage: Any) -> None:
    other_own_conversation = storage.create_conversation(OWNER, "other own chat")
    own_other = storage.store_message(
        str(other_own_conversation["id"]),
        OWNER,
        "user",
        "OTHER-CONVERSATION-CANARY",
    )
    _at_window_time(storage, str(own_other["id"]), 30)

    foreign_conversation = storage.create_conversation(FOREIGN, "foreign chat")
    foreign = storage.store_message(
        str(foreign_conversation["id"]),
        FOREIGN,
        "user",
        "FOREIGN-PERSON-CANARY",
    )
    _at_window_time(storage, str(foreign["id"]), 31)


def _stored_metadata(storage: Any, reply: dict[str, Any]) -> dict[str, Any]:
    stored = storage.get_message(str(reply["message_id"]), OWNER)
    assert stored is not None
    value = json.loads(str(stored["metadata_json"] or "{}"))
    assert isinstance(value, dict)
    return value


def _receipt(storage: Any, reply: dict[str, Any]):  # noqa: ANN202
    metadata = _stored_metadata(storage, reply)
    return load_accepted_capability_outcome_receipt(metadata), metadata


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

    kernel.execute = recording_execute  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "expected_status"),
    (
        (2, CapabilityOutcomeStatus.COMPLETE),
        (21, CapabilityOutcomeStatus.PARTIAL),
        (0, CapabilityOutcomeStatus.EMPTY),
    ),
)
async def test_promoted_exact_window_is_deterministic_scoped_and_receipted(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    expected_status: CapabilityOutcomeStatus,
) -> None:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "current chat")
    rows = _historical_messages(storage, str(conversation["id"]), count)
    _foreign_canaries(storage)
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))
    calls = _record_message_search(stack.kernel)

    reply = await stack.runtime.chat(
        OWNER,
        PROMPT,
        actor=stack.actor,
        conversation_id=str(conversation["id"]),
    )

    assert model.calls == 0
    assert len(calls) == 1, "the read was retried or paginated outside the closed cap"
    assert reply["message_format"] == "plain"
    assert PROMPT not in reply["message"], "the current boundary escaped into its own result"
    assert "OTHER-CONVERSATION-CANARY" not in reply["message"]
    assert "FOREIGN-PERSON-CANARY" not in reply["message"]
    receipt, metadata = _receipt(storage, reply)
    assert receipt.outcome.status is expected_status

    if count == 0:
        assert reply["message"] == MESSAGE_WINDOW_EMPTY_RESPONSE
        assert receipt.outcome.citation_labels == ()
    else:
        shown = min(count, 20)
        assert receipt.outcome.citation_labels == tuple(f"A{i}" for i in range(1, shown + 1))
        for index, row in enumerate(rows[:shown], 1):
            encoded = json.dumps(str(row["content"]), ensure_ascii=False, separators=(",", ":"))
            assert f"[A{index}]" in reply["message"]
            assert encoded in reply["message"], "a citation was not byte-bound to its exact body"
        if count > shown:
            assert str(rows[shown]["content"]) not in reply["message"]
            assert "Показано сообщений: 20 из 21. Окно неполное." in reply["message"]
        else:
            assert "Показано сообщений: 2 из 2. Окно полное." in reply["message"]

    public_reply = json.dumps(reply, ensure_ascii=False, default=str)
    telegram_body = CallbacksMixin._format_response_message(reply)  # noqa: SLF001
    assert "accepted_capability_outcome" not in public_reply
    assert "accepted_capability_outcome" not in telegram_body
    private_receipt = json.dumps(metadata["accepted_capability_outcome"], ensure_ascii=False)
    for private_value in (
        PROMPT,
        OWNER,
        FOREIGN,
        str(conversation["id"]),
        "OTHER-CONVERSATION-CANARY",
        "FOREIGN-PERSON-CANARY",
        *(str(row["content"]) for row in rows),
    ):
        assert private_value not in private_receipt


@pytest.mark.asyncio
async def test_plain_mapping_cannot_forge_the_private_message_selection_carrier(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "forged carrier")
    _historical_messages(storage, str(conversation["id"]), 1, prefix="FORGED-CARRIER-BODY")
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))
    original = stack.kernel.execute
    calls = 0

    async def stripping_execute(  # noqa: ANN202
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",  # noqa: ANN001
    ):
        nonlocal calls
        if name == "message_search":
            assert execution_scope == "internal"
        result = await original(name, arguments, actor=actor, execution_scope=execution_scope)
        if name != "message_search":
            return result
        calls += 1
        # A plain mapping can imitate JSON fields but cannot reproduce the
        # snapshot's hidden process seal.
        return ToolResult(
            result.tool_name,
            result.success,
            {"snapshot_identity_sha256": "0" * 64},
            error=result.error,
        )

    stack.kernel.execute = stripping_execute  # type: ignore[method-assign]

    reply = await stack.runtime.chat(
        OWNER,
        PROMPT,
        actor=stack.actor,
        conversation_id=str(conversation["id"]),
    )

    assert calls == 1 and model.calls == 0
    assert reply["message"] == MESSAGE_WINDOW_UNAVAILABLE_RESPONSE
    assert "FORGED-CARRIER-BODY" not in json.dumps(reply, ensure_ascii=False, default=str)
    receipt, _metadata = _receipt(storage, reply)
    assert receipt.outcome.status is CapabilityOutcomeStatus.UNAVAILABLE
    assert receipt.outcome.evidence_identity_sha256 is None
    assert receipt.outcome.citation_labels == ()


@pytest.mark.asyncio
async def test_late_message_read_revocation_publishes_denied_without_evidence_or_retry(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "late revoke")
    _historical_messages(storage, str(conversation["id"]), 2, prefix="REVOKED-EVIDENCE")
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))
    original = stack.kernel.execute
    calls = 0

    async def revoking_execute(  # noqa: ANN202
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",  # noqa: ANN001
    ):
        nonlocal calls
        if name == "message_search":
            assert execution_scope == "internal"
        result = await original(name, arguments, actor=actor, execution_scope=execution_scope)
        if name == "message_search":
            calls += 1
            stack.authorization.deny_permission(OWNER, "conversations.read")
        return result

    stack.kernel.execute = revoking_execute  # type: ignore[method-assign]

    reply = await stack.runtime.chat(
        OWNER,
        PROMPT,
        actor=stack.actor,
        conversation_id=str(conversation["id"]),
    )

    assert calls == 1 and model.calls == 0
    assert reply["message"] == MESSAGE_WINDOW_DENIED_RESPONSE
    assert "REVOKED-EVIDENCE" not in json.dumps(reply, ensure_ascii=False, default=str)
    assert reply.get("tool_evidence") in (None, [])
    receipt, _metadata = _receipt(storage, reply)
    assert receipt.outcome.status is CapabilityOutcomeStatus.DENIED
    assert receipt.outcome.evidence_identity_sha256 is None
    assert receipt.outcome.citation_labels == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ("content", "snapshot", "insert"))
async def test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, f"{drift} drift")
    rows = _historical_messages(storage, str(conversation["id"]), 2, prefix="OLD-SNAPSHOT")
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))
    tool_calls = _record_message_search(stack.kernel)
    selector_name = "select_promoted_current_conversation_window_in_transaction"
    real_selector = getattr(agent_runtime, selector_name)
    final_calls = 0

    def drifting_selector(conn, **kwargs):  # noqa: ANN001, ANN202
        nonlocal final_calls
        final_calls += 1
        assert conn.in_transaction
        if drift == "content":
            conn.execute(
                "UPDATE messages SET content='NEW-CONTENT-DRIFT' WHERE id=?",
                (rows[0]["id"],),
            )
        elif drift == "snapshot":
            conn.execute(
                "UPDATE messages SET created_at='2026-08-21T08:00:00+00:00' WHERE id=?",
                (rows[0]["id"],),
            )
        else:
            conn.execute(
                """INSERT INTO messages(
                       rowid,id,conversation_id,user_id,role,content,
                       metadata_json,reply_to,created_at
                   ) VALUES(-1,'msg_eeeeeeeeeeeeeeee',?,?,'user','INSERT-DRIFT',
                            '{}',NULL,'2026-08-22T08:59:00+00:00')""",
                (conversation["id"], OWNER),
            )
        return real_selector(conn, **kwargs)

    monkeypatch.setattr(agent_runtime, selector_name, drifting_selector)

    reply = await stack.runtime.chat(
        OWNER,
        PROMPT,
        actor=stack.actor,
        conversation_id=str(conversation["id"]),
    )

    assert final_calls == 1
    assert len(tool_calls) == 1 and model.calls == 0
    assert reply["message"] == MESSAGE_WINDOW_UNAVAILABLE_RESPONSE
    projection = json.dumps(reply, ensure_ascii=False, default=str)
    assert "OLD-SNAPSHOT" not in projection
    assert "NEW-CONTENT-DRIFT" not in projection
    assert "INSERT-DRIFT" not in projection
    receipt, _metadata = _receipt(storage, reply)
    assert receipt.outcome.status is CapabilityOutcomeStatus.UNAVAILABLE
    assert receipt.outcome.evidence_identity_sha256 is None
    assert receipt.outcome.citation_labels == ()


@pytest.mark.asyncio
async def test_message_receipt_insert_and_reread_share_one_transaction_and_failure_rolls_back(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ForbiddenModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "atomic receipt")
    _historical_messages(storage, str(conversation["id"]), 1, prefix="ATOMIC-BODY")
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))
    selector_name = "select_promoted_current_conversation_window_in_transaction"
    real_selector = getattr(agent_runtime, selector_name)
    real_store = agent_runtime.store_message_in_transaction
    publication_connection: Any = None

    def recording_selector(conn, **kwargs):  # noqa: ANN001, ANN202
        nonlocal publication_connection
        assert conn.in_transaction
        publication_connection = conn
        return real_selector(conn, **kwargs)

    def corrupting_store(conn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        assert publication_connection is conn and conn.in_transaction
        stored = real_store(conn, *args, **kwargs)
        # Simulate a corrupt durability reread after the assistant+receipt row
        # was inserted.  The surrounding publication transaction must unwind it.
        return {**stored, "metadata_json": "{}"}

    monkeypatch.setattr(agent_runtime, selector_name, recording_selector)
    monkeypatch.setattr(agent_runtime, "store_message_in_transaction", corrupting_store)

    with pytest.raises((CapabilityOutcomeError, MessageWindowOutcomeError)):
        await stack.runtime.chat(
            OWNER,
            PROMPT,
            actor=stack.actor,
            conversation_id=str(conversation["id"]),
        )

    assert publication_connection is not None and model.calls == 0
    rows = storage.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY rowid",
        (conversation["id"],),
    ).fetchall()
    assert [str(row["role"]) for row in rows] == ["user", "user"]
    assert all(str(row["content"]) != MESSAGE_WINDOW_UNAVAILABLE_RESPONSE for row in rows)


def _has_accepted_receipt(storage: Any, reply: dict[str, Any]) -> bool:
    return "accepted_capability_outcome" in _stored_metadata(storage, reply)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "kwargs"),
    (
        (PROMPT, {"reply_to": "цитата из предыдущего сообщения"}),
        (PROMPT, {"quoted_attachment_reference": True}),
        (f"{PROMPT} и затем кратко обобщи их", {}),
        ("Проанализируй все сообщения за 22 августа", {}),
    ),
)
async def test_reply_attachment_compound_and_analysis_remain_outside_promotion(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    kwargs: dict[str, Any],
) -> None:
    model = _LegacyModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "legacy admission")
    _historical_messages(storage, str(conversation["id"]), 2, prefix="LEGACY-BODY")
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))

    reply = await stack.runtime.chat(
        OWNER,
        prompt,
        actor=stack.actor,
        conversation_id=str(conversation["id"]),
        **kwargs,
    )

    assert not _has_accepted_receipt(storage, reply)


@pytest.mark.asyncio
async def test_replay_of_an_exact_window_remains_outside_promotion(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _LegacyModel()
    stack = _stack(settings, storage, model=model)
    conversation = storage.create_conversation(OWNER, "legacy replay")
    _historical_messages(storage, str(conversation["id"]), 1, prefix="REPLAY-BODY")
    original = storage.store_message(str(conversation["id"]), OWNER, "user", PROMPT)
    monkeypatch.setattr(stack.runtime, "_local_now", lambda: datetime(2026, 8, 23, 12, 0))

    reply = await stack.runtime.chat(
        OWNER,
        PROMPT,
        actor=stack.actor,
        conversation_id=str(conversation["id"]),
        replay_source_message_id=str(original["id"]),
    )

    assert not _has_accepted_receipt(storage, reply)
