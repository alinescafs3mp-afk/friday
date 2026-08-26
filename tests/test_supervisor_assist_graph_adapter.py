from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

import pytest

import friday.orchestration.current_file_web_comparison as comparison_module
import friday.orchestration.transient_web_comparison as web_module
from friday import semantic_supervisor_policy
from friday.execution_kernel import request_effect_possible, track_request_effects
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE,
    CompareCurrentFileWebGraphOutcomeReason,
    CompareCurrentFileWebGraphOutcomeStatus,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    load_compare_current_file_web_terminal_publication_receipt,
)
from friday.interaction_control_plane.runtime_trace import INTERACTION_TRACE_METADATA_KEY
from friday.interaction_control_plane.turn_trace import (
    CountAccounting,
    TraceIdentifierDomain,
    TurnTrace,
    derive_trace_identifier,
)
from friday.model_profiles import ModelProfileLease
from friday.orchestration.contracts import TurnInput
from friday.orchestration.current_file_web_comparison import (
    CurrentFileWebComparison,
    CurrentFileWebComparisonStatus,
    current_file_web_comparison_binding_sha256,
)
from friday.orchestration.execution_plan import (
    ValidatedExecutionPlan,
    ValidatedStep,
    mint_admission_seal,
)
from friday.orchestration.file_read import _file_requirements  # noqa: PLC2701
from friday.orchestration.router import ReadOnlyAttachmentReference
from friday.orchestration.supervisor_assist_graph_adapter import (
    AssistCancellation,
    AssistComparisonPublication,
    AssistConversationScope,
    AssistGraphAdmission,
    AssistGraphCursor,
    AssistMixedAuthorityTerminalPublication,
    AssistPublicationAction,
    AssistRestartDisposition,
    AssistStepSettlement,
    AssistTerminalPublication,
    AssistTraceInput,
    SupervisorAssistGraphAdapter,
    SupervisorAssistGraphAdapterError,
)
from friday.orchestration.supervisor_assist_ingress import SupervisorAssistIngressBindingV1
from friday.orchestration.supervisor_assist_production import (
    SupervisorAssistAuthorityGate,
    SupervisorAssistRestartActorResolver,
    supervisor_assist_read_only_effect_gate,
)
from friday.orchestration.supervisor_assist_recovery import (
    SupervisorAssistRecoverySurfaceLoader,
)
from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.orchestration.supervisor_contracts import (
    CapabilityEffectClass,
    CompletionCriterion,
    SupervisorBudgets,
    canonical_sha256,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityScope,
    PlanSourceBinding,
    durable_authority_binding_sha256,
    source_bindings_sha256,
)
from friday.orchestration.supervisor_review_policy import AdmittedReadRecovery
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonEvidence,
    seal_explicit_public_web_query,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import authorized_file_snapshot_token, raw_source_identity_sha256
from friday.storage.models import RawObject


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _trace() -> AssistTraceInput:
    return AssistTraceInput(
        latency_ms=10,
        model_calls=2,
        model_call_accounting=CountAccounting.COMPLETE,
        capability_calls=2,
        capability_call_accounting=CountAccounting.COMPLETE,
    )


def _restart_actor(graph: Any) -> ActorContext:
    return ActorContext(str(graph.user_id), "owner", "semantic-recovery")


def _restart_authority(actor: ActorContext, boundary: Any) -> bool:
    return bool(boundary.actor is actor and boundary.user_id == actor.user_id)


def _restart_effect(actor: ActorContext, boundary: Any) -> bool:
    return bool(boundary.actor is actor)


def _seed_surface(storage, label: str) -> tuple[CurrentFileWebAssistSurface, dict[str, object]]:
    user_id = f"assist-{label}"
    storage.ensure_user(user_id, source="assist-adapter-test")
    conversation = storage.create_conversation(user_id, "assist adapter", mode="dialogue")
    raw_id = f"raw_{hashlib.sha256(label.encode()).hexdigest()[:16]}"
    raw = RawObject(
        id=raw_id,
        user_id=user_id,
        source="upload",
        source_ref=f"sha256:{_sha256('source:' + label)}",
        raw_content=f"private body {label}",
        content_type="text/plain",
        metadata_json={},
        content_hash=_sha256("content:" + label),
        received_at="2026-08-26T08:00:00+00:00",
        created_at="2026-08-26T08:00:00+00:00",
    )
    storage.store_raw_object(raw)
    row = storage.execute(
        """SELECT id,source,source_ref,content_type,received_at,content_hash,
                  raw_content AS _raw_content,metadata_json AS _raw_metadata
             FROM raw_objects WHERE id=? AND user_id=?""",
        (raw_id, user_id),
    ).fetchone()
    assert row is not None
    projection = dict(row)
    actor = ActorContext(user_id, "owner", "test")
    query = f"current public facts {label}"
    message = f'Сравни файл.\nPublic web query: "{query}"'
    turn = TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=str(conversation["id"]),
        attachments=[],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    surface = CurrentFileWebAssistSurface(
        turn=turn,
        actor=actor,
        conversation_id=str(conversation["id"]),
        attachment=ReadOnlyAttachmentReference(
            ordinal=1,
            raw_object_id=raw_id,
            source_identity_sha256=raw_source_identity_sha256(projection),
            name="current.txt",
            media_type="text/plain",
        ),
        attachment_content_sha256=raw.content_hash,
        web_plan=seal_explicit_public_web_query(
            current_user_message=message,
            actor=actor,
            conversation_id=str(conversation["id"]),
        ),
        ingress_binding=SupervisorAssistIngressBindingV1.from_claimed_request(
            source_ref=f"assist-adapter:{label}",
            request_fingerprint_sha256=_sha256(f"request:{label}"),
        ),
    )
    return surface, projection


