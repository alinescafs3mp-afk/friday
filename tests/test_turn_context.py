from __future__ import annotations

import dataclasses
import json

import pytest

from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceIdentity,
    ConversationScopeKind,
    EffectOwner,
    FinalPublisher,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    PendingOwnerKind,
    TurnContextError,
    TurnContextIssuer,
    TurnIdentity,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.source_identity import AuthorizedFileSnapshotToken, authorized_file_snapshot_token
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_KEY = bytes(range(32))
_NOW_NS = 10_000_000_000_000
_DEADLINE_NS = _NOW_NS + 300_000_000_000
_CONVERSATION_ID = "conv_0123456789abcdef"
_REQUEST_BINDING = "1" * 64


def _actor(
    *,
    tenant_id: str = "tenant-alice",
    preset_key: str = "owner",
    identity_id: str | None = "principal-alice",
    session_id: str | None = "session-a",
    shared: bool = False,
    person_id: str = "",
) -> ActorContext:
    return ActorContext(
        user_id=tenant_id,
        preset_key=preset_key,
        source="api-token",
        identity_id=identity_id,
        session_id=session_id,
        shared_tenant=shared,
        person_id=person_id,
    )


def _model_input(
    actor: ActorContext,
    *,
    conversation_id: str | None = _CONVERSATION_ID,
    message: str = "Составь краткий план.",
    reply: str = "Предыдущая безопасная цитата",
    mode: TurnMode = TurnMode.DIALOGUE,
    enable_tools: bool = True,
    with_attachment: bool = True,
    attachment_raw: dict[str, object] | None = None,
) -> TurnInput:
    attachments = [attachment_raw or _file_raw()] if with_attachment else []
    return TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=conversation_id,
        attachments=attachments,
        enable_tools=enable_tools,
        synthetic_document_notice=False,
        mode=mode.value,
        reply_to=reply,
        quoted_attachment_reference=with_attachment,
        reply_assistant_reference=False,
    )


def _file_raw(
    *,
    raw_id: str = "raw_0123456789abcdef",
    source_ref: str = "attachment-source-1",
    content_sha256: str = "2" * 64,
    body: str = "PRIVATE SOURCE BODY",
) -> dict[str, object]:
    return {
        "id": raw_id,
        "source": "api",
        "source_ref": source_ref,
        "content_type": "application/pdf",
        "mime_type": "application/pdf",
        "filename": "/srv/private/customer-plan.pdf",
        "size_bytes": 1234,
        "transient_text": body,
        "received_at": "2026-08-29T00:00:00Z",
        "content_hash": content_sha256,
        "_raw_content": body,
        "_raw_metadata": "{}",
    }


def _file_token(raw: dict[str, object] | None = None) -> AuthorizedFileSnapshotToken:
    source = raw or _file_raw()
    token = authorized_file_snapshot_token(source, content_sha256=str(source["content_hash"]))
    assert token is not None
    return token


def _budget() -> InheritedTurnBudget:
    return InheritedTurnBudget(
        safety_deadline=TurnSafetyDeadline(_DEADLINE_NS),
        model_anti_loop=ModelAntiLoopBudget(max_model_calls=4, max_model_retries=1),
        resources=TurnResourceBudget(
            max_tool_calls=4,
            max_tool_rounds=2,
            max_advisory_calls=2,
            max_output_tokens=8192,
        ),
    )


def _sources(
    issuer: TurnContextIssuer,
    authority: object,
    *,
    with_attachment: bool,
    raw_source: dict[str, object] | None = None,
) -> tuple[AuthorizedSourceIdentity, ...]:
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(authority) is AuthenticatedIngressAuthority
    values = [issuer.accepted_ingress_source(authority)]
    if with_attachment:
        source = raw_source or _file_raw()
        values.append(
            issuer.registered_file_source(
                authority=authority,
                ordinal=1,
                token=_file_token(source),
                raw_source=source,
            )
        )
    return tuple(
        sorted(
            values,
            key=lambda item: (
                0 if item.kind.value == "accepted_ingress" else 1,
                item.ordinal or 0,
                item.kind.value,
                item.identity_sha256,
            ),
        )
    )


