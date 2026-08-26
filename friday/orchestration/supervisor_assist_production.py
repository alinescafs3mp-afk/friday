"""Concrete production dependencies for the bounded supervisor assist lane."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from friday.file_evidence_reader import (
    PreparedFileEvidence,
    prepare_current_turn_file_evidence,
    prepared_file_evidence_is_process_owned,
)
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    operational_capability_snapshot,
)
from friday.orchestration.supervisor_actor_binding import (
    supervisor_canary_actor_binding_from_transaction,
)
from friday.orchestration.supervisor_assist_controller import AssistCommittedObservation
from friday.orchestration.supervisor_assist_graph_adapter import (
    AssistAdmissionBoundary,
    AssistCapabilityBoundary,
    AssistPublicationAction,
    AssistPublicationBoundary,
    AssistRestartRebindBoundary,
)
from friday.orchestration.supervisor_assist_ports import AssistPromotionEvaluator
from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.orchestration.supervisor_production_baseline import PromotedObservationEligibility
from friday.orchestration.supervisor_promoted_product_event import (
    PromotedProductEmissionRequest,
    PromotedProductEventReplayError,
    build_promoted_other_turn_emission_request,
    emit_promoted_supervisor_product_event_in_transaction,
)
from friday.orchestration.transient_web_comparison import TRANSIENT_WEB_SECURITY_ID
from friday.permissions import ActorContext, AuthorizationService
from friday.storage._base import normalize_conversation_mode
from friday.storage._core import read_only_storage_snapshot

_FILE_SECURITY_ID = "files.read"
_CHAT_SECURITY_ID = "chat.use"
_ALLOWED_SECURITY_IDS = frozenset({_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID})


class CurrentTurnAssistFileEvidenceReader:
    """Re-read the exact current attachment through the existing evidence plane."""

    __slots__ = ("_authorization", "_files_root", "_max_bytes", "_storage")

    def __init__(
        self,
        *,
        storage: Any,
        authorization: AuthorizationService,
        files_root: Path,
        max_bytes: int,
    ) -> None:
        if not callable(getattr(storage, "transaction", None)):
            raise TypeError("assist file reader requires transactional storage")
        if not isinstance(authorization, AuthorizationService):
            raise TypeError("assist file reader requires AuthorizationService")
        if not isinstance(files_root, Path) or not files_root.is_absolute():
            raise ValueError("assist file root must be one absolute Path")
        if type(max_bytes) is not int or not 1 <= max_bytes <= (1 << 31):
            raise ValueError("assist file byte budget is invalid")
        self._storage = storage
        self._authorization = authorization
        self._files_root = files_root
        self._max_bytes = max_bytes

    async def prepare(
        self,
        surface: CurrentFileWebAssistSurface,
        *,
        absolute_deadline: float,
    ) -> PreparedFileEvidence:
        if type(surface) is not CurrentFileWebAssistSurface:
            raise TypeError("assist file reader requires the exact surface")
        if (
            isinstance(absolute_deadline, bool)
            or not isinstance(absolute_deadline, int | float)
            or not math.isfinite(float(absolute_deadline))
            or float(absolute_deadline) <= time.monotonic()
        ):
            raise TimeoutError("assist file deadline expired before preparation")
        evidence = await asyncio.to_thread(
            prepare_current_turn_file_evidence,
            self._storage,
            self._authorization,
            self._files_root,
            surface.actor,
            (surface.attachment,),
            max_bytes=self._max_bytes,
            absolute_deadline=float(absolute_deadline),
        )
        if not prepared_file_evidence_is_process_owned(evidence):
            raise TypeError("assist file evidence is not process-owned")
        return evidence


class SupervisorAssistActorBinding:
    """Read the durable deployment namespace without opening a writer."""

    __slots__ = ("_storage",)

    def __init__(self, storage: Any) -> None:
        if not hasattr(storage, "conn"):
            raise TypeError("assist actor binding requires storage")
        self._storage = storage

    def __call__(self, actor: ActorContext) -> str:
        with read_only_storage_snapshot(self._storage) as conn:
            return supervisor_canary_actor_binding_from_transaction(conn, actor)


class SupervisorAssistRestartActorResolver:
    """Reconstruct only the current personal account principal after restart."""

    __slots__ = ("_authorization",)

    def __init__(self, authorization: AuthorizationService) -> None:
        if not isinstance(authorization, AuthorizationService):
            raise TypeError("assist restart actor resolver requires AuthorizationService")
        self._authorization = authorization

    def __call__(self, graph: object) -> ActorContext | None:
        user_id = getattr(graph, "user_id", None)
        if type(user_id) is not str:
            return None
        actor = self._authorization.actor_for_user(
            user_id,
            source="semantic-recovery",
        )
        if type(actor) is not ActorContext or actor.user_id != user_id or actor.own_id != user_id:
            return None
        return actor


class SupervisorAssistAuthorityGate:
    """Fresh permission gate evaluated in the adapter's current DB snapshot."""

    __slots__ = ("_authorization", "_storage")

    def __init__(self, storage: Any, authorization: AuthorizationService) -> None:
        if not hasattr(storage, "conn"):
            raise TypeError("assist authority gate requires storage")
        if not isinstance(authorization, AuthorizationService):
            raise TypeError("assist authority gate requires AuthorizationService")
        self._storage = storage
        self._authorization = authorization

    @staticmethod
    def _capabilities(actor: ActorContext, boundary: object) -> tuple[str, ...] | None:
        if not isinstance(actor, ActorContext) or type(actor) is not ActorContext:
            return None
        if type(boundary) not in {
            AssistAdmissionBoundary,
            AssistCapabilityBoundary,
            AssistPublicationBoundary,
            AssistRestartRebindBoundary,
        }:
            return None
        carried = cast(
            AssistAdmissionBoundary
            | AssistCapabilityBoundary
            | AssistPublicationBoundary
            | AssistRestartRebindBoundary,
            boundary,
        )
        if actor.user_id != actor.own_id or carried.actor is not actor or carried.user_id != actor.user_id:
            return None
        if type(boundary) in {AssistAdmissionBoundary, AssistRestartRebindBoundary}:
            return (_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID)
        if type(boundary) is AssistCapabilityBoundary:
            security_id = cast(AssistCapabilityBoundary, boundary).security_id
            if security_id is None:
                return (_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID)
            return (security_id,) if security_id in _ALLOWED_SECURITY_IDS else None
        if type(boundary) is AssistPublicationBoundary:
            action = cast(AssistPublicationBoundary, boundary).action
            if action is AssistPublicationAction.COMPARISON:
                return (_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID)
            if action is AssistPublicationAction.TERMINAL:
                if cast(AssistPublicationBoundary, boundary).expected_reason.value == "authority_denied":
                    return (_CHAT_SECURITY_ID,)
                return (_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID)
            if action is AssistPublicationAction.CANCEL:
                return (_CHAT_SECURITY_ID,)
            if action in {
                AssistPublicationAction.RESTART_RETIREMENT,
            }:
                return (_FILE_SECURITY_ID, TRANSIENT_WEB_SECURITY_ID)
        return None

    def __call__(self, actor: ActorContext, boundary: object) -> bool:
        capabilities = self._capabilities(actor, boundary)
        if capabilities is None:
            return False
        with read_only_storage_snapshot(self._storage) as conn:
            row = conn.execute(
                "SELECT status FROM users WHERE id=?",
                (actor.own_id,),
            ).fetchone()
            if row is None or str(row["status"] or "") != "active":
                return False
            return all(
                self._authorization.authorize_in_transaction(conn, actor, security_id).allowed
                for security_id in capabilities
            )


