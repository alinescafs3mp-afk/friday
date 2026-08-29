from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from friday import execution_kernel as execution_kernel_module
from friday.execution_kernel import (
    bind_authenticated_request_effect_authority,
    track_request_effects,
)
from friday.orchestration import turn_context_publication as publication_module
from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.effect_outcome import (
    EffectAction,
    EffectCapability,
    EffectCompensationState,
    EffectObservationState,
    EffectObservationsV1,
    EffectOutcomeV1,
    EffectPublishability,
    EffectReconciliationState,
    EffectStatus,
    attach_accepted_effect_outcome_receipt,
)
from friday.orchestration.supervisor_assist_controller import (
    AssistPendingGraphDisposition,
    SupervisorAssistOutcome,
    SupervisorAssistResult,
)
from friday.orchestration.supervisor_assist_ingress import (
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.orchestration.supervisor_assist_runtime import (
    SemanticSupervisorAssistRuntime,
    SupervisorAssistRuntimeError,
)
from friday.orchestration.supervisor_effect_intent import (
    SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
    EffectIntentActionSelection,
    EffectIntentCapabilitySelection,
    EffectIntentSelectionV2,
)
from friday.orchestration.supervisor_effect_intent_runtime import (
    SupervisorEffectIntentShadowRuntime,
)
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
from friday.orchestration.turn_context_call_scope import require_authenticated_chat_call_scope
from friday.orchestration.turn_context_publication import bind_authenticated_turn_publication
from friday.orchestration.turn_context_runtime import (
    bind_authenticated_turn_context,
    current_authenticated_turn_context,
    current_primary_authenticated_turn_context,
    reserve_authenticated_advisory_call,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.secondary_brain import ModelWorkload
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_CONVERSATION = "conv_0123456789abcdef"
_MESSAGE = "Создай заметку о встрече"


def _turn(
    label: str,
    *,
    pending: PendingDurableTurnAdmission | None = None,
    max_advisory_calls: int = 1,
    monotonic_clock: list[int] | None = None,
    shared_tenant: bool = False,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext, ActorContext, float]:
    actor = ActorContext(
        user_id="shared-tenant" if shared_tenant else "owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
        session_id=f"session-{label}",
        shared_tenant=shared_tenant,
        person_id="person-alice" if shared_tenant else "",
    )
    issuer = TurnContextIssuer(
        hashlib.sha256(label.encode("ascii")).digest(),
        _monotonic_ns=((lambda: monotonic_clock[0]) if monotonic_clock is not None else time.monotonic_ns),
    )
    deadline = time.monotonic() + 2.0
    deadline_ns = int(deadline * 1_000_000_000)
    deadline = deadline_ns / 1_000_000_000
    deadline_ns = int(deadline * 1_000_000_000)
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-{label}",
        actor=actor,
        conversation_id=_CONVERSATION,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=actor.source,
        update_id=f"update-{label}",
        request_effect_binding_sha256=hashlib.sha256(f"effects-{label}".encode()).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message=_MESSAGE,
        actor=actor,
        conversation_id=_CONVERSATION,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.LEGACY,
        fallback_router_mode=None,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    pending_work = (
        issuer.bind_pending_work(authority=authority, admission=pending) if pending is not None else None
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(deadline_ns),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, max_advisory_calls, 8_192),
        ),
        pending_work_admission=pending_work,
    )
    return issuer, context, actor, deadline


def _chat_kwargs(actor: ActorContext, deadline: float) -> dict[str, Any]:
    return {
        "actor": actor,
        "conversation_id": _CONVERSATION,
        "attachments": None,
        "enable_tools": True,
        "turn_deadline": deadline,
    }


class _Primary:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0
        self.user_ids: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.expected_context: AuthenticatedTurnContext | None = None

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        del message
        self.calls += 1
        self.user_ids.append(user_id)
        self.kwargs = kwargs
        if self.expected_context is not None:
            assert current_primary_authenticated_turn_context(self.expected_context) is self.expected_context
        return self.response


