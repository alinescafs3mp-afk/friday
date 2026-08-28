from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from contextvars import copy_context

import pytest

from friday.execution_kernel import (
    ExecutionKernel,
    ToolSpec,
    bind_authenticated_request_effect_authority,
    confirm_staged_request_effect,
    mark_request_effect_possible,
    rollback_staged_request_effect,
    stage_request_effect_possible_in_transaction,
    track_request_effects,
)
from friday.orchestration.contracts import RouterMode, TurnInput
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
    suspend_authenticated_turn_context,
)
from friday.permissions import ActorContext, AuthorizationError
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_SERIALS = itertools.count(100_000)
_BASE_NOW_NS = 40_000_000_000_000
_CONVERSATION_ID = "conv_0123456789abcdef"
_AUTHORITY_ERROR = "Authenticated turn effect authority is unavailable"


class _AllowAll:
    def require(self, _actor: ActorContext, _security_id: str) -> None:
        return None

    def capability_requires_person(self, _security_id: str) -> bool:
        return False


def _authenticated_turn() -> tuple[
    TurnContextIssuer,
    AuthenticatedTurnContext,
    list[int],
    ActorContext,
]:
    serial = next(_SERIALS)
    now = [_BASE_NOW_NS + serial]
    issuer = TurnContextIssuer(
        hashlib.sha256(f"effect-guard-namespace-{serial}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now[0],
    )
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
        session_id="owner-session",
        shared_tenant=False,
        person_id="",
    )
    request_binding = hashlib.sha256(f"effect-guard-request-{serial}".encode("ascii")).hexdigest()
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-effect-guard-{serial}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.ENGINEER,
        source_id=f"source-effect-guard-{serial}",
        update_id=f"update-effect-guard-{serial}",
        request_effect_binding_sha256=request_binding,
    )
    model_input = TurnInput.from_chat(
        message=f"effect guard request {serial}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        attachments=[],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.ENGINEER.value,
        reply_to="",
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
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now[0] + 1_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, 1, 8_192),
        ),
        pending_work_admission=None,
    )
    return issuer, context, now, actor


def _kernel_tool(
    *,
    risk: str,
    handler: Callable[..., object],
    name: str,
) -> ExecutionKernel:
    kernel = ExecutionKernel(_AllowAll())  # type: ignore[arg-type]
    kernel.register(
        ToolSpec(
            name=name,
            description="Synthetic turn-context effect-boundary probe.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk=risk,
            handler=handler,  # type: ignore[arg-type]
        )
    )
    return kernel


