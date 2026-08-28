from __future__ import annotations

import asyncio
import hashlib
import itertools
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration import semantic_supervisor_runtime as supervisor_runtime_module
from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.router import OrchestrationRouter
from friday.orchestration.semantic_supervisor_runtime import SemanticSupervisorShadowRuntime
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.orchestration.turn_context_runtime import (
    bind_authenticated_turn_context,
    current_authenticated_turn_context,
    current_primary_authenticated_turn_context,
    suspend_authenticated_turn_context,
)
from friday.permissions import ActorContext
from friday.secondary_brain import SecondaryAttempt, SecondaryFailure
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_SERIALS = itertools.count(700_000)
_CONVERSATION_ID = "conv_abcdef0123456789"
_MESSAGE = "Сравни мой архив с актуальными правилами в интернете."


def _authenticated_turn(
    *,
    router_mode: RouterMode = RouterMode.SHADOW,
    max_advisory_calls: int = 1,
    clock: list[int] | None = None,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext, ActorContext, list[int]]:
    serial = next(_SERIALS)
    now = clock or [time.monotonic_ns()]
    issuer = TurnContextIssuer(
        hashlib.sha256(f"router-supervisor-{serial}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now[0],
    )
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
        session_id="owner-session",
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-router-supervisor-{serial}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id="api-token",
        update_id=f"update-router-supervisor-{serial}",
        request_effect_binding_sha256=hashlib.sha256(
            f"effects-router-supervisor-{serial}".encode("ascii")
        ).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message=_MESSAGE,
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    policy = issuer.issue_turn_policy(
        router_mode=router_mode,
        fallback_router_mode=(None if router_mode is RouterMode.LEGACY else RouterMode.LEGACY),
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now[0] + 5_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, max_advisory_calls, 8_192),
        ),
        pending_work_admission=None,
    )
    return issuer, context, actor, now


def _chat_kwargs(actor: ActorContext) -> dict[str, Any]:
    return {
        "actor": actor,
        "conversation_id": _CONVERSATION_ID,
        "attachments": None,
        "enable_tools": True,
    }


class _Primary:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] = {}
        self.calls = 0

    def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    async def chat(self, _user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.last_kwargs = kwargs
        return {"conversation_id": kwargs.get("conversation_id"), "message": "primary"}


class _Planner:
    def __init__(self) -> None:
        self.turns: list[TurnInput] = []
        self.detached_primary_closed = False

    async def plan(self, turn: TurnInput, **_kwargs: Any) -> None:
        self.turns.append(turn)
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        self.detached_primary_closed = True
        return None

    async def plan_attested(self, turn: TurnInput, **kwargs: Any) -> None:
        return await self.plan(turn, **kwargs)


class _Scheduler:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.calls = 0
        self.detached_primary_closed = False

    async def evaluate_shadow(self, _request: Any, **_kwargs: Any) -> SecondaryAttempt:
        self.calls += 1
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        self.detached_primary_closed = True
        if self.gate is not None:
            await self.gate.wait()
        return SecondaryAttempt.rejected(SecondaryFailure.TIMEOUT)


def _supervisor_settings() -> SimpleNamespace:
    return SimpleNamespace(
        semantic_supervisor_mode="shadow",
        semantic_supervisor_tasks=("compare_archive_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )


@pytest.mark.asyncio
async def test_router_carries_exact_context_and_exact_model_input_into_shadow() -> None:
    issuer, context, actor, _now = _authenticated_turn()
    primary = _Primary()
    planner = _Planner()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, planner), mode=RouterMode.SHADOW)

    with bind_authenticated_turn_context(issuer, context):
        result = await router.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor),
            _authenticated_turn_context=context,
        )
        assert current_primary_authenticated_turn_context(context) is context
    await router.drain_shadow()

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert planner.turns == [context.model_input]
    assert planner.turns[0] is context.model_input
    assert planner.detached_primary_closed is True


@pytest.mark.asyncio
async def test_router_fallback_carries_exact_context_without_rebuilding_turn() -> None:
    issuer, context, actor, _now = _authenticated_turn(router_mode=RouterMode.V12)
    primary = _Primary()
    planner = _Planner()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, planner), mode=RouterMode.V12)

    with bind_authenticated_turn_context(issuer, context):
        result = await router.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor),
            _authenticated_turn_context=context,
        )

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert planner.turns == [context.model_input]


