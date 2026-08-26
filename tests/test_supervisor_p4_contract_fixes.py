from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    PRIMARY_SYNTHESIS_CAPABILITY_ID,
    PRIMARY_SYNTHESIS_STEP_ID,
    CompareCurrentFileWebGraphError,
    CompareCurrentFileWebStepKind,
    bind_validated_plan_to_compare_current_file_web_graph,
)
from friday.interaction_control_plane.runtime_trace import build_work_trace
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CompletionDecision,
    ContinuationKind,
    CountAccounting,
    FailureReason,
    FailureStage,
    IntentClass,
    OutcomeStatus,
    PlaybookClass,
    WorkRelation,
)
from friday.orchestration.execution_plan import (
    ValidatedExecutionPlan,
    ValidatedStep,
    mint_admission_seal,
)
from friday.orchestration.supervisor_contracts import (
    FILE_CURRENT_READ_ID,
    PRIMARY_SYNTHESIS_ID,
    WEB_SEARCH_CURRENT_ID,
    CapabilityEffectClass,
    SupervisorBudgets,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityScope,
    PlanSourceBinding,
    durable_authority_binding_sha256,
    source_bindings_sha256,
)
from friday.orchestration.supervisor_trace_join import PrimaryTraceProjection
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.semantic_supervisor_policy import supervisor_product_policy_identity_for_mode

_ASSIST_POLICY = supervisor_product_policy_identity_for_mode("assist")
_BUDGETS = SupervisorBudgets(
    max_steps=_ASSIST_POLICY.max_steps,
    max_parallel_reads=_ASSIST_POLICY.max_parallel_reads,
    turn_deadline_ms=_ASSIST_POLICY.turn_deadline_ms,
    per_step_deadline_ms=_ASSIST_POLICY.per_step_deadline_ms,
    max_supervisor_calls=_ASSIST_POLICY.max_supervisor_calls,
    max_model_calls=_ASSIST_POLICY.max_model_calls,
    max_tool_calls=_ASSIST_POLICY.max_tool_calls,
    max_capability_calls=_ASSIST_POLICY.max_capability_calls,
    max_review_rounds=_ASSIST_POLICY.max_review_rounds,
    max_recovery_rounds=_ASSIST_POLICY.max_recovery_rounds,
    max_output_tokens=_ASSIST_POLICY.max_output_tokens,
)
_SOURCE = PlanSourceBinding.current_raw_object(
    raw_object_id="raw_test",
    source_identity_sha256="7" * 64,
    content_sha256="8" * 64,
)


def _step(
    step_id: str,
    capability_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    parallel_group: str | None = "evidence",
) -> ValidatedStep:
    is_model = capability_id == PRIMARY_SYNTHESIS_ID
    return ValidatedStep(
        step_id=step_id,
        capability_id=capability_id,
        effect_class=CapabilityEffectClass.READ,
        resolved_security_id=None if is_model else f"security.{step_id}",
        resolved_tool_id=None if is_model else f"tool.{step_id}",
        resolved_adapter_id=None if is_model else f"adapter.{step_id}",
        depends_on=dependencies,
        parallel_group=parallel_group,
        input={},
        idempotency_key=(step_id[0] * 64),
        deadline_ms=_BUDGETS.per_step_deadline_ms,
        max_calls=2 if is_model or capability_id == WEB_SEARCH_CURRENT_ID else 1,
        max_output_tokens=1_024 if is_model else 0,
    )


def _plan() -> ValidatedExecutionPlan:
    steps = (
        _step("s1", FILE_CURRENT_READ_ID),
        _step("s2", WEB_SEARCH_CURRENT_ID),
        _step(
            "s3",
            PRIMARY_SYNTHESIS_ID,
            dependencies=("s1", "s2"),
            parallel_group=None,
        ),
    )
    required_security_ids = tuple(
        sorted(step.resolved_security_id for step in steps if step.resolved_security_id is not None)
    )
    source_digest = source_bindings_sha256((_SOURCE,))
    budget_digest = _BUDGETS.canonical_sha256()
    return ValidatedExecutionPlan(
        proposal_digest="2" * 64,
        manifest_digest="3" * 64,
        binding_snapshot_sha256="4" * 64,
        policy_version=_ASSIST_POLICY.policy_id,
        policy_sha256=_ASSIST_POLICY.policy_sha256,
        actor_binding_sha256="5" * 64,
        conversation_binding_sha256="6" * 64,
        authority_scope=PlanAuthorityScope.ASSIST_EXECUTION,
        authority_binding_sha256=durable_authority_binding_sha256(
            scope=PlanAuthorityScope.ASSIST_EXECUTION,
            actor_binding_sha256="5" * 64,
            conversation_binding_sha256="6" * 64,
            proposal_sha256="2" * 64,
            manifest_sha256="3" * 64,
            policy_sha256=_ASSIST_POLICY.policy_sha256,
            source_bindings_sha256=source_digest,
            capability_bindings_sha256="4" * 64,
            budget_sha256=budget_digest,
            required_security_ids=required_security_ids,
        ),
        required_security_ids=required_security_ids,
        source_bindings=(_SOURCE,),
        source_bindings_sha256=source_digest,
        budget_sha256=budget_digest,
        budgets=_BUDGETS,
        effect_classes=tuple(CapabilityEffectClass.READ for _ in steps),
        confirmation_required=False,
        confirmation_present=False,
        fallback_owner="primary_only",
        publication_owner="primary",
        steps=steps,
        _seal=mint_admission_seal(),
    )


