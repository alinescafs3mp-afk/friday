from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any

import pytest

from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.orchestration.turn_context_runtime import (
    bind_authenticated_turn_context,
    suspend_authenticated_turn_context,
)
from friday.permissions import ActorContext
from friday.storage._conversations import store_message_in_transaction
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_SERIALS = itertools.count(200_000)
_BASE_NOW_NS = 50_000_000_000_000
_PUBLICATION_KEY = "authenticated_turn_publication"
_PUBLICATION_SCHEMA = "friday.authenticated-turn-publication.v1"


def _authenticated_turn(
    *,
    conversation_id: str,
    user_id: str,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext, list[int]]:
    serial = next(_SERIALS)
    now = [_BASE_NOW_NS + serial]
    issuer = TurnContextIssuer(
        hashlib.sha256(f"publication-guard-namespace-{serial}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now[0],
    )
    actor = ActorContext(
        user_id=user_id,
        preset_key="owner",
        source="api-token",
        identity_id=f"principal-{serial}",
        session_id=f"session-{serial}",
    )
    request_binding = hashlib.sha256(
        f"publication-guard-request-{serial}".encode("ascii")
    ).hexdigest()
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"accepted-publication-{serial}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"source-publication-{serial}",
        update_id=f"update-publication-{serial}",
        request_effect_binding_sha256=request_binding,
    )
    model_input = TurnInput.from_chat(
        message=f"publication request {serial}",
        actor=actor,
        conversation_id=conversation_id,
        attachments=[],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to="",
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
            TurnSafetyDeadline(now[0] + 1_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, 1, 8_192),
        ),
        pending_work_admission=None,
    )
    return issuer, context, now


def _projection(context: AuthenticatedTurnContext, role: str) -> dict[str, str]:
    return {
        "schema": _PUBLICATION_SCHEMA,
        "turn_id": context.turn_id,
        "context_authority_sha256": context.context_authority_sha256,
        "request_effect_binding_sha256": context.effect_fence.request_effect_binding_sha256,
        "publication_role": role,
    }


def _writes(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]


def test_exact_user_and_assistant_publications_share_one_atomic_closed_turn(
    storage: Any,
) -> None:
    user_id = "alice"
    conversation_id = str(storage.create_conversation(user_id, "Exact turn")["id"])
    issuer, context, _now = _authenticated_turn(
        conversation_id=conversation_id,
        user_id=user_id,
    )
    caller_metadata = {"carrier": {"ordinal": 1}}
    caller_snapshot = json.loads(json.dumps(caller_metadata))

    with bind_authenticated_turn_context(issuer, context), storage.transaction() as conn:
        user = store_message_in_transaction(
            conn,
            conversation_id,
            user_id,
            "user",
            "private user body",
            caller_metadata,
        )
        assistant = store_message_in_transaction(
            conn,
            conversation_id,
            user_id,
            "assistant",
            "private assistant body",
        )

    user_metadata = json.loads(str(user["metadata_json"]))
    assistant_metadata = json.loads(str(assistant["metadata_json"]))
    assert user_metadata == {
        "carrier": {"ordinal": 1},
        _PUBLICATION_KEY: _projection(context, "user"),
    }
    assert assistant_metadata == {
        _PUBLICATION_KEY: _projection(context, "assistant"),
    }
    assert caller_metadata == caller_snapshot
    actor_identity_id = context.authority.actor.identity_id
    assert actor_identity_id is not None
    for metadata in (user_metadata, assistant_metadata):
        projection = metadata[_PUBLICATION_KEY]
        assert set(projection) == {
            "schema",
            "turn_id",
            "context_authority_sha256",
            "request_effect_binding_sha256",
            "publication_role",
        }
        encoded = json.dumps(projection, sort_keys=True)
        assert "private user body" not in encoded
        assert "private assistant body" not in encoded
        assert context.authority.source_id not in encoded
        assert actor_identity_id not in encoded


@pytest.mark.parametrize("mismatch", ["conversation", "person"])
def test_scope_mismatch_has_zero_insert_or_update(storage: Any, mismatch: str) -> None:
    user_id = "alice"
    admitted_id = str(storage.create_conversation(user_id, "Admitted")["id"])
    other_id = str(storage.create_conversation(user_id, "Other")["id"])
    storage.ensure_user("bob")
    issuer, context, _now = _authenticated_turn(
        conversation_id=admitted_id,
        user_id=user_id,
    )
    statements: list[str] = []
    before = storage.conn.total_changes
    storage.conn.set_trace_callback(statements.append)
    try:
        with bind_authenticated_turn_context(issuer, context), pytest.raises(
            TurnContextError,
            match="publication scope",
        ):
            store_message_in_transaction(
                storage.conn,
                other_id if mismatch == "conversation" else admitted_id,
                user_id if mismatch == "conversation" else "bob",
                "assistant",
                "must not be stored",
            )
    finally:
        storage.conn.set_trace_callback(None)

    assert storage.conn.total_changes == before
    assert _writes(statements) == []


