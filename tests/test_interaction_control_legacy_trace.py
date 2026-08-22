from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from friday.interaction_control_plane.legacy_trace import (
    CapabilityStatus,
    LegacyTurnSignals,
    PublicationAuthorization,
    VerificationStatus,
    WebStatus,
    build_legacy_turn_trace,
)
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CompletionDecision,
    ContinuationKind,
    CountAccounting,
    FailureReason,
    FailureStage,
    IntentClass,
    OutcomeStatus,
    PlaybookClass,
    PublicationStatus,
    TurnTraceError,
)

_KEY = b"legacy-turn-projection-test-key!"


def _build(signals: LegacyTurnSignals):
    return build_legacy_turn_trace(
        namespace_key=_KEY,
        turn_identifier="raw-message-17",
        conversation_identifier="raw-conversation-9",
        signals=signals,
    )


def _outcomes(signals: LegacyTurnSignals) -> dict[CapabilityClass, OutcomeStatus]:
    return {step.capability: step.outcome for step in _build(signals).steps}


def test_success_projection_is_complete_closed_and_accounted() -> None:
    signals = LegacyTurnSignals(
        obsidian=CapabilityStatus.SUCCEEDED,
        model=CapabilityStatus.SUCCEEDED,
        verification=VerificationStatus.PASSED,
        authority_rechecked=True,
        latency_ms=81,
        model_calls=1,
        model_call_accounting=CountAccounting.LOWER_BOUND,
        capability_calls=2,
        capability_call_accounting=CountAccounting.COMPLETE,
    )

    trace = _build(signals)

    assert trace.intent is IntentClass.PERSONAL_ORGANIZATION
    assert trace.playbook is PlaybookClass.DIRECT
    assert trace.completion is CompletionDecision.COMPLETE
    assert trace.publication is PublicationStatus.ASSISTANT_COMMITTED
    assert trace.failure_stage is FailureStage.NONE
    assert trace.failure_reason is FailureReason.NONE
    assert trace.authority_rechecked is True
    assert trace.budget.model_calls == 1
    assert trace.budget.model_call_accounting is CountAccounting.LOWER_BOUND
    assert trace.budget.capability_calls == 2
    assert trace.budget.capability_call_accounting is CountAccounting.COMPLETE
    assert _outcomes(signals) == {
        CapabilityClass.OBSIDIAN: OutcomeStatus.SUCCEEDED,
        CapabilityClass.MODEL_SYNTHESIS: OutcomeStatus.SUCCEEDED,
        CapabilityClass.VERIFICATION: OutcomeStatus.SUCCEEDED,
    }
    encoded = trace.to_json()
    assert "raw-message-17" not in encoded
    assert "raw-conversation-9" not in encoded


def test_partial_internal_and_web_work_uses_compare_playbook() -> None:
    trace = _build(
        LegacyTurnSignals(
            document=CapabilityStatus.PARTIAL,
            web=WebStatus.PARTIAL,
            model=CapabilityStatus.SUCCEEDED,
        )
    )

    assert trace.intent is IntentClass.MIXED
    assert trace.playbook is PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES
    assert trace.partial_coverage is True
    assert trace.completion is CompletionDecision.PARTIAL
    assert {step.capability: step.outcome for step in trace.steps} == {
        CapabilityClass.DOCUMENT_RETRIEVAL: OutcomeStatus.PARTIAL,
        CapabilityClass.WEB_RESEARCH: OutcomeStatus.PARTIAL,
        CapabilityClass.MODEL_SYNTHESIS: OutcomeStatus.SUCCEEDED,
    }


def test_denied_source_answer_records_publication_failure_but_safe_notice_is_published() -> None:
    trace = _build(
        LegacyTurnSignals(
            document=CapabilityStatus.DENIED,
            publication=PublicationAuthorization.DENIED,
            authority_rechecked=True,
        )
    )

    assert (
        _outcomes(
            LegacyTurnSignals(
                document=CapabilityStatus.DENIED,
                publication=PublicationAuthorization.DENIED,
                authority_rechecked=True,
            )
        )[CapabilityClass.DOCUMENT_RETRIEVAL]
        is OutcomeStatus.DENIED
    )
    assert trace.failure_stage is FailureStage.PUBLICATION
    assert trace.failure_reason is FailureReason.AUTHORITY_DENIED
    assert trace.completion is CompletionDecision.FAILED
    assert trace.publication is PublicationStatus.ASSISTANT_COMMITTED


@pytest.mark.parametrize(
    ("signals", "stage", "reason"),
    [
        (
            LegacyTurnSignals(model=CapabilityStatus.FAILED),
            FailureStage.CAPABILITY,
            FailureReason.PROVIDER_FAILURE,
        ),
        (
            LegacyTurnSignals(
                model=CapabilityStatus.SUCCEEDED,
                verification=VerificationStatus.FAILED,
            ),
            FailureStage.SYNTHESIS_CONTRADICTION,
            FailureReason.VERIFICATION_REJECTED,
        ),
    ],
)
def test_model_and_verification_failures_are_distinct(
    signals: LegacyTurnSignals,
    stage: FailureStage,
    reason: FailureReason,
) -> None:
    trace = _build(signals)

    assert trace.failure_stage is stage
    assert trace.failure_reason is reason
    assert trace.completion is CompletionDecision.FAILED


