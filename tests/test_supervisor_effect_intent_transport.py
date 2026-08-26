from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.effect_outcome import EffectAction, EffectCapability
from friday.orchestration.supervisor_effect_intent import (
    EffectIntentReason,
    EffectIntentV1,
)
from friday.orchestration.supervisor_effect_intent_transport import (
    SUPERVISOR_EFFECT_INTENT_INPUT_SCHEMA,
    SupervisorEffectIntentTransportError,
    SupervisorEffectIntentTransportFailure,
    build_supervisor_effect_intent_request,
    describe_supervisor_effect_intent,
    parse_supervisor_effect_intent_result,
    supervisor_effect_intent_messages,
)
from friday.secondary_brain import (
    EffectClass,
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryResult,
)
from friday.secondary_brain.contracts import JsonValue
from friday.secondary_brain.profiles import SecondaryRuntimeProfile, get_secondary_runtime_profile


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


_MANIFEST = _digest("manifest")
_PROPOSAL = _digest("proposal")
_OTHER = _digest("other")


def _accepted_profile() -> SecondaryRuntimeProfile:
    profile = get_secondary_runtime_profile(semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID)
    if profile is None:
        raise RuntimeError("accepted secondary profile fixture is unavailable")
    return profile


_PROFILE = _accepted_profile()


def _intent(*, action: EffectAction = EffectAction.CREATE) -> EffectIntentV1:
    return EffectIntentV1(
        capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
        action=action,
        manifest_digest=_MANIFEST,
        proposal_digest=_PROPOSAL,
        reason=EffectIntentReason.EXPLICIT_USER_REQUEST,
    )


def _result(
    *,
    action: EffectAction = EffectAction.CREATE,
    raw: str | None = None,
    structured: dict[str, Any] | None = None,
    served_model_alias: str | None = None,
) -> SecondaryResult:
    intent = _intent(action=action)
    return SecondaryResult(
        visible_content=intent.to_json() if raw is None else raw,
        structured_output=cast(
            JsonValue,
            intent.to_payload() if structured is None else structured,
        ),
        served_model_alias=(
            _PROFILE.served_model_alias if served_model_alias is None else served_model_alias
        ),
    )


def _identity() -> dict[str, object]:
    return {
        "candidate_profile_id": _PROFILE.profile_id,
        "candidate_profile_mode": "assist",
        "candidate_profile_allow_private_text": True,
        "candidate_profile_context_tokens": _PROFILE.max_context_tokens,
        "candidate_profile_manifest_sha256": _PROFILE.manifest_sha256,
        "candidate_profile_admission": "accepted",
        "served_model_alias": _PROFILE.served_model_alias,
        "gateway_ca_certificate_sha256": _PROFILE.gateway_ca_certificate_sha256,
    }


def _diagnostics(*, requested_mode: str = "shadow") -> dict[str, object]:
    policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(requested_mode)
    return {
        "enabled": True,
        "configured": True,
        "mode": "assist",
        "state": "healthy",
        "available": True,
        "served_model_match": True,
        "profile": _PROFILE.profile_id,
        "profile_admission": "accepted",
        "profile_manifest_match": True,
        "probe_success_total": 7,
        "model_inventory_probe_success_total": 9,
        "semantic_supervisor": {
            "workload": ModelWorkload.PLAN_CANDIDATE.value,
            "requested_mode": requested_mode,
            "effective_mode": "shadow",
            "policy_id": policy.policy_id,
            "policy_sha256": policy.policy_sha256,
            "workload_available": True,
            "runtime_available": True,
            "closed_reason": "admitted",
        },
    }


class _Scheduler:
    def __init__(
        self,
        result: SecondaryResult | None = None,
        *,
        requested_mode: str = "shadow",
    ) -> None:
        self.alias = _PROFILE.served_model_alias
        self.identity = _identity()
        self.diagnostics = _diagnostics(requested_mode=requested_mode)
        self.result = result or _result()
        self.evaluate_calls = 0
        self.dispatches = 0
        self.requests: list[ModelRequest] = []
        self.mutate_before_dispatch: Callable[[_Scheduler], None] | None = None
        self.mutate_after_result: Callable[[_Scheduler], None] | None = None
        self.invoke_validator = True
        self.invoke_dispatch_observer = True
        self.wait_forever = False

    @property
    def served_model_alias(self) -> str:
        return self.alias

    def product_attestation_identity(self) -> Mapping[str, object]:
        return self.identity

    def diagnostics_status(self) -> Mapping[str, object]:
        return self.diagnostics

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt:
        self.evaluate_calls += 1
        self.requests.append(request)
        assert invalidate_on_rejection is False
        if self.mutate_before_dispatch is not None:
            self.mutate_before_dispatch(self)
        if pre_dispatch_validator is not None and not pre_dispatch_validator():
            return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
        if self.invoke_dispatch_observer and dispatch_observer is not None:
            dispatch_observer()
            self.dispatches += 1
        if self.wait_forever:
            await asyncio.Event().wait()
        accepted = True
        if self.invoke_validator and validator is not None:
            accepted = validator(self.result)
        if self.mutate_after_result is not None:
            self.mutate_after_result(self)
        if not accepted:
            return SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
        return SecondaryAttempt.success(self.result)