def _context(
    *,
    issuer: TurnContextIssuer | None = None,
    actor: ActorContext | None = None,
    conversation_id: str | None = _CONVERSATION_ID,
    interaction_mode: TurnMode = TurnMode.DIALOGUE,
    ingress_token: str = "accepted-ingress-0001",
    source_id: str = "source-0001",
    update_id: str = "update-0001",
    request_binding: str | None = _REQUEST_BINDING,
    message: str = "Составь краткий план.",
    reply: str = "Предыдущая безопасная цитата",
    enable_tools: bool = True,
    with_attachment: bool = True,
    decision: TurnPolicyDecision | None = None,
    pending: PendingDurableTurnAdmission | None = None,
) -> AuthenticatedTurnContext:
    effective_issuer = issuer or TurnContextIssuer(_KEY)
    effective_actor = actor or _actor()
    authority = effective_issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=ingress_token,
        actor=effective_actor,
        conversation_id=conversation_id,
        interaction_mode=interaction_mode,
        source_id=source_id,
        update_id=update_id,
        request_effect_binding_sha256=request_binding,
    )
    if pending is None:
        router_mode = RouterMode.V12
        fallback = RouterMode.LEGACY
        pending_binding = None
    else:
        router_mode = RouterMode.LEGACY
        fallback = None
        pending_binding = effective_issuer.bind_pending_work(
            authority=authority,
            admission=pending,
        )
    policy = effective_issuer.issue_turn_policy(
        router_mode=router_mode,
        fallback_router_mode=fallback,
        decision=decision or TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    raw_source = _file_raw() if with_attachment else None
    model_input = _model_input(
        effective_actor,
        conversation_id=conversation_id,
        message=message,
        reply=reply,
        mode=interaction_mode,
        enable_tools=enable_tools,
        with_attachment=with_attachment,
        attachment_raw=raw_source,
    )
    budget = _budget()
    if not enable_tools:
        budget = dataclasses.replace(
            budget,
            resources=TurnResourceBudget(0, 0, 2, 8192),
        )
    return effective_issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=_sources(
            effective_issuer,
            authority,
            with_attachment=with_attachment,
            raw_source=raw_source,
        ),
        turn_policy=policy,
        inherited_budget=budget,
        pending_work_admission=pending_binding,
        now_monotonic_ns=_NOW_NS,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_context_is_immutable_and_composes_the_exact_existing_contracts() -> None:
    context = _context()

    assert context.model_payload() == context.model_input.model_payload()
    assert context.authority.actor == _actor()
    assert context.authority.tenant_id == "tenant-alice"
    assert context.authority.person_id == "tenant-alice"
    assert context.authority.conversation_id == _CONVERSATION_ID
    assert context.authority.interaction_mode is TurnMode.DIALOGUE
    assert context.turn_policy.decision.intent is TurnIntent.PASSTHROUGH
    assert not hasattr(context, "__dict__")
    assert "Составь краткий план" not in repr(context)
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.context_authority_sha256 = "0" * 64  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.model_input.message = "drift"  # type: ignore[misc]


def test_same_accepted_ingress_is_restart_deterministic() -> None:
    first = _context(issuer=TurnContextIssuer(_KEY))
    restarted = _context(issuer=TurnContextIssuer(bytes(_KEY)))

    assert first.identity == restarted.identity
    assert first.turn_id == restarted.turn_id
    assert first.authority == restarted.authority
    assert first.canonical_bytes() == restarted.canonical_bytes()
    assert first.canonical_sha256() == restarted.canonical_sha256()


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("ingress_token", "accepted-ingress-0002"),
        ("conversation_id", "conv_fedcba9876543210"),
        ("source_id", "source-0002"),
        ("update_id", "update-0002"),
        ("request_binding", "3" * 64),
    ],
)
def test_ingress_scope_drift_changes_turn_identity(changed: str, value: str) -> None:
    baseline = _context(with_attachment=False)
    if changed == "ingress_token":
        candidate = _context(ingress_token=value, with_attachment=False)
    elif changed == "conversation_id":
        candidate = _context(conversation_id=value, with_attachment=False)
    elif changed == "source_id":
        candidate = _context(source_id=value, with_attachment=False)
    elif changed == "update_id":
        candidate = _context(update_id=value, with_attachment=False)
    else:
        candidate = _context(request_binding=value, with_attachment=False)

    assert baseline.turn_id != candidate.turn_id


