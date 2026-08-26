"""Code-owned validated execution plan.  Model output cannot construct it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from friday.orchestration.supervisor_contracts import (
    CapabilityEffectClass,
    SupervisorContractError,
    SupervisorProposal,
    canonical_sha256,
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
        }


@dataclass(frozen=True, slots=True)
class ValidatedExecutionPlan:
    proposal_digest: str
    manifest_digest: str
    binding_snapshot_sha256: str
    policy_version: str
    actor_binding_sha256: str
    conversation_binding_sha256: str
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
        if len(self.binding_snapshot_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.binding_snapshot_sha256
        ):
            raise ExecutionPlanError("validated execution plan binding snapshot is invalid")
        if not self.steps:
            raise ExecutionPlanError("validated execution plan needs at least one admitted step")
        if any(effect is not CapabilityEffectClass.READ for effect in self.effect_classes):
            raise ExecutionPlanError("P1 validated plans may admit read steps only")
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
            "actor_binding_sha256": self.actor_binding_sha256,
            "conversation_binding_sha256": self.conversation_binding_sha256,
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
    actor_binding_sha256: str,
    conversation_binding_sha256: str,
    steps: tuple[ValidatedStep, ...],
    seal: _AdmissionSeal,
) -> ValidatedExecutionPlan:
    effects = tuple(step.effect_class for step in steps)
    return ValidatedExecutionPlan(
        proposal_digest=proposal.canonical_sha256(),
        manifest_digest=manifest_digest,
        binding_snapshot_sha256=binding_snapshot_sha256,
        policy_version=policy_version,
        actor_binding_sha256=actor_binding_sha256,
        conversation_binding_sha256=conversation_binding_sha256,
        effect_classes=effects,
        confirmation_required=False,
        confirmation_present=False,
        fallback_owner="primary_only",
        publication_owner="primary",
        steps=steps,
        _seal=seal,
    )
