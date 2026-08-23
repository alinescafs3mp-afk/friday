from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from friday.orchestration.capability_outcome import (
    CAPABILITY_OUTCOME_SCHEMA,
    CapabilityOutcome,
    CapabilityOutcomeError,
    CapabilityOutcomeReason,
    CapabilityOutcomeStatus,
    CompletionGateDecision,
    evaluate_read_only_completion,
    require_complete_read_only_publication,
)
from friday.orchestration.contracts import RouteClass

_PLAN = "a" * 64
_EVIDENCE = "b" * 64


def _outcome(
    status: CapabilityOutcomeStatus,
    *,
    route: RouteClass = RouteClass.FILE_READ,
) -> CapabilityOutcome:
    if status in {CapabilityOutcomeStatus.COMPLETE, CapabilityOutcomeStatus.PARTIAL}:
        evidence: str | None = _EVIDENCE
        citations = ("A1",)
        authority_rechecked = True
        verified = True
    elif status is CapabilityOutcomeStatus.EMPTY:
        evidence = _EVIDENCE
        citations = ()
        authority_rechecked = True
        verified = True
    elif status is CapabilityOutcomeStatus.UNAVAILABLE:
        evidence = None
        citations = ()
        authority_rechecked = False
        verified = False
    else:
        evidence = None
        citations = ()
        authority_rechecked = True
        verified = False
    return CapabilityOutcome(
        route=route,
        status=status,
        plan_sha256=_PLAN,
        evidence_identity_sha256=evidence,
        citation_labels=citations,
        authority_rechecked=authority_rechecked,
        verified=verified,
    )


def _gate(outcome: CapabilityOutcome) -> CompletionGateDecision:
    answer = "Проверенный ответ. [A1]" if outcome.citation_labels else "Закрытый результат."
    return evaluate_read_only_completion(
        outcome,
        expected_route=outcome.route,
        expected_plan_sha256=outcome.plan_sha256,
        expected_evidence_identity_sha256=outcome.evidence_identity_sha256,
        expected_citation_labels=outcome.citation_labels,
        answer=answer,
        authority_rechecked=outcome.authority_rechecked,
        verification_passed=outcome.verified,
    )


def test_capability_outcome_is_immutable_canonical_closed_and_round_trips() -> None:
    outcome = _outcome(CapabilityOutcomeStatus.COMPLETE)

    assert CapabilityOutcome.parse(outcome.to_json()) == outcome
    assert CapabilityOutcome.parse(outcome.to_payload()) == outcome
    assert outcome.to_payload()["schema"] == CAPABILITY_OUTCOME_SCHEMA
    assert len(outcome.canonical_sha256()) == 64
    with pytest.raises(FrozenInstanceError):
        outcome.status = CapabilityOutcomeStatus.EMPTY  # type: ignore[misc]
    with pytest.raises(TypeError):
        outcome.citation_labels[0] = "A2"  # type: ignore[index]

    payload = json.dumps(outcome.to_payload(), ensure_ascii=False, sort_keys=True)
    for forbidden in ("Проверенный ответ", "/private/path", "raw_0123456789abcdef"):
        assert forbidden not in payload


@pytest.mark.parametrize(
    ("status", "reason", "retryable", "decision"),
    (
        (
            CapabilityOutcomeStatus.COMPLETE,
            CapabilityOutcomeReason.NONE,
            False,
            CompletionGateDecision.READY_TO_PUBLISH,
        ),
        (
            CapabilityOutcomeStatus.PARTIAL,
            CapabilityOutcomeReason.PARTIAL_COVERAGE,
            False,
            CompletionGateDecision.RETURN_PARTIAL,
        ),
        (
            CapabilityOutcomeStatus.EMPTY,
            CapabilityOutcomeReason.NO_EVIDENCE,
            False,
            CompletionGateDecision.RETURN_EMPTY,
        ),
        (
            CapabilityOutcomeStatus.UNAVAILABLE,
            CapabilityOutcomeReason.CAPABILITY_UNAVAILABLE,
            True,
            CompletionGateDecision.RETRY,
        ),
        (
            CapabilityOutcomeStatus.DENIED,
            CapabilityOutcomeReason.AUTHORITY_DENIED,
            False,
            CompletionGateDecision.DENY,
        ),
    ),
)
def test_status_reason_retryability_and_gate_decision_are_code_owned(
    status: CapabilityOutcomeStatus,
    reason: CapabilityOutcomeReason,
    retryable: bool,
    decision: CompletionGateDecision,
) -> None:
    outcome = _outcome(status)

    assert outcome.reason_code is reason
    assert outcome.retryable is retryable
    assert _gate(outcome) is decision