class _EffectScheduler:
    def workload_mode(self, workload: ModelWorkload) -> str:
        assert workload is ModelWorkload.EFFECT_PLANNING
        return "shadow"


class _EffectStorage:
    def __init__(self, *, user_id: str = "owner", effect_id_sha256: str = "7" * 64) -> None:
        metadata: dict[str, Any] = {}
        attach_accepted_effect_outcome_receipt(
            metadata,
            EffectOutcomeV1(
                effect_id_sha256=effect_id_sha256,
                work_item_sha256="8" * 64,
                capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
                action=EffectAction.CREATE,
                request_sha256="9" * 64,
                authorization_basis_sha256="a" * 64,
                idempotency_key_sha256="b" * 64,
                status=EffectStatus.SUCCEEDED,
                reconciliation=EffectReconciliationState.NOT_REQUIRED,
                compensation=EffectCompensationState.NOT_REQUIRED,
                side_effect_receipt_sha256="c" * 64,
                compensation_receipt_sha256=None,
                evidence_sha256="d" * 64,
                observations=EffectObservationsV1(
                    server_sync=EffectObservationState.PENDING,
                    reingest=EffectObservationState.PENDING,
                    physical_device=EffectObservationState.PENDING,
                ),
                publishability=EffectPublishability.ACCEPTED_FACTS,
                authority_rechecked=True,
            ),
        )
        self.row = {
            "id": "message-1",
            "user_id": user_id,
            "conversation_id": _CONVERSATION,
            "role": "assistant",
            "metadata_json": json.dumps(metadata),
        }
        self.events: list[dict[str, Any]] = []
        self.lookups: list[tuple[str, str]] = []

    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
        self.lookups.append((message_id, user_id))
        expected = (str(self.row["id"]), str(self.row["user_id"]))
        return dict(self.row) if (message_id, user_id) == expected else None

    def record_event(self, _event_type: str, payload: dict[str, Any] | None = None) -> str:
        assert payload is not None
        self.events.append(payload)
        return "event-1"


