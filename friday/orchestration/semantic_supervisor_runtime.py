"""Non-owning live shadow seam for the optional semantic supervisor.

The wrapper deliberately sits outside the primary routing decision.  It takes
one bounded, private projection before the primary await, returns the exact
primary object, and only then starts discarded secondary work.  No proposal
can reach tools, publication, storage, or the caller.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, TypeVar, cast

from friday import semantic_supervisor_policy
from friday.orchestration.contracts import TurnInput
from friday.orchestration.policy_kernel import PolicyAdmissionContext
from friday.orchestration.semantic_supervisor import (
    binding_digest,
    build_supervisor_input,
    build_supervisor_request,
    map_secondary_failure,
    shadow_policy_admission_context,
    supervisor_eligibility,
    supervisor_mode_from_settings,
    supervisor_timeout_sec,
    validate_shadow_proposal,
)
from friday.orchestration.supervisor_contracts import (
    SupervisorContractError,
    SupervisorInput,
    SupervisorMode,
)
from friday.orchestration.supervisor_observation import (
    SupervisorObservation,
    SupervisorSkipReason,
    parsed_observation,
    skipped_observation,
)
from friday.orchestration.supervisor_trace_join import (
    PrimaryTraceProjection,
    load_primary_trace_projection,
    persist_joined_supervisor_observation,
)
from friday.orchestration.turn_context import AuthenticatedTurnContext, TurnContextError
from friday.orchestration.turn_context_advisory import suspend_authenticated_advisory_authority
from friday.orchestration.turn_context_call_scope import (
    UNSPECIFIED_CHAT_ADJUNCT,
    AuthenticatedChatCallScope,
    require_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_runtime import (
    current_primary_authenticated_turn_context,
    reserve_authenticated_advisory_call,
)
from friday.pending_durable_turn import (
    PendingDurableTurnAdmission,
    pending_comparison_current_attachment_count,
)
from friday.permissions import ActorContext
from friday.secondary_brain import (
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryResult,
)
from friday.turn_intent_policy import TurnPolicyDecision

_MAX_PENDING_SHADOW_ATTEMPTS = 4
_MAX_RETAINED_OBSERVATIONS = 256
_MAX_DISPATCH_SCOPES = 4_096
_SHADOW_CLOSE_DRAIN_TIMEOUT_SEC = 1.0
_RUNTIME_STATUS_SCHEMA = "friday.semantic-supervisor-shadow-runtime.v1"
_SAFE_ROUTES = frozenset({"legacy", "shadow", "canary", "v12"})
_SAFE_PROFILE_ID = semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID

RuntimeT = TypeVar("RuntimeT")


class ShadowEvaluator(Protocol):
    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt: ...


class PrimaryChatRuntime(Protocol):
    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _PreparedShadow:
    turn: TurnInput
    supervisor_input: SupervisorInput
    request: ModelRequest
    context: PolicyAdmissionContext
    task_class: str
    requested_mode: str
    current_route: str
    accepted_profile_id: str
    # Raw scope carriers are retained only for the pre-S2 legacy path.  An
    # authenticated prepared job uses the issued digests and leaves these
    # fields empty before it can outlive the primary call.
    routing_user_id: str | None
    actor: ActorContext | None
    conversation_id: str | None
    current_attachment_count: int
    dispatch_scope: str
    dispatch_epoch: int
    # Inherited turn cap. None means only the supervisor timeout, which starts
    # at dispatch after the primary await, not at prepare.
    parent_deadline_monotonic: float | None


@dataclass(slots=True)
class _ShadowJob:
    """Process-local bookkeeping for exactly one accepted shadow job."""

    prepared: _PreparedShadow
    primary_trace: PrimaryTraceProjection | None = None
    captured: dict[str, object] = field(
        default_factory=lambda: {
            "proposal_digest": "",
            "proposal_parse_status": "not_received",
            "policy_verdict": "not_evaluated",
            "policy_reason": "none",
            "step_count": 0,
            "effect_classes": (),
            "dispatched": False,
        }
    )
    cancel_reason: SupervisorSkipReason | None = None
    terminal_recorded: bool = False


def _closed_requested_mode(settings: object) -> str:
    return supervisor_mode_from_settings(settings).value


def _current_route(runtime: object) -> str:
    raw = getattr(runtime, "mode", "legacy")
    value = str(getattr(raw, "value", raw) or "").strip().casefold()
    return value if value in _SAFE_ROUTES else "legacy"


def _accepted_profile_id(settings: object) -> str:
    candidate = str(getattr(settings, "secondary_llm_profile", "") or "")
    return _SAFE_PROFILE_ID if candidate == _SAFE_PROFILE_ID else ""


def _shadow_workload_admitted(scheduler: object | None) -> bool:
    """Use the scheduler's exact product-policy result, including for doubles."""

    if scheduler is None:
        return False
    workload_mode = getattr(scheduler, "workload_mode", None)
    if not callable(workload_mode):
        return False
    try:
        admitted = workload_mode(ModelWorkload.PLAN_CANDIDATE)
    except Exception:
        return False
    return str(getattr(admitted, "value", admitted) or "").strip().casefold() == "shadow"


