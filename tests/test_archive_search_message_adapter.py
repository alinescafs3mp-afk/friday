from __future__ import annotations

import copy
import pickle
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from friday.retrieval.archive_search_contract import (
    ArchiveContextWindow,
    ArchiveLifecycleConstraint,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
    ConversationScope,
)
from friday.retrieval.archive_search_message_adapter import (
    ArchiveMessageAdapterError,
    archive_message_storage_controls,
)
from friday.retrieval.archive_search_message_adapter import (
    project_archive_message_page as _project_archive_message_page,
)
from friday.retrieval.catalog_contract import (
    CatalogIndexLane,
    CatalogIndexState,
    CatalogIndexStatus,
    IndexIncompleteReason,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    LifecycleState,
    MessageRole,
    MessageWindowLocator,
    SearchCorpus,
    SearchExecutionBinding,
    SearchLane,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
)
from friday.storage import SCHEMA_VERSION, init_storage
from friday.storage._archive_search_messages import (
    ArchiveMessageScope,
    select_authorized_archive_message_page_in_transaction,
)

CURRENT = "conv_0000000000000001"
OTHER = "conv_0000000000000002"
FOREIGN = "conv_0000000000000003"
BOUNDARY = "msg_0000000000000064"
START = "2026-08-23T08:00:00+00:00"
END = "2026-08-23T09:00:00+00:00"
SECRET = "needle private message body"
TENANT = "tenant-main"
SNAPSHOT = "message-snapshot-1"