@pytest.mark.parametrize(
    "status",
    (
        CapabilityOutcomeStatus.PARTIAL,
        CapabilityOutcomeStatus.EMPTY,
        CapabilityOutcomeStatus.UNAVAILABLE,
        CapabilityOutcomeStatus.DENIED,
    ),
)
def test_only_complete_is_publishable_by_the_current_routes(status: CapabilityOutcomeStatus) -> None:
    outcome = _outcome(status)
    with pytest.raises(CapabilityOutcomeError, match="not complete"):
        require_complete_read_only_publication(
            outcome,
            expected_route=outcome.route,
            expected_plan_sha256=outcome.plan_sha256,
            expected_evidence_identity_sha256=outcome.evidence_identity_sha256,
            expected_citation_labels=outcome.citation_labels,
            answer="Ответ [A1]" if outcome.citation_labels else "Закрытый результат",
            authority_rechecked=outcome.authority_rechecked,
            verification_passed=outcome.verified,
        )


def test_complete_outcome_passes_for_both_existing_routes() -> None:
    for route in (RouteClass.FILE_READ, RouteClass.ARCHIVE_READ):
        outcome = _outcome(CapabilityOutcomeStatus.COMPLETE, route=route)
        assert (
            require_complete_read_only_publication(
                outcome,
                expected_route=route,
                expected_plan_sha256=_PLAN,
                expected_evidence_identity_sha256=_EVIDENCE,
                expected_citation_labels=("A1",),
                answer="Ответ по источнику. [A1]",
                authority_rechecked=True,
                verification_passed=True,
            )
            is outcome
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: replace(item, route=RouteClass.WEB_READ),
        lambda item: replace(item, evidence_identity_sha256=None),
        lambda item: replace(item, citation_labels=()),
        lambda item: replace(item, authority_rechecked=False),
        lambda item: replace(item, verified=False),
        lambda item: replace(item, citation_labels=("A1", "A1")),
        lambda item: replace(item, citation_labels=["A1"]),
    ),
)
def test_complete_contract_rejects_route_evidence_citation_and_authority_mutations(mutation) -> None:
    with pytest.raises(CapabilityOutcomeError):
        mutation(_outcome(CapabilityOutcomeStatus.COMPLETE))


def test_noncomplete_status_shapes_cannot_claim_incompatible_state() -> None:
    with pytest.raises(CapabilityOutcomeError):
        replace(_outcome(CapabilityOutcomeStatus.EMPTY), citation_labels=("A1",))
    with pytest.raises(CapabilityOutcomeError):
        replace(_outcome(CapabilityOutcomeStatus.UNAVAILABLE), authority_rechecked=True)
    with pytest.raises(CapabilityOutcomeError):
        replace(_outcome(CapabilityOutcomeStatus.DENIED), authority_rechecked=False)
    with pytest.raises(CapabilityOutcomeError):
        replace(_outcome(CapabilityOutcomeStatus.DENIED), evidence_identity_sha256=_EVIDENCE)


def test_parser_rejects_unknown_duplicate_and_status_derived_mutations() -> None:
    payload = _outcome(CapabilityOutcomeStatus.COMPLETE).to_payload()
    payload["private_body"] = "secret"
    with pytest.raises(CapabilityOutcomeError, match="closed contract"):
        CapabilityOutcome.parse(payload)

    duplicate = (
        _outcome(CapabilityOutcomeStatus.COMPLETE)
        .to_json()
        .replace(
            '"schema":',
            '"schema":"duplicate","schema":',
            1,
        )
    )
    with pytest.raises(CapabilityOutcomeError, match="duplicate"):
        CapabilityOutcome.parse(duplicate)

    for key, value in (("retryable", True), ("reason_code", "capability_unavailable")):
        mutated = _outcome(CapabilityOutcomeStatus.COMPLETE).to_payload()
        mutated[key] = value
        with pytest.raises(CapabilityOutcomeError, match="retryability or reason"):
            CapabilityOutcome.parse(mutated)


@pytest.mark.parametrize(
    "overrides",
    (
        {"expected_route": RouteClass.ARCHIVE_READ},
        {"expected_plan_sha256": "c" * 64},
        {"expected_evidence_identity_sha256": "d" * 64},
        {"expected_citation_labels": ("A2",)},
        {"answer": "Ответ без метки"},
        {"authority_rechecked": False},
        {"verification_passed": False},
    ),
)
def test_gate_fails_closed_on_every_publication_binding(overrides: dict[str, object]) -> None:
    outcome = _outcome(CapabilityOutcomeStatus.COMPLETE)
    inputs: dict[str, object] = {
        "expected_route": RouteClass.FILE_READ,
        "expected_plan_sha256": _PLAN,
        "expected_evidence_identity_sha256": _EVIDENCE,
        "expected_citation_labels": ("A1",),
        "answer": "Ответ. [A1]",
        "authority_rechecked": True,
        "verification_passed": True,
    }
    inputs.update(overrides)

    with pytest.raises(CapabilityOutcomeError):
        evaluate_read_only_completion(outcome, **inputs)  # type: ignore[arg-type]
