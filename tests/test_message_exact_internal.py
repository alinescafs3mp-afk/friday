"""Focused contract tests for the queryless exact-message internal lane.

These tests exercise the real SQLite storage selector and the direct trusted
adapter.  The lane stays deliberately separate from the released lexical and
archive readers, which are sampled again at the end of this module.
"""

from __future__ import annotations

import copy
import hashlib
import pickle
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
    MESSAGE_EXACT_MAX_FULL_PAGE_CHARS,
    MESSAGE_EXACT_MAX_FULL_ROW_CHARS,
    MessageExactContentCoverage,
    MessageExactContentMode,
    MessageExactContractError,
    MessageExactPublicationStatus,
    MessageExactRequest,
    MessageExactRowCoverage,
)
from friday.retrieval.message_exact_internal import (
    MESSAGE_EXACT_ADAPTER_BINDING,
    MessageExactAdapterBinding,
    MessageExactInternalAdapter,
    MessageExactInternalError,
    MessageExactReadDenied,
)
from friday.storage import FridayStorage
from friday.storage._archive_search_messages import (
    ArchiveMessageScope,
    select_authorized_archive_message_page_in_transaction,
)
from friday.storage._message_exact_internal import MessageExactStorageError
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

OWNER = "message-exact-owner"
FOREIGN = "message-exact-foreign"
BASE_TIME = "2026-09-01T08:00:00+00:00"
EQUAL_TIME = "2026-09-01T08:30:00+00:00"


