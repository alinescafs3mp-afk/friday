from __future__ import annotations

import inspect
from dataclasses import replace
from types import MappingProxyType

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    operational_capability_snapshot,
)
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_GATE_ID,
    SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
    SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
    SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
    SUPERVISOR_ASSIST_PROMOTION_SCHEMA,
    AssistPromotionCandidate,
    AssistPromotionEvidenceAuthority,
    AssistPromotionLiveEvidence,
    AssistPromotionOperatorGate,
    AssistPromotionReadiness,
    AssistPromotionReason,
    SupervisorSchedulerAdmissionSnapshot,
    admit_supervisor_assist_promotion,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass

SOURCE = "1" * 64
ACTOR = "2" * 64
OTHER_ACTOR = "3" * 64
OTHER = "f" * 64


def _scheduler(**changes: object) -> SupervisorSchedulerAdmissionSnapshot:
    values: dict[str, object] = {
        "workload": semantic_supervisor_policy.SUPERVISOR_WORKLOAD,
        "requested_mode": SupervisorMode.ASSIST.value,
        "effective_mode": SupervisorMode.SHADOW.value,
        "policy_id": semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID,
        "policy_sha256": semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256,
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        "profile_admission": "accepted",
        "closed_reason": "admitted",
        "workload_available": True,
        "runtime_available": True,
    }
    values.update(changes)
    return SupervisorSchedulerAdmissionSnapshot(**values)  # type: ignore[arg-type]


def _candidate(
    *,
    mode: SupervisorMode = SupervisorMode.ASSIST,
    snapshot: CapabilityBindingSnapshot | None = None,
    **changes: object,
) -> AssistPromotionCandidate:
    current = snapshot or operational_capability_snapshot()
    values: dict[str, object] = {
        "requested_mode": mode,
        "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        "source_revision_sha256": SOURCE,
        "expected_registry_binding_sha256": current.digest_hex(),
        "binding_snapshot": current,
        "scheduler": _scheduler(requested_mode=mode.value),
        "max_steps": SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
        "max_review_rounds": SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
        "actor_binding_sha256": ACTOR if mode is SupervisorMode.CANARY else None,
    }
    values.update(changes)
    return AssistPromotionCandidate(**values)  # type: ignore[arg-type]


def _evidence(
    candidate: AssistPromotionCandidate,
    **changes: object,
) -> AssistPromotionLiveEvidence:
    # This is a contract fixture for a future evidence producer.  It is not a
    # claim that the repository currently has accepted production evidence.
    values: dict[str, object] = {
        "evidence_id": "future_joined_operator_fixture",
        "authority": AssistPromotionEvidenceAuthority.PRODUCTION_JOINED,
        "observed_mode": (
            SupervisorMode.ASSIST
            if candidate.requested_mode is SupervisorMode.CANARY
            else SupervisorMode.SHADOW
        ),
        "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        "source_revision_sha256": candidate.source_revision_sha256,
        "promotion_policy_sha256": SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
        "p1_policy_id": semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID,
        "p1_policy_sha256": semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256,
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        "registry_binding_sha256": candidate.expected_registry_binding_sha256,
        "max_steps": candidate.max_steps,
        "max_review_rounds": candidate.max_review_rounds,
        "observation_count": 20,
        "joined_trace_count": 20,
        "representative_window_attested": True,
        "primary_fallback_proven": True,
        "laptop_unavailable_fallback_proven": True,
        "final_authority_recheck_proven": True,
        "primary_publication_owner_proven": True,
        "hidden_owner_count": 0,
        "duplicate_capability_count": 0,
        "duplicate_effect_count": 0,
        "duplicate_publication_count": 0,
        "false_completion_regression_count": 0,
    }
    values.update(changes)
    return AssistPromotionLiveEvidence(**values)  # type: ignore[arg-type]


