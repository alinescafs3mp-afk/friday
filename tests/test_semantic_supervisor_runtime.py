from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration import semantic_supervisor_runtime as runtime_module
from friday.orchestration.contracts import TurnInput
from friday.orchestration.semantic_supervisor import build_supervisor_input
from friday.orchestration.semantic_supervisor_runtime import (
    SemanticSupervisorShadowRuntime,
    build_semantic_supervisor_runtime,
)
from friday.orchestration.supervisor_contracts import (
    FILE_CURRENT_READ_ID,
    PRIMARY_SYNTHESIS_ID,
    SUPERVISOR_PROPOSAL_SCHEMA,
    WEB_SEARCH_CURRENT_ID,
    canonical_sha256,
)
from friday.orchestration.supervisor_observation import SupervisorSkipReason
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.secondary_brain import (
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryResult,
)


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "semantic_supervisor_mode": "shadow",
        "semantic_supervisor_tasks": ("compare_current_file_with_current_web",),
        "semantic_supervisor_max_steps": 6,
        "semantic_supervisor_max_review_rounds": 0,
        "semantic_supervisor_timeout_sec": 12.0,
        "secondary_llm_profile": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _actor() -> ActorContext:
    return ActorContext(
        user_id="person_private_7f",
        preset_key="owner",
        source="runtime-test",
    )


def _chat_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "actor": _actor(),
        "conversation_id": "conv_34a31c84d9c948dd",
        "attachments": [{"mime_type": "text/plain", "text": "private attachment body"}],
        "enable_tools": True,
    }
    values.update(overrides)
    return values


