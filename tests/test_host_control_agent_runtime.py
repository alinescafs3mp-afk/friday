from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import AgentContext, AgentRuntime
from friday.execution_kernel import ToolResult
from friday.file_evidence import current_turn_file_reference_of
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.organs.host_control import HOST_FILES_READ
from friday.permissions import AuthorizationService


class _JqRuntimeKernel:
    def __init__(self, authorization: AuthorizationService) -> None:
        self.authorization = authorization
        self.executed: list[tuple[str, dict[str, Any], Any]] = []

    @staticmethod
    def get_tool_definitions(actor: Any, *, topic: str | None = None) -> list[dict[str, Any]]:
        del actor, topic
        return [
            {
                "type": "function",
                "function": {
                    "name": "host_json_extract",
                    "description": "synthetic jq contract",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "fields": {"type": "array", "items": {"type": "string"}},
                            "compact": {"type": "boolean"},
                        },
                        "required": ["fields"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "synthetic external contract",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    @staticmethod
    def get_tool(name: str) -> Any:
        contracts = {
            "host_json_extract": ("observe", "host.files.read"),
            "web_search": ("observe", "web.search"),
        }
        risk, security_id = contracts[name]
        return SimpleNamespace(risk=risk, security_id=security_id)

    async def execute(self, name: str, arguments: Any, *, actor: Any = None) -> ToolResult:
        self.executed.append((name, dict(arguments), actor))
        return ToolResult(name, True, data={"result": {"name": "Ada"}})


class _JqRuntimeModel:
    enabled = True
    model = "synthetic-jq-runtime"
    total_budget_sec = 30.0

    def __init__(self, arguments: dict[str, Any]) -> None:
        self.arguments = arguments
        self.offered_tools: list[list[dict[str, Any]]] = []

    @property
    def offered_names(self) -> list[set[str]]:
        return [
            {str((item.get("function") or {}).get("name") or "") for item in tools if isinstance(item, dict)}
            for tools in self.offered_tools
        ]

    async def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.offered_tools.append([dict(item) for item in (tools or []) if isinstance(item, dict)])
        if len(self.offered_tools) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-jq",
                        "function": {
                            "name": "host_json_extract",
                            "arguments": json.dumps(self.arguments),
                        },
                    }
                ],
            }
        return {"content": "Локальный JSON обработан.", "_queue_wait_sec": 0.0}


async def _stored_json_attachment(settings: Any, storage: Any) -> tuple[str, dict[str, Any]]:
    storage.ensure_user("alice", preset_key="owner")
    payload = b'{"name":"Ada","nested":{"value":7}}'
    request_filename = "unsafe/path/input.json"
    ingested = await IngestionPipeline(settings, storage, KnowledgeGraph(storage)).ingest_file(
        "alice",
        None,
        payload,
        filename=request_filename,
        mime_type="application/json",
        metadata={"uploaded_by": "alice"},
        source_ref="test-file:jq-current-upload",
    )
    raw_id = str(ingested["raw_object_id"])
    assert storage.get_raw_object(raw_id, "alice") is None
    from friday.server import _current_turn_file_attachment, _current_turn_upload_raw

    authorization = AuthorizationService(storage)
    authorization.register_capability(HOST_FILES_READ)
    actor = authorization.actor_for_user("alice", source="test")
    assert authorization.authorize(actor, "files.read").allowed
    assert authorization.authorize(actor, "host.files.read").allowed
    state = SimpleNamespace(
        auth_service=authorization,
        settings=replace(settings, host_control_enabled=True),
        storage=storage,
    )
    raw = await _current_turn_upload_raw(
        state=state,
        actor=actor,
        raw_id=raw_id,
        filename=request_filename,
        mime_type="application/json",
        file_content=payload,
    )
    assert isinstance(raw, dict)
    attachment = _current_turn_file_attachment(
        filename=request_filename,
        file_ingestion=ingested,
        raw=raw,
        storage=storage,
        tenant_id=actor.user_id,
        uploaded_by=actor.own_id,
    )
    token = current_turn_file_reference_of(attachment)
    assert token is not None
    assert token.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert attachment["filename"] == "input.json"
    return raw_id, attachment


def _runtime_stack(settings: Any, storage: Any, arguments: dict[str, Any]):
    authorization = AuthorizationService(storage)
    kernel = _JqRuntimeKernel(authorization)
    model = _JqRuntimeModel(arguments)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,  # type: ignore[arg-type]
    )
    return authorization, kernel, model, runtime