@pytest.mark.asyncio
async def test_effect_shadow_uses_shared_slot_context_deadline_and_detached_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as module

    issuer, context, actor, deadline = _turn("outer-effect")
    primary = _Primary(
        {
            "conversation_id": _CONVERSATION,
            "message_id": "message-1",
            "message": "primary",
        }
    )
    primary.expected_context = context
    witness = SimpleNamespace(
        artifact_file_sha256="1" * 64,
        maturity_facts_sha256="2" * 64,
        source_revision_sha256="3" * 64,
        registry_binding_sha256="4" * 64,
        effect_registry_binding_sha256="5" * 64,
    )
    monkeypatch.setattr(
        module, "accepted_read_only_maturity_witness_is_current", lambda value: value is witness
    )
    selected_deadlines: list[float] = []

    async def select(_scheduler: object, **kwargs: Any) -> EffectIntentSelectionV2:
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        assert execution_kernel_module._REQUEST_EFFECTS.get() is None
        assert execution_kernel_module._AUTHENTICATED_REQUEST_EFFECT_AUTHORITY.get() is None
        assert (
            execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.get()
            is execution_kernel_module._EFFECT_BOUNDARY_UNSET
        )
        assert execution_kernel_module._PHYSICAL_TOOL_START.get() is None
        assert publication_module._PUBLICATION_LEASE.get() is None
        assert publication_module._CARRIED_PREFLIGHT.get() is None
        selected_deadlines.append(kwargs["absolute_deadline_monotonic"])
        projection = kwargs["projection"]
        return EffectIntentSelectionV2(
            capability=EffectIntentCapabilitySelection.OBSIDIAN_NOTE_MUTATION,
            action=EffectIntentActionSelection.CREATE,
            manifest_digest=SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
            projection_digest=projection.projection_digest,
        )

    monkeypatch.setattr(module, "select_supervisor_effect_intent", select)
    storage = _EffectStorage()
    wrapper = SupervisorEffectIntentShadowRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_effect_mode="shadow",
            semantic_supervisor_effect_evidence_sha256="1" * 64,
        ),
        primary=primary,
        scheduler=_EffectScheduler(),  # type: ignore[arg-type]
        storage=storage,
        maturity_witness=witness,  # type: ignore[arg-type]
    )
    kg = object()
    hybrid = object()
    ingestion = {"promoted": False, "category": "web_request"}

    marker = object()
    expected_token = execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.set(marker)
    physical_token = execution_kernel_module._PHYSICAL_TOOL_START.set(marker)
    preflight_token = publication_module._CARRIED_PREFLIGHT.set(marker)
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
                conversation_id=_CONVERSATION,
                person_id=actor.own_id,
                final_publisher=FinalPublisher.PRIMARY,
            ),
        ):
            with pytest.raises(TurnContextError, match="message drifted"):
                await wrapper.chat(
                    "owner",
                    "подменённый запрос",
                    **_chat_kwargs(actor, deadline),
                    _authenticated_turn_context=context,
                )
            assert primary.calls == 0
            response = await wrapper.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, deadline),
                kg=kg,
                hybrid_searcher=hybrid,
                ingestion_result=ingestion,
                _authenticated_turn_context=context,
            )
            for _ in range(20):
                if selected_deadlines:
                    break
                await asyncio.sleep(0)
            assert selected_deadlines
            with pytest.raises(TurnContextError, match="exhausted"):
                reserve_authenticated_advisory_call(context)
            assert execution_kernel_module._REQUEST_EFFECTS.get() is effects
            assert execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.get() is marker
            assert execution_kernel_module._PHYSICAL_TOOL_START.get() is marker
            assert publication_module._PUBLICATION_LEASE.get() is not None
            assert publication_module._CARRIED_PREFLIGHT.get() is marker
    finally:
        publication_module._CARRIED_PREFLIGHT.reset(preflight_token)
        execution_kernel_module._PHYSICAL_TOOL_START.reset(physical_token)
        execution_kernel_module._EXPECTED_EFFECT_BOUNDARY.reset(expected_token)

    assert response is primary.response
    assert primary.kwargs["_authenticated_turn_context"] is context
    assert primary.kwargs["kg"] is kg
    assert primary.kwargs["hybrid_searcher"] is hybrid
    assert primary.kwargs["ingestion_result"] is ingestion
    assert selected_deadlines[0] <= context.inherited_budget.safety_deadline.monotonic_ns / 1e9
    await wrapper.close()


@pytest.mark.asyncio
async def test_effect_shadow_reads_person_owned_receipt_in_shared_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as module

    issuer, context, actor, deadline = _turn(
        "outer-effect-shared-tenant",
        shared_tenant=True,
    )
    primary = _Primary(
        {
            "conversation_id": _CONVERSATION,
            "message_id": "message-1",
            "message": "primary",
        }
    )
    witness = SimpleNamespace(
        artifact_file_sha256="1" * 64,
        maturity_facts_sha256="2" * 64,
        source_revision_sha256="3" * 64,
        registry_binding_sha256="4" * 64,
        effect_registry_binding_sha256="5" * 64,
    )
    monkeypatch.setattr(
        module,
        "accepted_read_only_maturity_witness_is_current",
        lambda value: value is witness,
    )

    async def select(_scheduler: object, **kwargs: Any) -> EffectIntentSelectionV2:
        projection = kwargs["projection"]
        return EffectIntentSelectionV2(
            capability=EffectIntentCapabilitySelection.OBSIDIAN_NOTE_MUTATION,
            action=EffectIntentActionSelection.CREATE,
            manifest_digest=SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
            projection_digest=projection.projection_digest,
        )

    monkeypatch.setattr(module, "select_supervisor_effect_intent", select)
    storage = _EffectStorage(user_id=actor.own_id, effect_id_sha256="6" * 64)
    wrapper = SupervisorEffectIntentShadowRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_effect_mode="shadow",
            semantic_supervisor_effect_evidence_sha256="1" * 64,
        ),
        primary=primary,
        scheduler=_EffectScheduler(),  # type: ignore[arg-type]
        storage=storage,
        maturity_witness=witness,  # type: ignore[arg-type]
    )

    with bind_authenticated_turn_context(issuer, context):
        response = await wrapper.chat(
            context.authority.tenant_id,
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            _authenticated_turn_context=context,
        )
    for _ in range(20):
        if not wrapper.semantic_supervisor_effect_status()["pending"]:
            break
        await asyncio.sleep(0)

    assert response is primary.response
    assert primary.calls == 1
    assert primary.user_ids == [context.authority.tenant_id]
    assert storage.lookups == [("message-1", actor.own_id)]
    assert len(storage.events) == 1
    await wrapper.close()


