"""Pure Policy Kernel: untrusted proposal in, admitted plan or a closed rejection."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    manifest_matches_snapshot,
    operational_capability_snapshot,
)
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
    WEB_SEARCH_CURRENT_ID,
    CapabilityAvailability,
    CapabilityEffectClass,
    CapabilityManifest,
    CompletionCriterion,
    ContinuationDecision,
    ExpectedOutcome,
    ReviewMode,
    StepKind,
    SupervisorContractError,
    SupervisorInput,
    SupervisorProposal,
    SupervisorStep,
    TaskClass,
    canonical_sha256,
    parse_supervisor_goal,
    parse_supervisor_purpose,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityAttestor,
    PlanAuthorityBoundary,
    PlanAuthorityDecision,
    PlanAuthorityReason,
    PlanAuthorityScope,
    PlanSourceBinding,
    PlanSourceKind,
    source_bindings_sha256,
)


class PolicyReason(StrEnum):
    ADMITTED = "admitted"
    STALE_MANIFEST = "stale_manifest"
    REGISTRY_DRIFT = "registry_drift"
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
    PARTIAL_CAPABILITY = "partial_capability"
    EXPECTED_OUTCOME_MISMATCH = "expected_outcome_mismatch"
    DEPENDENCY_SHAPE_MISMATCH = "dependency_shape_mismatch"
    COMPLETION_CRITERIA_MISMATCH = "completion_criteria_mismatch"
    REVIEW_NOT_ADMITTED = "review_not_admitted"
    CONTROL_TEXT_NOT_ADMITTED = "control_text_not_admitted"
    BUDGET_DRIFT = "budget_drift"
    DEADLINE_EXPIRED = "deadline_expired"
    AUTHORITY_DENIED = "authority_denied"
    AUTHORITY_STALE = "authority_stale"
    SOURCE_DRIFT = "source_drift"


_EXPECTED_OUTCOME_BY_TARGET = {
    FILE_CURRENT_READ_ID: ExpectedOutcome.COMPLETE_SOURCE_EVIDENCE,
    WEB_SEARCH_CURRENT_ID: ExpectedOutcome.VERIFIED_CURRENT_SOURCES,
    ARCHIVE_SEARCH_ID: ExpectedOutcome.ARCHIVE_EVIDENCE,
    CONVERSATION_WINDOW_READ_ID: ExpectedOutcome.CONVERSATION_WINDOW,
    PRIMARY_SYNTHESIS_ID: ExpectedOutcome.CITED_COMPARISON,
}
_REQUIRED_TARGETS_BY_TASK = {
    TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB: {
        FILE_CURRENT_READ_ID,
        WEB_SEARCH_CURRENT_ID,
    },
    TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB: {
        ARCHIVE_SEARCH_ID,
        WEB_SEARCH_CURRENT_ID,
    },
}
_REQUIRED_CRITERIA_BY_TASK = {
    TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB: {
        CompletionCriterion.CURRENT_ATTACHMENT_EVIDENCE_PRESENT,
        CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
        CompletionCriterion.MATERIAL_DIFFERENCES_SOURCE_BOUND,
    },
    TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB: {
        CompletionCriterion.ARCHIVE_EVIDENCE_PRESENT,
        CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
        CompletionCriterion.MATERIAL_DIFFERENCES_SOURCE_BOUND,
    },
}


@dataclass(frozen=True, slots=True)
class PolicyAdmissionContext:
    actor_binding_sha256: str
    conversation_binding_sha256: str
    authority_scope: PlanAuthorityScope
    source_bindings: tuple[PlanSourceBinding, ...]
    turn_deadline_monotonic_ns: int
    authority_attestor: PlanAuthorityAttestor = field(repr=False, compare=False)
    confirmation_present: bool = False
    capability_bindings: CapabilityBindingSnapshot = field(
        default_factory=operational_capability_snapshot,
        repr=False,
        compare=False,
    )

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
        if not isinstance(self.capability_bindings, CapabilityBindingSnapshot):
            raise ExecutionPlanError("capability bindings must be a code-owned snapshot")
        if type(self.authority_scope) is not PlanAuthorityScope:
            raise ExecutionPlanError("authority scope must be code-owned")
        try:
            source_bindings_sha256(self.source_bindings)
        except (TypeError, ValueError) as exc:
            raise ExecutionPlanError("source bindings must be code-owned") from exc
        if (
            type(self.turn_deadline_monotonic_ns) is not int
            or self.turn_deadline_monotonic_ns <= 0
            or not callable(self.authority_attestor)
        ):
            raise ExecutionPlanError("deadline and authority attestor must be code-owned")


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


def _control_text_is_admitted(proposal: SupervisorProposal) -> bool:
    """Recheck typed objects so callers cannot bypass the closed JSON parser."""

    try:
        parse_supervisor_goal(proposal.goal)
        for step in proposal.steps:
            parse_supervisor_purpose(step.purpose)
    except (AttributeError, SupervisorContractError, TypeError):
        return False
    return True


def _attachment_ordinals(supervisor_input: SupervisorInput) -> set[int]:
    return {item.ordinal for item in supervisor_input.turn.attachments}


def _readable_attachment_ordinals(supervisor_input: SupervisorInput) -> set[int]:
    return {item.ordinal for item in supervisor_input.turn.attachments if item.text_available}


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
        if step.expected_outcome is not _EXPECTED_OUTCOME_BY_TARGET[PRIMARY_SYNTHESIS_ID]:
            return PolicyReason.EXPECTED_OUTCOME_MISMATCH
        return None
    capability = capabilities.get(step.target_id)
    if capability is None:
        return PolicyReason.UNKNOWN_CAPABILITY
    if capability.availability is CapabilityAvailability.UNAVAILABLE:
        return PolicyReason.UNAVAILABLE_CAPABILITY
    if capability.availability is CapabilityAvailability.PARTIAL:
        return PolicyReason.PARTIAL_CAPABILITY
    if capability.effect_class is not CapabilityEffectClass.READ:
        return PolicyReason.EFFECT_NOT_ADMITTED
    expected = _EXPECTED_OUTCOME_BY_TARGET.get(step.target_id)
    if expected is None or step.expected_outcome is not expected:
        return PolicyReason.EXPECTED_OUTCOME_MISMATCH
    if step.target_id == FILE_CURRENT_READ_ID:
        ordinal = step.input.get("attachment_ordinal")
        if (
            not isinstance(ordinal, int)
            or ordinal not in _attachment_ordinals(supervisor_input)
            or ordinal not in _readable_attachment_ordinals(supervisor_input)
        ):
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
    capability_steps = tuple(step for step in proposal.steps if step.kind is StepKind.CAPABILITY)
    model_steps = tuple(step for step in proposal.steps if step.kind is StepKind.MODEL)
    capability_targets = [step.target_id for step in capability_steps]
    if len(model_steps) != 1 or model_steps[0].target_id != PRIMARY_SYNTHESIS_ID:
        return PolicyReason.MISSING_SYNTHESIS
    required_targets = _REQUIRED_TARGETS_BY_TASK.get(proposal.task_class)
    if required_targets is None:
        return PolicyReason.TASK_CLASS_MISMATCH
    if len(capability_targets) != len(required_targets) or set(capability_targets) != required_targets:
        return PolicyReason.UNSUPPORTED_COMBINATION
    if proposal.review_mode is not ReviewMode.NONE:
        return PolicyReason.REVIEW_NOT_ADMITTED
    if proposal.continuation_decision is not ContinuationDecision.NEW_TASK:
        return PolicyReason.CONTINUATION_NOT_ALLOWED
    if proposal.task_class is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
        if not supervisor_input.turn.attachments or "web" not in supervisor_input.available_evidence:
            return PolicyReason.TASK_CLASS_MISMATCH
    elif proposal.task_class is TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB and (
        "archive" not in supervisor_input.available_evidence
        or "web" not in supervisor_input.available_evidence
    ):
        return PolicyReason.TASK_CLASS_MISMATCH
    capability_step_ids = {step.step_id for step in capability_steps}
    if any(step.depends_on for step in capability_steps):
        return PolicyReason.DEPENDENCY_SHAPE_MISMATCH
    synthesis = model_steps[0]
    if synthesis.parallel_group is not None or set(synthesis.depends_on) != capability_step_ids:
        return PolicyReason.DEPENDENCY_SHAPE_MISMATCH
    if set(proposal.completion_criteria) != _REQUIRED_CRITERIA_BY_TASK[proposal.task_class]:
        return PolicyReason.COMPLETION_CRITERIA_MISMATCH
    if proposal.continuation_decision not in proposal_allowed_actions(supervisor_input):
        return PolicyReason.CONTINUATION_NOT_ALLOWED
    return None


def proposal_allowed_actions(supervisor_input: SupervisorInput) -> set[ContinuationDecision]:
    return set(supervisor_input.continuation.allowed_actions)


def _code_owned_task_class(supervisor_input: SupervisorInput) -> TaskClass:
    evidence = set(supervisor_input.available_evidence)
    if supervisor_input.turn.attachments and {"current_attachment", "web"} <= evidence:
        return TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    if not supervisor_input.turn.attachments and {"archive", "web"} <= evidence:
        return TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB
    return TaskClass.UNKNOWN


def _parallel_read_groups(proposal: SupervisorProposal) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in proposal.steps:
        if step.kind is StepKind.CAPABILITY and step.parallel_group:
            counts[step.parallel_group] = counts.get(step.parallel_group, 0) + 1
    return counts


def _budget_matches_policy(supervisor_input: SupervisorInput, identity: object) -> bool:
    budgets = supervisor_input.budgets
    return bool(
        budgets.max_steps == getattr(identity, "max_steps", None)
        and budgets.max_parallel_reads == getattr(identity, "max_parallel_reads", None)
        and budgets.turn_deadline_ms == getattr(identity, "turn_deadline_ms", None)
        and budgets.per_step_deadline_ms == getattr(identity, "per_step_deadline_ms", None)
        and budgets.max_supervisor_calls == getattr(identity, "max_supervisor_calls", None)
        and budgets.max_model_calls == getattr(identity, "max_model_calls", None)
        and budgets.max_tool_calls == getattr(identity, "max_tool_calls", None)
        and budgets.max_capability_calls == getattr(identity, "max_capability_calls", None)
        and budgets.max_review_rounds == getattr(identity, "max_review_rounds", None)
        and budgets.max_recovery_rounds == getattr(identity, "max_recovery_rounds", None)
        and budgets.max_output_tokens == getattr(identity, "max_output_tokens", None)
    )


def _source_scope_is_admitted(
    context: PolicyAdmissionContext,
    *,
    max_review_rounds: int,
) -> bool:
    if max_review_rounds == 1:
        return bool(
            context.authority_scope is PlanAuthorityScope.ASSIST_EXECUTION
            and len(context.source_bindings) == 1
            and context.source_bindings[0].kind is PlanSourceKind.CURRENT_RAW_OBJECT
        )
    return bool(
        context.authority_scope is PlanAuthorityScope.SHADOW_ONLY
        and len(context.source_bindings) == 1
        and context.source_bindings[0].kind is PlanSourceKind.SHADOW_PROJECTION
    )


def admit_supervisor_proposal(
    proposal: SupervisorProposal,
    supervisor_input: SupervisorInput,
    context: PolicyAdmissionContext,
) -> PolicyDecision:
    """Validate one untrusted proposal against the current projection and policy."""

    policy_identity = semantic_supervisor_policy.supervisor_product_policy_identity_for_review_rounds(
        supervisor_input.budgets.max_review_rounds
    )
    if policy_identity is None:
        return _reject(PolicyReason.REVIEW_NOT_ADMITTED)
    if not _budget_matches_policy(supervisor_input, policy_identity):
        return _reject(PolicyReason.BUDGET_DRIFT)
    if proposal.budget_sha256 != supervisor_input.budgets.canonical_sha256():
        return _reject(PolicyReason.BUDGET_DRIFT)
    now_ns = time.monotonic_ns()
    if (
        context.turn_deadline_monotonic_ns <= now_ns
        or context.turn_deadline_monotonic_ns - now_ns > supervisor_input.budgets.turn_deadline_ms * 1_000_000
    ):
        return _reject(PolicyReason.DEADLINE_EXPIRED)
    if not _source_scope_is_admitted(
        context,
        max_review_rounds=supervisor_input.budgets.max_review_rounds,
    ):
        return _reject(PolicyReason.SOURCE_DRIFT)
    if proposal.task_class.value not in policy_identity.admitted_tasks:
        return _reject(PolicyReason.TASK_CLASS_MISMATCH)
    if proposal.manifest_id != supervisor_input.manifest.manifest_id:
        return _reject(PolicyReason.STALE_MANIFEST)
    current_bindings = operational_capability_snapshot()
    if (
        current_bindings.digest_hex() != context.capability_bindings.digest_hex()
        or not manifest_matches_snapshot(supervisor_input.manifest, context.capability_bindings)
    ):
        return _reject(PolicyReason.REGISTRY_DRIFT)
    if not _control_text_is_admitted(proposal):
        return _reject(PolicyReason.CONTROL_TEXT_NOT_ADMITTED)
    if proposal.task_class is not _code_owned_task_class(supervisor_input):
        return _reject(PolicyReason.TASK_CLASS_MISMATCH)
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

    required_security_ids = tuple(
        sorted(
            {
                binding.security_id
                for step in proposal.steps
                if step.kind is StepKind.CAPABILITY
                for binding in (context.capability_bindings.binding_for(step.target_id),)
                if binding is not None and binding.security_id
            }
        )
    )
    authority_boundary = PlanAuthorityBoundary(
        scope=context.authority_scope,
        actor_binding_sha256=context.actor_binding_sha256,
        conversation_binding_sha256=context.conversation_binding_sha256,
        proposal_sha256=proposal.canonical_sha256(),
        manifest_sha256=supervisor_input.manifest.digest_hex(),
        policy_sha256=policy_identity.policy_sha256,
        source_bindings_sha256=source_bindings_sha256(context.source_bindings),
        capability_bindings_sha256=context.capability_bindings.digest_hex(),
        budget_sha256=supervisor_input.budgets.canonical_sha256(),
        required_security_ids=required_security_ids,
        turn_deadline_monotonic_ns=context.turn_deadline_monotonic_ns,
    )
    try:
        authority = context.authority_attestor(authority_boundary)
    except Exception:
        return _reject(PolicyReason.AUTHORITY_DENIED)
    if type(authority) is not PlanAuthorityDecision:
        return _reject(PolicyReason.AUTHORITY_DENIED)
    if authority.reason is PlanAuthorityReason.SOURCE_DRIFT:
        return _reject(PolicyReason.SOURCE_DRIFT)
    if authority.reason is PlanAuthorityReason.STALE:
        return _reject(PolicyReason.AUTHORITY_STALE)
    if authority.reason is not PlanAuthorityReason.ADMITTED or authority.attestation is None:
        return _reject(PolicyReason.AUTHORITY_DENIED)
    now_ns = time.monotonic_ns()
    if not authority.attestation.is_fresh_for(authority_boundary, now_ns=now_ns):
        return _reject(PolicyReason.AUTHORITY_STALE)

    admitted_step_items: list[ValidatedStep] = []
    for step in proposal.steps:
        binding = (
            context.capability_bindings.binding_for(step.target_id)
            if step.kind is StepKind.CAPABILITY
            else None
        )
        if step.kind is StepKind.CAPABILITY and (binding is None or not binding.available):
            return _reject(PolicyReason.REGISTRY_DRIFT)
        security_id = binding.security_id if binding is not None else None
        tool_id = binding.tool_id if binding is not None else None
        adapter_id = binding.adapter_id if binding is not None else None
        effect_class = binding.effect_class if binding is not None else CapabilityEffectClass.READ
        admitted_step_items.append(
            ValidatedStep(
                step_id=step.step_id,
                capability_id=step.target_id,
                effect_class=effect_class,
                resolved_security_id=security_id,
                resolved_tool_id=tool_id,
                resolved_adapter_id=adapter_id,
                depends_on=step.depends_on,
                parallel_group=step.parallel_group,
                input=step.input,
                idempotency_key=canonical_sha256(
                    {
                        "step_id": step.step_id,
                        "target_id": step.target_id,
                        "input": dict(step.input),
                        "manifest_id": supervisor_input.manifest.digest_hex(),
                        "binding_snapshot_sha256": context.capability_bindings.digest_hex(),
                        "security_id": security_id,
                        "tool_id": tool_id,
                        "adapter_id": adapter_id,
                        "actor_binding_sha256": context.actor_binding_sha256,
                        "authority_binding_sha256": authority_boundary.durable_binding_sha256(),
                        "source_bindings_sha256": authority_boundary.source_bindings_sha256,
                        "budget_sha256": authority_boundary.budget_sha256,
                    }
                ),
                deadline_ms=supervisor_input.budgets.per_step_deadline_ms,
                max_calls=(
                    semantic_supervisor_policy.SUPERVISOR_PRIMARY_MODEL_CALLS
                    if binding is None
                    else 1 + supervisor_input.budgets.max_recovery_rounds
                    if (
                        step.target_id == WEB_SEARCH_CURRENT_ID
                        and supervisor_input.budgets.max_recovery_rounds == 1
                    )
                    else 1
                ),
                max_output_tokens=(
                    semantic_supervisor_policy.SUPERVISOR_PRIMARY_OUTPUT_TOKENS if binding is None else 0
                ),
            )
        )
    admitted_steps = tuple(admitted_step_items)
    plan = plan_from_admitted_proposal(
        proposal,
        manifest_digest=supervisor_input.manifest.digest_hex(),
        binding_snapshot_sha256=context.capability_bindings.digest_hex(),
        policy_version=policy_identity.policy_id,
        policy_sha256=policy_identity.policy_sha256,
        actor_binding_sha256=context.actor_binding_sha256,
        conversation_binding_sha256=context.conversation_binding_sha256,
        authority_scope=context.authority_scope,
        authority_binding_sha256=authority_boundary.durable_binding_sha256(),
        required_security_ids=required_security_ids,
        source_bindings=context.source_bindings,
        budgets=supervisor_input.budgets,
        steps=admitted_steps,
        seal=mint_admission_seal(),
    )
    if not authority.attestation.is_fresh_for(authority_boundary, now_ns=time.monotonic_ns()):
        return _reject(PolicyReason.AUTHORITY_STALE)
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
