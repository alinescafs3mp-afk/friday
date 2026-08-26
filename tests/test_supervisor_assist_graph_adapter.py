from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

import pytest

import friday.orchestration.current_file_web_comparison as comparison_module
import friday.orchestration.transient_web_comparison as web_module
from friday.execution_kernel import request_effect_possible, track_request_effects
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
    CompareCurrentFileWebGraphOutcomeReason,
    CompareCurrentFileWebGraphOutcomeStatus,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
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
    AssistRestartDisposition,
    AssistStepSettlement,
    AssistTerminalPublication,
    AssistTraceInput,
    SupervisorAssistGraphAdapter,
    SupervisorAssistGraphAdapterError,
)
from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.orchestration.supervisor_contracts import (
    CapabilityEffectClass,
    CompletionCriterion,
    canonical_sha256,
)
from friday.orchestration.supervisor_review_policy import AdmittedReadRecovery
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonEvidence,
    seal_explicit_public_web_query,
)
from friday.permissions import ActorContext
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
    )
    return surface, projection


def _plan(surface: CurrentFileWebAssistSurface) -> ValidatedExecutionPlan:
    query = surface.turn.message.rsplit('"', 2)[1]
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
        ),
    )
    return ValidatedExecutionPlan(
        proposal_digest=_sha256("proposal:" + surface.conversation_id),
        manifest_digest=_sha256("manifest:" + surface.conversation_id),
        binding_snapshot_sha256=_sha256("registry:" + surface.conversation_id),
        policy_version="semantic-supervisor-product-policy-v1",
        actor_binding_sha256=_sha256("actor:" + surface.actor.user_id),
        conversation_binding_sha256=_sha256("conversation:" + surface.conversation_id),
        effect_classes=tuple(step.effect_class for step in steps),
        confirmation_required=False,
        confirmation_present=False,
        fallback_owner="primary_only",
        publication_owner="primary",
        steps=steps,
        _seal=mint_admission_seal(),
    )


def _admit(adapter: SupervisorAssistGraphAdapter, surface: CurrentFileWebAssistSurface):
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
            authority_rechecked=(kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS and (
                accepted or state is CompareCurrentFileWebStepState.DENIED
            )),
            verified=accepted,
        ),
    )


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
    assert storage.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (other.conversation_id,)
    ).fetchone()[0] == 0
    source = Path("friday/orchestration/supervisor_assist_graph_adapter.py").read_text(
        encoding="utf-8"
    )
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
        assert (
            reader.load_current(AssistConversationScope(graph.user_id, graph.conversation_id))
            == graph
        )
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

    restarted = SupervisorAssistGraphAdapter(storage).retire_active_after_restart(limit=1)
    assert len(restarted.results) == 1
    assert (
        restarted.results[0].disposition
        is AssistRestartDisposition.RETIRED_EVIDENCE_NOT_REPLAYABLE
    )
    assert (
        restarted.results[0].publication.graph.outcome_reason
        is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    )


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
    cancelled = adapter.cancel(
        AssistGraphCursor.from_graph(cancel_graph),
        AssistCancellation("cancel", _trace()),
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
    restarted = SupervisorAssistGraphAdapter(storage).reconcile_all_active_after_restart(batch_limit=1)
    flattened = tuple(item for batch in restarted for item in batch.results)
    assert len(flattened) == 2
    assert all(
        item.disposition is AssistRestartDisposition.RETIRED_EVIDENCE_NOT_REPLAYABLE
        for item in flattened
    )
    first_retired = adapter.load(AssistGraphCursor.from_graph(first_graph))
    second_retired = adapter.load(AssistGraphCursor.from_graph(second_graph))
    assert first_retired is not None
    assert second_retired is not None
    assert (
        first_retired.outcome_reason
        is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    )
    assert (
        second_retired.outcome_reason
        is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    )
    for restart_surface in (first, second):
        assert storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'",
            (restart_surface.conversation_id,),
        ).fetchone()[0] == 1
