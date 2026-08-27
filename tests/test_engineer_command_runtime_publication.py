"""AgentRuntime publication boundary for Engineer command result carriers."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult, ToolSpec
from friday.organs.engineer import ENGINEER_COMMAND_MANAGE, ENGINEER_USE
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService

_JOB_ID = "0123456789abcdef0123456789abcdef"
_PAYLOAD = b"PK\x03\x04engineer-command-result"
_FILENAME = f"engineer-command-{_JOB_ID}.zip"


class _StatusThenFinishModel:
    enabled = True
    model = "synthetic-engineer-command-status-model"
    total_budget_sec = 10.0

    def __init__(self, *, status_call_count: int = 1) -> None:
        self.status_requested = False
        self.status_call_count = status_call_count
        self.tool_result_messages: list[str] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        names = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        has_tool_result = any(str(item.get("role") or "") == "tool" for item in messages)
        if has_tool_result:
            self.tool_result_messages.extend(
                str(item.get("content") or "") for item in messages if str(item.get("role") or "") == "tool"
            )
        if "engineer_command_status" in names and not self.status_requested:
            self.status_requested = True
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"status-call-{index}",
                        "type": "function",
                        "function": {
                            "name": "engineer_command_status",
                            "arguments": json.dumps({"job_id": _JOB_ID}),
                        },
                    }
                    for index in range(self.status_call_count)
                ],
                "finish_reason": "tool_calls",
                "_queue_wait_sec": 0.0,
            }
        if has_tool_result:
            return {
                "content": "Статус Engineer-команды получен.",
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }

        system_text = "\n".join(
            str(item.get("content") or "") for item in messages if str(item.get("role") or "") == "system"
        )
        if "Ответь одним словом: РАЗГОВОР или ЗАПРОС." in system_text:
            content = "ЗАПРОС"
        elif "Классифицируй ТОЛЬКО чтение личной ленты/календаря" in system_text:
            content = '{"direction":"none","window_kind":"none"}'
        elif "Никаких пояснений, только JSON." in system_text:
            content = '{"вид":"действие","запрос":"","кто":"","дни":[]}'
        else:
            content = "Статус Engineer-команды получен."
        return {
            "content": content,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _StatusKernel(ExecutionKernel):
    def __init__(
        self,
        authorization: AuthorizationService,
        settings: Any,
        storage: Any,
        *,
        status: str,
        attachment: dict[str, Any] | None,
        revoke_after_status: bool = False,
    ) -> None:
        super().__init__(authorization, settings)
        self._test_storage = storage
        self._status = status
        self._attachment = attachment
        self._revoke_after_status = revoke_after_status
        self.seen_status_arguments: dict[str, Any] = {}
        self.status_execute_count = 0

        async def status_handler(*, actor: ActorContext, job_id: str, **_kwargs: Any) -> dict[str, Any]:
            del actor, job_id
            return {"ok": True}

        self.register(
            ToolSpec(
                name="engineer_command_status",
                description="Inspect one owned Engineer command job.",
                parameters={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                    "additionalProperties": True,
                },
                security_id="engineer.command.manage",
                risk="observe",
                handler=status_handler,
            )
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        if name != "engineer_command_status":
            return await super().execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )
        self.seen_status_arguments = dict(arguments)
        self.status_execute_count += 1
        assert str(arguments.get("job_id") or "") == _JOB_ID
        assert str(arguments.get("_conversation_id") or "").startswith("conv_")
        if self._revoke_after_status:
            self._test_storage.set_permission_override(
                LEGACY_OWNER_USER_ID,
                "engineer.command.manage",
                "deny",
            )
        data = {
            "ok": True,
            "job_id": _JOB_ID,
            "status": self._status,
            "receipt": {"job_id": _JOB_ID, "status": self._status},
        }
        return ToolResult(name, True, data=data, attachment=self._attachment)


class _CurrentStatusThenGuessModel(_StatusThenFinishModel):
    def __init__(self) -> None:
        super().__init__()
        self.second_tool_round_seen = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        names = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        if "engineer_command_status" in names and not self.status_requested:
            self.status_requested = True
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "current-status-call",
                        "type": "function",
                        "function": {
                            "name": "engineer_command_status",
                            "arguments": "{}",
                        },
                    }
                ],
                "finish_reason": "tool_calls",
                "_queue_wait_sec": 0.0,
            }
        if any(str(item.get("role") or "") == "tool" for item in messages):
            self.second_tool_round_seen = True
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "guessed-status-call",
                        "type": "function",
                        "function": {
                            "name": "engineer_command_status",
                            "arguments": json.dumps({"job_id": _JOB_ID}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
                "_queue_wait_sec": 0.0,
            }
        return await super().chat(messages, tools=tools, **kwargs)


class _CurrentStatusRefusalKernel(_StatusKernel):
    def __init__(self, *args: Any, refusal_code: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refusal_code = refusal_code

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        del actor, execution_scope
        assert name == "engineer_command_status"
        assert "job_id" not in arguments
        assert str(arguments.get("_conversation_id") or "").startswith("conv_")
        self.status_execute_count += 1
        return ToolResult(
            name,
            False,
            data={"ok": False, "error_code": self.refusal_code, "status": "failed"},
            error=f"Host control refused: {self.refusal_code}",
        )


def _document_carrier() -> dict[str, Any]:
    return {
        "kind": "document",
        "filename": _FILENAME,
        "mime_type": "application/zip",
        "content_base64": base64.b64encode(_PAYLOAD).decode("ascii"),
    }


def _runtime(
    settings: Any,
    storage: Any,
    *,
    status: str,
    attachment: dict[str, Any] | None,
    revoke_after_status: bool = False,
    status_call_count: int = 1,
) -> tuple[AgentRuntime, ActorContext, _StatusThenFinishModel]:
    configured = replace(settings, engineer_mode_enabled=True, verify_answers=False)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_USE)
    authorization.register_capability(ENGINEER_COMMAND_MANAGE)
    kernel = _StatusKernel(
        authorization,
        configured,
        storage,
        status=status,
        attachment=attachment,
        revoke_after_status=revoke_after_status,
    )
    model = _StatusThenFinishModel(status_call_count=status_call_count)
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]
    actor = authorization.actor_for_user(LEGACY_OWNER_USER_ID, source="synthetic-test")
    return runtime, actor, model


async def _chat(runtime: AgentRuntime, actor: ActorContext) -> dict[str, Any]:
    return await runtime.chat(
        LEGACY_OWNER_USER_ID,
        f"Что сейчас происходит с Engineer-командой {_JOB_ID}?",
        actor=actor,
        enable_tools=True,
        mode="engineer",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("current_job_not_found", "нет текущей Engineer-команды"),
        ("current_job_ambiguous", "несколько незавершённых Engineer-команд"),
        ("current_job_uncertain", "Состояние текущей Engineer-команды неизвестно"),
    ],
)
async def test_current_job_resolution_refusal_is_structural_and_model_cannot_guess(
    settings: Any,
    storage: Any,
    error_code: str,
    expected: str,
) -> None:
    configured = replace(settings, engineer_mode_enabled=True, verify_answers=False)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_USE)
    authorization.register_capability(ENGINEER_COMMAND_MANAGE)
    kernel = _CurrentStatusRefusalKernel(
        authorization,
        configured,
        storage,
        status="running",
        attachment=None,
        refusal_code=error_code,
    )
    model = _CurrentStatusThenGuessModel()
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]
    actor = authorization.actor_for_user(LEGACY_OWNER_USER_ID, source="synthetic-test")

    response = await runtime.chat(
        LEGACY_OWNER_USER_ID,
        "Что происходит с текущей Engineer-командой?",
        actor=actor,
        enable_tools=True,
        mode="engineer",
    )

    assert expected in response["message"]
    assert kernel.status_execute_count == 1
    assert model.second_tool_round_seen is False


@pytest.mark.asyncio
async def test_terminal_command_status_publishes_one_protected_generated_raw(
    settings: Any,
    storage: Any,
) -> None:
    runtime, actor, model = _runtime(
        settings,
        storage,
        status="completed",
        attachment=_document_carrier(),
    )

    response = await _chat(runtime, actor)

    assert model.status_requested is True
    assert isinstance(runtime.kernel, _StatusKernel)
    assert runtime.kernel.seen_status_arguments["_conversation_id"] == response["conversation_id"]
    assert len(response["files"]) == 1
    published = response["files"][0]
    assert published["filename"] == _FILENAME
    assert published["mime_type"] == "application/zip"
    assert base64.b64decode(published["content_base64"], validate=True) == _PAYLOAD
    rendered_tool_results = "\n".join(model.tool_result_messages)
    assert base64.b64encode(_PAYLOAD).decode("ascii") not in rendered_tool_results
    assert "_attachment" not in rendered_tool_results

    rows = storage.execute(
        "SELECT id,source,raw_content,content_type,metadata_json FROM raw_objects "
        "WHERE content_type='generated_file'"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    assert row["id"] == published["id"]
    assert row["source"] == "generated"
    assert row["raw_content"] == ""
    assert metadata["generated_artifact"] is True
    assert metadata["generated_for"] == LEGACY_OWNER_USER_ID
    assert metadata["filename"] == _FILENAME
    assert Path(settings.files_dir, metadata["stored_path"]).read_bytes() == _PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "attachment"),
    [
        ("running", None),
        pytest.param("running", _document_carrier(), id="running-unexpected-carrier"),
        ("unknown", None),
        pytest.param("unknown", _document_carrier(), id="unknown-unexpected-carrier"),
    ],
)
async def test_running_command_status_never_publishes_a_file(
    settings: Any,
    storage: Any,
    status: str,
    attachment: dict[str, Any] | None,
) -> None:
    runtime, actor, _model = _runtime(
        settings,
        storage,
        status=status,
        attachment=attachment,
    )

    response = await _chat(runtime, actor)

    assert response["files"] == []
    count = storage.execute(
        "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
    ).fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_command_status_is_the_only_executed_call_in_its_selected_batch(
    settings: Any,
    storage: Any,
) -> None:
    runtime, actor, _model = _runtime(
        settings,
        storage,
        status="completed",
        attachment=_document_carrier(),
        status_call_count=2,
    )

    response = await _chat(runtime, actor)

    assert isinstance(runtime.kernel, _StatusKernel)
    assert runtime.kernel.status_execute_count == 1
    assert len(response["files"]) == 1
    count = storage.execute(
        "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
    ).fetchone()[0]
    assert count == 1


@pytest.mark.asyncio
async def test_fresh_late_command_manage_revoke_drops_carrier_without_failure_or_raw(
    settings: Any,
    storage: Any,
) -> None:
    runtime, actor, _model = _runtime(
        settings,
        storage,
        status="completed",
        attachment=_document_carrier(),
        revoke_after_status=True,
    )

    response = await _chat(runtime, actor)

    assert response["files"] == []
    assert "право его публикации изменилось" in response["message"]
    count = storage.execute(
        "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
    ).fetchone()[0]
    assert count == 0
