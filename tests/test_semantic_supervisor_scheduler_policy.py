from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import httpx
import pytest

import friday.secondary_brain.profiles as profiles
from friday import semantic_supervisor_policy as policy
from friday.orchestration import supervisor_contracts
from friday.secondary_brain import (
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryMode,
    SecondaryResult,
    SecondaryState,
    build_secondary_brain,
)
from friday.secondary_brain.profiles import SecondaryProfileAdmission
from friday.secondary_brain.scheduler import SecondaryBrainScheduler

_API_KEY = "a" * 64
_TASK = "compare_current_file_with_current_web"


def _exact_loopback_settings(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requested_mode: str = "shadow",
    generic_workloads: tuple[str, ...] = (),
    tasks: tuple[object, ...] = (_TASK,),
    private: bool = True,
    **changes: Any,
) -> Any:
    accepted = profiles.ACCEPTED_SECONDARY_RUNTIME_PROFILES[policy.SUPERVISOR_RUNTIME_PROFILE_ID]
    loopback = replace(
        accepted,
        endpoint_base_url="http://127.0.0.1:30001/v1",
        gateway_ca_certificate_sha256="",
    )
    monkeypatch.setattr(
        profiles,
        "ACCEPTED_SECONDARY_RUNTIME_PROFILES",
        MappingProxyType({loopback.profile_id: loopback}),
    )
    configured = replace(
        settings,
        secondary_llm_enabled=True,
        secondary_llm_mode="assist",
        secondary_llm_base_url=loopback.endpoint_base_url,
        secondary_llm_model=loopback.served_model_alias,
        secondary_llm_api_key=_API_KEY,
        secondary_llm_ca_file="",
        secondary_llm_max_context_tokens=loopback.max_context_tokens,
        secondary_llm_max_concurrency=loopback.max_concurrency,
        secondary_llm_profile=loopback.profile_id,
        secondary_llm_workloads=generic_workloads,
        secondary_llm_allow_private_text=private,
        secondary_llm_document_map_mode="disabled",
        semantic_supervisor_mode=requested_mode,
        semantic_supervisor_tasks=tasks,
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=(
            1 if requested_mode in policy.SUPERVISOR_ASSIST_REQUESTED_MODES else 0
        ),
        semantic_supervisor_timeout_sec=12.0,
    )
    return replace(configured, **changes)


def _closed_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda _request: httpx.Response(503))


def _request() -> ModelRequest:
    return ModelRequest(
        workload=ModelWorkload.CLASSIFY,
        messages=({"role": "user", "content": "classify"},),
        max_output_tokens=32,
        absolute_deadline_monotonic=time.monotonic() + 5.0,
    )


def _plan_request() -> ModelRequest:
    return ModelRequest(
        workload=ModelWorkload.PLAN_CANDIDATE,
        messages=(
            {"role": "system", "content": "Return one JSON object."},
            {"role": "user", "content": "lowest-priority semantic plan"},
        ),
        max_output_tokens=64,
        absolute_deadline_monotonic=time.monotonic() + 5.0,
        priority=ModelPriority.BACKGROUND,
        require_structured_output=True,
        structured_output_schema={"type": "object"},
        contains_private_text=True,
    )


def _foreground_extract_request() -> ModelRequest:
    return ModelRequest(
        workload=ModelWorkload.EXTRACT,
        messages=({"role": "user", "content": "foreground accepted work"},),
        max_output_tokens=64,
        absolute_deadline_monotonic=time.monotonic() + 5.0,
        priority=ModelPriority.FOREGROUND,
        contains_private_text=True,
    )


