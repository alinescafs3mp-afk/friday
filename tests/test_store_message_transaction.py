from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from friday.storage import _conversations
from friday.storage._conversations import (
    create_conversation_in_transaction,
    store_message_in_transaction,
)


def test_create_conversation_delegates_once_and_preserves_the_stored_row(
    storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = "alice"
    title = "Точный заголовок " + "я" * 220
    created_at = "2026-08-18T09:10:11Z"
    conversation_id = "conv_transaction_exact"
    original = create_conversation_in_transaction
    observed_connections: list[sqlite3.Connection] = []
    storage.ensure_user(user_id)

    monkeypatch.setattr(_conversations, "new_id", lambda prefix: conversation_id)
    monkeypatch.setattr(_conversations, "utc_now", lambda: created_at)

    def observed_create(
        conn: sqlite3.Connection,
        forwarded_user_id: str,
        forwarded_title: str = "",
        mode: str = "dialogue",
    ) -> dict[str, Any]:
        observed_connections.append(conn)
        assert conn.in_transaction
        return original(conn, forwarded_user_id, forwarded_title, mode)

    monkeypatch.setattr(_conversations, "create_conversation_in_transaction", observed_create)

    stored = storage.create_conversation(user_id, title, mode="knowledge")

    assert observed_connections == [storage.conn]
    assert stored == {
        "id": conversation_id,
        "user_id": user_id,
        "title": title[:200],
        "last_message": "",
        "unread_count": 0,
        "is_pinned": 0,
        "is_archived": 0,
        "mode": "knowledge_work",
        "created_at": created_at,
        "updated_at": created_at,
    }
    rows = storage.execute(
        "SELECT * FROM conversations WHERE id=? AND user_id=?",
        (conversation_id, user_id),
    ).fetchall()
    assert [dict(row) for row in rows] == [stored]


def test_store_message_delegates_once_and_preserves_the_stored_row(
    storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = "alice"
    conversation = storage.create_conversation(user_id, "Transaction publication")
    conversation_id = str(conversation["id"])
    content = "Точная строка: 📦\n" + "x" * 210
    metadata = {"z": "Юникод", "a": {"n": 1}}
    parent = storage.store_message(conversation_id, user_id, "user", "parent")
    created_at = "2026-08-18T10:20:30Z"
    message_id = "msg_transaction_exact"
    original = store_message_in_transaction
    observed_connections: list[sqlite3.Connection] = []

    monkeypatch.setattr(_conversations, "new_id", lambda prefix: message_id)
    monkeypatch.setattr(_conversations, "utc_now", lambda: created_at)

    def observed_store(
        conn: sqlite3.Connection,
        forwarded_conversation_id: str,
        forwarded_user_id: str,
        role: str,
        forwarded_content: str,
        forwarded_metadata: dict[str, Any] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        observed_connections.append(conn)
        assert conn.in_transaction
        return original(
            conn,
            forwarded_conversation_id,
            forwarded_user_id,
            role,
            forwarded_content,
            forwarded_metadata,
            reply_to,
        )

    monkeypatch.setattr(_conversations, "store_message_in_transaction", observed_store)

    stored = storage.store_message(
        conversation_id,
        user_id,
        "assistant",
        content,
        metadata,
        str(parent["id"]),
    )

    assert observed_connections == [storage.conn]
    assert stored == {
        "id": message_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "role": "assistant",
        "content": content,
        "metadata_json": '{"a": {"n": 1}, "z": "Юникод"}',
        "reply_to": parent["id"],
        "created_at": created_at,
    }
    rows = storage.execute(
        "SELECT * FROM messages WHERE conversation_id=? AND user_id=?",
        (conversation_id, user_id),
    ).fetchall()
    assert {str(row["id"]): dict(row) for row in rows} == {
        str(parent["id"]): parent,
        str(stored["id"]): stored,
    }
    assert stored["content"].encode("utf-8") == content.encode("utf-8")
    assert stored["metadata_json"].encode("utf-8") == (
        b'{"a": {"n": 1}, "z": "\xd0\xae\xd0\xbd\xd0\xb8\xd0\xba\xd0\xbe\xd0\xb4"}'
    )
    updated = storage.get_conversation(conversation_id, user_id)
    assert updated is not None
    assert updated["last_message"] == content[:200]
    assert updated["updated_at"] == created_at


def test_transaction_scoped_store_rolls_back_with_its_caller(
    storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = "alice"
    conversation = storage.create_conversation(user_id, "Caller owns commit")
    conversation_id = str(conversation["id"])
    before = storage.get_conversation(conversation_id, user_id)
    assert before is not None

    monkeypatch.setattr(_conversations, "new_id", lambda prefix: "msg_rolled_back")
    monkeypatch.setattr(_conversations, "utc_now", lambda: "2026-08-18T11:22:33Z")

    with pytest.raises(RuntimeError, match="abort caller transaction"), storage.transaction() as conn:
        stored = store_message_in_transaction(
            conn,
            conversation_id,
            user_id,
            "user",
            "Это сообщение не должно пережить rollback",
            {"attempt": 1},
        )
        assert stored["id"] == "msg_rolled_back"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND user_id=?",
                (conversation_id, user_id),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT last_message FROM conversations WHERE id=? AND user_id=?",
                (conversation_id, user_id),
            ).fetchone()[0]
            == "Это сообщение не должно пережить rollback"
        )
        raise RuntimeError("abort caller transaction")

    assert storage.count_messages(conversation_id, user_id=user_id) == 0
    assert storage.get_conversation(conversation_id, user_id) == before


def test_transaction_scoped_conversation_and_messages_roll_back_together(
    storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = "alice"
    storage.ensure_user(user_id)
    generated = {
        "conv": iter(["conv_atomic_rollback"]),
        "msg": iter(["msg_user_rollback", "msg_assistant_rollback"]),
    }

    def deterministic_id(prefix: str) -> str:
        return next(generated[prefix])

    monkeypatch.setattr(_conversations, "new_id", deterministic_id)
    monkeypatch.setattr(_conversations, "utc_now", lambda: "2026-08-18T12:34:56Z")

    with pytest.raises(RuntimeError, match="abort atomic publication"), storage.transaction() as conn:
        conversation = create_conversation_in_transaction(conn, user_id, "Atomic publication")
        conversation_id = str(conversation["id"])
        store_message_in_transaction(conn, conversation_id, user_id, "user", "Вопрос")
        store_message_in_transaction(conn, conversation_id, user_id, "assistant", "Ответ")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            == 2
        )
        raise RuntimeError("abort atomic publication")

    assert storage.get_conversation("conv_atomic_rollback", user_id) is None
    assert storage.count_messages("conv_atomic_rollback", user_id=user_id) == 0


def test_transaction_scoped_store_keeps_the_conversation_owner_boundary(storage: Any) -> None:
    conversation = storage.create_conversation("alice", "Private conversation")
    conversation_id = str(conversation["id"])
    storage.ensure_user("bob")

    with (
        pytest.raises(ValueError, match="Conversation does not belong to user"),
        storage.transaction() as conn,
    ):
        store_message_in_transaction(conn, conversation_id, "bob", "user", "Чужое сообщение")

    assert storage.count_messages(conversation_id, user_id="alice") == 0
    assert storage.count_messages(conversation_id, user_id="bob") == 0
