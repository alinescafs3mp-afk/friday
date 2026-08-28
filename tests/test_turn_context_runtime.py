from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import itertools
import threading
from collections.abc import Callable

import pytest

import friday.orchestration.turn_context_runtime as runtime_seam
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
    authenticated_turn_entrypoint,
    bind_authenticated_turn_context,
    bind_authenticated_turn_context_async,
    current_authenticated_turn_context,
    current_primary_authenticated_turn_context,
    require_current_authenticated_turn_context,
    reserve_authenticated_advisory_call,
    suspend_authenticated_turn_context,
)
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_SERIALS = itertools.count(1)
_BASE_NOW_NS = 30_000_000_000_000
_CONVERSATION_ID = "conv_0123456789abcdef"


def _namespace(label: str) -> bytes:
    return hashlib.sha256(label.encode("ascii")).digest()


def _issuer(
    key: bytes,
    now: list[int] | Callable[[], int],
) -> TurnContextIssuer:
    clock = now if callable(now) else lambda: now[0]
    return TurnContextIssuer(key, _monotonic_ns=clock)


def _context(
    issuer: TurnContextIssuer,
    *,
    now_ns: int,
    deadline_delta_ns: int = 1_000_000_000,
    max_advisory_calls: int = 2,
    serial: int | None = None,
) -> AuthenticatedTurnContext:
    value = next(_SERIALS) if serial is None else serial
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
        session_id="owner-session",
        shared_tenant=False,
        person_id="",
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-runtime-{value}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.ENGINEER,
        source_id=f"source-runtime-{value}",
        update_id=f"update-runtime-{value}",
        request_effect_binding_sha256=hashlib.sha256(f"effect-runtime-{value}".encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message=f"runtime request {value}",
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
    return issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now_ns + deadline_delta_ns),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, max_advisory_calls, 8_192),
        ),
        pending_work_admission=None,
    )


def test_binding_keeps_exact_object_and_fences_nested_and_sequential_replay() -> None:
    now = [_BASE_NOW_NS]
    issuer = _issuer(_namespace("exact-binding"), now)
    context = _context(issuer, now_ns=now[0])
    another = _context(issuer, now_ns=now[0])
    canonical_clone = dataclasses.replace(context)
    renewed = issuer.authenticate_turn(
        authority=context.authority,
        model_input=context.model_input,
        authorized_sources=context.authorized_sources,
        turn_policy=context.turn_policy,
        inherited_budget=dataclasses.replace(
            context.inherited_budget,
            safety_deadline=TurnSafetyDeadline(now[0] + 2_000_000_000),
        ),
        pending_work_admission=context.pending_work_admission,
    )

    assert canonical_clone is not context
    assert canonical_clone.canonical_bytes() == context.canonical_bytes()
    assert renewed.turn_id == context.turn_id
    assert renewed.canonical_bytes() != context.canonical_bytes()
    assert current_authenticated_turn_context() is None
    assert current_primary_authenticated_turn_context() is None

    with bind_authenticated_turn_context(issuer, context) as admitted:
        assert admitted is context
        assert require_current_authenticated_turn_context(context) is context
        assert current_primary_authenticated_turn_context(context) is context
        with bind_authenticated_turn_context(issuer, context):
            assert current_authenticated_turn_context() is context
        with (
            pytest.raises(TurnContextError, match="cannot replace"),
            bind_authenticated_turn_context(issuer, another),
        ):
            pytest.fail("another turn replaced the active root")
        with (
            pytest.raises(TurnContextError, match="cannot replace"),
            bind_authenticated_turn_context(issuer, canonical_clone),
        ):
            pytest.fail("a canonical-equal copy replaced the exact object")
        with (
            pytest.raises(TurnContextError, match="cannot replace"),
            bind_authenticated_turn_context(issuer, renewed),
        ):
            pytest.fail("a changed context reused the active turn identity")

    assert current_authenticated_turn_context() is None
    with (
        pytest.raises(TurnContextError, match="already admitted"),
        bind_authenticated_turn_context(issuer, context),
    ):
        pytest.fail("a primary root was admitted twice")
    with (
        pytest.raises(TurnContextError, match="context changed"),
        bind_authenticated_turn_context(issuer, canonical_clone),
    ):
        pytest.fail("a canonical-equal reissue bypassed the replay fence")
    with (
        pytest.raises(TurnContextError, match="context changed"),
        bind_authenticated_turn_context(issuer, renewed),
    ):
        pytest.fail("a changed same-turn context bypassed the replay fence")