@pytest.mark.asyncio
async def test_effect_shadow_deadline_flip_after_reservation_cannot_replace_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as module

    clock = [time.monotonic_ns()]
    issuer, context, actor, deadline = _turn("outer-effect-deadline-flip", monotonic_clock=clock)
    primary = _Primary(
        {
            "conversation_id": _CONVERSATION,
            "message_id": "message-1",
            "message": "primary",
        }
    )
    primary.expected_context = context
    witness = SimpleNamespace(
        artifact_file_sha256="1" * 64,
        maturity_facts_sha256="2" * 64,
        source_revision_sha256="3" * 64,
        registry_binding_sha256="4" * 64,
        effect_registry_binding_sha256="5" * 64,
    )
    monkeypatch.setattr(
        module, "accepted_read_only_maturity_witness_is_current", lambda value: value is witness
    )
    selected = False

    async def forbidden_select(_scheduler: object, **_kwargs: Any) -> EffectIntentSelectionV2:
        nonlocal selected
        selected = True
        raise AssertionError("expired optional observer must not start")

    monkeypatch.setattr(module, "select_supervisor_effect_intent", forbidden_select)
    original_reserve = module.reserve_authenticated_advisory_call

    def reserve_then_expire(expected: AuthenticatedTurnContext | None = None) -> int:
        reserved = original_reserve(expected)
        clock[0] = context.inherited_budget.safety_deadline.monotonic_ns
        return reserved

    monkeypatch.setattr(module, "reserve_authenticated_advisory_call", reserve_then_expire)
    wrapper = SupervisorEffectIntentShadowRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_effect_mode="shadow",
            semantic_supervisor_effect_evidence_sha256="1" * 64,
        ),
        primary=primary,
        scheduler=_EffectScheduler(),  # type: ignore[arg-type]
        storage=_EffectStorage(),
        maturity_witness=witness,  # type: ignore[arg-type]
    )

    with bind_authenticated_turn_context(issuer, context):
        response = await wrapper.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            _authenticated_turn_context=context,
        )

    await asyncio.sleep(0)
    assert response is primary.response
    assert primary.calls == 1
    assert primary.kwargs["_authenticated_turn_context"] is context
    assert selected is False
    assert not wrapper._tasks  # noqa: SLF001
    assert wrapper._skip_counts["capacity"] == 1  # noqa: SLF001
    await wrapper.close()