def _ingestion_result_allows_shadow(value: dict[str, Any] | None) -> bool:
    if value is None:
        return True
    if type(value) is not dict:
        return False
    category = value.get("category")
    base_keys = {"promoted", "queued_for_review", "action", "category", "reason"}
    if category == "system_notice":
        if set(value) != base_keys | {"synthetic"} or value.get("synthetic") is not True:
            return False
    elif category in {"web_request", "archive_search_request", "compare_current_file_web"}:
        if set(value) != base_keys:
            return False
    else:
        return False
    return bool(
        value.get("promoted") is False
        and value.get("queued_for_review") is False
        and value.get("action") == "transient"
        and isinstance(value.get("reason"), str)
    )


def _primary_result_matches_conversation(
    result: object,
    expected_conversation_id: str | None,
) -> bool:
    """Require the primary's actual durable scope to equal the sampled scope."""

    if type(result) is not dict or type(expected_conversation_id) is not str:
        return False
    if not 1 <= len(expected_conversation_id) <= 200:
        return False
    try:
        expected_conversation_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    actual = result.get("conversation_id")
    return type(actual) is str and actual == expected_conversation_id


def build_semantic_supervisor_runtime(
    settings: object,
    runtime: RuntimeT,
    scheduler: ShadowEvaluator | object | None,
) -> RuntimeT | SemanticSupervisorShadowRuntime:
    """Install the P1 sidecar only after the exact scheduler policy admits it.

    Default, invalid, mixed, and unavailable configurations preserve object
    identity.  This is important to both legacy lifecycle ownership and the
    existing route type checks.
    """

    if supervisor_mode_from_settings(settings) is SupervisorMode.OFF:
        return runtime
    if not _shadow_workload_admitted(scheduler):
        return runtime
    return SemanticSupervisorShadowRuntime(
        settings=settings,
        primary=cast(PrimaryChatRuntime, runtime),
        scheduler=cast(ShadowEvaluator, scheduler),
    )