def supervisor_assist_read_only_effect_gate(boundary: object) -> bool:
    """Admit only the fixed read graph; the adapter owns the durable request fence."""

    if type(boundary) is AssistAdmissionBoundary:
        return True
    if type(boundary) is AssistRestartRebindBoundary:
        return True
    if type(boundary) is AssistCapabilityBoundary:
        security_id = cast(AssistCapabilityBoundary, boundary).security_id
        return security_id is None or security_id in _ALLOWED_SECURITY_IDS
    if type(boundary) is AssistPublicationBoundary:
        return cast(AssistPublicationBoundary, boundary).action in {
            AssistPublicationAction.COMPARISON,
            AssistPublicationAction.TERMINAL,
            AssistPublicationAction.CANCEL,
            AssistPublicationAction.RESTART_RETIREMENT,
            AssistPublicationAction.EXPIRY_RETIREMENT,
        }
    return False


class AssistConversationModeReader:
    """Prove one active personal dialogue without advancing storage clocks."""

    __slots__ = ("_storage",)

    def __init__(self, storage: Any) -> None:
        if not hasattr(storage, "conn"):
            raise TypeError("assist conversation reader requires storage")
        self._storage = storage

    def __call__(self, person_id: str, conversation_id: str) -> bool:
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                row = conn.execute(
                    "SELECT mode,is_archived FROM conversations WHERE id=? AND user_id=?",
                    (conversation_id, person_id),
                ).fetchone()
                return bool(
                    row is not None
                    and int(row["is_archived"]) == 0
                    and normalize_conversation_mode(str(row["mode"] or "dialogue")) == "dialogue"
                )
        except Exception:
            return False


