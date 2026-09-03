"""Model-selected ``message_search`` stays inside its private publication boundary."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService

_OWNER = "model-message-search-owner"
_QUERY = "NEBULA-742"
_PRIVATE_BODY = f"PRIVATE-MESSAGE-BODY-{_QUERY}: launch window is synthetic."
_MODEL_BODY = f"MODEL-DERIVED-PRIVATE-BODY-{_QUERY}"
_MALFORMED_CANARY = "MALFORMED-MESSAGE-RESULT-PRIVATE-CANARY"
_REQUEST = f"Расскажи, что тебе известно про {_QUERY}."
_DENIAL = "Доступ к переписке изменился до публикации; найденные данные не публикую."


class _EmptySearcher:
    async def search(self, user_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, query, kwargs
        return {
            "results": [],
            "entity_matches": [],
            "graph_context": {},
            "matched_at_least": 0,
        }


class _RecordingKernel(ExecutionKernel):
    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        self.calls.append((name, dict(arguments), execution_scope))
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _MalformedMessageKernel(_RecordingKernel):
    def __init__(
        self,
        authorization: AuthorizationService,
        settings: Any,
        *,
        malformation: str,
    ) -> None:
        super().__init__(authorization, settings)
        self.malformation = malformation

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        result = await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )
        if name != "archive_search" or not result.success:
            return result
        data = result.data
        if type(data) is str:
            payload = json.loads(data)
            assert isinstance(payload, dict)
            if self.malformation == "extra-field":
                payload["private_body"] = _MALFORMED_CANARY
            else:
                payload["count"] = _MALFORMED_CANARY
            forged = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return ToolResult(name, True, data=forged)
        data = copy.deepcopy(data)
        assert isinstance(data, dict)
        if self.malformation == "extra-field":
            data["private_body"] = _MALFORMED_CANARY
        else:
            data["count"] = _MALFORMED_CANARY
        return ToolResult(name, True, data=data)


class _ModelSelectedMessageSearch:
    enabled = True
    model = "synthetic-model-selected-message-search"
    total_budget_sec = 10.0

    def __init__(
        self,
        *,
        mutate_after_result: Callable[[], None] | None = None,
        expect_private_result: bool = True,
    ) -> None:
        self.mutate_after_result = mutate_after_result
        self.expect_private_result = expect_private_result
        self.mutated = False
        self.initial_offered_names: set[str] = set()
        self.synthesis_offered_names: list[set[str]] = []
        self.synthesis_transcripts: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        snapshot = copy.deepcopy(messages)
        system_text = "\n".join(
            str(item.get("content") or "") for item in snapshot if str(item.get("role") or "") == "system"
        )
        if "Ответь одним словом: РАЗГОВОР или ЗАПРОС." in system_text:
            return {"content": "ЗАПРОС", "tool_calls": None, "_queue_wait_sec": 0.0}
        if "Классифицируй ТОЛЬКО чтение личной ленты/календаря" in system_text:
            return {
                "content": '{"direction":"none","window_kind":"none"}',
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        if "Никаких пояснений, только JSON." in system_text:
            return {
                "content": '{"вид": "архив", "запрос": "", "кто": "", "дни": []}',
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }

        offered = {
            str((item.get("function") or {}).get("name") or item.get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        if any(str(item.get("role") or "") == "tool" for item in snapshot):
            self.synthesis_transcripts.append(snapshot)
            self.synthesis_offered_names.append(offered)
            if self.expect_private_result:
                assert _PRIVATE_BODY in serialized
            else:
                assert _PRIVATE_BODY not in serialized
                assert _MALFORMED_CANARY not in serialized
            if self.mutate_after_result is not None:
                assert not self.mutated
                self.mutate_after_result()
                self.mutated = True
            return {
                "content": _MODEL_BODY
                if self.expect_private_result
                else "Непроверяемый результат не использован.",
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }

        assert _PRIVATE_BODY not in serialized
        assert "archive_search" in offered
        assert "message_search" not in offered
        self.initial_offered_names = offered
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-model-message-search",
                    "type": "function",
                    "function": {
                        "name": "archive_search",
                        "arguments": json.dumps(
                            {"query": _QUERY, "corpora": ["messages"]},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
            "_queue_wait_sec": 0.0,
        }


def _runtime(
    settings: Any,
    storage: Any,
    model: _ModelSelectedMessageSearch,
    *,
    kernel: _RecordingKernel | None = None,
) -> tuple[AgentRuntime, _RecordingKernel, Any]:
    storage.ensure_user(_OWNER, preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    bound_kernel = kernel or _RecordingKernel(authorization, settings)
    bound_kernel.bind_services(
        storage,
        graph,
        object(),
        IngestionPipeline(settings, storage, graph),
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=bound_kernel,
    )
    actor = authorization.actor_for_user(_OWNER, source="model-message-search-test")
    return runtime, bound_kernel, actor


def _seed_private_message(storage: Any) -> tuple[str, str]:
    source = storage.create_conversation(_OWNER, "private source conversation")
    storage.store_message(str(source["id"]), _OWNER, "user", _PRIVATE_BODY)
    current = storage.create_conversation(_OWNER, "current model-selected conversation")
    return str(source["id"]), str(current["id"])


def _stored_turn(
    storage: Any,
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    assistant = storage.get_message(str(response["message_id"]), _OWNER)
    assert assistant is not None
    assistant_metadata = json.loads(str(assistant["metadata_json"] or "{}"))
    return assistant, assistant_metadata


async def _forbidden_voice(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    raise AssertionError("message_search private bytes reached the voice carrier")


@pytest.mark.asyncio
async def test_model_selected_message_search_closes_synthesis_tools_and_marks_lineage(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_conversation_id, current_conversation_id = _seed_private_message(storage)
    model = _ModelSelectedMessageSearch()
    runtime, kernel, actor = _runtime(settings, storage, model)
    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        conversation_id=current_conversation_id,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert {"archive_search", "make_file", "speak"} <= model.initial_offered_names
    assert "message_search" not in model.initial_offered_names
    assert all("web_search" not in names for names in model.synthesis_offered_names)
    assert [name for name, _arguments, _scope in kernel.calls] == ["archive_search"]
    assert kernel.calls[0][2] == "dialogue"
    assert response["tools_used"] == ["archive_search"]
    assert response["files"] == []
    assert response["voice"] is None
    assistant, metadata = _stored_turn(storage, response)
    assert "message_search" not in str(metadata.get("tools_used") or [])
    assert metadata.get("private_context_lineage") is True or "archive_search" in (
        metadata.get("tools_used") or []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["search.use"])
async def test_model_selected_message_search_late_revoke_is_source_free(
    permission: str,
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_conversation_id, current_conversation_id = _seed_private_message(storage)
    model = _ModelSelectedMessageSearch(
        mutate_after_result=lambda: storage.set_permission_override(_OWNER, permission, "deny")
    )
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        conversation_id=current_conversation_id,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.mutated is True
    assert all("web_search" not in names for names in model.synthesis_offered_names)
    assert [name for name, _arguments, _scope in kernel.calls] == ["archive_search"]
    assert kernel.calls[0][2] == "dialogue"
    assert response["archive_search_authority_changed_before_publication"] is True
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _PRIVATE_BODY not in public
    assert _MODEL_BODY not in public
    assert response.get("tool_evidence") in (None, [])
    assert response["files"] == []
    assert response["voice"] is None
    assert response["citations"] == []
    assert response["web_sources"] == []
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    for private_value in (_PRIVATE_BODY, _MODEL_BODY, source_conversation_id):
        assert private_value not in public

    assistant, metadata = _stored_turn(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    for private_value in (_PRIVATE_BODY, _MODEL_BODY, source_conversation_id):
        assert private_value not in durable
    assert "message_search" not in (metadata.get("tools_used") or [])
    assert metadata["knowledge_object_ids"] == []
    for continuation_key in (
        "message_locate_pending_action",
        "message_locate_source_user_message_id",
        "filename_result_pending_action",
        "filename_result_pending_origin",
        "filename_result_pending_message_source_user_message_id",
    ):
        assert continuation_key not in metadata
    trace = metadata["interaction_trace"]
    assert trace["continuation"] == "none"
    assert trace["state_restored"] is False
    assert all(
        str(step.get("capability") or "") != "message_retrieval"
        for step in trace.get("steps", [])
        if isinstance(step, dict)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["extra-field", "ill-typed-field"])
async def test_model_selected_message_search_rejects_unprojectable_success(
    malformation: str,
    settings: Any,
    storage: Any,
) -> None:
    _source_conversation_id, current_conversation_id = _seed_private_message(storage)
    model = _ModelSelectedMessageSearch(expect_private_result=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = _MalformedMessageKernel(authorization, settings, malformation=malformation)
    runtime, kernel, actor = _runtime(settings, storage, model, kernel=kernel)

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        conversation_id=current_conversation_id,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert [name for name, _arguments, _scope in kernel.calls] == ["archive_search"]
    assert all("message_search" not in names for names in model.synthesis_offered_names)
    assert kernel.calls[0][2] == "dialogue"
    projection = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _PRIVATE_BODY not in projection
    assert _MALFORMED_CANARY not in projection
    assert response.get("tool_evidence") in (None, [])
    assistant, metadata = _stored_turn(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _PRIVATE_BODY not in durable
    assert _MALFORMED_CANARY not in durable
    assert metadata["private_context_lineage"] is False