@pytest.mark.asyncio
async def test_exact_live_request_effect_binding_allows_observing_handler() -> None:
    issuer, context, _now, actor = _authenticated_turn()
    calls = 0
    expected_actor = actor

    async def observe(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"same_actor": actor is expected_actor}

    kernel = _kernel_tool(risk="observe", handler=observe, name="guard_exact_observe")
    callback_calls = 0

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    with (
        track_request_effects(
            callback,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        result = await kernel.execute("guard_exact_observe", {}, actor=actor)

    assert result.success is True
    assert calls == 1
    assert callback_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tracked", [False, True])
async def test_missing_or_wrong_binding_has_zero_callback_and_handler_calls(tracked: bool) -> None:
    issuer, context, _now, actor = _authenticated_turn()
    handler_calls = 0
    callback_calls = 0

    async def mutate(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"changed": True}

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    kernel = _kernel_tool(risk="mutate", handler=mutate, name="guard_missing_mutate")
    if tracked:
        with (
            track_request_effects(callback, request_binding_sha256="f" * 64),
            bind_authenticated_turn_context(issuer, context),
        ):
            result = await kernel.execute("guard_missing_mutate", {}, actor=actor)
    else:
        with bind_authenticated_turn_context(issuer, context):
            result = await kernel.execute("guard_missing_mutate", {}, actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert callback_calls == 0
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_observing_tool_is_guarded_by_request_effect_binding() -> None:
    issuer, context, _now, actor = _authenticated_turn()
    handler_calls = 0

    async def observe(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"read": True}

    kernel = _kernel_tool(risk="observe", handler=observe, name="guard_wrong_observe")
    with (
        track_request_effects(lambda: True, request_binding_sha256="e" * 64),
        bind_authenticated_turn_context(issuer, context),
    ):
        result = await kernel.execute("guard_wrong_observe", {}, actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_same_digest_nested_request_effect_replacement_is_rejected() -> None:
    issuer, context, _now, actor = _authenticated_turn()
    callback_calls = 0
    handler_calls = 0

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    async def mutate(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"changed": True}

    kernel = _kernel_tool(risk="mutate", handler=mutate, name="guard_exact_identity")
    digest = context.effect_fence.request_effect_binding_sha256
    with (
        track_request_effects(callback, request_binding_sha256=digest) as original,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(original),
    ):
        with (
            pytest.raises(TurnContextError, match="nested or replaced"),
            bind_authenticated_request_effect_authority(original),
        ):
            pytest.fail("the exact authority was admitted twice")
        with track_request_effects(callback, request_binding_sha256=digest) as replacement:
            with (
                pytest.raises(TurnContextError, match="nested or replaced"),
                bind_authenticated_request_effect_authority(replacement),
            ):
                pytest.fail("an equal-digest witness replaced ingress authority")
            refused = await kernel.execute("guard_exact_identity", {}, actor=actor)
        accepted = await kernel.execute("guard_exact_identity", {}, actor=actor)

    assert refused.success is False
    assert refused.error == _AUTHORITY_ERROR
    assert accepted.success is True
    assert callback_calls == 1
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_suspended_authority_has_zero_callback_handler_or_approval_storage_calls() -> None:
    issuer, context, _now, actor = _authenticated_turn()
    handler_calls = 0
    callback_calls = 0

    async def mutate(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"changed": True}

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    kernel = _kernel_tool(risk="mutate", handler=mutate, name="guard_suspended_mutate")
    with (
        track_request_effects(
            callback,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
        suspend_authenticated_turn_context(),
    ):
        result = await kernel.execute("guard_suspended_mutate", {}, actor=actor)
        approved = await kernel.execute_approved("approval-never-read", actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert approved.success is False
    assert approved.error == _AUTHORITY_ERROR
    assert callback_calls == 0
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_approved_execution_requires_exact_live_request_effect_binding_before_storage() -> None:
    issuer, context, _now, actor = _authenticated_turn()
    kernel = ExecutionKernel(_AllowAll())  # type: ignore[arg-type]

    with (
        track_request_effects(lambda: True, request_binding_sha256="b" * 64),
        bind_authenticated_turn_context(issuer, context),
    ):
        result = await kernel.execute_approved("approval-never-read", actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR


@pytest.mark.asyncio
async def test_stale_authority_is_rejected_before_handler() -> None:
    issuer, context, now, actor = _authenticated_turn()
    handler_calls = 0

    async def observe(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"read": True}

    kernel = _kernel_tool(risk="observe", handler=observe, name="guard_stale_observe")
    with (
        track_request_effects(
            lambda: True,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        now[0] = context.inherited_budget.safety_deadline.monotonic_ns + 1
        result = await kernel.execute("guard_stale_observe", {}, actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert handler_calls == 0


def test_mark_and_stage_cannot_bypass_live_context_with_cached_flags_or_mismatch() -> None:
    issuer, context, _now, _actor = _authenticated_turn()
    callback_calls = 0

    def callback(_connection: object | None = None) -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    with (
        track_request_effects(callback, request_binding_sha256="d" * 64) as effects,
        bind_authenticated_turn_context(issuer, context),
    ):
        effects.possible = True
        effects.staged = True
        assert mark_request_effect_possible() is False
        assert stage_request_effect_possible_in_transaction(object()) is False
    exact_issuer, exact_context, _exact_now, _exact_actor = _authenticated_turn()
    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=callback,
            request_binding_sha256=exact_context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(exact_issuer, exact_context),
        bind_authenticated_request_effect_authority(effects),
    ):
        assert (
            stage_request_effect_possible_in_transaction(
                object(),
                expected_request_binding_sha256="c" * 64,
            )
            is False
        )

    assert callback_calls == 0


def test_confirm_and_rollback_require_exact_live_staged_authority() -> None:
    issuer, context, _now, _actor = _authenticated_turn()
    callback_calls = 0

    def stage_callback(_connection: object) -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    digest = context.effect_fence.request_effect_binding_sha256
    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=stage_callback,
            request_binding_sha256=digest,
        ) as original,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(original),
    ):
        confirm_staged_request_effect()
        assert original.possible is False
        assert stage_request_effect_possible_in_transaction(object()) is True
        assert original.staged is True

        with track_request_effects(lambda: True, request_binding_sha256=digest) as replacement:
            replacement.staged = True
            confirm_staged_request_effect()
            rollback_staged_request_effect()
            assert replacement.possible is False
            assert replacement.staged is True
            assert original.staged is True

        with suspend_authenticated_turn_context():
            confirm_staged_request_effect()
            rollback_staged_request_effect()
        assert original.possible is False
        assert original.staged is True

        confirm_staged_request_effect()
        assert original.possible is True
        assert original.staged is False

    assert callback_calls == 1


def test_inherited_request_effect_witness_is_revoked_when_its_scope_exits() -> None:
    issuer, context, _now, _actor = _authenticated_turn()
    callback_calls = 0

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    with (
        track_request_effects(
            callback,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
    ):
        with bind_authenticated_request_effect_authority(effects):
            inherited = copy_context()
        assert inherited.run(mark_request_effect_possible) is False

    assert callback_calls == 0


class _AuditSpyStorage:
    def __init__(self) -> None:
        self.audit_calls = 0

    def log_audit(self, _entry: object) -> None:
        self.audit_calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("deny", [False, True])
async def test_expiry_during_authorization_has_zero_later_audit_callback_or_handler(
    deny: bool,
) -> None:
    issuer, context, now, actor = _authenticated_turn()
    callback_calls = 0
    handler_calls = 0

    class _ExpireDuringAuthorization(_AllowAll):
        def require(self, _actor: ActorContext, _security_id: str) -> None:
            now[0] = context.inherited_budget.safety_deadline.monotonic_ns + 1
            if deny:
                raise AuthorizationError("denied after authority expiry")

    async def handler(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"called": True}

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    storage = _AuditSpyStorage()
    kernel = ExecutionKernel(_ExpireDuringAuthorization())  # type: ignore[arg-type]
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    tool_name = f"guard_authorization_expiry_{deny}"
    kernel.register(
        ToolSpec(
            name=tool_name,
            description="Expire authority during authorization.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe" if deny else "mutate",
            handler=handler,
        )
    )

    with (
        track_request_effects(
            callback,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        result = await kernel.execute(tool_name, {}, actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert storage.audit_calls == 0
    assert callback_calls == 0
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_request_effect_revocation_during_authorization_blocks_read_handler() -> None:
    issuer, context, _now, actor = _authenticated_turn()
    handler_calls = 0

    class _RevokeDuringAuthorization(_AllowAll):
        effects: object | None = None

        def require(self, _actor: ActorContext, _security_id: str) -> None:
            assert self.effects is not None
            self.effects.active = False  # type: ignore[attr-defined]

    authorization = _RevokeDuringAuthorization()

    async def observe(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"read": True}

    kernel = ExecutionKernel(authorization)  # type: ignore[arg-type]
    kernel.register(
        ToolSpec(
            name="guard_authorization_revocation",
            description="Revoke request authority during authorization.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe",
            handler=observe,
        )
    )
    with track_request_effects(
        lambda: True,
        request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
    ) as effects:
        authorization.effects = effects
        with (
            bind_authenticated_turn_context(issuer, context),
            bind_authenticated_request_effect_authority(effects),
        ):
            result = await kernel.execute("guard_authorization_revocation", {}, actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_expiry_during_approved_read_blocks_claim_audit_and_handler() -> None:
    issuer, context, now, actor = _authenticated_turn()
    handler_calls = 0
    callback_calls = 0

    class _ApprovalStorage(_AuditSpyStorage):
        def __init__(self) -> None:
            super().__init__()
            self.read_calls = 0
            self.claim_calls = 0

        def get_action_approval(self, _approval_id: str, _user_id: str) -> dict[str, object]:
            self.read_calls += 1
            now[0] = context.inherited_budget.safety_deadline.monotonic_ns + 1
            return {"tool": "guard_approved_race", "payload": {}}

        def claim_action_approval(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.claim_calls += 1
            return {"claimed": True}

    async def handler(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal handler_calls
        del actor
        handler_calls += 1
        return {"changed": True}

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return True

    storage = _ApprovalStorage()
    kernel = ExecutionKernel(_AllowAll())  # type: ignore[arg-type]
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    kernel.register(
        ToolSpec(
            name="guard_approved_race",
            description="Approved TOCTOU probe.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="mutate",
            handler=handler,
        )
    )

    with (
        track_request_effects(
            callback,
            request_binding_sha256=context.effect_fence.request_effect_binding_sha256,
        ) as effects,
        bind_authenticated_turn_context(issuer, context),
        bind_authenticated_request_effect_authority(effects),
    ):
        result = await kernel.execute_approved("approval-race", actor=actor)

    assert result.success is False
    assert result.error == _AUTHORITY_ERROR
    assert storage.read_calls == 1
    assert storage.claim_calls == 0
    assert storage.audit_calls == 0
    assert callback_calls == 0
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_legacy_untracked_observe_and_mutate_behavior_is_unchanged() -> None:
    actor = ActorContext(user_id="legacy", preset_key="owner", source="test")
    observe_calls = 0
    mutate_calls = 0

    async def observe(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal observe_calls
        del actor
        observe_calls += 1
        return {"read": True}

    async def mutate(*, actor: ActorContext) -> dict[str, bool]:
        nonlocal mutate_calls
        del actor
        mutate_calls += 1
        return {"changed": True}

    observe_kernel = _kernel_tool(risk="observe", handler=observe, name="legacy_guard_observe")
    mutate_kernel = _kernel_tool(risk="mutate", handler=mutate, name="legacy_guard_mutate")

    observed = await observe_kernel.execute("legacy_guard_observe", {}, actor=actor)
    mutated = await mutate_kernel.execute("legacy_guard_mutate", {}, actor=actor)

    assert observed.success is True
    assert mutated.success is True
    assert observe_calls == 1
    assert mutate_calls == 1
    assert mark_request_effect_possible() is True
    assert stage_request_effect_possible_in_transaction(object()) is True
    with track_request_effects(lambda: True) as legacy_effects:
        confirm_staged_request_effect()
        assert legacy_effects.possible is True
        legacy_effects.possible = False
        legacy_effects.staged = True
        rollback_staged_request_effect()
        assert legacy_effects.staged is False