def _plan(surface: CurrentFileWebAssistSurface) -> ValidatedExecutionPlan:
    query = surface.turn.message.rsplit('"', 2)[1]
    policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode("assist")
    budgets = SupervisorBudgets(
        max_steps=policy.max_steps,
        max_parallel_reads=policy.max_parallel_reads,
        turn_deadline_ms=policy.turn_deadline_ms,
        per_step_deadline_ms=policy.per_step_deadline_ms,
        max_supervisor_calls=policy.max_supervisor_calls,
        max_model_calls=policy.max_model_calls,
        max_tool_calls=policy.max_tool_calls,
        max_capability_calls=policy.max_capability_calls,
        max_review_rounds=policy.max_review_rounds,
        max_recovery_rounds=policy.max_recovery_rounds,
        max_output_tokens=policy.max_output_tokens,
    )
    steps = (
        ValidatedStep(
            step_id="s1",
            capability_id="file.current.read",
            effect_class=CapabilityEffectClass.READ,
            resolved_security_id="files.read",
            resolved_tool_id="file_read",
            resolved_adapter_id="friday.orchestration.file_read.V12FileReadHandler",
            depends_on=(),
            parallel_group="evidence",
            input={"attachment_ordinal": 1},
            idempotency_key=_sha256("file:" + surface.conversation_id),
            deadline_ms=budgets.per_step_deadline_ms,
            max_calls=1,
            max_output_tokens=0,
        ),
        ValidatedStep(
            step_id="s2",
            capability_id="web.search.current",
            effect_class=CapabilityEffectClass.READ,
            resolved_security_id="web.compare.transient",
            resolved_tool_id=(
                "friday.orchestration.transient_web_comparison.TransientWebComparisonAdapter.research"
            ),
            resolved_adapter_id="transient_web_comparison",
            depends_on=(),
            parallel_group="evidence",
            input={"query_intent": query},
            idempotency_key=_sha256("web:" + surface.conversation_id),
            deadline_ms=budgets.per_step_deadline_ms,
            max_calls=2,
            max_output_tokens=0,
        ),
        ValidatedStep(
            step_id="s3",
            capability_id="primary.synthesis",
            effect_class=CapabilityEffectClass.READ,
            resolved_security_id=None,
            resolved_tool_id=None,
            resolved_adapter_id=None,
            depends_on=("s1", "s2"),
            parallel_group=None,
            input={},
            idempotency_key=_sha256("model:" + surface.conversation_id),
            deadline_ms=budgets.per_step_deadline_ms,
            max_calls=semantic_supervisor_policy.SUPERVISOR_PRIMARY_MODEL_CALLS,
            max_output_tokens=semantic_supervisor_policy.SUPERVISOR_PRIMARY_OUTPUT_TOKENS,
        ),
    )
    proposal_digest = _sha256("proposal:" + surface.conversation_id)
    manifest_digest = _sha256("manifest:" + surface.conversation_id)
    binding_snapshot_sha256 = _sha256("registry:" + surface.conversation_id)
    actor_binding_sha256 = _sha256("actor:" + surface.actor.user_id)
    conversation_binding_sha256 = _sha256("conversation:" + surface.conversation_id)
    source = PlanSourceBinding.current_raw_object(
        raw_object_id=surface.attachment.raw_object_id,
        source_identity_sha256=surface.attachment.source_identity_sha256,
        content_sha256=surface.attachment_content_sha256,
    )
    source_digest = source_bindings_sha256((source,))
    budget_digest = budgets.canonical_sha256()
    required_security_ids = tuple(
        sorted(step.resolved_security_id for step in steps if step.resolved_security_id is not None)
    )
    return ValidatedExecutionPlan(
        proposal_digest=proposal_digest,
        manifest_digest=manifest_digest,
        binding_snapshot_sha256=binding_snapshot_sha256,
        policy_version=policy.policy_id,
        policy_sha256=policy.policy_sha256,
        actor_binding_sha256=actor_binding_sha256,
        conversation_binding_sha256=conversation_binding_sha256,
        authority_scope=PlanAuthorityScope.ASSIST_EXECUTION,
        authority_binding_sha256=durable_authority_binding_sha256(
            scope=PlanAuthorityScope.ASSIST_EXECUTION,
            actor_binding_sha256=actor_binding_sha256,
            conversation_binding_sha256=conversation_binding_sha256,
            proposal_sha256=proposal_digest,
            manifest_sha256=manifest_digest,
            policy_sha256=policy.policy_sha256,
            source_bindings_sha256=source_digest,
            capability_bindings_sha256=binding_snapshot_sha256,
            budget_sha256=budget_digest,
            required_security_ids=required_security_ids,
        ),
        required_security_ids=required_security_ids,
        source_bindings=(source,),
        source_bindings_sha256=source_digest,
        budget_sha256=budget_digest,
        budgets=budgets,
        effect_classes=tuple(step.effect_class for step in steps),
        confirmation_required=False,
        confirmation_present=False,
        fallback_owner="primary_only",
        publication_owner="primary",
        steps=steps,
        _seal=mint_admission_seal(),
    )


