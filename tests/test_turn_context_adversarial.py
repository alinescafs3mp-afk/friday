from __future__ import annotations

import dataclasses
import inspect
import json
from typing import Any

import pytest

from friday.file_evidence import stamp_current_turn_file_reference
from friday.orchestration.contracts import AttachmentDescriptor, RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceIdentity,
    AuthorizedSourceKind,
    EffectFence,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    PendingOwnerKind,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.source_identity import AuthorizedFileSnapshotToken, authorized_file_snapshot_token
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_KEY = b"authenticated-turn-context-key!!"
_NOW_NS = 20_000_000_000_000
_DEADLINE_NS = _NOW_NS + 120_000_000_000
_CONVERSATION = "conv_0123456789abcdef"


def _actor(
    user_id: str = "alice",
    *,
    preset: str = "owner",
    session: str = "session-a",
    shared: bool = False,
    person: str = "",
) -> ActorContext:
    return ActorContext(
        user_id=user_id,
        preset_key=preset,
        source="api-token",
        identity_id=f"principal-{person or user_id}",
        session_id=session,
        shared_tenant=shared,
        person_id=person,
    )


def _input(
    actor: ActorContext,
    *,
    message: str = "ordinary request",
    reply: str = "",
    mode: TurnMode = TurnMode.DIALOGUE,
    enable_tools: bool = True,
    conversation_id: str | None = _CONVERSATION,
    attachment: bool = False,
    attachment_raw: dict[str, object] | None = None,
) -> TurnInput:
    return TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=conversation_id,
        attachments=([attachment_raw or _raw_source()] if attachment else []),
        enable_tools=enable_tools,
        synthetic_document_notice=False,
        mode=mode.value,
        reply_to=reply,
        quoted_attachment_reference=attachment,
        reply_assistant_reference=False,
    )


def _raw_source(
    *,
    raw_id: str = "raw_0123456789abcdef",
    source_ref: str = "source-a",
    source_identity_material: str = "body-a",
    content_sha256: str = "a" * 64,
    content_type: str = "text/plain",
) -> dict[str, object]:
    return {
        "id": raw_id,
        "source": "api",
        "source_ref": source_ref,
        "content_type": content_type,
        "mime_type": content_type,
        "content": source_identity_material,
        "received_at": "2026-08-29T00:00:00Z",
        "content_hash": content_sha256,
        "_raw_content": source_identity_material,
        "_raw_metadata": "{}",
    }


def _token(raw: dict[str, object] | None = None) -> AuthorizedFileSnapshotToken:
    source = raw or _raw_source()
    token = authorized_file_snapshot_token(
        source,
        content_sha256=str(source["content_hash"]),
    )
    assert token is not None
    return token


def _budget(deadline: int = _DEADLINE_NS) -> InheritedTurnBudget:
    return InheritedTurnBudget(
        TurnSafetyDeadline(deadline),
        ModelAntiLoopBudget(4, 1),
        TurnResourceBudget(4, 2, 1, 8192),
    )


def _authority(
    issuer: TurnContextIssuer,
    actor: ActorContext,
    *,
    token: str = "accepted-01",
    conversation_id: str | None = _CONVERSATION,
    mode: TurnMode = TurnMode.DIALOGUE,
    source_id: str = "source-01",
    update_id: str = "update-01",
    request_binding: str | None = "b" * 64,
) -> object:
    return issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=token,
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=mode,
        source_id=source_id,
        update_id=update_id,
        request_effect_binding_sha256=request_binding,
    )


