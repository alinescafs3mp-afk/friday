from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

import friday.orchestration.transient_web_comparison as web_module
from friday import semantic_supervisor_policy
from friday.evidence_bundle import CitationBinding, EvidenceBundle, EvidencePart
from friday.execution_kernel import track_request_effects
from friday.file_evidence import (
    FileBodyKind,
    FileEvidenceSet,
    FileEvidenceView,
    FileRegistrationKind,
    stamp_current_turn_file_reference_for_tenant,
)
from friday.file_evidence_reader import (
    _PROCESS_AUTHORITY as _FILE_EVIDENCE_AUTHORITY,  # noqa: PLC2701
)
from friday.file_evidence_reader import FileEvidenceUnavailable, PreparedFileEvidence
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    FILE_READ_STEP_ID,
    WEB_READ_STEP_ID,
    CompareCurrentFileWebGraphOutcomeReason,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebWorkGraph,
)
from friday.interaction_control_plane.runtime_trace import INTERACTION_TRACE_METADATA_KEY
from friday.interaction_control_plane.turn_trace import TurnTrace
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    operational_capability_snapshot,
)
from friday.orchestration.contracts import TurnInput
from friday.orchestration.policy_kernel import PolicyAdmissionContext, admit_supervisor_proposal
from friday.orchestration.router import ReadOnlyAttachmentReference
from friday.orchestration.semantic_supervisor import (
    ParsedSupervisorProposal,
    binding_digest,
    build_supervisor_input,
)
from friday.orchestration.supervisor_assist_controller import (
    SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA,
    AssistObservationStatus,
    AssistPendingGraphDisposition,
    SupervisorAssistController,
    SupervisorAssistOutcome,
)
from friday.orchestration.supervisor_assist_graph_adapter import (
    AssistCapabilityBoundary,
    AssistConversationScope,
    AssistGraphCursor,
    AssistGraphPublication,
    AssistPublicationAction,
    AssistPublicationBoundary,
    SupervisorAssistGraphAdapter,
)
from friday.orchestration.supervisor_assist_ingress import (
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.orchestration.supervisor_assist_promotion import (
    AssistPromotionDecision,
    AssistPromotionReadiness,
    AssistPromotionReason,
)
from friday.orchestration.supervisor_assist_recovery import (
    SupervisorAssistRecoverySurfaceLoader,
)
from friday.orchestration.supervisor_assist_surface import (
    CurrentFileWebAssistSurface,
    prepare_current_file_web_assist_surface,
)
from friday.orchestration.supervisor_contracts import (
    SUPERVISOR_PROPOSAL_SCHEMA,
    CompletionCriterion,
    ReviewRecommendedAction,
    ReviewVerdict,
    SupervisorInput,
    SupervisorMode,
    SupervisorProposal,
    SupervisorReview,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityBoundary,
    PlanAuthorityDecision,
    PlanAuthorityReason,
    PlanAuthorityScope,
    PlanSourceBinding,
    attest_plan_authority,
    source_bindings_sha256,
)
from friday.orchestration.supervisor_plan_authority_gate import (
    SupervisorAssistPlanAuthorityGate,
)
from friday.orchestration.supervisor_review_policy import (
    SupervisorReviewContext,
    admit_supervisor_review,
)
from friday.orchestration.supervisor_review_transport import AdmittedSupervisorReview
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonEvidence,
    TransientWebEvidenceStatus,
)
from friday.orchestration.turn_context import TurnContextError
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext, AuthorizationError, AuthorizationService
from friday.source_identity import (
    raw_source_identity_sha256,
    tenant_authorized_file_snapshot_token,
)
from friday.storage.models import RawObject

_INGRESS_BINDING = SupervisorAssistIngressBindingV1.from_claimed_request(
    source_ref="assist-controller:test",
    request_fingerprint_sha256="f" * 64,
)


@pytest.fixture(autouse=True)
def _exact_request_effect_fence():
    with track_request_effects(
        lambda: True,
        before_effect_in_transaction=lambda _conn: True,
        request_binding_sha256=_INGRESS_BINDING.canonical_sha256(),
    ):
        yield


def _pending_decision(
    relation: SupervisorAssistPendingRelation,
    pending: PendingDurableTurnAdmission,
) -> SupervisorAssistPendingDecision:
    return SupervisorAssistPendingDecision.for_graph(
        relation=relation,
        pending=pending,
        root_request_binding_sha256=_INGRESS_BINDING.canonical_sha256(),
        current=(
            _INGRESS_BINDING
            if relation is SupervisorAssistPendingRelation.ROOT_REPLAY
            else SupervisorAssistIngressBindingV1.from_claimed_request(
                source_ref=f"assist-controller:{relation.value}",
                request_fingerprint_sha256="e" * 64,
            )
        ),
    )


async def _cancel_pending(
    controller: SupervisorAssistController,
    scope: AssistConversationScope,
    pending: PendingDurableTurnAdmission,
    *,
    user_message: str = "cancel",
) -> Any:
    decision = _pending_decision(SupervisorAssistPendingRelation.EXPLICIT_CANCEL, pending)
    with track_request_effects(
        lambda: True,
        before_effect_in_transaction=lambda _conn: True,
        request_binding_sha256=decision.current_request_binding_sha256,
    ):
        return await controller.cancel_active(
            scope,
            decision=decision,
            user_message=user_message,
            absolute_deadline=time.monotonic() + 3,
        )


class _Carrier(dict[str, Any]):
    pass


