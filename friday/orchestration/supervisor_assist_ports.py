"""Narrow non-owning ports used before a promoted supervisor owns a turn.

These adapters may ask the optional secondary model for one proposal or one
review and may attest the existing primary model runtime.  They deliberately
have no storage, capability, publication, or WorkGraph handle.  A caller must
cross a separate atomic ownership boundary before executing an admitted plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration.capability_binding import CapabilityBindingSnapshot
from friday.orchestration.policy_kernel import PolicyAdmissionContext
from friday.orchestration.semantic_supervisor import (
    ParsedSupervisorProposal,
    build_supervisor_request,
    parse_and_admit_supervisor_proposal,
)
from friday.orchestration.supervisor_assist_activation import AssistPromotionActivationMaterial
from friday.orchestration.supervisor_assist_promotion import (
    AssistPromotionDecision,
    admit_supervisor_assist_promotion,
)
from friday.orchestration.supervisor_contracts import SupervisorInput
from friday.orchestration.supervisor_review_policy import SupervisorReviewContext
from friday.orchestration.supervisor_review_transport import (
    AdmittedSupervisorReview,
    build_supervisor_review_request,
    parse_and_admit_supervisor_review,
)
from friday.secondary_brain import ModelRequest, SecondaryAttempt, SecondaryResult


class AssistSecondaryScheduler(Protocol):
    """The already-admitted optional scheduler surface; no generic client."""

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt: ...


class PrimaryModelRuntime(Protocol):
    """Exact primary model surface required by the comparison executor."""

    async def attest(self, *, absolute_deadline: float) -> object: ...

    def public_status(self) -> Mapping[str, object]: ...

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None: ...

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool: ...

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]: ...


def _status(runtime: object, method_name: str) -> Mapping[str, object]:
    method = getattr(runtime, method_name, None)
    if not callable(method):
        return {}
    try:
        value = method()
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _guarded(guard: Callable[[], bool] | None) -> Callable[[], bool] | None:
    if guard is None:
        return None

    def evaluate() -> bool:
        try:
            return guard() is True
        except Exception:
            return False

    return evaluate


@dataclass(frozen=True, slots=True)
class AssistPromotionEvaluator:
    """Re-evaluate immutable evidence against fresh scheduler/registry facts."""

    material: AssistPromotionActivationMaterial
    scheduler: object

    def decide(
        self,
        *,
        binding_snapshot: CapabilityBindingSnapshot,
        actor_binding_sha256: str | None = None,
    ) -> AssistPromotionDecision | None:
        if not isinstance(self.material, AssistPromotionActivationMaterial):
            return None
        try:
            candidate = self.material.fresh_candidate(
                _status(self.scheduler, "public_status"),
                _status(self.scheduler, "diagnostics_status"),
                binding_snapshot,
                actor_binding_sha256=actor_binding_sha256,
            )
            evidence = self.material.loaded_evidence
            if candidate is None or evidence is None:
                return None
            decision = admit_supervisor_assist_promotion(
                candidate,
                evidence.evidence,
                self.material.operator_gate,
            )
        except Exception:
            return None
        return decision if decision.promotion_admitted else None


@dataclass(slots=True)
class SchedulerAssistPlanner:
    """Return only a Policy-Kernel-minted plan from one bounded proposal call."""

    scheduler: AssistSecondaryScheduler

    async def propose(
        self,
        supervisor_input: SupervisorInput,
        context: PolicyAdmissionContext,
        *,
        absolute_deadline: float,
        pre_dispatch_validator: Callable[[], bool] | None = None,
    ) -> ParsedSupervisorProposal | None:
        try:
            request = build_supervisor_request(
                supervisor_input,
                absolute_deadline_monotonic=absolute_deadline,
            )
        except Exception:
            return None
        accepted: ParsedSupervisorProposal | None = None

        def validate(result: SecondaryResult) -> bool:
            nonlocal accepted
            try:
                parsed = parse_and_admit_supervisor_proposal(result, supervisor_input, context)
            except Exception:
                return False
            if not parsed.decision.admitted or parsed.decision.plan is None:
                return False
            accepted = parsed
            return True

        try:
            attempt = await self.scheduler.evaluate_shadow(
                request,
                validator=validate,
                invalidate_on_rejection=False,
                pre_dispatch_validator=_guarded(pre_dispatch_validator),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if not isinstance(attempt, SecondaryAttempt) or not attempt.succeeded:
            return None
        return accepted


@dataclass(slots=True)
class SchedulerAssistReviewer:
    """Run at most the one review requested by the controller."""

    scheduler: AssistSecondaryScheduler

    async def review(
        self,
        context: SupervisorReviewContext,
        *,
        absolute_deadline: float,
        pre_dispatch_validator: Callable[[], bool] | None = None,
    ) -> AdmittedSupervisorReview | None:
        try:
            request = build_supervisor_review_request(
                context,
                absolute_deadline_monotonic=absolute_deadline,
            )
        except Exception:
            return None
        accepted: AdmittedSupervisorReview | None = None

        def validate(result: SecondaryResult) -> bool:
            nonlocal accepted
            try:
                parsed = parse_and_admit_supervisor_review(result, context)
            except Exception:
                return False
            if not parsed.decision.admitted:
                return False
            accepted = parsed
            return True

        try:
            attempt = await self.scheduler.evaluate_shadow(
                request,
                validator=validate,
                invalidate_on_rejection=False,
                pre_dispatch_validator=_guarded(pre_dispatch_validator),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if not isinstance(attempt, SecondaryAttempt) or not attempt.succeeded:
            return None
        return accepted


@dataclass(slots=True)
class AttestedPrimaryModel:
    """Require an explicit live attestation before the ownership commit."""

    runtime: PrimaryModelRuntime

    async def prepare_primary_model(self, *, absolute_deadline: float) -> bool:
        try:
            await self.runtime.attest(absolute_deadline=absolute_deadline)
            status = self.runtime.public_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return isinstance(status, Mapping) and status.get("status") == "canary_ready"

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        return await self.runtime.acquire_lease(
            requirements,
            absolute_deadline=absolute_deadline,
        )

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        return await self.runtime.lease_is_current(
            lease,
            requirements,
            absolute_deadline=absolute_deadline,
        )

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]:
        return await self.runtime.complete(
            lease,
            requirements,
            messages,
            max_tokens=max_tokens,
            priority=priority,
            absolute_deadline=absolute_deadline,
            temperature=temperature,
        )


__all__ = [
    "AssistPromotionEvaluator",
    "AssistSecondaryScheduler",
    "AttestedPrimaryModel",
    "PrimaryModelRuntime",
    "SchedulerAssistPlanner",
    "SchedulerAssistReviewer",
]
