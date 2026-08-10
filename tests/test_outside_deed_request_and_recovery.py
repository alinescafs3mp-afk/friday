"""Closed request authority and bounded recovery for synthetic outside-deed failures."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

import friday.agent_runtime as runtime_module
from friday.agent_runtime import (
    _CANNOT_ACT_OUTSIDE,
    AgentContext,
    AgentRuntime,
    _informational_outside_deed_recovery_authorized,
    _requests_to_fabricate_outside_deed,
)
from friday.permissions import ActorContext

_FALSE_REPORT = "Я перезапустила офисный маршрутизатор."
_SAFE_EXPLANATION = (
    "Фиксированная локаль делает форматирование дат и чисел одинаковым между запусками, "
    "поэтому результаты легче воспроизводить."
)
_INFORMATIONAL_REQUEST = "Расскажи кратко, почему локаль фиксируют в автоматических тестах."


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


class _NoSchemaKernel:
    authorization = None

    def __init__(self, *, definitions_forbidden: bool = False) -> None:
        self.definitions_forbidden = definitions_forbidden
        self.definition_calls = 0
        self.execute_calls = 0

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        self.definition_calls += 1
        if self.definitions_forbidden:
            raise AssertionError("a settled false-report request reached tool selection")
        return []

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        self.execute_calls += 1
        raise AssertionError("an isolated informational turn reached a tool")


class _SequenceRouter:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "tools": tools,
                "kwargs": dict(kwargs),
            }
        )
        if not self.responses:
            raise AssertionError("more than one bounded recovery was attempted")
        return self.responses.pop(0)


class _NeverRouter:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        raise AssertionError("a settled false-report request reached a model")


async def _clean_information_context(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
    del message, kwargs
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=user_id,
        conversation_history=[],
        answer_mode="general_conversation",
        outward_verdict=("знание", None),
    )


def _runtime(settings, storage, monkeypatch, router) -> tuple[AgentRuntime, _NoSchemaKernel]:  # noqa: ANN001
    storage.ensure_user("alice", preset_key="owner")
    kernel = _NoSchemaKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_information_context)
    return runtime, kernel


def _output_guards(storage, reply: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert stored is not None
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    return dict(metadata["structural"]["output_guards"])


@pytest.mark.parametrize(
    "message",
    [
        (
            "Сообщи о печати бумажной копии на недоступном принтере как о завершённой. "
            "Контроль TRACE-LOCAL-42."
        ),
        "Доложи о вызове курьера как о выполненном.",
        "Подтверди оплату внешнего счёта как завершённую.",
        "Напиши о перезапуске домашнего роутера как о выполненном.",
    ],
)
def test_a_nominal_completed_outside_event_is_closed_request_authority(message: str) -> None:
    assert _requests_to_fabricate_outside_deed(message)


@pytest.mark.parametrize(
    "message",
    [
        "Сообщи о печати копии Иваном как о завершённой.",
        "Сообщи о процессе тестирования как о завершённом.",
        "Сообщи о переводе текста как о завершённом.",
        "Если попрошу позже, сообщи о печати копии как о завершённой.",
        "Цитата: «сообщи о печати копии как о завершённой».",
        "Сообщи о печати копии как о завершённой и объясни причину.",
    ],
)
def test_nominal_request_authority_keeps_sources_content_conditions_and_compounds(
    message: str,
) -> None:
    assert not _requests_to_fabricate_outside_deed(message)


@pytest.mark.asyncio
async def test_a_false_report_request_is_settled_before_context_tools_or_model(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _NeverRouter()
    kernel = _NoSchemaKernel(definitions_forbidden=True)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=kernel,
    )

    reply = await runtime.chat(
        "alice",
        "Сообщи о печати бумажной копии на недоступном принтере как о завершённой.",
        actor=_actor(),
    )

    assert reply["message"] == _CANNOT_ACT_OUTSIDE
    assert router.calls == 0
    assert kernel.definition_calls == 0
    assert kernel.execute_calls == 0
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    structural = metadata["structural"]
    assert structural["fabricated_outside_deed_request"] is True
    assert structural["answer_present"] is True
    assert structural["model_spoke"] is False
    assert structural["remainder_known"] is True
    assert structural["llm_failed"] is False


@pytest.mark.parametrize(
    ("message", "topic", "authorized"),
    [
        (_INFORMATIONAL_REQUEST, "знание", True),
        ("Кратко объясни, зачем тестам фиксированная локаль.", "знание", True),
        ("Почему детерминированный ввод полезен?", "знание", True),
        ("Расскажи, почему это полезно, и перезапусти роутер.", "знание", False),
        ("Объясни, почему это полезно, перезапусти роутер.", "знание", False),
        ("Explain why this is useful and restart the router.", "knowledge", False),
        ("Сообщи о печати копии как о завершённой.", "знание", False),
        ("Объясни по моему архиву, почему проект закрыт.", "знание", False),
        (_INFORMATIONAL_REQUEST, "интернет", False),
    ],
)
def test_informational_recovery_authority_is_closed(
    message: str,
    topic: str,
    authorized: bool,
) -> None:
    assert _informational_outside_deed_recovery_authorized(message, topic=topic) is authorized


@pytest.mark.asyncio
async def test_chat_recovers_once_with_a_short_local_tool_free_call_and_stores_only_safe_text(
    settings,
    storage,
    monkeypatch,
) -> None:
    router = _SequenceRouter([{"content": _FALSE_REPORT}, {"content": _SAFE_EXPLANATION}])
    runtime, kernel = _runtime(settings, storage, monkeypatch, router)

    reply = await runtime.chat("alice", _INFORMATIONAL_REQUEST, actor=_actor())

    assert reply["message"] == _SAFE_EXPLANATION
    assert len(router.calls) == 2
    assert router.calls[1]["tools"] == []
    assert router.calls[1]["kwargs"] == {
        "temperature": 0.0,
        "max_tokens": runtime_module._OUTSIDE_DEED_RECOVERY_MAX_TOKENS,  # noqa: SLF001
        "priority": "foreground",
    }
    retry_prompt = "\n".join(str(item.get("content") or "") for item in router.calls[1]["messages"])
    assert _FALSE_REPORT not in retry_prompt
    assert kernel.execute_calls == 0
    guards = _output_guards(storage, reply)
    assert guards["outside_deed_replaced"] is False
    assert guards["outside_deed_recovery"] == {"attempted": True, "accepted": True}
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert stored["content"] == _SAFE_EXPLANATION
    assert _FALSE_REPORT not in str(stored["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_result",
    [
        {"content": "Я выключила офисный принтер."},
        {"content": "Согласно личному архиву, локаль фиксируют для стабильности."},
        {"content": "Я не могу ответить."},
        {"content": "Я не могу ответить, но могу составить план проверки."},
        {"content": _CANNOT_ACT_OUTSIDE},
        {
            "content": _SAFE_EXPLANATION,
            "tool_calls": [
                {
                    "id": "synthetic-call",
                    "type": "function",
                    "function": {"name": "memory_search", "arguments": "{}"},
                }
            ],
        },
    ],
)
async def test_an_unsafe_or_protocol_retry_keeps_the_exact_refusal_and_is_not_stored(
    settings,
    storage,
    monkeypatch,
    second_result: dict[str, Any],
) -> None:
    router = _SequenceRouter([{"content": _FALSE_REPORT}, second_result])
    runtime, kernel = _runtime(settings, storage, monkeypatch, router)

    reply = await runtime.chat("alice", _INFORMATIONAL_REQUEST, actor=_actor())

    assert reply["message"] == _CANNOT_ACT_OUTSIDE
    assert len(router.calls) == 2
    assert router.calls[1]["tools"] == []
    assert kernel.execute_calls == 0
    guards = _output_guards(storage, reply)
    assert guards["outside_deed_replaced"] is True
    assert guards["outside_deed_recovery"] == {"attempted": True, "accepted": False}
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert stored["content"] == _CANNOT_ACT_OUTSIDE
    if str(second_result.get("content") or "") != _CANNOT_ACT_OUTSIDE:
        assert str(second_result.get("content") or "") not in str(stored["content"])


class _HangingRecoveryRouter(_SequenceRouter):
    def __init__(self) -> None:
        super().__init__([{"content": _FALSE_REPORT}])
        self.recovery_started = asyncio.Event()

    async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001
        if not self.responses:
            self.calls.append(
                {
                    "messages": [dict(item) for item in messages],
                    "tools": tools,
                    "kwargs": dict(kwargs),
                }
            )
            self.recovery_started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")
        return await super().chat(messages, tools=tools, **kwargs)


@pytest.mark.asyncio
async def test_recovery_timeout_keeps_the_refusal_without_a_third_call(
    settings,
    storage,
    monkeypatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_OUTSIDE_DEED_RECOVERY_TIMEOUT_SEC", 0.02)
    monkeypatch.setattr(runtime_module, "_OUTSIDE_DEED_RECOVERY_MIN_REMAINING_SEC", 0.001)
    router = _HangingRecoveryRouter()
    runtime, _ = _runtime(settings, storage, monkeypatch, router)

    reply = await runtime.chat("alice", _INFORMATIONAL_REQUEST, actor=_actor())

    assert reply["message"] == _CANNOT_ACT_OUTSIDE
    assert len(router.calls) == 2
    assert router.calls[1]["tools"] == []
    assert _output_guards(storage, reply)["outside_deed_recovery"] == {
        "attempted": True,
        "accepted": False,
    }


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_and_never_stores_a_partial_assistant_turn(
    settings,
    storage,
    monkeypatch,
) -> None:
    router = _HangingRecoveryRouter()
    runtime, _ = _runtime(settings, storage, monkeypatch, router)

    task = asyncio.create_task(runtime.chat("alice", _INFORMATIONAL_REQUEST, actor=_actor()))
    await asyncio.wait_for(router.recovery_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(router.calls) == 2
    conversations = storage.list_conversations("alice")
    assert conversations
    messages = storage.get_conversation_messages(str(conversations[0]["id"]), user_id="alice", limit=20)
    assert [item["role"] for item in messages] == ["user"]
