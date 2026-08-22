"""The legacy turn boundary persists a closed, privacy-safe Turn Trace."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.interaction_control_plane import (
    CapabilityClass,
    ContinuationKind,
    IntentClass,
    OutcomeStatus,
    TurnTrace,
    WorkRelation,
)
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
) -> _StoredTurn:  # noqa: ANN001
    storage.ensure_user(_USER_ID, preset_key="owner")
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
        return {
            "content": _MODEL_TEXT,
            "tools_used": list(tools_used),
            "tool_evidence": list(tool_evidence),
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
