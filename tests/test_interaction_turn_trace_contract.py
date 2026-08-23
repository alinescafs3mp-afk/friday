from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from friday.interaction_control_plane import (
    TURN_TRACE_SCHEMA,
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
    TurnTraceError,
    WorkRelation,
    derive_trace_identifier,
)
from friday.interaction_control_plane.runtime_trace import attach_trace_to_metadata, build_work_trace

_KEY = bytes(range(32))


def _digest(domain: TraceIdentifierDomain, raw: str) -> str:
    return derive_trace_identifier(domain=domain, raw_identifier=raw, namespace_key=_KEY)


def _trace() -> TurnTrace:
    return TurnTrace(
        turn_digest=_digest(TraceIdentifierDomain.TURN, "telegram-message-7744"),
        conversation_digest=_digest(TraceIdentifierDomain.CONVERSATION, "private-chat-42"),
        work_item_digest=None,
        work_relation=WorkRelation.DIRECT,
        intent=IntentClass.DOCUMENT_WORK,
        continuation=ContinuationKind.NONE,
        playbook=PlaybookClass.DIRECT,
        steps=(
            CapabilityStepTrace(
                step_digest=_digest(TraceIdentifierDomain.STEP, "read-attached-document"),
                capability=CapabilityClass.DOCUMENT_RETRIEVAL,
                outcome=OutcomeStatus.SUCCEEDED,
                attempts=1,
                required=True,
            ),
        ),
        completion=CompletionDecision.COMPLETE,
        publication=PublicationStatus.ASSISTANT_COMMITTED,
        failure_stage=FailureStage.NONE,
        failure_reason=FailureReason.NONE,
        ambiguity_present=False,
        partial_coverage=False,
        state_restored=False,
        authority_rechecked=True,
        budget=TraceBudget(
            latency_ms=312,
            model_calls=1,
            model_call_accounting=CountAccounting.COMPLETE,
            capability_calls=1,
            capability_call_accounting=CountAccounting.COMPLETE,
            input_tokens=800,
            output_tokens=120,
            token_accounting=TokenAccounting.PROVIDER_REPORTED,
        ),
    )


def test_turn_trace_is_immutable_canonical_and_round_trips() -> None:
    trace = _trace()

    with pytest.raises(FrozenInstanceError):
        trace.partial_coverage = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        trace.steps[0].attempts = 2  # type: ignore[misc]

    restored_from_json = TurnTrace.parse(trace.to_json())
    restored_from_payload = TurnTrace.parse(trace.to_payload())

    assert restored_from_json == trace
    assert restored_from_payload == trace
    assert restored_from_json.to_json() == trace.to_json()
    assert trace.to_payload()["schema"] == TURN_TRACE_SCHEMA


def test_identifier_derivation_is_stable_keyed_and_domain_separated() -> None:
    raw = "low-entropy-id-42"
    turn = _digest(TraceIdentifierDomain.TURN, raw)

    assert turn == _digest(TraceIdentifierDomain.TURN, raw)
    assert turn != _digest(TraceIdentifierDomain.CONVERSATION, raw)
    assert turn != derive_trace_identifier(
        domain=TraceIdentifierDomain.TURN,
        raw_identifier=raw,
        namespace_key=b"z" * 32,
    )
    assert len(turn) == 64
    assert raw not in turn


def test_serialized_trace_contains_no_raw_identifiers_or_free_form_content() -> None:
    encoded = _trace().to_json()

    for private_value in (
        "telegram-message-7744",
        "private-chat-42",
        "read-attached-document",
        "Иван Артемьев",
        "Projects/Secret.md",
        "найди договор",
    ):
        assert private_value not in encoded
    assert not any(
        key in _trace().to_payload()
        for key in ("message", "query", "title", "path", "person_id", "tenant_id", "message_id")
    )