@pytest.mark.asyncio
async def test_router_rejects_suspended_stale_and_identity_drift_before_primary() -> None:
    issuer, context, actor, now = _authenticated_turn()
    _other_issuer, other, _other_actor, _other_now = _authenticated_turn()
    primary = _Primary()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, _Planner()), mode=RouterMode.SHADOW)
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, primary),
        scheduler=cast(Any, _Scheduler()),
    )

    with bind_authenticated_turn_context(issuer, context):
        with suspend_authenticated_turn_context(), pytest.raises(
            TurnContextError, match="primary authority"
        ):
            await router.chat("owner", _MESSAGE, **_chat_kwargs(actor))
        with suspend_authenticated_turn_context(), pytest.raises(
            TurnContextError, match="primary authority"
        ):
            await wrapper.chat("owner", _MESSAGE, **_chat_kwargs(actor))
        with pytest.raises(TurnContextError, match="identity drifted"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor),
                _authenticated_turn_context=other,
            )
        with pytest.raises(TurnContextError, match="identity drifted"):
            await wrapper.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor),
                _authenticated_turn_context=other,
            )
        now[0] = context.inherited_budget.safety_deadline.monotonic_ns
        with pytest.raises(TurnContextError, match="deadline"):
            await router.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor),
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="deadline"):
            await wrapper.chat(
                "owner",
                _MESSAGE,
                **_chat_kwargs(actor),
                _authenticated_turn_context=context,
            )

    assert primary.calls == 0


@pytest.mark.asyncio
async def test_semantic_wrapper_uses_exact_turn_and_retains_no_authenticated_scope_carriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer, context, actor, _now = _authenticated_turn()
    primary = _Primary()
    gate = asyncio.Event()
    scheduler = _Scheduler(gate)
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, primary),
        scheduler=cast(Any, scheduler),
    )
    seen_turns: list[TurnInput] = []
    original = supervisor_runtime_module.build_supervisor_input

    def capture(turn: TurnInput, settings: object) -> Any:
        seen_turns.append(turn)
        return original(turn, settings)

    monkeypatch.setattr(supervisor_runtime_module, "build_supervisor_input", capture)

    with bind_authenticated_turn_context(issuer, context):
        result = await wrapper.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor),
            _authenticated_turn_context=context,
        )
        while scheduler.calls == 0:
            await asyncio.sleep(0)
        job = next(iter(wrapper._shadow_jobs.values()))
        assert job.prepared.turn is context.model_input
        assert job.prepared.actor is None
        assert job.prepared.routing_user_id is None
        assert job.prepared.conversation_id is None
        gate.set()
        await wrapper.drain_shadow()

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert seen_turns and all(turn is context.model_input for turn in seen_turns)
    assert scheduler.detached_primary_closed is True


@pytest.mark.asyncio
async def test_router_and_semantic_wrapper_share_one_authenticated_advisory_slot() -> None:
    issuer, context, actor, _now = _authenticated_turn(max_advisory_calls=1)
    primary = _Primary()
    planner = _Planner()
    router = OrchestrationRouter(cast(Any, primary), cast(Any, planner), mode=RouterMode.SHADOW)
    scheduler = _Scheduler()
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, router),
        scheduler=cast(Any, scheduler),
    )

    with bind_authenticated_turn_context(issuer, context):
        result = await wrapper.chat(
            "owner",
            _MESSAGE,
            **_chat_kwargs(actor),
            _authenticated_turn_context=context,
        )
        await router.drain_shadow()
        await wrapper.drain_shadow()

    assert result["message"] == "primary"
    assert primary.last_kwargs["_authenticated_turn_context"] is context
    assert planner.turns == [context.model_input]
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason.value == "saturated"


@pytest.mark.asyncio
async def test_no_context_preserves_private_kwarg_absence_for_both_wrappers() -> None:
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")
    router_primary = _Primary()
    router = OrchestrationRouter(
        cast(Any, router_primary), cast(Any, _Planner()), mode=RouterMode.LEGACY
    )

    await router.chat("owner", _MESSAGE, **_chat_kwargs(actor))
    assert "_authenticated_turn_context" not in router_primary.last_kwargs

    supervisor_primary = _Primary()
    wrapper = SemanticSupervisorShadowRuntime(
        settings=_supervisor_settings(),
        primary=cast(Any, supervisor_primary),
        scheduler=cast(Any, _Scheduler()),
    )
    await wrapper.chat("owner", _MESSAGE, **_chat_kwargs(actor))
    await wrapper.drain_shadow()
    assert "_authenticated_turn_context" not in supervisor_primary.last_kwargs
