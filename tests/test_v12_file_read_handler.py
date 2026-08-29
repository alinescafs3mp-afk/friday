from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.execution_kernel import bind_authenticated_request_effect_authority, track_request_effects
from friday.file_evidence import (
    current_turn_file_reference_for_tenant,
    stamp_current_turn_file_reference,
    stamp_current_turn_file_reference_for_tenant,
)
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
from friday.orchestration.capability_outcome import (
    ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY,
    CapabilityOutcome,
    CapabilityOutcomeStatus,
    attach_accepted_capability_outcome_receipt,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.contracts import RouterMode
from friday.orchestration.file_read import (
    _MAX_ANSWER_JSON_UTF8_BYTES,
    V12FileReadError,
    V12FileReadHandler,
    _attested_input_max_bytes,
    _call_model_once,
    _file_requirements,
    _lease_is_current_before_deadline,
    _messages_fit_attested_context,
    _two_call_read_model_output_limits,
    _within_parent_deadline,
)
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.orchestration.turn_context_call_scope import require_authenticated_chat_call_scope
from friday.orchestration.turn_context_publication import bind_authenticated_turn_publication
from friday.orchestration.turn_context_runtime import (
    bind_authenticated_turn_context,
    suspend_authenticated_turn_context,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import raw_source_identity_sha256
from friday.storage.models import RawObject, new_id
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision


class _CurrentCarrier(dict[str, object]):
    pass


class _EqualStringLookalike(str):
    def __eq__(self, _other: object) -> bool:
        return True


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
        self.process_lease_checks = 0

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
        assert absolute_deadline > time.monotonic()
        self.lease_checks += 1
        return bool(
            self.lease_current
            and lease is self.lease
            and self.lease is not None
            and self.lease.requirements_sha256 == requirements.canonical_sha256()
        )

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        self.process_lease_checks += 1
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


class _MeasuredContextModel(_Model):
    def __init__(
        self,
        synthesis: str,
        *,
        available_context_tokens: int,
        reject_acquire: bool = False,
    ) -> None:
        super().__init__(synthesis)
        self._available_context_tokens = available_context_tokens
        self.reject_acquire = reject_acquire
        self.acquire_calls = 0
        self.acquired_requirements: list[ModelRequirements] = []

    def available_context_tokens(self) -> int:
        return self._available_context_tokens

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        self.acquire_calls += 1
        self.acquired_requirements.append(requirements)
        if self.reject_acquire:
            return None
        return await super().acquire_lease(
            requirements,
            absolute_deadline=absolute_deadline,
        )


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


def _authenticated_current_file_context(
    storage: Any,
    request: ReadOnlyRouteRequest,
    reference: ReadOnlyAttachmentReference,
    *,
    token: str,
    request_binding: str = "c" * 64,
    deadline_ns: int | None = None,
    max_model_calls: int = 4,
    max_output_tokens: int = 4096,
) -> tuple[
    TurnContextIssuer,
    AuthenticatedTurnContext,
    ReadOnlyRouteRequest,
    TurnInput,
    dict[str, object],
]:
    issuer = TurnContextIssuer(b"s2-v12-file-boundary-test-key!!!")
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=token,
        actor=request.actor,
        conversation_id=request.conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"source-{token}",
        update_id=f"update-{token}",
        request_effect_binding_sha256=request_binding,
    )
    row = storage.execute(
        """SELECT id, user_id, source, source_ref, content_type, received_at,
                  content_hash, raw_content, raw_content AS _raw_content,
                  metadata_json, metadata_json AS _raw_metadata
             FROM raw_objects WHERE id=? AND user_id=?""",
        (reference.raw_object_id, request.actor.user_id),
    ).fetchone()
    assert row is not None
    carrier: dict[str, object] = _CurrentCarrier(
        {
            "filename": reference.name,
            "mime_type": reference.media_type,
            "raw_object_id": str(row["id"]),
            "transient_text": "available",
            "extraction_success": True,
            "persisted": True,
            "current_turn_only": True,
        }
    )
    stamp_current_turn_file_reference_for_tenant(
        carrier,
        dict(row),
        tenant_id=request.actor.user_id,
    )
    current_token = current_turn_file_reference_for_tenant(
        carrier,
        tenant_id=request.actor.user_id,
    )
    assert current_token is not None
    turn = TurnInput.from_chat(
        message="Что сказано в документе?",
        actor=request.actor,
        conversation_id=request.conversation_id,
        attachments=[carrier],
        enable_tools=True,
        synthetic_document_notice=False,
        mode="dialogue",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    source = issuer.current_attachment_source(
        authority=authority,
        carrier=carrier,
        descriptor=turn.attachments[0],
    )
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.V12,
        fallback_router_mode=RouterMode.LEGACY,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=turn,
        authorized_sources=(issuer.accepted_ingress_source(authority), source),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(deadline_ns or (time.monotonic_ns() + 30_000_000_000)),
            ModelAntiLoopBudget(max_model_calls, min(1, max_model_calls - 1)),
            TurnResourceBudget(4, 2, 1, max_output_tokens),
        ),
        pending_work_admission=None,
    )
    descriptor = turn.attachments[0]
    bound_request = replace(
        request,
        attachments=(
            ReadOnlyAttachmentReference(
                ordinal=descriptor.ordinal,
                raw_object_id=current_token.raw_id,
                source_identity_sha256=current_token.source_identity_sha256,
                name=descriptor.name,
                media_type=descriptor.media_type,
            ),
        ),
        conversation_mode=turn.conversation_mode,
        reply_to=turn.reply_quote,
        synthetic_document_notice=turn.synthetic_document_notice,
        quoted_attachment_reference=turn.quoted_attachment_reference,
        reply_assistant_reference=turn.reply_assistant_reference,
    )
    return issuer, context, bound_request, turn, carrier


