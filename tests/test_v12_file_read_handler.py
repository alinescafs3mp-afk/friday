from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.execution_kernel import track_request_effects
from friday.file_evidence import stamp_current_turn_file_reference
from friday.interaction_control_plane import (
    CapabilityClass,
    CompletionDecision,
    CountAccounting,
    IntentClass,
    PublicationStatus,
    TokenAccounting,
    TurnTrace,
)
from friday.interaction_control_plane.runtime_trace import INTERACTION_TRACE_METADATA_KEY
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration import (
    OrchestrationRouter,
    ReadOnlyAttachmentReference,
    ReadOnlyRouteRequest,
    RouteClass,
    TurnInput,
    TurnPlan,
)
from friday.orchestration.capability_outcome import CapabilityOutcome, CapabilityOutcomeStatus
from friday.orchestration.file_read import V12FileReadError, V12FileReadHandler
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import raw_source_identity_sha256
from friday.storage.models import RawObject, new_id


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="v12-file-handler-test")


def _plan() -> TurnPlan:
    return TurnPlan.parse(
        {
            "schema": "friday.turn-plan.v1",
            "route": "file_read",
            "objective": "Сравнить сведения в приложенных файлах",
            "evidence_requests": [{"kind": "attached_files", "query": "", "max_items": 2, "required": True}],
            "tool_intents": [],
            "output": {
                "format": "text",
                "language": "ru",
                "require_citations": True,
                "one_message": True,
            },
            "confidence": 0.99,
            "fallback": "legacy",
            "reason_code": "current_files",
        }
    )


def _text_digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _register(
    storage: Any,
    settings: Any,
    *,
    text: str,
    filename: str,
    ordinal: int = 1,
) -> ReadOnlyAttachmentReference:
    storage.ensure_user("alice", preset_key="owner")
    content = text.encode()
    digest = hashlib.sha256(content).hexdigest()
    relative = f"alice/{digest[:2]}/{digest}.txt"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "mime_type": "text/plain",
            "stored_path": relative,
            "sha256": digest,
            "size_bytes": len(content),
            "uploaded_by": "alice",
            "extraction_receipt_version": 1,
            "extraction_success": True,
            "extraction_error": "",
            "text_extraction_success": True,
            "text_sha256": _text_digest(text),
            "extraction_chars": len(text),
            "text_truncated": False,
            "archive_truncated": False,
            "source_truncated_for_parse": False,
            "parse_deadline_reached": False,
            "parse_pages_read": 0,
            "parse_pages_truncated": False,
            "parse_total_pages": 0,
            "vision_pages_total": 0,
            "vision_pages_read": 0,
            "archive_files": 0,
            "archive_files_read": 0,
            "vision_used": False,
            "vision_review_required": False,
            "unsupported_format": False,
        },
    )
    storage.store_raw_object(raw)
    row = storage.execute(
        """SELECT id, user_id, source, source_ref, content_type, received_at,
                  content_hash, raw_content AS _raw_content,
                  metadata_json AS _raw_metadata
             FROM raw_objects WHERE id=?""",
        (raw.id,),
    ).fetchone()
    assert row is not None
    return ReadOnlyAttachmentReference(
        ordinal=ordinal,
        raw_object_id=raw.id,
        source_identity_sha256=raw_source_identity_sha256(dict(row)),
        name=filename,
        media_type="text/plain",
    )


