from __future__ import annotations

import hashlib

import pytest

from friday.file_evidence import (
    stamp_current_turn_file_reference,
    stamp_current_turn_file_reference_for_tenant,
)
from friday.orchestration.contracts import RouterMode
from friday.orchestration.turn_context import (
    AuthorizedSourceKind,
    ConversationScopeKind,
    IngressKind,
    PendingOwnerKind,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
)
from friday.orchestration.turn_context_ingress import (
    issue_authenticated_scalar_turn_context,
    issue_authenticated_turn_context,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_NOW_NS = 50_000_000_000_000
_CONVERSATION_ID = "conv_0123456789abcdef"


class _Carrier(dict[str, object]):
    pass


class _StringLookalike(str):
    pass


def _issuer(label: str, now: list[int]) -> TurnContextIssuer:
    return TurnContextIssuer(
        hashlib.sha256(label.encode("ascii")).digest(),
        _monotonic_ns=lambda: now[0],
    )


def _effect(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _current_carrier(
    ordinal: int,
    *,
    tenant_id: str = "owner",
    raw_id: str | None = None,
    legacy: bool = False,
) -> dict[str, object]:
    resolved_raw_id = raw_id or f"raw_{ordinal:016x}"
    raw = {
        "id": resolved_raw_id,
        "user_id": tenant_id,
        "source": "upload",
        "source_ref": f"telegram-file:{ordinal}",
        "content_type": "file",
        "received_at": "2026-08-29T00:00:00Z",
        "content_hash": f"{ordinal % 16:x}" * 64,
        "raw_content": f"body-{ordinal}",
        "metadata_json": "{}",
    }
    carrier: dict[str, object] = _Carrier(
        {
            "raw_object_id": resolved_raw_id,
            "filename": f"file-{ordinal}.txt",
            "mime_type": "text/plain",
            "transient_text": f"body-{ordinal}",
            "extraction_success": True,
            "persisted": True,
            "current_turn_only": True,
        }
    )
    if legacy:
        stamp_current_turn_file_reference(carrier, raw)
    else:
        stamp_current_turn_file_reference_for_tenant(carrier, raw, tenant_id=tenant_id)
    return carrier


def _issue_upload_turn(
    issuer: TurnContextIssuer,
    actor: ActorContext,
    attachments: list[dict[str, object]],
    *,
    now: int,
):
    return issue_authenticated_turn_context(
        issuer,
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"request-upload-{len(attachments)}-{now}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=actor.source,
        update_id=f"request-upload-{len(attachments)}-{now}",
        request_effect_binding_sha256=_effect(f"upload-{len(attachments)}-{now}"),
        message="Разбери приложенные файлы.",
        enable_tools=True,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
        router_mode=RouterMode.V12,
        deadline_monotonic_ns=now + 2_000_000_000,
        max_output_tokens=4096,
        attachments=attachments,
    )


def test_signed_http_scalar_context_is_one_exact_closed_turn() -> None:
    now = [_NOW_NS]
    issuer = _issuer("scalar-http", now)
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="api-token",
        identity_id="owner-principal",
        session_id="owner-session",
    )
    decision = TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH)

    context = issue_authenticated_scalar_turn_context(
        issuer,
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token="request-source-1",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=actor.source,
        update_id="request-source-1",
        request_effect_binding_sha256=_effect("http-effect"),
        message="Проверь текущий статус.",
        enable_tools=True,
        decision=decision,
        router_mode=RouterMode.SHADOW,
        deadline_monotonic_ns=now[0] + 2_000_000_000,
        max_output_tokens=4096,
    )

    assert issuer.require_context(context) is context
    assert context.authority.actor is actor
    assert context.authority.conversation.kind is ConversationScopeKind.EXISTING
    assert context.authority.conversation_id == _CONVERSATION_ID
    assert context.model_input.message == "Проверь текущий статус."
    assert context.model_input.conversation_mode == TurnMode.DIALOGUE.value
    assert context.turn_policy.decision is decision
    assert context.turn_policy.router_mode is RouterMode.SHADOW
    assert context.turn_policy.fallback_router_mode is RouterMode.LEGACY
    assert context.inherited_budget.resources.max_tool_calls == 4
    assert context.inherited_budget.resources.max_tool_rounds == 2
    assert context.inherited_budget.resources.max_advisory_calls == 3
    assert context.inherited_budget.resources.max_output_tokens == 4096
    assert len(context.authorized_sources) == 1
    assert context.authorized_sources[0].private_carrier is context.authority


@pytest.mark.parametrize(
    "conversation_id",
    (None, "", "conv_FFFFFFFFFFFFFFFF", _StringLookalike(_CONVERSATION_ID)),
)
def test_authenticated_ingress_never_mints_new_or_coercible_conversation_scope(
    conversation_id: object,
) -> None:
    now = [_NOW_NS + 5_000]
    issuer = _issuer("exact-conversation", now)
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")

    with pytest.raises(TurnContextError, match="exact existing conversation"):
        issue_authenticated_turn_context(
            issuer,
            ingress_kind=IngressKind.SIGNED_HTTP,
            ingress_issued_token="request-source-invalid-conversation",
            actor=actor,
            conversation_id=conversation_id,  # type: ignore[arg-type]
            interaction_mode=TurnMode.DIALOGUE,
            source_id=actor.source,
            update_id="request-source-invalid-conversation",
            request_effect_binding_sha256=_effect("invalid-conversation-effect"),
            message="Проверь",
            enable_tools=True,
            decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
            router_mode=RouterMode.LEGACY,
            deadline_monotonic_ns=now[0] + 1_000_000_000,
            max_output_tokens=2048,
        )


