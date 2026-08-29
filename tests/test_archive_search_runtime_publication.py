"""AgentRuntime admits and publishes only phase-2-attested archive pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

import friday.agent_runtime as agent_runtime_module
import friday.interaction_control_plane.archive_candidate_selection_store as candidate_store_module
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _archive_definitive_absence_claim,
    _archive_search_code_owned_corpora,
    _archive_search_literal_shape_is_safe,
    _archive_search_public_summary,
    _archive_search_semantic_content,
    _ArchiveSearchPublicSummary,
    is_archive_search_current_text,
)
from friday.agent_runtime.llm import LLMRouter
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.ingestion import IngestionPipeline
from friday.interaction_control_plane.archive_candidate_selection import (
    ARCHIVE_CANDIDATE_CANCELLED,
    ARCHIVE_CANDIDATE_EXPIRED,
    ARCHIVE_CANDIDATE_STALE,
    archive_candidate_cancel_requested,
    archive_candidate_reask_prompt,
    archive_candidate_selection_offer_suffix,
)
from friday.interaction_control_plane.archive_candidate_selection_store import (
    cancel_archive_candidate_selection_in_transaction,
    expire_archive_candidate_selection_in_transaction,
    get_archive_candidate_selection_work_item_in_transaction,
)
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    get_current_recall_selected_archive_evidence_work_item_in_transaction,
    get_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.interaction_control_plane.legacy_trace import CapabilityStatus
from friday.interaction_control_plane.work_item_contract import WorkState, WorkTransition
from friday.interaction_control_plane.work_item_store import WorkItemConflictError
from friday.knowledge_graph import KnowledgeGraph
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration.archive_recall_outcome import (
    ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
    ArchiveRecallLane,
    ArchiveRecallStatus,
    load_accepted_archive_recall_outcome_receipt,
)
from friday.orchestration.router import OrchestrationRouter
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ArchiveSearchAuthorityError,
    ArchiveSearchPublicationDenialReason,
    ArchiveSearchPublicationDenied,
    attest_archive_search_before_publication,
    create_archive_model_batch_ledger,
)
from friday.storage import FridayStorage
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id
from friday.turn_intent_policy import (
    WEATHER_LOCATION_CLARIFICATION,
    TurnIntent,
    TurnPolicyDecision,
)
from friday.web_surfer import WebSurfer

_OWNER = "archive-runtime-owner"
_QUERY = "ARCHIVE-RUNTIME-PRIVATE-CANARY-7421"
_CANDIDATE_QUERY = "needle7421"
_CANDIDATE_FIRST_BODY = f"{_CANDIDATE_QUERY} exact alpha source body"
_CANDIDATE_SECOND_BODY = f"{_CANDIDATE_QUERY} exact beta source body"
_ANSWER = f"В личном архиве найдено значение {_QUERY} [A1.1]."


@pytest.mark.parametrize("value", ["отмена", "Отмена!", "cancel", "CANCEL."])
def test_archive_candidate_cancel_parser_accepts_only_exact_ru_en_commands(value: str) -> None:
    assert archive_candidate_cancel_requested(value)


@pytest.mark.parametrize("value", ["отмени", "cancel selection", "please cancel", 1, None])
def test_archive_candidate_cancel_parser_rejects_free_form_values(value: object) -> None:
    assert not archive_candidate_cancel_requested(value)


class _ArchiveModel:
    enabled = True
    model = "archive-runtime-publication-model"
    total_budget_sec = 3.0

    def __init__(
        self,
        *,
        before_answer: Callable[[], None] | None = None,
        second_round_calls: list[dict[str, Any]] | None = None,
        final_answer: str = _ANSWER,
        first_arguments: dict[str, Any] | None = None,
        expected_marker: str = _QUERY,
    ) -> None:
        self.before_answer = before_answer
        self.second_round_calls = second_round_calls
        self.final_answer = final_answer
        self.first_arguments = first_arguments or {
            "query": _QUERY,
            "corpora": ["documents"],
            "limit": 5,
        }
        self.expected_marker = expected_marker
        self.calls = 0
        self.archive_tool_body = ""
        self.second_round_tool_names: list[str] = []
        self.call_payloads: list[str] = []
        self.call_kwargs: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.call_payloads.append(json.dumps(messages, ensure_ascii=False, sort_keys=True))
        self.call_kwargs.append(dict(kwargs))
        tools = kwargs.get("tools") or []
        tool_names = [
            str((item.get("function") or {}).get("name") or item.get("name") or "") for item in tools
        ]
        if self.calls == 1:
            assert "archive_search" in tool_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "archive-first",
                        "type": "function",
                        "function": {
                            "name": "archive_search",
                            "arguments": json.dumps(self.first_arguments),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }

        tool_messages = [item for item in messages if item.get("role") == "tool"]
        assert tool_messages
        self.archive_tool_body = str(tool_messages[0].get("content") or "")
        public_page = json.loads(self.archive_tool_body)
        assert public_page["schema"] == "friday.archive-search-page.public.v1"
        if public_page.get("candidates"):
            assert self.expected_marker in self.archive_tool_body
        self.second_round_tool_names = tool_names

        if self.calls == 2 and self.second_round_calls is not None:
            return {
                "content": "",
                "tool_calls": self.second_round_calls,
                "finish_reason": "tool_calls",
            }
        if self.before_answer is not None:
            self.before_answer()
        return {
            "content": self.final_answer,
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _NeverPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("durable archive continuation reached the planner")

    async def plan_attested(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self.plan(*_args, **_kwargs)


class _SpyKernel(ExecutionKernel):
    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        # Never retain the process-private invocation itself in test output.
        self.calls.append(
            (
                name,
                {key: value for key, value in arguments.items() if key != "_archive_invocation"},
            )
        )
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _CopiedPayloadKernel(_SpyKernel):
    """Simulate a broken adapter returning plausible JSON without its carrier."""

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        del actor, execution_scope
        self.calls.append(
            (
                name,
                {key: value for key, value in arguments.items() if key != "_archive_invocation"},
            )
        )
        return ToolResult(
            name,
            True,
            data=json.dumps(
                {
                    "copied_private_value": _QUERY,
                    "schema": "friday.archive-search-page.public.v1",
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


class _AdversarialArchiveModel:
    enabled = True
    model = "archive-runtime-adversarial-model"
    total_budget_sec = 3.0

    def __init__(
        self,
        first_arguments: dict[str, Any],
        *,
        tool_name: str = "archive_search",
    ) -> None:
        self.first_arguments = first_arguments
        self.tool_name = tool_name
        self.calls = 0
        self.tool_body = ""
        self.first_offered_tool_names: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            self.first_offered_tool_names = [
                str((item.get("function") or {}).get("name") or item.get("name") or "")
                for item in (kwargs.get("tools") or [])
            ]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "archive-adversarial",
                        "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "arguments": json.dumps(self.first_arguments),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        assert tool_messages
        self.tool_body = str(tool_messages[-1].get("content") or "")
        return {
            "content": "Приватный результат не был принят.",
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _DirectAnswerModel:
    enabled = True
    model = "archive-runtime-direct-answer-model"
    total_budget_sec = 3.0

    def __init__(self) -> None:
        self.calls = 0
        self.call_kwargs: list[dict[str, Any]] = []

    async def chat(self, _messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.call_kwargs.append(dict(kwargs))
        return {
            "content": f"По памяти в архиве якобы есть {_QUERY}.",
            "tool_calls": None,
            "finish_reason": "stop",
        }


class _SelectedArchiveExplanationModel:
    """Exact fake for the attested two-pass selected-evidence continuation."""

    def __init__(
        self,
        *,
        answer: str = "В выбранном фрагменте указано контрольное значение [A1.1].",
        verifier_supported: bool = True,
        after_verifier: Callable[[], None] | None = None,
        lease_valid_checks: int | None = None,
        final_lease_error: str | None = None,
    ) -> None:
        self.answer = answer
        self.verifier_supported = verifier_supported
        self.after_verifier = after_verifier
        self.lease_valid_checks = lease_valid_checks
        self.final_lease_error = final_lease_error
        self.lease_checks = 0
        self.process_lease_checks = 0
        self.calls: list[list[dict[str, Any]]] = []
        self.lease: ModelProfileLease | None = None
        self.citation_labels: tuple[str, ...] = ()
        self.evidence_fragments: tuple[dict[str, str], ...] = ()

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        assert absolute_deadline > 0
        self.lease = ModelProfileLease(
            profile_id="selected-archive-explanation-test:dispatcher",
            attestation_sha256="a" * 64,
            requirements_sha256=requirements.canonical_sha256(),
            capabilities=requirements.capabilities,
            required_context_tokens=requirements.required_context_tokens,
            prepared_evidence_items=requirements.prepared_evidence_items,
            max_tool_steps=requirements.max_tool_steps,
            max_tool_rounds=requirements.max_tool_rounds,
            max_tool_calls=requirements.max_tool_calls,
            effect=requirements.effect,
            verifier_required=requirements.verifier_required,
            process_epoch_sha256="b" * 64,
            _gate_authority=self,
            _gate_generation=1,
        )
        return self.lease

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        self.lease_checks += 1
        if self.lease_checks == 3 and self.final_lease_error == "timeout":
            raise TimeoutError("test final lease timeout")
        if self.lease_checks == 3 and self.final_lease_error == "provider":
            raise RuntimeError("test final lease provider failure")
        return bool(
            absolute_deadline > 0
            and lease is self.lease
            and self.lease is not None
            and self.lease.requirements_sha256 == requirements.canonical_sha256()
            and (self.lease_valid_checks is None or self.lease_checks <= self.lease_valid_checks)
        )

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        self.process_lease_checks += 1
        return bool(
            lease is self.lease
            and self.lease is not None
            and self.lease.requirements_sha256 == requirements.canonical_sha256()
        )

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert lease is self.lease
        assert self.lease is not None
        assert self.lease.requirements_sha256 == requirements.canonical_sha256()
        assert requirements.max_tool_rounds == 0
        assert requirements.max_tool_calls == 0
        self.calls.append(messages)
        if len(self.calls) == 1:
            synthesis = json.loads(str(messages[-1]["content"]))
            fragments = synthesis["evidence"]["fragments"]
            assert isinstance(fragments, list)
            self.evidence_fragments = tuple(dict(item) for item in fragments)
            self.citation_labels = tuple(str(item["label"]) for item in fragments)
            assert self.citation_labels == tuple(f"A1.{index}" for index in range(1, len(fragments) + 1))
            content = self.answer
        else:
            assert self.citation_labels
            content = json.dumps(
                {
                    "schema": "friday.v12-file-verifier.v1",
                    "supported": self.verifier_supported,
                    "citation_labels": list(self.citation_labels),
                    "unsupported_claims": 0 if self.verifier_supported else 1,
                }
            )
            if self.after_verifier is not None:
                self.after_verifier()
        return {"content": content, "tool_calls": None, "finish_reason": "stop"}


class _WebThenArchiveModel:
    enabled = True
    model = "archive-runtime-web-then-archive-model"
    total_budget_sec = 3.0

    def __init__(self) -> None:
        self.calls = 0
        self.first_offered_tool_names: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        tools = kwargs.get("tools") or []
        offered = [str((item.get("function") or {}).get("name") or item.get("name") or "") for item in tools]
        if self.calls == 1:
            self.first_offered_tool_names = offered
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "forbidden-web-first",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": _QUERY}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        if self.calls == 2:
            assert offered == ["archive_search"]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "archive-after-denied-web",
                        "type": "function",
                        "function": {
                            "name": "archive_search",
                            "arguments": json.dumps({"query": _QUERY, "corpora": ["documents"], "limit": 5}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        archive_bodies = [
            str(item.get("content") or "")
            for item in messages
            if item.get("role") == "tool" and str(item.get("content") or "").startswith("{")
        ]
        assert archive_bodies and _QUERY in archive_bodies[-1]
        return {"content": _ANSWER, "tool_calls": None, "finish_reason": "stop"}


class _CancelAfterFirstPageModel:
    enabled = True
    model = "archive-runtime-cancel-after-page-model"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0
        self.second_call_started = asyncio.Event()

    async def chat(self, _messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "archive-before-cancel",
                        "type": "function",
                        "function": {
                            "name": "archive_search",
                            "arguments": json.dumps({"query": _QUERY, "corpora": ["documents"], "limit": 5}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        self.second_call_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _CancelDuringArchiveKernel(_SpyKernel):
    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.archive_call_started = asyncio.Event()

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        if name == "archive_search":
            self.archive_call_started.set()
            await asyncio.Event().wait()
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


def _seed_document(
    storage: Any,
    *,
    suffix: str = "",
    inbox_status: InboxStatus = InboxStatus.PENDING,
) -> str:
    raw_id = new_id("raw")
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=_OWNER,
            source="upload",
            source_ref=f"telegram-file:archive-runtime-publication{suffix}",
            raw_content=f"Закрытый документ{suffix}. Контрольное значение: {_QUERY}.",
            content_type="file",
            metadata_json={
                "filename": "archive-runtime.txt",
                "mime_type": "text/plain",
                "media_kind": "document",
                "uploaded_by": _OWNER,
            },
            content_hash=hashlib.sha256(f"archive-runtime-source{suffix}".encode()).hexdigest(),
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=_OWNER,
            raw_object_id=raw_id,
            status=inbox_status,
        )
    )
    return raw_id


def _seed_message_archive(
    storage: Any,
    *,
    suffix: str,
    compact: bool = False,
    selected_text: str | None = None,
) -> tuple[str, str]:
    storage.ensure_user(_OWNER, preset_key="user")
    conversation = storage.create_conversation(_OWNER, title=f"message archive{suffix}")
    storage.store_message(
        str(conversation["id"]),
        _OWNER,
        "user",
        "q" if compact else f"Исходный вопрос{suffix}.",
    )
    selected_text = selected_text or (_QUERY if compact else f"Ответ до исходной границы{suffix}: {_QUERY}.")
    storage.store_message(
        str(conversation["id"]),
        _OWNER,
        "assistant",
        selected_text,
    )
    return str(conversation["id"]), selected_text


def _reopen_storage(settings: Any, storage: Any) -> FridayStorage:
    return FridayStorage(
        replace(
            settings,
            database_path=storage.settings.database_path,
            database_must_exist=True,
        )
    )


_FRESH_INTERPRETER_REPLAY_PROBE = r"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

source_root, database_path, user_id, conversation_id, work_item_id = sys.argv[1:]
sys.path.insert(0, source_root)

from friday.config import load_settings
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    get_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.permissions import AuthorizationService
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    replay_archive_evidence_in_transaction,
)
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.storage import FridayStorage

settings = replace(
    load_settings(),
    database_path=Path(database_path),
    database_must_exist=True,
)
storage = FridayStorage(settings)
try:
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user(user_id, source="archive-fresh-interpreter-probe")
    with storage.transaction() as conn:
        item = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if item is None:
            raise RuntimeError("durable archive Work Item is unavailable")
        evidence = item.selected_evidence
        replay = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=actor.user_id,
            principal_id=user_id,
            origin_boundary_user_message_id=evidence.origin_boundary_user_message_id,
            corpus=ArchiveSearchCorpus(evidence.corpus.value),
            source_ref=evidence.source_ref,
            passage_refs=evidence.passage_refs,
            expected_source_snapshot_sha256=evidence.source_snapshot_sha256,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade(
                evidence.coverage_grade.value
            ),
        )
        payload = {
            "corpus": replay.corpus.value,
            "coverage_grade": (
                None if replay.coverage_grade is None else replay.coverage_grade.value
            ),
            "model_visible_sha256": hashlib.sha256(replay.model_visible_bytes).hexdigest(),
            "passage_count": len(replay.excerpts),
            "status": replay.status.value,
            "work_revision": item.revision,
        }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
finally:
    storage.close(final=True)
"""


def _fresh_interpreter_replay(storage: Any, item: Any) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
        (
            sys.executable,
            "-I",
            "-c",
            _FRESH_INTERPRETER_REPLAY_PROBE,
            str(Path(__file__).resolve().parents[1]),
            str(storage.settings.database_path),
            item.user_id,
            item.conversation_id,
            item.id,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert frozenset(payload) == frozenset(
        {
            "corpus",
            "coverage_grade",
            "model_visible_sha256",
            "passage_count",
            "status",
            "work_revision",
        }
    )
    return payload


def _fresh_interpreter_replay_after_clean_shutdown(
    settings: Any,
    storage: Any,
    item: Any,
    request: pytest.FixtureRequest,
) -> tuple[dict[str, Any], FridayStorage]:
    storage.close(final=True)
    payload = _fresh_interpreter_replay(storage, item)
    reopened = _reopen_storage(settings, storage)
    request.addfinalizer(lambda: reopened.close(final=True))
    return payload, reopened


async def _runtime(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    before_answer: Callable[[], None] | None = None,
    second_round_calls: list[dict[str, Any]] | None = None,
    model_override: Any = None,
    kernel_factory: type[_SpyKernel] = _SpyKernel,
    verify_answers: bool = False,
    outward_kind: str = "архив",
    context_initializer: Callable[[AgentContext], None] | None = None,
    archive_query: str = _QUERY,
) -> tuple[AgentRuntime, _SpyKernel, ActorContext, Any, WebSurfer, list[AgentContext]]:
    configured = replace(settings, verify_answers=verify_answers)
    storage.ensure_user(_OWNER, preset_key="user")
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user(_OWNER, source="archive-runtime-test")
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(configured, storage, graph)
    web = WebSurfer(configured)
    kernel = kernel_factory(authorization, configured)
    kernel.bind_services(storage, graph, web, ingestion)
    model = model_override or _ArchiveModel(
        before_answer=before_answer,
        second_round_calls=second_round_calls,
    )
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]
    contexts: list[AgentContext] = []

    def remember_context(context: AgentContext) -> None:
        if any(item is context for item in contexts):
            return
        if context_initializer is not None:
            context_initializer(context)
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
            search_query=archive_query,
            outward_verdict=(outward_kind, archive_query) if outward_kind else None,
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
    return runtime, kernel, actor, model, web, contexts


async def _chat(
    runtime: AgentRuntime,
    actor: ActorContext,
    *,
    answer_with_voice: bool = False,
    message: str = "Найди контрольное значение в моём личном архиве.",
) -> dict[str, Any]:
    return await runtime.chat(
        _OWNER,
        message,
        actor=actor,
        enable_tools=True,
        answer_with_voice=answer_with_voice,
    )


async def _create_durable_selected_archive_work(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    suffix: str,
) -> tuple[str, dict[str, Any], Any]:
    raw_id = _seed_document(
        storage,
        suffix=suffix,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    runtime, kernel, actor, model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
    )
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert model.calls == 2
    row = storage.execute(
        """SELECT id FROM work_items
             WHERE user_id=? AND conversation_id=?
               AND kind='recall_selected_archive_evidence'""",
        (_OWNER, str(response["conversation_id"])),
    ).fetchone()
    assert row is not None
    with storage.transaction() as conn:
        item = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=str(row["id"]),
            user_id=_OWNER,
            conversation_id=str(response["conversation_id"]),
        )
    assert item is not None
    assert item.state is WorkState.ACTIVE
    assert item.transition is WorkTransition.CREATED
    assert item.revision == 1
    return raw_id, response, item


