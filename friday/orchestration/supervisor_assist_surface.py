"""Exact prospective surface for the one promoted read-only supervisor journey."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from friday.file_evidence import (
    CurrentTurnFileReferenceToken,
    current_turn_file_reference_for_tenant,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    CompareCurrentFileWebGraphError,
    CompareCurrentFileWebPlanStepBinding,
    CompareCurrentFileWebStepKind,
    bind_validated_plan_to_compare_current_file_web_graph,
)
from friday.orchestration.contracts import TurnInput
from friday.orchestration.execution_plan import ValidatedExecutionPlan
from friday.orchestration.router import ReadOnlyAttachmentReference, current_attachment_references
from friday.orchestration.semantic_supervisor import supervisor_eligibility
from friday.orchestration.supervisor_assist_ingress import SupervisorAssistIngressBindingV1
from friday.orchestration.supervisor_contracts import TaskClass
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityScope,
    current_raw_source_matches,
)
from friday.orchestration.transient_web_comparison import (
    SealedPublicWebQuery,
    TransientWebComparisonError,
    seal_compare_current_file_public_web_query,
    seal_explicit_public_web_query,
)
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceKind,
    TurnContextError,
    TurnMode,
)
from friday.orchestration.turn_context_call_scope import (
    AuthenticatedChatCallScope,
    require_current_authenticated_chat_call_scope,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnPolicyDecision

_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")


@dataclass(frozen=True, slots=True)
class CurrentFileWebAssistSurface:
    """Process-local exact facts; it grants no durable ownership by itself."""

    turn: TurnInput = field(repr=False)
    actor: ActorContext = field(repr=False)
    conversation_id: str
    attachment: ReadOnlyAttachmentReference = field(repr=False)
    attachment_content_sha256: str
    web_plan: SealedPublicWebQuery = field(repr=False)
    ingress_binding: SupervisorAssistIngressBindingV1 = field(repr=False)
    _authenticated_context: AuthenticatedTurnContext | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _authenticated_scope: AuthenticatedChatCallScope | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.turn, TurnInput)
            or not isinstance(self.actor, ActorContext)
            or _CONVERSATION_ID_RE.fullmatch(self.conversation_id) is None
            or type(self.attachment) is not ReadOnlyAttachmentReference
            or re.fullmatch(r"[0-9a-f]{64}", self.attachment_content_sha256) is None
            or type(self.web_plan) is not SealedPublicWebQuery
            or type(self.ingress_binding) is not SupervisorAssistIngressBindingV1
        ):
            raise ValueError("assist surface is invalid")
        if (self._authenticated_context is None) is not (self._authenticated_scope is None):
            raise ValueError("assist surface authenticated scope is incomplete")
        if self._authenticated_context is not None:
            context = self._authenticated_context
            scope = self._authenticated_scope
            if (
                type(context) is not AuthenticatedTurnContext
                or type(scope) is not AuthenticatedChatCallScope
                or context.model_input is not self.turn
                or scope.model_input is not self.turn
                or context.authority.actor is not self.actor
                or context.authority.conversation_id != self.conversation_id
                or not hmac.compare_digest(
                    context.effect_fence.request_effect_binding_sha256,
                    self.ingress_binding.canonical_sha256(),
                )
            ):
                raise ValueError("assist surface authenticated scope is invalid")

    def require_current_authenticated_call_scope(self) -> AuthenticatedChatCallScope | None:
        """Revalidate the exact transient call before a post-await mutation."""

        context = self._authenticated_context
        sealed = self._authenticated_scope
        if context is None and sealed is None:
            return None
        if type(context) is not AuthenticatedTurnContext or type(sealed) is not AuthenticatedChatCallScope:
            raise TurnContextError("assist surface authenticated scope is invalid")
        current = require_current_authenticated_chat_call_scope(context)
        if (
            current is not sealed
            or current.model_input is not self.turn
            or context.authority.actor is not self.actor
            or context.authority.conversation_id != self.conversation_id
            or not hmac.compare_digest(
                context.effect_fence.request_effect_binding_sha256,
                self.ingress_binding.canonical_sha256(),
            )
        ):
            raise TurnContextError("assist surface authenticated scope drifted")
        return current


def _transient_web_ingestion(value: object) -> bool:
    if value is None:
        return True
    if type(value) is not dict or set(value) != {
        "promoted",
        "queued_for_review",
        "action",
        "category",
        "reason",
    }:
        return False
    return bool(
        value.get("promoted") is False
        and value.get("queued_for_review") is False
        and value.get("action") == "transient"
        and value.get("category") in {"web_request", "compare_current_file_web"}
        and type(value.get("reason")) is str
        and bool(str(value.get("reason") or "").strip())
    )


def prepare_authenticated_current_file_web_assist_surface(
    settings: object,
    *,
    authenticated_context: AuthenticatedTurnContext,
    authenticated_scope: AuthenticatedChatCallScope,
    explicit_mode_requested: bool,
    ingress_binding: SupervisorAssistIngressBindingV1 | None,
    conversation_is_dialogue: Callable[[str, str], bool],
) -> CurrentFileWebAssistSurface | None:
    """Derive the promoted surface only from one live authenticated call seal."""

    if (
        type(authenticated_context) is not AuthenticatedTurnContext
        or type(authenticated_scope) is not AuthenticatedChatCallScope
    ):
        raise TurnContextError("assist surface requires an authenticated call scope")
    current_scope = require_current_authenticated_chat_call_scope(authenticated_context)
    if current_scope is not authenticated_scope:
        raise TurnContextError("assist surface authenticated call scope identity drifted")

    context = authenticated_context
    scope = authenticated_scope
    turn = context.model_input
    actor = context.authority.actor
    conversation_id = context.authority.conversation_id
    if type(ingress_binding) is not SupervisorAssistIngressBindingV1:
        raise TurnContextError("assist surface lost its authenticated ingress binding")
    if not hmac.compare_digest(
        ingress_binding.canonical_sha256(),
        context.effect_fence.request_effect_binding_sha256,
    ):
        raise TurnContextError("assist surface request-effect binding drifted")
    if (
        scope.model_input is not turn
        or turn.message_truncated
        or turn.attachments_truncated
        or turn.enable_tools is not True
        or turn.synthetic_document_notice is not False
        or turn.quoted_attachment_reference is not False
        or turn.reply_assistant_reference is not False
        or turn.reply_quote
        or turn.reply_quote_truncated
        or turn.conversation_mode != TurnMode.DIALOGUE.value
        or context.turn_policy.decision.handled
        or context.pending_work_admission is not None
        or scope.pending_work_bound
        or type(explicit_mode_requested) is not bool
        or explicit_mode_requested
        or actor.user_id != actor.own_id
        or type(conversation_id) is not str
        or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None
        or not _transient_web_ingestion(scope.ingestion_result)
        or len(turn.attachments) != 1
        or len(scope.attachment_carriers) != 1
        or len(scope.attachment_sources) != 1
    ):
        return None
    try:
        if conversation_is_dialogue(actor.own_id, conversation_id) is not True:
            return None
    except Exception:
        return None

    descriptor = turn.attachments[0]
    carrier = scope.attachment_carriers[0]
    source = scope.attachment_sources[0]
    token = source.private_carrier
    if (
        source.kind is not AuthorizedSourceKind.CURRENT_ATTACHMENT
        or source.ordinal != 1
        or source.model_descriptor is not descriptor
        or type(token) is not CurrentTurnFileReferenceToken
        or current_turn_file_reference_for_tenant(carrier, tenant_id=actor.user_id) is not token
        or carrier.get("persisted") is not True
        or carrier.get("current_turn_only") is not True
    ):
        raise TurnContextError("assist surface current source authority drifted")
    attachment = ReadOnlyAttachmentReference(
        ordinal=descriptor.ordinal,
        raw_object_id=token.raw_id,
        source_identity_sha256=token.source_identity_sha256,
        name=descriptor.name,
        media_type=descriptor.media_type,
    )
    eligibility = supervisor_eligibility(turn, settings)
    if (
        not eligibility.eligible
        or eligibility.task_class is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    ):
        return None
    try:
        web_plan = seal_explicit_public_web_query(
            current_user_message=turn.message,
            actor=actor,
            conversation_id=conversation_id,
        )
    except (TransientWebComparisonError, TypeError, ValueError, UnicodeError):
        try:
            web_plan = seal_compare_current_file_public_web_query(
                current_user_message=turn.message,
                actor=actor,
                conversation_id=conversation_id,
            )
        except (TransientWebComparisonError, TypeError, ValueError, UnicodeError):
            return None
    return CurrentFileWebAssistSurface(
        turn=turn,
        actor=actor,
        conversation_id=conversation_id,
        attachment=attachment,
        attachment_content_sha256=token.content_sha256,
        web_plan=web_plan,
        ingress_binding=ingress_binding,
        _authenticated_context=context,
        _authenticated_scope=scope,
    )


def prepare_current_file_web_assist_surface(
    settings: object,
    *,
    user_id: str,
    message: str,
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
    pending_durable_admission: PendingDurableTurnAdmission | None,
    ingress_binding: SupervisorAssistIngressBindingV1 | None,
    conversation_is_dialogue: Callable[[str, str], bool],
) -> CurrentFileWebAssistSurface | None:
    """Recognize the promoted surface without reading a file or calling a model."""

    if (
        not isinstance(actor, ActorContext)
        or type(user_id) is not str
        or user_id != actor.user_id
        or actor.user_id != actor.own_id
        or type(message) is not str
        or type(conversation_id) is not str
        or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None
        or type(explicit_mode_requested) is not bool
        or explicit_mode_requested
        or enable_tools is not True
        or synthetic_document_notice is not False
        or replay_source_message_id is not None
        or mode is not None
        or answer_with_voice is not False
        or reply_to is not None
        or quoted_attachment_reference is not False
        or reply_assistant_reference is not False
        or reply_assistant_message_id is not None
        or turn_policy is not None
        or pending_durable_admission is not None
        or type(ingress_binding) is not SupervisorAssistIngressBindingV1
        or not _transient_web_ingestion(ingestion_result)
        or type(attachments) is not list
        or len(attachments) != 1
        or not isinstance(attachments[0], Mapping)
    ):
        return None
    try:
        if conversation_is_dialogue(str(actor.own_id or ""), conversation_id) is not True:
            return None
    except Exception:
        return None

    carrier = attachments[0]
    token = current_turn_file_reference_for_tenant(
        carrier,
        tenant_id=actor.user_id,
    )
    if type(token) is not CurrentTurnFileReferenceToken:
        return None
    snapshot = dict(carrier)
    try:
        turn = TurnInput.from_chat(
            message=message,
            actor=actor,
            conversation_id=conversation_id,
            attachments=[snapshot],
            enable_tools=True,
            synthetic_document_notice=False,
            mode=None,
            reply_to=None,
            quoted_attachment_reference=False,
            reply_assistant_reference=False,
        )
    except Exception:
        return None
    if (
        turn.message != message
        or turn.message_truncated
        or turn.attachments_truncated
        or len(turn.attachments) != 1
    ):
        return None
    references = current_attachment_references(turn, (snapshot,), (token,))
    if len(references) != 1:
        return None
    eligibility = supervisor_eligibility(turn, settings)
    if (
        not eligibility.eligible
        or eligibility.task_class is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    ):
        return None
    try:
        web_plan = seal_explicit_public_web_query(
            current_user_message=message,
            actor=actor,
            conversation_id=conversation_id,
        )
    except (TransientWebComparisonError, TypeError, ValueError, UnicodeError):
        try:
            web_plan = seal_compare_current_file_public_web_query(
                current_user_message=message,
                actor=actor,
                conversation_id=conversation_id,
            )
        except (TransientWebComparisonError, TypeError, ValueError, UnicodeError):
            return None
    return CurrentFileWebAssistSurface(
        turn=turn,
        actor=actor,
        conversation_id=conversation_id,
        attachment=references[0],
        attachment_content_sha256=token.content_sha256,
        web_plan=web_plan,
        ingress_binding=ingress_binding,
    )


def bind_assist_plan_to_surface(
    plan: ValidatedExecutionPlan,
    surface: CurrentFileWebAssistSurface,
) -> tuple[CompareCurrentFileWebPlanStepBinding, ...] | None:
    """Bind proposal-local steps to the fixed graph and exact outbound query."""

    if (
        type(plan) is not ValidatedExecutionPlan
        or type(surface) is not CurrentFileWebAssistSurface
        or plan.authority_scope is not PlanAuthorityScope.ASSIST_EXECUTION
        or len(plan.source_bindings) != 1
        or not current_raw_source_matches(
            plan.source_bindings[0],
            raw_object_id=surface.attachment.raw_object_id,
            source_identity_sha256=surface.attachment.source_identity_sha256,
            content_sha256=surface.attachment_content_sha256,
        )
    ):
        return None
    try:
        bindings = bind_validated_plan_to_compare_current_file_web_graph(plan)
    except (CompareCurrentFileWebGraphError, TypeError, ValueError):
        return None
    by_kind = {item.graph_kind: item for item in bindings}
    file_input = by_kind[CompareCurrentFileWebStepKind.FILE_READ].plan_step.input
    web_input = by_kind[CompareCurrentFileWebStepKind.WEB_READ].plan_step.input
    query_intent = web_input.get("query_intent")
    try:
        query_sha256 = (
            hashlib.sha256(query_intent.encode("utf-8", errors="strict")).hexdigest()
            if type(query_intent) is str
            else ""
        )
    except UnicodeError:
        return None
    if file_input.get("attachment_ordinal") != 1 or not hmac.compare_digest(
        query_sha256,
        surface.web_plan.query_sha256,
    ):
        return None
    return bindings


__all__ = [
    "CurrentFileWebAssistSurface",
    "bind_assist_plan_to_surface",
    "prepare_authenticated_current_file_web_assist_surface",
    "prepare_current_file_web_assist_surface",
]
