"""Code-owned validated execution plan.  Model output cannot construct it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from friday.orchestration.supervisor_contracts import (
    CapabilityEffectClass,
    SupervisorBudgets,
    SupervisorContractError,
    SupervisorProposal,
    canonical_sha256,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityScope,
    PlanSourceBinding,
    durable_authority_binding_sha256,
    source_bindings_sha256,
)

VALIDATED_EXECUTION_PLAN_SCHEMA = "friday.validated-execution-plan.v1"


class ExecutionPlanError(SupervisorContractError):
    """A validated plan cannot be built from untrusted model output."""


@dataclass(frozen=True, slots=True)
class _AdmissionSeal:
    """Private constructor token.  Only Policy Kernel may mint this object."""

    token: str


@dataclass(frozen=True, slots=True)
class ValidatedStep:
    step_id: str
    capability_id: str
    effect_class: CapabilityEffectClass
    resolved_security_id: str | None
    resolved_tool_id: str | None
    resolved_adapter_id: str | None
    depends_on: tuple[str, ...]
    parallel_group: str | None
    input: Mapping[str, Any]
    idempotency_key: str
    deadline_ms: int
    max_calls: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        identities = (
            self.resolved_security_id,
            self.resolved_tool_id,
            self.resolved_adapter_id,
        )
        if any(item is None for item in identities) and not all(item is None for item in identities):
            raise ExecutionPlanError("resolved capability identities must be complete or absent")
        for item in identities:
            if item is not None and (not item or len(item) > 256 or item != item.strip()):
                raise ExecutionPlanError("resolved capability identity is invalid")
        if type(self.deadline_ms) is not int or not 100 <= self.deadline_ms <= 15_000:
            raise ExecutionPlanError("validated step deadline is invalid")
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= 2:
            raise ExecutionPlanError("validated step call budget is invalid")
        if type(self.max_output_tokens) is not int or not 0 <= self.max_output_tokens <= 1_024:
            raise ExecutionPlanError("validated step output budget is invalid")
        if self.resolved_security_id is not None and self.max_output_tokens != 0:
            raise ExecutionPlanError("capability step call/output budget is invalid")

    def payload(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "capability_id": self.capability_id,
            "effect_class": self.effect_class.value,
            "resolved_security_id": self.resolved_security_id,
            "resolved_tool_id": self.resolved_tool_id,
            "resolved_adapter_id": self.resolved_adapter_id,
            "depends_on": list(self.depends_on),
            "parallel_group": self.parallel_group,
            "input": dict(self.input),
            "idempotency_key": self.idempotency_key,
            "deadline_ms": self.deadline_ms,
            "max_calls": self.max_calls,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class ValidatedExecutionPlan:
    proposal_digest: str
    manifest_digest: str
    binding_snapshot_sha256: str
    policy_version: str
    policy_sha256: str
    actor_binding_sha256: str
    conversation_binding_sha256: str
    authority_scope: PlanAuthorityScope
    authority_binding_sha256: str
    required_security_ids: tuple[str, ...]
    source_bindings: tuple[PlanSourceBinding, ...]
    source_bindings_sha256: str
    budget_sha256: str
    budgets: SupervisorBudgets
    effect_classes: tuple[CapabilityEffectClass, ...]
    confirmation_required: bool
    confirmation_present: bool
    fallback_owner: str
    publication_owner: str
    steps: tuple[ValidatedStep, ...]
    _seal: _AdmissionSeal

    def __post_init__(self) -> None:
        if not isinstance(self._seal, _AdmissionSeal) or self._seal.token != "policy-kernel-v1":
            raise ExecutionPlanError("validated execution plan cannot be constructed from model output")
        for label, value in (
            ("binding snapshot", self.binding_snapshot_sha256),
            ("policy", self.policy_sha256),
            ("authority binding", self.authority_binding_sha256),
            ("source bindings", self.source_bindings_sha256),
            ("budget", self.budget_sha256),
        ):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ExecutionPlanError(f"validated execution plan {label} is invalid")
        if type(self.authority_scope) is not PlanAuthorityScope:
            raise ExecutionPlanError("validated execution plan authority scope is invalid")
        try:
            exact_sources = source_bindings_sha256(self.source_bindings)
        except (TypeError, ValueError) as exc:
            raise ExecutionPlanError("validated execution plan source bindings are invalid") from exc
        if exact_sources != self.source_bindings_sha256:
            raise ExecutionPlanError("validated execution plan source binding digest is stale")
        if type(self.budgets) is not SupervisorBudgets or (
            self.budgets.canonical_sha256() != self.budget_sha256
        ):
            raise ExecutionPlanError("validated execution plan budget binding is stale")
        try:
            exact_authority = durable_authority_binding_sha256(
                scope=self.authority_scope,
                actor_binding_sha256=self.actor_binding_sha256,
                conversation_binding_sha256=self.conversation_binding_sha256,
                proposal_sha256=self.proposal_digest,
                manifest_sha256=self.manifest_digest,
                policy_sha256=self.policy_sha256,
                source_bindings_sha256=self.source_bindings_sha256,
                capability_bindings_sha256=self.binding_snapshot_sha256,
                budget_sha256=self.budget_sha256,
                required_security_ids=self.required_security_ids,
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionPlanError("validated execution plan authority binding is invalid") from exc
        if exact_authority != self.authority_binding_sha256:
            raise ExecutionPlanError("validated execution plan authority binding is stale")
        if any(step.deadline_ms != self.budgets.per_step_deadline_ms for step in self.steps):
            raise ExecutionPlanError("validated execution plan deadline binding is stale")
        if not self.steps:
            raise ExecutionPlanError("validated execution plan needs at least one admitted step")
        if any(effect is not CapabilityEffectClass.READ for effect in self.effect_classes):
            raise ExecutionPlanError("P1 validated plans may admit read steps only")
        if self.effect_classes != tuple(step.effect_class for step in self.steps):
            raise ExecutionPlanError("validated execution plan effect binding is stale")
        capability_steps = tuple(step for step in self.steps if step.resolved_security_id is not None)
        model_steps = tuple(step for step in self.steps if step.resolved_security_id is None)
        if (
            tuple(
                sorted(
                    {
                        step.resolved_security_id
                        for step in capability_steps
                        if step.resolved_security_id is not None
                    }
                )
            )
            != self.required_security_ids
        ):
            raise ExecutionPlanError("validated execution plan security binding is stale")
        if (
            len(self.steps) > self.budgets.max_steps
            or sum(step.max_calls for step in capability_steps) != self.budgets.max_capability_calls
            or sum(step.max_calls for step in model_steps)
            > self.budgets.max_model_calls - self.budgets.max_supervisor_calls
            or sum(step.max_output_tokens for step in model_steps) > self.budgets.max_output_tokens
        ):
            raise ExecutionPlanError("validated execution plan resource binding is stale")
        parallel_counts: dict[str, int] = {}
        for step in capability_steps:
            if step.parallel_group is not None:
                parallel_counts[step.parallel_group] = parallel_counts.get(step.parallel_group, 0) + 1
        if any(count > self.budgets.max_parallel_reads for count in parallel_counts.values()):
            raise ExecutionPlanError("validated execution plan parallel budget is stale")
        if self.publication_owner != "primary":
            raise ExecutionPlanError("publication owner must remain the primary model")
        if self.fallback_owner != "primary_only":
            raise ExecutionPlanError("fallback owner must remain primary_only")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": VALIDATED_EXECUTION_PLAN_SCHEMA,
            "proposal_digest": self.proposal_digest,
            "manifest_digest": self.manifest_digest,
            "binding_snapshot_sha256": self.binding_snapshot_sha256,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "actor_binding_sha256": self.actor_binding_sha256,
            "conversation_binding_sha256": self.conversation_binding_sha256,
            "authority_scope": self.authority_scope.value,
            "authority_binding_sha256": self.authority_binding_sha256,
            "required_security_ids": list(self.required_security_ids),
            "source_bindings": [item.payload() for item in self.source_bindings],
            "source_bindings_sha256": self.source_bindings_sha256,
            "budget_sha256": self.budget_sha256,
            "budgets": self.budgets.payload(),
            "effect_classes": [item.value for item in self.effect_classes],
            "confirmation_required": self.confirmation_required,
            "confirmation_present": self.confirmation_present,
            "fallback_owner": self.fallback_owner,
            "publication_owner": self.publication_owner,
            "steps": [item.payload() for item in self.steps],
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def parse(cls, value: object) -> ValidatedExecutionPlan:
        del value
        raise ExecutionPlanError("validated execution plan cannot be parsed from model output")


def mint_admission_seal() -> _AdmissionSeal:
    """Return the kernel-only constructor token.  Not a public parser API."""

    return _AdmissionSeal(token="policy-kernel-v1")


def plan_from_admitted_proposal(
    proposal: SupervisorProposal,
    *,
    manifest_digest: str,
    binding_snapshot_sha256: str,
    policy_version: str,
    policy_sha256: str,
    actor_binding_sha256: str,
    conversation_binding_sha256: str,
    authority_scope: PlanAuthorityScope,
    authority_binding_sha256: str,
    required_security_ids: tuple[str, ...],
    source_bindings: tuple[PlanSourceBinding, ...],
    budgets: SupervisorBudgets,
    steps: tuple[ValidatedStep, ...],
    seal: _AdmissionSeal,
) -> ValidatedExecutionPlan:
    effects = tuple(step.effect_class for step in steps)
    return ValidatedExecutionPlan(
        proposal_digest=proposal.canonical_sha256(),
        manifest_digest=manifest_digest,
        binding_snapshot_sha256=binding_snapshot_sha256,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        actor_binding_sha256=actor_binding_sha256,
        conversation_binding_sha256=conversation_binding_sha256,
        authority_scope=authority_scope,
        authority_binding_sha256=authority_binding_sha256,
        required_security_ids=required_security_ids,
        source_bindings=source_bindings,
        source_bindings_sha256=source_bindings_sha256(source_bindings),
        budget_sha256=budgets.canonical_sha256(),
        budgets=budgets,
        effect_classes=effects,
        confirmation_required=False,
        confirmation_present=False,
        fallback_owner="primary_only",
        publication_owner="primary",
        steps=steps,
        _seal=seal,
    )