class _Model:
    enabled = True
    model = "v12-file-handler-test"

    def __init__(
        self,
        synthesis: str,
        *,
        verifier: str | None = None,
        mutate: Callable[[], None] | None = None,
    ) -> None:
        self.synthesis = synthesis
        self.verifier = verifier or json.dumps(
            {
                "schema": "friday.v12-file-verifier.v1",
                "supported": True,
                "citation_labels": ["A1"],
                "unsupported_claims": 0,
            }
        )
        self.mutate = mutate
        self.calls: list[dict[str, Any]] = []
        self.lease: ModelProfileLease | None = None
        self.lease_current = True
        self.lease_checks = 0

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        assert absolute_deadline > time.monotonic()
        self.lease = ModelProfileLease(
            profile_id="v12-file-handler-test:dispatcher",
            attestation_sha256="a" * 64,
            requirements_sha256=requirements.canonical_sha256(),
            capabilities=requirements.capabilities,
            required_context_tokens=requirements.required_context_tokens,
            prepared_evidence_items=requirements.prepared_evidence_items,
            max_tool_steps=requirements.max_tool_steps,
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
        assert absolute_deadline > time.monotonic()
        self.lease_checks += 1
        return bool(
            self.lease_current
            and lease is self.lease
            and self.lease is not None
            and self.lease.requirements_sha256 == requirements.canonical_sha256()
        )

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not await self.lease_is_current(
            lease,
            requirements,
            absolute_deadline=float(kwargs["absolute_deadline"]),
        ):
            raise RuntimeError("stale test lease")
        self.calls.append(
            {
                "lease": lease,
                "requirements_sha256": requirements.canonical_sha256(),
                "messages": messages,
                "tools": None,
                "allow_retries": False,
                "open_silent_cooldown": False,
                "require_full_context": True,
                **kwargs,
            }
        )
        if len(self.calls) == 1:
            if self.mutate is not None:
                self.mutate()
            content = self.synthesis
        else:
            content = self.verifier
        return {
            "content": content,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _request(
    reference: ReadOnlyAttachmentReference | tuple[ReadOnlyAttachmentReference, ...],
    *,
    conversation_id: str | None,
) -> tuple[ReadOnlyRouteRequest, TurnInput, TurnPlan]:
    references = reference if isinstance(reference, tuple) else (reference,)
    actor = _actor()
    snapshots = [
        {
            "filename": item.name,
            "mime_type": item.media_type,
            "transient_text": "available",
        }
        for item in references
    ]
    turn = TurnInput.from_chat(
        message="Сравни приложенные документы" if len(references) > 1 else "Что сказано в документе?",
        actor=actor,
        conversation_id=conversation_id,
        attachments=snapshots,
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    request = ReadOnlyRouteRequest(
        user_id="alice",
        actor=actor,
        conversation_id=conversation_id,
        attachments=references,
        synthetic_document_notice=False,
        replay_source_message_id=None,
        conversation_mode="dialogue",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
        reply_assistant_message_id=None,
        turn_deadline=time.monotonic() + 10,
    )
    return request, turn, _plan()


def _handler(storage: Any, settings: Any, model: _Model) -> V12FileReadHandler:
    return V12FileReadHandler(
        storage=storage,
        authorization=AuthorizationService(storage),
        settings=settings,
        model=model,
    )


@pytest.mark.asyncio
async def test_file_handler_synthesizes_verifies_and_atomically_publishes(settings, storage) -> None:
    conversation = storage.create_conversation("alice", mode="knowledge_work")
    reference = _register(storage, settings, text="Дата договора: 18 августа.", filename="a.txt")
    model = _Model("В договоре указано 18 августа. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    assert result.message == "В договоре указано 18 августа. [A1]"
    assert result.verified is True
    assert result.citation_labels == ("A1",)
    assert result.interaction_mode == "knowledge_work"
    assert len(model.calls) == 2
    assert all(call["tools"] is None and call["allow_retries"] is False for call in model.calls)
    assert [call["max_tokens"] for call in model.calls] == [512, 256]
    assert model.calls[0]["lease"] is model.calls[1]["lease"] is model.lease
    assert model.calls[0]["requirements_sha256"] == model.calls[1]["requirements_sha256"]
    messages = storage.get_conversation_messages(str(conversation["id"]), user_id="alice")
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Что сказано в документе?"),
        ("assistant", "В договоре указано 18 августа. [A1]"),
    ]
    assistant_metadata = json.loads(messages[-1]["metadata_json"])
    assert assistant_metadata["evidence_identity_sha256"] == result.evidence_identity_sha256
    assert assistant_metadata["conversation_attachment_raw_ids"] == [reference.raw_object_id]
    assert assistant_metadata["verified"] is True
    trace = TurnTrace.parse(assistant_metadata[INTERACTION_TRACE_METADATA_KEY])
    assert trace.intent is IntentClass.DOCUMENT_WORK
    assert [step.capability for step in trace.steps] == [
        CapabilityClass.DOCUMENT_RETRIEVAL,
        CapabilityClass.MODEL_SYNTHESIS,
        CapabilityClass.VERIFICATION,
    ]
    assert trace.completion is CompletionDecision.COMPLETE
    assert trace.publication is PublicationStatus.ASSISTANT_COMMITTED
    assert trace.authority_rechecked is True
    assert trace.budget.model_calls == 2
    assert trace.budget.model_call_accounting is CountAccounting.COMPLETE
    assert trace.budget.capability_calls == 1
    assert trace.budget.capability_call_accounting is CountAccounting.COMPLETE
    assert trace.budget.token_accounting is TokenAccounting.UNAVAILABLE
    serialized_trace = trace.to_json()
    for private_value in (
        str(conversation["id"]),
        str(messages[0]["id"]),
        str(messages[-1]["id"]),
        reference.raw_object_id,
        "alice",
        "a.txt",
        "Дата договора: 18 августа.",
        "Что сказано в документе?",
        "В договоре указано 18 августа. [A1]",
    ):
        assert private_value not in serialized_trace


@pytest.mark.asyncio
async def test_router_level_trace_latency_is_clamped_instead_of_omitted(
    settings,
    storage,
    monkeypatch,
) -> None:
    from friday.orchestration import file_read as file_read_module

    reference = _register(storage, settings, text="Old router trace source.", filename="old.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=None)
    request = replace(
        request,
        turn_deadline=None,
        orchestration_started_at=0.0,
        planner_model_calls_lower_bound=1,
    )
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    real_monotonic = time.monotonic

    class _LongRunningClock:
        @staticmethod
        def monotonic() -> float:
            return real_monotonic() + 100_000.0

    monkeypatch.setattr(file_read_module, "time", _LongRunningClock)
    result = await handler.handle(request, turn, plan, preparation)

    messages = storage.get_conversation_messages(result.conversation_id, user_id="alice")
    trace = TurnTrace.parse(json.loads(messages[-1]["metadata_json"])[INTERACTION_TRACE_METADATA_KEY])
    assert trace.budget.latency_ms == 86_400_000
    assert trace.budget.model_calls == 3
    assert trace.budget.model_call_accounting is CountAccounting.LOWER_BOUND


@pytest.mark.asyncio
async def test_file_handler_publishes_when_shadow_trace_key_is_unavailable(
    settings,
    storage,
    monkeypatch,
) -> None:
    from friday.orchestration import file_read as file_read_module

    conversation = storage.create_conversation("alice", mode="knowledge_work")
    reference = _register(storage, settings, text="Trace is observational.", filename="trace.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    def unavailable(_executor: Any) -> bytes:
        raise RuntimeError("synthetic trace key outage")

    monkeypatch.setattr(file_read_module, "load_trace_namespace_key", unavailable)
    result = await handler.handle(request, turn, plan, preparation)

    assert result.message == "Источник прочитан. [A1]"
    messages = storage.get_conversation_messages(str(conversation["id"]), user_id="alice")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assistant_metadata = json.loads(messages[-1]["metadata_json"])
    assert INTERACTION_TRACE_METADATA_KEY not in assistant_metadata
    assert assistant_metadata["verified"] is True


@pytest.mark.asyncio
async def test_real_router_trace_includes_planning_and_router_level_latency(settings, storage) -> None:
    reference = _register(storage, settings, text="Router trace source.", filename="router.txt")
    raw = storage.get_raw_object(reference.raw_object_id, "alice")
    assert raw is not None

    class _CurrentTurnCarrier(dict[str, Any]):
        pass

    attachment = _CurrentTurnCarrier(
        {
            "filename": "router.txt",
            "mime_type": "text/plain",
            "size_bytes": len("Router trace source."),
            "raw_object_id": reference.raw_object_id,
            "persisted": True,
            "current_turn_only": True,
            "transient_text": "available",
        }
    )
    stamp_current_turn_file_reference(attachment, raw)

    class _Planner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
            del turn, turn_deadline
            raise AssertionError("V12 router must use attested planning")

        async def plan_attested(
            self,
            turn: TurnInput,
            *,
            turn_deadline: float | None = None,
        ) -> TurnPlan:
            del turn, turn_deadline
            self.calls += 1
            await asyncio.sleep(0.03)
            return _plan()

    class _NeverLegacy:
        async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
            del user_id, message, kwargs
            raise AssertionError("eligible V12 route must not fall back to legacy")

    planner = _Planner()
    model = _Model("Источник прочитан через V12 router. [A1]")
    handler = _handler(storage, settings, model)
    router = OrchestrationRouter(
        _NeverLegacy(),
        planner,
        mode="v12",
        allowed_routes=("file_read",),
        route_handlers={RouteClass.FILE_READ: handler},
        planner_timeout_sec=1.0,
        preparation_timeout_sec=1.0,
        route_timeout_sec=10.0,
    )

    result = await router.chat(
        "alice",
        "Что сказано в документе?",
        actor=_actor(),
        attachments=[attachment],
        enable_tools=True,
    )

    assert planner.calls == 1
    messages = storage.get_conversation_messages(str(result["conversation_id"]), user_id="alice")
    trace = TurnTrace.parse(json.loads(messages[-1]["metadata_json"])[INTERACTION_TRACE_METADATA_KEY])
    assert [step.capability for step in trace.steps] == [
        CapabilityClass.MODEL_PLANNING,
        CapabilityClass.DOCUMENT_RETRIEVAL,
        CapabilityClass.MODEL_SYNTHESIS,
        CapabilityClass.VERIFICATION,
    ]
    assert trace.budget.model_calls == 3
    assert trace.budget.model_call_accounting is CountAccounting.LOWER_BOUND
    assert trace.budget.latency_ms >= 20


@pytest.mark.asyncio
async def test_stale_file_model_lease_is_rejected_before_selection_or_publication(
    settings,
    storage,
) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="LEASE-SOURCE", filename="lease.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    model.lease_current = False

    assert await handler.preparation_is_current(request, turn, plan, preparation) is False
    with pytest.raises(V12FileReadError, match="authority changed before synthesis"):
        await handler.handle(request, turn, plan, preparation)

    assert model.calls == []
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_epoch_loss_after_synthesis_never_reacquires_or_publishes(settings, storage) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="DRIFT-SOURCE", filename="drift.txt")
    model = _Model("Источник прочитан. [A1]")

    def revoke_after_synthesis() -> None:
        model.lease_current = False

    model.mutate = revoke_after_synthesis
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original_lease = model.lease

    with pytest.raises(RuntimeError, match="stale test lease"):
        await handler.handle(request, turn, plan, preparation)

    assert model.lease is original_lease
    assert len(model.calls) == 1
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_file_handler_creates_conversation_and_two_messages_in_one_commit(settings, storage) -> None:
    reference = _register(storage, settings, text="Номер: 42.", filename="number.txt")
    handler = _handler(storage, settings, _Model("В документе номер 42. [A1]"))
    request, turn, plan = _request(reference, conversation_id=None)
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    result = await handler.handle(request, turn, plan, preparation)

    conversation = storage.get_conversation(result.conversation_id, "alice")
    assert conversation is not None
    assert conversation["mode"] == "dialogue"
    messages = storage.get_conversation_messages(result.conversation_id, user_id="alice")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert result.message_id == messages[-1]["id"]
    assert result.outcome.status is CapabilityOutcomeStatus.COMPLETE
    assert result.outcome.route is RouteClass.FILE_READ
    assert result.outcome.evidence_identity_sha256 == result.evidence_identity_sha256
    assert result.outcome.citation_labels == result.citation_labels
    assert "outcome" not in result.response(conversation_mode="dialogue")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (
        CapabilityOutcomeStatus.PARTIAL,
        CapabilityOutcomeStatus.EMPTY,
        CapabilityOutcomeStatus.UNAVAILABLE,
        CapabilityOutcomeStatus.DENIED,
    ),
)
async def test_file_handler_completion_gate_rolls_back_every_noncomplete_outcome(
    settings,
    storage,
    monkeypatch,
    status: CapabilityOutcomeStatus,
) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="GATE-SOURCE", filename="gate.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    def outcome_for_status(turn_plan: TurnPlan, evidence: Any) -> CapabilityOutcome:
        if status in {CapabilityOutcomeStatus.COMPLETE, CapabilityOutcomeStatus.PARTIAL}:
            evidence_digest: str | None = evidence.identity_sha256
            citations = evidence.bundle.citation_labels
            authority_rechecked = True
            verified = True
        elif status is CapabilityOutcomeStatus.EMPTY:
            evidence_digest = evidence.identity_sha256
            citations = ()
            authority_rechecked = True
            verified = True
        elif status is CapabilityOutcomeStatus.UNAVAILABLE:
            evidence_digest = None
            citations = ()
            authority_rechecked = False
            verified = False
        else:
            evidence_digest = None
            citations = ()
            authority_rechecked = True
            verified = False
        return CapabilityOutcome(
            route=RouteClass.FILE_READ,
            status=status,
            plan_sha256=turn_plan.canonical_sha256(),
            evidence_identity_sha256=evidence_digest,
            citation_labels=citations,
            authority_rechecked=authority_rechecked,
            verified=verified,
        )

    monkeypatch.setattr(handler, "_completion_outcome", outcome_for_status)

    with pytest.raises(V12FileReadError, match="completion gate"):
        await handler.handle(request, turn, plan, preparation)

    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_two_file_handler_requires_both_sources_in_answer_and_verifier(settings, storage) -> None:
    first = _register(storage, settings, text="ALPHA", filename="a.txt", ordinal=1)
    second = _register(storage, settings, text="BETA", filename="b.txt", ordinal=2)
    verifier = json.dumps(
        {
            "schema": "friday.v12-file-verifier.v1",
            "supported": True,
            "citation_labels": ["A1", "A2"],
            "unsupported_claims": 0,
        }
    )
    model = _Model("Первый — ALPHA [A1], второй — BETA [A2].", verifier=verifier)
    handler = _handler(storage, settings, model)
    request, turn, plan = _request((first, second), conversation_id=None)
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)
    assert result.citation_labels == ("A1", "A2")
    assert "ALPHA" in json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    assert "BETA" in json.dumps(model.calls[0]["messages"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_prepare_reserves_the_full_verifier_context_before_selection(settings, storage) -> None:
    reference = _register(storage, settings, text="X" * 3_500, filename="large.txt")
    handler = _handler(storage, settings, _Model("Ответ. [A1]"))
    request, turn, plan = _request(reference, conversation_id=None)

    assert await handler.prepare(request, turn, plan) is None


@pytest.mark.asyncio
async def test_maximum_accepted_answer_still_reaches_the_verifier(settings, storage) -> None:
    reference = _register(storage, settings, text="SOURCE", filename="source.txt")
    # Quotes are the worst nested-JSON case: the answer is encoded into the
    # verifier payload and that payload is encoded once more as chat content.
    answer = '"' * 1_018 + " [A1]"
    assert len(json.dumps(answer, ensure_ascii=False).encode("utf-8")) <= 2_048
    model = _Model(answer)
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=None)
    preparation = await handler.prepare(request, turn, plan)

    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    assert result.message == answer
    assert len(model.calls) == 2
    assert all(call["require_full_context"] is True for call in model.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verifier",
    [
        "not-json",
        json.dumps(
            {
                "schema": "friday.v12-file-verifier.v1",
                "supported": False,
                "citation_labels": ["A1"],
                "unsupported_claims": 1,
            }
        ),
        '{"schema":"friday.v12-file-verifier.v1","supported":true,'
        '"citation_labels":["A1"],"citation_labels":["A1"],"unsupported_claims":0}',
    ],
)
async def test_verifier_rejection_publishes_nothing(settings, storage, verifier: str) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SOURCE", filename="source.txt")
    handler = _handler(storage, settings, _Model("Ответ. [A1]", verifier=verifier))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    with pytest.raises(V12FileReadError):
        await handler.handle(request, turn, plan, preparation)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "Ответ без метки.",
        "Ответ [A1], придуманный источник [A2].",
        "Ответ [A1]. Чужая метка [A0].",
        "Ответ [A1]. Чужая метка [A9999].",
        "Ответ [A1]. Чужая метка [A1000000].",
        "Ответ [A1]. Чужая метка [AA1].",
        "Ответ [A1]. Чужая метка [A_1].",
        "Ответ [A1]. Чужая метка [A-1].",
        "Ответ [A1]. Чужая метка [A 1].",
        "Ответ [A1]. Чужая метка [A.1].",
        "Ответ [A1]. Чужая метка [A/1].",
        "Ответ [A1]. Чужая метка [A:1].",
        "Ответ [A1]. Чужая метка [A\u200b1].",
        "Ответ [A1]. Чужая метка [ A1].",
        "Ответ [A1]. Чужая метка [A1 ].",
        "Ответ [A1]. Чужая метка [B#1].",
        "Ответ [A1]. Чужая метка [B1].",
        "Ответ [A1]. Чужая метка [Б1].",
        r"Ответ [A1]. Чужая метка \[B1\].",
        "Ответ [A1]. Чужая метка ［B1］.",
        "Ответ [A1]. Чужая метка 【B1】.",
        "Ответ [A1]. Посторонняя вставка [приложение].",
        "<think>скрыто</think> Ответ [A1].",
    ],
)
async def test_unsafe_synthesis_publishes_nothing(settings, storage, answer: str) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SOURCE", filename="source.txt")
    handler = _handler(storage, settings, _Model(answer))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    with pytest.raises(V12FileReadError):
        await handler.handle(request, turn, plan, preparation)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_named_runtime_secret_in_synthesis_publishes_nothing(
    settings,
    storage,
    monkeypatch,
) -> None:
    secret = "sk-friday-model-leak-1234567890"
    monkeypatch.setenv("FRIDAY_API_TOKEN", secret)
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SAFE-SOURCE", filename="source.txt")
    handler = _handler(storage, settings, _Model(f"Ответ {secret}. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    with pytest.raises(V12FileReadError, match="unsafe text"):
        await handler.handle(request, turn, plan, preparation)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_post_synthesis_source_mutation_prevents_both_message_rows(settings, storage) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SOURCE-BEFORE", filename="source.txt")

    def mutate() -> None:
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET deleted_at='2026-08-18T00:00:00Z' WHERE id=?",
                (reference.raw_object_id,),
            )

    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]", mutate=mutate))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    with pytest.raises(V12FileReadError, match="authority changed"):
        await handler.handle(request, turn, plan, preparation)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_publication_deadline_rolls_back_and_never_commits_in_background(
    settings,
    storage,
    monkeypatch,
) -> None:
    import friday.orchestration.file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="DEADLINE-SOURCE", filename="deadline.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original = file_read_module.reauthorize_prepared_file_evidence_in_transaction

    def delayed_reauthorization(*args, **kwargs):  # noqa: ANN002, ANN003
        time.sleep(0.08)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        file_read_module,
        "reauthorize_prepared_file_evidence_in_transaction",
        delayed_reauthorization,
    )
    monkeypatch.setattr(file_read_module, "_PUBLICATION_RESERVE_SEC", 0.005)
    expired_request = replace(request, turn_deadline=time.monotonic() + 0.04)

    with pytest.raises(TimeoutError, match="final reauthorization"):
        await handler.handle(expired_request, turn, plan, preparation)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0
    await asyncio.sleep(0.1)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_commit_boundary_deadline_rolls_back_both_rows(settings, storage, monkeypatch) -> None:
    import friday.orchestration.file_read as file_read_module
    import friday.storage._core as storage_core

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="COMMIT-DEADLINE", filename="commit.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original_refresh = storage_core._refresh_private_derivative_authority  # noqa: SLF001

    def delayed_refresh(conn):  # noqa: ANN001, ANN202
        time.sleep(0.08)
        return original_refresh(conn)

    monkeypatch.setattr(storage_core, "_refresh_private_derivative_authority", delayed_refresh)
    monkeypatch.setattr(file_read_module, "_PUBLICATION_RESERVE_SEC", 0.005)

    with pytest.raises(TimeoutError, match="before transaction commit"):
        await handler.handle(
            replace(request, turn_deadline=time.monotonic() + 0.04),
            turn,
            plan,
            preparation,
        )
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_publication_refuses_an_ambient_implicit_transaction(settings, storage) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="OUTER-BOUNDARY", filename="outer.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    storage.execute("UPDATE users SET display_name=display_name WHERE id='alice'")
    assert storage.conn.in_transaction is True

    with pytest.raises(RuntimeError, match="outer commit boundary"):
        await handler.handle(request, turn, plan, preparation)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0
    storage.conn.rollback()


