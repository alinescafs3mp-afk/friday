"""Reversible dispatch between the frozen legacy runtime and V12 routes."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from friday.file_evidence import CurrentTurnFileReferenceToken, current_turn_file_reference_of
from friday.orchestration.contracts import (
    EvidenceKind,
    RouteClass,
    RouterMode,
    ToolEffect,
    TurnInput,
    TurnPlan,
)
from friday.orchestration.planner import PlannerModel, V12Planner
from friday.permissions import ActorContext

LOGGER = logging.getLogger(__name__)
_MAX_PENDING_SHADOW_PLANS = 4
_EVIDENCE_CITATION_RE = re.compile(r"\[(A[1-9][0-9]{0,2})\]")


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
        turn_deadline: float | None = None,
    ) -> dict[str, Any]: ...


class TurnPlanner(Protocol):
    async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan: ...


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
        source_requests = plan.evidence_requests
        return bool(
            1 <= len(attachment_references) <= 12
            and len(attachment_references) == len(turn.attachments)
            and len({item.raw_object_id for item in attachment_references}) == len(attachment_references)
            and len(source_requests) == 1
            and source_requests[0].kind is EvidenceKind.ATTACHED_FILES
            and source_requests[0].required
            and source_requests[0].max_items >= len(attachment_references)
            and not turn.quoted_attachment_reference
            and not turn.reply_assistant_reference
            and not turn.synthetic_document_notice
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

    async def _try_plan(self, turn: TurnInput, *, turn_deadline: float | None) -> TurnPlan | None:
        try:
            timeout = self._planner_timeout_sec
            if turn_deadline is not None:
                timeout = min(timeout, turn_deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("turn planning deadline has expired")
            return await asyncio.wait_for(
                self._planner.plan(turn, turn_deadline=turn_deadline),
                timeout=max(0.001, timeout),
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

    async def _complete_shadow_plan(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None,
        started: float,
    ) -> None:
        plan = await self._try_plan(turn, turn_deadline=turn_deadline)
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
        turn_deadline: float | None = None,
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
        if self.mode is RouterMode.LEGACY:
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        started = time.monotonic()
        if self.mode is RouterMode.CANARY and not self._actor_canary_eligible(actor):
            self._observe(
                started=started,
                status="actor_not_in_canary",
                plan=None,
                selected_runtime="legacy",
            )
            return await self._legacy.chat(user_id, message, **legacy_kwargs)
        if self.mode in {RouterMode.CANARY, RouterMode.V12} and answer_with_voice:
            self._observe(
                started=started,
                status="voice_not_in_canary",
                plan=None,
                selected_runtime="legacy",
            )
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
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        plan = await self._try_plan(turn, turn_deadline=turn_deadline)
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
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        assert handler is not None and plan is not None  # narrowed by the predicate above
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
        )
        try:
            preparation = await asyncio.wait_for(
                handler.prepare(request, turn, plan),
                timeout=max(0.001, min(preparation_budget, remaining)),
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
            return await self._legacy.chat(user_id, message, **legacy_kwargs)

        self._observe(started=started, status="selected", plan=plan, selected_runtime="v12")
        # Never catch-and-retry after effect-free preparation succeeds. Exactly
        # one publication owner remains stronger than an optimistic fallback.
        remaining = handler_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("V12 route deadline expired before handler execution")
        try:
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
        source_route = plan.route in {
            RouteClass.FILE_READ,
            RouteClass.ARCHIVE_READ,
            RouteClass.WEB_READ,
        }
        if (
            not isinstance(handler_result, ReadOnlyRouteResult)
            or handler_result.evidence_identity_sha256 != preparation.evidence_identity_sha256
            or (
                source_route
                and (
                    not handler_result.verified
                    or not handler_result.citation_labels
                    or set(_EVIDENCE_CITATION_RE.findall(handler_result.message))
                    != set(handler_result.citation_labels)
                )
            )
        ):
            self._observe(
                started=started,
                status="handler_invalid_result",
                plan=plan,
                selected_runtime="v12",
            )
            raise TypeError("read-only V12 handler returned an invalid result")
        self._observe(started=started, status="completed", plan=plan, selected_runtime="v12")
        return handler_result.response(conversation_mode=turn.conversation_mode)


def build_orchestrated_agent(
    settings: Any,
    legacy: ChatRuntime,
    model: PlannerModel,
    *,
    route_handlers: Mapping[RouteClass, ReadOnlyRouteHandler] | None = None,
) -> ChatRuntime:
    """Return legacy byte-for-byte by default; wrap only after explicit opt-in."""

    mode = RouterMode.fail_closed(getattr(settings, "router_mode", "legacy"))
    if mode is RouterMode.LEGACY:
        return legacy
    planner = V12Planner(model, timeout_sec=getattr(settings, "router_plan_timeout_sec", 12.0))
    return OrchestrationRouter(
        legacy,
        planner,
        mode=mode,
        allowed_routes=getattr(settings, "router_canary_routes", ()),
        canary_user_ids=getattr(settings, "router_canary_user_ids", ()),
        route_handlers=route_handlers,
        route_timeout_sec=getattr(settings, "agent_turn_budget_sec", 60.0),
        planner_timeout_sec=getattr(settings, "router_plan_timeout_sec", 12.0),
    )