def _turn(
    actor: ActorContext,
    conversation_id: str,
    *,
    label: str,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    now = time.monotonic_ns()
    issuer = TurnContextIssuer(
        hashlib.sha256(f"message-exact:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now,
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"message-exact-ingress-{label}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"message-exact-source-{label}",
        update_id=f"message-exact-update-{label}",
        request_effect_binding_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message="read the exact current conversation",
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
    *,
    principal_id: str,
    conversation_id: str,
    label: str,
) -> tuple[AuthorizationService, ActorContext, MessageExactInternalAdapter, AuthenticatedTurnContext]:
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user(principal_id, source="message-exact-test")
    issuer, context = _turn(actor, conversation_id, label=label)
    return authorization, actor, MessageExactInternalAdapter(authorization, issuer), context


def _set_time(storage: Any, message_id: str, created_at: str) -> None:
    with storage.transaction() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE id=?", (created_at, message_id))


def _store(
    storage: Any,
    conversation_id: str,
    principal_id: str,
    role: str,
    content: str,
    *,
    at: str = BASE_TIME,
    metadata: dict[str, Any] | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    row = storage.store_message(
        conversation_id,
        principal_id,
        role,
        content,
        metadata=metadata,
        reply_to=reply_to,
    )
    _set_time(storage, str(row["id"]), at)
    return row


def _request(
    conversation_id: str,
    boundary_id: str,
    **overrides: Any,
) -> MessageExactRequest:
    values: dict[str, Any] = {
        "conversation_id": conversation_id,
        "accepted_boundary_user_message_id": boundary_id,
    }
    values.update(overrides)
    return MessageExactRequest.create(**values)


def _scope_sql(statements: list[str]) -> tuple[str, ...]:
    normalized = tuple(" ".join(statement.casefold().split()) for statement in statements)
    return tuple(
        statement
        for statement in normalized
        if " conversations " in f" {statement} " or " messages " in f" {statement} "
    )


def _seed_owner(storage: Any, title: str = "exact current conversation") -> dict[str, Any]:
    storage.ensure_user(OWNER, preset_key="owner")
    return storage.create_conversation(OWNER, title)


def test_adapter_binding_is_closed_read_only_and_not_model_visible() -> None:
    payload = MESSAGE_EXACT_ADAPTER_BINDING.payload()
    assert payload["capability_id"] == "conversation.window.read"
    assert payload["security_ids"] == ["conversations.read", "search.use"]
    assert payload["effect_class"] == "read"
    assert payload["model_visible"] is False
    assert len(MESSAGE_EXACT_ADAPTER_BINDING.canonical_sha256()) == 64

    tampered = MessageExactAdapterBinding()
    object.__setattr__(tampered, "model_visible", True)
    with pytest.raises(MessageExactInternalError, match="not closed"):
        tampered.payload()


def test_queryless_current_scope_preserves_message_and_reply_identity(storage: Any) -> None:
    conversation = _seed_owner(storage)
    storage.ensure_user(FOREIGN, preset_key="owner")
    other_owned = storage.create_conversation(OWNER, "other owned conversation")
    foreign = storage.create_conversation(FOREIGN, "foreign conversation")

    parent = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "user",
        "CURRENT-PARENT exact body",
        metadata={"source": "typed-parent"},
    )
    reply = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "assistant",
        "CURRENT-REPLY exact body",
        metadata={"source": "typed-reply"},
        reply_to=str(parent["id"]),
    )
    _store(storage, str(conversation["id"]), OWNER, "system", "SYSTEM-CANARY")
    _store(storage, str(other_owned["id"]), OWNER, "user", "OTHER-OWNED-CANARY")
    _store(storage, str(foreign["id"]), FOREIGN, "user", "FOREIGN-CANARY")
    boundary = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "user",
        "CURRENT-BOUNDARY-CANARY",
    )
    _store(storage, str(conversation["id"]), OWNER, "assistant", "AFTER-BOUNDARY-CANARY")

    _authorization, _actor, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="queryless-current",
    )
    request = _request(str(conversation["id"]), str(boundary["id"]))
    assert "query" not in request.to_private_payload()
    assert "query" not in request.to_private_json()

    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)

    assert [row.message_id for row in page.rows] == [parent["id"], reply["id"]]
    assert [row.content for row in page.rows] == [
        "CURRENT-PARENT exact body",
        "CURRENT-REPLY exact body",
    ]
    assert page.rows[1].reply_to_message_id == parent["id"]
    assert page.rows[1].reply_revision_sha256 == page.rows[0].revision_sha256
    assert page.boundary.message_id == boundary["id"]
    assert page.boundary.content == "CURRENT-BOUNDARY-CANARY"
    assert page.offset == 0
    assert page.total_rows == 2

    projection = adapter.project_for_model(page)
    projected = projection.to_model_json()
    for private in (
        OWNER,
        FOREIGN,
        str(conversation["id"]),
        str(parent["id"]),
        str(reply["id"]),
        "OTHER-OWNED-CANARY",
        "FOREIGN-CANARY",
        "CURRENT-BOUNDARY-CANARY",
        "AFTER-BOUNDARY-CANARY",
        "SYSTEM-CANARY",
    ):
        assert private not in projected


