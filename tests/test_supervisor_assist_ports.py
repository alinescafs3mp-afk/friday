from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

import friday.orchestration.supervisor_assist_ports as ports
from friday.orchestration.capability_binding import operational_capability_snapshot
from friday.orchestration.semantic_supervisor import ParsedSupervisorProposal
from friday.orchestration.supervisor_assist_activation import (
    AssistPromotionActivationMaterial,
    AssistPromotionActivationReason,
)
from friday.orchestration.supervisor_assist_promotion import AssistPromotionOperatorGate
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.secondary_brain import (
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryResult,
)


def _request() -> ModelRequest:
    return ModelRequest(
        workload=ModelWorkload.PLAN_CANDIDATE,
        messages=({"role": "user", "content": "{}"},),
        max_output_tokens=8,
        absolute_deadline_monotonic=time.monotonic() + 10,
        priority=ModelPriority.BACKGROUND,
    )


class _Scheduler:
    def __init__(
        self,
        *,
        result: SecondaryResult | None = None,
        call_validator: bool = True,
        failure: SecondaryFailure | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.result = result or SecondaryResult(visible_content="{}", structured_output={})
        self.call_validator = call_validator
        self.failure = failure
        self.raises = raises
        self.guard_result: bool | None = None
        self.calls = 0

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Any = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Any = None,
        dispatch_observer: Any = None,
    ) -> SecondaryAttempt:
        del request, invalidate_on_rejection, dispatch_observer
        self.calls += 1
        if pre_dispatch_validator is not None:
            self.guard_result = pre_dispatch_validator()
            if not self.guard_result:
                return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
        if self.raises is not None:
            raise self.raises
        accepted = validator(self.result) if self.call_validator and validator else True
        if not accepted:
            return SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
        if self.failure is not None:
            return SecondaryAttempt.rejected(self.failure)
        return SecondaryAttempt.success(self.result)


@pytest.mark.asyncio
async def test_planner_returns_only_validator_retained_kernel_plan(monkeypatch: Any) -> None:
    scheduler = _Scheduler()
    expected = SimpleNamespace(
        decision=SimpleNamespace(admitted=True, plan=object()),
    )
    monkeypatch.setattr(ports, "build_supervisor_request", lambda *_args, **_kwargs: _request())
    monkeypatch.setattr(
        ports,
        "parse_and_admit_supervisor_proposal",
        lambda *_args, **_kwargs: expected,
    )

    result = await ports.SchedulerAssistPlanner(cast(Any, scheduler)).propose(
        cast(Any, object()),
        cast(Any, object()),
        absolute_deadline=time.monotonic() + 5,
    )

    assert result is expected
    assert scheduler.calls == 1


@pytest.mark.asyncio
async def test_planner_rejects_success_without_validator_and_closes_guard_exceptions(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(ports, "build_supervisor_request", lambda *_args, **_kwargs: _request())
    unvalidated = _Scheduler(call_validator=False)
    assert (
        await ports.SchedulerAssistPlanner(cast(Any, unvalidated)).propose(
            cast(Any, object()),
            cast(Any, object()),
            absolute_deadline=time.monotonic() + 5,
        )
        is None
    )

    guarded = _Scheduler()

    def broken_guard() -> bool:
        raise RuntimeError("private guard failure")

    assert (
        await ports.SchedulerAssistPlanner(cast(Any, guarded)).propose(
            cast(Any, object()),
            cast(Any, object()),
            absolute_deadline=time.monotonic() + 5,
            pre_dispatch_validator=broken_guard,
        )
        is None
    )
    assert guarded.guard_result is False


@pytest.mark.asyncio
async def test_planner_propagates_cancellation(monkeypatch: Any) -> None:
    monkeypatch.setattr(ports, "build_supervisor_request", lambda *_args, **_kwargs: _request())
    scheduler = _Scheduler(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ports.SchedulerAssistPlanner(cast(Any, scheduler)).propose(
            cast(Any, object()),
            cast(Any, object()),
            absolute_deadline=time.monotonic() + 5,
        )


@pytest.mark.asyncio
async def test_reviewer_returns_only_a_policy_admitted_review(monkeypatch: Any) -> None:
    monkeypatch.setattr(ports, "build_supervisor_review_request", lambda *_args, **_kwargs: _request())
    accepted = SimpleNamespace(decision=SimpleNamespace(admitted=True))
    monkeypatch.setattr(
        ports,
        "parse_and_admit_supervisor_review",
        lambda *_args, **_kwargs: accepted,
    )
    scheduler = _Scheduler()

    result = await ports.SchedulerAssistReviewer(cast(Any, scheduler)).review(
        cast(Any, object()),
        absolute_deadline=time.monotonic() + 5,
    )

    assert result is accepted

    rejected = SimpleNamespace(decision=SimpleNamespace(admitted=False))
    monkeypatch.setattr(
        ports,
        "parse_and_admit_supervisor_review",
        lambda *_args, **_kwargs: rejected,
    )
    assert (
        await ports.SchedulerAssistReviewer(cast(Any, _Scheduler())).review(
            cast(Any, object()),
            absolute_deadline=time.monotonic() + 5,
        )
        is None
    )


class _Primary:
    def __init__(self, *, status: str = "canary_ready", fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.attest_calls = 0

    async def attest(self, *, absolute_deadline: float) -> object:
        assert absolute_deadline > time.monotonic()
        self.attest_calls += 1
        if self.fail:
            raise RuntimeError("provider body must not escape")
        return object()

    def public_status(self) -> dict[str, object]:
        return {"status": self.status}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "fail", "expected"),
    [("canary_ready", False, True), ("closed", False, False), ("canary_ready", True, False)],
)
async def test_primary_preparation_requires_explicit_clear_attestation(
    status: str,
    fail: bool,
    expected: bool,
) -> None:
    runtime = _Primary(status=status, fail=fail)
    prepared = await ports.AttestedPrimaryModel(cast(Any, runtime)).prepare_primary_model(
        absolute_deadline=time.monotonic() + 5,
    )
    assert prepared is expected
    assert runtime.attest_calls == 1


def test_default_off_activation_never_creates_a_promoted_decision() -> None:
    material = AssistPromotionActivationMaterial(
        configured=False,
        reason=AssistPromotionActivationReason.DEFAULT_OFF,
        requested_mode=SupervisorMode.OFF,
        source_revision_sha256=None,
        registry_binding_sha256=None,
        scheduler_snapshot=None,
        loaded_evidence=None,
        accepted_latency_budget=None,
        operator_gate=AssistPromotionOperatorGate(),
    )
    evaluator = ports.AssistPromotionEvaluator(material, object())
    assert evaluator.decide(binding_snapshot=operational_capability_snapshot()) is None


def test_port_module_does_not_expose_storage_or_publication_handles() -> None:
    exported = set(ports.__all__)
    assert not any("storage" in item.casefold() or "publish" in item.casefold() for item in exported)
    assert ParsedSupervisorProposal.__module__ != ports.__name__