class SupervisorPromotedProductObserver:
    """Append body-free events only from exact committed traces and receipts."""

    __slots__ = (
        "_actor_binding",
        "_binding_snapshot_factory",
        "_promotion",
        "_storage",
    )

    def __init__(
        self,
        *,
        storage: Any,
        promotion_evaluator: AssistPromotionEvaluator,
        actor_binding: Callable[[ActorContext], str],
        binding_snapshot_factory: Callable[[], CapabilityBindingSnapshot] = (operational_capability_snapshot),
    ) -> None:
        if not hasattr(storage, "conn"):
            raise TypeError("promoted observer requires storage")
        if not callable(getattr(promotion_evaluator, "decide", None)):
            raise TypeError("promoted observer requires a promotion evaluator")
        if not callable(actor_binding) or not callable(binding_snapshot_factory):
            raise TypeError("promoted observer binding dependencies are unavailable")
        self._storage = storage
        self._promotion = promotion_evaluator
        self._actor_binding = actor_binding
        self._binding_snapshot_factory = binding_snapshot_factory

    def _emit_committed(self, observation: AssistCommittedObservation) -> None:
        request = PromotedProductEmissionRequest(
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=observation.primary_trace_sha256,
            execution_receipt_sha256=observation.execution_receipt_sha256,
            supervisor_invoked=True,
        )
        try:
            with self._storage.transaction() as conn:
                emit_promoted_supervisor_product_event_in_transaction(
                    conn,
                    promotion_decision=observation.promotion_decision,
                    request=request,
                )
        except PromotedProductEventReplayError:
            return

    async def __call__(self, observation: AssistCommittedObservation) -> None:
        if type(observation) is not AssistCommittedObservation:
            raise TypeError("promoted observation requires the exact committed receipt")
        await asyncio.to_thread(self._emit_committed, observation)

    def _emit_ordinary(self, response: Mapping[str, Any], actor: ActorContext) -> bool:
        if (
            type(actor) is not ActorContext
            or actor.user_id != actor.own_id
            or type(response.get("message_id")) is not str
            or type(response.get("conversation_id")) is not str
        ):
            return False
        snapshot = self._binding_snapshot_factory()
        actor_binding = self._actor_binding(actor)
        decision = self._promotion.decide(
            binding_snapshot=snapshot,
            actor_binding_sha256=actor_binding,
        )
        if decision is None or not decision.promotion_admitted:
            return False
        request = build_promoted_other_turn_emission_request(
            self._storage.conn,
            assistant_message_id=str(response["message_id"]),
            user_id=actor.user_id,
            conversation_id=str(response["conversation_id"]),
        )
        with suppress(PromotedProductEventReplayError), self._storage.transaction() as conn:
            emit_promoted_supervisor_product_event_in_transaction(
                conn,
                promotion_decision=decision,
                request=request,
            )
        return True

    async def observe_ordinary(
        self,
        response: Mapping[str, Any],
        actor: ActorContext,
    ) -> bool:
        return await asyncio.to_thread(self._emit_ordinary, response, actor)


__all__ = [
    "AssistConversationModeReader",
    "CurrentTurnAssistFileEvidenceReader",
    "SupervisorAssistActorBinding",
    "SupervisorAssistAuthorityGate",
    "SupervisorAssistRestartActorResolver",
    "SupervisorPromotedProductObserver",
    "supervisor_assist_read_only_effect_gate",
]
