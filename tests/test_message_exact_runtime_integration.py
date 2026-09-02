"""Runtime activation coverage for the code-owned exact-message lane.

These tests deliberately exercise the primary runtime boundary rather than the
standalone carrier/storage contracts.  They keep the direct adapter invisible
to the model, pin its accepted boundary to the durable authenticated user row,
and observe the final assistant transaction without starting any external
service.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import friday.agent_runtime as agent_runtime
from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.turn_context import AuthenticatedTurnContext, TurnContextIssuer
from friday.permissions import AuthorizationService, LEGACY_OWNER_USER_ID
from friday.retrieval.message_exact_contract import (
    MESSAGE_EXACT_MAX_FULL_PAGE_CHARS,
    MESSAGE_EXACT_MAX_FULL_ROW_CHARS,
    MessageExactContentMode,
    MessageExactPage,
    MessageExactProjection,
    MessageExactPublicationStatus,
    MessageExactRequest,
)
from friday.retrieval.message_exact_internal import MessageExactInternalAdapter
from friday.web_surfer import WebSurfer

_PROMPT = "проанализируй все сообщения за 1 сентября"
_TOPICAL_PROMPT = "найди ORIONMARKER в нашей переписке"
_SOURCE_CANARY = "EXACT-RUNTIME-SOURCE-CANARY-8D4"
_METADATA_CANARY = "EXACT-RUNTIME-METADATA-CANARY-8D4"
_MODEL_DRAFT = f"Модельный черновик по {_SOURCE_CANARY}."
_PUBLICATION_DENIAL = (
    "Доступ к переписке изменился до публикации; найденные данные не публикую."
)


class _ProjectionModel:
    enabled = True
    model = "synthetic-message-exact-runtime-model"
    total_budget_sec = 10.0

    def __init__(self, mutate_after_projection: Callable[[], None] | None = None) -> None:
        self._mutate_after_projection = mutate_after_projection
        self.calls: list[list[dict[str, Any]]] = []
        self.main_messages: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del tools, kwargs
        # JSON round-tripping is intentional: a process-private page/request in
        # model arguments must fail here instead of being hidden by ``default``.
        snapshot = json.loads(json.dumps(messages, ensure_ascii=False))
        assert isinstance(snapshot, list)
        self.calls.append(snapshot)
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        system_text = "\n".join(
            str(item.get("content") or "")
            for item in snapshot
            if str(item.get("role") or "") == "system"
        )
        if "FRIDAY_UNTRUSTED_MESSAGE_HISTORY_DATA" in serialized:
            self.main_messages.append(snapshot)
            if self._mutate_after_projection is not None:
                mutate, self._mutate_after_projection = self._mutate_after_projection, None
                mutate()
            return {
                "content": _MODEL_DRAFT,
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }
        if "Ответь одним словом: РАЗГОВОР или ЗАПРОС." in system_text:
            content = "ЗАПРОС"
        elif "Классифицируй ТОЛЬКО чтение личной ленты/календаря" in system_text:
            content = '{"direction":"none","window_kind":"none"}'
        elif "Никаких пояснений, только JSON." in system_text:
            content = '{"вид":"архив","запрос":"","кто":"","дни":[]}'
        else:
            # Lexical message-location admission owns the focused cases below.
            # Any unrelated optional arbiter gets a harmless request verdict.
            content = "ЗАПРОС"
        return {
            "content": content,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _set_created_at(storage: Any, message_id: str, value: str) -> None:
    with storage.transaction() as conn:
        changed = conn.execute(
            "UPDATE messages SET created_at=? WHERE id=?",
            (value, message_id),
        )
    assert changed.rowcount == 1


def _seed_window(storage: Any, *, title: str) -> tuple[dict[str, Any], dict[str, Any]]:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, title)
    target = storage.store_message(
        str(conversation["id"]),
        LEGACY_OWNER_USER_ID,
        "assistant",
        f"{_SOURCE_CANARY}: подтверждённая строка окна.",
        metadata={"private_runtime_marker": _METADATA_CANARY},
    )
    _set_created_at(storage, str(target["id"]), "2026-09-01T08:00:00+00:00")

    # The target must not reach the model through ordinary recent history.  It
    # sits outside the newest twenty rows and is available only to the exact
    # September 1 selector.
    for index in range(24):
        filler = storage.store_message(
            str(conversation["id"]),
            LEGACY_OWNER_USER_ID,
            "user" if index % 2 == 0 else "assistant",
            f"ordinary recent filler {index:02d}",
        )
        _set_created_at(
            storage,
            str(filler["id"]),
            f"2026-09-02T12:{index:02d}:00+00:00",
        )
    return conversation, target


def _observe_exact_lane(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    observed = SimpleNamespace(
        prepared=[],
        projected=[],
        reauthorized=[],
        assistant_stores=[],
    )
    original_prepare = MessageExactInternalAdapter.prepare_in_transaction
    original_project = MessageExactInternalAdapter.project_for_model
    original_reauthorize = (
        MessageExactInternalAdapter.reauthorize_for_publication_in_transaction
    )
    original_store = agent_runtime.store_message_in_transaction

    def recording_prepare(
        adapter: MessageExactInternalAdapter,
        conn: Any,
        *,
        context: AuthenticatedTurnContext,
        request: MessageExactRequest,
    ) -> MessageExactPage:
        assert conn.in_transaction
        page = original_prepare(adapter, conn, context=context, request=request)
        observed.prepared.append(
            SimpleNamespace(conn=conn, context=context, request=request, page=page)
        )
        return page

    def recording_project(
        adapter: MessageExactInternalAdapter,
        page: MessageExactPage,
    ) -> MessageExactProjection:
        projection = original_project(adapter, page)
        observed.projected.append(SimpleNamespace(page=page, projection=projection))
        return projection

    def recording_reauthorize(
        adapter: MessageExactInternalAdapter,
        conn: Any,
        *,
        context: AuthenticatedTurnContext,
        page: MessageExactPage,
    ):  # noqa: ANN202 - retain the exact production decision type
        assert conn.in_transaction
        decision = original_reauthorize(adapter, conn, context=context, page=page)
        observed.reauthorized.append(
            SimpleNamespace(
                conn=conn,
                context=context,
                page=page,
                decision=decision,
                in_transaction=conn.in_transaction,
            )
        )
        return decision

    def recording_store(conn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        role = str(args[2] if len(args) > 2 else kwargs.get("role") or "")
        if role == "assistant":
            assert conn.in_transaction
            content = str(args[3] if len(args) > 3 else kwargs.get("content") or "")
            observed.assistant_stores.append(
                SimpleNamespace(
                    conn=conn,
                    content=content,
                    in_transaction=conn.in_transaction,
                    reauthorization_count=len(observed.reauthorized),
                )
            )
        return original_store(conn, *args, **kwargs)

    monkeypatch.setattr(
        MessageExactInternalAdapter,
        "prepare_in_transaction",
        recording_prepare,
    )
    monkeypatch.setattr(MessageExactInternalAdapter, "project_for_model", recording_project)
    monkeypatch.setattr(
        MessageExactInternalAdapter,
        "reauthorize_for_publication_in_transaction",
        recording_reauthorize,
    )
    monkeypatch.setattr(agent_runtime, "store_message_in_transaction", recording_store)
    return observed


def _record_legacy_message_search(runtime: AgentRuntime) -> list[SimpleNamespace]:
    calls: list[SimpleNamespace] = []
    original_execute = runtime.kernel.execute

    async def recording_execute(
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ):  # noqa: ANN202 - retain ToolResult from the real kernel
        if name == "message_search":
            calls.append(
                SimpleNamespace(
                    arguments=dict(arguments),
                    execution_scope=execution_scope,
                )
            )
        return await original_execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )

    runtime.kernel.execute = recording_execute  # type: ignore[method-assign]
    return calls


def _post_authenticated_window(
    client: TestClient,
    settings: Any,
    conversation_id: str,
    *,
    prompt: str = _PROMPT,
    source_ref: str,
) -> dict[str, Any]:
    response = client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "message": prompt,
            "conversation_id": conversation_id,
            "source_ref": source_ref,
            "enable_tools": True,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _current_user_row(storage: Any, conversation_id: str, prompt: str) -> dict[str, Any]:
    rows = storage.get_conversation_messages(
        conversation_id,
        user_id=LEGACY_OWNER_USER_ID,
        limit=100,
    )
    matches = [
        row
        for row in rows
        if row.get("role") == "user" and row.get("content") == prompt
    ]
    assert len(matches) == 1
    return matches[0]


def _projection_payload(model: _ProjectionModel) -> tuple[str, dict[str, Any]]:
    assert len(model.main_messages) == 1
    blocks = [
        str(item.get("content") or "")
        for item in model.main_messages[0]
        if str(item.get("content") or "").startswith(
            "FRIDAY_UNTRUSTED_MESSAGE_HISTORY_DATA\n"
        )
    ]
    assert len(blocks) == 1
    encoded = blocks[0].split("\n", 1)[1]
    payload = json.loads(encoded)
    assert isinstance(payload, dict)
    return encoded, payload


def _stored_assistant(storage: Any, response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    assistant = storage.get_message(
        str(response["message_id"]),
        LEGACY_OWNER_USER_ID,
    )
    assert assistant is not None and assistant["role"] == "assistant"
    metadata = json.loads(str(assistant["metadata_json"] or "{}"))
    assert isinstance(metadata, dict)
    return assistant, metadata


def test_authenticated_queryless_window_uses_durable_boundary_projection_and_one_late_gate(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    configured = replace(settings, verify_answers=False)
    observed = _observe_exact_lane(monkeypatch)
    app = create_app(configured)
    with TestClient(app) as client:
        conversation, target = _seed_window(
            app.state.storage,
            title="authenticated exact runtime success",
        )
        runtime = app.state.agent
        model = _ProjectionModel()
        runtime.llm = model
        monkeypatch.setattr(runtime, "_local_now", lambda: datetime(2026, 9, 3, 12, 0))
        legacy_calls = _record_legacy_message_search(runtime)

        response = _post_authenticated_window(
            client,
            configured,
            str(conversation["id"]),
            source_ref="message-exact-runtime-success",
        )
        current = _current_user_row(app.state.storage, str(conversation["id"]), _PROMPT)
        assistant, assistant_metadata = _stored_assistant(app.state.storage, response)

    assert response["message"].endswith(_MODEL_DRAFT)
    assert legacy_calls == [], "the exact queryless window fell through to the legacy kernel"
    assert len(observed.prepared) == len(observed.projected) == 1
    prepared = observed.prepared[0]
    projected = observed.projected[0]
    assert prepared.page is projected.page
    assert prepared.request.accepted_boundary_user_message_id == current["id"]
    assert prepared.request.conversation_id == conversation["id"]
    assert prepared.request.content_mode is MessageExactContentMode.FULL_CONTENT
    assert not hasattr(prepared.request, "query")
    assert prepared.context.authority.conversation_id == conversation["id"]
    assert prepared.context.authority.person_id == LEGACY_OWNER_USER_ID

    user_metadata = json.loads(str(current["metadata_json"]))
    publication = user_metadata["authenticated_turn_publication"]
    assert publication == {
        "schema": "friday.authenticated-turn-publication.v1",
        "turn_id": prepared.context.turn_id,
        "context_authority_sha256": prepared.context.context_authority_sha256,
        "request_effect_binding_sha256": (
            prepared.context.effect_fence.request_effect_binding_sha256
        ),
        "publication_role": "user",
    }

    encoded_projection, model_payload = _projection_payload(model)
    projection = projected.projection
    expected_rows = [
        {
            "index": index,
            "at": row.at,
            "role": row.role.value,
            "text": row.text,
        }
        for index, row in enumerate(projection.rows, 1)
    ]
    assert model_payload == {
        "schema": "friday.untrusted-message-history.v1",
        "complete": True,
        "total": len(expected_rows),
        "timezone": "UTC",
        "rows": expected_rows,
    }
    assert expected_rows == [
        {
            "index": 1,
            "at": "2026-09-01T08:00:00+00:00",
            "role": "assistant",
            "text": target["content"],
        }
    ]
    assert all(len(row["text"]) <= MESSAGE_EXACT_MAX_FULL_ROW_CHARS for row in expected_rows)
    assert sum(len(row["text"]) for row in expected_rows) <= MESSAGE_EXACT_MAX_FULL_PAGE_CHARS

    model_serialized = json.dumps(model.main_messages[0], ensure_ascii=False, sort_keys=True)
    page = prepared.page
    private_values = (
        str(target["id"]),
        str(current["id"]),
        str(conversation["id"]),
        LEGACY_OWNER_USER_ID,
        _METADATA_CANARY,
        page.authority_handle,
        page.snapshot_handle,
        page.selection_handle,
        page.next_continuation.token if page.next_continuation is not None else "",
    )
    for private in private_values:
        if private:
            assert private not in model_serialized
            assert private not in encoded_projection

    assert len(observed.reauthorized) == len(observed.assistant_stores) == 1
    late = observed.reauthorized[0]
    assistant_store = observed.assistant_stores[0]
    assert late.page is page
    assert late.context is prepared.context
    assert late.decision.status is MessageExactPublicationStatus.AUTHORIZED
    assert late.decision.authorizes(page) is True
    assert assistant_store.reauthorization_count == 1
    assert assistant_store.conn is late.conn
    assert late.in_transaction is assistant_store.in_transaction is True
    assert assistant["content"] == response["message"]
    durable = json.dumps(
        {"content": assistant["content"], "metadata": assistant_metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    for private in private_values:
        if private:
            assert private not in durable


@pytest.mark.parametrize(
    ("change", "expected_status"),
    (
        ("revoke", MessageExactPublicationStatus.DENIED),
        ("drift", MessageExactPublicationStatus.DRIFTED),
    ),
)
def test_revoke_or_drift_after_model_yields_one_uniform_source_free_denial(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected_status: MessageExactPublicationStatus,
) -> None:
    from friday.server import create_app

    configured = replace(settings, verify_answers=False)
    observed = _observe_exact_lane(monkeypatch)
    app = create_app(configured)
    with TestClient(app) as client:
        conversation, target = _seed_window(
            app.state.storage,
            title=f"exact runtime late {change}",
        )

        def mutate() -> None:
            if change == "revoke":
                app.state.storage.set_permission_override(
                    LEGACY_OWNER_USER_ID,
                    "conversations.read",
                    "deny",
                )
            else:
                _set_created_at(
                    app.state.storage,
                    str(target["id"]),
                    "2026-09-02T08:00:00+00:00",
                )

        runtime = app.state.agent
        model = _ProjectionModel(mutate)
        runtime.llm = model
        monkeypatch.setattr(runtime, "_local_now", lambda: datetime(2026, 9, 3, 12, 0))
        legacy_calls = _record_legacy_message_search(runtime)

        response = _post_authenticated_window(
            client,
            configured,
            str(conversation["id"]),
            source_ref=f"message-exact-runtime-{change}",
        )
        assistant, metadata = _stored_assistant(app.state.storage, response)

    assert legacy_calls == []
    assert len(model.main_messages) == 1
    assert len(observed.prepared) == len(observed.projected) == 1
    assert len(observed.reauthorized) == len(observed.assistant_stores) == 1
    late = observed.reauthorized[0]
    assert late.page is observed.prepared[0].page
    assert late.decision.status is expected_status
    assert late.decision.authorizes(late.page) is False
    assistant_store = observed.assistant_stores[0]
    assert assistant_store.reauthorization_count == 1
    assert assistant_store.conn is late.conn
    assert late.in_transaction is assistant_store.in_transaction is True

    assert response["message"] == _PUBLICATION_DENIAL
    assert assistant["content"] == _PUBLICATION_DENIAL
    assert response.get("tool_evidence") in (None, [])
    assert response.get("citations") in (None, [])
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    for private in (
        _SOURCE_CANARY,
        _METADATA_CANARY,
        _MODEL_DRAFT,
        str(target["id"]),
        observed.prepared[0].page.selection_handle,
    ):
        assert private not in public
        assert private not in durable


def test_authenticated_window_without_adapter_preserves_legacy_kernel_route(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    configured = replace(settings, verify_answers=False)
    observed = _observe_exact_lane(monkeypatch)
    app = create_app(configured)
    with TestClient(app) as client:
        conversation, _target = _seed_window(
            app.state.storage,
            title="authenticated legacy without exact adapter",
        )
        runtime = app.state.agent
        runtime._message_exact_adapter = None  # noqa: SLF001 - explicit compatibility seam
        model = _ProjectionModel()
        runtime.llm = model
        monkeypatch.setattr(runtime, "_local_now", lambda: datetime(2026, 9, 3, 12, 0))
        legacy_calls = _record_legacy_message_search(runtime)

        response = _post_authenticated_window(
            client,
            configured,
            str(conversation["id"]),
            source_ref="message-exact-runtime-no-adapter",
        )
        current = _current_user_row(app.state.storage, str(conversation["id"]), _PROMPT)

    assert response["message"].endswith(_MODEL_DRAFT)
    assert len(model.main_messages) == 1
    assert len(legacy_calls) == 1
    assert legacy_calls[0].execution_scope == "internal"
    assert legacy_calls[0].arguments["query"] == ""
    assert legacy_calls[0].arguments["before_message_id"] == current["id"]
    assert json.loads(str(current["metadata_json"]))["authenticated_turn_publication"][
        "publication_role"
    ] == "user"
    assert observed.prepared == []
    assert observed.projected == []
    assert observed.reauthorized == []


@pytest.mark.asyncio
async def test_window_without_authenticated_context_preserves_legacy_kernel_route(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    conversation, _target = _seed_window(
        storage,
        title="legacy exact adapter without authenticated context",
    )
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, configured)
    kernel.bind_services(
        storage,
        graph,
        WebSurfer(configured),
        IngestionPipeline(configured, storage, graph),
    )
    adapter = MessageExactInternalAdapter(
        authorization,
        TurnContextIssuer(hashlib.sha256(b"message-exact-runtime-no-context").digest()),
    )
    model = _ProjectionModel()
    observed = _observe_exact_lane(monkeypatch)
    runtime = AgentRuntime(
        configured,
        storage,
        llm=model,
        kernel=kernel,
        message_exact_adapter=adapter,
    )
    monkeypatch.setattr(runtime, "_local_now", lambda: datetime(2026, 9, 3, 12, 0))
    legacy_calls = _record_legacy_message_search(runtime)

    response = await runtime.chat(
        LEGACY_OWNER_USER_ID,
        _PROMPT,
        actor=authorization.actor_for_user(LEGACY_OWNER_USER_ID, source="test"),
        conversation_id=str(conversation["id"]),
        enable_tools=True,
    )
    current = _current_user_row(storage, str(conversation["id"]), _PROMPT)

    assert response["message"].endswith(_MODEL_DRAFT)
    assert len(model.main_messages) == 1
    assert len(legacy_calls) == 1
    assert legacy_calls[0].execution_scope == "internal"
    assert legacy_calls[0].arguments["before_message_id"] == current["id"]
    assert "authenticated_turn_publication" not in json.loads(str(current["metadata_json"]))
    assert observed.prepared == []
    assert observed.projected == []
    assert observed.reauthorized == []


def test_authenticated_topical_query_never_enters_queryless_exact_lane(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    configured = replace(settings, verify_answers=False)
    observed = _observe_exact_lane(monkeypatch)
    app = create_app(configured)
    with TestClient(app) as client:
        conversation, target = _seed_window(
            app.state.storage,
            title="authenticated topical message search",
        )
        topical = app.state.storage.store_message(
            str(conversation["id"]),
            LEGACY_OWNER_USER_ID,
            "user",
            "ORIONMARKER belongs only to the topical legacy search.",
        )
        runtime = app.state.agent
        runtime.llm = _ProjectionModel()
        monkeypatch.setattr(runtime, "_local_now", lambda: datetime(2026, 9, 3, 12, 0))
        legacy_calls = _record_legacy_message_search(runtime)

        response = _post_authenticated_window(
            client,
            configured,
            str(conversation["id"]),
            prompt=_TOPICAL_PROMPT,
            source_ref="message-exact-runtime-topical",
        )
        current = _current_user_row(
            app.state.storage,
            str(conversation["id"]),
            _TOPICAL_PROMPT,
        )

    assert len(legacy_calls) == 1
    call = legacy_calls[0]
    assert call.execution_scope == "internal"
    assert call.arguments == {
        "query": "ORIONMARKER",
        "limit": 20,
        "before_message_id": current["id"],
        "match_all_terms": True,
    }
    assert "ORIONMARKER" in response["message"]
    assert str(topical["id"]) != str(target["id"])
    assert observed.prepared == []
    assert observed.projected == []
    assert observed.reauthorized == []