class SemanticSupervisorShadowRuntime:
    """Bounded semantic shadow sidecar with no primary or lifecycle ownership."""

    def __init__(
        self,
        *,
        settings: object,
        primary: PrimaryChatRuntime,
        scheduler: ShadowEvaluator,
    ) -> None:
        self._settings = settings
        self._primary = primary
        self._scheduler = scheduler
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self._shadow_task_scopes: dict[asyncio.Task[None], str] = {}
        self._shadow_jobs: dict[asyncio.Task[None], _ShadowJob] = {}
        self._dispatch_epochs: OrderedDict[str, int] = OrderedDict()
        self._dispatch_epoch_serial = 0
        self._observations: deque[SupervisorObservation] = deque(maxlen=_MAX_RETAINED_OBSERVATIONS)
        self._skip_counts: Counter[str] = Counter()
        self._parse_counts: Counter[str] = Counter()
        self._policy_counts: Counter[str] = Counter()
        self._observation_total = 0
        self._invoked_total = 0
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        """Keep every pre-existing runtime surface available to callers."""

        return getattr(self._primary, name)

    @property
    def semantic_supervisor_observations(self) -> tuple[SupervisorObservation, ...]:
        return tuple(self._observations)

    def semantic_supervisor_status(self) -> dict[str, object]:
        """Return a bounded body-free aggregate suitable for health surfaces."""

        requested = _closed_requested_mode(self._settings)
        policy_identity = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(requested)
        return {
            "schema": _RUNTIME_STATUS_SCHEMA,
            "installed": True,
            "role": "discarded_advisory_shadow",
            "requested_mode": requested,
            "effective_mode": "shadow" if requested != "off" and not self._closed else "off",
            "promotion_admitted": False,
            "policy_id": policy_identity.policy_id,
            "policy_sha256": policy_identity.policy_sha256,
            "accepted_profile_id": _accepted_profile_id(self._settings),
            "runtime_owner": "unchanged",
            "publication_owner": "primary",
            "tools_allowed": False,
            "effects_allowed": False,
            "execution_allowed": False,
            "max_pending": _MAX_PENDING_SHADOW_ATTEMPTS,
            "pending": len(self._shadow_tasks),
            "retained": len(self._observations),
            "observation_total": self._observation_total,
            "invoked_total": self._invoked_total,
            "skip_reasons": dict(sorted(self._skip_counts.items())),
            "parse_statuses": dict(sorted(self._parse_counts.items())),
            "policy_verdicts": dict(sorted(self._policy_counts.items())),
        }

    def _record(
        self,
        observation: SupervisorObservation,
        primary_trace: PrimaryTraceProjection | None = None,
    ) -> None:
        if primary_trace is not None:
            observation = observation.with_primary_trace(
                trace_digest=primary_trace.trace_digest,
                capability_outcomes=primary_trace.capability_outcomes,
                completion=primary_trace.completion,
                publication=primary_trace.publication,
                authority_rechecked=primary_trace.authority_rechecked,
                state_restored=primary_trace.state_restored,
                retry_occurred=primary_trace.retry_occurred,
            )
        self._observations.append(observation)
        self._observation_total += 1
        self._invoked_total += int(observation.invoked)
        self._skip_counts[observation.skip_reason.value] += 1
        self._parse_counts[observation.proposal_parse_status] += 1
        self._policy_counts[observation.policy_verdict] += 1
        persist_joined_supervisor_observation(
            self._primary,
            observation_payload=observation.payload(),
            primary_trace=primary_trace,
        )

    @staticmethod
    def _latency_bucket(elapsed_sec: float) -> str:
        elapsed_ms = max(0, round(elapsed_sec * 1_000))
        if elapsed_ms < 250:
            return "lt_250ms"
        if elapsed_ms < 1_000:
            return "250_999ms"
        if elapsed_ms < 2_000:
            return "1_2s"
        if elapsed_ms < 5_000:
            return "2_5s"
        if elapsed_ms <= 15_000:
            return "5_15s"
        return "over_15s"

    def _advance_dispatch_epoch(self, actor: ActorContext, conversation_id: str | None) -> tuple[str, int]:
        """Invalidate older same-scope shadows before this turn can establish state."""

        try:
            scope = binding_digest(
                "dispatch-scope",
                str(actor.own_id or ""),
                str(conversation_id or "new-conversation"),
            )
        except Exception:
            scope = binding_digest("dispatch-scope", "invalid")
        self._dispatch_epoch_serial += 1
        epoch = self._dispatch_epoch_serial
        self._dispatch_epochs[scope] = epoch
        self._dispatch_epochs.move_to_end(scope)
        while len(self._dispatch_epochs) > _MAX_DISPATCH_SCOPES:
            self._dispatch_epochs.popitem(last=False)
        for task, task_scope in tuple(self._shadow_task_scopes.items()):
            if task_scope == scope:
                self._shadow_task_scopes.pop(task, None)
                job = self._shadow_jobs.get(task)
                if job is not None and job.cancel_reason is None:
                    job.cancel_reason = SupervisorSkipReason.EXACT_LANE
                task.cancel()
        return scope, epoch

    def _skipped(
        self,
        reason: SupervisorSkipReason,
        *,
        turn: TurnInput | None = None,
    ) -> SupervisorObservation:
        manifest_digest = ""
        supervisor_input_digest = ""
        if turn is not None:
            try:
                supervisor_input = build_supervisor_input(turn, self._settings)
                manifest_digest = supervisor_input.manifest.digest_hex()
                supervisor_input_digest = binding_digest(
                    "supervisor-input",
                    supervisor_input.canonical_sha256(),
                )
            except Exception:
                pass
        return skipped_observation(
            requested_mode=_closed_requested_mode(self._settings),
            skip_reason=reason,
            current_route=_current_route(self._primary),
            accepted_profile_id=_accepted_profile_id(self._settings),
            manifest_digest=manifest_digest,
            supervisor_input_digest=supervisor_input_digest,
        )

    @staticmethod
    def _special_surface(
        *,
        replay_source_message_id: str | None,
        explicit_mode_requested: bool,
        answer_with_voice: bool,
        reply_to: str | None,
        quoted_attachment_reference: bool,
        reply_assistant_reference: bool,
        reply_assistant_message_id: str | None,
        turn_policy: TurnPolicyDecision | None,
        synthetic_document_notice: bool,
    ) -> bool:
        return bool(
            synthetic_document_notice
            or replay_source_message_id
            or explicit_mode_requested
            or answer_with_voice
            or reply_to
            or quoted_attachment_reference
            or reply_assistant_reference
            or reply_assistant_message_id
            or turn_policy is not None
        )

    def _pending_is_exactly_ordinary(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int,
        carried: object,
    ) -> bool:
        # A carried receipt represents owned or uncertain exact state.  Neither
        # may be sampled even if its scope later proves stale.
        if carried is not None:
            return False
        admission = getattr(self._primary, "pending_durable_turn_admission", None)
        if not callable(admission):
            admission = getattr(self._primary, "owns_pending_durable_turn", None)
        if not callable(admission):
            return False
        kwargs: dict[str, object] = {
            "actor": actor,
            "conversation_id": conversation_id,
        }
        if current_attachment_count:
            kwargs["current_attachment_count"] = current_attachment_count
        try:
            admission_user_id = actor.own_id if actor.shared_tenant else user_id
            result = admission(admission_user_id, message, **kwargs)
        except Exception:
            return False
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            elif isinstance(result, asyncio.Future):
                result.cancel()
            else:
                iterator = result.__await__()
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            return False
        return result is False

    def _prepare_shadow(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        attachments: list[dict[str, Any]] | None,
        enable_tools: bool,
        ingestion_result: dict[str, Any] | None,
        synthetic_document_notice: bool,
        replay_source_message_id: str | None,
        mode: str | None,
        explicit_mode_requested: bool,
        answer_with_voice: bool,
        reply_to: str | None,
        quoted_attachment_reference: bool,
        reply_assistant_reference: bool,
        reply_assistant_message_id: str | None,
        turn_policy: TurnPolicyDecision | None,
        turn_deadline: float | None,
        pending_durable_admission: object,
        dispatch_scope: str,
        dispatch_epoch: int,
        authenticated_scope: AuthenticatedChatCallScope | None,
    ) -> tuple[_PreparedShadow | None, SupervisorObservation | None]:
        if self._closed:
            return None, self._skipped(SupervisorSkipReason.SECONDARY_UNAVAILABLE)
        if not _ingestion_result_allows_shadow(ingestion_result) or self._special_surface(
            replay_source_message_id=replay_source_message_id,
            explicit_mode_requested=explicit_mode_requested,
            answer_with_voice=answer_with_voice,
            reply_to=reply_to,
            quoted_attachment_reference=quoted_attachment_reference,
            reply_assistant_reference=reply_assistant_reference,
            reply_assistant_message_id=reply_assistant_message_id,
            turn_policy=turn_policy,
            synthetic_document_notice=synthetic_document_notice,
        ):
            return None, self._skipped(SupervisorSkipReason.SPECIAL_SURFACE)
        if enable_tools is not True:
            return None, self._skipped(SupervisorSkipReason.EVIDENCE_UNAVAILABLE)

        if authenticated_scope is None:
            if attachments is not None and (
                type(attachments) is not list or any(not isinstance(item, Mapping) for item in attachments)
            ):
                return None, self._skipped(SupervisorSkipReason.EVIDENCE_UNAVAILABLE)
            try:
                attachment_snapshot = [dict(item) for item in (attachments or ())]
                if len(attachment_snapshot) != len(attachments or ()):
                    return None, self._skipped(SupervisorSkipReason.EVIDENCE_UNAVAILABLE)
                turn = TurnInput.from_chat(
                    message=message,
                    actor=actor,
                    conversation_id=conversation_id,
                    attachments=attachment_snapshot,
                    enable_tools=enable_tools,
                    synthetic_document_notice=False,
                    mode=None,
                    reply_to=None,
                    quoted_attachment_reference=False,
                    reply_assistant_reference=False,
                )
            except Exception:
                return None, self._skipped(SupervisorSkipReason.EVIDENCE_UNAVAILABLE)
        else:
            turn = authenticated_scope.model_input
            attachment_snapshot = []
        if authenticated_scope is None and turn.message != message:
            # Pending/exact ownership must always be checked against the same
            # bytes the caller supplied. TurnInput deliberately bounds its
            # projection, so an oversized/truncated turn is not shadowable.
            return None, self._skipped(SupervisorSkipReason.EVIDENCE_UNAVAILABLE, turn=turn)

        current_attachment_count = (
            pending_comparison_current_attachment_count(
                attachments,
                tenant_id=actor.user_id,
            )
            if authenticated_scope is None
            else len(turn.attachments)
        )

        if authenticated_scope is None:
            if not self._pending_is_exactly_ordinary(
                user_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                current_attachment_count=current_attachment_count,
                carried=pending_durable_admission,
            ):
                return None, self._skipped(SupervisorSkipReason.EXACT_LANE, turn=turn)
        elif authenticated_scope.pending_work_bound:
            return None, self._skipped(SupervisorSkipReason.EXACT_LANE, turn=turn)

        eligibility = supervisor_eligibility(turn, self._settings)
        if not eligibility.eligible:
            return None, self._skipped(eligibility.skip_reason, turn=turn)

        parent_deadline: float | None = None
        if authenticated_scope is not None:
            parent_deadline = authenticated_scope.conservative_deadline_monotonic
        elif turn_deadline is not None:
            if not isinstance(turn_deadline, (int, float)) or isinstance(turn_deadline, bool):
                return None, self._skipped(SupervisorSkipReason.TIMEOUT, turn=turn)
            external_deadline = float(turn_deadline)
            if not math.isfinite(external_deadline):
                return None, self._skipped(SupervisorSkipReason.TIMEOUT, turn=turn)
            parent_deadline = external_deadline
        started = time.monotonic()
        deadline = started + supervisor_timeout_sec(self._settings)
        if parent_deadline is not None:
            deadline = min(deadline, parent_deadline)

        try:
            supervisor_input = build_supervisor_input(turn, self._settings)
        except Exception:
            return None, self._skipped(SupervisorSkipReason.EVIDENCE_UNAVAILABLE, turn=turn)

        try:
            if authenticated_scope is None:
                person_id = str(actor.own_id or "")
                conversation_scope = str(conversation_id or "new-conversation")
                if not 1 <= len(person_id) <= 512 or not 1 <= len(conversation_scope) <= 512:
                    raise ValueError("binding scope is unavailable")
                actor_binding = binding_digest("actor", person_id)
                conversation_binding = binding_digest(
                    "conversation",
                    actor_binding,
                    conversation_scope,
                )
            else:
                actor_binding = authenticated_scope.actor_binding_sha256
                conversation_binding = authenticated_scope.conversation_binding_sha256
            context = shadow_policy_admission_context(
                supervisor_input,
                actor_binding_sha256=actor_binding,
                conversation_binding_sha256=conversation_binding,
                turn_deadline_monotonic_ns=int(deadline * 1_000_000_000),
            )
        except Exception:
            return None, self._skipped(SupervisorSkipReason.BINDING_UNAVAILABLE, turn=turn)

        try:
            request = build_supervisor_request(
                supervisor_input,
                absolute_deadline_monotonic=deadline,
            )
        except SupervisorContractError:
            return None, self._skipped(SupervisorSkipReason.SECRET_MATERIAL, turn=turn)
        except Exception:
            return None, self._skipped(SupervisorSkipReason.SECONDARY_UNAVAILABLE, turn=turn)

        return (
            _PreparedShadow(
                turn=turn,
                supervisor_input=supervisor_input,
                request=request,
                context=context,
                task_class=eligibility.task_class.value,
                requested_mode=_closed_requested_mode(self._settings),
                current_route=_current_route(self._primary),
                accepted_profile_id=_accepted_profile_id(self._settings),
                routing_user_id=user_id if authenticated_scope is None else None,
                actor=actor if authenticated_scope is None else None,
                conversation_id=conversation_id if authenticated_scope is None else None,
                current_attachment_count=(current_attachment_count if authenticated_scope is None else 0),
                dispatch_scope=dispatch_scope,
                dispatch_epoch=dispatch_epoch,
                parent_deadline_monotonic=parent_deadline,
            ),
            None,
        )

    def _observation_from_attempt(
        self,
        prepared: _PreparedShadow,
        attempt: SecondaryAttempt,
        captured: Mapping[str, object],
    ) -> SupervisorObservation:
        skip = captured.get("skip")
        if not isinstance(skip, SupervisorSkipReason):
            skip = map_secondary_failure(attempt.failure)
        transport_result_received = captured.get("proposal_parse_status") in {"parsed", "malformed"}
        dispatched = captured.get("dispatched") is True or transport_result_received
        if attempt.succeeded or transport_result_received:
            health = "accepted"
        elif dispatched:
            health = "closed_failure"
        else:
            health = "not_called"
        raw_step_count = captured.get("step_count", 0)
        step_count = raw_step_count if type(raw_step_count) is int else 0
        raw_effects = captured.get("effect_classes", ())
        effect_classes = (
            raw_effects
            if isinstance(raw_effects, tuple)
            and all(item in {"read", "write", "high"} for item in raw_effects)
            else ()
        )
        return parsed_observation(
            requested_mode=prepared.requested_mode,
            manifest_digest=prepared.supervisor_input.manifest.digest_hex(),
            supervisor_input_digest=binding_digest(
                "supervisor-input",
                prepared.supervisor_input.canonical_sha256(),
            ),
            proposal_digest=(
                binding_digest("proposal", str(captured["proposal_digest"]))
                if captured.get("proposal_digest")
                else ""
            ),
            proposal_parse_status=str(captured.get("proposal_parse_status", "not_received")),
            policy_verdict=str(captured.get("policy_verdict", "not_evaluated")),
            policy_reason=str(captured.get("policy_reason", "none")),
            task_class=prepared.task_class,
            step_count=step_count,
            effect_classes=effect_classes,
            current_route=prepared.current_route,
            endpoint_health_class=health,
            accepted_profile_id=prepared.accepted_profile_id,
            skip_reason=skip,
            invoked=dispatched,
            planner_latency_bucket=str(captured.get("planner_latency_bucket", "not_called")),
        )

    def _record_terminal(self, job: _ShadowJob, observation: SupervisorObservation) -> None:
        if job.terminal_recorded:
            return
        self._record(observation, job.primary_trace)
        job.terminal_recorded = True

    def _cancelled_job_observation(self, job: _ShadowJob) -> SupervisorObservation:
        captured = job.captured
        if not isinstance(captured.get("skip"), SupervisorSkipReason):
            captured["skip"] = job.cancel_reason or (
                SupervisorSkipReason.SECONDARY_UNAVAILABLE
                if self._closed
                else SupervisorSkipReason.EXACT_LANE
            )
        return self._observation_from_attempt(
            job.prepared,
            SecondaryAttempt.rejected(SecondaryFailure.CANCELLED),
            captured,
        )

    def _parent_deadline_elapsed(self, prepared: _PreparedShadow) -> bool:
        parent = prepared.parent_deadline_monotonic
        return parent is not None and parent <= time.monotonic()

    def _rebake_prepared_for_dispatch(
        self, prepared: _PreparedShadow
    ) -> _PreparedShadow | SupervisorSkipReason:
        """Start the supervisor timeout at dispatch, capped by the inherited turn."""

        now = time.monotonic()
        if self._parent_deadline_elapsed(prepared):
            return SupervisorSkipReason.TIMEOUT
        deadline = now + supervisor_timeout_sec(self._settings)
        parent = prepared.parent_deadline_monotonic
        if parent is not None:
            deadline = min(deadline, parent)
        if deadline <= now:
            return SupervisorSkipReason.TIMEOUT
        try:
            request = build_supervisor_request(
                prepared.supervisor_input,
                absolute_deadline_monotonic=deadline,
            )
            context = shadow_policy_admission_context(
                prepared.supervisor_input,
                actor_binding_sha256=prepared.context.actor_binding_sha256,
                conversation_binding_sha256=prepared.context.conversation_binding_sha256,
                turn_deadline_monotonic_ns=int(deadline * 1_000_000_000),
            )
        except SupervisorContractError:
            return SupervisorSkipReason.SECRET_MATERIAL
        except Exception:
            return SupervisorSkipReason.SECONDARY_UNAVAILABLE
        return replace(prepared, request=request, context=context)

    async def _complete_shadow(self, job: _ShadowJob) -> None:
        prepared = job.prepared
        if (
            prepared.actor is not None
            and prepared.routing_user_id is not None
            and not self._pending_is_exactly_ordinary(
                prepared.routing_user_id,
                prepared.turn.message,
                actor=prepared.actor,
                conversation_id=prepared.conversation_id,
                current_attachment_count=prepared.current_attachment_count,
                carried=None,
            )
        ):
            self._record_terminal(
                job,
                self._skipped(SupervisorSkipReason.EXACT_LANE, turn=prepared.turn),
            )
            return
        rebaked = self._rebake_prepared_for_dispatch(prepared)
        if isinstance(rebaked, SupervisorSkipReason):
            self._record_terminal(
                job,
                self._skipped(rebaked, turn=prepared.turn),
            )
            return
        prepared = rebaked
        job.prepared = prepared
        if prepared.request.absolute_deadline_monotonic <= time.monotonic():
            self._record_terminal(
                job,
                self._skipped(SupervisorSkipReason.TIMEOUT, turn=prepared.turn),
            )
            return
        captured = job.captured

        def _dispatch_is_still_ordinary() -> bool:
            if prepared.request.absolute_deadline_monotonic <= time.monotonic():
                captured["skip"] = SupervisorSkipReason.TIMEOUT
                return False
            if self._dispatch_epochs.get(prepared.dispatch_scope) != prepared.dispatch_epoch:
                captured["skip"] = SupervisorSkipReason.EXACT_LANE
                return False
            if (
                prepared.actor is not None
                and prepared.routing_user_id is not None
                and not self._pending_is_exactly_ordinary(
                    prepared.routing_user_id,
                    prepared.turn.message,
                    actor=prepared.actor,
                    conversation_id=prepared.conversation_id,
                    current_attachment_count=prepared.current_attachment_count,
                    carried=None,
                )
            ):
                captured["skip"] = SupervisorSkipReason.EXACT_LANE
                return False
            if prepared.request.absolute_deadline_monotonic <= time.monotonic():
                captured["skip"] = SupervisorSkipReason.TIMEOUT
                return False
            return True

        def _validator(result: SecondaryResult) -> bool:
            try:
                digest, verdict, reason, steps, effects = validate_shadow_proposal(
                    result,
                    prepared.supervisor_input,
                    prepared.context,
                )
            except Exception:
                captured["proposal_parse_status"] = "malformed"
                captured["skip"] = SupervisorSkipReason.MALFORMED_PROPOSAL
                return False
            captured.update(
                {
                    "proposal_digest": digest,
                    "proposal_parse_status": "parsed",
                    "policy_verdict": verdict,
                    "policy_reason": reason,
                    "step_count": steps,
                    "effect_classes": effects,
                }
            )
            if verdict != "valid":
                captured["skip"] = SupervisorSkipReason.POLICY_REJECTED
                return False
            return True

        def _mark_dispatched() -> None:
            # This observer runs synchronously only after the HTTP task exists.
            # It retains no endpoint, prompt, response, actor, or conversation data.
            captured["dispatched"] = True

        planner_started = time.monotonic()
        try:
            evaluator = self._scheduler.evaluate_shadow
            attempt = await evaluator(
                prepared.request,
                validator=_validator,
                invalidate_on_rejection=False,
                pre_dispatch_validator=_dispatch_is_still_ordinary,
                dispatch_observer=_mark_dispatched,
            )
            captured["planner_latency_bucket"] = self._latency_bucket(time.monotonic() - planner_started)
            if not isinstance(attempt, SecondaryAttempt):
                attempt = SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
            elif attempt.succeeded and captured.get("proposal_parse_status") != "parsed":
                captured["proposal_parse_status"] = "malformed"
                captured["skip"] = SupervisorSkipReason.MALFORMED_PROPOSAL
                attempt = SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
            elif attempt.succeeded and captured.get("policy_verdict") != "valid":
                captured.setdefault("skip", SupervisorSkipReason.POLICY_REJECTED)
                attempt = SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)
            self._record_terminal(job, self._observation_from_attempt(prepared, attempt, captured))
        except asyncio.CancelledError:
            captured.setdefault(
                "planner_latency_bucket",
                self._latency_bucket(time.monotonic() - planner_started),
            )
            if not isinstance(captured.get("skip"), SupervisorSkipReason):
                captured["skip"] = job.cancel_reason or (
                    SupervisorSkipReason.SECONDARY_UNAVAILABLE
                    if self._closed
                    else SupervisorSkipReason.EXACT_LANE
                )
            self._record_terminal(job, self._cancelled_job_observation(job))
            raise
        except Exception:
            captured.setdefault(
                "planner_latency_bucket",
                self._latency_bucket(time.monotonic() - planner_started),
            )
            self._record_terminal(
                job,
                self._observation_from_attempt(
                    prepared,
                    SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE),
                    captured,
                ),
            )

    def _schedule_after_primary(
        self,
        prepared: _PreparedShadow | None,
        skipped: SupervisorObservation | None,
        primary_trace: PrimaryTraceProjection | None,
        authenticated_context: AuthenticatedTurnContext | None,
    ) -> None:
        if skipped is not None:
            self._record(skipped, primary_trace)
            return
        if prepared is None:
            self._record(
                self._skipped(SupervisorSkipReason.SECONDARY_UNAVAILABLE),
                primary_trace,
            )
            return
        if self._closed:
            self._record(
                self._skipped(SupervisorSkipReason.SECONDARY_UNAVAILABLE, turn=prepared.turn),
                primary_trace,
            )
            return
        if self._parent_deadline_elapsed(prepared):
            self._record(
                self._skipped(SupervisorSkipReason.TIMEOUT, turn=prepared.turn),
                primary_trace,
            )
            return
        if len(self._shadow_tasks) >= _MAX_PENDING_SHADOW_ATTEMPTS:
            self._record(
                self._skipped(SupervisorSkipReason.SATURATED, turn=prepared.turn),
                primary_trace,
            )
            return
        if authenticated_context is not None:
            try:
                reserve_authenticated_advisory_call(authenticated_context)
            except TurnContextError:
                self._record(
                    self._skipped(SupervisorSkipReason.SATURATED, turn=prepared.turn),
                    primary_trace,
                )
                return
        job = _ShadowJob(prepared=prepared, primary_trace=primary_trace)
        with suspend_authenticated_advisory_authority():
            task = asyncio.create_task(
                self._complete_shadow(job),
                name="friday-semantic-supervisor-shadow",
            )
            self._shadow_tasks.add(task)
            self._shadow_task_scopes[task] = prepared.dispatch_scope
            self._shadow_jobs[task] = job
            # add_done_callback captures its registration Context separately
            # from the task.  It must remain inside the same suspension seam.
            task.add_done_callback(self._shadow_done)

    def _finalize_shadow_task(self, task: asyncio.Task[None]) -> None:
        job = self._shadow_jobs.get(task)
        if job is not None and not job.terminal_recorded:
            if task.cancelled():
                observation = self._cancelled_job_observation(job)
            else:
                job.captured.setdefault("skip", SupervisorSkipReason.SECONDARY_UNAVAILABLE)
                observation = self._observation_from_attempt(
                    job.prepared,
                    SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE),
                    job.captured,
                )
            self._record_terminal(job, observation)
        self._shadow_tasks.discard(task)
        self._shadow_task_scopes.pop(task, None)
        self._shadow_jobs.pop(task, None)
        if task.cancelled():
            return
        # Consume any invariant-breaking exception without logging bodies or
        # allowing optional observability to affect the primary service.
        task.exception()

    def _shadow_done(self, task: asyncio.Task[None]) -> None:
        self._finalize_shadow_task(task)

    async def drain_shadow(self) -> None:
        pending = tuple(self._shadow_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            for task in pending:
                if task.done():
                    self._finalize_shadow_task(task)

    async def close(self) -> None:
        """Bound cancellation of sidecars; the service retains primary ownership."""

        if self._closed and not self._shadow_tasks:
            return
        self._closed = True
        pending = tuple(self._shadow_tasks)
        for task in pending:
            job = self._shadow_jobs.get(task)
            if job is not None and job.cancel_reason is None:
                job.cancel_reason = SupervisorSkipReason.SECONDARY_UNAVAILABLE
            task.cancel()
        if not pending:
            return
        try:
            # A faulty optional evaluator may suppress cancellation.  Never let
            # it hold the primary application's teardown chain indefinitely.
            await asyncio.wait(
                pending,
                timeout=_SHADOW_CLOSE_DRAIN_TIMEOUT_SEC,
            )
        finally:
            # Cancelling close() itself must not strand the closed wrapper with
            # tracked work that an idempotent retry refuses to drain.  No await
            # is needed to settle this optional, already-discarded ownership.
            for task in pending:
                if task.done():
                    self._finalize_shadow_task(task)
                    continue
                task.cancel()
                job = self._shadow_jobs.get(task)
                if job is not None:
                    self._record_terminal(job, self._cancelled_job_observation(job))
                    self._shadow_jobs.pop(task, None)
                self._shadow_tasks.discard(task)
                self._shadow_task_scopes.pop(task, None)

    async def chat(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        enable_tools: bool = True,
        kg: Any = UNSPECIFIED_CHAT_ADJUNCT,
        hybrid_searcher: Any = UNSPECIFIED_CHAT_ADJUNCT,
        ingestion_result: Any = UNSPECIFIED_CHAT_ADJUNCT,
        synthetic_document_notice: bool = False,
        replay_source_message_id: str | None = None,
        mode: str | None = None,
        answer_with_voice: bool = False,
        reply_to: str | None = None,
        quoted_attachment_reference: bool = False,
        reply_assistant_reference: bool = False,
        reply_assistant_message_id: str | None = None,
        turn_policy: TurnPolicyDecision | None = None,
        telegram_update_id: str | None = None,
        turn_deadline: float | None = None,
        _pending_durable_admission: PendingDurableTurnAdmission | None = None,
        _semantic_supervisor_explicit_mode_requested: bool | None = None,
        _authenticated_turn_context: AuthenticatedTurnContext | None = None,
    ) -> dict[str, Any]:
        authenticated_context = current_primary_authenticated_turn_context(_authenticated_turn_context)
        authenticated_scope = (
            require_authenticated_chat_call_scope(
                authenticated_context,
                user_id=user_id,
                message=message,
                actor=actor,
                conversation_id=conversation_id,
                attachments=attachments,
                enable_tools=enable_tools,
                synthetic_document_notice=synthetic_document_notice,
                replay_source_message_id=replay_source_message_id,
                mode=mode,
                answer_with_voice=answer_with_voice,
                reply_to=reply_to,
                quoted_attachment_reference=quoted_attachment_reference,
                reply_assistant_reference=reply_assistant_reference,
                reply_assistant_message_id=reply_assistant_message_id,
                turn_policy=turn_policy,
                telegram_update_id=telegram_update_id,
                turn_deadline=turn_deadline,
                pending_durable_admission=_pending_durable_admission,
                kg=kg,
                hybrid_searcher=hybrid_searcher,
                ingestion_result=ingestion_result,
            )
            if authenticated_context is not None
            else None
        )
        effective_turn_deadline = (
            authenticated_scope.deadline_monotonic if authenticated_scope is not None else turn_deadline
        )
        effective_ingestion_result = (
            authenticated_scope.ingestion_result
            if authenticated_scope is not None
            else (None if ingestion_result is UNSPECIFIED_CHAT_ADJUNCT else ingestion_result)
        )
        dispatch_scope, dispatch_epoch = self._advance_dispatch_epoch(actor, conversation_id)
        if _semantic_supervisor_explicit_mode_requested is None:
            explicit_mode_requested = mode is not None
        else:
            explicit_mode_requested = _semantic_supervisor_explicit_mode_requested
        try:
            if type(explicit_mode_requested) is not bool:
                raise TypeError("explicit mode provenance must be boolean")
            prepared, skipped = self._prepare_shadow(
                user_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                attachments=attachments,
                enable_tools=enable_tools,
                ingestion_result=effective_ingestion_result,
                synthetic_document_notice=synthetic_document_notice,
                replay_source_message_id=replay_source_message_id,
                mode=mode,
                explicit_mode_requested=explicit_mode_requested,
                answer_with_voice=answer_with_voice,
                reply_to=reply_to,
                quoted_attachment_reference=quoted_attachment_reference,
                reply_assistant_reference=reply_assistant_reference,
                reply_assistant_message_id=reply_assistant_message_id,
                turn_policy=turn_policy,
                turn_deadline=effective_turn_deadline,
                pending_durable_admission=_pending_durable_admission,
                dispatch_scope=dispatch_scope,
                dispatch_epoch=dispatch_epoch,
                authenticated_scope=authenticated_scope,
            )
        except Exception:
            prepared = None
            skipped = self._skipped(SupervisorSkipReason.SECONDARY_UNAVAILABLE)

        primary_kwargs: dict[str, Any] = {
            "actor": actor,
            "conversation_id": conversation_id,
            "attachments": attachments,
            "enable_tools": enable_tools,
            "synthetic_document_notice": synthetic_document_notice,
            "replay_source_message_id": replay_source_message_id,
            "mode": mode,
            "answer_with_voice": answer_with_voice,
            "reply_to": reply_to,
            "quoted_attachment_reference": quoted_attachment_reference,
            "reply_assistant_reference": reply_assistant_reference,
            "reply_assistant_message_id": reply_assistant_message_id,
            "turn_deadline": effective_turn_deadline,
        }
        if authenticated_scope is not None:
            primary_kwargs.update(authenticated_scope.exact_service_kwargs())
        else:
            primary_kwargs.update(
                kg=None if kg is UNSPECIFIED_CHAT_ADJUNCT else kg,
                hybrid_searcher=(None if hybrid_searcher is UNSPECIFIED_CHAT_ADJUNCT else hybrid_searcher),
                ingestion_result=effective_ingestion_result,
            )
        if turn_policy is not None:
            primary_kwargs["turn_policy"] = turn_policy
        if telegram_update_id is not None:
            primary_kwargs["telegram_update_id"] = telegram_update_id
        if _pending_durable_admission is not None:
            primary_kwargs["_pending_durable_admission"] = _pending_durable_admission
        if authenticated_context is not None:
            primary_kwargs["_authenticated_turn_context"] = authenticated_context
            # Close mutation/replacement between initial preparation and the
            # nested primary boundary.  No body-bearing projection survives in
            # the detached semantic job.
            revalidated_scope = require_authenticated_chat_call_scope(
                authenticated_context,
                user_id=user_id,
                message=message,
                actor=actor,
                conversation_id=conversation_id,
                attachments=attachments,
                enable_tools=enable_tools,
                synthetic_document_notice=synthetic_document_notice,
                replay_source_message_id=replay_source_message_id,
                mode=mode,
                answer_with_voice=answer_with_voice,
                reply_to=reply_to,
                quoted_attachment_reference=quoted_attachment_reference,
                reply_assistant_reference=reply_assistant_reference,
                reply_assistant_message_id=reply_assistant_message_id,
                turn_policy=turn_policy,
                telegram_update_id=telegram_update_id,
                turn_deadline=effective_turn_deadline,
                pending_durable_admission=_pending_durable_admission,
                kg=kg,
                hybrid_searcher=hybrid_searcher,
                ingestion_result=ingestion_result,
            )
            primary_kwargs.update(revalidated_scope.exact_service_kwargs())
        primary_result = await self._primary.chat(user_id, message, **primary_kwargs)
        # The primary response object is sacrosanct even if task creation or
        # structural observation unexpectedly fails.
        with suppress(Exception):
            if prepared is not None and not _primary_result_matches_conversation(
                primary_result,
                conversation_id,
            ):
                skipped = self._skipped(SupervisorSkipReason.EXACT_LANE, turn=prepared.turn)
                prepared = None
            primary_trace = load_primary_trace_projection(self._primary, primary_result)
            self._schedule_after_primary(
                prepared,
                skipped,
                primary_trace,
                authenticated_context,
            )
        return primary_result
