from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import itertools
import math
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from friday import execution_kernel as execution_kernel_module
from friday import semantic_supervisor_policy
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _CurrentDocumentSecondaryMapPlan,
    _project_pending_durable_admission_for_agent_leaf,
)
from friday.execution_kernel import (
    bind_authenticated_request_effect_authority,
    track_request_effects,
)
from friday.file_evidence import stamp_current_turn_file_reference_for_tenant
from friday.orchestration import semantic_supervisor_runtime as supervisor_runtime_module
from friday.orchestration import turn_context_call_scope as call_scope_module
from friday.orchestration import turn_context_publication as publication_module
from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.router import OrchestrationRouter, _authenticated_attachment_references
from friday.orchestration.semantic_supervisor_runtime import SemanticSupervisorShadowRuntime
from friday.orchestration.supervisor_assist_controller import (
    SupervisorAssistOutcome,
    SupervisorAssistResult,
)
from friday.orchestration.supervisor_assist_ingress import SupervisorAssistIngressBindingV1
from friday.orchestration.supervisor_assist_runtime import SemanticSupervisorAssistRuntime
from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    FinalPublisher,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.orchestration.turn_context_call_scope import (
    AuthenticatedChatCallScope,
    require_authenticated_chat_call_scope,
    require_current_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_publication import bind_authenticated_turn_publication
from friday.orchestration.turn_context_runtime import (
    bind_authenticated_turn_context,
    current_authenticated_turn_context,
    current_primary_authenticated_turn_context,
    suspend_authenticated_turn_context,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.secondary_brain import SecondaryAttempt, SecondaryFailure
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_SERIALS = itertools.count(700_000)
_CONVERSATION_ID = "conv_abcdef0123456789"
_MESSAGE = "Сравни мой архив с актуальными правилами в интернете."


def _authenticated_turn(
    *,
    router_mode: RouterMode = RouterMode.SHADOW,
    max_advisory_calls: int = 1,
    clock: list[int] | None = None,
    telegram: bool = False,
    pending: PendingDurableTurnAdmission | None = None,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext, ActorContext, list[int]]:
    serial = next(_SERIALS)
    now = clock or [time.monotonic_ns()]
    issuer = TurnContextIssuer(
        hashlib.sha256(f"router-supervisor-{serial}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now[0],
    )
    actor = (
        ActorContext(
            user_id="owner",
            preset_key="owner",
            source="telegram-bridge",
            identity_id="123456789",
            session_id="owner-session",
            telegram_chat_id="-100123456789",
        )
        if telegram
        else ActorContext(
            user_id="owner",
            preset_key="owner",
            source="api-token",
            identity_id="owner-principal",
            session_id="owner-session",
        )
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.TELEGRAM if telegram else IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-router-supervisor-{serial}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=str(actor.telegram_chat_id) if telegram else "api-token",
        update_id=str(serial) if telegram else f"update-router-supervisor-{serial}",
        request_effect_binding_sha256=hashlib.sha256(
            f"effects-router-supervisor-{serial}".encode("ascii")
        ).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message=_MESSAGE,
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    effective_router_mode = RouterMode.LEGACY if pending is not None else router_mode
    policy = issuer.issue_turn_policy(
        router_mode=effective_router_mode,
        fallback_router_mode=(None if effective_router_mode is RouterMode.LEGACY else RouterMode.LEGACY),
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now[0] + 120_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, max_advisory_calls, 8_192),
        ),
        pending_work_admission=(
            issuer.bind_pending_work(authority=authority, admission=pending) if pending is not None else None
        ),
    )
    return issuer, context, actor, now


class _AttachmentCarrier(dict[str, Any]):
    pass


def _authenticated_attachment_turn(
    *,
    clock: list[int] | None = None,
    message: str = "Прочитай приложенный файл.",
) -> tuple[
    TurnContextIssuer,
    AuthenticatedTurnContext,
    ActorContext,
    _AttachmentCarrier,
]:
    serial = next(_SERIALS)
    now = time.monotonic_ns() if clock is None else clock[0]
    issuer = TurnContextIssuer(
        hashlib.sha256(f"router-attachment-{serial}".encode("ascii")).digest(),
        _monotonic_ns=(time.monotonic_ns if clock is None else lambda: clock[0]),
    )
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-router-attachment-{serial}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id="api-token",
        update_id=f"update-router-attachment-{serial}",
        request_effect_binding_sha256=hashlib.sha256(
            f"effects-router-attachment-{serial}".encode("ascii")
        ).hexdigest(),
    )
    raw_id = f"raw_{serial:016x}"
    carrier = _AttachmentCarrier(
        filename="report.pdf",
        raw_object_id=raw_id,
        knowledge_object_id=None,
        mime_type="application/pdf",
        size_bytes=321,
        transient_text="bounded extracted text",
        extraction_success=True,
        extraction_error="",
        text_truncated=False,
        advisory_only=False,
        verification_eligible=True,
        parse_deadline_reached=False,
        parse_pages_read=1,
        parse_pages_truncated=False,
        parse_total_pages=1,
        archive_truncated=False,
        source_truncated_for_parse=False,
        persisted=True,
        current_turn_only=True,
        # Future server-owned attestation objects may be non-JSON.  They are
        # identity-bound, while every model-visible body field above is hashed.
        process_marker=object(),
    )
    stamp_current_turn_file_reference_for_tenant(
        carrier,
        {
            "id": raw_id,
            "user_id": actor.user_id,
            "source": "api",
            "source_ref": f"current:{serial}",
            "content_type": "application/pdf",
            "received_at": "2026-08-29T00:00:00Z",
            "content_hash": "a" * 64,
            "raw_content": "bounded extracted text",
            "metadata_json": "{}",
        },
        tenant_id=actor.user_id,
    )
    model_input = TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        attachments=[carrier],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    source = issuer.current_attachment_source(
        authority=authority,
        carrier=carrier,
        descriptor=model_input.attachments[0],
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority), source),
        turn_policy=issuer.issue_turn_policy(
            router_mode=RouterMode.V12,
            fallback_router_mode=RouterMode.LEGACY,
            decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
        ),
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now + 120_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, 1, 8_192),
        ),
        pending_work_admission=None,
    )
    return issuer, context, actor, carrier


