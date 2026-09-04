from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday.agent_runtime import (
    AUTONOMOUS_ENGINEER_SYSTEM_PROMPT,
    AgentContext,
    AgentRuntime,
    _engineer_initial_model_phase,
    _engineer_planning_only_request,
    _engineer_tool_result_requires_replan,
)
from friday.agent_runtime.llm import LLMRouter
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
    assert "plan → execute → observe → replan" in folded
    assert "успех одной команды" in folded


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Как там статус фоновой задачи?", ("status", False)),
        ("Готов ли план миграции?", ("status", False)),
        ("Готова ли стратегия?", ("status", False)),
        ("Какой прогресс по плану?", ("status", False)),
        ("Запусти uname -a.", ("execute", False)),
        ("Составь план миграции сервиса.", ("plan", True)),
        ("Мне нужен пошаговый план миграции сервиса.", ("plan", True)),
        ("Составь план остановки сервиса.", ("plan", True)),
        ("Пошаговый план остановки сервиса.", ("plan", True)),
        ("Перепланируй следующие шаги после сбоя.", ("plan", True)),
        ("Проведи аудит и диагностику узла.", ("plan", True)),
    ],
)
def test_engineer_phase_classifier_separates_status_execution_and_planning(
    message: str,
    expected: tuple[str, bool],
) -> None:
    assert _engineer_initial_model_phase(message, None) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Составь план миграции сервиса.", True),
        ("Сделай план миграции сервиса.", True),
        ("Составь план остановки сервиса.", True),
        ("Мне нужен план миграции сервиса.", True),
        ("Нужен пошаговый план миграции.", True),
        ("План миграции сервиса.", True),
        ("Пошаговый план миграции сервиса.", True),
        ("Стратегия миграции сервиса.", True),
        ("План остановки сервиса.", True),
        ("Пошаговый план остановки сервиса.", True),
        ("Стратегия отмены фоновой задачи.", True),
        ("Migration plan to stop the service.", True),
        ("Распиши план миграции.", True),
        ("Сформируй план миграции.", True),
        ("Подскажи план миграции.", True),
        ("Как лучше спланировать миграцию?", True),
        ("Could you draft a migration plan?", True),
        ("I need a migration plan.", True),
        ("What is a good migration plan?", True),
        ("Migration plan for the service.", True),
        ("I need a plan, including how to execute it.", False),
        ("I need a plan, and I will execute it myself.", False),
        ("Составь план, как выполнить миграцию.", False),
        ("Спланируй комплексный анализ неизвестного сервиса.", True),
        ("Спланируй и выполни комплексный анализ сервиса.", False),
        ("Составь план и затем аккуратно выполни его.", False),
        ("Составь план, затем проверь сервис.", False),
        ("Составь план миграции. Выполни его.", False),
        ("Составь план миграции.\nПриступай к выполнению.", False),
        ("Create a migration plan, then implement it.", False),
        ("Plan migration. Execute it.", False),
        ("Plan migration, then carry it out.", False),
        ("Plan migration and carefully execute it.", False),
        ("Продолжи работу по этому плану", False),
        ("План не сработал, почини", False),
        ("Составь план и сохрани его в файл", False),
        ("Составь план и отправь мне документом", False),
        ("План готов — действуй", False),
        ("Составь план и приступи", False),
        ("Составь план и запиши его подробно.", False),
        ("Create a plan and write it clearly.", False),
        ("Сделай подробный и безопасный план.", True),
        ("Сделай аудит и составь план.", False),
        ("Сделай аудит и план исправлений.", False),
        ("Проведи аудит и сначала составь план.", False),
        ("Проанализируй существующий план миграции.", False),
        ("Execute the migration plan.", False),
        ("Отмени план миграции.", False),
        ("Создай файл plan.txt.", False),
        ("Как там статус плана?", False),
        ("Готов ли план миграции?", False),
        ("Готова ли стратегия?", False),
        ("Какой прогресс по плану?", False),
    ],
)
def test_engineer_planning_only_policy_separates_plan_artifact_from_execution(
    message: str,
    expected: bool,
) -> None:
    assert _engineer_planning_only_request(message) is expected


def test_engineer_replan_signal_includes_semantic_failure_and_render_truncation() -> None:
    semantic_failure = ToolResult(
        "engineer_command_run",
        True,
        data={"ok": False, "error": "provider refused"},
    )
    large_result = ToolResult("engineer_command_run", True, data="x" * 13_000)

    assert _engineer_tool_result_requires_replan(semantic_failure) is True
    assert _engineer_tool_result_requires_replan(large_result) is False
    large_result.to_llm_message()
    assert _engineer_tool_result_requires_replan(large_result) is True


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