def _gate(
    candidate: AssistPromotionCandidate,
    evidence: AssistPromotionLiveEvidence,
    **changes: object,
) -> AssistPromotionOperatorGate:
    values: dict[str, object] = {
        "enabled": True,
        "gate_id": SUPERVISOR_ASSIST_PROMOTION_GATE_ID,
        "promotion_policy_sha256": SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
        "target_mode": candidate.requested_mode,
        "task_class": candidate.task_class,
        "source_revision_sha256": candidate.source_revision_sha256,
        "registry_binding_sha256": candidate.expected_registry_binding_sha256,
        "accepted_evidence_sha256": evidence.canonical_sha256(),
        "canary_actor_bindings": ((ACTOR,) if candidate.requested_mode is SupervisorMode.CANARY else ()),
    }
    values.update(changes)
    return AssistPromotionOperatorGate(**values)  # type: ignore[arg-type]


def test_default_gate_is_closed_and_source_readiness_is_not_live_acceptance() -> None:
    candidate = _candidate()

    decision = admit_supervisor_assist_promotion(
        candidate,
        None,
        AssistPromotionOperatorGate(),
    )

    assert decision.promotion_admitted is False
    assert decision.reason is AssistPromotionReason.OPERATOR_GATE_CLOSED
    assert decision.readiness is AssistPromotionReadiness.SOURCE_READY
    assert decision.source_ready is True
    assert decision.live_evidence_ready is False
    assert decision.operator_gate_bound is False
    assert decision.evidence_sha256 is None
    assert decision.admitted_mode is SupervisorMode.OFF
    assert decision.execution_authorized is False
    assert decision.publication_authorized is False
    assert decision.storage_write_authorized is False


def test_future_joined_fixture_can_admit_assist_but_grants_no_turn_authority() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)

    decision = admit_supervisor_assist_promotion(candidate, evidence, _gate(candidate, evidence))

    assert decision.promotion_admitted is True
    assert decision.reason is AssistPromotionReason.ADMITTED
    assert decision.readiness is AssistPromotionReadiness.LIVE_EVIDENCE_READY
    assert decision.admitted_mode is SupervisorMode.ASSIST
    assert decision.operator_gate_bound is True
    assert decision.evidence_sha256 == evidence.canonical_sha256()
    assert decision.execution_authorized is False
    assert decision.publication_authorized is False
    assert decision.storage_write_authorized is False
    assert set(inspect.signature(admit_supervisor_assist_promotion).parameters) == {
        "candidate",
        "evidence",
        "operator_gate",
    }


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        (SupervisorMode.OFF, AssistPromotionReason.MODE_OFF),
        (SupervisorMode.SHADOW, AssistPromotionReason.SHADOW_NEVER_OWNS),
    ],
)
def test_off_and_shadow_can_never_be_promoted_owners(
    mode: SupervisorMode,
    reason: AssistPromotionReason,
) -> None:
    candidate = _candidate(mode=mode)

    decision = admit_supervisor_assist_promotion(
        candidate,
        None,
        AssistPromotionOperatorGate(),
    )

    assert decision.promotion_admitted is False
    assert decision.source_ready is False
    assert decision.reason is reason
    assert decision.admitted_mode is SupervisorMode.OFF


def test_only_exact_current_file_with_current_web_is_source_ready() -> None:
    candidate = _candidate(task_class=TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB)

    decision = admit_supervisor_assist_promotion(
        candidate,
        None,
        AssistPromotionOperatorGate(),
    )

    assert decision.reason is AssistPromotionReason.TASK_NOT_ADMITTED
    assert decision.source_ready is False


@pytest.mark.parametrize(
    "authority",
    [
        AssistPromotionEvidenceAuthority.SOURCE_READY,
        AssistPromotionEvidenceAuthority.SYNTHETIC_OFFLINE,
        AssistPromotionEvidenceAuthority.ISOLATED_LIVE_PROTOCOL,
    ],
)
def test_source_synthetic_and_isolated_protocol_evidence_never_promote(
    authority: AssistPromotionEvidenceAuthority,
) -> None:
    candidate = _candidate()
    evidence = _evidence(candidate, authority=authority)

    decision = admit_supervisor_assist_promotion(candidate, evidence, _gate(candidate, evidence))

    assert decision.promotion_admitted is False
    assert decision.source_ready is True
    assert decision.live_evidence_ready is False
    assert decision.reason is AssistPromotionReason.PRODUCTION_JOINED_EVIDENCE_REQUIRED