def _chat_kwargs(
    actor: ActorContext,
    context: AuthenticatedTurnContext | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "actor": actor,
        "conversation_id": _CONVERSATION_ID,
        "attachments": None,
        "enable_tools": True,
    }
    if context is not None:
        values["turn_deadline"] = _exact_deadline_float(context)
        if context.authority.ingress_kind is IngressKind.TELEGRAM:
            values["telegram_update_id"] = context.authority.update_id
        if context.pending_work_admission is not None:
            values["_pending_durable_admission"] = context.pending_work_admission.admission
    return values


def _exact_deadline_float(context: AuthenticatedTurnContext) -> float:
    target = context.inherited_budget.safety_deadline.monotonic_ns
    candidate = target / 1_000_000_000
    for _ in range(4):
        observed = int(candidate * 1_000_000_000)
        if observed == target:
            return candidate
        candidate = math.nextafter(candidate, math.inf if observed < target else -math.inf)
    raise AssertionError("test deadline cannot be represented by the raw float carrier")


def _scope_kwargs(
    actor: ActorContext,
    context: AuthenticatedTurnContext,
    *,
    attachments: list[dict[str, Any]] | None = None,
    kg: Any = None,
    hybrid_searcher: Any = None,
    ingestion_result: dict[str, Any] | None = None,
    runtime_router_mode: RouterMode | None = None,
) -> dict[str, Any]:
    return {
        "user_id": "owner",
        "message": context.model_input.message,
        "actor": actor,
        "conversation_id": _CONVERSATION_ID,
        "attachments": attachments,
        "enable_tools": True,
        "synthetic_document_notice": False,
        "replay_source_message_id": None,
        "mode": None,
        "answer_with_voice": False,
        "reply_to": None,
        "quoted_attachment_reference": False,
        "reply_assistant_reference": False,
        "reply_assistant_message_id": None,
        "turn_policy": None,
        "telegram_update_id": None,
        "turn_deadline": _exact_deadline_float(context),
        "pending_durable_admission": (
            context.pending_work_admission.admission if context.pending_work_admission is not None else None
        ),
        "kg": kg,
        "hybrid_searcher": hybrid_searcher,
        "ingestion_result": ingestion_result,
        "runtime_router_mode": runtime_router_mode,
    }


class _Primary:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] = {}
        self.calls = 0

    def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    async def chat(self, _user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.last_kwargs = kwargs
        return {"conversation_id": kwargs.get("conversation_id"), "message": "primary"}


class _Planner:
    def __init__(self) -> None:
        self.turns: list[TurnInput] = []
        self.detached_primary_closed = False

    async def plan(self, turn: TurnInput, **_kwargs: Any) -> None:
        self.turns.append(turn)
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        self.detached_primary_closed = True
        return None

    async def plan_attested(self, turn: TurnInput, **kwargs: Any) -> None:
        return await self.plan(turn, **kwargs)


class _Scheduler:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.calls = 0
        self.detached_primary_closed = False

    async def evaluate_shadow(self, _request: Any, **_kwargs: Any) -> SecondaryAttempt:
        self.calls += 1
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        self.detached_primary_closed = True
        if self.gate is not None:
            await self.gate.wait()
        return SecondaryAttempt.rejected(SecondaryFailure.TIMEOUT)


def _supervisor_settings() -> SimpleNamespace:
    return SimpleNamespace(
        semantic_supervisor_mode="shadow",
        semantic_supervisor_tasks=("compare_archive_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )


@pytest.mark.asyncio
async def test_exact_authenticated_current_file_web_turn_reaches_assist_without_reclassification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_assist_surface as assist_surface_module

    message = "Сравни приложенный файл с актуальными правилами в интернете."
    issuer, context, actor, carrier = _authenticated_attachment_turn(message=message)
    ingress = SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref="authenticated-current-file-web",
        request_fingerprint_sha256="e" * 64,
    )
    surface = object.__new__(CurrentFileWebAssistSurface)
    constructor_calls = 0

    def prepare_authenticated_surface(
        _settings: object,
        *,
        authenticated_context: AuthenticatedTurnContext,
        authenticated_scope: AuthenticatedChatCallScope,
        ingress_binding: SupervisorAssistIngressBindingV1 | None,
        explicit_mode_requested: bool,
        conversation_is_dialogue: Any,
    ) -> CurrentFileWebAssistSurface:
        nonlocal constructor_calls
        constructor_calls += 1
        assert authenticated_context is expected_context
        assert authenticated_scope is require_current_authenticated_chat_call_scope(authenticated_context)
        assert authenticated_scope.attachment_carriers == (carrier,)
        assert ingress_binding is ingress
        assert explicit_mode_requested is False
        assert conversation_is_dialogue(actor.own_id, _CONVERSATION_ID) is True
        return surface

    monkeypatch.setattr(
        assist_surface_module,
        "prepare_authenticated_current_file_web_assist_surface",
        prepare_authenticated_surface,
        raising=False,
    )

    class Controller:
        def __init__(self) -> None:
            self.classify_calls = 0
            self.execute_calls = 0

        def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

        def classify_supervisor_assist_pending(self, *_args: Any, **_kwargs: Any) -> bool:
            self.classify_calls += 1
            raise AssertionError("authenticated pending-free successor was reclassified")

        async def execute(
            self,
            admitted_surface: object,
            *,
            legacy_primary: Any,
            absolute_deadline: float,
        ) -> SupervisorAssistResult:
            del legacy_primary
            assert admitted_surface is surface
            assert absolute_deadline > 0
            self.execute_calls += 1
            return SupervisorAssistResult(
                outcome=SupervisorAssistOutcome.PUBLISHED,
                response={"conversation_id": _CONVERSATION_ID, "message": "promoted"},
            )

        async def cancel_active(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def reconcile_pending_before_legacy(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def close(self) -> None:
            return None

    expected_context = context
    primary = _Primary()
    controller = Controller()
    runtime = SemanticSupervisorAssistRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_mode="assist",
            semantic_supervisor_timeout_sec=12.0,
        ),
        primary=primary,
        controller=cast(Any, controller),
        conversation_is_dialogue=lambda person, conversation: (
            person == actor.own_id and conversation == _CONVERSATION_ID
        ),
    )
    ingestion = {
        "promoted": False,
        "queued_for_review": False,
        "action": "transient",
        "category": "web_request",
        "reason": "explicit current public web request",
    }

    with bind_authenticated_turn_context(issuer, context):
        response = await runtime.chat(
            actor.user_id,
            message,
            actor=actor,
            conversation_id=_CONVERSATION_ID,
            attachments=[carrier],
            enable_tools=True,
            ingestion_result=ingestion,
            turn_deadline=_exact_deadline_float(context),
            _semantic_supervisor_ingress_binding=ingress,
            _authenticated_turn_context=context,
        )

    assert response["message"] == "promoted"
    assert constructor_calls == 1
    assert controller.classify_calls == 0
    assert controller.execute_calls == 1
    assert primary.calls == 0


@pytest.mark.asyncio
async def test_router_carries_exact_context_and_exact_model_input_into_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer, context, actor, _now = _authenticated_turn()
    primary = _Primary()
    planner = _Planner()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, planner), mode=RouterMode.SHADOW)
    callback_contexts: list[AuthenticatedTurnContext | None] = []
    original_done = router._shadow_done

    def observed_done(task: asyncio.Task[None]) -> None:
        callback_contexts.append(current_authenticated_turn_context())
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        original_done(task)

    monkeypatch.setattr(router, "_shadow_done", observed_done)

    with bind_authenticated_turn_context(issuer, context):
        result = await router.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, context),
            _authenticated_turn_context=context,
        )
        assert current_primary_authenticated_turn_context(context) is context
    await router.drain_shadow()
    await asyncio.sleep(0)

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert planner.turns == [context.model_input]
    assert planner.turns[0] is context.model_input
    assert planner.detached_primary_closed is True
    assert callback_contexts == [None]


