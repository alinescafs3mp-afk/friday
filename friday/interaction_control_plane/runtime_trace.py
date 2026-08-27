"""Runtime helpers for durable, privacy-safe interaction traces."""

from __future__ import annotations

import json
from dataclasses import replace
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
_ASSISTANT_METADATA_MAX_BYTES = 65_536


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
    capability_attempts: tuple[int, ...] | None = None,
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
    publication: PublicationStatus = PublicationStatus.ASSISTANT_COMMITTED,
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
    if capability_attempts is None:
        attempts = tuple(
            0 if outcome is OutcomeStatus.NOT_STARTED else 1 for _, outcome in capability_outcomes
        )
    else:
        if type(capability_attempts) is not tuple or len(capability_attempts) != len(capability_outcomes):
            raise ValueError("capability attempts must align exactly with capability outcomes")
        attempts = capability_attempts
        for (_, outcome), attempt in zip(capability_outcomes, attempts, strict=True):
            if (outcome is OutcomeStatus.NOT_STARTED) != (attempt == 0):
                raise ValueError("capability attempts contradict the recorded outcome")
    steps = tuple(
        CapabilityStepTrace(
            step_digest=derive_trace_identifier(
                domain=TraceIdentifierDomain.STEP,
                raw_identifier=f"{turn_digest}:{ordinal}:{capability.value}",
                namespace_key=namespace_key,
            ),
            capability=capability,
            outcome=outcome,
            attempts=attempt,
            required=True,
        )
        for ordinal, ((capability, outcome), attempt) in enumerate(
            zip(capability_outcomes, attempts, strict=True),
            start=1,
        )
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
        publication=publication,
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


def build_committed_direct_trace(
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
    """Build the closed success projection stored with a committed assistant row."""

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


def build_work_trace(
    *,
    namespace_key: bytes,
    turn_identifier: str,
    conversation_identifier: str,
    work_item_identifier: str,
    work_relation: WorkRelation,
    intent: IntentClass,
    playbook: PlaybookClass,
    capability_outcomes: tuple[tuple[CapabilityClass, OutcomeStatus], ...],
    capability_attempts: tuple[int, ...] | None = None,
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
    publication: PublicationStatus = PublicationStatus.ASSISTANT_COMMITTED,
) -> TurnTrace:
    """Build one Work Item trace while retaining only its opaque HMAC identity."""

    if work_relation not in {WorkRelation.NEW, WorkRelation.CONTINUED}:
        raise ValueError("work trace relation must be new or continued")
    direct = build_direct_trace(
        namespace_key=namespace_key,
        turn_identifier=turn_identifier,
        conversation_identifier=conversation_identifier,
        intent=intent,
        playbook=playbook,
        capability_outcomes=capability_outcomes,
        capability_attempts=capability_attempts,
        continuation=continuation,
        completion=completion,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        ambiguity_present=ambiguity_present,
        partial_coverage=partial_coverage,
        state_restored=state_restored,
        latency_ms=latency_ms,
        model_calls=model_calls,
        model_call_accounting=model_call_accounting,
        capability_calls=capability_calls,
        capability_call_accounting=capability_call_accounting,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_accounting=token_accounting,
        authority_rechecked=authority_rechecked,
        publication=publication,
    )
    return replace(
        direct,
        work_item_digest=derive_trace_identifier(
            domain=TraceIdentifierDomain.WORK_ITEM,
            raw_identifier=work_item_identifier,
            namespace_key=namespace_key,
        ),
        work_relation=work_relation,
    )


def attach_trace_to_metadata(
    metadata: dict[str, Any],
    trace: TurnTrace,
    *,
    max_serialized_bytes: int = _ASSISTANT_METADATA_MAX_BYTES,
) -> bool:
    """Attach a trace only when the whole stored metadata remains readable.

    Assistant continuity readers accept one bounded 64 KiB metadata object.
    Tracing is observational and must never make attachment lineage or the
    answer itself disappear, so an over-budget trace is omitted atomically.
    """

    if not isinstance(metadata, dict) or not isinstance(trace, TurnTrace):
        return False
    if not isinstance(max_serialized_bytes, int) or isinstance(max_serialized_bytes, bool):
        return False
    if max_serialized_bytes <= 0:
        return False
    candidate = dict(metadata)
    candidate[INTERACTION_TRACE_METADATA_KEY] = trace.to_payload()
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return False
    if len(encoded.encode("utf-8")) > max_serialized_bytes:
        return False
    metadata[INTERACTION_TRACE_METADATA_KEY] = candidate[INTERACTION_TRACE_METADATA_KEY]
    return True


__all__ = [
    "INTERACTION_TRACE_METADATA_KEY",
    "attach_trace_to_metadata",
    "build_direct_trace",
    "build_committed_direct_trace",
    "build_work_trace",
    "load_trace_namespace_key",
]
