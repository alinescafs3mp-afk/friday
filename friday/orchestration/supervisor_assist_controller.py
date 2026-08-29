"""Bounded owner for the promoted current-file/current-web assist journey.

The controller has one irreversible boundary: before WorkGraph admission it may
fall back to the unchanged primary route exactly once; after durable ownership
it can only complete, publish a code-owned terminal, or leave an interrupted
RUNNING graph for startup retirement.  Proposal and review model output remain
process-local and never become execution or publication authority.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import math
import re
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, cast

from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    PreparedFileEvidence,
    prepared_file_evidence_is_process_owned,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    FILE_READ_STEP_ID,
    WEB_READ_STEP_ID,
    CompareCurrentFileWebGraphOutcomeReason,
    CompareCurrentFileWebGraphOutcomeStatus,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebGraphTransition,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebWorkGraph,
)
from friday.interaction_control_plane.turn_trace import CountAccounting
from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    ModelProfileLease,
    ModelRequirements,
)
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    operational_capability_snapshot,
)
from friday.orchestration.current_file_web_comparison import (
    CurrentFileWebComparison,
    CurrentFileWebComparisonError,
    compare_current_file_with_web,
    current_file_web_comparison_is_process_owned,
    current_file_web_comparison_lease_is_current,
    current_file_web_comparison_process_lease_is_current,
    current_file_web_model_budget,
    current_file_web_model_requirements,
    current_file_web_request_is_admitted,
)
from friday.orchestration.execution_plan import ValidatedExecutionPlan
from friday.orchestration.policy_kernel import PolicyAdmissionContext
from friday.orchestration.semantic_supervisor import (
    ParsedSupervisorProposal,
    binding_digest,
    build_supervisor_input,
    supervisor_timeout_sec,
)
from friday.orchestration.supervisor_assist_graph_adapter import (
    AssistAdmissionBoundary,
    AssistBoundaryCheck,
    AssistCancellation,
    AssistCapabilityBoundary,
    AssistClaimedStep,
    AssistComparisonLeaseCheck,
    AssistComparisonPublication,
    AssistConversationScope,
    AssistGraphAdmission,
    AssistGraphCursor,
    AssistGraphPublication,
    AssistMixedAuthorityTerminalPublication,
    AssistPublicationBoundary,
    AssistRestartRebindBoundary,
    AssistRestartResult,
    AssistRestartScan,
    AssistStepSettlement,
    AssistTerminalPublication,
    AssistTraceInput,
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
    RecoveredAssistSurface,
)
from friday.orchestration.supervisor_assist_surface import (
    CurrentFileWebAssistSurface,
    bind_assist_plan_to_surface,
)
from friday.orchestration.supervisor_contracts import (
    WEB_SEARCH_CURRENT_ID,
    CapabilityEffectClass,
    CompletionCriterion,
    SupervisorInput,
    SupervisorMode,
    canonical_sha256,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityAttestor,
    PlanAuthorityBoundary,
    PlanAuthorityDecision,
    PlanAuthorityReason,
    PlanAuthorityScope,
    PlanSourceBinding,
    current_raw_source_matches,
)
from friday.orchestration.supervisor_review_policy import (
    AdmittedReadRecovery,
    DeterministicReviewState,
    ReadRecoveryCandidate,
    SupervisorReviewContext,
)
from friday.orchestration.supervisor_review_transport import AdmittedSupervisorReview
from friday.orchestration.transient_web_comparison import (
    SealedPublicWebQuery,
    TransientWebComparisonEvidence,
    TransientWebEvidenceStatus,
)
from friday.orchestration.turn_context import TurnContextError
from friday.orchestration.turn_context_call_scope import AuthenticatedChatCallScope
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext, AuthorizationError
from friday.semantic_supervisor_policy import (
    SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
    SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
    SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256,
)
from friday.source_identity import authorized_file_snapshot_token_authorizes_scope

SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA = "friday.semantic-supervisor-assist-controller-status.v1"
SUPERVISOR_ASSIST_RESTART_STATUS_SCHEMA = "friday.semantic-supervisor-assist-restart-status.v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STATUS_REASON_KEYS = 32
_FILE_AUTHORITY_DENIAL_REASONS = frozenset(
    {"files_read_denied", "foreign_file_read_denied", "principal_not_active"}
)


class SupervisorAssistControllerError(RuntimeError):
    """A promoted turn could not cross or finish its exact bounded lane."""


class SupervisorAssistOutcome(StrEnum):
    LEGACY = "legacy"
    PUBLISHED = "published"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    INTERRUPTED = "interrupted"


class AssistObservationStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    EMITTED = "emitted"
    FAILED = "failed"


class AssistPendingGraphDisposition(StrEnum):
    LIVE_IN_PROCESS = "live_in_process"
    RETIRED = "retired"
    UNCERTAIN = "uncertain"


class _AdmissionCertainty(StrEnum):
    NO_COMMIT = "no_commit"
    OWNED = "owned"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class AssistCommittedObservation:
    """Only body-free facts passed to the post-commit product emitter."""

    promotion_decision: AssistPromotionDecision = field(repr=False)
    primary_trace_sha256: str
    execution_receipt_sha256: str

    def __post_init__(self) -> None:
        decision = self.promotion_decision
        if (
            type(decision) is not AssistPromotionDecision
            or not decision.promotion_admitted
            or decision.reason is not AssistPromotionReason.ADMITTED
            or decision.readiness is not AssistPromotionReadiness.LIVE_EVIDENCE_READY
            or decision.admitted_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
        ):
            raise ValueError("committed observation needs one admitted promotion decision")
        for value in (self.primary_trace_sha256, self.execution_receipt_sha256):
            if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
                raise ValueError("committed observation digest is invalid")


class AssistFileEvidenceReader(Protocol):
    async def prepare(
        self,
        surface: CurrentFileWebAssistSurface,
        *,
        absolute_deadline: float,
    ) -> PreparedFileEvidence: ...


class AssistWebEvidenceReader(Protocol):
    async def research(
        self,
        *,
        plan: SealedPublicWebQuery,
        actor: ActorContext,
        conversation_id: str | None,
        current_user_message: str,
        absolute_deadline: float | None = None,
    ) -> TransientWebComparisonEvidence: ...


class AssistComparisonSynthesizer(Protocol):
    async def __call__(
        self,
        model: AssistPrimaryModel,
        *,
        request: str,
        accepted_plan_sha256: str,
        prepared_file: PreparedFileEvidence,
        web_evidence: TransientWebComparisonEvidence,
        absolute_deadline: float,
    ) -> CurrentFileWebComparison: ...


class AssistPromotionDecisionProvider(Protocol):
    def decide(
        self,
        *,
        binding_snapshot: CapabilityBindingSnapshot,
        actor_binding_sha256: str | None = None,
    ) -> AssistPromotionDecision | None: ...


class AssistPlanner(Protocol):
    async def propose(
        self,
        supervisor_input: SupervisorInput,
        context: PolicyAdmissionContext,
        *,
        absolute_deadline: float,
        pre_dispatch_validator: Callable[[], bool] | None = None,
    ) -> ParsedSupervisorProposal | None: ...


class AssistReviewer(Protocol):
    async def review(
        self,
        context: SupervisorReviewContext,
        *,
        absolute_deadline: float,
        pre_dispatch_validator: Callable[[], bool] | None = None,
    ) -> AdmittedSupervisorReview | None: ...


class AssistPrimaryModel(Protocol):
    async def prepare_primary_model(self, *, absolute_deadline: float) -> bool: ...

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None: ...

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool: ...

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool: ...

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
    ) -> dict[str, Any]: ...


class AssistPostCommitObserver(Protocol):
    def __call__(
        self,
        observation: AssistCommittedObservation,
    ) -> Awaitable[None] | None: ...


class AssistAuthorityCheck(Protocol):
    """Fresh permission check over the exact process-owned actor and boundary."""

    def __call__(self, actor: ActorContext, boundary: object, /) -> bool: ...


class AssistEffectCheck(Protocol):
    """Request-fence check invoked at the adapter's first persistent seam."""

    def __call__(self, boundary: object, /) -> bool: ...


class AssistPlanAuthorityCheck(Protocol):
    """Fresh personal/source admission invoked by Policy Kernel before mint."""

    def __call__(
        self,
        surface: CurrentFileWebAssistSurface,
        boundary: PlanAuthorityBoundary,
        /,
    ) -> PlanAuthorityDecision: ...


