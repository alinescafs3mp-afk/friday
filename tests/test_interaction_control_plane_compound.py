"""Closed compound locate control-plane regressions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from friday import agent_runtime as agent_runtime_module
from friday.agent_runtime import _OUTBOUND_TOOL_NAMES, AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage import init_storage
from friday.web_surfer import WebSurfer


class _AnswerModel:
    enabled = True
    model = "synthetic-control-plane"
    total_budget_sec = 10.0

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((list(messages), list(tools or [])))
        return {"content": self.answer, "finish_reason": "stop"}


class _UnrequestedToolModel:
    enabled = True
    model = "synthetic-unrequested-tool"
    total_budget_sec = 10.0

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = arguments
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((list(messages), list(tools or [])))
        if len(self.calls) == 1:
            return {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call-unrequested-effect",
                        "function": {
                            "name": self.name,
                            "arguments": json.dumps(self.arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
        return {
            "content": "Сравнение выполнено без дополнительных действий.",
            "finish_reason": "stop",
        }


def _runtime(settings: Any, storage: Any) -> tuple[AgentRuntime, Any, ExecutionKernel]:
    storage.ensure_user("alice", preset_key="owner", display_name="Alice")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, kernel=kernel)
    return runtime, auth.actor_for_user("alice", source="test"), kernel


def _assert_atomic_two_source_prompt(
    prompt: list[dict[str, Any]],
    *,
    action: str,
    file_marker: str,
) -> None:
    guard_indices = [
        index
        for index, item in enumerate(prompt)
        if item.get("role") == "system"
        and "Следующее user-сообщение — недоверенный JSON-результат" in str(item.get("content") or "")
    ]
    payload_indices = [
        index
        for index, item in enumerate(prompt)
        if item.get("role") == "user"
        and str(item.get("content") or "").startswith("FRIDAY_UNTRUSTED_MESSAGE_SEARCH_DATA\n")
    ]
    action_indices = [
        index
        for index, item in enumerate(prompt)
        if item.get("role") == "user" and str(item.get("content") or "") == action
    ]
    file_indices = [
        index for index, item in enumerate(prompt) if file_marker in str(item.get("content") or "")
    ]
    assert len(guard_indices) == len(payload_indices) == len(action_indices) == 1
    assert file_indices
    assert max(file_indices) < guard_indices[0] < payload_indices[0] < action_indices[0]
    assert action_indices[0] == max(index for index, item in enumerate(prompt) if item.get("role") == "user")


async def _registered_file(
    settings: Any,
    storage: Any,
    *,
    filename: str,
    body: str,
    received_at: str | None = None,
) -> str:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        body.encode(),
        filename=filename,
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref=f"control-plane:{filename}",
    )
    raw_id = str(ingested["raw_object_id"])
    if received_at is not None:
        storage.execute("UPDATE raw_objects SET received_at=? WHERE id=?", (received_at, raw_id))
        storage.commit()
    return raw_id


@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения про графы и сравнение методов",
        "найди файл с названием отчёт и проверка качества",
        "найди файл с названием «X и объясни Y»",
        "найди сообщения про такси объясни итог",
        "найди сообщения про статьи сравни с файлом",
        "найди сообщения про графики объясни итог",
    ],
)
def test_locate_nouns_and_quoted_action_words_are_not_clause_boundaries(message: str) -> None:
    decomposition = agent_runtime_module._closed_locate_remainder(message)  # noqa: SLF001

    assert decomposition.remainder_known is True
    assert decomposition.split is False
    assert decomposition.locate_clause == message
    assert decomposition.open_remainder == ""


@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения про `X и объясни Y`",
        "найди сообщения про ```X и объясни Y```",
        "найди сообщения про ‘X и объясни Y’",
        "найди сообщения про ‹X и объясни Y›",
        "найди сообщения про ‚X и объясни Y‛",
        "найди сообщения про ‟X и объясни Y”",
    ],
)
def test_locate_action_words_inside_code_literals_are_not_boundaries(message: str) -> None:
    decomposition = agent_runtime_module._closed_locate_remainder(message)  # noqa: SLF001

    assert decomposition.remainder_known is True
    assert decomposition.split is False
    assert decomposition.locate_clause == message


@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения где я писал про «проект Радикс» и затем объясни решение",
        "найди сообщения где я писал про “проект Радикс” и затем объясни решение",
        "найди сообщения где я писал про `проект Радикс` и затем объясни решение",
    ],
)
def test_quoted_topic_is_preserved_before_real_compound_separator(message: str) -> None:
    decomposition = agent_runtime_module._closed_locate_remainder(message)  # noqa: SLF001

    assert decomposition.remainder_known is True
    assert decomposition.split is True
    assert "проект Радикс" in decomposition.locate_clause
    assert decomposition.open_remainder == "объясни решение"


@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения про `X и объясни Y",
        "найди сообщения про ``X и объясни Y`",
        "найди сообщения про ```X и объясни Y",
    ],
)
def test_unclosed_or_malformed_code_literal_makes_locate_split_unknown(message: str) -> None:
    decomposition = agent_runtime_module._closed_locate_remainder(message)  # noqa: SLF001

    assert decomposition.remainder_known is False
    assert decomposition.split is False
    assert decomposition.open_remainder == ""


@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения где я писал про «проект Радикс и объясни Y",
        "найди сообщения где я писал про “проект Радикс и объясни Y",
        'найди сообщения где я писал про "проект Радикс и объясни Y',
        "найди сообщения где я писал про 'проект Радикс и объясни Y",
        "найди сообщения где я писал про «проект Радикс и объясни Y”",
        "найди сообщения где я писал про „проект Радикс и объясни Y»",
        "найди сообщения где я писал про ‘проект Радикс и объясни Y",
        "найди сообщения где я писал про ‹проект Радикс и объясни Y",
        "найди сообщения где я писал про ‚проект Радикс и объясни Y’",
        "найди сообщения где я писал про проект Радикс’ и объясни Y",
        "найди сообщения где я писал про проект Радикс' и объясни Y",
    ],
)
def test_unclosed_or_mismatched_quote_makes_locate_split_unknown(message: str) -> None:
    decomposition = agent_runtime_module._closed_locate_remainder(message)  # noqa: SLF001

    assert decomposition.remainder_known is False
    assert decomposition.split is False
    assert decomposition.open_remainder == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения где я писал про «проект Радикс и объясни Y",
        'найди сообщения где я писал про "проект Радикс и объясни Y',
        "найди сообщения где я писал про 'проект Радикс и объясни Y",
        "найди сообщения где я писал про «проект Радикс и объясни Y”",
        "найди сообщения где я писал про ‘проект Радикс и объясни Y",
        "найди сообщения где я писал про ‹проект Радикс и объясни Y",
        "найди сообщения где я писал про проект Радикс’ и объясни Y",
        "найди сообщения где я писал про проект Радикс' и объясни Y",
    ],
)
async def test_broken_quote_never_promotes_tail_to_model_action(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        model = _AnswerModel("HOSTILE-FALSE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            message,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert model.calls == []
        assert calls == []
        assert "HOSTILE-FALSE-COMPLETION" not in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.parametrize(
    "message",
    [
        "найди сообщения, где я писал don't omit any result",
        "найди сообщения, где я писал l’utilisateur actif",
    ],
)
def test_inner_word_apostrophes_are_not_malformed_quote_boundaries(message: str) -> None:
    decomposition = agent_runtime_module._closed_locate_remainder(message)  # noqa: SLF001

    assert decomposition.remainder_known is True


@pytest.mark.asyncio
async def test_action_word_after_noun_suffix_is_not_promoted_without_separator(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс обсуждали графики производительности.",
        )
        model = _AnswerModel("HOSTILE-PROMOTED-SUFFIX")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про графики объясни итог",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert model.calls == []
        message_calls = [arguments for name, arguments in calls if name == "message_search"]
        assert len(message_calls) == 1
        assert message_calls[0]["query"] != "график"
        assert "HOSTILE-PROMOTED-SUFFIX" not in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.parametrize(
    "locate_clause",
    [
        "найди сообщения где я писал про проект Радикс",
        "найди сообщения где я писал про «проект Радикс»",
        "найди сообщения где я писал про `проект Радикс`",
    ],
)
@pytest.mark.asyncio
async def test_compound_message_locate_keeps_exact_remainder_out_of_search(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    locate_clause: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "объясни, какое решение мы приняли"
        model = _AnswerModel("Мы решили оставить только CUDA graphs.")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            locate_clause + " и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        message_calls = [arguments for name, arguments in calls if name == "message_search"]
        assert len(message_calls) == 1, (calls, result, model.calls)
        boundary_id = message_calls[0].pop("before_message_id", "")
        assert isinstance(boundary_id, str) and boundary_id.startswith("msg_")
        assert message_calls == [
            {
                "query": "проект Радикс",
                "limit": 20,
                "match_all_terms": True,
                "role": "user",
            }
        ]
        assert remainder not in str(message_calls)
        assert [name for name, _arguments in calls] == ["message_search"], (
            result,
            model.calls,
        )
        synthesis_messages, synthesis_tools = model.calls[-1]
        assert [item["content"] for item in synthesis_messages if item.get("role") == "user"][-1] == remainder
        offered_names = {
            str((tool.get("function") or {}).get("name") or tool.get("name") or "")
            for tool in synthesis_tools
        }
        assert not (offered_names & _OUTBOUND_TOOL_NAMES)
        assert "найдено сообщений: 1" in result["message"]
        assert "только CUDA graphs" in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_compound_message_named_file_is_reauthorized_before_exact_action_prompt(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        raw_id = await _registered_file(
            settings,
            storage,
            filename="Штатка.txt",
            body="NAMED-FILE-REAUTHORIZED-BODY",
        )
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сопоставь их с файлом «Штатка»"
        model = _AnswerModel("Сопоставление выполнено по двум подтверждённым источникам.")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"], (
            result,
            model.calls,
        )
        assert remainder not in str(calls[0][1])
        assert len(model.calls) == 1
        prompt, offered = model.calls[0]
        _assert_atomic_two_source_prompt(
            prompt,
            action=remainder,
            file_marker="NAMED-FILE-REAUTHORIZED-BODY",
        )
        user_texts = [str(item.get("content") or "") for item in prompt if item.get("role") == "user"]
        assert user_texts[-1] == remainder
        assert "NAMED-FILE-REAUTHORIZED-BODY" in "\n".join(user_texts)
        assert not {
            "web_search",
            "web_fetch",
            "web_research",
            "workspace_create",
        } & {str((tool.get("function") or {}).get("name") or tool.get("name") or "") for tool in offered}
        assert "Сопоставление выполнено" in result["message"]
        assistant = storage.get_message(str(result["message_id"]), actor.own_id)
        metadata = json.loads(str((assistant or {}).get("metadata_json") or "{}"))
        assert metadata["filename_selected_raw_id"] == raw_id
        assert "message_locate_pending_action" not in metadata
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_thematic_comparison_admits_complete_long_message_tail(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="State.txt",
            body="COMPLETE-LONG-FILE-SOURCE",
        )
        conversation = storage.create_conversation("alice")
        tail = "MESSAGE-TAIL-CONTRADICTION"
        long_message = "проект Радикс " + ("длинный подтверждённый контекст " * 170) + tail
        assert 4_000 < len(long_message) < 8_000
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            long_message,
        )
        remainder = "сравни их с файлом «State»"
        model = _AnswerModel("Сравнение выполнено по полным источникам.")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"], result
        assert calls[0][1]["limit"] == 21
        assert calls[0][1]["include_full_content"] is True
        assert len(model.calls) == 1
        prompt, offered = model.calls[0]
        assert offered == []
        _assert_atomic_two_source_prompt(
            prompt,
            action=remainder,
            file_marker="COMPLETE-LONG-FILE-SOURCE",
        )
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        assert tail in prompt_text
        assert tail not in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["oversized_row", "twenty_first_hit"])
async def test_thematic_comparison_refuses_incomplete_message_evidence(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="State.txt",
            body="INCOMPLETE-GATE-FILE-SOURCE",
        )
        conversation = storage.create_conversation("alice")
        if failure == "oversized_row":
            bodies = ["проект Радикс " + ("X" * 8_100) + "OVERSIZED-TAIL"]
        else:
            bodies = [f"проект Радикс совпадение {index:02d}" for index in range(21)]
        for body in bodies:
            storage.store_message(str(conversation["id"]), "alice", "user", body)
        model = _AnswerModel("HOSTILE-FULL-COMPARISON-CLAIM")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем сравни их с файлом «State»",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"]
        assert calls[0][1]["limit"] == 21
        assert calls[0][1]["include_full_content"] is True
        assert model.calls == []
        assert "Полный набор сообщений по точной теме не удалось подтвердить" in result["message"]
        assert "HOSTILE-FULL-COMPARISON-CLAIM" not in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("remind", {"what": "UNREQUESTED-EFFECT", "when": "завтра"}),
        ("source_search", {"query": "UNREQUESTED-SECOND-READ"}),
        ("memory_save", {"content": "UNREQUESTED-MUTATION"}),
    ],
)
async def test_resolved_two_source_comparison_is_synthesis_only(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="State.txt",
            body="SYNTHESIS-ONLY-FILE-SOURCE",
        )
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        model = _UnrequestedToolModel(tool_name, arguments)
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем сравни их с файлом «State»",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"], result
        assert model.calls
        assert all(offered == [] for _prompt, offered in model.calls)
        assert "UNREQUESTED" not in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_compound_message_locate_keeps_unselected_document_comparison_pending(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сравни с документом"
        model = _AnswerModel("Ложно утверждаю, что сравнение выполнено.")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"]
        assert remainder not in str(calls[0][1])
        assert model.calls == []
        assert "найдено сообщений: 1" in result["message"]
        assert "документ для сравнения не выбран" in result["message"]
        assert "Ложно утверждаю" not in result["message"]
        assistant = storage.get_message(str(result["message_id"]), actor.own_id)
        metadata = json.loads(str((assistant or {}).get("metadata_json") or "{}"))
        assert metadata["message_locate_pending_action"] == remainder
        assert metadata["structural"]["remainder_known"] is True
        assert metadata["structural"]["model_spoke"] is False
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remainder",
    [
        "сравни их с PDF-файлом «Штатка»",
        "сравни их с документом под названием «Штатка»",
        "сравни их с моим документом",
        "сопоставь с файлом Штатка",
        "сравни это с последним файлом",
        "сравни это с загруженным файлом",
        "сопоставь сообщения с содержимым документа",
        "сравни их с данными из файла «Штатка»",
        "сравни найденное с последней версией отчёта",
        "сопоставь сообщения с информацией во вложении",
        "сравни сообщения и файл «Штатка»",
        "проверь сообщения по документу «Штатка»",
        "сравни это с текстом PDF-файла",
        "сравни с JPEG «Штатка»",
        "сопоставь со скриншотом «Штатка»",
    ],
)
async def test_any_unresolved_file_comparison_remains_pending_without_model_claim(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    remainder: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        model = _AnswerModel("HOSTILE-CLAIMS-TWO-SOURCE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"]
        assert model.calls == []
        assert "HOSTILE-CLAIMS-TWO-SOURCE-COMPLETION" not in result["message"]
        assert "документ для сравнения не выбран" in result["message"].casefold()
        assistant = storage.get_message(str(result["message_id"]), actor.own_id)
        metadata = json.loads(str((assistant or {}).get("metadata_json") or "{}"))
        assert metadata["message_locate_pending_action"] == remainder
        assert metadata["structural"]["model_spoke"] is False
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_pending_message_comparison_resumes_on_exact_current_attachment_with_original_boundary(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "AMBIENT-CONTEXT-MUST-NOT-ENTER-COMPARISON",
        )
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сравни с документом"
        model = _AnswerModel("HOSTILE-FALSE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        pending = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )
        pending_row = storage.get_message(str(pending["message_id"]), actor.own_id)
        pending_metadata = json.loads(str((pending_row or {}).get("metadata_json") or "{}"))
        original_boundary = pending_metadata["message_locate_source_user_message_id"]
        assert model.calls == []

        raw_id = await _registered_file(
            settings,
            storage,
            filename="Штатка.txt",
            body="CURRENT-ATTACHMENT-REAUTHORIZED-BODY",
        )
        attachment = runtime._owned_file_attachment(  # noqa: SLF001
            raw_id,
            tenant_id="alice",
            person_id="alice",
        )
        assert attachment is not None
        model.answer = "Сравнение выполнено после повторного чтения обоих источников."
        resumed = await runtime.chat(
            "alice",
            "Загружен документ: Штатка.txt",
            actor=actor,
            conversation_id=str(conversation["id"]),
            attachments=[attachment],
            synthetic_document_notice=True,
            enable_tools=True,
        )

        message_calls = [arguments for name, arguments in calls if name == "message_search"]
        assert len(message_calls) == 2, (resumed, model.calls, calls)
        assert message_calls[1]["before_message_id"] == original_boundary
        assert message_calls[1]["query"] == "проект Радикс"
        assert len(model.calls) == 1
        prompt, _offered = model.calls[0]
        _assert_atomic_two_source_prompt(
            prompt,
            action=remainder,
            file_marker="CURRENT-ATTACHMENT-REAUTHORIZED-BODY",
        )
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        assert [item["content"] for item in prompt if item.get("role") == "user"][-1] == remainder
        assert "CURRENT-ATTACHMENT-REAUTHORIZED-BODY" in prompt_text
        assert "CUDA graphs only" in prompt_text
        assert "FRIDAY_UNTRUSTED_MESSAGE_SEARCH_DATA" in prompt_text
        assert "AMBIENT-CONTEXT-MUST-NOT-ENTER-COMPARISON" not in prompt_text
        assert "HOSTILE-FALSE-COMPLETION" not in resumed["message"]
        assert "Сравнение выполнено после повторного чтения" in resumed["message"]
        resumed_row = storage.get_message(str(resumed["message_id"]), actor.own_id)
        resumed_metadata = json.loads(str((resumed_row or {}).get("metadata_json") or "{}"))
        assert "message_locate_pending_action" not in resumed_metadata
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_pending_message_comparison_resumes_on_quote_only_exact_filename(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="Штатка.txt",
            body="QUOTE-ONLY-SELECTED-FILE-BODY",
        )
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сравни с документом"
        model = _AnswerModel("HOSTILE-FALSE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        pending = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )
        pending_row = storage.get_message(str(pending["message_id"]), actor.own_id)
        pending_metadata = json.loads(str((pending_row or {}).get("metadata_json") or "{}"))
        original_boundary = pending_metadata["message_locate_source_user_message_id"]
        assert model.calls == []

        model.answer = "Сравнение выполнено по подтверждённым источникам."
        resumed = await runtime.chat(
            "alice",
            "«Штатка.txt»",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        message_calls = [arguments for name, arguments in calls if name == "message_search"]
        assert len(message_calls) == 2, (resumed, model.calls, calls)
        assert message_calls[1]["before_message_id"] == original_boundary
        assert len(model.calls) == 1
        prompt, _offered = model.calls[0]
        _assert_atomic_two_source_prompt(
            prompt,
            action=remainder,
            file_marker="QUOTE-ONLY-SELECTED-FILE-BODY",
        )
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        assert [item["content"] for item in prompt if item.get("role") == "user"][-1] == remainder
        assert "QUOTE-ONLY-SELECTED-FILE-BODY" in prompt_text
        assert "CUDA graphs only" in prompt_text
        assert "FRIDAY_UNTRUSTED_MESSAGE_SEARCH_DATA" in prompt_text
        assert "HOSTILE-FALSE-COMPLETION" not in resumed["message"]
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locate_clause", "remainder"),
    [
        ("что я тебе писал сегодня", "сравни с документом «Штатка»"),
        (
            "покажи все мои сообщения сегодня",
            "проанализируй их с документом «Штатка»",
        ),
    ],
)
async def test_complete_day_window_comparison_admits_both_exact_sources(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    locate_clause: str,
    remainder: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="Штатка.txt",
            body="DAY-WINDOW-SELECTED-FILE-BODY",
        )
        conversation = storage.create_conversation("alice")
        tail = "DAY-WINDOW-PRIVATE-TAIL"
        day_message = (
            "Сегодня по проекту решили оставить CUDA graphs only. " + ("подтверждённый контекст " * 80) + tail
        )
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            day_message,
        )
        model = _AnswerModel("Сопоставление дневной переписки и документа выполнено.")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            locate_clause + " и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls] == ["message_search"]
        assert calls[0][1]["include_full_content"] is True
        assert len(model.calls) == 1, result
        prompt, _offered = model.calls[0]
        _assert_atomic_two_source_prompt(
            prompt,
            action=remainder,
            file_marker="DAY-WINDOW-SELECTED-FILE-BODY",
        )
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        assert [item["content"] for item in prompt if item.get("role") == "user"][-1] == remainder
        assert "DAY-WINDOW-SELECTED-FILE-BODY" in prompt_text
        assert "CUDA graphs only" in prompt_text
        assert tail in prompt_text
        assert tail not in result["message"]
        assert "FRIDAY_UNTRUSTED_MESSAGE_SEARCH_DATA" in prompt_text
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("intermediate", ["99", "файл «Штатка»"])
async def test_pending_message_comparison_resumes_after_filename_ordinal_reauth(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    intermediate: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="Штатка первая.txt",
            body="FIRST-NAMED-CANDIDATE",
            received_at="2026-08-19T10:00:00+00:00",
        )
        second_raw_id = await _registered_file(
            settings,
            storage,
            filename="Штатка вторая.txt",
            body="SECOND-NAMED-CANDIDATE",
            received_at="2026-08-19T11:00:00+00:00",
        )
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сопоставь их с файлом «Штатка»"
        model = _AnswerModel("HOSTILE-FALSE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        pending = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )
        pending_row = storage.get_message(str(pending["message_id"]), actor.own_id)
        pending_metadata = json.loads(str((pending_row or {}).get("metadata_json") or "{}"))
        original_boundary = pending_metadata["message_locate_source_user_message_id"]
        assert pending_metadata["message_locate_pending_action"] == remainder
        assert pending_metadata["filename_result_pending_action"] == remainder
        assert pending_metadata["filename_result_pending_origin"] == "message_locate"
        assert pending_metadata["filename_result_pending_message_source_user_message_id"] == original_boundary
        assert "укажите номер файла" in pending["message"].casefold()
        assert model.calls == []

        still_pending = await runtime.chat(
            "alice",
            intermediate,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )
        still_pending_row = storage.get_message(str(still_pending["message_id"]), actor.own_id)
        still_pending_metadata = json.loads(str((still_pending_row or {}).get("metadata_json") or "{}"))
        assert still_pending_metadata["message_locate_source_user_message_id"] == original_boundary
        assert still_pending_metadata["message_locate_pending_action"] == remainder
        assert still_pending_metadata["filename_result_pending_action"] == remainder
        assert still_pending_metadata["filename_result_pending_origin"] == "message_locate"
        assert (
            still_pending_metadata["filename_result_pending_message_source_user_message_id"]
            == original_boundary
        )
        assert model.calls == []

        model.answer = "Сопоставлен второй выбранный документ."
        resumed = await runtime.chat(
            "alice",
            "2",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        message_calls = [arguments for name, arguments in calls if name == "message_search"]
        assert len(message_calls) == 3, (resumed, model.calls, calls)
        assert all(call["before_message_id"] == original_boundary for call in message_calls[1:])
        assert len(model.calls) == 1
        prompt, _offered = model.calls[0]
        _assert_atomic_two_source_prompt(
            prompt,
            action=remainder,
            file_marker="SECOND-NAMED-CANDIDATE",
        )
        prompt_text = "\n".join(str(item.get("content") or "") for item in prompt)
        assert [item["content"] for item in prompt if item.get("role") == "user"][-1] == remainder
        assert "SECOND-NAMED-CANDIDATE" in prompt_text
        assert "FIRST-NAMED-CANDIDATE" not in prompt_text
        assert "CUDA graphs only" in prompt_text
        assert "FRIDAY_UNTRUSTED_MESSAGE_SEARCH_DATA" in prompt_text
        assert "HOSTILE-FALSE-COMPLETION" not in resumed["message"]
        resumed_row = storage.get_message(str(resumed["message_id"]), actor.own_id)
        resumed_metadata = json.loads(str((resumed_row or {}).get("metadata_json") or "{}"))
        assert resumed_metadata["filename_selected_raw_id"] == second_raw_id
        assert "message_locate_pending_action" not in resumed_metadata
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ["message_pointer", "both_pointers", "foreign_conversation", "message_keys_removed"],
)
async def test_message_comparison_filename_sidecar_requires_exact_source_chain(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        await _registered_file(
            settings,
            storage,
            filename="Штатка первая.txt",
            body="FIRST-SOURCE-MUST-NOT-BE-RELEASED",
            received_at="2026-08-19T10:00:00+00:00",
        )
        await _registered_file(
            settings,
            storage,
            filename="Штатка вторая.txt",
            body="SECOND-SOURCE-MUST-NOT-BE-RELEASED",
            received_at="2026-08-19T11:00:00+00:00",
        )
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сопоставь их с файлом «Штатка»"
        model = _AnswerModel("HOSTILE-FALSE-TWO-SOURCE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        pending = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )
        pending_row = storage.get_message(str(pending["message_id"]), actor.own_id)
        metadata = json.loads(str((pending_row or {}).get("metadata_json") or "{}"))
        original_source = metadata["message_locate_source_user_message_id"]
        assert metadata["filename_result_pending_message_source_user_message_id"] == original_source

        if tamper == "message_pointer":
            metadata["message_locate_source_user_message_id"] = "msg_0000000000000000"
        elif tamper == "both_pointers":
            metadata["message_locate_source_user_message_id"] = "msg_0000000000000000"
            metadata["filename_result_pending_message_source_user_message_id"] = "msg_0000000000000000"
        elif tamper == "foreign_conversation":
            foreign = storage.create_conversation("alice")
            foreign_source = storage.store_message(
                str(foreign["id"]),
                "alice",
                "user",
                "найди сообщения где я писал про проект Радикс и затем " + remainder,
            )
            foreign_id = str(foreign_source["id"])
            metadata["message_locate_source_user_message_id"] = foreign_id
            metadata["filename_result_pending_message_source_user_message_id"] = foreign_id
        else:
            metadata.pop("message_locate_pending_action")
            metadata.pop("message_locate_source_user_message_id")
        storage.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), str(pending["message_id"])),
        )
        storage.commit()

        refused = await runtime.chat(
            "alice",
            "2",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert [name for name, _arguments in calls].count("message_search") == 1
        assert model.calls == []
        assert "Связь списка файлов с исходным поиском сообщений" in refused["message"]
        assert "HOSTILE-FALSE-TWO-SOURCE-COMPLETION" not in refused["message"]
        refused_row = storage.get_message(str(refused["message_id"]), actor.own_id)
        refused_metadata = json.loads(str((refused_row or {}).get("metadata_json") or "{}"))
        assert "filename_result_pending_action" not in refused_metadata
        assert "message_locate_pending_action" not in refused_metadata
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_stale_message_pending_pointer_does_not_resume_on_new_attachment(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "user",
            "По проекту Радикс решили оставить CUDA graphs only.",
        )
        remainder = "сравни с документом"
        model = _AnswerModel("HOSTILE-SHOULD-NOT-RESUME-STALE-ACTION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        pending = await runtime.chat(
            "alice",
            "найди сообщения где я писал про проект Радикс и затем " + remainder,
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )
        stale = (datetime.now(UTC) - timedelta(hours=13)).isoformat()
        storage.execute(
            "UPDATE messages SET created_at=? WHERE id=?",
            (stale, str(pending["message_id"])),
        )
        storage.commit()
        raw_id = await _registered_file(
            settings,
            storage,
            filename="Штатка.txt",
            body="STALE-POINTER-ATTACHMENT-BODY",
        )
        attachment = runtime._owned_file_attachment(  # noqa: SLF001
            raw_id,
            tenant_id="alice",
            person_id="alice",
        )
        assert attachment is not None
        await runtime.chat(
            "alice",
            "Загружен документ: Штатка.txt",
            actor=actor,
            conversation_id=str(conversation["id"]),
            attachments=[attachment],
            synthetic_document_notice=True,
            enable_tools=True,
        )

        assert [name for name, _arguments in calls].count("message_search") == 1
        assert all(
            remainder not in "\n".join(str(item.get("content") or "") for item in prompt)
            for prompt, _tools in model.calls
        )
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_unclosed_code_literal_in_message_locate_fails_before_search_or_hostile_model(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        model = _AnswerModel("HOSTILE-FALSE-COMPLETION")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "найди сообщения где я писал про `проект Радикс и объясни секрет",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        assert calls == []
        assert model.calls == []
        assert "Не удалось безопасно отделить поиск сообщений" in result["message"]
        assert "HOSTILE-FALSE-COMPLETION" not in result["message"]
    finally:
        storage.close(final=True)


@pytest.mark.asyncio
async def test_person_followup_arbiter_outage_is_unknown_without_web_or_archive(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        runtime, actor, kernel = _runtime(settings, storage)
        conversation = storage.create_conversation("alice")
        storage.store_message(str(conversation["id"]), "alice", "user", "Что писал Yato?")
        storage.store_message(
            str(conversation["id"]),
            "alice",
            "assistant",
            "Yato писал о тестировании.",
            metadata={
                "tools_used": ["user_activity"],
                "structural": {
                    "verdict_kind": "человек",
                    "answer_present": True,
                    "model_spoke": True,
                    "remainder_known": False,
                },
            },
        )
        model = _AnswerModel("MODEL-MUST-NOT-SPEAK")
        runtime.llm = model  # type: ignore[assignment]

        async def arbiter_outage(*_args: Any, **_kwargs: Any) -> tuple[str, None]:
            return "", None

        monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", arbiter_outage)
        calls: list[tuple[str, dict[str, Any]]] = []
        original_execute = kernel.execute

        async def observed_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
            execution_scope: str = "dialogue",
        ) -> Any:
            if name in {"message_search", "source_search"}:
                assert execution_scope == "internal"
            calls.append((name, dict(arguments)))
            return await original_execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )

        kernel.execute = observed_execute  # type: ignore[method-assign]
        result = await runtime.chat(
            "alice",
            "А Пегас?",
            actor=actor,
            conversation_id=str(conversation["id"]),
            enable_tools=True,
        )

        forbidden = {
            "web_search",
            "web_fetch",
            "web_research",
            "memory_search",
            "source_search",
            "collect_files",
            "what_happened",
            "upcoming",
        }
        assert not ({name for name, _arguments in calls} & forbidden)
        assert calls == []
        assert "Не удалось однозначно определить участника" in result["message"]
        assert "MODEL-MUST-NOT-SPEAK" not in result["message"]
        assert result["tools_used"] == []
    finally:
        storage.close(final=True)
