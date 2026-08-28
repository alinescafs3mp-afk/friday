"""Exact raw-call validation for an authenticated turn.

The ingress-issued :class:`AuthenticatedTurnContext` is authoritative.  This
module proves that compatibility arguments still describe that same call; it
never derives a replacement model input or policy from them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from friday.file_evidence import current_turn_file_reference_of
from friday.orchestration.contracts import AttachmentDescriptor, RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceIdentity,
    AuthorizedSourceKind,
    IngressKind,
    TurnContextError,
    TurnMode,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnPolicyDecision


@dataclass(frozen=True, slots=True)
class AuthenticatedChatCallScope:
    """Validated process-local projection; never store it past the primary call."""

    model_input: TurnInput
    attachment_carriers: tuple[Mapping[str, Any], ...]
    attachment_sources: tuple[AuthorizedSourceIdentity, ...]
    deadline_monotonic: float
    deadline_monotonic_ns: int
    router_mode: RouterMode
    actor_binding_sha256: str
    conversation_binding_sha256: str
    pending_work_bound: bool


def _normalized_text(value: str | None) -> str:
    return str(value or "").strip().encode("utf-8", errors="replace").decode("utf-8")


def _attachment_scope(
    context: AuthenticatedTurnContext,
    attachments: list[dict[str, Any]] | None,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[AuthorizedSourceIdentity, ...]]:
    if attachments is None:
        carriers: tuple[Mapping[str, Any], ...] = ()
    elif type(attachments) is list and all(isinstance(item, Mapping) for item in attachments):
        carriers = tuple(attachments)
    else:
        raise TurnContextError("authenticated turn attachment carriers are invalid")

    turn = context.model_input
    if turn.attachments_truncated or len(carriers) != len(turn.attachments):
        raise TurnContextError("authenticated turn attachment cardinality drifted")
    sources_by_ordinal = {
        source.ordinal: source
        for source in context.authorized_sources
        if source.kind is not AuthorizedSourceKind.ACCEPTED_INGRESS
    }
    if set(sources_by_ordinal) != set(range(1, len(turn.attachments) + 1)):
        raise TurnContextError("authenticated turn attachment source set drifted")

    ordered_sources: list[AuthorizedSourceIdentity] = []
    for ordinal, (carrier, descriptor) in enumerate(
        zip(carriers, turn.attachments, strict=True),
        start=1,
    ):
        try:
            projected = AttachmentDescriptor.from_raw(carrier, ordinal=ordinal)
        except Exception as exc:
            raise TurnContextError("authenticated turn attachment descriptor is invalid") from exc
        source = sources_by_ordinal[ordinal]
        if projected != descriptor or source.model_descriptor is not descriptor:
            raise TurnContextError("authenticated turn attachment descriptor drifted")
        # Chat attachments are current-ingress carriers.  Registered archive or
        # reply sources need a future typed call carrier and are closed here.
        if (
            source.kind is not AuthorizedSourceKind.CURRENT_ATTACHMENT
            or current_turn_file_reference_of(carrier) is not source.private_carrier
        ):
            raise TurnContextError("authenticated turn attachment carrier drifted")
        ordered_sources.append(source)
    return carriers, tuple(ordered_sources)


def require_authenticated_chat_call_scope(
    context: AuthenticatedTurnContext,
    *,
    user_id: str,
    message: str,
    actor: ActorContext,
    conversation_id: str | None,
    attachments: list[dict[str, Any]] | None,
    enable_tools: bool,
    synthetic_document_notice: bool,
    replay_source_message_id: str | None,
    mode: str | None,
    answer_with_voice: bool,
    reply_to: str | None,
    quoted_attachment_reference: bool,
    reply_assistant_reference: bool,
    reply_assistant_message_id: str | None,
    turn_policy: TurnPolicyDecision | None,
    telegram_update_id: str | None,
    turn_deadline: float | None,
    pending_durable_admission: PendingDurableTurnAdmission | None,
    runtime_router_mode: RouterMode | None = None,
) -> AuthenticatedChatCallScope:
    """Require every authority-relevant compatibility argument to be exact."""

    if type(context) is not AuthenticatedTurnContext:
        raise TurnContextError("authenticated chat call has an invalid context")
    turn = context.model_input
    authority = context.authority
    if (
        authority.actor is not actor
        or type(user_id) is not str
        or authority.tenant_id != user_id
        or authority.conversation_id != conversation_id
    ):
        raise TurnContextError("authenticated turn actor or conversation scope drifted")

    carriers, attachment_sources = _attachment_scope(context, attachments)
    if type(message) is not str or turn.message_truncated:
        raise TurnContextError("authenticated turn message is not exact")
    expected_message = (
        ("Загружены документы." if len(carriers) > 1 else "Загружен документ.")
        if synthetic_document_notice and carriers
        else _normalized_text(message)
    )
    if turn.message != expected_message:
        raise TurnContextError("authenticated turn message drifted")

    if type(enable_tools) is not bool or turn.enable_tools is not enable_tools:
        raise TurnContextError("authenticated turn tool authority drifted")
    if (
        type(synthetic_document_notice) is not bool
        or turn.synthetic_document_notice is not synthetic_document_notice
        or type(quoted_attachment_reference) is not bool
        or turn.quoted_attachment_reference is not quoted_attachment_reference
        or type(reply_assistant_reference) is not bool
        or turn.reply_assistant_reference is not reply_assistant_reference
    ):
        raise TurnContextError("authenticated turn surface flags drifted")
    if turn.reply_quote_truncated or turn.reply_quote != _normalized_text(reply_to):
        raise TurnContextError("authenticated turn reply scope drifted")
    if replay_source_message_id is not None or reply_assistant_message_id is not None:
        raise TurnContextError("authenticated turn carries an unbound replay or reply identity")
    if type(answer_with_voice) is not bool or answer_with_voice:
        raise TurnContextError("authenticated turn carries unbound voice delivery")

    raw_mode = _normalized_text(mode or TurnMode.DIALOGUE.value).casefold()
    if len(raw_mode) > 40 or raw_mode != turn.conversation_mode:
        raise TurnContextError("authenticated turn interaction mode drifted")
    expected_policy = context.turn_policy.decision if context.turn_policy.decision.handled else None
    if turn_policy is not expected_policy:
        raise TurnContextError("authenticated turn policy carrier drifted")
    expected_pending = (
        context.pending_work_admission.admission
        if context.pending_work_admission is not None
        else None
    )
    if pending_durable_admission is not expected_pending:
        raise TurnContextError("authenticated turn pending-work carrier drifted")

    if authority.ingress_kind is IngressKind.TELEGRAM:
        if type(telegram_update_id) is not str or telegram_update_id != authority.update_id:
            raise TurnContextError("authenticated turn Telegram update identity drifted")
    elif telegram_update_id is not None:
        raise TurnContextError("signed HTTP turn carries a Telegram update identity")

    if (
        not isinstance(turn_deadline, (int, float))
        or isinstance(turn_deadline, bool)
        or not math.isfinite(float(turn_deadline))
        or int(float(turn_deadline) * 1_000_000_000)
        != context.inherited_budget.safety_deadline.monotonic_ns
    ):
        raise TurnContextError("authenticated turn deadline drifted")
    sealed_router_mode = context.turn_policy.router_mode
    if (
        runtime_router_mode is not None
        and sealed_router_mode is not RouterMode.LEGACY
        and runtime_router_mode is not sealed_router_mode
    ):
        raise TurnContextError("authenticated turn router mode drifted")

    return AuthenticatedChatCallScope(
        model_input=turn,
        attachment_carriers=carriers,
        attachment_sources=attachment_sources,
        deadline_monotonic=float(turn_deadline),
        deadline_monotonic_ns=context.inherited_budget.safety_deadline.monotonic_ns,
        router_mode=sealed_router_mode,
        actor_binding_sha256=authority.actor_binding_sha256,
        conversation_binding_sha256=authority.conversation.binding_sha256,
        pending_work_bound=context.pending_work_admission is not None,
    )


__all__ = ["AuthenticatedChatCallScope", "require_authenticated_chat_call_scope"]