def _seal_authenticated_file_call_scope(
    context: AuthenticatedTurnContext,
    request: ReadOnlyRouteRequest,
    turn: TurnInput,
    carrier: dict[str, object],
) -> None:
    require_authenticated_chat_call_scope(
        context,
        user_id=request.user_id,
        message=turn.message,
        actor=request.actor,
        conversation_id=request.conversation_id,
        attachments=[carrier],
        enable_tools=turn.enable_tools,
        synthetic_document_notice=turn.synthetic_document_notice,
        replay_source_message_id=None,
        mode=None,
        answer_with_voice=False,
        reply_to=None,
        quoted_attachment_reference=turn.quoted_attachment_reference,
        reply_assistant_reference=turn.reply_assistant_reference,
        reply_assistant_message_id=None,
        turn_policy=None,
        telegram_update_id=None,
        turn_deadline=_exact_deadline_float(context),
        pending_durable_admission=None,
        runtime_router_mode=RouterMode.V12,
    )


def _exact_deadline_float(context: AuthenticatedTurnContext) -> float:
    target = context.inherited_budget.safety_deadline.monotonic_ns
    candidate = target / 1_000_000_000
    for _ in range(4):
        observed = int(candidate * 1_000_000_000)
        if observed == target:
            return candidate
        candidate = math.nextafter(candidate, math.inf if observed < target else -math.inf)
    raise AssertionError("test deadline cannot be represented by the raw float carrier")


async def _leased_model_call(model: _Model) -> tuple[ModelRequirements, ModelProfileLease]:
    requirements = _file_requirements(1)
    lease = await model.acquire_lease(
        requirements,
        absolute_deadline=time.monotonic() + 10,
    )
    assert type(lease) is ModelProfileLease
    return requirements, lease