class _Primary:
    def __init__(self, *, admission: object = False, gate: asyncio.Event | None = None) -> None:
        self.admission = admission
        self.gate = gate
        self.entered = asyncio.Event()
        self.calls = 0
        self.close_calls = 0
        self.marker = {
            "conversation_id": "conv_34a31c84d9c948dd",
            "message": "primary",
            "nested": {"same": True},
        }
        self.last_kwargs: dict[str, Any] = {}
        self.admission_user_ids: list[str] = []
        self.chat_user_ids: list[str] = []
        self.compatibility_marker = object()

    def pending_durable_turn_admission(self, user_id: str, *_args: Any, **_kwargs: Any) -> object:
        self.admission_user_ids.append(user_id)
        return self.admission

    async def chat(self, user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.chat_user_ids.append(user_id)
        self.last_kwargs = kwargs
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        self.marker["conversation_id"] = kwargs.get("conversation_id")
        return self.marker

    async def close(self) -> None:
        self.close_calls += 1


def _valid_proposal(request: Any) -> dict[str, Any]:
    envelope = json.loads(request.messages[1]["content"])
    manifest_id = envelope["untrusted_payload"]["capability_manifest"]["manifest_id"]
    budget_sha256 = envelope["untrusted_payload"]["response_template"]["budget_sha256"]
    return {
        "schema": SUPERVISOR_PROPOSAL_SCHEMA,
        "manifest_id": manifest_id,
        "budget_sha256": budget_sha256,
        "task_class": "compare_current_file_with_current_web",
        "goal": "Compare the supplied document with current public rules.",
        "continuation_decision": "new_task",
        "risk_hints": ["external_read", "multi_source"],
        "steps": [
            {
                "step_id": "s1",
                "kind": "capability",
                "target_id": FILE_CURRENT_READ_ID,
                "purpose": "Read the current attachment.",
                "depends_on": [],
                "parallel_group": "evidence",
                "input": {"attachment_ordinal": 1},
                "expected_outcome": "complete_source_evidence",
            },
            {
                "step_id": "s2",
                "kind": "capability",
                "target_id": WEB_SEARCH_CURRENT_ID,
                "purpose": "Find current public rules.",
                "depends_on": [],
                "parallel_group": "evidence",
                "input": {"query_intent": "current public rules for the supplied document"},
                "expected_outcome": "verified_current_sources",
            },
            {
                "step_id": "s3",
                "kind": "model",
                "target_id": PRIMARY_SYNTHESIS_ID,
                "purpose": "Compare admitted evidence.",
                "depends_on": ["s1", "s2"],
                "parallel_group": None,
                "input": {},
                "expected_outcome": "cited_comparison",
            },
        ],
        "completion_criteria": [
            "current_attachment_evidence_present",
            "current_public_evidence_has_coverage",
            "material_differences_source_bound",
        ],
        "review_mode": "none",
        "fallback": "primary_only",
    }


class _Scheduler:
    def __init__(
        self,
        *,
        outcome: str = "valid",
        gate: asyncio.Event | None = None,
    ) -> None:
        self.outcome = outcome
        self.gate = gate
        self.calls = 0
        self.cancelled = 0
        self.dispatched = 0
        self.requests: list[Any] = []

    def workload_mode(self, _workload: object) -> str:
        return "shadow"

    async def evaluate_shadow(
        self,
        request: Any,
        *,
        validator: Any = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Any = None,
        dispatch_observer: Any = None,
    ) -> SecondaryAttempt:
        assert invalidate_on_rejection is False
        self.calls += 1
        self.requests.append(request)
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if pre_dispatch_validator is not None and pre_dispatch_validator() is not True:
            return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
        self.dispatched += 1
        if dispatch_observer is not None:
            dispatch_observer()
        if self.outcome == "timeout":
            return SecondaryAttempt.rejected(SecondaryFailure.TIMEOUT)
        if self.outcome == "success_without_validation":
            return SecondaryAttempt.success(SecondaryResult(visible_content="{}", structured_output={}))
        proposal = _valid_proposal(request) if self.outcome == "valid" else {"malformed": True}
        result = SecondaryResult(
            visible_content=json.dumps(proposal, ensure_ascii=False),
            structured_output=proposal,
            served_model_alias="accepted-test-alias",
        )
        accepted = bool(validator(result)) if validator is not None else False
        return (
            SecondaryAttempt.success(result)
            if accepted
            else SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
        )


def _wrapper(
    *,
    primary: _Primary | None = None,
    scheduler: _Scheduler | None = None,
    settings: SimpleNamespace | None = None,
) -> tuple[SemanticSupervisorShadowRuntime, _Primary, _Scheduler]:
    primary = primary or _Primary()
    scheduler = scheduler or _Scheduler()
    built = build_semantic_supervisor_runtime(settings or _settings(), primary, scheduler)
    assert isinstance(built, SemanticSupervisorShadowRuntime)
    return built, primary, scheduler


def test_builder_preserves_identity_when_off_or_policy_gate_is_not_shadow() -> None:
    primary = _Primary()
    scheduler = _Scheduler()
    assert (
        build_semantic_supervisor_runtime(_settings(semantic_supervisor_mode="off"), primary, scheduler)
        is primary
    )

    scheduler.workload_mode = lambda _workload: "disabled"  # type: ignore[method-assign]
    assert build_semantic_supervisor_runtime(_settings(), primary, scheduler) is primary


def test_assist_requested_shadow_runtime_reports_v2_without_claiming_authority() -> None:
    wrapper, _, _ = _wrapper(
        settings=_settings(
            semantic_supervisor_mode="assist",
            semantic_supervisor_max_review_rounds=1,
        )
    )

    status = wrapper.semantic_supervisor_status()
    assert status["role"] == "discarded_advisory_shadow"
    assert status["requested_mode"] == "assist"
    assert status["effective_mode"] == "shadow"
    assert status["policy_id"] == semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
    assert status["policy_sha256"] == (semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256)
    assert status["promotion_admitted"] is False
    assert status["execution_allowed"] is False
    assert status["tools_allowed"] is False
    assert status["effects_allowed"] is False


@pytest.mark.asyncio
async def test_primary_identity_and_no_shadow_before_successful_primary() -> None:
    primary_gate = asyncio.Event()
    wrapper, primary, scheduler = _wrapper(primary=_Primary(gate=primary_gate))

    chat = asyncio.create_task(
        wrapper.chat(
            "person_private_7f",
            "Сравни этот договор с текущими публичными правилами в интернете.",
            **_chat_kwargs(),
        )
    )
    await primary.entered.wait()
    assert scheduler.calls == 0
    primary_gate.set()
    result = await chat
    assert result is primary.marker
    assert primary.calls == 1
    await wrapper.drain_shadow()

    assert scheduler.calls == 1
    observation = wrapper.semantic_supervisor_observations[-1]
    assert observation.invoked is True
    assert observation.policy_verdict == "valid"
    assert observation.skip_reason is SupervisorSkipReason.NONE
    assert wrapper.compatibility_marker is primary.compatibility_marker


@pytest.mark.asyncio
async def test_primary_failure_never_starts_shadow() -> None:
    class _FailingPrimary(_Primary):
        async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise RuntimeError("primary failure")

    wrapper, primary, scheduler = _wrapper(primary=_FailingPrimary())
    with pytest.raises(RuntimeError, match="primary failure"):
        await wrapper.chat(
            "person_private_7f",
            "Сравни этот договор с текущими публичными правилами в интернете.",
            **_chat_kwargs(),
        )
    assert primary.calls == 1
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations == ()


@pytest.mark.asyncio
async def test_four_pending_tasks_bound_shadow_and_fifth_is_structurally_saturated() -> None:
    shadow_gate = asyncio.Event()
    wrapper, primary, scheduler = _wrapper(scheduler=_Scheduler(gate=shadow_gate))

    for index in range(5):
        result = await wrapper.chat(
            "person_private_7f",
            f"Сравни этот договор с текущими публичными правилами в интернете. {index}",
            **_chat_kwargs(conversation_id=f"conv_pending_bound_{index}"),
        )
        assert result is primary.marker
    await asyncio.sleep(0)

    assert scheduler.calls == 4
    assert wrapper.semantic_supervisor_status()["pending"] == 4
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.SATURATED
    shadow_gate.set()
    await wrapper.drain_shadow()
    assert primary.calls == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"replay_source_message_id": "msg_0123456789abcdef"}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"answer_with_voice": True}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"synthetic_document_notice": True}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"mode": "dialogue"}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"reply_to": "quoted private body"}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"turn_policy": SimpleNamespace(handled=True)}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"ingestion_result": {"promoted": True}}, SupervisorSkipReason.SPECIAL_SURFACE),
        ({"enable_tools": False}, SupervisorSkipReason.EVIDENCE_UNAVAILABLE),
    ],
)
async def test_special_surfaces_bypass_shadow(override: dict[str, Any], reason: SupervisorSkipReason) -> None:
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(**override),
    )
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ingestion_result",
    [
        {
            "promoted": False,
            "queued_for_review": False,
            "action": "transient",
            "category": "web_request",
            "reason": "code-owned command classification",
        },
        {
            "promoted": False,
            "queued_for_review": False,
            "action": "transient",
            "category": "system_notice",
            "reason": "file ingestion handled separately",
            "synthetic": True,
        },
    ],
)
async def test_code_owned_transient_ingestion_surface_remains_shadow_eligible(
    ingestion_result: dict[str, Any],
) -> None:
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(ingestion_result=ingestion_result),
    )
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert primary.last_kwargs["ingestion_result"] is ingestion_result
    assert scheduler.calls == 1
    assert wrapper.semantic_supervisor_observations[-1].policy_verdict == "valid"