def _message_id(number: int) -> str:
    return f"msg_{number:016x}"


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """CREATE TABLE conversations (
                   id TEXT PRIMARY KEY,
                   user_id TEXT NOT NULL,
                   title TEXT NOT NULL DEFAULT '',
                   is_archived INTEGER NOT NULL DEFAULT 0
               );
               CREATE TABLE messages (
                   id TEXT PRIMARY KEY,
                   conversation_id TEXT NOT NULL,
                   user_id TEXT NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL,
                   created_at TEXT NOT NULL
               );
               CREATE TABLE users (
                   id TEXT PRIMARY KEY,
                   status TEXT NOT NULL
               );
               CREATE TABLE schema_meta (
                   key TEXT PRIMARY KEY,
                   value TEXT NOT NULL
               );
               CREATE VIRTUAL TABLE messages_fts USING fts5(
                   content,
                   content=messages,
                   content_rowid=rowid,
                   tokenize='unicode61 remove_diacritics 2'
               );
               CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                   INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
               END;
               CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                   INSERT INTO messages_fts(messages_fts, rowid, content)
                   VALUES ('delete', old.rowid, old.content);
               END;
               CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                   INSERT INTO messages_fts(messages_fts, rowid, content)
                   VALUES ('delete', old.rowid, old.content);
                   INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
               END;"""
        )
        conn.executemany(
            "INSERT INTO users(id, status) VALUES(?, 'active')",
            (("alice",), ("bob",)),
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('fts_build', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.executemany(
            "INSERT INTO conversations(id, user_id, title, is_archived) VALUES(?, ?, ?, ?)",
            (
                (CURRENT, "alice", "Friday project", 0),
                (OTHER, "alice", "Old archive", 1),
                (FOREIGN, "bob", "Foreign title", 0),
            ),
        )
        _insert(conn, 10, CURRENT, "alice", "user", SECRET, "2026-08-23T08:10:00+00:00")
        _insert(
            conn,
            20,
            CURRENT,
            "alice",
            "assistant",
            "context between matches",
            "2026-08-23T08:11:00+00:00",
        )
        _insert(
            conn,
            30,
            CURRENT,
            "alice",
            "assistant",
            "needle second answer",
            "2026-08-23T08:12:00+00:00",
        )
        _insert(
            conn,
            40,
            OTHER,
            "alice",
            "user",
            "needle archived discussion",
            "2026-08-23T08:20:00+00:00",
        )
        _insert(
            conn,
            50,
            FOREIGN,
            "bob",
            "user",
            "needle foreign secret",
            "2026-08-23T08:30:00+00:00",
        )
        _insert(
            conn,
            100,
            CURRENT,
            "alice",
            "user",
            "current request boundary",
            "2026-08-23T09:05:00+00:00",
        )
        conn.commit()
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _insert(
    conn: sqlite3.Connection,
    number: int,
    conversation_id: str,
    principal_id: str,
    role: str,
    content: str,
    created_at: str,
) -> None:
    conn.execute(
        """INSERT INTO messages(
               rowid, id, conversation_id, user_id, role, content, created_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
        (
            number,
            _message_id(number),
            conversation_id,
            principal_id,
            role,
            content,
            created_at,
        ),
    )


def _request(
    *,
    scope: ConversationScope = ConversationScope.CURRENT,
    query: str = "needle",
    limit: int = 10,
    context: ArchiveContextWindow = ArchiveContextWindow(1, 1),
    lifecycle: tuple[LifecycleState, ...] = (
        LifecycleState.ACTIVE,
        LifecycleState.ARCHIVED,
    ),
) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        conversation_scope=scope,
        roles=(MessageRole.USER, MessageRole.ASSISTANT),
        temporal_constraints=(
            ArchiveTemporalConstraint(
                ArchiveSearchCorpus.MESSAGES,
                TemporalRole.CONVERSATION_TIME,
                TemporalValueKind.INSTANT,
                TemporalPrecision.INSTANT,
                START,
                END,
            ),
        ),
        lifecycle_constraints=(ArchiveLifecycleConstraint.create(ArchiveSearchCorpus.MESSAGES, lifecycle),),
        limit=limit,
        context=context,
    )


def _page(
    conn: sqlite3.Connection,
    request: ArchiveSearchRequest,
    *,
    principal_id: str = "alice",
    selector_limit: int | None = None,
):
    controls = archive_message_storage_controls(request)
    return select_authorized_archive_message_page_in_transaction(
        conn,
        principal_id=principal_id,
        query=request.query,
        scope=ArchiveMessageScope(request.conversation_scope.value),
        conversation_id=CURRENT if request.conversation_scope is ConversationScope.CURRENT else None,
        boundary_user_message_id=(
            BOUNDARY if request.conversation_scope is ConversationScope.CURRENT else None
        ),
        roles=controls["roles"],  # type: ignore[arg-type]
        lifecycle_states=controls["lifecycle_states"],  # type: ignore[arg-type]
        since=controls["since"],  # type: ignore[arg-type]
        until=controls["until"],  # type: ignore[arg-type]
        limit=request.limit if selector_limit is None else selector_limit,
        context_before=request.context.before,
        context_after=request.context.after,
    )


def _current_index() -> CatalogIndexState:
    return CatalogIndexState(CatalogIndexLane.LEXICAL, CatalogIndexStatus.CURRENT, None)


def _binding(
    request: ArchiveSearchRequest,
    *,
    tenant_id: str = TENANT,
    principal_id: str = "alice",
    snapshot: str = SNAPSHOT,
    run: str = "message-run-1",
) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        requested_targets=((SearchCorpus.CONVERSATION, SearchLane.MESSAGE_HISTORY),),
        snapshot_discriminator=snapshot,
        run_discriminator=run,
        privacy_key=b"m" * 32,
    )


def project_archive_message_page(  # type: ignore[no-untyped-def]
    *,
    tenant_id: str = TENANT,
    execution_binding: SearchExecutionBinding | None = None,
    snapshot_discriminator: str = SNAPSHOT,
    **kwargs,
):
    request = kwargs["request"]
    assert type(request) is ArchiveSearchRequest
    return _project_archive_message_page(
        tenant_id=tenant_id,
        execution_binding=execution_binding or _binding(request),
        snapshot_discriminator=snapshot_discriminator,
        **kwargs,
    )


def test_current_page_projects_exact_ledger_windows_and_honest_complete_coverage() -> None:
    request = _request()
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None and page.is_valid()
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )

    assert projection.is_valid()
    assert len(projection.candidates) == 1
    candidate = projection.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == CURRENT
    assert candidate.resolved_source.source_ref.principal_id == "alice"
    assert candidate.matches[0].rank == 1
    assert candidate.title == "Friday project"
    assert len(candidate.passages) == 2
    assert all(type(item.passage_ref.locator) is MessageWindowLocator for item in candidate.passages)
    assert len(candidate.temporal_facts) == 2
    assert all(item.role is TemporalRole.CONVERSATION_TIME for item in candidate.temporal_facts)
    assert "context between matches" in " ".join(item.excerpt for item in candidate.passages)

    coverage = projection.to_coverage(
        _binding(request),
        tenant_id=TENANT,
        principal_id="alice",
        snapshot_discriminator=SNAPSHOT,
        returned=1,
    )
    assert coverage.states == (CoverageState.COMPLETE,)
    assert coverage.authority_rechecked is coverage.snapshot_current is True
    assert coverage.eligible_authorized == coverage.examined == 3
    assert coverage.matched_at_least == 2 and coverage.returned == 1


def test_all_scope_merges_passages_by_conversation_and_keeps_unique_lane_ranks() -> None:
    request = _request(scope=ConversationScope.ALL, context=ArchiveContextWindow())
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
        )

    assert len(projection.candidates) == 2
    assert [item.matches[0].rank for item in projection.candidates] == [1, 2]
    assert {item.resolved_source.source_ref.canonical_object_id for item in projection.candidates} == {
        CURRENT,
        OTHER,
    }
    assert all(
        "foreign secret" not in passage.excerpt for item in projection.candidates for passage in item.passages
    )


@pytest.mark.parametrize(
    "request_override",
    (
        {"query": "different"},
        {"limit": 11},
        {"context": ArchiveContextWindow(0, 1)},
        {"lifecycle": (LifecycleState.ACTIVE,)},
    ),
)
def test_projection_rejects_selector_control_drift(request_override: dict[str, object]) -> None:
    selected_request = _request()
    with _database() as conn:
        page = _page(conn, selected_request)
        assert page is not None
        with pytest.raises(ArchiveMessageAdapterError, match="projection failed") as failure:
            project_archive_message_page(
                principal_id="alice",
                request=_request(**request_override),  # type: ignore[arg-type]
                page=page,
                index_state=_current_index(),
                current_conversation_id=CURRENT,
                boundary_user_message_id=BOUNDARY,
            )
    assert SECRET not in str(failure.value)


def test_empty_page_cannot_move_between_principals_conversations_or_boundaries() -> None:
    request = _request(query="absent term")
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None and page.hits == ()
        attempts = (
            {"principal_id": "bob", "current_conversation_id": CURRENT, "boundary": BOUNDARY},
            {"principal_id": "alice", "current_conversation_id": OTHER, "boundary": BOUNDARY},
            {
                "principal_id": "alice",
                "current_conversation_id": CURRENT,
                "boundary": _message_id(99),
            },
        )
        for attempt in attempts:
            with pytest.raises(ArchiveMessageAdapterError, match="projection failed"):
                project_archive_message_page(
                    principal_id=attempt["principal_id"],
                    request=request,
                    page=page,
                    index_state=_current_index(),
                    current_conversation_id=attempt["current_conversation_id"],
                    boundary_user_message_id=attempt["boundary"],
                )


def test_stale_derivative_never_projects_factual_evidence_or_proves_absence() -> None:
    request = _request()
    stale = CatalogIndexState(
        CatalogIndexLane.LEXICAL,
        CatalogIndexStatus.STALE,
        IndexIncompleteReason.SOURCE_CHANGED,
    )
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=stale,
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    assert projection.candidates == ()
    coverage = projection.to_coverage(
        _binding(request),
        tenant_id=TENANT,
        principal_id="alice",
        snapshot_discriminator=SNAPSHOT,
        returned=0,
    )
    assert coverage.states == (CoverageState.PARTIAL, CoverageState.STALE)
    assert coverage.snapshot_current is False
    assert coverage.absence_decision().value == "not_established"


@pytest.mark.parametrize(
    "reason",
    (IndexIncompleteReason.SOURCE_UNAVAILABLE, IndexIncompleteReason.EXTRACTION_FAILED),
)
def test_failed_partial_derivative_is_unavailable_not_backfill_pending(
    reason: IndexIncompleteReason,
) -> None:
    request = _request()
    partial = CatalogIndexState(CatalogIndexLane.LEXICAL, CatalogIndexStatus.PARTIAL, reason)
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=partial,
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    coverage = projection.to_coverage(
        _binding(request),
        tenant_id=TENANT,
        principal_id="alice",
        snapshot_discriminator=SNAPSHOT,
        returned=0,
    )
    assert coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)


def test_exact_rerun_equivalence_detects_ledger_or_metadata_drift() -> None:
    request = _request()
    with _database() as conn:
        first_page = _page(conn, request)
        unchanged_page = _page(conn, request)
        assert first_page is not None and unchanged_page is not None
        first = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=first_page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
        unchanged = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=unchanged_page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
        assert first.same_evidence_as(unchanged)

        _insert(
            conn,
            90,
            CURRENT,
            "alice",
            "assistant",
            "new nonmatching accepted row",
            "2026-08-23T08:50:00+00:00",
        )
        drifted_page = _page(conn, request)
        assert drifted_page is not None
        drifted = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=drifted_page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
        assert not first.same_evidence_as(drifted)


def test_exact_rerun_identity_includes_boundary_and_selector_overfetch_controls() -> None:
    request = _request(limit=1)
    with _database() as conn:
        original_page = _page(conn, request, selector_limit=10)
        wider_page = _page(conn, request, selector_limit=20)
        assert original_page is not None and wider_page is not None
        original = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=original_page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
        wider = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=wider_page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
        assert not original.same_evidence_as(wider)

        conn.execute("UPDATE messages SET content='changed boundary' WHERE rowid=100")
        changed_boundary_page = _page(conn, request, selector_limit=10)
        assert changed_boundary_page is not None
        changed_boundary = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=changed_boundary_page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
        assert not original.same_evidence_as(changed_boundary)


def test_backend_cap_has_no_cursor_until_a_real_tail_exists() -> None:
    request = _request(limit=1)
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None and page.has_more
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    coverage = projection.to_coverage(
        _binding(request),
        tenant_id=TENANT,
        principal_id="alice",
        snapshot_discriminator=SNAPSHOT,
        returned=1,
    )
    assert coverage.states == (CoverageState.CAPPED, CoverageState.PARTIAL)
    assert coverage.next_cursor_available is False


def test_internal_overfetch_can_complete_one_conversation_without_a_fake_tail() -> None:
    request = _request(limit=1)
    with _database() as conn:
        page = _page(conn, request, selector_limit=10)
        assert page is not None and page.has_more is False
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    assert len(projection.candidates) == 1
    coverage = projection.to_coverage(
        _binding(request),
        tenant_id=TENANT,
        principal_id="alice",
        snapshot_discriminator=SNAPSHOT,
        returned=0,
    )
    assert coverage.states == (CoverageState.COMPLETE,)
    assert coverage.limit is None
    assert coverage.next_cursor_available is False


def test_empty_complete_page_never_invents_a_continuation_tail() -> None:
    request = _request(query="definitely-absent")
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None and page.hits == ()
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    coverage = projection.to_coverage(
        _binding(request),
        tenant_id=TENANT,
        principal_id="alice",
        snapshot_discriminator=SNAPSHOT,
        returned=0,
    )
    assert coverage.states == (CoverageState.COMPLETE,)
    assert coverage.next_cursor_available is False


def test_page_and_projection_are_tamper_copy_and_pickle_closed() -> None:
    request = _request()
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    for private in (page, projection):
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError, match="process-private"):
                operation(private)

    object.__setattr__(page.hits[0].message, "content", "tampered body")
    assert page.is_valid() is False
    with pytest.raises(ArchiveMessageAdapterError, match="projection failed"):
        project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    object.__setattr__(projection, "examined", 0)
    assert projection.is_valid() is False


def test_unsupported_hints_and_coverage_actor_or_request_transfer_fail_closed() -> None:
    hinted = ArchiveSearchRequest.create(
        query="needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        title_hints=("private title",),
    )
    with pytest.raises(ArchiveMessageAdapterError, match="hint semantics"):
        archive_message_storage_controls(hinted)

    request = _request()
    with _database() as conn:
        page = _page(conn, request)
        assert page is not None
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
        )
    with pytest.raises(ArchiveMessageAdapterError, match="coverage proof"):
        projection.to_coverage(
            _binding(request),
            tenant_id=TENANT,
            principal_id="bob",
            snapshot_discriminator=SNAPSHOT,
            returned=1,
        )
    with pytest.raises(ArchiveMessageAdapterError, match="coverage proof"):
        projection.to_coverage(
            _binding(request, tenant_id="tenant-other", principal_id="bob", run="bob-run"),
            tenant_id=TENANT,
            principal_id="alice",
            snapshot_discriminator=SNAPSHOT,
            returned=1,
        )
    with pytest.raises(ArchiveMessageAdapterError, match="coverage proof"):
        projection.to_coverage(
            _binding(request, run="different-run"),
            tenant_id=TENANT,
            principal_id="alice",
            snapshot_discriminator=SNAPSHOT,
            returned=1,
        )
    with pytest.raises(ArchiveMessageAdapterError, match="projection failed"):
        project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
            current_conversation_id=CURRENT,
            boundary_user_message_id=BOUNDARY,
            execution_binding=_binding(
                request,
                tenant_id="tenant-other",
                principal_id="bob",
                run="bob-run",
            ),
        )
    with pytest.raises(ArchiveMessageAdapterError, match="coverage proof"):
        projection.to_coverage(
            _binding(_request(query="different")),
            tenant_id=TENANT,
            principal_id="alice",
            snapshot_discriminator=SNAPSHOT,
            returned=1,
        )


def test_real_schema_storage_snapshot_projects_without_a_test_only_path(settings) -> None:
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        conversation = storage.create_conversation("alice", "Real Friday archive")
        storage.store_message(
            conversation["id"],
            "alice",
            "user",
            "real-schema-needle durable discussion",
        )
        request = ArchiveSearchRequest.create(
            query="real-schema-needle",
            corpora=(ArchiveSearchCorpus.MESSAGES,),
            conversation_scope=ConversationScope.ALL,
            roles=(MessageRole.USER, MessageRole.ASSISTANT),
            limit=5,
            context=ArchiveContextWindow(1, 1),
        )
        controls = archive_message_storage_controls(request)
        with storage.transaction() as conn:
            page = select_authorized_archive_message_page_in_transaction(
                conn,
                principal_id="alice",
                query=request.query,
                scope=ArchiveMessageScope.ALL,
                roles=controls["roles"],  # type: ignore[arg-type]
                lifecycle_states=controls["lifecycle_states"],  # type: ignore[arg-type]
                limit=5,
                context_before=1,
                context_after=1,
            )
        assert page is not None and page.is_valid()
        projection = project_archive_message_page(
            principal_id="alice",
            request=request,
            page=page,
            index_state=_current_index(),
        )
        assert len(projection.candidates) == 1
        assert projection.candidates[0].title == "Real Friday archive"
    finally:
        storage.close(final=True)