def test_file_model_requirements_are_closed_process_singletons() -> None:
    one = _file_requirements(1)
    two = _file_requirements(2)
    measured = _file_requirements(1, 40_960)
    measured_tiers = (8_192, 16_384, 24_576, 32_768, 40_960)

    assert _file_requirements(1) is one
    assert _file_requirements(2) is two
    assert _file_requirements(1, 40_960) is measured
    assert one.prepared_evidence_items == 1
    assert two.prepared_evidence_items == 2
    assert one.required_context_tokens == 8_192
    assert measured.required_context_tokens == 40_960
    assert measured.canonical_sha256() != one.canonical_sha256()
    assert _attested_input_max_bytes(8_192) == 5_500
    assert _attested_input_max_bytes(40_960) == 27_500
    for tier in measured_tiers:
        assert _file_requirements(1, tier) is _file_requirements(1, tier)
        assert _attested_input_max_bytes(tier) == (5_500 * tier) // 8_192
    assert one.max_tool_steps == one.max_tool_rounds == one.max_tool_calls == 0
    assert two.max_tool_steps == two.max_tool_rounds == two.max_tool_calls == 0
    for malformed in (True, 0, 3, 1.0, "1"):
        with pytest.raises(ValueError, match="closed lease projection"):
            _file_requirements(malformed)  # type: ignore[arg-type]
    for malformed_context in (True, 0, 8_191, 12_288, 40_961, 8_192.0, "40960"):
        with pytest.raises(ValueError, match="closed measured tiers"):
            _file_requirements(1, malformed_context)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_call_model_once_does_not_signal_dispatch_for_pre_dispatch_rejection() -> None:
    model = _Model("unused")
    requirements, lease = await _leased_model_call(model)
    dispatches: list[str] = []

    with pytest.raises(V12FileReadError, match="context tier"):
        await _call_model_once(
            model,
            lease,
            requirements,
            [{"role": "user", "content": "x" * 6_000}],
            max_tokens=16,
            deadline=time.monotonic() + 10,
            priority="foreground",
            on_dispatch=lambda: dispatches.append("dispatch"),
        )

    assert dispatches == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_call_model_once_does_not_signal_dispatch_without_model_budget() -> None:
    model = _Model("unused")
    requirements, lease = await _leased_model_call(model)
    dispatches: list[str] = []

    with pytest.raises(TimeoutError, match="no model budget"):
        await _call_model_once(
            model,
            lease,
            requirements,
            [{"role": "user", "content": "safe"}],
            max_tokens=16,
            deadline=time.monotonic(),
            priority="foreground",
            on_dispatch=lambda: dispatches.append("dispatch"),
        )

    assert dispatches == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_call_model_once_signals_dispatch_exactly_once() -> None:
    model = _Model("accepted")
    requirements, lease = await _leased_model_call(model)
    dispatches: list[str] = []

    response = await _call_model_once(
        model,
        lease,
        requirements,
        [{"role": "user", "content": "safe"}],
        max_tokens=16,
        deadline=time.monotonic() + 10,
        priority="foreground",
        on_dispatch=lambda: dispatches.append("dispatch"),
    )

    assert response["content"] == "accepted"
    assert dispatches == ["dispatch"]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_call_model_once_rejects_v2_lease_drift_before_dispatch() -> None:
    model = _Model("unused")
    requirements, lease = await _leased_model_call(model)
    drifted = replace(lease, max_tool_calls=1)
    dispatches: list[str] = []

    with pytest.raises(V12FileReadError, match="authority changed before model call"):
        await _call_model_once(
            model,
            drifted,
            requirements,
            [{"role": "user", "content": "safe"}],
            max_tokens=16,
            deadline=time.monotonic() + 10,
            priority="foreground",
            on_dispatch=lambda: dispatches.append("dispatch"),
        )
    cloned_requirements = replace(requirements)
    assert cloned_requirements == requirements
    assert cloned_requirements is not requirements
    with pytest.raises(V12FileReadError, match="authority changed before model call"):
        await _call_model_once(
            model,
            lease,
            cloned_requirements,
            [{"role": "user", "content": "safe"}],
            max_tokens=16,
            deadline=time.monotonic() + 10,
            priority="foreground",
            on_dispatch=lambda: dispatches.append("dispatch"),
        )

    assert dispatches == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_call_model_once_rejects_non_boolean_lease_verdict() -> None:
    class _CoercingModel(_Model):
        async def lease_is_current(self, *_args: Any, **_kwargs: Any) -> Any:
            return 1

    model = _CoercingModel("unused")
    requirements, lease = await _leased_model_call(model)

    with pytest.raises(V12FileReadError, match="authority changed before model call"):
        await _call_model_once(
            model,
            lease,
            requirements,
            [{"role": "user", "content": "safe"}],
            max_tokens=16,
            deadline=time.monotonic() + 10,
            priority="foreground",
        )

    assert model.calls == []