def _admit(adapter: SupervisorAssistGraphAdapter, surface: CurrentFileWebAssistSurface):
    with track_request_effects(
        lambda: True,
        before_effect_in_transaction=lambda _conn: True,
        request_binding_sha256=surface.ingress_binding.canonical_sha256(),
    ):
        return adapter.admit(
            AssistGraphAdmission(surface, _plan(surface), _sha256("runtime")),
            authority_check=lambda boundary: boundary.actor is surface.actor,
            effect_check=lambda boundary: boundary.actor is surface.actor,
        )


def _claim(adapter, graph, surface, kind):
    return adapter.claim(
        AssistGraphCursor.from_graph(graph),
        kind,
        surface=surface,
        authority_check=lambda boundary: boundary.actor is surface.actor,
        effect_check=lambda boundary: boundary.actor is surface.actor,
    ).graph


def _settle(adapter, graph, kind, state, evidence=None):
    accepted = state in {
        CompareCurrentFileWebStepState.COMPLETE,
        CompareCurrentFileWebStepState.PARTIAL,
        CompareCurrentFileWebStepState.EMPTY,
    }
    return adapter.settle(
        AssistGraphCursor.from_graph(graph),
        AssistStepSettlement(
            kind=kind,
            state=state,
            outcome_sha256=_sha256(f"outcome:{graph.id}:{kind.value}:{state.value}"),
            evidence_identity_sha256=evidence if accepted else None,
            authority_rechecked=(
                kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS
                and (accepted or state is CompareCurrentFileWebStepState.DENIED)
            ),
            verified=accepted,
        ),
    )


def _mixed_authority_graph(
    adapter: SupervisorAssistGraphAdapter,
    surface: CurrentFileWebAssistSurface,
    *,
    denied_kind: CompareCurrentFileWebStepKind,
):
    graph = _admit(adapter, surface)
    for kind in (
        CompareCurrentFileWebStepKind.FILE_READ,
        CompareCurrentFileWebStepKind.WEB_READ,
    ):
        graph = _claim(adapter, graph, surface, kind)
        graph = _settle(
            adapter,
            graph,
            kind,
            (
                CompareCurrentFileWebStepState.DENIED
                if kind is denied_kind
                else CompareCurrentFileWebStepState.COMPLETE
            ),
            None if kind is denied_kind else _sha256(f"usable:{graph.id}:{kind.value}"),
        )
    return graph


def test_admission_is_one_existing_dialogue_transaction_and_never_upgrades_file_token(storage) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, _projection = _seed_surface(storage, "admit")
    graph = _admit(adapter, surface)

    assert graph.state is CompareCurrentFileWebGraphState.ACTIVE
    assert graph.current_file_raw_object_id == surface.attachment.raw_object_id
    messages = storage.execute(
        "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY rowid",
        (surface.conversation_id,),
    ).fetchall()
    assert [(row["role"], row["content"]) for row in messages] == [("user", surface.turn.message)]

    other, _ = _seed_surface(storage, "deny")
    with pytest.raises(SupervisorAssistGraphAdapterError, match="authority"):
        adapter.admit(
            AssistGraphAdmission(other, _plan(other), _sha256("runtime")),
            authority_check=lambda _boundary: False,
            effect_check=lambda _boundary: True,
        )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (other.conversation_id,)
        ).fetchone()[0]
        == 0
    )
    source = Path("friday/orchestration/supervisor_assist_graph_adapter.py").read_text(encoding="utf-8")
    assert "prepare_current_turn_file_evidence" not in source


def test_graph_lookup_is_effect_free_and_never_opens_a_writer(storage) -> None:
    writer = SupervisorAssistGraphAdapter(storage)
    surface, _ = _seed_surface(storage, "lookup")
    graph = _admit(writer, surface)
    observed_before = storage.execute(
        "SELECT observed_at FROM relation_revision_context WHERE singleton=1"
    ).fetchone()[0]

    class ReadOnlyStorageProbe:
        def __init__(self, delegate: Any) -> None:
            self.conn = delegate.conn

        def transaction(self) -> NoReturn:
            raise AssertionError("graph lookup must not enter the storage writer path")

    fence_calls: list[str] = []

    def before_effect() -> bool:
        fence_calls.append("zero-argument")
        return True

    def before_effect_in_transaction(_conn: object) -> bool:
        fence_calls.append("transaction")
        return True

    reader = SupervisorAssistGraphAdapter(ReadOnlyStorageProbe(storage))
    with track_request_effects(
        before_effect,
        before_effect_in_transaction=before_effect_in_transaction,
    ) as effects:
        assert reader.load(AssistGraphCursor.from_graph(graph)) == graph
        assert reader.load_current(AssistConversationScope(graph.user_id, graph.conversation_id)) == graph
        assert request_effect_possible() is False
        assert effects.staged is False

    observed_after = storage.execute(
        "SELECT observed_at FROM relation_revision_context WHERE singleton=1"
    ).fetchone()[0]
    assert observed_after == observed_before
    assert fence_calls == []


