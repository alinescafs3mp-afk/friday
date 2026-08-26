from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    FILE_READ_STEP_ID,
    PRIMARY_SYNTHESIS_STEP_ID,
    WEB_READ_STEP_ID,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebWorkGraph,
    attach_compare_current_file_web_publication_receipt,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
    cancel_compare_current_file_web_work_graph_in_transaction,
    claim_compare_current_file_web_step_in_transaction,
    complete_compare_current_file_web_work_graph_in_transaction,
    create_compare_current_file_web_work_graph_in_transaction,
    settle_compare_current_file_web_step_in_transaction,
)
from friday.interaction_control_plane.runtime_trace import (
    INTERACTION_TRACE_METADATA_KEY,
    attach_trace_to_metadata,
    build_committed_direct_trace,
    build_work_trace,
    load_trace_namespace_key,
)
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
    TurnTrace,
    WorkRelation,
)
from friday.orchestration.supervisor_assist_promotion import (
    AssistPromotionDecision,
    AssistPromotionReadiness,
    AssistPromotionReason,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass, canonical_sha256
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PROMOTED_PRODUCT_EVENT,
    PromotedObservationEligibility,
    PromotedSupervisorProductObservation,
    PromotedUserVisibleOutcome,
)
from friday.orchestration.supervisor_promoted_product_event import (
    PromotedOutcomeEvaluatorAuthority,
    PromotedProductEmissionRequest,
    PromotedProductEventConflictError,
    PromotedProductEventError,
    PromotedProductEventReplayError,
    PromotedProductOutcomeInput,
    PromotedProductOutcomeReceipt,
    SupervisorLatencyBudgetDocument,
    emit_promoted_supervisor_product_event,
    load_accepted_supervisor_latency_budget,
)
from friday.storage._conversations import store_message_in_transaction
from friday.storage.models import RawObject, new_id

_OWNER = "promoted-product-owner"


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _next_instant(graph: CompareCurrentFileWebWorkGraph) -> str:
    return (datetime.fromisoformat(graph.updated_at) + timedelta(seconds=1)).isoformat()


def _promotion(mode: SupervisorMode = SupervisorMode.ASSIST) -> AssistPromotionDecision:
    return AssistPromotionDecision(
        promotion_admitted=True,
        readiness=AssistPromotionReadiness.LIVE_EVIDENCE_READY,
        reason=AssistPromotionReason.ADMITTED,
        requested_mode=mode,
        admitted_mode=mode,
        source_ready=True,
        live_evidence_ready=True,
        operator_gate_bound=True,
        evidence_sha256=_sha256(f"promotion:{mode.value}"),
    )


def _seed_graph(storage, label: str) -> CompareCurrentFileWebWorkGraph:
    storage.ensure_user(_OWNER, source="promoted-product-test")
    conversation = storage.create_conversation(_OWNER, f"promoted {label}")
    anchor = storage.store_message(
        str(conversation["id"]),
        _OWNER,
        "user",
        f"private request {label}",
    )
    raw_id = new_id("raw")
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=_OWNER,
            source="upload",
            source_ref=f"sha256:{_sha256(f'source:{label}')}",
            raw_content=f"private file body {label}",
            content_type="text/plain",
            content_hash=_sha256(f"content:{label}"),
        )
    )
    kinds = tuple(CompareCurrentFileWebStepKind)
    graph = CompareCurrentFileWebWorkGraph.admitted(
        user_id=_OWNER,
        conversation_id=str(conversation["id"]),
        anchor_user_message_id=str(anchor["id"]),
        current_file_raw_object_id=raw_id,
        proposal_sha256=_sha256(f"proposal:{label}"),
        accepted_plan_sha256=_sha256(f"plan:{label}"),
        manifest_sha256=_sha256(f"manifest:{label}"),
        policy_sha256=_sha256(f"policy:{label}"),
        runtime_profile_sha256=_sha256(f"runtime:{label}"),
        adapter_registry_sha256=_sha256(f"registry:{label}"),
        actor_binding_sha256=_sha256(f"actor:{label}"),
        conversation_binding_sha256=_sha256(f"conversation:{label}"),
        current_file_source_identity_sha256=_sha256(f"source-identity:{label}"),
        current_file_content_sha256=_sha256(f"content-identity:{label}"),
        step_input_identities={kind: _sha256(f"input:{label}:{kind.value}") for kind in kinds},
        step_idempotency_keys={kind: _sha256(f"key:{label}:{kind.value}") for kind in kinds},
        now="2026-08-26T10:00:00+00:00",
        expires_at="2026-08-26T22:00:00+00:00",
    )
    with storage.transaction() as conn:
        return create_compare_current_file_web_work_graph_in_transaction(conn, graph)