@pytest.mark.asyncio
async def test_lease_check_is_deadline_bounded_and_call_cancellation_propagates() -> None:
    class _BlockingModel(_Model):
        def __init__(self) -> None:
            super().__init__("unused")
            self.started = asyncio.Event()

        async def complete(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class _BlockingLeaseModel(_Model):
        async def lease_is_current(self, *_args: Any, **_kwargs: Any) -> bool:
            await asyncio.Event().wait()
            return True

    deadline_model = _BlockingLeaseModel("unused")
    requirements, lease = await _leased_model_call(deadline_model)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _lease_is_current_before_deadline(
            deadline_model,
            lease,
            requirements,
            absolute_deadline=started + 0.01,
        )
    assert time.monotonic() - started < 1

    model = _BlockingModel()
    requirements, lease = await _leased_model_call(model)
    task = asyncio.create_task(
        _call_model_once(
            model,
            lease,
            requirements,
            [{"role": "user", "content": "safe"}],
            max_tokens=16,
            deadline=time.monotonic() + 10,
            priority="foreground",
        )
    )
    await asyncio.wait_for(model.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "deadline",
    [True, float("nan"), float("inf"), -float("inf")],
    ids=("bool", "nan", "positive-infinity", "negative-infinity"),
)
def test_file_read_rejects_malformed_deadlines_before_parent_clamping(deadline: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _within_parent_deadline(deadline, None)  # type: ignore[arg-type]


def test_file_read_rejects_nonfuture_deadline_before_parent_clamping() -> None:
    with pytest.raises(TimeoutError):
        _within_parent_deadline(time.monotonic() - 1, None)


@pytest.mark.asyncio
async def test_prepared_file_context_repr_contains_no_body_or_private_path(settings, storage) -> None:
    body = "REPR-BODY-CANARY-7421"
    filename = "repr-private-path-canary.txt"
    reference = _register(storage, settings, text=body, filename=filename)
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=None)

    context = await handler._prepare_context(  # noqa: SLF001
        request,
        turn,
        plan,
        time.monotonic() + 1,
    )
    preparation = await handler.prepare(request, turn, plan)
    assert context is not None
    assert preparation is not None

    for value in (body, filename, reference.raw_object_id):
        assert value not in repr(context)
        assert value not in repr(preparation.private_payload)
        assert value not in repr(preparation)


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
    receipt = load_accepted_capability_outcome_receipt(
        assistant_metadata,
        expected_outcome=result.outcome,
    )
    assert receipt.outcome == result.outcome
    assert receipt.outcome_sha256 == result.outcome.canonical_sha256()
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
async def test_authenticated_current_file_route_keeps_exact_context_and_effect_owner(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(
        storage,
        settings,
        text="Authenticated current source.",
        filename="authenticated.txt",
    )
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-success",
    )
    staged: list[object] = []

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda conn: staged.append(conn) is None,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
        bind_authenticated_turn_publication(
            context,
            conversation_id=str(conversation["id"]),
            person_id="alice",
            final_publisher=context.effect_fence.final_publisher,
        ),
    ):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        assert await handler.preparation_is_current(request, turn, plan, preparation) is True
        result = await handler.handle(request, turn, plan, preparation)

    assert result.message == "Источник прочитан. [A1]"
    assert len(staged) == 1
    assert effects.possible is True
    assert effects.staged is False
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 2


@pytest.mark.asyncio
async def test_authenticated_file_route_narrows_parent_output_and_tool_budget(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Budget source.", filename="budget.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-budget",
        max_output_tokens=128,
    )
    assert _two_call_read_model_output_limits(
        context,
        synthesis_max_tokens=512,
        verifier_max_tokens=256,
    ) == (128, 128)

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda _conn: True,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
        bind_authenticated_turn_publication(
            context,
            conversation_id=str(conversation["id"]),
            person_id="alice",
            final_publisher=context.effect_fence.final_publisher,
        ),
    ):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        result = await handler.handle(request, turn, plan, preparation)

    assert result.verified is True
    assert [call["max_tokens"] for call in model.calls] == [128, 128]
    requirements = preparation.private_payload.model_requirements
    assert requirements.max_tool_steps == 0
    assert requirements.max_tool_rounds == 0
    assert requirements.max_tool_calls == 0


@pytest.mark.asyncio
async def test_authenticated_file_route_refuses_insufficient_model_call_ceiling(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Budget source.", filename="budget.txt")
    handler = _handler(storage, settings, _Model("unused"))
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-call-budget",
        max_model_calls=1,
    )

    with bind_authenticated_turn_context(issuer, context):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        with pytest.raises(V12FileReadError, match="no file model-call budget"):
            await handler.prepare(request, turn, plan)


@pytest.mark.asyncio
async def test_authenticated_current_file_route_rejects_drift_and_suspended_context(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Private source.", filename="private.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-reject",
    )

    with bind_authenticated_turn_context(issuer, context):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        drifted = replace(
            request.attachments[0],
            raw_object_id="raw_fedcba9876543210",
            source_identity_sha256="e" * 64,
        )
        with pytest.raises(V12FileReadError, match="inputs drifted"):
            await handler.preparation_is_current(
                replace(request, attachments=(drifted,)),
                turn,
                plan,
                preparation,
            )
        with (
            suspend_authenticated_turn_context(),
            pytest.raises(TurnContextError, match="primary authority"),
        ):
            await handler.preparation_is_current(request, turn, plan, preparation)

    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_authenticated_file_preparation_clamps_to_parent_deadline(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Deadline source.", filename="deadline.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    parent_deadline_ns = time.monotonic_ns() + 3_000_000_000
    request = replace(request, turn_deadline=time.monotonic() + 120.0)
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-deadline",
        deadline_ns=parent_deadline_ns,
    )
    observed: list[float] = []
    original_prepare = handler._prepare_context

    async def capture_deadline(
        candidate_request: ReadOnlyRouteRequest,
        candidate_turn: TurnInput,
        candidate_plan: TurnPlan,
        absolute_deadline: float,
    ) -> Any:
        observed.append(absolute_deadline)
        return await original_prepare(
            candidate_request,
            candidate_turn,
            candidate_plan,
            absolute_deadline,
        )

    monkeypatch.setattr(handler, "_prepare_context", capture_deadline)
    with bind_authenticated_turn_context(issuer, context):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)

    assert preparation is not None
    assert len(observed) == 1
    assert observed[0] < parent_deadline_ns / 1_000_000_000


