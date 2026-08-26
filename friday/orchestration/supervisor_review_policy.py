"""Deterministic P4 admission for one advisory supervisor review.

The model can classify a body-free outcome summary.  Only this policy may map
that review to one already-declared read step, and it never executes the step.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from enum import StrEnum

from friday.orchestration.supervisor_contracts import (
    SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
    SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
    CapabilityEffectClass,
    CompletionCriterion,
    ReviewRecommendedAction,
    ReviewVerdict,
    SupervisorReview,
    canonical_sha256,
)

SUPERVISOR_REVIEW_CONTEXT_SCHEMA = "friday.supervisor-review-context.v2"
SUPERVISOR_REVIEW_POLICY_VERSION = "semantic-supervisor-review-policy-v2"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class DeterministicReviewState(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    PARTIAL = "partial"
    FAILED = "failed"


class ReviewPolicyReason(StrEnum):
    ADMITTED = "admitted"
    DIGEST_MISMATCH = "digest_mismatch"
    PRODUCT_POLICY_MISMATCH = "product_policy_mismatch"
    REVIEW_ROUND_EXHAUSTED = "review_round_exhausted"
    PUBLICATION_ALREADY_STARTED = "publication_already_started"
    EFFECT_SCOPE_NOT_ADMITTED = "effect_scope_not_admitted"
    CRITERIA_MISMATCH = "criteria_mismatch"
    DETERMINISTIC_VERDICT_MISMATCH = "deterministic_verdict_mismatch"
    RECOVERY_NOT_DECLARED = "recovery_not_declared"
    RECOVERY_NOT_READ_ONLY = "recovery_not_read_only"
    RECOVERY_BUDGET_EXHAUSTED = "recovery_budget_exhausted"


@dataclass(frozen=True, slots=True)
class ReadRecoveryCandidate:
    """One code-declared failed read step; model output cannot create it."""

    step_id: str
    capability_id: str
    criterion: CompletionCriterion
    effect_class: CapabilityEffectClass
    idempotency_key: str
    eligible: bool

    def __post_init__(self) -> None:
        if _SAFE_ID_RE.fullmatch(self.step_id) is None:
            raise ValueError("recovery step_id is invalid")
        if _SAFE_ID_RE.fullmatch(self.capability_id) is None:
            raise ValueError("recovery capability_id is invalid")
        if not isinstance(self.criterion, CompletionCriterion):
            raise ValueError("recovery criterion is invalid")
        if not isinstance(self.effect_class, CapabilityEffectClass):
            raise ValueError("recovery effect class is invalid")
        if _DIGEST_RE.fullmatch(self.idempotency_key) is None:
            raise ValueError("recovery idempotency key is invalid")
        if not isinstance(self.eligible, bool):
            raise ValueError("recovery eligibility must be boolean")


@dataclass(frozen=True, slots=True)
class SupervisorReviewContext:
    """Body-free, Work-Item-revision-bound input to review admission."""

    plan_digest: str
    outcome_digest: str
    work_item_digest: str
    work_revision: int
    deterministic_state: DeterministicReviewState
    failed_criteria: tuple[CompletionCriterion, ...]
    review_round: int
    max_review_rounds: int
    recovery_budget_remaining: int
    effect_started: bool
    publication_started: bool
    recovery_candidate: ReadRecoveryCandidate | None
    product_policy_id: str = SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
    product_policy_sha256: str = SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256

    def __post_init__(self) -> None:
        for label, value in (
            ("plan_digest", self.plan_digest),
            ("outcome_digest", self.outcome_digest),
            ("work_item_digest", self.work_item_digest),
        ):
            if _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if _SAFE_ID_RE.fullmatch(self.product_policy_id) is None:
            raise ValueError("product_policy_id is invalid")
        if _DIGEST_RE.fullmatch(self.product_policy_sha256) is None:
            raise ValueError("product_policy_sha256 is invalid")
        if not isinstance(self.work_revision, int) or isinstance(self.work_revision, bool):
            raise ValueError("work_revision is invalid")
        if self.work_revision < 1:
            raise ValueError("work_revision is invalid")
        if not isinstance(self.deterministic_state, DeterministicReviewState):
            raise ValueError("deterministic_state is invalid")
        if (
            not isinstance(self.failed_criteria, tuple)
            or len(self.failed_criteria) > 4
            or len(set(self.failed_criteria)) != len(self.failed_criteria)
            or any(not isinstance(item, CompletionCriterion) for item in self.failed_criteria)
        ):
            raise ValueError("failed_criteria are invalid")
        if self.deterministic_state is DeterministicReviewState.COMPLETE:
            if self.failed_criteria:
                raise ValueError("complete deterministic state cannot have failed criteria")
        elif not self.failed_criteria:
            raise ValueError("non-complete deterministic state needs failed criteria")
        for numeric_label, numeric_value in (
            ("review_round", self.review_round),
            ("max_review_rounds", self.max_review_rounds),
            ("recovery_budget_remaining", self.recovery_budget_remaining),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value not in {0, 1}
            ):
                raise ValueError(f"{numeric_label} must be zero or one")
        if not isinstance(self.effect_started, bool) or not isinstance(self.publication_started, bool):
            raise ValueError("review lifecycle fields must be boolean")

    def payload(self) -> dict[str, object]:
        candidate = self.recovery_candidate
        return {
            "schema": SUPERVISOR_REVIEW_CONTEXT_SCHEMA,
            "product_policy_id": self.product_policy_id,
            "product_policy_sha256": self.product_policy_sha256,
            "plan_digest": self.plan_digest,
            "outcome_digest": self.outcome_digest,
            "work_item_digest": self.work_item_digest,
            "work_revision": self.work_revision,
            "deterministic_state": self.deterministic_state.value,
            "failed_criteria": [item.value for item in self.failed_criteria],
            "review_round": self.review_round,
            "max_review_rounds": self.max_review_rounds,
            "recovery_budget_remaining": self.recovery_budget_remaining,
            "effect_started": self.effect_started,
            "publication_started": self.publication_started,
            "recovery_candidate": (
                None
                if candidate is None
                else {
                    "step_id": candidate.step_id,
                    "capability_id": candidate.capability_id,
                    "criterion": candidate.criterion.value,
                    "effect_class": candidate.effect_class.value,
                    "idempotency_key": candidate.idempotency_key,
                    "eligible": candidate.eligible,
                }
            ),
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class AdmittedReadRecovery:
    step_id: str
    capability_id: str
    criterion: CompletionCriterion
    idempotency_key: str
    review_digest: str
    context_digest: str


@dataclass(frozen=True, slots=True)
class SupervisorReviewDecision:
    admitted: bool
    reason: ReviewPolicyReason
    recommended_action: ReviewRecommendedAction | None = None
    recovery: AdmittedReadRecovery | None = None

    @property
    def reason_code(self) -> str:
        return self.reason.value


def _reject(reason: ReviewPolicyReason) -> SupervisorReviewDecision:
    return SupervisorReviewDecision(admitted=False, reason=reason)


def admit_supervisor_review(
    review: SupervisorReview,
    context: SupervisorReviewContext,
) -> SupervisorReviewDecision:
    """Admit one review or one predeclared read recovery recommendation."""

    if not isinstance(review, SupervisorReview) or not isinstance(context, SupervisorReviewContext):
        raise TypeError("review admission requires typed contracts")
    if context.product_policy_id != SUPERVISOR_ASSIST_PRODUCT_POLICY_ID or not hmac.compare_digest(
        context.product_policy_sha256,
        SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
    ):
        return _reject(ReviewPolicyReason.PRODUCT_POLICY_MISMATCH)
    if not hmac.compare_digest(review.plan_digest, context.plan_digest) or not hmac.compare_digest(
        review.outcome_digest,
        context.outcome_digest,
    ):
        return _reject(ReviewPolicyReason.DIGEST_MISMATCH)
    if context.max_review_rounds != 1 or context.review_round != 1:
        return _reject(ReviewPolicyReason.REVIEW_ROUND_EXHAUSTED)
    if context.publication_started:
        return _reject(ReviewPolicyReason.PUBLICATION_ALREADY_STARTED)
    if context.effect_started:
        return _reject(ReviewPolicyReason.EFFECT_SCOPE_NOT_ADMITTED)
    if set(review.failed_criteria) != set(context.failed_criteria):
        return _reject(ReviewPolicyReason.CRITERIA_MISMATCH)
    if context.deterministic_state is DeterministicReviewState.COMPLETE:
        if review.verdict is not ReviewVerdict.COMPLETE:
            return _reject(ReviewPolicyReason.DETERMINISTIC_VERDICT_MISMATCH)
    elif review.verdict is ReviewVerdict.COMPLETE:
        return _reject(ReviewPolicyReason.DETERMINISTIC_VERDICT_MISMATCH)

    if review.verdict is not ReviewVerdict.RETRY_READ_ONLY_STEP:
        return SupervisorReviewDecision(
            admitted=True,
            reason=ReviewPolicyReason.ADMITTED,
            recommended_action=review.recommended_action,
        )

    candidate = context.recovery_candidate
    if (
        candidate is None
        or not candidate.eligible
        or len(review.failed_criteria) != 1
        or candidate.criterion is not review.failed_criteria[0]
    ):
        return _reject(ReviewPolicyReason.RECOVERY_NOT_DECLARED)
    if candidate.effect_class is not CapabilityEffectClass.READ:
        return _reject(ReviewPolicyReason.RECOVERY_NOT_READ_ONLY)
    if context.recovery_budget_remaining != 1:
        return _reject(ReviewPolicyReason.RECOVERY_BUDGET_EXHAUSTED)
    recovery = AdmittedReadRecovery(
        step_id=candidate.step_id,
        capability_id=candidate.capability_id,
        criterion=candidate.criterion,
        idempotency_key=candidate.idempotency_key,
        review_digest=review.canonical_sha256(),
        context_digest=context.canonical_sha256(),
    )
    return SupervisorReviewDecision(
        admitted=True,
        reason=ReviewPolicyReason.ADMITTED,
        recommended_action=review.recommended_action,
        recovery=recovery,
    )


__all__ = [
    "AdmittedReadRecovery",
    "DeterministicReviewState",
    "ReadRecoveryCandidate",
    "ReviewPolicyReason",
    "SUPERVISOR_REVIEW_CONTEXT_SCHEMA",
    "SUPERVISOR_REVIEW_POLICY_VERSION",
    "SupervisorReviewContext",
    "SupervisorReviewDecision",
    "admit_supervisor_review",
]