@pytest.mark.asyncio
async def test_equivalent_issuers_share_one_concurrent_and_sequential_root_fence() -> None:
    now = [_BASE_NOW_NS + 10_000]
    key = _namespace("equivalent-issuers")
    first = _issuer(key, now)
    equivalent = _issuer(key, now)
    context = _context(first, now_ns=now[0])
    assert equivalent.require_context(context) is context
    start = asyncio.Event()

    async def contender(issuer: TurnContextIssuer) -> bool:
        await start.wait()
        try:
            with bind_authenticated_turn_context(issuer, context):
                await asyncio.sleep(0)
                return True
        except TurnContextError:
            return False

    tasks = [asyncio.create_task(contender(first)), asyncio.create_task(contender(equivalent))]
    start.set()
    assert sorted(await asyncio.gather(*tasks)) == [False, True]

    with (
        pytest.raises(TurnContextError, match="already admitted"),
        bind_authenticated_turn_context(equivalent, context),
    ):
        pytest.fail("equivalent issuer replayed the root sequentially")


@pytest.mark.asyncio
async def test_suspension_removes_all_primary_authority_from_advisory_tasks() -> None:
    now = [_BASE_NOW_NS + 20_000]
    issuer = _issuer(_namespace("suspended-advisory"), now)
    context = _context(issuer, now_ns=now[0], max_advisory_calls=3)

    class Runtime:
        _turn_context_issuer = issuer

        @authenticated_turn_entrypoint
        async def effect(
            self,
            *,
            _authenticated_turn_context: AuthenticatedTurnContext | None = None,
        ) -> AuthenticatedTurnContext | None:
            return current_primary_authenticated_turn_context(_authenticated_turn_context)

    async def advisory() -> None:
        assert current_authenticated_turn_context() is None
        with pytest.raises(TurnContextError, match="primary authority"):
            current_primary_authenticated_turn_context()
        with pytest.raises(TurnContextError, match="no primary authority"):
            reserve_authenticated_advisory_call()
        with pytest.raises(TurnContextError, match="suspended"):
            await Runtime().effect(_authenticated_turn_context=context)

    with bind_authenticated_turn_context(issuer, context):
        assert reserve_authenticated_advisory_call(context) == 1
        with (
            pytest.raises(RuntimeError, match="advisory failed"),
            suspend_authenticated_turn_context(),
        ):
            raise RuntimeError("advisory failed")
        assert current_primary_authenticated_turn_context(context) is context
        with suspend_authenticated_turn_context():
            await asyncio.create_task(advisory())
        assert current_primary_authenticated_turn_context(context) is context


@pytest.mark.asyncio
async def test_shared_advisory_budget_is_atomic_and_zero_budget_fails_closed() -> None:
    now = [_BASE_NOW_NS + 30_000]
    issuer = _issuer(_namespace("shared-budget"), now)
    context = _context(issuer, now_ns=now[0], max_advisory_calls=2)

    async def reserve() -> int | str:
        await asyncio.sleep(0)
        try:
            return reserve_authenticated_advisory_call(context)
        except TurnContextError as exc:
            return str(exc)

    with bind_authenticated_turn_context(issuer, context):
        results = await asyncio.gather(*(asyncio.create_task(reserve()) for _ in range(3)))
    assert sorted(item for item in results if type(item) is int) == [1, 2]
    errors = [item for item in results if type(item) is str]
    assert errors == ["authenticated turn advisory budget is exhausted"]

    zero_now = [_BASE_NOW_NS + 31_000]
    zero_issuer = _issuer(_namespace("zero-budget"), zero_now)
    zero = _context(zero_issuer, now_ns=zero_now[0], max_advisory_calls=0)
    with (
        bind_authenticated_turn_context(zero_issuer, zero),
        pytest.raises(TurnContextError, match="exhausted"),
    ):
        reserve_authenticated_advisory_call(zero)


@pytest.mark.asyncio
async def test_detached_task_is_revoked_after_root_exit_and_exception_restores_context() -> None:
    now = [_BASE_NOW_NS + 40_000]
    issuer = _issuer(_namespace("detached-task"), now)
    context = _context(issuer, now_ns=now[0])
    release = asyncio.Event()

    async def detached() -> AuthenticatedTurnContext:
        await release.wait()
        return require_current_authenticated_turn_context(context)

    task: asyncio.Task[AuthenticatedTurnContext]
    with (
        pytest.raises(RuntimeError, match="primary failed"),
        bind_authenticated_turn_context(issuer, context),
    ):
        task = asyncio.create_task(detached())
        with suspend_authenticated_turn_context():
            assert current_authenticated_turn_context() is None
        assert current_authenticated_turn_context() is context
        raise RuntimeError("primary failed")

    assert current_authenticated_turn_context() is None
    release.set()
    with pytest.raises(TurnContextError, match="no longer active"):
        await task