def _settle(
    storage,
    graph: CompareCurrentFileWebWorkGraph,
    step_id: str,
    state: CompareCurrentFileWebStepState,
) -> CompareCurrentFileWebWorkGraph:
    with storage.transaction() as conn:
        claimed = claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=step_id,
            now=_next_instant(graph),
        )
    step = claimed.step(step_id)
    usable = state in {
        CompareCurrentFileWebStepState.COMPLETE,
        CompareCurrentFileWebStepState.PARTIAL,
        CompareCurrentFileWebStepState.EMPTY,
    }
    is_read = step.kind in {
        CompareCurrentFileWebStepKind.FILE_READ,
        CompareCurrentFileWebStepKind.WEB_READ,
    }
    with storage.transaction() as conn:
        return settle_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=claimed.id,
            user_id=claimed.user_id,
            conversation_id=claimed.conversation_id,
            expected_revision=claimed.revision,
            step_id=step_id,
            state=state,
            outcome_sha256=_sha256(f"outcome:{claimed.id}:{step_id}:{state.value}"),
            evidence_identity_sha256=(
                _sha256(f"evidence:{claimed.id}:{step_id}:{state.value}") if usable else None
            ),
            authority_rechecked=is_read
            and state
            in {
                CompareCurrentFileWebStepState.COMPLETE,
                CompareCurrentFileWebStepState.PARTIAL,
                CompareCurrentFileWebStepState.EMPTY,
                CompareCurrentFileWebStepState.DENIED,
            },
            verified=usable,
            now=_next_instant(claimed),
        )


def _work_trace(
    conn,
    *,
    graph: CompareCurrentFileWebWorkGraph,
    turn_message_id: str,
    work_item_identifier: str,
    completion: CompletionDecision,
    failure_stage: FailureStage,
    failure_reason: FailureReason,
    authority_rechecked: bool,
) -> object:
    outcomes = (
        (CapabilityClass.DOCUMENT_RETRIEVAL, OutcomeStatus.SUCCEEDED),
        (
            CapabilityClass.WEB_RESEARCH,
            OutcomeStatus.SUCCEEDED if completion is CompletionDecision.COMPLETE else OutcomeStatus.DENIED,
        ),
        (
            CapabilityClass.MODEL_SYNTHESIS,
            OutcomeStatus.SUCCEEDED
            if completion is CompletionDecision.COMPLETE
            else OutcomeStatus.NOT_STARTED,
        ),
    )
    return build_work_trace(
        namespace_key=load_trace_namespace_key(conn),
        turn_identifier=turn_message_id,
        conversation_identifier=graph.conversation_id,
        work_item_identifier=work_item_identifier,
        work_relation=WorkRelation.NEW,
        intent=IntentClass.MIXED,
        playbook=PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES,
        capability_outcomes=outcomes,
        continuation=ContinuationKind.NONE,
        completion=completion,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        ambiguity_present=False,
        partial_coverage=completion is not CompletionDecision.COMPLETE,
        state_restored=False,
        latency_ms=730,
        model_calls=1,
        model_call_accounting=CountAccounting.LOWER_BOUND,
        capability_calls=2,
        capability_call_accounting=CountAccounting.COMPLETE,
        authority_rechecked=authority_rechecked,
    )