def test_actor_tenant_person_preset_and_session_drift_change_identity() -> None:
    candidates = (
        _actor(),
        _actor(identity_id="principal-bob"),
        _actor(tenant_id="tenant-bob"),
        _actor(preset_key="guest"),
        _actor(session_id="session-b"),
        _actor(
            tenant_id="shared-tenant",
            identity_id="principal-alice",
            shared=True,
            person_id="person-alice",
        ),
        _actor(
            tenant_id="shared-tenant",
            identity_id="principal-bob",
            shared=True,
            person_id="person-bob",
        ),
    )
    turn_ids = {_context(actor=actor, with_attachment=False).turn_id for actor in candidates}

    assert len(turn_ids) == len(candidates)


def test_same_turn_identity_binds_changed_model_input_to_a_different_context_and_fence() -> None:
    first = _context(message="first", reply="one", with_attachment=False)
    drifted = _context(message="second", reply="two", enable_tools=False, with_attachment=False)

    assert first.turn_id == drifted.turn_id
    assert first.model_input_binding_sha256 != drifted.model_input_binding_sha256
    assert first.context_authority_sha256 != drifted.context_authority_sha256
    assert first.canonical_sha256() != drifted.canonical_sha256()
    assert first.effect_fence.binding_sha256 != drifted.effect_fence.binding_sha256


def test_primary_model_payload_is_unchanged_and_contains_no_internal_identity() -> None:
    pending = PendingDurableTurnAdmission.owned(
        person_id="tenant-alice",
        conversation_id=_CONVERSATION_ID,
        work_item_id="work_0123456789abcdef",
        revision=3,
    )
    context = _context(pending=pending)
    payload = context.model_payload()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload == context.model_input.model_payload()
    assert _all_keys(payload).isdisjoint(
        {
            "actor_id",
            "tenant_id",
            "person_id",
            "source_id",
            "update_id",
            "turn_id",
            "pending_work_admission",
            "work_item_id",
            "work_graph_id",
            "path",
        }
    )
    for private_value in (
        "tenant-alice",
        "principal-alice",
        "source-0001",
        "update-0001",
        "work_0123456789abcdef",
        "/srv/private/customer-plan.pdf",
        "PRIVATE SOURCE BODY",
    ):
        assert private_value not in encoded


def test_canonical_spine_is_body_free_but_binds_policy_sources_and_pending() -> None:
    decision = TurnPolicyDecision(
        intent=TurnIntent.LOCAL_DIAGNOSTICS,
        public_response="PRIVATE CODE-OWNED RESPONSE BODY",
    )
    context = _context(decision=decision)
    canonical = context.canonical_bytes().decode("ascii")

    for private_value in (
        "Составь краткий план",
        "Предыдущая безопасная цитата",
        "PRIVATE SOURCE BODY",
        "PRIVATE CODE-OWNED RESPONSE BODY",
        "/srv/private/customer-plan.pdf",
        "tenant-alice",
        "principal-alice",
        "source-0001",
        "update-0001",
    ):
        assert private_value not in canonical
    assert "model_input_binding_sha256" in canonical
    assert "decision_binding_sha256" in canonical


def test_advisory_projection_has_no_authority_tools_effects_or_publication() -> None:
    context = _context()
    projection = context.advisory_projection()
    keys = _all_keys(projection)

    assert projection["advisory_only"] is True
    assert "message" in keys
    assert keys.isdisjoint(
        {
            "authority",
            "enable_tools",
            "effect_fence",
            "effect_owner",
            "final_publisher",
            "authorized_sources",
            "pending_work_admission",
            "turn_id",
            "actor_id",
            "tenant_id",
            "person_id",
        }
    )


def test_budget_children_cannot_extend_parent_and_limits_stay_separate() -> None:
    parent = _budget()
    child = parent.derive_child(
        safety_deadline_monotonic_ns=_DEADLINE_NS + 999_999,
        max_model_calls=64,
        max_model_retries=16,
        max_tool_calls=64,
        max_tool_rounds=32,
        max_advisory_calls=16,
        max_output_tokens=1_000_000,
    )

    assert child.safety_deadline.monotonic_ns == parent.safety_deadline.monotonic_ns
    assert child.model_anti_loop == parent.model_anti_loop
    assert child.resources == parent.resources
    assert type(parent.safety_deadline) is TurnSafetyDeadline
    assert type(parent.model_anti_loop) is ModelAntiLoopBudget


