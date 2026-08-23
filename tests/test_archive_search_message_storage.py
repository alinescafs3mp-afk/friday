from __future__ import annotations

import copy
import pickle
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from friday.retrieval.contracts import LifecycleState, MessageRole
from friday.storage._archive_search_messages import (
    ArchiveMessageScope,
    ArchiveMessageSearchPage,
    ArchiveMessageStorageError,
    select_authorized_archive_message_page_in_transaction,
)

_CURRENT = "conv_0000000000000001"
_OTHER = "conv_0000000000000002"
_FOREIGN = "conv_0000000000000003"
_BOUNDARY = "msg_0000000000000100"
_SINCE = "2026-08-23T08:00:00+00:00"
_UNTIL = "2026-08-23T09:00:00+00:00"


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
            "INSERT INTO conversations(id, user_id, is_archived) VALUES(?, ?, ?)",
            (
                (_CURRENT, "alice", 0),
                (_OTHER, "alice", 1),
                (_FOREIGN, "bob", 0),
            ),
        )
        conn.execute(
            """INSERT INTO messages(
                   rowid, id, conversation_id, user_id, role, content, created_at
               ) VALUES(100, ?, ?, 'alice', 'user', 'current private question',
                        '2026-08-23T09:05:00+00:00')""",
            (_BOUNDARY, _CURRENT),
        )
        conn.commit()
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _insert(
    conn: sqlite3.Connection,
    number: int,
    *,
    conversation_id: str = _CURRENT,
    principal_id: str = "alice",
    role: str = "user",
    content: str = "plain context",
    created_at: str = "2026-08-23T08:30:00+00:00",
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


def _current(conn: sqlite3.Connection, **overrides: object) -> ArchiveMessageSearchPage | None:
    values: dict[str, object] = {
        "principal_id": "alice",
        "query": "needle",
        "scope": ArchiveMessageScope.CURRENT,
        "conversation_id": _CURRENT,
        "boundary_user_message_id": _BOUNDARY,
        "since": _SINCE,
        "until": _UNTIL,
    }
    values.update(overrides)
    return select_authorized_archive_message_page_in_transaction(conn, **values)  # type: ignore[arg-type]


def test_current_scope_authorizes_before_recall_and_returns_exact_bounded_context() -> None:
    with _database() as conn:
        _insert(conn, 10, content="first private row", created_at="2026-08-23T08:10:00+00:00")
        _insert(
            conn,
            20,
            role="assistant",
            content="assistant before",
            created_at="2026-08-23T08:11:00+00:00",
        )
        _insert(
            conn,
            30,
            role="assistant",
            content="needle exact hit",
            created_at="2026-08-23T08:12:00+00:00",
        )
        _insert(conn, 40, content="user after", created_at="2026-08-23T08:13:00+00:00")
        _insert(
            conn,
            50,
            role="assistant",
            content="assistant tail",
            created_at="2026-08-23T08:14:00+00:00",
        )
        _insert(conn, 60, role="system", content="needle hidden system row")
        _insert(conn, 70, content="needle at excluded end", created_at=_UNTIL)
        _insert(conn, 110, content="needle inserted after boundary", created_at="2026-08-23T08:15:00+00:00")
        _insert(
            conn,
            120,
            conversation_id=_OTHER,
            content="needle other owned conversation",
        )
        _insert(
            conn,
            130,
            conversation_id=_FOREIGN,
            principal_id="bob",
            content="needle foreign secret",
        )

        page = _current(
            conn,
            roles=(MessageRole.ASSISTANT,),
            context_before=1,
            context_after=1,
        )
        assert page is not None
        assert page.total == page.returned == 1
        assert page.examined == 3
        assert page.has_more is False
        assert page.roles == (MessageRole.ASSISTANT,)
        hit = page.hits[0]
        assert hit.match_rank == 1
        assert hit.message.content == "needle exact hit"
        assert [(item.relative_position, item.row.content) for item in hit.context] == [
            (-1, "assistant before"),
            (0, "needle exact hit"),
            (1, "user after"),
        ]
        assert hit.ledger.row_count == 5
        assert hit.ledger.boundary_identity_sha256 == page.boundary_identity_sha256
        assert len(hit.ledger.row_ledger_sha256) == 64

        rendered = repr(page) + repr(hit) + repr(hit.message) + repr(hit.ledger)
        for private in ("needle", "alice", _CURRENT, _BOUNDARY, "foreign secret"):
            assert private not in rendered
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError, match="process-private"):
                operation(page)


