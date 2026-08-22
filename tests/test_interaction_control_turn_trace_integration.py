"""The legacy turn boundary persists a closed, privacy-safe Turn Trace."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, _record_trace_tool_outcome
from friday.execution_kernel import ToolResult
from friday.interaction_control_plane import (
    CapabilityClass,
    CompletionDecision,
    ContinuationKind,
    FailureReason,
    IntentClass,
    OutcomeStatus,
    TurnTrace,
    WorkRelation,
)
from friday.interaction_control_plane.legacy_trace import CapabilityStatus
from friday.permissions import ActorContext

_USER_ID = "usr_raw_turn_trace_alice_92471"
_USER_TEXT = "Объясни синтетический термин ультрафиолетокрыл-92471."
_MODEL_TEXT = "Синтетический ответ содержит маркер янтарнохвост-73109."


class _UnexpectedModel:
    enabled = True
    model = "unexpected-turn-trace-model"
    total_budget_sec = 5.0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        raise AssertionError("the deterministic legacy-turn seam reached the model")


@dataclass(frozen=True, slots=True)
class _StoredTurn:
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    trace_payload: dict[str, object]


async def _run_legacy_turn(
    settings,
    storage,
    monkeypatch,
    *,
    tools_used: tuple[str, ...] = (),
    tool_evidence: tuple[dict[str, str], ...] = (),
    trace_tool_outcomes: tuple[tuple[str, CapabilityStatus], ...] = (),
    file_clips: tuple[dict[str, Any], ...] = (),
    context_overrides: dict[str, Any] | None = None,
) -> _StoredTurn:  # noqa: ANN001
    storage.ensure_user(_USER_ID, preset_key="owner")
    runtime = AgentRuntime(settings, storage, llm=_UnexpectedModel())

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        context = AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode="general_conversation",
        )
        for tool_name, outcome in trace_tool_outcomes:
            _record_trace_tool_outcome(context, tool_name, outcome)
        for name, value in (context_overrides or {}).items():
            setattr(context, name, value)
        return context

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {
            "content": _MODEL_TEXT,
            "tools_used": list(tools_used),
            "tool_evidence": list(tool_evidence),
            "file_clips": list(file_clips),
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    reply = await runtime.chat(
        _USER_ID,
        _USER_TEXT,
        actor=ActorContext(user_id=_USER_ID, preset_key="owner", source="test"),
        enable_tools=False,
    )

    conversation_id = str(reply["conversation_id"])
    assistant_message_id = str(reply["message_id"])
    rows = storage.get_conversation_messages(conversation_id, user_id=_USER_ID)
    user_rows = [row for row in rows if row["role"] == "user"]
    assert len(user_rows) == 1
    assistant = storage.get_message(assistant_message_id, _USER_ID)
    assert assistant is not None
    metadata = json.loads(str(assistant["metadata_json"] or "{}"))
    payload = metadata.get("interaction_trace")
    assert isinstance(payload, dict)
    return _StoredTurn(
        conversation_id=conversation_id,
        user_message_id=str(user_rows[0]["id"]),
        assistant_message_id=assistant_message_id,
        trace_payload=payload,
    )


@pytest.mark.asyncio
async def test_an_ordinary_legacy_answer_persists_a_valid_turn_trace(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(settings, storage, monkeypatch)

    trace = TurnTrace.parse(stored.trace_payload)

    assert trace.to_payload() == stored.trace_payload


@pytest.mark.asyncio
async def test_the_turn_trace_survives_storage_reopen(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    from friday.storage import init_storage

    stored = await _run_legacy_turn(settings, storage, monkeypatch)
    storage.close()
    reopened = init_storage(settings)
    try:
        assistant = reopened.get_message(stored.assistant_message_id, _USER_ID)
        assert assistant is not None
        metadata = json.loads(str(assistant["metadata_json"] or "{}"))
        reopened_payload = metadata.get("interaction_trace")

        assert reopened_payload == stored.trace_payload
        assert TurnTrace.parse(reopened_payload).to_payload() == stored.trace_payload
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_the_turn_trace_contains_neither_prose_nor_raw_identifiers(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(settings, storage, monkeypatch)

    serialized = json.dumps(stored.trace_payload, ensure_ascii=False, sort_keys=True).casefold()
    for private_value in (
        _USER_TEXT,
        _MODEL_TEXT,
        "ультрафиолетокрыл-92471",
        "янтарнохвост-73109",
        _USER_ID,
        stored.conversation_id,
        stored.user_message_id,
        stored.assistant_message_id,
    ):
        assert private_value.casefold() not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "intent", "capability"),
    [
        ("message_search", IntentClass.MESSAGE_RECALL, CapabilityClass.MESSAGE_RETRIEVAL),
        ("entity_lookup", IntentClass.ENTITY_LOOKUP, CapabilityClass.ENTITY_LOOKUP),
    ],
)
async def test_closed_generic_read_tools_are_not_lost_from_the_trace(
    settings,
    storage,
    monkeypatch,
    tool_name: str,
    intent: IntentClass,
    capability: CapabilityClass,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        tools_used=(tool_name,),
        tool_evidence=({"tool": tool_name, "output": "private synthetic result"},),
    )

    trace = TurnTrace.parse(stored.trace_payload)
    assert trace.intent is intent
    assert any(
        step.capability is capability and step.outcome is OutcomeStatus.SUCCEEDED for step in trace.steps
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "intent", "capability"),
    [
        ("memory_search", IntentClass.DOCUMENT_WORK, CapabilityClass.DOCUMENT_RETRIEVAL),
        ("message_search", IntentClass.MESSAGE_RECALL, CapabilityClass.MESSAGE_RETRIEVAL),
        ("entity_lookup", IntentClass.ENTITY_LOOKUP, CapabilityClass.ENTITY_LOOKUP),
    ],
)
async def test_a_seventh_closed_read_success_survives_the_public_evidence_cap(
    settings,
    storage,
    monkeypatch,
    tool_name: str,
    intent: IntentClass,
    capability: CapabilityClass,
) -> None:  # noqa: ANN001
    public_prefix = tuple(f"synthetic_{index}" for index in range(6))
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        tools_used=(*public_prefix, tool_name),
        tool_evidence=tuple({"tool": name, "output": "bounded public evidence"} for name in public_prefix),
        trace_tool_outcomes=((tool_name, CapabilityStatus.SUCCEEDED),),
    )

    trace = TurnTrace.parse(stored.trace_payload)
    assert trace.intent is intent
    assert trace.budget.capability_calls == 7
    assert any(
        step.capability is capability and step.outcome is OutcomeStatus.SUCCEEDED for step in trace.steps
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "publicly_reported", "expected"),
    [
        (CapabilityStatus.SUCCEEDED, True, OutcomeStatus.SUCCEEDED),
        (CapabilityStatus.EMPTY, False, OutcomeStatus.EMPTY),
        (CapabilityStatus.FAILED, False, OutcomeStatus.FAILED),
    ],
)
async def test_collect_files_is_only_a_file_effect_and_keeps_its_terminal_outcome(
    settings,
    storage,
    monkeypatch,
    status: CapabilityStatus,
    publicly_reported: bool,
    expected: OutcomeStatus,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        tools_used=("collect_files",) if publicly_reported else (),
        tool_evidence=(
            ({"tool": "collect_files", "output": "archive receipt"},) if publicly_reported else ()
        ),
        trace_tool_outcomes=(("collect_files", status),),
    )

    trace = TurnTrace.parse(stored.trace_payload)
    outcomes = {step.capability: step.outcome for step in trace.steps}
    assert trace.intent is IntentClass.EFFECT
    assert CapabilityClass.DOCUMENT_RETRIEVAL not in outcomes
    assert outcomes[CapabilityClass.FILE_GENERATION] is expected
    assert trace.budget.capability_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collect_status", "make_status", "expected"),
    [
        (
            CapabilityStatus.SUCCEEDED,
            CapabilityStatus.SUCCEEDED,
            OutcomeStatus.SUCCEEDED,
        ),
        (
            CapabilityStatus.FAILED,
            CapabilityStatus.SUCCEEDED,
            OutcomeStatus.PARTIAL,
        ),
        (CapabilityStatus.SUCCEEDED, CapabilityStatus.FAILED, OutcomeStatus.PARTIAL),
        (CapabilityStatus.EMPTY, CapabilityStatus.FAILED, OutcomeStatus.PARTIAL),
        (CapabilityStatus.EMPTY, CapabilityStatus.NOT_STARTED, OutcomeStatus.PARTIAL),
        (CapabilityStatus.FAILED, CapabilityStatus.DENIED, OutcomeStatus.DENIED),
    ],
)
async def test_mixed_collect_and_make_file_reflects_proven_successes(
    settings,
    storage,
    monkeypatch,
    collect_status: CapabilityStatus,
    make_status: CapabilityStatus,
    expected: OutcomeStatus,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        tools_used=("collect_files", "make_file"),
        trace_tool_outcomes=(
            ("collect_files", collect_status),
            ("make_file", make_status),
        ),
        file_clips=({"filename": "confirmed.zip", "content_base64": "UEs="},),
    )

    trace = TurnTrace.parse(stored.trace_payload)
    outcomes = {step.capability: step.outcome for step in trace.steps}
    assert trace.intent is IntentClass.EFFECT
    assert outcomes[CapabilityClass.FILE_GENERATION] is expected
    assert trace.budget.capability_calls == sum(
        status is not CapabilityStatus.NOT_STARTED for status in (collect_status, make_status)
    )
    if expected is OutcomeStatus.PARTIAL:
        assert trace.completion is CompletionDecision.PARTIAL
    if expected is OutcomeStatus.DENIED:
        assert trace.failure_reason is FailureReason.AUTHORITY_DENIED


@pytest.mark.asyncio
async def test_a_not_started_private_attempt_does_not_increment_capability_calls(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        tools_used=("make_file",),
        trace_tool_outcomes=(("make_file", CapabilityStatus.NOT_STARTED),),
    )

    trace = TurnTrace.parse(stored.trace_payload)
    outcomes = {step.capability: step.outcome for step in trace.steps}
    assert outcomes[CapabilityClass.FILE_GENERATION] is OutcomeStatus.NOT_STARTED
    assert trace.budget.capability_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "context_overrides", "capability"),
    [
        (
            "memory_search",
            {"filename_result_settled": True, "filename_result_raw_ids": ["raw_synthetic"]},
            CapabilityClass.DOCUMENT_RETRIEVAL,
        ),
        (
            "message_search",
            {"message_locate_evidence_ready": True},
            CapabilityClass.MESSAGE_RETRIEVAL,
        ),
        (
            "entity_lookup",
            {
                "person_document_inventory_settled": True,
                "person_document_inventory_succeeded": True,
            },
            CapabilityClass.ENTITY_LOOKUP,
        ),
    ],
)
async def test_independent_code_success_plus_a_failed_closed_read_is_partial(
    settings,
    storage,
    monkeypatch,
    tool_name: str,
    context_overrides: dict[str, Any],
    capability: CapabilityClass,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        trace_tool_outcomes=((tool_name, CapabilityStatus.FAILED),),
        context_overrides=context_overrides,
    )

    trace = TurnTrace.parse(stored.trace_payload)
    outcomes = {step.capability: step.outcome for step in trace.steps}
    assert outcomes[capability] is OutcomeStatus.PARTIAL


@pytest.mark.asyncio
async def test_a_failed_source_search_with_zero_expected_rows_is_not_empty(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    stored = await _run_legacy_turn(
        settings,
        storage,
        monkeypatch,
        trace_tool_outcomes=(("source_search", CapabilityStatus.FAILED),),
        context_overrides={
            "source_search_used": True,
            "source_search_result_expected_count": 0,
        },
    )

    trace = TurnTrace.parse(stored.trace_payload)
    outcomes = {step.capability: step.outcome for step in trace.steps}
    assert outcomes[CapabilityClass.DOCUMENT_RETRIEVAL] is OutcomeStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("made", "expected"),
    [
        (
            {"kind": "document", "filename": "report.pdf", "content_base64": "AA=="},
            CapabilityStatus.SUCCEEDED,
        ),
        (None, CapabilityStatus.FAILED),
    ],
)
async def test_late_make_file_records_its_real_terminal_outcome(
    settings,
    storage,
    monkeypatch,
    made: dict[str, str] | None,
    expected: CapabilityStatus,
) -> None:  # noqa: ANN001
    runtime = AgentRuntime(settings, storage, llm=_UnexpectedModel())
    context = AgentContext(conversation_id="conv_late_file", user_id=_USER_ID, person_id=_USER_ID)

    async def make_file(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        return made

    monkeypatch.setattr(runtime, "_make_file_from_answer", make_file)
    result = await runtime._file_for_a_request_that_wanted_one(  # noqa: SLF001
        "Создай PDF-отчёт по готовому тексту.",
        "Отчёт\n\nРаздел один\n\nПроверенный факт.\n\nРаздел два\n\nИтог.",
        ActorContext(user_id=_USER_ID, preset_key="owner", source="test"),
        context=context,
    )

    assert result is made
    assert context.late_make_file_attempts == 1
    assert context.trace_tool_outcomes == [("make_file", expected)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_success", "expected"),
    [
        (True, CapabilityStatus.SUCCEEDED),
        (False, CapabilityStatus.FAILED),
        (None, CapabilityStatus.NOT_STARTED),
    ],
)
async def test_agentic_make_file_records_the_validated_tool_result(
    settings,
    storage,
    tool_success: bool | None,
    expected: CapabilityStatus,
) -> None:  # noqa: ANN001
    class _Model:
        enabled = True
        model = "synthetic-make-file-ledger"
        total_budget_sec = 5.0

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001
            del messages, tools, kwargs
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call_make_file_trace",
                            "type": "function",
                            "function": {
                                "name": "make_file",
                                "arguments": json.dumps(
                                    {
                                        "kind": "docx",
                                        "title": "Отчёт",
                                        "blocks": [{"kind": "text", "text": "Проверенный факт."}],
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                }
            return {"content": "Готово.", "finish_reason": "stop", "tool_calls": None}

    class _Kernel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        @staticmethod
        def get_tool(name: str):  # noqa: ANN205
            if tool_success is None:
                # Expire after the model-selected call passed the outer deadline
                # check but before the mutator can enter the kernel.
                context.turn_deadline = 0.0
            return SimpleNamespace(risk="mutate") if name == "make_file" else None

        async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
            if tool_success is None:
                raise AssertionError("deadline-skipped mutator reached the kernel")
            self.calls.append(name)
            if not tool_success:
                return ToolResult(name, False, error="synthetic renderer failure")
            return ToolResult(
                name,
                True,
                data={"created": True},
                attachment={
                    "kind": "document",
                    "filename": "report.docx",
                    "content_base64": "AA==",
                },
            )

    model = _Model()
    kernel = _Kernel()
    runtime = AgentRuntime(settings, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]
    context = AgentContext(
        conversation_id="conv_agentic_make_file",
        user_id=_USER_ID,
        person_id=_USER_ID,
        outward_verdict=("файл", None),
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Создай Word-отчёт с проверенным фактом.",
        ActorContext(user_id=_USER_ID, preset_key="owner", source="test"),
        tools=[{"type": "function", "function": {"name": "make_file"}}],
        attachments=None,
    )

    assert kernel.calls == ([] if tool_success is None else ["make_file"])
    assert result["tools_used"] == ["make_file"]
    assert context.trace_tool_outcomes == [("make_file", expected)]


@pytest.mark.asyncio
async def test_interleaved_turns_never_mix_structural_trace_state(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    users = ("usr_trace_concurrent_a", "usr_trace_concurrent_b")
    for user_id in users:
        storage.ensure_user(user_id, preset_key="owner")
    runtime = AgentRuntime(settings, storage, llm=_UnexpectedModel())
    reached = [asyncio.Event(), asyncio.Event()]
    tool_names = ("message_search", "entity_lookup")
    user_indexes = {user_id: index for index, user_id in enumerate(users)}

    async def prepare(prepared_user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=prepared_user_id,
            person_id=prepared_user_id,
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del message, attachments
        turn_index = user_indexes[context.user_id]
        selected_tool = tool_names[turn_index]
        reached[turn_index].set()
        await reached[1 - turn_index].wait()
        return {
            "content": _MODEL_TEXT,
            "tools_used": [selected_tool],
            "tool_evidence": [{"tool": selected_tool, "output": "private synthetic result"}],
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)

    replies = await asyncio.gather(
        *(
            runtime.chat(
                user_id,
                f"Synthetic concurrent turn {index}",
                actor=ActorContext(user_id=user_id, preset_key="owner", source="test"),
                enable_tools=False,
            )
            for index, user_id in enumerate(users)
        )
    )
    traces: list[TurnTrace] = []
    for user_id, reply in zip(users, replies, strict=True):
        row = storage.get_message(str(reply["message_id"]), user_id)
        assert row is not None
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        traces.append(TurnTrace.parse(metadata["interaction_trace"]))

    assert traces[0].intent is IntentClass.MESSAGE_RECALL
    assert traces[1].intent is IntentClass.ENTITY_LOOKUP
    assert {step.capability for step in traces[0].steps} >= {
        CapabilityClass.MESSAGE_RETRIEVAL,
        CapabilityClass.MODEL_SYNTHESIS,
    }
    assert {step.capability for step in traces[1].steps} >= {
        CapabilityClass.ENTITY_LOOKUP,
        CapabilityClass.MODEL_SYNTHESIS,
    }
    assert traces[0].turn_digest != traces[1].turn_digest
    assert traces[0].conversation_digest != traces[1].conversation_digest


@pytest.mark.asyncio
async def test_two_turns_share_only_the_conversation_digest_and_classify_a_direct_reply(
    settings,
    storage,
    monkeypatch,
) -> None:  # noqa: ANN001
    first = await _run_legacy_turn(settings, storage, monkeypatch)
    runtime = AgentRuntime(settings, storage, llm=_UnexpectedModel())

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": _MODEL_TEXT, "tools_used": [], "_model_generated": True}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    reply = await runtime.chat(
        _USER_ID,
        "Уточни ответ, не сохраняя эту строку в trace.",
        actor=ActorContext(user_id=_USER_ID, preset_key="owner", source="test"),
        conversation_id=first.conversation_id,
        enable_tools=False,
        reply_to="Приватная цитата текущего запроса.",
        reply_assistant_reference=True,
        reply_assistant_message_id=first.assistant_message_id,
    )
    assistant = storage.get_message(str(reply["message_id"]), _USER_ID)
    assert assistant is not None
    metadata = json.loads(str(assistant["metadata_json"] or "{}"))
    second = TurnTrace.parse(metadata["interaction_trace"])
    initial = TurnTrace.parse(first.trace_payload)

    assert second.conversation_digest == initial.conversation_digest
    assert second.turn_digest != initial.turn_digest
    assert second.work_relation is WorkRelation.DIRECT
    assert second.continuation is ContinuationKind.REFERENCE
    assert second.state_restored is False