@pytest.mark.asyncio
async def test_router_fallback_carries_exact_context_without_rebuilding_turn() -> None:
    issuer, context, actor, _now = _authenticated_turn(router_mode=RouterMode.V12)
    primary = _Primary()
    planner = _Planner()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, planner), mode=RouterMode.V12)

    with bind_authenticated_turn_context(issuer, context):
        result = await router.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, context),
            _authenticated_turn_context=context,
        )

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert planner.turns == [context.model_input]


@pytest.mark.asyncio
async def test_router_rejects_suspended_stale_and_identity_drift_before_primary() -> None:
    issuer, context, actor, now = _authenticated_turn()
    _other_issuer, other, _other_actor, _other_now = _authenticated_turn()
    primary = _Primary()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, _Planner()), mode=RouterMode.SHADOW)
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, primary),
        scheduler=cast(Any, _Scheduler()),
    )

    with bind_authenticated_turn_context(issuer, context):
        with suspend_authenticated_turn_context(), pytest.raises(TurnContextError, match="primary authority"):
            await router.chat("owner", _MESSAGE, **_chat_kwargs(actor, context))
        with suspend_authenticated_turn_context(), pytest.raises(TurnContextError, match="primary authority"):
            await wrapper.chat("owner", _MESSAGE, **_chat_kwargs(actor, context))
        with pytest.raises(TurnContextError, match="identity drifted"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                _authenticated_turn_context=other,
            )
        with pytest.raises(TurnContextError, match="identity drifted"):
            await wrapper.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                _authenticated_turn_context=other,
            )
        now[0] = context.inherited_budget.safety_deadline.monotonic_ns
        with pytest.raises(TurnContextError, match="deadline"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="deadline"):
            await wrapper.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                _authenticated_turn_context=context,
            )

    assert primary.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "actor",
        "conversation_id",
        "message",
        "attachments",
        "enable_tools",
        "synthetic_document_notice",
        "replay_source_message_id",
        "mode",
        "answer_with_voice",
        "reply_to",
        "quoted_attachment_reference",
        "reply_assistant_reference",
        "reply_assistant_message_id",
        "turn_policy",
        "telegram_update_id",
        "turn_deadline",
        "_pending_durable_admission",
    ],
)
async def test_every_authenticated_raw_call_drift_fails_before_primary(drift: str) -> None:
    issuer, context, actor, _now = _authenticated_turn()
    primary = _Primary()
    router = OrchestrationRouter(
        cast(Any, primary),
        cast(Any, _Planner()),
        mode=RouterMode.SHADOW,
    )
    kwargs = _chat_kwargs(actor, context)
    message = _MESSAGE
    if drift == "actor":
        kwargs[drift] = dataclasses.replace(actor)
    elif drift == "conversation_id":
        kwargs[drift] = "conv_1111111111111111"
    elif drift == "message":
        message = f"{_MESSAGE} drift"
    elif drift == "attachments":
        kwargs[drift] = [{}]
    elif drift in {
        "enable_tools",
        "synthetic_document_notice",
        "answer_with_voice",
        "quoted_attachment_reference",
        "reply_assistant_reference",
    }:
        kwargs[drift] = drift != "enable_tools"
    elif drift == "mode":
        kwargs[drift] = "research"
    elif drift == "turn_policy":
        kwargs[drift] = TurnPolicyDecision(intent=TurnIntent.META_CAPABILITIES)
    elif drift == "turn_deadline":
        kwargs[drift] = float(kwargs[drift]) + 1.0
    elif drift == "_pending_durable_admission":
        kwargs[drift] = PendingDurableTurnAdmission.owned(
            person_id="owner",
            conversation_id=_CONVERSATION_ID,
        )
    else:
        kwargs[drift] = "drifted-identity"

    with (
        bind_authenticated_turn_context(issuer, context),
        pytest.raises(TurnContextError, match="authenticated turn|signed HTTP"),
    ):
        await router.chat(
            "owner",
            message,
            **kwargs,
            _authenticated_turn_context=context,
        )

    assert primary.calls == 0