def _request(*, action: EffectAction = EffectAction.CREATE) -> ModelRequest:
    return build_supervisor_effect_intent_request(
        capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
        action=action,
        manifest_digest=_MANIFEST,
        proposal_digest=_PROPOSAL,
        absolute_deadline_monotonic=time.monotonic() + 10.0,
    )


async def _describe(
    scheduler: _Scheduler,
    *,
    action: EffectAction = EffectAction.CREATE,
    deadline: float | None = None,
) -> EffectIntentV1:
    return await describe_supervisor_effect_intent(
        scheduler,
        capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
        action=action,
        manifest_digest=_MANIFEST,
        proposal_digest=_PROPOSAL,
        absolute_deadline_monotonic=(time.monotonic() + 10.0 if deadline is None else deadline),
    )


def test_request_is_symbolic_effect_free_structured_and_bounded() -> None:
    request = _request()

    assert request.workload is ModelWorkload.PLAN_CANDIDATE
    assert request.priority is ModelPriority.BACKGROUND
    assert request.effect_class is EffectClass.NONE
    assert request.max_output_tokens == 256
    assert request.require_structured_output is True
    assert request.require_independent_model is True
    assert request.contains_private_text is False
    schema = request.structured_output_schema
    assert isinstance(schema, Mapping)
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    assert properties["capability"]["enum"] == [EffectCapability.OBSIDIAN_NOTE_MUTATION.value]
    assert properties["action"]["enum"] == [EffectAction.CREATE.value]
    assert properties["manifest_digest"]["enum"] == [_MANIFEST]
    assert properties["proposal_digest"]["enum"] == [_PROPOSAL]

    payload = json.loads(request.messages[1]["content"])
    assert payload["schema"] == SUPERVISOR_EFFECT_INTENT_INPUT_SCHEMA
    assert payload["requested_intent"] == {
        "capability": EffectCapability.OBSIDIAN_NOTE_MUTATION.value,
        "action": EffectAction.CREATE.value,
        "manifest_digest": _MANIFEST,
        "proposal_digest": _PROPOSAL,
    }
    serialized = json.dumps(request.messages, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) < 3_500
    for forbidden in (
        "/home/private/note.md",
        "private note body",
        "user-1234",
        "obsidian_create_note",
        "arguments",
        "permission_token",
        "tool_handle",
        "effect_handle",
        "publication_handle",
    ):
        assert forbidden not in serialized


def test_message_and_dispatch_apis_accept_no_body_or_authority_surface() -> None:
    assert set(inspect.signature(supervisor_effect_intent_messages).parameters) == {
        "capability",
        "action",
        "manifest_digest",
        "proposal_digest",
    }
    assert set(inspect.signature(describe_supervisor_effect_intent).parameters) == {
        "runtime",
        "capability",
        "action",
        "manifest_digest",
        "proposal_digest",
        "absolute_deadline_monotonic",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [EffectAction.CREATE, EffectAction.APPEND])