def _complete_graph(
    storage,
    label: str,
    *,
    work_item_identifier: str | None = None,
) -> tuple[CompareCurrentFileWebWorkGraph, object, str]:
    graph = _seed_graph(storage, label)
    for step_id in (FILE_READ_STEP_ID, WEB_READ_STEP_ID, PRIMARY_SYNTHESIS_STEP_ID):
        graph = _settle(storage, graph, step_id, CompareCurrentFileWebStepState.COMPLETE)
    receipt = graph.publication_receipt(final_authority_rechecked=True)
    with storage.transaction() as conn:
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            "PRIVATE COMPLETED ANSWER",
            {},
            graph.anchor_user_message_id,
        )
        trace = _work_trace(
            conn,
            graph=graph,
            turn_message_id=graph.anchor_user_message_id,
            work_item_identifier=work_item_identifier or graph.id,
            completion=CompletionDecision.COMPLETE,
            failure_stage=FailureStage.NONE,
            failure_reason=FailureReason.NONE,
            authority_rechecked=True,
        )
        metadata = attach_compare_current_file_web_publication_receipt({}, receipt)
        assert attach_trace_to_metadata(metadata, trace)  # type: ignore[arg-type]
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), str(assistant["id"])),
        )
        completed = complete_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            now=_next_instant(graph),
        )
    return completed, trace, receipt.canonical_sha256()


def _terminal_graph(storage, label: str) -> tuple[CompareCurrentFileWebWorkGraph, object, str]:
    graph = _seed_graph(storage, label)
    with storage.transaction() as conn:
        graph = claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=FILE_READ_STEP_ID,
            now=_next_instant(graph),
        )
    with storage.transaction() as conn:
        terminal = cancel_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            now=_next_instant(graph),
        )
    assistant = storage.get_message(
        str(terminal.publication_assistant_message_id),
        terminal.user_id,
    )
    assert assistant is not None
    metadata = json.loads(str(assistant["metadata_json"]))
    trace = TurnTrace.parse(metadata[INTERACTION_TRACE_METADATA_KEY])
    assert terminal.terminal_publication_receipt_sha256 is not None
    return terminal, trace, terminal.terminal_publication_receipt_sha256


def _event_payload(storage) -> dict[str, object]:
    row = storage.execute(
        "SELECT payload FROM runtime_events WHERE event_type=? ORDER BY rowid DESC LIMIT 1",
        (SUPERVISOR_PROMOTED_PRODUCT_EVENT,),
    ).fetchone()
    assert row is not None
    return json.loads(str(row["payload"]))


def test_full_graph_event_requires_exact_committed_trace_and_receipt(storage) -> None:
    graph, trace, receipt_sha256 = _complete_graph(storage, "full")
    trace_sha256 = canonical_sha256(trace.to_payload())  # type: ignore[attr-defined]

    emitted = emit_promoted_supervisor_product_event(
        storage.conn,
        promotion_decision=_promotion(),
        request=PromotedProductEmissionRequest(
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=trace_sha256,
            execution_receipt_sha256=receipt_sha256,
            supervisor_invoked=True,
        ),
    )

    payload = _event_payload(storage)
    event = PromotedSupervisorProductObservation.parse(payload)
    assert event.primary_trace_sha256 == trace_sha256
    assert event.execution_receipt_sha256 == receipt_sha256
    assert event.promotion_evidence_sha256 == _promotion().evidence_sha256
    assert event.user_visible_outcome is PromotedUserVisibleOutcome.NOT_EVALUATED
    assert emitted.event_sha256 == canonical_sha256(payload)
    serialized = json.dumps(payload, sort_keys=True)
    for forbidden in (
        graph.id,
        graph.user_id,
        graph.conversation_id,
        graph.anchor_user_message_id,
        str(graph.publication_assistant_message_id),
        "PRIVATE COMPLETED ANSWER",
        "private request",
        "private file body",
    ):
        assert forbidden not in serialized