def test_sqlite_rowids_above_one_billion_remain_valid_exact_sequences(storage: Any) -> None:
    conversation = _seed_owner(storage, "high SQLite rowid")
    source_id = "msg_1000000000000001"
    boundary_id = "msg_1000000000000002"
    source_sequence = 1_000_000_001
    boundary_sequence = 1_000_000_002
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO messages(
                   rowid,id,conversation_id,user_id,role,content,
                   metadata_json,reply_to,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                source_sequence,
                source_id,
                conversation["id"],
                OWNER,
                "assistant",
                "HIGH-ROWID-SOURCE",
                "{}",
                None,
                BASE_TIME,
            ),
        )
        conn.execute(
            """INSERT INTO messages(
                   rowid,id,conversation_id,user_id,role,content,
                   metadata_json,reply_to,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                boundary_sequence,
                boundary_id,
                conversation["id"],
                OWNER,
                "user",
                "HIGH-ROWID-BOUNDARY",
                "{}",
                None,
                "2026-09-01T08:00:01+00:00",
            ),
        )

    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="high-sqlite-rowid",
    )
    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=_request(str(conversation["id"]), boundary_id),
        )

    assert page.total_rows == 1
    assert page.boundary.message_id == boundary_id
    assert page.boundary.storage_sequence == boundary_sequence
    assert [row.message_id for row in page.rows] == [source_id]
    assert [row.storage_sequence for row in page.rows] == [source_sequence]
    assert [row.content for row in page.rows] == ["HIGH-ROWID-SOURCE"]


def test_role_and_microsecond_half_open_window_are_exact(storage: Any) -> None:
    conversation = _seed_owner(storage, "microsecond exact window")
    below = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "assistant",
        "BELOW-SINCE",
        at="2026-09-01T10:00:00.123455+00:00",
    )
    at_since = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "assistant",
        "AT-SINCE",
        at="2026-09-01T10:00:00.123456+00:00",
    )
    role_excluded = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "user",
        "ROLE-EXCLUDED",
        at="2026-09-01T10:00:00.123457+00:00",
    )
    inside = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "assistant",
        "INSIDE-WINDOW",
        at="2026-09-01T10:00:00.123458+00:00",
    )
    at_until = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "assistant",
        "AT-UNTIL",
        at="2026-09-01T10:00:00.123459+00:00",
    )
    boundary = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "user",
        "MICROSECOND-BOUNDARY",
        at="2026-09-01T10:00:01+00:00",
    )
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="microsecond-window",
    )
    request = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        roles=(MessageRole.ASSISTANT,),
        since="2026-09-01T10:00:00.123456+00:00",
        until="2026-09-01T10:00:00.123459+00:00",
    )
    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)

    assert page.total_rows == 2
    assert [row.message_id for row in page.rows] == [at_since["id"], inside["id"]]
    assert [row.created_at for row in page.rows] == [
        "2026-09-01T10:00:00.123456+00:00",
        "2026-09-01T10:00:00.123458+00:00",
    ]
    assert all(row.role is MessageRole.ASSISTANT for row in page.rows)
    excluded = {below["id"], role_excluded["id"], at_until["id"]}
    assert excluded.isdisjoint(row.message_id for row in page.rows)


@pytest.mark.parametrize("denied", (None, "conversations.read", "search.use"))
def test_both_permissions_are_fresh_before_conversation_count_or_body_sql(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    denied: str | None,
) -> None:
    conversation = _seed_owner(storage, f"permission order {denied}")
    _store(storage, str(conversation["id"]), OWNER, "assistant", "PRIVATE-AUTH-BODY")
    boundary = _store(storage, str(conversation["id"]), OWNER, "user", "PRIVATE-BOUNDARY")
    authorization, actor, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label=f"permission-{denied or 'allow'}",
    )
    # The actor is intentionally stale: mutable permission state changes after
    # both it and the authenticated turn were minted.
    if denied is not None:
        authorization.deny_permission(OWNER, denied)

    checked: list[str] = []
    traced: list[tuple[str, tuple[str, ...]]] = []
    real_authorize = authorization.authorize_in_transaction

    def recording_authorize(conn: Any, checked_actor: ActorContext, security_id: str):  # noqa: ANN202
        checked.append(security_id)
        return real_authorize(conn, checked_actor, security_id)

    monkeypatch.setattr(authorization, "authorize_in_transaction", recording_authorize)
    request = _request(str(conversation["id"]), str(boundary["id"]))
    with storage.transaction() as conn:
        conn.set_trace_callback(lambda statement: traced.append((statement, tuple(checked))))
        try:
            if denied is None:
                page = adapter.prepare_in_transaction(conn, context=context, request=request)
                assert [row.content for row in page.rows] == ["PRIVATE-AUTH-BODY"]
            else:
                with pytest.raises(MessageExactReadDenied):
                    adapter.prepare_in_transaction(conn, context=context, request=request)
        finally:
            conn.set_trace_callback(None)

    expected = (
        ("conversations.read", "search.use") if denied in {None, "search.use"} else ("conversations.read",)
    )
    assert tuple(checked) == expected
    scope_reads = [
        (statement, at_time)
        for statement, at_time in traced
        if " conversations " in f" {statement.casefold()} " or " messages " in f" {statement.casefold()} "
    ]
    if denied is None:
        assert scope_reads
        assert all(at_time == ("conversations.read", "search.use") for _, at_time in scope_reads)
    else:
        assert scope_reads == []
    assert actor.preset_key == "owner", "the request-time actor stayed stale by construction"


def test_foreign_actor_conversation_and_boundary_fail_closed(storage: Any) -> None:
    current = _seed_owner(storage, "owner current")
    storage.ensure_user(FOREIGN, preset_key="owner")
    other = storage.create_conversation(OWNER, "other owner conversation")
    foreign = storage.create_conversation(FOREIGN, "foreign actor conversation")
    assistant = _store(storage, str(current["id"]), OWNER, "assistant", "ASSISTANT-BOUNDARY")
    current_boundary = _store(storage, str(current["id"]), OWNER, "user", "OWNER-BOUNDARY")
    other_boundary = _store(storage, str(other["id"]), OWNER, "user", "OTHER-BOUNDARY")
    foreign_boundary = _store(storage, str(foreign["id"]), FOREIGN, "user", "FOREIGN-BOUNDARY")

    _auth, _actor_value, adapter, current_context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(current["id"]),
        label="foreign-scope-owner",
    )

    def reject_and_trace(
        scoped_adapter: MessageExactInternalAdapter,
        scoped_context: AuthenticatedTurnContext,
        scoped_request: MessageExactRequest,
        expected_error: type[Exception],
    ) -> tuple[Exception, tuple[str, ...]]:
        traced: list[str] = []
        with storage.transaction() as conn:
            conn.set_trace_callback(traced.append)
            try:
                with pytest.raises(expected_error) as raised:
                    scoped_adapter.prepare_in_transaction(
                        conn,
                        context=scoped_context,
                        request=scoped_request,
                    )
            finally:
                conn.set_trace_callback(None)
        return raised.value, _scope_sql(traced)

    _raised, scope_reads = reject_and_trace(
        adapter,
        current_context,
        _request(str(other["id"]), str(other_boundary["id"])),
        MessageExactInternalError,
    )
    assert scope_reads == ()
    for invalid_boundary in (other_boundary["id"], foreign_boundary["id"], assistant["id"]):
        raised, scope_reads = reject_and_trace(
            adapter,
            current_context,
            _request(str(current["id"]), str(invalid_boundary)),
            MessageExactStorageError,
        )
        assert "BOUNDARY" not in str(raised)
        assert len(scope_reads) == 1
        ownership_probe = scope_reads[0]
        assert "select boundary.rowid from users principal" in ownership_probe
        assert "join conversations owned" in ownership_probe
        assert "join messages boundary" in ownership_probe
        assert all(
            forbidden not in ownership_probe
            for forbidden in ("count(", ".content", "metadata_json", "length(")
        )

    _foreign_auth, _foreign_actor, foreign_adapter, foreign_context = _adapter(
        storage,
        principal_id=FOREIGN,
        conversation_id=str(current["id"]),
        label="foreign-scope-actor",
    )
    raised, scope_reads = reject_and_trace(
        foreign_adapter,
        foreign_context,
        _request(str(current["id"]), str(current_boundary["id"])),
        MessageExactStorageError,
    )
    assert OWNER not in str(raised)
    assert FOREIGN not in str(raised)
    assert len(scope_reads) == 1
    assert "select boundary.rowid from users principal" in scope_reads[0]
    assert all(
        forbidden not in scope_reads[0] for forbidden in ("count(", ".content", "metadata_json", "length(")
    )


def test_equal_timestamp_restart_paging_is_chronological_and_never_deduplicates(
    storage: Any,
) -> None:
    conversation = _seed_owner(storage, "equal timestamp paging")
    inserted = [
        _store(
            storage,
            str(conversation["id"]),
            OWNER,
            "user" if index % 2 == 0 else "assistant",
            "DUPLICATE-BODY",
            at=EQUAL_TIME,
        )
        for index in range(5)
    ]
    boundary = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "user",
        "PAGING-BOUNDARY",
        at=EQUAL_TIME,
    )
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="equal-timestamp-paging",
    )
    request = _request(str(conversation["id"]), str(boundary["id"]), page_size=2)
    with storage.transaction() as conn:
        first = adapter.prepare_in_transaction(conn, context=context, request=request)
    assert first.next_continuation is not None

    # Reopening the SQLite owner exercises a durable deployment-key cursor, not
    # an accidental connection-local offset.
    storage.close()
    reopened = FridayStorage(
        replace(
            storage.settings,
            database_path=storage.settings.database_path,
            database_must_exist=True,
        )
    )
    try:
        restarted_adapter = MessageExactInternalAdapter(
            AuthorizationService(reopened),
            adapter._issuer,  # noqa: SLF001 - exact restart fixture keeps the trusted issuer
        )
        pages = [first]
        continuation = first.next_continuation
        while continuation is not None:
            resumed = _request(
                str(conversation["id"]),
                str(boundary["id"]),
                page_size=2,
                continuation=continuation,
            )
            with reopened.transaction() as conn:
                page = restarted_adapter.prepare_in_transaction(
                    conn,
                    context=context,
                    request=resumed,
                )
            if continuation is first.next_continuation:
                with reopened.transaction() as conn:
                    replayed = restarted_adapter.prepare_in_transaction(
                        conn,
                        context=context,
                        request=resumed,
                    )
                assert replayed.selection_handle == page.selection_handle
                assert replayed.snapshot_handle == page.snapshot_handle
                assert [row.message_id for row in replayed.rows] == [row.message_id for row in page.rows]
                assert (
                    restarted_adapter.project_for_model(replayed).to_model_payload()
                    == restarted_adapter.project_for_model(page).to_model_payload()
                )
            pages.append(page)
            continuation = page.next_continuation
    finally:
        reopened.close()

    rows = [row for page in pages for row in page.rows]
    assert [page.offset for page in pages] == [0, 2, 4]
    assert [page.total_rows for page in pages] == [5, 5, 5]
    assert [row.message_id for row in rows] == [row["id"] for row in inserted]
    assert [row.storage_sequence for row in rows] == sorted(row.storage_sequence for row in rows)
    assert [row.content for row in rows] == ["DUPLICATE-BODY"] * 5
    assert len({row.message_id for row in rows}) == 5


@pytest.mark.parametrize(
    (
        "bodies",
        "page_size",
        "visible_lengths",
        "expected_rows",
        "expected_content",
        "truncated",
    ),
    (
        (
            ("R" * MESSAGE_EXACT_MAX_FULL_ROW_CHARS,),
            100,
            (MESSAGE_EXACT_MAX_FULL_ROW_CHARS,),
            MessageExactRowCoverage.COMPLETE,
            MessageExactContentCoverage.COMPLETE,
            0,
        ),
        (
            ("R" * (MESSAGE_EXACT_MAX_FULL_ROW_CHARS + 1),),
            100,
            (MESSAGE_EXACT_MAX_FULL_ROW_CHARS,),
            MessageExactRowCoverage.COMPLETE,
            MessageExactContentCoverage.TRUNCATED,
            1,
        ),
        (
            tuple("P" * MESSAGE_EXACT_MAX_FULL_ROW_CHARS for _ in range(10)),
            100,
            tuple(MESSAGE_EXACT_MAX_FULL_ROW_CHARS for _ in range(10)),
            MessageExactRowCoverage.COMPLETE,
            MessageExactContentCoverage.COMPLETE,
            0,
        ),
        (
            tuple("P" * MESSAGE_EXACT_MAX_FULL_ROW_CHARS for _ in range(11)),
            100,
            tuple(MESSAGE_EXACT_MAX_FULL_ROW_CHARS for _ in range(9))
            + (MESSAGE_EXACT_MAX_FULL_ROW_CHARS - 1, 1),
            MessageExactRowCoverage.COMPLETE,
            MessageExactContentCoverage.TRUNCATED,
            2,
        ),
        (
            ("short-one", "short-two"),
            1,
            (len("short-one"),),
            MessageExactRowCoverage.PARTIAL,
            MessageExactContentCoverage.COMPLETE,
            0,
        ),
    ),
)
def test_full_content_8k_80k_budgets_and_row_coverage_are_independent(
    storage: Any,
    bodies: tuple[str, ...],
    page_size: int,
    visible_lengths: tuple[int, ...],
    expected_rows: MessageExactRowCoverage,
    expected_content: MessageExactContentCoverage,
    truncated: int,
) -> None:
    conversation = _seed_owner(storage, f"full content {len(bodies)}")
    for index, body in enumerate(bodies):
        _store(
            storage,
            str(conversation["id"]),
            OWNER,
            "user" if index % 2 == 0 else "assistant",
            body,
        )
    boundary = _store(storage, str(conversation["id"]), OWNER, "user", "LONG-BOUNDARY")
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label=f"full-content-{len(bodies)}-{page_size}",
    )
    request = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        page_size=page_size,
        content_mode=MessageExactContentMode.FULL_CONTENT,
    )
    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)
    projection = adapter.project_for_model(page)

    assert projection.row_coverage is expected_rows
    assert projection.content_coverage is expected_content
    assert projection.truncated_rows == truncated
    assert len(projection.rows) == len(page.rows) == len(visible_lengths)
    assert tuple(len(row.text) for row in projection.rows) == visible_lengths
    for source, projected, visible_length in zip(
        page.rows,
        projection.rows,
        visible_lengths,
        strict=True,
    ):
        assert projected.content_chars == len(source.content)
        if visible_length == len(source.content):
            assert projected.truncated is False
            assert projected.text == source.content
        else:
            assert projected.truncated is True
            assert projected.text == source.content[: visible_length - 1] + "…"
    assert all(len(row.text) <= MESSAGE_EXACT_MAX_FULL_ROW_CHARS for row in projection.rows)
    assert sum(len(row.text) for row in projection.rows) <= MESSAGE_EXACT_MAX_FULL_PAGE_CHARS
    if len(bodies) in {10, 11}:
        assert sum(len(row.text) for row in projection.rows) == MESSAGE_EXACT_MAX_FULL_PAGE_CHARS


def test_full_content_preserves_exact_whitespace_and_newlines(storage: Any) -> None:
    conversation = _seed_owner(storage, "full content whitespace")
    exact_body = "  leading spaces\nsecond\tline\n\ntrailing spaces  "
    _store(storage, str(conversation["id"]), OWNER, "assistant", exact_body)
    boundary = _store(storage, str(conversation["id"]), OWNER, "user", "WHITESPACE-BOUNDARY")
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="full-content-whitespace",
    )
    request = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        content_mode=MessageExactContentMode.FULL_CONTENT,
    )
    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)
    projection = adapter.project_for_model(page)

    assert projection.rows[0].text == exact_body
    assert projection.rows[0].content_chars == len(exact_body)
    assert projection.rows[0].truncated is False
    assert projection.content_coverage is MessageExactContentCoverage.COMPLETE


@pytest.mark.parametrize("revoked", ("conversations.read", "search.use"))
def test_late_revoke_returns_a_source_free_denial(
    storage: Any,
    revoked: str,
) -> None:
    conversation = _seed_owner(storage, f"late revoke {revoked}")
    source = _store(storage, str(conversation["id"]), OWNER, "assistant", "LATE-REVOKE-BODY")
    boundary = _store(storage, str(conversation["id"]), OWNER, "user", "LATE-REVOKE-BOUNDARY")
    authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label=f"late-revoke-{revoked}",
    )
    request = _request(str(conversation["id"]), str(boundary["id"]))
    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)

    authorization.deny_permission(OWNER, revoked)
    with storage.transaction() as conn:
        traced: list[str] = []
        conn.set_trace_callback(traced.append)
        try:
            decision = adapter.reauthorize_for_publication_in_transaction(
                conn,
                context=context,
                page=page,
            )
        finally:
            conn.set_trace_callback(None)

    assert decision.status is MessageExactPublicationStatus.DENIED
    assert decision.authorized is False
    assert decision.authorizes(page) is False
    public = decision.to_public_payload()
    assert public == {
        "authorized": False,
        "schema": "friday.message-exact-publication-decision.v1",
        "status": "denied",
    }
    rendered = repr(decision) + repr(public)
    for private in (OWNER, str(conversation["id"]), str(source["id"]), "LATE-REVOKE-BODY"):
        assert private not in rendered
    assert not any(
        " conversations " in f" {statement.casefold()} " or " messages " in f" {statement.casefold()} "
        for statement in traced
    )


def test_publication_decision_is_bound_to_exact_page_selection(storage: Any) -> None:
    conversation = _seed_owner(storage, "publication page binding")
    _store(storage, str(conversation["id"]), OWNER, "user", "PAGE-BINDING-FIRST")
    _store(storage, str(conversation["id"]), OWNER, "assistant", "PAGE-BINDING-SECOND")
    boundary = _store(storage, str(conversation["id"]), OWNER, "user", "PAGE-BINDING-BOUNDARY")
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="publication-page-binding",
    )
    first_request = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        page_size=1,
    )
    with storage.transaction() as conn:
        first = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=first_request,
        )
    assert first.next_continuation is not None
    second_request = _request(
        str(conversation["id"]),
        str(boundary["id"]),
        page_size=1,
        continuation=first.next_continuation,
    )
    with storage.transaction() as conn:
        second = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=second_request,
        )

    assert first.authority_handle == second.authority_handle
    assert first.selection_handle != second.selection_handle
    with storage.transaction() as conn:
        first_decision = adapter.reauthorize_for_publication_in_transaction(
            conn,
            context=context,
            page=first,
        )
    with storage.transaction() as conn:
        second_decision = adapter.reauthorize_for_publication_in_transaction(
            conn,
            context=context,
            page=second,
        )

    assert first_decision.status is MessageExactPublicationStatus.AUTHORIZED
    assert second_decision.status is MessageExactPublicationStatus.AUTHORIZED
    assert first_decision.authorizes(first)
    assert first_decision.authorizes(second) is False
    assert second_decision.authorizes(second)
    assert second_decision.authorizes(first) is False


def test_page_rows_and_decisions_are_immutable_process_private_carriers(storage: Any) -> None:
    conversation = _seed_owner(storage, "private carriers")
    source = _store(storage, str(conversation["id"]), OWNER, "assistant", "PRIVATE-CARRIER-BODY")
    boundary = _store(storage, str(conversation["id"]), OWNER, "user", "PRIVATE-CARRIER-BOUNDARY")
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="private-carriers",
    )
    request = _request(str(conversation["id"]), str(boundary["id"]))
    with storage.transaction() as conn:
        page = adapter.prepare_in_transaction(conn, context=context, request=request)
        decision = adapter.reauthorize_for_publication_in_transaction(
            conn,
            context=context,
            page=page,
        )
    assert decision.status is MessageExactPublicationStatus.AUTHORIZED
    assert decision.authorizes(page)

    for private in (page, page.rows[0], page.boundary, decision):
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError, match="process-private"):
                operation(private)
    with pytest.raises(TypeError, match="immutable"):
        page.rows[0].content = "forged"  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable"):
        page.rows = ()  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable"):
        decision.status = MessageExactPublicationStatus.DENIED  # type: ignore[misc]

    rendered = repr(page) + repr(page.rows[0]) + repr(decision)
    for private in (OWNER, str(conversation["id"]), str(source["id"]), "PRIVATE-CARRIER-BODY"):
        assert private not in rendered
    assert not hasattr(page, "to_model_payload")
    assert not hasattr(page.rows[0], "to_model_payload")

    with storage.transaction() as conn:
        row_tampered_page = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=request,
        )
        page_tampered = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=request,
        )
    object.__setattr__(row_tampered_page.rows[0], "content", "FORGED-ROW-BODY")
    object.__setattr__(page_tampered, "total_rows", page_tampered.total_rows + 1)
    for tampered in (row_tampered_page, page_tampered):
        with pytest.raises(MessageExactContractError):
            adapter.project_for_model(tampered)
        with storage.transaction() as conn, pytest.raises(MessageExactInternalError):
            adapter.reauthorize_for_publication_in_transaction(
                conn,
                context=context,
                page=tampered,
            )


def test_new_lane_does_not_change_legacy_or_archive_message_behavior(storage: Any) -> None:
    conversation = _seed_owner(storage, "legacy archive compatibility")
    source = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "assistant",
        "legacycompatneedle exact archival body",
        at="2026-09-01T09:00:00+00:00",
    )
    boundary = _store(
        storage,
        str(conversation["id"]),
        OWNER,
        "user",
        "legacy archive boundary",
        at="2026-09-01T09:01:00+00:00",
    )

    def legacy_shape() -> list[tuple[str, str, str]]:
        return [
            (str(row["id"]), str(row["role"]), str(row["content"]))
            for row in storage.search_messages(
                OWNER,
                "legacycompatneedle",
                conversation_id=str(conversation["id"]),
            )
        ]

    def archive_shape() -> tuple[int, tuple[tuple[str, str], ...]]:
        with storage.transaction() as conn:
            page = select_authorized_archive_message_page_in_transaction(
                conn,
                principal_id=OWNER,
                query="legacycompatneedle",
                scope=ArchiveMessageScope.CURRENT,
                conversation_id=str(conversation["id"]),
                boundary_user_message_id=str(boundary["id"]),
            )
        assert page is not None
        return page.total, tuple((hit.message.message_id, hit.message.content) for hit in page.hits)

    legacy_before = legacy_shape()
    archive_before = archive_shape()
    _authorization, _actor_value, adapter, context = _adapter(
        storage,
        principal_id=OWNER,
        conversation_id=str(conversation["id"]),
        label="legacy-compatibility",
    )
    with storage.transaction() as conn:
        exact = adapter.prepare_in_transaction(
            conn,
            context=context,
            request=_request(str(conversation["id"]), str(boundary["id"])),
        )
    assert [row.message_id for row in exact.rows] == [source["id"]]

    assert (
        legacy_shape()
        == legacy_before
        == [(str(source["id"]), "assistant", "legacycompatneedle exact archival body")]
    )
    assert (
        archive_shape()
        == archive_before
        == (
            1,
            ((str(source["id"]), "legacycompatneedle exact archival body"),),
        )
    )


def test_request_parser_rejects_query_fields_duplicate_json_and_nonfinite_values() -> None:
    request = _request("conv_0000000000000001", "msg_0000000000000001")
    payload = request.to_private_payload()
    payload["query"] = "there is no query lane"
    with pytest.raises(MessageExactContractError):
        MessageExactRequest.from_private_payload(payload)

    canonical = request.to_private_json()
    duplicate = canonical.replace(
        '"schema":"friday.message-exact-request.private.v1"',
        '"schema":"friday.message-exact-request.private.v1",'
        '"schema":"friday.message-exact-request.private.v1"',
    )
    with pytest.raises(MessageExactContractError, match="duplicate"):
        MessageExactRequest.parse_private(duplicate)
    nonfinite = canonical.replace('"page_size":50', '"page_size":NaN')
    with pytest.raises(MessageExactContractError, match="non-finite"):
        MessageExactRequest.parse_private(nonfinite)