class _PhaseScriptModel(LLMRouter):
    """Real-router type with deterministic generations and captured controls."""

    def __init__(self, settings, script: list[dict[str, object]]) -> None:  # noqa: ANN001
        super().__init__(replace(settings, llm_enabled=True))
        self.script = script
        self.call_controls: list[dict[str, object]] = []
        self.message_snapshots: list[list[dict[str, object]]] = []

    async def chat(  # type: ignore[override]
        self,
        _messages,
        *,
        tools=None,
        enable_thinking=None,
        max_tokens=None,
        **_kwargs,
    ):
        offered_names = [str((item.get("function") or {}).get("name") or "") for item in (tools or [])]
        self.message_snapshots.append([dict(item) for item in _messages])
        self.call_controls.append(
            {
                "enable_thinking": enable_thinking,
                "max_tokens": max_tokens,
                "tools": offered_names,
                "require_full_context": bool(_kwargs.get("require_full_context", False)),
            }
        )
        result = dict(self.script[len(self.call_controls) - 1])
        result.setdefault("tool_calls", None)
        result.setdefault("finish_reason", "stop")
        result["_offered_tool_names"] = offered_names
        return result


def _command_call(call_id: str, command: str) -> dict[str, object]:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {
                    "name": "engineer_command_run",
                    "arguments": json.dumps({"command": command}),
                },
            }
        ],
        "finish_reason": "tool_calls",
    }


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


class _SelectiveReplanKernel(_CommandKernel):
    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        result = await super().execute(name, arguments, actor=actor)
        if len(self.executions) == 2 and isinstance(result.data, dict):
            result.data["requires_follow_up"] = True
        return result


class _FirstSemanticFailureThenSuccessKernel(_CommandKernel):
    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        self.executions.append((name, dict(arguments), actor))
        if len(self.executions) == 1:
            return ToolResult(
                name,
                True,
                data={"ok": False, "error": "provider refused", "status": "failed"},
            )
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "status": "completed",
                "exit_code": 0,
                "stdout": "recovered",
                "stderr": "",
            },
        )


class _GroundedResolutionKernel(_CommandKernel):
    def __init__(self, *, blocked: bool) -> None:
        super().__init__()
        self.blocked = blocked

    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        self.executions.append((name, dict(arguments), actor))
        if self.blocked:
            return ToolResult(
                name,
                False,
                data={
                    "ok": False,
                    "status": "failed",
                    "error_code": "external_blocker",
                },
                error="External access is unavailable",
            )
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "status": "completed",
                "changed": True,
                "goal_complete": True,
                "stdout": "verified",
                "stderr": "",
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
async def test_complex_engineer_task_uses_bounded_plan_replan_and_off_final(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("inspect", "printf inspect"),
            _command_call("verify", "printf verify"),
            {
                "content": "Планировочный вывод: обе проверки завершены.",
                "tool_calls": None,
                "finish_reason": "stop",
            },
            _command_call("confirm", "printf confirm"),
            {
                "content": "Обе проверки выполнены и подтверждены.",
                "tool_calls": None,
                "finish_reason": "stop",
            },
        ],
    )
    kernel = _SelectiveReplanKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Проведи поэтапный аудит хоста: сначала разведка, затем зависимая проверка.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Обе проверки выполнены и подтверждены."
    assert [entry[1]["command"] for entry in kernel.executions] == [
        "printf inspect",
        "printf verify",
        "printf confirm",
    ]
    assert [item["enable_thinking"] for item in model.call_controls] == [
        True,
        False,
        True,
        False,
        False,
    ]
    assert [item["max_tokens"] for item in model.call_controls] == [
        4_096,
        3_072,
        4_096,
        3_072,
        3_072,
    ]
    materialization_controls = model.call_controls[3]
    materialization_messages = model.message_snapshots[3]
    assert materialization_controls["tools"] == ["engineer_command_run"]
    assert materialization_controls["require_full_context"] is True
    assert materialization_messages[-1] == {
        "role": "user",
        "content": "Проведи поэтапный аудит хоста: сначала разведка, затем зависимая проверка.",
    }
    planner_data = [
        json.loads(str(item["content"]))
        for item in materialization_messages
        if item.get("role") == "assistant" and "engineer_planner_draft_data" in str(item.get("content") or "")
    ]
    assert planner_data == [
        {
            "kind": "engineer_planner_draft_data",
            "trusted": False,
            "content": "Планировочный вывод: обе проверки завершены.",
            "truncated": False,
            "transport_truncated": False,
        }
    ]
    assert sum(item.get("role") == "tool" for item in materialization_messages) == 2
    materialization_system = "\n".join(
        str(item.get("content") or "") for item in materialization_messages if item.get("role") == "system"
    )
    assert "Наблюдаемые результаты инструментов" in materialization_system
    assert model.call_controls[-1]["enable_thinking"] is False