def test_exact_terminal_receipt_can_emit_without_a_completion_claim(storage) -> None:
    graph, trace, receipt_sha256 = _terminal_graph(storage, "terminal")

    emit_promoted_supervisor_product_event(
        storage.conn,
        promotion_decision=_promotion(),
        request=PromotedProductEmissionRequest(
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=canonical_sha256(trace.to_payload()),  # type: ignore[attr-defined]
            execution_receipt_sha256=receipt_sha256,
            supervisor_invoked=True,
        ),
    )

    event = PromotedSupervisorProductObservation.parse(_event_payload(storage))
    assert graph.state.value == "terminal"
    assert event.execution_receipt_sha256 == receipt_sha256
    assert event.user_visible_outcome is PromotedUserVisibleOutcome.NOT_EVALUATED


def test_precommit_and_mismatched_work_item_trace_fail_without_event(storage) -> None:
    active = _seed_graph(storage, "precommit")
    active = _settle(
        storage,
        active,
        FILE_READ_STEP_ID,
        CompareCurrentFileWebStepState.UNAVAILABLE,
    )
    active = _settle(
        storage,
        active,
        WEB_READ_STEP_ID,
        CompareCurrentFileWebStepState.DENIED,
    )
    prepared = active.terminal_publication_receipt(final_authority_rechecked=False)
    with pytest.raises(PromotedProductEventError, match="not one exact durable"):
        emit_promoted_supervisor_product_event(
            storage.conn,
            promotion_decision=_promotion(),
            request=PromotedProductEmissionRequest(
                eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
                primary_trace_sha256=_sha256("uncommitted-trace"),
                execution_receipt_sha256=prepared.canonical_sha256(),
                supervisor_invoked=True,
            ),
        )

    _graph, wrong_trace, receipt_sha256 = _complete_graph(
        storage,
        "wrong-work-item",
        work_item_identifier="graph_aaaaaaaaaaaaaaaa",
    )
    with pytest.raises(PromotedProductEventError, match="durable identities"):
        emit_promoted_supervisor_product_event(
            storage.conn,
            promotion_decision=_promotion(),
            request=PromotedProductEmissionRequest(
                eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
                primary_trace_sha256=canonical_sha256(wrong_trace.to_payload()),  # type: ignore[attr-defined]
                execution_receipt_sha256=receipt_sha256,
                supervisor_invoked=True,
            ),
        )

    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM runtime_events WHERE event_type=?",
            (SUPERVISOR_PROMOTED_PRODUCT_EVENT,),
        ).fetchone()["count"]
        == 0
    )


def test_replay_is_rejected_and_does_not_duplicate_the_event(storage) -> None:
    _graph, trace, receipt_sha256 = _complete_graph(storage, "replay")
    request = PromotedProductEmissionRequest(
        eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
        primary_trace_sha256=canonical_sha256(trace.to_payload()),  # type: ignore[attr-defined]
        execution_receipt_sha256=receipt_sha256,
        supervisor_invoked=True,
    )
    emit_promoted_supervisor_product_event(
        storage.conn,
        promotion_decision=_promotion(),
        request=request,
    )

    with pytest.raises(PromotedProductEventReplayError):
        emit_promoted_supervisor_product_event(
            storage.conn,
            promotion_decision=_promotion(),
            request=request,
        )

    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM runtime_events WHERE event_type=?",
            (SUPERVISOR_PROMOTED_PRODUCT_EVENT,),
        ).fetchone()["count"]
        == 1
    )


