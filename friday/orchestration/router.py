"""Reversible dispatch between the frozen legacy runtime and V12 routes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from friday.file_evidence import CurrentTurnFileReferenceToken, current_turn_file_reference_of
from friday.interaction_control_plane import FailureStage
from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    require_complete_read_only_publication,
)
from friday.orchestration.contracts import (
    RouteClass,
    RouterMode,
    ToolEffect,
    TurnInput,
    TurnPlan,
)
from friday.orchestration.file_read_contract import (
    archive_read_plan_supports_selection,
    file_read_plan_supports_attachment_count,
)
from friday.orchestration.planner import AttestedPlannerRuntime, PlannerModel, V12Planner
from friday.pending_durable_turn import (
    PendingDurableAdmissionState,
    PendingDurableTurnAdmission,
)
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnPolicyDecision

LOGGER = logging.getLogger(__name__)
_MAX_PENDING_SHADOW_PLANS = 4


class _PendingDurableAdmission(StrEnum):
    ORDINARY = "ordinary"
    OWNED = "owned"
    UNCERTAIN = "uncertain"


def _observe_failure_route(route: str) -> None:
    # Failure retention imports storage contracts; keep it out of the router's
    # import graph because storage conversation hooks also import the observer.
    try:
        from friday.interaction_control_plane.failure_store import observe_failure_route

        observe_failure_route(route)
    except Exception as exc:  # noqa: BLE001 - observability cannot break routing
        LOGGER.warning("interaction failure route omitted (%s)", type(exc).__name__)


def _observe_failure_stage(stage: FailureStage) -> None:
    try:
        from friday.interaction_control_plane.failure_store import observe_failure_stage

        observe_failure_stage(stage)
    except Exception as exc:  # noqa: BLE001 - observability cannot break routing
        LOGGER.warning("interaction failure stage omitted (%s)", type(exc).__name__)


def _observe_legacy_capability_owner() -> None:
    """Bind a possible pre-commit failure to the runtime that owns the turn."""

    _observe_failure_route("legacy")
    _observe_failure_stage(FailureStage.CAPABILITY)


class ChatRuntime(Protocol):
    async def chat(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        enable_tools: bool = True,
        kg: Any = None,
        hybrid_searcher: Any = None,
        ingestion_result: dict[str, Any] | None = None,
        synthetic_document_notice: bool = False,
        replay_source_message_id: str | None = None,
        mode: str | None = None,
        answer_with_voice: bool = False,
        reply_to: str | None = None,
        quoted_attachment_reference: bool = False,
        reply_assistant_reference: bool = False,
        reply_assistant_message_id: str | None = None,
        turn_policy: TurnPolicyDecision | None = None,
        turn_deadline: float | None = None,
        _pending_durable_admission: PendingDurableTurnAdmission | None = None,
    ) -> dict[str, Any]: ...


class PendingDurableTurnOwner(Protocol):
    """Optional synchronous admission surface implemented by the legacy owner.

    The first identifier is the same person-scoped conversation owner used by
    ``AgentRuntime.chat``: ``actor.own_id`` in a shared archive, otherwise the
    requested user after the ordinary impersonation check.  Implementations
    must validate that the conversation belongs to that exact person and is in
    dialogue mode before consulting pending state.  No cross-person or
    cross-conversation lookup may influence the result.
    """

    def owns_pending_durable_turn(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> bool: ...


class TurnPlanner(Protocol):
    async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan: ...

    async def plan_attested(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None = None,
    ) -> TurnPlan: ...


@dataclass(frozen=True, slots=True)
class ReadOnlyRouteRequest:
    """Exact private request context available to a code-owned read handler.

    It intentionally has no kernel, ingestion result, generic tool toggle, KG
    writer or legacy runtime. A handler receives the model plan and the source
    descriptors, then must obtain evidence through its own read-only service.
    """

    user_id: str
    actor: ActorContext
    conversation_id: str | None
    attachments: tuple[ReadOnlyAttachmentReference, ...]
    synthetic_document_notice: bool
    replay_source_message_id: str | None
    conversation_mode: str | None
    reply_to: str | None
    quoted_attachment_reference: bool
    reply_assistant_reference: bool
    reply_assistant_message_id: str | None
    turn_deadline: float | None
    # Code-owned observability scope. Direct handler callers leave these at the
    # defaults; the real router supplies the start preceding its attested
    # planner call and the planner model-call lower bound.
    orchestration_started_at: float | None = field(default=None, repr=False, compare=False)
    planner_model_calls_lower_bound: int = field(default=0, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ReadOnlyAttachmentReference:
    """Bounded current-turn pointer; evidence bytes must be reauthorized later."""

    ordinal: int
    raw_object_id: str
    source_identity_sha256: str
    name: str
    media_type: str


@dataclass(frozen=True, slots=True)
class ReadOnlyRoutePreparation:
    """Code-owned, effect-free admission result created before route selection."""

    route: RouteClass
    plan_sha256: str
    evidence_identity_sha256: str
    private_payload: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("plan", self.plan_sha256),
            ("evidence", self.evidence_identity_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"prepared read-only route has an invalid {label} digest")


class ReadOnlyRouteHandler(Protocol):
    route: RouteClass
    effect: ToolEffect

    async def prepare(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
    ) -> ReadOnlyRoutePreparation | None: ...

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool: ...

    async def handle(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> ReadOnlyRouteResult: ...


@dataclass(frozen=True, slots=True)
class ReadOnlyRouteResult:
    """Closed result with no file, voice, tool or other effect carrier."""

    message: str
    conversation_id: str
    message_id: str
    evidence_identity_sha256: str
    citation_labels: tuple[str, ...]
    verified: bool
    outcome: CapabilityOutcome
    message_format: str = "markdown"
    interaction_mode: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("read-only route result requires a message")
        if len(self.message) > 100_000:
            raise ValueError("read-only route result message is too large")
        try:
            self.message.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("read-only route result message must be valid UTF-8") from exc
        if re.fullmatch(r"conv_[0-9a-f]{16}", self.conversation_id) is None:
            raise ValueError("read-only route result has an invalid conversation id")
        if re.fullmatch(r"msg_[0-9a-f]{16}", self.message_id) is None:
            raise ValueError("read-only route result has an invalid message id")
        if self.message_format not in {"markdown", "plain"}:
            raise ValueError("read-only route result has an invalid message format")
        if self.interaction_mode is not None and self.interaction_mode not in {
            "dialogue",
            "knowledge_work",
            "research",
        }:
            raise ValueError("read-only route result has an invalid interaction mode")
        if re.fullmatch(r"[0-9a-f]{64}", self.evidence_identity_sha256) is None:
            raise ValueError("read-only route result has an invalid evidence digest")
        if not isinstance(self.citation_labels, tuple):
            raise ValueError("read-only route result citation labels must be an immutable tuple")
        if len(set(self.citation_labels)) != len(self.citation_labels):
            raise ValueError("read-only route result citation labels must be unique")
        if len(self.citation_labels) > 32 or any(
            re.fullmatch(r"A[1-9][0-9]{0,2}", label) is None for label in self.citation_labels
        ):
            raise ValueError("read-only route result has invalid citation labels")
        if not isinstance(self.verified, bool):
            raise ValueError("read-only route result verified must be boolean")
        if type(self.outcome) is not CapabilityOutcome:
            raise ValueError("read-only route result requires CapabilityOutcome v1")

    def response(self, *, conversation_mode: str) -> dict[str, Any]:
        effective_mode = self.interaction_mode or conversation_mode
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "message": self.message,
            "message_format": self.message_format,
            "verified": self.verified,
            "verification_status": "verified" if self.verified else "unknown",
            "verification": {
                "status": "verified" if self.verified else "unknown",
                "score": 1.0 if self.verified else None,
                "issues": [],
            },
            "citations": [{"label": label} for label in self.citation_labels],
            "citation_check": {
                "status": "verified" if self.verified else "unknown",
                "checked": len(self.citation_labels),
            },
            "tools_used": [],
            "voice": None,
            "attachments": [],
            "attachment_context_available": True,
            "context": {"interaction_mode": effective_mode},
        }


@dataclass(frozen=True, slots=True)
class RouteObservation:
    mode: str
    status: str
    route: str
    selected_runtime: str
    reason_code: str
    confidence_milli: int
    elapsed_ms: int
    plan_sha256: str


def _allowed_routes(values: object) -> frozenset[RouteClass]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    accepted: set[RouteClass] = set()
    for value in values:
        try:
            accepted.add(RouteClass(str(value).strip().casefold()))
        except ValueError:
            continue
    # Effectful plans remain legacy-owned until the effect-plan phase is built.
    accepted.discard(RouteClass.EFFECT)
    accepted.discard(RouteClass.UNKNOWN)
    return frozenset(accepted)


def _plan_applicable(
    turn: TurnInput,
    plan: TurnPlan,
    attachment_references: tuple[ReadOnlyAttachmentReference, ...],
) -> bool:
    """Bind semantic plan claims to the normalized facts of this exact turn."""

    if (
        turn.message_truncated
        or turn.reply_quote_truncated
        or turn.attachments_truncated
        or plan.route is RouteClass.UNKNOWN
    ):
        return False
    if plan.route is RouteClass.FILE_READ:
        return bool(
            1 <= len(attachment_references) <= 12
            and len(attachment_references) == len(turn.attachments)
            and len({item.raw_object_id for item in attachment_references}) == len(attachment_references)
            and file_read_plan_supports_attachment_count(plan, len(attachment_references))
            and not turn.quoted_attachment_reference
            and not turn.reply_assistant_reference
            and not turn.synthetic_document_notice
        )
    if plan.route is RouteClass.ARCHIVE_READ:
        return bool(
            not turn.attachments
            and not attachment_references
            and archive_read_plan_supports_selection(plan)
            and not turn.quoted_attachment_reference
            and not turn.reply_assistant_reference
            and not turn.synthetic_document_notice
            and not turn.reply_quote
        )
    if plan.route is RouteClass.SMALL_TALK:
        return not (
            turn.attachments
            or turn.synthetic_document_notice
            or turn.quoted_attachment_reference
            or turn.reply_assistant_reference
        )
    if plan.route is RouteClass.ORDINARY_DIALOGUE:
        return not (
            turn.attachments
            or turn.synthetic_document_notice
            or turn.quoted_attachment_reference
            or turn.reply_assistant_reference
        )
    return True


def _current_attachment_references(
    turn: TurnInput,
    snapshots: tuple[Mapping[str, Any], ...],
    tokens: tuple[CurrentTurnFileReferenceToken | None, ...],
) -> tuple[ReadOnlyAttachmentReference, ...]:
    references: list[ReadOnlyAttachmentReference] = []
    for descriptor, raw, token in zip(turn.attachments, snapshots, tokens, strict=False):
        raw_id = raw.get("raw_object_id")
        if (
            token is None
            or not isinstance(raw_id, str)
            or re.fullmatch(r"raw_[0-9a-f]{16}", raw_id) is None
            or token.raw_id != raw_id
            or raw.get("persisted") is not True
            or raw.get("current_turn_only") is not True
        ):
            continue
        references.append(
            ReadOnlyAttachmentReference(
                ordinal=descriptor.ordinal,
                raw_object_id=raw_id,
                source_identity_sha256=token.source_identity_sha256,
                name=descriptor.name,
                media_type=descriptor.media_type,
            )
        )
    return tuple(references)


class OrchestrationRouter:
    """Own exactly one user-visible runtime per request.

    Shadow invokes only the planner, whose contract has no tool executor and no
    storage handle, before delegating the entire turn to legacy.  Canary/V12 can
    select only a registered read-only route.  Once a route handler starts, an
    exception is never retried through legacy: that would create two effect
    owners if the handler crossed an unseen boundary before failing.
    """

    def __init__(
        self,
        legacy: ChatRuntime,
        planner: TurnPlanner,
        *,
        mode: RouterMode | str,
        allowed_routes: object = (),
        canary_user_ids: object = (),
        route_handlers: Mapping[RouteClass, ReadOnlyRouteHandler] | None = None,
        route_timeout_sec: float = 60.0,
        planner_timeout_sec: float = 12.0,
        preparation_timeout_sec: float = 5.0,
    ) -> None:
        self._legacy = legacy
        self._planner = planner
        self.mode = RouterMode.fail_closed(mode)
        self.allowed_routes = _allowed_routes(allowed_routes)
        self._canary_user_ids = (
            frozenset(
                str(value).strip()
                for value in canary_user_ids
                if isinstance(value, str) and str(value).strip()
            )
            if isinstance(canary_user_ids, (list, tuple, set, frozenset))
            else frozenset()
        )
        self._route_handlers = dict(route_handlers or {})
        self._route_timeout_sec = max(1.0, float(route_timeout_sec))
        self._planner_timeout_sec = max(1.0, float(planner_timeout_sec))
        self._preparation_timeout_sec = max(0.01, min(5.0, float(preparation_timeout_sec)))
        self._observations: deque[RouteObservation] = deque(maxlen=128)
        self._shadow_tasks: set[asyncio.Task[None]] = set()

    def __getattr__(self, name: str) -> Any:
        # Feedback and any non-chat compatibility surface retain one owner.
        return getattr(self._legacy, name)

    @property
    def observations(self) -> tuple[RouteObservation, ...]:
        return tuple(self._observations)

    def _observe(
        self,
        *,
        started: float,
        status: str,
        plan: TurnPlan | None,
        selected_runtime: str,
    ) -> None:
        observation = RouteObservation(
            mode=self.mode.value,
            status=status,
            route=plan.route.value if plan is not None else "unknown",
            selected_runtime=selected_runtime,
            # Model labels are hashed with the plan but never copied into
            # diagnostics: they may echo a private filename or user phrase.
            reason_code=status,
            confidence_milli=round(plan.confidence * 1000) if plan is not None else 0,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            plan_sha256=plan.canonical_sha256() if plan is not None else "",
        )
        self._observations.append(observation)
        LOGGER.info(
            "v12_route mode=%s status=%s route=%s selected=%s confidence_milli=%d elapsed_ms=%d",
            observation.mode,
            observation.status,
            observation.route,
            observation.selected_runtime,
            observation.confidence_milli,
            observation.elapsed_ms,
        )

    async def _try_plan(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None,
        attested: bool,
    ) -> TurnPlan | None:
        try:
            timeout = self._planner_timeout_sec
            if turn_deadline is not None:
                timeout = min(timeout, turn_deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("turn planning deadline has expired")
            call = self._planner.plan_attested if attested else self._planner.plan
            return await asyncio.wait_for(
                call(turn, turn_deadline=turn_deadline), timeout=max(0.001, timeout)
            )
        except Exception as exc:
            # No prompts, filenames, exception bodies or user identifiers enter logs.
            LOGGER.warning("v12 planner rejected; retaining legacy (%s)", type(exc).__name__)
            return None

    def _actor_canary_eligible(self, actor: ActorContext) -> bool:
        return bool(
            self.mode is RouterMode.V12
            or actor.own_id in self._canary_user_ids
            or (actor.is_owner and "owner" in self._canary_user_ids)
        )

    def owns_pending_durable_turn(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> bool:
        """Expose exact synchronous ownership to the HTTP pre-ingestion seam."""

        admission = self.pending_durable_turn_admission(
            user_id,
            message,
            actor=actor,
            conversation_id=conversation_id,
        )
        if admission is None:
            raise RuntimeError("pending durable ownership is uncertain")
        return admission is not False

    def pending_durable_turn_admission(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> PendingDurableTurnAdmission | bool | None:
        """Return a bound owner decision when the legacy runtime can provide one."""

        if not conversation_id:
            return False
        if not actor.shared_tenant and actor.user_id != user_id and not actor.is_owner:
            return None
        person_id = actor.own_id if actor.shared_tenant else user_id

        try:
            owner_check = getattr(self._legacy, "pending_durable_turn_admission", None)
            if not callable(owner_check):
                owner_check = getattr(self._legacy, "owns_pending_durable_turn", None)
            if not callable(owner_check):
                return False
            result = owner_check(
                person_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
            )
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
                LOGGER.warning(
                    "pending durable turn admission returned an awaitable; retaining legacy"
                )
                return None
            if isinstance(result, PendingDurableTurnAdmission):
                if not result.matches_scope(
                    person_id=person_id,
                    conversation_id=conversation_id,
                ):
                    LOGGER.warning("pending durable turn admission returned a foreign binding")
                    return None
                return result
            if result is True:
                return PendingDurableTurnAdmission.owned(
                    person_id=person_id,
                    conversation_id=conversation_id,
                )
            if result is False:
                return False
            LOGGER.warning(
                "pending durable turn admission returned an invalid result; retaining legacy (%s)",
                type(result).__name__,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - uncertain admission must retain legacy
            LOGGER.warning(
                "pending durable turn admission rejected; retaining legacy (%s)",
                type(exc).__name__,
            )
            return None

    def _legacy_pending_durable_turn_admission(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> _PendingDurableAdmission:
        """Ask only the proven owner, failing uncertainty closed to legacy."""

        admission = self.pending_durable_turn_admission(
            user_id,
            message,
            actor=actor,
            conversation_id=conversation_id,
        )
        if admission is False:
            return _PendingDurableAdmission.ORDINARY
        if admission is None:
            return _PendingDurableAdmission.UNCERTAIN
        if not isinstance(admission, PendingDurableTurnAdmission):
            return _PendingDurableAdmission.UNCERTAIN
        return (
            _PendingDurableAdmission.OWNED
            if admission.state is PendingDurableAdmissionState.OWNED
            else _PendingDurableAdmission.UNCERTAIN
        )

    async def _complete_shadow_plan(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None,
        started: float,
    ) -> None:
        plan = await self._try_plan(turn, turn_deadline=turn_deadline, attested=False)
        self._observe(
            started=started,
            status="planned" if plan is not None else "planner_rejected",
            plan=plan,
            selected_runtime="legacy",
        )

    def _schedule_shadow_plan(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None,
        started: float,
    ) -> None:
        if turn_deadline is not None and turn_deadline <= time.monotonic():
            self._observe(
                started=started,
                status="shadow_deadline_spent",
                plan=None,
                selected_runtime="legacy",
            )
            return
        if len(self._shadow_tasks) >= _MAX_PENDING_SHADOW_PLANS:
            self._observe(
                started=started,
                status="shadow_dropped_backpressure",
                plan=None,
                selected_runtime="legacy",
            )
            return
        task = asyncio.create_task(
            self._complete_shadow_plan(turn, turn_deadline=turn_deadline, started=started),
            name="friday-v12-shadow-plan",
        )
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_done)

    def _shadow_done(self, task: asyncio.Task[None]) -> None:
        self._shadow_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            LOGGER.error("v12 shadow task failed (%s)", type(exception).__name__)

    async def drain_shadow(self) -> None:
        """Test/diagnostic barrier for bounded, effect-free shadow tasks."""

        pending = tuple(self._shadow_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Cancel and drain every in-memory shadow plan before service teardown."""

        pending = tuple(self._shadow_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._shadow_tasks.clear()

    async def chat(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        enable_tools: bool = True,
        kg: Any = None,
        hybrid_searcher: Any = None,
        ingestion_result: dict[str, Any] | None = None,
        synthetic_document_notice: bool = False,
        replay_source_message_id: str | None = None,
        mode: str | None = None,
        answer_with_voice: bool = False,
        reply_to: str | None = None,
        quoted_attachment_reference: bool = False,
        reply_assistant_reference: bool = False,
        reply_assistant_message_id: str | None = None,
        turn_policy: TurnPolicyDecision | None = None,
        turn_deadline: float | None = None,
        _pending_durable_admission: PendingDurableTurnAdmission | None = None,
    ) -> dict[str, Any]:
        legacy_kwargs = {
            "actor": actor,
            "conversation_id": conversation_id,
            "attachments": attachments,
            "enable_tools": enable_tools,
            "kg": kg,
            "hybrid_searcher": hybrid_searcher,
            "ingestion_result": ingestion_result,
            "synthetic_document_notice": synthetic_document_notice,
            "replay_source_message_id": replay_source_message_id,
            "mode": mode,
            "answer_with_voice": answer_with_voice,
            "reply_to": reply_to,
            "quoted_attachment_reference": quoted_attachment_reference,
            "reply_assistant_reference": reply_assistant_reference,
            "reply_assistant_message_id": reply_assistant_message_id,
            "turn_deadline": turn_deadline,
        }
        if _pending_durable_admission is not None:
            legacy_kwargs["_pending_durable_admission"] = _pending_durable_admission
        if turn_policy is not None:
            legacy_kwargs["turn_policy"] = turn_policy
        if turn_policy is not None and turn_policy.handled:
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)
        if self.mode is RouterMode.LEGACY:
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        started = time.monotonic()
        # A durable code-owned question remains legacy-owned even while V12 is
        # sampled.  Admission is deliberately synchronous and scalar-only: no
        # file carrier, reply/replay context, voice request or policy decision
        # crosses this optional compatibility boundary.
        plain_durable_surface = bool(
            not attachments
            and enable_tools is True
            and ingestion_result is None
            and synthetic_document_notice is False
            and replay_source_message_id is None
            and answer_with_voice is False
            and reply_to is None
            and quoted_attachment_reference is False
            and reply_assistant_reference is False
            and reply_assistant_message_id is None
            and turn_policy is None
            and mode is None
        )
        durable_receipt: PendingDurableTurnAdmission | bool | None
        if _pending_durable_admission is not None:
            person_id = actor.own_id if actor.shared_tenant else user_id
            durable_receipt = (
                _pending_durable_admission
                if _pending_durable_admission.matches_scope(
                    person_id=person_id,
                    conversation_id=str(conversation_id or ""),
                )
                else None
            )
        else:
            durable_receipt = (
                self.pending_durable_turn_admission(
                    user_id,
                    message,
                    actor=actor,
                    conversation_id=conversation_id,
                )
                if plain_durable_surface
                else False
            )
        if durable_receipt is False:
            durable_admission = _PendingDurableAdmission.ORDINARY
        elif (
            not isinstance(durable_receipt, PendingDurableTurnAdmission)
            or durable_receipt.state is PendingDurableAdmissionState.UNCERTAIN
        ):
            durable_admission = _PendingDurableAdmission.UNCERTAIN
        else:
            durable_admission = _PendingDurableAdmission.OWNED
        if isinstance(durable_receipt, PendingDurableTurnAdmission):
            legacy_kwargs["_pending_durable_admission"] = durable_receipt
        if durable_admission is not _PendingDurableAdmission.ORDINARY:
            self._observe(
                started=started,
                status=(
                    "durable_turn_owned"
                    if durable_admission is _PendingDurableAdmission.OWNED
                    else "durable_turn_ownership_uncertain"
                ),
                plan=None,
                selected_runtime="legacy",
            )
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        if self.mode is RouterMode.CANARY and not self._actor_canary_eligible(actor):
            self._observe(
                started=started,
                status="actor_not_in_canary",
                plan=None,
                selected_runtime="legacy",
            )
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)
        if self.mode in {RouterMode.CANARY, RouterMode.V12} and answer_with_voice:
            self._observe(
                started=started,
                status="voice_not_in_canary",
                plan=None,
                selected_runtime="legacy",
            )
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        # Capture the scalar attachment state before the first await. The
        # planner and evidence admission must refer to the same Raw Objects.
        attachment_carriers = tuple(item for item in (attachments or []) if isinstance(item, Mapping))
        attachment_tokens = tuple(current_turn_file_reference_of(item) for item in attachment_carriers)
        attachment_snapshots = tuple(dict(item) for item in attachment_carriers)
        turn = TurnInput.from_chat(
            message=message,
            actor=actor,
            conversation_id=conversation_id,
            attachments=attachment_snapshots,
            enable_tools=enable_tools,
            synthetic_document_notice=synthetic_document_notice,
            mode=mode,
            reply_to=reply_to,
            quoted_attachment_reference=quoted_attachment_reference,
            reply_assistant_reference=reply_assistant_reference,
        )
        attachment_references = _current_attachment_references(
            turn,
            attachment_snapshots,
            attachment_tokens,
        )
        if self.mode is RouterMode.SHADOW:
            _observe_legacy_capability_owner()
            result = await self._legacy.chat(user_id, message, **legacy_kwargs)
            self._schedule_shadow_plan(turn, turn_deadline=turn_deadline, started=started)
            return result

        legacy_reserve = max(30.0, self._planner_timeout_sec * 2.0)
        preparation_budget = self._preparation_timeout_sec
        # A failed/malformed canary plan must leave enough of the original
        # turn for a meaningful legacy fallback. Near the boundary we do not
        # sample at all; planning is optional, the proven runtime is not.
        if (
            turn_deadline is not None
            and turn_deadline - time.monotonic()
            <= self._planner_timeout_sec + preparation_budget + legacy_reserve
        ):
            self._observe(
                started=started,
                status="legacy_budget_reserved",
                plan=None,
                selected_runtime="legacy",
            )
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        _observe_failure_stage(FailureStage.PLANNING)
        plan = await self._try_plan(turn, turn_deadline=turn_deadline, attested=True)
        handler = self._route_handlers.get(plan.route) if plan is not None else None
        eligible = bool(
            plan is not None
            and plan.read_only
            and _plan_applicable(turn, plan, attachment_references)
            # Until the execution-kernel ToolCatalog is wired into V12, a
            # model-supplied effect label is never authority. The first canary
            # handlers therefore accept evidence-only plans with zero tools.
            and not plan.tool_intents
            and plan.route in self.allowed_routes
            and handler is not None
            and handler.route is plan.route
            and handler.effect is ToolEffect.READ
        )
        if not eligible:
            self._observe(
                started=started,
                status="legacy_fallback",
                plan=plan,
                selected_runtime="legacy",
            )
            if turn_deadline is not None and turn_deadline - time.monotonic() < legacy_reserve:
                raise TimeoutError("legacy fallback reserve was exhausted during V12 planning")
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        assert handler is not None and plan is not None  # narrowed by the predicate above
        _observe_failure_route(plan.route.value)
        _observe_failure_stage(FailureStage.CAPABILITY)
        handler_deadline = turn_deadline or (time.monotonic() + self._route_timeout_sec)
        remaining = handler_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("V12 route deadline expired before handler admission")
        request = ReadOnlyRouteRequest(
            user_id=user_id,
            actor=actor,
            conversation_id=conversation_id,
            attachments=attachment_references,
            synthetic_document_notice=synthetic_document_notice,
            replay_source_message_id=replay_source_message_id,
            conversation_mode=turn.conversation_mode,
            reply_to=turn.reply_quote,
            quoted_attachment_reference=quoted_attachment_reference,
            reply_assistant_reference=reply_assistant_reference,
            reply_assistant_message_id=reply_assistant_message_id,
            turn_deadline=handler_deadline,
            orchestration_started_at=started,
            planner_model_calls_lower_bound=1,
        )
        preparation_deadline = min(handler_deadline, time.monotonic() + preparation_budget)
        try:
            preparation = await asyncio.wait_for(
                handler.prepare(request, turn, plan),
                timeout=max(0.001, preparation_deadline - time.monotonic()),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._observe(
                started=started,
                status="prepare_failed",
                plan=plan,
                selected_runtime="legacy",
            )
            if turn_deadline is not None and turn_deadline - time.monotonic() < legacy_reserve:
                raise TimeoutError("legacy fallback reserve was exhausted during V12 admission") from exc
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)
        if (
            not isinstance(preparation, ReadOnlyRoutePreparation)
            or preparation.route is not plan.route
            or preparation.plan_sha256 != plan.canonical_sha256()
        ):
            self._observe(
                started=started,
                status="prepare_rejected",
                plan=plan,
                selected_runtime="legacy",
            )
            if turn_deadline is not None and turn_deadline - time.monotonic() < legacy_reserve:
                raise TimeoutError("legacy fallback reserve was exhausted during V12 admission")
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        try:
            current = await asyncio.wait_for(
                handler.preparation_is_current(request, turn, plan, preparation),
                timeout=max(0.001, preparation_deadline - time.monotonic()),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._observe(
                started=started,
                status="prepare_authority_failed",
                plan=plan,
                selected_runtime="legacy",
            )
            if turn_deadline is not None and turn_deadline - time.monotonic() < legacy_reserve:
                raise TimeoutError(
                    "legacy fallback reserve was exhausted during V12 authority check"
                ) from exc
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)
        if current is not True:
            self._observe(
                started=started,
                status="prepare_authority_rejected",
                plan=plan,
                selected_runtime="legacy",
            )
            if turn_deadline is not None and turn_deadline - time.monotonic() < legacy_reserve:
                raise TimeoutError("legacy fallback reserve was exhausted during V12 authority check")
            _observe_legacy_capability_owner()
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        self._observe(started=started, status="selected", plan=plan, selected_runtime="v12")
        # Never catch-and-retry after effect-free preparation succeeds. Exactly
        # one publication owner remains stronger than an optimistic fallback.
        remaining = handler_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("V12 route deadline expired before handler execution")
        try:
            _observe_failure_stage(FailureStage.CAPABILITY)
            handler_result = await asyncio.wait_for(
                handler.handle(request, turn, plan, preparation),
                timeout=max(0.001, remaining),
            )
        except TimeoutError:
            self._observe(
                started=started,
                status="handler_timeout",
                plan=plan,
                selected_runtime="v12",
            )
            raise
        except BaseException:
            self._observe(
                started=started,
                status="handler_failed",
                plan=plan,
                selected_runtime="v12",
            )
            raise
        try:
            if not isinstance(handler_result, ReadOnlyRouteResult):
                raise TypeError("read-only V12 handler returned a non-result")
            if handler_result.evidence_identity_sha256 != preparation.evidence_identity_sha256:
                raise ValueError("read-only V12 result evidence is not the prepared evidence")
            require_complete_read_only_publication(
                handler_result.outcome,
                expected_route=plan.route,
                expected_plan_sha256=plan.canonical_sha256(),
                expected_evidence_identity_sha256=preparation.evidence_identity_sha256,
                expected_citation_labels=handler_result.citation_labels,
                answer=handler_result.message,
                authority_rechecked=True,
                verification_passed=handler_result.verified,
            )
        except (TypeError, ValueError):
            self._observe(
                started=started,
                status="handler_invalid_result",
                plan=plan,
                selected_runtime="v12",
            )
            raise TypeError("read-only V12 handler returned an invalid result") from None
        self._observe(started=started, status="completed", plan=plan, selected_runtime="v12")
        return handler_result.response(conversation_mode=turn.conversation_mode)