def test_all_scope_has_exact_totals_stable_ties_and_never_crosses_conversations() -> None:
    with _database() as conn:
        shared_time = "2026-08-23T08:20:00+00:00"
        _insert(conn, 10, content="needle same rank", created_at=shared_time)
        _insert(
            conn,
            19,
            conversation_id=_OTHER,
            role="assistant",
            content="other before",
            created_at="2026-08-23T08:19:00+00:00",
        )
        _insert(conn, 20, conversation_id=_OTHER, content="needle same rank", created_at=shared_time)
        _insert(
            conn,
            21,
            conversation_id=_OTHER,
            role="assistant",
            content="other after",
            created_at="2026-08-23T08:21:00+00:00",
        )
        _insert(
            conn,
            30,
            conversation_id=_FOREIGN,
            principal_id="bob",
            content="needle foreign top rank",
            created_at="2026-08-23T08:59:00+00:00",
        )
        _insert(conn, 40, content="needle excluded boundary instant", created_at=_UNTIL)

        first = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id="alice",
            query="needle",
            scope=ArchiveMessageScope.ALL,
            since=_SINCE,
            until=_UNTIL,
            limit=1,
            context_before=1,
            context_after=1,
        )
        assert first is not None
        assert first.total == 2 and first.returned == 1 and first.has_more is True
        assert first.hits[0].message.conversation_id == _OTHER
        assert [item.row.content for item in first.hits[0].context] == [
            "other before",
            "needle same rank",
            "other after",
        ]
        assert {item.row.conversation_id for item in first.hits[0].context} == {_OTHER}
        assert len(first.ledgers) == 1

        complete = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id="alice",
            query="needle",
            scope=ArchiveMessageScope.ALL,
            since=_SINCE,
            until=_UNTIL,
            limit=2,
        )
        assert complete is not None
        assert [item.message.conversation_id for item in complete.hits] == [_OTHER, _CURRENT]
        assert [item.match_rank for item in complete.hits] == [1, 2]
        assert complete.total == complete.returned == 2
        assert complete.has_more is False
        assert len(complete.ledgers) == 2
        assert all(item.conversation_id != _FOREIGN for item in complete.ledgers)


def test_foreign_corpus_cannot_change_principal_local_lexical_ranks_or_scores() -> None:
    with _database() as conn:
        shared_time = "2026-08-23T08:20:00+00:00"
        _insert(
            conn,
            5,
            content="alpha beta",
            created_at="2026-08-23T08:10:00+00:00",
        )
        _insert(conn, 10, content="alpha", created_at=shared_time)
        _insert(conn, 20, content="beta", created_at=shared_time)

        def ranked() -> list[tuple[str, float]]:
            page = select_authorized_archive_message_page_in_transaction(
                conn,
                principal_id="alice",
                query="alpha beta",
                scope=ArchiveMessageScope.ALL,
                since=_SINCE,
                until=_UNTIL,
            )
            assert page is not None
            return [(item.message.content, item.lexical_score) for item in page.hits]

        before = ranked()
        assert before == [("alpha beta", -2.0), ("beta", -1.0), ("alpha", -1.0)]
        for number in range(300, 360):
            _insert(
                conn,
                number,
                conversation_id=_FOREIGN,
                principal_id="bob",
                content="beta",
                created_at=shared_time,
            )
        assert ranked() == before


def test_lifecycle_is_applied_before_ranking_limit_and_coverage_counts() -> None:
    with _database() as conn:
        _insert(
            conn,
            10,
            conversation_id=_CURRENT,
            content="needle active",
            created_at="2026-08-23T08:10:00+00:00",
        )
        _insert(
            conn,
            20,
            conversation_id=_OTHER,
            content="needle archived",
            created_at="2026-08-23T08:50:00+00:00",
        )

        active = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id="alice",
            query="needle",
            scope=ArchiveMessageScope.ALL,
            since=_SINCE,
            until=_UNTIL,
            limit=1,
            lifecycle_states=(LifecycleState.ACTIVE,),
        )
        assert active is not None
        assert active.lifecycle_states == (LifecycleState.ACTIVE,)
        assert [item.message.conversation_id for item in active.hits] == [_CURRENT]
        assert active.examined == active.total == active.returned == 1
        assert active.has_more is False

        archived = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id="alice",
            query="needle",
            scope=ArchiveMessageScope.ALL,
            since=_SINCE,
            until=_UNTIL,
            limit=1,
            lifecycle_states=(LifecycleState.ARCHIVED,),
        )
        assert archived is not None
        assert [item.message.conversation_id for item in archived.hits] == [_OTHER]
        assert archived.examined == archived.total == archived.returned == 1

        excluded_current = _current(
            conn,
            lifecycle_states=(LifecycleState.ARCHIVED,),
        )
        assert excluded_current is not None
        assert excluded_current.examined == excluded_current.total == excluded_current.returned == 0

        deleted = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id="alice",
            query="needle",
            scope=ArchiveMessageScope.ALL,
            lifecycle_states=(LifecycleState.DELETED,),
        )
        assert deleted is not None
        assert deleted.examined == deleted.total == deleted.returned == 0