@pytest.mark.asyncio
async def test_authenticated_file_route_detects_source_mutation_after_model_await(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Mutation source.", filename="mutation.txt")
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-mutation",
    )
    source_token = context.authorized_sources[1].private_carrier
    model = _Model(
        "Источник прочитан. [A1]",
        mutate=lambda: object.__setattr__(source_token, "content_sha256", "d" * 64),
    )
    handler = _handler(storage, settings, model)

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda _conn: True,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        with pytest.raises(V12FileReadError, match="file preparation authority drifted"):
            await handler.handle(request, turn, plan, preparation)

    assert effects.possible is False
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_authenticated_file_route_rejects_conversation_mode_change_after_model_await(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    conversation_id = str(conversation["id"])
    reference = _register(storage, settings, text="Mode source.", filename="mode.txt")
    request, _legacy_turn, plan = _request(reference, conversation_id=conversation_id)
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-file-mode-change",
    )
    model = _Model(
        "Источник прочитан. [A1]",
        mutate=lambda: storage.set_conversation_mode(conversation_id, "alice", "research"),
    )
    handler = _handler(storage, settings, model)
    staged: list[object] = []

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda conn: staged.append(conn) is None,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        with pytest.raises(V12FileReadError, match="conversation mode changed before publication"):
            await handler.handle(request, turn, plan, preparation)

    assert staged == []
    assert effects.possible is False
    assert storage.count_messages(conversation_id, user_id="alice") == 0


@pytest.mark.asyncio
async def test_authenticated_file_route_detects_plan_mutation_after_model_await(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Pinned plan.", filename="plan.txt")
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-plan-mutation",
    )
    original_reason = plan.reason_code
    model = _Model(
        "Источник прочитан. [A1]",
        mutate=lambda: object.__setattr__(plan, "reason_code", "mutated_plan"),
    )
    handler = _handler(storage, settings, model)

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda _conn: True,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        try:
            with pytest.raises(V12FileReadError, match="file preparation authority drifted"):
                await handler.handle(request, turn, plan, preparation)
        finally:
            object.__setattr__(plan, "reason_code", original_reason)

    assert effects.possible is False
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("ordinal", True),
        ("raw_object_id", _EqualStringLookalike("raw_ffffffffffffffff")),
        ("source_identity_sha256", _EqualStringLookalike("f" * 64)),
        ("name", _EqualStringLookalike("other.txt")),
        ("media_type", _EqualStringLookalike("application/octet-stream")),
    ),
)
async def test_authenticated_file_route_rejects_coercible_reference_fields(
    settings: Any,
    storage: Any,
    field: str,
    value: object,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Exact reference.", filename="exact.txt")
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token=f"authenticated-reference-{field}",
    )
    mutated = replace(request.attachments[0], **{field: value})
    request = replace(request, attachments=(mutated,))
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))

    with bind_authenticated_turn_context(issuer, context):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        with pytest.raises(V12FileReadError, match="inputs drifted"):
            await handler.prepare(request, turn, plan)


@pytest.mark.asyncio
async def test_authenticated_file_preparation_rejects_evidence_and_conversation_substitution(
    settings: Any,
    storage: Any,
) -> None:
    first_conversation = storage.create_conversation("alice", mode="dialogue")
    second_conversation = storage.create_conversation("alice", mode="dialogue")
    first_reference = _register(storage, settings, text="First source.", filename="first.txt")
    second_reference = _register(
        storage,
        settings,
        text="Second source.",
        filename="second.txt",
    )
    first_request, _legacy_turn, plan = _request(
        first_reference,
        conversation_id=str(first_conversation["id"]),
    )
    second_request, second_turn, second_plan = _request(
        second_reference,
        conversation_id=str(second_conversation["id"]),
    )
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    second_preparation = await handler.prepare(second_request, second_turn, second_plan)
    assert second_preparation is not None
    issuer, context, first_request, first_turn, carrier = _authenticated_current_file_context(
        storage,
        first_request,
        first_reference,
        token="authenticated-preparation-substitution",
    )

    with bind_authenticated_turn_context(issuer, context):
        _seal_authenticated_file_call_scope(context, first_request, first_turn, carrier)
        first_preparation = await handler.prepare(first_request, first_turn, plan)
        assert first_preparation is not None
        first_payload = first_preparation.private_payload
        second_payload = second_preparation.private_payload
        object.__setattr__(first_payload, "evidence", second_payload.evidence)
        object.__setattr__(
            first_payload,
            "conversation_id",
            str(second_conversation["id"]),
        )
        object.__setattr__(
            first_preparation,
            "evidence_identity_sha256",
            second_payload.evidence.identity_sha256,
        )
        with pytest.raises(V12FileReadError, match="preparation authority is invalid"):
            await handler.handle(first_request, first_turn, plan, first_preparation)