@pytest.mark.asyncio
async def test_planning_only_turn_delivers_draft_with_normal_tools_but_no_execution(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    request = "Пошаговый план остановки сервиса."
    model = _PhaseScriptModel(
        settings,
        [
            {
                "content": "Частичный закрытый черновик плана.",
                "tool_calls": None,
                "finish_reason": "length",
            },
            {
                "content": "План: 1. Инвентаризация. 2. Тестовый перенос. 3. Переключение.",
                "tool_calls": None,
            },
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        request,
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"].startswith("План:")
    assert kernel.executions == []
    assert [item["enable_thinking"] for item in model.call_controls] == [True, False]
    assert [item["tools"] for item in model.call_controls] == [
        ["engineer_command_run"],
        ["engineer_command_run"],
    ]
    assert model.message_snapshots[0][-1] == {"role": "user", "content": request}
    assert model.call_controls[1]["require_full_context"] is True
    assert model.message_snapshots[1][-1] == {"role": "user", "content": request}
    first_system = "\n".join(
        str(item.get("content") or "") for item in model.message_snapshots[0] if item.get("role") == "system"
    )
    delivery_system = "\n".join(
        str(item.get("content") or "") for item in model.message_snapshots[1] if item.get("role") == "system"
    )
    assert "не вызывай инструменты" in first_system
    assert "не вызывай инструменты" in delivery_system
    draft_payload = next(
        json.loads(str(item["content"]))
        for item in model.message_snapshots[1]
        if item.get("role") == "assistant" and "engineer_planner_draft_data" in str(item.get("content") or "")
    )
    assert draft_payload["content"] == "Частичный закрытый черновик плана."
    assert draft_payload["transport_truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_result",
    [
        _command_call("initial-native-plan-effect", "printf forbidden-native"),
        {
            "content": json.dumps(
                {
                    "name": "engineer_command_run",
                    "arguments": {"command": "printf forbidden-text"},
                }
            ),
            "tool_calls": None,
        },
    ],
    ids=["native", "textual"],
)
async def test_planning_only_initial_tool_call_gets_one_no_effect_plan_delivery(
    settings,
    storage,
    initial_result: dict[str, object],
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    request = "Сделай план миграции сервиса."
    model = _PhaseScriptModel(
        settings,
        [
            initial_result,
            {"content": "План: 1. Инвентаризация. 2. Тестовый перенос.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        request,
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"].startswith("План:")
    assert kernel.executions == []
    assert [item["enable_thinking"] for item in model.call_controls] == [True, False]
    assert [item["tools"] for item in model.call_controls] == [
        ["engineer_command_run"],
        ["engineer_command_run"],
    ]
    assert model.call_controls[1]["require_full_context"] is True
    assert model.message_snapshots[1][-1] == {"role": "user", "content": request}
    delivery_system = "\n".join(
        str(item.get("content") or "") for item in model.message_snapshots[1] if item.get("role") == "system"
    )
    assert "tool call отклонён до исполнения" in delivery_system


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery_result",
    [
        _command_call("delivery-native-plan-effect", "printf forbidden-native"),
        {
            "content": json.dumps(
                {
                    "name": "engineer_command_run",
                    "arguments": {"command": "printf forbidden-text"},
                }
            ),
            "tool_calls": None,
        },
    ],
    ids=["native", "textual"],
)
async def test_planning_only_delivery_tool_call_fails_without_effect_or_third_call(
    settings,
    storage,
    delivery_result: dict[str, object],
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            {"content": "Черновик плана.", "tool_calls": None},
            delivery_result,
            {"content": "Этот третий ответ не должен запрашиваться.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Сделай план миграции сервиса.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["llm_failed"] is True
    assert response["_model_generated"] is False
    assert kernel.executions == []
    assert len(model.call_controls) == 2
    assert [item["enable_thinking"] for item in model.call_controls] == [True, False]
    assert [item["tools"] for item in model.call_controls] == [
        ["engineer_command_run"],
        ["engineer_command_run"],
    ]


@pytest.mark.asyncio
async def test_planning_only_repeated_tool_call_after_edge_repair_fails_without_effect(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("initial-plan-effect", "printf first-forbidden"),
            _command_call("repeated-plan-effect", "printf second-forbidden"),
            {"content": "Третий ответ не должен запрашиваться.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Составь план миграции сервиса.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["llm_failed"] is True
    assert kernel.executions == []
    assert len(model.call_controls) == 2
    assert [item["enable_thinking"] for item in model.call_controls] == [True, False]


@pytest.mark.asyncio
async def test_planning_only_invalid_delivery_is_not_repaired_with_a_third_call(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            {"content": "Черновик плана.", "tool_calls": None},
            {"content": "Сейчас запущу миграцию.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Составь план миграции сервиса.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["llm_failed"] is True
    assert kernel.executions == []
    assert len(model.call_controls) == 2
    assert [item["tools"] for item in model.call_controls] == [
        ["engineer_command_run"],
        ["engineer_command_run"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_text",
    [
        "Продолжи работу по этому плану",
        "План не сработал, почини",
        "Составь план и сохрани его в файл",
        "Составь план и отправь мне документом",
        "План готов — действуй",
        "Составь план и приступи",
    ],
)
async def test_plan_wording_with_execution_authority_keeps_normal_tool_protocol(
    settings,
    storage,
    monkeypatch,
    owner_text: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("authorized-plan-work", "printf continued"),
            {"content": "Работа выполнена и подтверждена.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        owner_text,
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert _engineer_planning_only_request(owner_text) is False
    assert response["content"] == "Работа выполнена и подтверждена."
    assert [item[1]["command"] for item in kernel.executions] == ["printf continued"]
    assert [item["tools"] for item in model.call_controls] == [
        ["engineer_command_run"],
        ["engineer_command_run"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("materialized_content", ["Всё уже готово.", ""])
async def test_operational_planner_prose_cannot_terminate_without_observable_step(
    settings,
    storage,
    materialized_content: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            {"content": "Сначала проверю сервис.", "tool_calls": None},
            {"content": materialized_content, "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Проведи комплексный аудит сервиса.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["llm_failed"] is True
    assert response["_model_generated"] is False
    assert "Всё уже готово" not in response["content"]
    assert kernel.executions == []
    assert len(model.call_controls) == 2
    assert [item["enable_thinking"] for item in model.call_controls] == [True, False]


@pytest.mark.asyncio
async def test_operational_planner_materialization_requires_one_nearest_tool_call(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    first_call = _command_call("first-materialized", "printf first")
    second_call = _command_call("second-materialized", "printf second")
    first_calls = first_call["tool_calls"]
    second_calls = second_call["tool_calls"]
    assert isinstance(first_calls, list)
    assert isinstance(second_calls, list)
    model = _PhaseScriptModel(
        settings,
        [
            {"content": "Нужны две проверки.", "tool_calls": None},
            {
                "content": "",
                "tool_calls": [*first_calls, *second_calls],
                "finish_reason": "tool_calls",
            },
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Проведи комплексный аудит сервиса.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["llm_failed"] is True
    assert kernel.executions == []
    assert len(model.call_controls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocked", "grounded_answer", "evidence_marker"),
    [
        (False, "Проверка завершена: наблюдаемый результат — verified.", "verified"),
        (True, "Продолжение невозможно: внешний доступ недоступен.", "External access is unavailable"),
    ],
)
async def test_replan_draft_accepts_evidence_grounded_final_or_blocker_without_extra_tool(
    settings,
    storage,
    monkeypatch,
    blocked: bool,
    grounded_answer: str,
    evidence_marker: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    request = "Проверь состояние сервиса."
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("grounded-first", "printf inspect"),
            {
                "content": "Черновой вывод после наблюдаемого результата.",
                "tool_calls": None,
            },
            {"content": grounded_answer, "tool_calls": None},
        ],
    )
    kernel = _GroundedResolutionKernel(blocked=blocked)
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        request,
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == grounded_answer
    assert len(kernel.executions) == 1
    assert [item["enable_thinking"] for item in model.call_controls] == [False, True, False]
    assert [item["tools"] for item in model.call_controls] == [
        ["engineer_command_run"],
        ["engineer_command_run"],
        ["engineer_command_run"],
    ]
    resolution_messages = model.message_snapshots[2]
    assert model.call_controls[2]["require_full_context"] is True
    assert resolution_messages[-1] == {"role": "user", "content": request}
    assert any(item.get("role") == "tool" for item in resolution_messages)
    assert evidence_marker in "\n".join(
        str(item.get("content") or "") for item in resolution_messages if item.get("role") == "tool"
    )
    resolution_system = "\n".join(
        str(item.get("content") or "") for item in resolution_messages if item.get("role") == "system"
    )
    assert "либо краткий итог" in resolution_system
    assert "либо конкретный blocker" in resolution_system


@pytest.mark.asyncio
async def test_simple_command_failure_gets_one_bounded_replan(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("simple-first", "printf first"),
            _command_call("simple-replanned", "printf recovered"),
            {"content": "Повторный шаг подтверждён.", "tool_calls": None},
        ],
    )
    kernel = _FirstSemanticFailureThenSuccessKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Запусти printf first.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Повторный шаг подтверждён."
    assert [item[1]["command"] for item in kernel.executions] == [
        "printf first",
        "printf recovered",
    ]
    assert [item["enable_thinking"] for item in model.call_controls] == [False, True, False]
    assert [item["max_tokens"] for item in model.call_controls] == [3_072, 4_096, 3_072]


@pytest.mark.asyncio
async def test_simple_engineer_command_keeps_thinking_off(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("simple", "printf simple"),
            {"content": "Команда подтверждена.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Запусти printf simple.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Команда подтверждена."
    assert [item["enable_thinking"] for item in model.call_controls] == [False, False]
    assert [item["max_tokens"] for item in model.call_controls] == [3_072, 3_072]


@pytest.mark.asyncio
async def test_engineer_status_keeps_thinking_off(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "status",
                        "function": {
                            "name": "engineer_command_status",
                            "arguments": "{}",
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "Задача завершена.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Как там статус фоновой задачи?",
        actor,
        tools=[_schema("engineer_command_status")],
        attachments=None,
    )

    assert response["content"] == "Задача завершена."
    assert [entry[0] for entry in kernel.executions] == ["engineer_command_status"]
    assert [item["enable_thinking"] for item in model.call_controls] == [False, False]
    assert [item["max_tokens"] for item in model.call_controls] == [3_072, 3_072]


@pytest.mark.asyncio
async def test_failed_engineer_status_remains_on_closed_no_thinking_lane(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "failed-status",
                        "function": {
                            "name": "engineer_command_status",
                            "arguments": "{}",
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "Статус получить не удалось.", "tool_calls": None},
        ],
    )
    kernel = _FirstSemanticFailureThenSuccessKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Как там статус фоновой задачи?",
        actor,
        tools=[_schema("engineer_command_status")],
        attachments=None,
    )

    assert response["content"] == "Статус получить не удалось."
    assert [item["enable_thinking"] for item in model.call_controls] == [False, False]


@pytest.mark.asyncio
async def test_no_thinking_public_length_answer_preserves_complete_partial_text(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            _command_call("length-public", "printf checked"),
            {
                "content": (
                    "Проверка завершена и подтверждена фактическим результатом команды. "
                    "Дополнительная часть оборвана"
                ),
                "tool_calls": None,
                "finish_reason": "length",
            },
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Запусти printf checked.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"].startswith(
        "Проверка завершена и подтверждена фактическим результатом команды."
    )
    assert "Ответ сокращён до последнего завершённого предложения" in response["content"]
    assert response["_model_output_truncated"] is True
    assert [item["enable_thinking"] for item in model.call_controls] == [False, False]


@pytest.mark.asyncio
async def test_reasoning_exhaustion_gets_one_no_thinking_recovery(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _PhaseScriptModel(
        settings,
        [
            {
                "content": "private reasoning without a completed answer",
                "tool_calls": None,
                "finish_reason": "length",
            },
            {
                **_command_call("recovered", "printf recovered"),
            },
            {"content": "Анализ восстановлен и подтверждён.", "tool_calls": None},
        ],
    )
    kernel = _CommandKernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Спланируй и выполни комплексный анализ неизвестного сервиса.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["content"] == "Анализ восстановлен и подтверждён."
    assert [entry[1]["command"] for entry in kernel.executions] == ["printf recovered"]
    assert [item["enable_thinking"] for item in model.call_controls] == [True, False, False]
    assert [item["max_tokens"] for item in model.call_controls] == [4_096, 3_072, 3_072]


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
            "content": "Состояние Engineer-задачи `" + "3" * 32 + "` неизвестно.",
            "metadata_json": json.dumps({"engineer_command_unknown": {"job_id": "3" * 32}}),
        },
        {
            "role": "assistant",
            "content": "Состояние Engineer-задачи `" + "4" * 32 + "` неизвестно.",
            "metadata_json": "{}",
        },
        {
            "role": "assistant",
            "content": (
                "Engineer-задание 080521363782558983 завершено. Проверенный архив результата приложен."
            ),
            "metadata_json": "{}",
        },
        {
            "role": "assistant",
            "content": ("Engineer-задание 180521363782558983 завершено. Файл результата приложен."),
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
    assert not any("Состояние Engineer-задачи" in item for item in rendered)


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
