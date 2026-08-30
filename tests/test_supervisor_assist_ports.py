from __future__ import annotations

import asyncio
import hashlib
import math
import time
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

import friday.orchestration.supervisor_assist_ports as ports
from friday import execution_kernel as execution_kernel_module
from friday.execution_kernel import (
    bind_authenticated_request_effect_authority,
    track_request_effects,
)
from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    ModelProfileLease,
    ModelRequirements,
)
from friday.orchestration import turn_context_publication as publication_module
from friday.orchestration.capability_binding import operational_capability_snapshot
from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.semantic_supervisor import ParsedSupervisorProposal
from friday.orchestration.supervisor_assist_activation import (
    AssistPromotionActivationMaterial,
    AssistPromotionActivationReason,
)
from friday.orchestration.supervisor_assist_promotion import AssistPromotionOperatorGate
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    FinalPublisher,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.orchestration.turn_context_call_scope import require_authenticated_chat_call_scope
from friday.orchestration.turn_context_publication import bind_authenticated_turn_publication
from friday.orchestration.turn_context_runtime import (
    bind_authenticated_turn_context,
    current_authenticated_turn_context,
    current_primary_authenticated_turn_context,
    reserve_authenticated_advisory_call,
)
from friday.permissions import ActorContext
from friday.secondary_brain import (
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryResult,
)
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision


def _request(*, absolute_deadline: float | None = None) -> ModelRequest:
    return ModelRequest(
        workload=ModelWorkload.PLAN_CANDIDATE,
        messages=({"role": "user", "content": "{}"},),
        max_output_tokens=8,
        absolute_deadline_monotonic=(
            time.monotonic() + 10 if absolute_deadline is None else absolute_deadline
        ),
        priority=ModelPriority.BACKGROUND,
    )


def _authenticated_call(
    label: str,
    *,
    max_advisory_calls: int,
) -> tuple[
    TurnContextIssuer,
    AuthenticatedTurnContext,
    ActorContext,
    float,
    dict[str, Any],
]:
    actor = ActorContext("owner", "owner", "test")
    issuer = TurnContextIssuer(hashlib.sha256(label.encode("ascii")).digest())
    deadline_ns = time.monotonic_ns() + 5_000_000_000
    deadline = deadline_ns / 1_000_000_000
    deadline_ns = int(deadline * 1_000_000_000)
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"lease-{label}",
        actor=actor,
        conversation_id="conv_1234567890abcdef",
        interaction_mode=TurnMode.DIALOGUE,
        source_id=actor.source,
        update_id=f"request-{label}",
        request_effect_binding_sha256="b" * 64,
    )
    turn = TurnInput.from_chat(
        message="authenticated advisory",
        actor=actor,
        conversation_id="conv_1234567890abcdef",
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.LEGACY,
        fallback_router_mode=None,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=turn,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(deadline_ns),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, max_advisory_calls, 2_048),
        ),
        pending_work_admission=None,
    )
    ingestion = {"reason": "exact adjunct"}
    return issuer, context, actor, deadline, ingestion