def test_assist_and_canary_require_distinct_predecessor_evidence() -> None:
    assist = _candidate()
    assist_evidence = _evidence(assist)
    canary = _candidate(mode=SupervisorMode.CANARY)
    wrong_canary_evidence = _evidence(canary, observed_mode=SupervisorMode.SHADOW)

    assist_decision = admit_supervisor_assist_promotion(
        assist,
        assist_evidence,
        _gate(assist, assist_evidence),
    )
    canary_decision = admit_supervisor_assist_promotion(
        canary,
        wrong_canary_evidence,
        _gate(canary, wrong_canary_evidence),
    )

    assert assist_decision.promotion_admitted is True
    assert canary_decision.promotion_admitted is False
    assert canary_decision.reason is AssistPromotionReason.EVIDENCE_STAGE_DRIFT


def test_canary_requires_nonempty_exact_actor_binding_allowlist() -> None:
    candidate = _candidate(mode=SupervisorMode.CANARY)
    evidence = _evidence(candidate)

    empty = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, canary_actor_bindings=()),
    )
    wrong = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, canary_actor_bindings=(OTHER_ACTOR,)),
    )
    exact = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, canary_actor_bindings=(OTHER_ACTOR, ACTOR)),
    )

    assert empty.reason is AssistPromotionReason.CANARY_ALLOWLIST_REQUIRED
    assert wrong.reason is AssistPromotionReason.CANARY_ACTOR_NOT_ALLOWLISTED
    assert exact.promotion_admitted is True
    assert exact.admitted_mode is SupervisorMode.CANARY


def test_assist_rejects_a_canary_allowlist_instead_of_blurring_modes() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)

    decision = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, canary_actor_bindings=(ACTOR,)),
    )

    assert decision.reason is AssistPromotionReason.ASSIST_ALLOWLIST_NOT_ADMITTED
    assert decision.promotion_admitted is False


@pytest.mark.parametrize(
    ("scheduler", "reason"),
    [
        (
            _scheduler(policy_id="gptoss20b-semantic-supervisor-v2"),
            AssistPromotionReason.P1_POLICY_IDENTITY_DRIFT,
        ),
        (
            _scheduler(policy_sha256=OTHER),
            AssistPromotionReason.P1_POLICY_IDENTITY_DRIFT,
        ),
        (
            _scheduler(runtime_profile_id="gptoss20b-other-profile"),
            AssistPromotionReason.RUNTIME_PROFILE_IDENTITY_DRIFT,
        ),
        (
            _scheduler(runtime_profile_manifest_sha256=OTHER),
            AssistPromotionReason.RUNTIME_PROFILE_IDENTITY_DRIFT,
        ),
        (
            _scheduler(workload="extract"),
            AssistPromotionReason.SCHEDULER_IDENTITY_DRIFT,
        ),
        (
            _scheduler(requested_mode="shadow"),
            AssistPromotionReason.SCHEDULER_IDENTITY_DRIFT,
        ),
        (
            _scheduler(effective_mode="assist"),
            AssistPromotionReason.SCHEDULER_IDENTITY_DRIFT,
        ),
        (
            _scheduler(profile_admission="provisional_shadow"),
            AssistPromotionReason.SCHEDULER_IDENTITY_DRIFT,
        ),
        (
            _scheduler(closed_reason="endpoint_unavailable"),
            AssistPromotionReason.SCHEDULER_IDENTITY_DRIFT,
        ),
        (
            _scheduler(workload_available=False),
            AssistPromotionReason.SCHEDULER_WORKLOAD_UNAVAILABLE,
        ),
    ],
)
def test_exact_p1_policy_profile_and_shadow_scheduler_identity_are_required(
    scheduler: SupervisorSchedulerAdmissionSnapshot,
    reason: AssistPromotionReason,
) -> None:
    candidate = _candidate(scheduler=scheduler)

    decision = admit_supervisor_assist_promotion(
        candidate,
        None,
        AssistPromotionOperatorGate(),
    )

    assert decision.source_ready is False
    assert decision.reason is reason