def _chat_response(configured: Any, content: str, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "X-Friday-Secondary-Profile-Id": configured.secondary_llm_profile,
            "X-Friday-Secondary-Profile-Sha256": (
                profiles.ACCEPTED_SECONDARY_RUNTIME_PROFILES[configured.secondary_llm_profile].manifest_sha256
            ),
        },
        json={
            "model": configured.secondary_llm_model,
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _mark_epoch_fresh(scheduler: SecondaryBrainScheduler) -> None:
    scheduler._epoch_admitted = True  # noqa: SLF001 - isolate priority behavior from probes
    scheduler._last_probe_success_monotonic = time.monotonic()  # noqa: SLF001


def _unit_scheduler() -> SecondaryBrainScheduler:
    return SecondaryBrainScheduler(
        mode=SecondaryMode.SHADOW,
        allowed_workloads=frozenset({ModelWorkload.CLASSIFY}),
        allow_private_text=False,
        client=None,
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
    )


def _admission(**changes: Any) -> policy.SupervisorPolicyAdmission:
    values: dict[str, Any] = {
        "requested_mode": "shadow",
        "task_allowlist": (_TASK,),
        "max_steps": 6,
        "max_review_rounds": 0,
        "timeout_sec": 12.0,
        "allow_private_text": True,
        "secondary_runtime_state": "configured",
        "profile_admission": "accepted",
        "runtime_profile_id": policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256),
    }
    values.update(changes)
    return policy.evaluate_supervisor_policy_admission(**values)


def test_orchestration_and_scheduler_share_one_immutable_runtime_bound_policy() -> None:
    assert supervisor_contracts.SUPERVISOR_PRODUCT_POLICY is policy.SUPERVISOR_PRODUCT_POLICY
    assert (
        supervisor_contracts.SUPERVISOR_PRODUCT_POLICY_SHA256
        == policy.SUPERVISOR_PRODUCT_POLICY_SHA256
        == supervisor_contracts.canonical_sha256(dict(policy.SUPERVISOR_PRODUCT_POLICY))
    )
    assert policy.SUPERVISOR_PRODUCT_POLICY["runtime_profile_id"] == (policy.SUPERVISOR_RUNTIME_PROFILE_ID)
    assert policy.SUPERVISOR_PRODUCT_POLICY["runtime_profile_manifest_sha256"] == (
        policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
    )
    assert policy.SUPERVISOR_PRODUCT_POLICY["runtime_recertification"] is False
    with pytest.raises(TypeError):
        policy.SUPERVISOR_PRODUCT_POLICY["effective_mode"] = "assist"  # type: ignore[index]

    assert supervisor_contracts.SUPERVISOR_ASSIST_PRODUCT_POLICY is policy.SUPERVISOR_ASSIST_PRODUCT_POLICY
    assert (
        supervisor_contracts.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
        == policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
        == supervisor_contracts.canonical_sha256(dict(policy.SUPERVISOR_ASSIST_PRODUCT_POLICY))
    )
    assert policy.SUPERVISOR_ASSIST_PRODUCT_POLICY["max_review_rounds"] == 1
    assert policy.SUPERVISOR_PRODUCT_POLICY["max_review_rounds"] == 0
    with pytest.raises(TypeError):
        policy.SUPERVISOR_ASSIST_PRODUCT_POLICY["max_review_rounds"] = 0  # type: ignore[index]

    assert (
        supervisor_contracts.canonical_sha256(dict(policy.SUPERVISOR_EFFECT_SHADOW_POLICY))
        == policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256
    )
    assert policy.SUPERVISOR_EFFECT_SHADOW_POLICY["workload"] == "effect_planning"
    assert policy.SUPERVISOR_EFFECT_SHADOW_POLICY["contains_private_text"] is True
    assert policy.SUPERVISOR_EFFECT_SHADOW_POLICY["effects_allowed"] is False
    assert policy.SUPERVISOR_EFFECT_SHADOW_POLICY["publication_allowed"] is False
    with pytest.raises(TypeError):
        policy.SUPERVISOR_EFFECT_SHADOW_POLICY["effects_allowed"] = True  # type: ignore[index]