def _bind_call_scope(
    context: AuthenticatedTurnContext,
    actor: ActorContext,
    deadline: float,
    ingestion: dict[str, Any],
) -> None:
    require_authenticated_chat_call_scope(
        context,
        user_id=actor.user_id,
        message="authenticated advisory",
        actor=actor,
        conversation_id="conv_1234567890abcdef",
        attachments=[],
        enable_tools=True,
        synthetic_document_notice=False,
        replay_source_message_id=None,
        mode=None,
        answer_with_voice=False,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
        reply_assistant_message_id=None,
        turn_policy=None,
        telegram_update_id=None,
        turn_deadline=deadline,
        pending_durable_admission=None,
        ingestion_result=ingestion,
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


class _AuthorityProbeScheduler(_Scheduler):
    def __init__(self, *, mutate: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.deadlines: list[float] = []
        self.mutate = mutate

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Any = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Any = None,
        dispatch_observer: Any = None,
    ) -> SecondaryAttempt:
        del invalidate_on_rejection, dispatch_observer
        self.calls += 1
        self.deadlines.append(request.absolute_deadline_monotonic)
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        assert execution_kernel_module._REQUEST_EFFECTS.get() is None
        assert execution_kernel_module._AUTHENTICATED_REQUEST_EFFECT_AUTHORITY.get() is None
        assert publication_module._PUBLICATION_LEASE.get() is None
        if pre_dispatch_validator is not None:
            self.guard_result = pre_dispatch_validator()
            if not self.guard_result:
                return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
        if self.mutate is not None:
            self.mutate["reason"] = "mutated while advisory authority was suspended"
        accepted = validator(self.result) if validator else True
        return (
            SecondaryAttempt.success(self.result)
            if accepted
            else SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
        )


class _AdmissionRefreshScheduler:
    def __init__(
        self,
        *,
        ready: bool = True,
        raises: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self.ready = ready
        self.raises = raises
        self.hang = hang
        self.calls = 0
        self.deadlines: list[float] = []

    async def refresh_semantic_supervisor_runtime_admission(
        self,
        *,
        absolute_deadline_monotonic: float,
    ) -> bool:
        self.calls += 1
        self.deadlines.append(absolute_deadline_monotonic)
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        assert execution_kernel_module._REQUEST_EFFECTS.get() is None
        assert execution_kernel_module._AUTHENTICATED_REQUEST_EFFECT_AUTHORITY.get() is None
        assert publication_module._PUBLICATION_LEASE.get() is None
        if self.raises is not None:
            raise self.raises
        if self.hang:
            await asyncio.Event().wait()
        return self.ready


@pytest.mark.asyncio
async def test_promotion_refresh_is_content_free_bounded_and_does_not_consume_advisory_slot() -> None:
    issuer, context, actor, deadline, ingestion = _authenticated_call(
        "assist-admission-refresh",
        max_advisory_calls=1,
    )
    scheduler = _AdmissionRefreshScheduler()
    evaluator = ports.AssistPromotionEvaluator(cast(Any, object()), scheduler)

    with ExitStack() as stack:
        effects = stack.enter_context(
            track_request_effects(
                lambda: True,
                request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
            )
        )
        stack.enter_context(bind_authenticated_turn_context(issuer, context))
        _bind_call_scope(context, actor, deadline, ingestion)
        stack.enter_context(bind_authenticated_request_effect_authority(effects))
        stack.enter_context(
            bind_authenticated_turn_publication(
                context,
                conversation_id="conv_1234567890abcdef",
                person_id=actor.own_id,
                final_publisher=FinalPublisher.PRIMARY,
            )
        )

        assert await evaluator.refresh_runtime_admission(absolute_deadline=deadline + 30)
        conservative_deadline = math.nextafter(
            context.inherited_budget.safety_deadline.monotonic_ns / 1_000_000_000,
            -math.inf,
        )
        assert scheduler.deadlines == [conservative_deadline]
        assert reserve_authenticated_advisory_call(context) == 1
        assert current_primary_authenticated_turn_context(context) is context
        assert execution_kernel_module._REQUEST_EFFECTS.get() is effects
        assert publication_module._PUBLICATION_LEASE.get() is not None


@pytest.mark.asyncio
async def test_promotion_refresh_fails_closed_without_retry_and_preserves_cancellation() -> None:
    failed = _AdmissionRefreshScheduler(ready=False)
    evaluator = ports.AssistPromotionEvaluator(cast(Any, object()), failed)
    assert not await evaluator.refresh_runtime_admission(absolute_deadline=time.monotonic() + 5)
    assert failed.calls == 1

    assert not await evaluator.refresh_runtime_admission(absolute_deadline=time.monotonic() - 1)
    assert failed.calls == 1

    hanging = _AdmissionRefreshScheduler(hang=True)
    started = time.monotonic()
    assert not await ports.AssistPromotionEvaluator(
        cast(Any, object()),
        hanging,
    ).refresh_runtime_admission(absolute_deadline=started + 0.01)
    assert hanging.calls == 1
    assert time.monotonic() - started < 0.5

    cancelled = _AdmissionRefreshScheduler(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ports.AssistPromotionEvaluator(
            cast(Any, object()),
            cancelled,
        ).refresh_runtime_admission(absolute_deadline=time.monotonic() + 5)
    assert cancelled.calls == 1


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


@pytest.mark.asyncio
async def test_authenticated_planner_and_reviewer_share_slots_deadline_and_suspension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer, context, actor, deadline, ingestion = _authenticated_call(
        "assist-shared-slots",
        max_advisory_calls=2,
    )
    planner_scheduler = _AuthorityProbeScheduler()
    reviewer_scheduler = _AuthorityProbeScheduler()
    parsed = SimpleNamespace(decision=SimpleNamespace(admitted=True, plan=object()))
    reviewed = SimpleNamespace(decision=SimpleNamespace(admitted=True))
    monkeypatch.setattr(
        ports,
        "build_supervisor_request",
        lambda *_args, **kwargs: _request(absolute_deadline=kwargs["absolute_deadline_monotonic"]),
    )
    monkeypatch.setattr(ports, "parse_and_admit_supervisor_proposal", lambda *_args: parsed)
    monkeypatch.setattr(
        ports,
        "build_supervisor_review_request",
        lambda *_args, **kwargs: _request(absolute_deadline=kwargs["absolute_deadline_monotonic"]),
    )
    monkeypatch.setattr(ports, "parse_and_admit_supervisor_review", lambda *_args: reviewed)

    with ExitStack() as stack:
        effects = stack.enter_context(
            track_request_effects(
                lambda: True,
                request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
            )
        )
        stack.enter_context(bind_authenticated_turn_context(issuer, context))
        _bind_call_scope(context, actor, deadline, ingestion)
        stack.enter_context(bind_authenticated_request_effect_authority(effects))
        stack.enter_context(
            bind_authenticated_turn_publication(
                context,
                conversation_id="conv_1234567890abcdef",
                person_id=actor.own_id,
                final_publisher=FinalPublisher.PRIMARY,
            )
        )

        proposed = await ports.SchedulerAssistPlanner(cast(Any, planner_scheduler)).propose(
            cast(Any, object()),
            cast(Any, object()),
            absolute_deadline=deadline + 30,
            pre_dispatch_validator=lambda: True,
        )
        admitted_review = await ports.SchedulerAssistReviewer(cast(Any, reviewer_scheduler)).review(
            cast(Any, object()),
            absolute_deadline=deadline + 30,
            pre_dispatch_validator=lambda: True,
        )

        assert proposed is parsed
        assert admitted_review is reviewed
        conservative_deadline = math.nextafter(
            context.inherited_budget.safety_deadline.monotonic_ns / 1_000_000_000,
            -math.inf,
        )
        assert planner_scheduler.deadlines == [conservative_deadline]
        assert reviewer_scheduler.deadlines == [conservative_deadline]
        assert current_primary_authenticated_turn_context(context) is context
        assert execution_kernel_module._REQUEST_EFFECTS.get() is effects
        assert publication_module._PUBLICATION_LEASE.get() is not None
        with pytest.raises(TurnContextError, match="exhausted"):
            reserve_authenticated_advisory_call(context)


@pytest.mark.asyncio
async def test_rejected_request_build_does_not_consume_authenticated_advisory_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer, context, actor, deadline, ingestion = _authenticated_call(
        "assist-build-rejected",
        max_advisory_calls=1,
    )

    def reject_build(*_args: Any, **_kwargs: Any) -> ModelRequest:
        raise ValueError("request is not admissible")

    monkeypatch.setattr(ports, "build_supervisor_request", reject_build)
    scheduler = _Scheduler()
    with bind_authenticated_turn_context(issuer, context):
        _bind_call_scope(context, actor, deadline, ingestion)
        assert (
            await ports.SchedulerAssistPlanner(cast(Any, scheduler)).propose(
                cast(Any, object()),
                cast(Any, object()),
                absolute_deadline=deadline,
            )
            is None
        )
        assert scheduler.calls == 0
        assert reserve_authenticated_advisory_call(context) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("port_kind", ["planner", "reviewer"])
async def test_authenticated_advisory_revalidates_exact_call_scope_after_await(
    monkeypatch: pytest.MonkeyPatch,
    port_kind: str,
) -> None:
    issuer, context, actor, deadline, ingestion = _authenticated_call(
        f"assist-revalidate-{port_kind}",
        max_advisory_calls=1,
    )
    scheduler = _AuthorityProbeScheduler(mutate=ingestion)
    monkeypatch.setattr(
        ports,
        "build_supervisor_request",
        lambda *_args, **kwargs: _request(absolute_deadline=kwargs["absolute_deadline_monotonic"]),
    )
    monkeypatch.setattr(
        ports,
        "parse_and_admit_supervisor_proposal",
        lambda *_args: SimpleNamespace(
            decision=SimpleNamespace(admitted=True, plan=object()),
        ),
    )
    monkeypatch.setattr(
        ports,
        "build_supervisor_review_request",
        lambda *_args, **kwargs: _request(absolute_deadline=kwargs["absolute_deadline_monotonic"]),
    )
    monkeypatch.setattr(
        ports,
        "parse_and_admit_supervisor_review",
        lambda *_args: SimpleNamespace(decision=SimpleNamespace(admitted=True)),
    )

    with bind_authenticated_turn_context(issuer, context):
        _bind_call_scope(context, actor, deadline, ingestion)
        with pytest.raises(TurnContextError, match="chat call scope drifted"):
            if port_kind == "planner":
                await ports.SchedulerAssistPlanner(cast(Any, scheduler)).propose(
                    cast(Any, object()),
                    cast(Any, object()),
                    absolute_deadline=deadline,
                )
            else:
                await ports.SchedulerAssistReviewer(cast(Any, scheduler)).review(
                    cast(Any, object()),
                    absolute_deadline=deadline,
                )


class _Primary:
    def __init__(self, *, status: str = "canary_ready", fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.attest_calls = 0
        self.context_tokens: object = 8_192
        self.context_failure = False

    async def attest(self, *, absolute_deadline: float) -> object:
        assert absolute_deadline > time.monotonic()
        self.attest_calls += 1
        if self.fail:
            raise RuntimeError("provider body must not escape")
        return object()

    def public_status(self) -> dict[str, object]:
        return {"status": self.status}

    def available_context_tokens(self) -> int:
        if self.context_failure:
            raise RuntimeError("private provider detail")
        return cast(int, self.context_tokens)


def _primary_requirements() -> ModelRequirements:
    return ModelRequirements(
        capabilities=frozenset(
            {
                ModelCapability.CONTEXT_8K,
                ModelCapability.PREPARED_EVIDENCE_2,
                ModelCapability.REMOTE_CANCELLATION,
            }
        ),
        required_context_tokens=8_192,
        prepared_evidence_items=2,
        max_tool_steps=0,
        max_tool_rounds=0,
        max_tool_calls=0,
        effect=ModelEffect.READ,
        verifier_required=True,
    )


class _LeasedPrimary(_Primary):
    def __init__(self) -> None:
        super().__init__()
        self.requirements = _primary_requirements()
        self.lease = ModelProfileLease(
            profile_id="assist-ports-test:primary",
            attestation_sha256="a" * 64,
            requirements_sha256=self.requirements.canonical_sha256(),
            capabilities=self.requirements.capabilities,
            required_context_tokens=self.requirements.required_context_tokens,
            prepared_evidence_items=self.requirements.prepared_evidence_items,
            max_tool_steps=self.requirements.max_tool_steps,
            max_tool_rounds=self.requirements.max_tool_rounds,
            max_tool_calls=self.requirements.max_tool_calls,
            effect=self.requirements.effect,
            verifier_required=self.requirements.verifier_required,
            process_epoch_sha256="b" * 64,
            _gate_authority=self,
            _gate_generation=1,
        )
        self.current: object = True
        self.acquire_calls = 0
        self.current_calls = 0
        self.process_current_calls = 0
        self.complete_calls = 0
        self.hang_on: set[str] = set()

    async def _maybe_hang(self, operation: str) -> None:
        if operation in self.hang_on:
            await asyncio.Event().wait()

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease:
        assert requirements is self.requirements
        assert absolute_deadline > time.monotonic()
        self.acquire_calls += 1
        await self._maybe_hang("acquire")
        return self.lease

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> object:
        assert lease is self.lease and requirements is self.requirements
        assert absolute_deadline > time.monotonic()
        self.current_calls += 1
        await self._maybe_hang("current")
        return self.current

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> object:
        assert lease is self.lease and requirements is self.requirements
        self.process_current_calls += 1
        return self.current

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
        del messages, max_tokens, priority, temperature
        assert lease is self.lease and requirements is self.requirements
        assert absolute_deadline > time.monotonic()
        self.complete_calls += 1
        await self._maybe_hang("complete")
        return {"content": "ok", "finish_reason": "stop", "tool_calls": None}


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


@pytest.mark.parametrize(
    ("projected", "expected"),
    [(40_960, 40_960), (0, 0), (-1, 0), (True, 0), (1 << 63, 0)],
)
def test_primary_port_projects_only_strict_context_capacity(
    projected: object,
    expected: int,
) -> None:
    runtime = _Primary()
    runtime.context_tokens = projected
    model = ports.AttestedPrimaryModel(cast(Any, runtime))

    assert model.available_context_tokens() == expected

    runtime.context_failure = True
    assert model.available_context_tokens() == 0


@pytest.mark.asyncio
async def test_primary_port_binds_every_v2_lease_field_without_retry() -> None:
    runtime = _LeasedPrimary()
    model = ports.AttestedPrimaryModel(cast(Any, runtime))
    deadline = time.monotonic() + 5
    assert repr(model) == "AttestedPrimaryModel()"

    lease = await model.acquire_lease(runtime.requirements, absolute_deadline=deadline)
    assert lease is runtime.lease
    assert await model.lease_is_current(
        lease,
        runtime.requirements,
        absolute_deadline=deadline,
    )
    assert model.lease_is_process_current(lease, runtime.requirements)
    assert await model.complete(
        lease,
        runtime.requirements,
        [],
        max_tokens=1,
        priority="foreground",
        absolute_deadline=deadline,
    ) == {"content": "ok", "finish_reason": "stop", "tool_calls": None}
    assert (
        runtime.acquire_calls,
        runtime.current_calls,
        runtime.process_current_calls,
        runtime.complete_calls,
    ) == (1, 1, 1, 1)

    runtime.lease = replace(runtime.lease, max_tool_calls=1)
    assert (
        await model.acquire_lease(
            runtime.requirements,
            absolute_deadline=time.monotonic() + 5,
        )
        is None
    )
    assert runtime.acquire_calls == 2


@pytest.mark.asyncio
async def test_primary_port_closes_expiry_epoch_loss_and_non_boolean_currentness() -> None:
    runtime = _LeasedPrimary()
    model = ports.AttestedPrimaryModel(cast(Any, runtime))
    deadline = time.monotonic() + 5
    lease = await model.acquire_lease(runtime.requirements, absolute_deadline=deadline)
    assert lease is runtime.lease

    runtime.current = 1
    assert not await model.lease_is_current(
        lease,
        runtime.requirements,
        absolute_deadline=deadline,
    )
    assert not model.lease_is_process_current(lease, runtime.requirements)
    runtime.current = False  # exact runtime epoch/process revocation projection
    assert not await model.lease_is_current(
        lease,
        runtime.requirements,
        absolute_deadline=deadline,
    )
    checked = runtime.current_calls
    assert not await model.lease_is_current(
        lease,
        runtime.requirements,
        absolute_deadline=time.monotonic() - 1,
    )
    assert runtime.current_calls == checked


@pytest.mark.asyncio
async def test_primary_port_bounds_hostile_waits_and_preserves_cancellation() -> None:
    runtime = _LeasedPrimary()
    model = ports.AttestedPrimaryModel(cast(Any, runtime))

    runtime.hang_on.add("acquire")
    assert (
        await model.acquire_lease(
            runtime.requirements,
            absolute_deadline=time.monotonic() + 0.01,
        )
        is None
    )
    runtime.hang_on.clear()
    lease = await model.acquire_lease(
        runtime.requirements,
        absolute_deadline=time.monotonic() + 5,
    )
    assert lease is runtime.lease

    runtime.hang_on.add("current")
    assert not await model.lease_is_current(
        lease,
        runtime.requirements,
        absolute_deadline=time.monotonic() + 0.01,
    )
    runtime.hang_on = {"complete"}
    with pytest.raises(TimeoutError):
        await model.complete(
            lease,
            runtime.requirements,
            [],
            max_tokens=1,
            priority="foreground",
            absolute_deadline=time.monotonic() + 0.01,
        )

    task = asyncio.create_task(
        model.complete(
            lease,
            runtime.requirements,
            [],
            max_tokens=1,
            priority="foreground",
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_secondary_absence_is_a_bounded_none_without_primary_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ports, "build_supervisor_request", lambda *_args, **_kwargs: _request())
    monkeypatch.setattr(ports, "build_supervisor_review_request", lambda *_args, **_kwargs: _request())

    assert (
        await ports.SchedulerAssistPlanner(cast(Any, object())).propose(
            cast(Any, object()),
            cast(Any, object()),
            absolute_deadline=time.monotonic() + 5,
        )
        is None
    )
    assert (
        await ports.SchedulerAssistReviewer(cast(Any, object())).review(
            cast(Any, object()),
            absolute_deadline=time.monotonic() + 5,
        )
        is None
    )


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
    evaluator = ports.AssistPromotionEvaluator(material, cast(Any, object()))
    assert evaluator.decide(binding_snapshot=operational_capability_snapshot()) is None


def test_port_module_does_not_expose_storage_or_publication_handles() -> None:
    exported = set(ports.__all__)
    assert not any("storage" in item.casefold() or "publish" in item.casefold() for item in exported)
    assert ParsedSupervisorProposal.__module__ != ports.__name__
