from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

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
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
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
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext, ActorContext, float]:
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
        session_id=f"session-{label}",
    )
    issuer = TurnContextIssuer(hashlib.sha256(label.encode("ascii")).digest())
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
        self.kwargs: dict[str, Any] = {}
        self.expected_context: AuthenticatedTurnContext | None = None

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, message
        self.calls += 1
        self.kwargs = kwargs
        if self.expected_context is not None:
            assert current_primary_authenticated_turn_context(self.expected_context) is self.expected_context
        return self.response


class _EffectScheduler:
    def workload_mode(self, workload: ModelWorkload) -> str:
        assert workload is ModelWorkload.EFFECT_PLANNING
        return "shadow"


class _EffectStorage:
    def __init__(self) -> None:
        metadata: dict[str, Any] = {}
        attach_accepted_effect_outcome_receipt(
            metadata,
            EffectOutcomeV1(
                effect_id_sha256="7" * 64,
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
            "user_id": "owner",
            "conversation_id": _CONVERSATION,
            "role": "assistant",
            "metadata_json": json.dumps(metadata),
        }
        self.events: list[dict[str, Any]] = []

    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
        return dict(self.row) if (message_id, user_id) == ("message-1", "owner") else None

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

    with bind_authenticated_turn_context(issuer, context):
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
            _authenticated_turn_context=context,
        )
        for _ in range(20):
            if selected_deadlines:
                break
            await asyncio.sleep(0)
        assert selected_deadlines
        with pytest.raises(TurnContextError, match="exhausted"):
            reserve_authenticated_advisory_call(context)

    assert response is primary.response
    assert primary.kwargs["_authenticated_turn_context"] is context
    assert selected_deadlines[0] <= context.inherited_budget.safety_deadline.monotonic_ns / 1e9
    await wrapper.close()


class _AssistController:
    def __init__(self) -> None:
        self.classify_calls = 0
        self.execute_calls = 0

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

    with bind_authenticated_turn_context(issuer, context):
        response = await runtime.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor, deadline),
            _authenticated_turn_context=context,
        )

    assert response is primary.response
    assert primary.kwargs["_authenticated_turn_context"] is context
    assert controller.classify_calls == controller.execute_calls == 0


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