def test_claim_denial_is_atomic_and_review_recovery_is_typed(storage) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, _ = _seed_surface(storage, "claim")
    graph = _admit(adapter, surface)
    with pytest.raises(SupervisorAssistGraphAdapterError, match="capability authority"):
        adapter.claim(
            AssistGraphCursor.from_graph(graph),
            CompareCurrentFileWebStepKind.WEB_READ,
            surface=surface,
            authority_check=lambda _boundary: False,
            effect_check=lambda _boundary: True,
        )
    current = adapter.load_current(AssistConversationScope(graph.user_id, graph.conversation_id))
    assert current == graph

    assert current.steps[1].state is CompareCurrentFileWebStepState.PENDING

    graph = _claim(adapter, graph, surface, CompareCurrentFileWebStepKind.WEB_READ)
    graph = _settle(
        adapter,
        graph,
        CompareCurrentFileWebStepKind.WEB_READ,
        CompareCurrentFileWebStepState.FAILED,
    )
    step = graph.steps[1]
    assert step.outcome_sha256 is not None
    recovery = AdmittedReadRecovery(
        step_id=step.step_id,
        capability_id=step.capability_id,
        criterion=CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE,
        idempotency_key=step.idempotency_key_sha256,
        review_digest=_sha256("review"),
        context_digest=_sha256("context"),
    )
    graph = adapter.admit_review_recovery(AssistGraphCursor.from_graph(graph), recovery)
    assert graph.steps[1].state is CompareCurrentFileWebStepState.PENDING
    assert graph.steps[1].prior_outcome_sha256 == step.outcome_sha256

    restarted = SupervisorAssistGraphAdapter(storage).retire_active_after_restart(
        actor_resolver=_restart_actor,
        authority_check=_restart_authority,
        effect_check=_restart_effect,
        limit=1,
    )
    assert len(restarted.results) == 1
    assert restarted.results[0].disposition is AssistRestartDisposition.RETIRED_EVIDENCE_NOT_REPLAYABLE
    assert (
        restarted.results[0].publication.graph.outcome_reason
        is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    )


def test_restart_scan_pages_one_stable_insertion_snapshot(storage: Any) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    first_surface, _ = _seed_surface(storage, "restart-scan-first")
    second_surface, _ = _seed_surface(storage, "restart-scan-second")
    first = _admit(adapter, first_surface)
    second = _admit(adapter, second_surface)

    first_page = adapter.active_after_restart(limit=1)
    assert first_page.has_more is True
    assert first_page.next_after_rowid is not None
    assert first_page.snapshot_upper_rowid is not None
    assert tuple(item.graph_id for item in first_page.cursors) == (first.id,)

    later_surface, _ = _seed_surface(storage, "restart-scan-later")
    later = _admit(adapter, later_surface)
    second_page = adapter.active_after_restart(
        limit=1,
        after_rowid=first_page.next_after_rowid,
        snapshot_upper_rowid=first_page.snapshot_upper_rowid,
    )
    assert second_page.has_more is False
    assert second_page.next_after_rowid is None
    assert second_page.snapshot_upper_rowid == first_page.snapshot_upper_rowid
    assert tuple(item.graph_id for item in second_page.cursors) == (second.id,)

    fresh = adapter.active_after_restart(limit=100)
    assert {item.graph_id for item in fresh.cursors} == {first.id, second.id, later.id}