def _settings(mode: SupervisorMode = SupervisorMode.ASSIST) -> SimpleNamespace:
    return SimpleNamespace(
        semantic_supervisor_mode=mode.value,
        semantic_supervisor_tasks=("compare_current_file_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )


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
        evidence_sha256="e" * 64,
    )


class _Promotion:
    def __init__(
        self,
        *,
        admitted: bool = True,
        mode: SupervisorMode = SupervisorMode.ASSIST,
    ) -> None:
        self.admitted = admitted
        self.mode = mode
        self.calls = 0
        self.actor_bindings: list[str | None] = []
        self.scheduler = SimpleNamespace()

    def decide(
        self,
        *,
        binding_snapshot: CapabilityBindingSnapshot,
        actor_binding_sha256: str | None = None,
    ) -> AssistPromotionDecision | None:
        assert type(binding_snapshot) is CapabilityBindingSnapshot
        self.calls += 1
        self.actor_bindings.append(actor_binding_sha256)
        return _promotion(self.mode) if self.admitted else None


class _Planner:
    def __init__(self, *, admit: bool = True) -> None:
        self.admit = admit
        self.calls = 0

    async def propose(
        self,
        supervisor_input: SupervisorInput,
        context: PolicyAdmissionContext,
        *,
        absolute_deadline: float,
        pre_dispatch_validator: Callable[[], bool] | None = None,
    ) -> ParsedSupervisorProposal | None:
        self.calls += 1
        assert absolute_deadline > time.monotonic()
        if not self.admit or pre_dispatch_validator is None or pre_dispatch_validator() is not True:
            return None
        query = "актуальные публичные правила 2026"
        review_modes = (
            ("secondary_after_deterministic_checks", "none")
            if supervisor_input.budgets.max_review_rounds == 1
            else ("none",)
        )
        for review_mode in review_modes:
            proposal = SupervisorProposal.parse(
                {
                    "schema": SUPERVISOR_PROPOSAL_SCHEMA,
                    "manifest_id": supervisor_input.manifest.manifest_id,
                    "budget_sha256": supervisor_input.budgets.canonical_sha256(),
                    "task_class": "compare_current_file_with_current_web",
                    "goal": "Compare the supplied file with current public evidence.",
                    "continuation_decision": "new_task",
                    "risk_hints": ["external_read", "multi_source"],
                    "steps": [
                        {
                            "step_id": "s1",
                            "kind": "capability",
                            "target_id": "file.current.read",
                            "purpose": "Read the current file.",
                            "depends_on": [],
                            "parallel_group": "evidence",
                            "input": {"attachment_ordinal": 1},
                            "expected_outcome": "complete_source_evidence",
                        },
                        {
                            "step_id": "s2",
                            "kind": "capability",
                            "target_id": "web.search.current",
                            "purpose": "Read current public evidence.",
                            "depends_on": [],
                            "parallel_group": "evidence",
                            "input": {"query_intent": query},
                            "expected_outcome": "verified_current_sources",
                        },
                        {
                            "step_id": "s3",
                            "kind": "model",
                            "target_id": "primary.synthesis",
                            "purpose": "Compare admitted evidence with citations.",
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
                    "review_mode": review_mode,
                    "fallback": "primary_only",
                }
            )
            decision = admit_supervisor_proposal(proposal, supervisor_input, context)
            if decision.admitted and decision.plan is not None:
                return ParsedSupervisorProposal(
                    proposal_digest=decision.plan.proposal_digest,
                    decision=decision,
                )
        return None


class _Primary:
    def __init__(self, readiness: tuple[bool, ...] = (True,)) -> None:
        self.readiness = readiness
        self.prepare_calls = 0
        self.acquire_calls = 0
        self.lease_checks = 0
        self.calls: list[list[dict[str, Any]]] = []
        self.requirements: ModelRequirements | None = None
        self.lease: ModelProfileLease | None = None

    async def prepare_primary_model(self, *, absolute_deadline: float) -> bool:
        assert absolute_deadline > time.monotonic()
        index = min(self.prepare_calls, len(self.readiness) - 1)
        self.prepare_calls += 1
        return self.readiness[index]

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease:
        assert absolute_deadline > time.monotonic()
        self.acquire_calls += 1
        self.requirements = requirements
        self.lease = ModelProfileLease(
            profile_id="assist-controller-test:dispatcher",
            attestation_sha256="a" * 64,
            requirements_sha256=requirements.canonical_sha256(),
            capabilities=requirements.capabilities,
            required_context_tokens=requirements.required_context_tokens,
            prepared_evidence_items=requirements.prepared_evidence_items,
            max_tool_steps=requirements.max_tool_steps,
            effect=requirements.effect,
            verifier_required=requirements.verifier_required,
            process_epoch_sha256="b" * 64,
            _gate_authority=self,
            _gate_generation=1,
        )
        return self.lease

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        assert absolute_deadline > time.monotonic()
        assert lease is self.lease and requirements is self.requirements
        self.lease_checks += 1
        return True

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]:
        assert lease is self.lease and requirements is self.requirements
        assert max_tokens and priority == "foreground" and temperature == 0.0
        assert absolute_deadline > time.monotonic()
        self.calls.append(messages)
        if len(self.calls) == 1:
            payload = json.loads(str(messages[-1]["content"]))
            labels = payload["trusted_control"]["citation_labels"]
            answer = " ".join(f"Проверенный факт [{label}]." for label in labels)
            return {"content": answer, "finish_reason": "stop", "tool_calls": None}
        payload = json.loads(str(messages[-1]["content"]))
        labels = ["F1", *(row["label"] for row in payload["evidence"]["web"]["sources"])]
        return {
            "content": json.dumps(
                {
                    "schema": "friday.v12-file-verifier.v1",
                    "supported": True,
                    "citation_labels": labels,
                    "unsupported_claims": 0,
                }
            ),
            "finish_reason": "stop",
            "tool_calls": None,
        }


def _surface(*, actor: ActorContext | None = None) -> CurrentFileWebAssistSurface:
    actor = actor or ActorContext("local:alice", "owner", "test")
    message = (
        "Сравни текущий файл с текущими данными в интернете.\n"
        "Публичный веб-запрос: «актуальные публичные правила 2026»"
    )
    raw_id = "raw_1234567890abcdef"
    carrier = _Carrier(
        {
            "raw_object_id": raw_id,
            "persisted": True,
            "current_turn_only": True,
            "mime_type": "text/plain",
            "transient_text": "private body",
            "extraction_success": True,
        }
    )
    stamp_current_turn_file_reference_for_tenant(
        carrier,
        {
            "id": raw_id,
            "user_id": actor.user_id,
            "source": "upload",
            "source_ref": "sha256:" + "1" * 64,
            "content_type": "text/plain",
            "received_at": "2026-08-26T00:00:00+00:00",
            "content_hash": "2" * 64,
            "raw_content": "private body",
            "metadata_json": "{}",
        },
        tenant_id=actor.user_id,
    )
    surface = prepare_current_file_web_assist_surface(
        _settings(),
        user_id=actor.user_id,
        message=message,
        actor=actor,
        conversation_id="conv_1234567890abcdef",
        attachments=[carrier],
        enable_tools=True,
        ingestion_result=None,
        synthetic_document_notice=False,
        replay_source_message_id=None,
        mode=None,
        explicit_mode_requested=False,
        answer_with_voice=False,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
        reply_assistant_message_id=None,
        turn_policy=None,
        pending_durable_admission=None,
        ingress_binding=_INGRESS_BINDING,
        conversation_is_dialogue=lambda person_id, conversation_id: (
            person_id == actor.own_id and conversation_id == "conv_1234567890abcdef"
        ),
    )
    assert type(surface) is CurrentFileWebAssistSurface
    return surface


def _stored_surface(
    storage: Any,
    label: str,
) -> tuple[CurrentFileWebAssistSurface, dict[str, object]]:
    user_id = f"assist-{label}"
    storage.ensure_user(user_id, source="assist-controller-test")
    conversation = storage.create_conversation(user_id, "assist controller", mode="dialogue")
    raw_id = f"raw_{hashlib.sha256(label.encode()).hexdigest()[:16]}"
    text = f"Локальный проверенный факт {label}."
    content_sha256 = hashlib.sha256(text.encode()).hexdigest()
    raw = RawObject(
        id=raw_id,
        user_id=user_id,
        source="upload",
        source_ref=f"sha256:{hashlib.sha256(('source:' + label).encode()).hexdigest()}",
        raw_content=text,
        content_type="text/plain",
        metadata_json={},
        content_hash=content_sha256,
        received_at="2026-08-26T08:00:00+00:00",
        created_at="2026-08-26T08:00:00+00:00",
    )
    storage.store_raw_object(raw)
    row = storage.execute(
        """SELECT id,user_id,source,source_ref,content_type,received_at,content_hash,
                  raw_content AS _raw_content,metadata_json AS _raw_metadata
             FROM raw_objects WHERE id=? AND user_id=?""",
        (raw_id, user_id),
    ).fetchone()
    assert row is not None
    projection = dict(row)
    actor = ActorContext(user_id, "owner", "test")
    message = (
        "Сравни текущий файл с текущими данными в интернете.\n"
        "Публичный веб-запрос: «актуальные публичные правила 2026»"
    )
    turn = TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=str(conversation["id"]),
        attachments=[
            {
                "mime_type": "text/plain",
                "size_bytes": len(text.encode()),
                "transient_text": text,
            }
        ],
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
        attachment_content_sha256=content_sha256,
        web_plan=web_module.seal_explicit_public_web_query(
            current_user_message=message,
            actor=actor,
            conversation_id=str(conversation["id"]),
        ),
        ingress_binding=_INGRESS_BINDING,
    )
    return surface, projection


def _prepared_file(
    surface: CurrentFileWebAssistSurface,
    projection: dict[str, object],
) -> PreparedFileEvidence:
    text = str(projection["_raw_content"])
    token = tenant_authorized_file_snapshot_token(
        projection,
        content_sha256=surface.attachment_content_sha256,
        tenant_id=surface.actor.user_id,
        storage_owner_id=surface.actor.user_id,
    )
    assert token is not None
    view = FileEvidenceView(
        raw_id=surface.attachment.raw_object_id,
        source_identity_sha256=token.source.identity_sha256,
        registration=FileRegistrationKind.VALID,
        disk_verified=True,
        workspace_relative_path=None,
        workspace_sha256=None,
        workspace_source_sha256=None,
        body_kind=FileBodyKind.EXTRACTED,
        source_complete=True,
        projection_applied=False,
        projection_empty_no_match=False,
        source_readable=True,
        verification_eligible=True,
    )
    evidence_set = FileEvidenceSet(items=(view,), expected_count=1)
    part = EvidencePart(
        label="A1",
        display_name="current.txt",
        media_type="text/plain",
        source_identity_sha256=token.source.identity_sha256,
        text=text,
    )
    return PreparedFileEvidence(
        tenant_id=surface.actor.user_id,
        person_id=surface.actor.own_id,
        raw_ids=(surface.attachment.raw_object_id,),
        snapshot_tokens=(token,),
        file_evidence_set=evidence_set,
        bundle=EvidenceBundle(
            parts=(part,),
            citations=(CitationBinding("A1", token.source.identity_sha256),),
            file_evidence_set_sha256=evidence_set.identity_sha256(),
        ),
        historical_selection=None,
        _process_authority=_FILE_EVIDENCE_AUTHORITY,
    )


def _web_evidence(
    surface: CurrentFileWebAssistSurface,
    status: TransientWebEvidenceStatus = TransientWebEvidenceStatus.SOURCED,
) -> TransientWebComparisonEvidence:
    if status is TransientWebEvidenceStatus.SOURCED:
        sources: list[dict[str, object]] = [
            {
                "url": "https://public.example/current",
                "title": "Public current source",
                "text": "Текущий публичный проверенный факт.",
                "text_length": len("Текущий публичный проверенный факт."),
                "status_code": 200,
                "error": "",
                "truncated": False,
            }
        ]
        counters = {
            "requested_sources": 1,
            "completed_sources": 1,
            "failed_sources": 0,
            "timed_out_sources": 0,
            "search_timed_out": False,
        }
    elif status is TransientWebEvidenceStatus.EMPTY:
        sources = []
        counters = {
            "requested_sources": 0,
            "completed_sources": 0,
            "failed_sources": 0,
            "timed_out_sources": 0,
            "search_timed_out": False,
        }
    else:
        sources = []
        counters = {
            "requested_sources": 1,
            "completed_sources": 0,
            "failed_sources": 1,
            "timed_out_sources": 0,
            "search_timed_out": False,
            "search_failed": True,
        }
    return web_module._project_report(  # noqa: PLC2701
        surface.web_plan,
        {
            "query": "актуальные публичные правила 2026",
            "sources": sources,
            **counters,
        },
    )


class _FileReader:
    def __init__(
        self,
        evidence: PreparedFileEvidence,
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.evidence = evidence
        self.gate = gate
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def prepare(
        self,
        _surface: CurrentFileWebAssistSurface,
        *,
        absolute_deadline: float,
    ) -> PreparedFileEvidence:
        assert absolute_deadline > time.monotonic()
        self.calls += 1
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.evidence


class _WebReader:
    def __init__(
        self,
        evidence: TransientWebComparisonEvidence | tuple[TransientWebComparisonEvidence, ...],
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.evidence: tuple[TransientWebComparisonEvidence, ...] = (
            evidence if isinstance(evidence, tuple) else (evidence,)
        )
        self.gate = gate
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def research(self, **kwargs: Any) -> TransientWebComparisonEvidence:
        assert float(kwargs["absolute_deadline"]) > time.monotonic()
        index = min(self.calls, len(self.evidence) - 1)
        self.calls += 1
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.evidence[index]


class _Reviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(
        self,
        context: SupervisorReviewContext,
        *,
        absolute_deadline: float,
        pre_dispatch_validator: Callable[[], bool] | None = None,
    ) -> AdmittedSupervisorReview | None:
        self.calls += 1
        assert absolute_deadline > time.monotonic()
        assert pre_dispatch_validator is not None and pre_dispatch_validator() is True
        criterion = CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE
        review = SupervisorReview(
            plan_digest=context.plan_digest,
            outcome_digest=context.outcome_digest,
            verdict=ReviewVerdict.RETRY_READ_ONLY_STEP,
            failed_criteria=(criterion,),
            recommended_action=ReviewRecommendedAction.REQUEST_READ_ONLY_RECOVERY,
            reason_code="retry_public_read",
        )
        decision = admit_supervisor_review(review, context)
        return AdmittedSupervisorReview(
            review=review,
            decision=decision,
            context_sha256=context.canonical_sha256(),
        )


class _CountingAdapter(SupervisorAssistGraphAdapter):
    def __init__(
        self,
        storage: Any,
        *,
        commit_then_raise: bool = False,
        fail_claim_once: bool = False,
    ) -> None:
        super().__init__(storage)
        self.commit_then_raise = commit_then_raise
        self.fail_claim_once = fail_claim_once
        self.admit_calls = 0
        self.publish_calls = 0
        self.terminal_calls = 0
        self.mixed_terminal_calls = 0
        self.cancel_calls = 0
        self.restart_calls = 0
        self.rebind_calls = 0

    def admit(self, *args: Any, **kwargs: Any) -> CompareCurrentFileWebWorkGraph:
        self.admit_calls += 1
        graph = super().admit(*args, **kwargs)
        if self.commit_then_raise:
            self.commit_then_raise = False
            raise RuntimeError("lost admission acknowledgement")
        return graph

    def claim(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail_claim_once:
            self.fail_claim_once = False
            raise RuntimeError("injected claim failure")
        return super().claim(*args, **kwargs)

    def publish_comparison(self, *args: Any, **kwargs: Any) -> AssistGraphPublication:
        self.publish_calls += 1
        return super().publish_comparison(*args, **kwargs)

    def publish_terminal(self, *args: Any, **kwargs: Any) -> AssistGraphPublication:
        self.terminal_calls += 1
        return super().publish_terminal(*args, **kwargs)

    def publish_terminal_after_mixed_authority_denial(
        self, *args: Any, **kwargs: Any
    ) -> AssistGraphPublication:
        self.mixed_terminal_calls += 1
        return super().publish_terminal_after_mixed_authority_denial(*args, **kwargs)

    def cancel(self, *args: Any, **kwargs: Any) -> AssistGraphPublication:
        self.cancel_calls += 1
        return super().cancel(*args, **kwargs)

    def restart_or_retire(self, *args: Any, **kwargs: Any) -> Any:
        self.restart_calls += 1
        return super().restart_or_retire(*args, **kwargs)

    def rebind_after_restart(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> CompareCurrentFileWebWorkGraph:
        self.rebind_calls += 1
        return super().rebind_after_restart(*args, **kwargs)


def _controller(
    *,
    promotion: _Promotion | None = None,
    planner: _Planner | None = None,
    primary: _Primary | None = None,
    graph_adapter: Any = None,
    file_reader: Any = None,
    web_reader: Any = None,
    reviewer: Any = None,
    observer: Any = lambda _observation: None,
    max_review_rounds: int = 1,
    synthesizer: Any = None,
    binding_snapshot_factory: Any = operational_capability_snapshot,
    settings: Any = None,
    authority_check: Any = lambda _actor, _boundary: True,
    plan_authority_check: Any = lambda _surface, boundary: attest_plan_authority(
        boundary,
        witness_sha256="9" * 64,
    ),
    effect_check: Any = lambda _boundary: True,
    recovery_surface_loader: Any = None,
) -> SupervisorAssistController:
    kwargs: dict[str, Any] = {}
    if synthesizer is not None:
        kwargs["synthesizer"] = synthesizer
    if recovery_surface_loader is not None:
        kwargs["recovery_surface_loader"] = recovery_surface_loader
    return SupervisorAssistController(
        settings=settings or _settings(),
        promotion_evaluator=promotion or _Promotion(),
        planner=planner or _Planner(),
        reviewer=reviewer,
        primary_model=primary or _Primary(),
        graph_adapter=graph_adapter or SimpleNamespace(),
        file_reader=file_reader or SimpleNamespace(prepare=lambda *_args, **_kwargs: None),
        web_reader=web_reader or SimpleNamespace(research=lambda *_args, **_kwargs: None),
        canary_actor_binding=lambda _actor: "c" * 64,
        authority_check=authority_check,
        plan_authority_check=plan_authority_check,
        effect_check=effect_check,
        post_commit_observer=observer,
        max_review_rounds=max_review_rounds,
        binding_snapshot_factory=binding_snapshot_factory,
        **kwargs,
    )


async def _seed_interrupted_active_graph(
    storage: Any,
    label: str,
) -> tuple[
    CurrentFileWebAssistSurface,
    dict[str, object],
    CompareCurrentFileWebWorkGraph,
]:
    surface, projection = _stored_surface(storage, label)
    adapter = _CountingAdapter(storage)
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    controller = _controller(
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
    )
    legacy_calls = 0

    async def forbidden_legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    task.cancel()
    interrupted = await asyncio.wait_for(task, timeout=2)
    await controller.close()

    pending = interrupted.pending_admission
    assert interrupted.outcome is SupervisorAssistOutcome.INTERRUPTED
    assert pending is not None
    assert pending.work_graph_id is not None and pending.revision is not None
    graph = adapter.load(
        AssistGraphCursor(
            graph_id=pending.work_graph_id,
            user_id=surface.actor.user_id,
            conversation_id=surface.conversation_id,
            revision=pending.revision,
        )
    )
    assert graph is not None and graph.state is CompareCurrentFileWebGraphState.ACTIVE
    assert graph.restart_count == 0
    assert adapter.publish_calls == adapter.terminal_calls == adapter.restart_calls == 0
    assert legacy_calls == 0
    return surface, projection, graph


@pytest.mark.asyncio
async def test_preownership_failure_calls_legacy_exactly_once() -> None:
    promotion = _Promotion(admitted=False)
    planner = _Planner()
    controller = _controller(promotion=promotion, planner=planner)
    calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"message": "legacy"}

    result = await controller.execute(
        _surface(),
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 3,
    )

    assert result.outcome is SupervisorAssistOutcome.LEGACY
    assert calls == 1
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_plan_mint_authority_denial_falls_back_before_graph_admission() -> None:
    planner = _Planner()
    controller = _controller(
        planner=planner,
        plan_authority_check=lambda _surface, _boundary: PlanAuthorityDecision.rejected(
            PlanAuthorityReason.DENIED
        ),
    )
    legacy_calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return {"message": "legacy"}

    result = await controller.execute(
        _surface(),
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 3,
    )

    assert result.outcome is SupervisorAssistOutcome.LEGACY
    assert planner.calls == 1
    assert legacy_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_at", "propagates"),
    [(2, False), (3, False), (4, True)],
)
async def test_authenticated_scope_is_revalidated_after_await_and_immediately_before_admission(
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int,
    propagates: bool,
) -> None:
    checks = 0

    def revalidate(_surface: CurrentFileWebAssistSurface) -> None:
        nonlocal checks
        checks += 1
        if checks == fail_at:
            raise TurnContextError("injected authenticated scope drift")

    monkeypatch.setattr(
        CurrentFileWebAssistSurface,
        "require_current_authenticated_call_scope",
        revalidate,
    )

    class AdmissionSpy:
        def __init__(self) -> None:
            self.admit_calls = 0

        def admit(self, *_args: Any, **_kwargs: Any) -> None:
            self.admit_calls += 1

    adapter = AdmissionSpy()
    controller = _controller(graph_adapter=adapter)
    legacy_calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return {"message": "legacy"}

    call = controller.execute(
        _surface(),
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 3,
    )
    if propagates:
        with pytest.raises(TurnContextError, match="injected authenticated scope drift"):
            await call
    else:
        result = await call
        assert result.outcome is SupervisorAssistOutcome.LEGACY

    assert checks == fail_at
    assert adapter.admit_calls == 0
    assert legacy_calls == (0 if propagates else 1)


def test_production_plan_authority_gate_rechecks_principal_and_exact_raw_source(storage: Any) -> None:
    surface, _ = _stored_surface(storage, "plan-authority")
    supervisor_input = build_supervisor_input(surface.turn, _settings())
    snapshot = operational_capability_snapshot()
    source = PlanSourceBinding.current_raw_object(
        raw_object_id=surface.attachment.raw_object_id,
        source_identity_sha256=surface.attachment.source_identity_sha256,
        content_sha256=surface.attachment_content_sha256,
    )
    boundary = PlanAuthorityBoundary(
        scope=PlanAuthorityScope.ASSIST_EXECUTION,
        actor_binding_sha256=binding_digest("actor", str(surface.actor.own_id)),
        conversation_binding_sha256=binding_digest(
            "conversation",
            surface.conversation_id,
        ),
        proposal_sha256="a" * 64,
        manifest_sha256=supervisor_input.manifest.digest_hex(),
        policy_sha256=semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
        source_bindings_sha256=source_bindings_sha256((source,)),
        capability_bindings_sha256=snapshot.digest_hex(),
        budget_sha256=supervisor_input.budgets.canonical_sha256(),
        required_security_ids=("files.read", "web.compare.transient"),
        turn_deadline_monotonic_ns=(
            time.monotonic_ns() + supervisor_input.budgets.turn_deadline_ms * 1_000_000
        ),
    )
    authorization = AuthorizationService(storage)
    authorization.grant_permission(surface.actor.user_id, "web.compare.transient")
    gate = SupervisorAssistPlanAuthorityGate(storage, authorization)

    admitted = gate(surface, boundary)
    assert admitted.reason is PlanAuthorityReason.ADMITTED
    assert admitted.attestation is not None
    assert admitted.attestation.is_fresh_for(boundary, now_ns=time.monotonic_ns())

    storage.execute(
        "UPDATE raw_objects SET content_hash=? WHERE id=? AND user_id=?",
        ("f" * 64, surface.attachment.raw_object_id, surface.actor.user_id),
    )
    storage.conn.commit()
    assert gate(surface, boundary).reason is PlanAuthorityReason.SOURCE_DRIFT

    storage.execute(
        "UPDATE users SET status='disabled' WHERE id=?",
        (surface.actor.own_id,),
    )
    storage.conn.commit()
    assert gate(surface, boundary).reason is PlanAuthorityReason.DENIED


@pytest.mark.asyncio
async def test_shared_owner_equal_reaches_planner_but_other_participant_falls_back() -> None:
    planner = _Planner(admit=False)
    controller = _controller(planner=planner)
    legacy_calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return {"message": "legacy"}

    owner = ActorContext(
        "shared:team",
        "owner",
        "test",
        shared_tenant=True,
        person_id="shared:team",
    )
    participant = replace(owner, person_id="person:bob")
    owner_result = await controller.execute(
        _surface(actor=owner),
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 3,
    )
    participant_surface = replace(_surface(actor=owner), actor=participant)
    participant_result = await controller.execute(
        participant_surface,
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 3,
    )

    assert owner_result.outcome is SupervisorAssistOutcome.LEGACY
    assert participant_result.outcome is SupervisorAssistOutcome.LEGACY
    assert planner.calls == 1
    assert legacy_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["files_read_denied", "foreign_file_read_denied", "principal_not_active"],
)
async def test_fresh_file_authority_failures_are_denied(reason: str) -> None:
    class Reader:
        async def prepare(self, *_args: object, **_kwargs: object) -> PreparedFileEvidence:
            raise FileEvidenceUnavailable(reason)

    controller = _controller(file_reader=Reader())
    surface = _surface()
    graph = SimpleNamespace()
    record = SimpleNamespace(
        surface=surface,
        metrics=SimpleNamespace(capability_calls=0, accounting_complete=True),
        graph=graph,
    )

    result = await controller._read_file(  # noqa: SLF001
        cast(Any, record),
        absolute_deadline=time.monotonic() + 3,
    )

    assert result.state is CompareCurrentFileWebStepState.DENIED
    assert result.authority_rechecked is True
    assert result.verified is False


@pytest.mark.asyncio
async def test_primary_readiness_recovers_on_the_next_turn(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "laptop")
    primary = _Primary((False, True))
    adapter = _CountingAdapter(storage)
    controller = _controller(
        primary=primary,
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface)),
    )
    legacy_calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return {"message": "legacy"}

    first = await controller.execute(
        surface,
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 4,
    )
    second = await controller.execute(
        surface,
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 4,
    )

    assert first.outcome is SupervisorAssistOutcome.LEGACY
    assert second.outcome is SupervisorAssistOutcome.PUBLISHED
    assert second.observation_status is AssistObservationStatus.EMITTED
    assert legacy_calls == 1
    assert primary.prepare_calls == 2
    assert adapter.admit_calls == adapter.publish_calls == 1