@pytest.mark.asyncio
async def test_primary_cancellation_revokes_authority_in_its_detached_child() -> None:
    now = [_BASE_NOW_NS + 45_000]
    issuer = _issuer(_namespace("cancelled-primary"), now)
    context = _context(issuer, now_ns=now[0])
    entered = asyncio.Event()
    release_child = asyncio.Event()
    children: list[asyncio.Task[AuthenticatedTurnContext]] = []

    async def detached() -> AuthenticatedTurnContext:
        await release_child.wait()
        return require_current_authenticated_turn_context(context)

    async def primary() -> None:
        with bind_authenticated_turn_context(issuer, context):
            children.append(asyncio.create_task(detached()))
            entered.set()
            await asyncio.Future()

    root = asyncio.create_task(primary())
    await entered.wait()
    root.cancel()
    with pytest.raises(asyncio.CancelledError):
        await root
    release_child.set()
    assert len(children) == 1
    with pytest.raises(TurnContextError, match="no longer active"):
        await children[0]


@pytest.mark.asyncio
async def test_entrypoint_propagates_ambient_context_and_preserves_legacy_path() -> None:
    now = [_BASE_NOW_NS + 50_000]
    issuer = _issuer(_namespace("entrypoint"), now)
    context = _context(issuer, now_ns=now[0])

    class Runtime:
        _turn_context_issuer = issuer

        @authenticated_turn_entrypoint
        async def nested(
            self,
            *,
            _authenticated_turn_context: AuthenticatedTurnContext | None = None,
        ) -> AuthenticatedTurnContext:
            return require_current_authenticated_turn_context(_authenticated_turn_context)

        @authenticated_turn_entrypoint
        async def run(
            self,
            *,
            _authenticated_turn_context: AuthenticatedTurnContext | None = None,
        ) -> AuthenticatedTurnContext:
            assert _authenticated_turn_context is context
            return await self.nested()

        @authenticated_turn_entrypoint
        async def legacy(
            self,
            *,
            _authenticated_turn_context: AuthenticatedTurnContext | None = None,
        ) -> AuthenticatedTurnContext | None:
            assert _authenticated_turn_context is None
            return current_authenticated_turn_context()

    service = Runtime()
    assert await service.run(_authenticated_turn_context=context) is context
    assert await service.legacy() is None

    with pytest.raises(TypeError, match="keyword-only"):

        @authenticated_turn_entrypoint
        async def invalid(
            _authenticated_turn_context: AuthenticatedTurnContext | None = None,
        ) -> None:
            return None


@pytest.mark.asyncio
async def test_async_binding_spelling_rechecks_deadline() -> None:
    now = [_BASE_NOW_NS + 60_000]
    issuer = _issuer(_namespace("async-binding"), now)
    context = _context(issuer, now_ns=now[0], deadline_delta_ns=10)

    async with bind_authenticated_turn_context_async(issuer, context):
        assert require_current_authenticated_turn_context() is context
        now[0] += 10
        with pytest.raises(TurnContextError, match="deadline"):
            require_current_authenticated_turn_context()


def test_capacity_prunes_only_expired_inactive_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_seam, "_EXECUTION_LEDGERS", {})
    monkeypatch.setattr(runtime_seam, "_MAX_EXECUTION_LEDGERS", 2)
    first_now = [_BASE_NOW_NS + 70_000]
    second_now = [_BASE_NOW_NS + 70_000]
    third_now = [_BASE_NOW_NS + 70_000]
    first_issuer = _issuer(_namespace("capacity-first"), first_now)
    second_issuer = _issuer(_namespace("capacity-second"), second_now)
    third_issuer = _issuer(_namespace("capacity-third"), third_now)
    first = _context(first_issuer, now_ns=first_now[0], deadline_delta_ns=10)
    second = _context(second_issuer, now_ns=second_now[0])
    third = _context(third_issuer, now_ns=third_now[0])

    with bind_authenticated_turn_context(first_issuer, first):
        pass
    with bind_authenticated_turn_context(second_issuer, second):
        pass
    with (
        pytest.raises(TurnContextError, match="capacity"),
        bind_authenticated_turn_context(third_issuer, third),
    ):
        pytest.fail("a live replay fence was evicted")
    assert {ledger.context for ledger in runtime_seam._EXECUTION_LEDGERS.values()} == {
        first,
        second,
    }

    first_now[0] += 10
    with (
        pytest.raises(TurnContextError, match="deadline"),
        bind_authenticated_turn_context(first_issuer, first),
    ):
        pytest.fail("the exact expired context became replayable")
    with bind_authenticated_turn_context(third_issuer, third):
        pass
    retained = tuple(ledger.context for ledger in runtime_seam._EXECUTION_LEDGERS.values())
    assert first not in retained
    assert second in retained
    assert third in retained
    assert len(retained) == 2
    with (
        pytest.raises(TurnContextError, match="deadline"),
        bind_authenticated_turn_context(first_issuer, first),
    ):
        pytest.fail("pruning made an expired exact context admissible")


