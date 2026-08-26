from __future__ import annotations

from dataclasses import replace

import pytest

from friday.orchestration.supervisor_contracts import (
    SUPERVISOR_REVIEW_SCHEMA,
    CapabilityEffectClass,
    CompletionCriterion,
    SupervisorContractError,
    SupervisorReview,
)
from friday.orchestration.supervisor_review_policy import (
    DeterministicReviewState,
    ReadRecoveryCandidate,
    ReviewPolicyReason,
    SupervisorReviewContext,
    admit_supervisor_review,
)

PLAN = "1" * 64
OUTCOME = "2" * 64
WORK = "3" * 64
IDEMPOTENCY = "4" * 64


def _review(
    *,
    verdict: str = "retry_read_only_step",
    failed: list[str] | None = None,
    action: str = "request_read_only_recovery",
    plan: str = PLAN,
    outcome: str = OUTCOME,
) -> SupervisorReview:
    return SupervisorReview.parse(
        {
            "schema": SUPERVISOR_REVIEW_SCHEMA,
            "plan_digest": plan,
            "outcome_digest": outcome,
            "verdict": verdict,
            "failed_criteria": (failed if failed is not None else ["current_public_evidence_has_coverage"]),
            "recommended_action": action,
            "reason_code": "public_evidence_incomplete",
        }
    )


def _candidate(
    *,
    effect: CapabilityEffectClass = CapabilityEffectClass.READ,
    criterion: CompletionCriterion = CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
    eligible: bool = True,
) -> ReadRecoveryCandidate:
    return ReadRecoveryCandidate(
        step_id="s2",
        capability_id="web.search.current",
        criterion=criterion,
        effect_class=effect,
        idempotency_key=IDEMPOTENCY,
        eligible=eligible,
    )


def _context(**changes: object) -> SupervisorReviewContext:
    values: dict[str, object] = {
        "plan_digest": PLAN,
        "outcome_digest": OUTCOME,
        "work_item_digest": WORK,
        "work_revision": 4,
        "deterministic_state": DeterministicReviewState.PARTIAL,
        "failed_criteria": (CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,),
        "review_round": 1,
        "max_review_rounds": 1,
        "recovery_budget_remaining": 1,
        "effect_started": False,
        "publication_started": False,
        "recovery_candidate": _candidate(),
    }
    values.update(changes)
    return SupervisorReviewContext(**values)  # type: ignore[arg-type]


def test_one_declared_read_recovery_is_admitted_but_not_executed() -> None:
    review = _review()
    context = _context()

    decision = admit_supervisor_review(review, context)

    assert decision.admitted is True
    assert decision.reason is ReviewPolicyReason.ADMITTED
    assert decision.recommended_action is not None
    recovery = decision.recovery
    assert recovery is not None
    assert recovery.step_id == "s2"
    assert recovery.capability_id == "web.search.current"
    assert recovery.idempotency_key == IDEMPOTENCY
    assert recovery.review_digest == review.canonical_sha256()
    assert recovery.context_digest == context.canonical_sha256()


def test_complete_deterministic_result_accepts_only_complete_publish_review() -> None:
    context = _context(
        deterministic_state=DeterministicReviewState.COMPLETE,
        failed_criteria=(),
        recovery_budget_remaining=0,
        recovery_candidate=None,
    )
    complete = _review(verdict="complete", failed=[], action="publish")

    assert admit_supervisor_review(complete, context).admitted is True
    inconsistent = _review()
    assert admit_supervisor_review(inconsistent, context).reason is ReviewPolicyReason.CRITERIA_MISMATCH


@pytest.mark.parametrize(
    ("review", "context", "reason"),
    [
        (_review(plan="5" * 64), _context(), ReviewPolicyReason.DIGEST_MISMATCH),
        (_review(outcome="6" * 64), _context(), ReviewPolicyReason.DIGEST_MISMATCH),
        (_review(), _context(review_round=0), ReviewPolicyReason.REVIEW_ROUND_EXHAUSTED),
        (_review(), _context(max_review_rounds=0), ReviewPolicyReason.REVIEW_ROUND_EXHAUSTED),
        (
            _review(),
            _context(publication_started=True),
            ReviewPolicyReason.PUBLICATION_ALREADY_STARTED,
        ),
        (_review(), _context(effect_started=True), ReviewPolicyReason.EFFECT_SCOPE_NOT_ADMITTED),
        (
            _review(),
            _context(failed_criteria=(CompletionCriterion.CURRENT_ATTACHMENT_EVIDENCE_PRESENT,)),
            ReviewPolicyReason.CRITERIA_MISMATCH,
        ),
        (
            _review(verdict="complete", failed=[], action="publish"),
            _context(),
            ReviewPolicyReason.CRITERIA_MISMATCH,
        ),
        (
            _review(),
            _context(recovery_candidate=None),
            ReviewPolicyReason.RECOVERY_NOT_DECLARED,
        ),
        (
            _review(),
            _context(recovery_candidate=_candidate(eligible=False)),
            ReviewPolicyReason.RECOVERY_NOT_DECLARED,
        ),
        (
            _review(),
            _context(recovery_candidate=_candidate(effect=CapabilityEffectClass.WRITE)),
            ReviewPolicyReason.RECOVERY_NOT_READ_ONLY,
        ),
        (
            _review(),
            _context(recovery_budget_remaining=0),
            ReviewPolicyReason.RECOVERY_BUDGET_EXHAUSTED,
        ),
    ],
)
def test_review_policy_rejects_scope_lifecycle_and_recovery_widening(
    review: SupervisorReview,
    context: SupervisorReviewContext,
    reason: ReviewPolicyReason,
) -> None:
    decision = admit_supervisor_review(review, context)
    assert decision.admitted is False
    assert decision.reason is reason
    assert decision.recovery is None


@pytest.mark.parametrize(
    "changes",
    [
        {"recommended_action": "publish"},
        {"failed_criteria": []},
        {
            "failed_criteria": [
                "current_public_evidence_has_coverage",
                "current_public_evidence_has_coverage",
            ]
        },
        {
            "failed_criteria": [
                "current_public_evidence_has_coverage",
                "current_attachment_evidence_present",
            ]
        },
    ],
)
def test_review_parser_rejects_contradictory_or_duplicate_recovery(changes: dict[str, object]) -> None:
    payload = _review().payload()
    payload.update(changes)
    with pytest.raises(SupervisorContractError):
        SupervisorReview.parse(payload)


def test_non_recovery_review_cannot_manufacture_a_recovery() -> None:
    review = _review(
        verdict="use_primary_only",
        action="use_primary_only",
    )
    decision = admit_supervisor_review(review, _context())
    assert decision.admitted is True
    assert decision.recovery is None


def test_context_digest_changes_with_work_revision_and_recovery_budget() -> None:
    context = _context()
    assert context.canonical_sha256() != replace(context, work_revision=5).canonical_sha256()
    assert (
        context.canonical_sha256()
        != replace(
            context,
            recovery_budget_remaining=0,
        ).canonical_sha256()
    )