@pytest.mark.asyncio
async def test_authenticated_file_route_rechecks_prepared_authority_after_model_await(
    settings: Any,
    storage: Any,
) -> None:
    conversation = storage.create_conversation("alice", mode="dialogue")
    reference = _register(storage, settings, text="Await source.", filename="await.txt")
    request, _legacy_turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    issuer, context, request, turn, carrier = _authenticated_current_file_context(
        storage,
        request,
        reference,
        token="authenticated-prepared-await-mutation",
    )
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda _conn: True,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        _seal_authenticated_file_call_scope(context, request, turn, carrier)
        preparation = await handler.prepare(request, turn, plan)
        assert preparation is not None
        evidence = preparation.private_payload.evidence
        model.mutate = lambda: object.__setattr__(evidence, "person_id", "mallory")
        with pytest.raises(V12FileReadError, match="preparation authority drifted"):
            await handler.handle(request, turn, plan, preparation)

    assert effects.possible is False
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


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
    assert (
        load_accepted_capability_outcome_receipt(
            assistant_metadata,
            expected_outcome=result.outcome,
        ).outcome
        == result.outcome
    )


@pytest.mark.asyncio
async def test_file_handler_rolls_back_when_accepted_outcome_receipt_exceeds_budget(
    settings,
    storage,
    monkeypatch,
) -> None:
    from friday.orchestration import file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="Receipt is mandatory.", filename="receipt.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    def reject_for_size(metadata: dict[str, Any], outcome: CapabilityOutcome):
        return attach_accepted_capability_outcome_receipt(
            metadata,
            outcome,
            max_serialized_bytes=1,
        )

    monkeypatch.setattr(
        file_read_module,
        "attach_accepted_capability_outcome_receipt",
        reject_for_size,
    )

    with pytest.raises(V12FileReadError, match="receipt rejected publication"):
        await handler.handle(request, turn, plan, preparation)

    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_file_handler_rolls_back_when_durable_outcome_receipt_is_missing(
    settings,
    storage,
    monkeypatch,
) -> None:
    from friday.orchestration import file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="Durable receipt.", filename="durable.txt")
    handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original_store = file_read_module.store_message_in_transaction

    def strip_durable_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        row = original_store(*args, **kwargs)
        if len(args) >= 4 and args[3] == "assistant":
            conn = args[0]
            conn.execute("UPDATE messages SET metadata_json='{}' WHERE id=?", (row["id"],))
            durable = conn.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
            assert durable is not None
            return dict(durable)
        return row

    monkeypatch.setattr(
        file_read_module,
        "store_message_in_transaction",
        strip_durable_receipt,
    )

    with pytest.raises(V12FileReadError, match="not stored durably"):
        await handler.handle(request, turn, plan, preparation)

    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


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
    with pytest.raises(V12FileReadError, match="authority changed before model call"):
        await handler.handle(request, turn, plan, preparation)

    assert model.calls == []
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_prepared_file_turn_stored_seal_rejects_exact_lease_transplant(
    settings,
    storage,
) -> None:
    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="SEALED-SOURCE", filename="sealed.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    prepared = preparation.private_payload
    original_lease = prepared.model_lease
    replacement = await model.acquire_lease(
        prepared.model_requirements,
        absolute_deadline=time.monotonic() + 1,
    )
    assert type(replacement) is ModelProfileLease
    assert replacement is not original_lease

    with pytest.raises(ValueError, match="not process-owned"):
        replace(prepared, model_lease=replacement)


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

    with pytest.raises(V12FileReadError, match="authority changed before model call"):
        await handler.handle(request, turn, plan, preparation)

    assert model.lease is original_lease
    assert len(model.calls) == 1
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_epoch_loss_after_final_reauth_rolls_back_before_effect_or_messages(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="FINAL-LEASE-SOURCE", filename="final.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original_lease = model.lease
    original_reauthorize = file_read_module.reauthorize_prepared_file_evidence_in_transaction
    original_current = model.lease_is_current
    original_process_current = model.lease_is_process_current
    original_stage = file_read_module.stage_request_effect_possible_in_transaction
    publication_conn: sqlite3.Connection | None = None
    reauthorization_completed = False
    remote_check_transactions: list[bool] = []
    process_check_transactions: list[bool] = []
    effect_stage_calls = 0

    def revoke_after_reauthorization(*args: Any, **kwargs: Any) -> bool:
        nonlocal publication_conn, reauthorization_completed
        accepted = original_reauthorize(*args, **kwargs)
        assert accepted is True
        publication_conn = args[0]
        reauthorization_completed = True
        model.lease_current = False
        return accepted

    async def observe_current(
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        if len(model.calls) == 2:
            remote_check_transactions.append(bool(storage.conn.in_transaction))
        return await original_current(
            lease,
            requirements,
            absolute_deadline=absolute_deadline,
        )

    def observe_process_current(
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        assert reauthorization_completed and publication_conn is not None
        process_check_transactions.append(publication_conn.in_transaction)
        return original_process_current(lease, requirements)

    def observe_stage(*args: Any, **kwargs: Any) -> bool:
        nonlocal effect_stage_calls
        effect_stage_calls += 1
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(
        file_read_module,
        "reauthorize_prepared_file_evidence_in_transaction",
        revoke_after_reauthorization,
    )
    monkeypatch.setattr(model, "lease_is_current", observe_current)
    monkeypatch.setattr(model, "lease_is_process_current", observe_process_current)
    monkeypatch.setattr(file_read_module, "stage_request_effect_possible_in_transaction", observe_stage)

    with pytest.raises(V12FileReadError, match="authority changed before publication"):
        await handler.handle(request, turn, plan, preparation)

    assert remote_check_transactions == [False]
    assert process_check_transactions == [True]
    assert publication_conn is not None and publication_conn.in_transaction is False
    assert effect_stage_calls == 0
    assert len(model.calls) == 2
    assert model.lease is original_lease
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_process_epoch_loss_before_file_commit_rolls_back_staged_publication(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="COMMIT-LEASE-SOURCE", filename="commit.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original_process_current = model.lease_is_process_current
    original_stage = file_read_module.stage_request_effect_possible_in_transaction
    process_check_transactions: list[bool] = []
    effect_stage_calls = 0

    def revoke_before_commit(
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        process_check_transactions.append(bool(storage.conn.in_transaction))
        if len(process_check_transactions) == 2:
            model.lease_current = False
        return original_process_current(lease, requirements)

    def observe_stage(*args: Any, **kwargs: Any) -> bool:
        nonlocal effect_stage_calls
        effect_stage_calls += 1
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(model, "lease_is_process_current", revoke_before_commit)
    monkeypatch.setattr(file_read_module, "stage_request_effect_possible_in_transaction", observe_stage)

    with pytest.raises(V12FileReadError, match="authority changed before transaction commit"):
        await handler.handle(request, turn, plan, preparation)

    assert process_check_transactions == [True, True]
    assert storage.conn.in_transaction is False
    assert effect_stage_calls == 1
    assert len(model.calls) == 2
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_cancellation_during_final_remote_lease_check_precedes_transaction(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.file_read as file_read_module

    conversation = storage.create_conversation("alice")
    reference = _register(storage, settings, text="CANCEL-LEASE-SOURCE", filename="cancel.txt")
    model = _Model("Источник прочитан. [A1]")
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=str(conversation["id"]))
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    original_reauthorize = file_read_module.reauthorize_prepared_file_evidence_in_transaction
    original_current = model.lease_is_current
    original_stage = file_read_module.stage_request_effect_possible_in_transaction
    final_check_started = asyncio.Event()
    reauthorization_calls = 0
    effect_stage_calls = 0

    def mark_reauthorization(*args: Any, **kwargs: Any) -> bool:
        nonlocal reauthorization_calls
        reauthorization_calls += 1
        return original_reauthorize(*args, **kwargs)

    async def block_final_current(
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        if len(model.calls) == 2:
            assert storage.conn.in_transaction is False
            final_check_started.set()
            await asyncio.Future()
        return await original_current(
            lease,
            requirements,
            absolute_deadline=absolute_deadline,
        )

    def observe_stage(*args: Any, **kwargs: Any) -> bool:
        nonlocal effect_stage_calls
        effect_stage_calls += 1
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(
        file_read_module,
        "reauthorize_prepared_file_evidence_in_transaction",
        mark_reauthorization,
    )
    monkeypatch.setattr(model, "lease_is_current", block_final_current)
    monkeypatch.setattr(file_read_module, "stage_request_effect_possible_in_transaction", observe_stage)

    task = asyncio.create_task(handler.handle(request, turn, plan, preparation))
    await asyncio.wait_for(final_check_started.wait(), timeout=1.0)
    isolation_key = "test:s5:file-publication-split-phase"
    storage.kv_set(isolation_key, "committed outside publication")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert storage.conn.in_transaction is False
    assert reauthorization_calls == 0
    assert effect_stage_calls == 0
    assert model.process_lease_checks == 0
    assert len(model.calls) == 2
    assert storage.kv_get(isolation_key) == "committed outside publication"
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0
    await asyncio.sleep(0)
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
    assert ACCEPTED_CAPABILITY_OUTCOME_METADATA_KEY not in result.response(conversation_mode="dialogue")


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
    legacy_handler = _handler(storage, settings, _Model("Ответ. [A1]"))
    request, turn, plan = _request(reference, conversation_id=None)

    assert await legacy_handler.prepare(request, turn, plan) is None

    measured_model = _MeasuredContextModel(
        "Ответ. [A1]",
        available_context_tokens=40_960,
    )
    measured_handler = _handler(storage, settings, measured_model)
    preparation = await measured_handler.prepare(request, turn, plan)

    assert preparation is not None
    assert preparation.private_payload.model_requirements is _file_requirements(1, 16_384)
    synthesis_messages = measured_handler._synthesis_messages(  # noqa: SLF001
        turn,
        plan,
        preparation.private_payload.evidence.bundle,
    )
    empty_verifier_messages = measured_handler._verifier_messages(  # noqa: SLF001
        turn,
        preparation.private_payload.evidence.bundle,
        "",
    )
    serialized_empty_verifier_bytes = len(
        json.dumps(
            empty_verifier_messages,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    empty_answer_bytes = len(json.dumps("", ensure_ascii=False).encode("utf-8"))
    reserved_verifier_bytes = serialized_empty_verifier_bytes + 2 * (
        _MAX_ANSWER_JSON_UTF8_BYTES - empty_answer_bytes
    )
    assert _messages_fit_attested_context(synthesis_messages, 8_192)
    assert serialized_empty_verifier_bytes <= _attested_input_max_bytes(8_192)
    assert reserved_verifier_bytes > _attested_input_max_bytes(8_192)


@pytest.mark.asyncio
async def test_measured_q38_selects_the_least_exact_context_and_legacy_stays_at_8k(
    settings,
    storage,
) -> None:
    small_reference = _register(storage, settings, text="Короткий источник.", filename="small.txt")
    small_model = _MeasuredContextModel(
        "Источник прочитан. [A1]",
        available_context_tokens=40_960,
    )
    small_handler = _handler(storage, settings, small_model)
    small_request, small_turn, small_plan = _request(small_reference, conversation_id=None)

    small_preparation = await small_handler.prepare(small_request, small_turn, small_plan)

    assert small_preparation is not None
    assert small_model.acquire_calls == 1
    assert small_preparation.private_payload.model_requirements is _file_requirements(1, 8_192)

    large_reference = _register(storage, settings, text="L" * 6_000, filename="large-measured.txt")
    large_model = _MeasuredContextModel(
        "Источник прочитан. [A1]",
        available_context_tokens=40_960,
    )
    large_handler = _handler(storage, settings, large_model)
    large_request, large_turn, large_plan = _request(large_reference, conversation_id=None)

    large_preparation = await large_handler.prepare(large_request, large_turn, large_plan)

    assert large_preparation is not None
    assert large_model.acquire_calls == 1
    assert large_preparation.private_payload.model_requirements is _file_requirements(1, 24_576)
    synthesis_messages = large_handler._synthesis_messages(  # noqa: SLF001
        large_turn,
        large_plan,
        large_preparation.private_payload.evidence.bundle,
    )
    assert not _messages_fit_attested_context(synthesis_messages, 8_192)
    assert _messages_fit_attested_context(synthesis_messages, 40_960)

    legacy_handler = _handler(storage, settings, _Model("Источник прочитан. [A1]"))
    legacy_request, legacy_turn, legacy_plan = _request(large_reference, conversation_id=None)
    assert await legacy_handler.prepare(legacy_request, legacy_turn, legacy_plan) is None


@pytest.mark.asyncio
async def test_measured_context_rejected_acquire_is_not_retried(settings, storage) -> None:
    reference = _register(storage, settings, text="R" * 6_000, filename="rejected.txt")
    model = _MeasuredContextModel(
        "Источник прочитан. [A1]",
        available_context_tokens=40_960,
        reject_acquire=True,
    )
    handler = _handler(storage, settings, model)
    request, turn, plan = _request(reference, conversation_id=None)

    assert await handler.prepare(request, turn, plan) is None
    assert model.acquire_calls == 1
    assert model.acquired_requirements == [_file_requirements(1, 24_576)]
    assert model.calls == []


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
