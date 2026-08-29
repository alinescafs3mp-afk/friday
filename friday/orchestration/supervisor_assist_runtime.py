"""Production wrapper for the one promoted semantic-supervisor journey.

The wrapper owns no capability and no durable state.  It recognizes the exact
prospective surface, delegates a promoted turn to the bounded controller, and
otherwise invokes the pre-existing primary runtime exactly once.  A durable
graph already present in the conversation always stays ahead of new planning.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from friday.orchestration.supervisor_assist_controller import (
    AssistPendingGraphDisposition,
    SupervisorAssistOutcome,
    SupervisorAssistResult,
)
from friday.orchestration.supervisor_assist_graph_adapter import AssistConversationScope
from friday.orchestration.supervisor_assist_ingress import (
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.orchestration.supervisor_assist_surface import (
    CurrentFileWebAssistSurface,
    prepare_current_file_web_assist_surface,
)
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    PendingOwnerKind,
)
from friday.orchestration.turn_context_call_scope import (
    UNSPECIFIED_CHAT_ADJUNCT,
    require_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnPolicyDecision


class SupervisorAssistRuntimeError(RuntimeError):
    """The promoted wrapper could not prove one safe response owner."""


class _PrimaryChatRuntime(Protocol):
    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]: ...


class _AssistController(Protocol):
    def semantic_supervisor_status(self) -> dict[str, object]: ...

    def start_restart_recovery(self, *, batch_limit: int = 100) -> None: ...

    async def wait_restart_recovery(self) -> None: ...

    def pending_durable_turn_admission(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int = 0,
    ) -> PendingDurableTurnAdmission | bool | None: ...

    def classify_supervisor_assist_pending(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        ingress_binding: SupervisorAssistIngressBindingV1 | None,
        current_attachment_count: int = 0,
    ) -> SupervisorAssistPendingDecision | bool: ...

    async def execute(
        self,
        surface: CurrentFileWebAssistSurface | None,
        *,
        legacy_primary: Callable[[], Awaitable[Mapping[str, Any]]],
        absolute_deadline: float,
    ) -> SupervisorAssistResult: ...

    async def cancel_active(
        self,
        scope: AssistConversationScope,
        *,
        decision: SupervisorAssistPendingDecision,
        user_message: str,
        absolute_deadline: float,
    ) -> SupervisorAssistResult | None: ...

    async def reconcile_pending_before_legacy(
        self,
        scope: AssistConversationScope,
        decision: SupervisorAssistPendingDecision,
        *,
        absolute_deadline: float,
    ) -> AssistPendingGraphDisposition: ...

    async def close(self) -> None: ...


class AssistOrdinaryPostCommitObserver(Protocol):
    """Observe only an already-committed primary response; never infer facts."""

    def __call__(
        self,
        response: Mapping[str, Any],
        actor: ActorContext,
    ) -> Awaitable[bool | None] | bool | None: ...


def _future_deadline(settings: object, inherited: object) -> float:
    now = time.monotonic()
    configured = getattr(settings, "semantic_supervisor_timeout_sec", None)
    if (
        isinstance(configured, bool)
        or not isinstance(configured, int | float)
        or not math.isfinite(float(configured))
        or float(configured) <= 0
    ):
        configured = 0.001
    deadline = now + float(configured)
    if (
        not isinstance(inherited, bool)
        and isinstance(inherited, int | float)
        and math.isfinite(float(inherited))
    ):
        deadline = min(deadline, float(inherited))
    # The controller treats an exhausted deadline as an ordinary pre-ownership
    # fallback.  Keep the value finite and let that single owner make the call.
    return max(now, deadline)


def _validated_pending(
    value: object,
    *,
    person_id: str,
    conversation_id: str,
) -> PendingDurableTurnAdmission | bool | None:
    if value is False:
        return False
    if value is None:
        return None
    if isinstance(value, PendingDurableTurnAdmission) and value.matches_scope(
        person_id=person_id,
        conversation_id=conversation_id,
    ):
        return value
    return None


class SemanticSupervisorAssistRuntime:
    """Install assist/canary without replacing the primary compatibility API."""

    def __init__(
        self,
        *,
        settings: object,
        primary: _PrimaryChatRuntime,
        controller: _AssistController,
        conversation_is_dialogue: Callable[[str, str], bool],
        ordinary_observer: AssistOrdinaryPostCommitObserver | None = None,
    ) -> None:
        for label, dependency in (
            ("primary chat", getattr(primary, "chat", None)),
            ("controller execute", getattr(controller, "execute", None)),
            (
                "controller pending admission",
                getattr(controller, "pending_durable_turn_admission", None),
            ),
            (
                "controller assist ingress classifier",
                getattr(controller, "classify_supervisor_assist_pending", None),
            ),
            ("controller cancellation", getattr(controller, "cancel_active", None)),
            (
                "controller pending reconciliation",
                getattr(controller, "reconcile_pending_before_legacy", None),
            ),
            ("controller close", getattr(controller, "close", None)),
            ("conversation mode reader", conversation_is_dialogue),
        ):
            if not callable(dependency):
                raise TypeError(f"{label} is unavailable")
        if ordinary_observer is not None and not callable(ordinary_observer):
            raise TypeError("ordinary observer is unavailable")
        requested = SupervisorMode.fail_closed(
            getattr(settings, "semantic_supervisor_mode", SupervisorMode.OFF.value)
        )
        if requested not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
            raise ValueError("assist runtime requires an exact promoted mode")
        self._settings = settings
        self._primary = primary
        self._controller = controller
        self._conversation_is_dialogue = conversation_is_dialogue
        self._ordinary_observer = ordinary_observer
        self._closed = False
        self._ordinary_event_success_total = 0
        self._ordinary_event_failure_total = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    def semantic_supervisor_status(self) -> dict[str, object]:
        method = getattr(self._controller, "semantic_supervisor_status", None)
        try:
            value = method() if callable(method) else {}
        except Exception:
            value = {}
        status = dict(value) if isinstance(value, Mapping) else {}
        status["ordinary_event_success_total"] = self._ordinary_event_success_total
        status["ordinary_event_failure_total"] = self._ordinary_event_failure_total
        if self._closed:
            status["effective_mode"] = SupervisorMode.OFF.value
            status["promotion_admitted"] = False
            status["closed"] = True
        return status

    def _controller_pending(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int,
    ) -> PendingDurableTurnAdmission | bool | None:
        if not conversation_id:
            return False
        person_id = actor.own_id if actor.shared_tenant else user_id
        try:
            value = self._controller.pending_durable_turn_admission(
                person_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                current_attachment_count=current_attachment_count,
            )
        except Exception:
            return None
        return _validated_pending(
            value,
            person_id=person_id,
            conversation_id=conversation_id,
        )

    def pending_durable_turn_admission(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int = 0,
    ) -> PendingDurableTurnAdmission | bool:
        """Put the promoted graph ahead of ingestion and every new planner call."""

        if type(current_attachment_count) is not int or current_attachment_count not in {0, 1}:
            return False
        if not conversation_id:
            return False
        person_id = actor.own_id if actor.shared_tenant else user_id
        controller = self._controller_pending(
            user_id,
            message,
            actor=actor,
            conversation_id=conversation_id,
            current_attachment_count=current_attachment_count,
        )
        if isinstance(controller, PendingDurableTurnAdmission):
            return controller
        if controller is None:
            return PendingDurableTurnAdmission.uncertain(
                person_id=person_id,
                conversation_id=conversation_id,
            )

        method = getattr(self._primary, "pending_durable_turn_admission", None)
        if not callable(method):
            method = getattr(self._primary, "owns_pending_durable_turn", None)
        if not callable(method):
            return False
        try:
            value = method(
                person_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                current_attachment_count=current_attachment_count,
            )
        except Exception:
            return PendingDurableTurnAdmission.uncertain(
                person_id=person_id,
                conversation_id=conversation_id,
            )
        primary = _validated_pending(
            value,
            person_id=person_id,
            conversation_id=conversation_id,
        )
        if isinstance(primary, PendingDurableTurnAdmission):
            return primary
        if primary is None:
            return PendingDurableTurnAdmission.uncertain(
                person_id=person_id,
                conversation_id=conversation_id,
            )
        return False

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
        """Expose the assist-only root/new/cancel relation without legacy collapse."""

        if (
            not conversation_id
            or type(current_attachment_count) is not int
            or current_attachment_count not in {0, 1}
        ):
            return False
        person_id = actor.own_id if actor.shared_tenant else user_id
        try:
            value = self._controller.classify_supervisor_assist_pending(
                person_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                ingress_binding=ingress_binding,
                current_attachment_count=current_attachment_count,
            )
        except Exception:
            value = None
        if value is False:
            return False
        current_sha256 = (
            None
            if type(ingress_binding) is not SupervisorAssistIngressBindingV1
            else ingress_binding.canonical_sha256()
        )
        if (
            type(value) is SupervisorAssistPendingDecision
            and value.person_id == person_id
            and value.conversation_id == conversation_id
            and value.current_request_binding_sha256 == current_sha256
            and value.matches_message(message)
        ):
            return value
        return SupervisorAssistPendingDecision.uncertain(
            person_id=person_id,
            conversation_id=conversation_id,
            current=ingress_binding,
        )

    def owns_pending_durable_turn(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int = 0,
    ) -> bool:
        return (
            self.pending_durable_turn_admission(
                user_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                current_attachment_count=current_attachment_count,
            )
            is not False
        )

    async def _observe_ordinary(self, response: Mapping[str, Any], actor: ActorContext) -> None:
        if self._ordinary_observer is None:
            return
        try:
            emitted = self._ordinary_observer(response, actor)
            if inspect.isawaitable(emitted):
                emitted = await emitted
        except BaseException:
            self._ordinary_event_failure_total += 1
        else:
            if emitted is not False:
                self._ordinary_event_success_total += 1

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
        _semantic_supervisor_ingress_binding: SupervisorAssistIngressBindingV1 | None = None,
        _semantic_supervisor_pending_decision: SupervisorAssistPendingDecision | None = None,
        _semantic_supervisor_explicit_mode_requested: bool = False,
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
        if self._closed:
            raise SupervisorAssistRuntimeError("assist runtime is closed")
        effective_turn_deadline = (
            authenticated_scope.deadline_monotonic if authenticated_scope is not None else turn_deadline
        )
        effective_ingestion_result = (
            authenticated_scope.ingestion_result
            if authenticated_scope is not None
            else (None if ingestion_result is UNSPECIFIED_CHAT_ADJUNCT else ingestion_result)
        )
        legacy_kwargs: dict[str, Any] = {
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
            "turn_policy": turn_policy,
            "turn_deadline": effective_turn_deadline,
            "_pending_durable_admission": _pending_durable_admission,
        }
        if authenticated_scope is not None:
            legacy_kwargs.update(authenticated_scope.exact_service_kwargs())
        else:
            legacy_kwargs.update(
                kg=None if kg is UNSPECIFIED_CHAT_ADJUNCT else kg,
                hybrid_searcher=(None if hybrid_searcher is UNSPECIFIED_CHAT_ADJUNCT else hybrid_searcher),
                ingestion_result=effective_ingestion_result,
            )
        if telegram_update_id is not None:
            legacy_kwargs["telegram_update_id"] = telegram_update_id
        if authenticated_context is not None:
            legacy_kwargs["_authenticated_turn_context"] = authenticated_context
        legacy_calls = 0

        async def legacy_primary() -> dict[str, Any]:
            nonlocal legacy_calls
            legacy_calls += 1
            if legacy_calls != 1:
                raise SupervisorAssistRuntimeError("legacy primary was requested more than once")
            if authenticated_context is not None:
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
                legacy_kwargs.update(revalidated_scope.exact_service_kwargs())
            response = await self._primary.chat(user_id, message, **legacy_kwargs)
            if type(response) is not dict:
                raise SupervisorAssistRuntimeError("legacy primary returned an invalid response")
            return response

        attachment_count = len(attachments) if isinstance(attachments, list) else 0
        deadline = _future_deadline(self._settings, effective_turn_deadline)
        normalized = message.strip().casefold() if isinstance(message, str) else ""
        authenticated_pending = (
            authenticated_context.pending_work_admission if authenticated_context is not None else None
        )
        if authenticated_pending is not None and (
            authenticated_pending.owner_kind is PendingOwnerKind.UNCERTAIN_FAIL_CLOSED
        ):
            raise SupervisorAssistRuntimeError("authenticated pending-work ownership is uncertain")
        if authenticated_context is not None and authenticated_pending is None:
            if _semantic_supervisor_pending_decision is not None:
                raise SupervisorAssistRuntimeError("authenticated ordinary turn cannot acquire pending work")
            response = await legacy_primary()
            await self._observe_ordinary(response, actor)
            return response
        if (
            authenticated_pending is not None
            and authenticated_pending.owner_kind is not PendingOwnerKind.WORK_GRAPH
        ):
            response = await legacy_primary()
            await self._observe_ordinary(response, actor)
            return response
        active: SupervisorAssistPendingDecision | bool
        if _semantic_supervisor_pending_decision is not None:
            active = _semantic_supervisor_pending_decision
            expected_current = (
                None
                if type(_semantic_supervisor_ingress_binding) is not SupervisorAssistIngressBindingV1
                else _semantic_supervisor_ingress_binding.canonical_sha256()
            )
            if (
                type(active) is not SupervisorAssistPendingDecision
                or active.person_id != actor.own_id
                or active.conversation_id != str(conversation_id or "")
                or active.current_request_binding_sha256 != expected_current
                or not active.matches_message(message)
            ):
                raise SupervisorAssistRuntimeError("carried assist ingress decision is invalid")
        else:
            active = self.classify_supervisor_assist_pending(
                user_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                ingress_binding=_semantic_supervisor_ingress_binding,
                current_attachment_count=1 if attachment_count == 1 else 0,
            )
        if (
            type(active) is SupervisorAssistPendingDecision
            and active.relation is SupervisorAssistPendingRelation.UNCERTAIN
        ):
            raise SupervisorAssistRuntimeError("durable assist ownership is uncertain")
        if authenticated_pending is not None and (
            type(active) is not SupervisorAssistPendingDecision
            or active.pending is not authenticated_pending.admission
        ):
            raise SupervisorAssistRuntimeError("authenticated assist pending-work binding drifted")
        scope = (
            AssistConversationScope(
                user_id=actor.own_id,
                conversation_id=str(conversation_id or ""),
            )
            if type(active) is SupervisorAssistPendingDecision
            else None
        )
        if (
            scope is not None
            and type(active) is SupervisorAssistPendingDecision
            and active.relation is SupervisorAssistPendingRelation.ROOT_REPLAY
        ):
            raise SupervisorAssistRuntimeError("assist root replay crossed the idempotency boundary")
        if (
            scope is not None
            and type(active) is SupervisorAssistPendingDecision
            and active.relation is SupervisorAssistPendingRelation.EXPLICIT_CANCEL
            and normalized in {"отмена", "cancel"}
        ):
            cancelled = await self._controller.cancel_active(
                scope,
                decision=active,
                user_message=normalized,
                absolute_deadline=deadline,
            )
            if cancelled is None or type(cancelled.response) is not dict:
                raise SupervisorAssistRuntimeError("active assist cancellation is uncertain")
            return cancelled.response

        if (
            type(active) is SupervisorAssistPendingDecision
            and active.relation is SupervisorAssistPendingRelation.NEW_TURN
            and scope is not None
        ):
            try:
                disposition = await self._controller.reconcile_pending_before_legacy(
                    scope,
                    active,
                    absolute_deadline=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise SupervisorAssistRuntimeError("durable assist reconciliation is uncertain") from exc
            if disposition is AssistPendingGraphDisposition.UNCERTAIN:
                raise SupervisorAssistRuntimeError("durable assist reconciliation is uncertain")
            if disposition not in {
                AssistPendingGraphDisposition.LIVE_IN_PROCESS,
                AssistPendingGraphDisposition.RETIRED,
            }:
                raise SupervisorAssistRuntimeError("durable assist reconciliation is invalid")
            if authenticated_context is None and (
                isinstance(legacy_kwargs.get("_pending_durable_admission"), PendingDurableTurnAdmission)
                and legacy_kwargs["_pending_durable_admission"].work_graph_id is not None
            ):
                # The graph binding fenced intake for this wrapper.  It is not a
                # legacy Work Item grant and must not be reinterpreted as one.
                legacy_kwargs["_pending_durable_admission"] = None
            response = await legacy_primary()
            await self._observe_ordinary(response, actor)
            return response

        surface = prepare_current_file_web_assist_surface(
            self._settings,
            user_id=user_id,
            message=message,
            actor=actor,
            conversation_id=conversation_id,
            attachments=attachments,
            enable_tools=enable_tools,
            ingestion_result=effective_ingestion_result,
            synthetic_document_notice=synthetic_document_notice,
            replay_source_message_id=replay_source_message_id,
            mode=mode,
            explicit_mode_requested=_semantic_supervisor_explicit_mode_requested,
            answer_with_voice=answer_with_voice,
            reply_to=reply_to,
            quoted_attachment_reference=quoted_attachment_reference,
            reply_assistant_reference=reply_assistant_reference,
            reply_assistant_message_id=reply_assistant_message_id,
            turn_policy=turn_policy,
            pending_durable_admission=_pending_durable_admission,
            ingress_binding=_semantic_supervisor_ingress_binding,
            conversation_is_dialogue=self._conversation_is_dialogue,
        )
        result = await self._controller.execute(
            surface,
            legacy_primary=legacy_primary,
            absolute_deadline=deadline,
        )
        if type(result) is not SupervisorAssistResult:
            raise SupervisorAssistRuntimeError("assist controller returned an invalid result")
        if result.outcome is SupervisorAssistOutcome.LEGACY:
            if legacy_calls != 1 or type(result.response) is not dict:
                raise SupervisorAssistRuntimeError("legacy fallback has no exact response")
            await self._observe_ordinary(result.response, actor)
            return result.response
        if legacy_calls != 0:
            raise SupervisorAssistRuntimeError("promoted owner crossed the legacy boundary")
        if type(result.response) is not dict:
            raise SupervisorAssistRuntimeError("promoted owner has no committed response")
        return result.response

    def start_restart_recovery(self, *, batch_limit: int = 100) -> None:
        if self._closed:
            return
        self._controller.start_restart_recovery(batch_limit=batch_limit)

    async def wait_restart_recovery(self) -> None:
        await self._controller.wait_restart_recovery()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._controller.close()


__all__ = [
    "AssistOrdinaryPostCommitObserver",
    "SemanticSupervisorAssistRuntime",
    "SupervisorAssistRuntimeError",
]