@pytest.mark.asyncio
async def test_session_restored_mode_is_forwarded_without_becoming_an_explicit_mode_surface() -> None:
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(
            mode="dialogue",
            _semantic_supervisor_explicit_mode_requested=False,
        ),
    )
    await wrapper.drain_shadow()

    assert primary.calls == 1
    assert primary.last_kwargs["mode"] == "dialogue"
    assert "_semantic_supervisor_explicit_mode_requested" not in primary.last_kwargs
    assert scheduler.calls == 1
    assert wrapper.semantic_supervisor_observations[-1].policy_verdict == "valid"


@pytest.mark.asyncio
async def test_body_explicit_mode_provenance_bypasses_even_when_mode_matches_session_default() -> None:
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(
            mode="dialogue",
            _semantic_supervisor_explicit_mode_requested=True,
        ),
    )
    await wrapper.drain_shadow()

    assert primary.calls == 1
    assert primary.last_kwargs["mode"] == "dialogue"
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.SPECIAL_SURFACE


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", [None, True, object()])
async def test_only_exact_false_pending_admission_may_sample(admission: object) -> None:
    wrapper, primary, scheduler = _wrapper(primary=_Primary(admission=admission))
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.EXACT_LANE


@pytest.mark.asyncio
async def test_pending_state_that_appears_during_primary_closes_shadow_before_endpoint_call() -> None:
    class _ChangingPrimary(_Primary):
        def __init__(self) -> None:
            super().__init__()
            self.admission_calls = 0

        def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> object:
            self.admission_calls += 1
            return self.admission_calls != 1

    wrapper, primary, scheduler = _wrapper(primary=_ChangingPrimary())
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.EXACT_LANE


