from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday.agent_runtime import AUTONOMOUS_ENGINEER_SYSTEM_PROMPT, AgentContext, AgentRuntime
from friday.execution_kernel import ToolResult
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext


def _schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def test_autonomous_prompt_requires_staged_unbounded_long_work() -> None:
    folded = AUTONOMOUS_ENGINEER_SYSTEM_PROMPT.casefold()
    assert "выполняй стадийно" in folded
    assert "durable job без угаданного дедлайна" in folded
    assert "не останавливайся после одной команды" in folded


class _AnswerModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.offered: list[str] = []
        self.messages: list[dict[str, object]] = []

    async def chat(self, messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.messages = [dict(item) for item in messages]
        self.offered = [str((item.get("function") or {}).get("name") or "") for item in (tools or [])]
        return {
            "content": "Готово.",
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _CommandModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
        self.calls = 0

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "live-command-step",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps(self.arguments),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {
            "content": "Команда выполнена.",
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _NamedToolModel(_CommandModel):
    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        super().__init__(arguments)
        self.name = name

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "autonomous-tool-step",
                        "function": {
                            "name": self.name,
                            "arguments": json.dumps(self.arguments),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {
            "content": "Готово.",
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _RepeatedNativeIdCommandModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls <= 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        # SGLang/OpenAI-compatible transports may restart ids on
                        # every generation; this value deliberately repeats.
                        "id": "call_0",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": f"printf step-{self.calls}"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {"content": "Оба шага выполнены.", "tool_calls": None, "finish_reason": "stop"}


class _RepeatedFailedCommandModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"failed-command-{self.calls}",
                    "function": {
                        "name": "engineer_command_run",
                        "arguments": json.dumps({"command": "nmap 192.168.1.35", "timeout_sec": 300}),
                    },
                }
            ],
            "finish_reason": "tool_calls",
        }


class _TerminalImitationThenCommandModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return {
                "content": (
                    "Engineer-задание 91635651342541428833561255858983 завершено. "
                    "Проверенный архив результата приложен."
                ),
                "tool_calls": None,
                "finish_reason": "stop",
            }
        if self.calls == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "repaired-command",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": "printf repaired"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {"content": "Проверка выполнена: repaired.", "tool_calls": None, "finish_reason": "stop"}


class _UnstartedProgressThenCommandModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self, claim: str = "Собираю сводку по файлу hui2.exe.") -> None:
        self.calls = 0
        self.claim = claim
        self.tool_choices: list[str | None] = []

    async def chat(self, _messages, *, tools=None, tool_choice=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        self.tool_choices.append(tool_choice)
        if self.calls == 1:
            return {
                "content": self.claim,
                "tool_calls": None,
                "finish_reason": "stop",
            }
        if self.calls == 2:
            assert tool_choice == "engineer_command_run"
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "forced-command-after-false-progress",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": "printf inspected"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {
            "content": "Файл проверен: inspected.",
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _CommandThenProviderFailureModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "command-before-provider-failure",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": "printf inspected"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        raise ConnectionError("primary disappeared after command")


class _CommandKernel:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict[str, object], ActorContext]] = []

    def get_tool(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(risk="high", timeout_sec=None)

    @staticmethod
    def tool_is_approval_free(name: str) -> bool:
        return name not in {
            "code_run",
            "conflict_decide",
            "entity_merge_decide",
            "host_action_run",
            "mission_compensation",
        }

    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        self.executions.append((name, dict(arguments), actor))
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "status": "completed",
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
            },
        )


class _FailingCommandKernel(_CommandKernel):
    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        self.executions.append((name, dict(arguments), actor))
        return ToolResult(
            name,
            False,
            data={"ok": False, "error_code": "resource_boundary_unproven", "status": "failed"},
            error="Host control refused: resource_boundary_unproven",
        )


class _StatusRecoveryCommandKernel(_CommandKernel):
    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        self.executions.append((name, dict(arguments), actor))
        if name == "engineer_command_status":
            return ToolResult(
                name,
                True,
                data={
                    "ok": True,
                    "job_id": "1" * 32,
                    "status": "completed",
                    "exit_code": 0,
                    "stdout": "verified scan output",
                    "stderr": "",
                },
            )
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "job_id": "1" * 32,
                "status": "completed",
                "exit_code": 0,
                "stdout": "verified scan output",
                "stderr": "",
            },
        )


class _RunningCommandKernel(_CommandKernel):
    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        self.executions.append((name, dict(arguments), actor))
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "job_id": "2" * 32,
                "status": "running",
                "stdout_bytes": 22,
                "stderr_bytes": 0,
            },
        )