@pytest.mark.parametrize(
    "denied_kind",
    [
        CompareCurrentFileWebStepKind.FILE_READ,
        CompareCurrentFileWebStepKind.WEB_READ,
    ],
)
def test_mixed_authority_denial_terminalizes_atomically_without_model(
    storage: Any,
    denied_kind: CompareCurrentFileWebStepKind,
) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, _ = _seed_surface(storage, f"mixed-{denied_kind.value}")
    graph = _mixed_authority_graph(adapter, surface, denied_kind=denied_kind)
    request = AssistMixedAuthorityTerminalPublication(_trace())
    boundary_actions: list[AssistPublicationAction] = []

    with pytest.raises(SupervisorAssistGraphAdapterError, match="authority"):
        adapter.publish_terminal_after_mixed_authority_denial(
            AssistGraphCursor.from_graph(graph),
            request,
            authority_check=lambda _boundary: False,
            effect_check=lambda _boundary: True,
        )
    assert adapter.load(AssistGraphCursor.from_graph(graph)) == graph

    def authority(boundary: Any) -> bool:
        boundary_actions.append(boundary.action)
        return bool(
            boundary.actor is surface.actor
            and boundary.expected_status is CompareCurrentFileWebGraphOutcomeStatus.DENIED
            and boundary.expected_reason is CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED
        )

    published = adapter.publish_terminal_after_mixed_authority_denial(
        AssistGraphCursor.from_graph(graph),
        request,
        authority_check=authority,
        effect_check=lambda boundary: boundary.actor is surface.actor,
    )

    assert boundary_actions == [AssistPublicationAction.TERMINAL]
    assert published.graph.state is CompareCurrentFileWebGraphState.TERMINAL
    assert published.graph.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.DENIED
    assert published.graph.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED
    assert published.graph.revision == graph.revision + 3
    synthesis = published.graph.steps[2]
    assert synthesis.state is CompareCurrentFileWebStepState.UNAVAILABLE
    assert synthesis.attempt == 1
    assert synthesis.evidence_identity_sha256 is None
    assert published.public_citations == ()
    messages = storage.execute(
        "SELECT role,metadata_json FROM messages WHERE conversation_id=? ORDER BY rowid",
        (surface.conversation_id,),
    ).fetchall()
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert all("private body" not in row["metadata_json"] for row in messages)

    with pytest.raises(SupervisorAssistGraphAdapterError):
        adapter.publish_terminal_after_mixed_authority_denial(
            AssistGraphCursor.from_graph(graph),
            request,
            authority_check=lambda _boundary: True,
            effect_check=lambda _boundary: True,
        )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 1
    )


def test_mixed_authority_terminal_fails_closed_on_restart_drift_and_forgery(storage: Any) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    restart_surface, _ = _seed_surface(storage, "mixed-restart")
    restart_graph = _mixed_authority_graph(
        adapter,
        restart_surface,
        denied_kind=CompareCurrentFileWebStepKind.WEB_READ,
    )
    restarted_adapter = SupervisorAssistGraphAdapter(storage)
    with pytest.raises(SupervisorAssistGraphAdapterError, match="process actor"):
        restarted_adapter.publish_terminal_after_mixed_authority_denial(
            AssistGraphCursor.from_graph(restart_graph),
            AssistMixedAuthorityTerminalPublication(_trace()),
            authority_check=lambda _boundary: True,
            effect_check=lambda _boundary: True,
        )
    restarted = restarted_adapter.reconcile_all_active_after_restart(
        actor_resolver=_restart_actor,
        authority_check=_restart_authority,
        effect_check=_restart_effect,
        batch_limit=1,
    )
    assert restarted[-1].has_more is False
    retired = restarted_adapter.load(AssistGraphCursor.from_graph(restart_graph))
    assert retired is not None
    assert retired.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE

    drift_surface, _ = _seed_surface(storage, "mixed-drift")
    drift_graph = _mixed_authority_graph(
        adapter,
        drift_surface,
        denied_kind=CompareCurrentFileWebStepKind.FILE_READ,
    )
    storage.archive_conversation(drift_surface.conversation_id, drift_surface.actor.user_id)
    with pytest.raises(SupervisorAssistGraphAdapterError, match="live dialogue"):
        adapter.publish_terminal_after_mixed_authority_denial(
            AssistGraphCursor.from_graph(drift_graph),
            AssistMixedAuthorityTerminalPublication(_trace()),
            authority_check=lambda _boundary: True,
            effect_check=lambda _boundary: True,
        )
    assert adapter.load(AssistGraphCursor.from_graph(drift_graph)) == drift_graph

    forged_surface, _ = _seed_surface(storage, "mixed-forged")
    forged_graph = _mixed_authority_graph(
        adapter,
        forged_surface,
        denied_kind=CompareCurrentFileWebStepKind.WEB_READ,
    )
    forged_request = AssistMixedAuthorityTerminalPublication(_trace())
    object.__setattr__(forged_request, "trace", object())
    with pytest.raises(ValueError, match="mixed-authority"):
        adapter.publish_terminal_after_mixed_authority_denial(
            AssistGraphCursor.from_graph(forged_graph),
            forged_request,
            authority_check=lambda _boundary: True,
            effect_check=lambda _boundary: True,
        )
    assert adapter.load(AssistGraphCursor.from_graph(forged_graph)) == forged_graph


def _web_evidence(surface: CurrentFileWebAssistSurface) -> TransientWebComparisonEvidence:
    query = surface.turn.message.rsplit('"', 2)[1]
    return web_module._project_report(  # noqa: PLC2701
        surface.web_plan,
        {
            "query": query,
            "sources": [
                {
                    "url": "https://public.example/current",
                    "title": "Public source",
                    "text": "Current public fact.",
                    "text_length": len("Current public fact."),
                    "status_code": 200,
                    "error": "",
                    "truncated": False,
                }
            ],
            "requested_sources": 1,
            "completed_sources": 1,
            "failed_sources": 0,
            "timed_out_sources": 0,
            "search_timed_out": False,
        },
    )


