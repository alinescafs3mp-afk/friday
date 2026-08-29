"""Code-owned construction of one admitted scalar authenticated turn.

The HTTP boundary decides whether a request belongs to this deliberately
narrow first runtime slice.  This module only turns already-authenticated,
already-claimed inputs into the single immutable context used downstream.
"""

from __future__ import annotations

from types import MappingProxyType

from friday.orchestration.contracts import RouterMode, TurnInput
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
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnPolicyDecision

_TOOL_BUDGETS = MappingProxyType(
    {
        TurnMode.DIALOGUE: (4, 2),
        TurnMode.KNOWLEDGE_WORK: (8, 3),
        TurnMode.RESEARCH: (12, 5),
        TurnMode.ENGINEER: (48, 24),
    }
)

# The live composition can schedule three independent, effect-free observers:
# the V12 plan shadow, the semantic plan candidate and the effect-intent shadow.
# Keeping one bounded slot for each preserves the pre-S2 shadow evidence path;
# every observer still has to reserve its own slot from the shared runtime ledger.
_MAX_ADVISORY_CALLS = 3


def issue_authenticated_scalar_turn_context(
    issuer: TurnContextIssuer,
    *,
    ingress_kind: IngressKind,
    ingress_issued_token: str,
    actor: ActorContext,
    conversation_id: str,
    interaction_mode: TurnMode,
    source_id: str,
    update_id: str,
    request_effect_binding_sha256: str,
    message: str,
    enable_tools: bool,
    decision: TurnPolicyDecision,
    router_mode: RouterMode,
    deadline_monotonic_ns: int,
    max_output_tokens: int,
    pending_admission: PendingDurableTurnAdmission | None = None,
) -> AuthenticatedTurnContext:
    """Issue the exact context for an already-admitted, attachment-free turn."""

    if type(issuer) is not TurnContextIssuer:
        raise TypeError("scalar turn context requires the exact issuer")
    if type(enable_tools) is not bool:
        raise TypeError("scalar turn tool authority must be boolean")
    if type(decision) is not TurnPolicyDecision:
        raise TypeError("scalar turn policy decision has an invalid type")
    if type(router_mode) is not RouterMode:
        raise TypeError("scalar turn router mode has an invalid type")

    authority = issuer.issue_ingress_authority(
        ingress_kind=ingress_kind,
        ingress_issued_token=ingress_issued_token,
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=interaction_mode,
        source_id=source_id,
        update_id=update_id,
        request_effect_binding_sha256=request_effect_binding_sha256,
    )
    model_input = TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=conversation_id,
        attachments=(),
        enable_tools=enable_tools,
        synthetic_document_notice=False,
        mode=interaction_mode.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    if model_input.message_truncated:
        raise TurnContextError("scalar authenticated turn message exceeds its exact model projection")
    policy = issuer.issue_turn_policy(
        router_mode=router_mode,
        fallback_router_mode=None if router_mode is RouterMode.LEGACY else RouterMode.LEGACY,
        decision=decision,
    )
    calls, rounds = _TOOL_BUDGETS[interaction_mode] if enable_tools else (0, 0)
    budget = InheritedTurnBudget(
        safety_deadline=TurnSafetyDeadline(deadline_monotonic_ns),
        # This first propagation slice records a non-renewable ceiling.  Exact
        # shared consumption is activated only when every primary model seam
        # participates; until then the existing stricter runtime loops remain
        # authoritative and this ceiling must not shorten them.
        model_anti_loop=ModelAntiLoopBudget(max_model_calls=64, max_model_retries=16),
        resources=TurnResourceBudget(
            max_tool_calls=calls,
            max_tool_rounds=rounds,
            max_advisory_calls=_MAX_ADVISORY_CALLS,
            max_output_tokens=max_output_tokens,
        ),
    )
    pending = (
        issuer.bind_pending_work(authority=authority, admission=pending_admission)
        if pending_admission is not None
        else None
    )
    return issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=budget,
        pending_work_admission=pending,
    )


__all__ = ["issue_authenticated_scalar_turn_context"]
