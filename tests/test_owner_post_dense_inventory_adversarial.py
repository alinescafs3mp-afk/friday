"""Synthetic full-chat regressions for the owner's dense inventory follow-ups.

The cases here contain no copied conversation text or private records.  They use
only a bounded fake kernel and deterministic fake routers; no provider or network
is reachable.  Assertions coordinate successive turns through the durable
assistant-message metadata that the chat surface publishes, not process-private
``AgentContext`` state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

OWNER = "owner_dense"
PERSON_ONE = "person_orbit_one"
PERSON_TWO = "person_orbit_two"


def _actor() -> ActorContext:
    return ActorContext(user_id=OWNER, preset_key="owner", source="synthetic-test")


class _AllowAll:
    def authorize(self, actor, capability, **kwargs):  # noqa: ANN001, ARG002
        return SimpleNamespace(allowed=True)


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "bounded synthetic read",
            "parameters": {"type": "object"},
        },
    }


async def _prepare_synthetic_context(
    user_id: str,
    message: str,
    conversation_id: str,
    **kwargs: Any,
) -> AgentContext:
    """Keep chat/history/intake plumbing while excluding retrieval and arbiters."""

    history = list(kwargs.get("prior_history") or [])
    previous_user = next(
        (str(row.get("content") or "") for row in reversed(history) if str(row.get("role") or "") == "user"),
        "",
    )
    previous_assistant = next(
        (
            str(row.get("content") or "")
            for row in reversed(history)
            if str(row.get("role") or "") == "assistant"
        ),
        "",
    )
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=str(kwargs.get("person_id") or user_id),
        conversation_history=history,
        previous_user_turn=previous_user,
        previous_answer=previous_assistant,
        ingestion=dict(kwargs.get("ingestion_result") or {}),
        interaction_mode=str(kwargs.get("interaction_mode") or "dialogue"),
        search_query=message,
        small_talk=message.strip().casefold().strip(".!?") == "приём",
    )


async def _prepare_native_person_context(
    user_id: str,
    message: str,
    conversation_id: str,
    **kwargs: Any,
) -> AgentContext:
    context = await _prepare_synthetic_context(user_id, message, conversation_id, **kwargs)
    context.outward_verdict = ("человек", "Незнакомец")
    return context


class _NeverModel:
    enabled = True
    total_budget_sec = 1.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("a code-owned inventory turn reached the model")


class _HostileZeroModel:
    """The exact unsupported claim seen after an unresolved activity result."""

    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {
            "content": "У участника 0 файлов.",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


class _NativeActivityThenAnswerModel:
    """Select ``user_activity`` natively, then try to state a zero."""

    enabled = True
    total_budget_sec = 1.0

    def __init__(self, answer: str = "У Незнакомца 0 файлов и 0 сообщений.") -> None:
        self.answer = answer
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        offered = [
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        ]
        common = {
            "_queue_wait_sec": 0.0,
            "_offered_tool_names": offered,
        }
        if self.calls == 1:
            return {
                **common,
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-owner-native-activity",
                        "type": "function",
                        "function": {
                            "name": "user_activity",
                            "arguments": json.dumps({"person": "Незнакомец"}, ensure_ascii=False),
                        },
                    }
                ],
            }
        return {**common, "content": self.answer, "tool_calls": None}


class _ConversationModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {
            "content": "На связи.",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


class _ExactInventoryKernel:
    authorization = _AllowAll()

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [_tool("user_activity")]

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "user_activity"
        assert params["documents_only"] is True
        assert params["limit"] == 200
        assert params["offset"] == 0
        self.calls.append(dict(params))

        # The exact @handle resolves to a real account whose current-day result
        # is genuinely empty.  A subsequent correction asks for the prior full
        # day and receives a complete two-row inventory.
        rows = (
            []
            if len(self.calls) == 1
            else [
                {"что": "synthetic-alpha.pdf", "когда": params["since"]},
                {"что": "synthetic-beta.docx", "когда": params["until"]},
            ]
        )
        total = len(rows)
        return ToolResult(
            tool,
            True,
            {
                "resolved": {
                    "user_id": PERSON_ONE,
                    "display_name": "Орбита",
                    "username": "orbit_one",
                },
                "человек": "Орбита (@orbit_one)",
                "период": {"с": params["since"], "по": params["until"]},
                "документов с подтверждённым автором": total,
                "документов без отметки автора": 0,
                "документы": rows,
                "пагинация": {
                    "смещение": 0,
                    "показано": total,
                    "из подтверждённых": total,
                    "следующее смещение": None,
                    "подтверждённый перечень показан полностью": True,
                },
            },
        )


class _UnresolvedActivityKernel:
    authorization = _AllowAll()

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [_tool("user_activity")]

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((str(tool), dict(params)))
        assert tool == "user_activity"
        return ToolResult(
            tool,
            True,
            {"resolved": None, "reason": "not_found", "candidates": []},
        )


class _NativeActivityKernel(_UnresolvedActivityKernel):
    def __init__(self, data: Any) -> None:
        super().__init__()
        self.data = data

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((str(tool), dict(params)))
        assert tool == "user_activity"
        return ToolResult(tool, True, self.data)


def _seed_people(storage) -> None:  # noqa: ANN001
    storage.ensure_user(OWNER, preset_key="owner", display_name="Владелец")
    storage.ensure_user(
        PERSON_ONE,
        preset_key="user",
        display_name="Орбита",
        username="orbit_one",
    )
    storage.ensure_user(
        PERSON_TWO,
        preset_key="user",
        display_name="Орбита",
        username="orbit_two",
    )


def _review_receipt(storage, text: str) -> dict[str, Any]:  # noqa: ANN001
    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="synthetic-test",
        source_ref=new_id("turn"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    storage.store_raw_object(raw)
    item = storage.store_inbox_item(
        InboxItem(
            id=new_id("inb"),
            user_id=OWNER,
            raw_object_id=raw.id,
            suggested_action="review",
        )
    )
    return {
        "action": "review",
        "queued_for_review": True,
        "inbox_id": item.id,
    }


def _assistant_metadata(storage, reply: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    row = storage.get_message(str(reply["message_id"]), OWNER)
    assert row is not None
    return json.loads(str(row["metadata_json"] or "{}"))


def _assert_code_owned_inventory(storage, reply: dict[str, Any]) -> None:  # noqa: ANN001
    metadata = _assistant_metadata(storage, reply)
    structural = metadata["structural"]
    assert structural["person_document_inventory"] is True
    assert structural["remainder_known"] is True
    assert structural["model_spoke"] is False
    assert metadata["tools_used"] == reply["tools_used"]


@pytest.mark.asyncio
async def test_six_turn_inventory_is_filled_by_handle_then_rerun_for_corrected_day(
    settings,
    storage,
    monkeypatch,
) -> None:
    """Ambiguity, clarification and date correction stay one exact read task."""

    _seed_people(storage)
    kernel = _ExactInventoryKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverModel(),
        kernel=kernel,
    )
    fixed_now = datetime(2026, 8, 10, 12, 34, 56)
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_synthetic_context)
    monkeypatch.setattr(runtime, "_local_now", lambda: fixed_now)
    monkeypatch.setattr(runtime, "_local_today", lambda: fixed_now.date())

    first_text = "Какие документы сегодня загрузила Орбита?"
    first_intake = _review_receipt(storage, first_text)
    first = await runtime.chat(
        OWNER,
        first_text,
        actor=_actor(),
        ingestion_result=first_intake,
    )

    assert kernel.calls == [], "an ambiguous display name must not reach the cross-account tool"
    assert first["message"].casefold().count("итог неизвестен") == 1
    assert "это всё" in first["message"].casefold()
    assert first["tools_used"] == []
    _assert_code_owned_inventory(storage, first)

    clarification = "Я про @orbit_one"
    clarification_intake = _review_receipt(storage, clarification)
    second = await runtime.chat(
        OWNER,
        clarification,
        actor=_actor(),
        conversation_id=first["conversation_id"],
        ingestion_result=clarification_intake,
    )

    assert len(kernel.calls) == 1
    assert kernel.calls[0]["person"] == PERSON_ONE
    assert "загрузил документов: 0" in second["message"].casefold()
    assert "0 из 0" in second["message"]
    assert "неизвест" not in second["message"].casefold()
    assert second["tools_used"] == ["user_activity"]
    _assert_code_owned_inventory(storage, second)

    correction = "Уже вчера, а не сегодня."
    correction_intake = _review_receipt(storage, correction)
    third = await runtime.chat(
        OWNER,
        correction,
        actor=_actor(),
        conversation_id=first["conversation_id"],
        ingestion_result=correction_intake,
    )

    assert len(kernel.calls) == 2
    today_call, yesterday_call = kernel.calls
    for field in ("person", "limit", "offset", "documents_only"):
        assert today_call[field] == yesterday_call[field]
    assert yesterday_call["person"] == PERSON_ONE
    assert today_call["since"] != yesterday_call["since"]
    assert today_call["until"] != yesterday_call["until"]
    assert today_call["since"].startswith("2026-08-09T21:00:00")
    assert yesterday_call["since"].startswith("2026-08-08T21:00:00")
    assert "synthetic-alpha.pdf" in third["message"]
    assert "synthetic-beta.docx" in third["message"]
    assert "полный подтверждённый перечень за весь день получен" in third["message"].casefold()
    assert "2 из 2" in third["message"]
    assert "неизвест" not in third["message"].casefold()
    assert third["tools_used"] == ["user_activity"]
    _assert_code_owned_inventory(storage, third)

    # Intake may conservatively create a review candidate before chat routing.
    # Every inventory request/correction is withdrawn, and none is promoted.
    for receipt in (first_intake, clarification_intake, correction_intake):
        assert receipt == {
            "action": "transient",
            "queued_for_review": False,
            "inbox_id": None,
        }
    assert storage.count_inbox(OWNER, InboxStatus.PENDING) == 0
    assert storage.count_knowledge_objects(OWNER) == 0


@pytest.mark.asyncio
async def test_unresolved_activity_transport_cannot_become_hostile_models_zero(
    settings,
    storage,
    monkeypatch,
) -> None:
    """Transport success with ``resolved: null`` is UNKNOWN, never empty data."""

    storage.ensure_user(OWNER, preset_key="owner", display_name="Владелец")
    storage.ensure_user("person_signal", display_name="Сигнал", username="signal_user")
    kernel = _UnresolvedActivityKernel()
    model = _HostileZeroModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_synthetic_context)

    reply = await runtime.chat(
        OWNER,
        "Что загружал Сигнал?",
        actor=_actor(),
    )

    assert kernel.calls == [("user_activity", {"person": "Сигнал"})]
    assert reply["tools_used"] == ["user_activity"]
    assert reply["message"].casefold().count("итог неизвестен") == 1
    assert "0 файлов" not in reply["message"].casefold()
    assert "не было" in reply["message"].casefold()
    assert model.calls == 0, "the model must not interpret an unresolved account as an empty result"

    metadata = _assistant_metadata(storage, reply)
    structural = metadata["structural"]
    assert structural["person_activity_unresolved"] is True
    assert structural["remainder_known"] is True
    assert structural["model_spoke"] is False
    assert "person_document_inventory" not in structural


@pytest.mark.asyncio
async def test_native_unresolved_activity_is_not_fact_evidence_for_a_hostile_zero(
    settings,
    storage,
    monkeypatch,
) -> None:
    """The model-selected tool path enforces the same resolver boundary."""

    storage.ensure_user(OWNER, preset_key="owner", display_name="Владелец")
    kernel = _NativeActivityKernel({"resolved": None, "reason": "not_found", "candidates": []})
    model = _NativeActivityThenAnswerModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_native_person_context)

    reply = await runtime.chat(
        OWNER,
        "Что писал Незнакомец?",
        actor=_actor(),
    )

    assert kernel.calls == [("user_activity", {"person": "Незнакомец"})]
    assert model.calls == 2
    assert reply["tools_used"] == ["user_activity"]
    assert reply["message"].casefold().count("итог неизвестен") == 1
    assert "0 файлов" not in reply["message"].casefold()
    metadata = _assistant_metadata(storage, reply)
    assert metadata["structural"]["person_activity_unresolved"] is True
    assert metadata["structural"]["model_spoke"] is False


@pytest.mark.asyncio
async def test_native_resolved_empty_activity_can_support_an_explicit_zero(
    settings,
    storage,
    monkeypatch,
) -> None:
    """A zero is admissible only beside a concrete resolved-account proof."""

    storage.ensure_user(OWNER, preset_key="owner", display_name="Владелец")
    kernel = _NativeActivityKernel(
        {
            "resolved": {
                "user_id": "synthetic-resolved-person",
                "display_name": "Незнакомец",
                "username": "resolved_person",
            },
            "summary": {"messages": 0},
            "messages": [],
            "items": [],
        }
    )
    model = _NativeActivityThenAnswerModel("Незнакомец не писал сообщений и не загружал файлов.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_native_person_context)

    reply = await runtime.chat(
        OWNER,
        "Что писал Незнакомец?",
        actor=_actor(),
    )

    assert kernel.calls == [("user_activity", {"person": "Незнакомец"})]
    assert reply["message"] == "Незнакомец не писал сообщений и не загружал файлов."
    assert "неизвест" not in reply["message"].casefold()
    metadata = _assistant_metadata(storage, reply)
    assert "person_activity_unresolved" not in metadata["structural"]
    assert metadata["structural"]["model_spoke"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_data", [None, {}])
async def test_code_owned_prefetch_rejects_a_success_without_resolution_proof(
    settings,
    storage,
    monkeypatch,
    malformed_data: Any,
) -> None:
    """A production ToolResult missing fact data is UNKNOWN, not legacy empty."""

    storage.ensure_user(OWNER, preset_key="owner", display_name="Владелец")
    storage.ensure_user("person_signal", display_name="Сигнал", username="signal_user")
    kernel = _NativeActivityKernel(malformed_data)
    model = _HostileZeroModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_synthetic_context)

    reply = await runtime.chat(OWNER, "Что загружал Сигнал?", actor=_actor())

    assert kernel.calls == [("user_activity", {"person": "Сигнал"})]
    assert model.calls == 0
    assert reply["message"].casefold().count("итог неизвестен") == 1
    assert "0 файлов" not in reply["message"].casefold()


@pytest.mark.asyncio
async def test_unrelated_ping_closes_pending_inventory_before_a_later_handle(
    settings,
    storage,
    monkeypatch,
) -> None:
    """A handle may fill only the immediately active inventory, not a stale one."""

    _seed_people(storage)
    kernel = _ExactInventoryKernel()
    model = _ConversationModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    fixed_now = datetime(2026, 8, 10, 12, 34, 56)
    monkeypatch.setattr(runtime, "_prepare_context", _prepare_synthetic_context)
    monkeypatch.setattr(runtime, "_local_now", lambda: fixed_now)
    monkeypatch.setattr(runtime, "_local_today", lambda: fixed_now.date())

    pending = await runtime.chat(
        OWNER,
        "Какие документы сегодня загрузила Орбита?",
        actor=_actor(),
    )
    ping = await runtime.chat(
        OWNER,
        "Приём",
        actor=_actor(),
        conversation_id=pending["conversation_id"],
    )
    stale_handle = await runtime.chat(
        OWNER,
        "Я про @orbit_one",
        actor=_actor(),
        conversation_id=pending["conversation_id"],
    )

    assert kernel.calls == []
    assert ping["message"] == "На связи."
    assert "документ" not in ping["message"].casefold()
    assert "неизвест" not in ping["message"].casefold()
    assert stale_handle["message"] == "На связи."
    for reply in (ping, stale_handle):
        structural = _assistant_metadata(storage, reply)["structural"]
        assert "person_document_inventory" not in structural
    assert _assistant_metadata(storage, pending)["structural"]["person_document_inventory"] is True
