from __future__ import annotations

import dataclasses
import json

import pytest

from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceIdentity,
    AuthorizedSourceKind,
    EffectOwner,
    FinalPublisher,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextIssuer,
    TurnPolicy,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext

_KEY = bytes(range(32))
_DEADLINE_MS = 2_000_000_000_000


def _actor(
    *,
    tenant_id: str = "tenant-alice",
    person_id: str = "",
    identity_id: str | None = "principal-alice",
    shared: bool = False,
) -> ActorContext:
    return ActorContext(
        user_id=tenant_id,
        preset_key="owner",
        source="test",
        identity_id=identity_id,
        shared_tenant=shared,
        person_id=person_id,
    )


def _turn_input(actor: ActorContext, *, conversation_id: str | None = "conv-main") -> TurnInput:
    return TurnInput.from_chat(
        message="Составь краткий план.",
        actor=actor,
        conversation_id=conversation_id,
        attachments=[
            {
                "filename": "/srv/private/customer-plan.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 1234,
                "transient_text": "PRIVATE SOURCE BODY",
            }
        ],
        enable_tools=True,
        synthetic_document_notice=False,
        mode="Dialogue",
        reply_to="Предыдущая безопасная цитата",
        quoted_attachment_reference=True,
        reply_assistant_reference=False,
    )


def _budget() -> InheritedTurnBudget:
    return InheritedTurnBudget(
        safety_deadline=TurnSafetyDeadline(_DEADLINE_MS),
        model_anti_loop=ModelAntiLoopBudget(max_model_calls=4, max_model_retries=1),
        resources=TurnResourceBudget(
            max_tool_calls=6,
            max_advisory_calls=2,
            max_output_tokens=8192,
        ),
    )


def _source_tuple(*sources: AuthorizedSourceIdentity) -> tuple[AuthorizedSourceIdentity, ...]:
    return tuple(sorted(sources, key=lambda item: (item.kind.value, item.identity_sha256)))


def _context(
    *,
    issuer: TurnContextIssuer | None = None,
    actor: ActorContext | None = None,
    conversation_id: str | None = "conv-main",
    ingress_token: str = "accepted-ingress-0001",
    source_id: str = "source-0001",
    update_id: str = "update-0001",
    pending: PendingDurableTurnAdmission | None = None,
    include_private_source: bool = True,
) -> AuthenticatedTurnContext:
    effective_issuer = issuer or TurnContextIssuer(_KEY)
    effective_actor = actor or _actor()
    authority = effective_issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=ingress_token,
        actor=effective_actor,
        conversation_id=conversation_id,
        source_id=source_id,
        update_id=update_id,
    )
    sources = [effective_issuer.accepted_ingress_source(authority)]
    if include_private_source:
        sources.append(
            effective_issuer.issue_authorized_source(
                kind=AuthorizedSourceKind.REGISTERED_FILE,
                code_owned_reference="/srv/private/customer-plan.pdf",
            )
        )
    return effective_issuer.authenticate_turn(
        authority=authority,
        model_input=_turn_input(effective_actor, conversation_id=conversation_id),
        authorized_sources=_source_tuple(*sources),
        turn_policy=TurnPolicy(
            router_mode=RouterMode.V12,
            fallback_router_mode=RouterMode.LEGACY,
        ),
        inherited_budget=_budget(),
        pending_work_admission=pending,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_context_is_immutable_and_wraps_the_exact_turn_input() -> None:
    actor = _actor()
    issuer = TurnContextIssuer(_KEY)
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token="accepted-ingress-0001",
        actor=actor,
        conversation_id="conv-main",
        source_id="source-0001",
        update_id="update-0001",
    )
    model_input = _turn_input(actor)
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=TurnPolicy(RouterMode.V12, RouterMode.LEGACY),
        inherited_budget=_budget(),
        pending_work_admission=None,
    )

    assert context.model_input is model_input
    assert context.model_payload() == model_input.model_payload()
    assert context.model_payload() is not model_input.model_payload()
    assert not hasattr(context, "__dict__")
    assert "Составь краткий план" not in repr(context)
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.identity = context.identity  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.model_input.message = "drift"  # type: ignore[misc]