def test_telegram_scalar_context_uses_authenticated_chat_and_update() -> None:
    now = [_NOW_NS + 10_000]
    issuer = _issuer("scalar-telegram", now)
    actor = ActorContext(
        user_id="owner",
        preset_key="owner",
        source="telegram-bridge",
        identity_id="71001",
        session_id="telegram-session",
        telegram_chat_id="-77001",
    )
    context = issue_authenticated_scalar_turn_context(
        issuer,
        ingress_kind=IngressKind.TELEGRAM,
        ingress_issued_token="telegram-update:88001",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id="-77001",
        update_id="88001",
        request_effect_binding_sha256=_effect("telegram-effect"),
        message="Привет",
        enable_tools=False,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
        router_mode=RouterMode.LEGACY,
        deadline_monotonic_ns=now[0] + 1_000_000_000,
        max_output_tokens=2048,
    )

    assert context.authority.source_id == actor.telegram_chat_id
    assert context.authority.update_id == "88001"
    assert context.turn_policy.fallback_router_mode is None
    assert context.inherited_budget.resources.max_tool_calls == 0
    assert context.inherited_budget.resources.max_tool_rounds == 0


def test_matching_pending_owner_is_bound_into_the_same_authority() -> None:
    now = [_NOW_NS + 20_000]
    issuer = _issuer("scalar-pending", now)
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")
    admission = PendingDurableTurnAdmission.owned(
        person_id=actor.own_id,
        conversation_id=_CONVERSATION_ID,
        work_item_id="work_0123456789abcdef",
        revision=3,
    )

    context = issue_authenticated_scalar_turn_context(
        issuer,
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token="request-source-pending",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=actor.source,
        update_id="request-source-pending",
        request_effect_binding_sha256=_effect("pending-effect"),
        message="Продолжи",
        enable_tools=True,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
        router_mode=RouterMode.LEGACY,
        deadline_monotonic_ns=now[0] + 1_000_000_000,
        max_output_tokens=2048,
        pending_admission=admission,
    )

    assert context.pending_work_admission is not None
    assert context.pending_work_admission.admission is admission
    assert context.pending_work_admission.owner_kind is PendingOwnerKind.WORK_ITEM


def test_scalar_context_never_mints_a_fresh_or_overlong_deadline() -> None:
    now = [_NOW_NS + 30_000]
    issuer = _issuer("scalar-deadline", now)
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")

    with pytest.raises(TurnContextError, match="exceeds"):
        issue_authenticated_scalar_turn_context(
            issuer,
            ingress_kind=IngressKind.SIGNED_HTTP,
            ingress_issued_token="request-source-too-long",
            actor=actor,
            conversation_id=_CONVERSATION_ID,
            interaction_mode=TurnMode.DIALOGUE,
            source_id=actor.source,
            update_id="request-source-too-long",
            request_effect_binding_sha256=_effect("deadline-effect"),
            message="Проверь",
            enable_tools=True,
            decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
            router_mode=RouterMode.LEGACY,
            deadline_monotonic_ns=now[0] + 3_600_000_000_001,
            max_output_tokens=2048,
        )

    with pytest.raises(TurnContextError, match="message exceeds"):
        issue_authenticated_scalar_turn_context(
            issuer,
            ingress_kind=IngressKind.SIGNED_HTTP,
            ingress_issued_token="request-source-oversized",
            actor=actor,
            conversation_id=_CONVERSATION_ID,
            interaction_mode=TurnMode.DIALOGUE,
            source_id=actor.source,
            update_id="request-source-oversized",
            request_effect_binding_sha256=_effect("oversized-effect"),
            message="x" * 16_001,
            enable_tools=True,
            decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
            router_mode=RouterMode.LEGACY,
            deadline_monotonic_ns=now[0] + 1_000_000_000,
            max_output_tokens=2048,
        )


@pytest.mark.parametrize("count", (1, 10))
def test_current_upload_context_binds_one_ordered_same_tenant_source_per_descriptor(count: int) -> None:
    now = _NOW_NS + 40_000 + count
    clock = [now]
    issuer = _issuer(f"upload-{count}", clock)
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")
    carriers = [_current_carrier(ordinal) for ordinal in range(1, count + 1)]

    context = _issue_upload_turn(issuer, actor, carriers, now=now)

    assert issuer.require_context(context) is context
    assert context.model_input.attachments_truncated is False
    assert len(context.model_input.attachments) == count
    assert [source.kind for source in context.authorized_sources] == [
        AuthorizedSourceKind.ACCEPTED_INGRESS,
        *([AuthorizedSourceKind.CURRENT_ATTACHMENT] * count),
    ]
    for ordinal, source in enumerate(context.authorized_sources[1:], start=1):
        assert source.ordinal == ordinal
        assert source.model_descriptor is context.model_input.attachments[ordinal - 1]
        assert source.private_carrier.tenant_id == actor.user_id


@pytest.mark.parametrize(
    "carriers",
    (
        [_current_carrier(1, tenant_id="bob")],
        [_current_carrier(1, legacy=True)],
        [
            _current_carrier(1, raw_id="raw_00000000000000aa"),
            _current_carrier(2, raw_id="raw_00000000000000aa"),
        ],
        [_current_carrier(ordinal) for ordinal in range(1, 12)],
    ),
)
def test_current_upload_context_rejects_foreign_legacy_duplicate_and_overbound_sources(
    carriers: list[dict[str, object]],
) -> None:
    now = _NOW_NS + 50_000 + len(carriers)
    issuer = _issuer(f"rejected-upload-{len(carriers)}", [now])
    actor = ActorContext(user_id="owner", preset_key="owner", source="api-token")

    with pytest.raises(TurnContextError):
        _issue_upload_turn(issuer, actor, carriers, now=now)