async def test_one_current_scheduler_call_returns_exact_untrusted_intent(
    action: EffectAction,
) -> None:
    scheduler = _Scheduler(_result(action=action))

    intent = await _describe(scheduler, action=action)

    assert type(intent) is EffectIntentV1
    assert intent == _intent(action=action)
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 1
    assert len(scheduler.requests) == 1
    request = scheduler.requests[0]
    response_schema = request.structured_output_schema
    assert isinstance(response_schema, Mapping)
    assert response_schema["properties"]["action"]["enum"] == [action.value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_mode", "expected_policy_id", "expected_policy_sha256"),
    [
        (
            "shadow",
            semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID,
            semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256,
        ),
        (
            "assist",
            semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
            semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
        ),
        (
            "canary",
            semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
            semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
        ),
    ],
)
async def test_transport_requires_the_mode_owned_non_authorizing_policy_identity(
    requested_mode: str,
    expected_policy_id: str,
    expected_policy_sha256: str,
) -> None:
    scheduler = _Scheduler(requested_mode=requested_mode)

    intent = await _describe(scheduler)

    assert intent == _intent()
    supervisor = scheduler.diagnostics["semantic_supervisor"]
    assert isinstance(supervisor, dict)
    assert supervisor["policy_id"] == expected_policy_id
    assert supervisor["policy_sha256"] == expected_policy_sha256
    assert supervisor["effective_mode"] == "shadow"
    assert scheduler.evaluate_calls == scheduler.dispatches == 1
    assert scheduler.requests[0].effect_class is EffectClass.NONE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_mode", "foreign_policy_id", "foreign_policy_sha256"),
    [
        (
            "shadow",
            semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
            semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
        ),
        (
            "assist",
            semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID,
            semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256,
        ),
        (
            "canary",
            semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID,
            semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256,
        ),
    ],
)
async def test_cross_mode_policy_identity_fails_before_model_call(
    requested_mode: str,
    foreign_policy_id: str,
    foreign_policy_sha256: str,
) -> None:
    scheduler = _Scheduler(requested_mode=requested_mode)
    supervisor = scheduler.diagnostics["semantic_supervisor"]
    assert isinstance(supervisor, dict)
    supervisor["policy_id"] = foreign_policy_id
    supervisor["policy_sha256"] = foreign_policy_sha256

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE
    assert scheduler.evaluate_calls == scheduler.dispatches == 0


def test_raw_structured_parity_is_exact() -> None:
    changed = _intent().to_payload()
    changed["reason"] = EffectIntentReason.DECLARED_PLAN_EFFECT.value
    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        parse_supervisor_effect_intent_result(
            _result(structured=changed),
            capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
            action=EffectAction.CREATE,
            manifest_digest=_MANIFEST,
            proposal_digest=_PROPOSAL,
        )
    assert raised.value.failure is SupervisorEffectIntentTransportFailure.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_async_transport_reports_schema_validated_model_rejection() -> None:
    changed = _intent().to_payload()
    changed["action"] = EffectAction.APPEND.value
    scheduler = _Scheduler(_result(structured=changed))

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.INVALID_RESPONSE
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 1


@pytest.mark.parametrize(
    "result",
    [
        _result(action=EffectAction.APPEND),
        _result(
            structured={
                **_intent().to_payload(),
                "arguments": {"path": "/private/note.md"},
            }
        ),
        _result(served_model_alias="forged-model"),
    ],
)
def test_model_cannot_widen_action_contract_or_runtime_identity(result: SecondaryResult) -> None:
    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        parse_supervisor_effect_intent_result(
            result,
            capability=EffectCapability.OBSIDIAN_NOTE_MUTATION,
            action=EffectAction.CREATE,
            manifest_digest=_MANIFEST,
            proposal_digest=_PROPOSAL,
        )
    assert raised.value.failure is SupervisorEffectIntentTransportFailure.INVALID_RESPONSE


def test_request_rejects_untyped_symbols_digests_and_deadlines() -> None:
    kwargs: dict[str, Any] = {
        "capability": EffectCapability.OBSIDIAN_NOTE_MUTATION,
        "action": EffectAction.CREATE,
        "manifest_digest": _MANIFEST,
        "proposal_digest": _PROPOSAL,
        "absolute_deadline_monotonic": time.monotonic() + 10.0,
    }
    for field, value in (
        ("capability", EffectCapability.OBSIDIAN_NOTE_MUTATION.value),
        ("action", EffectAction.CREATE.value),
        ("manifest_digest", "not-a-digest"),
        ("proposal_digest", "F" * 64),
        ("absolute_deadline_monotonic", float("nan")),
    ):
        changed = dict(kwargs)
        changed[field] = value
        with pytest.raises(SupervisorEffectIntentTransportError) as raised:
            build_supervisor_effect_intent_request(**changed)
        assert raised.value.failure is SupervisorEffectIntentTransportFailure.INVALID_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda runtime: runtime.identity.__setitem__("candidate_profile_id", "wrong-profile"),
        lambda runtime: runtime.identity.__setitem__("candidate_profile_manifest_sha256", _OTHER),
        lambda runtime: runtime.diagnostics.__setitem__("served_model_match", False),
        lambda runtime: runtime.diagnostics.__setitem__("profile_manifest_match", False),
        lambda runtime: runtime.diagnostics.__setitem__("probe_success_total", 0),
        lambda runtime: runtime.diagnostics.__setitem__("model_inventory_probe_success_total", 0),
        lambda runtime: runtime.diagnostics["semantic_supervisor"].__setitem__("runtime_available", False),
    ],
)
async def test_unaccepted_profile_alias_or_epoch_fails_before_call(
    mutation: Callable[[_Scheduler], None],
) -> None:
    scheduler = _Scheduler()
    mutation(scheduler)

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE
    assert scheduler.evaluate_calls == 0


