from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
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


class _AnswerModel:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.offered: list[str] = []

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
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


class _CommandKernel:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict[str, object], ActorContext]] = []

    def get_tool(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(risk="high", timeout_sec=None)

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
    } <= set(model.offered)
    assert "engineer_compile_java" not in model.offered
    assert "engineer_decompile_artifact" not in model.offered


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
    digest = hashlib.sha256(b"msg_0123456789abcdef\x00live-command-step").hexdigest()[:32]
    assert arguments["_step_id"] == f"ecstep-{digest}"


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