def _comparison(plan_sha256: str, file_sha256: str, web_sha256: str) -> CurrentFileWebComparison:
    requirements = _file_requirements(2)
    lease = ModelProfileLease(
        profile_id="assist-adapter-test:dispatcher",
        attestation_sha256="a" * 64,
        requirements_sha256=requirements.canonical_sha256(),
        capabilities=requirements.capabilities,
        required_context_tokens=requirements.required_context_tokens,
        prepared_evidence_items=requirements.prepared_evidence_items,
        max_tool_steps=requirements.max_tool_steps,
        effect=requirements.effect,
        verifier_required=requirements.verifier_required,
        process_epoch_sha256="b" * 64,
        _gate_authority=object(),
        _gate_generation=1,
    )
    answer = "Файл фиксирует локальный факт [F1]. Источник подтверждает его [W1]."
    source_sha256 = canonical_sha256(
        {
            "file_evidence_sha256": file_sha256,
            "schema": "friday.current-file-web-source-evidence-identity.v1",
            "web_evidence_sha256": web_sha256,
        }
    )
    model_sha256 = _sha256("model evidence")
    binding = current_file_web_comparison_binding_sha256(
        accepted_plan_sha256=plan_sha256,
        source_evidence_sha256=source_sha256,
        model_evidence_sha256=model_sha256,
        status=CurrentFileWebComparisonStatus.COMPLETE,
        partial_reasons=(),
    )
    identity = comparison_module._result_identity_payload(  # noqa: PLC2701
        answer=answer,
        status=CurrentFileWebComparisonStatus.COMPLETE,
        partial_reasons=(),
        accepted_plan_sha256=plan_sha256,
        file_evidence_sha256=file_sha256,
        web_evidence_sha256=web_sha256,
        source_evidence_sha256=source_sha256,
        model_evidence_sha256=model_sha256,
        binding_sha256=binding,
        citation_labels=("F1", "W1"),
        model_calls=2,
    )
    return CurrentFileWebComparison(
        answer=answer,
        status=CurrentFileWebComparisonStatus.COMPLETE,
        partial_reasons=(),
        accepted_plan_sha256=plan_sha256,
        file_evidence_sha256=file_sha256,
        web_evidence_sha256=web_sha256,
        source_evidence_sha256=source_sha256,
        model_evidence_sha256=model_sha256,
        binding_sha256=binding,
        citation_labels=("F1", "W1"),
        model_calls=2,
        lease=lease,
        requirements=requirements,
        _process_seal_sha256=comparison_module._process_seal(  # noqa: PLC2701
            identity, lease=lease, requirements=requirements
        ),
        _process_authority=comparison_module._PROCESS_AUTHORITY,  # noqa: PLC2701
    )


