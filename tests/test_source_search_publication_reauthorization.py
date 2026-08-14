"""Late publication authorization for private ``source_search`` evidence.

Every case runs the complete :class:`AgentRuntime` against an isolated test
database and an in-process execution kernel.  The synthetic model mutates the
durable authority only after the projected private row has reached synthesis;
no provider, live service, or network adapter is involved.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.permissions import AuthorizationService
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

_OWNER = "source-publication-owner"
_REQUEST = "Найди в ранее загруженном файле, какая должность указана у Синтетикова."
_QUERY = "синтетиков"
_FOCUS = "синтетиков должност"
_SOURCE_CANARY = "SOURCE-SEARCH-PUBLICATION-CANARY-814"
_MODEL_CANARY = "MODEL-SOURCE-SEARCH-PUBLICATION-CANARY-814"
_STRUCTURAL_CANARY = "STRUCTURAL-SOURCE-SEARCH-PUBLICATION-CANARY-814"
_VERIFIER_ISSUE_CANARY = "VERIFIER-ISSUE-SOURCE-CANARY-814"
_FORBIDDEN_WEB_CANARY = "FORBIDDEN-SOURCE-WEB-SIBLING-CANARY-814"
_ISSUE = "source_search_authority_changed_before_publication"
_ANSWER = (
    "В найденном ранее загруженном источнике указана должность: "
    f"ведущий инженер по эксплуатации ({_SOURCE_CANARY}; {_MODEL_CANARY})."
)


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
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _MissingIdentityKernel(_RecordingKernel):
    """Return two valid projected rows but only one durable Raw identity."""

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
        if name != "source_search" or not result.success:
            return result
        data = copy.deepcopy(result.data)
        assert isinstance(data, dict)
        rows = data.get("results")
        assert isinstance(rows, list) and len(rows) == 2
        assert isinstance(rows[1], dict)
        rows[1].pop("raw_object_id", None)
        return ToolResult(name, True, data=data)


class _DeterministicSourceModel:
    enabled = True
    model = "synthetic-deterministic-source-publication-model"
    total_budget_sec = 10.0

    def __init__(self, mutate: Callable[[], None] | None = None) -> None:
        self.mutate = mutate
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        snapshot = copy.deepcopy(messages)
        self.calls.append(snapshot)
        serialized = json.dumps(snapshot, ensure_ascii=False)
        assert "FRIDAY_SOURCE_SEARCH_DATA" in serialized
        assert _SOURCE_CANARY in serialized
        if self.mutate is not None:
            mutate, self.mutate = self.mutate, None
            mutate()
        return {
            "content": _ANSWER,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _VerifierIssueCanaryModel:
    enabled = True
    model = "synthetic-source-verifier-issue-model"
    total_budget_sec = 10.0

    def __init__(self) -> None:
        self.synthesis_calls = 0
        self.verifier_calls = 0
        self.repair_calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        snapshot = copy.deepcopy(messages)
        system_text = "\n".join(
            str(item.get("content") or "") for item in snapshot if str(item.get("role") or "") == "system"
        )
        serialized = json.dumps(snapshot, ensure_ascii=False)
        if "Проверь ответ по двум независимым условиям" in system_text:
            self.verifier_calls += 1
            assert _SOURCE_CANARY in serialized
            return {
                "content": json.dumps(
                    {
                        "ok": False,
                        "request_satisfied": False,
                        "score": 0.0,
                        "issues": [f"private verifier quote: {_SOURCE_CANARY}; {_VERIFIER_ISSUE_CANARY}"],
                    },
                    ensure_ascii=False,
                ),
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        if "Автопроверка нашла в ответе несоответствия" in system_text:
            self.repair_calls += 1
            assert _VERIFIER_ISSUE_CANARY in serialized
            return {
                "content": _ANSWER,
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        assert "FRIDAY_SOURCE_SEARCH_DATA" in serialized
        assert _SOURCE_CANARY in serialized
        self.synthesis_calls += 1
        return {
            "content": _ANSWER,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _ModelSelectedSourceModel:
    enabled = True
    model = "synthetic-model-selected-source-publication-model"
    total_budget_sec = 10.0

    def __init__(
        self,
        mutate: Callable[[], None] | None,
        *,
        include_web_sibling: bool = False,
    ) -> None:
        self.mutate = mutate
        self.include_web_sibling = include_web_sibling
        self.calls: list[list[dict[str, Any]]] = []
        self.mutated = False
        self.initial_offered_names: set[str] = set()
        self.synthesis_offered_names: list[set[str]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        snapshot = copy.deepcopy(messages)
        self.calls.append(snapshot)
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

        serialized = json.dumps(snapshot, ensure_ascii=False)
        offered = {
            str((item.get("function") or {}).get("name") or item.get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        if any(str(item.get("role") or "") == "tool" for item in snapshot):
            assert _SOURCE_CANARY in serialized
            self.synthesis_offered_names.append(offered)
            assert offered == set(), "source_search synthesis must revoke every schema"
            if self.mutate is not None:
                assert not self.mutated
                self.mutate()
                self.mutated = True
            return {
                "content": _ANSWER,
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }

        assert "source_search" in offered
        self.initial_offered_names = offered
        tool_calls = [
            {
                "id": "call-source-publication-reauth",
                "type": "function",
                "function": {
                    "name": "source_search",
                    "arguments": json.dumps({"query": "ORION", "focus": "ORION unit", "limit": 10}),
                },
            }
        ]
        if self.include_web_sibling:
            assert "web_search" in offered
            tool_calls.append(
                {
                    "id": "call-forbidden-web-sibling",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": _FORBIDDEN_WEB_CANARY}),
                    },
                }
            )
        return {
            "content": "",
            "tool_calls": tool_calls,
            "_queue_wait_sec": 0.0,
        }


class _ForbiddenModel:
    enabled = True
    model = "forbidden-source-publication-model"
    total_budget_sec = 10.0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        raise AssertionError("terminal source_search answer must not call the model")


def _seed_source(
    storage: Any,
    *,
    text: str | None = None,
    filename: str = "synthetic-source.txt",
) -> RawObject:
    raw = RawObject(
        id=new_id("raw"),
        user_id=_OWNER,
        source="synthetic-upload",
        source_ref=new_id("synthetic-source"),
        raw_content=text
        or (
            "Синтетическое служебное вступление.\n"
            f"Должность Синтетикова ({_SOURCE_CANARY}): ведущий инженер по эксплуатации."
        ),
        content_type="file",
        metadata_json={"filename": filename, "uploaded_by": _OWNER},
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=_OWNER,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
        )
    )
    return raw


def _soft_delete_raw(storage: Any, raw_id: str) -> None:
    with storage.transaction() as connection:
        cursor = connection.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-14T00:00:00Z' WHERE id=?",
            (raw_id,),
        )
    assert cursor.rowcount == 1


def _runtime(
    settings: Any,
    storage: Any,
    model: Any,
    *,
    kernel_type: type[_RecordingKernel] = _RecordingKernel,
    verify_answers: bool = False,
) -> tuple[AgentRuntime, _RecordingKernel, Any]:
    storage.ensure_user(_OWNER, preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = kernel_type(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    runtime = AgentRuntime(
        replace(settings, verify_answers=verify_answers, verify_min_answer_chars=1),
        storage,
        llm=model,
        kernel=kernel,
    )
    actor = authorization.actor_for_user(_OWNER, source="synthetic-test")
    return runtime, kernel, actor


def _stored_rows(
    storage: Any,
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    assistant = storage.get_message(str(response["message_id"]), _OWNER)
    assert assistant is not None and assistant["role"] == "assistant"
    assistant_metadata = json.loads(str(assistant["metadata_json"] or "{}"))
    assert isinstance(assistant_metadata, dict)
    messages = storage.get_conversation_messages(
        str(response["conversation_id"]),
        user_id=_OWNER,
        limit=20,
    )
    user = next(item for item in messages if item["role"] == "user")
    user_metadata = json.loads(str(user["metadata_json"] or "{}"))
    assert isinstance(user_metadata, dict)
    return assistant, assistant_metadata, user_metadata


def _assert_source_publication_failed_closed(
    storage: Any,
    response: dict[str, Any],
    *,
    forbidden: tuple[str, ...] = (_SOURCE_CANARY, _MODEL_CANARY, _STRUCTURAL_CANARY),
) -> None:
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    for canary in forbidden:
        assert canary not in public
    assert response["source_search_authority_changed_before_publication"] is True
    assert response["attachment_authority_changed_before_publication"] is False
    assert response["message_format"] == "plain"
    assert response["verified"] is False
    assert response["verification_status"] == "unknown"
    assert response["verification"]["issues"] == [_ISSUE]
    assert response["files"] == []
    assert response["voice"] is None
    assert response["citations"] == []
    assert response["web_sources"] == []

    assistant, metadata, user_metadata = _stored_rows(storage, response)
    assert assistant["content"] == response["message"]
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    for canary in forbidden:
        assert canary not in durable
    assert metadata["verification_status"] == "unknown"
    assert metadata["verification"]["issues"] == [_ISSUE]
    assert metadata["private_context_lineage"] is True
    assert "source_search_result_raw_ids" not in metadata
    assert user_metadata["private_context_lineage"] is True


@pytest.mark.asyncio
async def test_deterministic_source_search_soft_delete_after_synthesis_fails_closed(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    raw = _seed_source(storage)
    model = _DeterministicSourceModel(lambda: _soft_delete_raw(storage, raw.id))
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert len(model.calls) == 1
    assert kernel.calls == [("source_search", {"query": _QUERY, "focus": _FOCUS, "limit": 10})]
    _assert_source_publication_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_model_selected_source_search_soft_delete_after_tool_await_fails_closed(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    raw = _seed_source(
        storage,
        text=f"ORION unit {_SOURCE_CANARY}: ведущий инженер по эксплуатации.",
        filename="synthetic-orion.txt",
    )
    model = _ModelSelectedSourceModel(lambda: _soft_delete_raw(storage, raw.id))
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        _OWNER,
        "Расскажи про ORION",
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.mutated is True, json.dumps(model.calls, ensure_ascii=False)
    assert kernel.calls == [("source_search", {"query": "ORION", "focus": "ORION unit", "limit": 10})]
    _assert_source_publication_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_model_selected_source_search_skips_web_sibling_and_revokes_schemas(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    raw = _seed_source(
        storage,
        text=f"ORION unit {_SOURCE_CANARY}: ведущий инженер по эксплуатации.",
        filename="synthetic-orion-sibling.txt",
    )
    model = _ModelSelectedSourceModel(None, include_web_sibling=True)
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        _OWNER,
        "Расскажи про ORION",
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert "source_search" in model.initial_offered_names
    assert "web_search" in model.initial_offered_names
    assert model.synthesis_offered_names == [set()]
    assert kernel.calls == [("source_search", {"query": "ORION", "focus": "ORION unit", "limit": 10})]
    assert response["tools_used"] == ["source_search"]
    assert response["message"].endswith(_ANSWER)
    assert response["web_sources"] == []
    assert response["web_query_notice"] == ""
    assert _FORBIDDEN_WEB_CANARY not in json.dumps(response, ensure_ascii=False)
    assistant, metadata, _ = _stored_rows(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _FORBIDDEN_WEB_CANARY not in durable
    assert metadata["tools_used"] == ["source_search"]
    assert metadata["source_search_result_raw_ids"] == [raw.id]
    assert metadata["private_context_lineage"] is True


@pytest.mark.asyncio
async def test_source_search_knowledge_read_revoked_after_synthesis_fails_closed(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    _seed_source(storage)
    model = _DeterministicSourceModel(
        lambda: storage.set_permission_override(_OWNER, "knowledge.read", "deny")
    )
    runtime, _, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert len(model.calls) == 1
    _assert_source_publication_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_source_search_verifier_issue_cannot_persist_private_canary(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    raw = _seed_source(storage)
    model = _VerifierIssueCanaryModel()
    runtime, kernel, actor = _runtime(
        settings,
        storage,
        model,
        verify_answers=True,
    )

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.synthesis_calls == 1
    assert model.verifier_calls == 2
    assert model.repair_calls == 1
    assert kernel.calls == [("source_search", {"query": _QUERY, "focus": _FOCUS, "limit": 10})]
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    for canary in (_SOURCE_CANARY, _MODEL_CANARY, _VERIFIER_ISSUE_CANARY):
        assert canary not in public
    assert response["source_search_authority_changed_before_publication"] is False
    assert response["attachment_authority_changed_before_publication"] is False
    assert response["verified"] is False
    assert response["verification_status"] == "unknown"
    assert response["files"] == []
    assert response["voice"] is None

    assistant, metadata, _ = _stored_rows(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    for canary in (_SOURCE_CANARY, _MODEL_CANARY, _VERIFIER_ISSUE_CANARY):
        assert canary not in durable
    assert metadata["verification_status"] == "unknown"
    assert metadata["source_search_result_raw_ids"] == [raw.id]
    assert metadata["private_context_lineage"] is True


@pytest.mark.asyncio
async def test_source_search_shown_and_raw_identity_cardinality_mismatch_fails_closed(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    first = _seed_source(storage, filename="synthetic-first.txt")
    second = _seed_source(storage, filename="synthetic-second.txt")
    model = _DeterministicSourceModel()
    runtime, kernel, actor = _runtime(
        settings,
        storage,
        model,
        kernel_type=_MissingIdentityKernel,
    )

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert {first.id, second.id} == {
        str(row["id"]) for row in storage.search_raw_objects(_OWNER, _QUERY, limit=10)
    }
    assert model.calls == [], "an unstamped or cardinality-mismatched page must stop before synthesis"
    assert len(kernel.calls) == 1
    assert "не завершился с проверяемым результатом" in response["message"]
    assert response["verified"] is False
    assistant, metadata, _user_metadata = _stored_rows(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _SOURCE_CANARY not in durable
    assert _MODEL_CANARY not in durable
    assert "source_search_result_raw_ids" not in metadata


@pytest.mark.asyncio
async def test_unchanged_source_search_publishes_and_keeps_exact_private_lineage(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    raw = _seed_source(storage)
    model = _DeterministicSourceModel()
    runtime, kernel, actor = _runtime(settings, storage, model)

    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert len(model.calls) == 1
    assert kernel.calls == [("source_search", {"query": _QUERY, "focus": _FOCUS, "limit": 10})]
    assert response["message"].endswith(_ANSWER)
    assert response["source_search_authority_changed_before_publication"] is False
    assert response["attachment_authority_changed_before_publication"] is False
    assert raw.id not in json.dumps(response, ensure_ascii=False)
    assistant, metadata, user_metadata = _stored_rows(storage, response)
    assert assistant["content"] == response["message"]
    assert metadata["source_search_result_raw_ids"] == [raw.id]
    assert metadata["private_context_lineage"] is True
    assert user_metadata["private_context_lineage"] is True


@pytest.mark.asyncio
async def test_terminal_source_search_structural_text_is_discarded_after_late_revocation(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(_OWNER, preset_key="owner")
    runtime, kernel, actor = _runtime(settings, storage, _ForbiddenModel())
    original = runtime._prefetch_archived_source_if_asked

    async def prefetch_then_close_the_remainder(*args: Any, **kwargs: Any) -> bool:
        owned = await original(*args, **kwargs)
        assert owned is True
        context = args[6]
        assert context.source_search_result_expected_count == 0
        assert context.source_search_result_raw_ids == []
        context.structural_answer = _STRUCTURAL_CANARY
        context.open_remainder = ""
        context.remainder_known = True
        storage.set_permission_override(_OWNER, "knowledge.read", "deny")
        return True

    monkeypatch.setattr(runtime, "_prefetch_archived_source_if_asked", prefetch_then_close_the_remainder)
    response = await runtime.chat(
        _OWNER,
        _REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert kernel.calls == [("source_search", {"query": _QUERY, "focus": _FOCUS, "limit": 10})]
    _assert_source_publication_failed_closed(storage, response)