def test_uncertain_mutation_is_neither_failed_nor_complete() -> None:
    trace = _build(LegacyTurnSignals(obsidian=CapabilityStatus.UNCERTAIN))

    assert (
        _outcomes(LegacyTurnSignals(obsidian=CapabilityStatus.UNCERTAIN))[CapabilityClass.OBSIDIAN]
        is OutcomeStatus.UNCERTAIN
    )
    assert trace.completion is CompletionDecision.UNCERTAIN
    assert trace.failure_stage is FailureStage.NONE
    assert trace.failure_reason is FailureReason.NONE


def test_one_successful_effect_cannot_hide_an_empty_required_branch() -> None:
    trace = _build(
        LegacyTurnSignals(
            obsidian=CapabilityStatus.SUCCEEDED,
            web=WebStatus.EMPTY,
        )
    )

    assert trace.completion is CompletionDecision.NOT_EVALUATED
    assert {step.capability: step.outcome for step in trace.steps} == {
        CapabilityClass.WEB_RESEARCH: OutcomeStatus.EMPTY,
        CapabilityClass.OBSIDIAN: OutcomeStatus.SUCCEEDED,
    }


@pytest.mark.parametrize(
    ("signals", "reason"),
    [
        (LegacyTurnSignals(web=WebStatus.FAILED), FailureReason.PROVIDER_FAILURE),
        (LegacyTurnSignals(obsidian=CapabilityStatus.UNAVAILABLE), FailureReason.SOURCE_UNAVAILABLE),
        (LegacyTurnSignals(document=CapabilityStatus.DENIED), FailureReason.AUTHORITY_DENIED),
    ],
)
def test_failed_unavailable_and_denied_capabilities_are_classified(
    signals: LegacyTurnSignals,
    reason: FailureReason,
) -> None:
    trace = _build(signals)

    assert trace.failure_stage is FailureStage.CAPABILITY
    assert trace.failure_reason is reason
    assert trace.completion is CompletionDecision.FAILED


def test_continuation_and_restoration_are_independent_and_ambiguity_is_explicit() -> None:
    reply = _build(
        LegacyTurnSignals(
            message=CapabilityStatus.EMPTY,
            continuation=ContinuationKind.REFERENCE,
            ambiguity_present=False,
            state_restored=False,
        )
    )
    resumed_choice = _build(
        LegacyTurnSignals(
            message=CapabilityStatus.PARTIAL,
            continuation=ContinuationKind.RESUME,
            ambiguity_present=True,
            state_restored=True,
        )
    )

    assert reply.intent is IntentClass.MESSAGE_RECALL
    assert reply.playbook is PlaybookClass.RECALL_CONVERSATION
    assert reply.continuation is ContinuationKind.REFERENCE
    assert reply.ambiguity_present is False
    assert reply.state_restored is False
    assert reply.completion is CompletionDecision.NOT_EVALUATED
    assert resumed_choice.continuation is ContinuationKind.RESUME
    assert resumed_choice.ambiguity_present is True
    assert resumed_choice.state_restored is True
    assert resumed_choice.completion is CompletionDecision.WAITING_FOR_INPUT


@pytest.mark.parametrize(
    ("web", "outcome"),
    [
        (WebStatus.SOURCED, OutcomeStatus.SUCCEEDED),
        (WebStatus.PARTIAL, OutcomeStatus.PARTIAL),
        (WebStatus.EMPTY, OutcomeStatus.EMPTY),
        (WebStatus.FAILED, OutcomeStatus.FAILED),
        (WebStatus.UNAVAILABLE, OutcomeStatus.UNAVAILABLE),
    ],
)
def test_every_closed_web_status_has_a_stable_outcome(web: WebStatus, outcome: OutcomeStatus) -> None:
    assert _outcomes(LegacyTurnSignals(web=web))[CapabilityClass.WEB_RESEARCH] is outcome


def test_fail_closed_status_parsers_do_not_echo_or_admit_unknown_values() -> None:
    private = "provider-error: Projects/Private.md"

    assert WebStatus.fail_closed("sourced") is WebStatus.SOURCED
    assert WebStatus.fail_closed(private) is WebStatus.UNAVAILABLE
    assert WebStatus.fail_closed(None) is WebStatus.UNAVAILABLE
    assert VerificationStatus.fail_closed("passed") is VerificationStatus.PASSED
    assert VerificationStatus.fail_closed(private) is VerificationStatus.UNKNOWN
    assert VerificationStatus.fail_closed(None) is VerificationStatus.UNKNOWN


def test_signals_are_frozen_typed_and_call_accounting_fails_closed() -> None:
    signals = LegacyTurnSignals()

    with pytest.raises(FrozenInstanceError):
        signals.small_talk = True  # type: ignore[misc]
    with pytest.raises(TurnTraceError, match="web must be a WebStatus"):
        LegacyTurnSignals(web="sourced")  # type: ignore[arg-type]
    with pytest.raises(TurnTraceError, match="unobserved model call count"):
        replace(signals, model_calls=1)
    with pytest.raises(TurnTraceError, match="signals must be LegacyTurnSignals"):
        build_legacy_turn_trace(
            namespace_key=_KEY,
            turn_identifier="turn",
            conversation_identifier="conversation",
            signals={"query": "private"},  # type: ignore[arg-type]
        )


def test_no_active_capability_falls_back_to_conversation() -> None:
    trace = _build(LegacyTurnSignals(small_talk=True))

    assert trace.intent is IntentClass.SMALL_TALK
    assert trace.completion is CompletionDecision.COMPLETE
    assert len(trace.steps) == 1
    assert trace.steps[0].capability is CapabilityClass.CONVERSATION
    assert trace.steps[0].outcome is OutcomeStatus.SUCCEEDED