@pytest.mark.asyncio
async def test_primary_conversation_rebinding_closes_stale_scope_before_shadow() -> None:
    stale_id = "conv_stale_34a31c84d9c948dd"
    actual_id = "conv_actual_34a31c84d9c948dd"

    class _RebindingPrimary(_Primary):
        def pending_durable_turn_admission(
            self,
            user_id: str,
            _message: str,
            *,
            conversation_id: str | None,
            **_kwargs: Any,
        ) -> object:
            self.admission_user_ids.append(user_id)
            if conversation_id == actual_id:
                return PendingDurableTurnAdmission.owned(
                    person_id=user_id,
                    conversation_id=actual_id,
                )
            return False

        async def chat(self, user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            self.chat_user_ids.append(user_id)
            self.last_kwargs = kwargs
            return {"conversation_id": actual_id, "message": "primary"}

    wrapper, primary, scheduler = _wrapper(primary=_RebindingPrimary())
    result = await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(conversation_id=stale_id),
    )
    await wrapper.drain_shadow()

    assert result["conversation_id"] == actual_id
    assert primary.calls == 1
    assert primary.admission_user_ids == ["person_private_7f"]
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is (SupervisorSkipReason.EXACT_LANE)


@pytest.mark.asyncio
async def test_pending_state_that_appears_during_scheduler_admission_closes_before_dispatch() -> None:
    admission_gate = asyncio.Event()
    primary = _Primary()
    scheduler = _Scheduler(gate=admission_gate)
    wrapper, _, _ = _wrapper(primary=primary, scheduler=scheduler)

    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    while scheduler.calls == 0:
        await asyncio.sleep(0)
    primary.admission = PendingDurableTurnAdmission.owned(
        person_id="person_private_7f",
        conversation_id="conv_34a31c84d9c948dd",
    )
    admission_gate.set()
    await wrapper.drain_shadow()

    assert primary.calls == 1
    assert scheduler.dispatched == 0
    observation = wrapper.semantic_supervisor_observations[-1]
    assert observation.invoked is False
    assert observation.endpoint_health_class == "not_called"
    assert observation.skip_reason is SupervisorSkipReason.EXACT_LANE


@pytest.mark.asyncio
async def test_deadline_expiring_inside_pending_recheck_closes_before_dispatch() -> None:
    class _SlowPendingPrimary(_Primary):
        def __init__(self) -> None:
            super().__init__()
            self.admission_calls = 0

        def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> object:
            self.admission_calls += 1
            if self.admission_calls == 3:
                time.sleep(0.11)
            return False

    primary = _SlowPendingPrimary()
    scheduler = _Scheduler()
    wrapper, _, _ = _wrapper(
        primary=primary,
        scheduler=scheduler,
        settings=_settings(semantic_supervisor_timeout_sec=0.1),
    )
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await wrapper.drain_shadow()

    assert primary.admission_calls == 3
    assert scheduler.calls == 1
    assert scheduler.dispatched == 0
    observation = wrapper.semantic_supervisor_observations[-1]
    assert observation.invoked is False
    assert observation.endpoint_health_class == "not_called"
    assert observation.skip_reason is SupervisorSkipReason.TIMEOUT


@pytest.mark.asyncio
async def test_carried_pending_receipt_bypasses_shadow_and_is_forwarded_unchanged() -> None:
    receipt = PendingDurableTurnAdmission.owned(
        person_id="person_private_7f",
        conversation_id="conv_34a31c84d9c948dd",
    )
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(_pending_durable_admission=receipt),
    )
    await wrapper.drain_shadow()
    assert scheduler.calls == 0
    assert primary.admission_user_ids == []
    assert primary.last_kwargs["_pending_durable_admission"] is receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "attachments"),
    [
        ("отмена", [{"mime_type": "text/plain"}]),
        ("2", None),
    ],
)
async def test_exact_control_lane_bypasses_shadow(message: str, attachments: object) -> None:
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        message,
        **_chat_kwargs(attachments=attachments),
    )
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert scheduler.calls == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.EXACT_LANE


@pytest.mark.asyncio
async def test_invalid_attachment_shape_bypasses_instead_of_sampling_a_different_projection() -> None:
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(attachments=[{"mime_type": "text/plain"}, "invalid-carrier"]),
    )
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert scheduler.calls == 0