class _AssistController:
    def __init__(self) -> None:
        self.classify_calls = 0
        self.execute_calls = 0
        self.reconcile_calls = 0

    def semantic_supervisor_status(self) -> dict[str, object]:
        return {}

    def start_restart_recovery(self, *, batch_limit: int = 100) -> None:
        del batch_limit

    async def wait_restart_recovery(self) -> None:
        return None

    def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def classify_supervisor_assist_pending(self, *_args: Any, **_kwargs: Any) -> bool:
        self.classify_calls += 1
        return False

    async def execute(self, _surface: object, *, legacy_primary: Any, absolute_deadline: float) -> Any:
        del absolute_deadline
        self.execute_calls += 1
        return SupervisorAssistResult(
            outcome=SupervisorAssistOutcome.LEGACY,
            response=await legacy_primary(),
        )

    async def cancel_active(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def reconcile_pending_before_legacy(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> AssistPendingGraphDisposition:
        self.reconcile_calls += 1
        return AssistPendingGraphDisposition.LIVE_IN_PROCESS

    async def close(self) -> None:
        return None


def _assist_runtime(primary: _Primary, controller: _AssistController) -> SemanticSupervisorAssistRuntime:
    return SemanticSupervisorAssistRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_mode="assist",
            semantic_supervisor_timeout_sec=12.0,
        ),
        primary=primary,
        controller=controller,  # type: ignore[arg-type]
        conversation_is_dialogue=lambda *_args: True,
    )


@pytest.mark.asyncio
async def test_authenticated_ordinary_assist_turn_cannot_mint_pending_work() -> None:
    issuer, context, actor, deadline = _turn("outer-assist-ordinary")
    primary = _Primary({"conversation_id": _CONVERSATION, "message": "primary"})
    primary.expected_context = context
    controller = _AssistController()
    runtime = _assist_runtime(primary, controller)
    kg = object()
    hybrid = object()
    ingestion = {"promoted": False, "category": "web_request"}

    with bind_authenticated_turn_context(issuer, context):
        response = await runtime.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            kg=kg,
            hybrid_searcher=hybrid,
            ingestion_result=ingestion,
            _authenticated_turn_context=context,
        )

    assert response is primary.response
    assert primary.kwargs["_authenticated_turn_context"] is context
    assert primary.kwargs["kg"] is kg
    assert primary.kwargs["hybrid_searcher"] is hybrid
    assert primary.kwargs["ingestion_result"] is ingestion
    assert controller.classify_calls == controller.execute_calls == 0


@pytest.mark.asyncio
async def test_outer_effect_and_assist_omission_preserves_inner_exact_adjunct_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_effect_intent_runtime as module

    issuer, context, actor, deadline = _turn("outer-effect-assist-omission")
    kg = object()
    hybrid = object()
    ingestion = {"promoted": False, "reason": "inner code-owned exact refs"}

    class ExactPrimary(_Primary):
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
            )
            assert admitted.knowledge_graph is kg
            assert admitted.hybrid_searcher is hybrid
            assert admitted.ingestion_result is ingestion
            assert not {"kg", "hybrid_searcher", "ingestion_result"}.intersection(kwargs)
            return await super().chat(user_id, message, **kwargs)

    primary = ExactPrimary(
        {
            "conversation_id": _CONVERSATION,
            "message_id": "message-1",
            "message": "primary",
        }
    )
    assist = _assist_runtime(primary, _AssistController())
    witness = SimpleNamespace(
        artifact_file_sha256="1" * 64,
        maturity_facts_sha256="2" * 64,
        source_revision_sha256="3" * 64,
        registry_binding_sha256="4" * 64,
        effect_registry_binding_sha256="5" * 64,
    )
    monkeypatch.setattr(
        module, "accepted_read_only_maturity_witness_is_current", lambda value: value is witness
    )
    effect = SupervisorEffectIntentShadowRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_effect_mode="shadow",
            semantic_supervisor_effect_evidence_sha256="1" * 64,
        ),
        primary=assist,
        scheduler=_EffectScheduler(),  # type: ignore[arg-type]
        storage=_EffectStorage(),
        maturity_witness=witness,  # type: ignore[arg-type]
    )

    with bind_authenticated_turn_context(issuer, context):
        response = await effect.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            _authenticated_turn_context=context,
        )

    assert response is primary.response
    assert primary.calls == 1
    await effect.close()