def test_p2_primary_role_maps_to_one_distinct_p3_synthesis_node() -> None:
    bindings = bind_validated_plan_to_compare_current_file_web_graph(_plan())

    assert tuple(item.graph_kind for item in bindings) == tuple(CompareCurrentFileWebStepKind)
    synthesis = bindings[-1]
    assert synthesis.plan_step.capability_id == PRIMARY_SYNTHESIS_ID == "primary.synthesis"
    assert synthesis.graph_step_id == PRIMARY_SYNTHESIS_STEP_ID
    assert synthesis.graph_capability_id == PRIMARY_SYNTHESIS_CAPABILITY_ID == "model.primary.synthesis"


def test_p3_synthesis_alias_is_never_accepted_as_a_p2_plan_capability() -> None:
    plan = _plan()
    invalid_synthesis = replace(
        plan.steps[-1],
        capability_id=PRIMARY_SYNTHESIS_CAPABILITY_ID,
    )
    with pytest.raises(CompareCurrentFileWebGraphError, match="fixed P3 journey"):
        bind_validated_plan_to_compare_current_file_web_graph(
            replace(plan, steps=(*plan.steps[:2], invalid_synthesis))
        )


@pytest.mark.parametrize(
    "mutate_steps",
    (
        lambda steps: (
            replace(steps[0], max_calls=2),
            replace(steps[1], max_calls=1),
            steps[2],
        ),
        lambda steps: (
            steps[0],
            steps[1],
            replace(steps[2], max_output_tokens=1_023),
        ),
    ),
)
def test_graph_use_rejects_reallocated_or_drifted_step_budgets(
    mutate_steps: Callable[[tuple[ValidatedStep, ...]], tuple[ValidatedStep, ...]],
) -> None:
    plan = _plan()
    drifted = replace(plan, steps=mutate_steps(plan.steps))

    with pytest.raises(CompareCurrentFileWebGraphError, match="dependency shape"):
        bind_validated_plan_to_compare_current_file_web_graph(drifted)


def test_pending_admission_keeps_work_item_and_work_graph_bindings_typed() -> None:
    work = PendingDurableTurnAdmission.owned(
        person_id="alice",
        conversation_id="conv_1111111111111111",
        work_item_id="work_2222222222222222",
        revision=3,
    )
    graph = PendingDurableTurnAdmission.owned(
        person_id="alice",
        conversation_id="conv_1111111111111111",
        work_graph_id="graph_3333333333333333",
        revision=4,
    )

    assert work.binding_id == work.work_item_id
    assert work.work_graph_id is None
    assert graph.binding_id == graph.work_graph_id
    assert graph.work_item_id is None
    assert work.is_bound is graph.is_bound is True


@pytest.mark.parametrize(
    ("work_item_id", "work_graph_id", "revision"),
    (
        ("graph_3333333333333333", None, 1),
        (None, "work_2222222222222222", 1),
        ("work_2222222222222222", "graph_3333333333333333", 1),
        (None, "graph_3333333333333333", None),
    ),
)
def test_pending_admission_rejects_cross_kind_or_ambiguous_bindings(
    work_item_id: str | None,
    work_graph_id: str | None,
    revision: int | None,
) -> None:
    with pytest.raises(ValueError):
        PendingDurableTurnAdmission.owned(
            person_id="alice",
            conversation_id="conv_1111111111111111",
            work_item_id=work_item_id,
            work_graph_id=work_graph_id,
            revision=revision,
        )


def test_joined_primary_projection_preserves_opaque_work_item_digest() -> None:
    trace = build_work_trace(
        namespace_key=b"k" * 32,
        turn_identifier="msg_1111111111111111",
        conversation_identifier="conv_2222222222222222",
        work_item_identifier="graph_3333333333333333",
        work_relation=WorkRelation.NEW,
        intent=IntentClass.MIXED,
        playbook=PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES,
        capability_outcomes=(
            (CapabilityClass.DOCUMENT_RETRIEVAL, OutcomeStatus.SUCCEEDED),
            (CapabilityClass.WEB_RESEARCH, OutcomeStatus.SUCCEEDED),
            (CapabilityClass.MODEL_SYNTHESIS, OutcomeStatus.SUCCEEDED),
        ),
        capability_attempts=(1, 2, 1),
        continuation=ContinuationKind.NONE,
        completion=CompletionDecision.COMPLETE,
        failure_stage=FailureStage.NONE,
        failure_reason=FailureReason.NONE,
        ambiguity_present=False,
        partial_coverage=False,
        state_restored=False,
        latency_ms=12,
        model_calls=2,
        model_call_accounting=CountAccounting.COMPLETE,
        capability_calls=2,
        capability_call_accounting=CountAccounting.COMPLETE,
        authority_rechecked=True,
    )

    projection = PrimaryTraceProjection.from_trace(trace)
    assert trace.work_item_digest is not None
    assert projection.work_item_digest == trace.work_item_digest
    assert projection.payload()["work_item_digest"] == trace.work_item_digest
    assert projection.retry_occurred is True
    assert "graph_3333333333333333" not in str(projection.payload())
