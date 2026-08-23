"""End-to-end Obsidian exact-byte authority through archive_search."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.organs.obsidian import OBSIDIAN_READ
from friday.organs.obsidian.runtime import ObsidianRuntime
from friday.organs.obsidian.vault_store import VaultStore
from friday.permissions import ActorContext, AuthorizationService
from friday.web_surfer import WebSurfer

_OWNER = "archive-obsidian-runtime-owner"
_QUERY = "OBSIDIAN-RUNTIME-QUERY-8247"
_SECRET = "OBSIDIAN-CURRENT-BODY-SECRET-5931"
_CHANGED_SECRET = "OBSIDIAN-CHANGED-BODY-SECRET-3816"
_PATH = "Projects/Runtime Authority.md"
_MOVED_PATH = "Projects/Runtime Authority Moved.md"
_BODY = f"Current canonical note body. Value: {_SECRET}.\n"
_ANSWER = f"В текущей заметке указано значение {_SECRET} [A1.1]."
_SAFE_ANSWER = "Точное содержимое заметки сейчас недоступно."


class _UnusedManager:
    def close(self) -> None:
        return None


class _ArchiveObsidianModel:
    enabled = True
    model = "archive-obsidian-runtime-model"
    total_budget_sec = 3.0

    def __init__(
        self,
        *,
        answer: str,
        before_answer: Callable[[], None] | None = None,
    ) -> None:
        self.answer = answer
        self.before_answer = before_answer
        self.calls = 0
        self.archive_tool_body = ""
        self.archive_page: dict[str, Any] = {}

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        tools = kwargs.get("tools") or []
        tool_names = {
            str((item.get("function") or {}).get("name") or item.get("name") or "") for item in tools
        }
        if self.calls == 1:
            assert "archive_search" in tool_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "archive-obsidian-first",
                        "type": "function",
                        "function": {
                            "name": "archive_search",
                            "arguments": json.dumps(
                                {
                                    "query": _QUERY,
                                    "corpora": ["obsidian"],
                                    "limit": 5,
                                }
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }

        tool_messages = [item for item in messages if item.get("role") == "tool"]
        assert len(tool_messages) == 1
        self.archive_tool_body = str(tool_messages[0].get("content") or "")
        page = json.loads(self.archive_tool_body)
        assert type(page) is dict
        self.archive_page = page
        assert page["schema"] == "friday.archive-search-page.public.v1"
        assert self.archive_tool_body == json.dumps(
            page,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if self.before_answer is not None:
            self.before_answer()
        return {
            "content": self.answer,
            "tool_calls": None,
            "finish_reason": "stop",
        }


def _seed_current_note(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> tuple[ObsidianRuntime, VaultStore, dict[str, Any], Any]:
    storage.ensure_user(_OWNER, preset_key="user")
    root = tmp_path / "vault"
    bundle = storage.create_obsidian_bundle(
        _OWNER,
        config_root=str(tmp_path / "config"),
        database_root=str(tmp_path / "database"),
        api_endpoint=f"unix://{tmp_path}/syncthing.sock",
        api_key_ref=f"secret:obsidian:{_OWNER}",
        server_path=str(root),
        folder_id=f"friday-{_OWNER}",
        setup_token_hash=hashlib.sha256(b"archive-obsidian-runtime-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    vault = storage.update_obsidian_vault(_OWNER, state="ready")
    store = VaultStore(root)
    written = store.write_text(_PATH, _BODY, create_only=True)
    binding = storage.upsert_obsidian_note_binding(
        _OWNER,
        vault_id=str(vault["id"]),
        integration_id="archive-obsidian-runtime-note",
        current_path=written.path,
        current_revision=written.revision,
        origin="user",
    )
    storage.upsert_obsidian_note_index(
        _OWNER,
        binding_id=str(binding["id"]),
        revision=written.revision,
        metadata={"aliases": []},
        metadata_coverage="complete",
        body_text=_BODY,
        body_coverage="complete",
        source_size_bytes=len(_BODY.encode("utf-8")),
        title="Runtime Authority",
    )
    runtime = ObsidianRuntime(settings, storage, _UnusedManager())  # type: ignore[arg-type]
    return runtime, store, bundle, written


async def _runtime(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    obsidian: ObsidianRuntime,
    answer: str,
    before_answer: Callable[[], None] | None = None,
) -> tuple[AgentRuntime, ActorContext, _ArchiveObsidianModel, WebSurfer, list[AgentContext]]:
    configured = replace(settings, verify_answers=False)
    authorization = AuthorizationService(storage)
    authorization.register_capability(OBSIDIAN_READ)
    actor = authorization.actor_for_user(_OWNER, source="archive-obsidian-runtime-test")
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(configured, storage, graph)
    web = WebSurfer(configured)
    kernel = ExecutionKernel(authorization, configured)
    kernel.bind_services(storage, graph, web, ingestion)
    kernel.bind_archive_obsidian_exact_file_reader_factory(obsidian.bind_archive_exact_file_reader)
    model = _ArchiveObsidianModel(answer=answer, before_answer=before_answer)
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]
    contexts: list[AgentContext] = []

    def remember_context(context: AgentContext) -> None:
        if not any(item is context for item in contexts):
            contexts.append(context)

    async def narrow_context(
        user_id: str,
        message: str,
        conversation_id: str,
        **kwargs: Any,
    ) -> AgentContext:
        del message
        context = AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=str(kwargs.get("person_id") or user_id),
            search_query=_QUERY,
            outward_verdict=("архив", _QUERY),
            interaction_mode="dialogue",
            turn_deadline=kwargs.get("turn_deadline"),
        )
        remember_context(context)
        return context

    monkeypatch.setattr(runtime, "_prepare_context", narrow_context)
    original_isolate = runtime._isolate_archive_search_context

    def observe_archive_isolation(context: AgentContext, message: str) -> None:
        remember_context(context)
        original_isolate(context, message)

    monkeypatch.setattr(runtime, "_isolate_archive_search_context", observe_archive_isolation)
    return runtime, actor, model, web, contexts


async def _chat(runtime: AgentRuntime, actor: ActorContext) -> dict[str, Any]:
    return await runtime.chat(
        _OWNER,
        "Найди контрольное значение в моём личном архиве.",
        actor=actor,
        enable_tools=True,
        answer_with_voice=False,
    )


def _lexical_coverage(page: dict[str, Any]) -> dict[str, Any]:
    return next(
        item for item in page["coverage"] if item["corpus"] == "obsidian" and item["lane"] == "lexical"
    )


@pytest.mark.asyncio
async def test_current_note_exact_bytes_reach_model_and_publish(
    settings: Any,
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obsidian, _store, bundle, written = _seed_current_note(settings, storage, tmp_path)
    reader = await obsidian.bind_archive_exact_file_reader(_OWNER)
    assert reader is not None
    assert reader(str(bundle["vault"]["id"]), written.path, written.revision) == _BODY.encode()
    runtime, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        obsidian=obsidian,
        answer=_ANSWER,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert model.calls == 2
    assert _SECRET in model.archive_tool_body
    assert model.archive_page["candidates"]
    assert _lexical_coverage(model.archive_page)["states"] == ["complete"]
    assert response["message"] == _ANSWER
    assert response["archive_search_authority_changed_before_publication"] is False
    assert contexts[0].archive_exact_file_reader is not None
    assert contexts[0].archive_search_ledger_frozen is True
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None and stored["content"] == _ANSWER


@pytest.mark.asyncio
async def test_missing_exact_reader_never_projects_indexed_body_and_reports_unavailable(
    settings: Any,
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obsidian, _store, _bundle, _written = _seed_current_note(settings, storage, tmp_path)
    invalid_root = tmp_path / "not-a-vault-directory"
    invalid_root.write_text("ordinary file", encoding="utf-8")
    storage.execute(
        "UPDATE obsidian_vaults SET server_path=? WHERE user_id=?",
        (str(invalid_root), _OWNER),
    )
    assert await obsidian.bind_archive_exact_file_reader(_OWNER) is None
    runtime, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        obsidian=obsidian,
        answer=_SAFE_ANSWER,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert model.calls == 2
    assert model.archive_page["candidates"] == []
    assert _SECRET not in model.archive_tool_body
    assert _BODY.strip() not in model.archive_tool_body
    coverage = _lexical_coverage(model.archive_page)
    assert "unavailable" in coverage["states"]
    assert coverage["authority_rechecked"] is False
    assert coverage["snapshot_current"] is False
    assert _SECRET not in json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert contexts[0].archive_exact_file_reader is None
    assert contexts[0].archive_search_ledger_frozen is True


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["revision", "path", "vault"])
async def test_note_drift_after_model_admission_forces_source_free_fallback(
    settings: Any,
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    obsidian, store, _bundle, written = _seed_current_note(settings, storage, tmp_path)

    def mutate() -> None:
        if drift == "revision":
            store.write_text(
                _PATH,
                f"Changed canonical note body. Value: {_CHANGED_SECRET}.\n",
                expected_revision=written.revision,
            )
        elif drift == "path":
            store.move(_PATH, _MOVED_PATH, expected_revision=written.revision)
        else:
            storage.execute(
                "UPDATE obsidian_vaults SET server_path=? WHERE user_id=?",
                (str(tmp_path / "different-vault-root"), _OWNER),
            )

    runtime, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        obsidian=obsidian,
        answer=_ANSWER,
        before_answer=mutate,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert model.calls == 2
    assert _SECRET in model.archive_tool_body
    assert response["archive_search_authority_changed_before_publication"] is True
    assert response["message"] != _ANSWER
    assert response["files"] == []
    assert response["voice"] is None
    public = json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert _SECRET not in public
    assert _CHANGED_SECRET not in public
    assert contexts[0].archive_search_ledger_frozen is True
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    assert _SECRET not in str(stored["content"])
    assert _CHANGED_SECRET not in str(stored["content"])