@pytest.mark.asyncio
async def test_lease_drift_immediately_before_dispatch_fails_without_model_call() -> None:
    scheduler = _Scheduler()
    scheduler.mutate_before_dispatch = lambda runtime: runtime.diagnostics.__setitem__(
        "probe_success_total", 8
    )

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.RUNTIME_STALE
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 0


@pytest.mark.asyncio
async def test_policy_identity_drift_before_dispatch_fails_without_model_call() -> None:
    scheduler = _Scheduler(requested_mode="assist")

    def drift_policy(runtime: _Scheduler) -> None:
        supervisor = runtime.diagnostics["semantic_supervisor"]
        assert isinstance(supervisor, dict)
        supervisor["policy_id"] = semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID
        supervisor["policy_sha256"] = semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256

    scheduler.mutate_before_dispatch = drift_policy

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.RUNTIME_STALE
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda runtime: runtime.diagnostics.__setitem__("probe_success_total", 8),
        lambda runtime: setattr(runtime, "alias", "different-model"),
        lambda runtime: runtime.diagnostics["semantic_supervisor"].__setitem__("runtime_available", False),
    ],
)
async def test_epoch_alias_or_admission_drift_after_response_discards_intent(
    mutation: Callable[[_Scheduler], None],
) -> None:
    scheduler = _Scheduler()
    scheduler.mutate_after_result = mutation

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.RUNTIME_STALE
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 1


@pytest.mark.asyncio
async def test_policy_identity_drift_after_response_discards_intent() -> None:
    scheduler = _Scheduler(requested_mode="canary")

    def drift_policy(runtime: _Scheduler) -> None:
        supervisor = runtime.diagnostics["semantic_supervisor"]
        assert isinstance(supervisor, dict)
        supervisor["policy_sha256"] = _OTHER

    scheduler.mutate_after_result = drift_policy

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.RUNTIME_STALE
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 1


@pytest.mark.asyncio
async def test_expired_deadline_fails_before_scheduler_admission() -> None:
    scheduler = _Scheduler()

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler, deadline=time.monotonic() - 1.0)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED
    assert scheduler.evaluate_calls == 0


@pytest.mark.asyncio
async def test_inflight_deadline_cancels_the_only_attempt() -> None:
    scheduler = _Scheduler()
    scheduler.wait_forever = True

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler, deadline=time.monotonic() + 0.02)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED
    assert scheduler.evaluate_calls == 1
    assert scheduler.dispatches == 1


@pytest.mark.asyncio
async def test_runtime_cannot_skip_transport_validator_or_dispatch_witness() -> None:
    scheduler = _Scheduler()
    scheduler.invoke_validator = False

    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)

    assert raised.value.failure is SupervisorEffectIntentTransportFailure.INVALID_RESPONSE
    assert scheduler.evaluate_calls == 1

    scheduler = _Scheduler()
    scheduler.invoke_dispatch_observer = False
    with pytest.raises(SupervisorEffectIntentTransportError) as raised:
        await _describe(scheduler)
    assert raised.value.failure is SupervisorEffectIntentTransportFailure.INVALID_RESPONSE
    assert scheduler.evaluate_calls == 1


@pytest.mark.asyncio
async def test_caller_cancellation_is_control_flow_not_a_model_result() -> None:
    class _Cancelled(_Scheduler):
        async def evaluate_shadow(self, *args: Any, **kwargs: Any) -> SecondaryAttempt:
            self.evaluate_calls += 1
            raise asyncio.CancelledError

    scheduler = _Cancelled()
    with pytest.raises(asyncio.CancelledError):
        await _describe(scheduler)
    assert scheduler.evaluate_calls == 1