@pytest.mark.asyncio
async def test_sealed_router_mode_controls_legacy_and_nonlegacy_mismatch_closes() -> None:
    mismatch_issuer, mismatch, mismatch_actor, _now = _authenticated_turn(router_mode=RouterMode.SHADOW)
    mismatch_primary = _Primary()
    mismatch_router = OrchestrationRouter(
        cast(Any, mismatch_primary),
        cast(Any, _Planner()),
        mode=RouterMode.V12,
    )
    with (
        bind_authenticated_turn_context(mismatch_issuer, mismatch),
        pytest.raises(TurnContextError, match="router mode"),
    ):
        await mismatch_router.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(mismatch_actor, mismatch),
            _authenticated_turn_context=mismatch,
        )
    assert mismatch_primary.calls == 0

    pending = PendingDurableTurnAdmission.owned(
        person_id="owner",
        conversation_id=_CONVERSATION_ID,
    )
    issuer, context, actor, _now = _authenticated_turn(
        router_mode=RouterMode.V12,
        pending=pending,
    )
    primary = _Primary()
    router = OrchestrationRouter(
        cast(Any, primary),
        cast(Any, _Planner()),
        mode=RouterMode.V12,
    )
    kwargs = _chat_kwargs(actor, context)
    with bind_authenticated_turn_context(issuer, context):
        result = await router.chat(
            "owner",
            _MESSAGE,
            **kwargs,
            _authenticated_turn_context=context,
        )
        wrong = dict(kwargs)
        wrong["_pending_durable_admission"] = dataclasses.replace(pending)
        with pytest.raises(TurnContextError, match="pending-work"):
            await router.chat(
                "owner",
                _MESSAGE,
                **wrong,
                _authenticated_turn_context=context,
            )
    assert result["message"] == "primary"
    assert primary.last_kwargs["_pending_durable_admission"] is pending


@pytest.mark.parametrize("binding_kind", ("work_graph", "work_item"))
def test_agent_leaf_projects_only_authenticated_work_graph_admission(
    binding_kind: str,
) -> None:
    binding = (
        {"work_graph_id": "graph_0123456789abcdef"}
        if binding_kind == "work_graph"
        else {"work_item_id": "work_0123456789abcdef"}
    )
    admission = PendingDurableTurnAdmission.owned(
        person_id="owner",
        conversation_id=_CONVERSATION_ID,
        revision=3,
        **binding,
    )
    issuer, context, actor, _now = _authenticated_turn(pending=admission)
    kwargs = _scope_kwargs(actor, context, runtime_router_mode=RouterMode.LEGACY)

    with bind_authenticated_turn_context(issuer, context):
        scope = require_authenticated_chat_call_scope(context, **kwargs)
        projected = _project_pending_durable_admission_for_agent_leaf(
            authenticated_turn_context=context,
            authenticated_call_scope=scope,
            carried_admission=admission,
        )
        assert context.pending_work_admission is not None
        assert context.pending_work_admission.admission is admission
        with pytest.raises(TurnContextError, match="pending admission drifted"):
            _project_pending_durable_admission_for_agent_leaf(
                authenticated_turn_context=context,
                authenticated_call_scope=scope,
                carried_admission=dataclasses.replace(admission),
            )

    assert projected is (None if binding_kind == "work_graph" else admission)
    assert (
        _project_pending_durable_admission_for_agent_leaf(
            authenticated_turn_context=None,
            authenticated_call_scope=None,
            carried_admission=admission,
        )
        is admission
    )


def test_authenticated_scope_projects_float_deadline_below_integer_parent() -> None:
    parent_deadline_ns = 16_809_353_055_389
    raw_float_carrier = 16_809.35305539
    issuer, context, actor, _now = _authenticated_turn(clock=[parent_deadline_ns - 120_000_000_000])
    kwargs = _scope_kwargs(actor, context)
    kwargs["turn_deadline"] = raw_float_carrier

    with bind_authenticated_turn_context(issuer, context):
        scope = require_authenticated_chat_call_scope(context, **kwargs)

    parent_float = parent_deadline_ns / 1_000_000_000
    assert scope.deadline_monotonic == raw_float_carrier
    assert scope.deadline_monotonic > parent_float
    assert scope.conservative_deadline_monotonic < parent_float


@pytest.mark.asyncio
async def test_telegram_update_must_be_the_exact_authenticated_update() -> None:
    issuer, context, actor, _now = _authenticated_turn(
        router_mode=RouterMode.LEGACY,
        telegram=True,
    )
    primary = _Primary()
    router = OrchestrationRouter(
        cast(Any, primary),
        cast(Any, _Planner()),
        mode=RouterMode.V12,
    )
    kwargs = _chat_kwargs(actor, context)
    with bind_authenticated_turn_context(issuer, context):
        await router.chat(
            "owner",
            _MESSAGE,
            **kwargs,
            _authenticated_turn_context=context,
        )
        wrong = dict(kwargs)
        wrong["telegram_update_id"] = "999999999"
        with pytest.raises(TurnContextError, match="Telegram update"):
            await router.chat(
                "owner",
                _MESSAGE,
                **wrong,
                _authenticated_turn_context=context,
            )
    assert primary.calls == 1