@pytest.mark.parametrize(
    ("private", "available", "closed_reason"),
    ((True, True, "admitted"), (False, False, "private_text_required")),
)
def test_effect_shadow_has_an_independent_private_capable_scheduler_lane(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    private: bool,
    available: bool,
    closed_reason: str,
) -> None:
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        requested_mode="off",
        generic_workloads=(),
        private=private,
        semantic_supervisor_effect_mode="shadow",
    )
    assert configured.secondary_llm_configured is available
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert (ModelWorkload.EFFECT_PLANNING in scheduler.allowed_workloads) is available
        assert scheduler.workload_mode(ModelWorkload.PLAN_CANDIDATE) is SecondaryMode.DISABLED
        assert scheduler.workload_mode(ModelWorkload.EFFECT_PLANNING) is (
            SecondaryMode.SHADOW if available else SecondaryMode.DISABLED
        )
        effect = scheduler.public_status()["effect_shadow"]
        assert effect == {
            "workload": "effect_planning",
            "requested_mode": "shadow",
            "effective_mode": "shadow" if available else "off",
            "policy_id": policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
            "policy_sha256": policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
            "workload_available": available,
            "runtime_available": False,
            "closed_reason": closed_reason,
        }
        assert scheduler.diagnostics_status()["effect_shadow"] == effect
    finally:
        asyncio.run(scheduler.aclose())


@pytest.mark.parametrize("requested_mode", ["shadow", "assist", "canary"])
def test_semantic_only_scheduler_auto_admits_plan_candidate_in_shadow(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    requested_mode: str,
) -> None:
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        requested_mode=requested_mode,
        generic_workloads=(),
    )
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert configured.secondary_llm_workloads == ()
        assert scheduler.allowed_workloads == frozenset({ModelWorkload.PLAN_CANDIDATE})
        assert scheduler.workload_mode(ModelWorkload.PLAN_CANDIDATE) is SecondaryMode.SHADOW
        public = scheduler.public_status()
        supervisor = public["semantic_supervisor"]
        identity = policy.supervisor_product_policy_identity_for_mode(requested_mode)
        assert supervisor == {
            "workload": "plan_candidate",
            "requested_mode": requested_mode,
            "effective_mode": "shadow",
            "policy_id": identity.policy_id,
            "policy_sha256": identity.policy_sha256,
            "workload_available": True,
            "runtime_available": False,
            "closed_reason": "admitted",
        }
        diagnostics = scheduler.diagnostics_status()
        assert diagnostics["semantic_supervisor"] == supervisor
        assert diagnostics["workloads"]["plan_candidate"]["routing_mode"] == "shadow"
        assert diagnostics["workloads"]["plan_candidate"]["available"] is True
    finally:
        asyncio.run(scheduler.aclose())


def test_assist_policy_requires_its_exact_review_budget_and_narrow_task() -> None:
    admitted = _admission(
        requested_mode="assist",
        max_review_rounds=1,
    )
    assert admitted.workload_available is True
    assert admitted.effective_mode == "shadow"
    assert admitted.policy_id == policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
    assert admitted.policy_sha256 == policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256

    wrong_budget = _admission(requested_mode="assist", max_review_rounds=0)
    assert wrong_budget.closed_reason is policy.SupervisorPolicyClosedReason.INVALID_BOUNDS
    widened_task = _admission(
        requested_mode="assist",
        max_review_rounds=1,
        task_allowlist=("compare_archive_with_current_web",),
    )
    assert widened_task.closed_reason is policy.SupervisorPolicyClosedReason.TASK_ALLOWLIST_INVALID


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"task_allowlist": ()}, "task_allowlist_empty"),
        (
            {"task_allowlist": (_TASK, "unknown_task")},
            "task_allowlist_invalid",
        ),
        ({"task_allowlist": (_TASK, _TASK)}, "task_allowlist_invalid"),
        ({"task_allowlist": (_TASK, 7)}, "task_allowlist_invalid"),
        ({"max_steps": 0}, "invalid_bounds"),
        ({"max_steps": 1}, "invalid_bounds"),
        ({"max_steps": 2}, "invalid_bounds"),
        ({"max_steps": 5}, "invalid_bounds"),
        ({"max_steps": True}, "invalid_bounds"),
        ({"max_review_rounds": 1}, "invalid_bounds"),
        ({"max_review_rounds": 2}, "invalid_bounds"),
        ({"timeout_sec": float("nan")}, "invalid_bounds"),
        ({"timeout_sec": 15.1}, "invalid_bounds"),
        ({"allow_private_text": False}, "private_text_required"),
        ({"profile_admission": "provisional_shadow"}, "accepted_profile_required"),
        ({"runtime_profile_manifest_sha256": "0" * 64}, "runtime_profile_mismatch"),
    ],
)
def test_supervisor_policy_rejections_are_closed_and_effective_mode_is_off(
    changes: dict[str, Any],
    reason: str,
) -> None:
    admission = _admission(**changes)
    assert admission.requested_mode == "shadow"
    assert admission.effective_mode == "off"
    assert admission.workload_available is False
    assert admission.closed_reason.value == reason