@pytest.mark.asyncio
async def test_authenticated_assist_new_turn_never_reconciles_under_successor_authority() -> None:
    admission = PendingDurableTurnAdmission.owned(
        person_id="owner",
        conversation_id=_CONVERSATION,
        work_graph_id="graph_0123456789abcdef",
        revision=3,
    )
    issuer, context, actor, deadline = _turn("outer-assist-await-mutation", pending=admission)
    ingestion = {"promoted": False, "reason": "exact successor input"}
    primary = _Primary({"conversation_id": _CONVERSATION, "message": "primary"})
    primary.expected_context = context
    controller = _AssistController()
    runtime = _assist_runtime(primary, controller)
    ingress = SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref="outer-assist-await-mutation:current",
        request_fingerprint_sha256="e" * 64,
    )
    decision = SupervisorAssistPendingDecision.for_graph(
        relation=SupervisorAssistPendingRelation.NEW_TURN,
        pending=admission,
        root_request_binding_sha256="f" * 64,
        current=ingress,
    )

    with bind_authenticated_turn_context(issuer, context):
        response = await runtime.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            ingestion_result=ingestion,
            _pending_durable_admission=admission,
            _semantic_supervisor_ingress_binding=ingress,
            _semantic_supervisor_pending_decision=decision,
            _authenticated_turn_context=context,
        )

    assert response is primary.response
    assert primary.calls == 1
    assert primary.kwargs["_pending_durable_admission"] is admission
    assert primary.kwargs["ingestion_result"] is ingestion
    assert controller.reconcile_calls == controller.execute_calls == 0


@pytest.mark.asyncio
async def test_authenticated_assist_rejects_raw_and_pending_identity_drift_before_work() -> None:
    admission = PendingDurableTurnAdmission.owned(
        person_id="owner",
        conversation_id=_CONVERSATION,
        work_graph_id="graph_0123456789abcdef",
        revision=3,
    )
    issuer, context, actor, deadline = _turn("outer-assist-pending", pending=admission)
    primary = _Primary({"message": "must not run"})
    controller = _AssistController()
    runtime = _assist_runtime(primary, controller)

    with bind_authenticated_turn_context(issuer, context):
        with pytest.raises(TurnContextError, match="message drifted"):
            await runtime.chat(
                "owner",
                "другой запрос",
                **_chat_kwargs(actor, deadline),
                _pending_durable_admission=admission,
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="pending-work carrier drifted"):
            await runtime.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor, deadline),
                _pending_durable_admission=dataclasses.replace(admission),
                _authenticated_turn_context=context,
            )

    assert primary.calls == controller.classify_calls == controller.execute_calls == 0


@pytest.mark.asyncio
async def test_authenticated_assist_graph_requires_exact_pre_admitted_decision() -> None:
    admission = PendingDurableTurnAdmission.owned(
        person_id="owner",
        conversation_id=_CONVERSATION,
        work_graph_id="graph_0123456789abcdef",
        revision=3,
    )
    issuer, context, actor, deadline = _turn("outer-assist-graph", pending=admission)
    primary = _Primary({"message": "must not run"})
    controller = _AssistController()
    runtime = _assist_runtime(primary, controller)
    ingress = SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref="outer-assist:current",
        request_fingerprint_sha256="e" * 64,
    )
    different = dataclasses.replace(admission)
    decision = SupervisorAssistPendingDecision.for_graph(
        relation=SupervisorAssistPendingRelation.NEW_TURN,
        pending=different,
        root_request_binding_sha256="f" * 64,
        current=ingress,
    )

    with (
        bind_authenticated_turn_context(issuer, context),
        pytest.raises(SupervisorAssistRuntimeError, match="binding drifted"),
    ):
        await runtime.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            _pending_durable_admission=admission,
            _semantic_supervisor_ingress_binding=ingress,
            _semantic_supervisor_pending_decision=decision,
            _authenticated_turn_context=context,
        )

    assert primary.calls == controller.execute_calls == 0