def test_v12_attachment_refs_use_exact_context_token_and_reject_equal_replacement() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()
    kwargs: dict[str, Any] = {
        "user_id": "owner",
        "message": context.model_input.message,
        "actor": actor,
        "conversation_id": _CONVERSATION_ID,
        "attachments": [carrier],
        "enable_tools": True,
        "synthetic_document_notice": False,
        "replay_source_message_id": None,
        "mode": None,
        "answer_with_voice": False,
        "reply_to": None,
        "quoted_attachment_reference": False,
        "reply_assistant_reference": False,
        "reply_assistant_message_id": None,
        "turn_policy": None,
        "telegram_update_id": None,
        "turn_deadline": _exact_deadline_float(context),
        "pending_durable_admission": None,
        "runtime_router_mode": RouterMode.V12,
    }
    with bind_authenticated_turn_context(issuer, context):
        scope = require_authenticated_chat_call_scope(context, **kwargs)
        references = _authenticated_attachment_references(scope)
        token = context.authorized_sources[1].private_carrier
        assert references[0].raw_object_id == token.raw_id
        assert references[0].source_identity_sha256 == token.source_identity_sha256

        replacement = dataclasses.replace(token)
        clone = _AttachmentCarrier(carrier)
        object.__setattr__(clone, "_current_turn_file_reference", replacement)
        kwargs["attachments"] = [clone]
        with pytest.raises(TurnContextError, match="attachment carrier"):
            require_authenticated_chat_call_scope(context, **kwargs)


def test_authenticated_attachment_seal_rejects_same_token_clone_and_body_mutation() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()
    kwargs = _scope_kwargs(
        actor,
        context,
        attachments=[carrier],
        runtime_router_mode=RouterMode.V12,
    )
    with bind_authenticated_turn_context(issuer, context):
        scope = require_authenticated_chat_call_scope(context, **kwargs)
        token = context.authorized_sources[1].private_carrier

        clone = _AttachmentCarrier(carrier)
        object.__setattr__(clone, "_current_turn_file_reference", token)
        clone["transient_text"] = "replaced body under the exact token"
        kwargs["attachments"] = [clone]
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            require_authenticated_chat_call_scope(context, **kwargs)

        kwargs["attachments"] = [carrier]
        carrier["transient_text"] = "mutated after initial validation"
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            require_authenticated_chat_call_scope(context, **kwargs)

        assert scope.attachment_carriers[0] is carrier


def test_authenticated_attachment_scope_binding_rejects_whole_carrier_substitution() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()
    kwargs = _scope_kwargs(
        actor,
        context,
        attachments=[carrier],
        runtime_router_mode=RouterMode.V12,
    )
    with bind_authenticated_turn_context(issuer, context):
        scope = require_authenticated_chat_call_scope(context, **kwargs)
        original_carriers = scope.attachment_carriers
        original_hashes = scope.attachment_carrier_sha256
        clone = _AttachmentCarrier(carrier)
        object.__setattr__(
            clone,
            "_current_turn_file_reference",
            context.authorized_sources[1].private_carrier,
        )
        clone["transient_text"] = "substituted body"
        try:
            object.__setattr__(scope, "attachment_carriers", (clone,))
            object.__setattr__(
                scope,
                "attachment_carrier_sha256",
                (
                    call_scope_module._process_local_projection_sha256(  # noqa: SLF001
                        clone,
                        label="attachment 1",
                    ),
                ),
            )
            with pytest.raises(TurnContextError, match="chat call scope drifted"):
                require_current_authenticated_chat_call_scope(context)
        finally:
            object.__setattr__(scope, "attachment_carriers", original_carriers)
            object.__setattr__(scope, "attachment_carrier_sha256", original_hashes)


def test_authenticated_raw_call_rejects_conversation_and_optional_carrier_lookalikes() -> None:
    issuer, context, actor, _now = _authenticated_turn(router_mode=RouterMode.LEGACY)

    class EqualConversation(str):
        def __eq__(self, _other: object) -> bool:
            return True

    class StatefulText:
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "" if self.calls == 1 else "engineer"

    with bind_authenticated_turn_context(issuer, context):
        for field, value in (
            ("conversation_id", EqualConversation("conv_ffffffffffffffff")),
            ("reply_to", ""),
            ("reply_to", StatefulText()),
            ("mode", "dialogue"),
            ("mode", StatefulText()),
        ):
            kwargs = _scope_kwargs(actor, context)
            kwargs[field] = value
            with pytest.raises(TurnContextError):
                require_authenticated_chat_call_scope(context, **kwargs)


def _agent_model_context(context: AuthenticatedTurnContext) -> AgentContext:
    return AgentContext(
        conversation_id=_CONVERSATION_ID,
        user_id="owner",
        person_id="owner",
        current_attachment_present=True,
        focused_attachment_turn=True,
        _authenticated_turn_context=context,
    )


def _agent_model_runtime(llm: Any, *, secondary: Any = None) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime.llm = llm
    runtime.secondary_brain = secondary
    runtime.settings = SimpleNamespace(llm_timeout_sec=30.0)
    return runtime