@pytest.mark.parametrize(
    "private_key",
    ["message", "query", "title", "path", "person_id", "tenant_id", "conversation_id", "message_id"],
)
def test_parser_rejects_every_free_form_or_raw_identifier_extension(private_key: str) -> None:
    payload = _trace().to_payload()
    payload[private_key] = "must-never-enter-an-error-or-event"

    with pytest.raises(TurnTraceError) as caught:
        TurnTrace.parse(payload)

    assert "must-never-enter" not in str(caught.value)


def test_parser_rejects_unknown_nested_keys_and_duplicate_json_keys() -> None:
    step_payload = _trace().to_payload()
    steps = step_payload["steps"]
    assert isinstance(steps, list)
    assert isinstance(steps[0], dict)
    steps[0]["tool_name"] = "private-provider-name"

    with pytest.raises(TurnTraceError, match="closed contract"):
        TurnTrace.parse(step_payload)
    with pytest.raises(TurnTraceError, match="duplicate object key"):
        TurnTrace.parse('{"schema":"first","schema":"second"}')


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent", "document_work\nprivate-query"),
        ("intent", "new_future_intent"),
        ("turn_digest", "raw-message-id"),
        ("conversation_digest", "A" * 64),
    ],
)
def test_parser_rejects_controls_unknown_enums_and_non_digest_identifiers(field: str, value: str) -> None:
    payload = _trace().to_payload()
    payload[field] = value

    with pytest.raises(TurnTraceError):
        TurnTrace.parse(payload)


def test_parser_rejects_oversize_payloads_steps_and_counts() -> None:
    with pytest.raises(TurnTraceError, match="serialized bytes"):
        TurnTrace.parse(json.dumps({"padding": "x" * 17_000}))

    too_many_steps = _trace().to_payload()
    too_many_steps["steps"] = [
        {
            "step_digest": _digest(TraceIdentifierDomain.STEP, f"step-{index}"),
            "capability": CapabilityClass.OTHER_READ.value,
            "outcome": OutcomeStatus.NOT_STARTED.value,
            "attempts": 0,
            "required": False,
        }
        for index in range(33)
    ]
    with pytest.raises(TurnTraceError, match="at most 32"):
        TurnTrace.parse(too_many_steps)

    invalid_count = _trace().to_payload()
    budget = invalid_count["budget"]
    assert isinstance(budget, dict)
    budget["model_calls"] = True
    with pytest.raises(TurnTraceError, match="model_calls"):
        TurnTrace.parse(invalid_count)


def test_token_accounting_distinguishes_unavailable_counts_from_reported_zero() -> None:
    unavailable = replace(
        _trace().budget,
        input_tokens=0,
        output_tokens=0,
        token_accounting=TokenAccounting.UNAVAILABLE,
    )
    reported_zero = replace(
        unavailable,
        token_accounting=TokenAccounting.PROVIDER_REPORTED,
    )

    assert unavailable.to_payload()["token_accounting"] == "unavailable"
    assert reported_zero.to_payload()["token_accounting"] == "provider_reported"
    with pytest.raises(TurnTraceError, match="unobserved token counts"):
        replace(unavailable, input_tokens=1)


def test_call_accounting_distinguishes_unknown_lower_bound_and_complete_counts() -> None:
    unavailable = replace(
        _trace().budget,
        model_calls=0,
        model_call_accounting=CountAccounting.UNAVAILABLE,
        capability_calls=0,
        capability_call_accounting=CountAccounting.UNAVAILABLE,
    )
    lower_bound = replace(
        unavailable,
        model_calls=1,
        model_call_accounting=CountAccounting.LOWER_BOUND,
    )
    exact_zero = replace(
        unavailable,
        model_call_accounting=CountAccounting.COMPLETE,
        capability_call_accounting=CountAccounting.COMPLETE,
    )

    assert unavailable.to_payload()["model_call_accounting"] == "unavailable"
    assert lower_bound.to_payload()["model_call_accounting"] == "lower_bound"
    assert exact_zero.to_payload()["capability_call_accounting"] == "complete"
    with pytest.raises(TurnTraceError, match="unobserved model call count"):
        replace(unavailable, model_calls=1)
    with pytest.raises(TurnTraceError, match="unobserved capability call count"):
        replace(unavailable, capability_calls=1)