def test_same_accepted_ingress_is_restart_deterministic() -> None:
    first = _context(issuer=TurnContextIssuer(_KEY))
    restarted = _context(issuer=TurnContextIssuer(bytes(_KEY)))

    assert first.identity == restarted.identity
    assert first.turn_id == restarted.turn_id
    assert first.authority == restarted.authority
    assert first.authorized_sources == restarted.authorized_sources
    assert first.canonical_bytes() == restarted.canonical_bytes()
    assert first.canonical_sha256() == restarted.canonical_sha256()


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("ingress_token", "accepted-ingress-0002"),
        ("conversation_id", "conv-other"),
        ("source_id", "source-0002"),
        ("update_id", "update-0002"),
    ],
)
def test_ingress_scope_drift_changes_turn_identity(changed: str, value: str) -> None:
    baseline = _context(include_private_source=False)
    if changed == "ingress_token":
        candidate = _context(include_private_source=False, ingress_token=value)
    elif changed == "conversation_id":
        candidate = _context(include_private_source=False, conversation_id=value)
    elif changed == "source_id":
        candidate = _context(include_private_source=False, source_id=value)
    else:
        candidate = _context(include_private_source=False, update_id=value)

    assert baseline.turn_id != candidate.turn_id


def test_actor_tenant_and_person_drift_change_turn_identity() -> None:
    baseline = _context(actor=_actor(), include_private_source=False)
    principal_drift = _context(
        actor=_actor(identity_id="principal-bob"),
        include_private_source=False,
    )
    tenant_drift = _context(
        actor=_actor(tenant_id="tenant-bob", identity_id="principal-alice"),
        include_private_source=False,
    )
    shared_alice = _context(
        actor=_actor(
            tenant_id="shared-tenant",
            person_id="person-alice",
            identity_id="principal-alice",
            shared=True,
        ),
        include_private_source=False,
    )
    shared_bob = _context(
        actor=_actor(
            tenant_id="shared-tenant",
            person_id="person-bob",
            identity_id="principal-bob",
            shared=True,
        ),
        include_private_source=False,
    )

    assert len(
        {
            baseline.turn_id,
            principal_drift.turn_id,
            tenant_drift.turn_id,
            shared_alice.turn_id,
            shared_bob.turn_id,
        }
    ) == 5


def test_primary_payload_is_unchanged_and_contains_no_internal_identity() -> None:
    pending = PendingDurableTurnAdmission.owned(
        person_id="tenant-alice",
        conversation_id="conv-main",
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


def test_canonical_spine_has_no_message_source_body_or_private_path() -> None:
    context = _context()
    canonical = context.canonical_bytes().decode("ascii")

    for private_value in (
        "Составь краткий план",
        "Предыдущая безопасная цитата",
        "PRIVATE SOURCE BODY",
        "/srv/private/customer-plan.pdf",
        "tenant-alice",
        "principal-alice",
        "source-0001",
        "update-0001",
    ):
        assert private_value not in canonical
    assert {item.name for item in dataclasses.fields(AuthorizedSourceIdentity)} == {
        "kind",
        "identity_sha256",
        "_seal",
    }


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
        safety_deadline_unix_ms=_DEADLINE_MS + 999_999,
        max_model_calls=64,
        max_model_retries=16,
        max_tool_calls=64,
        max_advisory_calls=16,
        max_output_tokens=1_000_000,
    )

    assert child.safety_deadline.unix_ms == parent.safety_deadline.unix_ms
    assert child.model_anti_loop == parent.model_anti_loop
    assert child.resources == parent.resources
    assert type(parent.safety_deadline) is TurnSafetyDeadline
    assert type(parent.model_anti_loop) is ModelAntiLoopBudget
    assert parent.safety_deadline is not parent.model_anti_loop


def test_effect_fence_has_one_primary_effect_owner_and_final_publisher() -> None:
    context = _context()

    assert context.effect_fence.turn_id == context.turn_id
    assert context.effect_fence.effect_owner is EffectOwner.PRIMARY
    assert context.effect_fence.final_publisher is FinalPublisher.PRIMARY
    assert tuple(EffectOwner) == (EffectOwner.PRIMARY,)
    assert tuple(FinalPublisher) == (FinalPublisher.PRIMARY,)
