from __future__ import annotations

import json

import pytest

from friday.orchestration.supervisor_contracts import (
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
)
from friday.orchestration.supervisor_review_transport import (
    build_supervisor_review_request,
    parse_and_admit_supervisor_review,
    supervisor_review_messages,
)
from friday.secondary_brain import EffectClass, ModelWorkload, SecondaryResult


def _context(*, complete: bool = False) -> SupervisorReviewContext:
    failed = () if complete else (CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,)
    return SupervisorReviewContext(
        plan_digest="1" * 64,
        outcome_digest="2" * 64,
        work_item_digest="3" * 64,
        work_revision=5,
        deterministic_state=(
            DeterministicReviewState.COMPLETE if complete else DeterministicReviewState.PARTIAL
        ),
        failed_criteria=failed,
        review_round=1,
        max_review_rounds=1,
        recovery_budget_remaining=0 if complete else 1,
        effect_started=False,
        publication_started=False,
        recovery_candidate=(
            None
            if complete
            else ReadRecoveryCandidate(
                step_id="s2",
                capability_id="web.search.current",
                criterion=CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
                effect_class=CapabilityEffectClass.READ,
                idempotency_key="4" * 64,
                eligible=True,
            )
        ),
    )


def _result(context: SupervisorReviewContext, *, retry: bool = True) -> SecondaryResult:
    payload = {
        "schema": "friday.supervisor-review.v1",
        "plan_digest": context.plan_digest,
        "outcome_digest": context.outcome_digest,
        "verdict": "retry_read_only_step" if retry else "complete",
        "failed_criteria": [item.value for item in context.failed_criteria],
        "recommended_action": "request_read_only_recovery" if retry else "publish",
        "reason_code": "bounded_review",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return SecondaryResult(visible_content=raw, structured_output=payload)


def test_request_is_body_free_effect_free_and_bounded() -> None:
    context = _context()
    request = build_supervisor_review_request(
        context,
        absolute_deadline_monotonic=1234.5,
    )
    assert request.workload is ModelWorkload.PLAN_CANDIDATE
    assert request.effect_class is EffectClass.NONE
    assert request.require_independent_model is True
    assert request.contains_private_text is False
    assert request.max_output_tokens == 256
    serialized = json.dumps(request.messages, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) < 3_500
    assert 'tools_allowed":false' in request.messages[1]["content"]
    assert 'publication_allowed":false' in request.messages[1]["content"]


def test_parsed_review_maps_only_to_code_declared_recovery() -> None:
    context = _context()
    admitted = parse_and_admit_supervisor_review(_result(context), context)
    assert isinstance(admitted.review, SupervisorReview)
    assert admitted.decision.admitted is True
    assert admitted.decision.reason is ReviewPolicyReason.ADMITTED
    assert admitted.decision.recovery is not None
    assert admitted.decision.recovery.step_id == "s2"
    assert admitted.decision.recovery.idempotency_key == "4" * 64
    assert admitted.context_sha256 == context.canonical_sha256()


def test_complete_review_can_only_confirm_deterministic_completion() -> None:
    context = _context(complete=True)
    admitted = parse_and_admit_supervisor_review(_result(context, retry=False), context)
    assert admitted.decision.admitted is True
    assert admitted.decision.recovery is None


def test_raw_structured_mismatch_and_duplicate_keys_are_rejected() -> None:
    context = _context()
    result = _result(context)
    assert isinstance(result.structured_output, dict)
    changed = dict(result.structured_output)
    changed["reason_code"] = "different"
    with pytest.raises(SupervisorContractError, match="differ"):
        parse_and_admit_supervisor_review(
            SecondaryResult(visible_content=result.visible_content, structured_output=changed),
            context,
        )
    duplicate = result.visible_content.replace(
        '"verdict":"retry_read_only_step"',
        '"verdict":"retry_read_only_step","verdict":"complete"',
    )
    with pytest.raises(SupervisorContractError, match="duplicate"):
        parse_and_admit_supervisor_review(
            SecondaryResult(visible_content=duplicate, structured_output=result.structured_output),
            context,
        )


def test_context_digest_drift_is_rejected_by_policy() -> None:
    original = _context()
    changed = SupervisorReviewContext(
        plan_digest=original.plan_digest,
        outcome_digest="9" * 64,
        work_item_digest=original.work_item_digest,
        work_revision=original.work_revision + 1,
        deterministic_state=original.deterministic_state,
        failed_criteria=original.failed_criteria,
        review_round=1,
        max_review_rounds=1,
        recovery_budget_remaining=1,
        effect_started=False,
        publication_started=False,
        recovery_candidate=original.recovery_candidate,
    )
    decision = parse_and_admit_supervisor_review(_result(original), changed).decision
    assert decision.admitted is False
    assert decision.reason is ReviewPolicyReason.DIGEST_MISMATCH


def test_messages_reject_non_context_and_deadline_rejects_nonfinite() -> None:
    with pytest.raises(TypeError):
        supervisor_review_messages(object())  # type: ignore[arg-type]
    with pytest.raises(SupervisorContractError, match="deadline"):
        build_supervisor_review_request(_context(), absolute_deadline_monotonic=float("nan"))