class SupervisorAssistGraphPort(Protocol):
    """Exact typed durable surface; implementations keep storage private."""

    def admit(
        self,
        admission: AssistGraphAdmission,
        *,
        authority_check: AssistBoundaryCheck[AssistAdmissionBoundary],
        effect_check: AssistBoundaryCheck[AssistAdmissionBoundary],
    ) -> CompareCurrentFileWebWorkGraph: ...

    def load(self, cursor: AssistGraphCursor) -> CompareCurrentFileWebWorkGraph | None: ...

    def load_current(
        self,
        scope: AssistConversationScope,
    ) -> CompareCurrentFileWebWorkGraph | None: ...

    def active_after_restart(
        self,
        *,
        limit: int,
        after_rowid: int | None = None,
        snapshot_upper_rowid: int | None = None,
    ) -> AssistRestartScan: ...

    def rebind_after_restart(
        self,
        cursor: AssistGraphCursor,
        admission: AssistGraphAdmission,
        *,
        authority_check: AssistBoundaryCheck[AssistRestartRebindBoundary],
        effect_check: AssistBoundaryCheck[AssistRestartRebindBoundary],
    ) -> CompareCurrentFileWebWorkGraph: ...

    def claim(
        self,
        cursor: AssistGraphCursor,
        kind: CompareCurrentFileWebStepKind,
        *,
        surface: CurrentFileWebAssistSurface,
        authority_check: AssistBoundaryCheck[AssistCapabilityBoundary],
        effect_check: AssistBoundaryCheck[AssistCapabilityBoundary],
    ) -> AssistClaimedStep: ...

    def settle(
        self,
        cursor: AssistGraphCursor,
        settlement: AssistStepSettlement,
    ) -> CompareCurrentFileWebWorkGraph: ...

    def admit_review_recovery(
        self,
        cursor: AssistGraphCursor,
        recovery: AdmittedReadRecovery,
    ) -> CompareCurrentFileWebWorkGraph: ...

    def publish_comparison(
        self,
        cursor: AssistGraphCursor,
        publication: AssistComparisonPublication,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        lease_check: AssistComparisonLeaseCheck,
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication: ...

    def publish_terminal(
        self,
        cursor: AssistGraphCursor,
        publication: AssistTerminalPublication,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication: ...

    def publish_terminal_after_mixed_authority_denial(
        self,
        cursor: AssistGraphCursor,
        publication: AssistMixedAuthorityTerminalPublication,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication: ...

    def cancel(
        self,
        cursor: AssistGraphCursor,
        cancellation: AssistCancellation,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistGraphPublication: ...

    def restart_or_retire(
        self,
        cursor: AssistGraphCursor,
        *,
        authority_check: AssistBoundaryCheck[AssistPublicationBoundary],
        effect_check: AssistBoundaryCheck[AssistPublicationBoundary],
    ) -> AssistRestartResult: ...


@dataclass(frozen=True, slots=True)
class SupervisorAssistResult:
    """Internal wrapper result; server wiring may unwrap its response mapping."""

    outcome: SupervisorAssistOutcome
    response: Mapping[str, Any] | None = field(default=None, repr=False)
    pending_admission: PendingDurableTurnAdmission | None = field(default=None, repr=False)
    promotion_decision: AssistPromotionDecision | None = field(default=None, repr=False)
    execution_receipt_sha256: str | None = None
    primary_trace_sha256: str | None = None
    observation_status: AssistObservationStatus = AssistObservationStatus.NOT_APPLICABLE

    @property
    def owned(self) -> bool:
        return self.outcome is not SupervisorAssistOutcome.LEGACY


@dataclass(slots=True)
class _RunMetrics:
    started_at: float
    model_calls: int = 1  # one admitted supervisor proposal call
    capability_calls: int = 0
    accounting_complete: bool = True
    state_restored: bool = False


@dataclass(frozen=True, slots=True)
class _ProspectiveAdmission:
    surface: CurrentFileWebAssistSurface = field(repr=False)
    plan: ValidatedExecutionPlan = field(repr=False)
    decision: AssistPromotionDecision = field(repr=False)
    binding_snapshot: CapabilityBindingSnapshot = field(repr=False)
    canary_actor_binding_sha256: str | None
    absolute_deadline: float = field(repr=False)


@dataclass(slots=True)
class _OwnedRun:
    surface: CurrentFileWebAssistSurface = field(repr=False)
    decision: AssistPromotionDecision = field(repr=False)
    plan: ValidatedExecutionPlan = field(repr=False)
    canary_actor_binding_sha256: str | None
    pending: PendingDurableTurnAdmission = field(repr=False)
    graph: CompareCurrentFileWebWorkGraph = field(repr=False)
    task: asyncio.Task[Any] = field(repr=False)
    metrics: _RunMetrics = field(repr=False)
    children: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    shutdown_requested: bool = False
    cancel_requested: bool = False
    cancellation_result: SupervisorAssistResult | None = field(default=None, repr=False)
    cancellation_done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    committed_result: SupervisorAssistResult | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _ReadResult:
    kind: CompareCurrentFileWebStepKind
    state: CompareCurrentFileWebStepState
    outcome_sha256: str
    evidence_identity_sha256: str | None
    authority_rechecked: bool
    verified: bool
    prepared_file: PreparedFileEvidence | None = field(default=None, repr=False)
    web_evidence: TransientWebComparisonEvidence | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _AdmissionAttempt:
    certainty: _AdmissionCertainty
    graph: CompareCurrentFileWebWorkGraph | None = field(default=None, repr=False)
    interrupted: bool = False


def _exact_future_deadline(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or float(value) <= time.monotonic()
    ):
        return None
    return float(value)


def _bounded_primary_journey_deadline(
    surface: CurrentFileWebAssistSurface,
    requested_deadline: float,
) -> float | None:
    """Intersect the code-owned primary journey with one authenticated root."""

    scope = surface.require_current_authenticated_call_scope()
    deadline = _exact_future_deadline(requested_deadline)
    requirements = current_file_web_model_requirements()
    model_calls, max_output_tokens = current_file_web_model_budget()
    if (
        deadline is None
        or type(requirements) is not ModelRequirements
        or type(model_calls) is not int
        or model_calls <= 0
        or type(max_output_tokens) is not int
        or max_output_tokens <= 0
        or requirements.max_tool_steps != 0
        or requirements.max_tool_rounds != 0
        or requirements.max_tool_calls != 0
        or ModelCapability.NATIVE_TOOL_CALLS in requirements.capabilities
        or requirements.effect is not ModelEffect.READ
        or requirements.verifier_required is not True
    ):
        return None
    if scope is None:
        return deadline
    if type(scope) is not AuthenticatedChatCallScope:
        return None
    context = current_primary_authenticated_turn_context()
    if context is None or context.model_input is not surface.turn:
        return None
    bounded_deadline = _exact_future_deadline(min(deadline, scope.conservative_deadline_monotonic))
    if bounded_deadline is None:
        return None
    try:
        child = context.inherited_budget.derive_child(
            safety_deadline_monotonic_ns=scope.deadline_monotonic_ns,
            max_model_calls=model_calls,
            max_model_retries=0,
            max_tool_calls=requirements.max_tool_calls,
            max_tool_rounds=requirements.max_tool_rounds,
            max_advisory_calls=0,
            max_output_tokens=max_output_tokens,
        )
    except (TypeError, ValueError, TurnContextError):
        return None
    return (
        bounded_deadline
        if child.model_anti_loop.max_model_calls == model_calls
        and child.model_anti_loop.max_model_retries == 0
        and child.resources.max_tool_calls == 0
        and child.resources.max_tool_rounds == 0
        and 0 < child.resources.max_output_tokens <= max_output_tokens
        and child.safety_deadline.monotonic_ns <= scope.deadline_monotonic_ns
        else None
    )


def _read_outcome_sha256(
    kind: CompareCurrentFileWebStepKind,
    state: CompareCurrentFileWebStepState,
    evidence_identity_sha256: str | None,
) -> str:
    return canonical_sha256(
        {
            "schema": "friday.semantic-supervisor-assist-read-outcome.v1",
            "kind": kind.value,
            "state": state.value,
            "evidence_identity_sha256": evidence_identity_sha256,
        }
    )


def _file_evidence_matches_surface(
    evidence: object,
    surface: CurrentFileWebAssistSurface,
) -> bool:
    if type(evidence) is not PreparedFileEvidence:
        return False
    prepared = cast(PreparedFileEvidence, evidence)
    if (
        not prepared_file_evidence_is_process_owned(prepared)
        or prepared.tenant_id != surface.actor.user_id
        or prepared.person_id != surface.actor.own_id
        or prepared.historical_selection is not None
        or prepared.raw_ids != (surface.attachment.raw_object_id,)
        or len(prepared.snapshot_tokens) != 1
    ):
        return False
    token = prepared.snapshot_tokens[0]
    return bool(
        authorized_file_snapshot_token_authorizes_scope(
            token,
            tenant_id=surface.actor.user_id,
            storage_owner_id=surface.actor.user_id,
        )
        and token.source.raw_id == surface.attachment.raw_object_id
        and token.source.identity_sha256 == surface.attachment.source_identity_sha256
        and token.content_sha256 == surface.attachment_content_sha256
    )


def _web_evidence_matches_surface(
    evidence: object,
    surface: CurrentFileWebAssistSurface,
) -> bool:
    if type(evidence) is not TransientWebComparisonEvidence:
        return False
    web_evidence = cast(TransientWebComparisonEvidence, evidence)
    try:
        web_evidence.__post_init__()
        return bool(
            web_evidence.plan_sha256 == surface.web_plan.canonical_sha256()
            and web_evidence.query_sha256 == surface.web_plan.query_sha256
        )
    except Exception:
        return False


def _graph_matches_pristine_admission(
    graph: object,
    prospective: _ProspectiveAdmission,
) -> bool:
    if type(graph) is not CompareCurrentFileWebWorkGraph:
        return False
    admitted_graph = cast(CompareCurrentFileWebWorkGraph, graph)
    surface = prospective.surface
    plan = prospective.plan
    bindings = bind_assist_plan_to_surface(plan, surface)
    if bindings is None:
        return False
    return bool(
        admitted_graph.state is CompareCurrentFileWebGraphState.ACTIVE
        and admitted_graph.transition is CompareCurrentFileWebGraphTransition.ADMITTED
        and admitted_graph.revision == 1
        and admitted_graph.user_id == surface.actor.user_id
        and admitted_graph.conversation_id == surface.conversation_id
        and hmac.compare_digest(
            admitted_graph.anchor_request_binding_sha256,
            surface.ingress_binding.canonical_sha256(),
        )
        and admitted_graph.current_file_raw_object_id == surface.attachment.raw_object_id
        and admitted_graph.current_file_source_identity_sha256 == surface.attachment.source_identity_sha256
        and admitted_graph.current_file_content_sha256 == surface.attachment_content_sha256
        and admitted_graph.proposal_sha256 == plan.proposal_digest
        and admitted_graph.accepted_plan_sha256 == plan.canonical_sha256()
        and admitted_graph.manifest_sha256 == plan.manifest_digest
        and admitted_graph.policy_sha256 == plan.policy_sha256
        and admitted_graph.runtime_profile_sha256 == SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        and admitted_graph.adapter_registry_sha256 == plan.binding_snapshot_sha256
        and admitted_graph.actor_binding_sha256 == plan.actor_binding_sha256
        and admitted_graph.conversation_binding_sha256 == plan.conversation_binding_sha256
        and all(
            admitted_graph.step(binding.graph_step_id).idempotency_key_sha256
            == binding.plan_step.idempotency_key
            for binding in bindings
        )
    )


def _graph_matches_restart_rebind(
    graph: object,
    prospective: _ProspectiveAdmission,
    predecessor: CompareCurrentFileWebWorkGraph,
) -> bool:
    if type(graph) is not CompareCurrentFileWebWorkGraph:
        return False
    rebound = cast(CompareCurrentFileWebWorkGraph, graph)
    surface = prospective.surface
    plan = prospective.plan
    bindings = bind_assist_plan_to_surface(plan, surface)
    if bindings is None:
        return False
    return bool(
        rebound.id == predecessor.id
        and rebound.state is CompareCurrentFileWebGraphState.ACTIVE
        and rebound.transition is CompareCurrentFileWebGraphTransition.RESTART_REBIND
        and rebound.revision == predecessor.revision + 1
        and getattr(rebound, "restart_count", 0) == 1
        and rebound.user_id == surface.actor.user_id
        and rebound.conversation_id == surface.conversation_id
        and rebound.anchor_user_message_id == predecessor.anchor_user_message_id
        and hmac.compare_digest(
            rebound.anchor_request_binding_sha256,
            surface.ingress_binding.canonical_sha256(),
        )
        and rebound.current_file_raw_object_id == surface.attachment.raw_object_id
        and rebound.current_file_source_identity_sha256 == surface.attachment.source_identity_sha256
        and rebound.current_file_content_sha256 == surface.attachment_content_sha256
        and rebound.proposal_sha256 == plan.proposal_digest
        and rebound.accepted_plan_sha256 == plan.canonical_sha256()
        and rebound.manifest_sha256 == plan.manifest_digest
        and rebound.policy_sha256 == plan.policy_sha256
        and rebound.runtime_profile_sha256 == SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        and rebound.adapter_registry_sha256 == plan.binding_snapshot_sha256
        and rebound.actor_binding_sha256 == plan.actor_binding_sha256
        and rebound.conversation_binding_sha256 == plan.conversation_binding_sha256
        and all(
            rebound.step(binding.graph_step_id).state is CompareCurrentFileWebStepState.PENDING
            for binding in bindings
        )
        and all(
            rebound.step(binding.graph_step_id).idempotency_key_sha256 == binding.plan_step.idempotency_key
            for binding in bindings
        )
    )


def _safe_counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items())[:_MAX_STATUS_REASON_KEYS])


def _scheduler_identity(evaluator: object) -> dict[str, object]:
    scheduler = getattr(evaluator, "scheduler", None)
    public_method = getattr(scheduler, "public_status", None)
    diagnostics_method = getattr(scheduler, "diagnostics_status", None)
    try:
        public = public_method() if callable(public_method) else {}
    except Exception:
        public = {}
    try:
        diagnostics = diagnostics_method() if callable(diagnostics_method) else {}
    except Exception:
        diagnostics = {}
    public = public if isinstance(public, Mapping) else {}
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    supervisor = public.get("semantic_supervisor")
    supervisor = supervisor if isinstance(supervisor, Mapping) else {}
    retry_after = diagnostics.get("circuit_retry_after_sec")
    if (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, int | float)
        or not math.isfinite(float(retry_after))
        or float(retry_after) < 0
    ):
        retry_after = None
    return {
        "state": public.get("state") if type(public.get("state")) is str else "unknown",
        "available": public.get("available") is True,
        "workload": (supervisor.get("workload") if type(supervisor.get("workload")) is str else "unknown"),
        "policy_id": (supervisor.get("policy_id") if type(supervisor.get("policy_id")) is str else "unknown"),
        "policy_sha256": (
            supervisor.get("policy_sha256")
            if type(supervisor.get("policy_sha256")) is str
            and _DIGEST_RE.fullmatch(str(supervisor.get("policy_sha256"))) is not None
            else None
        ),
        "workload_available": supervisor.get("workload_available") is True,
        "runtime_available": supervisor.get("runtime_available") is True,
        "closed_reason": (
            supervisor.get("closed_reason") if type(supervisor.get("closed_reason")) is str else "unknown"
        ),
        "circuit_retry_after_sec": retry_after,
    }


class SupervisorAssistController:
    """One process-local orchestrator around the durable graph adapter."""

    def __init__(
        self,
        *,
        settings: object,
        promotion_evaluator: AssistPromotionDecisionProvider,
        planner: AssistPlanner,
        reviewer: AssistReviewer | None,
        primary_model: AssistPrimaryModel,
        graph_adapter: SupervisorAssistGraphPort,
        file_reader: AssistFileEvidenceReader,
        web_reader: AssistWebEvidenceReader,
        canary_actor_binding: Callable[[ActorContext], str],
        authority_check: AssistAuthorityCheck,
        plan_authority_check: AssistPlanAuthorityCheck,
        effect_check: AssistEffectCheck,
        post_commit_observer: AssistPostCommitObserver,
        max_review_rounds: int,
        binding_snapshot_factory: Callable[[], CapabilityBindingSnapshot] = (operational_capability_snapshot),
        synthesizer: AssistComparisonSynthesizer | None = None,
        recovery_surface_loader: (
            Callable[[CompareCurrentFileWebWorkGraph], RecoveredAssistSurface | None] | None
        ) = None,
    ) -> None:
        if type(max_review_rounds) is not int or max_review_rounds not in {0, 1}:
            raise ValueError("assist controller review rounds must be zero or one")
        for label, dependency in (
            ("promotion evaluator", getattr(promotion_evaluator, "decide", None)),
            ("planner", getattr(planner, "propose", None)),
            ("primary model", getattr(primary_model, "prepare_primary_model", None)),
            ("file reader", getattr(file_reader, "prepare", None)),
            ("web reader", getattr(web_reader, "research", None)),
            ("canary actor binding", canary_actor_binding),
            ("authority check", authority_check),
            ("plan authority check", plan_authority_check),
            ("effect check", effect_check),
            ("post-commit observer", post_commit_observer),
            ("binding snapshot factory", binding_snapshot_factory),
            ("synthesizer", compare_current_file_with_web if synthesizer is None else synthesizer),
        ):
            if not callable(dependency):
                raise TypeError(f"{label} is unavailable")
        if reviewer is not None and not callable(getattr(reviewer, "review", None)):
            raise TypeError("reviewer is unavailable")
        if recovery_surface_loader is not None and not callable(recovery_surface_loader):
            raise TypeError("recovery surface loader is unavailable")
        self._settings = settings
        self._promotion = promotion_evaluator
        self._planner = planner
        self._reviewer = reviewer
        self._primary_model = primary_model
        self._graph_adapter = graph_adapter
        self._file_reader = file_reader
        self._web_reader = web_reader
        self._canary_actor_binding = canary_actor_binding
        self._authority_check = authority_check
        self._plan_authority_check = plan_authority_check
        self._effect_check = effect_check
        self._post_commit_observer = post_commit_observer
        self._max_review_rounds = max_review_rounds
        self._binding_snapshot_factory = binding_snapshot_factory
        self._synthesizer = (
            cast(AssistComparisonSynthesizer, compare_current_file_with_web)
            if synthesizer is None
            else synthesizer
        )
        self._recovery_surface_loader = recovery_surface_loader
        self._restart_binding_nonce = secrets.token_hex(32)
        self._active_by_scope: dict[tuple[str, str], _OwnedRun] = {}
        self._active_by_graph: dict[str, _OwnedRun] = {}
        self._retained_by_scope: dict[tuple[str, str], _OwnedRun] = {}
        self._known_durable_active_scopes: set[tuple[str, str]] = set()
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()
        self._fallback_reasons: Counter[str] = Counter()
        self._promotion_attempt_total = 0
        self._promotion_evaluation_total = 0
        self._promotion_admitted_total = 0
        self._fallback_total = 0
        self._invoked_total = 0
        self._publication_total = 0
        self._terminal_publication_total = 0
        self._event_success_total = 0
        self._event_failure_total = 0
        self._ownership_uncertain_total = 0
        self._restart_recovery_task: asyncio.Task[None] | None = None
        self._restart_recovery_discovered = 0
        self._restart_recovery_rebound = 0
        self._restart_recovery_completed = 0
        self._restart_recovery_retained = 0
        self._restart_recovery_failed = 0
        self._restart_recovery_has_more = False
        self._restart_recovery_started = False
        self._restart_recovery_finished = False
        self._last_admitted_mode = SupervisorMode.OFF
        self._last_admitted_actor_binding_sha256: str | None = None
        self._closed = False

    def _current_promotion(self) -> AssistPromotionDecision | None:
        """Re-evaluate the last admitted actor against fresh local runtime facts."""

        admitted_mode = self._last_admitted_mode
        if self._closed or admitted_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
            return None
        actor_binding = self._last_admitted_actor_binding_sha256
        if admitted_mode is SupervisorMode.CANARY and actor_binding is None:
            return None
        if admitted_mode is SupervisorMode.ASSIST:
            actor_binding = None
        snapshot = self._fresh_snapshot()
        if snapshot is None:
            return None
        return self._decide_promotion(
            snapshot,
            canary_actor_binding=actor_binding,
            count_evaluation=False,
        )

    def semantic_supervisor_status(self) -> dict[str, object]:
        """Return bounded aggregates without user, graph, query or body data."""

        requested = SupervisorMode.fail_closed(
            getattr(self._settings, "semantic_supervisor_mode", SupervisorMode.OFF.value)
        )
        current = self._current_promotion()
        return {
            "schema": SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA,
            "installed": True,
            "role": "durable_read_only_assist",
            "requested_mode": requested.value,
            "effective_mode": (
                current.admitted_mode.value if current is not None else SupervisorMode.OFF.value
            ),
            "promotion_admitted": current is not None,
            "max_review_rounds": self._max_review_rounds,
            "promotion_attempt_total": self._promotion_attempt_total,
            "promotion_evaluation_total": self._promotion_evaluation_total,
            "promotion_admitted_total": self._promotion_admitted_total,
            "active_tasks": len(self._active_by_graph),
            "retained_active_graphs": len(set(self._retained_by_scope) | self._known_durable_active_scopes),
            "fallback_total": self._fallback_total,
            "invoked_total": self._invoked_total,
            "publication_total": self._publication_total,
            "terminal_publication_total": self._terminal_publication_total,
            "event_success_total": self._event_success_total,
            "event_failure_total": self._event_failure_total,
            "ownership_uncertain_total": self._ownership_uncertain_total,
            "restart_recovery": self.restart_recovery_status(),
            "fallback_reasons": _safe_counter(self._fallback_reasons),
            "runtime_owner": "durable_graph_after_admission",
            "publication_owner": "primary",
            "tools_allowed": False,
            "effects_allowed": False,
            "closed": self._closed,
            "scheduler": _scheduler_identity(self._promotion),
        }

    def restart_recovery_status(self) -> dict[str, object]:
        """Expose bounded counters only; never graph, request, or user identity."""

        task = self._restart_recovery_task
        return {
            "schema": SUPERVISOR_ASSIST_RESTART_STATUS_SCHEMA,
            "started": self._restart_recovery_started,
            "running": task is not None and not task.done(),
            "finished": self._restart_recovery_finished,
            "discovered": self._restart_recovery_discovered,
            "rebound": self._restart_recovery_rebound,
            "completed": self._restart_recovery_completed,
            "retained": self._restart_recovery_retained,
            "failed": self._restart_recovery_failed,
            "has_more": self._restart_recovery_has_more,
        }

    def _restart_rebind_or_recover(
        self,
        predecessor: CompareCurrentFileWebWorkGraph,
        prospective: _ProspectiveAdmission,
    ) -> _AdmissionAttempt:
        """Cross the one-shot restart CAS and recover a lost commit acknowledgement."""

        request = AssistGraphAdmission(
            surface=prospective.surface,
            plan=prospective.plan,
            runtime_profile_sha256=SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256,
        )

        def restart_check(
            downstream: AssistBoundaryCheck[AssistRestartRebindBoundary],
        ) -> AssistBoundaryCheck[AssistRestartRebindBoundary]:
            def check(boundary: AssistRestartRebindBoundary) -> bool:
                if self._closed:
                    return False
                fresh = self._fresh_snapshot()
                if (
                    type(boundary) is not AssistRestartRebindBoundary
                    or fresh is None
                    or fresh.digest_hex() != prospective.plan.binding_snapshot_sha256
                    or self._decide_promotion(
                        fresh,
                        canary_actor_binding=prospective.canary_actor_binding_sha256,
                        count_evaluation=False,
                    )
                    is None
                    or boundary.actor is not prospective.surface.actor
                    or boundary.graph_id != predecessor.id
                    or boundary.user_id != predecessor.user_id
                    or boundary.conversation_id != predecessor.conversation_id
                    or boundary.expected_revision != predecessor.revision
                    or boundary.request_binding_sha256 != predecessor.anchor_request_binding_sha256
                    or boundary.predecessor_plan_sha256 != predecessor.accepted_plan_sha256
                    or boundary.accepted_plan_sha256 != prospective.plan.canonical_sha256()
                    or boundary.adapter_registry_sha256 != prospective.plan.binding_snapshot_sha256
                    or boundary.actor_binding_sha256 != prospective.plan.actor_binding_sha256
                    or boundary.conversation_binding_sha256 != prospective.plan.conversation_binding_sha256
                    or hmac.compare_digest(
                        boundary.actor_binding_sha256,
                        predecessor.actor_binding_sha256,
                    )
                    or hmac.compare_digest(
                        boundary.conversation_binding_sha256,
                        predecessor.conversation_binding_sha256,
                    )
                    or boundary.current_file_raw_object_id != predecessor.current_file_raw_object_id
                    or boundary.current_file_source_identity_sha256
                    != predecessor.current_file_source_identity_sha256
                    or boundary.current_file_content_sha256 != predecessor.current_file_content_sha256
                    or boundary.web_plan_sha256 != prospective.surface.web_plan.canonical_sha256()
                    or boundary.web_query_sha256 != prospective.surface.web_plan.query_sha256
                    or boundary.runtime_profile_sha256 != SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
                ):
                    return False
                try:
                    return downstream(boundary) is True
                except Exception:
                    return False

            return check

        failure: BaseException | None = None
        try:
            graph = self._graph_adapter.rebind_after_restart(
                AssistGraphCursor.from_graph(predecessor),
                request,
                authority_check=restart_check(
                    self._authority_for(prospective.surface.actor),
                ),
                effect_check=restart_check(self._effect_check),
            )
        except BaseException as exc:  # the CAS acknowledgement can be lost
            failure = exc
            graph = None
        if _graph_matches_restart_rebind(graph, prospective, predecessor):
            return _AdmissionAttempt(
                certainty=_AdmissionCertainty.OWNED,
                graph=graph,
                interrupted=isinstance(failure, asyncio.CancelledError),
            )
        try:
            current = self._graph_adapter.load_current(
                AssistConversationScope(
                    user_id=predecessor.user_id,
                    conversation_id=predecessor.conversation_id,
                )
            )
        except BaseException:
            return _AdmissionAttempt(
                certainty=_AdmissionCertainty.UNCERTAIN,
                interrupted=isinstance(failure, asyncio.CancelledError),
            )
        if _graph_matches_restart_rebind(current, prospective, predecessor):
            return _AdmissionAttempt(
                certainty=_AdmissionCertainty.OWNED,
                graph=current,
                interrupted=isinstance(failure, asyncio.CancelledError),
            )
        return _AdmissionAttempt(
            certainty=_AdmissionCertainty.UNCERTAIN,
            interrupted=isinstance(failure, asyncio.CancelledError),
        )

    def _retain_restart_scope(
        self,
        scope: tuple[str, str],
        *,
        failed: bool = False,
    ) -> None:
        self._known_durable_active_scopes.add(scope)
        self._restart_recovery_retained += 1
        if failed:
            self._restart_recovery_failed += 1

    async def _recover_restart_cursor(self, cursor: AssistGraphCursor) -> None:
        """Freshly re-plan and resume one exact durable owner without legacy fallback."""

        self._restart_recovery_discovered += 1
        scope = (cursor.user_id, cursor.conversation_id)
        self._known_durable_active_scopes.add(scope)
        started_at = time.monotonic()
        try:
            graph = self._graph_adapter.load(cursor)
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._retain_restart_scope(scope, failed=True)
            return
        if graph is None:
            self._known_durable_active_scopes.discard(scope)
            return
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.id != cursor.graph_id
            or graph.user_id != cursor.user_id
            or graph.conversation_id != cursor.conversation_id
        ):
            self._retain_restart_scope(scope, failed=True)
            return
        if graph.state is not CompareCurrentFileWebGraphState.ACTIVE:
            self._known_durable_active_scopes.discard(scope)
            return
        if scope in self._active_by_scope or scope in self._retained_by_scope:
            self._retain_restart_scope(scope)
            return
        if graph.restart_count != 0 or self._recovery_surface_loader is None:
            self._retain_restart_scope(scope)
            return
        try:
            recovered = self._recovery_surface_loader(graph)
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._retain_restart_scope(scope, failed=True)
            return
        if (
            type(recovered) is not RecoveredAssistSurface
            or recovered.graph != graph
            or recovered.surface.actor.user_id != graph.user_id
            or recovered.surface.conversation_id != graph.conversation_id
        ):
            self._retain_restart_scope(scope)
            return
        deadline = time.monotonic() + supervisor_timeout_sec(self._settings)
        try:
            prospective = await self._prepare_prospective(
                recovered.surface,
                absolute_deadline=deadline,
                restart_cursor=AssistGraphCursor.from_graph(graph),
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            prospective = None
        if prospective is None:
            self._retain_restart_scope(scope)
            return
        attempt = self._restart_rebind_or_recover(graph, prospective)
        if attempt.certainty is not _AdmissionCertainty.OWNED or attempt.graph is None:
            self._retain_restart_scope(scope, failed=True)
            return
        record: _OwnedRun | None = None
        try:
            record = self._register_owned(
                prospective,
                attempt.graph,
                started_at=started_at,
                state_restored=True,
            )
            self._restart_recovery_rebound += 1
            if attempt.interrupted or self._closed:
                self._retain_restart_scope(scope)
                return
            result = await self._run_owned(
                record,
                absolute_deadline=prospective.absolute_deadline,
            )
            if result.outcome in {
                SupervisorAssistOutcome.PUBLISHED,
                SupervisorAssistOutcome.TERMINAL,
                SupervisorAssistOutcome.CANCELLED,
            }:
                self._restart_recovery_completed += 1
            else:
                self._retain_restart_scope(scope)
        except asyncio.CancelledError:
            self._retain_restart_scope(scope)
            raise
        except BaseException:
            self._retain_restart_scope(scope, failed=True)
        finally:
            if record is not None:
                self._unregister_owned(record)

    async def _recover_active_after_restart(self, *, batch_limit: int) -> None:
        after_rowid: int | None = None
        snapshot_upper_rowid: int | None = None
        try:
            while not self._closed:
                scan = self._graph_adapter.active_after_restart(
                    limit=batch_limit,
                    after_rowid=after_rowid,
                    snapshot_upper_rowid=snapshot_upper_rowid,
                )
                if type(scan) is not AssistRestartScan:
                    raise SupervisorAssistControllerError("assist restart scan returned an invalid page")
                scan.__post_init__()
                if snapshot_upper_rowid is not None and scan.snapshot_upper_rowid != snapshot_upper_rowid:
                    raise SupervisorAssistControllerError("assist restart scan changed its snapshot boundary")
                snapshot_upper_rowid = scan.snapshot_upper_rowid
                self._restart_recovery_has_more = scan.has_more
                for cursor in scan.cursors:
                    if self._closed:
                        return
                    await self._recover_restart_cursor(cursor)
                if not scan.has_more:
                    self._restart_recovery_has_more = False
                    return
                if (
                    not scan.cursors
                    or scan.next_after_rowid is None
                    or (after_rowid is not None and scan.next_after_rowid <= after_rowid)
                ):
                    raise SupervisorAssistControllerError("assist restart scan did not advance")
                after_rowid = scan.next_after_rowid
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._restart_recovery_has_more = True
            raise
        except BaseException:
            self._restart_recovery_failed += 1
            self._restart_recovery_has_more = True
        finally:
            self._restart_recovery_finished = True

    def start_restart_recovery(self, *, batch_limit: int = 100) -> None:
        """Start one bounded, non-boot-gating recovery pass."""

        if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
            raise ValueError("assist restart recovery batch limit must be between 1 and 100")
        if self._closed or self._restart_recovery_started:
            return
        loop = asyncio.get_running_loop()
        self._restart_recovery_started = True
        self._restart_recovery_finished = False
        self._restart_recovery_task = loop.create_task(
            self._recover_active_after_restart(batch_limit=batch_limit),
            name="semantic-supervisor-restart-recovery",
        )

    async def wait_restart_recovery(self) -> None:
        """Wait for the one startup pass; intended for shutdown and proof tests."""

        task = self._restart_recovery_task
        if task is not None:
            await asyncio.shield(task)

    async def _legacy(
        self,
        legacy_primary: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        reason: str,
    ) -> SupervisorAssistResult:
        self._fallback_total += 1
        self._fallback_reasons[reason] += 1
        response = await legacy_primary()
        if not isinstance(response, Mapping):
            raise TypeError("legacy primary returned a non-mapping response")
        return SupervisorAssistResult(
            outcome=SupervisorAssistOutcome.LEGACY,
            response=response,
        )

    def _fresh_snapshot(self) -> CapabilityBindingSnapshot | None:
        try:
            snapshot = self._binding_snapshot_factory()
        except Exception:
            return None
        return snapshot if type(snapshot) is CapabilityBindingSnapshot else None

    def _authority_for(self, actor: ActorContext) -> Callable[[object], bool]:
        def check(boundary: object) -> bool:
            return self._authority_check(actor, boundary) is True

        return check

    def _decide_promotion(
        self,
        snapshot: CapabilityBindingSnapshot,
        *,
        canary_actor_binding: str | None,
        count_evaluation: bool = True,
    ) -> AssistPromotionDecision | None:
        if count_evaluation:
            self._promotion_evaluation_total += 1
        try:
            decision = self._promotion.decide(
                binding_snapshot=snapshot,
                actor_binding_sha256=canary_actor_binding,
            )
        except Exception:
            return None
        if type(decision) is not AssistPromotionDecision:
            return None
        admitted = cast(AssistPromotionDecision, decision)
        if (
            not admitted.promotion_admitted
            or admitted.reason is not AssistPromotionReason.ADMITTED
            or admitted.readiness is not AssistPromotionReadiness.LIVE_EVIDENCE_READY
            or admitted.admitted_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
            or admitted.requested_mode is not admitted.admitted_mode
            or admitted.evidence_sha256 is None
            or admitted.execution_authorized
            or admitted.publication_authorized
            or admitted.storage_write_authorized
        ):
            return None
        return admitted

    async def _prepare_prospective(
        self,
        surface: CurrentFileWebAssistSurface,
        *,
        absolute_deadline: float,
        restart_cursor: AssistGraphCursor | None = None,
    ) -> _ProspectiveAdmission | None:
        if (
            type(surface) is not CurrentFileWebAssistSurface
            or surface.actor.user_id != surface.actor.own_id
            or not current_file_web_request_is_admitted(surface.turn.message)
            or (restart_cursor is not None and type(restart_cursor) is not AssistGraphCursor)
            or self._closed
        ):
            return None
        deadline = _bounded_primary_journey_deadline(surface, absolute_deadline)
        if deadline is None:
            return None
        requested_mode = SupervisorMode.fail_closed(
            getattr(self._settings, "semantic_supervisor_mode", SupervisorMode.OFF.value)
        )
        if requested_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
            return None
        canary_binding: str | None = None
        if requested_mode is SupervisorMode.CANARY:
            try:
                canary_binding = self._canary_actor_binding(surface.actor)
            except Exception:
                return None
            if type(canary_binding) is not str or _DIGEST_RE.fullmatch(canary_binding) is None:
                return None
        snapshot = self._fresh_snapshot()
        if snapshot is None:
            return None
        self._promotion_attempt_total += 1
        decision = self._decide_promotion(
            snapshot,
            canary_actor_binding=canary_binding,
        )
        if decision is None:
            self._last_admitted_mode = SupervisorMode.OFF
            self._last_admitted_actor_binding_sha256 = None
            return None
        try:
            supervisor_input = build_supervisor_input(surface.turn, self._settings)
            restart_material = (
                ()
                if restart_cursor is None
                else (
                    "restart",
                    self._restart_binding_nonce,
                    restart_cursor.graph_id,
                    str(restart_cursor.revision),
                )
            )
            actor_binding_sha256 = binding_digest(
                "actor",
                surface.actor.own_id,
                *restart_material,
            )
            conversation_binding_sha256 = binding_digest(
                "conversation",
                surface.conversation_id,
                *restart_material,
            )
            source_binding = PlanSourceBinding.current_raw_object(
                raw_object_id=surface.attachment.raw_object_id,
                source_identity_sha256=surface.attachment.source_identity_sha256,
                content_sha256=surface.attachment_content_sha256,
            )

            def attest(boundary: PlanAuthorityBoundary) -> PlanAuthorityDecision:
                if (
                    type(boundary) is not PlanAuthorityBoundary
                    or boundary.scope is not PlanAuthorityScope.ASSIST_EXECUTION
                    or boundary.actor_binding_sha256 != actor_binding_sha256
                    or boundary.conversation_binding_sha256 != conversation_binding_sha256
                    or boundary.manifest_sha256 != supervisor_input.manifest.digest_hex()
                    or boundary.policy_sha256 != SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
                    or boundary.budget_sha256 != supervisor_input.budgets.canonical_sha256()
                    or boundary.capability_bindings_sha256 != snapshot.digest_hex()
                    or boundary.turn_deadline_monotonic_ns != int(deadline * 1_000_000_000)
                ):
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.INVALID_BOUNDARY)
                try:
                    result = self._plan_authority_check(surface, boundary)
                except Exception:
                    return PlanAuthorityDecision.rejected(PlanAuthorityReason.DENIED)
                return (
                    result
                    if type(result) is PlanAuthorityDecision
                    else PlanAuthorityDecision.rejected(PlanAuthorityReason.DENIED)
                )

            context = PolicyAdmissionContext(
                actor_binding_sha256=actor_binding_sha256,
                conversation_binding_sha256=conversation_binding_sha256,
                authority_scope=PlanAuthorityScope.ASSIST_EXECUTION,
                source_bindings=(source_binding,),
                turn_deadline_monotonic_ns=int(deadline * 1_000_000_000),
                authority_attestor=cast(PlanAuthorityAttestor, attest),
                capability_bindings=snapshot,
            )
        except Exception:
            return None

        def planning_still_current() -> bool:
            if self._closed:
                return False
            fresh = self._fresh_snapshot()
            return bool(
                fresh is not None
                and fresh.digest_hex() == snapshot.digest_hex()
                and self._decide_promotion(
                    fresh,
                    canary_actor_binding=canary_binding,
                    count_evaluation=False,
                )
                is not None
            )

        parsed = await self._planner.propose(
            supervisor_input,
            context,
            absolute_deadline=deadline,
            pre_dispatch_validator=planning_still_current,
        )
        surface.require_current_authenticated_call_scope()
        if type(parsed) is not ParsedSupervisorProposal:
            return None
        proposal = cast(ParsedSupervisorProposal, parsed)
        if not proposal.decision.admitted or type(proposal.decision.plan) is not ValidatedExecutionPlan:
            return None
        plan = cast(ValidatedExecutionPlan, proposal.decision.plan)
        if (
            proposal.proposal_digest != plan.proposal_digest
            or plan.manifest_digest != supervisor_input.manifest.digest_hex()
            or plan.policy_version != SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
            or plan.policy_sha256 != SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
            or plan.binding_snapshot_sha256 != snapshot.digest_hex()
            or plan.actor_binding_sha256 != context.actor_binding_sha256
            or plan.conversation_binding_sha256 != context.conversation_binding_sha256
            or plan.authority_scope is not PlanAuthorityScope.ASSIST_EXECUTION
            or plan.budget_sha256 != supervisor_input.budgets.canonical_sha256()
            or plan.budgets != supervisor_input.budgets
            or len(plan.source_bindings) != 1
            or not current_raw_source_matches(
                plan.source_bindings[0],
                raw_object_id=surface.attachment.raw_object_id,
                source_identity_sha256=surface.attachment.source_identity_sha256,
                content_sha256=surface.attachment_content_sha256,
            )
            or bind_assist_plan_to_surface(plan, surface) is None
        ):
            return None
        primary_ready = await self._primary_model.prepare_primary_model(
            absolute_deadline=deadline,
        )
        surface.require_current_authenticated_call_scope()
        if primary_ready is not True:
            return None
        fresh = self._fresh_snapshot()
        final_decision = (
            self._decide_promotion(fresh, canary_actor_binding=canary_binding)
            if fresh is not None and fresh.digest_hex() == plan.binding_snapshot_sha256
            else None
        )
        if final_decision is None:
            return None
        assert fresh is not None
        self._last_admitted_mode = final_decision.admitted_mode
        self._last_admitted_actor_binding_sha256 = (
            canary_binding if final_decision.admitted_mode is SupervisorMode.CANARY else None
        )
        return _ProspectiveAdmission(
            surface=surface,
            plan=plan,
            decision=final_decision,
            binding_snapshot=fresh,
            canary_actor_binding_sha256=canary_binding,
            absolute_deadline=deadline,
        )

    def _admit_or_recover(
        self,
        prospective: _ProspectiveAdmission,
    ) -> _AdmissionAttempt:
        request = AssistGraphAdmission(
            surface=prospective.surface,
            plan=prospective.plan,
            runtime_profile_sha256=SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256,
        )

        def admission_check(
            downstream: AssistBoundaryCheck[AssistAdmissionBoundary],
        ) -> AssistBoundaryCheck[AssistAdmissionBoundary]:
            def check(boundary: AssistAdmissionBoundary) -> bool:
                if self._closed:
                    return False
                fresh = self._fresh_snapshot()
                if (
                    type(boundary) is not AssistAdmissionBoundary
                    or fresh is None
                    or fresh.digest_hex() != prospective.plan.binding_snapshot_sha256
                    or self._decide_promotion(
                        fresh,
                        canary_actor_binding=prospective.canary_actor_binding_sha256,
                        count_evaluation=False,
                    )
                    is None
                    or boundary.actor is not prospective.surface.actor
                    or re.fullmatch(r"graph_[0-9a-f]{16}", boundary.graph_id) is None
                    or boundary.user_id != prospective.surface.actor.user_id
                    or boundary.conversation_id != prospective.surface.conversation_id
                    or boundary.request_binding_sha256
                    != prospective.surface.ingress_binding.canonical_sha256()
                    or boundary.accepted_plan_sha256 != prospective.plan.canonical_sha256()
                    or boundary.adapter_registry_sha256 != prospective.plan.binding_snapshot_sha256
                    or boundary.actor_binding_sha256 != prospective.plan.actor_binding_sha256
                    or boundary.conversation_binding_sha256 != prospective.plan.conversation_binding_sha256
                    or boundary.current_file_raw_object_id != prospective.surface.attachment.raw_object_id
                    or boundary.current_file_source_identity_sha256
                    != prospective.surface.attachment.source_identity_sha256
                    or boundary.current_file_content_sha256 != prospective.surface.attachment_content_sha256
                    or boundary.web_plan_sha256 != prospective.surface.web_plan.canonical_sha256()
                    or boundary.web_query_sha256 != prospective.surface.web_plan.query_sha256
                    or boundary.runtime_profile_sha256 != SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
                ):
                    return False
                try:
                    return downstream(boundary) is True
                except Exception:
                    return False

            return check

        failure: BaseException | None = None
        try:
            graph = self._graph_adapter.admit(
                request,
                authority_check=admission_check(
                    self._authority_for(prospective.surface.actor),
                ),
                effect_check=admission_check(self._effect_check),
            )
        except BaseException as exc:  # commit acknowledgement can be lost with any exception
            failure = exc
            graph = None
        if _graph_matches_pristine_admission(graph, prospective):
            return _AdmissionAttempt(
                certainty=_AdmissionCertainty.OWNED,
                graph=graph,
                interrupted=isinstance(failure, asyncio.CancelledError),
            )

        scope = AssistConversationScope(
            user_id=prospective.surface.actor.user_id,
            conversation_id=prospective.surface.conversation_id,
        )
        try:
            current = self._graph_adapter.load_current(scope)
        except BaseException:
            return _AdmissionAttempt(
                certainty=_AdmissionCertainty.UNCERTAIN,
                interrupted=isinstance(failure, asyncio.CancelledError),
            )
        if current is None:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            return _AdmissionAttempt(certainty=_AdmissionCertainty.NO_COMMIT)
        if _graph_matches_pristine_admission(current, prospective):
            return _AdmissionAttempt(
                certainty=_AdmissionCertainty.OWNED,
                graph=current,
                interrupted=isinstance(failure, asyncio.CancelledError),
            )
        return _AdmissionAttempt(
            certainty=_AdmissionCertainty.UNCERTAIN,
            interrupted=isinstance(failure, asyncio.CancelledError),
        )

    def _register_owned(
        self,
        prospective: _ProspectiveAdmission,
        graph: CompareCurrentFileWebWorkGraph,
        *,
        started_at: float,
        state_restored: bool = False,
    ) -> _OwnedRun:
        task = asyncio.current_task()
        if task is None:
            raise SupervisorAssistControllerError("assist execution needs one asyncio task")
        scope = (graph.user_id, graph.conversation_id)
        if (
            scope in self._active_by_scope
            or scope in self._retained_by_scope
            or graph.id in self._active_by_graph
        ):
            raise SupervisorAssistControllerError("assist graph already has an in-process owner")
        pending = PendingDurableTurnAdmission.owned(
            person_id=graph.user_id,
            conversation_id=graph.conversation_id,
            work_graph_id=graph.id,
            revision=graph.revision,
        )
        record = _OwnedRun(
            surface=prospective.surface,
            decision=prospective.decision,
            plan=prospective.plan,
            canary_actor_binding_sha256=prospective.canary_actor_binding_sha256,
            pending=pending,
            graph=graph,
            task=task,
            metrics=_RunMetrics(
                started_at=started_at,
                state_restored=state_restored,
            ),
        )
        self._active_by_scope[scope] = record
        self._active_by_graph[graph.id] = record
        self._known_durable_active_scopes.discard(scope)
        self._invoked_total += 1
        self._promotion_admitted_total += 1
        return record

    def _unregister_owned(self, record: _OwnedRun) -> None:
        scope = (record.graph.user_id, record.graph.conversation_id)
        if self._active_by_scope.get(scope) is record:
            self._active_by_scope.pop(scope, None)
        if self._active_by_graph.get(record.graph.id) is record:
            self._active_by_graph.pop(record.graph.id, None)
        if record.graph.state is CompareCurrentFileWebGraphState.ACTIVE and record.committed_result is None:
            self._retained_by_scope[scope] = record
            self._known_durable_active_scopes.add(scope)
        else:
            self._retained_by_scope.pop(scope, None)
            self._known_durable_active_scopes.discard(scope)

    async def _read_file(
        self,
        record: _OwnedRun,
        *,
        absolute_deadline: float,
    ) -> _ReadResult:
        kind = CompareCurrentFileWebStepKind.FILE_READ
        record.metrics.capability_calls += 1
        try:
            evidence = await self._await_owned(
                record,
                self._file_reader.prepare(
                    record.surface,
                    absolute_deadline=absolute_deadline,
                ),
            )
        except TurnContextError:
            raise
        except AuthorizationError:
            state = CompareCurrentFileWebStepState.DENIED
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=True,
                verified=False,
            )
        except FileEvidenceUnavailable as exc:
            denied = bool(
                len(exc.args) == 1
                and type(exc.args[0]) is str
                and exc.args[0] in _FILE_AUTHORITY_DENIAL_REASONS
            )
            state = (
                CompareCurrentFileWebStepState.DENIED
                if denied
                else CompareCurrentFileWebStepState.UNAVAILABLE
            )
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=denied,
                verified=False,
            )
        except TimeoutError:
            state = CompareCurrentFileWebStepState.UNAVAILABLE
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            state = CompareCurrentFileWebStepState.FAILED
            record.metrics.accounting_complete = False
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
        if not _file_evidence_matches_surface(evidence, record.surface):
            state = CompareCurrentFileWebStepState.FAILED
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
        identity = evidence.identity_sha256
        state = CompareCurrentFileWebStepState.COMPLETE
        return _ReadResult(
            kind=kind,
            state=state,
            outcome_sha256=_read_outcome_sha256(kind, state, identity),
            evidence_identity_sha256=identity,
            authority_rechecked=True,
            verified=True,
            prepared_file=evidence,
        )

    async def _read_web(
        self,
        record: _OwnedRun,
        *,
        absolute_deadline: float,
    ) -> _ReadResult:
        kind = CompareCurrentFileWebStepKind.WEB_READ
        record.metrics.capability_calls += 1
        try:
            evidence = await self._await_owned(
                record,
                self._web_reader.research(
                    plan=record.surface.web_plan,
                    actor=record.surface.actor,
                    conversation_id=record.surface.conversation_id,
                    current_user_message=record.surface.turn.message,
                    absolute_deadline=absolute_deadline,
                ),
            )
        except TurnContextError:
            raise
        except AuthorizationError:
            state = CompareCurrentFileWebStepState.DENIED
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=True,
                verified=False,
            )
        except TimeoutError:
            state = CompareCurrentFileWebStepState.UNAVAILABLE
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            state = CompareCurrentFileWebStepState.FAILED
            record.metrics.accounting_complete = False
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
        if not _web_evidence_matches_surface(evidence, record.surface):
            state = CompareCurrentFileWebStepState.FAILED
            return _ReadResult(
                kind=kind,
                state=state,
                outcome_sha256=_read_outcome_sha256(kind, state, None),
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
        identity = evidence.canonical_sha256()
        state = {
            TransientWebEvidenceStatus.SOURCED: CompareCurrentFileWebStepState.COMPLETE,
            TransientWebEvidenceStatus.EMPTY: CompareCurrentFileWebStepState.EMPTY,
            TransientWebEvidenceStatus.UNAVAILABLE: CompareCurrentFileWebStepState.UNAVAILABLE,
        }[evidence.status]
        accepted = state in {
            CompareCurrentFileWebStepState.COMPLETE,
            CompareCurrentFileWebStepState.EMPTY,
        }
        return _ReadResult(
            kind=kind,
            state=state,
            outcome_sha256=_read_outcome_sha256(
                kind,
                state,
                identity if accepted else None,
            ),
            evidence_identity_sha256=identity if accepted else None,
            authority_rechecked=accepted,
            verified=accepted,
            web_evidence=evidence,
        )

    def _spawn_child(
        self,
        record: _OwnedRun,
        awaitable: Awaitable[Any],
    ) -> asyncio.Task[Any]:
        async def run() -> Any:
            return await awaitable

        task: asyncio.Task[Any] = asyncio.create_task(run())
        record.children.add(task)
        task.add_done_callback(record.children.discard)
        return task

    @staticmethod
    async def _await_owned(record: _OwnedRun, awaitable: Awaitable[Any]) -> Any:
        """Revalidate the exact authenticated surface before using an awaited result."""

        try:
            return await awaitable
        finally:
            record.surface.require_current_authenticated_call_scope()

    @staticmethod
    def _raise_gathered_scope_drift(values: tuple[object, ...] | list[object]) -> None:
        """Do not downgrade a child task's authority failure into interruption."""

        for value in values:
            if isinstance(value, TurnContextError):
                raise value

    async def _review_web_recovery(
        self,
        record: _OwnedRun,
        *,
        absolute_deadline: float,
    ) -> AdmittedReadRecovery | None:
        if self._max_review_rounds == 0 or self._reviewer is None or record.stop.is_set():
            return None
        web = record.graph.step(WEB_READ_STEP_ID)
        if (
            web.state
            not in {
                CompareCurrentFileWebStepState.EMPTY,
                CompareCurrentFileWebStepState.UNAVAILABLE,
                CompareCurrentFileWebStepState.FAILED,
            }
            or web.attempt != 1
            or web.outcome_sha256 is None
        ):
            return None
        criterion = CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE
        deterministic = (
            DeterministicReviewState.INCOMPLETE
            if web.state is CompareCurrentFileWebStepState.EMPTY
            else DeterministicReviewState.FAILED
        )
        context = SupervisorReviewContext(
            plan_digest=record.graph.accepted_plan_sha256,
            outcome_digest=web.outcome_sha256,
            work_item_digest=record.graph.canonical_sha256(),
            work_revision=record.graph.revision,
            deterministic_state=deterministic,
            failed_criteria=(criterion,),
            review_round=1,
            max_review_rounds=1,
            recovery_budget_remaining=1,
            effect_started=False,
            publication_started=False,
            recovery_candidate=ReadRecoveryCandidate(
                step_id=web.step_id,
                capability_id=WEB_SEARCH_CURRENT_ID,
                criterion=criterion,
                effect_class=CapabilityEffectClass.READ,
                idempotency_key=web.idempotency_key_sha256,
                eligible=True,
            ),
        )

        def review_still_current() -> bool:
            if self._closed or record.stop.is_set():
                return False
            snapshot = self._fresh_snapshot()
            return bool(
                snapshot is not None and snapshot.digest_hex() == record.graph.adapter_registry_sha256
            )

        try:
            admitted = await self._await_owned(
                record,
                self._reviewer.review(
                    context,
                    absolute_deadline=absolute_deadline,
                    pre_dispatch_validator=review_still_current,
                ),
            )
        except asyncio.CancelledError:
            raise
        except TurnContextError:
            raise
        except Exception:
            record.metrics.accounting_complete = False
            return None
        if type(admitted) is not AdmittedSupervisorReview:
            record.metrics.accounting_complete = False
            return None
        reviewed = cast(AdmittedSupervisorReview, admitted)
        if (
            reviewed.context_sha256 != context.canonical_sha256()
            or not reviewed.decision.admitted
            or reviewed.decision.recovery is None
        ):
            record.metrics.accounting_complete = False
            return None
        record.metrics.model_calls += 1
        return reviewed.decision.recovery

    @staticmethod
    def _cursor(record: _OwnedRun) -> AssistGraphCursor:
        return AssistGraphCursor.from_graph(record.graph)

    @staticmethod
    def _replace_graph(
        record: _OwnedRun,
        graph: CompareCurrentFileWebWorkGraph,
    ) -> None:
        """Keep the process-owned pending binding on the exact durable revision."""

        record.graph = graph
        record.pending = PendingDurableTurnAdmission.owned(
            person_id=graph.user_id,
            conversation_id=graph.conversation_id,
            work_graph_id=graph.id,
            revision=graph.revision,
        )

    def _capability_check(
        self,
        record: _OwnedRun,
        kind: CompareCurrentFileWebStepKind,
        downstream: AssistBoundaryCheck[AssistCapabilityBoundary],
    ) -> AssistBoundaryCheck[AssistCapabilityBoundary]:
        bindings = bind_assist_plan_to_surface(record.plan, record.surface)
        if bindings is None:
            return lambda _boundary: False
        binding = next((item for item in bindings if item.graph_kind is kind), None)
        if binding is None:
            return lambda _boundary: False
        expected = record.graph.step(binding.graph_step_id)
        expected_revision = record.graph.revision + 1
        expected_attempt = expected.attempt + 1

        def check(boundary: AssistCapabilityBoundary) -> bool:
            record.surface.require_current_authenticated_call_scope()
            if self._closed or record.stop.is_set():
                return False
            try:
                record.surface.web_plan.__post_init__()
            except Exception:
                return False
            fresh = self._fresh_snapshot()
            if fresh is None or fresh.digest_hex() != record.graph.adapter_registry_sha256:
                return False
            plan_step = binding.plan_step
            if kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS:
                resolved = fresh.binding_for(plan_step.capability_id)
                if (
                    resolved is None
                    or not resolved.available
                    or resolved.security_id != plan_step.resolved_security_id
                    or resolved.tool_id != plan_step.resolved_tool_id
                    or resolved.adapter_id != plan_step.resolved_adapter_id
                ):
                    return False
            if (
                type(boundary) is not AssistCapabilityBoundary
                or boundary.actor is not record.surface.actor
                or boundary.graph_id != record.graph.id
                or boundary.user_id != record.surface.actor.user_id
                or boundary.conversation_id != record.surface.conversation_id
                or boundary.revision != expected_revision
                or boundary.step_kind is not kind
                or boundary.step_id != binding.graph_step_id
                or boundary.capability_id != binding.graph_capability_id
                # P3 carries only its structural step projection.  Exact P2
                # resolved identities are re-proved against ``fresh`` above;
                # the adapter boundary must agree with the durable projection.
                or boundary.security_id != expected.security_id
                or boundary.adapter_id != expected.adapter_id
                or boundary.attempt != expected_attempt
                or boundary.input_identity_sha256 != expected.input_identity_sha256
                or boundary.accepted_plan_sha256 != record.graph.accepted_plan_sha256
                or boundary.adapter_registry_sha256 != record.graph.adapter_registry_sha256
                or boundary.current_file_raw_object_id != record.surface.attachment.raw_object_id
                or boundary.current_file_source_identity_sha256
                != record.surface.attachment.source_identity_sha256
                or boundary.current_file_content_sha256 != record.surface.attachment_content_sha256
            ):
                return False
            try:
                admitted = downstream(boundary) is True
                if admitted:
                    record.surface.require_current_authenticated_call_scope()
                return admitted
            except TurnContextError:
                raise
            except Exception:
                return False

        return check

    def _publication_check(
        self,
        record: _OwnedRun,
        downstream: AssistBoundaryCheck[AssistPublicationBoundary],
        *,
        allow_interrupted: bool = False,
        require_current_surface_scope: bool = False,
    ) -> AssistBoundaryCheck[AssistPublicationBoundary]:
        def check(boundary: AssistPublicationBoundary) -> bool:
            # Retained/cancel/restart terminalization can run after the root call
            # scope has ended; only the fresh owner publication opts into this.
            if require_current_surface_scope:
                record.surface.require_current_authenticated_call_scope()
            if self._closed or (record.shutdown_requested and not allow_interrupted):
                return False
            try:
                record.surface.web_plan.__post_init__()
            except Exception:
                return False
            fresh = self._fresh_snapshot()
            if (
                type(boundary) is not AssistPublicationBoundary
                or boundary.actor is not record.surface.actor
                or fresh is None
                or fresh.digest_hex() != record.graph.adapter_registry_sha256
                or bind_assist_plan_to_surface(record.plan, record.surface) is None
                or boundary.graph_id != record.graph.id
                or boundary.user_id != record.surface.actor.user_id
                or boundary.conversation_id != record.surface.conversation_id
                or boundary.revision != record.graph.revision
                or boundary.accepted_plan_sha256 != record.graph.accepted_plan_sha256
                or boundary.adapter_registry_sha256 != record.graph.adapter_registry_sha256
                or boundary.current_file_raw_object_id != record.surface.attachment.raw_object_id
                or boundary.current_file_source_identity_sha256
                != record.surface.attachment.source_identity_sha256
                or boundary.current_file_content_sha256 != record.surface.attachment_content_sha256
            ):
                return False
            try:
                admitted = downstream(boundary) is True
                if admitted and require_current_surface_scope:
                    record.surface.require_current_authenticated_call_scope()
                return admitted
            except TurnContextError:
                raise
            except Exception:
                return False

        return check

    def pending_durable_turn_admission(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int = 0,
    ) -> PendingDurableTurnAdmission | bool | None:
        """Expose exact durable ownership to pre-ingestion server routing."""

        if (
            type(user_id) is not str
            or type(message) is not str
            or not message
            or type(actor) is not ActorContext
            or actor.user_id != user_id
            or actor.own_id != user_id
            or type(conversation_id) is not str
            or type(current_attachment_count) is not int
            or current_attachment_count not in {0, 1}
        ):
            return False
        scope = AssistConversationScope(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        key = (scope.user_id, scope.conversation_id)
        active = self._active_by_scope.get(key)
        if active is not None:
            if (
                active.graph.state is CompareCurrentFileWebGraphState.ACTIVE
                and active.committed_result is None
            ):
                return active.pending
            if (
                active.graph.state is not CompareCurrentFileWebGraphState.ACTIVE
                and active.committed_result is not None
            ):
                self._known_durable_active_scopes.discard(key)
                return False
            self._known_durable_active_scopes.add(key)
            return None
        retained = self._retained_by_scope.get(key)
        try:
            graph = self._graph_adapter.load_current(scope)
        except Exception:
            self._known_durable_active_scopes.add(key)
            return None
        if graph is None:
            self._retained_by_scope.pop(key, None)
            self._known_durable_active_scopes.discard(key)
            return False
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.state is not CompareCurrentFileWebGraphState.ACTIVE
            or graph.user_id != scope.user_id
            or graph.conversation_id != scope.conversation_id
        ):
            self._known_durable_active_scopes.add(key)
            return None
        self._known_durable_active_scopes.add(key)
        pending = PendingDurableTurnAdmission.owned(
            person_id=graph.user_id,
            conversation_id=graph.conversation_id,
            work_graph_id=graph.id,
            revision=graph.revision,
        )
        if retained is not None:
            self._replace_graph(retained, graph)
            pending = retained.pending
        return pending

    def classify_supervisor_assist_pending(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        ingress_binding: SupervisorAssistIngressBindingV1 | None,
        current_attachment_count: int = 0,
    ) -> SupervisorAssistPendingDecision | bool:
        """Classify one request against the immutable root of an ACTIVE graph."""

        person_id = actor.own_id if isinstance(actor, ActorContext) else ""
        if type(conversation_id) is not str:
            return False
        pending = self.pending_durable_turn_admission(
            user_id,
            message,
            actor=actor,
            conversation_id=conversation_id,
            current_attachment_count=current_attachment_count,
        )
        if pending is False:
            return False
        if type(pending) is not PendingDurableTurnAdmission or pending.work_graph_id is None:
            return SupervisorAssistPendingDecision.uncertain(
                person_id=person_id,
                conversation_id=conversation_id,
                current=ingress_binding,
            )
        try:
            graph = self._graph_adapter.load(
                AssistGraphCursor(
                    graph_id=pending.work_graph_id,
                    user_id=pending.person_id,
                    conversation_id=pending.conversation_id,
                    revision=int(pending.revision or 0),
                )
            )
        except Exception:
            return SupervisorAssistPendingDecision.uncertain(
                person_id=person_id,
                conversation_id=conversation_id,
                current=ingress_binding,
            )
        if graph is None or graph.state is not CompareCurrentFileWebGraphState.ACTIVE:
            return False
        if (
            graph.id != pending.work_graph_id
            or graph.user_id != pending.person_id
            or graph.conversation_id != pending.conversation_id
            or not graph.has_exact_request_binding
            or type(ingress_binding) is not SupervisorAssistIngressBindingV1
        ):
            return SupervisorAssistPendingDecision.uncertain(
                person_id=person_id,
                conversation_id=conversation_id,
                current=ingress_binding,
            )
        exact_pending = PendingDurableTurnAdmission.owned(
            person_id=graph.user_id,
            conversation_id=graph.conversation_id,
            work_graph_id=graph.id,
            revision=graph.revision,
        )
        current_sha256 = ingress_binding.canonical_sha256()
        normalized = message.strip().casefold()
        relation = (
            SupervisorAssistPendingRelation.EXPLICIT_CANCEL
            if normalized in {"отмена", "cancel"}
            else (
                SupervisorAssistPendingRelation.ROOT_REPLAY
                if hmac.compare_digest(
                    graph.anchor_request_binding_sha256,
                    current_sha256,
                )
                else SupervisorAssistPendingRelation.NEW_TURN
            )
        )
        return SupervisorAssistPendingDecision.for_graph(
            relation=relation,
            pending=exact_pending,
            root_request_binding_sha256=graph.anchor_request_binding_sha256,
            current=ingress_binding,
        )

    async def _claim(
        self,
        record: _OwnedRun,
        kind: CompareCurrentFileWebStepKind,
    ) -> bool:
        if record.stop.is_set():
            return False
        record.surface.require_current_authenticated_call_scope()
        previous_revision = record.graph.revision
        previous = record.graph.step(
            next(
                item.graph_step_id
                for item in bind_assist_plan_to_surface(record.plan, record.surface) or ()
                if item.graph_kind is kind
            )
        )
        try:
            claimed = self._graph_adapter.claim(
                self._cursor(record),
                kind,
                surface=record.surface,
                authority_check=self._capability_check(
                    record,
                    kind,
                    self._authority_for(record.surface.actor),
                ),
                effect_check=self._capability_check(
                    record,
                    kind,
                    self._effect_check,
                ),
            )
        except TurnContextError:
            raise
        except Exception:
            return False
        if type(claimed) is not AssistClaimedStep:
            return False
        graph = claimed.graph
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.id != record.graph.id
            or graph.state is not CompareCurrentFileWebGraphState.ACTIVE
            or graph.revision != previous_revision + 1
            or graph.step(previous.step_id).state is not CompareCurrentFileWebStepState.RUNNING
            or graph.step(previous.step_id).attempt != previous.attempt + 1
        ):
            return False
        self._replace_graph(record, graph)
        return True

    async def _settle(self, record: _OwnedRun, result: _ReadResult) -> bool:
        if record.stop.is_set():
            return False
        record.surface.require_current_authenticated_call_scope()
        settlement = AssistStepSettlement(
            kind=result.kind,
            state=result.state,
            outcome_sha256=result.outcome_sha256,
            evidence_identity_sha256=result.evidence_identity_sha256,
            authority_rechecked=result.authority_rechecked,
            verified=result.verified,
        )
        previous_revision = record.graph.revision
        try:
            graph = self._graph_adapter.settle(self._cursor(record), settlement)
        except Exception:
            return False
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.id != record.graph.id
            or graph.state is not CompareCurrentFileWebGraphState.ACTIVE
            or graph.revision != previous_revision + 1
        ):
            return False
        self._replace_graph(record, graph)
        return True

    def _trace(self, record: _OwnedRun) -> AssistTraceInput:
        latency_ms = max(0, int((time.monotonic() - record.metrics.started_at) * 1_000))
        accounting = (
            CountAccounting.COMPLETE if record.metrics.accounting_complete else CountAccounting.LOWER_BOUND
        )
        return AssistTraceInput(
            latency_ms=latency_ms,
            model_calls=record.metrics.model_calls,
            model_call_accounting=accounting,
            capability_calls=record.metrics.capability_calls,
            capability_call_accounting=accounting,
            state_restored=record.metrics.state_restored,
        )

    def _committed_result(
        self,
        record: _OwnedRun,
        publication: AssistGraphPublication,
        *,
        outcome: SupervisorAssistOutcome,
        include_file_citation: bool,
    ) -> SupervisorAssistResult:
        if type(publication) is not AssistGraphPublication:
            raise SupervisorAssistControllerError("graph adapter returned an invalid publication")
        graph = publication.graph
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.id != record.graph.id
            or graph.state is CompareCurrentFileWebGraphState.ACTIVE
            or type(publication.content) is not str
            or not publication.content.strip()
            or type(publication.assistant_message_id) is not str
            or type(publication.public_citations) is not tuple
            or _DIGEST_RE.fullmatch(publication.primary_trace_sha256) is None
            or _DIGEST_RE.fullmatch(publication.execution_receipt_sha256) is None
        ):
            raise SupervisorAssistControllerError("committed publication is malformed")
        citations: list[dict[str, object]] = []
        if include_file_citation:
            citations.append(
                {
                    "label": "F1",
                    "kind": "current_attachment",
                    "attachment_ordinal": 1,
                }
            )
        for citation in publication.public_citations:
            payload = citation.payload()
            if type(payload) is not dict or set(payload) != {"label", "url", "title"}:
                raise SupervisorAssistControllerError("public citation projection is malformed")
            citations.append(dict(payload))
        response: dict[str, Any] = {
            "user_id": graph.user_id,
            "message": publication.content,
            "conversation_id": graph.conversation_id,
            "message_id": publication.assistant_message_id,
            "message_format": "markdown",
            "tools_used": [],
            "context": {"interaction_mode": "dialogue"},
        }
        if citations:
            response["citations"] = citations
        self._replace_graph(record, graph)
        self._publication_total += 1
        if graph.state is CompareCurrentFileWebGraphState.TERMINAL:
            self._terminal_publication_total += 1
        result = SupervisorAssistResult(
            outcome=outcome,
            response=response,
            pending_admission=record.pending,
            promotion_decision=record.decision,
            execution_receipt_sha256=publication.execution_receipt_sha256,
            primary_trace_sha256=publication.primary_trace_sha256,
        )
        record.committed_result = result
        return result

    async def _observe_committed(
        self,
        record: _OwnedRun,
        result: SupervisorAssistResult,
    ) -> SupervisorAssistResult:
        assert result.primary_trace_sha256 is not None
        assert result.execution_receipt_sha256 is not None
        observation = AssistCommittedObservation(
            promotion_decision=record.decision,
            primary_trace_sha256=result.primary_trace_sha256,
            execution_receipt_sha256=result.execution_receipt_sha256,
        )
        try:
            emitted = self._post_commit_observer(observation)
            if inspect.isawaitable(emitted):
                await self._spawn_child(record, emitted)
        except BaseException:
            self._event_failure_total += 1
            completed = replace(
                result,
                observation_status=AssistObservationStatus.FAILED,
            )
        else:
            self._event_success_total += 1
            completed = replace(
                result,
                observation_status=AssistObservationStatus.EMITTED,
            )
        record.committed_result = completed
        return completed

    async def _publish_comparison(
        self,
        record: _OwnedRun,
        *,
        prepared_file: PreparedFileEvidence,
        web_evidence: TransientWebComparisonEvidence,
        comparison: CurrentFileWebComparison,
        absolute_deadline: float,
    ) -> SupervisorAssistResult | None:
        if (
            not current_file_web_comparison_is_process_owned(comparison)
            or comparison.accepted_plan_sha256 != record.graph.accepted_plan_sha256
            or comparison.file_evidence_sha256 != prepared_file.identity_sha256
            or comparison.web_evidence_sha256 != web_evidence.canonical_sha256()
            or comparison.citation_labels
            != ("F1", *(citation.label for citation in web_evidence.public_citations()))
        ):
            return None
        request = AssistComparisonPublication(
            current_file_snapshot=prepared_file.snapshot_tokens[0],
            comparison=comparison,
            web_evidence=web_evidence,
            trace=self._trace(record),
        )
        async with record.mutation_lock:
            if record.stop.is_set() or record.committed_result is not None:
                return record.committed_result
            try:
                lease_task = self._spawn_child(
                    record,
                    current_file_web_comparison_lease_is_current(
                        self._primary_model,
                        comparison,
                        absolute_deadline=absolute_deadline,
                    ),
                )
                lease_current = await self._await_owned(record, lease_task)
                if (
                    lease_current is not True
                    or record.stop.is_set()
                    or _exact_future_deadline(absolute_deadline) is None
                ):
                    return record.committed_result
                record.surface.require_current_authenticated_call_scope()

                def final_lease_check(candidate: CurrentFileWebComparison, /) -> bool:
                    if (
                        candidate is not comparison
                        or record.stop.is_set()
                        or _exact_future_deadline(absolute_deadline) is None
                    ):
                        return False
                    record.surface.require_current_authenticated_call_scope()
                    if not current_file_web_comparison_process_lease_is_current(
                        self._primary_model,
                        candidate,
                    ):
                        return False
                    record.surface.require_current_authenticated_call_scope()
                    return bool(
                        candidate is comparison
                        and not record.stop.is_set()
                        and _exact_future_deadline(absolute_deadline) is not None
                    )

                publication = self._graph_adapter.publish_comparison(
                    self._cursor(record),
                    request,
                    authority_check=self._publication_check(
                        record,
                        self._authority_for(record.surface.actor),
                        require_current_surface_scope=True,
                    ),
                    lease_check=final_lease_check,
                    effect_check=self._publication_check(
                        record,
                        self._effect_check,
                        require_current_surface_scope=True,
                    ),
                )
                result = self._committed_result(
                    record,
                    publication,
                    outcome=SupervisorAssistOutcome.PUBLISHED,
                    include_file_citation=True,
                )
            except asyncio.CancelledError:
                if record.stop.is_set():
                    return record.committed_result
                raise
            except TurnContextError:
                raise
            except Exception:
                return None
        return await self._observe_committed(record, result)

    async def _publish_terminal(
        self,
        record: _OwnedRun,
        *,
        synthesis_state: CompareCurrentFileWebStepState | None = None,
    ) -> SupervisorAssistResult | None:
        synthesis_settlement = None
        if synthesis_state is not None:
            if synthesis_state not in {
                CompareCurrentFileWebStepState.UNAVAILABLE,
                CompareCurrentFileWebStepState.FAILED,
            }:
                raise SupervisorAssistControllerError("terminal synthesis state is invalid")
            synthesis_outcome_sha256 = canonical_sha256(
                {
                    "schema": "friday.semantic-supervisor-assist-synthesis-outcome.v1",
                    "state": synthesis_state.value,
                }
            )
            synthesis_settlement = AssistStepSettlement(
                kind=CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS,
                state=synthesis_state,
                outcome_sha256=synthesis_outcome_sha256,
                evidence_identity_sha256=None,
                authority_rechecked=False,
                verified=False,
            )
            expected_status = (
                CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE
                if synthesis_state is CompareCurrentFileWebStepState.UNAVAILABLE
                else CompareCurrentFileWebGraphOutcomeStatus.FAILED
            )
            expected_reason = (
                CompareCurrentFileWebGraphOutcomeReason.CAPABILITY_UNAVAILABLE
                if synthesis_state is CompareCurrentFileWebStepState.UNAVAILABLE
                else CompareCurrentFileWebGraphOutcomeReason.STEP_FAILED
            )
        else:
            try:
                expected_status, expected_reason = record.graph.terminal_disposition()
            except Exception:
                return None
        request = AssistTerminalPublication(
            expected_status=expected_status,
            expected_reason=expected_reason,
            trace=self._trace(record),
            synthesis_settlement=synthesis_settlement,
        )
        async with record.mutation_lock:
            if record.stop.is_set() or record.committed_result is not None:
                return record.committed_result
            record.surface.require_current_authenticated_call_scope()
            try:
                publication = self._graph_adapter.publish_terminal(
                    self._cursor(record),
                    request,
                    authority_check=self._publication_check(
                        record,
                        self._authority_for(record.surface.actor),
                        require_current_surface_scope=True,
                    ),
                    effect_check=self._publication_check(
                        record,
                        self._effect_check,
                        require_current_surface_scope=True,
                    ),
                )
                result = self._committed_result(
                    record,
                    publication,
                    outcome=SupervisorAssistOutcome.TERMINAL,
                    include_file_citation=False,
                )
            except TurnContextError:
                raise
            except Exception:
                return None
        return await self._observe_committed(record, result)

    async def _publish_mixed_authority_terminal(
        self,
        record: _OwnedRun,
    ) -> SupervisorAssistResult | None:
        request = AssistMixedAuthorityTerminalPublication(trace=self._trace(record))
        async with record.mutation_lock:
            if record.stop.is_set() or record.committed_result is not None:
                return record.committed_result
            record.surface.require_current_authenticated_call_scope()
            try:
                publication = self._graph_adapter.publish_terminal_after_mixed_authority_denial(
                    self._cursor(record),
                    request,
                    authority_check=self._publication_check(
                        record,
                        self._authority_for(record.surface.actor),
                        require_current_surface_scope=True,
                    ),
                    effect_check=self._publication_check(
                        record,
                        self._effect_check,
                        require_current_surface_scope=True,
                    ),
                )
                result = self._committed_result(
                    record,
                    publication,
                    outcome=SupervisorAssistOutcome.TERMINAL,
                    include_file_citation=False,
                )
            except TurnContextError:
                raise
            except Exception:
                return None
        return await self._observe_committed(record, result)

    async def _stop_result(self, record: _OwnedRun) -> SupervisorAssistResult:
        if record.cancel_requested:
            await record.cancellation_done.wait()
            cancelled = record.cancellation_result
            if cancelled is not None:
                return replace(cancelled, response=None)
        return SupervisorAssistResult(
            outcome=SupervisorAssistOutcome.INTERRUPTED,
            pending_admission=record.pending,
            promotion_decision=record.decision,
        )

    async def _admit_web_recovery(
        self,
        record: _OwnedRun,
        recovery: AdmittedReadRecovery,
    ) -> bool:
        if record.stop.is_set():
            return False
        record.surface.require_current_authenticated_call_scope()
        previous_revision = record.graph.revision
        try:
            graph = self._graph_adapter.admit_review_recovery(
                self._cursor(record),
                recovery,
            )
        except Exception:
            return False
        web = graph.step(WEB_READ_STEP_ID) if type(graph) is CompareCurrentFileWebWorkGraph else None
        if (
            type(graph) is not CompareCurrentFileWebWorkGraph
            or graph.id != record.graph.id
            or graph.revision != previous_revision + 1
            or graph.state is not CompareCurrentFileWebGraphState.ACTIVE
            or web is None
            or web.state is not CompareCurrentFileWebStepState.PENDING
            or web.attempt != 1
            or web.recovery_review_sha256 != recovery.review_digest
            or web.recovery_context_sha256 != recovery.context_digest
        ):
            return False
        self._replace_graph(record, graph)
        return True

    async def _run_owned(
        self,
        record: _OwnedRun,
        *,
        absolute_deadline: float,
    ) -> SupervisorAssistResult:
        try:
            async with record.mutation_lock:
                file_claimed = await self._claim(
                    record,
                    CompareCurrentFileWebStepKind.FILE_READ,
                )
            if not file_claimed:
                return await self._stop_result(record)
            if record.stop.is_set():
                return await self._stop_result(record)
            file_task = self._spawn_child(
                record,
                self._read_file(record, absolute_deadline=absolute_deadline),
            )

            async with record.mutation_lock:
                web_claimed = await self._claim(
                    record,
                    CompareCurrentFileWebStepKind.WEB_READ,
                )
            if not web_claimed:
                file_value = await self._await_owned(
                    record,
                    asyncio.gather(file_task, return_exceptions=True),
                )
                self._raise_gathered_scope_drift(file_value)
                if not record.stop.is_set() and type(file_value[0]) is _ReadResult:
                    async with record.mutation_lock:
                        await self._settle(record, file_value[0])
                return await self._stop_result(record)
            if record.stop.is_set():
                return await self._stop_result(record)
            web_task = self._spawn_child(
                record,
                self._read_web(record, absolute_deadline=absolute_deadline),
            )
            values = await self._await_owned(
                record,
                asyncio.gather(file_task, web_task, return_exceptions=True),
            )
            self._raise_gathered_scope_drift(values)
            if record.stop.is_set():
                return await self._stop_result(record)
            if type(values[0]) is not _ReadResult or type(values[1]) is not _ReadResult:
                record.metrics.accounting_complete = False
                return await self._stop_result(record)
            file_result = values[0]
            web_result = values[1]
            async with record.mutation_lock:
                file_settled = await self._settle(record, file_result)
            if not file_settled:
                return await self._stop_result(record)
            async with record.mutation_lock:
                web_settled = await self._settle(record, web_result)
            if not web_settled:
                return await self._stop_result(record)

            if record.graph.step(WEB_READ_STEP_ID).state in {
                CompareCurrentFileWebStepState.EMPTY,
                CompareCurrentFileWebStepState.UNAVAILABLE,
                CompareCurrentFileWebStepState.FAILED,
            }:
                review_task = self._spawn_child(
                    record,
                    self._review_web_recovery(
                        record,
                        absolute_deadline=absolute_deadline,
                    ),
                )
                try:
                    recovery = await self._await_owned(record, review_task)
                except asyncio.CancelledError:
                    return await self._stop_result(record)
                if type(recovery) is AdmittedReadRecovery and not record.stop.is_set():
                    async with record.mutation_lock:
                        recovery_admitted = await self._admit_web_recovery(record, recovery)
                    if not recovery_admitted:
                        return await self._stop_result(record)
                    async with record.mutation_lock:
                        retry_claimed = await self._claim(
                            record,
                            CompareCurrentFileWebStepKind.WEB_READ,
                        )
                    if not retry_claimed:
                        return await self._stop_result(record)
                    if record.stop.is_set():
                        return await self._stop_result(record)
                    retry_task = self._spawn_child(
                        record,
                        self._read_web(record, absolute_deadline=absolute_deadline),
                    )
                    try:
                        retry_value = await self._await_owned(record, retry_task)
                    except asyncio.CancelledError:
                        return await self._stop_result(record)
                    if type(retry_value) is not _ReadResult:
                        record.metrics.accounting_complete = False
                        return await self._stop_result(record)
                    web_result = retry_value
                    async with record.mutation_lock:
                        retry_settled = await self._settle(record, web_result)
                    if not retry_settled:
                        return await self._stop_result(record)

            if record.stop.is_set():
                return await self._stop_result(record)
            prepared_file = file_result.prepared_file
            web_evidence = web_result.web_evidence
            if prepared_file is not None and web_evidence is not None:
                async with record.mutation_lock:
                    synthesis_claimed = await self._claim(
                        record,
                        CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS,
                    )
                if not synthesis_claimed:
                    return await self._stop_result(record)
                if record.stop.is_set():
                    return await self._stop_result(record)
                synthesis_task = self._spawn_child(
                    record,
                    self._synthesizer(
                        self._primary_model,
                        request=record.surface.turn.message,
                        accepted_plan_sha256=record.graph.accepted_plan_sha256,
                        prepared_file=prepared_file,
                        web_evidence=web_evidence,
                        absolute_deadline=absolute_deadline,
                    ),
                )
                try:
                    comparison = await self._await_owned(record, synthesis_task)
                except TurnContextError:
                    raise
                except CurrentFileWebComparisonError as exc:
                    record.metrics.model_calls += exc.model_calls
                    terminal = await self._publish_terminal(
                        record,
                        synthesis_state=CompareCurrentFileWebStepState.FAILED,
                    )
                    return terminal or await self._stop_result(record)
                except asyncio.CancelledError:
                    return await self._stop_result(record)
                except Exception:
                    record.metrics.accounting_complete = False
                    terminal = await self._publish_terminal(
                        record,
                        synthesis_state=CompareCurrentFileWebStepState.FAILED,
                    )
                    return terminal or await self._stop_result(record)
                if type(comparison) is not CurrentFileWebComparison:
                    record.metrics.accounting_complete = False
                    terminal = await self._publish_terminal(
                        record,
                        synthesis_state=CompareCurrentFileWebStepState.FAILED,
                    )
                    return terminal or await self._stop_result(record)
                record.metrics.model_calls += comparison.model_calls
                published = await self._publish_comparison(
                    record,
                    prepared_file=prepared_file,
                    web_evidence=web_evidence,
                    comparison=comparison,
                    absolute_deadline=absolute_deadline,
                )
                return published or await self._stop_result(record)

            usable_read = any(
                record.graph.step(step_id).state
                in {
                    CompareCurrentFileWebStepState.COMPLETE,
                    CompareCurrentFileWebStepState.PARTIAL,
                }
                for step_id in (FILE_READ_STEP_ID, WEB_READ_STEP_ID)
            )
            mixed_authority_denial = (
                sum(
                    record.graph.step(step_id).state is CompareCurrentFileWebStepState.DENIED
                    for step_id in (FILE_READ_STEP_ID, WEB_READ_STEP_ID)
                )
                == 1
                and sum(
                    record.graph.step(step_id).state
                    in {
                        CompareCurrentFileWebStepState.COMPLETE,
                        CompareCurrentFileWebStepState.PARTIAL,
                    }
                    for step_id in (FILE_READ_STEP_ID, WEB_READ_STEP_ID)
                )
                == 1
            )
            if mixed_authority_denial:
                terminal = await self._publish_mixed_authority_terminal(record)
                return terminal or await self._stop_result(record)
            if usable_read:
                async with record.mutation_lock:
                    synthesis_claimed = await self._claim(
                        record,
                        CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS,
                    )
                if not synthesis_claimed:
                    return await self._stop_result(record)
                terminal = await self._publish_terminal(
                    record,
                    synthesis_state=CompareCurrentFileWebStepState.UNAVAILABLE,
                )
            else:
                terminal = await self._publish_terminal(record)
            return terminal or await self._stop_result(record)
        except asyncio.CancelledError:
            record.shutdown_requested = True
            record.stop.set()
            for child in tuple(record.children):
                if not child.done():
                    child.cancel()
            if record.children:
                await asyncio.gather(*record.children, return_exceptions=True)
            return await self._stop_result(record)

    async def _reconcile_retained(self, record: _OwnedRun) -> bool:
        scope = (record.graph.user_id, record.graph.conversation_id)
        try:
            current = self._graph_adapter.load_current(
                AssistConversationScope(
                    user_id=record.graph.user_id,
                    conversation_id=record.graph.conversation_id,
                )
            )
        except Exception:
            return False
        if current is None:
            self._retained_by_scope.pop(scope, None)
            self._known_durable_active_scopes.discard(scope)
            return True
        if (
            type(current) is not CompareCurrentFileWebWorkGraph
            or current.id != record.graph.id
            or current.user_id != record.graph.user_id
            or current.conversation_id != record.graph.conversation_id
        ):
            return False
        if current.state is not CompareCurrentFileWebGraphState.ACTIVE:
            self._retained_by_scope.pop(scope, None)
            self._known_durable_active_scopes.discard(scope)
            return True
        self._replace_graph(record, current)
        try:
            restarted = self._graph_adapter.restart_or_retire(
                self._cursor(record),
                authority_check=self._publication_check(
                    record,
                    self._authority_for(record.surface.actor),
                    allow_interrupted=True,
                ),
                effect_check=self._publication_check(
                    record,
                    self._effect_check,
                    allow_interrupted=True,
                ),
            )
        except Exception:
            try:
                remaining = self._graph_adapter.load_current(
                    AssistConversationScope(
                        user_id=record.graph.user_id,
                        conversation_id=record.graph.conversation_id,
                    )
                )
            except Exception:
                return False
            if remaining is not None:
                return False
            self._retained_by_scope.pop(scope, None)
            self._known_durable_active_scopes.discard(scope)
            return True
        if type(restarted) is not AssistRestartResult:
            return False
        result = self._committed_result(
            record,
            restarted.publication,
            outcome=SupervisorAssistOutcome.TERMINAL,
            include_file_citation=False,
        )
        await self._observe_committed(record, result)
        self._retained_by_scope.pop(scope, None)
        self._known_durable_active_scopes.discard(scope)
        return True

    async def reconcile_pending_before_legacy(
        self,
        scope: AssistConversationScope,
        decision: SupervisorAssistPendingDecision,
        *,
        absolute_deadline: float,
    ) -> AssistPendingGraphDisposition:
        """Retire same-process interrupted work before an overlapping legacy turn."""

        pending = decision.pending if type(decision) is SupervisorAssistPendingDecision else None
        if (
            type(scope) is not AssistConversationScope
            or type(decision) is not SupervisorAssistPendingDecision
            or decision.relation is not SupervisorAssistPendingRelation.NEW_TURN
            or type(pending) is not PendingDurableTurnAdmission
            or not pending.is_owned
            or pending.work_graph_id is None
            or pending.work_item_id is not None
            or not pending.matches_scope(
                person_id=scope.user_id,
                conversation_id=scope.conversation_id,
            )
            or _exact_future_deadline(absolute_deadline) is None
            or self._closed
        ):
            return AssistPendingGraphDisposition.UNCERTAIN
        key = (scope.user_id, scope.conversation_id)
        active = self._active_by_scope.get(key)
        if active is not None:
            if (
                active.graph.id != pending.work_graph_id
                or pending.revision is None
                or active.graph.revision < pending.revision
                or active.graph.anchor_request_binding_sha256 != decision.root_request_binding_sha256
                or active.pending.work_graph_id != active.graph.id
                or active.pending.revision != active.graph.revision
            ):
                return AssistPendingGraphDisposition.UNCERTAIN
            if (
                active.graph.state is CompareCurrentFileWebGraphState.ACTIVE
                and active.committed_result is None
            ):
                return AssistPendingGraphDisposition.LIVE_IN_PROCESS
            return AssistPendingGraphDisposition.RETIRED

        retained = self._retained_by_scope.get(key)
        if retained is not None:
            if (
                retained.graph.id != pending.work_graph_id
                or retained.graph.revision != pending.revision
                or retained.graph.anchor_request_binding_sha256 != decision.root_request_binding_sha256
                or retained.pending != pending
            ):
                return AssistPendingGraphDisposition.UNCERTAIN
            return (
                AssistPendingGraphDisposition.RETIRED
                if await self._reconcile_retained(retained)
                else AssistPendingGraphDisposition.UNCERTAIN
            )

        try:
            current = self._graph_adapter.load_current(scope)
        except Exception:
            self._known_durable_active_scopes.add(key)
            return AssistPendingGraphDisposition.UNCERTAIN
        if current is None:
            self._known_durable_active_scopes.discard(key)
            return AssistPendingGraphDisposition.RETIRED
        self._known_durable_active_scopes.add(key)
        return AssistPendingGraphDisposition.UNCERTAIN

    async def execute(
        self,
        surface: CurrentFileWebAssistSurface | None,
        *,
        legacy_primary: Callable[[], Awaitable[Mapping[str, Any]]],
        absolute_deadline: float,
    ) -> SupervisorAssistResult:
        """Execute one eligible turn or invoke the legacy primary before ownership."""

        if not callable(legacy_primary):
            raise TypeError("legacy primary is unavailable")
        task = asyncio.current_task()
        if task is None:
            raise SupervisorAssistControllerError("assist dispatch needs one asyncio task")
        self._dispatch_tasks.add(task)
        record: _OwnedRun | None = None
        started_at = time.monotonic()
        try:
            deadline = _exact_future_deadline(absolute_deadline)
            if self._closed:
                return await self._legacy(legacy_primary, reason="controller_closed")
            if type(surface) is not CurrentFileWebAssistSurface:
                return await self._legacy(legacy_primary, reason="surface_not_admitted")
            admitted_surface = cast(CurrentFileWebAssistSurface, surface)
            scope = (admitted_surface.actor.user_id, admitted_surface.conversation_id)
            if scope in self._active_by_scope:
                return await self._legacy(legacy_primary, reason="conversation_assist_active")
            retained = self._retained_by_scope.get(scope)
            if retained is not None:
                if not hmac.compare_digest(
                    retained.graph.anchor_request_binding_sha256,
                    admitted_surface.ingress_binding.canonical_sha256(),
                ):
                    # A distinct successor cannot terminalize its predecessor.
                    # The retained owner waits for the next pre-admission pass.
                    return await self._legacy(
                        legacy_primary,
                        reason="conversation_assist_retained_predecessor",
                    )
                if not await self._reconcile_retained(retained):
                    self._ownership_uncertain_total += 1
                    return SupervisorAssistResult(
                        outcome=SupervisorAssistOutcome.OWNERSHIP_UNCERTAIN,
                        pending_admission=PendingDurableTurnAdmission.uncertain(
                            person_id=admitted_surface.actor.user_id,
                            conversation_id=admitted_surface.conversation_id,
                        ),
                        promotion_decision=retained.decision,
                    )
            elif scope in self._known_durable_active_scopes:
                pending = self.pending_durable_turn_admission(
                    admitted_surface.actor.user_id,
                    admitted_surface.turn.message,
                    actor=admitted_surface.actor,
                    conversation_id=admitted_surface.conversation_id,
                    current_attachment_count=1,
                )
                if pending is not False:
                    self._ownership_uncertain_total += 1
                    return SupervisorAssistResult(
                        outcome=SupervisorAssistOutcome.OWNERSHIP_UNCERTAIN,
                        pending_admission=(
                            pending
                            if type(pending) is PendingDurableTurnAdmission
                            else PendingDurableTurnAdmission.uncertain(
                                person_id=admitted_surface.actor.user_id,
                                conversation_id=admitted_surface.conversation_id,
                            )
                        ),
                    )
            if deadline is None:
                return await self._legacy(legacy_primary, reason="deadline_exhausted")
            try:
                prospective = await self._prepare_prospective(
                    admitted_surface,
                    absolute_deadline=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                prospective = None
            if prospective is None:
                return await self._legacy(legacy_primary, reason="promotion_not_admitted")
            if _exact_future_deadline(prospective.absolute_deadline) is None:
                return await self._legacy(legacy_primary, reason="deadline_exhausted")
            admitted_surface.require_current_authenticated_call_scope()
            attempt = self._admit_or_recover(prospective)
            if attempt.certainty is _AdmissionCertainty.NO_COMMIT:
                return await self._legacy(legacy_primary, reason="ownership_not_committed")
            if attempt.certainty is _AdmissionCertainty.UNCERTAIN or attempt.graph is None:
                self._ownership_uncertain_total += 1
                return SupervisorAssistResult(
                    outcome=SupervisorAssistOutcome.OWNERSHIP_UNCERTAIN,
                    pending_admission=PendingDurableTurnAdmission.uncertain(
                        person_id=admitted_surface.actor.user_id,
                        conversation_id=admitted_surface.conversation_id,
                    ),
                    promotion_decision=prospective.decision,
                )
            try:
                record = self._register_owned(
                    prospective,
                    attempt.graph,
                    started_at=started_at,
                )
            except Exception:
                self._ownership_uncertain_total += 1
                return SupervisorAssistResult(
                    outcome=SupervisorAssistOutcome.OWNERSHIP_UNCERTAIN,
                    pending_admission=PendingDurableTurnAdmission.uncertain(
                        person_id=admitted_surface.actor.user_id,
                        conversation_id=admitted_surface.conversation_id,
                    ),
                    promotion_decision=prospective.decision,
                )
            if attempt.interrupted:
                return SupervisorAssistResult(
                    outcome=SupervisorAssistOutcome.INTERRUPTED,
                    pending_admission=record.pending,
                    promotion_decision=record.decision,
                )
            return await self._run_owned(
                record,
                absolute_deadline=prospective.absolute_deadline,
            )
        finally:
            if record is not None:
                self._unregister_owned(record)
            self._dispatch_tasks.discard(task)

    async def cancel_active(
        self,
        scope: object,
        *,
        decision: SupervisorAssistPendingDecision,
        user_message: str,
        absolute_deadline: float,
    ) -> SupervisorAssistResult | None:
        """Cancel exactly one in-process graph after draining its body tasks."""

        deadline = _exact_future_deadline(absolute_deadline)
        pending = decision.pending if type(decision) is SupervisorAssistPendingDecision else None
        if (
            type(scope) is not AssistConversationScope
            or type(decision) is not SupervisorAssistPendingDecision
            or decision.relation is not SupervisorAssistPendingRelation.EXPLICIT_CANCEL
            or type(pending) is not PendingDurableTurnAdmission
            or pending.work_graph_id is None
            or decision.current_request_binding_sha256 is None
            or user_message not in {"отмена", "cancel"}
            or deadline is None
            or self._closed
        ):
            return None
        exact_scope = cast(AssistConversationScope, scope)
        key = (exact_scope.user_id, exact_scope.conversation_id)
        record = self._active_by_scope.get(key)
        retained = False
        if record is None:
            record = self._retained_by_scope.get(key)
            retained = record is not None
        if record is None:
            return None
        if (
            record.graph.id != pending.work_graph_id
            or pending.revision is None
            or record.graph.revision < pending.revision
            or record.graph.anchor_request_binding_sha256 != decision.root_request_binding_sha256
        ):
            return None
        if (
            record.graph.state is not CompareCurrentFileWebGraphState.ACTIVE
            and record.committed_result is not None
        ):
            return record.committed_result
        record.cancel_requested = True
        record.stop.set()
        for child in tuple(record.children):
            if not child.done():
                child.cancel()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                if record.children:
                    await asyncio.gather(*tuple(record.children), return_exceptions=True)
            async with record.mutation_lock:
                if record.committed_result is not None:
                    result = None
                else:
                    request = AssistCancellation(
                        user_message=user_message,
                        trace=self._trace(record),
                        request_binding_sha256=decision.current_request_binding_sha256,
                    )
                    publication = self._graph_adapter.cancel(
                        self._cursor(record),
                        request,
                        authority_check=self._publication_check(
                            record,
                            self._authority_for(record.surface.actor),
                            allow_interrupted=retained,
                        ),
                        effect_check=self._publication_check(
                            record,
                            self._effect_check,
                            allow_interrupted=retained,
                        ),
                    )
                    result = self._committed_result(
                        record,
                        publication,
                        outcome=SupervisorAssistOutcome.CANCELLED,
                        include_file_citation=False,
                    )
            if result is not None:
                result = await self._observe_committed(record, result)
        except BaseException:
            result = SupervisorAssistResult(
                outcome=SupervisorAssistOutcome.INTERRUPTED,
                pending_admission=record.pending,
                promotion_decision=record.decision,
            )
        record.cancellation_result = result
        record.cancellation_done.set()
        if result is not None and result.outcome is SupervisorAssistOutcome.CANCELLED:
            self._retained_by_scope.pop(key, None)
            self._known_durable_active_scopes.discard(key)
        return result

    async def close(self) -> None:
        """Drain process work without converting RUNNING graphs into terminals."""

        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        recovery_task = self._restart_recovery_task
        if recovery_task is not None and recovery_task is not current and not recovery_task.done():
            recovery_task.cancel()
        records = tuple(self._active_by_graph.values())
        owned_tasks = {record.task for record in records}
        children: list[asyncio.Task[Any]] = []
        for record in records:
            record.shutdown_requested = True
            record.stop.set()
            children.extend(record.children)
        for child in children:
            if not child.done():
                child.cancel()
        preownership = tuple(
            task
            for task in self._dispatch_tasks
            if task is not current and task not in owned_tasks and not task.done()
        )
        for task in preownership:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        remaining = tuple(task for task in self._dispatch_tasks if task is not current and not task.done())
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        if recovery_task is not None and recovery_task is not current:
            await asyncio.gather(recovery_task, return_exceptions=True)


__all__ = [
    "AssistCommittedObservation",
    "AssistComparisonSynthesizer",
    "AssistAuthorityCheck",
    "AssistEffectCheck",
    "AssistFileEvidenceReader",
    "AssistObservationStatus",
    "AssistPendingGraphDisposition",
    "AssistPlanAuthorityCheck",
    "AssistPlanner",
    "AssistPostCommitObserver",
    "AssistPrimaryModel",
    "AssistPromotionDecisionProvider",
    "AssistReviewer",
    "AssistWebEvidenceReader",
    "SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA",
    "SupervisorAssistControllerError",
    "SupervisorAssistGraphPort",
    "SupervisorAssistOutcome",
    "SupervisorAssistResult",
]