def test_current_boundary_fail_closed_empty_is_distinct_and_controls_are_closed() -> None:
    with _database() as conn:
        _insert(conn, 10, content="ordinary authorized history")
        _insert(
            conn,
            200,
            conversation_id=_FOREIGN,
            principal_id="bob",
            content="foreign boundary",
        )
        _insert(conn, 90, role="assistant", content="assistant cannot be boundary")

        assert _current(conn, principal_id="bob") is None
        assert _current(conn, boundary_user_message_id=_message_id(200)) is None
        assert _current(conn, boundary_user_message_id=_message_id(90)) is None

        empty = _current(conn, query="absent lexical token")
        assert empty is not None
        assert empty.total == empty.returned == 0
        assert empty.examined == 2
        assert empty.has_more is False
        assert empty.ledgers == ()

        with pytest.raises(ArchiveMessageStorageError, match="time window"):
            _current(conn, since=_UNTIL, until=_SINCE)
        with pytest.raises(ArchiveMessageStorageError, match="context radius"):
            _current(conn, context_before=4)
        with pytest.raises(ArchiveMessageStorageError, match="roles"):
            _current(conn, roles=(MessageRole.SYSTEM,))
        with pytest.raises(ArchiveMessageStorageError, match="all-conversation"):
            select_authorized_archive_message_page_in_transaction(
                conn,
                principal_id="alice",
                query="needle",
                scope=ArchiveMessageScope.ALL,
                conversation_id=_CURRENT,
            )

    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="caller-owned transaction"):
            select_authorized_archive_message_page_in_transaction(
                conn,
                principal_id="alice",
                query="needle",
            )
    finally:
        conn.close()


def test_current_ledger_changes_only_for_rows_admitted_before_exact_boundary() -> None:
    with _database() as conn:
        _insert(conn, 10, content="needle stable hit")
        first = _current(conn)
        assert first is not None
        first_ledger = first.hits[0].ledger.row_ledger_sha256

        _insert(conn, 90, role="assistant", content="newly admitted pre-boundary row")
        admitted = _current(conn)
        assert admitted is not None
        assert admitted.hits[0].ledger.row_ledger_sha256 != first_ledger
        assert admitted.hits[0].ledger.row_count == first.hits[0].ledger.row_count + 1

        _insert(conn, 110, content="post-boundary append must not drift old window")
        later = _current(conn)
        assert later is not None
        assert later.hits[0].ledger.row_ledger_sha256 == admitted.hits[0].ledger.row_ledger_sha256
        assert later.boundary_identity_sha256 == admitted.boundary_identity_sha256


def test_interrupted_selector_leaves_caller_transaction_and_rows_untouched() -> None:
    with _database() as conn:
        for number in range(1, 90):
            _insert(conn, number, content=f"needle row {number}")
        before = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        conn.set_progress_handler(lambda: 1, 1)
        try:
            with pytest.raises(ArchiveMessageStorageError, match="selection is unavailable") as error:
                _current(conn)
            assert error.value.__cause__ is None
        finally:
            conn.set_progress_handler(None, 0)
        assert conn.in_transaction is True
        assert int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]) == before
        assert _current(conn) is not None


def test_fts_unavailable_is_explicit_not_a_false_empty_result() -> None:
    with _database() as conn:
        conn.execute("DROP TABLE messages_fts")
        with pytest.raises(ArchiveMessageStorageError, match="selection is unavailable") as error:
            _current(conn)
        assert error.value.__cause__ is None


def test_database_decode_failure_is_body_free() -> None:
    with _database() as conn:
        conn.execute(
            """INSERT INTO messages(
                   rowid, id, conversation_id, user_id, role, content, created_at
               ) VALUES(10, ?, ?, 'alice', 'user',
                        CAST(x'6e6565646c65208050524956415445' AS TEXT), ?)""",
            (_message_id(10), _CURRENT, "2026-08-23T08:20:00+00:00"),
        )

        with pytest.raises(ArchiveMessageStorageError, match="selection is unavailable") as error:
            _current(conn)
        rendered = str(error.value) + repr(error.value)
        assert "PRIVATE" not in rendered and "needle" not in rendered
        assert error.value.__cause__ is None