def test_capacity_never_evicts_an_active_fence_even_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_seam, "_EXECUTION_LEDGERS", {})
    monkeypatch.setattr(runtime_seam, "_MAX_EXECUTION_LEDGERS", 1)
    active_now = [_BASE_NOW_NS + 80_000]
    other_now = [_BASE_NOW_NS + 80_000]
    active_issuer = _issuer(_namespace("active-at-capacity"), active_now)
    other_issuer = _issuer(_namespace("other-at-capacity"), other_now)
    active = _context(active_issuer, now_ns=active_now[0], deadline_delta_ns=10)
    other = _context(other_issuer, now_ns=other_now[0])
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def hold_root() -> None:
        try:
            with bind_authenticated_turn_context(active_issuer, active):
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("test did not release active root")
        except BaseException as exc:  # noqa: BLE001 - propagated to the test thread
            failures.append(exc)

    thread = threading.Thread(target=hold_root)
    thread.start()
    assert entered.wait(timeout=5)
    active_now[0] += 10
    try:
        with (
            pytest.raises(TurnContextError, match="capacity"),
            bind_authenticated_turn_context(other_issuer, other),
        ):
            pytest.fail("an active expired fence was evicted")
        only = tuple(runtime_seam._EXECUTION_LEDGERS.values())
        assert len(only) == 1
        assert only[0].context is active
        assert only[0].active is True
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []


def test_failed_clock_and_wrong_issuer_do_not_prune_or_poison_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_seam, "_EXECUTION_LEDGERS", {})
    monkeypatch.setattr(runtime_seam, "_MAX_EXECUTION_LEDGERS", 1)
    failing = [False]
    fixed_now = _BASE_NOW_NS + 90_000

    def clock() -> int:
        if failing[0]:
            raise RuntimeError("clock unavailable")
        return fixed_now

    issuer = _issuer(_namespace("failed-clock"), clock)
    context = _context(issuer, now_ns=fixed_now)
    wrong = _issuer(_namespace("wrong-issuer"), [fixed_now])

    with (
        pytest.raises(TurnContextError, match="another issuer"),
        bind_authenticated_turn_context(wrong, context),
    ):
        pytest.fail("wrong issuer admitted a context")
    assert runtime_seam._EXECUTION_LEDGERS == {}

    with bind_authenticated_turn_context(issuer, context):
        pass
    failing[0] = True
    other_now = [fixed_now]
    other_issuer = _issuer(_namespace("failed-clock-other"), other_now)
    other = _context(other_issuer, now_ns=other_now[0])
    with (
        pytest.raises(TurnContextError, match="capacity"),
        bind_authenticated_turn_context(other_issuer, other),
    ):
        pytest.fail("a verifier failure evicted a replay fence")
    assert tuple(runtime_seam._EXECUTION_LEDGERS.values())[0].context is context


def test_integrity_failure_is_never_reclassified_as_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_seam, "_EXECUTION_LEDGERS", {})
    monkeypatch.setattr(runtime_seam, "_MAX_EXECUTION_LEDGERS", 1)
    now = [_BASE_NOW_NS + 100_000]
    issuer = _issuer(_namespace("integrity-failure"), now)
    context = _context(issuer, now_ns=now[0], deadline_delta_ns=10)
    with bind_authenticated_turn_context(issuer, context):
        pass

    now[0] += 10
    object.__setattr__(context, "context_authority_sha256", "0" * 64)
    other_now = [_BASE_NOW_NS + 100_000]
    other_issuer = _issuer(_namespace("integrity-failure-other"), other_now)
    other = _context(other_issuer, now_ns=other_now[0])
    with (
        pytest.raises(TurnContextError, match="capacity"),
        bind_authenticated_turn_context(other_issuer, other),
    ):
        pytest.fail("an integrity failure was pruned as ordinary expiry")
    assert tuple(runtime_seam._EXECUTION_LEDGERS.values())[0].context is context