def _context(*, private: bool = False) -> AgentContext:
    return AgentContext(
        conversation_id="conv-autonomous-engineer",
        user_id=LEGACY_OWNER_USER_ID,
        person_id=LEGACY_OWNER_USER_ID,
        interaction_mode="engineer",
        source_search_lineage_user_message_id="msg_0123456789abcdef",
        effect_root_user_message_id="msg_fedcba9876543210",
        engineer_command_telegram_update_id="123456",
        private_source_boundary_active=private,
        terse_request=private,
    )


@pytest.mark.asyncio
async def test_autonomous_engineer_retains_actor_tools_and_retires_bounded_wrappers(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _AnswerModel()
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            engineer_command_enabled=True,
        ),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=_CommandKernel(),  # type: ignore[arg-type]
    )

    await runtime._agentic_loop(  # noqa: SLF001
        _context(private=True),
        "Разберись автономно.",
        actor,
        tools=[
            _schema("engineer_command_run"),
            _schema("engineer_command_status"),
            _schema("memory_save"),
            _schema("web_search"),
            _schema("archive_search"),
            _schema("obsidian_create_note"),
            _schema("make_file"),
            _schema("code_run"),
            _schema("conflict_decide"),
            _schema("entity_merge_decide"),
            _schema("host_action_run"),
            _schema("mission_compensation"),
            _schema("engineer_compile_java"),
            _schema("engineer_decompile_artifact"),
        ],
        attachments=None,
    )

    assert {
        "engineer_command_run",
        "engineer_command_status",
        "memory_save",
        "web_search",
        "archive_search",
        "obsidian_create_note",
        "make_file",
    } <= set(model.offered)
    assert "engineer_compile_java" not in model.offered
    assert "engineer_decompile_artifact" not in model.offered
    assert "code_run" not in model.offered
    assert "conflict_decide" not in model.offered
    assert "entity_merge_decide" not in model.offered
    assert "host_action_run" not in model.offered
    assert "mission_compensation" not in model.offered
    system_text = "\n".join(
        str(item.get("content") or "") for item in model.messages if item.get("role") == "system"
    )
    assert "автономный инженер владельца" in system_text
    assert "НИКОГДА не приписывай ответ архиву" not in system_text