def _mint(
    *,
    issuer: TurnContextIssuer | None = None,
    actor: ActorContext | None = None,
    message: str = "ordinary request",
    reply: str = "",
    mode: TurnMode = TurnMode.DIALOGUE,
    attachment: bool = False,
    pending: PendingDurableTurnAdmission | None = None,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    effective_issuer = issuer or TurnContextIssuer(_KEY)
    effective_actor = actor or _actor()
    authority = _authority(effective_issuer, effective_actor, mode=mode)
    assert type(authority) is AuthenticatedIngressAuthority
    raw_source = _raw_source() if attachment else None
    sources = [effective_issuer.accepted_ingress_source(authority)]
    if attachment:
        assert raw_source is not None
        sources.append(
            effective_issuer.registered_file_source(
                authority=authority,
                ordinal=1,
                token=_token(raw_source),
                raw_source=raw_source,
            )
        )
    sources.sort(
        key=lambda item: (
            0 if item.kind is AuthorizedSourceKind.ACCEPTED_INGRESS else 1,
            item.ordinal or 0,
            item.kind.value,
            item.identity_sha256,
        )
    )
    if pending is None:
        router_mode = RouterMode.V12
        fallback = RouterMode.LEGACY
        pending_binding = None
    else:
        router_mode = RouterMode.LEGACY
        fallback = None
        pending_binding = effective_issuer.bind_pending_work(authority=authority, admission=pending)
    policy = effective_issuer.issue_turn_policy(
        router_mode=router_mode,
        fallback_router_mode=fallback,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = effective_issuer.authenticate_turn(
        authority=authority,
        model_input=_input(
            effective_actor,
            message=message,
            reply=reply,
            mode=mode,
            attachment=attachment,
            attachment_raw=raw_source,
        ),
        authorized_sources=tuple(sources),
        turn_policy=policy,
        inherited_budget=_budget(),
        pending_work_admission=pending_binding,
        now_monotonic_ns=_NOW_NS,
    )
    return effective_issuer, context


class _ActorSubclass(ActorContext):
    pass


class _TurnInputSubclass(TurnInput):
    pass


class _AttachmentCarrier(dict[str, object]):
    pass


def test_exact_actor_and_turn_input_types_are_required() -> None:
    issuer, context = _mint()
    with pytest.raises(TurnContextError, match="exact authenticated ActorContext"):
        _authority(issuer, _ActorSubclass("alice", "owner", "api-token"))

    subclass = _TurnInputSubclass(
        **{item.name: getattr(context.model_input, item.name) for item in dataclasses.fields(TurnInput)}
    )
    with pytest.raises(TurnContextError, match="exact TurnInput"):
        issuer.authenticate_turn(
            authority=context.authority,
            model_input=subclass,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=context.inherited_budget,
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )


def test_all_private_seals_are_frozen_and_canonical_bytes_cannot_mutate() -> None:
    _, context = _mint(attachment=True)
    before = context.canonical_bytes()

    for sealed in (
        context,
        context.authority,
        context.authority.conversation,
        context.authorized_sources[0],
        context.turn_policy,
        context.effect_fence,
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            sealed._seal.payload_sha256 = "0" * 64  # type: ignore[attr-defined,misc]
    assert context.canonical_bytes() == before


def test_dataclass_replace_cannot_drift_authenticated_components() -> None:
    _, context = _mint(attachment=True)
    with pytest.raises(TurnContextError, match="not issued"):
        dataclasses.replace(context.authority, update_binding_sha256="0" * 64)
    with pytest.raises(TurnContextError, match="not issued"):
        dataclasses.replace(context.authorized_sources[0], identity_sha256="0" * 64)
    with pytest.raises(TurnContextError, match="not issued"):
        dataclasses.replace(context, model_input_binding_sha256="0" * 64)


def test_trusted_issuer_rejects_a_self_consistent_rogue_context() -> None:
    trusted = TurnContextIssuer(_KEY)
    rogue = TurnContextIssuer(b"r" * 32)
    _, context = _mint(issuer=rogue)

    with pytest.raises(TurnContextError, match="another issuer"):
        trusted.require_context(context)


def test_message_reply_tools_and_attachment_drift_change_body_free_context_binding() -> None:
    issuer, baseline = _mint()
    variants = [
        _mint(message="different message")[1],
        _mint(reply="different reply")[1],
        _mint()[1],
        _mint(attachment=True)[1],
    ]
    # Rebuild the third variant against the same authority with tools disabled.
    tools_off = dataclasses.replace(baseline.model_input, enable_tools=False)
    variants[2] = issuer.authenticate_turn(
        authority=baseline.authority,
        model_input=tools_off,
        authorized_sources=baseline.authorized_sources,
        turn_policy=baseline.turn_policy,
        inherited_budget=dataclasses.replace(
            baseline.inherited_budget,
            resources=TurnResourceBudget(0, 0, 1, 8192),
        ),
        pending_work_admission=None,
        now_monotonic_ns=_NOW_NS,
    )

    assert {item.turn_id for item in variants} == {baseline.turn_id}
    assert (
        len({baseline.context_authority_sha256, *(item.context_authority_sha256 for item in variants)}) == 5
    )
    for context in variants:
        canonical = context.canonical_bytes().decode("ascii")
        assert context.model_input.message not in canonical
        if context.model_input.reply_quote:
            assert context.model_input.reply_quote not in canonical


def test_mode_and_attachment_cardinality_drift_are_rejected_not_reclassified() -> None:
    issuer, context = _mint()
    mode_drift = dataclasses.replace(context.model_input, conversation_mode="engineer")
    with pytest.raises(TurnContextError, match="mode differs"):
        issuer.authenticate_turn(
            authority=context.authority,
            model_input=mode_drift,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=context.inherited_budget,
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )

    added = dataclasses.replace(
        context.model_input,
        attachments=(AttachmentDescriptor(1, "attachment-1", "text", None, True),),
    )
    with pytest.raises(TurnContextError, match="attachment identities differ"):
        issuer.authenticate_turn(
            authority=context.authority,
            model_input=added,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=context.inherited_budget,
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )


def test_owner_guest_session_and_shared_actor_authority_do_not_collide() -> None:
    actors = (
        _actor(),
        _actor(preset="guest"),
        _actor(session="session-b"),
        _actor("shared", shared=True, person="alice"),
        _actor("shared", shared=True, person="bob"),
    )
    contexts = tuple(_mint(actor=actor)[1] for actor in actors)

    assert len({item.authority.actor_binding_sha256 for item in contexts}) == len(contexts)
    assert len({item.turn_id for item in contexts}) == len(contexts)


def test_plain_string_and_lookalike_cannot_mint_source_authority() -> None:
    issuer, context = _mint()
    assert not hasattr(issuer, "issue_authorized_source")
    with pytest.raises(TurnContextError, match="process-owned token"):
        issuer.registered_file_source(
            authority=context.authority,
            ordinal=1,
            token=object(),  # type: ignore[arg-type]
            raw_source=_raw_source(),
        )
    with pytest.raises(TurnContextError, match="not issued"):
        AuthorizedSourceIdentity(
            kind=AuthorizedSourceKind.REGISTERED_FILE,
            ordinal=1,
            turn_authority_sha256=context.authority.canonical_sha256(),
            identity_sha256="0" * 64,
            model_descriptor_sha256="1" * 64,
            private_carrier="arbitrary caller string",
            _seal=object(),  # type: ignore[arg-type]
        )


def test_source_identity_binds_exact_raw_source_content_and_turn_scope() -> None:
    issuer = TurnContextIssuer(_KEY)
    actor = _actor()
    first_authority = _authority(issuer, actor, token="accepted-01")
    second_authority = _authority(issuer, actor, token="accepted-02")
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(first_authority) is AuthenticatedIngressAuthority
    assert type(second_authority) is AuthenticatedIngressAuthority
    raw_a = _raw_source()
    raw_b = _raw_source(source_ref="source-b")
    raw_c = _raw_source(content_sha256="c" * 64)
    sources = (
        issuer.registered_file_source(
            authority=first_authority,
            ordinal=1,
            token=_token(raw_a),
            raw_source=raw_a,
        ),
        issuer.registered_file_source(
            authority=first_authority,
            ordinal=1,
            token=_token(raw_b),
            raw_source=raw_b,
        ),
        issuer.registered_file_source(
            authority=first_authority,
            ordinal=1,
            token=_token(raw_c),
            raw_source=raw_c,
        ),
        issuer.registered_file_source(
            authority=second_authority,
            ordinal=1,
            token=_token(raw_a),
            raw_source=raw_a,
        ),
    )

    assert len({item.identity_sha256 for item in sources}) == len(sources)


def test_registered_source_rejects_snapshot_or_model_descriptor_substitution() -> None:
    issuer = TurnContextIssuer(_KEY)
    actor = _actor()
    authority = _authority(issuer, actor)
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(authority) is AuthenticatedIngressAuthority
    text_raw = _raw_source()
    pdf_raw = _raw_source(
        raw_id="raw_fedcba9876543210",
        source_ref="source-pdf",
        content_sha256="d" * 64,
        content_type="application/pdf",
    )
    with pytest.raises(TurnContextError, match="process-owned snapshot"):
        issuer.registered_file_source(
            authority=authority,
            ordinal=1,
            token=_token(text_raw),
            raw_source=pdf_raw,
        )
    wrong_source = issuer.registered_file_source(
        authority=authority,
        ordinal=1,
        token=_token(pdf_raw),
        raw_source=pdf_raw,
    )
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.V12,
        fallback_router_mode=RouterMode.LEGACY,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    with pytest.raises(TurnContextError, match="differs from its TurnInput descriptor"):
        issuer.authenticate_turn(
            authority=authority,
            model_input=_input(actor, attachment=True, attachment_raw=text_raw),
            authorized_sources=(issuer.accepted_ingress_source(authority), wrong_source),
            turn_policy=policy,
            inherited_budget=_budget(),
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )


def test_current_attachment_source_is_bound_to_the_exact_model_descriptor() -> None:
    issuer = TurnContextIssuer(_KEY)
    actor = _actor()
    authority = _authority(issuer, actor)
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(authority) is AuthenticatedIngressAuthority
    raw = {
        "id": "raw_0123456789abcdef",
        "source": "api",
        "source_ref": "current-source",
        "content_type": "text/plain",
        "received_at": "2026-08-29T00:00:00Z",
        "content_hash": "e" * 64,
        "raw_content": "CURRENT_SOURCE_BODY",
        "metadata_json": "{}",
    }
    carrier = _AttachmentCarrier(
        raw_object_id=raw["id"],
        mime_type="text/plain",
        content="CURRENT_SOURCE_BODY",
    )
    stamp_current_turn_file_reference(carrier, raw)
    source = issuer.current_attachment_source(authority=authority, ordinal=1, carrier=carrier)
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.V12,
        fallback_router_mode=RouterMode.LEGACY,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=TurnInput.from_chat(
            message="read it",
            actor=actor,
            conversation_id=_CONVERSATION,
            attachments=[carrier],
            enable_tools=True,
            synthetic_document_notice=False,
            mode="dialogue",
            reply_to=None,
            quoted_attachment_reference=False,
            reply_assistant_reference=False,
        ),
        authorized_sources=(issuer.accepted_ingress_source(authority), source),
        turn_policy=policy,
        inherited_budget=_budget(),
        pending_work_admission=None,
        now_monotonic_ns=_NOW_NS,
    )
    assert context.authorized_sources[1].model_descriptor_sha256 is not None

    drifted = _AttachmentCarrier(carrier)
    drifted["mime_type"] = "application/pdf"
    with pytest.raises(TurnContextError, match="differs from its TurnInput descriptor"):
        issuer.authenticate_turn(
            authority=authority,
            model_input=TurnInput.from_chat(
                message="read it",
                actor=actor,
                conversation_id=_CONVERSATION,
                attachments=[drifted],
                enable_tools=True,
                synthetic_document_notice=False,
                mode="dialogue",
                reply_to=None,
                quoted_attachment_reference=False,
                reply_assistant_reference=False,
            ),
            authorized_sources=context.authorized_sources,
            turn_policy=policy,
            inherited_budget=_budget(),
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )


def test_source_from_one_turn_cannot_be_grafted_to_another_actor() -> None:
    issuer, alice = _mint(attachment=True)
    bob_actor = _actor("bob")
    bob_authority = _authority(issuer, bob_actor, token="accepted-bob")
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(bob_authority) is AuthenticatedIngressAuthority
    bob_policy = issuer.issue_turn_policy(
        router_mode=RouterMode.V12,
        fallback_router_mode=RouterMode.LEGACY,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    with pytest.raises(TurnContextError, match="binding is stale|another turn authority|carrier is stale"):
        issuer.authenticate_turn(
            authority=bob_authority,
            model_input=_input(bob_actor, attachment=True),
            authorized_sources=alice.authorized_sources,
            turn_policy=bob_policy,
            inherited_budget=_budget(),
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )


def test_pending_scope_owner_and_uncertainty_are_exact() -> None:
    issuer, context = _mint()
    foreign = PendingDurableTurnAdmission.owned(
        person_id="bob",
        conversation_id=_CONVERSATION,
        work_item_id="work_0123456789abcdef",
        revision=1,
    )
    with pytest.raises(TurnContextError, match="another turn scope"):
        issuer.bind_pending_work(authority=context.authority, admission=foreign)

    uncertain = PendingDurableTurnAdmission.uncertain(
        person_id="alice",
        conversation_id=_CONVERSATION,
    )
    unbound = PendingDurableTurnAdmission.owned(
        person_id="alice",
        conversation_id=_CONVERSATION,
    )
    assert (
        issuer.bind_pending_work(
            authority=context.authority,
            admission=uncertain,
        ).owner_kind
        is PendingOwnerKind.UNCERTAIN_FAIL_CLOSED
    )
    assert (
        issuer.bind_pending_work(
            authority=context.authority,
            admission=unbound,
        ).owner_kind
        is PendingOwnerKind.LEGACY_PENDING_RUNTIME
    )


def test_identical_pending_id_under_another_person_has_another_opaque_binding() -> None:
    issuer = TurnContextIssuer(_KEY)
    alice_authority = _authority(issuer, _actor(), token="accepted-a")
    bob_authority = _authority(issuer, _actor("bob"), token="accepted-b")
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(alice_authority) is AuthenticatedIngressAuthority
    assert type(bob_authority) is AuthenticatedIngressAuthority
    alice = issuer.bind_pending_work(
        authority=alice_authority,
        admission=PendingDurableTurnAdmission.owned(
            person_id="alice",
            conversation_id=_CONVERSATION,
            work_item_id="work_0123456789abcdef",
            revision=1,
        ),
    )
    bob = issuer.bind_pending_work(
        authority=bob_authority,
        admission=PendingDurableTurnAdmission.owned(
            person_id="bob",
            conversation_id=_CONVERSATION,
            work_item_id="work_0123456789abcdef",
            revision=1,
        ),
    )

    assert alice.scope_binding_sha256 != bob.scope_binding_sha256
    assert alice.owner_binding_sha256 != bob.owner_binding_sha256


def test_two_pending_owners_and_untyped_pending_sequence_are_rejected() -> None:
    with pytest.raises(ValueError, match="binding is invalid"):
        PendingDurableTurnAdmission.owned(
            person_id="alice",
            conversation_id=_CONVERSATION,
            work_item_id="work_0123456789abcdef",
            work_graph_id="graph_0123456789abcdef",
            revision=1,
        )
    issuer, context = _mint()
    with pytest.raises(TurnContextError, match="pending work admission belongs to another issuer"):
        issuer.authenticate_turn(
            authority=context.authority,
            model_input=context.model_input,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=context.inherited_budget,
            pending_work_admission=(object(), object()),  # type: ignore[arg-type]
            now_monotonic_ns=_NOW_NS,
        )


def test_pending_owner_cannot_coexist_with_v12_strategy() -> None:
    issuer, context = _mint()
    admission = PendingDurableTurnAdmission.owned(
        person_id="alice",
        conversation_id=_CONVERSATION,
        work_item_id="work_0123456789abcdef",
        revision=1,
    )
    pending = issuer.bind_pending_work(authority=context.authority, admission=admission)
    with pytest.raises(TurnContextError, match="legacy continuation owner"):
        issuer.authenticate_turn(
            authority=context.authority,
            model_input=context.model_input,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=context.inherited_budget,
            pending_work_admission=pending,
            now_monotonic_ns=_NOW_NS,
        )


@pytest.mark.parametrize(
    ("deadline", "now"),
    [
        (1, 1),
        (_NOW_NS, _NOW_NS),
        (_NOW_NS - 1, _NOW_NS),
        (_NOW_NS + 3_600_000_000_001, _NOW_NS),
    ],
)
def test_expired_or_renewed_safety_deadline_is_rejected(deadline: int, now: int) -> None:
    issuer, context = _mint()
    with pytest.raises(TurnContextError, match="expired or exceeds"):
        issuer.authenticate_turn(
            authority=context.authority,
            model_input=context.model_input,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=_budget(deadline),
            pending_work_admission=None,
            now_monotonic_ns=now,
        )


def test_trusted_live_boundary_rejects_context_after_parent_deadline() -> None:
    issuer, context = _mint()

    assert issuer.require_live_context(context, now_monotonic_ns=_DEADLINE_NS - 1) is context
    assert issuer.require_context(context) is context
    with pytest.raises(TurnContextError, match="deadline has expired"):
        issuer.require_live_context(context, now_monotonic_ns=_DEADLINE_NS)


def test_tool_capability_requires_effect_binding_and_closed_mode_budget() -> None:
    issuer = TurnContextIssuer(_KEY)
    actor = _actor()
    authority = _authority(issuer, actor, request_binding=None)
    from friday.orchestration.turn_context import AuthenticatedIngressAuthority

    assert type(authority) is AuthenticatedIngressAuthority
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.V12,
        fallback_router_mode=RouterMode.LEGACY,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    sources = (issuer.accepted_ingress_source(authority),)
    with pytest.raises(TurnContextError, match="requires the admitted request effect binding"):
        issuer.authenticate_turn(
            authority=authority,
            model_input=_input(actor),
            authorized_sources=sources,
            turn_policy=policy,
            inherited_budget=_budget(),
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )

    tools_off = issuer.authenticate_turn(
        authority=authority,
        model_input=_input(actor, enable_tools=False),
        authorized_sources=sources,
        turn_policy=policy,
        inherited_budget=dataclasses.replace(
            _budget(),
            resources=TurnResourceBudget(0, 0, 1, 8192),
        ),
        pending_work_admission=None,
        now_monotonic_ns=_NOW_NS,
    )
    assert tools_off.effect_fence.request_effect_binding_sha256 is None

    bound_authority = _authority(issuer, actor)
    assert type(bound_authority) is AuthenticatedIngressAuthority
    with pytest.raises(TurnContextError, match="mode ceiling"):
        issuer.authenticate_turn(
            authority=bound_authority,
            model_input=_input(actor),
            authorized_sources=(issuer.accepted_ingress_source(bound_authority),),
            turn_policy=policy,
            inherited_budget=dataclasses.replace(
                _budget(),
                resources=TurnResourceBudget(5, 2, 1, 8192),
            ),
            pending_work_admission=None,
            now_monotonic_ns=_NOW_NS,
        )


@pytest.mark.parametrize(
    "build",
    [
        lambda: TurnSafetyDeadline(True),
        lambda: TurnSafetyDeadline(float("inf")),  # type: ignore[arg-type]
        lambda: ModelAntiLoopBudget(True, 0),
        lambda: ModelAntiLoopBudget(2, 2),
        lambda: TurnResourceBudget(0, 0, 0, float("nan")),  # type: ignore[arg-type]
        lambda: TurnResourceBudget(4, 0, 0, 1),
        lambda: TurnResourceBudget(1, 2, 0, 1),
        lambda: InheritedTurnBudget(
            ModelAntiLoopBudget(2, 0),  # type: ignore[arg-type]
            ModelAntiLoopBudget(2, 0),
            TurnResourceBudget(0, 0, 0, 1),
        ),
    ],
)
def test_safety_deadline_anti_loop_and_resources_are_type_separated(build: Any) -> None:
    with pytest.raises(TurnContextError):
        build()


def test_secondary_advisory_projection_rejects_secrets_and_oversize() -> None:
    _, secret = _mint(message="OPENAI_API_KEY=abcdefghijklmnop")
    _, oversized = _mint(message="я" * 6_000)

    with pytest.raises(TurnContextError, match="not safe"):
        secret.advisory_projection()
    with pytest.raises(TurnContextError, match="not safe"):
        oversized.advisory_projection()


def test_canonical_parser_rejects_nested_duplicates_nonfinite_and_body_injection() -> None:
    _, context = _mint()
    malformed = (
        b'{"outer":{"duplicate":1,"duplicate":2}}',
        b'{"outer":{"number":NaN}}',
        context.canonical_bytes().replace(
            b'"identity_sha256":',
            b'"body":"AUTHORIZED_SOURCE_BODY_CANARY","identity_sha256":',
            1,
        ),
    )
    for raw in malformed:
        with pytest.raises(TurnContextError, match="canonical turn context JSON is invalid") as error:
            context.verify_canonical_bytes(raw)
        assert "AUTHORIZED_SOURCE_BODY_CANARY" not in str(error.value)


def test_turn_input_contract_remains_byte_and_signature_compatible() -> None:
    actor = _actor()
    turn = _input(actor, attachment=True)
    before = json.dumps(
        turn.model_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    _, context = _mint(actor=actor, attachment=True)
    after = json.dumps(
        context.model_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    assert context.model_input == turn
    assert before == after
    assert [item.name for item in dataclasses.fields(TurnInput)] == [
        "message",
        "message_truncated",
        "reply_quote",
        "reply_quote_truncated",
        "conversation_present",
        "conversation_mode",
        "enable_tools",
        "attachments",
        "attachments_truncated",
        "synthetic_document_notice",
        "quoted_attachment_reference",
        "reply_assistant_reference",
        "actor_is_owner",
        "shared_archive",
    ]
    assert list(inspect.signature(TurnInput.from_chat).parameters) == [
        "message",
        "actor",
        "conversation_id",
        "attachments",
        "enable_tools",
        "synthetic_document_notice",
        "mode",
        "reply_to",
        "quoted_attachment_reference",
        "reply_assistant_reference",
    ]


def test_effect_fence_requires_exact_typed_primary_owners() -> None:
    _, context = _mint()
    with pytest.raises(TurnContextError, match="owners must be primary"):
        EffectFence(
            turn_id=context.turn_id,
            context_authority_sha256=context.context_authority_sha256,
            request_effect_binding_sha256="b" * 64,
            effect_owner="primary",  # type: ignore[arg-type]
            final_publisher=context.effect_fence.final_publisher,
            binding_sha256="0" * 64,
            _seal=object(),  # type: ignore[arg-type]
        )
