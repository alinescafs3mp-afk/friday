from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, cast

import pytest

import friday.orchestration.supervisor_assist_ports as ports
from friday import execution_kernel as execution_kernel_module
from friday.execution_kernel import (
    bind_authenticated_request_effect_authority,
    track_request_effects,
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
        assert planner_scheduler.deadlines == [deadline]
        assert reviewer_scheduler.deadlines == [deadline]
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