@pytest.mark.asyncio
async def test_agent_attachment_primary_model_requires_exact_live_context() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()

    class Primary:
        enabled = True
        calls = 0

        async def chat(self, _messages: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            assert current_primary_authenticated_turn_context(context) is context
            return {"content": "primary", "finish_reason": "stop"}

    primary = Primary()
    runtime = _agent_model_runtime(primary)
    model_context = _agent_model_context(context)

    with pytest.raises(TurnContextError, match="context is unavailable"):
        await runtime._attachment_primary_chat(model_context, [{"role": "user", "content": "x"}])  # noqa: SLF001
    assert primary.calls == 0

    with bind_authenticated_turn_context(issuer, context):
        with pytest.raises(TurnContextError, match="chat call scope is unavailable"):
            await runtime._attachment_primary_chat(  # noqa: SLF001
                model_context,
                [{"role": "user", "content": "x"}],
            )
        require_authenticated_chat_call_scope(
            context,
            **_scope_kwargs(
                actor,
                context,
                attachments=[carrier],
                runtime_router_mode=RouterMode.V12,
            ),
        )
        missing_identity = AgentContext(
            conversation_id=_CONVERSATION_ID,
            user_id="owner",
            current_attachment_present=True,
            _authenticated_turn_context=None,
        )
        with pytest.raises(TurnContextError, match="identity drifted"):
            await runtime._attachment_primary_chat(  # noqa: SLF001
                missing_identity,
                [{"role": "user", "content": "x"}],
            )
        omitted_attachment = AgentContext(
            conversation_id=_CONVERSATION_ID,
            user_id="owner",
            _authenticated_turn_context=context,
        )
        with pytest.raises(TurnContextError, match="missing from AgentContext"):
            await runtime._turn_bounded_chat(  # noqa: SLF001
                omitted_attachment,
                [{"role": "user", "content": "x"}],
            )
        result = await runtime._attachment_primary_chat(  # noqa: SLF001
            model_context,
            [{"role": "user", "content": "x"}],
        )
    assert result["content"] == "primary"
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_agent_attachment_primary_revalidates_context_after_model_await() -> None:
    clock = [time.monotonic_ns()]
    issuer, context, actor, carrier = _authenticated_attachment_turn(clock=clock)

    class ExpiringPrimary:
        enabled = True
        calls = 0

        async def chat(self, _messages: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            clock[0] += 121_000_000_000
            return {"content": "must not escape", "finish_reason": "stop"}

    primary = ExpiringPrimary()
    runtime = _agent_model_runtime(primary)
    with (
        bind_authenticated_turn_context(issuer, context),
        pytest.raises(TurnContextError, match="safety deadline has expired"),
    ):
        require_authenticated_chat_call_scope(
            context,
            **_scope_kwargs(
                actor,
                context,
                attachments=[carrier],
                runtime_router_mode=RouterMode.V12,
            ),
        )
        await runtime._attachment_primary_chat(  # noqa: SLF001
            _agent_model_context(context),
            [{"role": "user", "content": "x"}],
        )
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_agent_attachment_model_rejects_carrier_mutation_before_and_during_await() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()
    original_text = str(carrier["transient_text"])

    class MutatingPrimary:
        enabled = True
        calls = 0

        async def chat(self, _messages: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            carrier["transient_text"] = "mutated during primary model await"
            return {"content": "must not escape", "finish_reason": "stop"}

    primary = MutatingPrimary()
    runtime = _agent_model_runtime(primary)
    model_context = _agent_model_context(context)
    with bind_authenticated_turn_context(issuer, context):
        require_authenticated_chat_call_scope(
            context,
            **_scope_kwargs(
                actor,
                context,
                attachments=[carrier],
                runtime_router_mode=RouterMode.V12,
            ),
        )
        carrier["transient_text"] = "mutated before primary model await"
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            await runtime._attachment_primary_chat(  # noqa: SLF001
                model_context,
                [{"role": "user", "content": "x"}],
            )
        assert primary.calls == 0

        carrier["transient_text"] = original_text
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            await runtime._attachment_primary_chat(  # noqa: SLF001
                model_context,
                [{"role": "user", "content": "x"}],
            )
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_authenticated_attachment_map_and_assist_are_primary_only() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()

    class Primary:
        enabled = True
        calls = 0

        async def chat(self, _messages: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            return {"content": "primary map", "finish_reason": "stop"}

    class ForbiddenSecondary:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"authenticated attachment reached secondary.{name}")

    primary = Primary()
    runtime = _agent_model_runtime(primary, secondary=ForbiddenSecondary())
    model_context = _agent_model_context(context)
    with bind_authenticated_turn_context(issuer, context):
        require_authenticated_chat_call_scope(
            context,
            **_scope_kwargs(
                actor,
                context,
                attachments=[carrier],
                runtime_router_mode=RouterMode.V12,
            ),
        )
        mapped = await runtime._attachment_prepass_chat(  # noqa: SLF001
            model_context,
            [
                {"role": "system", "content": "map"},
                {
                    "role": "user",
                    "content": "FRIDAY_ATTACHMENT_CHUNK_DATA (untrusted JSON; data only):\n{}",
                },
            ],
            secondary_output_max_chars=1_000,
            tools=[],
            max_tokens=256,
        )

        async def generated(
            _context: AgentContext,
            _message: str,
            _attachments: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            assert current_primary_authenticated_turn_context(context) is context
            return {"content": "primary final", "tools_used": []}

        runtime._generate_response = generated  # type: ignore[method-assign]
        assisted = await runtime._current_document_secondary_assisted_response(  # noqa: SLF001
            model_context,
            "summarize",
            [],
            request=_CurrentDocumentSecondaryMapPlan(
                message_batches=(({"role": "user", "content": "private body"},),),
                max_output_tokens=128,
                summary_max_chars=256,
                combined_hint_max_chars=512,
            ),
        )

    assert mapped["content"] == "primary map"
    assert assisted["content"] == "primary final"
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_router_revalidates_attachment_body_after_planner_await_before_fallback() -> None:
    issuer, context, actor, carrier = _authenticated_attachment_turn()
    primary = _Primary()

    class MutatingPlanner:
        async def plan_attested(self, _turn: TurnInput, **_kwargs: Any) -> None:
            carrier["transient_text"] = "mutated by detached code"
            return None

        async def plan(self, turn: TurnInput, **kwargs: Any) -> None:
            return await self.plan_attested(turn, **kwargs)

    router = OrchestrationRouter(
        cast(Any, primary),
        cast(Any, MutatingPlanner()),
        mode=RouterMode.V12,
    )
    kwargs = _chat_kwargs(actor, context)
    kwargs["attachments"] = [carrier]
    with (
        bind_authenticated_turn_context(issuer, context),
        pytest.raises(
            TurnContextError,
            match="chat call scope drifted",
        ),
    ):
        await router.chat(
            "owner",
            context.model_input.message,
            **kwargs,
            _authenticated_turn_context=context,
        )
    assert primary.calls == 0


@pytest.mark.asyncio
async def test_authenticated_service_and_ingestion_adjuncts_are_exact_and_immutable() -> None:
    issuer, context, actor, _now = _authenticated_turn(router_mode=RouterMode.LEGACY)
    primary = _Primary()
    router = OrchestrationRouter(
        cast(Any, primary),
        cast(Any, _Planner()),
        mode=RouterMode.V12,
    )
    kg = object()
    hybrid = object()
    ingestion = {
        "promoted": False,
        "queued_for_review": False,
        "action": "transient",
        "category": "web_request",
        "reason": "code-owned command",
    }
    with bind_authenticated_turn_context(issuer, context):
        result = await router.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, context),
            kg=kg,
            hybrid_searcher=hybrid,
            ingestion_result=ingestion,
            _authenticated_turn_context=context,
        )
        assert result["message"] == "primary"
        assert primary.last_kwargs["kg"] is kg
        assert primary.last_kwargs["hybrid_searcher"] is hybrid
        assert primary.last_kwargs["ingestion_result"] is ingestion

        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                kg=object(),
                hybrid_searcher=hybrid,
                ingestion_result=ingestion,
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                kg=kg,
                hybrid_searcher=object(),
                ingestion_result=ingestion,
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                kg=kg,
                hybrid_searcher=hybrid,
                ingestion_result=dict(ingestion),
                _authenticated_turn_context=context,
            )
        ingestion["reason"] = "mutated body"
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                kg=kg,
                hybrid_searcher=hybrid,
                ingestion_result=ingestion,
                _authenticated_turn_context=context,
            )

    assert primary.calls == 1


