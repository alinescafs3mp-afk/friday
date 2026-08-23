"""Strict single-snapshot storage seam for promoted current-chat windows."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from friday.storage._conversations import (
    select_promoted_current_conversation_window_in_transaction,
)

_CONVERSATION = "conv_0000000000000001"
_OTHER_CONVERSATION = "conv_0000000000000002"
_BOUNDARY = "msg_0000000000000100"
_SINCE = "2026-08-23T08:00:00+00:00"
_UNTIL = "2026-08-23T09:00:00+00:00"
_ROW_KEYS = {"id", "user_id", "conversation_id", "role", "content", "created_at"}


def _message_id(number: int) -> str:
    return f"msg_{number:016x}"


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """CREATE TABLE conversations (
                   id TEXT PRIMARY KEY,
                   user_id TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE messages (
                   id TEXT PRIMARY KEY,
                   conversation_id TEXT NOT NULL,
                   user_id TEXT NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        conn.executemany(
            "INSERT INTO conversations(id, user_id) VALUES(?, ?)",
            ((_CONVERSATION, "alice"), (_OTHER_CONVERSATION, "alice")),
        )
        conn.execute(
            """INSERT INTO messages(
                   rowid, id, conversation_id, user_id, role, content, created_at
               ) VALUES(100, ?, ?, 'alice', 'user', 'current turn',
                        '2026-08-23T09:01:00+00:00')""",
            (_BOUNDARY, _CONVERSATION),
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
    rowid: int | None = None,
    conversation_id: str = _CONVERSATION,
    user_id: str = "alice",
    role: str = "user",
    content: str | None = None,
    created_at: str = "2026-08-23T08:15:00+00:00",
) -> None:
    conn.execute(
        """INSERT INTO messages(
               rowid, id, conversation_id, user_id, role, content, created_at
           ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
        (
            number if rowid is None else rowid,
            _message_id(number),
            conversation_id,
            user_id,
            role,
            content if content is not None else f"message {number}",
            created_at,
        ),
    )


def _select(conn: sqlite3.Connection, **overrides: object) -> dict[str, Any] | None:
    arguments: dict[str, Any] = {
        "own_id": "alice",
        "conversation_id": _CONVERSATION,
        "boundary_user_message_id": _BOUNDARY,
        "since": _SINCE,
        "until": _UNTIL,
    }
    arguments.update(overrides)
    return select_promoted_current_conversation_window_in_transaction(
        conn,
        **arguments,
    )


def test_selector_attests_scope_orders_same_second_and_projects_closed_rows() -> None:
    with _database() as conn:
        _insert(conn, 3, rowid=30, role="assistant", content="third")
        _insert(conn, 1, rowid=10, content="first")
        _insert(conn, 2, rowid=20, role="assistant", content="second")
        _insert(
            conn,
            4,
            rowid=40,
            conversation_id=_OTHER_CONVERSATION,
            content="other conversation",
        )
        result = _select(conn)

        assert result is not None
        assert list(result) == [
            "results",
            "boundary",
            "total",
            "shown",
            "complete",
            "since",
            "until",
            "role",
            "limit",
        ]
        assert [row["content"] for row in result["results"]] == [
            "first",
            "second",
            "third",
        ]
        assert all(set(row) == _ROW_KEYS for row in result["results"])
        assert set(result["boundary"]) == _ROW_KEYS
        assert result["boundary"] == {
            "id": _BOUNDARY,
            "user_id": "alice",
            "conversation_id": _CONVERSATION,
            "role": "user",
            "content": "current turn",
            "created_at": "2026-08-23T09:01:00+00:00",
        }
        assert result["total"] == result["shown"] == 3
        assert result["complete"] is True
        assert result["since"] == _SINCE
        assert result["until"] == _UNTIL
        assert result["role"] is None
        assert result["limit"] == 20

        assistants = _select(conn, role="assistant")
        assert assistants is not None
        assert [row["content"] for row in assistants["results"]] == [
            "second",
            "third",
        ]


def test_missing_foreign_or_mismatched_attestation_fails_closed() -> None:
    with _database() as conn:
        conn.execute("INSERT INTO conversations(id, user_id) VALUES('conv_0000000000000003', 'bob')")
        _insert(
            conn,
            200,
            rowid=200,
            conversation_id="conv_0000000000000003",
            user_id="bob",
            content="foreign boundary",
        )
        _insert(
            conn,
            90,
            rowid=90,
            role="assistant",
            content="not a user boundary",
        )

        assert _select(conn, own_id="bob") is None
        assert _select(conn, conversation_id=_OTHER_CONVERSATION) is None
        assert _select(conn, boundary_user_message_id=_message_id(999)) is None
        assert _select(conn, boundary_user_message_id=_message_id(200)) is None
        assert _select(conn, boundary_user_message_id=_message_id(90)) is None
        assert _select(conn, boundary_user_message_id="not-a-message-id") is None
        assert _select(conn, conversation_id="not-a-conversation-id") is None


@pytest.mark.parametrize(
    ("count", "expected_shown", "expected_complete"),
    ((0, 0, True), (20, 20, True), (21, 20, False)),
)
def test_empty_exact_cap_and_cap_plus_one_are_distinguishable(
    count: int,
    expected_shown: int,
    expected_complete: bool,
) -> None:
    with _database() as conn:
        for number in range(1, count + 1):
            _insert(conn, number)

        result = _select(conn)

        assert result is not None
        assert result["total"] == count
        assert result["shown"] == expected_shown
        assert result["complete"] is expected_complete
        assert len(result["results"]) == expected_shown


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"since": "2026-08-23T08:00:00"}, "must include an offset"),
        ({"until": "not-a-time"}, "invalid until boundary"),
        ({"since": _UNTIL}, "must be non-empty"),
        ({"since": "2026-08-23T10:00:00+00:00"}, "must be non-empty"),
        ({"role": "system"}, "invalid message role"),
        ({"limit": 0}, "limit must be between 1 and 20"),
        ({"limit": 21}, "limit must be between 1 and 20"),
        ({"offset": 1}, "offset must be zero"),
    ),
)
def test_invalid_window_controls_are_rejected(overrides: dict[str, object], message: str) -> None:
    with _database() as conn, pytest.raises(ValueError, match=message):
        _select(conn, **overrides)


def test_selector_requires_the_callers_existing_transaction() -> None:
    with _database() as conn:
        conn.rollback()
        with pytest.raises(RuntimeError, match="requires an existing transaction"):
            _select(conn)


def test_repeated_selector_observes_insert_move_content_and_timestamp_changes() -> None:
    with _database() as conn:
        _insert(conn, 1, rowid=10, content="initial")
        _insert(
            conn,
            2,
            rowid=20,
            conversation_id=_OTHER_CONVERSATION,
            content="moved later",
        )
        first = _select(conn)
        assert first is not None
        assert [row["content"] for row in first["results"]] == ["initial"]

        _insert(conn, 3, rowid=30, content="inserted")
        inserted = _select(conn)
        assert inserted is not None
        assert [row["content"] for row in inserted["results"]] == [
            "initial",
            "inserted",
        ]

        conn.execute(
            "UPDATE messages SET conversation_id=? WHERE id=?",
            (_CONVERSATION, _message_id(2)),
        )
        moved = _select(conn)
        assert moved is not None
        assert [row["content"] for row in moved["results"]] == [
            "initial",
            "moved later",
            "inserted",
        ]

        conn.execute("UPDATE messages SET content='changed' WHERE id=?", (_message_id(1),))
        changed = _select(conn)
        assert changed is not None
        assert changed["results"][0]["content"] == "changed"

        conn.execute(
            "UPDATE messages SET created_at='2026-08-23T09:30:00+00:00' WHERE id=?",
            (_message_id(3),),
        )
        shifted = _select(conn)
        assert shifted is not None
        assert [row["content"] for row in shifted["results"]] == [
            "changed",
            "moved later",
        ]
        assert shifted["total"] == shifted["shown"] == 2


def test_attestation_page_and_total_use_one_sql_statement() -> None:
    with _database() as conn:
        _insert(conn, 1)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        result = _select(conn)

        conn.set_trace_callback(None)
        assert result is not None
        reads = [statement for statement in statements if statement.lstrip().upper().startswith("WITH")]
        assert len(reads) == 1
        assert "owned_conversation AS MATERIALIZED" in reads[0]
        assert "totals AS" in reads[0]
