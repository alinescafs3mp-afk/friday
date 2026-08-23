"""Regression contract for the code-owned conversational Obsidian create lane.

The fixtures are deliberately synthetic.  The route is exercised at the
``AgentRuntime._agentic_loop`` seam so the tests observe both capability
projection and the exact kernel calls without depending on a live vault.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _claims_an_unconfirmed_obsidian_deed,
)
from friday.execution_kernel import ToolResult
from friday.organs.obsidian.conversation import obsidian_operation_id
from friday.permissions import ActorContext

_EXACT_CREATE = (
    "Создай в Obsidian заметку Projects/Friday Test.md. "
    "Заголовок: «Тест интеграции Friday». Внутри напиши, что заметка создана "
    "через Telegram, и добавь текущую дату."
)
_BATTERY_APPEND = (
    "Добавь в конец заметки `Projects/Friday Test.md` раздел «Проверка дополнения» "
    "и одну строку: «Этот текст был добавлен отдельной командой»."
)
_ROOT_MESSAGE_ID = "msg_0123456789abcdef"
_REVISION = "a" * 64


def _schema(name: str) -> dict[str, Any]:
    property_names: dict[str, tuple[str, ...]] = {
        "obsidian_search_notes": ("query", "limit"),
        "obsidian_read_note": ("path",),
        "obsidian_create_note": ("operation_id", "path", "content"),
        "obsidian_append_note": (
            "operation_id",
            "path",
            "text",
            "expected_revision",
            "work_item_id",
        ),
        "obsidian_prepend_note": (
            "operation_id",
            "path",
            "text",
            "expected_revision",
            "work_item_id",
        ),
        "obsidian_replace_note": (
            "operation_id",
            "path",
            "content",
            "expected_revision",
            "work_item_id",
        ),
        "obsidian_set_properties": (
            "operation_id",
            "path",
            "properties",
            "expected_revision",
            "work_item_id",
        ),
        "obsidian_daily_note": (
            "operation_id",
            "day",
            "content",
            "expected_revision",
            "work_item_id",
        ),
    }
    required_names: dict[str, tuple[str, ...]] = {
        "obsidian_search_notes": ("query",),
        "obsidian_read_note": ("path",),
        "obsidian_create_note": ("operation_id", "path"),
        "obsidian_append_note": ("operation_id", "path", "text"),
        "obsidian_prepend_note": ("operation_id", "path", "text"),
        "obsidian_replace_note": ("operation_id", "path", "content", "expected_revision"),
        "obsidian_set_properties": ("operation_id", "path", "properties"),
        "obsidian_daily_note": ("operation_id",),
    }
    properties = {key: {"type": "string"} for key in property_names.get(name, ())}
    if "limit" in properties:
        properties["limit"] = {"type": "integer"}
    if "properties" in properties:
        properties["properties"] = {"type": "object"}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic Obsidian contract tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required_names.get(name, ())),
                "additionalProperties": False,
            },
        },
    }


def _vaults(state: str = "ready") -> dict[str, Any]:
    return {
        "vaults": [
            {
                "id": "obsvault_0123456789abcdef",
                "name": "Friday",
                "state": state,
                "android_alias": "Friday",
            }
        ],
        "count": 1,
    }


def _receipt(
    arguments: dict[str, Any],
    kind: str,
    *,
    tool_name: str = "obsidian_create_note",
) -> dict[str, Any]:
    delivered = kind == "delivered"
    methods = {
        "obsidian_create_note": "create",
        "obsidian_append_note": "append",
        "obsidian_prepend_note": "prepend",
        "obsidian_replace_note": "replace",
        "obsidian_set_properties": "set_properties",
        "obsidian_daily_note": "daily_note",
    }
    path = str(arguments.get("path") or "Daily/2026-08-22.md")
    result: dict[str, Any] = {
        "operation_id": arguments["operation_id"],
        "method": methods[tool_name],
        "status": "delivered" if delivered else "scan_pending",
        "path": path,
        "revision": _REVISION,
        "previous_revision": None,
        "created": tool_name in {"obsidian_create_note", "obsidian_daily_note"},
        "applied": True,
        "replayed": False,
        "delivery": {
            "local_write_complete": True,
            "server_scan_complete": delivered,
            "android_connected": delivered,
            "android_completion": 100.0 if delivered else None,
            "android_received": delivered,
            "obsidian_opened": False,
        },
    }
    if kind == "wrong_operation":
        result["operation_id"] = "caller-or-model-controlled-id"
    elif kind == "wrong_path":
        result["path"] = "Projects/Other.md"
    elif kind == "invalid_revision":
        result["revision"] = "not-a-revision"
    elif kind == "unproved_local_write":
        result["delivery"]["local_write_complete"] = False
    elif kind == "unproved_delivery":
        result["status"] = "delivered"
        result["delivery"]["server_scan_complete"] = True
        result["delivery"]["android_received"] = False
    return result


def _note_summary() -> dict[str, Any]:
    return {
        "path": "Projects/Friday Test.md",
        "title": "Friday Test",
        "revision": _REVISION,
        "size_bytes": 42,
        "modified_at": "2026-08-22T09:00:00+03:00",
    }


class _Authorization:
    def __init__(self, denied_capabilities: frozenset[str] = frozenset()) -> None:
        self.denied_capabilities = denied_capabilities
        self.calls: list[tuple[str, str]] = []

    def authorize(self, actor: ActorContext, capability: str) -> Any:
        self.calls.append((actor.own_id, capability))
        return SimpleNamespace(allowed=capability not in self.denied_capabilities)


class _ConversationKernel:
    def __init__(
        self,
        *,
        vault_state: str = "ready",
        receipt_kind: str = "delivered",
        denied_capabilities: frozenset[str] = frozenset(),
        selected_error: str = "",
    ) -> None:
        self.vault_state = vault_state
        self.receipt_kind = receipt_kind
        self.selected_error = selected_error
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.authorization = _Authorization(denied_capabilities)

    @staticmethod
    def get_tool(name: str) -> Any:
        if name in {
            "obsidian_list_vaults",
            "obsidian_list_notes",
            "obsidian_list_templates",
            "obsidian_search_notes",
            "obsidian_read_note",
        }:
            return SimpleNamespace(security_id="obsidian.read", risk="observe")
        if name in {
            "obsidian_create_note",
            "obsidian_append_note",
            "obsidian_prepend_note",
            "obsidian_replace_note",
            "obsidian_set_properties",
            "obsidian_daily_note",
        }:
            return SimpleNamespace(security_id="obsidian.write", risk="mutate")
        return None

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
        arguments = dict(arguments)
        self.executed.append((str(name), arguments))
        if name == "obsidian_list_vaults":
            return ToolResult(str(name), True, data=_vaults(self.vault_state))
        if self.selected_error:
            return ToolResult(str(name), False, error=self.selected_error)
        if name == "obsidian_list_notes":
            return ToolResult(str(name), True, data={"notes": [_note_summary()], "count": 1})
        if name == "obsidian_list_templates":
            return ToolResult(
                str(name),
                True,
                data={
                    "templates": [
                        {
                            "name": "Meeting",
                            "path": "Templates/Meeting.md",
                            "title": "Meeting template",
                            "revision": _REVISION,
                            "modified_at": "2026-08-22T09:00:00+03:00",
                        }
                    ],
                    "count": 1,
                },
            )
        if name == "obsidian_search_notes":
            summary = _note_summary()
            return ToolResult(
                str(name),
                True,
                data={
                    "matches": [
                        {
                            "path": summary["path"],
                            "title": summary["title"],
                            "revision": summary["revision"],
                            "modified_at": summary["modified_at"],
                            "excerpt": "Friday synthetic result",
                            "score": 3.5,
                            "match_channels": ["lexical"],
                        }
                    ],
                    "count": 1,
                },
            )
        if name == "obsidian_read_note":
            body = "Синтетический текст заметки."
            return ToolResult(
                str(name),
                True,
                data={
                    "path": arguments["path"],
                    "title": "Friday Test",
                    "content": body,
                    "body": body,
                    "properties": {},
                    "revision": _REVISION,
                    "size_bytes": len(body.encode("utf-8")),
                    "modified_at": "2026-08-22T09:00:00+03:00",
                },
            )
        if name in {
            "obsidian_create_note",
            "obsidian_append_note",
            "obsidian_prepend_note",
            "obsidian_replace_note",
            "obsidian_set_properties",
            "obsidian_daily_note",
        }:
            return ToolResult(
                str(name),
                True,
                data=_receipt(arguments, self.receipt_kind, tool_name=str(name)),
            )
        raise AssertionError(f"unexpected tool call: {name}")


class _ConversationLLM:
    enabled = True
    total_budget_sec = 120.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {
            "content": "Обычный синтетический ответ без действий.",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def _context(*, private: bool = False) -> AgentContext:
    return AgentContext(
        conversation_id="synthetic-obsidian-conversation",
        user_id="alice",
        interaction_mode="dialogue",
        outward_verdict=("действие", None),
        private_source_boundary_active=private,
        effect_root_user_message_id=_ROOT_MESSAGE_ID,
    )


def _tools() -> list[dict[str, Any]]:
    return [
        _schema("obsidian_list_vaults"),
        _schema("obsidian_create_note"),
        _schema("web_search"),
    ]


def _all_tools() -> list[dict[str, Any]]:
    return [
        _schema(name)
        for name in (
            "obsidian_list_vaults",
            "obsidian_list_notes",
            "obsidian_list_templates",
            "obsidian_search_notes",
            "obsidian_read_note",
            "obsidian_create_note",
            "obsidian_append_note",
            "obsidian_prepend_note",
            "obsidian_replace_note",
            "obsidian_set_properties",
            "obsidian_daily_note",
            "web_search",
        )
    ]


def _runtime(settings, storage, kernel: _ConversationKernel, llm: _ConversationLLM) -> AgentRuntime:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=llm,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]
    return runtime


def _durable_context(storage, prompt: str) -> tuple[AgentContext, str]:
    conversation = storage.create_conversation("alice", title="synthetic Obsidian operation")
    stored = storage.store_message(
        str(conversation["id"]),
        "alice",
        "user",
        prompt,
        metadata={"synthetic": True},
    )
    message_id = str(stored["id"])
    return (
        AgentContext(
            conversation_id=str(conversation["id"]),
            user_id="alice",
            interaction_mode="dialogue",
            outward_verdict=("действие", None),
            source_search_lineage_user_message_id=message_id,
            source_search_lineage_message_owner_id="alice",
            effect_root_user_message_id=message_id,
        ),
        message_id,
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Заметка Projects/Friday Test.md создана и сохранена в Obsidian.",
        "Готово — заметка создана в Obsidian.",
        "Я создала заметку в Obsidian.",
        "Markdown-заметка записана в ваш vault.",
    ],
)
def test_model_only_obsidian_completion_claims_require_an_owned_receipt(claim: str) -> None:
    assert _claims_an_unconfirmed_obsidian_deed(claim) is True


@pytest.mark.parametrize(
    "text",
    [
        "Заметка в Obsidian не создана.",
        "Создать заметку в Obsidian?",
        "Заметка готова к созданию в Obsidian.",
        "Объясню, как создать заметку в Obsidian.",
        "Obsidian создан как приложение для заметок.",
        "Заметка создана автором романа в 2020 году.",
    ],
)
def test_nonactual_obsidian_wording_is_not_a_false_completion_claim(text: str) -> None:
    assert _claims_an_unconfirmed_obsidian_deed(text) is False


@pytest.mark.asyncio
async def test_exact_russian_create_is_two_code_owned_calls_without_a_model(settings, storage) -> None:
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _EXACT_CREATE,
        _actor(),
        _tools(),
        None,
    )

    assert llm.calls == 0
    assert [name for name, _ in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    assert kernel.executed[0][1] == {}
    create_arguments = kernel.executed[1][1]
    assert set(create_arguments) == {"operation_id", "path", "content"}
    assert create_arguments["path"] == "Projects/Friday Test.md"
    operation_id = str(create_arguments["operation_id"])
    assert operation_id not in _EXACT_CREATE
    assert operation_id.startswith("obsop_")
    assert len(operation_id) == len("obsop_") + 64
    assert set(operation_id.removeprefix("obsop_")) <= set("0123456789abcdef")
    assert create_arguments["content"] == (
        "# Тест интеграции Friday\n\nЗаметка создана через Telegram.\n\n2026-08-22\n"
    )
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "succeeded"
    assert result["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]


@pytest.mark.asyncio
async def test_acceptance_append_is_one_code_owned_mutation_without_a_model(settings, storage) -> None:
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _BATTERY_APPEND,
        _actor(),
        _all_tools(),
        None,
    )

    assert llm.calls == 0
    assert [name for name, _ in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_append_note",
    ]
    append = kernel.executed[1][1]
    assert append["path"] == "Projects/Friday Test.md"
    assert append["text"] == ("## Проверка дополнения\n\nЭтот текст был добавлен отдельной командой")
    assert str(append["operation_id"]).startswith("obsop_")
    assert result["_obsidian_owned"] is True
    assert result["tools_used"] == ["obsidian_list_vaults", "obsidian_append_note"]


@pytest.mark.asyncio
async def test_operation_id_is_stable_for_the_same_durable_user_message(settings, storage) -> None:
    operation_ids: list[str] = []
    for _ in range(2):
        kernel = _ConversationKernel()
        llm = _ConversationLLM()
        runtime = _runtime(settings, storage, kernel, llm)

        await runtime._agentic_loop(  # noqa: SLF001
            _context(),
            _EXACT_CREATE,
            _actor(),
            _tools(),
            None,
        )

        operation_ids.append(str(kernel.executed[1][1]["operation_id"]))
    assert operation_ids[0] == operation_ids[1]


@pytest.mark.asyncio
async def test_sticky_private_boundary_keeps_only_the_local_obsidian_schemas(settings, storage) -> None:
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)
    tools = _tools()

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(private=True),
        _EXACT_CREATE,
        _actor(),
        tools,
        None,
    )

    assert llm.calls == 0
    assert [name for name, _ in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    assert {str((tool.get("function") or {}).get("name") or "") for tool in tools} == {
        "obsidian_list_vaults",
        "obsidian_create_note",
    }
    assert result["_obsidian_owned"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vault_state",
    ["awaiting_obsidian_vault_registration", "disconnected"],
)
async def test_not_ready_vault_is_preflight_only_and_never_claims_creation(
    settings,
    storage,
    vault_state: str,
) -> None:
    kernel = _ConversationKernel(vault_state=vault_state)
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _EXACT_CREATE,
        _actor(),
        _tools(),
        None,
    )

    assert llm.calls == 0
    assert kernel.executed == [("obsidian_list_vaults", {})]
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "unavailable"
    assert result["tools_used"] == ["obsidian_list_vaults"]
    assert "заметка создана" not in str(result["content"]).casefold()


@pytest.mark.parametrize(
    "prompt",
    [
        "Не создавай в Obsidian заметку Projects/Friday Test.md.",
        "Как создать в Obsidian заметку Projects/Friday Test.md?",
        "Объясни команду «Создай в Obsidian заметку Projects/Friday Test.md».",
        "`Создай в Obsidian заметку Projects/Friday Test.md.`",
        "Фраза для документа: Создай в Obsidian заметку Projects/Friday Test.md.",
    ],
)
@pytest.mark.asyncio
async def test_quoted_negated_and_meta_mentions_have_zero_obsidian_calls(
    settings,
    storage,
    prompt: str,
) -> None:
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        prompt,
        _actor(),
        _tools(),
        None,
    )

    assert kernel.executed == []
    assert "заметка создана" not in str(result["content"]).casefold()


@pytest.mark.asyncio
async def test_pending_receipt_reports_only_the_proven_local_write(settings, storage) -> None:
    kernel = _ConversationKernel(receipt_kind="pending")
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _EXACT_CREATE,
        _actor(),
        _tools(),
        None,
    )

    assert llm.calls == 0
    assert result["_obsidian_owned"] is True
    assert result["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]
    folded = str(result["content"]).casefold()
    assert "локаль" in folded or "сервер" in folded
    assert "android получил" not in folded
    assert "открыта в obsidian" not in folded


@pytest.mark.asyncio
async def test_delivered_receipt_may_report_android_receipt_but_not_opening(settings, storage) -> None:
    kernel = _ConversationKernel(receipt_kind="delivered")
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _EXACT_CREATE,
        _actor(),
        _tools(),
        None,
    )

    assert llm.calls == 0
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "succeeded"
    folded = str(result["content"]).casefold()
    assert "android" in folded
    assert "получ" in folded
    assert "открыта в obsidian" not in folded


@pytest.mark.parametrize(
    "receipt_kind",
    [
        "wrong_operation",
        "wrong_path",
        "invalid_revision",
        "unproved_local_write",
        "unproved_delivery",
    ],
)
@pytest.mark.asyncio
async def test_invalid_mutation_receipt_never_becomes_a_success_claim(
    settings,
    storage,
    receipt_kind: str,
) -> None:
    kernel = _ConversationKernel(receipt_kind=receipt_kind)
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _EXACT_CREATE,
        _actor(),
        _tools(),
        None,
    )

    assert llm.calls == 0
    assert [name for name, _ in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "uncertain"
    folded = str(result["content"]).casefold()
    assert "квитанц" in folded
    assert "не повтор" in folded
    assert "заметка создана" not in folded


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        ("Invalid tool arguments: synthetic", "not_started"),
        ("Authorization denied", "denied"),
        ("Unknown tool", "unavailable"),
        ("Tool is not initialized", "unavailable"),
        ("synthetic timeout after dispatch", "uncertain"),
    ],
)
@pytest.mark.asyncio
async def test_failed_obsidian_execution_preserves_the_closed_failure_class(
    settings,
    storage,
    error: str,
    outcome: str,
) -> None:
    kernel = _ConversationKernel(selected_error=error)
    runtime = _runtime(settings, storage, kernel, _ConversationLLM())

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        _EXACT_CREATE,
        _actor(),
        _tools(),
        None,
    )

    assert [name for name, _arguments in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    assert result["_obsidian_outcome"] == outcome
    assert "заметка создана" not in str(result["content"]).casefold()


_SHIPPED_OPERATION_CASES = (
    pytest.param(
        "Покажи мои подключённые хранилища в Obsidian.",
        "obsidian_list_vaults",
        {},
        "Хранилища Obsidian: 1.",
        False,
        id="list-vaults",
    ),
    pytest.param(
        "Покажи список заметок в Obsidian.",
        "obsidian_list_notes",
        {},
        "Заметки Obsidian: 1.",
        False,
        id="list-notes",
    ),
    pytest.param(
        "Покажи шаблоны в Obsidian.",
        "obsidian_list_templates",
        {},
        "Шаблоны Obsidian: 1.",
        False,
        id="list-templates",
    ),
    pytest.param(
        "Найди в Obsidian заметки по запросу «Friday».",
        "obsidian_search_notes",
        {"query": "Friday", "limit": 20},
        "Friday synthetic result",
        False,
        id="search-notes",
    ),
    pytest.param(
        "Прочитай в Obsidian заметку «Projects/Friday Test.md».",
        "obsidian_read_note",
        {"path": "Projects/Friday Test.md"},
        "Синтетический текст заметки.",
        False,
        id="read-note",
    ),
    pytest.param(
        "Добавь в Obsidian в заметку «Projects/Friday Test.md» текст: «Новая строка».",
        "obsidian_append_note",
        {"path": "Projects/Friday Test.md", "text": "Новая строка"},
        "Текст добавлен в локальную серверную копию заметки.",
        True,
        id="append-note",
    ),
    pytest.param(
        "Добавь в Obsidian в начало заметки «Projects/Friday Test.md» текст: «Контекст».",
        "obsidian_prepend_note",
        {"path": "Projects/Friday Test.md", "text": "Контекст"},
        "Текст добавлен в начало локальной серверной копии заметки.",
        True,
        id="prepend-note",
    ),
    pytest.param(
        "Замени в Obsidian содержимое заметки «Projects/Friday Test.md» целиком на текст: «Новая версия».",
        "obsidian_replace_note",
        {"path": "Projects/Friday Test.md", "content": "Новая версия"},
        "Содержимое локальной серверной копии заметки заменено.",
        True,
        id="replace-note",
    ),
    pytest.param(
        "Установи в Obsidian у заметки «Projects/Friday Test.md» свойство «status» = «done».",
        "obsidian_set_properties",
        {"path": "Projects/Friday Test.md", "properties": {"status": "done"}},
        "Свойства изменены в локальной серверной копии заметки.",
        True,
        id="set-properties",
    ),
    pytest.param(
        "Добавь в Obsidian в сегодняшнюю ежедневную заметку текст: «Итог дня».",
        "obsidian_daily_note",
        {"day": "2026-08-22", "content": "Итог дня"},
        "Ежедневная заметка изменена в локальной серверной копии vault.",
        True,
        id="daily-note",
    ),
)


@pytest.mark.parametrize(
    ("prompt", "tool_name", "expected_arguments", "rendered_fragment", "mutating"),
    _SHIPPED_OPERATION_CASES,
)
@pytest.mark.asyncio
async def test_every_other_shipped_operation_is_preflighted_rendered_and_taints_lineage(
    settings,
    storage,
    prompt: str,
    tool_name: str,
    expected_arguments: dict[str, Any],
    rendered_fragment: str,
    mutating: bool,
) -> None:
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)
    context, message_id = _durable_context(storage, prompt)

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        prompt,
        _actor(),
        _all_tools(),
        None,
    )

    expected_names = (
        ["obsidian_list_vaults"]
        if tool_name == "obsidian_list_vaults"
        else ["obsidian_list_vaults", "obsidian_read_note", tool_name]
        if tool_name == "obsidian_replace_note"
        else ["obsidian_list_vaults", tool_name]
    )
    assert llm.calls == 0
    assert [name for name, _ in kernel.executed] == expected_names
    if tool_name != "obsidian_list_vaults":
        assert kernel.executed[0] == ("obsidian_list_vaults", {})
    selected_arguments = kernel.executed[-1][1]
    expected_selected_arguments = dict(expected_arguments)
    if mutating:
        expected_operation_id = obsidian_operation_id(
            storage,
            "alice",
            message_id,
            tool_name,
        )
        expected_selected_arguments["operation_id"] = expected_operation_id
        if tool_name == "obsidian_replace_note":
            expected_selected_arguments["expected_revision"] = _REVISION
        assert f"Operation ID: {expected_operation_id}" in result["content"]
    assert selected_arguments == expected_selected_arguments
    assert rendered_fragment in result["content"]
    assert result["tools_used"] == expected_names
    assert [item["tool"] for item in result["tool_evidence"]] == [tool_name]
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "succeeded"
    assert result["_obsidian_private_lineage_owned"] is True
    assert context.private_source_boundary_active is True
    stored = storage.get_message(message_id, "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["private_context_lineage"] is True


@pytest.mark.asyncio
async def test_replace_resumes_with_the_frozen_ledger_revision_without_a_second_read(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = (
        "Замени в Obsidian содержимое заметки «Projects/Friday Test.md» целиком на текст: «Новая версия»."
    )
    kernel = _ConversationKernel()
    runtime = _runtime(settings, storage, kernel, _ConversationLLM())
    context, message_id = _durable_context(storage, prompt)
    operation_id = obsidian_operation_id(storage, "alice", message_id, "obsidian_replace_note")
    frozen_revision = "b" * 64

    def continuation(owner_id: str, requested_operation_id: str) -> dict[str, Any] | None:
        assert (owner_id, requested_operation_id) == ("alice", operation_id)
        return {"method": "replace", "expected_revision": frozen_revision}

    monkeypatch.setattr(storage, "get_obsidian_operation", continuation)

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        prompt,
        _actor(),
        _all_tools(),
        None,
    )

    assert [name for name, _ in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_replace_note",
    ]
    assert kernel.executed[-1][1]["expected_revision"] == frozen_revision
    assert result["_obsidian_outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_replace_without_the_code_owned_read_capability_never_mutates(
    settings,
    storage,
) -> None:
    prompt = (
        "Замени в Obsidian содержимое заметки «Projects/Friday Test.md» целиком на текст: «Новая версия»."
    )
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        prompt,
        _actor(),
        [
            _schema("obsidian_list_vaults"),
            _schema("obsidian_replace_note"),
            _schema("web_search"),
        ],
        None,
    )

    assert llm.calls == 0
    assert kernel.executed == [("obsidian_list_vaults", {})]
    assert result["_obsidian_outcome"] == "unavailable"
    assert "изменений не было" in result["content"].casefold()


@pytest.mark.asyncio
async def test_fresh_write_revocation_after_preflight_blocks_the_mutator(settings, storage) -> None:
    prompt = "Добавь в Obsidian в заметку «Projects/Friday Test.md» текст: «Новая строка»."
    kernel = _ConversationKernel(denied_capabilities=frozenset({"obsidian.write"}))
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        prompt,
        _actor(),
        _all_tools(),
        None,
    )

    assert llm.calls == 0
    assert kernel.executed == [("obsidian_list_vaults", {})]
    assert kernel.authorization.calls == [
        ("alice", "obsidian.read"),
        ("alice", "obsidian.write"),
    ]
    assert result["tools_used"] == ["obsidian_list_vaults"]
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "denied"
    assert "право" in result["content"].casefold()
    assert "заметка создана" not in result["content"].casefold()


@pytest.mark.asyncio
async def test_selected_schema_absence_blocks_before_preflight_or_model(settings, storage) -> None:
    prompt = "Прочитай в Obsidian заметку «Projects/Friday Test.md»."
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        prompt,
        _actor(),
        [_schema("obsidian_list_vaults"), _schema("web_search")],
        None,
    )

    assert llm.calls == 0
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "unavailable"
    assert "недоступ" in result["content"].casefold()


@pytest.mark.asyncio
async def test_preflight_schema_absence_blocks_a_note_mutation(settings, storage) -> None:
    prompt = "Добавь в Obsidian в заметку «Projects/Friday Test.md» текст: «Новая строка»."
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)

    result = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        prompt,
        _actor(),
        [_schema("obsidian_append_note"), _schema("web_search")],
        None,
    )

    assert llm.calls == 0
    assert kernel.executed == []
    assert result["tools_used"] == []
    assert result["_obsidian_owned"] is True
    assert result["_obsidian_outcome"] == "unavailable"
    assert "недоступ" in result["content"].casefold()


@pytest.mark.asyncio
async def test_private_lineage_failure_blocks_a_mutation_after_preflight(
    settings,
    storage,
    monkeypatch,
) -> None:
    prompt = "Добавь в Obsidian в заметку «Projects/Friday Test.md» текст: «Новая строка»."
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)
    context, _ = _durable_context(storage, prompt)
    monkeypatch.setattr(runtime, "_persist_source_search_private_lineage", lambda _context: False)

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        prompt,
        _actor(),
        _all_tools(),
        None,
    )

    assert llm.calls == 0
    assert kernel.executed == [("obsidian_list_vaults", {})]
    assert result["tools_used"] == ["obsidian_list_vaults"]
    assert result["_obsidian_outcome"] == "unavailable"
    assert "операция не запускалась" in result["content"].casefold()


@pytest.mark.asyncio
async def test_daily_note_uses_the_root_turn_date_after_midnight(settings, storage) -> None:
    prompt = "Добавь в Obsidian в сегодняшнюю ежедневную заметку текст: «Итог дня»."
    kernel = _ConversationKernel()
    llm = _ConversationLLM()
    runtime = _runtime(settings, storage, kernel, llm)
    context, _ = _durable_context(storage, prompt)
    context.effect_local_date = date(2026, 8, 22)
    runtime._local_today = lambda: date(2026, 8, 23)  # type: ignore[method-assign]

    await runtime._agentic_loop(  # noqa: SLF001
        context,
        prompt,
        _actor(),
        _all_tools(),
        None,
    )

    assert [name for name, _ in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_daily_note",
    ]
    assert kernel.executed[-1][1]["day"] == "2026-08-22"
