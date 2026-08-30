"""Narrow non-owning ports used before a promoted supervisor owns a turn.

These adapters may ask the optional secondary model for one proposal or one
review and may attest the existing primary model runtime.  They deliberately
have no storage, capability, publication, or WorkGraph handle.  A caller must
cross a separate atomic ownership boundary before executing an admitted plan.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from friday.model_profiles import V12_MODEL_LEASE_SCHEMA, ModelProfileLease, ModelRequirements
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
    supervisor_assist_promotion_static_preflight,
)
from friday.orchestration.supervisor_contracts import SupervisorInput
from friday.orchestration.supervisor_review_policy import SupervisorReviewContext
from friday.orchestration.supervisor_review_transport import (
    AdmittedSupervisorReview,
    build_supervisor_review_request,
    parse_and_admit_supervisor_review,
)
from friday.orchestration.turn_context import AuthenticatedTurnContext, TurnContextError
from friday.orchestration.turn_context_advisory import suspend_authenticated_advisory_authority
from friday.orchestration.turn_context_call_scope import (
    AuthenticatedChatCallScope,
    require_current_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_runtime import (
    current_primary_authenticated_turn_context,
    reserve_authenticated_advisory_call,
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


class AssistRuntimeAdmissionScheduler(Protocol):
    """The content-free supervisor admission surface; no product request."""

    def public_status(self) -> Mapping[str, object]: ...

    def diagnostics_status(self) -> Mapping[str, object]: ...

    async def refresh_semantic_supervisor_runtime_admission(
        self,
        *,
        absolute_deadline_monotonic: float,
    ) -> bool: ...


class PrimaryModelRuntime(Protocol):
    """Exact primary model surface required by the comparison executor."""

    async def attest(self, *, absolute_deadline: float) -> object: ...

    def public_status(self) -> Mapping[str, object]: ...

    def available_context_tokens(self) -> int: ...

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

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
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


def _future_deadline(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or float(value) <= time.monotonic()
    ):
        return None
    return float(value)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("primary model deadline expired")
    return remaining


def _lease_matches_requirements(
    lease: object,
    requirements: object,
) -> bool:
    """Prove the complete v2 leased subset without widening runtime authority."""

    if type(lease) is not ModelProfileLease or type(requirements) is not ModelRequirements:
        return False
    try:
        return bool(
            lease.schema == V12_MODEL_LEASE_SCHEMA
            and lease.requirements_sha256 == requirements.canonical_sha256()
            and lease.capabilities == requirements.capabilities
            and lease.required_context_tokens == requirements.required_context_tokens
            and lease.prepared_evidence_items == requirements.prepared_evidence_items
            and lease.max_tool_steps == requirements.max_tool_steps
            and lease.max_tool_rounds == requirements.max_tool_rounds
            and lease.max_tool_calls == requirements.max_tool_calls
            and lease.effect is requirements.effect
            and lease.verifier_required is requirements.verifier_required
        )
    except Exception:
        return False


def _authenticated_advisory_scope(
    absolute_deadline: float,
) -> tuple[AuthenticatedTurnContext | None, AuthenticatedChatCallScope | None, float]:
    """Bind one advisory call to the live root and its stricter deadline."""

    context = current_primary_authenticated_turn_context()
    if context is None:
        return None, None, absolute_deadline
    scope = require_current_authenticated_chat_call_scope(context)
    return context, scope, min(absolute_deadline, scope.conservative_deadline_monotonic)


def _revalidate_authenticated_advisory_scope(
    context: AuthenticatedTurnContext | None,
    scope: AuthenticatedChatCallScope | None,
) -> None:
    if context is None and scope is None:
        return
    if type(context) is not AuthenticatedTurnContext or type(scope) is not AuthenticatedChatCallScope:
        raise TurnContextError("authenticated advisory call scope is invalid")
    if require_current_authenticated_chat_call_scope(context) is not scope:
        raise TurnContextError("authenticated advisory call scope drifted")


@dataclass(frozen=True, slots=True)
class AssistPromotionEvaluator:
    """Re-evaluate immutable evidence against fresh scheduler/registry facts."""

    material: AssistPromotionActivationMaterial
    scheduler: AssistRuntimeAdmissionScheduler

    def runtime_admission_refresh_is_eligible(
        self,
        *,
        binding_snapshot: CapabilityBindingSnapshot,
        actor_binding_sha256: str | None = None,
    ) -> bool:
        """Prove every promotion gate that does not need fresh runtime state."""

        if not isinstance(self.material, AssistPromotionActivationMaterial):
            return False
        try:
            candidate = self.material.fresh_candidate(
                _status(self.scheduler, "public_status"),
                _status(self.scheduler, "diagnostics_status"),
                binding_snapshot,
                actor_binding_sha256=actor_binding_sha256,
            )
            evidence = self.material.loaded_evidence
            return bool(
                candidate is not None
                and evidence is not None
                and supervisor_assist_promotion_static_preflight(
                    candidate,
                    evidence.evidence,
                    self.material.operator_gate,
                )
            )
        except Exception:
            return False

    async def refresh_runtime_admission(self, *, absolute_deadline: float) -> bool:
        """Refresh only content-free supervisor admission within the root deadline."""

        deadline = _future_deadline(absolute_deadline)
        refresh = getattr(self.scheduler, "refresh_semantic_supervisor_runtime_admission", None)
        if deadline is None or not callable(refresh):
            return False
        authenticated_context, authenticated_scope, effective_deadline = _authenticated_advisory_scope(
            deadline
        )
        try:
            async with asyncio.timeout(_remaining(effective_deadline)):
                with suspend_authenticated_advisory_authority():
                    ready = await refresh(
                        absolute_deadline_monotonic=effective_deadline,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        _revalidate_authenticated_advisory_scope(authenticated_context, authenticated_scope)
        return ready is True and _future_deadline(effective_deadline) is not None

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
        authenticated_context, authenticated_scope, effective_deadline = _authenticated_advisory_scope(
            absolute_deadline
        )
        try:
            request = build_supervisor_request(
                supervisor_input,
                absolute_deadline_monotonic=effective_deadline,
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

        if authenticated_context is not None:
            try:
                reserve_authenticated_advisory_call(authenticated_context)
            except TurnContextError:
                return None
        try:
            with suspend_authenticated_advisory_authority():
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
        _revalidate_authenticated_advisory_scope(authenticated_context, authenticated_scope)
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
        authenticated_context, authenticated_scope, effective_deadline = _authenticated_advisory_scope(
            absolute_deadline
        )
        try:
            request = build_supervisor_review_request(
                context,
                absolute_deadline_monotonic=effective_deadline,
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

        if authenticated_context is not None:
            try:
                reserve_authenticated_advisory_call(authenticated_context)
            except TurnContextError:
                return None
        try:
            with suspend_authenticated_advisory_authority():
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
        _revalidate_authenticated_advisory_scope(authenticated_context, authenticated_scope)
        if not isinstance(attempt, SecondaryAttempt) or not attempt.succeeded:
            return None
        return accepted


@dataclass(slots=True)
class AttestedPrimaryModel:
    """Require an explicit live attestation before the ownership commit."""

    runtime: PrimaryModelRuntime = field(repr=False)

    def available_context_tokens(self) -> int:
        """Project only a strict, currently attested context-token count."""

        try:
            value = self.runtime.available_context_tokens()
        except Exception:
            return 0
        return value if type(value) is int and 0 < value < (1 << 63) else 0

    async def prepare_primary_model(self, *, absolute_deadline: float) -> bool:
        deadline = _future_deadline(absolute_deadline)
        if deadline is None:
            return False
        try:
            async with asyncio.timeout(_remaining(deadline)):
                await self.runtime.attest(absolute_deadline=deadline)
            status = self.runtime.public_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return bool(
            _future_deadline(deadline) is not None
            and isinstance(status, Mapping)
            and status.get("status") == "canary_ready"
        )

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        deadline = _future_deadline(absolute_deadline)
        if type(requirements) is not ModelRequirements or deadline is None:
            return None
        try:
            async with asyncio.timeout(_remaining(deadline)):
                lease = await self.runtime.acquire_lease(
                    requirements,
                    absolute_deadline=deadline,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return None
        return (
            lease
            if _future_deadline(deadline) is not None and _lease_matches_requirements(lease, requirements)
            else None
        )

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        deadline = _future_deadline(absolute_deadline)
        if deadline is None or not _lease_matches_requirements(lease, requirements):
            return False
        try:
            async with asyncio.timeout(_remaining(deadline)):
                current = await self.runtime.lease_is_current(
                    lease,
                    requirements,
                    absolute_deadline=deadline,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return False
        return _future_deadline(deadline) is not None and current is True

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        if not _lease_matches_requirements(lease, requirements):
            return False
        try:
            current = self.runtime.lease_is_process_current(lease, requirements)
        except Exception:
            return False
        return current is True and _lease_matches_requirements(lease, requirements)

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
        deadline = _future_deadline(absolute_deadline)
        if deadline is None or not _lease_matches_requirements(lease, requirements):
            raise RuntimeError("primary model lease is invalid")
        async with asyncio.timeout(_remaining(deadline)):
            response = await self.runtime.complete(
                lease,
                requirements,
                messages,
                max_tokens=max_tokens,
                priority=priority,
                absolute_deadline=deadline,
                temperature=temperature,
            )
        if _future_deadline(deadline) is None:
            raise TimeoutError("primary model deadline expired")
        return response


__all__ = [
    "AssistPromotionEvaluator",
    "AssistRuntimeAdmissionScheduler",
    "AssistSecondaryScheduler",
    "AttestedPrimaryModel",
    "PrimaryModelRuntime",
    "SchedulerAssistPlanner",
    "SchedulerAssistReviewer",
]
