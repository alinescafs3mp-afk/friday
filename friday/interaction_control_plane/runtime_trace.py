"""Runtime helpers for durable, privacy-safe interaction traces."""

from __future__ import annotations

from typing import Any

from friday.audit_privacy import decode_audit_privacy_key
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CapabilityStepTrace,
    CompletionDecision,
    ContinuationKind,
    CountAccounting,
    FailureReason,
    FailureStage,
    IntentClass,
    OutcomeStatus,
    PlaybookClass,
    PublicationStatus,
    TokenAccounting,
    TraceBudget,
    TraceIdentifierDomain,
    TurnTrace,
    WorkRelation,
    derive_trace_identifier,
)

INTERACTION_TRACE_METADATA_KEY = "interaction_trace"


def load_trace_namespace_key(executor: Any) -> bytes:
    """Load the deployment-local privacy key through a storage/transaction executor."""

    try:
        row = executor.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
        return decode_audit_privacy_key(row[0] if row is not None else None)
    except Exception as exc:  # noqa: BLE001 - missing privacy authority must fail closed
        raise RuntimeError("interaction trace namespace key is unavailable") from exc


def build_direct_trace(
    *,
    namespace_key: bytes,
    turn_identifier: str,
    conversation_identifier: str,
    intent: IntentClass,
    playbook: PlaybookClass,
    capability_outcomes: tuple[tuple[CapabilityClass, OutcomeStatus], ...],
    continuation: ContinuationKind,
    completion: CompletionDecision,
    failure_stage: FailureStage,
    failure_reason: FailureReason,
    ambiguity_present: bool,
    partial_coverage: bool,
    state_restored: bool,
    latency_ms: int,
    model_calls: int,
    model_call_accounting: CountAccounting = CountAccounting.UNAVAILABLE,
    capability_calls: int,
    capability_call_accounting: CountAccounting = CountAccounting.UNAVAILABLE,
    input_tokens: int = 0,
    output_tokens: int = 0,
    token_accounting: TokenAccounting = TokenAccounting.UNAVAILABLE,
    authority_rechecked: bool,
) -> TurnTrace:
    """Build one published legacy/direct trace without inventing a Work Item."""

    turn_digest = derive_trace_identifier(
        domain=TraceIdentifierDomain.TURN,
        raw_identifier=turn_identifier,
        namespace_key=namespace_key,
    )
    conversation_digest = derive_trace_identifier(
        domain=TraceIdentifierDomain.CONVERSATION,
        raw_identifier=conversation_identifier,
        namespace_key=namespace_key,
    )
    steps = tuple(
        CapabilityStepTrace(
            step_digest=derive_trace_identifier(
                domain=TraceIdentifierDomain.STEP,
                raw_identifier=f"{turn_digest}:{ordinal}:{capability.value}",
                namespace_key=namespace_key,
            ),
            capability=capability,
            outcome=outcome,
            attempts=0 if outcome is OutcomeStatus.NOT_STARTED else 1,
            required=True,
        )
        for ordinal, (capability, outcome) in enumerate(capability_outcomes, start=1)
    )
    return TurnTrace(
        turn_digest=turn_digest,
        conversation_digest=conversation_digest,
        work_item_digest=None,
        work_relation=WorkRelation.DIRECT,
        intent=intent,
        continuation=continuation,
        playbook=playbook,
        steps=steps,
        completion=completion,
        publication=PublicationStatus.PUBLISHED,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        ambiguity_present=ambiguity_present,
        partial_coverage=partial_coverage,
        state_restored=state_restored,
        authority_rechecked=authority_rechecked,
        budget=TraceBudget(
            latency_ms=latency_ms,
            model_calls=model_calls,
            model_call_accounting=model_call_accounting,
            capability_calls=capability_calls,
            capability_call_accounting=capability_call_accounting,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_accounting=token_accounting,
        ),
    )


def build_published_direct_trace(
    *,
    namespace_key: bytes,
    turn_identifier: str,
    conversation_identifier: str,
    intent: IntentClass,
    playbook: PlaybookClass,
    capabilities: tuple[CapabilityClass, ...],
    latency_ms: int,
    model_calls: int,
    model_call_accounting: CountAccounting = CountAccounting.UNAVAILABLE,
    capability_calls: int,
    capability_call_accounting: CountAccounting = CountAccounting.UNAVAILABLE,
    input_tokens: int = 0,
    output_tokens: int = 0,
    token_accounting: TokenAccounting = TokenAccounting.UNAVAILABLE,
    authority_rechecked: bool,
) -> TurnTrace:
    """Build the closed success projection for work completed in one turn."""

    return build_direct_trace(
        namespace_key=namespace_key,
        turn_identifier=turn_identifier,
        conversation_identifier=conversation_identifier,
        intent=intent,
        playbook=playbook,
        capability_outcomes=tuple((capability, OutcomeStatus.SUCCEEDED) for capability in capabilities),
        continuation=ContinuationKind.NONE,
        completion=CompletionDecision.COMPLETE,
        failure_stage=FailureStage.NONE,
        failure_reason=FailureReason.NONE,
        ambiguity_present=False,
        partial_coverage=False,
        state_restored=False,
        latency_ms=latency_ms,
        model_calls=model_calls,
        model_call_accounting=model_call_accounting,
        capability_calls=capability_calls,
        capability_call_accounting=capability_call_accounting,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_accounting=token_accounting,
        authority_rechecked=authority_rechecked,
    )


__all__ = [
    "INTERACTION_TRACE_METADATA_KEY",
    "build_direct_trace",
    "build_published_direct_trace",
    "load_trace_namespace_key",
]
