"""Bounded owner for the promoted current-file/current-web assist journey.

The controller has one irreversible boundary: before WorkGraph admission it may
fall back to the unchanged primary route exactly once; after durable ownership
it can only complete, publish a code-owned terminal, or leave an interrupted
RUNNING graph for startup retirement.  Proposal and review model output remain
process-local and never become execution or publication authority.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
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
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    operational_capability_snapshot,
)
from friday.orchestration.current_file_web_comparison import (
    CurrentFileWebComparison,
    CurrentFileWebComparisonError,
    compare_current_file_with_web,
    current_file_web_comparison_is_process_owned,
    current_file_web_request_is_admitted,
)
from friday.orchestration.execution_plan import ValidatedExecutionPlan
from friday.orchestration.policy_kernel import PolicyAdmissionContext
from friday.orchestration.semantic_supervisor import (
    ParsedSupervisorProposal,
    binding_digest,
    build_supervisor_input,
)
from friday.orchestration.supervisor_assist_graph_adapter import (
    AssistAdmissionBoundary,
    AssistBoundaryCheck,
    AssistCancellation,
    AssistCapabilityBoundary,
    AssistClaimedStep,
    AssistComparisonPublication,
    AssistConversationScope,
    AssistGraphAdmission,
    AssistGraphCursor,
    AssistGraphPublication,
    AssistPublicationBoundary,
    AssistRestartResult,
    AssistStepSettlement,
    AssistTerminalPublication,
    AssistTraceInput,
)
from friday.orchestration.supervisor_assist_promotion import (
    AssistPromotionDecision,
    AssistPromotionReadiness,
    AssistPromotionReason,
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
from friday.orchestration.supervisor_review_policy import (
    AdmittedReadRecovery,
    DeterministicReviewState,
    ReadRecoveryCandidate,
    SupervisorReviewContext,
)
from friday.orchestration.supervisor_review_transport import AdmittedSupervisorReview
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonEvidence,
    TransientWebEvidenceStatus,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext, AuthorizationError
from friday.semantic_supervisor_policy import SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
from friday.source_identity import authorized_file_snapshot_token_is_process_owned

SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA = "friday.semantic-supervisor-assist-controller-status.v1"

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
        plan: object,
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


@dataclass(frozen=True, slots=True)
class _ProspectiveAdmission:
    surface: CurrentFileWebAssistSurface = field(repr=False)
    plan: ValidatedExecutionPlan = field(repr=False)
    decision: AssistPromotionDecision = field(repr=False)
    binding_snapshot: CapabilityBindingSnapshot = field(repr=False)
    canary_actor_binding_sha256: str


@dataclass(slots=True)
class _OwnedRun:
    surface: CurrentFileWebAssistSurface = field(repr=False)
    decision: AssistPromotionDecision = field(repr=False)
    plan: ValidatedExecutionPlan = field(repr=False)
    canary_actor_binding_sha256: str
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
    if (
        type(evidence) is not PreparedFileEvidence
        or not prepared_file_evidence_is_process_owned(evidence)
        or evidence.tenant_id != surface.actor.user_id
        or evidence.person_id != surface.actor.own_id
        or evidence.historical_selection is not None
        or evidence.raw_ids != (surface.attachment.raw_object_id,)
        or len(evidence.snapshot_tokens) != 1
    ):
        return False
    token = evidence.snapshot_tokens[0]
    return bool(
        authorized_file_snapshot_token_is_process_owned(token)
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
    try:
        evidence.__post_init__()
        return bool(
            evidence.plan_sha256 == surface.web_plan.canonical_sha256()
            and evidence.query_sha256 == surface.web_plan.query_sha256
        )
    except Exception:
        return False


def _graph_matches_pristine_admission(
    graph: object,
    prospective: _ProspectiveAdmission,
) -> bool:
    if type(graph) is not CompareCurrentFileWebWorkGraph:
        return False
    surface = prospective.surface
    plan = prospective.plan
    bindings = bind_assist_plan_to_surface(plan, surface)
    if bindings is None:
        return False
    return bool(
        graph.state is CompareCurrentFileWebGraphState.ACTIVE
        and graph.transition is CompareCurrentFileWebGraphTransition.ADMITTED
        and graph.revision == 1
        and graph.user_id == surface.actor.user_id
        and graph.conversation_id == surface.conversation_id
        and graph.current_file_raw_object_id == surface.attachment.raw_object_id
        and graph.current_file_source_identity_sha256 == surface.attachment.source_identity_sha256
        and graph.current_file_content_sha256 == surface.attachment_content_sha256
        and graph.proposal_sha256 == plan.proposal_digest
        and graph.accepted_plan_sha256 == plan.canonical_sha256()
        and graph.manifest_sha256 == plan.manifest_digest
        and graph.runtime_profile_sha256 == SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        and graph.adapter_registry_sha256 == plan.binding_snapshot_sha256
        and graph.actor_binding_sha256 == plan.actor_binding_sha256
        and graph.conversation_binding_sha256 == plan.conversation_binding_sha256
        and all(
            graph.step(binding.graph_step_id).idempotency_key_sha256 == binding.plan_step.idempotency_key
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
        effect_check: AssistEffectCheck,
        post_commit_observer: AssistPostCommitObserver,
        max_review_rounds: int,
        binding_snapshot_factory: Callable[[], CapabilityBindingSnapshot] = (operational_capability_snapshot),
        synthesizer: AssistComparisonSynthesizer | None = None,
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
            ("effect check", effect_check),
            ("post-commit observer", post_commit_observer),
            ("binding snapshot factory", binding_snapshot_factory),
            ("synthesizer", compare_current_file_with_web if synthesizer is None else synthesizer),
        ):
            if not callable(dependency):
                raise TypeError(f"{label} is unavailable")
        if reviewer is not None and not callable(getattr(reviewer, "review", None)):
            raise TypeError("reviewer is unavailable")
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
        self._effect_check = effect_check
        self._post_commit_observer = post_commit_observer
        self._max_review_rounds = max_review_rounds
        self._binding_snapshot_factory = binding_snapshot_factory
        self._synthesizer = (
            cast(AssistComparisonSynthesizer, compare_current_file_with_web)
            if synthesizer is None
            else synthesizer
        )
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
        self._last_promotion_admitted = False
        self._last_effective_mode = SupervisorMode.OFF
        self._closed = False

    def semantic_supervisor_status(self) -> dict[str, object]:
        """Return bounded aggregates without user, graph, query or body data."""

        requested = SupervisorMode.fail_closed(
            getattr(self._settings, "semantic_supervisor_mode", SupervisorMode.OFF.value)
        )
        return {
            "schema": SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA,
            "installed": True,
            "role": "durable_read_only_assist",
            "requested_mode": requested.value,
            "effective_mode": (SupervisorMode.OFF.value if self._closed else self._last_effective_mode.value),
            "promotion_admitted": self._last_promotion_admitted and not self._closed,
            "max_review_rounds": self._max_review_rounds,
            "promotion_attempt_total": self._promotion_attempt_total,
            "promotion_evaluation_total": self._promotion_evaluation_total,
            "promotion_admitted_total": self._promotion_admitted_total,
            "active_tasks": len(self._active_by_graph),
            "retained_active_graphs": len(
                set(self._retained_by_scope) | self._known_durable_active_scopes
            ),
            "fallback_total": self._fallback_total,
            "invoked_total": self._invoked_total,
            "publication_total": self._publication_total,
            "terminal_publication_total": self._terminal_publication_total,
            "event_success_total": self._event_success_total,
            "event_failure_total": self._event_failure_total,
            "ownership_uncertain_total": self._ownership_uncertain_total,
            "fallback_reasons": _safe_counter(self._fallback_reasons),
            "runtime_owner": "durable_graph_after_admission",
            "publication_owner": "primary",
            "tools_allowed": False,
            "effects_allowed": False,
            "closed": self._closed,
            "scheduler": _scheduler_identity(self._promotion),
        }

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
        canary_actor_binding: str,
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
        if (
            type(decision) is not AssistPromotionDecision
            or not decision.promotion_admitted
            or decision.reason is not AssistPromotionReason.ADMITTED
            or decision.readiness is not AssistPromotionReadiness.LIVE_EVIDENCE_READY
            or decision.admitted_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
            or decision.requested_mode is not decision.admitted_mode
            or decision.evidence_sha256 is None
            or decision.execution_authorized
            or decision.publication_authorized
            or decision.storage_write_authorized
        ):
            return None
        return decision

    async def _prepare_prospective(
        self,
        surface: CurrentFileWebAssistSurface,
        *,
        absolute_deadline: float,
    ) -> _ProspectiveAdmission | None:
        if (
            type(surface) is not CurrentFileWebAssistSurface
            or surface.actor.user_id != surface.actor.own_id
            or not current_file_web_request_is_admitted(surface.turn.message)
            or self._closed
        ):
            return None
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
            self._last_promotion_admitted = False
            self._last_effective_mode = SupervisorMode.OFF
            return None
        try:
            supervisor_input = build_supervisor_input(surface.turn, self._settings)
            context = PolicyAdmissionContext(
                actor_binding_sha256=binding_digest("actor", surface.actor.own_id),
                conversation_binding_sha256=binding_digest(
                    "conversation",
                    surface.conversation_id,
                ),
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
            absolute_deadline=absolute_deadline,
            pre_dispatch_validator=planning_still_current,
        )
        if (
            type(parsed) is not ParsedSupervisorProposal
            or not parsed.decision.admitted
            or type(parsed.decision.plan) is not ValidatedExecutionPlan
        ):
            return None
        plan = parsed.decision.plan
        if (
            parsed.proposal_digest != plan.proposal_digest
            or plan.binding_snapshot_sha256 != snapshot.digest_hex()
            or plan.actor_binding_sha256 != context.actor_binding_sha256
            or plan.conversation_binding_sha256 != context.conversation_binding_sha256
            or bind_assist_plan_to_surface(plan, surface) is None
        ):
            return None
        primary_ready = await self._primary_model.prepare_primary_model(
            absolute_deadline=absolute_deadline,
        )
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
        self._last_promotion_admitted = True
        self._last_effective_mode = final_decision.admitted_mode
        return _ProspectiveAdmission(
            surface=surface,
            plan=plan,
            decision=final_decision,
            binding_snapshot=fresh,
            canary_actor_binding_sha256=canary_binding,
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
                    or boundary.accepted_plan_sha256 != prospective.plan.canonical_sha256()
                    or boundary.adapter_registry_sha256
                    != prospective.plan.binding_snapshot_sha256
                    or boundary.actor_binding_sha256
                    != prospective.plan.actor_binding_sha256
                    or boundary.conversation_binding_sha256
                    != prospective.plan.conversation_binding_sha256
                    or boundary.current_file_raw_object_id
                    != prospective.surface.attachment.raw_object_id
                    or boundary.current_file_source_identity_sha256
                    != prospective.surface.attachment.source_identity_sha256
                    or boundary.current_file_content_sha256
                    != prospective.surface.attachment_content_sha256
                    or boundary.web_plan_sha256
                    != prospective.surface.web_plan.canonical_sha256()
                    or boundary.web_query_sha256 != prospective.surface.web_plan.query_sha256
                    or boundary.runtime_profile_sha256
                    != SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
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
            metrics=_RunMetrics(started_at=started_at),
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
        if (
            record.graph.state is CompareCurrentFileWebGraphState.ACTIVE
            and record.committed_result is None
        ):
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
            evidence = await self._file_reader.prepare(
                record.surface,
                absolute_deadline=absolute_deadline,
            )
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
            evidence = await self._web_reader.research(
                plan=record.surface.web_plan,
                actor=record.surface.actor,
                conversation_id=record.surface.conversation_id,
                current_user_message=record.surface.turn.message,
                absolute_deadline=absolute_deadline,
            )
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
            admitted = await self._reviewer.review(
                context,
                absolute_deadline=absolute_deadline,
                pre_dispatch_validator=review_still_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            record.metrics.accounting_complete = False
            return None
        if (
            type(admitted) is not AdmittedSupervisorReview
            or admitted.context_sha256 != context.canonical_sha256()
            or not admitted.decision.admitted
            or admitted.decision.recovery is None
        ):
            record.metrics.accounting_complete = False
            return None
        record.metrics.model_calls += 1
        return admitted.decision.recovery

    @staticmethod
    def _cursor(record: _OwnedRun) -> AssistGraphCursor:
        return AssistGraphCursor.from_graph(record.graph)

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
                or boundary.current_file_content_sha256
                != record.surface.attachment_content_sha256
            ):
                return False
            try:
                return downstream(boundary) is True
            except Exception:
                return False

        return check

    def _publication_check(
        self,
        record: _OwnedRun,
        downstream: AssistBoundaryCheck[AssistPublicationBoundary],
        *,
        allow_interrupted: bool = False,
    ) -> AssistBoundaryCheck[AssistPublicationBoundary]:
        def check(boundary: AssistPublicationBoundary) -> bool:
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
                or boundary.current_file_content_sha256
                != record.surface.attachment_content_sha256
            ):
                return False
            try:
                return downstream(boundary) is True
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
            return active.pending
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
            retained.graph = graph
            retained.pending = pending
        return pending

    async def _claim(
        self,
        record: _OwnedRun,
        kind: CompareCurrentFileWebStepKind,
    ) -> bool:
        if record.stop.is_set():
            return False
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
        record.graph = graph
        return True

    async def _settle(self, record: _OwnedRun, result: _ReadResult) -> bool:
        if record.stop.is_set():
            return False
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
        record.graph = graph
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
            state_restored=False,
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
        record.graph = graph
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
                publication = self._graph_adapter.publish_comparison(
                    self._cursor(record),
                    request,
                    authority_check=self._publication_check(
                        record,
                        self._authority_for(record.surface.actor),
                    ),
                    effect_check=self._publication_check(
                        record,
                        self._effect_check,
                    ),
                )
                result = self._committed_result(
                    record,
                    publication,
                    outcome=SupervisorAssistOutcome.PUBLISHED,
                    include_file_citation=True,
                )
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
            try:
                publication = self._graph_adapter.publish_terminal(
                    self._cursor(record),
                    request,
                    authority_check=self._publication_check(
                        record,
                        self._authority_for(record.surface.actor),
                    ),
                    effect_check=self._publication_check(
                        record,
                        self._effect_check,
                    ),
                )
                result = self._committed_result(
                    record,
                    publication,
                    outcome=SupervisorAssistOutcome.TERMINAL,
                    include_file_citation=False,
                )
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
        record.graph = graph
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
                file_value = await asyncio.gather(file_task, return_exceptions=True)
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
            values = await asyncio.gather(file_task, web_task, return_exceptions=True)
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
                    recovery = await review_task
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
                        retry_value = await retry_task
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
                    comparison = await synthesis_task
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
        record.graph = current
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
            scope = (surface.actor.user_id, surface.conversation_id)
            if scope in self._active_by_scope:
                return await self._legacy(legacy_primary, reason="conversation_assist_active")
            retained = self._retained_by_scope.get(scope)
            if retained is not None:
                if not await self._reconcile_retained(retained):
                    self._ownership_uncertain_total += 1
                    return SupervisorAssistResult(
                        outcome=SupervisorAssistOutcome.OWNERSHIP_UNCERTAIN,
                        pending_admission=PendingDurableTurnAdmission.uncertain(
                            person_id=surface.actor.user_id,
                            conversation_id=surface.conversation_id,
                        ),
                        promotion_decision=retained.decision,
                    )
            elif scope in self._known_durable_active_scopes:
                pending = self.pending_durable_turn_admission(
                    surface.actor.user_id,
                    surface.turn.message,
                    actor=surface.actor,
                    conversation_id=surface.conversation_id,
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
                                person_id=surface.actor.user_id,
                                conversation_id=surface.conversation_id,
                            )
                        ),
                    )
            if deadline is None:
                return await self._legacy(legacy_primary, reason="deadline_exhausted")
            try:
                prospective = await self._prepare_prospective(
                    surface,
                    absolute_deadline=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                prospective = None
            if prospective is None:
                return await self._legacy(legacy_primary, reason="promotion_not_admitted")
            if _exact_future_deadline(deadline) is None:
                return await self._legacy(legacy_primary, reason="deadline_exhausted")
            attempt = self._admit_or_recover(prospective)
            if attempt.certainty is _AdmissionCertainty.NO_COMMIT:
                return await self._legacy(legacy_primary, reason="ownership_not_committed")
            if attempt.certainty is _AdmissionCertainty.UNCERTAIN or attempt.graph is None:
                self._ownership_uncertain_total += 1
                return SupervisorAssistResult(
                    outcome=SupervisorAssistOutcome.OWNERSHIP_UNCERTAIN,
                    pending_admission=PendingDurableTurnAdmission.uncertain(
                        person_id=surface.actor.user_id,
                        conversation_id=surface.conversation_id,
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
                        person_id=surface.actor.user_id,
                        conversation_id=surface.conversation_id,
                    ),
                    promotion_decision=prospective.decision,
                )
            if attempt.interrupted:
                return SupervisorAssistResult(
                    outcome=SupervisorAssistOutcome.INTERRUPTED,
                    pending_admission=record.pending,
                    promotion_decision=record.decision,
                )
            return await self._run_owned(record, absolute_deadline=deadline)
        finally:
            if record is not None:
                self._unregister_owned(record)
            self._dispatch_tasks.discard(task)

    async def cancel_active(
        self,
        scope: object,
        *,
        user_message: str,
        absolute_deadline: float,
    ) -> SupervisorAssistResult | None:
        """Cancel exactly one in-process graph after draining its body tasks."""

        deadline = _exact_future_deadline(absolute_deadline)
        if (
            type(scope) is not AssistConversationScope
            or user_message not in {"отмена", "cancel"}
            or deadline is None
            or self._closed
        ):
            return None
        key = (scope.user_id, scope.conversation_id)
        record = self._active_by_scope.get(key)
        retained = False
        if record is None:
            record = self._retained_by_scope.get(key)
            retained = record is not None
        if record is None:
            return None
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
        current = asyncio.current_task()
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


__all__ = [
    "AssistCommittedObservation",
    "AssistComparisonSynthesizer",
    "AssistAuthorityCheck",
    "AssistEffectCheck",
    "AssistFileEvidenceReader",
    "AssistObservationStatus",
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