@pytest.mark.asyncio
async def test_admission_ack_loss_recovers_owner_and_never_calls_legacy(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "ack-loss")
    adapter = _CountingAdapter(storage, commit_then_raise=True)

    async def observer(_observation: object) -> None:
        raise RuntimeError("observer unavailable")

    controller = _controller(
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface)),
        observer=observer,
    )
    legacy_calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return {"message": "legacy"}

    result = await controller.execute(
        surface,
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 4,
    )

    assert result.outcome is SupervisorAssistOutcome.PUBLISHED
    assert result.observation_status is AssistObservationStatus.FAILED
    assert legacy_calls == 0
    assert adapter.admit_calls == 1
    assert adapter.publish_calls == 1
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM work_item_compare_current_file_web_graphs WHERE conversation_id=?",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_committed_graph_is_not_pending_or_cancellable_during_observer(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "committed-observer")
    adapter = _CountingAdapter(storage)
    observer_started = asyncio.Event()
    release_observer = asyncio.Event()
    observer_calls = 0

    async def observer(_observation: object) -> None:
        nonlocal observer_calls
        observer_calls += 1
        observer_started.set()
        await release_observer.wait()

    controller = _controller(
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface)),
        observer=observer,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(observer_started.wait(), timeout=3)

    pending = controller.pending_durable_turn_admission(
        surface.actor.user_id,
        "cancel",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
    )
    completed_row = storage.execute(
        """SELECT id,revision FROM work_item_compare_current_file_web_graphs
             WHERE user_id=? AND conversation_id=? ORDER BY updated_at DESC LIMIT 1""",
        (surface.actor.user_id, surface.conversation_id),
    ).fetchone()
    assert completed_row is not None
    completed_pending = PendingDurableTurnAdmission.owned(
        person_id=surface.actor.user_id,
        conversation_id=surface.conversation_id,
        work_graph_id=str(completed_row["id"]),
        revision=int(completed_row["revision"]),
    )
    stable = await _cancel_pending(
        controller,
        AssistConversationScope(surface.actor.user_id, surface.conversation_id),
        completed_pending,
    )

    assert pending is False
    assert stable is not None and stable.outcome is SupervisorAssistOutcome.PUBLISHED
    assert stable.response is not None
    assert stable.observation_status is AssistObservationStatus.NOT_APPLICABLE
    assert adapter.cancel_calls == 0
    assert observer_calls == 1

    release_observer.set()
    completed = await asyncio.wait_for(task, timeout=3)
    assert completed.outcome is SupervisorAssistOutcome.PUBLISHED
    assert completed.response == stable.response
    assert completed.observation_status is AssistObservationStatus.EMITTED
    assert observer_calls == 1


