"""Adversarial exact-message tests against a dedicated disposable database.

Only this module removes chat-immutability triggers, and only from the
``adversarial_storage`` database it creates itself.  Normal fixtures and the
released archive database shape are never weakened by these drift probes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import replace
from typing import Any

import pytest

from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.contracts import MessageRole
from friday.retrieval.message_exact_contract import (
    MessageExactContentMode,
    MessageExactContinuation,
    MessageExactContractError,
    MessageExactPublicationStatus,
    MessageExactRequest,
)
from friday.retrieval.message_exact_internal import MessageExactInternalAdapter
from friday.storage import FridayStorage
from friday.storage._message_exact_internal import (
    MessageExactStorageDrift,
    MessageExactStorageError,
)
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

OWNER = "message-exact-adversarial-owner"
BASE_TIME = "2026-09-01T10:00:00+00:00"


@pytest.fixture
def adversarial_storage(settings: Any, tmp_path: Any):  # noqa: ANN201
    database = tmp_path / "isolated-message-exact-adversarial.sqlite3"
    instance = FridayStorage(
        replace(
            settings,
            database_path=database,
            database_must_exist=False,
        )
    )
    try:
        yield instance
    finally:
        instance.close(final=True)


def _turn(
    actor: ActorContext,
    conversation_id: str,
    *,
    label: str,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    now = time.monotonic_ns()
    issuer = TurnContextIssuer(
        hashlib.sha256(f"message-exact-adversarial:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now,
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"message-exact-adversarial-ingress-{label}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"message-exact-adversarial-source-{label}",
        update_id=f"message-exact-adversarial-update-{label}",
        request_effect_binding_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message="adversarial exact current-conversation read",
        actor=actor,
        conversation_id=conversation_id,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.LEGACY,
        fallback_router_mode=None,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now + 60_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, 2, 16_384),
        ),
        pending_work_admission=None,
    )
    return issuer, context


def _adapter(
    storage: Any,
    conversation_id: str,
    *,
    label: str,
) -> tuple[AuthorizationService, MessageExactInternalAdapter, AuthenticatedTurnContext]:
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user(OWNER, source="message-exact-adversarial-test")
    issuer, context = _turn(actor, conversation_id, label=label)
    return authorization, MessageExactInternalAdapter(authorization, issuer), context


def _set_time(storage: Any, message_id: str, value: str) -> None:
    with storage.transaction() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE id=?", (value, message_id))


def _store(
    storage: Any,
    conversation_id: str,
    role: str,
    content: str,
    *,
    reply_to: str | None = None,
) -> dict[str, Any]:
    row = storage.store_message(
        conversation_id,
        OWNER,
        role,
        content,
        reply_to=reply_to,
    )
    _set_time(storage, str(row["id"]), BASE_TIME)
    return row


def _request(
    conversation_id: str,
    boundary_id: str,
    *,
    page_size: int = 2,
    continuation: MessageExactContinuation | None = None,
    roles: tuple[MessageRole, ...] = (MessageRole.ASSISTANT, MessageRole.USER),
    content_mode: MessageExactContentMode = MessageExactContentMode.EXCERPT,
    since: str | None = None,
    until: str | None = None,
) -> MessageExactRequest:
    return MessageExactRequest.create(
        conversation_id=conversation_id,
        accepted_boundary_user_message_id=boundary_id,
        page_size=page_size,
        continuation=continuation,
        roles=roles,
        content_mode=content_mode,
        since=since,
        until=until,
    )


def _seed_cursor(storage: Any) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    storage.ensure_user(OWNER, preset_key="owner")
    conversation = storage.create_conversation(OWNER, "cursor adversarial")
    rows = [
        _store(
            storage,
            str(conversation["id"]),
            "user" if index % 2 == 0 else "assistant",
            f"CURSOR-ROW-{index}",
        )
        for index in range(4)
    ]
    boundary = _store(storage, str(conversation["id"]), "user", "CURSOR-BOUNDARY")
    return conversation, rows, boundary


def _token_text(token: str) -> str:
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    return raw.decode("ascii")


def _opaque(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("ascii")).rstrip(b"=").decode("ascii")


@pytest.mark.parametrize(
    "corruption",
    ("signature", "noncanonical_signed", "duplicate_json", "nonfinite", "overflow"),
)
def test_signed_cursor_rejects_tamper_duplicate_json_and_nonfinite_payloads(
    adversarial_storage: Any,
    corruption: str,
) -> None:
    conversation, _rows, boundary = _seed_cursor(adversarial_storage)
    _authorization, adapter, context = _adapter(
        adversarial_storage,
        str(conversation["id"]),
        label=f"cursor-{corruption}",
    )
    initial = _request(str(conversation["id"]), str(boundary["id"]))
    with adversarial_storage.transaction() as conn:
        first = adapter.prepare_in_transaction(conn, context=context, request=initial)
    assert first.next_continuation is not None
    envelope = json.loads(_token_text(first.next_continuation.token))
    if corruption == "signature":
        envelope["signature"] = "0" * 64 if envelope["signature"] != "0" * 64 else "1" * 64
        malformed = _opaque(json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    elif corruption == "noncanonical_signed":
        payload = json.dumps(
            envelope["payload"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        # Payload and its valid signature are byte-for-byte unchanged.  Only
        # the envelope key order and whitespace differ from canonical JSON.
        malformed = _opaque(f'{{ "signature": "{envelope["signature"]}", "payload": {payload} }}')
    elif corruption == "duplicate_json":
        payload = json.dumps(
            envelope["payload"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        malformed = _opaque(
            f'{{"payload":{payload},"signature":"{envelope["signature"]}",'
            f'"signature":"{envelope["signature"]}"}}'
        )
    elif corruption == "nonfinite":
        malformed = _opaque(f'{{"payload":NaN,"signature":"{envelope["signature"]}"}}')
    else:
        malformed = _opaque(f'{{"payload":1e999,"signature":"{envelope["signature"]}"}}')
    resumed = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        continuation=MessageExactContinuation.create(malformed),
    )
    with adversarial_storage.transaction() as conn, pytest.raises(MessageExactStorageError):
        adapter.prepare_in_transaction(conn, context=context, request=resumed)


def test_cursor_is_bound_to_conversation_boundary_roles_time_and_content_mode(
    adversarial_storage: Any,
) -> None:
    conversation, _rows, boundary = _seed_cursor(adversarial_storage)
    _authorization, adapter, context = _adapter(
        adversarial_storage,
        str(conversation["id"]),
        label="cursor-scope-origin",
    )
    with adversarial_storage.transaction() as conn:
        first = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=_request(str(conversation["id"]), str(boundary["id"])),
        )
    assert first.next_continuation is not None
    continuation = first.next_continuation

    alternate_boundary = _store(
        adversarial_storage,
        str(conversation["id"]),
        "user",
        "ALTERNATE-BOUNDARY",
    )
    other = adversarial_storage.create_conversation(OWNER, "other cursor scope")
    _store(adversarial_storage, str(other["id"]), "assistant", "OTHER-CURSOR-ROW")
    other_boundary = _store(adversarial_storage, str(other["id"]), "user", "OTHER-BOUNDARY")
    _other_authorization, other_adapter, other_context = _adapter(
        adversarial_storage,
        str(other["id"]),
        label="cursor-scope-other",
    )

    cases = (
        (
            adapter,
            context,
            _request(
                str(conversation["id"]),
                str(alternate_boundary["id"]),
                continuation=continuation,
            ),
        ),
        (
            adapter,
            context,
            _request(
                str(conversation["id"]),
                str(boundary["id"]),
                continuation=continuation,
                page_size=3,
            ),
        ),
        (
            adapter,
            context,
            _request(
                str(conversation["id"]),
                str(boundary["id"]),
                continuation=continuation,
                roles=(MessageRole.USER,),
            ),
        ),
        (
            adapter,
            context,
            _request(
                str(conversation["id"]),
                str(boundary["id"]),
                continuation=continuation,
                since="2026-09-01T09:00:00+00:00",
                until="2026-09-01T11:00:00+00:00",
            ),
        ),
        (
            adapter,
            context,
            _request(
                str(conversation["id"]),
                str(boundary["id"]),
                continuation=continuation,
                content_mode=MessageExactContentMode.FULL_CONTENT,
            ),
        ),
        (
            other_adapter,
            other_context,
            _request(
                str(other["id"]),
                str(other_boundary["id"]),
                continuation=continuation,
            ),
        ),
    )
    for scoped_adapter, scoped_context, scoped_request in cases:
        with adversarial_storage.transaction() as conn, pytest.raises(MessageExactStorageError):
            scoped_adapter.prepare_in_transaction(
                conn,
                context=scoped_context,
                request=scoped_request,
            )


def test_overflow_numeric_metadata_fails_closed_before_projection(
    adversarial_storage: Any,
) -> None:
    adversarial_storage.ensure_user(OWNER, preset_key="owner")
    conversation = adversarial_storage.create_conversation(OWNER, "overflow metadata")
    source = _store(adversarial_storage, str(conversation["id"]), "assistant", "PRIVATE")
    boundary = _store(adversarial_storage, str(conversation["id"]), "user", "BOUNDARY")
    with adversarial_storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?",
            ('{"value":1e999}', source["id"]),
        )
    _authorization, adapter, context = _adapter(
        adversarial_storage,
        str(conversation["id"]),
        label="overflow-metadata",
    )
    with adversarial_storage.transaction() as conn, pytest.raises(MessageExactStorageError):
        adapter.prepare_in_transaction(
            conn,
            context=context,
            request=_request(str(conversation["id"]), str(boundary["id"])),
        )


def _drop_only_isolated_immutability_trigger(conn: Any, name: str) -> None:
    assert name in {
        "conversation_passage_message_bu_identity_immutable",
        "messages_are_never_rewritten",
        "messages_are_never_deleted",
    }
    conn.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - exact two-name allowlist above


def _mutate_source(
    storage: Any,
    source: dict[str, Any],
    alternate_parent: dict[str, Any],
    mutation: str,
) -> None:
    with storage.transaction() as conn:
        if mutation == "edited":
            _drop_only_isolated_immutability_trigger(conn, "messages_are_never_rewritten")
            conn.execute(
                "UPDATE messages SET content='EDITED-SOURCE-BODY' WHERE id=?",
                (source["id"],),
            )
        elif mutation == "deleted":
            _drop_only_isolated_immutability_trigger(conn, "messages_are_never_deleted")
            conn.execute("DELETE FROM messages WHERE id=?", (source["id"],))
        elif mutation == "created_at":
            conn.execute(
                "UPDATE messages SET created_at='2026-09-01T09:59:00+00:00' WHERE id=?",
                (source["id"],),
            )
        elif mutation == "reply":
            conn.execute(
                "UPDATE messages SET reply_to=? WHERE id=?",
                (alternate_parent["id"], source["id"]),
            )
        elif mutation == "message_id":
            _drop_only_isolated_immutability_trigger(
                conn,
                "conversation_passage_message_bu_identity_immutable",
            )
            conn.execute(
                "UPDATE messages SET id='msg_eeeeeeeeeeeeeeee' WHERE id=?",
                (source["id"],),
            )
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(mutation)


@pytest.mark.parametrize(
    "mutation",
    ("message_id", "edited", "deleted", "created_at", "reply"),
)
def test_resume_and_publication_reject_message_identity_body_time_and_reply_drift(
    adversarial_storage: Any,
    mutation: str,
) -> None:
    adversarial_storage.ensure_user(OWNER, preset_key="owner")
    conversation = adversarial_storage.create_conversation(OWNER, f"drift {mutation}")
    parent = _store(adversarial_storage, str(conversation["id"]), "user", "PARENT-A")
    alternate_parent = _store(
        adversarial_storage,
        str(conversation["id"]),
        "user",
        "PARENT-B",
    )
    source = _store(
        adversarial_storage,
        str(conversation["id"]),
        "assistant",
        "ORIGINAL-SOURCE-BODY",
        reply_to=str(parent["id"]),
    )
    boundary = _store(
        adversarial_storage,
        str(conversation["id"]),
        "user",
        "DRIFT-BOUNDARY",
    )
    _authorization, adapter, context = _adapter(
        adversarial_storage,
        str(conversation["id"]),
        label=f"source-drift-{mutation}",
    )
    initial = _request(str(conversation["id"]), str(boundary["id"]), page_size=2)
    with adversarial_storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=initial)
    assert page.next_continuation is not None
    assert [row.message_id for row in page.rows] == [parent["id"], alternate_parent["id"]]

    _mutate_source(adversarial_storage, source, alternate_parent, mutation)
    resumed = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        page_size=2,
        continuation=page.next_continuation,
    )
    with adversarial_storage.transaction() as conn, pytest.raises(MessageExactStorageDrift):
        adapter.prepare_in_transaction(conn, context=context, request=resumed)

    with adversarial_storage.transaction() as conn:
        decision = adapter.reauthorize_for_publication_in_transaction(
            conn,
            context=context,
            page=page,
        )
    assert decision.status is MessageExactPublicationStatus.DRIFTED
    assert decision.authorized is False
    assert decision.authorizes(page) is False
    public = decision.to_public_payload()
    assert public["status"] == "drifted"
    rendered = repr(decision) + json.dumps(public, sort_keys=True)
    for private in (
        OWNER,
        str(conversation["id"]),
        str(source["id"]),
        "ORIGINAL-SOURCE-BODY",
        "EDITED-SOURCE-BODY",
        str(parent["id"]),
        str(alternate_parent["id"]),
    ):
        assert private not in rendered


def test_recursive_reply_parent_content_drift_invalidates_resume_and_publication(
    adversarial_storage: Any,
) -> None:
    adversarial_storage.ensure_user(OWNER, preset_key="owner")
    conversation = adversarial_storage.create_conversation(OWNER, "recursive reply drift")
    parent = _store(adversarial_storage, str(conversation["id"]), "user", "PRIVATE-PARENT-BODY")
    reply = _store(
        adversarial_storage,
        str(conversation["id"]),
        "assistant",
        "PRIVATE-REPLY-BODY",
        reply_to=str(parent["id"]),
    )
    tail = _store(adversarial_storage, str(conversation["id"]), "assistant", "PRIVATE-TAIL")
    boundary = _store(
        adversarial_storage,
        str(conversation["id"]),
        "user",
        "RECURSIVE-BOUNDARY",
    )
    _authorization, adapter, context = _adapter(
        adversarial_storage,
        str(conversation["id"]),
        label="recursive-reply-parent",
    )
    request = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        page_size=1,
        roles=(MessageRole.ASSISTANT,),
    )
    with adversarial_storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)
    assert [row.message_id for row in page.rows] == [reply["id"]]
    assert page.rows[0].reply_to_message_id == parent["id"]
    assert page.next_continuation is not None

    with adversarial_storage.transaction() as conn:
        _drop_only_isolated_immutability_trigger(conn, "messages_are_never_rewritten")
        conn.execute(
            "UPDATE messages SET content='EDITED-PARENT-BODY' WHERE id=?",
            (parent["id"],),
        )
    resumed = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        page_size=1,
        continuation=page.next_continuation,
        roles=(MessageRole.ASSISTANT,),
    )
    with adversarial_storage.transaction() as conn, pytest.raises(MessageExactStorageDrift):
        adapter.prepare_in_transaction(conn, context=context, request=resumed)
    with adversarial_storage.transaction() as conn:
        decision = adapter.reauthorize_for_publication_in_transaction(
            conn,
            context=context,
            page=page,
        )
    assert decision.status is MessageExactPublicationStatus.DRIFTED
    assert decision.authorizes(page) is False
    rendered = repr(decision) + json.dumps(decision.to_public_payload(), sort_keys=True)
    for private in (
        str(parent["id"]),
        str(reply["id"]),
        str(tail["id"]),
        "PRIVATE-PARENT-BODY",
        "EDITED-PARENT-BODY",
    ):
        assert private not in rendered


def test_tampered_denied_decision_cannot_be_promoted_to_authorized(
    adversarial_storage: Any,
) -> None:
    adversarial_storage.ensure_user(OWNER, preset_key="owner")
    conversation = adversarial_storage.create_conversation(OWNER, "decision tamper")
    source = _store(adversarial_storage, str(conversation["id"]), "assistant", "DECISION-BODY")
    boundary = _store(adversarial_storage, str(conversation["id"]), "user", "DECISION-BOUNDARY")
    authorization, adapter, context = _adapter(
        adversarial_storage,
        str(conversation["id"]),
        label="decision-tamper",
    )
    with adversarial_storage.transaction() as conn:
        page = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=_request(str(conversation["id"]), str(boundary["id"])),
        )
    authorization.deny_permission(OWNER, "conversations.read")
    with adversarial_storage.transaction() as conn:
        decision = adapter.reauthorize_for_publication_in_transaction(
            conn,
            context=context,
            page=page,
        )
    assert decision.status is MessageExactPublicationStatus.DENIED

    object.__setattr__(decision, "status", MessageExactPublicationStatus.AUTHORIZED)
    assert decision.authorized is False
    assert decision.authorizes(page) is False
    with pytest.raises(MessageExactContractError, match="integrity"):
        decision.to_public_payload()
    rendered = repr(decision)
    assert "invalid=True" in rendered
    for private in (OWNER, str(conversation["id"]), str(source["id"]), "DECISION-BODY"):
        assert private not in rendered