async def _create_durable_archive_candidate_work(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Any, AgentRuntime, _SpyKernel, ActorContext, Any, WebSurfer]:
    _seed_message_archive(
        storage,
        suffix="-alpha",
        compact=True,
        selected_text=_CANDIDATE_FIRST_BODY,
    )
    _seed_message_archive(
        storage,
        suffix="-beta",
        compact=True,
        selected_text=_CANDIDATE_SECOND_BODY,
    )
    model = _ArchiveModel(
        final_answer=(f"Сначала второй источник [A2.1], затем первый [A1.1]: {_CANDIDATE_QUERY}."),
        first_arguments={
            "query": _CANDIDATE_QUERY,
            "corpora": ["messages"],
            "limit": 2,
        },
        expected_marker=_CANDIDATE_QUERY,
    )
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
        archive_query=_CANDIDATE_QUERY,
    )
    response = await _chat(
        runtime,
        actor,
        answer_with_voice=False,
        message="Найди сообщения с контрольным значением в моём личном архиве.",
    )
    row = storage.execute(
        """SELECT id FROM work_items
             WHERE user_id=? AND conversation_id=?
               AND kind='select_archive_candidate_and_replay_evidence'""",
        (_OWNER, str(response["conversation_id"])),
    ).fetchone()
    assert row is not None
    with storage.transaction() as conn:
        item = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=str(row["id"]),
            user_id=_OWNER,
            conversation_id=str(response["conversation_id"]),
        )
    assert item is not None
    return response, item, runtime, kernel, actor, model, web


async def _create_durable_archive_document_candidate_work(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Any, AgentRuntime, _SpyKernel, ActorContext, Any, WebSurfer]:
    _seed_document(storage, suffix="-candidate-alpha", inbox_status=InboxStatus.CLASSIFIED)
    _seed_document(storage, suffix="-candidate-beta", inbox_status=InboxStatus.CLASSIFIED)
    model = _ArchiveModel(
        final_answer=f"Сначала второй документ [A2.1], затем первый [A1.1]: {_QUERY}.",
        first_arguments={"query": _QUERY, "corpora": ["documents"], "limit": 2},
    )
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    response = await _chat(
        runtime,
        actor,
        answer_with_voice=False,
        message="Найди документы с контрольным значением в моём личном архиве.",
    )
    row = storage.execute(
        """SELECT id FROM work_items
             WHERE user_id=? AND conversation_id=?
               AND kind='select_archive_candidate_and_replay_evidence'""",
        (_OWNER, str(response["conversation_id"])),
    ).fetchone()
    assert row is not None
    with storage.transaction() as conn:
        item = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=str(row["id"]),
            user_id=_OWNER,
            conversation_id=str(response["conversation_id"]),
        )
    assert item is not None
    return response, item, runtime, kernel, actor, model, web


def _candidate_message_body(storage: Any, item: Any, ordinal: int) -> str:
    selected = item.candidate_set.selected_evidence(ordinal)
    row = storage.execute(
        """SELECT content FROM messages
             WHERE user_id=? AND conversation_id=? AND role='assistant'
             ORDER BY rowid DESC LIMIT 1""",
        (_OWNER, selected.source_ref.canonical_object_id),
    ).fetchone()
    assert row is not None
    return str(row["content"])


def _assert_archive_ledger_consumed(context: AgentContext) -> None:
    def forbidden_reauthorizer(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("consumed archive ledger reached a reauthorizer")

    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        attest_archive_search_before_publication(
            tenant_id=_OWNER,
            principal_id=_OWNER,
            ledger=context.archive_model_batch_ledger,  # type: ignore[arg-type]
            answer=_ANSWER,
            candidate_reauthorizer=cast(Any, forbidden_reauthorizer),
            coverage_reauthorizer=cast(Any, forbidden_reauthorizer),
            authority_context=object(),
        )
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.LEDGER_UNAVAILABLE


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Найди договор в моём архиве.", True),
        ("Покажи, что есть в моём архиве.", True),
        ("Есть ли в моём архиве договор?", True),
        ("Какие документы в моём архиве?", True),
        ("Не ищи в моём архиве.", False),
        ("Мне нравятся мои документы.", False),
        ("Мои документы лежат в шкафу.", False),
        ("Расскажи шутку про мой архив.", False),
        ("Удали договор из моего архива.", False),
        ("Сохрани это в мой архив.", False),
        ("Найди в моём архиве документы от пользователя Yato.", False),
        ("Не ищи в интернете, найди договор в моём архиве.", True),
        ("Не надо искать в интернете — покажи, что есть в моём архиве.", True),
        ("Найди договор в моём архиве, но не ищи в интернете.", True),
        ("Не ищи старое, но найди договор в моём архиве.", False),
        ("Не ищи в интернете, а найди договор в моём архиве.", True),
        ("Найди договор в моём архиве, но не показывай переписку.", True),
        ("Можешь ли найти договор в моём архиве?", True),
        ("Можешь найти договор в моём архиве?", True),
        ("Можешь ли найти что-нибудь в моём архиве?", True),
        ("Можешь ли ты найти договор в моём архиве?", True),
        ("Можешь, пожалуйста, найти договор в моём архиве?", True),
        ("Будь добра, найди договор в моём архиве.", True),
        ("Пятница, найди договор в моём архиве.", True),
        ("Теперь найди договор в моём архиве.", True),
        ("И ещё найди договор в моём архиве.", True),
        ("Мне надо найти договор в моём архиве.", True),
        ("Сможешь найти договор в моём архиве?", True),
        ("Могла бы ты найти договор в моём архиве?", True),
        ("Можешь ли искать в моём архиве?", False),
        ("Можешь ли ты искать в моём архиве?", False),
        ("Умеешь ли искать в моём архиве?", False),
        ("В заметке сказано найти договор в моём архиве.", False),
        ("Фраза выглядит так: не ищи в интернете, а найди договор в моём архиве.", False),
        ("Команда выглядит так: не ищи в интернете, а найди договор в моём архиве.", False),
        ("Пример команды: найди договор в моём архиве.", False),
        ("Вот пример: найди договор в моём архиве.", False),
        ("Например: найди договор в моём архиве.", False),
        ("Можно сказать так: найди договор в моём архиве.", False),
        ("Объясни фразу: найди договор в моём архиве.", False),
        ("Повтори: найди договор в моём архиве.", False),
        ("Переведи на английский: найди договор в моём архиве.", False),
        ("Исправь грамматику: найди договор в моём архиве.", False),
        ("Напиши в ответ: найди договор в моём архиве.", False),
        ("В инструкции указано: найди договор в моём архиве.", False),
        ("Со слов Ивана — найди договор в моём архиве.", False),
        ("Мне передали просьбу: найди договор в моём архиве.", False),
        ("Допустим, я скажу: найди договор в моём архиве.", False),
        ("Предположим, я попрошу найти договор в моём архиве.", False),
        ("Не повторяй фразу: найди договор в моём архиве.", False),
        ("Не интерпретируй как команду: найди договор в моём архиве.", False),
        ("Не исполняй следующий текст — найди договор в моём архиве.", False),
        ("Это не команда: найди договор в моём архиве.", False),
        ("Найди договор в моём архиве — сказал Иван.", False),
        ("Найди договор в моём архиве, попросил Иван.", False),
        ("Найди договор в моём архиве — так написал Иван.", False),
        ("Найди договор в моём архиве — это пример команды.", False),
        ("Найди договор в моём архиве — так выглядит фраза.", False),
        ("Найди договор в моём архиве (это цитата).", False),
        ("Найди перевод выражения найди договор в моём архиве.", False),
        ("Найди ошибку во фразе мой личный архив.", False),
        ("Найди слова мой архив в этом предложении.", False),
        ("Покажи, как выглядит команда найди договор в моём архиве.", False),
        ("Прочитай вслух фразу найди договор в моём архиве.", False),
        ("Перечисли слова в выражении мой личный архив.", False),
        ("Проверь грамматику фразы найди договор в моём архиве.", False),
        ("Найди договор в моём архиве — велел Иван.", False),
        ("Найди договор в моём архиве — произнёс Иван.", False),
        ("Найди договор в моём архиве — ответил Иван.", False),
        ("Найди договор в моём архиве — скомандовал Иван.", False),
        ("Найди договор в моём архиве — передал Иван.", False),
        ("Найди договор в моём архиве — это не команда.", False),
        ("Найди договор в моём архиве — не просьба.", False),
        ("Найди договор в моём архиве — просто текст.", False),
        ("Найди синоним для команды найди договор в моём архиве.", False),
        ("Найди смысл команды найди договор в моём архиве.", False),
        ("Найди различия между командами найди и покажи договор в моём архиве.", False),
        ("Найди в строке найди договор в моём архиве глагол.", False),
        ("Покажи синтаксис команды найди договор в моём архиве.", False),
        ("Прочитай текст найди договор в моём архиве задом наперёд.", False),
        ("Посмотри на предложение найди договор в моём архиве.", False),
        ("Открой кавычки: найди договор в моём архиве.", False),
        ("Найди пример договора в моём архиве.", True),
        ("Найди цитату Иванова в моём архиве.", True),
        ("Найди фразу про расторжение в моём архиве.", True),
        ("Найди договор в моём архиве. Это пример команды.", False),
        ("Найди договор в моём архиве. Так сказал Иван.", False),
        ("Найди договор в моём архиве. Это не команда.", False),
        ("Найди договор в моём архиве! Просто повтори текст.", False),
        ("Найди договор в моём архиве\nэто цитата", False),
        ("Найди договор в моём архиве … это просто пример", False),
        ("Найди договор в моём архиве сказал Иван.", False),
        ("Что есть в моём архиве — спросил Иван.", False),
        ("Что есть в моём архиве? Это пример вопроса.", False),
        ("Что есть в моём архиве? Так спросил Иван.", False),
        ("Какие документы есть в моём архиве — это не вопрос.", False),
        ("Есть ли договор в моём архиве? — спросил Иван.", False),
        ("Есть ли договор в моём архиве? Это пример вопроса.", False),
        ("Какие документы есть в моём архиве; это пример.", False),
        ("Найди ошибку в тексте договор в моём архиве.", False),
        ("Найди опечатку в тексте договор в моём архиве.", False),
        ("Найди количество букв в строке договор в моём архиве.", False),
        ("Покажи число слов в предложении договор в моём архиве.", False),
        ("Проверь грамматику текста договор в моём архиве.", False),
        ("Прочитай задом наперёд текст договор в моём архиве.", False),
        ("Найди подлежащее в предложении договор в моём архиве.", False),
        ("Найди слово договор во фразе договор в моём архиве.", False),
        ("Какие слова во фразе что есть в моём архиве?", False),
        ("Сколько букв в предложении что есть в моём архиве?", False),
        ("Кто автор текста что есть в моём архиве?", False),
        ("Где ошибка в строке что есть в моём архиве?", False),
        ("Какая грамматика у фразы что есть в моём архиве?", False),
        ("Что означает предложение что есть в моём архиве?", False),
        ("Есть ли ошибка во фразе что есть в моём архиве?", False),
        ("Найди договор в моём архиве。 Это пример команды。", False),
        ("Найди договор в моём архиве\u2028Это пример команды.", False),
        ("Найди договор в моём архиве\u2029Это пример команды.", False),
        ("Найди договор в моём архиве\vЭто пример команды.", False),
        ("Найди договор в моём архиве\fЭто пример команды.", False),
        ("Что есть в моём архиве？ Это пример вопроса．", False),
        ("Найди договор в моём архиве ‐ это пример команды.", False),
        ("Найди договор в моём архиве ‑ это пример команды.", False),
        ("Найди договор в моём архиве ‒ это пример команды.", False),
        ("Найди договор в моём архиве – это пример команды.", False),
        ("Найди договор в моём архиве ― это пример команды.", False),
        ("Найди договор в моём архиве − это пример команды.", False),
        ("Найди договор в моём архиве ➖ это пример команды.", False),
        ("Найди договор в моём архиве ± это пример команды.", False),
        ("Найди договор в моём архиве § это пример команды.", False),
        ("Найди договор в моём архиве • это пример команды.", False),
        ("Найди договор в моём архиве → это пример команды.", False),
        ("Найди договор ➖ это пример команды в моём архиве.", False),
        ("Найди договор ± это пример команды в моём архиве.", False),
        ("Найди договор в моём архиве произвольный хвост.", False),
        ("Найди договор в моём архиве за вчера.", True),
        ("Что происходило в моём хранилище 7 мая 2024 года?", False),
        ("Расскажи, как найти договор в моём архиве.", False),
        ("Объясни, как искать в моём архиве.", False),
        ("Научи меня искать в моём архиве.", False),
        ("Как найти договор в моём архиве?", False),
        ("Где кнопка, чтобы найти договор в моём архиве?", False),
        ("Я сказал ему найти договор в моём архиве.", False),
        ("Мне посоветовали найти договор в моём архиве.", False),
        ("Он сказал мне: найди договор в моём архиве.", False),
        ("Он попросил её: найди договор в моём архиве.", False),
        ("Он спросил, есть договор в моём архиве?", False),
        ("Она сказала: хочу, чтобы ты нашла договор в моём архиве.", False),
        ("Мой коллега сказал: найди договор в моём архиве.", False),
        ("Вчера он сказал: найди договор в моём архиве.", False),
        ("По словам Ивана, найди договор в моём архиве.", False),
        ("Цитата Ивана: найди договор в моём архиве.", False),
        ("Иван написал мне найди договор в моём архиве.", False),
        ("Он сказал найди договор в моём архиве.", False),
        ("Если я скажу найди договор в моём архиве, что ты сделаешь?", False),
        ("Я не хочу, чтобы ты нашла договор в моём архиве.", False),
        ("Я не прошу найти договор в моём архиве.", False),
        ("Не выполняй команду найди договор в моём архиве.", False),
        ("Не исполняй команду найди договор в моём архиве.", False),
        ("Не надо выполнять команду найди договор в моём архиве.", False),
        ("Игнорируй команду найди договор в моём архиве.", False),
        ("Не следуй инструкции найди договор в моём архиве.", False),
        ("Не выполняй старую команду, а найди договор в моём архиве.", False),
        ("Игнорируй предыдущую инструкцию и найди договор в моём архиве.", False),
        ("Не следуй старой инструкции; найди договор в моём архиве.", False),
        ("Я не хочу ждать, хочу найти договор в моём архиве.", False),
        ("Я не прошу ждать, прошу найти договор в моём архиве.", False),
        ("Цитата Ивана: всё готово. Найди договор в моём архиве.", False),
        ("Иван сказал привет, а теперь найди договор в моём архиве.", False),
        ("По словам Ивана всё готово, а теперь найди договор в моём архиве.", False),
        (
            "Ищи где угодно, только не в моём архиве. А теперь найди договор в моём архиве.",
            False,
        ),
        (
            "Найди первый договор не в моём архиве. Второй найди в моём архиве.",
            False,
        ),
        ("`найди договор в моём архиве`", False),
        ("```text\nнайди договор в моём архиве\n```", False),
        ("> найди договор в моём архиве", False),
        ("Найти договор в моём архиве.", True),
        ("Попробуй найти договор в моём архиве.", True),
        ("Хочу, чтобы ты нашла договор в моём архиве.", True),
        ("Мне нужно, чтобы ты нашла договор в моём архиве.", True),
        ("Давай найдём договор в моём архиве.", True),
        ("Не могла бы ты найти договор в моём архиве?", True),
        ("В моём архиве есть договор?", True),
        ("Есть договор в моём архиве?", True),
        ("Хочу узнать, есть ли договор в моём архиве.", True),
        ("В моём архиве есть договор.", False),
        ("Ищи где угодно, только не в моём архиве.", False),
        ("Поищи договор, но не в моём архиве.", False),
        ("Найди договор — только не в моём личном архиве.", False),
        ("Найди не в моём архиве, а в интернете.", False),
        ("Найди в интернете, но не в моём архиве.", False),
        ("Найди в интернете сведения из моего архива.", False),
        ("Найди в моём архиве сообщения Ивана про Альфу.", False),
        ("Найди в моём архиве документы, которые загрузил Иван.", False),
        ("Найди документы, которые Артемьев прислал, в моём архиве.", False),
        ("Найди в моём архиве документы, которые иван загрузил.", False),
        ("Найди в моём архиве документы, которые пользователь Yato прислал.", False),
        ("Найди в моём архиве присланные Артемьевым документы.", False),
        ("Найди в моём архиве документы, загруженные Артемьевым.", False),
        ("Найди в моём архиве документы от @yato.", False),
        ("Найди в моём архиве документы пользователя Yato.", False),
        ("Найди в моём архиве материалы участника Артемьев.", False),
        ("Найди в моём архиве присланный Иваном договор.", False),
        ("Найди в моём архиве договор, присланный Иваном.", False),
        ("Найди в моём архиве загруженный вчера Иваном файл.", False),
        ("Найди в моём архиве то, что прислал Иван.", False),
        ("Найди в моём архиве то, что Иван загрузил.", False),
        ("Найди в моём архиве документы, полученные от Ивана.", False),
        ("Найди в моём архиве документы про пользователя Yato.", True),
        ("Найди в моём архиве договор про Ивана.", True),
        ("Найди в моём архиве упоминания участника Артемьев.", True),
        ("Найди в моём архиве договор с пользователем Yato.", True),
        ("Найди договор №42 в моём архиве.", True),
        ("Найди документы по C++ в моём архиве.", True),
        ("Найди договор (редакция 2024) в моём архиве.", True),
        ("Найди «Альфа» в моём архиве.", True),
        ("Найди цену $100 в моём архиве.", True),
        ("Найди отчёт ISO–9001 в моём архиве.", True),
    ],
)
def test_archive_current_text_classifier_requires_positive_generic_read(
    message: str,
    expected: bool,
) -> None:
    assert is_archive_search_current_text(message) is expected


_UNSAFE_UNICODE_DASHES = tuple(
    character
    for character in map(chr, range(0x110000))
    if character != "-" and unicodedata.category(character) == "Pd"
)


@pytest.mark.parametrize("separator", _UNSAFE_UNICODE_DASHES + ("\u2212",))
def test_archive_classifier_rejects_every_non_ascii_dash_separator(separator: str) -> None:
    assert not is_archive_search_current_text(f"Найди договор в моём архиве {separator} это пример команды.")


@pytest.mark.parametrize(
    "unsafe_character",
    _UNSAFE_UNICODE_DASHES
    + ("\u200b", "\u200e", "\u2066", "\ufeff", "\x01", "\x7f", "\ud800", "\ue000", "、", "؛", "【"),
    ids=lambda character: f"U+{ord(character):04X}",
)
def test_archive_safe_outbound_prefix_never_hides_unsafe_characters(
    unsafe_character: str,
) -> None:
    assert not is_archive_search_current_text(
        f"Не ищи{unsafe_character} в интернете, а найди договор в моём архиве."
    )
    assert not is_archive_search_current_text(
        f"Не ищи в интернете, а найди договор в моём архиве{unsafe_character}."
    )


@pytest.mark.parametrize(
    "unsafe_character",
    ("\x00", "\x1f", "\x7f", "\x85", "\u200b", "\u2060", "\ufeff", "\u202a"),
)
def test_archive_classifier_rejects_unicode_control_categories(unsafe_character: str) -> None:
    assert unicodedata.category(unsafe_character).startswith("C")
    assert not _archive_search_literal_shape_is_safe(unsafe_character)
    assert not is_archive_search_current_text(f"Найди договор в моём архиве{unsafe_character}.")