def test_conflicting_trace_or_receipt_binding_is_rejected(storage) -> None:
    _graph, trace, receipt_sha256 = _complete_graph(storage, "conflict")
    trace_sha256 = canonical_sha256(trace.to_payload())  # type: ignore[attr-defined]
    conflicting = PromotedSupervisorProductObservation(
        mode=SupervisorMode.ASSIST,
        task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
        primary_trace_sha256=trace_sha256,
        promotion_evidence_sha256=_sha256("different-promotion"),
        execution_receipt_sha256=receipt_sha256,
        supervisor_invoked=True,
        user_visible_outcome=PromotedUserVisibleOutcome.NOT_EVALUATED,
    )
    storage.record_event(SUPERVISOR_PROMOTED_PRODUCT_EVENT, conflicting.payload())

    with pytest.raises(PromotedProductEventConflictError, match="conflicting"):
        emit_promoted_supervisor_product_event(
            storage.conn,
            promotion_decision=_promotion(),
            request=PromotedProductEmissionRequest(
                eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
                primary_trace_sha256=trace_sha256,
                execution_receipt_sha256=receipt_sha256,
                supervisor_invoked=True,
            ),
        )

    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM runtime_events WHERE event_type=?",
            (SUPERVISOR_PROMOTED_PRODUCT_EVENT,),
        ).fetchone()["count"]
        == 1
    )


class _FreeLabelEvaluator:
    def evaluate(self, _outcome_input: PromotedProductOutcomeInput) -> object:
        return "no_regression"


class _TypedEvaluator:
    def evaluate(
        self,
        outcome_input: PromotedProductOutcomeInput,
    ) -> PromotedProductOutcomeReceipt:
        return PromotedProductOutcomeReceipt(
            input_sha256=outcome_input.canonical_sha256(),
            outcome=PromotedUserVisibleOutcome.NO_REGRESSION,
            evaluator_authority=PromotedOutcomeEvaluatorAuthority.CODE_OWNED,
            evaluator_evidence_sha256=_sha256("mechanical-regression-evaluation"),
        )


def test_free_form_outcome_fails_to_not_evaluated_but_typed_receipt_can_bind(storage) -> None:
    _graph, trace, receipt_sha256 = _complete_graph(storage, "free-label")
    request = PromotedProductEmissionRequest(
        eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
        primary_trace_sha256=canonical_sha256(trace.to_payload()),  # type: ignore[attr-defined]
        execution_receipt_sha256=receipt_sha256,
        supervisor_invoked=True,
    )
    emit_promoted_supervisor_product_event(
        storage.conn,
        promotion_decision=_promotion(),
        request=request,
        outcome_evaluator=_FreeLabelEvaluator(),  # type: ignore[arg-type]
    )
    first = PromotedSupervisorProductObservation.parse(_event_payload(storage))
    assert first.user_visible_outcome is PromotedUserVisibleOutcome.NOT_EVALUATED

    _graph, trace, receipt_sha256 = _complete_graph(storage, "typed-label")
    emit_promoted_supervisor_product_event(
        storage.conn,
        promotion_decision=_promotion(),
        request=PromotedProductEmissionRequest(
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=canonical_sha256(trace.to_payload()),  # type: ignore[attr-defined]
            execution_receipt_sha256=receipt_sha256,
            supervisor_invoked=True,
        ),
        outcome_evaluator=_TypedEvaluator(),
    )
    second = PromotedSupervisorProductObservation.parse(_event_payload(storage))
    assert second.user_visible_outcome is PromotedUserVisibleOutcome.NO_REGRESSION