def test_local_p1_product_policy_drift_rejects_even_an_exact_scheduler_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    monkeypatch.setattr(
        semantic_supervisor_policy,
        "SUPERVISOR_PRODUCT_POLICY",
        MappingProxyType({"policy_id": semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID}),
    )

    decision = admit_supervisor_assist_promotion(
        candidate,
        None,
        AssistPromotionOperatorGate(),
    )

    assert decision.source_ready is False
    assert decision.reason is AssistPromotionReason.P1_POLICY_IDENTITY_DRIFT


def test_laptop_unavailable_rejects_without_erasing_source_or_live_readiness() -> None:
    candidate = _candidate(scheduler=_scheduler(runtime_available=False))
    evidence = _evidence(candidate)

    decision = admit_supervisor_assist_promotion(candidate, evidence, _gate(candidate, evidence))

    assert decision.promotion_admitted is False
    assert decision.reason is AssistPromotionReason.LAPTOP_RUNTIME_UNAVAILABLE
    assert decision.source_ready is True
    assert decision.live_evidence_ready is True
    assert decision.readiness is AssistPromotionReadiness.LIVE_EVIDENCE_READY


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"max_steps": 5}, AssistPromotionReason.BOUNDS_DRIFT),
        ({"max_steps": 7}, AssistPromotionReason.BOUNDS_DRIFT),
        ({"max_review_rounds": 2}, AssistPromotionReason.BOUNDS_DRIFT),
        ({"max_review_rounds": -1}, AssistPromotionReason.BOUNDS_DRIFT),
    ],
)
def test_exact_step_budget_and_at_most_one_review_are_enforced(
    changes: dict[str, object],
    reason: AssistPromotionReason,
) -> None:
    candidate = _candidate(**changes)
    decision = admit_supervisor_assist_promotion(
        candidate,
        None,
        AssistPromotionOperatorGate(),
    )

    assert decision.reason is reason
    assert decision.source_ready is False


def test_zero_review_p2_candidate_is_valid_but_needs_matching_fresh_evidence() -> None:
    candidate = _candidate(max_review_rounds=0)
    evidence = _evidence(candidate)

    decision = admit_supervisor_assist_promotion(candidate, evidence, _gate(candidate, evidence))

    assert decision.promotion_admitted is True


def test_registry_digest_and_exact_transient_web_binding_are_required() -> None:
    current = operational_capability_snapshot()
    stale = _candidate(expected_registry_binding_sha256=OTHER, snapshot=current)
    bindings = list(current.bindings)
    web_index = next(
        index for index, item in enumerate(bindings) if item.supervisor_capability_id == "web.search.current"
    )
    bindings[web_index] = replace(
        bindings[web_index],
        security_id="web.research",
        tool_id="web_research",
        adapter_id="friday.execution_kernel.ExecutionKernel._web_research",
    )
    legacy_snapshot = CapabilityBindingSnapshot(bindings=tuple(bindings))
    legacy = _candidate(snapshot=legacy_snapshot)

    stale_decision = admit_supervisor_assist_promotion(
        stale,
        None,
        AssistPromotionOperatorGate(),
    )
    legacy_decision = admit_supervisor_assist_promotion(
        legacy,
        None,
        AssistPromotionOperatorGate(),
    )

    assert stale_decision.reason is AssistPromotionReason.REGISTRY_BINDING_DRIFT
    assert legacy_decision.reason is AssistPromotionReason.REGISTRY_BINDING_DRIFT


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"source_revision_sha256": OTHER}, AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT),
        ({"promotion_policy_sha256": OTHER}, AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT),
        ({"p1_policy_sha256": OTHER}, AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT),
        (
            {"runtime_profile_manifest_sha256": OTHER},
            AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT,
        ),
        ({"registry_binding_sha256": OTHER}, AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT),
        ({"joined_trace_count": 19}, AssistPromotionReason.EVIDENCE_WINDOW_INCOMPLETE),
        ({"observation_count": 0, "joined_trace_count": 0}, AssistPromotionReason.EVIDENCE_WINDOW_INCOMPLETE),
        ({"representative_window_attested": False}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"primary_fallback_proven": False}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"laptop_unavailable_fallback_proven": False}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"final_authority_recheck_proven": False}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"primary_publication_owner_proven": False}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"hidden_owner_count": 1}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"duplicate_capability_count": 1}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"duplicate_effect_count": 1}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"duplicate_publication_count": 1}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
        ({"false_completion_regression_count": 1}, AssistPromotionReason.EVIDENCE_INVARIANT_FAILED),
    ],
)
def test_live_evidence_is_exact_joined_body_free_and_invariant_bound(
    changes: dict[str, object],
    reason: AssistPromotionReason,
) -> None:
    candidate = _candidate()
    evidence = _evidence(candidate, **changes)

    decision = admit_supervisor_assist_promotion(candidate, evidence, _gate(candidate, evidence))

    assert decision.promotion_admitted is False
    assert decision.reason is reason