def test_invalid_supervisor_config_does_not_poison_generic_extract(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        generic_workloads=("extract",),
        tasks=(_TASK, "unknown_task"),
    )
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert scheduler.served_model_alias == configured.secondary_llm_model
        assert ModelWorkload.EXTRACT in scheduler.allowed_workloads
        assert ModelWorkload.PLAN_CANDIDATE not in scheduler.allowed_workloads
        supervisor = scheduler.public_status()["semantic_supervisor"]
        assert supervisor["requested_mode"] == "shadow"
        assert supervisor["effective_mode"] == "off"
        assert supervisor["workload_available"] is False
        assert supervisor["closed_reason"] == "task_allowlist_invalid"
        assert scheduler.status().state is SecondaryState.PROBING
    finally:
        asyncio.run(scheduler.aclose())


@pytest.mark.parametrize(
    "changes",
    (
        {"semantic_supervisor_max_steps": 1},
        {"semantic_supervisor_max_steps": 2},
        {"semantic_supervisor_max_review_rounds": 1},
    ),
)
def test_manual_unsupported_p1_bounds_never_install_plan_candidate(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, int],
) -> None:
    configured = _exact_loopback_settings(settings, monkeypatch, **changes)
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert ModelWorkload.PLAN_CANDIDATE not in scheduler.allowed_workloads
        supervisor = scheduler.public_status()["semantic_supervisor"]
        assert supervisor["effective_mode"] == "off"
        assert supervisor["workload_available"] is False
        assert supervisor["closed_reason"] == "invalid_bounds"
    finally:
        asyncio.run(scheduler.aclose())


@pytest.mark.parametrize(
    ("env_name", "raw", "attribute"),
    (
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
            "999",
            "semantic_supervisor_max_steps",
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
            "not-an-int",
            "semantic_supervisor_max_steps",
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS",
            "-1",
            "semantic_supervisor_max_review_rounds",
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            "nan",
            "semantic_supervisor_timeout_sec",
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            "inf",
            "semantic_supervisor_timeout_sec",
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            "15.1",
            "semantic_supervisor_timeout_sec",
        ),
    ),
)
def test_invalid_loaded_p1_bounds_never_install_plan_candidate(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    raw: str,
    attribute: str,
) -> None:
    monkeypatch.setenv(env_name, raw)
    from friday.config import load_settings

    loaded = load_settings()
    configured = _exact_loopback_settings(
        loaded,
        monkeypatch,
        **{attribute: getattr(loaded, attribute)},
    )
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert ModelWorkload.PLAN_CANDIDATE not in scheduler.allowed_workloads
        supervisor = scheduler.public_status()["semantic_supervisor"]
        assert supervisor["effective_mode"] == "off"
        assert supervisor["workload_available"] is False
        assert supervisor["closed_reason"] == "invalid_bounds"
    finally:
        asyncio.run(scheduler.aclose())


