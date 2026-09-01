"""Retrieval discovery preserves legacy journeys and archive-turn isolation."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime, is_archive_search_current_text
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService

_LEGACY_RETRIEVAL_TOOLS = frozenset({"memory_search", "source_search", "message_search"})


class _ForbiddenModel:
    enabled = True
    model = "forbidden-hidden-message-prefetch-model"
    total_budget_sec = 60.0

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("a settled owner-history lookup reached the model")


class _RecordingOrdinaryModel:
    enabled = True
    model = "recording-ordinary-retrieval-model"
    total_budget_sec = 60.0

    def __init__(self) -> None:
        self.offered_tool_names: list[frozenset[str]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del messages
        self.offered_tool_names.append(
            frozenset(
                str((item.get("function") or {}).get("name") or item.get("name") or "")
                for item in (kwargs.get("tools") or [])
            )
        )
        return {
            "content": "Уточните, пожалуйста, с какого шага продолжить.",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _bound_kernel(settings, storage):  # noqa: ANN001, ANN202
    storage.ensure_user("archive-facade-owner", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        object(),  # Retrieval-only assertions never enter the web surface.
        IngestionPipeline(settings, storage, graph),
    )
    actor = authorization.actor_for_user("archive-facade-owner", source="test")
    return kernel, actor


def test_ordinary_dialogue_keeps_released_legacy_retrieval_schemas(
    settings,
    storage,
) -> None:
    kernel, actor = _bound_kernel(settings, storage)

    names = set(kernel.get_tool_names(actor))
    assert "archive_search" in names
    assert names >= _LEGACY_RETRIEVAL_TOOLS

    # Topic shortening changes descriptions, not the released ordinary
    # capability surface. Explicit archive turns are isolated later by
    # AgentRuntime before the first model token.
    for topic in ("", "архив", "знание", "файл", "человек"):
        definitions = kernel.get_tool_definitions(actor, topic=topic)
        offered = {str((definition.get("function") or {}).get("name") or "") for definition in definitions}
        assert "archive_search" in offered
        assert offered >= _LEGACY_RETRIEVAL_TOOLS


@pytest.mark.asyncio
async def test_unclassified_ordinary_first_model_call_keeps_legacy_retrieval_schemas(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, actor = _bound_kernel(settings, storage)
    model = _RecordingOrdinaryModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    message = "Продолжи нашу прежнюю задумку про орбитальный телескоп."
    arbiter_calls: list[str] = []

    async def leave_unclassified(
        candidate: str,
        *,
        previous_turn: str = "",
        context: Any = None,
    ) -> tuple[str, str | None]:
        del previous_turn, context
        arbiter_calls.append(candidate)
        return "", None

    monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", leave_unclassified)

    assert is_archive_search_current_text(message) is False
    reply = await runtime.chat(
        "archive-facade-owner",
        message,
        actor=actor,
    )

    assert arbiter_calls == [message]
    assert len(model.offered_tool_names) == 1
    first_offered = model.offered_tool_names[0]
    assert first_offered >= _LEGACY_RETRIEVAL_TOOLS
    assert "archive_search" not in first_offered
    assert reply["message"] == "Уточните, пожалуйста, с какого шага продолжить."


@pytest.mark.asyncio
async def test_legacy_archive_retrieval_tools_remain_visible_and_executable(
    settings,
    storage,
) -> None:
    kernel, actor = _bound_kernel(settings, storage)

    for name in sorted(_LEGACY_RETRIEVAL_TOOLS):
        registered = kernel.get_tool(name)
        assert registered is not None
        assert registered.model_visible is True
        assert registered.handler is not None

        result = await kernel.execute(name, {"query": "absentretrievalcanary"}, actor=actor)
        assert result.success is True, f"{name} ceased to be internally executable: {result.error}"


@pytest.mark.asyncio
async def test_visible_message_search_still_serves_code_owned_owner_history_prefetch(
    settings,
    storage,
) -> None:
    kernel, actor = _bound_kernel(settings, storage)
    kernel.authorization.deny_permission("archive-facade-owner", "knowledge.read")
    conversation = storage.create_conversation("archive-facade-owner", "hidden prefetch")
    historical = storage.store_message(
        str(conversation["id"]),
        "archive-facade-owner",
        "user",
        "HIDDEN-PREFETCH-CANARY про орбитальный телескоп",
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_ForbiddenModel(),
        kernel=kernel,
    )
    calls: list[dict[str, Any]] = []
    execution_scopes: list[str] = []
    original_execute = kernel.execute

    async def recording_execute(  # noqa: ANN001, ANN202
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",
    ):
        if name == "message_search":
            calls.append(dict(arguments))
            execution_scopes.append(execution_scope)
        return await original_execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )

    kernel.execute = recording_execute  # type: ignore[method-assign]

    reply = await runtime.chat(
        "archive-facade-owner",
        "что я писал про орбитальный телескоп?",
        actor=actor,
        conversation_id=str(conversation["id"]),
    )

    offered = {
        str((definition.get("function") or {}).get("name") or "")
        for definition in kernel.get_tool_definitions(actor, topic="человек")
    }
    assert "message_search" in offered
    assert len(calls) == 1
    assert execution_scopes == ["internal"]
    boundary_id = str(calls[0].pop("before_message_id", ""))
    boundary = storage.get_message(boundary_id, "archive-facade-owner")
    assert boundary is not None
    assert boundary["content"] == "что я писал про орбитальный телескоп?"
    assert calls == [
        {
            "query": "орбитальный телескоп",
            "limit": 20,
            "match_all_terms": True,
            "role": "user",
        }
    ]
    assert str(historical["content"]) in str(reply["message"])


@pytest.mark.asyncio
async def test_code_owned_message_search_reauthorizes_before_the_storage_read(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, actor = _bound_kernel(settings, storage)
    conversation = storage.create_conversation("archive-facade-owner", "revoked hidden prefetch")
    storage.store_message(
        str(conversation["id"]),
        "archive-facade-owner",
        "user",
        "REVOKED-HIDDEN-PREFETCH-BODY про телескоп",
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_ForbiddenModel(),
        kernel=kernel,
    )
    original_execute = kernel.execute
    executions = 0
    execution_scopes: list[str] = []

    def forbidden_storage_read(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("revoked message_search reached a message-body reader")

    monkeypatch.setattr(storage, "search_messages", forbidden_storage_read)
    monkeypatch.setattr(storage, "list_messages_window", forbidden_storage_read)

    async def revoke_after_preflight(  # noqa: ANN001, ANN202
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",
    ):
        nonlocal executions
        if name == "message_search":
            executions += 1
            execution_scopes.append(execution_scope)
            kernel.authorization.deny_permission(
                "archive-facade-owner",
                "conversations.read",
            )
        return await original_execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )

    kernel.execute = revoke_after_preflight  # type: ignore[method-assign]

    reply = await runtime.chat(
        "archive-facade-owner",
        "что я писал про телескоп?",
        actor=actor,
        conversation_id=str(conversation["id"]),
    )

    assert executions == 1
    assert execution_scopes == ["internal"]
    assert "REVOKED-HIDDEN-PREFETCH-BODY" not in str(reply)


@pytest.mark.asyncio
async def test_code_owned_message_search_revoke_and_final_publication_share_transaction(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, actor = _bound_kernel(settings, storage)
    conversation = storage.create_conversation(
        "archive-facade-owner",
        "same-transaction revoked hidden prefetch",
    )
    storage.store_message(
        str(conversation["id"]),
        "archive-facade-owner",
        "user",
        "SAME-TRANSACTION-REVOKED-BODY про телескоп",
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_ForbiddenModel(),
        kernel=kernel,
    )
    original_publication_check = runtime._message_search_publication_authorized  # noqa: SLF001
    original_execute = kernel.execute
    publication_checks = 0
    voice_checks: list[bool] = []
    kernel_calls: list[tuple[str, str]] = []

    async def recording_execute(  # noqa: ANN202
        name,  # noqa: ANN001
        arguments,  # noqa: ANN001
        *,
        actor=None,  # noqa: ANN001
        execution_scope="dialogue",  # noqa: ANN001
    ):
        kernel_calls.append((str(name), str(execution_scope)))
        return await original_execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )

    async def recording_voice_check(**kwargs: Any) -> bool:
        del kwargs
        voice_checks.append(True)
        return False

    def revoke_inside_publication(  # noqa: ANN202
        conn,  # noqa: ANN001
        *,
        actor,  # noqa: ANN001
        context,  # noqa: ANN001
    ):
        nonlocal publication_checks
        publication_checks += 1
        assert conn.in_transaction
        conn.execute(
            """INSERT INTO user_permission_overrides(user_id, security_id, effect, updated_at)
               VALUES(?, 'conversations.read', 'deny', ?)
               ON CONFLICT(user_id, security_id) DO UPDATE SET
                 effect=excluded.effect, updated_at=excluded.updated_at""",
            ("archive-facade-owner", "2026-09-01T00:00:00+00:00"),
        )
        return original_publication_check(conn, actor=actor, context=context)

    kernel.execute = recording_execute  # type: ignore[method-assign]
    monkeypatch.setattr(runtime, "_final_voice_can_start", recording_voice_check)
    monkeypatch.setattr(
        runtime,
        "_message_search_publication_authorized",
        revoke_inside_publication,
    )

    reply = await runtime.chat(
        "archive-facade-owner",
        "что я писал про телескоп?",
        actor=actor,
        conversation_id=str(conversation["id"]),
        answer_with_voice=True,
    )

    assert publication_checks == 1
    assert reply["message_search_authority_changed_before_publication"] is True
    assert reply["message"] == ("Доступ к переписке изменился до публикации; найденные данные не публикую.")
    assert "SAME-TRANSACTION-REVOKED-BODY" not in json.dumps(reply, ensure_ascii=False)
    assert reply["voice"] is None
    assert reply["files"] == []
    assert voice_checks == []
    assert kernel_calls == [("message_search", "internal")]

    persisted = storage.get_message(str(reply["message_id"]), "archive-facade-owner")
    assert persisted is not None
    assert "SAME-TRANSACTION-REVOKED-BODY" not in str(persisted)
    metadata = json.loads(str(persisted["metadata_json"] or "{}"))
    assert metadata.get("tools_used") == []
    assert metadata.get("knowledge_object_ids") == []
    for continuation_key in (
        "message_locate_pending_action",
        "message_locate_source_user_message_id",
        "filename_result_pending_action",
        "filename_result_pending_origin",
        "filename_result_pending_message_source_user_message_id",
    ):
        assert continuation_key not in metadata
    trace = metadata.get("interaction_trace")
    assert isinstance(trace, dict)
    assert trace.get("continuation") == "none"
    assert trace.get("state_restored") is False
    assert all(
        str(step.get("capability") or "") != "message_retrieval"
        for step in trace.get("steps", [])
        if isinstance(step, dict)
    )