@pytest.mark.asyncio
async def test_autonomous_engineer_cannot_fall_back_to_legacy_approval_tools(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _NamedToolModel(
        "conflict_decide",
        {"conflict_id": "conflict-model-invented", "decision": "keep_a"},
    )
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Реши задачу автономно без подтверждений.",
        actor,
        tools=[_schema("conflict_decide"), _schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Готово."
    assert storage.count_action_approvals(actor.own_id) == 0


@pytest.mark.asyncio
async def test_autonomous_command_uses_exact_current_message_and_code_owned_step(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _CommandModel({"command": "printf '%s\\n' ok"})
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            engineer_command_enabled=True,
        ),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Запусти printf и проверь результат.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Команда выполнена."
    assert len(kernel.executions) == 1
    name, arguments, execution_actor = kernel.executions[0]
    assert name == "engineer_command_run"
    assert execution_actor == actor
    assert arguments["command"] == "printf '%s\\n' ok"
    assert arguments["_conversation_id"] == "conv-autonomous-engineer"
    assert arguments["_source_message_id"] == "msg_0123456789abcdef"
    assert arguments["_telegram_update_id"] == "123456"
    digest = hashlib.sha256(b"msg_0123456789abcdef\x00engineer-command-step\x001").hexdigest()[:32]
    assert arguments["_step_id"] == f"ecstep-{digest}"


@pytest.mark.asyncio
async def test_repeated_native_call_ids_get_distinct_code_owned_step_identities(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _RepeatedNativeIdCommandModel()
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Выполни два зависимых шага.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Оба шага выполнены."
    assert [item[1]["command"] for item in kernel.executions] == ["printf step-1", "printf step-2"]
    step_ids = [str(item[1]["_step_id"]) for item in kernel.executions]
    assert len(set(step_ids)) == 2
    expected = [
        "ecstep-"
        + hashlib.sha256(f"msg_0123456789abcdef\x00engineer-command-step\x00{ordinal}".encode()).hexdigest()[
            :32
        ]
        for ordinal in (1, 2)
    ]
    assert step_ids == expected


@pytest.mark.asyncio
async def test_identical_failed_autonomous_command_is_not_executed_twice(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _RepeatedFailedCommandModel()
    kernel = _FailingCommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Просканируй хост.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert len(kernel.executions) == 1
    assert model.calls == 2
    assert "resource_boundary_unproven" in response["content"]
    assert "повторно не запускалась" in response["content"]


@pytest.mark.asyncio
async def test_reserved_terminal_imitation_is_repaired_into_real_command(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _TerminalImitationThenCommandModel()
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Исследуй хост.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.calls == 3
    assert [entry[1]["command"] for entry in kernel.executions] == ["printf repaired"]
    assert response["content"] == "Проверка выполнена: repaired."


@pytest.mark.parametrize(
    "claim",
    [
        "Собираю сводку по файлу hui2.exe.",
        "Сделаю перевод интерфейса SpaceSniffer на русский.",
    ],
)
@pytest.mark.asyncio
async def test_unstarted_engineer_progress_is_repaired_into_a_real_command(
    settings,
    storage,
    monkeypatch,
    claim: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _UnstartedProgressThenCommandModel(claim)
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Дай сводку по приложенному EXE.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.tool_choices == [None, "engineer_command_run", None]
    assert [entry[1]["command"] for entry in kernel.executions] == ["printf inspected"]
    assert response["content"] == "Файл проверен: inspected."


@pytest.mark.asyncio
async def test_command_result_survives_primary_provider_failure(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _CommandThenProviderFailureModel()
    kernel = _StatusRecoveryCommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Просканируй хост и верни результат.",
        actor,
        tools=[_schema("engineer_command_run"), _schema("engineer_command_status")],
        attachments=None,
    )

    assert model.calls == 2
    assert [entry[0] for entry in kernel.executions] == [
        "engineer_command_run",
        "engineer_command_status",
    ]
    assert response["tools_used"] == ["engineer_command_run", "engineer_command_status"]
    assert response["llm_failed"] is True
    assert "проверенный сырой результат" in response["content"]
    assert "verified scan output" in response["content"]


@pytest.mark.asyncio
async def test_running_command_returns_durable_status_without_model_polling(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _CommandModel({"command": "nmap 192.168.1.35"})
    kernel = _RunningCommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Найди уязвимости хоста.",
        actor,
        tools=[_schema("engineer_command_run"), _schema("engineer_command_status")],
        attachments=None,
    )

    assert model.calls == 1
    assert [entry[0] for entry in kernel.executions] == ["engineer_command_run"]
    assert response["tools_used"] == ["engineer_command_run"]
    assert response["llm_failed"] is False
    assert "действительно запущена" in response["content"]
    assert "2" * 32 in response["content"]


@pytest.mark.asyncio
async def test_opaque_current_upload_reaches_command_owned_reauthorization_boundary(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _CommandModel({"command": 'file "$FRIDAY_INPUT_DIR/hui2.exe"'})
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    context = _context()
    context.source_effect_reauth_required = True
    context.source_effect_authority = None

    response = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Разбери приложенный EXE.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=[{"filename": "hui2.exe", "raw_id": "raw_0123456789abcdef"}],
        source_effect_reauth_required=True,
    )

    assert response["content"] == "Команда выполнена."
    assert len(kernel.executions) == 1


def test_autonomous_engineer_history_excludes_operational_and_fabricated_terminal_rows(
    settings,
    storage,
) -> None:
    model = _AnswerModel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=_CommandKernel(),  # type: ignore[arg-type]
    )
    context = _context()
    context.conversation_history = [
        {"role": "user", "content": "Предыдущая задача"},
        {
            "role": "assistant",
            "content": "Engineer-задание " + "1" * 32 + " завершено.",
            "metadata_json": json.dumps({"engineer_command_terminal": {"job_id": "1" * 32}}),
        },
        {
            "role": "assistant",
            "content": "Engineer-задание " + "2" * 32 + " выполняется.",
            "metadata_json": json.dumps({"engineer_command_progress": {"job_id": "2" * 32}}),
        },
        {
            "role": "assistant",
            "content": (
                "Engineer-задание 080521363782558983 завершено. Проверенный архив результата приложен."
            ),
            "metadata_json": "{}",
        },
        {"role": "assistant", "content": "Обычный проверенный ответ.", "metadata_json": "{}"},
    ]

    messages = runtime._build_initial_messages(  # noqa: SLF001
        context,
        "Новая задача",
        None,
        tool_enabled=True,
    )
    rendered = [str(item.get("content") or "") for item in messages]

    assert "Предыдущая задача" in rendered
    assert "Обычный проверенный ответ." in rendered
    assert not any("Engineer-задание" in item for item in rendered)


@pytest.mark.asyncio
async def test_model_cannot_supply_an_engineer_step_identity(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _CommandModel(
        {
            "command": "true",
            "_step_id": "ecstep-00000000000000000000000000000000",
        }
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            engineer_command_enabled=True,
        ),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Запусти true.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert kernel.executions == []


@pytest.mark.asyncio
async def test_autonomous_engineer_does_not_require_lexical_permission_for_actor_tool(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    arguments: dict[str, object] = {
        "kind": "txt",
        "filename": "model-chosen.txt",
        "content": "result",
    }
    model = _NamedToolModel("make_file", arguments)
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            engineer_command_enabled=True,
        ),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Самостоятельно выбери нужные инструменты и доведи задачу до результата.",
        actor,
        tools=[_schema("make_file")],
        attachments=None,
    )

    assert len(kernel.executions) == 1
    name, actual_arguments, execution_actor = kernel.executions[0]
    assert name == "make_file"
    assert actual_arguments == arguments
    assert execution_actor == actor