@pytest.mark.parametrize("terminal", ("。", "．", "！", "？", "：", "；", "…", "‥"))
def test_archive_classifier_rejects_non_ascii_terminal_punctuation(terminal: str) -> None:
    assert unicodedata.category(terminal).startswith("P")
    assert not is_archive_search_current_text(f"Найди договор в моём архиве{terminal} Это пример команды.")


def test_archive_classifier_keeps_machine_filename_punctuation_allowlist() -> None:
    assert is_archive_search_current_text("Найди Infrastructure/QNAP_v2-archive.txt в моём архиве.")


@pytest.mark.asyncio
async def test_current_text_archive_route_skips_prepare_context_and_persists_private_lineage(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )

    async def forbidden_prepare(*_args: Any, **_kwargs: Any) -> AgentContext:
        raise AssertionError("archive current text reached ambient context preparation")

    async def forbidden_arbiter(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("archive current text reached a semantic arbiter")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_office_intent_arbiter", forbidden_arbiter)
    storage.set_permission_override(_OWNER, "search.use", "deny")
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert "недоступ" in response["message"].casefold()
    context = contexts[0]
    assert context.archive_search_isolated_turn is True
    assert context.outward_verdict == ("архив", None)
    assert context.conversation_history == []
    assert context.ingestion == {}
    user_row = storage.get_message(context.source_search_lineage_user_message_id, _OWNER)
    assert user_row is not None
    raw_metadata = user_row.get("metadata_json")
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
    assert metadata.get("private_context_lineage") is True


@pytest.mark.asyncio
async def test_archive_with_current_attachment_rejects_before_any_source_read(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = _seed_document(storage, suffix="-mixed-carrier")
    runtime, kernel, actor, model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("mixed archive turn read or projected an attachment")

    async def forbidden_async(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("mixed archive turn reached an external source reader")

    monkeypatch.setattr(runtime, "_validated_current_attachment_ids", forbidden)
    monkeypatch.setattr(runtime, "_owned_file_attachment", forbidden)
    monkeypatch.setattr(runtime, "_verify_registered_file_attachments", forbidden_async)
    monkeypatch.setattr(runtime, "_hydrate_legacy_document_metadata", forbidden_async)
    monkeypatch.setattr(runtime, "_resolve_workspace_inbox_request", forbidden_async)
    monkeypatch.setattr("friday.agent_runtime._project_attachments_for_request", forbidden)

    try:
        response = await runtime.chat(
            _OWNER,
            "Найди контрольное значение в моём личном архиве.",
            actor=actor,
            enable_tools=True,
            attachments=[
                {
                    "raw_object_id": raw_id,
                    "filename": "mixed-private.txt",
                    "transient_text": "MIXED-ATTACHMENT-BODY-MUST-NOT-BE-READ",
                }
            ],
        )
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert response["files"] == [] and response["voice"] is None
    assert "отдельн" in response["message"].casefold()
    assert "MIXED-ATTACHMENT" not in json.dumps(response, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("search_denied", [False, True], ids=["available", "denied"])
async def test_current_archive_intent_overrides_history_aware_weather_policy(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    search_denied: bool,
) -> None:
    if not search_denied:
        _seed_document(storage, suffix="-weather-policy")
    runtime, kernel, actor, model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
    )
    if search_denied:
        storage.set_permission_override(_OWNER, "search.use", "deny")
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в моём личном архиве про погоду?",
            actor=actor,
            enable_tools=True,
            turn_policy=TurnPolicyDecision(
                intent=TurnIntent.WEATHER_NEEDS_LOCATION,
                public_response=WEATHER_LOCATION_CLARIFICATION,
            ),
        )
    finally:
        await web.close()

    assert response["message"] != WEATHER_LOCATION_CLARIFICATION
    if search_denied:
        assert model.calls == 0 and kernel.calls == []
        assert "недоступ" in response["message"].casefold()
    else:
        assert model.calls == 2
        assert [name for name, _arguments in kernel.calls] == ["archive_search"]


@pytest.mark.asyncio
async def test_exact_archive_bytes_are_admitted_and_committed_in_phase2_transaction(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage)
    runtime, kernel, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
    )
    committed_in_transaction: list[bool] = []
    from friday import agent_runtime as runtime_module

    original_store = runtime_module.store_message_in_transaction

    def observe_store(conn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        committed_in_transaction.append(bool(conn.in_transaction))
        return original_store(conn, *args, **kwargs)

    monkeypatch.setattr(runtime_module, "store_message_in_transaction", observe_store)
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert model.calls == 2
    assert all(call.get("require_full_context") is True for call in model.call_kwargs)
    assert "[A#]" in model.call_payloads[0] and "[A#.N]" in model.call_payloads[0]
    assert model.call_kwargs[0].get("tool_choice") == "archive_search"
    assert model.call_kwargs[1].get("tool_choice") is None
    assert model.second_round_tool_names == ["archive_search"]
    assert model.archive_tool_body.startswith("{") and model.archive_tool_body.endswith("}")
    assert (
        model.archive_tool_body.encode("ascii")
        == contexts[0].archive_prepared_searches[0].authorized_batch.model_visible_canonical_bytes
    )
    assert response["message"] == _ANSWER
    assert response["archive_search_authority_changed_before_publication"] is False
    assert response["voice"] is None, "archive-backed turns must not enter TTS before phase-2"
    assert committed_in_transaction == [True]
    assert contexts[0].archive_search_ledger_frozen is True
    _assert_archive_ledger_consumed(contexts[0])
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None and stored["content"] == _ANSWER
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.FEDERATED_SEARCH
    assert receipt.outcome.semantic_verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["2", "второй", "second"])
async def test_archive_candidate_offer_preserves_citation_order_and_replays_strict_ordinal(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    expected_offer = archive_candidate_selection_offer_suffix(("A2", "A1"))
    selected_body = _candidate_message_body(storage, created, 2)
    other_body = _CANDIDATE_SECOND_BODY if selected_body == _CANDIDATE_FIRST_BODY else _CANDIDATE_FIRST_BODY
    try:
        assert initial["message"].endswith(f"\n\n{expected_offer}")
        assert tuple(item.public_citation_label for item in created.candidate_set.candidates) == ("A2", "A1")
        assert created.candidate_set.candidates[1].public_citation_label == "A1"
        assert selected_body in {_CANDIDATE_FIRST_BODY, _CANDIDATE_SECOND_BODY}
        assert created.state is WorkState.WAITING_FOR_INPUT
        assert created.transition is WorkTransition.QUESTION_ASKED
        assert created.revision == 1
        assert runtime.owns_pending_durable_turn(
            _OWNER,
            selection,
            actor=actor,
            conversation_id=created.conversation_id,
        )

        replay = await runtime.chat(
            _OWNER,
            selection,
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
    finally:
        await web.close()

    assert selected_body in replay["message"]
    assert other_body not in replay["message"]
    assert replay["verified"] is True
    assert replay["context"]["archive_candidate_selection"] == "partial"
    assert replay["tools_used"] == []
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert not runtime.owns_pending_durable_turn(
        _OWNER,
        selection,
        actor=actor,
        conversation_id=created.conversation_id,
    )
    with storage.transaction() as conn:
        completed = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
        promoted = get_current_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert completed is not None
    assert completed.state is WorkState.COMPLETED
    assert completed.transition is WorkTransition.CANDIDATE_REPLAYED
    assert completed.revision == 2
    assert completed.question.selected_ordinal == 2
    assert promoted is not None
    assert promoted.id != completed.id
    assert promoted.state is WorkState.ACTIVE
    assert promoted.transition is WorkTransition.EVIDENCE_REPLAYED
    assert promoted.revision == 2
    assert promoted.anchor_user_message_id == completed.question.replay_boundary_user_message_id
    assert promoted.anchor_assistant_message_id == completed.question.replay_assistant_message_id
    assert promoted.selected_evidence == replace(
        completed.candidate_set.selected_evidence(2),
        work_item_id=promoted.id,
    )


@pytest.mark.asyncio
async def test_archive_candidate_stop_is_atomic_emergency_cancel_and_does_not_own_next_turn(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    cancellation_transactions: list[bool] = []
    original_cancel = agent_runtime_module.cancel_archive_candidate_selection_in_transaction

    def observe_cancel(conn: Any, **kwargs: Any) -> Any:
        cancellation_transactions.append(bool(conn.in_transaction))
        return original_cancel(conn, **kwargs)

    monkeypatch.setattr(
        agent_runtime_module,
        "cancel_archive_candidate_selection_in_transaction",
        observe_cancel,
    )
    before = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    try:
        stopped = await runtime.chat(
            _OWNER,
            "стоп",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
        next_model = _DirectAnswerModel()
        runtime.llm = next_model  # type: ignore[assignment]
        following = await runtime.chat(
            _OWNER,
            "Объясни кратко теорию множеств.",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
    finally:
        await web.close()

    assert stopped["message"] == "Молчу."
    assert stopped["voice"] is None
    assert stopped["tools_used"] == []
    assert cancellation_transactions == [True]
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert not runtime.owns_pending_durable_turn(
        _OWNER,
        "2",
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert following["message"] != archive_candidate_reask_prompt(2)
    assert next_model.calls == 1
    after = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    assert after == before + 4
    with storage.transaction() as conn:
        cancelled = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert cancelled is not None
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.transition is WorkTransition.CANCELLED
    assert cancelled.revision == 2
    assert cancelled.expires_at == created.expires_at


@pytest.mark.asyncio
async def test_archive_candidate_stop_rolls_back_both_rows_when_cancel_cas_fails(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )

    def fail_cancel(*_args: Any, **_kwargs: Any) -> Any:
        raise WorkItemConflictError("candidate stop lost its CAS race")

    monkeypatch.setattr(
        agent_runtime_module,
        "cancel_archive_candidate_selection_in_transaction",
        fail_cancel,
    )
    before = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    try:
        with pytest.raises(WorkItemConflictError):
            await runtime.chat(
                _OWNER,
                "стоп",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
    finally:
        await web.close()

    after = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    assert after == before
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        waiting = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert waiting is not None
    assert waiting.state is WorkState.WAITING_FOR_INPUT
    assert waiting.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_request", ["отмена", "cancel"])
async def test_archive_candidate_cancel_survives_restart_and_never_resurrects(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    cancel_request: str,
) -> None:
    (
        _initial,
        created,
        _runtime_instance,
        _kernel,
        _actor,
        _model,
        web,
    ) = await _create_durable_archive_candidate_work(settings, storage, monkeypatch)
    await web.close()
    restarted_storage = _reopen_storage(settings, storage)
    no_model = _DirectAnswerModel()
    try:
        restarted, kernel, actor, _model, restarted_web, _contexts = await _runtime(
            settings,
            restarted_storage,
            monkeypatch,
            model_override=no_model,
            archive_query=_CANDIDATE_QUERY,
        )
        try:
            cancelled_response = await restarted.chat(
                _OWNER,
                cancel_request,
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
            assert not restarted.owns_pending_durable_turn(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
            )
            following = await restarted.chat(
                _OWNER,
                "Объясни кратко теорию множеств.",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
            with restarted_storage.transaction() as conn:
                cancelled = get_archive_candidate_selection_work_item_in_transaction(
                    conn,
                    work_item_id=created.id,
                    user_id=_OWNER,
                    conversation_id=created.conversation_id,
                )
        finally:
            await restarted_web.close()
    finally:
        restarted_storage.close(final=True)

    assert cancelled_response["message"] == ARCHIVE_CANDIDATE_CANCELLED
    assert cancelled_response["tools_used"] == []
    assert cancelled_response["voice"] is None
    assert _CANDIDATE_QUERY not in json.dumps(cancelled_response, ensure_ascii=False)
    assert following["message"] not in {
        ARCHIVE_CANDIDATE_CANCELLED,
        archive_candidate_reask_prompt(2),
    }
    assert no_model.calls == 1
    assert kernel.calls == []
    assert cancelled is not None
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.transition is WorkTransition.CANCELLED
    assert cancelled.revision == 2
    assert cancelled.expires_at == created.expires_at


@pytest.mark.asyncio
async def test_expired_candidate_is_owned_source_free_after_restart_and_never_resurrects(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_now = candidate_store_module._now

    def historical_now(value: str | None) -> str:
        return original_now(value or "2000-01-01T00:00:00+00:00")

    monkeypatch.setattr(candidate_store_module, "_now", historical_now)
    (
        _initial,
        created,
        _runtime_instance,
        _kernel,
        _actor,
        _model,
        web,
    ) = await _create_durable_archive_candidate_work(settings, storage, monkeypatch)
    monkeypatch.setattr(candidate_store_module, "_now", original_now)
    await web.close()
    restarted_storage = _reopen_storage(settings, storage)
    no_model = _DirectAnswerModel()
    try:
        restarted, kernel, actor, _model, restarted_web, _contexts = await _runtime(
            settings,
            restarted_storage,
            monkeypatch,
            model_override=no_model,
            archive_query=_CANDIDATE_QUERY,
        )
        try:
            from friday.server import _pending_durable_turn_admission_before_ingestion

            admission = _pending_durable_turn_admission_before_ingestion(
                restarted,
                person_id=_OWNER,
                message="2",
                actor=actor,
                conversation_id=created.conversation_id,
            )
            assert admission is not False
            assert restarted.owns_pending_durable_turn(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
            )
            expired_response = await restarted.chat(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
                _pending_durable_admission=admission,
            )
            assert not restarted.owns_pending_durable_turn(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
            )
            following = await restarted.chat(
                _OWNER,
                "Объясни кратко теорию множеств.",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
            with restarted_storage.transaction() as conn:
                expired = get_archive_candidate_selection_work_item_in_transaction(
                    conn,
                    work_item_id=created.id,
                    user_id=_OWNER,
                    conversation_id=created.conversation_id,
                )
        finally:
            await restarted_web.close()
    finally:
        restarted_storage.close(final=True)

    assert expired_response["message"] == ARCHIVE_CANDIDATE_EXPIRED
    assert expired_response["tools_used"] == []
    assert expired_response["voice"] is None
    assert _CANDIDATE_QUERY not in json.dumps(expired_response, ensure_ascii=False)
    assert following["message"] not in {
        ARCHIVE_CANDIDATE_EXPIRED,
        archive_candidate_reask_prompt(2),
    }
    assert no_model.calls == 1
    assert kernel.calls == []
    assert expired is not None
    assert expired.state is WorkState.EXPIRED
    assert expired.transition is WorkTransition.EXPIRED
    assert expired.revision == 2


@pytest.mark.asyncio
async def test_emergency_stop_precedes_due_candidate_expiry_receipt(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_now = candidate_store_module._now

    def historical_now(value: str | None) -> str:
        return original_now(value or "2000-01-01T00:00:00+00:00")

    monkeypatch.setattr(candidate_store_module, "_now", historical_now)
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings, storage, monkeypatch
    )
    monkeypatch.setattr(candidate_store_module, "_now", original_now)
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "стоп",
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert admission is not False
    try:
        stopped = await runtime.chat(
            _OWNER,
            "стоп",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
            _pending_durable_admission=admission,
        )
    finally:
        await web.close()

    assert stopped["message"] == "Молчу."
    assert stopped["voice"] is None
    assert stopped["tools_used"] == []
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        expired = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert expired is not None
    assert expired.state is WorkState.EXPIRED
    assert expired.revision == 2


@pytest.mark.asyncio
async def test_emergency_stop_precedes_stale_bound_admission_after_completion(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "стоп",
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert admission is not False
    try:
        await runtime.chat(
            _OWNER,
            "1",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
        stopped = await runtime.chat(
            _OWNER,
            "стоп",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
            _pending_durable_admission=admission,
        )
    finally:
        await web.close()

    assert stopped["message"] == "Молчу."
    assert stopped["voice"] is None
    assert stopped["tools_used"] == []
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        completed = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert completed is not None
    assert completed.state is WorkState.COMPLETED
    assert completed.revision == 2
    assert completed.question.selected_ordinal == 1


@pytest.mark.asyncio
async def test_emergency_stop_ack_survives_terminal_cas_race_after_initial_lookup(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    original = runtime._archive_candidate_silence_response

    def cancel_after_initial_lookup(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with storage.transaction() as conn:
            cancel_archive_candidate_selection_in_transaction(
                conn,
                work_item_id=created.id,
                user_id=_OWNER,
                conversation_id=created.conversation_id,
                expected_revision=created.revision,
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        runtime,
        "_archive_candidate_silence_response",
        cancel_after_initial_lookup,
    )
    try:
        stopped = await runtime.chat(
            _OWNER,
            "стоп",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
    finally:
        await web.close()

    assert stopped["message"] == "Молчу."
    assert stopped["voice"] is None
    assert stopped["tools_used"] == []
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        cancelled = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert cancelled is not None
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.revision == 2


@pytest.mark.asyncio
async def test_bound_candidate_admission_race_never_replays_or_reaches_model_twice(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "2",
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert admission is not False
    try:
        first = await runtime.chat(
            _OWNER,
            "1",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
        stale = await runtime.chat(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
            _pending_durable_admission=admission,
        )
    finally:
        await web.close()

    assert first["verified"] is True
    assert stale["message"] == ARCHIVE_CANDIDATE_STALE
    assert stale["tools_used"] == []
    assert stale["voice"] is None
    assert _CANDIDATE_QUERY not in json.dumps(stale, ensure_ascii=False)
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        completed = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert completed is not None
    assert completed.state is WorkState.COMPLETED
    assert completed.revision == 2
    assert completed.question.selected_ordinal == 1


@pytest.mark.asyncio
async def test_stale_bound_admission_never_cancels_a_newer_reasked_revision(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "2",
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert admission is not False
    try:
        await runtime.chat(
            _OWNER,
            "не номер",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
        before = int(
            storage.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
                (created.conversation_id, _OWNER),
            ).fetchone()["count"]
        )
        with pytest.raises(WorkItemConflictError):
            await runtime.chat(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
                _pending_durable_admission=admission,
            )
    finally:
        await web.close()

    after = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    assert after == before
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        waiting = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert waiting is not None
    assert waiting.state is WorkState.WAITING_FOR_INPUT
    assert waiting.revision == 2


@pytest.mark.asyncio
async def test_competing_policy_surface_is_lazily_cancelled_after_publication_and_next_turn(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    try:
        policy = await runtime.chat(
            _OWNER,
            "Какая погода?",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
            turn_policy=TurnPolicyDecision(
                intent=TurnIntent.WEATHER_NEEDS_LOCATION,
                public_response=WEATHER_LOCATION_CLARIFICATION,
            ),
        )
        assert not runtime.owns_pending_durable_turn(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=created.conversation_id,
        )
        next_model = _DirectAnswerModel()
        runtime.llm = next_model  # type: ignore[assignment]
        following = await runtime.chat(
            _OWNER,
            "Объясни кратко теорию множеств.",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
    finally:
        await web.close()

    assert policy["message"] == WEATHER_LOCATION_CLARIFICATION
    assert following["message"] != archive_candidate_reask_prompt(2)
    assert next_model.calls == 1
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        cancelled = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert cancelled is not None
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.transition is WorkTransition.CANCELLED
    assert cancelled.revision == 2
    assert cancelled.expires_at == created.expires_at


@pytest.mark.asyncio
async def test_failed_competing_surface_keeps_candidate_current_until_successful_retry(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    original_store = runtime.storage.store_message

    def fail_policy_publication(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("policy publication failed")

    policy = TurnPolicyDecision(
        intent=TurnIntent.WEATHER_NEEDS_LOCATION,
        public_response=WEATHER_LOCATION_CLARIFICATION,
    )
    monkeypatch.setattr(runtime.storage, "store_message", fail_policy_publication)
    try:
        with pytest.raises(RuntimeError, match="policy publication failed"):
            await runtime.chat(
                _OWNER,
                "Какая погода?",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
                turn_policy=policy,
            )
        with storage.transaction() as conn:
            still_waiting = get_archive_candidate_selection_work_item_in_transaction(
                conn,
                work_item_id=created.id,
                user_id=_OWNER,
                conversation_id=created.conversation_id,
            )
        assert still_waiting is not None
        assert still_waiting.state is WorkState.WAITING_FOR_INPUT
        assert still_waiting.revision == 1
        assert runtime.owns_pending_durable_turn(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=created.conversation_id,
        )

        monkeypatch.setattr(runtime.storage, "store_message", original_store)
        retried = await runtime.chat(
            _OWNER,
            "Какая погода?",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
            turn_policy=policy,
        )
        with storage.transaction() as conn:
            displaced_waiting = get_archive_candidate_selection_work_item_in_transaction(
                conn,
                work_item_id=created.id,
                user_id=_OWNER,
                conversation_id=created.conversation_id,
            )
        assert displaced_waiting is not None
        assert displaced_waiting.state is WorkState.WAITING_FOR_INPUT
        observed_before = str(
            storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[
                "observed_at"
            ]
        )
        assert not runtime.owns_pending_durable_turn(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=created.conversation_id,
        )
        observed_after = str(
            storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[
                "observed_at"
            ]
        )
        assert observed_after == observed_before
        with storage.transaction() as conn:
            still_displaced = get_archive_candidate_selection_work_item_in_transaction(
                conn,
                work_item_id=created.id,
                user_id=_OWNER,
                conversation_id=created.conversation_id,
            )
        assert still_displaced is not None
        assert still_displaced.state is WorkState.WAITING_FOR_INPUT
        assert still_displaced.revision == 1

        next_model = _DirectAnswerModel()
        runtime.llm = next_model  # type: ignore[assignment]
        following = await runtime.chat(
            _OWNER,
            "Объясни кратко теорию множеств.",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
    finally:
        await web.close()

    assert retried["message"] == WEATHER_LOCATION_CLARIFICATION
    assert following["message"] != archive_candidate_reask_prompt(2)
    assert next_model.calls == 1
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        cancelled = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert cancelled is not None
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.revision == 2


@pytest.mark.asyncio
async def test_archive_candidate_invalid_and_out_of_range_replies_reask_without_second_calls(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    responses: list[dict[str, Any]] = []
    try:
        for invalid in ("источник A1, пожалуйста", "3"):
            response = await runtime.chat(
                _OWNER,
                invalid,
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
            responses.append(response)
            assert response["message"] == archive_candidate_reask_prompt(2)
            assert response["context"]["archive_candidate_selection"] == "waiting_for_input"
            assert runtime.owns_pending_durable_turn(
                _OWNER,
                invalid,
                actor=actor,
                conversation_id=created.conversation_id,
            )
    finally:
        await web.close()

    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    for response in responses:
        source_free = json.dumps(response, ensure_ascii=False)
        assert _CANDIDATE_QUERY not in source_free
        assert "A1" not in source_free
    with storage.transaction() as conn:
        waiting = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert waiting is not None
    assert waiting.state is WorkState.WAITING_FOR_INPUT
    assert waiting.transition is WorkTransition.QUESTION_REASKED
    assert waiting.revision == 3
    assert waiting.question.prompt_revision == 3


@pytest.mark.asyncio
async def test_archive_candidate_replays_ordinal_after_runtime_restart(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _initial,
        created,
        _runtime_instance,
        _kernel,
        _actor,
        _model,
        web,
    ) = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    await web.close()
    restarted_storage = _reopen_storage(settings, storage)
    no_model = _DirectAnswerModel()
    try:
        restarted, kernel, actor, _model, restarted_web, _contexts = await _runtime(
            settings,
            restarted_storage,
            monkeypatch,
            model_override=no_model,
            archive_query=_CANDIDATE_QUERY,
        )
        try:
            assert restarted.owns_pending_durable_turn(
                _OWNER,
                "2-й",
                actor=actor,
                conversation_id=created.conversation_id,
            )
            replay = await restarted.chat(
                _OWNER,
                "2-й",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
        finally:
            await restarted_web.close()
    finally:
        restarted_storage.close(final=True)

    assert replay["verified"] is True
    assert _CANDIDATE_QUERY in replay["message"]
    assert replay["tools_used"] == []
    assert no_model.calls == 0
    assert kernel.calls == []
    with storage.transaction() as conn:
        completed = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert completed is not None
    assert completed.state is WorkState.COMPLETED
    assert completed.question.selected_ordinal == 2


@pytest.mark.asyncio
async def test_locate_select_and_explain_document_survives_both_runtime_restarts(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        initial,
        created,
        _runtime_instance,
        initial_kernel,
        _actor,
        initial_model,
        web,
    ) = await _create_durable_archive_document_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    await web.close()
    conversation_id = created.conversation_id
    expected = created.candidate_set.selected_evidence(2)

    selection_storage = _reopen_storage(settings, storage)
    selection_model = _DirectAnswerModel()
    try:
        selection_runtime, selection_kernel, actor, _model, selection_web, _contexts = await _runtime(
            settings,
            selection_storage,
            monkeypatch,
            model_override=selection_model,
        )
        try:
            selection = await selection_runtime.chat(
                _OWNER,
                "второй",
                actor=actor,
                conversation_id=conversation_id,
                enable_tools=True,
                answer_with_voice=False,
            )
        finally:
            await selection_web.close()
        with selection_storage.transaction() as conn:
            completed = get_archive_candidate_selection_work_item_in_transaction(
                conn,
                work_item_id=created.id,
                user_id=_OWNER,
                conversation_id=conversation_id,
            )
            promoted = get_current_recall_selected_archive_evidence_work_item_in_transaction(
                conn,
                user_id=_OWNER,
                conversation_id=conversation_id,
            )
    finally:
        selection_storage.close(final=True)

    assert selection["verified"] is True
    assert selection_model.calls == 0
    assert selection_kernel.calls == []
    assert initial_model.calls == 2
    assert [name for name, _arguments in initial_kernel.calls] == ["archive_search"]
    assert completed is not None
    assert completed.state is WorkState.COMPLETED
    assert completed.transition is WorkTransition.CANDIDATE_REPLAYED
    assert completed.question.selected_ordinal == 2
    assert promoted is not None
    assert promoted.state is WorkState.ACTIVE
    assert promoted.transition is WorkTransition.EVIDENCE_REPLAYED
    assert promoted.revision == 2
    assert promoted.selected_evidence == replace(expected, work_item_id=promoted.id)
    assert promoted.selected_evidence.origin_boundary_user_message_id == (
        created.candidate_set.origin_boundary_user_message_id
    )
    assert promoted.anchor_user_message_id == completed.question.replay_boundary_user_message_id
    assert promoted.anchor_assistant_message_id == completed.question.replay_assistant_message_id

    explanation_storage = _reopen_storage(settings, storage)
    ordinary_model = _DirectAnswerModel()
    explanation_model = _SelectedArchiveExplanationModel()
    try:
        explanation_runtime, explanation_kernel, actor, _model, explanation_web, _contexts = await _runtime(
            settings,
            explanation_storage,
            monkeypatch,
            model_override=ordinary_model,
        )
        explanation_runtime.settings = replace(
            explanation_runtime.settings,
            router_mode="v12",
            router_canary_routes=("archive_read",),
        )
        explanation_runtime._selected_archive_model = explanation_model
        planner = _NeverPlanner()
        orchestrated = OrchestrationRouter(
            explanation_runtime,
            planner,
            mode="v12",
            allowed_routes=("archive_read",),
        )
        question = "Что сказано в выбранном документе?"
        admission = orchestrated.pending_durable_turn_admission(
            _OWNER,
            question,
            actor=actor,
            conversation_id=conversation_id,
        )
        assert isinstance(admission, PendingDurableTurnAdmission)
        assert admission.work_item_id == promoted.id
        assert admission.revision == promoted.revision
        try:
            explanation = await orchestrated.chat(
                _OWNER,
                question,
                actor=actor,
                conversation_id=conversation_id,
                enable_tools=True,
                answer_with_voice=False,
                _pending_durable_admission=admission,
            )
        finally:
            await explanation_web.close()
        stored = explanation_storage.get_message(str(explanation["message_id"]), _OWNER)
        assert stored is not None
        receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
        with explanation_storage.transaction() as conn:
            advanced = get_recall_selected_archive_evidence_work_item_in_transaction(
                conn,
                work_item_id=promoted.id,
                user_id=_OWNER,
                conversation_id=conversation_id,
            )
            open_count = conn.execute(
                """SELECT COUNT(*) FROM work_items
                     WHERE user_id=? AND conversation_id=?
                       AND state IN ('active','waiting_for_input')""",
                (_OWNER, conversation_id),
            ).fetchone()[0]
    finally:
        explanation_storage.close(final=True)

    assert explanation["message"].endswith(explanation_model.answer)
    assert "Охват исходного поиска был частичным" in explanation["message"]
    assert explanation["citations"] == [{"label": "A1.1"}]
    assert len(explanation_model.calls) == 2
    assert ordinary_model.calls == 0
    assert planner.calls == 0
    assert explanation_kernel.calls == []
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_EXPLANATION
    assert receipt.outcome.semantic_verified is True
    assert advanced is not None
    assert advanced.revision == promoted.revision + 1
    assert advanced.selected_evidence == promoted.selected_evidence
    assert open_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        pytest.param("denied", ArchiveRecallStatus.DENIED, id="denied"),
        pytest.param("drifted", ArchiveRecallStatus.DRIFTED, id="drifted"),
        pytest.param("unavailable", ArchiveRecallStatus.UNAVAILABLE, id="unavailable"),
    ],
)
async def test_archive_candidate_replay_failure_is_source_free_receipted_and_suspends(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_status: ArchiveRecallStatus,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    selected = created.candidate_set.selected_evidence(2)
    original = runtime._archive_candidate_evidence_replay_response
    late_mutations: list[str] = []

    def mutate_after_admission(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if failure == "denied":
            storage.set_permission_override(_OWNER, "search.use", "deny")
        elif failure == "drifted":
            assert storage.archive_conversation(
                selected.source_ref.canonical_object_id,
                _OWNER,
            )
        late_mutations.append(failure)
        return original(*args, **kwargs)

    if failure == "unavailable":

        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("resolver unavailable")

        monkeypatch.setattr(
            agent_runtime_module,
            "replay_archive_evidence_in_transaction",
            unavailable,
        )
    monkeypatch.setattr(
        runtime,
        "_archive_candidate_evidence_replay_response",
        mutate_after_admission,
    )
    try:
        replay = await runtime.chat(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=created.conversation_id,
            enable_tools=True,
        )
    finally:
        await web.close()

    assert late_mutations == [failure]
    assert replay["message"] == ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE
    assert replay["context"]["archive_candidate_selection"] == expected_status.value
    assert replay["context"]["selected_archive_evidence_replay"] == expected_status.value
    assert replay["tools_used"] == []
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    source_free = json.dumps(replay, ensure_ascii=False)
    assert _CANDIDATE_QUERY not in source_free
    assert selected.source_ref.canonical_object_id not in source_free
    stored = storage.get_message(str(replay["message_id"]), _OWNER)
    assert stored is not None and stored["content"] == ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE
    durable_source_free = json.dumps(stored, ensure_ascii=False)
    assert _CANDIDATE_QUERY not in durable_source_free
    assert selected.source_ref.canonical_object_id not in durable_source_free
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.status is expected_status
    assert receipt.outcome.selected_evidence is None
    with storage.transaction() as conn:
        suspended = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert suspended is not None
    assert suspended.state is WorkState.SUSPENDED
    assert suspended.transition is WorkTransition.SUSPENDED
    assert suspended.revision == 2
    assert suspended.question.failed_ordinal == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["не номер", "2"])
async def test_archive_candidate_rechecks_dialogue_mode_before_any_continuation_row(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    helper_name = (
        "_archive_candidate_evidence_replay_response" if reply == "2" else "_archive_candidate_reask_response"
    )
    original = getattr(runtime, helper_name)

    def change_mode_after_admission(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert storage.set_conversation_mode(created.conversation_id, _OWNER, "research")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, helper_name, change_mode_after_admission)
    before = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    try:
        with pytest.raises(WorkItemConflictError):
            await runtime.chat(
                _OWNER,
                reply,
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
    finally:
        await web.close()
    after = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    assert after == before
    assert not runtime.owns_pending_durable_turn(
        _OWNER,
        reply,
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]


@pytest.mark.asyncio
async def test_bound_candidate_admission_never_reaches_model_after_early_mode_drift(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "2",
        actor=actor,
        conversation_id=created.conversation_id,
    )
    assert admission is not False
    assert storage.set_conversation_mode(created.conversation_id, _OWNER, "research")
    before = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    try:
        with pytest.raises(WorkItemConflictError):
            await runtime.chat(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
                _pending_durable_admission=admission,
            )
    finally:
        await web.close()

    after = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    assert after == before
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        waiting = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert waiting is not None
    assert waiting.state is WorkState.WAITING_FOR_INPUT
    assert waiting.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["cancelled", "mutation_rollback"])
async def test_archive_candidate_replay_cas_and_mutation_failures_leave_no_turn_rows(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    _initial, created, runtime, kernel, actor, model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    expected_exception: type[Exception]
    if race == "cancelled":
        original = runtime._archive_candidate_evidence_replay_response

        def cancel_after_admission(*args: Any, **kwargs: Any) -> dict[str, Any]:
            with storage.transaction() as conn:
                cancel_archive_candidate_selection_in_transaction(
                    conn,
                    work_item_id=created.id,
                    user_id=_OWNER,
                    conversation_id=created.conversation_id,
                    expected_revision=created.revision,
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(
            runtime,
            "_archive_candidate_evidence_replay_response",
            cancel_after_admission,
        )
        expected_exception = WorkItemConflictError
    else:

        def fail_candidate_promotion(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("candidate promotion failed")

        monkeypatch.setattr(
            agent_runtime_module,
            "promote_archive_candidate_selection_in_transaction",
            fail_candidate_promotion,
        )
        expected_exception = RuntimeError

    before = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    try:
        with pytest.raises(expected_exception):
            await runtime.chat(
                _OWNER,
                "2",
                actor=actor,
                conversation_id=created.conversation_id,
                enable_tools=True,
            )
    finally:
        await web.close()
    after = int(
        storage.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (created.conversation_id, _OWNER),
        ).fetchone()["count"]
    )
    assert after == before
    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    with storage.transaction() as conn:
        current = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert current is not None
    assert current.state is (WorkState.CANCELLED if race == "cancelled" else WorkState.WAITING_FOR_INPUT)
    assert current.revision == (2 if race == "cancelled" else 1)


@pytest.mark.asyncio
async def test_archive_candidate_hook_is_exact_owner_conversation_and_live_scope(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initial, created, runtime, _kernel, actor, _model, web = await _create_durable_archive_candidate_work(
        settings,
        storage,
        monkeypatch,
    )
    storage.ensure_user("archive-runtime-foreign-owner", preset_key="owner")
    foreign_actor = AuthorizationService(storage).actor_for_user(
        "archive-runtime-foreign-owner",
        source="archive-candidate-hook-test",
    )
    foreign_conversation = storage.create_conversation(_OWNER, title="foreign scope")
    try:
        assert runtime.owns_pending_durable_turn(
            _OWNER,
            "anything",
            actor=actor,
            conversation_id=created.conversation_id,
        )
        assert not runtime.owns_pending_durable_turn(
            _OWNER,
            "2",
            actor=foreign_actor,
            conversation_id=created.conversation_id,
        )
        assert not runtime.owns_pending_durable_turn(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=str(foreign_conversation["id"]),
        )
        with storage.transaction() as conn:
            expire_archive_candidate_selection_in_transaction(
                conn,
                work_item_id=created.id,
                user_id=_OWNER,
                conversation_id=created.conversation_id,
                expected_revision=created.revision,
                now="2999-01-01T00:00:00+00:00",
            )
        assert not runtime.owns_pending_durable_turn(
            _OWNER,
            "2",
            actor=actor,
            conversation_id=created.conversation_id,
        )
    finally:
        await web.close()


def test_interaction_control_plane_and_runtime_cold_import_together() -> None:
    source_root = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
        (
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, sys.argv[1]); "
                "import friday.interaction_control_plane; "
                "import friday.agent_runtime"
            ),
            source_root,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-durable-restart",
    )
    fresh_process, storage = _fresh_interpreter_replay_after_clean_shutdown(
        settings,
        storage,
        created,
        request,
    )
    assert fresh_process["status"] == "exact"
    assert fresh_process["corpus"] == created.selected_evidence.corpus.value
    assert fresh_process["coverage_grade"] == created.selected_evidence.coverage_grade.value
    assert fresh_process["passage_count"] == len(created.selected_evidence.passage_refs)
    assert fresh_process["work_revision"] == created.revision
    assert len(fresh_process["model_visible_sha256"]) == 64
    assert not (set(fresh_process["model_visible_sha256"]) - set("0123456789abcdef"))
    no_model = _DirectAnswerModel()
    reopened = _reopen_storage(settings, storage)
    try:
        restarted, kernel, actor, _model, web, _contexts = await _runtime(
            settings,
            reopened,
            monkeypatch,
            model_override=no_model,
        )
        try:
            replay = await restarted.chat(
                _OWNER,
                "Что в нём сказано?",
                actor=actor,
                conversation_id=str(initial["conversation_id"]),
                enable_tools=True,
                answer_with_voice=False,
            )
        finally:
            await web.close()
    finally:
        reopened.close(final=True)

    body_row = storage.execute(
        "SELECT raw_content FROM raw_objects WHERE id=? AND user_id=?",
        (raw_id, _OWNER),
    ).fetchone()
    assert body_row is not None
    locator = created.selected_evidence.passage_refs[0].locator
    expected_passage = str(body_row["raw_content"])[locator.start_char : locator.end_char]  # type: ignore[union-attr]
    assert expected_passage and expected_passage in replay["message"]
    assert (
        replay["context"]["selected_archive_evidence_replay"]
        == created.selected_evidence.coverage_grade.value
    )
    assert replay["tools_used"] == []
    assert no_model.calls == 0
    assert kernel.calls == []

    with storage.transaction() as conn:
        advanced = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert advanced is not None
    assert advanced.state is WorkState.ACTIVE
    assert advanced.transition is WorkTransition.EVIDENCE_REPLAYED
    assert advanced.revision == created.revision + 1
    assert advanced.anchor_assistant_message_id == replay["message_id"]


@pytest.mark.asyncio
async def test_natural_selected_document_question_uses_bound_preingestion_v12_without_ordinary_paths(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-natural-bound-v12-explain",
    )
    question = "Какое контрольное значение указано в выбранном документе?"
    explanation_model = _SelectedArchiveExplanationModel()
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    planner = _NeverPlanner()
    orchestrated = OrchestrationRouter(
        runtime,
        planner,
        mode="v12",
        allowed_routes=("archive_read",),
    )
    messages_before_admission = tuple(
        item["id"]
        for item in storage.get_conversation_messages(
            str(initial["conversation_id"]),
            user_id=_OWNER,
        )
    )
    admission = orchestrated.pending_durable_turn_admission(
        _OWNER,
        question,
        actor=actor,
        conversation_id=str(initial["conversation_id"]),
    )
    assert isinstance(admission, PendingDurableTurnAdmission)
    assert admission.is_bound
    assert admission.work_item_id == created.id
    assert admission.revision == created.revision
    assert (
        tuple(
            item["id"]
            for item in storage.get_conversation_messages(
                str(initial["conversation_id"]),
                user_id=_OWNER,
            )
        )
        == messages_before_admission
    )

    try:
        response = await orchestrated.chat(
            _OWNER,
            question,
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
            _pending_durable_admission=admission,
        )
    finally:
        await web.close()

    assert response["message"].endswith(explanation_model.answer)
    assert response["citations"] == [{"label": "A1.1"}]
    assert response["tools_used"] == []
    assert response["context"]["selected_archive_evidence_explanation"] in {
        "complete",
        "partial",
    }
    assert len(explanation_model.calls) == 2
    assert "не делай вывод" in str(explanation_model.calls[0][0]["content"])
    assert "во всём источнике" in str(explanation_model.calls[0][0]["content"])
    assert question in str(explanation_model.calls[0][1]["content"])
    assert ordinary_model.calls == 0
    assert planner.calls == 0
    assert kernel.calls == []
    assert orchestrated.observations[-1].status == "durable_turn_owned"

    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_EXPLANATION
    assert receipt.outcome.semantic_verified is True
    assert receipt.outcome.used_citation_labels == ("A1.1",)
    accepted = receipt.outcome.selected_evidence
    assert accepted is not None
    assert accepted.source_ref == created.selected_evidence.source_ref
    assert accepted.passage_refs == created.selected_evidence.passage_refs
    assert accepted.resolved_snapshot_sha256 == created.selected_evidence.source_snapshot_sha256
    metadata = json.loads(str(stored["metadata_json"]))
    assert metadata["interaction_trace"]["budget"]["model_calls"] == 2
    with storage.transaction() as conn:
        advanced = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert advanced is not None
    assert advanced.revision == created.revision + 1
    assert advanced.selected_evidence == created.selected_evidence
    assert advanced.accepted_outcome_sha256 == receipt.outcome_sha256


@pytest.mark.asyncio
async def test_natural_selected_reference_with_current_attachment_stays_on_current_file_surface(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-natural-current-attachment",
    )
    current_marker = "CURRENT-FILE-CANARY-9184"
    current_text = f"Закрытый документ-natural-current-source. Контрольное значение: {current_marker}."
    current_ingestion = await IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
    ).ingest_file(
        _OWNER,
        None,
        current_text.encode(),
        filename="current-deictic.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": _OWNER},
        source_ref="telegram-file:natural-current-source",
    )
    current_raw_id = str(current_ingestion["raw_object_id"])
    question = "Какой срок указан в нём?"
    explanation_model = _SelectedArchiveExplanationModel()

    class _CurrentFileAnswerModel(_DirectAnswerModel):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            self.call_kwargs.append(dict(kwargs))
            assert current_marker in json.dumps(messages, ensure_ascii=False, sort_keys=True)
            return {
                "content": f"В текущем файле указано {current_marker}.",
                "tool_calls": None,
                "finish_reason": "stop",
            }

    ordinary_model = _CurrentFileAnswerModel()
    runtime, _kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime._selected_archive_model = explanation_model
    routed_messages: list[str] = []
    original_file_turn_authority = agent_runtime_module.file_turn_authority

    def observe_file_turn_authority(message: str) -> Any:
        routed_messages.append(message)
        return original_file_turn_authority(message)

    monkeypatch.setattr(agent_runtime_module, "file_turn_authority", observe_file_turn_authority)
    try:
        response = await runtime.chat(
            _OWNER,
            question,
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            attachments=[
                {
                    "raw_object_id": current_raw_id,
                    "filename": "current-deictic.txt",
                }
            ],
        )
    finally:
        await web.close()

    assert question in routed_messages
    assert current_marker in response["message"]
    assert response["context"].get("selected_archive_evidence_explanation") is None
    assert explanation_model.calls == []
    with storage.transaction() as conn:
        unchanged = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert unchanged == created


@pytest.mark.asyncio
async def test_natural_selected_reference_reply_keeps_reply_file_priority_and_work_item_unchanged(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-natural-reply-file",
    )
    current_marker = "REPLIED-FILE-CANARY-6157"
    current_text = f"Текущий файл содержит {current_marker}."
    current_ingestion = await IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
    ).ingest_file(
        _OWNER,
        None,
        current_text.encode(),
        filename="reply-current.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": _OWNER},
        source_ref="telegram-file:natural-reply-current",
    )

    class _ReplyFileAnswerModel(_DirectAnswerModel):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            self.call_kwargs.append(dict(kwargs))
            assert current_marker in json.dumps(messages, ensure_ascii=False, sort_keys=True)
            return {
                "content": f"Ответ из файла: {current_marker}.",
                "tool_calls": None,
                "finish_reason": "stop",
            }

    ordinary_model = _ReplyFileAnswerModel()
    explanation_model = _SelectedArchiveExplanationModel()
    runtime, _kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime._selected_archive_model = explanation_model
    try:
        upload = await runtime.chat(
            _OWNER,
            "Загружен документ: reply-current.txt",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            attachments=[{"raw_object_id": str(current_ingestion["raw_object_id"])}],
            synthetic_document_notice=True,
        )
        reply = await runtime.chat(
            _OWNER,
            "Какой код указан в нём?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            attachments=[{"raw_object_id": str(current_ingestion["raw_object_id"])}],
            reply_to=str(upload["message_id"]),
            quoted_attachment_reference=True,
            reply_assistant_reference=True,
            reply_assistant_message_id=str(upload["message_id"]),
        )
    finally:
        await web.close()

    assert current_marker in upload["message"]
    assert current_marker in reply["message"]
    assert reply["context"].get("selected_archive_evidence_explanation") is None
    assert explanation_model.calls == []
    with storage.transaction() as conn:
        unchanged = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert unchanged == created


@pytest.mark.asyncio
async def test_selected_archive_explain_uses_attested_two_pass_model_and_atomic_receipt(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-v12-explain",
    )
    explanation_model = _SelectedArchiveExplanationModel()
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "Что в нём сказано?",
        actor=actor,
        conversation_id=str(initial["conversation_id"]),
    )
    assert isinstance(admission, PendingDurableTurnAdmission)
    assert admission.work_item_id == created.id
    assert admission.revision == created.revision
    planner = _NeverPlanner()
    orchestrated = OrchestrationRouter(
        runtime,
        planner,
        mode="v12",
        allowed_routes=("archive_read",),
    )
    try:
        response = await orchestrated.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert response["message"].endswith(explanation_model.answer)
    assert "Охват исходного поиска был частичным" in response["message"]
    assert response["citations"] == [{"label": "A1.1"}]
    assert response["context"]["selected_archive_evidence_explanation"] in {
        "complete",
        "partial",
    }
    assert len(explanation_model.calls) == 2
    assert _QUERY in json.dumps(explanation_model.calls[0], ensure_ascii=False)
    assert ordinary_model.calls == 0
    assert planner.calls == 0
    assert orchestrated.observations[-1].status == "durable_turn_owned"
    assert kernel.calls == []

    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_EXPLANATION
    assert receipt.outcome.semantic_verified is True
    assert receipt.outcome.used_citation_labels == ("A1.1",)
    metadata = json.loads(str(stored["metadata_json"]))
    assert metadata["interaction_trace"]["budget"]["model_calls"] == 2
    assert metadata["structural"]["model_spoke"] is True
    with storage.transaction() as conn:
        advanced = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert advanced is not None
    assert advanced.revision == created.revision + 1
    assert advanced.accepted_outcome_sha256 == receipt.outcome_sha256


@pytest.mark.asyncio
async def test_selected_archive_explain_preserves_two_passage_identities_and_nested_citations(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_text = f"Первый выбранный фрагмент {_QUERY}: MULTI-FIRST-7421."
    second_text = f"Второй выбранный фрагмент {_QUERY}: MULTI-SECOND-7421."
    source_conversation_id, _selected_text = _seed_message_archive(
        storage,
        suffix="-v12-explain-multiple-passages",
        compact=True,
        selected_text=first_text,
    )
    storage.store_message(
        source_conversation_id,
        _OWNER,
        "assistant",
        second_text,
    )
    search_model = _ArchiveModel(
        first_arguments={"query": _QUERY, "corpora": ["messages"], "limit": 5},
        final_answer=("Первый выбранный факт подтверждён [A1.1]. Второй выбранный факт подтверждён [A1.2]."),
    )
    initial_runtime, initial_kernel, actor, _model, initial_web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=search_model,
    )
    try:
        initial = await _chat(
            initial_runtime,
            actor,
            answer_with_voice=False,
            message="Найди оба контрольных сообщения в моём личном архиве.",
        )
    finally:
        await initial_web.close()

    assert [name for name, _arguments in initial_kernel.calls] == ["archive_search"]
    assert initial_kernel.calls[0][1]["corpora"] == ["messages"]
    row = storage.execute(
        """SELECT id FROM work_items
             WHERE user_id=? AND conversation_id=?
               AND kind='recall_selected_archive_evidence'""",
        (_OWNER, str(initial["conversation_id"])),
    ).fetchone()
    assert row is not None
    with storage.transaction() as conn:
        created = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=str(row["id"]),
            user_id=_OWNER,
            conversation_id=str(initial["conversation_id"]),
        )
    assert created is not None
    assert created.selected_evidence.corpus.value == "messages"
    assert created.selected_evidence.source_ref.canonical_object_id == source_conversation_id
    selected_identities = tuple(
        passage.to_private_json() for passage in created.selected_evidence.passage_refs
    )
    assert len(selected_identities) == 2
    assert selected_identities == tuple(sorted(set(selected_identities)))

    explanation_model = _SelectedArchiveExplanationModel(
        answer=(
            "Первый выбранный фрагмент содержит MULTI-FIRST-7421 [A1.1]. "
            "Второй содержит MULTI-SECOND-7421 [A1.2]."
        )
    )
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, continuation_actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=continuation_actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert response["citations"] == [{"label": "A1.1"}, {"label": "A1.2"}]
    assert response["message"].endswith(explanation_model.answer)
    assert explanation_model.citation_labels == ("A1.1", "A1.2")
    assert tuple(item["label"] for item in explanation_model.evidence_fragments) == (
        "A1.1",
        "A1.2",
    )
    projected_text = "\n".join(item["text"] for item in explanation_model.evidence_fragments)
    assert "MULTI-FIRST-7421" in projected_text
    assert "MULTI-SECOND-7421" in projected_text
    assert len(explanation_model.calls) == 2
    assert ordinary_model.calls == 0
    assert kernel.calls == []

    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_EXPLANATION
    assert receipt.outcome.used_citation_labels == ("A1.1", "A1.2")
    accepted_selection = receipt.outcome.selected_evidence
    assert accepted_selection is not None
    assert accepted_selection.corpus.value == created.selected_evidence.corpus.value
    assert accepted_selection.source_ref == created.selected_evidence.source_ref
    assert accepted_selection.resolved_snapshot_sha256 == created.selected_evidence.source_snapshot_sha256
    assert (
        tuple(passage.to_private_json() for passage in accepted_selection.passage_refs) == selected_identities
    )
    with storage.transaction() as conn:
        advanced = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert advanced is not None
    assert advanced.selected_evidence == created.selected_evidence
    assert advanced.accepted_outcome_sha256 == receipt.outcome_sha256


@pytest.mark.asyncio
async def test_selected_archive_explain_verifier_failure_falls_back_to_exact_replay(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-v12-explain-fallback",
    )
    explanation_model = _SelectedArchiveExplanationModel(verifier_supported=False)
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    body_row = storage.execute(
        "SELECT raw_content FROM raw_objects WHERE id=? AND user_id=?",
        (raw_id, _OWNER),
    ).fetchone()
    assert body_row is not None
    locator = created.selected_evidence.passage_refs[0].locator
    expected_passage = str(body_row["raw_content"])[locator.start_char : locator.end_char]  # type: ignore[union-attr]
    assert response["message"].startswith("Не удалось сформировать проверенное объяснение")
    assert expected_passage in response["message"]
    assert response["context"]["selected_archive_evidence_explanation"] == "fallback_exact_replay"
    assert response["context"]["llm_failed"] is True
    assert len(explanation_model.calls) == 2
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY
    metadata = json.loads(str(stored["metadata_json"]))
    trace = metadata["interaction_trace"]
    assert trace["failure_stage"] == "completion"
    assert trace["failure_reason"] == "verification_rejected"
    assert trace["budget"]["model_calls"] == 2
    assert trace["completion"] == "partial"
    assert trace["partial_coverage"] is True
    assert {step["capability"]: step["outcome"] for step in trace["steps"]} == {
        "document_retrieval": "partial",
        "model_synthesis": "succeeded",
        "verification": "failed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_valid_checks", "expected_calls", "expected_checks", "verification_outcome"),
    ((1, 1, 2, "unavailable"), (2, 2, 3, "succeeded")),
)
async def test_selected_archive_explain_lease_drift_falls_back_before_publication(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    lease_valid_checks: int,
    expected_calls: int,
    expected_checks: int,
    verification_outcome: str,
) -> None:
    _raw_id, initial, _created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix=f"-v12-explain-lease-drift-{lease_valid_checks}",
    )
    explanation_model = _SelectedArchiveExplanationModel(
        lease_valid_checks=lease_valid_checks,
    )
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert len(explanation_model.calls) == expected_calls
    assert explanation_model.lease_checks == expected_checks
    assert response["message"].startswith("Не удалось сформировать проверенное объяснение")
    assert response["context"]["selected_archive_evidence_explanation"] == "fallback_exact_replay"
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY
    metadata = json.loads(str(stored["metadata_json"]))
    trace = metadata["interaction_trace"]
    assert trace["failure_stage"] == "state_loss"
    assert trace["failure_reason"] == "stale_state"
    assert trace["budget"]["model_calls"] == expected_calls
    assert trace["completion"] == "partial"
    assert trace["partial_coverage"] is True
    assert {step["capability"]: step["outcome"] for step in trace["steps"]} == {
        "document_retrieval": "partial",
        "model_synthesis": "succeeded",
        "verification": verification_outcome,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["answer", "lease-transplant"])
async def test_selected_archive_explanation_process_binding_rejects_mutation(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _raw_id, initial, _created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix=f"-v12-explain-process-binding-{mutation}",
    )
    explanation_model = _SelectedArchiveExplanationModel()
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    original_check = agent_runtime_module.selected_archive_explanation_lease_is_current

    async def mutate_before_publication(
        model: Any,
        explanation: Any,
        *,
        absolute_deadline: float,
    ) -> bool:
        if mutation == "answer":
            replacement_answer = "Подменённый после проверки ответ [A1.1]."
            with pytest.raises(RuntimeError, match="accepted explanation is invalid"):
                replace(explanation, answer=replacement_answer)
            object.__setattr__(
                explanation,
                "answer",
                replacement_answer,
            )
        else:
            replacement = await model.acquire_lease(
                explanation.requirements,
                absolute_deadline=absolute_deadline,
            )
            assert type(replacement) is ModelProfileLease
            with pytest.raises(RuntimeError, match="accepted explanation is invalid"):
                replace(explanation, lease=replacement)
            object.__setattr__(explanation, "lease", replacement)
        return await original_check(
            model,
            explanation,
            absolute_deadline=absolute_deadline,
        )

    monkeypatch.setattr(
        agent_runtime_module,
        "selected_archive_explanation_lease_is_current",
        mutate_before_publication,
    )
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert len(explanation_model.calls) == 2
    assert explanation_model.lease_checks == 2
    assert response["context"]["selected_archive_evidence_explanation"] == "fallback_exact_replay"
    assert "Подменённый после проверки" not in response["message"]
    assert ordinary_model.calls == 0
    assert kernel.calls == []


@pytest.mark.asyncio
async def test_selected_archive_epoch_loss_after_final_reauth_uses_exact_replay_without_stale_row(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, _created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-v12-explain-late-epoch-loss",
    )
    explanation_model = _SelectedArchiveExplanationModel()
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    original_replay = runtime._replay_selected_archive_evidence_in_transaction  # noqa: SLF001
    original_transaction = storage.transaction
    reauth_calls = 0
    lease_checks = 0
    process_lease_checks = 0
    rollback_exceptions: list[str] = []

    @contextmanager
    def observe_transaction_rollback():
        try:
            with original_transaction() as conn:
                yield conn
        except BaseException as exc:
            rollback_exceptions.append(type(exc).__name__)
            raise

    def observe_reauth(*args: Any, **kwargs: Any) -> Any:
        nonlocal reauth_calls
        reauth_calls += 1
        return original_replay(*args, **kwargs)

    async def observe_remote_lease(
        _model: Any,
        _explanation: Any,
        *,
        absolute_deadline: float,
    ) -> bool:
        nonlocal lease_checks
        lease_checks += 1
        assert absolute_deadline > agent_runtime_module.time.monotonic()
        assert reauth_calls == 1
        assert not storage.conn.in_transaction
        return True

    def reject_restarted_process_lease(
        _model: Any,
        _explanation: Any,
    ) -> bool:
        nonlocal process_lease_checks
        process_lease_checks += 1
        assert reauth_calls == 2
        assert storage.conn.in_transaction
        return process_lease_checks == 1

    monkeypatch.setattr(
        runtime,
        "_replay_selected_archive_evidence_in_transaction",
        observe_reauth,
    )
    monkeypatch.setattr(
        agent_runtime_module,
        "selected_archive_explanation_lease_is_current",
        observe_remote_lease,
    )
    monkeypatch.setattr(
        agent_runtime_module,
        "selected_archive_explanation_process_lease_is_current",
        reject_restarted_process_lease,
    )
    monkeypatch.setattr(storage, "transaction", observe_transaction_rollback)
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert lease_checks == 1
    assert process_lease_checks == 2
    assert rollback_exceptions == ["_SelectedArchiveLeasePublicationRollback"]
    assert explanation_model.lease_checks == 2
    assert len(explanation_model.calls) == 2
    assert response["context"]["selected_archive_evidence_explanation"] == "fallback_exact_replay"
    assert explanation_model.answer not in response["message"]
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    contents = tuple(
        str(row["content"])
        for row in storage.execute(
            "SELECT content FROM messages WHERE user_id=? AND conversation_id=? ORDER BY rowid",
            (_OWNER, str(initial["conversation_id"])),
        ).fetchall()
    )
    assert explanation_model.answer not in contents
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY


@pytest.mark.asyncio
async def test_selected_archive_explain_without_attested_model_falls_back_honestly(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, _created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-v12-explain-no-model",
    )
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    assert runtime._selected_archive_model is None
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert response["message"].startswith("Не удалось сформировать проверенное объяснение")
    assert response["context"]["selected_archive_evidence_explanation"] == "fallback_exact_replay"
    assert response["context"]["llm_failed"] is True
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    metadata = json.loads(str(stored["metadata_json"]))
    trace = metadata["interaction_trace"]
    assert trace["failure_stage"] == "synthesis_contradiction"
    assert trace["failure_reason"] == "provider_failure"
    assert trace["budget"]["model_calls"] == 0
    assert trace["completion"] == "partial"
    assert trace["partial_coverage"] is True
    assert {step["capability"]: step["outcome"] for step in trace["steps"]} == {
        "document_retrieval": "partial",
        "model_synthesis": "unavailable",
        "verification": "not_started",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_lease_error", "expected_reason"),
    [("timeout", "timeout"), ("provider", "provider_failure")],
)
async def test_selected_archive_explain_final_lease_failure_is_not_reported_as_drift(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    final_lease_error: str,
    expected_reason: str,
) -> None:
    _raw_id, initial, _created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix=f"-v12-explain-final-lease-{final_lease_error}",
    )
    explanation_model = _SelectedArchiveExplanationModel(final_lease_error=final_lease_error)
    runtime, kernel, actor, ordinary_model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=_DirectAnswerModel(),
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert len(explanation_model.calls) == 2
    assert explanation_model.lease_checks == 3
    assert response["context"]["selected_archive_evidence_explanation"] == "fallback_exact_replay"
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    metadata = json.loads(str(stored["metadata_json"]))
    trace = metadata["interaction_trace"]
    assert trace["failure_stage"] == "state_loss"
    assert trace["failure_reason"] == expected_reason
    assert trace["budget"]["model_calls"] == 2
    assert {step["capability"]: step["outcome"] for step in trace["steps"]} == {
        "document_retrieval": "partial",
        "model_synthesis": "succeeded",
        "verification": "succeeded",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["replay", "explanation"])
async def test_selected_archive_deadline_immediately_before_commit_rolls_back_publication(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    _raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix=f"-deadline-before-{lane}-commit",
    )
    explanation_model = _SelectedArchiveExplanationModel()
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    conversation_id = str(initial["conversation_id"])
    boundary_message_id = ""
    if lane == "replay":
        boundary = storage.store_message(
            conversation_id,
            _OWNER,
            "user",
            "Покажи фрагмент.",
            metadata={"private_context_lineage": True},
        )
        boundary_message_id = str(boundary["id"])

    def assistant_ids() -> tuple[str, ...]:
        return tuple(
            str(item["id"])
            for item in storage.get_conversation_messages(conversation_id, user_id=_OWNER)
            if item["role"] == "assistant"
        )

    assistants_before = assistant_ids()
    base = agent_runtime_module.time.monotonic()
    deadline = base + 30.0
    clock = {"now": base}
    original_store = agent_runtime_module.store_message_in_transaction

    def expire_after_assistant_store(
        conn: Any,
        stored_conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        stored = original_store(
            conn,
            stored_conversation_id,
            user_id,
            role,
            content,
            metadata=metadata,
            reply_to=reply_to,
        )
        if role == "assistant":
            clock["now"] = deadline
        return stored

    monkeypatch.setattr(
        agent_runtime_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"]),
    )
    monkeypatch.setattr(
        agent_runtime_module,
        "store_message_in_transaction",
        expire_after_assistant_store,
    )
    try:
        with pytest.raises(TimeoutError, match=rf"archive {lane} deadline expired before commit"):
            if lane == "explanation":
                await runtime.chat(
                    _OWNER,
                    "Что в нём сказано?",
                    actor=actor,
                    conversation_id=conversation_id,
                    enable_tools=True,
                    answer_with_voice=False,
                    turn_deadline=deadline,
                )
            else:
                runtime._selected_archive_evidence_replay_response(  # noqa: SLF001
                    actor=actor,
                    conversation_id=conversation_id,
                    person_id=_OWNER,
                    request="Покажи фрагмент.",
                    boundary_message_id=boundary_message_id,
                    admitted_work_item=created,
                    turn_started=base,
                    publication_deadline=deadline,
                )
    finally:
        await web.close()

    assert assistant_ids() == assistants_before
    assert len(explanation_model.calls) == (2 if lane == "explanation" else 0)
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    with storage.transaction() as conn:
        unchanged = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert unchanged is not None
    assert unchanged.revision == created.revision
    assert unchanged.anchor_user_message_id == created.anchor_user_message_id
    assert unchanged.anchor_assistant_message_id == created.anchor_assistant_message_id
    assert unchanged.accepted_plan_sha256 == created.accepted_plan_sha256
    assert unchanged.accepted_outcome_sha256 == created.accepted_outcome_sha256
    assert unchanged.selected_evidence == created.selected_evidence


@pytest.mark.asyncio
async def test_selected_archive_show_passages_never_invokes_explanation_model(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, _created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-show-passages-no-model",
    )
    explanation_model = _SelectedArchiveExplanationModel()
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    try:
        response = await runtime.chat(
            _OWNER,
            "Покажи фрагмент.",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert explanation_model.calls == []
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    assert response["context"]["selected_archive_evidence_explanation"] == "not_requested"
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY


@pytest.mark.asyncio
async def test_selected_archive_explain_rechecks_source_after_model_before_publication(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-v12-explain-late-drift",
    )

    def mutate_after_verifier() -> None:
        storage.execute(
            "UPDATE raw_objects SET raw_content=? WHERE id=? AND user_id=?",
            ("Поздно изменённое содержимое.", raw_id, _OWNER),
        )
        storage.conn.commit()

    explanation_model = _SelectedArchiveExplanationModel(after_verifier=mutate_after_verifier)
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    runtime.settings = replace(
        runtime.settings,
        router_mode="v12",
        router_canary_routes=("archive_read",),
    )
    runtime._selected_archive_model = explanation_model
    try:
        response = await runtime.chat(
            _OWNER,
            "Что в нём сказано?",
            actor=actor,
            conversation_id=str(initial["conversation_id"]),
            enable_tools=True,
            answer_with_voice=False,
        )
    finally:
        await web.close()

    assert len(explanation_model.calls) == 2
    assert ordinary_model.calls == 0
    assert kernel.calls == []
    assert response["context"]["selected_archive_evidence_replay"] == "drifted"
    assert response["context"]["selected_archive_evidence_explanation"] == "not_published"
    serialized = json.dumps(response, ensure_ascii=False)
    assert _QUERY not in serialized
    assert "Поздно изменённое" not in serialized
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    receipt = load_accepted_archive_recall_outcome_receipt(stored["metadata_json"])
    assert receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY
    assert receipt.outcome.status is ArchiveRecallStatus.DRIFTED
    with storage.transaction() as conn:
        suspended = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert suspended is not None
    assert suspended.state is WorkState.SUSPENDED


@pytest.mark.asyncio
async def test_mixed_deictic_capability_phrases_never_claim_selected_archive_work(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix="-mixed-deictic-capabilities",
    )
    ordinary_model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=ordinary_model,
    )
    planner = _NeverPlanner()
    orchestrated = OrchestrationRouter(
        runtime,
        planner,
        mode="v12",
        allowed_routes=("archive_read",),
    )
    mixed_requests = {
        "web": "Найди это в интернете и скажи, что там сказано.",
        "search": "Поищи это в моём архиве и объясни, что в нём сказано.",
        "obsidian": "Создай в Obsidian заметку об этом и укажи, что там сказано.",
        "effect": "Отправь это Артемьеву и объясни, что в нём сказано.",
        "question_then_web": "Какой срок указан в нём, и найди подтверждение в интернете?",
        "question_then_obsidian": "Что там написано про QNAP, и создай об этом заметку в Obsidian?",
        "question_then_effect": "Кто там упомянут, и отправь ему это сообщение?",
    }
    try:
        for capability, message in mixed_requests.items():
            assert (
                runtime.pending_durable_turn_admission(
                    _OWNER,
                    message,
                    actor=actor,
                    conversation_id=str(initial["conversation_id"]),
                )
                is False
            ), capability
            assert (
                orchestrated.pending_durable_turn_admission(
                    _OWNER,
                    message,
                    actor=actor,
                    conversation_id=str(initial["conversation_id"]),
                )
                is False
            ), capability
    finally:
        await web.close()

    assert ordinary_model.calls == 0
    assert planner.calls == 0
    assert kernel.calls == []
    with storage.transaction() as conn:
        unchanged = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert unchanged == created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revoked_permission", "expected_replay_status"),
    [
        pytest.param("search.use", "denied", id="search-denied"),
        pytest.param("conversations.read", "denied", id="corpus-denied"),
        pytest.param(None, "drifted", id="source-drifted"),
    ],
)
@pytest.mark.parametrize(
    "natural_question",
    [
        pytest.param("Что в нём сказано?", id="exact-document-reference"),
        pytest.param(
            "Какое контрольное значение указано в выбранном сообщении?",
            id="natural-content-reference",
        ),
    ],
)
async def test_selected_message_archive_evidence_replays_after_restart_then_fails_closed(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    revoked_permission: str | None,
    expected_replay_status: str,
    natural_question: str,
) -> None:
    source_conversation_id, selected_text = _seed_message_archive(
        storage,
        suffix="-durable-message-restart",
    )
    search_model = _ArchiveModel(
        first_arguments={"query": _QUERY, "corpora": ["messages"], "limit": 5},
    )
    initial_runtime, initial_kernel, actor, _model, initial_web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=search_model,
    )
    try:
        initial = await _chat(
            initial_runtime,
            actor,
            answer_with_voice=False,
            message="Найди сообщения с контрольным значением в моём личном архиве.",
        )
    finally:
        await initial_web.close()

    assert [name for name, _arguments in initial_kernel.calls] == ["archive_search"]
    assert initial_kernel.calls[0][1]["corpora"] == ["messages"]
    row = storage.execute(
        """SELECT id FROM work_items
             WHERE user_id=? AND conversation_id=?
               AND kind='recall_selected_archive_evidence'""",
        (_OWNER, str(initial["conversation_id"])),
    ).fetchone()
    assert row is not None
    with storage.transaction() as conn:
        created = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=str(row["id"]),
            user_id=_OWNER,
            conversation_id=str(initial["conversation_id"]),
        )
    assert created is not None
    assert created.selected_evidence.corpus.value == "messages"
    assert created.selected_evidence.source_ref.canonical_object_id == source_conversation_id
    assert created.state is WorkState.ACTIVE
    assert created.transition is WorkTransition.CREATED
    assert created.revision == 1
    fresh_process, storage = _fresh_interpreter_replay_after_clean_shutdown(
        settings,
        storage,
        created,
        request,
    )
    assert fresh_process["status"] == "exact"
    assert fresh_process["corpus"] == "messages"
    assert fresh_process["coverage_grade"] == created.selected_evidence.coverage_grade.value
    assert fresh_process["passage_count"] == len(created.selected_evidence.passage_refs)
    assert fresh_process["work_revision"] == created.revision
    assert len(fresh_process["model_visible_sha256"]) == 64
    assert not (set(fresh_process["model_visible_sha256"]) - set("0123456789abcdef"))

    late_text = f"Сообщение после исходной границы: {_QUERY}-LATE."
    storage.store_message(
        source_conversation_id,
        _OWNER,
        "assistant",
        late_text,
    )

    no_model = _DirectAnswerModel()
    replay_storage = _reopen_storage(settings, storage)
    try:
        restarted, replay_kernel, replay_actor, _model, replay_web, _contexts = await _runtime(
            settings,
            replay_storage,
            monkeypatch,
            model_override=no_model,
        )
        replay_authorize_calls: list[tuple[str, str]] = []
        assert replay_kernel.authorization is not None
        original_authorize = replay_kernel.authorization.authorize

        def observe_replay_authorize(fresh_actor: ActorContext, security_id: str) -> Any:
            replay_authorize_calls.append((fresh_actor.own_id, security_id))
            return original_authorize(fresh_actor, security_id)

        monkeypatch.setattr(replay_kernel.authorization, "authorize", observe_replay_authorize)
        try:
            replay = await restarted.chat(
                _OWNER,
                natural_question,
                actor=replay_actor,
                conversation_id=str(initial["conversation_id"]),
                enable_tools=True,
                answer_with_voice=False,
            )
        finally:
            await replay_web.close()
    finally:
        replay_storage.close(final=True)

    assert selected_text in replay["message"]
    assert late_text not in replay["message"]
    assert (
        replay["context"]["selected_archive_evidence_replay"]
        == created.selected_evidence.coverage_grade.value
    )
    assert replay["tools_used"] == []
    assert no_model.calls == 0
    assert replay_kernel.calls == []
    assert replay_authorize_calls[-2:] == [
        (_OWNER, "search.use"),
        (_OWNER, "conversations.read"),
    ]
    with storage.transaction() as conn:
        advanced = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert advanced is not None
    assert advanced.state is WorkState.ACTIVE
    assert advanced.transition is WorkTransition.EVIDENCE_REPLAYED
    assert advanced.revision == created.revision + 1
    assert advanced.anchor_assistant_message_id == replay["message_id"]
    assert advanced.selected_evidence == created.selected_evidence
    replay_stored = storage.get_message(str(replay["message_id"]), _OWNER)
    assert replay_stored is not None
    replay_receipt = load_accepted_archive_recall_outcome_receipt(replay_stored["metadata_json"])
    assert replay_receipt.outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY
    replay_selection = replay_receipt.outcome.selected_evidence
    assert replay_selection is not None
    assert replay_selection.source_ref == created.selected_evidence.source_ref
    assert replay_selection.passage_refs == created.selected_evidence.passage_refs
    assert replay_selection.resolved_snapshot_sha256 == created.selected_evidence.source_snapshot_sha256

    drift_model = _DirectAnswerModel()
    failure_storage = _reopen_storage(settings, storage)
    try:
        drifted_runtime, drift_kernel, drift_actor, _model, drift_web, _contexts = await _runtime(
            settings,
            failure_storage,
            monkeypatch,
            model_override=drift_model,
        )
        original_replay_response = drifted_runtime._selected_archive_evidence_replay_response
        late_mutations: list[str] = []

        def mutate_after_work_item_admission(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if revoked_permission is not None:
                failure_storage.set_permission_override(_OWNER, revoked_permission, "deny")
                late_mutations.append(revoked_permission)
            else:
                assert failure_storage.archive_conversation(source_conversation_id, _OWNER) is True
                late_mutations.append("source_archived")
            return original_replay_response(*args, **kwargs)

        monkeypatch.setattr(
            drifted_runtime,
            "_selected_archive_evidence_replay_response",
            mutate_after_work_item_admission,
        )
        try:
            drifted = await drifted_runtime.chat(
                _OWNER,
                "Покажи фрагмент.",
                actor=drift_actor,
                conversation_id=str(initial["conversation_id"]),
                enable_tools=True,
                answer_with_voice=False,
            )
        finally:
            await drift_web.close()
    finally:
        failure_storage.close(final=True)

    assert late_mutations == [revoked_permission or "source_archived"]
    assert drifted["context"]["selected_archive_evidence_replay"] == expected_replay_status
    source_free = json.dumps(drifted, ensure_ascii=False)
    assert selected_text not in source_free
    assert late_text not in source_free
    assert _QUERY not in source_free
    assert source_conversation_id not in source_free
    assert drifted["tools_used"] == []
    assert drift_model.calls == 0
    assert drift_kernel.calls == []
    stored_failure = storage.get_message(str(drifted["message_id"]), _OWNER)
    assert stored_failure is not None and stored_failure["content"] == drifted["message"]
    durable_failure = json.dumps(
        {
            "content": stored_failure["content"],
            "metadata_json": stored_failure["metadata_json"],
        },
        ensure_ascii=False,
    )
    assert selected_text not in durable_failure
    assert late_text not in durable_failure
    assert _QUERY not in durable_failure
    assert source_conversation_id not in durable_failure
    stored_failure_receipt = load_accepted_archive_recall_outcome_receipt(stored_failure["metadata_json"])
    assert stored_failure_receipt.outcome.status.value == expected_replay_status
    assert stored_failure_receipt.outcome.selected_evidence is None
    with storage.transaction() as conn:
        suspended = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert suspended is not None
    assert suspended.state is WorkState.SUSPENDED
    assert suspended.transition is WorkTransition.SUSPENDED
    assert suspended.revision == advanced.revision + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("denied", ArchiveRecallStatus.DENIED),
        ("drifted", ArchiveRecallStatus.DRIFTED),
    ],
)
async def test_selected_archive_replay_failure_is_source_free_and_suspends(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_status: ArchiveRecallStatus,
) -> None:
    raw_id, initial, created = await _create_durable_selected_archive_work(
        settings,
        storage,
        monkeypatch,
        suffix=f"-{failure}",
    )
    no_model = _DirectAnswerModel()
    reopened = _reopen_storage(settings, storage)
    try:
        restarted, kernel, actor, _model, web, _contexts = await _runtime(
            settings,
            reopened,
            monkeypatch,
            model_override=no_model,
        )
        original_replay_response = restarted._selected_archive_evidence_replay_response
        late_mutations: list[str] = []

        def mutate_after_work_item_admission(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if failure == "denied":
                reopened.set_permission_override(_OWNER, "knowledge.read", "deny")
            else:
                reopened.execute(
                    "UPDATE raw_objects SET raw_content=? WHERE id=? AND user_id=?",
                    ("Изменённое содержимое без исходного фрагмента.", raw_id, _OWNER),
                )
                reopened.conn.commit()
            late_mutations.append(failure)
            return original_replay_response(*args, **kwargs)

        monkeypatch.setattr(
            restarted,
            "_selected_archive_evidence_replay_response",
            mutate_after_work_item_admission,
        )
        try:
            replay = await restarted.chat(
                _OWNER,
                "Покажи фрагмент.",
                actor=actor,
                conversation_id=str(initial["conversation_id"]),
                enable_tools=True,
                answer_with_voice=False,
            )
        finally:
            await web.close()
    finally:
        reopened.close(final=True)

    assert late_mutations == [failure]
    assert replay["context"]["selected_archive_evidence_replay"] == expected_status.value
    source_free = json.dumps(replay, ensure_ascii=False)
    assert _QUERY not in source_free
    assert "Изменённое содержимое" not in source_free
    assert raw_id not in source_free
    assert replay["tools_used"] == []
    assert no_model.calls == 0
    assert kernel.calls == []
    stored_failure = storage.get_message(str(replay["message_id"]), _OWNER)
    assert stored_failure is not None and stored_failure["content"] == replay["message"]
    durable_failure = json.dumps(
        {
            "content": stored_failure["content"],
            "metadata_json": stored_failure["metadata_json"],
        },
        ensure_ascii=False,
    )
    assert _QUERY not in durable_failure
    assert "Изменённое содержимое" not in durable_failure
    assert raw_id not in durable_failure
    stored_failure_receipt = load_accepted_archive_recall_outcome_receipt(stored_failure["metadata_json"])
    assert stored_failure_receipt.outcome.status is expected_status
    assert stored_failure_receipt.outcome.selected_evidence is None
    with storage.transaction() as conn:
        suspended = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=created.id,
            user_id=_OWNER,
            conversation_id=created.conversation_id,
        )
    assert suspended is not None
    assert suspended.state is WorkState.SUSPENDED
    assert suspended.transition is WorkTransition.SUSPENDED
    assert suspended.revision == created.revision + 1


@pytest.mark.asyncio
async def test_incomplete_empty_archive_page_cannot_publish_confident_absence(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = "В моём архиве ничего нет."
    model = _ArchiveModel(final_answer=rejected)
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert response["message"] != rejected
    assert "отсутствие результатов не подтверждено" in response["message"]
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None and stored["content"] == response["message"]
    raw_metadata = stored.get("metadata_json")
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
    assert metadata["interaction_trace"]["partial_coverage"] is True


@pytest.mark.asyncio
async def test_documents_only_zero_never_claims_global_archive_absence_with_message_hit(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(_OWNER, preset_key="user")
    seeded_conversation = storage.create_conversation(_OWNER, title="cross-corpus hit")
    storage.store_message(
        str(seeded_conversation["id"]),
        _OWNER,
        "user",
        f"Сообщение другого корпуса содержит {_QUERY}",
    )
    global_absence = "По вашему запросу в личном архиве ничего не найдено."
    model = _ArchiveModel(
        final_answer=global_absence,
        first_arguments={"query": _QUERY, "corpora": ["documents"], "limit": 5},
    )
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert kernel.calls[0][1]["corpora"] == [
        "documents",
        "knowledge",
        "messages",
        "obsidian",
    ]
    assert response["message"] != global_absence
    assert (
        "проверенных в этом ходе разделах" in response["message"]
        or "отсутствие результатов не подтверждено" in response["message"]
        or "найдены результаты" in response["message"]
    )


def test_code_owned_archive_absence_allows_only_confirmed_exhaustive_zero() -> None:
    inactive = CapabilityStatus.INACTIVE
    confirmed = _ArchiveSearchPublicSummary(
        candidate_count=0,
        allowed_labels=frozenset(),
        authorized_absence_confirmed=True,
        exhaustive=True,
        partial_coverage=False,
        document_status=CapabilityStatus.EMPTY,
        message_status=inactive,
        obsidian_status=inactive,
    )
    content, replaced = _archive_search_semantic_content(
        confirmed,
        "Модель не смогла ответить.",
    )
    assert replaced is True
    assert "совпадений не найдено" in content
    assert "проверенных в этом ходе разделах" in content
    assert "по выполненному поисковому запросу" in content.casefold()
    assert "пределах применённых условий" in content

    evidence_found = replace(
        confirmed,
        candidate_count=1,
        allowed_labels=frozenset({"A1", "A1.1"}),
        authorized_absence_confirmed=False,
        document_status=CapabilityStatus.SUCCEEDED,
    )
    assert _archive_search_semantic_content(evidence_found, _ANSWER) == (_ANSWER, False)
    guarded, guarded_replaced = _archive_search_semantic_content(
        evidence_found,
        "В архиве ничего не найдено.",
    )
    assert guarded_replaced is True
    assert "найдены результаты" in guarded


@pytest.mark.parametrize(
    "claim",
    (
        "В моём архиве ничего не найдено [A1].",
        "Договор отсутствует [A1].",
        "Совпадений нет [A1].",
        "Результатов поиска нет [A1].",
        "Ни одного совпадения [A1].",
        "Не найден договор [A1].",
        "Договоров не обнаружено [A1].",
        "Нет совпадений [A1].",
        "Релевантные материалы отсутствуют [A1].",
        "Таких данных не оказалось [A1].",
        "По документам договор не найден [A1.1].",
        "По найденным документам договор не найден [A1.1].",
        "По материалам таких данных не оказалось [A1.1].",
        "Договора здесь нет [A1.1].",
        "Результаты отсутствуют [A1].",
        "Архив не содержит договора [A1.1].",
        "Мне не удалось обнаружить договор [A1.1].",
        "No contract was found in the archive [A1.1].",
        "No matching documents were found [A1].",
        "There are no results [A1].",
        "The archive contains no contract [A1.1].",
        "I found no relevant materials [A1.1].",
        "В источнике указано, что результатов поиска нет. [A1.1]",
        "The source states that there are no results. [A1.1]",
        "В документе написано: «Совпадений нет», поэтому в архиве договора нет [A1.1].",
        'The document states "No matches", therefore there is no contract in the archive [A1.1].',
        "В документе написано: «Совпадений нет». [A1.1]",
        'The document says: "No matching documents were found." [A1.1]',
        "I couldn't find any relevant documents [A1.1].",
        "I could not find any relevant documents [A1.1].",
        "Nothing was found in the archive [A1.1].",
        "Nothing relevant was found [A1.1].",
        "The search returned no results [A1.1].",
        "The contract is absent from the archive [A1.1].",
        "The archive does not contain a contract [A1.1].",
        "We did not find any matches [A1.1].",
        "Unable to find a contract [A1.1].",
        "The contract could not be found [A1.1].",
        "No relevant materials could be located [A1.1].",
        "The requested contract is absent [A1.1].",
        "The results are empty [A1.1].",
        "Не получилось найти договор [A1.1].",
        "Найти договор не удалось [A1.1].",
        "Поиск не вернул результатов [A1.1].",
        "Совпадений обнаружить не удалось [A1.1].",
        "Договор найти не удалось [A1.1].",
        "Искомого договора в архиве не оказалось [A1.1].",
        "Поиск не выявил договор [A1.1].",
        "Запрошенный договор найден не был [A1.1].",
        "No hits [A1.1].",
        "Zero results [A1.1].",
        "Search returned zero results [A1.1].",
        "Search came back empty [A1.1].",
        "Search failed to locate the contract [A1.1].",
        "Contract is not present [A1.1].",
        "Contract is missing [A1.1].",
        "We found nothing [A1.1].",
        "None of the documents matched [A1.1].",
        "No documents matched [A1.1].",
        "Zero results were returned [A1.1].",
        "Search found zero hits [A1.1].",
        "Archive empty [A1.1].",
        "Archive lacks a contract [A1.1].",
        "Search did not find any results [A1.1].",
        "I found nothing relevant [A1.1].",
        "Query produced zero matches [A1.1].",
        "Ноль совпадений [A1.1].",
        "Поиск пуст [A1.1].",
        "Поиск безрезультатен [A1.1].",
        "Договор не нашёлся [A1.1].",
        "Договор не присутствует [A1.1].",
        "Документов не имеется [A1.1].",
        "Ни один документ не подошёл [A1.1].",
        "Поиск результатов не дал [A1.1].",
        "Совпадений не было [A1.1].",
        "Поиск оказался пустым [A1.1].",
        "Договора в выдаче нет [A1.1].",
        "Архив пуст [A1.1].",
        "Не нашлось ни одного договора [A1.1].",
        "Договор не удалось отыскать [A1.1].",
        "No relevant records were located [A1.1].",
        "The requested item was not found [A1.1].",
        "We failed to retrieve the requested item [A1.1].",
        "The lookup yielded zero matches [A1.1].",
        "The archive has no matching record [A1.1].",
        "Запись не обнаружена [A1.1].",
        "Материал не найден [A1.1].",
        "Не удалось отыскать нужную запись [A1.1].",
        "Запрос ничего не выявил [A1.1].",
        "Выдача оказалась пустой [A1.1].",
        "Результатов нет [A1.1].",
        "Ни одной записи не найдено [A1.1].",
        "Nothing could be located [A1.1].",
        "We could not discover the requested entry [A1.1].",
        "The requested record was not located [A1.1].",
        "The requested record could not be retrieved [A1.1].",
        "The requested record does not exist [A1.1].",
        "The query has no matching record [A1.1].",
        "The search yielded nothing [A1.1].",
        "The results came up empty [A1.1].",
        "There were not any results [A1.1].",
        "Not a single match was returned [A1.1].",
        "The search was unsuccessful [A1.1].",
        "There is no contract in the archive [A1.1].",
        "No contract exists [A1.1].",
        "The contract does not exist [A1.1].",
        "The contract wasn't found [A1.1].",
        "The files weren't located [A1.1].",
        "The contract isn't present [A1.1].",
        "The requested item is unavailable [A1.1].",
        "The requested item is not available [A1.1].",
        "Nothing turned up [A1.1].",
        "No relevant entries surfaced [A1.1].",
        "Not one result was returned [A1.1].",
        "We did not get any results [A1.1].",
        "No matching source appears in the archive [A1.1].",
        "The archive contains nothing relevant [A1.1].",
        "No contract [A1.1].",
        "None were found [A1.1].",
        "None [A1.1].",
        "Nothing in the archive [A1.1].",
        "The query came up with nothing [A1.1].",
        "The search drew a blank [A1.1].",
        "The archive is devoid of the contract [A1.1].",
        "The query hasn't found it [A1.1].",
        "We haven't found it [A1.1].",
        "It was nowhere to be found [A1.1].",
        "Нужный файл не отыскался [A1.1].",
        "Нужная запись не существует [A1.1].",
        "Ни одной записи в выдаче [A1.1].",
        "Никаких совпадений [A1.1].",
        "Запрос оказался безрезультатным [A1.1].",
        "Выдача была пустой [A1.1].",
        "Архив не имеет нужной записи [A1.1].",
        "Поиск не принёс результатов [A1.1].",
        "Поиск завершился без совпадений [A1.1].",
        "Не нашли договор [A1.1].",
        "Мы не смогли найти договор [A1.1].",
        "Договор недоступен [A1.1].",
        "Искомая запись недоступна [A1.1].",
        "Нужная запись не доступна [A1.1].",
        "Ничего не отыскалось [A1.1].",
        "Совпадения не встретились [A1.1].",
        "Нужной записи в архиве не существует [A1.1].",
        "Ничего [A1.1].",
        "Ничего в архиве [A1.1].",
        "Без совпадений [A1.1].",
        "Найти не смогли [A1.1].",
    ),
)
def test_evidence_found_archive_rejects_common_broad_absence_claims(claim: str) -> None:
    summary = _ArchiveSearchPublicSummary(
        candidate_count=1,
        allowed_labels=frozenset({"A1", "A1.1"}),
        authorized_absence_confirmed=False,
        exhaustive=False,
        partial_coverage=True,
        document_status=CapabilityStatus.PARTIAL,
        message_status=CapabilityStatus.INACTIVE,
        obsidian_status=CapabilityStatus.INACTIVE,
    )
    guarded, replaced = _archive_search_semantic_content(summary, claim)
    assert replaced is True
    assert "найдены результаты" in guarded


def test_archive_document_scope_is_not_a_quoted_source_absence() -> None:
    assert _archive_definitive_absence_claim("По документам договор не найден [A1.1].")


@pytest.mark.parametrize(
    "supported_claim",
    (
        "I found a relevant document [A1.1].",
        "The contract is present in the archive [A1.1].",
        "The search returned three results [A1.1].",
        "The contract is not signed [A1.1].",
        "The archive is not empty [A1.1].",
        "The contract is not missing [A1.1].",
        "The contract was found and is not signed [A1.1].",
        "The document is missing a signature [A1.1].",
        "The archive does not lack the contract [A1.1].",
        "The search was not unsuccessful [A1.1].",
        "No later than 2024, the contract was signed [A1.1].",
        "No doubt the contract exists [A1.1].",
        "No more than three results were returned [A1.1].",
        "Not nothing was found [A1.1].",
        "Нашла договор в архиве [A1.1].",
        "Поиск вернул три результата [A1.1].",
        "Договор присутствует в архиве [A1.1].",
        "Договор не подписан [A1.1].",
        "Архив не пуст [A1.1].",
        "Договор не просрочен [A1.1].",
        "Запрос не безрезультатен [A1.1].",
        "Договор не отсутствует [A1.1].",
        "Материал доступен в архиве [A1.1].",
    ),
)
def test_archive_absence_guard_preserves_bounded_positive_claims(
    supported_claim: str,
) -> None:
    summary = _ArchiveSearchPublicSummary(
        candidate_count=1,
        allowed_labels=frozenset({"A1", "A1.1"}),
        authorized_absence_confirmed=False,
        exhaustive=False,
        partial_coverage=True,
        document_status=CapabilityStatus.PARTIAL,
        message_status=CapabilityStatus.INACTIVE,
        obsidian_status=CapabilityStatus.INACTIVE,
    )
    assert not _archive_definitive_absence_claim(supported_claim)
    assert _archive_search_semantic_content(summary, supported_claim) == (
        supported_claim,
        False,
    )


@pytest.mark.parametrize(
    "ungrounded",
    (
        "В архиве найден договор.",
        "В архиве найден договор [A999].",
    ),
)
def test_evidence_found_archive_requires_an_exact_admitted_label(ungrounded: str) -> None:
    summary = _ArchiveSearchPublicSummary(
        candidate_count=1,
        allowed_labels=frozenset({"A1", "A1.1"}),
        authorized_absence_confirmed=False,
        exhaustive=False,
        partial_coverage=True,
        document_status=CapabilityStatus.PARTIAL,
        message_status=CapabilityStatus.INACTIVE,
        obsidian_status=CapabilityStatus.INACTIVE,
    )
    guarded, replaced = _archive_search_semantic_content(summary, ungrounded)
    assert replaced is True
    assert "надёжно сформулировать" in guarded


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["search_revoked", "source_deleted"])
async def test_late_archive_denial_or_source_drift_consumes_and_fails_closed(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    raw_id = _seed_document(storage)

    def mutate() -> None:
        if mutation == "search_revoked":
            storage.set_permission_override(_OWNER, "search.use", "deny")
        else:
            with storage.transaction() as conn:
                changed = conn.execute(
                    "UPDATE raw_objects SET deleted_at='2026-08-23T17:35:00Z' WHERE id=?",
                    (raw_id,),
                )
            assert changed.rowcount == 1

    runtime, kernel, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        before_answer=mutate,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert model.calls == 2
    assert response["archive_search_authority_changed_before_publication"] is True
    assert response["voice"] is None
    assert response["files"] == []
    assert _QUERY not in json.dumps(response, ensure_ascii=False, sort_keys=True)
    assert contexts[0].archive_search_ledger_frozen is True
    _assert_archive_ledger_consumed(contexts[0])
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    assert _QUERY not in str(stored["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded", [False, True], ids=["zero-hit", "seeded-hit"])
async def test_late_archive_denial_durable_trace_is_existence_indistinguishable(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    seeded: bool,
) -> None:
    if seeded:
        _seed_document(storage, suffix="-trace-denial")

    def revoke() -> None:
        storage.set_permission_override(_OWNER, "search.use", "deny")

    runtime, _kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        before_answer=revoke,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    raw_metadata = stored.get("metadata_json")
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
    trace = metadata["interaction_trace"]
    assert trace["partial_coverage"] is False
    assert trace["budget"]["model_calls"] == 0
    assert trace["budget"]["capability_calls"] == 0
    assert trace["budget"]["latency_ms"] == 0
    assert response["tools_used"] == []
    source_capabilities = {
        str(step.get("capability") or "") for step in trace["steps"] if isinstance(step, dict)
    }
    assert source_capabilities.isdisjoint({"document_retrieval", "message_retrieval", "obsidian"})


@pytest.mark.asyncio
async def test_late_archive_denial_hides_multi_page_operational_shape(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage, suffix="-denied-page-1")
    _seed_document(storage, suffix="-denied-page-2")

    class _TwoPageThenRevoke:
        enabled = True
        model = "archive-two-page-denial-model"
        total_budget_sec = 3.0

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                arguments = {"query": _QUERY, "corpora": ["documents"], "limit": 1}
            elif self.calls == 2:
                pages = [
                    json.loads(str(item.get("content") or ""))
                    for item in messages
                    if item.get("role") == "tool"
                ]
                continuation = pages[-1].get("continuation")
                assert isinstance(continuation, str) and continuation
                arguments = {
                    "query": _QUERY,
                    "corpora": ["documents"],
                    "limit": 1,
                    "continuation": continuation,
                }
            else:
                storage.set_permission_override(_OWNER, "search.use", "deny")
                return {"content": _ANSWER, "tool_calls": None, "finish_reason": "stop"}
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"archive-page-{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "archive_search",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }

    model = _TwoPageThenRevoke()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert model.calls == 3
    assert [name for name, _arguments in kernel.calls] == ["archive_search", "archive_search"]
    assert response["archive_search_authority_changed_before_publication"] is True
    assert response["tools_used"] == []
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    raw_metadata = stored.get("metadata_json")
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
    trace = metadata["interaction_trace"]
    assert trace["partial_coverage"] is False
    assert trace["budget"]["model_calls"] == 0
    assert trace["budget"]["capability_calls"] == 0
    assert trace["budget"]["latency_ms"] == 0


@pytest.mark.asyncio
async def test_after_first_archive_page_only_a_cursor_continuation_can_run(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage)
    second_round_calls = [
        {
            "id": "forbidden-speak",
            "type": "function",
            "function": {
                "name": "speak",
                "arguments": json.dumps({"text": _ANSWER}),
            },
        },
        {
            "id": "fresh-search-without-cursor",
            "type": "function",
            "function": {
                "name": "archive_search",
                "arguments": json.dumps(
                    {
                        "query": "unrelated fresh query",
                        "corpora": ["documents"],
                    }
                ),
            },
        },
    ]
    runtime, kernel, actor, model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        second_round_calls=second_round_calls,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert model.calls == 3
    assert response["archive_search_authority_changed_before_publication"] is False
    assert response["voice"] is None


@pytest.mark.asyncio
async def test_copied_archive_json_without_typed_carrier_never_reaches_model(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _AdversarialArchiveModel({"query": _QUERY, "corpora": ["documents"], "limit": 5})
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
        kernel_factory=_CopiedPayloadKernel,
    )
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert _QUERY not in model.tool_body
    assert "непроверяем" in model.tool_body.casefold()
    assert contexts[0].archive_search_used is False
    assert contexts[0].archive_model_batch_ledger is None
    with pytest.raises(ArchiveSearchAuthorityError):
        create_archive_model_batch_ledger(
            tenant_id=_OWNER,
            principal_id=_OWNER,
            turn_discriminator=contexts[0].source_search_lineage_user_message_id,
        )
    assert response["archive_search_authority_changed_before_publication"] is False
    assert response["voice"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_argument",
    ["_archive_invocation", "actor", "turn_ledger"],
)
async def test_model_supplied_archive_authority_arguments_are_rejected_before_kernel(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    private_argument: str,
) -> None:
    model = _AdversarialArchiveModel(
        {
            "query": _QUERY,
            "corpora": ["documents"],
            private_argument: {"spoof": _QUERY},
        }
    )
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()

    assert kernel.calls == []
    assert "отклон" in model.tool_body.casefold()
    assert contexts[0].archive_model_batch_ledger is None
    assert contexts[0].archive_search_used is False
    assert response["archive_search_authority_changed_before_publication"] is False


@pytest.mark.asyncio
async def test_archive_turn_is_one_synthesis_pass_even_when_generic_verifier_is_enabled(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday import agent_runtime as runtime_module

    _seed_document(storage)
    runtime, _kernel, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        verify_answers=True,
    )
    try:
        response = await _chat(runtime, actor)
    finally:
        await web.close()

    assert response["message"] == _ANSWER
    assert model.calls == 2, "archive evidence must not enter judge/repair generations"
    exact_body = (
        contexts[0]
        .archive_prepared_searches[0]
        .authorized_batch.model_visible_canonical_bytes.decode("ascii")
    )
    assert model.archive_tool_body == exact_body
    assert all(exact_body not in payload for payload in model.call_payloads[2:])
    assert (
        runtime_module._secondary_tool_evidence(  # noqa: SLF001 - defense-in-depth seam
            {"tool": "archive_search", "output": exact_body},
            _QUERY,
        )
        == ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outward_kind", "message"),
    [
        ("архив", "Найди контрольное значение в моём личном архиве."),
        ("знание", "Найди контрольное значение в моём личном архиве."),
    ],
)
async def test_archive_intent_closes_web_before_first_model_result(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    outward_kind: str,
    message: str,
) -> None:
    model = _AdversarialArchiveModel(
        {"query": _QUERY},
        tool_name="web_search",
    )
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
        outward_kind=outward_kind,
    )
    leaked_queries: list[str] = []

    async def forbidden_web_search(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        leaked_queries.append(query)
        raise AssertionError("private archive query reached WebSurfer")

    monkeypatch.setattr(web, "search", forbidden_web_search)
    try:
        if outward_kind == "знание":
            context = AgentContext(
                conversation_id="archive-personal-wording-conversation",
                user_id=_OWNER,
                person_id=_OWNER,
                search_query=_QUERY,
                outward_verdict=(outward_kind, _QUERY),
                interaction_mode="dialogue",
            )
            response = await runtime._agentic_loop(  # noqa: SLF001 - first-token seam
                context,
                message,
                actor,
                kernel.get_tool_definitions(actor, topic=outward_kind),
                None,
            )
        else:
            response = await _chat(
                runtime,
                actor,
                answer_with_voice=False,
                message=message,
            )
    finally:
        await web.close()

    assert "archive_search" in model.first_offered_tool_names
    assert "web_search" not in model.first_offered_tool_names
    assert not any(name == "web_search" for name, _arguments in kernel.calls)
    assert leaked_queries == []
    assert _QUERY not in model.tool_body
    assert response.get("archive_search_authority_changed_before_publication", False) is False


@pytest.mark.asyncio
async def test_archive_backed_file_request_has_no_second_model_or_file_carrier(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage)
    runtime, kernel, actor, model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        verify_answers=True,
    )
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
        made = await runtime._file_for_a_request_that_wanted_one(  # noqa: SLF001
            "Оформи результат в документ PDF.",
            response["message"],
            actor,
            evidence=[
                {
                    "tool": "archive_search",
                    "output": model.archive_tool_body,
                }
            ],
            context=contexts[0],
        )
    finally:
        await web.close()

    assert model.calls == 2
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert made is None
    assert response["files"] == []
    assert response["voice"] is None


@pytest.mark.asyncio
async def test_web_first_then_archive_later_never_executes_or_discloses_to_web(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage)
    model = _WebThenArchiveModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    leaked_queries: list[str] = []

    async def forbidden_web_search(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        leaked_queries.append(query)
        raise AssertionError("archive query reached WebSurfer")

    monkeypatch.setattr(web, "search", forbidden_web_search)
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()

    assert model.first_offered_tool_names == ["archive_search"]
    assert model.calls == 3
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert leaked_queries == []
    assert response["message"] == _ANSWER


@pytest.mark.asyncio
async def test_direct_archive_answer_without_admitted_result_is_never_published(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()

    assert model.calls == 1
    assert model.call_kwargs[0]["tool_choice"] == "archive_search"
    assert model.call_kwargs[0]["require_full_context"] is True
    assert kernel.calls == []
    assert contexts[0].archive_search_used is False
    assert contexts[0].archive_model_batch_ledger is None
    assert _QUERY not in response["message"]
    assert "недоступ" in response["message"].casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    (
        "Найди контрольное значение в моём личном архиве.",
        "Попробуй найти договор в моём личном архиве.",
        "Есть договор в моём личном архиве?",
    ),
)
async def test_archive_intent_with_search_permission_denied_has_zero_model_or_outbound_calls(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    storage.set_permission_override(_OWNER, "search.use", "deny")
    leaked_queries: list[str] = []

    async def forbidden_web_search(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        leaked_queries.append(query)
        raise AssertionError("denied archive query reached WebSurfer")

    monkeypatch.setattr(web, "search", forbidden_web_search)
    try:
        response = await _chat(runtime, actor, answer_with_voice=False, message=message)
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert leaked_queries == []
    assert contexts[0].archive_search_isolated_turn is True
    assert _QUERY not in response["message"]
    assert response["voice"] is None
    assert response["files"] == []


@pytest.mark.asyncio
async def test_direct_agentic_archive_intent_without_schema_is_private_deterministic_denial(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    context = AgentContext(
        conversation_id="archive-no-schema-direct",
        user_id=_OWNER,
        person_id=_OWNER,
        outward_verdict=("архив", _QUERY),
    )
    tools = [
        item
        for item in kernel.get_tool_definitions(actor)
        if str((item.get("function") or {}).get("name") or "") != "archive_search"
    ]
    try:
        response = await runtime._agentic_loop(  # noqa: SLF001 - direct security seam
            context,
            "Найди значение в моём личном архиве.",
            actor,
            tools,
            None,
        )
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert context.archive_search_isolated_turn is True
    assert _QUERY not in str(response)


@pytest.mark.asyncio
async def test_mixed_obsidian_and_archive_request_runs_neither_route(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(
            runtime,
            actor,
            answer_with_voice=False,
            message=(
                "Найди контрольное значение в моём личном архиве и создай в Obsidian "
                "заметку Projects/Leak.md с результатом."
            ),
        )
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert len(contexts) == 1
    assert contexts[0].archive_search_isolated_turn is True
    assert "отдельн" in response["message"].casefold()
    assert response["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "answer_with_voice"),
    [
        ("Найди договор в моём архиве и создай файл report.pdf.", False),
        ("Найди договор в моём архиве и поставь напоминание завтра.", False),
        ("Сохрани X в моём архиве и покажи мои документы.", False),
        ("Что я писал про Альфу и проверь договор в моём архиве.", False),
        ("Найди договор в моём архиве.", True),
    ],
)
async def test_archive_read_with_second_effect_runs_no_model_or_tool(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    answer_with_voice: bool,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await _chat(
            runtime,
            actor,
            message=message,
            answer_with_voice=answer_with_voice,
        )
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert "отдельн" in response["message"].casefold()
    assert response["files"] == []
    assert response["voice"] is None


@pytest.mark.asyncio
async def test_archive_read_with_reply_quote_runs_no_model_or_tool(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _DirectAnswerModel()
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        response = await runtime.chat(
            _OWNER,
            "Найди договор в моём архиве.",
            actor=actor,
            enable_tools=True,
            answer_with_voice=False,
            reply_to="AMBIENT-REPLY-PRIVATE-CARRIER",
        )
    finally:
        await web.close()

    assert model.calls == 0
    assert kernel.calls == []
    assert "отдельн" in response["message"].casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Найди в моём личном архиве файл archive-runtime.txt.",
        "Покажи из моего архива файл archive-runtime.txt.",
        "Что в моём архиве в файле archive-runtime.txt?",
        "Найди archive-runtime.txt в моём личном архиве.",
        "Найди в моём личном архиве сообщения про проект Альфа.",
        "Покажи в моём личном архиве переписку про проект Альфа.",
        "Найди в моём личном архиве знания про проект Альфа.",
        "В моём архиве покажи все мои сообщения за вчера.",
        "Не ищи в интернете, найди договор в моём архиве.",
        "Не надо искать в интернете — покажи, что есть в моём архиве.",
        "Найди договор в моём архиве, но не ищи в интернете.",
        "Можешь ли найти договор в моём архиве?",
        "Попробуй найти договор в моём архиве.",
        "Хочу, чтобы ты нашла договор в моём архиве.",
        "Давай найдём договор в моём архиве.",
        "Не могла бы ты найти договор в моём архиве?",
        "В моём архиве есть договор?",
        "Есть договор в моём архиве?",
        "Хочу узнать, есть ли договор в моём архиве.",
    ],
)
async def test_archive_query_nouns_and_filenames_stay_on_facade(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    _seed_document(storage)
    runtime, kernel, actor, model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
    )
    try:
        response = await _chat(runtime, actor, message=message)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert model.calls == 2
    assert "недоступен" not in response["message"].casefold()
    assert "отдельном ходе" not in response["message"].casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_corpora"),
    [
        (
            "Найди договор в моём личном архиве.",
            ["documents", "knowledge", "messages", "obsidian"],
        ),
        ("Найди файл в моём личном архиве.", ["documents"]),
        (
            "Найди сообщения и знания в моём личном архиве.",
            ["knowledge", "messages"],
        ),
        ("Найди заметки в моём личном архиве.", ["obsidian"]),
        (
            "Найди договор в моём архиве, только не в сообщениях.",
            ["documents", "knowledge", "obsidian"],
        ),
        (
            "Найди документы в моём архиве, но не заметки.",
            ["documents"],
        ),
        (
            "Найди всё кроме сообщений в моём архиве.",
            ["documents", "knowledge", "obsidian"],
        ),
        (
            "Найди в моём архиве не сообщения, а документы.",
            ["documents"],
        ),
        (
            "Найди в моём архиве только документы, не сообщения.",
            ["documents"],
        ),
        ("Найди не старые сообщения в моём архиве.", ["messages"]),
        ("Найди не все сообщения в моём архиве.", ["messages"]),
        ("Найди не удалённые сообщения в моём архиве.", ["messages"]),
        ("Найди не более пяти сообщений в моём архиве.", ["messages"]),
        (
            "Найди в моём архиве не только сообщения, но и документы.",
            ["documents", "messages"],
        ),
    ],
)
async def test_archive_corpus_scope_is_code_owned_from_current_text(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_corpora: list[str],
) -> None:
    _seed_document(storage, suffix="-code-owned-scope")
    model = _ArchiveModel(
        first_arguments={"query": _QUERY, "corpora": ["documents"], "limit": 5},
    )
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    try:
        await _chat(runtime, actor, message=message)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert kernel.calls[0][1]["corpora"] == expected_corpora


@pytest.mark.parametrize(
    "message",
    [
        "Не забудь найти сообщения в моём архиве.",
        "Найди не старые сообщения в моём архиве.",
        "Найди не все сообщения в моём архиве.",
        "Найди не удалённые сообщения в моём архиве.",
        "Найди не более пяти сообщений в моём архиве.",
    ],
)
def test_archive_corpus_qualifier_is_not_misread_as_scope_exclusion(message: str) -> None:
    assert _archive_search_code_owned_corpora(message) == ("messages",)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Не ищи переписку; найди договор в моём архиве.",
            ("documents", "knowledge", "obsidian"),
        ),
        (
            "Не ищи в моём архиве сообщения, но найди документы в моём архиве.",
            ("documents",),
        ),
    ],
)
def test_archive_negated_corpus_scope_is_derived_fail_closed(
    message: str,
    expected: tuple[str, ...],
) -> None:
    assert _archive_search_code_owned_corpora(message) == expected


@pytest.mark.asyncio
async def test_explicit_uploader_scope_never_broadens_into_archive_facade(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _AdversarialArchiveModel(
        {"query": _QUERY, "corpora": ["documents"]},
    )
    runtime, kernel, actor, _model, web, _contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    context = AgentContext(
        conversation_id="archive-uploader-direct",
        user_id=_OWNER,
        person_id=_OWNER,
        outward_verdict=("архив", _QUERY),
    )
    try:
        await runtime._agentic_loop(  # noqa: SLF001 - current-text authority seam
            context,
            "Найди в моём архиве документы от пользователя Yato.",
            actor,
            kernel.get_tool_definitions(actor, topic="архив"),
            None,
        )
    finally:
        await web.close()

    assert "archive_search" not in model.first_offered_tool_names
    assert not any(name == "archive_search" for name, _arguments in kernel.calls)
    assert context.archive_search_used is False


@pytest.mark.asyncio
async def test_archive_isolation_removes_ambient_canaries_from_model_and_metadata(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient = "AMBIENT-PRIVATE-CANARY-9137"
    late = "LATE-AMBIENT-MUTATION-6274"
    context_box: list[AgentContext] = []

    def initialize(context: AgentContext) -> None:
        context_box.append(context)
        context.knowledge_hits = [{"id": "ko_ambient", "title": ambient, "content": ambient, "_score": 1.0}]
        context.entity_hits = [{"id": "entity_ambient", "name": ambient}]
        context.conversation_history = [
            {"role": "user", "content": ambient},
            {"role": "assistant", "content": ambient},
        ]
        context.reply_quote = ambient
        context.retrieval_trace = [{"title": ambient, "reason": ambient}]
        context.graph_context = {"paths": [{"label": ambient}], "entities": [{"name": ambient}]}
        context.proactive_suggestions = [ambient]
        context.feedback_summary = {"canary": ambient}
        context.document_metadata_evidence = ambient
        context.standing_rules = [ambient]
        context.corrections = [ambient]
        context.previous_user_turn = ambient
        context.previous_answer = ambient
        context.ingestion = {"action": "review", "canary": ambient}
        context.kb_size = 77
        context.entity_count = 66
        context.relation_count = 55
        context.pending_inbox = 44
        context.pending_resolutions = 33
        context.rerank_dropped = 22
        context.matched_at_least = 11
        context.retrieval_confidence = 0.99

    def mutate_after_admission() -> None:
        context = context_box[0]
        context.knowledge_hits = [{"id": "ko_late", "title": late, "content": late}]
        context.entity_hits = [{"name": late}]
        context.conversation_history = [{"role": "assistant", "content": late}]
        context.graph_context = {"paths": [{"label": late}]}
        context.standing_rules = [late]
        context.corrections = [late]
        context.feedback_summary = {"late": late}
        storage.set_permission_override(_OWNER, "web.research", "deny")

    _seed_document(storage)
    model = _ArchiveModel(before_answer=mutate_after_admission)
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
        context_initializer=initialize,
    )
    monkeypatch.setattr(runtime, "_user_model_payload", lambda _user_id: {"canary": ambient})
    monkeypatch.setattr(runtime, "_custom_instructions", lambda _user_id: [ambient])
    monkeypatch.setattr(runtime, "_standing_rules", lambda _user_id: [ambient])
    monkeypatch.setattr(runtime, "_corrections", lambda _user_id: [ambient])
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()

    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert response["message"] == _ANSWER
    assert all(ambient not in payload and late not in payload for payload in model.call_payloads)
    context = contexts[0]
    assert context.knowledge_hits == []
    assert context.entity_hits == []
    assert context.conversation_history == []
    assert context.graph_context == {}
    assert context.standing_rules == []
    assert context.corrections == []
    stored = storage.get_message(str(response["message_id"]), _OWNER)
    assert stored is not None
    durable = str(stored.get("metadata_json") or "")
    assert ambient not in durable and late not in durable


@pytest.mark.asyncio
async def test_cancellation_before_archive_admission_abandons_empty_turn_ledger(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ArchiveModel()
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
        kernel_factory=_CancelDuringArchiveKernel,
    )
    assert isinstance(kernel, _CancelDuringArchiveKernel)
    task = asyncio.create_task(_chat(runtime, actor, answer_with_voice=False))
    try:
        await asyncio.wait_for(kernel.archive_call_started.wait(), timeout=3.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await web.close()

    context = contexts[0]
    assert context.archive_search_used is False
    assert context.archive_model_batch_ledger is None
    with pytest.raises(ArchiveSearchAuthorityError):
        create_archive_model_batch_ledger(
            tenant_id=_OWNER,
            principal_id=_OWNER,
            turn_discriminator=context.source_search_lineage_user_message_id,
        )


@pytest.mark.asyncio
async def test_cancellation_after_archive_admission_consumes_turn_ledger(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage)
    model = _CancelAfterFirstPageModel()
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
    )
    task = asyncio.create_task(_chat(runtime, actor, answer_with_voice=False))
    try:
        await asyncio.wait_for(model.second_call_started.wait(), timeout=3.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await web.close()

    context = contexts[0]
    assert [name for name, _arguments in kernel.calls] == ["archive_search"]
    assert context.archive_search_used is True
    assert context.archive_search_ledger_frozen is True
    _assert_archive_ledger_consumed(context)


@pytest.mark.asyncio
async def test_real_router_preserves_two_exact_archive_pages_through_final_answer(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_document(storage, suffix="-page-1")
    _seed_document(storage, suffix="-page-2")
    page_two_answer = f"Во второй странице найдено значение {_QUERY} [A21.1]."
    router = LLMRouter(replace(settings, llm_enabled=True))
    payloads: list[dict[str, Any]] = []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            return False

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            payload = kwargs.get("json")
            assert isinstance(payload, dict)
            payloads.append(payload)
            call_number = len(payloads)
            message: dict[str, Any]
            if call_number == 1:
                message = {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "router-archive-page-1",
                            "type": "function",
                            "function": {
                                "name": "archive_search",
                                "arguments": json.dumps(
                                    {"query": _QUERY, "corpora": ["documents"], "limit": 1}
                                ),
                            },
                        }
                    ],
                }
                finish_reason = "tool_calls"
            elif call_number == 2:
                first_pages = [
                    json.loads(str(item.get("content") or ""))
                    for item in payload.get("messages", [])
                    if item.get("role") == "tool"
                ]
                assert len(first_pages) == 1
                continuation = first_pages[0].get("continuation")
                assert isinstance(continuation, str) and continuation
                message = {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "router-archive-page-2",
                            "type": "function",
                            "function": {
                                "name": "archive_search",
                                "arguments": json.dumps(
                                    {
                                        "query": _QUERY,
                                        "corpora": ["documents"],
                                        "limit": 1,
                                        "continuation": continuation,
                                    }
                                ),
                            },
                        }
                    ],
                }
                finish_reason = "tool_calls"
            else:
                message = {"content": page_two_answer, "tool_calls": None}
                finish_reason = "stop"
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "model": router.model,
                    "choices": [{"message": message, "finish_reason": finish_reason}],
                    "usage": {},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=router,
    )
    try:
        response = await _chat(runtime, actor, answer_with_voice=False)
    finally:
        await web.close()

    assert response["message"] == page_two_answer
    assert [name for name, _arguments in kernel.calls] == ["archive_search", "archive_search"]
    assert len(payloads) == 3
    assert payloads[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "archive_search"},
    }
    prepared = contexts[0].archive_prepared_searches
    assert len(prepared) == 2
    exact_pages = [item.authorized_batch.model_visible_canonical_bytes.decode("ascii") for item in prepared]
    public_pages = [json.loads(item) for item in exact_pages]
    assert public_pages[0]["candidates"][0]["label"] == "A1"
    assert public_pages[0]["candidates"][0]["passages"][0]["label"] == "A1.1"
    assert public_pages[1]["candidates"][0]["label"] == "A21"
    assert public_pages[1]["candidates"][0]["passages"][0]["label"] == "A21.1"
    summary = _archive_search_public_summary(prepared)
    assert {"A1", "A1.1", "A21", "A21.1"}.issubset(summary.allowed_labels)
    assert _archive_search_semantic_content(summary, page_two_answer) == (
        page_two_answer,
        False,
    )
    wrong_page, replaced = _archive_search_semantic_content(
        summary,
        f"Во второй странице найдено значение {_QUERY} [A2.1].",
    )
    assert replaced is True
    assert "надёжно сформулировать" in wrong_page
    for payload_index, expected_count in ((1, 1), (2, 2)):
        transmitted = [
            str(item.get("content") or "")
            for item in payloads[payload_index]["messages"]
            if item.get("role") == "tool"
        ]
        assert transmitted == exact_pages[:expected_count]


@pytest.mark.asyncio
@pytest.mark.parametrize("outward_kind", ["", "знание", "архив"])
async def test_archive_call_without_current_turn_archive_authority_is_rejected(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    outward_kind: str,
) -> None:
    model = _AdversarialArchiveModel(
        {"query": _QUERY, "corpora": ["documents"]},
    )
    runtime, kernel, actor, _model, web, contexts = await _runtime(
        settings,
        storage,
        monkeypatch,
        model_override=model,
        outward_kind=outward_kind,
    )
    try:
        response = await _chat(
            runtime,
            actor,
            answer_with_voice=False,
            message="А что там?",
        )
    finally:
        await web.close()

    assert "archive_search" not in model.first_offered_tool_names
    assert kernel.calls == []
    assert contexts[0].archive_model_batch_ledger is None
    assert contexts[0].archive_search_used is False
    assert response["archive_search_authority_changed_before_publication"] is False