def test_invalid_supervisor_policy_does_not_poison_generic_extract(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        generic_workloads=("extract",),
    )
    monkeypatch.setattr(
        policy,
        "SUPERVISOR_PRODUCT_POLICY",
        MappingProxyType({**policy.SUPERVISOR_PRODUCT_POLICY, "max_steps": 5}),
    )
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert scheduler.served_model_alias == configured.secondary_llm_model
        assert scheduler.allowed_workloads == frozenset({ModelWorkload.EXTRACT})
        supervisor = scheduler.public_status()["semantic_supervisor"]
        assert supervisor["effective_mode"] == "off"
        assert supervisor["closed_reason"] == "policy_invalid"
    finally:
        asyncio.run(scheduler.aclose())


def test_mixed_invalid_generic_workloads_remain_fail_closed_but_do_not_block_supervisor(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        generic_workloads=("extract", "unknown_workload"),
    )
    scheduler = build_secondary_brain(configured, transport=_closed_transport())
    try:
        assert scheduler.allowed_workloads == frozenset({ModelWorkload.PLAN_CANDIDATE})
        assert scheduler.workload_mode(ModelWorkload.PLAN_CANDIDATE) is SecondaryMode.SHADOW
        assert scheduler.public_status()["semantic_supervisor"]["closed_reason"] == "admitted"
    finally:
        asyncio.run(scheduler.aclose())