def test_independent_gate_is_evidence_source_registry_and_mode_bound() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)

    closed = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        AssistPromotionOperatorGate(),
    )
    wrong_evidence = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, accepted_evidence_sha256=OTHER),
    )
    wrong_source = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, source_revision_sha256=OTHER),
    )
    wrong_registry = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, registry_binding_sha256=OTHER),
    )
    wrong_mode = admit_supervisor_assist_promotion(
        candidate,
        evidence,
        _gate(candidate, evidence, target_mode=SupervisorMode.CANARY),
    )

    assert closed.reason is AssistPromotionReason.OPERATOR_GATE_CLOSED
    assert wrong_evidence.reason is AssistPromotionReason.OPERATOR_EVIDENCE_NOT_BOUND
    assert wrong_source.reason is AssistPromotionReason.OPERATOR_GATE_DRIFT
    assert wrong_registry.reason is AssistPromotionReason.OPERATOR_GATE_DRIFT
    assert wrong_mode.reason is AssistPromotionReason.OPERATOR_GATE_DRIFT


def test_malformed_untyped_evidence_or_gate_rejects_without_authority() -> None:
    candidate = _candidate()

    malformed_evidence = admit_supervisor_assist_promotion(
        candidate,
        object(),  # type: ignore[arg-type]
        AssistPromotionOperatorGate(),
    )
    malformed_gate = admit_supervisor_assist_promotion(
        candidate,
        None,
        object(),  # type: ignore[arg-type]
    )

    assert malformed_evidence.reason is AssistPromotionReason.MALFORMED
    assert malformed_gate.reason is AssistPromotionReason.MALFORMED
    assert malformed_evidence.promotion_admitted is False
    assert malformed_gate.promotion_admitted is False
    with pytest.raises(TypeError, match="typed candidate"):
        admit_supervisor_assist_promotion(
            object(),  # type: ignore[arg-type]
            None,
            AssistPromotionOperatorGate(),
        )


def test_live_evidence_payload_is_closed_body_free_and_digest_sensitive() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)
    payload = evidence.payload()

    assert payload["schema"] == SUPERVISOR_ASSIST_PROMOTION_SCHEMA
    assert set(payload) == {
        "schema",
        "evidence_id",
        "authority",
        "observed_mode",
        "task_class",
        "source_revision_sha256",
        "promotion_policy_sha256",
        "p1_policy_id",
        "p1_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
        "registry_binding_sha256",
        "max_steps",
        "max_review_rounds",
        "observation_count",
        "joined_trace_count",
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
    }
    assert not {
        "body",
        "prompt",
        "response",
        "query",
        "path",
        "actor_id",
        "conversation_id",
        "execute",
        "publish",
        "write",
    } & set(payload)
    assert (
        evidence.canonical_sha256()
        != replace(
            evidence,
            observation_count=evidence.observation_count + 1,
            joined_trace_count=evidence.joined_trace_count + 1,
        ).canonical_sha256()
    )


def test_structural_malformed_contract_values_raise_before_evaluation() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _candidate(source_revision_sha256="not-a-digest")
    with pytest.raises(ValueError, match="duplicates"):
        AssistPromotionOperatorGate(canary_actor_bindings=(ACTOR, ACTOR))
    with pytest.raises(ValueError, match="exceeds"):
        _evidence(_candidate(), observation_count=1, joined_trace_count=2)