async def _run_attached_model_jq(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
):
    raw_id, attachment = await _stored_json_attachment(settings, storage)
    conversation = storage.create_conversation("alice", "jq runtime")
    authorization, kernel, model, runtime = _runtime_stack(settings, storage, arguments)
    actor = authorization.actor_for_user("alice", source="test")

    async def no_prefetch(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", no_prefetch)
    result = await runtime.chat(
        "alice",
        "Извлеки поле name из этого JSON-файла.",
        actor=actor,
        conversation_id=conversation["id"],
        attachments=[attachment],
        enable_tools=True,
    )
    return raw_id, conversation, kernel, model, result


def test_jq_is_a_reviewed_local_private_source_tool_with_no_model_raw_selector() -> None:
    assert agent_runtime_module._private_source_tool_policy("host_json_extract") == "local"  # noqa: SLF001
    schemas = _JqRuntimeKernel.get_tool_definitions(object())
    jq_schema = next(item for item in schemas if item["function"]["name"] == "host_json_extract")
    assert set(jq_schema["function"]["parameters"]["properties"]) == {"compact", "fields"}
    projected = agent_runtime_module._project_private_source_tool_schemas(schemas)  # noqa: SLF001
    assert {str((item.get("function") or {}).get("name") or "") for item in projected} == {
        "host_json_extract"
    }


def test_jq_attachment_authority_is_exact_and_ambiguity_fails_closed() -> None:
    first = {
        "_registered_file_bytes_verified": True,
        "filename": "first.json",
        "mime_type": "application/json",
        "raw_object_id": "raw_0123456789abcdef",
    }
    second = {**first, "filename": "second.json", "raw_object_id": "raw_fedcba9876543210"}
    selector = agent_runtime_module._single_authorized_json_attachment_raw_id  # noqa: SLF001
    assert selector([]) == ""
    assert selector([first]) == first["raw_object_id"]
    assert selector([first, second]) == ""
    assert selector([{**first, "_registered_file_bytes_verified": False}]) == ""
    assert selector([{**first, "filename": "input.txt", "mime_type": "text/plain"}]) == ""


@pytest.mark.asyncio
async def test_current_json_model_call_gets_exact_code_owned_raw_and_runtime_context(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible_arguments = {"fields": ["name"], "compact": True}
    raw_id, conversation, kernel, model, result = await _run_attached_model_jq(
        settings,
        storage,
        monkeypatch,
        visible_arguments,
    )

    rows = storage.get_conversation_messages(conversation["id"], user_id="alice")
    source_message = next(row for row in rows if row["role"] == "user")
    assert len(kernel.executed) == 1, result
    name, executed_arguments, actor = kernel.executed[0]
    assert name == "host_json_extract"
    assert executed_arguments == {
        **visible_arguments,
        "_conversation_id": conversation["id"],
        "_raw_id": raw_id,
        "_source_message_id": source_message["id"],
    }
    assert actor.own_id == "alice"
    assert model.offered_names[0] == {"host_json_extract"}
    assert model.offered_names[1] == {"host_json_extract"}
    assert "raw_id" not in json.dumps(model.offered_tools, sort_keys=True)
    assert result["tools_used"] == ["host_json_extract"]


@pytest.mark.asyncio
async def test_successful_jq_result_activates_boundary_before_next_model_round(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    authorization, kernel, model, runtime = _runtime_stack(
        settings,
        storage,
        {"fields": ["name"]},
    )
    context = AgentContext(
        conversation_id="conversation:test",
        user_id="alice",
        person_id="alice",
        effect_root_user_message_id="message:test",
        host_json_attachment_raw_id="raw_0123456789abcdef",
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Synthetic local JSON request",
        authorization.actor_for_user("alice", source="test"),
        kernel.get_tool_definitions(object()),
        None,
    )

    assert model.offered_names[0] == {"host_json_extract", "web_search"}
    assert model.offered_names[1] == {"host_json_extract"}
    assert context.private_source_boundary_active is True
    assert result["tools_used"] == ["host_json_extract"]


@pytest.mark.parametrize(
    "private_key",
    ["_conversation_id", "_raw_id", "_source_message_id", "raw_id"],
)
@pytest.mark.asyncio
async def test_model_cannot_spoof_jq_private_context(
    private_key: str,
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw, _conversation, kernel, model, result = await _run_attached_model_jq(
        settings,
        storage,
        monkeypatch,
        {
            "fields": ["name"],
            private_key: "attacker-controlled",
        },
    )

    assert kernel.executed == []
    assert len(model.offered_names) == 2
    assert result["tools_used"] == ["host_json_extract"]