def test_direct_constructor_rejects_mutable_steps_and_incoherent_closed_states() -> None:
    trace = _trace()

    with pytest.raises(TurnTraceError, match="immutable tuple"):
        replace(trace, steps=list(trace.steps))  # type: ignore[arg-type]
    with pytest.raises(TurnTraceError, match="direct work"):
        replace(trace, work_item_digest=_digest(TraceIdentifierDomain.WORK_ITEM, "work-1"))
    with pytest.raises(TurnTraceError, match="failure stage and reason"):
        replace(trace, failure_stage=FailureStage.CAPABILITY)
    with pytest.raises(TurnTraceError, match="step digests must be unique"):
        replace(trace, steps=(trace.steps[0], trace.steps[0]))
    with pytest.raises(TurnTraceError, match="partial coverage"):
        replace(trace, partial_coverage=True)
    with pytest.raises(TurnTraceError, match="retain ambiguity"):
        replace(trace, ambiguity_present=True)
    with pytest.raises(TurnTraceError, match="required step"):
        replace(
            trace,
            steps=(replace(trace.steps[0], outcome=OutcomeStatus.FAILED),),
        )
    with pytest.raises(TurnTraceError, match="failure stage"):
        replace(
            trace,
            failure_stage=FailureStage.CAPABILITY,
            failure_reason=FailureReason.PROVIDER_FAILURE,
        )
    with pytest.raises(TurnTraceError, match="failed completion"):
        replace(trace, completion=CompletionDecision.FAILED)
    with pytest.raises(TurnTraceError, match="publication failure"):
        replace(trace, publication=PublicationStatus.FAILED)


def test_optional_failed_step_does_not_make_an_otherwise_complete_trace_incoherent() -> None:
    trace = _trace()
    optional = CapabilityStepTrace(
        step_digest=_digest(TraceIdentifierDomain.STEP, "optional-observation"),
        capability=CapabilityClass.OTHER_READ,
        outcome=OutcomeStatus.FAILED,
        attempts=1,
        required=False,
    )

    assert replace(trace, steps=(*trace.steps, optional)).completion is CompletionDecision.COMPLETE


def test_legacy_continuation_can_be_observed_without_inventing_a_work_item() -> None:
    trace = replace(_trace(), continuation=ContinuationKind.REFERENCE)

    assert trace.work_relation is WorkRelation.DIRECT
    assert trace.work_item_digest is None
    assert TurnTrace.parse(trace.to_json()) == trace


@pytest.mark.parametrize(
    ("relation", "continuation"),
    (
        (WorkRelation.NEW, ContinuationKind.NONE),
        (WorkRelation.CONTINUED, ContinuationKind.CONSTRAINT_UPDATE),
    ),
)
def test_work_trace_keeps_only_a_domain_separated_work_item_digest(
    relation: WorkRelation,
    continuation: ContinuationKind,
) -> None:
    raw_work_item = "wi_0123456789abcdef01234567"
    trace = build_work_trace(
        namespace_key=_KEY,
        turn_identifier="msg_0123456789abcdef",
        conversation_identifier="conv_0123456789abcdef",
        work_item_identifier=raw_work_item,
        work_relation=relation,
        intent=IntentClass.MESSAGE_RECALL,
        playbook=PlaybookClass.RECALL_CONVERSATION,
        capability_outcomes=((CapabilityClass.MESSAGE_RETRIEVAL, OutcomeStatus.SUCCEEDED),),
        continuation=continuation,
        completion=CompletionDecision.COMPLETE,
        failure_stage=FailureStage.NONE,
        failure_reason=FailureReason.NONE,
        ambiguity_present=False,
        partial_coverage=False,
        state_restored=relation is WorkRelation.CONTINUED,
        latency_ms=1,
        model_calls=0,
        capability_calls=1,
        capability_call_accounting=CountAccounting.COMPLETE,
        authority_rechecked=True,
    )

    assert trace.work_relation is relation
    assert trace.work_item_digest == _digest(TraceIdentifierDomain.WORK_ITEM, raw_work_item)
    assert raw_work_item not in trace.to_json()
    assert TurnTrace.parse(trace.to_json()) == trace