def test_other_turn_adds_a_body_free_false_invocation_denominator(storage) -> None:
    storage.ensure_user(_OWNER, source="promoted-product-test")
    conversation = storage.create_conversation(_OWNER, "ordinary")
    with storage.transaction() as conn:
        turn = store_message_in_transaction(
            conn,
            str(conversation["id"]),
            _OWNER,
            "user",
            "PRIVATE ORDINARY REQUEST",
        )
        assistant = store_message_in_transaction(
            conn,
            str(conversation["id"]),
            _OWNER,
            "assistant",
            "PRIVATE ORDINARY ANSWER",
            {},
            str(turn["id"]),
        )
        trace = build_committed_direct_trace(
            namespace_key=load_trace_namespace_key(conn),
            turn_identifier=str(turn["id"]),
            conversation_identifier=str(conversation["id"]),
            intent=IntentClass.ORDINARY_DIALOGUE,
            playbook=PlaybookClass.DIRECT,
            capabilities=(CapabilityClass.MODEL_SYNTHESIS,),
            latency_ms=90,
            model_calls=1,
            model_call_accounting=CountAccounting.COMPLETE,
            capability_calls=0,
            capability_call_accounting=CountAccounting.COMPLETE,
            authority_rechecked=False,
        )
        metadata: dict[str, object] = {}
        assert attach_trace_to_metadata(metadata, trace)  # type: ignore[arg-type]
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), str(assistant["id"])),
        )
    trace_sha256 = canonical_sha256(trace.to_payload())

    emit_promoted_supervisor_product_event(
        storage.conn,
        promotion_decision=_promotion(),
        request=PromotedProductEmissionRequest(
            eligibility=PromotedObservationEligibility.OTHER_TURN,
            primary_trace_sha256=trace_sha256,
            execution_receipt_sha256=None,
            supervisor_invoked=False,
        ),
        outcome_evaluator=_TypedEvaluator(),
    )

    event = PromotedSupervisorProductObservation.parse(_event_payload(storage))
    assert event.eligibility is PromotedObservationEligibility.OTHER_TURN
    assert event.task_class is None
    assert event.execution_receipt_sha256 is None
    assert event.supervisor_invoked is False
    assert event.user_visible_outcome is PromotedUserVisibleOutcome.NOT_EVALUATED
    assert _OWNER not in json.dumps(event.payload())


def test_latency_budget_document_is_exact_hash_closed_and_body_free() -> None:
    document = SupervisorLatencyBudgetDocument(
        target_mode=SupervisorMode.ASSIST,
        source_revision_sha256=_sha256("release"),
        maximum_user_visible_latency_ms=2_500,
    )
    raw = json.dumps(
        document.payload(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256(raw).hexdigest()

    accepted = load_accepted_supervisor_latency_budget(
        raw,
        expected_document_sha256=digest,
    )

    assert accepted.document == document
    assert accepted.document_sha256 == digest
    assert not {"body", "path", "query", "user_id", "conversation_id"} & set(accepted.document.payload())
    with pytest.raises(PromotedProductEventError, match="digest does not match"):
        load_accepted_supervisor_latency_budget(
            raw,
            expected_document_sha256=_sha256("wrong"),
        )
    extended = dict(document.payload())
    extended["body"] = "PRIVATE BUDGET PROSE"
    extended_raw = json.dumps(extended, sort_keys=True).encode("utf-8")
    with pytest.raises(PromotedProductEventError, match="keys do not match"):
        load_accepted_supervisor_latency_budget(
            extended_raw,
            expected_document_sha256=hashlib.sha256(extended_raw).hexdigest(),
        )


def test_emission_rejects_untyped_inputs_and_an_open_transaction(storage) -> None:
    with pytest.raises(PromotedProductEventError, match="exact closed contract"):
        emit_promoted_supervisor_product_event(
            storage.conn,
            promotion_decision=_promotion(),
            request={"body": "PRIVATE"},  # type: ignore[arg-type]
        )
    with storage.transaction() as conn, pytest.raises(PromotedProductEventError, match="post-commit"):
        emit_promoted_supervisor_product_event(
            conn,
            promotion_decision=_promotion(),
            request=PromotedProductEmissionRequest(
                eligibility=PromotedObservationEligibility.OTHER_TURN,
                primary_trace_sha256=_sha256("trace"),
                execution_receipt_sha256=None,
                supervisor_invoked=False,
            ),
        )