def test_comparison_settlement_trace_assistant_receipt_and_closure_are_atomic(storage) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, projection = _seed_surface(storage, "publish")
    graph = _admit(adapter, surface)
    file_sha256 = _sha256("file evidence")
    web = _web_evidence(surface)
    graph = _settle(
        adapter,
        _claim(adapter, graph, surface, CompareCurrentFileWebStepKind.FILE_READ),
        CompareCurrentFileWebStepKind.FILE_READ,
        CompareCurrentFileWebStepState.COMPLETE,
        file_sha256,
    )
    graph = _settle(
        adapter,
        _claim(adapter, graph, surface, CompareCurrentFileWebStepKind.WEB_READ),
        CompareCurrentFileWebStepKind.WEB_READ,
        CompareCurrentFileWebStepState.COMPLETE,
        web.canonical_sha256(),
    )
    graph = _claim(adapter, graph, surface, CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS)
    token = authorized_file_snapshot_token(
        projection,
        content_sha256=surface.attachment_content_sha256,
    )
    assert token is not None
    comparison = _comparison(graph.accepted_plan_sha256, file_sha256, web.canonical_sha256())
    published = adapter.publish_comparison(
        AssistGraphCursor.from_graph(graph),
        AssistComparisonPublication(token, comparison, web, _trace()),
        authority_check=lambda boundary: boundary.actor is surface.actor,
        effect_check=lambda boundary: boundary.actor is surface.actor,
    )

    assert published.graph.state is CompareCurrentFileWebGraphState.COMPLETED
    assert published.graph.publication_receipt_sha256 == published.execution_receipt_sha256
    row = storage.execute(
        "SELECT content,metadata_json,reply_to FROM messages WHERE id=?",
        (published.assistant_message_id,),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    trace = TurnTrace.parse(metadata[INTERACTION_TRACE_METADATA_KEY])
    namespace = storage.execute(
        "SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'"
    ).fetchone()[0]
    from friday.audit_privacy import decode_audit_privacy_key

    assert trace.turn_digest == derive_trace_identifier(
        domain=TraceIdentifierDomain.TURN,
        raw_identifier=graph.anchor_user_message_id,
        namespace_key=decode_audit_privacy_key(namespace),
    )
    assert row["reply_to"] == graph.anchor_user_message_id
    assert web.sources[0]._text not in row["metadata_json"]  # noqa: SLF001


def test_cancel_rejects_wrong_request_effect_fence_atomically(storage) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, _ = _seed_surface(storage, "cancel-fence")
    graph = _admit(adapter, surface)
    expected_binding = _sha256("cancel-fence:expected")

    with (
        track_request_effects(
            lambda: True,
            before_effect_in_transaction=lambda _conn: True,
            request_binding_sha256=_sha256("cancel-fence:actual"),
        ),
        pytest.raises(SupervisorAssistGraphAdapterError, match="request effect fence"),
    ):
        adapter.cancel(
            AssistGraphCursor.from_graph(graph),
            AssistCancellation(
                "cancel",
                _trace(),
                request_binding_sha256=expected_binding,
            ),
            authority_check=lambda _boundary: True,
            effect_check=lambda _boundary: True,
        )

    current = adapter.load_current(AssistConversationScope(surface.actor.user_id, surface.conversation_id))
    rows = storage.execute(
        "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY rowid",
        (surface.conversation_id,),
    ).fetchall()
    assert current is not None and current.state is CompareCurrentFileWebGraphState.ACTIVE
    assert [(row["role"], row["content"]) for row in rows] == [("user", surface.turn.message)]


def test_terminal_cancel_and_startup_reconcile_publish_closed_receipts(storage) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, _ = _seed_surface(storage, "terminal")
    graph = _admit(adapter, surface)
    for kind in (CompareCurrentFileWebStepKind.FILE_READ, CompareCurrentFileWebStepKind.WEB_READ):
        graph = _settle(
            adapter,
            _claim(adapter, graph, surface, kind),
            kind,
            CompareCurrentFileWebStepState.EMPTY,
            _sha256(f"empty:{kind.value}"),
        )
    status, reason = graph.terminal_disposition()
    terminal = adapter.publish_terminal(
        AssistGraphCursor.from_graph(graph),
        AssistTerminalPublication(status, reason, _trace()),
        authority_check=lambda _boundary: True,
        effect_check=lambda _boundary: True,
    )
    assert terminal.graph.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.EMPTY
    assert terminal.graph.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.NO_COMPARABLE_EVIDENCE

    cancel_surface, _ = _seed_surface(storage, "cancel")
    cancel_graph = _admit(adapter, cancel_surface)
    cancel_request_binding_sha256 = _sha256("cancel-request")
    with track_request_effects(
        lambda: True,
        before_effect_in_transaction=lambda _conn: True,
        request_binding_sha256=cancel_request_binding_sha256,
    ):
        cancelled = adapter.cancel(
            AssistGraphCursor.from_graph(cancel_graph),
            AssistCancellation(
                "cancel",
                _trace(),
                request_binding_sha256=cancel_request_binding_sha256,
            ),
            authority_check=lambda boundary: boundary.actor is cancel_surface.actor,
            effect_check=lambda boundary: boundary.actor is cancel_surface.actor,
        )
    roles = storage.execute(
        "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY rowid",
        (cancel_surface.conversation_id,),
    ).fetchall()
    assert [(row["role"], row["content"]) for row in roles] == [
        ("user", cancel_surface.turn.message),
        ("user", "cancel"),
        ("assistant", COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE),
    ]
    assert cancelled.graph.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.CANCELLED

    first, _ = _seed_surface(storage, "restart-one")
    second, _ = _seed_surface(storage, "restart-two")
    first_graph = _admit(adapter, first)
    second_graph = _admit(adapter, second)
    restarted = SupervisorAssistGraphAdapter(storage).reconcile_all_active_after_restart(
        actor_resolver=_restart_actor,
        authority_check=_restart_authority,
        effect_check=_restart_effect,
        batch_limit=1,
    )
    flattened = tuple(item for batch in restarted for item in batch.results)
    assert len(flattened) == 2
    assert all(
        item.disposition is AssistRestartDisposition.RETIRED_EVIDENCE_NOT_REPLAYABLE for item in flattened
    )
    first_retired = adapter.load(AssistGraphCursor.from_graph(first_graph))
    second_retired = adapter.load(AssistGraphCursor.from_graph(second_graph))
    assert first_retired is not None
    assert second_retired is not None
    assert first_retired.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    assert second_retired.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    for restart_surface in (first, second):
        assert (
            storage.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'",
                (restart_surface.conversation_id,),
            ).fetchone()[0]
            == 1
        )