def build_orchestrated_agent(
    settings: Any,
    legacy: ChatRuntime,
    model: PlannerModel,
    *,
    route_handlers: Mapping[RouteClass, ReadOnlyRouteHandler] | None = None,
    attested_runtime: AttestedPlannerRuntime | None = None,
) -> ChatRuntime:
    """Return legacy byte-for-byte by default; wrap only after explicit opt-in."""

    mode = RouterMode.fail_closed(getattr(settings, "router_mode", "legacy"))
    if mode is RouterMode.LEGACY:
        return legacy
    if mode in {RouterMode.CANARY, RouterMode.V12} and attested_runtime is None:
        return legacy
    planner = V12Planner(
        model,
        timeout_sec=getattr(settings, "router_plan_timeout_sec", 12.0),
        attested_runtime=attested_runtime,
    )
    return OrchestrationRouter(
        legacy,
        planner,
        mode=mode,
        allowed_routes=getattr(settings, "router_canary_routes", ()),
        canary_user_ids=getattr(settings, "router_canary_user_ids", ()),
        route_handlers=route_handlers if mode in {RouterMode.CANARY, RouterMode.V12} else {},
        route_timeout_sec=getattr(settings, "agent_turn_budget_sec", 60.0),
        planner_timeout_sec=getattr(settings, "router_plan_timeout_sec", 12.0),
    )
