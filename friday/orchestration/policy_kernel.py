"""Pure Policy Kernel: untrusted proposal in, admitted plan or a closed rejection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.execution_plan import (
    ExecutionPlanError,
    ValidatedExecutionPlan,
    ValidatedStep,
    mint_admission_seal,
    plan_from_admitted_proposal,
)
from friday.orchestration.supervisor_contracts import (
    ARCHIVE_SEARCH_ID,
    CONVERSATION_WINDOW_READ_ID,
    FILE_CURRENT_READ_ID,
    PRIMARY_SYNTHESIS_ID,
    SECONDARY_SUPERVISOR_ID,
    SUPERVISOR_PRODUCT_POLICY_ID,
    WEB_SEARCH_CURRENT_ID,
    CapabilityAvailability,
    CapabilityEffectClass,
    CapabilityManifest,
    ContinuationDecision,
    StepKind,
    SupervisorInput,
    SupervisorProposal,
    SupervisorStep,
    TaskClass,
    canonical_sha256,
)


class PolicyReason(StrEnum):
    ADMITTED = "admitted"
    STALE_MANIFEST = "stale_manifest"
    UNKNOWN_CAPABILITY = "unknown_capability"
    UNAVAILABLE_CAPABILITY = "unavailable_capability"
    EFFECT_NOT_ADMITTED = "effect_not_admitted"
    KIND_TARGET_MISMATCH = "kind_target_mismatch"
    INPUT_NOT_IN_PROJECTION = "input_not_in_projection"
    UNSUPPORTED_COMBINATION = "unsupported_combination"
    PARALLELISM_EXCEEDED = "parallelism_exceeded"
    STEP_BUDGET_EXCEEDED = "step_budget_exceeded"
    CONTINUATION_NOT_ALLOWED = "continuation_not_allowed"
    MISSING_SYNTHESIS = "missing_synthesis"
    SELF_APPROVAL = "self_approval"
    TASK_CLASS_MISMATCH = "task_class_mismatch"


@dataclass(frozen=True, slots=True)
class PolicyAdmissionContext:
    actor_binding_sha256: str
    conversation_binding_sha256: str
    confirmation_present: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("actor_binding_sha256", self.actor_binding_sha256),
            ("conversation_binding_sha256", self.conversation_binding_sha256),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ExecutionPlanError(f"{label} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    admitted: bool
    reason: PolicyReason
    plan: ValidatedExecutionPlan | None = None

    @property
    def reason_code(self) -> str:
        return self.reason.value


def _reject(reason: PolicyReason) -> PolicyDecision:
    return PolicyDecision(admitted=False, reason=reason)


def _attachment_ordinals(supervisor_input: SupervisorInput) -> set[int]:
    return {item.ordinal for item in supervisor_input.turn.attachments}


def _validate_step_against_manifest(
    step: SupervisorStep,
    manifest: CapabilityManifest,
    supervisor_input: SupervisorInput,
) -> PolicyReason | None:
    capabilities = manifest.capability_by_id()
    roles = manifest.role_by_id()
    if step.kind is StepKind.MODEL:
        role = roles.get(step.target_id)
        if role is None:
            return PolicyReason.UNKNOWN_CAPABILITY
        if role.availability is not CapabilityAvailability.AVAILABLE:
            return PolicyReason.UNAVAILABLE_CAPABILITY
        if step.target_id == SECONDARY_SUPERVISOR_ID:
            return PolicyReason.SELF_APPROVAL
        if step.target_id != PRIMARY_SYNTHESIS_ID:
            return PolicyReason.KIND_TARGET_MISMATCH
        return None
    capability = capabilities.get(step.target_id)
    if capability is None:
        return PolicyReason.UNKNOWN_CAPABILITY
    if capability.availability is CapabilityAvailability.UNAVAILABLE:
        return PolicyReason.UNAVAILABLE_CAPABILITY
    if capability.effect_class is not CapabilityEffectClass.READ:
        return PolicyReason.EFFECT_NOT_ADMITTED
    if step.target_id == FILE_CURRENT_READ_ID:
        ordinal = step.input.get("attachment_ordinal")
        if not isinstance(ordinal, int) or ordinal not in _attachment_ordinals(supervisor_input):
            return PolicyReason.INPUT_NOT_IN_PROJECTION
        if "current_attachment" not in supervisor_input.available_evidence:
            return PolicyReason.INPUT_NOT_IN_PROJECTION
    elif step.target_id == WEB_SEARCH_CURRENT_ID:
        if "web" not in supervisor_input.available_evidence:
            return PolicyReason.INPUT_NOT_IN_PROJECTION
    elif step.target_id == ARCHIVE_SEARCH_ID:
        if "archive" not in supervisor_input.available_evidence:
            return PolicyReason.INPUT_NOT_IN_PROJECTION
    elif step.target_id == CONVERSATION_WINDOW_READ_ID:
        if "conversation_window" not in supervisor_input.available_evidence:
            return PolicyReason.INPUT_NOT_IN_PROJECTION
    else:
        return PolicyReason.UNKNOWN_CAPABILITY
    return None


def _validate_task_shape(
    proposal: SupervisorProposal, supervisor_input: SupervisorInput
) -> PolicyReason | None:
    capability_targets = {step.target_id for step in proposal.steps if step.kind is StepKind.CAPABILITY}
    model_targets = {step.target_id for step in proposal.steps if step.kind is StepKind.MODEL}
    if PRIMARY_SYNTHESIS_ID not in model_targets:
        return PolicyReason.MISSING_SYNTHESIS
    if proposal.task_class is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
        if FILE_CURRENT_READ_ID not in capability_targets or WEB_SEARCH_CURRENT_ID not in capability_targets:
            return PolicyReason.TASK_CLASS_MISMATCH
        if not supervisor_input.turn.attachments or "web" not in supervisor_input.available_evidence:
            return PolicyReason.TASK_CLASS_MISMATCH
    elif proposal.task_class is TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB:
        if ARCHIVE_SEARCH_ID not in capability_targets or WEB_SEARCH_CURRENT_ID not in capability_targets:
            return PolicyReason.TASK_CLASS_MISMATCH
        if (
            "archive" not in supervisor_input.available_evidence
            or "web" not in supervisor_input.available_evidence
        ):
            return PolicyReason.TASK_CLASS_MISMATCH
    elif proposal.task_class is TaskClass.ORDINARY_DIALOGUE:
        if capability_targets - {CONVERSATION_WINDOW_READ_ID}:
            return PolicyReason.UNSUPPORTED_COMBINATION
    if proposal.continuation_decision not in proposal_allowed_actions(supervisor_input):
        return PolicyReason.CONTINUATION_NOT_ALLOWED
    return None


def proposal_allowed_actions(supervisor_input: SupervisorInput) -> set[ContinuationDecision]:
    return set(supervisor_input.continuation.allowed_actions)


def _parallel_read_groups(proposal: SupervisorProposal) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in proposal.steps:
        if step.kind is StepKind.CAPABILITY and step.parallel_group:
            counts[step.parallel_group] = counts.get(step.parallel_group, 0) + 1
    return counts


def admit_supervisor_proposal(
    proposal: SupervisorProposal,
    supervisor_input: SupervisorInput,
    context: PolicyAdmissionContext,
) -> PolicyDecision:
    """Validate one untrusted proposal against the current projection and policy."""

    if proposal.manifest_id != supervisor_input.manifest.manifest_id:
        return _reject(PolicyReason.STALE_MANIFEST)
    if len(proposal.steps) > supervisor_input.budgets.max_steps:
        return _reject(PolicyReason.STEP_BUDGET_EXCEEDED)
    for count in _parallel_read_groups(proposal).values():
        if count > supervisor_input.budgets.max_parallel_reads:
            return _reject(PolicyReason.PARALLELISM_EXCEEDED)
    for step in proposal.steps:
        reason = _validate_step_against_manifest(step, supervisor_input.manifest, supervisor_input)
        if reason is not None:
            return _reject(reason)
    shape_reason = _validate_task_shape(proposal, supervisor_input)
    if shape_reason is not None:
        return _reject(shape_reason)

    admitted_steps = tuple(
        ValidatedStep(
            step_id=step.step_id,
            capability_id=step.target_id,
            effect_class=(
                CapabilityEffectClass.READ if step.kind is StepKind.CAPABILITY else CapabilityEffectClass.READ
            ),
            depends_on=step.depends_on,
            parallel_group=step.parallel_group,
            input=step.input,
            idempotency_key=canonical_sha256(
                {
                    "step_id": step.step_id,
                    "target_id": step.target_id,
                    "input": dict(step.input),
                    "manifest_id": supervisor_input.manifest.digest_hex(),
                    "actor_binding_sha256": context.actor_binding_sha256,
                }
            ),
        )
        for step in proposal.steps
    )
    plan = plan_from_admitted_proposal(
        proposal,
        manifest_digest=supervisor_input.manifest.digest_hex(),
        policy_version=SUPERVISOR_PRODUCT_POLICY_ID,
        actor_binding_sha256=context.actor_binding_sha256,
        conversation_binding_sha256=context.conversation_binding_sha256,
        steps=admitted_steps,
        seal=mint_admission_seal(),
    )
    return PolicyDecision(admitted=True, reason=PolicyReason.ADMITTED, plan=plan)


def risk_hints_cannot_downgrade_effect(
    proposal: SupervisorProposal,
    supervisor_input: SupervisorInput,
) -> Mapping[str, Any]:
    """Return the code-owned effect projection.  Model risk_hints are ignored."""

    effects = []
    for step in proposal.steps:
        if step.kind is StepKind.CAPABILITY:
            capability = supervisor_input.manifest.capability_by_id().get(step.target_id)
            if capability is not None:
                effects.append(capability.effect_class.value)
    return {
        "model_risk_hints": [item.value for item in proposal.risk_hints],
        "code_owned_effects": effects,
        "hints_are_advisory_only": True,
    }
