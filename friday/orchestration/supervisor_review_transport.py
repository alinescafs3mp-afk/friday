"""Body-free transport for one bounded semantic review call.

The transport deliberately receives only the deterministic review context.  It
has no evidence body, tool, storage, publication, or recovery executor.  The
secondary result remains untrusted until ``admit_supervisor_review`` maps it to
the one code-declared recovery candidate, if any.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from friday.model_input_hygiene import secondary_model_messages_are_secret_free
from friday.orchestration.supervisor_contracts import (
    SUPERVISOR_PRODUCT_POLICY_ID,
    SUPERVISOR_PRODUCT_POLICY_SHA256,
    SUPERVISOR_REVIEW_SCHEMA,
    CompletionCriterion,
    ReviewRecommendedAction,
    ReviewVerdict,
    SupervisorContractError,
    SupervisorReview,
    canonical_dumps,
)
from friday.orchestration.supervisor_review_policy import (
    SUPERVISOR_REVIEW_POLICY_VERSION,
    SupervisorReviewContext,
    SupervisorReviewDecision,
    admit_supervisor_review,
)
from friday.secondary_brain import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryResult,
)

SUPERVISOR_REVIEW_INPUT_SCHEMA = "friday.supervisor-review-input.v1"

# Same exact accepted 4K-profile input allowance used by proposal planning:
# 4096 total - 512 output - 256 adapter reserve.  The review output is smaller,
# but retaining the stricter shared allowance makes endpoint drift observable.
_MAX_INPUT_UTF8_BYTES = 3_328
_MAX_OUTPUT_TOKENS = 256
_SYSTEM_PROMPT = """\
Return exactly one JSON object matching response_schema and no prose. The input
contains code-owned, body-free deterministic results. Do not invent evidence,
steps, capabilities, authority, effects, tools, identifiers, or permission.
Assess only whether the named completion criteria failed. A recovery verdict is
advisory and can refer only to the single code-declared recovery availability.
"""


@dataclass(frozen=True, slots=True)
class AdmittedSupervisorReview:
    """One parsed review plus its deterministic, non-executing policy result."""

    review: SupervisorReview
    decision: SupervisorReviewDecision
    context_sha256: str


def _review_response_schema(context: SupervisorReviewContext) -> dict[str, Any]:
    """Return a closed grammar; policy admission remains the final authority."""

    failed = [item.value for item in context.failed_criteria]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "plan_digest",
            "outcome_digest",
            "verdict",
            "failed_criteria",
            "recommended_action",
            "reason_code",
        ],
        "properties": {
            "schema": {"type": "string", "enum": [SUPERVISOR_REVIEW_SCHEMA]},
            "plan_digest": {"type": "string", "enum": [context.plan_digest]},
            "outcome_digest": {"type": "string", "enum": [context.outcome_digest]},
            "verdict": {
                "type": "string",
                "enum": [item.value for item in ReviewVerdict],
            },
            "failed_criteria": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [item.value for item in CompletionCriterion],
                },
                "uniqueItems": True,
                "maxItems": 4,
            },
            "recommended_action": {
                "type": "string",
                "enum": [
                    item.value
                    for item in ReviewRecommendedAction
                    if item is not ReviewRecommendedAction.SKIP_REVIEW
                ],
            },
            "reason_code": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_.-]{0,63}$",
            },
        },
        # This is guidance for constrained decoding, not a grant. The parser
        # and policy require exact equality again after transport.
        "x-code-owned-failed-criteria": failed,
    }


def supervisor_review_messages(
    context: SupervisorReviewContext,
) -> tuple[dict[str, str], ...]:
    if not isinstance(context, SupervisorReviewContext):
        raise TypeError("review transport requires SupervisorReviewContext")
    candidate = context.recovery_candidate
    payload = {
        "schema": SUPERVISOR_REVIEW_INPUT_SCHEMA,
        "trusted_policy": {
            "product_policy_id": SUPERVISOR_PRODUCT_POLICY_ID,
            "product_policy_sha256": SUPERVISOR_PRODUCT_POLICY_SHA256,
            "review_policy_version": SUPERVISOR_REVIEW_POLICY_VERSION,
            "tools_allowed": False,
            "effects_allowed": False,
            "publication_allowed": False,
            "max_review_rounds": 1,
            "max_recovery_steps": 1,
        },
        "deterministic_context": context.payload(),
        "recovery_available": bool(candidate is not None and candidate.eligible),
        "response_schema": _review_response_schema(context),
    }
    messages = (
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": canonical_dumps(payload)},
    )
    size = sum(len(item["content"].encode("utf-8", errors="strict")) for item in messages)
    if size > _MAX_INPUT_UTF8_BYTES:
        raise SupervisorContractError("supervisor review input exceeds its body-free budget")
    if not secondary_model_messages_are_secret_free(messages):
        raise SupervisorContractError("supervisor review input contains secret material")
    return messages


def build_supervisor_review_request(
    context: SupervisorReviewContext,
    *,
    absolute_deadline_monotonic: float,
) -> ModelRequest:
    if (
        isinstance(absolute_deadline_monotonic, bool)
        or not isinstance(absolute_deadline_monotonic, int | float)
        or not math.isfinite(float(absolute_deadline_monotonic))
    ):
        raise SupervisorContractError("supervisor review deadline is invalid")
    return ModelRequest(
        workload=ModelWorkload.PLAN_CANDIDATE,
        messages=supervisor_review_messages(context),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        absolute_deadline_monotonic=float(absolute_deadline_monotonic),
        priority=ModelPriority.BACKGROUND,
        effect_class=EffectClass.NONE,
        modality=ModelModality.TEXT,
        require_structured_output=True,
        structured_output_schema=_review_response_schema(context),
        require_independent_model=True,
        contains_private_text=False,
    )


def parse_and_admit_supervisor_review(
    result: SecondaryResult,
    context: SupervisorReviewContext,
) -> AdmittedSupervisorReview:
    """Reparse exact visible JSON, prove transport parity, then apply policy."""

    if not isinstance(result, SecondaryResult):
        raise TypeError("review result must be SecondaryResult")
    if not isinstance(result.structured_output, Mapping):
        raise SupervisorContractError("supervisor review must be one structured object")
    review = SupervisorReview.parse(result.visible_content)
    structured = SupervisorReview.parse(result.structured_output)
    if structured.canonical_sha256() != review.canonical_sha256():
        raise SupervisorContractError("supervisor review raw and structured bodies differ")
    decision = admit_supervisor_review(review, context)
    return AdmittedSupervisorReview(
        review=review,
        decision=decision,
        context_sha256=context.canonical_sha256(),
    )


__all__ = [
    "AdmittedSupervisorReview",
    "SUPERVISOR_REVIEW_INPUT_SCHEMA",
    "build_supervisor_review_request",
    "parse_and_admit_supervisor_review",
    "supervisor_review_messages",
]