def test_work_trace_rejects_a_direct_relation_or_missing_continuation() -> None:
    common = {
        "namespace_key": _KEY,
        "turn_identifier": "msg_0123456789abcdef",
        "conversation_identifier": "conv_0123456789abcdef",
        "work_item_identifier": "wi_0123456789abcdef01234567",
        "intent": IntentClass.MESSAGE_RECALL,
        "playbook": PlaybookClass.RECALL_CONVERSATION,
        "capability_outcomes": ((CapabilityClass.MESSAGE_RETRIEVAL, OutcomeStatus.SUCCEEDED),),
        "completion": CompletionDecision.COMPLETE,
        "failure_stage": FailureStage.NONE,
        "failure_reason": FailureReason.NONE,
        "ambiguity_present": False,
        "partial_coverage": False,
        "state_restored": False,
        "latency_ms": 1,
        "model_calls": 0,
        "capability_calls": 1,
        "capability_call_accounting": CountAccounting.COMPLETE,
        "authority_rechecked": True,
    }
    with pytest.raises(ValueError, match="relation"):
        build_work_trace(
            **common,
            work_relation=WorkRelation.DIRECT,
            continuation=ContinuationKind.NONE,
        )
    with pytest.raises(TurnTraceError, match="must declare a continuation"):
        build_work_trace(
            **common,
            work_relation=WorkRelation.CONTINUED,
            continuation=ContinuationKind.NONE,
        )


def test_trace_attachment_never_pushes_message_metadata_past_the_continuity_budget() -> None:
    metadata = {"conversation_attachment_raw_ids": ["raw_0123456789abcdef"], "padding": "x" * 15_000}
    before = dict(metadata)

    assert attach_trace_to_metadata(metadata, _trace(), max_serialized_bytes=15_200) is False
    assert metadata == before


def test_trace_attachment_is_atomic_and_records_only_assistant_commit_scope() -> None:
    metadata = {"private_context_lineage": True}

    assert attach_trace_to_metadata(metadata, _trace()) is True
    restored = TurnTrace.parse(metadata["interaction_trace"])
    assert restored.publication is PublicationStatus.ASSISTANT_COMMITTED


def test_default_trace_metadata_budget_matches_the_assistant_continuity_ceiling() -> None:
    metadata = {
        "conversation_attachment_raw_ids": ["raw_0123456789abcdef"],
        "padding": "x" * 50_000,
    }

    assert len(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")) > 16_384
    assert attach_trace_to_metadata(metadata, _trace()) is True
    assert len(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")) <= 65_536

    oversized = {"padding": "x" * 65_536}
    before = dict(oversized)
    assert attach_trace_to_metadata(oversized, _trace()) is False
    assert oversized == before


@pytest.mark.parametrize(
    ("raw_identifier", "namespace_key"),
    [
        ("", _KEY),
        ("id\nwith-control", _KEY),
        ("x" * 4_097, _KEY),
        ("valid", b"short"),
    ],
)
def test_identifier_derivation_rejects_unsafe_or_unbounded_inputs(
    raw_identifier: str, namespace_key: bytes
) -> None:
    with pytest.raises(TurnTraceError):
        derive_trace_identifier(
            domain=TraceIdentifierDomain.TURN,
            raw_identifier=raw_identifier,
            namespace_key=namespace_key,
        )