def test_effect_fence_carries_real_request_binding_and_full_context_authority() -> None:
    context = _context()

    assert context.effect_fence.turn_id == context.turn_id
    assert context.effect_fence.context_authority_sha256 == context.context_authority_sha256
    assert context.effect_fence.request_effect_binding_sha256 == _REQUEST_BINDING
    assert context.effect_fence.effect_owner is EffectOwner.PRIMARY
    assert context.effect_fence.final_publisher is FinalPublisher.PRIMARY
    assert tuple(EffectOwner) == (EffectOwner.PRIMARY,)
    assert tuple(FinalPublisher) == (FinalPublisher.PRIMARY,)


def test_pending_work_wraps_the_existing_single_owner_and_hides_raw_identity() -> None:
    admission = PendingDurableTurnAdmission.owned(
        person_id="tenant-alice",
        conversation_id=_CONVERSATION_ID,
        work_item_id="work_0123456789abcdef",
        revision=7,
    )
    context = _context(pending=admission)
    pending = context.pending_work_admission

    assert pending is not None
    assert pending.admission is admission
    assert pending.owner_kind is PendingOwnerKind.WORK_ITEM
    assert context.turn_policy.router_mode is RouterMode.LEGACY
    assert "work_0123456789abcdef" not in context.canonical_bytes().decode("ascii")
    with pytest.raises(ValueError, match="binding is invalid"):
        PendingDurableTurnAdmission.owned(
            person_id="tenant-alice",
            conversation_id=_CONVERSATION_ID,
            work_item_id="work_0123456789abcdef",
            work_graph_id="graph_0123456789abcdef",
            revision=1,
        )


def test_new_conversation_has_one_typed_target_without_a_fake_id() -> None:
    context = _context(conversation_id=None, with_attachment=False)

    assert context.authority.conversation.kind is ConversationScopeKind.NEW
    assert context.authority.conversation_id is None
    assert context.model_input.conversation_present is False
    assert context.authority.conversation.binding_sha256 in context.canonical_bytes().decode("ascii")


def test_canonical_serialization_accepts_only_its_unique_exact_bytes() -> None:
    context = _context()
    canonical = context.canonical_bytes()

    context.verify_canonical_bytes(canonical)
    noncanonical = json.dumps(
        context.canonical_payload(),
        ensure_ascii=True,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("ascii")
    for malformed in (
        b" " + canonical,
        noncanonical,
        b'{"schema":"duplicate",' + canonical[1:],
        canonical[:-1] + b',"extra":true}',
        canonical + b" trailing",
        b"\xff",
        b"{" + b" " * 32_768 + b"}",
    ):
        with pytest.raises(TurnContextError, match="canonical turn context JSON is invalid"):
            context.verify_canonical_bytes(malformed)


@pytest.mark.parametrize("invalid_number", [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"1.0"])
def test_canonical_serialization_rejects_nonfinite_and_float_numbers(invalid_number: bytes) -> None:
    context = _context()
    malformed = context.canonical_bytes().replace(
        b'"max_tool_calls":4',
        b'"max_tool_calls":' + invalid_number,
    )

    with pytest.raises(TurnContextError, match="canonical turn context JSON is invalid"):
        context.verify_canonical_bytes(malformed)


@pytest.mark.parametrize(
    "turn_id",
    ["", "turn_0", "TURN_" + "0" * 64, "turn_" + "A" * 64, "turn_" + "0" * 65],
)
def test_malformed_and_oversized_turn_identities_are_rejected(turn_id: str) -> None:
    with pytest.raises(TurnContextError, match="turn_id is invalid"):
        TurnIdentity(turn_id=turn_id, authority_sha256="0" * 64)


def test_trusted_consumption_boundary_rejects_another_issuer() -> None:
    trusted = TurnContextIssuer(_KEY)
    rogue = TurnContextIssuer(b"z" * 32)
    context = _context(issuer=rogue)

    assert rogue.require_context(context) is context
    with pytest.raises(TurnContextError, match="another issuer"):
        trusted.require_context(context)
