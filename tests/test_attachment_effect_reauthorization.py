"""Private file authority is rechecked immediately before irreversible tools.

The model and every source are synthetic.  Mutations happen only after the
registered file projection has reached the model (or, for the code-owned exact
workspace route, after its source snapshot has been built).  No network or live
service is used.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer

_OWNER = "attachment-effect-owner"
_SOURCE_CANARY = "PRIVATE-ATTACHMENT-EFFECT-CANARY-814"
_MUTATOR_CANARY = "MUTATOR-PAYLOAD-ATTACHMENT-CANARY-814"
_WORKSPACE_CANARY = "WORKSPACE-PRE-EFFECT-CANARY-814"
_WORKSPACE_PATH = "private/effect-authority.txt"
_LINEAGE_SOURCE_CANARY = "SOURCE-SEARCH-LINEAGE-FAILURE-CANARY-814"
_WEB_PREFETCH_QUERY_CANARY = "PRIVATE-WEB-PREFETCH-QUERY-CANARY-814"
_ATTACHMENT_CHUNK_PREFIX = "FRIDAY_ATTACHMENT_CHUNK_DATA"
_ATTACHMENT_REDUCE_PREFIX = "FRIDAY_ATTACHMENT_REDUCE_DATA"
_EXACT_WORKSPACE_PROMPT = (
    "Используй именно workspace_create и создай в MCP outbox файл effect-exact.txt. "
    "Первая строка — только значение номера документа без подписи. Вторая строка — "
    "только значение контрольного маркера без подписи. Никаких других строк."
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


class _RecordingEffectKernel(ExecutionKernel):
    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        self.executed.append((name, dict(arguments)))
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _OfflineWebPrefetchKernel(ExecutionKernel):
    """Record deterministic web entry without touching a public provider."""

    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.web_research_calls: list[dict[str, Any]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        if name == "web_research":
            self.web_research_calls.append(dict(arguments))
            public_text = "Synthetic public result which must remain unreachable after revocation."
            return ToolResult(
                name,
                True,
                data={
                    "outbound_attempted": True,
                    "sources": [
                        {
                            "url": "https://public.invalid/prefetch-reauth",
                            "title": "Synthetic public source",
                            "text": public_text,
                            "text_length": len(public_text),
                            "status_code": 200,
                            "error": "",
                            "truncated": False,
                        }
                    ],
                    "requested_sources": 1,
                    "completed_sources": 1,
                    "failed_sources": 0,
                    "timed_out_sources": 0,
                    "search_timed_out": False,
                },
            )
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _LateMutatingEffectModel:
    enabled = True
    model = "synthetic-late-mutating-attachment-effect-model"
    total_budget_sec = 10.0

    def __init__(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        mutate: Callable[[], None],
    ) -> None:
        self.tool_name = tool_name
        self.arguments = dict(arguments)
        self.mutate = mutate
        self.mutated = False
        self.prepass_calls = 0
        self.primary_calls = 0
        self.post_tool_calls = 0
        self.unclassified_calls: list[dict[str, Any]] = []

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
        serialized = json.dumps(snapshot, ensure_ascii=False)
        if _ATTACHMENT_CHUNK_PREFIX in serialized or _ATTACHMENT_REDUCE_PREFIX in serialized:
            assert _SOURCE_CANARY in serialized
            assert not self.mutated
            self.prepass_calls += 1
            return {
                "content": f"Сводка зарегистрированного источника: {_SOURCE_CANARY}.",
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        if "Ответь одним словом: РАЗГОВОР или ЗАПРОС." in system_text:
            return {"content": "ЗАПРОС", "tool_calls": None, "_queue_wait_sec": 0.0}
        if "Классифицируй ТОЛЬКО чтение личной ленты/календаря" in system_text:
            return {
                "content": '{"direction":"none","window_kind":"none"}',
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        if "Реши, что от тебя хотят" in system_text:
            return {
                "content": '{"вид":"действие","запрос":"","кто":"","дни":[]}',
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        if "Часть просьбы человека уже решена без тебя" in system_text:
            return {
                "content": '{"остаток":""}',
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }

        if any(str(item.get("role") or "") == "tool" for item in snapshot):
            self.post_tool_calls += 1
            return {
                "content": "Не удалось сохранить данные: источник больше недоступен.",
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }

        assert _SOURCE_CANARY in serialized, "mutation ran before the registered source projection"
        offered = {
            str((item.get("function") or {}).get("name") or item.get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        self.unclassified_calls.append(
            {
                "has_source_canary": _SOURCE_CANARY in serialized,
                "offered": sorted(offered),
                "roles": [str(item.get("role") or "") for item in snapshot],
                "system_tail": system_text[-500:],
            }
        )
        assert self.tool_name in offered
        assert not self.mutated
        self.primary_calls += 1
        self.mutate()
        self.mutated = True
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-late-{self.tool_name}",
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "arguments": json.dumps(self.arguments, ensure_ascii=False),
                    },
                }
            ],
            "_queue_wait_sec": 0.0,
        }


class _LateRevokingWebPrefetchModel:
    """Revoke the projected Raw while deterministic web arbitration is in flight."""

    enabled = True
    model = "synthetic-late-revoking-deterministic-web-prefetch-model"
    total_budget_sec = 10.0

    def __init__(self, mutate: Callable[[], None]) -> None:
        self.mutate = mutate
        self.mutated = False
        self.prepass_calls = 0
        self.arbiter_calls = 0
        self.primary_calls = 0
        self.arbiter_serialized = ""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del tools, kwargs
        snapshot = copy.deepcopy(messages)
        serialized = json.dumps(snapshot, ensure_ascii=False)
        system_text = "\n".join(
            str(item.get("content") or "") for item in snapshot if str(item.get("role") or "") == "system"
        )
        if _ATTACHMENT_CHUNK_PREFIX in serialized or _ATTACHMENT_REDUCE_PREFIX in serialized:
            assert _SOURCE_CANARY in serialized
            assert not self.mutated
            self.prepass_calls += 1
            return {
                "content": f"Сводка зарегистрированного источника: {_SOURCE_CANARY}.",
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        if "Реши, что от тебя хотят" in system_text:
            assert self.arbiter_calls == 0
            assert not self.mutated
            self.arbiter_serialized = serialized
            assert "свежие сведения" in serialized
            # The classifier receives the person's current request, not private
            # source bytes.  Raw authority is nevertheless already frozen in the
            # full turn and must be rechecked after this await, before disclosure.
            assert _SOURCE_CANARY not in serialized
            self.arbiter_calls += 1
            self.mutate()
            self.mutated = True
            return {
                "content": json.dumps(
                    {
                        "вид": "интернет",
                        "запрос": _WEB_PREFETCH_QUERY_CANARY,
                        "кто": "",
                        "дни": [],
                    },
                    ensure_ascii=False,
                ),
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }

        assert self.mutated, "primary synthesis ran before the web arbiter mutation"
        self.primary_calls += 1
        return {
            # Deliberately unsafe draft: final publication reauthorization must
            # independently suppress everything derived from the revoked Raw.
            "content": f"{_SOURCE_CANARY} {_WEB_PREFETCH_QUERY_CANARY}",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _ForbiddenModel:
    enabled = True
    model = "forbidden-exact-workspace-effect-model"
    total_budget_sec = 10.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.calls += 1
        raise AssertionError("exact workspace projection must not call the model")


class _RevokingWorkspaceKernel:
    def __init__(
        self,
        authorization: AuthorizationService,
        mutate: Callable[[], None],
    ) -> None:
        self.authorization = authorization
        self.mutate = mutate
        self.mutated = False
        self.executed: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def get_tool(name: str) -> Any:
        if name == "workspace_create":
            return SimpleNamespace(risk="mutate", security_id="mcp.files.create")
        return None

    def get_tool_definitions(
        self,
        actor: Any,
        *,
        topic: str = "",
        execution_scope: str = "dialogue",
    ) -> list[dict[str, Any]]:
        del actor, topic, execution_scope
        if not self.mutated:
            self.mutate()
            self.mutated = True
        return [
            {
                "type": "function",
                "function": {
                    "name": "workspace_create",
                    "description": "synthetic external outbox create",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["filename", "content"],
                    },
                },
            }
        ]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
    ) -> ToolResult:
        del actor
        self.executed.append((name, dict(arguments)))
        return ToolResult(name, True, data={"created": True, "filename": arguments.get("filename")})


class _MutableWorkspaceEffectKernel(ExecutionKernel):
    """Synthetic MCP provider whose source identity changes after synthesis."""

    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.changed = False
        self.provider_calls: list[tuple[str, dict[str, Any]]] = []
        self.effect_calls: list[tuple[str, dict[str, Any]]] = []
        self.source_text = f"Приватный raw-less источник MCP.\nКонтрольный маркер: {_SOURCE_CANARY}.\n"

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: Any = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        del execution_scope
        if name == "workspace_read":
            self.provider_calls.append((name, dict(arguments)))
            assert actor is not None and actor.own_id == _OWNER
            assert arguments == {"relative_path": _WORKSPACE_PATH, "offset": 0}
            return ToolResult(
                name,
                True,
                data={
                    "scope": "workspace_inbox",
                    "path": _WORKSPACE_PATH,
                    "filename": _WORKSPACE_PATH.rsplit("/", 1)[-1],
                    "mime_type": "text/plain",
                    "size_bytes": len(self.source_text.encode()),
                    "sha256": ("d" if self.changed else "a") * 64,
                    "source_sha256": ("e" if self.changed else "b") * 64,
                    "readable": True,
                    "source_complete": True,
                    "advisory_only": False,
                    "verification_eligible": True,
                    "unsupported_format": False,
                    "text": self.source_text,
                    "offset": 0,
                    "next_offset": None,
                    "text_chars": len(self.source_text),
                    "projection_complete": True,
                },
            )
        self.effect_calls.append((name, dict(arguments)))
        # No external provider or durable mutation is allowed even if the
        # production guard regresses; the recorded entry is the test failure.
        return ToolResult(name, True, data={"synthetic_target_entered": True})


class _LineagePersistenceFailureModel:
    enabled = True
    model = "synthetic-source-lineage-persistence-failure-model"
    total_budget_sec = 10.0

    def __init__(self) -> None:
        self.primary_calls = 0
        self.second_model_serialized = ""
        self.second_offered_names: set[str] = set()

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
        if "Никаких пояснений, только JSON." in system_text or "Реши, что от тебя хотят" in system_text:
            return {
                "content": '{"вид":"архив","запрос":"","кто":"","дни":[]}',
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
            self.second_model_serialized = serialized
            self.second_offered_names = offered
            assert _LINEAGE_SOURCE_CANARY not in serialized
            assert offered == set()
            return {
                "content": (
                    "Не удалось безопасно закрепить приватный контекст локального "
                    "источника; найденные данные не использованы."
                ),
                "tool_calls": None,
                "finish_reason": "stop",
                "_queue_wait_sec": 0.0,
            }

        assert _LINEAGE_SOURCE_CANARY not in serialized
        assert "source_search" in offered
        self.primary_calls += 1
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-source-lineage-failure",
                    "type": "function",
                    "function": {
                        "name": "source_search",
                        "arguments": json.dumps(
                            {
                                "query": "ORION-LINEAGE",
                                "focus": "ORION-LINEAGE marker",
                                "limit": 10,
                            }
                        ),
                    },
                }
            ],
            "_queue_wait_sec": 0.0,
        }


async def _ingest_registered(
    settings: Any,
    storage: Any,
    *,
    body: str,
    filename: str,
) -> str:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_file(
        _OWNER,
        None,
        body.encode(),
        filename=filename,
        mime_type="text/plain",
        metadata={"uploaded_by": _OWNER},
        source_ref=f"telegram-file:{filename}",
    )
    return str(result["raw_object_id"])


def _soft_delete(storage: Any, raw_id: str) -> None:
    with storage.transaction() as connection:
        cursor = connection.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-14T00:00:00Z' WHERE id=?",
            (raw_id,),
        )
    assert cursor.rowcount == 1


def _change_raw_identity(storage: Any, raw_id: str) -> None:
    with storage.transaction() as connection:
        cursor = connection.execute(
            "UPDATE raw_objects SET raw_content=? WHERE id=?",
            ("CHANGED-AFTER-SOURCE-PROJECTION", raw_id),
        )
    assert cursor.rowcount == 1


def _effect_candidate_ids(storage: Any) -> list[str]:
    rows = storage.execute(
        "SELECT id, metadata_json FROM raw_objects WHERE user_id=?",
        (_OWNER,),
    ).fetchall()
    found: list[str] = []
    for row in rows:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        if isinstance(metadata, dict) and metadata.get("tool") in {"memory_save", "entity_create"}:
            found.append(str(row["id"]))
    return found


def _stored_assistant(storage: Any, response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    row = storage.get_message(str(response["message_id"]), _OWNER)
    assert row is not None and row["role"] == "assistant"
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    assert isinstance(metadata, dict)
    return row, metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "mutation", "user_request", "arguments"),
    [
        pytest.param(
            "memory_save",
            "soft_delete",
            "Прочитай этот файл и запомни его содержание.",
            {
                "content": f"Сохранить из файла: {_SOURCE_CANARY}; {_MUTATOR_CANARY}",
                "title": "Синтетическая заметка из файла",
            },
            id="memory-save-soft-delete",
        ),
        pytest.param(
            "memory_save",
            "identity_change",
            "Прочитай этот файл и запомни его содержание.",
            {
                "content": f"Сохранить из файла: {_SOURCE_CANARY}; {_MUTATOR_CANARY}",
                "title": "Синтетическая заметка из файла",
            },
            id="memory-save-identity-change",
        ),
        pytest.param(
            "entity_create",
            "soft_delete",
            "Прочитай этот файл и создай сущность по содержащемуся в нём маркеру.",
            {
                "name": _MUTATOR_CANARY,
                "entity_type": "other",
                "description": f"Источник: {_SOURCE_CANARY}",
            },
            id="entity-create-soft-delete",
        ),
    ],
)
async def test_registered_attachment_is_reauthorized_before_source_derived_mutator(
    settings: Any,
    storage: Any,
    tool_name: str,
    mutation: str,
    user_request: str,
    arguments: dict[str, Any],
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    raw_id = await _ingest_registered(
        configured,
        storage,
        body=(
            "Зарегистрированный частный источник.\n"
            f"Контрольный маркер: {_SOURCE_CANARY}.\n"
            "Эта строка предназначена только для текущего файлового запроса."
        ),
        filename=f"effect-{tool_name}-{mutation}.txt",
    )

    def mutate() -> None:
        if mutation == "soft_delete":
            _soft_delete(storage, raw_id)
        else:
            _change_raw_identity(storage, raw_id)

    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(configured, storage, graph)
    kernel = _RecordingEffectKernel(authorization, configured)
    kernel.bind_services(storage, graph, WebSurfer(configured), pipeline)
    model = _LateMutatingEffectModel(
        tool_name=tool_name,
        arguments=arguments,
        mutate=mutate,
    )
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)
    actor = authorization.actor_for_user(_OWNER, source="synthetic-test")
    assert _effect_candidate_ids(storage) == []

    response = await runtime.chat(
        _OWNER,
        user_request,
        actor=actor,
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.primary_calls == 1, model.unclassified_calls
    assert model.mutated is True
    assert all(name != tool_name for name, _ in kernel.executed)
    assert _effect_candidate_ids(storage) == []
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _SOURCE_CANARY not in public
    assert _MUTATOR_CANARY not in public
    assert response["verified"] is False
    assert response["verification_status"] == "unknown"
    assert response["files"] == []
    assert response["voice"] is None
    assert any(
        marker in str(response["message"]).casefold()
        for marker in ("источник", "недоступ", "измен", "не удалось")
    )
    assistant, metadata = _stored_assistant(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _SOURCE_CANARY not in durable
    assert _MUTATOR_CANARY not in durable


@pytest.mark.asyncio
async def test_registered_attachment_is_reauthorized_before_deterministic_web_prefetch(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    raw_id = await _ingest_registered(
        configured,
        storage,
        body=(
            "Зарегистрированный частный источник для интернет-проверки.\n"
            f"Контрольный маркер: {_SOURCE_CANARY}.\n"
            "Тема документа не должна покинуть процесс после отзыва источника."
        ),
        filename="deterministic-web-prefetch-source.txt",
    )
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(configured, storage, graph)
    kernel = _OfflineWebPrefetchKernel(authorization, configured)
    kernel.bind_services(storage, graph, WebSurfer(configured), pipeline)
    model = _LateRevokingWebPrefetchModel(lambda: _soft_delete(storage, raw_id))
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)

    response = await runtime.chat(
        _OWNER,
        "Прочитай этот файл и найди в интернете свежие сведения по теме.",
        actor=authorization.actor_for_user(_OWNER, source="synthetic-test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.arbiter_calls == 1
    assert model.arbiter_serialized
    assert model.mutated is True
    assert kernel.web_research_calls == [], (
        "deterministic prefetch entered web_research after its registered Raw was revoked"
    )
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _SOURCE_CANARY not in public
    assert _WEB_PREFETCH_QUERY_CANARY not in public
    assert response["verified"] is False
    assert response["verification_status"] == "unknown"
    assert response["files"] == []
    assert response["voice"] is None
    assert response["attachment_authority_changed_before_publication"] is True
    assert "источник стал недоступен или изменился" in str(response["message"]).casefold()
    assistant, metadata = _stored_assistant(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _SOURCE_CANARY not in durable
    assert _WEB_PREFETCH_QUERY_CANARY not in durable


@pytest.mark.asyncio
async def test_exact_workspace_create_reauthorizes_registered_source_before_kernel_entry(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    raw_id = await _ingest_registered(
        configured,
        storage,
        body=(
            f"{_WORKSPACE_CANARY}\nНомер документа: DOC-EFFECT-42\nКонтрольный маркер: CONTROL-EFFECT-814\n"
        ),
        filename="workspace-effect-source.txt",
    )
    authorization = AuthorizationService(storage)
    kernel = _RevokingWorkspaceKernel(
        authorization,
        lambda: _soft_delete(storage, raw_id),
    )
    model = _ForbiddenModel()
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]

    response = await runtime.chat(
        _OWNER,
        _EXACT_WORKSPACE_PROMPT,
        actor=authorization.actor_for_user(_OWNER, source="synthetic-test"),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert kernel.mutated is True
    assert kernel.executed == []
    assert model.calls == 0
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _WORKSPACE_CANARY not in public
    assert "DOC-EFFECT-42" not in public
    assert "CONTROL-EFFECT-814" not in public
    assert response["files"] == []
    assert response["voice"] is None
    message = str(response["message"]).casefold()
    assert "источник" in message
    assert "не опубликован" in message
    assistant, metadata = _stored_assistant(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _WORKSPACE_CANARY not in durable
    assert "DOC-EFFECT-42" not in durable
    assert "CONTROL-EFFECT-814" not in durable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "user_request", "arguments"),
    [
        pytest.param(
            "memory_save",
            "Прочитай этот файл и запомни его содержание.",
            {
                "content": f"MCP source: {_SOURCE_CANARY}; {_MUTATOR_CANARY}",
                "title": "Synthetic MCP source memory",
            },
            id="memory-save",
        ),
        pytest.param(
            "web_search",
            "Прочитай этот файл и проверь контрольный маркер в интернете.",
            {"query": f"{_SOURCE_CANARY} {_MUTATOR_CANARY}"},
            id="web-search",
        ),
    ],
)
async def test_rawless_workspace_source_identity_is_rechecked_before_model_selected_effect(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    user_request: str,
    arguments: dict[str, Any],
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    conversation = storage.create_conversation(_OWNER, title="synthetic MCP effect authority")
    storage.store_message(
        str(conversation["id"]),
        _OWNER,
        "assistant",
        "Ранее был прочитан приватный источник MCP.",
        metadata={
            "workspace_inbox_relative_path": _WORKSPACE_PATH,
            "workspace_inbox_sha256": "a" * 64,
            "workspace_inbox_source_sha256": "b" * 64,
            "attachment_context_used": True,
            "private_context_lineage": True,
        },
    )
    authorization = AuthorizationService(storage)
    kernel = _MutableWorkspaceEffectKernel(authorization, configured)
    graph = KnowledgeGraph(storage)
    kernel.bind_services(
        storage,
        graph,
        WebSurfer(configured),
        IngestionPipeline(configured, storage, graph),
    )
    model = _LateMutatingEffectModel(
        tool_name=tool_name,
        arguments=arguments,
        mutate=lambda: setattr(kernel, "changed", True),
    )
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)

    if tool_name == "web_search":

        async def no_deterministic_web_prefetch(*args: Any, **kwargs: Any) -> None:
            del args, kwargs

        monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", no_deterministic_web_prefetch)

    response = await runtime.chat(
        _OWNER,
        user_request,
        actor=authorization.actor_for_user(_OWNER, source="synthetic-test"),
        conversation_id=str(conversation["id"]),
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.primary_calls == 1, model.unclassified_calls
    assert model.mutated is True
    assert kernel.changed is True
    assert kernel.effect_calls == []
    assert all(name == "workspace_read" for name, _ in kernel.provider_calls)
    assert len(kernel.provider_calls) == 3, (
        "expected initial read, pre-effect reauthorization, and final-publication reread"
    )
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _SOURCE_CANARY not in public
    assert _MUTATOR_CANARY not in public
    assert response["verified"] is False
    assert response["verification_status"] == "unknown"
    assert response["files"] == []
    assert response["voice"] is None
    assert "источник" in str(response["message"]).casefold()
    assistant, metadata = _stored_assistant(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _SOURCE_CANARY not in durable
    assert _MUTATOR_CANARY not in durable
    assert "workspace_inbox_relative_path" not in metadata
    assert "workspace_inbox_sha256" not in metadata
    assert "workspace_inbox_source_sha256" not in metadata


@pytest.mark.asyncio
async def test_missing_active_source_evidence_still_requires_reauth_before_mutator(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    raw_id = await _ingest_registered(
        configured,
        storage,
        body=f"Private evidence-gap source.\nMarker: {_SOURCE_CANARY}.\n",
        filename="effect-missing-active-evidence.txt",
    )
    original_evidence_builder = agent_runtime_module._file_evidence_set_from_attachments
    evidence_calls = 0

    def omit_active_evidence_once(
        attachments: Any,
        *,
        expected_count: int,
    ) -> Any:
        nonlocal evidence_calls
        evidence_calls += 1
        if evidence_calls == 1:
            assert len(attachments) == 1
            assert expected_count == 1
            return None
        return original_evidence_builder(attachments, expected_count=expected_count)

    monkeypatch.setattr(
        agent_runtime_module,
        "_file_evidence_set_from_attachments",
        omit_active_evidence_once,
    )
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(configured, storage, graph)
    kernel = _RecordingEffectKernel(authorization, configured)
    kernel.bind_services(storage, graph, WebSurfer(configured), pipeline)
    model = _LateMutatingEffectModel(
        tool_name="memory_save",
        arguments={
            "content": f"Evidence gap: {_SOURCE_CANARY}; {_MUTATOR_CANARY}",
            "title": "Synthetic missing-authority memory",
        },
        mutate=lambda: None,
    )
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)

    response = await runtime.chat(
        _OWNER,
        "Прочитай этот файл и запомни его содержание.",
        actor=authorization.actor_for_user(_OWNER, source="synthetic-test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert evidence_calls >= 2
    assert model.primary_calls == 1, model.unclassified_calls
    assert all(name != "memory_save" for name, _ in kernel.executed)
    assert _effect_candidate_ids(storage) == []
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _SOURCE_CANARY not in public
    assert _MUTATOR_CANARY not in public
    assert response["verified"] is False
    assert response["verification_status"] == "unknown"
    assert response["files"] == []
    assert response["voice"] is None
    assistant, metadata = _stored_assistant(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _SOURCE_CANARY not in durable
    assert _MUTATOR_CANARY not in durable


@pytest.mark.asyncio
async def test_model_selected_source_search_lineage_persistence_failure_never_projects_private_row(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user(_OWNER, preset_key="owner")
    await _ingest_registered(
        configured,
        storage,
        body=(f"ORION-LINEAGE private source.\nPrivate marker: {_LINEAGE_SOURCE_CANARY}.\n"),
        filename="source-lineage-persistence-failure.txt",
    )
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(configured, storage, graph)
    kernel = _RecordingEffectKernel(authorization, configured)
    kernel.bind_services(storage, graph, WebSurfer(configured), pipeline)
    model = _LineagePersistenceFailureModel()
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)
    monkeypatch.setattr(runtime, "_persist_source_search_private_lineage", lambda context: False)

    response = await runtime.chat(
        _OWNER,
        "Расскажи про ORION-LINEAGE",
        actor=authorization.actor_for_user(_OWNER, source="synthetic-test"),
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
    )

    assert model.primary_calls == 1
    assert model.second_model_serialized
    assert _LINEAGE_SOURCE_CANARY not in model.second_model_serialized
    assert model.second_offered_names == set()
    assert kernel.executed == [
        (
            "source_search",
            {"query": "ORION-LINEAGE", "focus": "ORION-LINEAGE marker", "limit": 10},
        )
    ]
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _LINEAGE_SOURCE_CANARY not in public
    assert response["files"] == []
    assert response["voice"] is None
    assert "не удалось безопасно закрепить" in str(response["message"]).casefold()
    assistant, metadata = _stored_assistant(storage, response)
    durable = json.dumps(
        {"content": assistant["content"], "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert _LINEAGE_SOURCE_CANARY not in durable
    assert "source_search_result_raw_ids" not in metadata
