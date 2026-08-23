"""The final spoken carrier must reauthorize its private source before entry.

The written assistant row and the synthesized Telegram carrier are two distinct
publication/effect boundaries.  These tests mutate durable authority immediately
before final TTS or from inside an already-admitted synthetic ``speak`` handler.
No network, live service, real speech engine, or production database is used.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

_SOURCE_CANARY = "FINAL-TTS-SOURCE-AUTHORITY-CANARY-814"
_SOURCE_SEARCH_OWNER = "final-tts-source-search-owner"
_SOURCE_SEARCH_REQUEST = "Найди в ранее загруженном файле, какая должность указана у Синтетикова."


class _ReviewModel:
    enabled = True
    model = "final-tts-source-reauthorization-model"
    total_budget_sec = 3.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        prompt = "\n".join(str(item.get("content") or "") for item in messages)
        assert _SOURCE_CANARY in prompt
        self.calls += 1
        return {
            "content": f"Документ содержит строку {_SOURCE_CANARY}.",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _SpeakKernel(ExecutionKernel):
    """Minimal authorized kernel which makes provider entry directly observable."""

    def __init__(
        self,
        storage: Any,
        settings: Any,
        *,
        on_speak: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(AuthorizationService(storage), settings)
        self.storage = storage
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_speak = on_speak
        self._tools["speak"].handler = self._synthetic_speak  # noqa: SLF001

    async def _synthetic_speak(
        self,
        *,
        actor: ActorContext,
        text: str,
    ) -> dict[str, Any]:
        del actor
        self.calls.append(("speak", {"text": text}))
        if self.on_speak is not None:
            self.on_speak()
        return {
            "spoken": True,
            "_attachment": {
                "kind": "voice",
                "mime_type": "audio/ogg",
                "audio_base64": "c3ludGhldGljLW9nZw==",
            },
        }


async def _ingest(settings: Any, storage: Any) -> str:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_file(
        "alice",
        None,
        f"# Private source\n\n{_SOURCE_CANARY}\n".encode(),
        filename="final-tts-source.md",
        mime_type="text/markdown",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FINAL-TTS-SOURCE-REAUTH",
    )
    return str(result["raw_object_id"])


def _soft_delete_raw(storage: Any, raw_id: str) -> None:
    with storage.transaction() as conn:
        cursor = conn.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-14T00:00:00Z' WHERE id=?",
            (raw_id,),
        )
    assert cursor.rowcount == 1


def _stored_assistant(storage: Any, response: dict[str, Any], owner: str = "alice") -> dict[str, Any]:
    row = storage.get_message(str(response["message_id"]), owner)
    assert row is not None and row["role"] == "assistant"
    return row


def _assert_attachment_source_failed_closed(storage: Any, response: dict[str, Any]) -> None:
    assert _SOURCE_CANARY not in json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert response["attachment_authority_changed_before_publication"] is True
    assert response["voice"] is None
    stored = _stored_assistant(storage, response)
    assert _SOURCE_CANARY not in str(stored["content"])


def _speak_audit_reasons(storage: Any, owner: str = "alice") -> list[str]:
    rows = storage.execute(
        """SELECT after_json FROM audit_log
             WHERE user_id=? AND action='tool.invoke' AND target_id='speak'
             ORDER BY rowid""",
        (owner,),
    ).fetchall()
    return [str(json.loads(str(row["after_json"] or "{}")).get("reason") or "") for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "raw_soft_deleted",
        "files_read_denied",
        "principal_disabled",
        "principal_preset_downgraded",
    ],
)
async def test_final_tts_reauthorizes_file_source_immediately_before_synthesis(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _ingest(configured, storage)
    model = _ReviewModel()
    kernel = _SpeakKernel(storage, configured)
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]

    original_voice = runtime._voice_of_the_final_answer  # noqa: SLF001

    async def revoke_before_synthesis(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if mutation == "raw_soft_deleted":
            _soft_delete_raw(storage, raw_id)
        elif mutation == "files_read_denied":
            storage.set_permission_override("alice", "files.read", "deny")
        elif mutation == "principal_disabled":
            assert storage.update_user("alice", status="disabled") is not None
        elif mutation == "principal_preset_downgraded":
            assert storage.update_user("alice", preset_key="guest") is not None
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(mutation)
        return await original_voice(*args, **kwargs)

    monkeypatch.setattr(runtime, "_voice_of_the_final_answer", revoke_before_synthesis)
    result = await runtime.chat(
        "alice",
        "Загружен документ: final-tts-source.md",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=False,
        synthetic_document_notice=True,
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert kernel.calls == [], "a revoked private source reached the mutating TTS handler"
    _assert_attachment_source_failed_closed(storage, result)


@pytest.mark.asyncio
async def test_tts_capability_revoked_before_synthesis_drops_only_voice(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _ingest(configured, storage)
    model = _ReviewModel()
    kernel = _SpeakKernel(storage, configured)
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]
    original_voice = runtime._voice_of_the_final_answer  # noqa: SLF001

    async def revoke_tts_then_synthesize(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        storage.set_permission_override("alice", "tts.use", "deny")
        return await original_voice(*args, **kwargs)

    monkeypatch.setattr(runtime, "_voice_of_the_final_answer", revoke_tts_then_synthesize)
    result = await runtime.chat(
        "alice",
        "Загружен документ: final-tts-source.md",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=False,
        synthetic_document_notice=True,
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert kernel.calls == []
    assert result["voice"] is None
    assert result["attachment_authority_changed_before_publication"] is False
    assert _SOURCE_CANARY in result["message"]
    assert _stored_assistant(storage, result)["content"] == result["message"]


@pytest.mark.asyncio
async def test_file_source_revoked_during_admitted_speak_drops_whole_answer_but_keeps_entry_ledger(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _ingest(configured, storage)
    model = _ReviewModel()
    kernel = _SpeakKernel(
        storage,
        configured,
        on_speak=lambda: _soft_delete_raw(storage, raw_id),
    )
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: final-tts-source.md",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=False,
        synthetic_document_notice=True,
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert [name for name, _ in kernel.calls] == ["speak"]
    assert _speak_audit_reasons(storage) == ["started", "ok"]
    _assert_attachment_source_failed_closed(storage, result)


@pytest.mark.asyncio
async def test_tts_capability_revoked_during_admitted_speak_keeps_text_but_drops_voice(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _ingest(configured, storage)
    model = _ReviewModel()
    kernel = _SpeakKernel(
        storage,
        configured,
        on_speak=lambda: storage.set_permission_override("alice", "tts.use", "deny"),
    )
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: final-tts-source.md",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=False,
        synthetic_document_notice=True,
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert [name for name, _ in kernel.calls] == ["speak"]
    assert _speak_audit_reasons(storage) == ["started", "ok"]
    assert result["voice"] is None
    assert result["attachment_authority_changed_before_publication"] is False
    assert _SOURCE_CANARY in result["message"]
    assert _stored_assistant(storage, result)["content"] == result["message"]


@pytest.mark.asyncio
async def test_unchanged_file_source_synthesizes_once_and_publishes_one_model_answer(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _ingest(configured, storage)
    model = _ReviewModel()
    kernel = _SpeakKernel(storage, configured)
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: final-tts-source.md",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=False,
        synthetic_document_notice=True,
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert [name for name, _ in kernel.calls] == ["speak"]
    assert result["voice"]["audio_base64"] == "c3ludGhldGljLW9nZw=="
    assert _SOURCE_CANARY in result["message"]
    assert _stored_assistant(storage, result)["content"] == result["message"]


class _EmptySearcher:
    async def search(self, user_id: str, query: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, query, kwargs
        return {
            "results": [],
            "entity_matches": [],
            "graph_context": {},
            "matched_at_least": 0,
        }


class _SourceSearchModel:
    enabled = True
    model = "final-tts-source-search-model"
    total_budget_sec = 3.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        serialized = json.dumps(messages, ensure_ascii=False)
        assert "FRIDAY_SOURCE_SEARCH_DATA" in serialized
        assert _SOURCE_CANARY in serialized
        self.calls += 1
        return {
            "content": f"В источнике указана должность: инженер ({_SOURCE_CANARY}).",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _SourceSearchKernel(ExecutionKernel):
    def __init__(
        self,
        authorization: AuthorizationService,
        settings: Any,
        *,
        on_speak: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(authorization, settings)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.speak_entries = 0
        self.on_speak = on_speak

    def install_synthetic_speak(self) -> None:
        self._tools["speak"].handler = self._synthetic_speak  # noqa: SLF001

    async def _synthetic_speak(
        self,
        *,
        actor: ActorContext,
        text: str,
    ) -> dict[str, Any]:
        del actor, text
        self.speak_entries += 1
        if self.on_speak is not None:
            self.on_speak()
        return {
            "spoken": True,
            "_attachment": {
                "kind": "voice",
                "mime_type": "audio/ogg",
                "audio_base64": "c3ludGhldGljLW9nZw==",
            },
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


def _seed_source_search_raw(storage: Any) -> RawObject:
    raw = RawObject(
        id=new_id("raw"),
        user_id=_SOURCE_SEARCH_OWNER,
        source="synthetic-upload",
        source_ref=new_id("synthetic-source"),
        raw_content=(
            f"Служебное вступление.\nДолжность Синтетикова ({_SOURCE_CANARY}): инженер по эксплуатации."
        ),
        content_type="file",
        metadata_json={"filename": "source-search.txt", "uploaded_by": _SOURCE_SEARCH_OWNER},
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=_SOURCE_SEARCH_OWNER,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
        )
    )
    return raw


def _source_search_runtime(
    settings: Any,
    storage: Any,
    *,
    on_speak: Callable[[], None] | None = None,
) -> tuple[AgentRuntime, _SourceSearchKernel, ActorContext, _SourceSearchModel]:
    storage.ensure_user(_SOURCE_SEARCH_OWNER, preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = _SourceSearchKernel(authorization, settings, on_speak=on_speak)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    kernel.install_synthetic_speak()
    model = _SourceSearchModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,
    )
    return runtime, kernel, authorization.actor_for_user(_SOURCE_SEARCH_OWNER, source="test"), model


def _assert_source_search_failed_closed(storage: Any, response: dict[str, Any]) -> None:
    assert _SOURCE_CANARY not in json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert response["source_search_authority_changed_before_publication"] is True
    assert response["voice"] is None
    assert _SOURCE_CANARY not in str(
        _stored_assistant(storage, response, owner=_SOURCE_SEARCH_OWNER)["content"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["raw_soft_deleted", "knowledge_read_denied"])
async def test_final_tts_reauthorizes_source_search_immediately_before_synthesis(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    raw = _seed_source_search_raw(storage)
    runtime, kernel, actor, model = _source_search_runtime(settings, storage)
    original_voice = runtime._voice_of_the_final_answer  # noqa: SLF001

    async def revoke_then_synthesize(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if mutation == "raw_soft_deleted":
            _soft_delete_raw(storage, raw.id)
        elif mutation == "knowledge_read_denied":
            storage.set_permission_override(_SOURCE_SEARCH_OWNER, "knowledge.read", "deny")
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(mutation)
        return await original_voice(*args, **kwargs)

    monkeypatch.setattr(runtime, "_voice_of_the_final_answer", revoke_then_synthesize)
    response = await runtime.chat(
        _SOURCE_SEARCH_OWNER,
        _SOURCE_SEARCH_REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert [name for name, _ in kernel.calls] == ["source_search"]
    _assert_source_search_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_source_search_revoked_during_admitted_speak_drops_voice_and_keeps_entry_ledger(
    settings: Any,
    storage: Any,
) -> None:
    raw = _seed_source_search_raw(storage)
    runtime, kernel, actor, model = _source_search_runtime(
        settings,
        storage,
        on_speak=lambda: _soft_delete_raw(storage, raw.id),
    )

    response = await runtime.chat(
        _SOURCE_SEARCH_OWNER,
        _SOURCE_SEARCH_REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert [name for name, _ in kernel.calls] == ["source_search", "speak"]
    assert kernel.speak_entries == 1
    assert _speak_audit_reasons(storage, owner=_SOURCE_SEARCH_OWNER) == ["started", "ok"]
    _assert_source_search_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_unchanged_source_search_synthesizes_once_and_publishes_one_model_answer(
    settings: Any,
    storage: Any,
) -> None:
    _seed_source_search_raw(storage)
    runtime, kernel, actor, model = _source_search_runtime(settings, storage)

    response = await runtime.chat(
        _SOURCE_SEARCH_OWNER,
        _SOURCE_SEARCH_REQUEST,
        actor=actor,
        enable_tools=True,
        hybrid_searcher=_EmptySearcher(),
        answer_with_voice=True,
    )

    assert model.calls == 1
    assert [name for name, _ in kernel.calls] == ["source_search", "speak"]
    assert kernel.speak_entries == 1
    assert response["voice"]["audio_base64"] == "c3ludGhldGljLW9nZw=="
    assert _SOURCE_CANARY in response["message"]


class _WorkspaceKernel:
    def __init__(
        self,
        storage: Any,
        workspace_text: str,
        *,
        on_speak: Callable[[], None] | None = None,
    ) -> None:
        self.authorization = AuthorizationService(storage)
        self.workspace_text = workspace_text
        self.on_speak = on_speak
        self.changed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def get_tool(name: str) -> Any:
        if name == "speak":
            return SimpleNamespace(risk="mutate", security_id="tts.use")
        return None

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name == "speak":
            if not self.authorization.authorize(actor, "tts.use").allowed:
                return ToolResult(name, False, error="Authorization denied")
            if self.on_speak is not None:
                self.on_speak()
            return ToolResult(
                name,
                True,
                data={"spoken": True},
                attachment={
                    "kind": "voice",
                    "mime_type": "audio/ogg",
                    "audio_base64": "c3ludGhldGljLW9nZw==",
                },
            )
        if name == "workspace_list":
            return ToolResult(
                name,
                True,
                data={
                    "scope": "workspace_inbox",
                    "matched_at_least": 1,
                    "scan_limit_reached": False,
                    "entries": [
                        {
                            "path": "dept/final-tts.md",
                            "name": "final-tts.md",
                            "type": "file",
                            "size_bytes": len(self.workspace_text.encode()),
                            "modified_ns": 1,
                        }
                    ],
                    "returned": 1,
                    "complete": True,
                    "projection_truncated": False,
                    "snapshot_sha256": "c" * 64,
                    "next_cursor": None,
                },
            )
        assert name == "workspace_read"
        assert arguments == {"relative_path": "dept/final-tts.md", "offset": 0}
        return ToolResult(
            name,
            True,
            data={
                "scope": "workspace_inbox",
                "path": "dept/final-tts.md",
                "filename": "final-tts.md",
                "mime_type": "text/markdown",
                "size_bytes": len(self.workspace_text.encode()),
                "sha256": ("d" if self.changed else "a") * 64,
                "source_sha256": ("e" if self.changed else "b") * 64,
                "readable": True,
                "source_complete": True,
                "advisory_only": False,
                "verification_eligible": True,
                "unsupported_format": False,
                "extraction_status": "readable",
                "source_truncated": False,
                "parse_deadline_reached": False,
                "parse_pages_read": 0,
                "parse_total_pages": 0,
                "parse_pages_truncated": False,
                "archive_truncated": False,
                "source_truncated_for_parse": False,
                "text": self.workspace_text,
                "offset": 0,
                "next_offset": None,
                "text_chars": len(self.workspace_text),
                "projection_complete": True,
            },
        )

    def get_tool_definitions(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        raise AssertionError("deterministic workspace read built model-selected tool schemas")


def _workspace_runtime(
    settings: Any,
    storage: Any,
    *,
    on_speak: Callable[[], None] | None = None,
) -> tuple[AgentRuntime, _WorkspaceKernel, _ReviewModel]:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    workspace_text = f"Поле X: {_SOURCE_CANARY}.\nКонтрольный документ MCP."
    kernel = _WorkspaceKernel(storage, workspace_text, on_speak=on_speak)
    model = _ReviewModel()
    return (
        AgentRuntime(configured, storage, llm=model, kernel=kernel),  # type: ignore[arg-type]
        kernel,
        model,
    )


async def _workspace_chat(
    runtime: AgentRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    async def forbidden_prepare(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("explicit MCP inbox read entered general retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    return await runtime.chat(
        "alice",
        "Прочитай файл final-tts.md из MCP inbox и дай подробное ревью его содержимого.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        answer_with_voice=True,
    )


@pytest.mark.asyncio
async def test_final_tts_reauthorizes_workspace_source_immediately_before_synthesis(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, model = _workspace_runtime(settings, storage)
    original_voice = runtime._voice_of_the_final_answer  # noqa: SLF001

    async def replace_workspace_then_synthesize(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        kernel.changed = True
        return await original_voice(*args, **kwargs)

    monkeypatch.setattr(runtime, "_voice_of_the_final_answer", replace_workspace_then_synthesize)
    response = await _workspace_chat(runtime, monkeypatch)

    names = [name for name, _ in kernel.calls]
    assert model.calls == 1
    assert "speak" not in names
    _assert_attachment_source_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_workspace_change_during_speak_is_caught_by_last_provider_reread(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, model = _workspace_runtime(settings, storage)
    kernel.on_speak = lambda: setattr(kernel, "changed", True)
    response = await _workspace_chat(runtime, monkeypatch)

    names = [name for name, _ in kernel.calls]
    assert model.calls == 1
    assert names.count("speak") == 1
    assert names[-1] == "workspace_read", "final provider reread did not happen after admitted TTS"
    _assert_attachment_source_failed_closed(storage, response)


@pytest.mark.asyncio
async def test_unchanged_workspace_voice_has_one_tts_and_final_provider_reread_after_it(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, model = _workspace_runtime(settings, storage)
    response = await _workspace_chat(runtime, monkeypatch)

    names = [name for name, _ in kernel.calls]
    assert model.calls == 1
    assert names.count("speak") == 1
    assert names.count("workspace_read") == 3
    assert names[-1] == "workspace_read", "provider reread must remain the last await before commit"
    assert response["voice"]["audio_base64"] == "c3ludGhldGljLW9nZw=="
    assert _SOURCE_CANARY in response["message"]


@pytest.mark.asyncio
async def test_archive_backed_turn_never_starts_tts_before_consuming_phase2_ledger(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_archive_search_runtime_publication import (
        _chat as archive_chat,
    )
    from tests.test_archive_search_runtime_publication import (
        _runtime as archive_runtime,
    )
    from tests.test_archive_search_runtime_publication import (
        _seed_document as seed_archive_document,
    )

    seed_archive_document(storage)
    runtime, kernel, actor, _model, web, contexts = await archive_runtime(
        settings,
        storage,
        monkeypatch,
    )

    async def forbidden_tts(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("archive-backed answer entered TTS before phase-2 publication")

    monkeypatch.setattr(runtime, "_voice_of_the_final_answer", forbidden_tts)
    try:
        response = await archive_chat(runtime, actor)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert contexts[0].archive_search_ledger_frozen is True
    assert response["archive_search_authority_changed_before_publication"] is False
    assert response["voice"] is None