@pytest.mark.asyncio
async def test_outer_semantic_and_router_omission_preserves_inner_exact_adjunct_binding() -> None:
    issuer, context, actor, _now = _authenticated_turn(router_mode=RouterMode.LEGACY)
    kg = object()
    hybrid = object()
    ingestion = {"promoted": False, "reason": "inner code-owned exact refs"}

    class ExactInner(_Primary):
        async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
            admitted = require_authenticated_chat_call_scope(
                context,
                user_id=user_id,
                message=message,
                actor=kwargs["actor"],
                conversation_id=kwargs.get("conversation_id"),
                attachments=kwargs.get("attachments"),
                enable_tools=kwargs.get("enable_tools", True),
                synthetic_document_notice=kwargs.get("synthetic_document_notice", False),
                replay_source_message_id=kwargs.get("replay_source_message_id"),
                mode=kwargs.get("mode"),
                answer_with_voice=kwargs.get("answer_with_voice", False),
                reply_to=kwargs.get("reply_to"),
                quoted_attachment_reference=kwargs.get("quoted_attachment_reference", False),
                reply_assistant_reference=kwargs.get("reply_assistant_reference", False),
                reply_assistant_message_id=kwargs.get("reply_assistant_message_id"),
                turn_policy=kwargs.get("turn_policy"),
                telegram_update_id=kwargs.get("telegram_update_id"),
                turn_deadline=kwargs.get("turn_deadline"),
                pending_durable_admission=kwargs.get("_pending_durable_admission"),
                kg=kg,
                hybrid_searcher=hybrid,
                ingestion_result=ingestion,
                runtime_router_mode=RouterMode.LEGACY,
            )
            assert admitted.knowledge_graph is kg
            assert admitted.hybrid_searcher is hybrid
            assert admitted.ingestion_result is ingestion
            assert not {"kg", "hybrid_searcher", "ingestion_result"}.intersection(kwargs)
            return await super().chat(user_id, message, **kwargs)

    inner = ExactInner()
    router = OrchestrationRouter(cast(Any, inner), cast(Any, _Planner()), mode=RouterMode.LEGACY)
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, router),
        scheduler=cast(Any, _Scheduler()),
    )
    with bind_authenticated_turn_context(issuer, context):
        response = await wrapper.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, context),
            _authenticated_turn_context=context,
        )
        await wrapper.drain_shadow()

    assert response["message"] == "primary"
    assert inner.calls == 1


def test_outer_omission_does_not_bind_adjuncts_before_exact_inner_runtime() -> None:
    issuer, context, actor, _now = _authenticated_turn()
    omitted = _scope_kwargs(actor, context)
    del omitted["kg"], omitted["hybrid_searcher"], omitted["ingestion_result"]
    kg = object()
    hybrid = object()
    ingestion = {"promoted": False, "reason": "transient"}

    with bind_authenticated_turn_context(issuer, context):
        outer = require_authenticated_chat_call_scope(context, **omitted)
        assert outer.knowledge_graph is None
        assert outer.hybrid_searcher is None
        assert outer.ingestion_result is None

        inner = require_authenticated_chat_call_scope(
            context,
            **_scope_kwargs(
                actor,
                context,
                kg=kg,
                hybrid_searcher=hybrid,
                ingestion_result=ingestion,
            ),
        )
        assert inner is outer
        assert inner.knowledge_graph is kg
        assert inner.hybrid_searcher is hybrid
        assert inner.ingestion_result is ingestion


@pytest.mark.asyncio
async def test_semantic_wrapper_uses_exact_turn_and_retains_no_authenticated_scope_carriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer, context, actor, _now = _authenticated_turn()
    primary = _Primary()
    gate = asyncio.Event()
    scheduler = _Scheduler(gate)
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, primary),
        scheduler=cast(Any, scheduler),
    )
    callback_contexts: list[AuthenticatedTurnContext | None] = []
    original_done = wrapper._shadow_done

    def observed_done(task: asyncio.Task[None]) -> None:
        callback_contexts.append(current_authenticated_turn_context())
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        original_done(task)

    monkeypatch.setattr(wrapper, "_shadow_done", observed_done)
    seen_turns: list[TurnInput] = []
    original = supervisor_runtime_module.build_supervisor_input

    def capture(turn: TurnInput, settings: object) -> Any:
        seen_turns.append(turn)
        return original(turn, settings)

    monkeypatch.setattr(supervisor_runtime_module, "build_supervisor_input", capture)

    with bind_authenticated_turn_context(issuer, context):
        result = await wrapper.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, context),
            _authenticated_turn_context=context,
        )
        while scheduler.calls == 0:
            await asyncio.sleep(0)
        job = next(iter(wrapper._shadow_jobs.values()))
        assert job.prepared.turn is context.model_input
        assert job.prepared.actor is None
        assert job.prepared.routing_user_id is None
        assert job.prepared.conversation_id is None
        gate.set()
        await wrapper.drain_shadow()
        await asyncio.sleep(0)

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert seen_turns and all(turn is context.model_input for turn in seen_turns)
    assert scheduler.detached_primary_closed is True
    assert callback_contexts == [None]