def test_startup_recovery_rechecks_production_boundaries_and_does_not_mask_faults(
    storage: Any,
) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surfaces: dict[str, CurrentFileWebAssistSurface] = {}
    graphs: dict[str, Any] = {}
    for label in ("current", "denied", "drift", "inactive", "archived"):
        surface, _ = _seed_surface(storage, f"restart-production-{label}")
        storage.update_user(surface.actor.user_id, preset_key="user")
        surfaces[label] = surface
        graphs[label] = _admit(adapter, surface)

    authorization = AuthorizationService(storage)
    for surface in surfaces.values():
        authorization.grant_permission(surface.actor.user_id, "web.compare.transient")
    recovered = SupervisorAssistRecoverySurfaceLoader(storage, authorization)(graphs["current"])
    assert recovered is not None
    assert recovered.graph == graphs["current"]
    assert recovered.surface.turn.message == surfaces["current"].turn.message
    assert recovered.surface.actor is not surfaces["current"].actor
    assert recovered.surface.ingress_binding == surfaces["current"].ingress_binding
    assert recovered.surface.attachment.raw_object_id == graphs["current"].current_file_raw_object_id
    authorization.deny_permission(surfaces["denied"].actor.user_id, "files.read")
    storage.update_user(surfaces["inactive"].actor.user_id, status="disabled")
    storage.archive_conversation(
        surfaces["archived"].conversation_id,
        surfaces["archived"].actor.user_id,
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET content_hash=? WHERE id=? AND user_id=?",
            (
                _sha256("changed-after-admission"),
                surfaces["drift"].attachment.raw_object_id,
                surfaces["drift"].actor.user_id,
            ),
        )
    loader = SupervisorAssistRecoverySurfaceLoader(storage, authorization)
    assert loader(graphs["drift"]) is None
    assert loader(graphs["inactive"]) is None
    assert loader(graphs["archived"]) is None

    gate = SupervisorAssistAuthorityGate(storage, authorization)
    checked: list[tuple[str, AssistPublicationAction]] = []

    def authority(actor: ActorContext, boundary: Any) -> bool:
        checked.append((actor.user_id, boundary.action))
        return gate(actor, boundary)

    batches = SupervisorAssistGraphAdapter(storage).reconcile_all_active_after_restart(
        actor_resolver=SupervisorAssistRestartActorResolver(authorization),
        authority_check=authority,
        effect_check=lambda _actor, boundary: supervisor_assist_read_only_effect_gate(boundary),
        batch_limit=2,
    )
    results = tuple(item for batch in batches for item in batch.results)
    retained = tuple(item for batch in batches for item in batch.retained)
    assert len(results) == 1
    assert results[0].publication.graph.id == graphs["current"].id
    assert {item.graph_id for item in retained} == {
        graphs[label].id for label in ("denied", "drift", "inactive", "archived")
    }
    assert (
        surfaces["current"].actor.user_id,
        AssistPublicationAction.RESTART_RETIREMENT,
    ) in checked
    for label in ("denied", "drift", "inactive", "archived"):
        current = adapter.load(AssistGraphCursor.from_graph(graphs[label]))
        assert current is not None and current.state is CompareCurrentFileWebGraphState.ACTIVE
        assistant_count = storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (surfaces[label].conversation_id,),
        ).fetchone()[0]
        assert assistant_count == 0

    fault_surface, _ = _seed_surface(storage, "restart-production-fault")
    fault_graph = _admit(adapter, fault_surface)
    with pytest.raises(RuntimeError, match="injected recovery fault"):
        SupervisorAssistGraphAdapter(storage).retire_active_after_restart(
            actor_resolver=_restart_actor,
            authority_check=lambda _actor, _boundary: (_ for _ in ()).throw(
                RuntimeError("injected recovery fault")
            ),
            effect_check=_restart_effect,
            limit=100,
        )
    assert adapter.load(AssistGraphCursor.from_graph(fault_graph)) == fault_graph


def test_expiry_is_bounded_source_free_and_does_not_mask_lifecycle_faults(storage: Any) -> None:
    adapter = SupervisorAssistGraphAdapter(storage)
    surface, _ = _seed_surface(storage, "expiry-production")
    graph = _admit(adapter, surface)
    storage.update_user(surface.actor.user_id, status="disabled")
    storage.archive_conversation(surface.conversation_id, surface.actor.user_id)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET content_hash=? WHERE id=? AND user_id=?",
            (
                _sha256("expiry-source-drift"),
                surface.attachment.raw_object_id,
                surface.actor.user_id,
            ),
        )

    denied = SupervisorAssistGraphAdapter(storage).expire_due(
        lifecycle_check=lambda _boundary: False,
        now=graph.expires_at,
    )
    assert denied.retired == ()
    assert tuple(item.graph_id for item in denied.retained) == (graph.id,)
    assert adapter.load(AssistGraphCursor.from_graph(graph)) == graph
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 0
    )

    def fail_lifecycle(_boundary: Any) -> bool:
        raise RuntimeError("injected expiry lifecycle fault")

    with pytest.raises(RuntimeError, match="injected expiry lifecycle fault"):
        SupervisorAssistGraphAdapter(storage).expire_due(
            lifecycle_check=fail_lifecycle,
            now=graph.expires_at,
        )
    assert adapter.load(AssistGraphCursor.from_graph(graph)) == graph

    retired = SupervisorAssistGraphAdapter(storage).expire_due(
        lifecycle_check=supervisor_assist_read_only_effect_gate,
        now=graph.expires_at,
    )
    assert len(retired.retired) == 1
    assert retired.retained == ()
    assert retired.retired[0].outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EXPIRED
    assistant = storage.execute(
        """SELECT content,metadata_json FROM messages
             WHERE conversation_id=? AND role='assistant'""",
        (surface.conversation_id,),
    ).fetchone()
    assert assistant is not None and assistant["content"] == COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE
    receipt = load_compare_current_file_web_terminal_publication_receipt(str(assistant["metadata_json"]))
    assert receipt.model_spoke is receipt.evidence_cited is False
    assert receipt.final_authority_rechecked is receipt.completion_claimed is False