@pytest.mark.asyncio
async def test_postownership_synthesis_failure_terminalizes_without_legacy(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "post-owner")
    adapter = _CountingAdapter(storage)
    legacy_calls = 0

    async def legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return {"message": "legacy"}

    async def broken_synthesis(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("primary failed after ownership")

    controller = _controller(
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface)),
        synthesizer=broken_synthesis,
    )
    result = await controller.execute(
        surface,
        legacy_primary=legacy,
        absolute_deadline=time.monotonic() + 4,
    )

    assert result.outcome is SupervisorAssistOutcome.TERMINAL
    assert legacy_calls == 0
    assert adapter.publish_calls == 0
    assert adapter.terminal_calls == 1
    current = adapter.load_current(AssistConversationScope(surface.actor.user_id, surface.conversation_id))
    assert current is None


@pytest.mark.asyncio
async def test_mixed_authority_denial_closes_without_synthesis_capability(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "mixed-authority")
    adapter = _CountingAdapter(storage)
    primary = _Primary()
    capability_claims: list[CompareCurrentFileWebStepKind] = []
    publication_actions: list[AssistPublicationAction] = []

    class DeniedWebReader:
        async def research(self, **_kwargs: Any) -> Any:
            raise AuthorizationError("web authority was revoked after claim")

    def authority(actor: ActorContext, boundary: object) -> bool:
        assert actor is surface.actor
        if type(boundary) is AssistCapabilityBoundary:
            capability_claims.append(boundary.step_kind)
            return boundary.step_kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS
        if type(boundary) is AssistPublicationBoundary:
            publication_actions.append(boundary.action)
            return boundary.action is AssistPublicationAction.TERMINAL
        return True

    controller = _controller(
        primary=primary,
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=DeniedWebReader(),
        authority_check=authority,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    result = await controller.execute(
        surface,
        legacy_primary=forbidden_legacy,
        absolute_deadline=time.monotonic() + 4,
    )

    assert result.outcome is SupervisorAssistOutcome.TERMINAL
    assert capability_claims == [
        CompareCurrentFileWebStepKind.FILE_READ,
        CompareCurrentFileWebStepKind.WEB_READ,
    ]
    assert publication_actions == [AssistPublicationAction.TERMINAL]
    assert primary.acquire_calls == 0 and primary.calls == []
    assert adapter.mixed_terminal_calls == 1
    assert adapter.terminal_calls == adapter.publish_calls == 0
    pending = result.pending_admission
    assert pending is not None and pending.work_graph_id is not None and pending.revision is not None
    closed = adapter.load(
        AssistGraphCursor(
            graph_id=pending.work_graph_id,
            user_id=surface.actor.user_id,
            conversation_id=surface.conversation_id,
            revision=pending.revision,
        )
    )
    assert closed is not None
    assert closed.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED


@pytest.mark.asyncio
@pytest.mark.parametrize("max_review_rounds", [0, 1])
async def test_review_and_web_recovery_are_strictly_bounded(
    storage: Any,
    max_review_rounds: int,
) -> None:
    surface, projection = _stored_surface(storage, f"review-{max_review_rounds}")
    adapter = _CountingAdapter(storage)
    reviewer = _Reviewer()
    empty = _web_evidence(surface, TransientWebEvidenceStatus.EMPTY)
    web_reader = _WebReader((empty, _web_evidence(surface)) if max_review_rounds else empty)
    controller = _controller(
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=web_reader,
        reviewer=reviewer,
        max_review_rounds=max_review_rounds,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    result = await controller.execute(
        surface,
        legacy_primary=forbidden_legacy,
        absolute_deadline=time.monotonic() + 4,
    )

    assert result.outcome is SupervisorAssistOutcome.PUBLISHED
    assert reviewer.calls == max_review_rounds
    assert web_reader.calls == 1 + max_review_rounds
    assert adapter.publish_calls == 1
    assert result.response is not None
    labels = [item["label"] for item in result.response["citations"]]
    assert labels == (["F1", "W1"] if max_review_rounds else ["F1"])


@pytest.mark.asyncio
async def test_overlapping_turn_uses_legacy_once_and_exact_cancel_drains_children(
    storage: Any,
) -> None:
    surface, projection = _stored_surface(storage, "overlap")
    adapter = _CountingAdapter(storage)
    planner = _Planner()
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    observed: list[object] = []
    controller = _controller(
        planner=planner,
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
        observer=observed.append,
    )
    first_legacy_calls = 0
    overlap_legacy_calls = 0

    async def first_legacy() -> dict[str, object]:
        nonlocal first_legacy_calls
        first_legacy_calls += 1
        return {"message": "unexpected"}

    async def overlap_legacy() -> dict[str, object]:
        nonlocal overlap_legacy_calls
        overlap_legacy_calls += 1
        return {"message": "ordinary overlap"}

    first_task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=first_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    active_graph = adapter.load_current(
        AssistConversationScope(surface.actor.user_id, surface.conversation_id)
    )
    active_pending = controller.pending_durable_turn_admission(
        surface.actor.user_id,
        "ещё один вопрос",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
    )
    assert active_graph is not None
    assert isinstance(active_pending, PendingDurableTurnAdmission)
    assert active_pending.revision == active_graph.revision
    lagging_pending = PendingDurableTurnAdmission.owned(
        person_id=active_pending.person_id,
        conversation_id=active_pending.conversation_id,
        work_graph_id=active_pending.work_graph_id,
        revision=active_graph.revision - 1,
    )
    assert (
        await controller.reconcile_pending_before_legacy(
            AssistConversationScope(surface.actor.user_id, surface.conversation_id),
            _pending_decision(SupervisorAssistPendingRelation.NEW_TURN, lagging_pending),
            absolute_deadline=time.monotonic() + 3,
        )
        is AssistPendingGraphDisposition.LIVE_IN_PROCESS
    )
    overlap = await controller.execute(
        surface,
        legacy_primary=overlap_legacy,
        absolute_deadline=time.monotonic() + 4,
    )
    cancelled = await _cancel_pending(
        controller,
        AssistConversationScope(surface.actor.user_id, surface.conversation_id),
        active_pending,
    )
    first = await asyncio.wait_for(first_task, timeout=2)

    assert overlap.outcome is SupervisorAssistOutcome.LEGACY
    assert overlap_legacy_calls == 1 and first_legacy_calls == 0
    assert planner.calls == 1 and adapter.admit_calls == 1
    assert cancelled is not None and cancelled.outcome is SupervisorAssistOutcome.CANCELLED
    assert cancelled.response is not None
    assert first.outcome is SupervisorAssistOutcome.CANCELLED
    assert first.response is None
    assert file_reader.cancelled and web_reader.cancelled
    assert adapter.cancel_calls == 1 and len(observed) == 1
    closed = adapter.load(AssistGraphCursor.from_graph(active_graph))
    assert closed is not None
    assert cancelled.pending_admission is not None
    assert cancelled.pending_admission.revision == closed.revision


@pytest.mark.asyncio
async def test_pending_revision_tracks_settlements_before_cancellation(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "pending-settled")
    adapter = _CountingAdapter(storage)

    class BlockingReviewer:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def review(self, *_args: Any, **_kwargs: Any) -> None:
            self.started.set()
            await asyncio.Event().wait()

    reviewer = BlockingReviewer()
    controller = _controller(
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface, TransientWebEvidenceStatus.EMPTY)),
        reviewer=reviewer,
        max_review_rounds=1,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(reviewer.started.wait(), timeout=2)
    graph = adapter.load_current(AssistConversationScope(surface.actor.user_id, surface.conversation_id))
    pending = controller.pending_durable_turn_admission(
        surface.actor.user_id,
        "cancel",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
    )
    assert graph is not None
    assert graph.step(FILE_READ_STEP_ID).state is CompareCurrentFileWebStepState.COMPLETE
    assert graph.step(WEB_READ_STEP_ID).state is CompareCurrentFileWebStepState.EMPTY
    assert isinstance(pending, PendingDurableTurnAdmission)
    assert pending.revision == graph.revision

    cancelled = await _cancel_pending(
        controller,
        AssistConversationScope(surface.actor.user_id, surface.conversation_id),
        pending,
    )
    original = await asyncio.wait_for(task, timeout=2)
    closed = adapter.load(AssistGraphCursor.from_graph(graph))
    assert cancelled is not None and cancelled.pending_admission is not None
    assert closed is not None
    assert cancelled.pending_admission.revision == closed.revision
    assert original.pending_admission is not None
    assert original.pending_admission.revision == closed.revision


@pytest.mark.asyncio
async def test_retained_owner_is_visible_with_zero_attachments_and_can_be_cancelled(
    storage: Any,
) -> None:
    surface, projection = _stored_surface(storage, "retained")
    adapter = _CountingAdapter(storage)
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    controller = _controller(
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    task.cancel()
    interrupted = await asyncio.wait_for(task, timeout=2)
    pending = controller.pending_durable_turn_admission(
        surface.actor.user_id,
        "cancel",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
    )
    assert isinstance(pending, PendingDurableTurnAdmission)
    cancelled = await _cancel_pending(
        controller,
        AssistConversationScope(surface.actor.user_id, surface.conversation_id),
        pending,
    )

    assert interrupted.outcome is SupervisorAssistOutcome.INTERRUPTED
    assert file_reader.cancelled and web_reader.cancelled
    assert isinstance(pending, PendingDurableTurnAdmission) and pending.is_owned
    assert cancelled is not None and cancelled.outcome is SupervisorAssistOutcome.CANCELLED
    assert adapter.cancel_calls == 1
    assert controller.semantic_supervisor_status()["retained_active_graphs"] == 0


@pytest.mark.asyncio
async def test_pending_classifier_binds_root_new_cancel_and_missing_ingress(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "pending-classifier")
    adapter = _CountingAdapter(storage)
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    controller = _controller(
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    new_ingress = SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref="assist-controller:new-turn",
        request_fingerprint_sha256="d" * 64,
    )
    cancel_ingress = SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref="assist-controller:explicit-cancel",
        request_fingerprint_sha256="c" * 64,
    )

    root = controller.classify_supervisor_assist_pending(
        surface.actor.user_id,
        surface.turn.message,
        actor=surface.actor,
        conversation_id=surface.conversation_id,
        ingress_binding=surface.ingress_binding,
        current_attachment_count=1,
    )
    new = controller.classify_supervisor_assist_pending(
        surface.actor.user_id,
        "ещё один вопрос",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
        ingress_binding=new_ingress,
    )
    cancel = controller.classify_supervisor_assist_pending(
        surface.actor.user_id,
        " ОтМеНа ",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
        ingress_binding=cancel_ingress,
    )
    missing = controller.classify_supervisor_assist_pending(
        surface.actor.user_id,
        "ещё один вопрос",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
        ingress_binding=None,
    )

    assert type(root) is SupervisorAssistPendingDecision
    assert root.relation is SupervisorAssistPendingRelation.ROOT_REPLAY
    assert type(new) is SupervisorAssistPendingDecision
    assert new.relation is SupervisorAssistPendingRelation.NEW_TURN
    assert new.pending == root.pending
    assert type(cancel) is SupervisorAssistPendingDecision
    assert cancel.relation is SupervisorAssistPendingRelation.EXPLICIT_CANCEL
    assert cancel.pending == root.pending
    assert type(missing) is SupervisorAssistPendingDecision
    assert missing.relation is SupervisorAssistPendingRelation.UNCERTAIN
    assert missing.pending is None

    with track_request_effects(
        lambda: True,
        before_effect_in_transaction=lambda _conn: True,
        request_binding_sha256=cancel.current_request_binding_sha256,
    ):
        cancelled = await controller.cancel_active(
            AssistConversationScope(surface.actor.user_id, surface.conversation_id),
            decision=cancel,
            user_message="отмена",
            absolute_deadline=time.monotonic() + 3,
        )
    original = await asyncio.wait_for(task, timeout=2)
    assert cancelled is not None and cancelled.outcome is SupervisorAssistOutcome.CANCELLED
    assert original.outcome is SupervisorAssistOutcome.CANCELLED


@pytest.mark.asyncio
async def test_retained_owner_is_retired_before_an_overlapping_legacy_turn(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "retained-reconcile")
    adapter = _CountingAdapter(storage)
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    observed: list[object] = []
    controller = _controller(
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
        observer=observed.append,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    task.cancel()
    interrupted = await asyncio.wait_for(task, timeout=2)
    pending = controller.pending_durable_turn_admission(
        surface.actor.user_id,
        "новый ход",
        actor=surface.actor,
        conversation_id=surface.conversation_id,
    )
    assert isinstance(pending, PendingDurableTurnAdmission)

    disposition = await controller.reconcile_pending_before_legacy(
        AssistConversationScope(surface.actor.user_id, surface.conversation_id),
        _pending_decision(SupervisorAssistPendingRelation.NEW_TURN, pending),
        absolute_deadline=time.monotonic() + 3,
    )
    assert interrupted.outcome is SupervisorAssistOutcome.INTERRUPTED
    assert disposition is AssistPendingGraphDisposition.RETIRED
    assert adapter.restart_calls == 1
    assert len(observed) == 1
    assert (
        adapter.load_current(AssistConversationScope(surface.actor.user_id, surface.conversation_id)) is None
    )
    assert controller.semantic_supervisor_status()["retained_active_graphs"] == 0


@pytest.mark.asyncio
async def test_distinct_successor_never_reconciles_retained_predecessor_in_execute(
    storage: Any,
) -> None:
    surface, projection = _stored_surface(storage, "retained-successor-race")
    adapter = _CountingAdapter(storage)
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    observed: list[object] = []
    controller = _controller(
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
        observer=observed.append,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after predecessor ownership")

    predecessor = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    predecessor.cancel()
    interrupted = await asyncio.wait_for(predecessor, timeout=2)
    successor = replace(
        surface,
        ingress_binding=SupervisorAssistIngressBindingV1.from_claimed_request(
            source_ref="assist-controller:retained-successor",
            request_fingerprint_sha256="d" * 64,
        ),
    )
    legacy_calls = 0
    legacy_response = {"message": "independent successor"}

    async def successor_legacy() -> dict[str, object]:
        nonlocal legacy_calls
        legacy_calls += 1
        return legacy_response

    result = await controller.execute(
        successor,
        legacy_primary=successor_legacy,
        absolute_deadline=time.monotonic() + 3,
    )
    current = adapter.load_current(AssistConversationScope(surface.actor.user_id, surface.conversation_id))

    assert interrupted.outcome is SupervisorAssistOutcome.INTERRUPTED
    assert result.outcome is SupervisorAssistOutcome.LEGACY
    assert result.response is legacy_response
    assert legacy_calls == 1
    assert adapter.restart_calls == 0
    assert observed == []
    assert current is not None and current.state is CompareCurrentFileWebGraphState.ACTIVE
    assert controller.semantic_supervisor_status()["retained_active_graphs"] == 1


@pytest.mark.asyncio
async def test_health_rechecks_current_promotion_and_registry_without_authorizing(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "health-fresh")
    adapter = _CountingAdapter(storage)
    promotion = _Promotion()

    class SnapshotFactory:
        def __init__(self) -> None:
            self.available = True

        def __call__(self) -> CapabilityBindingSnapshot:
            if not self.available:
                raise RuntimeError("registry unavailable")
            return operational_capability_snapshot()

    snapshots = SnapshotFactory()
    controller = _controller(
        promotion=promotion,
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface)),
        binding_snapshot_factory=snapshots,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    result = await controller.execute(
        surface,
        legacy_primary=forbidden_legacy,
        absolute_deadline=time.monotonic() + 4,
    )
    evaluation_count = controller.semantic_supervisor_status()["promotion_evaluation_total"]
    admitted = controller.semantic_supervisor_status()

    assert result.outcome is SupervisorAssistOutcome.PUBLISHED
    assert admitted["promotion_admitted"] is True
    assert admitted["effective_mode"] == SupervisorMode.ASSIST.value
    assert admitted["promotion_evaluation_total"] == evaluation_count
    assert promotion.actor_bindings[-1] is None

    # The production evaluator derives this denial from fresh local scheduler
    # and immutable activation facts; the controller must not trust its last turn.
    promotion.admitted = False
    scheduler_closed = controller.semantic_supervisor_status()
    assert scheduler_closed["promotion_admitted"] is False
    assert scheduler_closed["effective_mode"] == SupervisorMode.OFF.value
    assert scheduler_closed["promotion_evaluation_total"] == evaluation_count
    assert promotion.actor_bindings[-1] is None

    promotion.admitted = True
    snapshots.available = False
    registry_unavailable = controller.semantic_supervisor_status()
    assert registry_unavailable["promotion_admitted"] is False
    assert registry_unavailable["effective_mode"] == SupervisorMode.OFF.value
    assert registry_unavailable["promotion_evaluation_total"] == evaluation_count


@pytest.mark.asyncio
async def test_canary_health_rechecks_exact_last_admitted_actor(storage: Any) -> None:
    surface, projection = _stored_surface(storage, "health-canary")
    adapter = _CountingAdapter(storage)
    promotion = _Promotion(mode=SupervisorMode.CANARY)
    controller = _controller(
        settings=_settings(SupervisorMode.CANARY),
        promotion=promotion,
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(surface, projection)),
        web_reader=_WebReader(_web_evidence(surface)),
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    result = await controller.execute(
        surface,
        legacy_primary=forbidden_legacy,
        absolute_deadline=time.monotonic() + 4,
    )
    admitted = controller.semantic_supervisor_status()

    assert result.outcome is SupervisorAssistOutcome.PUBLISHED
    assert admitted["promotion_admitted"] is True
    assert admitted["effective_mode"] == SupervisorMode.CANARY.value
    assert promotion.actor_bindings[-1] == "c" * 64

    promotion.admitted = False
    scheduler_closed = controller.semantic_supervisor_status()
    assert scheduler_closed["promotion_admitted"] is False
    assert scheduler_closed["effective_mode"] == SupervisorMode.OFF.value
    assert promotion.actor_bindings[-1] == "c" * 64


@pytest.mark.asyncio
async def test_close_drains_process_tasks_but_leaves_graph_for_startup_retirement(
    storage: Any,
) -> None:
    surface, projection = _stored_surface(storage, "close")
    adapter = _CountingAdapter(storage)
    gate = asyncio.Event()
    file_reader = _FileReader(_prepared_file(surface, projection), gate=gate)
    web_reader = _WebReader(_web_evidence(surface), gate=gate)
    controller = _controller(
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
    )

    async def forbidden_legacy() -> dict[str, object]:
        raise AssertionError("legacy cannot run after ownership")

    task = asyncio.create_task(
        controller.execute(
            surface,
            legacy_primary=forbidden_legacy,
            absolute_deadline=time.monotonic() + 5,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(file_reader.started.wait(), web_reader.started.wait()),
        timeout=2,
    )
    await asyncio.wait_for(controller.close(), timeout=2)
    result = await asyncio.wait_for(task, timeout=2)
    graph = adapter.load_current(AssistConversationScope(surface.actor.user_id, surface.conversation_id))
    status = controller.semantic_supervisor_status()

    assert result.outcome is SupervisorAssistOutcome.INTERRUPTED
    assert graph is not None and graph.state is CompareCurrentFileWebGraphState.ACTIVE
    assert adapter.publish_calls == adapter.terminal_calls == adapter.cancel_calls == 0
    assert status["schema"] == SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA
    assert status["closed"] is True
    assert status["active_tasks"] == 0
    assert status["retained_active_graphs"] == 1
    assert surface.turn.message not in json.dumps(status, ensure_ascii=False)


@pytest.mark.asyncio
async def test_startup_restart_recovery_replans_rebinds_and_publishes_once(
    storage: Any,
) -> None:
    surface, projection, predecessor = await _seed_interrupted_active_graph(
        storage,
        "restart-resume",
    )
    storage.update_user(surface.actor.user_id, preset_key="owner")
    loader = SupervisorAssistRecoverySurfaceLoader(storage, AuthorizationService(storage))
    recovered = loader(predecessor)
    assert recovered is not None and recovered.graph == predecessor
    assert recovered.surface.actor is not surface.actor
    assert recovered.surface.ingress_binding == surface.ingress_binding

    adapter = _CountingAdapter(storage)
    planner = _Planner()
    primary = _Primary()
    file_reader = _FileReader(_prepared_file(recovered.surface, projection))
    web_reader = _WebReader(_web_evidence(recovered.surface))
    observed: list[object] = []
    controller = _controller(
        planner=planner,
        primary=primary,
        graph_adapter=adapter,
        file_reader=file_reader,
        web_reader=web_reader,
        observer=observed.append,
        recovery_surface_loader=loader,
    )

    controller.start_restart_recovery(batch_limit=1)
    controller.start_restart_recovery(batch_limit=1)
    await asyncio.wait_for(controller.wait_restart_recovery(), timeout=5)

    status = controller.restart_recovery_status()
    assert status == {
        "schema": "friday.semantic-supervisor-assist-restart-status.v1",
        "started": True,
        "running": False,
        "finished": True,
        "discovered": 1,
        "rebound": 1,
        "completed": 1,
        "retained": 0,
        "failed": 0,
        "has_more": False,
    }
    assert planner.calls == 1
    assert primary.prepare_calls == 1
    assert file_reader.calls == web_reader.calls == 1
    assert adapter.admit_calls == 0
    assert adapter.rebind_calls == adapter.publish_calls == 1
    assert (
        adapter.terminal_calls
        == adapter.mixed_terminal_calls
        == adapter.cancel_calls
        == adapter.restart_calls
        == 0
    )
    assert len(observed) == 1
    assert controller.semantic_supervisor_status()["fallback_total"] == 0

    completed = adapter.load(AssistGraphCursor.from_graph(predecessor))
    assert completed is not None
    assert completed.state is CompareCurrentFileWebGraphState.COMPLETED
    assert completed.restart_count == 1
    assistant = storage.execute(
        "SELECT metadata_json FROM messages WHERE id=?",
        (completed.publication_assistant_message_id,),
    ).fetchone()
    assert assistant is not None
    trace = TurnTrace.parse(json.loads(assistant["metadata_json"])[INTERACTION_TRACE_METADATA_KEY])
    assert trace.state_restored is True
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 1
    )
    await controller.close()


@pytest.mark.asyncio
async def test_startup_restart_recovery_recovers_lost_rebind_ack_without_duplicate(
    storage: Any,
) -> None:
    surface, projection, predecessor = await _seed_interrupted_active_graph(
        storage,
        "restart-lost-rebind-ack",
    )
    storage.update_user(surface.actor.user_id, preset_key="owner")
    loader = SupervisorAssistRecoverySurfaceLoader(storage, AuthorizationService(storage))
    recovered = loader(predecessor)
    assert recovered is not None

    class LostAckAdapter(_CountingAdapter):
        def rebind_after_restart(self, *args: Any, **kwargs: Any) -> NoReturn:
            super().rebind_after_restart(*args, **kwargs)
            raise RuntimeError("synthetic lost restart commit acknowledgement")

    adapter = LostAckAdapter(storage)
    controller = _controller(
        graph_adapter=adapter,
        file_reader=_FileReader(_prepared_file(recovered.surface, projection)),
        web_reader=_WebReader(_web_evidence(recovered.surface)),
        recovery_surface_loader=loader,
    )

    controller.start_restart_recovery()
    await asyncio.wait_for(controller.wait_restart_recovery(), timeout=5)

    status = controller.restart_recovery_status()
    assert status["rebound"] == status["completed"] == 1
    assert status["retained"] == status["failed"] == 0
    assert adapter.rebind_calls == adapter.publish_calls == 1
    completed = adapter.load(AssistGraphCursor.from_graph(predecessor))
    assert completed is not None and completed.state is CompareCurrentFileWebGraphState.COMPLETED
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 1
    )
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "loader_result"),
    (("unavailable", None), ("invalid", object())),
)
async def test_startup_restart_recovery_retains_invalid_or_unavailable_surface(
    storage: Any,
    label: str,
    loader_result: object | None,
) -> None:
    surface, _projection, predecessor = await _seed_interrupted_active_graph(
        storage,
        f"restart-retain-{label}",
    )
    adapter = _CountingAdapter(storage)

    def recovery_loader(_graph: CompareCurrentFileWebWorkGraph) -> Any:
        return loader_result

    controller = _controller(
        graph_adapter=adapter,
        recovery_surface_loader=recovery_loader,
    )

    controller.start_restart_recovery(batch_limit=1)
    await asyncio.wait_for(controller.wait_restart_recovery(), timeout=5)

    status = controller.restart_recovery_status()
    assert status["started"] is status["finished"] is True
    assert status["running"] is status["has_more"] is False
    assert status["discovered"] == status["retained"] == 1
    assert status["rebound"] == status["completed"] == 0
    assert adapter.rebind_calls == adapter.publish_calls == adapter.terminal_calls == 0
    assert adapter.restart_calls == adapter.cancel_calls == 0
    assert controller.semantic_supervisor_status()["fallback_total"] == 0

    retained = adapter.load(AssistGraphCursor.from_graph(predecessor))
    assert retained == predecessor
    assert retained is not None and retained.state is CompareCurrentFileWebGraphState.ACTIVE
    assert retained.restart_count == 0
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (surface.conversation_id,),
        ).fetchone()[0]
        == 0
    )
    await controller.close()