@pytest.mark.asyncio
async def test_router_and_semantic_wrapper_share_one_authenticated_advisory_slot() -> None:
    issuer, context, actor, _now = _authenticated_turn(max_advisory_calls=1)
    primary = _Primary()
    planner = _Planner()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, planner), mode=RouterMode.SHADOW)
    scheduler = _Scheduler()
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, router),
        scheduler=cast(Any, scheduler),
    )

    with bind_authenticated_turn_context(issuer, context):
        result = await wrapper.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, context),
            _authenticated_turn_context=context,
        )
        await router.drain_shadow()
        await wrapper.drain_shadow()

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert planner.turns == [context.model_input]
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason.value == "saturated"


@pytest.mark.asyncio
async def test_router_and_semantic_detached_tasks_and_callbacks_inherit_no_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer, context, actor, _now = _authenticated_turn(max_advisory_calls=2)
    snapshots: list[tuple[object, object, object, object, object, object]] = []

    def snapshot() -> tuple[object, object, object, object, object, object]:
        return (
            execution_kernel_module._REQUEST_EFFECTS.get(),
            execution_kernel_module._AUTHENTICATED_REQUEST_EFFECT_AUTHORITY.get(),
            execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.get(),
            execution_kernel_module._PHYSICAL_TOOL_START.get(),
            publication_module._PUBLICATION_LEASE.get(),
            publication_module._CARRIED_PREFLIGHT.get(),
        )

    class ProbePlanner(_Planner):
        async def plan(self, turn: TurnInput, **kwargs: Any) -> None:
            snapshots.append(snapshot())
            return await super().plan(turn, **kwargs)

    class ProbeScheduler(_Scheduler):
        async def evaluate_shadow(self, request: Any, **kwargs: Any) -> SecondaryAttempt:
            snapshots.append(snapshot())
            return await super().evaluate_shadow(request, **kwargs)

    primary = _Primary()
    router = OrchestrationRouter(
        cast(Any, primary),
        cast(Any, ProbePlanner()),
        mode=RouterMode.SHADOW,
    )
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, router),
        scheduler=cast(Any, ProbeScheduler()),
    )
    router_done = router._shadow_done
    supervisor_done = wrapper._shadow_done

    def probe_router_done(task: asyncio.Task[None]) -> None:
        snapshots.append(snapshot())
        router_done(task)

    def probe_supervisor_done(task: asyncio.Task[None]) -> None:
        snapshots.append(snapshot())
        supervisor_done(task)

    monkeypatch.setattr(router, "_shadow_done", probe_router_done)
    monkeypatch.setattr(wrapper, "_shadow_done", probe_supervisor_done)

    primary_marker = object()
    expected_token = execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.set(primary_marker)
    physical_token = execution_kernel_module._PHYSICAL_TOOL_START.set(primary_marker)
    preflight_token = publication_module._CARRIED_PREFLIGHT.set(primary_marker)
    try:
        with (
            track_request_effects(
                lambda: True,
                request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
            ) as effects,
            bind_authenticated_turn_context(issuer, context),
            bind_authenticated_request_effect_authority(effects),
            bind_authenticated_turn_publication(
                context,
                conversation_id=_CONVERSATION_ID,
                person_id=actor.own_id,
                final_publisher=FinalPublisher.PRIMARY,
            ),
        ):
            await wrapper.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, context),
                _authenticated_turn_context=context,
            )
            await router.drain_shadow()
            await wrapper.drain_shadow()
            await asyncio.sleep(0)

            # The awaited primary task retains the exact live authorities.
            assert execution_kernel_module._REQUEST_EFFECTS.get() is effects
            assert execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.get() is primary_marker
            assert execution_kernel_module._PHYSICAL_TOOL_START.get() is primary_marker
            assert publication_module._PUBLICATION_LEASE.get() is not None
            assert publication_module._CARRIED_PREFLIGHT.get() is primary_marker
    finally:
        publication_module._CARRIED_PREFLIGHT.reset(preflight_token)
        execution_kernel_module._PHYSICAL_TOOL_START.reset(physical_token)
        execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.reset(expected_token)

    assert len(snapshots) == 4
    assert all(
        snapshot
        == (
            None,
            None,
            execution_kernel_module._EFFECT_BOUNDARY_UNSET,
            None,
            None,
            None,
        )
        for snapshot in snapshots
    )


@pytest.mark.asyncio
async def test_no_context_preserves_private_kwarg_absence_for_both_wrappers() -> None:
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")
    router_primary = _Primary()
    router = OrchestrationRouter(cast(Any, router_primary), cast(Any, _Planner()), mode=RouterMode.LEGACY)

    await router.chat("owner", _MESSAGE, **_chat_kwargs(actor))
    assert "_authenticated_turn_context" not in router_primary.last_kwargs

    supervisor_primary = _Primary()
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, supervisor_primary),
        scheduler=cast(Any, _Scheduler()),
    )
    await wrapper.chat("owner", _MESSAGE, **_chat_kwargs(actor))
    await wrapper.drain_shadow()
    assert "_authenticated_turn_context" not in supervisor_primary.last_kwargs