@pytest.mark.asyncio
async def test_evaluate_shadow_returns_one_accounted_attempt_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SecondaryResult(visible_content="discarded", latency_sec=0.25)
    transport_attempt = SecondaryAttempt.success(result, queue_wait_sec=0.125)
    calls: list[bool] = []
    scheduler = _unit_scheduler()

    async def attempt(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        calls.append(shadow)
        return transport_attempt

    monkeypatch.setattr(scheduler, "attempt", attempt)
    returned = await scheduler.evaluate_shadow(_request(), validator=lambda _result: True)
    assert returned is transport_attempt
    assert calls == [True]
    diagnostics = scheduler.diagnostics_status()
    assert diagnostics["shadow"]["valid_total"] == 1
    assert diagnostics["workloads"]["classify"]["success_total"] == 1

    invalid = await scheduler.evaluate_shadow(_request(), validator=lambda _result: False)
    assert invalid.failure is SecondaryFailure.MALFORMED_RESPONSE
    assert calls == [True, True]
    assert scheduler.diagnostics_status()["shadow"] == {
        "valid_total": 1,
        "invalid_total": 1,
        "skipped_total": 0,
        "in_flight": 0,
    }

    skipped_attempt = SecondaryAttempt.rejected(SecondaryFailure.TIMEOUT)

    async def skipped(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        calls.append(shadow)
        return skipped_attempt

    monkeypatch.setattr(scheduler, "attempt", skipped)
    skipped_result = await scheduler.evaluate_shadow(_request())
    assert skipped_result is skipped_attempt
    assert calls == [True, True, True]
    assert scheduler.diagnostics_status()["shadow"] == {
        "valid_total": 1,
        "invalid_total": 1,
        "skipped_total": 1,
        "in_flight": 0,
    }


@pytest.mark.asyncio
async def test_semantic_policy_rejection_does_not_invalidate_shared_secondary_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _unit_scheduler()
    result = SecondaryResult(visible_content="discarded")
    invalidations = 0

    async def attempt(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        return SecondaryAttempt.success(result)

    async def reject(_request: ModelRequest, *_args: Any, **_kwargs: Any) -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(scheduler, "attempt", attempt)
    monkeypatch.setattr(scheduler, "_reject_valid_result", reject)
    rejected = await scheduler.evaluate_shadow(
        _request(),
        validator=lambda _result: False,
        invalidate_on_rejection=False,
    )
    assert rejected.failure is SecondaryFailure.MALFORMED_RESPONSE
    assert invalidations == 0
    assert scheduler.diagnostics_status()["shadow"]["invalid_total"] == 1


@pytest.mark.asyncio
async def test_evaluate_shadow_closes_unexpected_errors_but_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _unit_scheduler()

    async def broken(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        raise RuntimeError("optional adapter failed")

    monkeypatch.setattr(scheduler, "attempt", broken)
    closed = await scheduler.evaluate_shadow(_request())
    assert closed.failure is SecondaryFailure.MALFORMED_RESPONSE
    assert scheduler.diagnostics_status()["shadow"]["invalid_total"] == 1

    async def cancelled(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "attempt", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.evaluate_shadow(_request())


@pytest.mark.asyncio
async def test_foreground_work_preempts_semantic_plan_without_admission_busy(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_entered = asyncio.Event()
    plan_cancelled = asyncio.Event()
    never_release = asyncio.Event()
    posts: list[str] = []
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        generic_workloads=("extract",),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        content = payload["messages"][-1]["content"]
        posts.append(content)
        if content == "lowest-priority semantic plan":
            plan_entered.set()
            try:
                await never_release.wait()
            finally:
                plan_cancelled.set()
            return _chat_response(configured, '{"plan":"discarded"}')
        return _chat_response(configured, "foreground result")

    scheduler = build_secondary_brain(configured, transport=httpx.MockTransport(handler))
    _mark_epoch_fresh(scheduler)
    plan = asyncio.create_task(scheduler.evaluate_shadow(_plan_request(), validator=lambda _result: True))
    try:
        await asyncio.wait_for(plan_entered.wait(), timeout=0.5)
        foreground = await scheduler.attempt(_foreground_extract_request())
        displaced = await plan

        assert foreground.result is not None
        assert foreground.result.visible_content == "foreground result"
        assert displaced.failure is SecondaryFailure.CANCELLED
        assert plan_cancelled.is_set()
        assert posts == ["lowest-priority semantic plan", "foreground accepted work"]
        assert scheduler.status().state is SecondaryState.HEALTHY
        assert scheduler.diagnostics_status()["workloads"]["extract"]["skip_reasons"] == {}
    finally:
        never_release.set()
        await asyncio.gather(plan, return_exceptions=True)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_two_foreground_preemptors_cancel_semantic_once_and_release_permit(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_entered = asyncio.Event()
    never_release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    posts: list[str] = []
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        generic_workloads=("extract",),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        content = payload["messages"][-1]["content"]
        posts.append(content)
        if content == "lowest-priority semantic plan":
            plan_entered.set()
            await never_release.wait()
            return _chat_response(configured, '{"plan":"discarded"}')
        return _chat_response(configured, "foreground result")

    scheduler = build_secondary_brain(configured, transport=httpx.MockTransport(handler))
    _mark_epoch_fresh(scheduler)
    client = scheduler._client  # noqa: SLF001 - exercise the real shared permit cleanup
    assert client is not None
    original_finish = client._finish  # noqa: SLF001

    async def gated_finish(
        failure: SecondaryFailure | None,
        *,
        failure_scope_workload: ModelWorkload | None = None,
        cancellation_is_local: bool = False,
    ) -> None:
        if failure is SecondaryFailure.CANCELLED and failure_scope_workload is ModelWorkload.PLAN_CANDIDATE:
            cleanup_entered.set()
            await release_cleanup.wait()
        await original_finish(
            failure,
            failure_scope_workload=failure_scope_workload,
            cancellation_is_local=cancellation_is_local,
        )

    monkeypatch.setattr(client, "_finish", gated_finish)
    plan = asyncio.create_task(scheduler.evaluate_shadow(_plan_request()))
    first: asyncio.Task[SecondaryAttempt] | None = None
    second: asyncio.Task[SecondaryAttempt] | None = None
    try:
        await asyncio.wait_for(plan_entered.wait(), timeout=0.5)
        first = asyncio.create_task(scheduler.attempt(_foreground_extract_request()))
        await asyncio.wait_for(cleanup_entered.wait(), timeout=0.5)
        second = asyncio.create_task(scheduler.attempt(_foreground_extract_request()))
        await asyncio.sleep(0)
        assert second.done() is False
        release_cleanup.set()

        results = await asyncio.gather(first, second)
        displaced = await plan
        assert displaced.failure is SecondaryFailure.CANCELLED
        assert any(item.result is not None for item in results)
        assert all(
            item.result is not None or item.failure is SecondaryFailure.ADMISSION_BUSY for item in results
        )
        assert client.status().active_requests == 0
        assert client._semaphore.locked() is False  # noqa: SLF001

        after = await scheduler.attempt(_foreground_extract_request())
        assert after.result is not None
        assert client.status().active_requests == 0
        assert posts.count("lowest-priority semantic plan") == 1
    finally:
        never_release.set()
        release_cleanup.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await asyncio.gather(plan, return_exceptions=True)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_strand_priority_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _unit_scheduler()
    entered = asyncio.Event()

    async def blocked_observed(
        request: ModelRequest,
        *,
        shadow: bool,
        pre_dispatch_validator: Any,
    ) -> SecondaryAttempt:
        del shadow, pre_dispatch_validator
        if request.workload is ModelWorkload.PLAN_CANDIDATE:
            entered.set()
        else:
            entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(scheduler, "_attempt_observed", blocked_observed)

    foreground = asyncio.create_task(scheduler.attempt(_request()))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    await scheduler._plan_priority_lock.acquire()  # noqa: SLF001
    try:
        foreground.cancel()
        await asyncio.sleep(0)
        foreground.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreground
    finally:
        scheduler._plan_priority_lock.release()  # noqa: SLF001
    assert scheduler._non_plan_attempts_in_flight == 0  # noqa: SLF001

    entered.clear()
    plan = asyncio.create_task(scheduler.attempt(_plan_request(), shadow=True))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    await scheduler._plan_priority_lock.acquire()  # noqa: SLF001
    try:
        plan.cancel()
        await asyncio.sleep(0)
        plan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await plan
    finally:
        scheduler._plan_priority_lock.release()  # noqa: SLF001
    assert scheduler._plan_candidate_attempts == set()  # noqa: SLF001
    assert scheduler._preempted_plan_attempts == set()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason", "expected_failure"),
    [
        ("not json", "stop", SecondaryFailure.MALFORMED_RESPONSE),
        ('{"partial":true}', "length", SecondaryFailure.MALFORMED_RESPONSE),
        ("<think>hidden</think>{}", "stop", SecondaryFailure.REASONING_LEAK),
    ],
)
async def test_semantic_protocol_quality_failure_does_not_poison_shared_runtime(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    finish_reason: str,
    expected_failure: SecondaryFailure,
) -> None:
    calls = 0
    configured = _exact_loopback_settings(
        settings,
        monkeypatch,
        generic_workloads=("extract",),
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _chat_response(configured, content, finish_reason=finish_reason)
        return _chat_response(configured, "foreground result")

    scheduler = build_secondary_brain(configured, transport=httpx.MockTransport(handler))
    _mark_epoch_fresh(scheduler)
    try:
        rejected = await scheduler.evaluate_shadow(_plan_request())
        assert rejected.failure is expected_failure
        assert scheduler._epoch_admitted is True  # noqa: SLF001
        assert scheduler.status().state is not SecondaryState.COOLDOWN

        foreground = await scheduler.attempt(_foreground_extract_request())
        assert foreground.result is not None
        assert foreground.result.visible_content == "foreground result"
        assert calls == 2
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_late_dispatch_guard_rejects_after_endpoint_admission_without_http_post(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = 0
    configured = _exact_loopback_settings(settings, monkeypatch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _chat_response(configured, '{"plan":"must-not-be-seen"}')

    scheduler = build_secondary_brain(configured, transport=httpx.MockTransport(handler))
    _mark_epoch_fresh(scheduler)
    try:
        rejected = await scheduler.evaluate_shadow(
            _plan_request(),
            pre_dispatch_validator=lambda: False,
        )
        assert rejected.failure is SecondaryFailure.CANCELLED
        assert posts == 0
        assert scheduler._epoch_admitted is True  # noqa: SLF001
        assert scheduler.status().state is not SecondaryState.COOLDOWN
        diagnostics = scheduler.diagnostics_status()
        assert diagnostics["endpoint_admission_total"] == 1
        assert diagnostics["endpoint_request_total"] == 0
        assert diagnostics["endpoint_success_total"] == 0
    finally:
        await scheduler.aclose()