def _tracked_fence_callbacks(storage: Any, request_key: str, lease_token: str) -> tuple[Any, Any]:
    def ordinary() -> bool:
        return storage.idempotency_mark_effect_possible(
            "alice",
            request_key,
            lease_token,
            {"uncertain": True},
        )

    def in_transaction(conn: Any) -> bool:
        cursor = conn.execute(
            """UPDATE request_idempotency SET response_json=?
               WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
            ('{"uncertain":true}', "alice", request_key, lease_token),
        )
        return cursor.rowcount == 1

    return ordinary, in_transaction


@pytest.mark.asyncio
async def test_publication_commits_idempotency_fence_and_messages_atomically(settings, storage) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="ATOMIC-FENCE", filename="fence.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    request_key = "v12-atomic-fence-success"
    claim = storage.idempotency_claim("alice", request_key, request_hash="a" * 64)
    lease_token = str(claim["lease_token"])
    ordinary, in_transaction = _tracked_fence_callbacks(storage, request_key, lease_token)

    with track_request_effects(
        ordinary,
        before_effect_in_transaction=in_transaction,
    ) as effects:
        await handler.handle(request, turn, plan, preparation)
        assert effects.possible is True
        assert effects.staged is False

    row = storage.execute(
        "SELECT response_json FROM request_idempotency WHERE user_id=? AND request_key=?",
        ("alice", request_key),
    ).fetchone()
    assert row is not None and json.loads(row["response_json"]) == {"uncertain": True}
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 2


@pytest.mark.asyncio
async def test_rolled_back_publication_clears_only_the_staged_idempotency_fence(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="ROLLBACK-FENCE", filename="rollback.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    def fail_publication(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise V12FileReadError("injected publication failure")

    monkeypatch.setattr(file_read_module, "store_message_in_transaction", fail_publication)
    request_key = "v12-atomic-fence-rollback"
    claim = storage.idempotency_claim("alice", request_key, request_hash="b" * 64)
    lease_token = str(claim["lease_token"])
    ordinary, in_transaction = _tracked_fence_callbacks(storage, request_key, lease_token)

    with track_request_effects(
        ordinary,
        before_effect_in_transaction=in_transaction,
    ) as effects:
        with pytest.raises(V12FileReadError, match="injected publication failure"):
            await handler.handle(request, turn, plan, preparation)
        assert effects.possible is False
        assert effects.staged is False

    row = storage.execute(
        "SELECT response_json FROM request_idempotency WHERE user_id=? AND request_key=?",
        ("alice", request_key),
    ).fetchone()
    assert row is not None and json.loads(row["response_json"]) == {}
    assert storage.idempotency_release("alice", request_key, lease_token) is True


@pytest.mark.asyncio
async def test_publication_writer_lock_obeys_the_remaining_deadline(settings, storage) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="LOCK-BOUNDARY", filename="lock.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    started = threading.Event()
    release = threading.Event()

    def hold_writer() -> None:
        with storage.transaction() as conn:
            conn.execute("SELECT 1")
            started.set()
            assert release.wait(3)

    owner = threading.Thread(target=hold_writer, daemon=True)
    owner.start()
    assert await asyncio.to_thread(started.wait, 1)
    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="writer lock deadline"):
            await handler.handle(
                replace(request, turn_deadline=time.monotonic() + 2.15),
                turn,
                plan,
                preparation,
            )
        assert time.monotonic() - started_at < 1.0
    finally:
        release.set()
        await asyncio.to_thread(owner.join, 2)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_publication_sqlite_lock_obeys_the_remaining_deadline(settings, storage) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SQLITE-LOCK", filename="sqlite-lock.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    outsider = sqlite3.connect(settings.database_path, timeout=0, isolation_level=None)
    outsider.execute("BEGIN IMMEDIATE")
    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="SQLite writer deadline"):
            await handler.handle(
                replace(request, turn_deadline=time.monotonic() + 2.15),
                turn,
                plan,
                preparation,
            )
        assert time.monotonic() - started_at < 1.0
    finally:
        outsider.rollback()
        outsider.close()
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_secret_rotation_while_waiting_for_writer_lock_stops_publication(
    settings,
    storage,
    monkeypatch,
) -> None:
    future_secret = "sk-friday-lock-wait-secret-1234567890"
    monkeypatch.setenv("FRIDAY_API_TOKEN", "sk-friday-initial-secret-1234567890")
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SAFE-BODY", filename="lock-rotation.txt")
    handler = _handler(storage, settings, _Model(f"Ответ {future_secret}. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    started = threading.Event()

    def hold_then_rotate() -> None:
        with storage.transaction() as conn:
            conn.execute("SELECT 1")
            started.set()
            time.sleep(0.08)
            os.environ["FRIDAY_API_TOKEN"] = future_secret

    owner = threading.Thread(target=hold_then_rotate, daemon=True)
    owner.start()
    assert await asyncio.to_thread(started.wait, 1)

    with pytest.raises(V12FileReadError, match="publication output requires"):
        await handler.handle(
            replace(request, turn_deadline=time.monotonic() + 4.0),
            turn,
            plan,
            preparation,
        )
    await asyncio.to_thread(owner.join, 2)
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_secret_rotation_after_prepare_stops_before_any_model_or_write(
    settings,
    storage,
    monkeypatch,
) -> None:
    future_secret = "sk-friday-post-prepare-secret-1234567890"
    conversation = storage.create_conversation("alice")
    reference = _register(
        storage,
        settings,
        text=f"Previously ordinary source value: {future_secret}",
        filename="rotation.txt",
    )
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    monkeypatch.setenv("FRIDAY_API_TOKEN", future_secret)

    with pytest.raises(V12FileReadError, match="secret projection"):
        await handler.handle(request, turn, plan, preparation)
    assert model.calls == []
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_prepare_rejects_foreign_conversation_and_more_than_two_sources(settings, storage) -> None:
    storage.ensure_user("bob", preset_key="owner")
    foreign = storage.create_conversation("bob")
    references = tuple(
        _register(storage, settings, text=f"SOURCE-{index}", filename=f"{index}.txt", ordinal=index)
        for index in range(1, 4)
    )
    handler = _handler(storage, settings, _Model("unused [A1]"))

    request, turn, plan = _request(references[0], conversation_id=str(foreign["id"]))
    assert await handler.prepare(request, turn, plan) is None

    request, turn, plan = _request(references, conversation_id=None)
    assert await handler.prepare(request, turn, plan) is None


@pytest.mark.asyncio
async def test_prepare_writer_barrier_is_bounded_before_legacy_fallback(
    settings,
    storage,
    monkeypatch,
) -> None:
    import friday.orchestration.file_read as file_read_module

    reference = _register(storage, settings, text="PREPARE-LOCK", filename="locked.txt")
    handler = _handler(storage, settings, _Model("unused [A1]"))
    request, turn, plan = _request(reference, conversation_id=None)
    started = threading.Event()
    release = threading.Event()

    def hold_writer() -> None:
        with storage.transaction() as conn:
            conn.execute("SELECT 1")
            started.set()
            assert release.wait(2)

    owner = threading.Thread(target=hold_writer, daemon=True)
    owner.start()
    assert await asyncio.to_thread(started.wait, 1)
    monkeypatch.setattr(file_read_module, "_PREPARATION_BUDGET_SEC", 0.05)
    started_at = time.monotonic()
    try:
        assert await handler.prepare(request, turn, plan) is None
        assert time.monotonic() - started_at < 0.5
    finally:
        release.set()
        await asyncio.to_thread(owner.join, 1)