@pytest.mark.asyncio
async def test_truncated_turn_projection_never_rechecks_pending_with_different_message_bytes() -> None:
    wrapper, primary, scheduler = _wrapper()
    message = "Сравни этот договор с текущими публичными правилами в интернете. " + "x" * 20_000
    await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    await wrapper.drain_shadow()

    assert primary.calls == 1
    assert scheduler.calls == 0
    assert primary.last_kwargs["attachments"] == _chat_kwargs()["attachments"]
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is (
        SupervisorSkipReason.EVIDENCE_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_spent_deadline_and_malformed_adapter_are_closed_observations() -> None:
    class _SlowPrimary(_Primary):
        async def chat(self, _user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            self.last_kwargs = kwargs
            await asyncio.sleep(0.11)
            return self.marker

    slow_wrapper, slow_primary, slow_scheduler = _wrapper(
        primary=_SlowPrimary(),
        settings=_settings(semantic_supervisor_timeout_sec=0.1),
    )
    result = await slow_wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    assert result is slow_primary.marker
    assert slow_scheduler.calls == 0
    assert slow_wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.TIMEOUT

    for outcome in ("malformed", "success_without_validation"):
        malformed_wrapper, _, malformed_scheduler = _wrapper(scheduler=_Scheduler(outcome=outcome))
        await malformed_wrapper.chat(
            "person_private_7f",
            "Сравни этот договор с текущими публичными правилами в интернете.",
            **_chat_kwargs(),
        )
        await malformed_wrapper.drain_shadow()
        assert malformed_scheduler.calls == 1
        malformed = malformed_wrapper.semantic_supervisor_observations[-1]
        assert malformed.skip_reason is SupervisorSkipReason.MALFORMED_PROPOSAL
        assert malformed.proposal_parse_status == "malformed"
        assert malformed.endpoint_health_class == "accepted"


@pytest.mark.asyncio
async def test_secondary_timeout_is_structural_and_never_changes_primary() -> None:
    wrapper, primary, scheduler = _wrapper(scheduler=_Scheduler(outcome="timeout"))
    result = await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await wrapper.drain_shadow()
    assert result is primary.marker
    assert primary.calls == 1
    assert scheduler.calls == 1
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.TIMEOUT


@pytest.mark.asyncio
async def test_observations_and_status_do_not_retain_bodies_ids_or_guessable_raw_digests() -> None:
    message = "Сравни этот договор с текущими публичными правилами в интернете; phrase-only-for-privacy-test."
    settings = _settings()
    wrapper, _, scheduler = _wrapper(settings=settings)
    kwargs = _chat_kwargs()
    turn = TurnInput.from_chat(
        message=message,
        actor=kwargs["actor"],
        conversation_id=kwargs["conversation_id"],
        attachments=kwargs["attachments"],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    raw_input_digest = build_supervisor_input(turn, settings).canonical_sha256()

    await wrapper.chat("person_private_7f", message, **kwargs)
    await wrapper.drain_shadow()
    raw_proposal_digest = canonical_sha256(_valid_proposal(scheduler.requests[0]))
    serialized = json.dumps(
        {
            "observations": [item.payload() for item in wrapper.semantic_supervisor_observations],
            "status": wrapper.semantic_supervisor_status(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for forbidden in (
        message,
        "phrase-only-for-privacy-test",
        "person_private_7f",
        "conv_34a31c84d9c948dd",
        "private attachment body",
        raw_input_digest,
        raw_proposal_digest,
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_fragment",
    [
        "/home/jericho/private/client.txt",
        "raw_0123456789abcdef",
    ],
)
async def test_private_path_and_raw_id_are_never_retained(private_fragment: str) -> None:
    wrapper, primary, scheduler = _wrapper()
    message = f"Сравни этот договор с текущими публичными правилами в интернете: {private_fragment}"
    await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    await wrapper.drain_shadow()
    assert primary.calls == 1
    assert scheduler.calls == 0
    serialized = json.dumps(
        [item.payload() for item in wrapper.semantic_supervisor_observations],
        ensure_ascii=False,
    )
    assert private_fragment not in serialized
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is SupervisorSkipReason.SECRET_MATERIAL


@pytest.mark.asyncio
async def test_primary_delegation_omits_absent_internal_extension_kwargs() -> None:
    wrapper, primary, _ = _wrapper()
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await wrapper.drain_shadow()
    assert "turn_policy" not in primary.last_kwargs
    assert "_pending_durable_admission" not in primary.last_kwargs


@pytest.mark.asyncio
async def test_shared_archive_uses_person_scope_only_for_read_only_pending_admission() -> None:
    actor = ActorContext(
        user_id="shared_tenant_id",
        preset_key="owner",
        source="runtime-test",
        shared_tenant=True,
        person_id="private_person_id",
    )
    wrapper, primary, scheduler = _wrapper()
    await wrapper.chat(
        "shared_tenant_id",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(actor=actor),
    )
    await wrapper.drain_shadow()
    assert scheduler.calls == 1
    assert primary.admission_user_ids == [
        "private_person_id",
        "private_person_id",
        "private_person_id",
    ]
    assert primary.chat_user_ids == ["shared_tenant_id"]


@pytest.mark.asyncio
async def test_drain_waits_and_close_cancels_only_sidecar_tasks() -> None:
    drain_gate = asyncio.Event()
    wrapper, primary, _ = _wrapper(scheduler=_Scheduler(gate=drain_gate))
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    drain = asyncio.create_task(wrapper.drain_shadow())
    await asyncio.sleep(0)
    assert drain.done() is False
    drain_gate.set()
    await drain

    close_gate = asyncio.Event()
    close_scheduler = _Scheduler(gate=close_gate)
    close_wrapper, close_primary, _ = _wrapper(primary=_Primary(), scheduler=close_scheduler)
    await close_wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await asyncio.sleep(0)
    await close_wrapper.close()
    await close_wrapper.close()
    assert close_scheduler.cancelled == 1
    assert close_primary.close_calls == 0
    assert close_wrapper.semantic_supervisor_status()["pending"] == 0
    assert close_wrapper.semantic_supervisor_status()["effective_mode"] == "off"
    assert close_wrapper.semantic_supervisor_status()["observation_total"] == 1
    assert close_wrapper.semantic_supervisor_status()["invoked_total"] == 0
    assert close_wrapper.semantic_supervisor_observations[-1].skip_reason is (
        SupervisorSkipReason.SECONDARY_UNAVAILABLE
    )
    assert primary.close_calls == 0


@pytest.mark.asyncio
async def test_superseded_dispatched_shadow_is_accounted_without_body_state() -> None:
    class _DispatchedScheduler(_Scheduler):
        def __init__(self) -> None:
            super().__init__()
            self.after_dispatch = asyncio.Event()
            self.release = asyncio.Event()

        async def evaluate_shadow(
            self,
            request: Any,
            *,
            validator: Any = None,
            invalidate_on_rejection: bool = True,
            pre_dispatch_validator: Any = None,
            dispatch_observer: Any = None,
        ) -> SecondaryAttempt:
            assert invalidate_on_rejection is False
            self.calls += 1
            self.requests.append(request)
            if pre_dispatch_validator is not None and pre_dispatch_validator() is not True:
                return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
            self.dispatched += 1
            if dispatch_observer is not None:
                dispatch_observer()
            self.after_dispatch.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            proposal = _valid_proposal(request)
            result = SecondaryResult(
                visible_content=json.dumps(proposal, ensure_ascii=False),
                structured_output=proposal,
                served_model_alias="accepted-test-alias",
            )
            assert validator is not None and validator(result) is True
            return SecondaryAttempt.success(result)

    scheduler = _DispatchedScheduler()
    wrapper, primary, _ = _wrapper(scheduler=scheduler)
    message = "Сравни этот договор с текущими публичными правилами в интернете."
    await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    await scheduler.after_dispatch.wait()

    second = await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    assert second is primary.marker
    while scheduler.calls < 2:
        await asyncio.sleep(0)
    scheduler.release.set()
    await wrapper.drain_shadow()

    assert scheduler.cancelled == 1
    cancelled = next(
        item
        for item in wrapper.semantic_supervisor_observations
        if item.skip_reason is SupervisorSkipReason.EXACT_LANE and item.invoked
    )
    assert cancelled.endpoint_health_class == "closed_failure"
    assert wrapper.semantic_supervisor_status()["observation_total"] == 2
    assert wrapper.semantic_supervisor_status()["invoked_total"] == 2


@pytest.mark.asyncio
async def test_same_scope_no_yield_cancellation_still_has_one_terminal_observation_per_job() -> None:
    wrapper, primary, scheduler = _wrapper()
    message = "Сравни этот договор с текущими публичными правилами в интернете."

    # _Primary.chat has no suspension point. The second call therefore cancels
    # the first accepted task before its coroutine body has ever started.
    first = await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    second = await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    await wrapper.drain_shadow()

    assert first is primary.marker
    assert second is primary.marker
    assert primary.calls == 2
    assert scheduler.calls == 1
    observations = wrapper.semantic_supervisor_observations
    assert len(observations) == 2
    cancelled = [item for item in observations if item.skip_reason is SupervisorSkipReason.EXACT_LANE]
    assert len(cancelled) == 1
    assert cancelled[0].invoked is False
    assert cancelled[0].endpoint_health_class == "not_called"
    assert wrapper.semantic_supervisor_status()["observation_total"] == 2
    assert wrapper.semantic_supervisor_status()["invoked_total"] == 1
    serialized = json.dumps([item.payload() for item in observations], ensure_ascii=False)
    assert message not in serialized
    assert "person_private_7f" not in serialized
    assert "conv_34a31c84d9c948dd" not in serialized


@pytest.mark.asyncio
async def test_close_has_bounded_drain_when_optional_evaluator_suppresses_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubbornScheduler(_Scheduler):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        async def evaluate_shadow(self, request: Any, **_kwargs: Any) -> SecondaryAttempt:
            self.calls += 1
            self.requests.append(request)
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                await self.release.wait()
            finally:
                self.finished.set()
            return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)

    monkeypatch.setattr(runtime_module, "_SHADOW_CLOSE_DRAIN_TIMEOUT_SEC", 0.01)
    scheduler = _StubbornScheduler()
    wrapper, _, _ = _wrapper(scheduler=scheduler)
    await wrapper.chat(
        "person_private_7f",
        "Сравни этот договор с текущими публичными правилами в интернете.",
        **_chat_kwargs(),
    )
    await scheduler.entered.wait()

    await asyncio.wait_for(wrapper.close(), timeout=0.1)
    assert wrapper.semantic_supervisor_status()["pending"] == 0
    assert wrapper.semantic_supervisor_status()["effective_mode"] == "off"
    assert wrapper.semantic_supervisor_status()["observation_total"] == 1
    assert wrapper.semantic_supervisor_status()["invoked_total"] == 0
    assert wrapper.semantic_supervisor_observations[-1].endpoint_health_class == "not_called"
    scheduler.release.set()
    await asyncio.wait_for(scheduler.finished.wait(), timeout=0.1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_close_settles_tracking_and_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CancellationSuppressingScheduler(_Scheduler):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        async def evaluate_shadow(self, request: Any, **_kwargs: Any) -> SecondaryAttempt:
            self.calls += 1
            self.requests.append(request)
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                self.cancel_seen.set()
                await self.release.wait()
            finally:
                self.finished.set()
            return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)

    monkeypatch.setattr(runtime_module, "_SHADOW_CLOSE_DRAIN_TIMEOUT_SEC", 10.0)
    scheduler = _CancellationSuppressingScheduler()
    wrapper, primary, _ = _wrapper(scheduler=scheduler)
    message = "Сравни этот договор с текущими публичными правилами в интернете."
    await wrapper.chat("person_private_7f", message, **_chat_kwargs())
    await scheduler.entered.wait()

    closing = asyncio.create_task(wrapper.close())
    await scheduler.cancel_seen.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert wrapper.semantic_supervisor_status()["effective_mode"] == "off"
    assert wrapper.semantic_supervisor_status()["pending"] == 0
    assert wrapper.semantic_supervisor_status()["observation_total"] == 1
    assert wrapper.semantic_supervisor_status()["invoked_total"] == 0
    assert wrapper.semantic_supervisor_observations[-1].skip_reason is (
        SupervisorSkipReason.SECONDARY_UNAVAILABLE
    )

    await asyncio.wait_for(wrapper.close(), timeout=0.1)
    assert wrapper.semantic_supervisor_status()["pending"] == 0
    assert wrapper.semantic_supervisor_status()["observation_total"] == 1
    assert primary.close_calls == 0

    scheduler.release.set()
    await asyncio.wait_for(scheduler.finished.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert wrapper.semantic_supervisor_status()["observation_total"] == 1