@pytest.mark.parametrize("failure", ["suspended", "stale"])
def test_suspended_or_stale_authority_has_zero_database_writes(storage: Any, failure: str) -> None:
    user_id = "alice"
    conversation_id = str(storage.create_conversation(user_id, "Guarded")["id"])
    issuer, context, now = _authenticated_turn(
        conversation_id=conversation_id,
        user_id=user_id,
    )
    statements: list[str] = []
    before = storage.conn.total_changes
    storage.conn.set_trace_callback(statements.append)
    try:
        with bind_authenticated_turn_context(issuer, context):
            if failure == "stale":
                now[0] = context.inherited_budget.safety_deadline.monotonic_ns + 1
                with pytest.raises(TurnContextError, match="deadline"):
                    store_message_in_transaction(
                        storage.conn,
                        conversation_id,
                        user_id,
                        "assistant",
                        "must not be stored",
                    )
            else:
                with suspend_authenticated_turn_context(), pytest.raises(
                    TurnContextError,
                    match="primary authority",
                ):
                    store_message_in_transaction(
                        storage.conn,
                        conversation_id,
                        user_id,
                        "assistant",
                        "must not be stored",
                    )
    finally:
        storage.conn.set_trace_callback(None)

    assert storage.conn.total_changes == before
    assert _writes(statements) == []


def test_reserved_metadata_collision_fails_without_mutating_caller_or_database(storage: Any) -> None:
    user_id = "alice"
    conversation_id = str(storage.create_conversation(user_id, "Collision")["id"])
    issuer, context, _now = _authenticated_turn(
        conversation_id=conversation_id,
        user_id=user_id,
    )
    caller_metadata = {_PUBLICATION_KEY: {"schema": "caller-owned"}, "keep": True}
    caller_snapshot = json.loads(json.dumps(caller_metadata))
    before = storage.conn.total_changes

    with bind_authenticated_turn_context(issuer, context), pytest.raises(
        TurnContextError,
        match="metadata is reserved",
    ):
        store_message_in_transaction(
            storage.conn,
            conversation_id,
            user_id,
            "user",
            "must not be stored",
            caller_metadata,
        )

    assert storage.conn.total_changes == before
    assert caller_metadata == caller_snapshot


@pytest.mark.parametrize("metadata", [None, {"z": "Юникод", "a": {"n": 1}}])
def test_legacy_publication_bytes_and_caller_metadata_remain_unchanged(
    storage: Any,
    metadata: dict[str, Any] | None,
) -> None:
    user_id = "alice"
    conversation_id = str(storage.create_conversation(user_id, "Legacy parity")["id"])
    caller_snapshot = None if metadata is None else json.loads(json.dumps(metadata))

    with storage.transaction() as conn:
        stored = store_message_in_transaction(
            conn,
            conversation_id,
            user_id,
            "assistant",
            "legacy bytes",
            metadata,
        )

    expected = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    assert str(stored["metadata_json"]).encode("utf-8") == expected.encode("utf-8")
    assert _PUBLICATION_KEY not in json.loads(str(stored["metadata_json"]))
    assert metadata == caller_snapshot


def test_live_non_conversation_role_keeps_legacy_metadata_bytes(storage: Any) -> None:
    user_id = "alice"
    conversation_id = str(storage.create_conversation(user_id, "Other role")["id"])
    issuer, context, _now = _authenticated_turn(
        conversation_id=conversation_id,
        user_id=user_id,
    )
    metadata = {"kind": "code-owned-system-event"}

    with bind_authenticated_turn_context(issuer, context), storage.transaction() as conn:
        stored = store_message_in_transaction(
            conn,
            conversation_id,
            user_id,
            "system",
            "event",
            metadata,
        )

    assert str(stored["metadata_json"]) == json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert metadata == {"kind": "code-owned-system-event"}


def test_scope_guard_runs_before_any_connection_statement(storage: Any) -> None:
    user_id = "alice"
    conversation_id = str(storage.create_conversation(user_id, "No SQL")["id"])
    issuer, context, _now = _authenticated_turn(
        conversation_id=conversation_id,
        user_id=user_id,
    )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        with bind_authenticated_turn_context(issuer, context), pytest.raises(TurnContextError):
            store_message_in_transaction(
                storage.conn,
                "conv_ffffffffffffffff",
                user_id,
                "user",
                "no statement",
            )
    finally:
        storage.conn.set_trace_callback(None)

    assert statements == []
