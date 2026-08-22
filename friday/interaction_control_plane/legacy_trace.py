"""Closed semantic projection from the legacy runtime into Turn Trace v1.

Only structural states cross this boundary. Raw prompts, queries, paths,
people, providers and account identifiers cannot enter ``LegacyTurnSignals``.
The builder accepts message/conversation identifiers separately and passes them
straight to the keyed digest boundary in :func:`build_direct_trace`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from friday.interaction_control_plane.runtime_trace import build_direct_trace
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
    TurnTrace,
    TurnTraceError,
)

_MAX_LATENCY_MS = 86_400_000
_MAX_CALLS = 1_024


class CapabilityStatus(StrEnum):
    """Closed final state for a legacy capability other than web/verification."""

    INACTIVE = "inactive"
    NOT_STARTED = "not_started"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    EMPTY = "empty"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    UNCERTAIN = "uncertain"

    @classmethod
    def fail_closed(cls, value: object) -> Self:
        """Parse a private runtime signal without reflecting unknown input."""

        try:
            return cls(value) if isinstance(value, str) else cls.UNAVAILABLE
        except ValueError:
            return cls.UNAVAILABLE


class WebStatus(StrEnum):
    """Closed legacy web-evidence states."""

    NONE = "none"
    SOURCED = "sourced"
    PARTIAL = "partial"
    EMPTY = "empty"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"

    @classmethod
    def fail_closed(cls, value: object) -> Self:
        """Parse a legacy value without reflecting unknown/private input."""

        try:
            return cls(value) if isinstance(value, str) else cls.UNAVAILABLE
        except ValueError:
            return cls.UNAVAILABLE


class VerificationStatus(StrEnum):
    """Closed legacy answer-verification states."""

    SKIPPED = "skipped"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def fail_closed(cls, value: object) -> Self:
        """Parse a legacy value without reflecting unknown/private input."""

        try:
            return cls(value) if isinstance(value, str) else cls.UNKNOWN
        except ValueError:
            return cls.UNKNOWN


class PublicationAuthorization(StrEnum):
    """Whether source-derived content survived the final authority check."""

    AUTHORIZED = "authorized"
    DENIED = "denied"


def _require_bool(value: object, *, label: str) -> None:
    if not isinstance(value, bool):
        raise TurnTraceError(f"{label} must be a boolean")


def _require_int(value: object, *, label: str, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise TurnTraceError(f"{label} must be an integer between 0 and {maximum}")


def _require_enum(value: object, enum_type: type[StrEnum], *, label: str) -> None:
    if not isinstance(value, enum_type):
        raise TurnTraceError(f"{label} must be a {enum_type.__name__}")


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyTurnSignals:
    """Privacy-safe, immutable final signals from one legacy turn."""

    document: CapabilityStatus = CapabilityStatus.INACTIVE
    message: CapabilityStatus = CapabilityStatus.INACTIVE
    web: WebStatus = WebStatus.NONE
    entity: CapabilityStatus = CapabilityStatus.INACTIVE
    obsidian: CapabilityStatus = CapabilityStatus.INACTIVE
    file: CapabilityStatus = CapabilityStatus.INACTIVE
    personal: CapabilityStatus = CapabilityStatus.INACTIVE
    model: CapabilityStatus = CapabilityStatus.INACTIVE
    verification: VerificationStatus = VerificationStatus.SKIPPED
    publication: PublicationAuthorization = PublicationAuthorization.AUTHORIZED
    small_talk: bool = False
    continuation: ContinuationKind = ContinuationKind.NONE
    coverage_partial: bool = False
    ambiguity_present: bool = False
    state_restored: bool = False
    authority_rechecked: bool = False
    latency_ms: int = 0
    model_calls: int = 0
    model_call_accounting: CountAccounting = CountAccounting.UNAVAILABLE
    capability_calls: int = 0
    capability_call_accounting: CountAccounting = CountAccounting.UNAVAILABLE

    def __post_init__(self) -> None:
        for label in ("document", "message", "entity", "obsidian", "file", "personal", "model"):
            _require_enum(getattr(self, label), CapabilityStatus, label=label)
        _require_enum(self.web, WebStatus, label="web")
        _require_enum(self.verification, VerificationStatus, label="verification")
        _require_enum(self.publication, PublicationAuthorization, label="publication")
        _require_enum(self.continuation, ContinuationKind, label="continuation")
        _require_enum(self.model_call_accounting, CountAccounting, label="model_call_accounting")
        _require_enum(
            self.capability_call_accounting,
            CountAccounting,
            label="capability_call_accounting",
        )
        for label in (
            "small_talk",
            "coverage_partial",
            "ambiguity_present",
            "state_restored",
            "authority_rechecked",
        ):
            _require_bool(getattr(self, label), label=label)
        _require_int(self.latency_ms, label="latency_ms", maximum=_MAX_LATENCY_MS)
        _require_int(self.model_calls, label="model_calls", maximum=_MAX_CALLS)
        _require_int(self.capability_calls, label="capability_calls", maximum=_MAX_CALLS)
        if self.model_call_accounting is CountAccounting.UNAVAILABLE and self.model_calls:
            raise TurnTraceError("unobserved model call count must be zero")
        if self.capability_call_accounting is CountAccounting.UNAVAILABLE and self.capability_calls:
            raise TurnTraceError("unobserved capability call count must be zero")


def _active(status: CapabilityStatus) -> bool:
    return status is not CapabilityStatus.INACTIVE


def _outcome(status: CapabilityStatus) -> OutcomeStatus:
    if status is CapabilityStatus.INACTIVE:
        raise TurnTraceError("inactive capability cannot become a trace step")
    return OutcomeStatus(status.value)


def _intent(signals: LegacyTurnSignals) -> IntentClass:
    document = _active(signals.document)
    message = _active(signals.message)
    web = signals.web is not WebStatus.NONE
    entity = _active(signals.entity)
    obsidian = _active(signals.obsidian)
    file_or_personal = _active(signals.file) or _active(signals.personal)
    domain_count = sum(int(active) for active in (document, message, web, entity, obsidian, file_or_personal))
    if domain_count > 1:
        return IntentClass.MIXED
    if signals.small_talk:
        return IntentClass.SMALL_TALK
    if message:
        return IntentClass.MESSAGE_RECALL
    if document:
        return IntentClass.DOCUMENT_WORK
    if web:
        return IntentClass.WEB_RESEARCH
    if entity:
        return IntentClass.ENTITY_LOOKUP
    if obsidian or _active(signals.personal):
        return IntentClass.PERSONAL_ORGANIZATION
    if _active(signals.file):
        return IntentClass.EFFECT
    return IntentClass.ORDINARY_DIALOGUE


def _playbook(signals: LegacyTurnSignals) -> PlaybookClass:
    if _active(signals.document) and signals.web is not WebStatus.NONE:
        return PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES
    if _active(signals.message):
        return PlaybookClass.RECALL_CONVERSATION
    if _active(signals.document):
        return PlaybookClass.LOCATE_AND_EXPLAIN_DOCUMENT
    return PlaybookClass.DIRECT


def _capability_outcomes(signals: LegacyTurnSignals) -> tuple[tuple[CapabilityClass, OutcomeStatus], ...]:
    outcomes: list[tuple[CapabilityClass, OutcomeStatus]] = []
    for capability, status in (
        (CapabilityClass.DOCUMENT_RETRIEVAL, signals.document),
        (CapabilityClass.MESSAGE_RETRIEVAL, signals.message),
    ):
        if _active(status):
            outcomes.append((capability, _outcome(status)))
    if signals.web is not WebStatus.NONE:
        outcomes.append(
            (
                CapabilityClass.WEB_RESEARCH,
                {
                    WebStatus.SOURCED: OutcomeStatus.SUCCEEDED,
                    WebStatus.PARTIAL: OutcomeStatus.PARTIAL,
                    WebStatus.EMPTY: OutcomeStatus.EMPTY,
                    WebStatus.FAILED: OutcomeStatus.FAILED,
                    WebStatus.UNAVAILABLE: OutcomeStatus.UNAVAILABLE,
                }[signals.web],
            )
        )
    for capability, status in (
        (CapabilityClass.ENTITY_LOOKUP, signals.entity),
        (CapabilityClass.OBSIDIAN, signals.obsidian),
        (CapabilityClass.FILE_GENERATION, signals.file),
        (CapabilityClass.PERSONAL_ORGANIZATION, signals.personal),
        (CapabilityClass.MODEL_SYNTHESIS, signals.model),
    ):
        if _active(status):
            outcomes.append((capability, _outcome(status)))
    if signals.verification is not VerificationStatus.SKIPPED:
        outcomes.append(
            (
                CapabilityClass.VERIFICATION,
                OutcomeStatus.SUCCEEDED
                if signals.verification is VerificationStatus.PASSED
                else OutcomeStatus.FAILED
                if signals.verification is VerificationStatus.FAILED
                else OutcomeStatus.UNAVAILABLE,
            )
        )
    if not outcomes:
        outcomes.append((CapabilityClass.CONVERSATION, OutcomeStatus.SUCCEEDED))
    return tuple(outcomes)


def build_legacy_turn_trace(
    *,
    namespace_key: bytes,
    turn_identifier: str,
    conversation_identifier: str,
    signals: LegacyTurnSignals,
) -> TurnTrace:
    """Build the final legacy trace after a durable assistant publication.

    A denied source-derived response becomes a safe denial notice. The stored
    trace therefore records only that the assistant row committed; transport
    delivery is outside this boundary.
    """

    if not isinstance(signals, LegacyTurnSignals):
        raise TurnTraceError("signals must be LegacyTurnSignals")
    outcomes = _capability_outcomes(signals)
    partial_coverage = bool(signals.coverage_partial or signals.web is WebStatus.PARTIAL)
    uncertain_outcome = any(outcome is OutcomeStatus.UNCERTAIN for _, outcome in outcomes)
    all_outcomes_succeeded = all(outcome is OutcomeStatus.SUCCEEDED for _, outcome in outcomes)

    if signals.publication is PublicationAuthorization.DENIED:
        failure_stage = FailureStage.PUBLICATION
        failure_reason = FailureReason.AUTHORITY_DENIED
    elif signals.model is CapabilityStatus.FAILED:
        failure_stage = FailureStage.CAPABILITY
        failure_reason = FailureReason.PROVIDER_FAILURE
    elif signals.verification is VerificationStatus.FAILED:
        failure_stage = FailureStage.SYNTHESIS_CONTRADICTION
        failure_reason = FailureReason.VERIFICATION_REJECTED
    elif any(
        outcome is OutcomeStatus.DENIED
        for capability, outcome in outcomes
        if capability not in {CapabilityClass.MODEL_SYNTHESIS, CapabilityClass.VERIFICATION}
    ):
        failure_stage = FailureStage.CAPABILITY
        failure_reason = FailureReason.AUTHORITY_DENIED
    elif any(
        outcome is OutcomeStatus.FAILED
        for capability, outcome in outcomes
        if capability not in {CapabilityClass.MODEL_SYNTHESIS, CapabilityClass.VERIFICATION}
    ):
        failure_stage = FailureStage.CAPABILITY
        failure_reason = FailureReason.PROVIDER_FAILURE
    elif any(
        outcome in {OutcomeStatus.NOT_STARTED, OutcomeStatus.UNAVAILABLE}
        for capability, outcome in outcomes
        if capability
        not in {
            CapabilityClass.CONVERSATION,
            CapabilityClass.MODEL_SYNTHESIS,
            CapabilityClass.VERIFICATION,
        }
    ):
        failure_stage = FailureStage.CAPABILITY
        failure_reason = FailureReason.SOURCE_UNAVAILABLE
    else:
        failure_stage = FailureStage.NONE
        failure_reason = FailureReason.NONE

    completion = (
        CompletionDecision.FAILED
        if failure_stage is not FailureStage.NONE
        else CompletionDecision.WAITING_FOR_INPUT
        if signals.ambiguity_present
        else CompletionDecision.UNCERTAIN
        if uncertain_outcome
        else CompletionDecision.PARTIAL
        if partial_coverage
        else CompletionDecision.COMPLETE
        if all_outcomes_succeeded and (signals.small_talk or signals.obsidian is CapabilityStatus.SUCCEEDED)
        else CompletionDecision.NOT_EVALUATED
    )

    return build_direct_trace(
        namespace_key=namespace_key,
        turn_identifier=turn_identifier,
        conversation_identifier=conversation_identifier,
        intent=_intent(signals),
        playbook=_playbook(signals),
        capability_outcomes=outcomes,
        continuation=signals.continuation,
        completion=completion,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        ambiguity_present=signals.ambiguity_present,
        partial_coverage=partial_coverage,
        state_restored=signals.state_restored,
        latency_ms=signals.latency_ms,
        model_calls=signals.model_calls,
        model_call_accounting=signals.model_call_accounting,
        capability_calls=signals.capability_calls,
        capability_call_accounting=signals.capability_call_accounting,
        authority_rechecked=signals.authority_rechecked,
    )


__all__ = [
    "CapabilityStatus",
    "LegacyTurnSignals",
    "PublicationAuthorization",
    "VerificationStatus",
    "WebStatus",
    "build_legacy_turn_trace",
]
