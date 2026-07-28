"""Storage methods for conversations, messages and channel sessions.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    Any,
    StorageShared,
    json,
    new_id,
    normalize_conversation_mode,
    utc_now,
)


class ConversationsMixin(StorageShared):
    def create_conversation(
        self,
        user_id: str,
        title: str = "",
        *,
        mode: str = "dialogue",
    ) -> dict[str, Any]:
        self.ensure_user(user_id)
        conversation_id = new_id("conv")
        now = utc_now()
        normalized_mode = normalize_conversation_mode(mode)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO conversations(id, user_id, title, last_message, unread_count,
                   is_pinned, is_archived, mode, created_at, updated_at)
                   VALUES(?, ?, ?, '', 0, 0, 0, ?, ?, ?)""",
                (conversation_id, user_id, title[:200], normalized_mode, now, now),
            )
        return self.get_conversation(conversation_id, user_id) or {}

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def archive_conversation(self, conversation_id: str, user_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET is_archived=1, updated_at=? WHERE id=? AND user_id=?",
                (utc_now(), conversation_id, user_id),
            )
        return cursor.rowcount > 0

    def set_conversation_archived(
        self, conversation_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or unarchive a conversation; archived ones drop out of the default list."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET is_archived=?, updated_at=? WHERE id=? AND user_id=?",
                (1 if archived else 0, utc_now(), conversation_id, user_id),
            )
        return self.get_conversation(conversation_id, user_id)

    def delete_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        """Hard-delete a conversation and its transient history.

        Conversations are chat history, not knowledge provenance (Knowledge Objects
        keep their own Raw Objects), so deletion is immediate rather than two-phase.
        Feedback that targeted this conversation's answers is removed as well, and any
        channel session bound to it is cleared so the next message starts fresh.
        """
        current = self.get_conversation(conversation_id, user_id)
        if not current:
            return {"existed": False, "conversation_id": conversation_id}
        deleted: dict[str, int] = {}
        message_ids_subquery = "SELECT id FROM messages WHERE conversation_id=? AND user_id=?"
        with self.transaction() as conn:
            # feedback_state references feedback(id), so it must be cleared first.
            for table in ("feedback_state", "feedback"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE user_id=? "  # nosec B608
                    f"AND target_id IN ({message_ids_subquery})",
                    (user_id, conversation_id, user_id),
                )
                if cursor.rowcount:
                    deleted[table] = cursor.rowcount
            messages = conn.execute(
                "DELETE FROM messages WHERE conversation_id=? AND user_id=?",
                (conversation_id, user_id),
            )
            deleted["messages"] = messages.rowcount
            sessions = conn.execute(
                "DELETE FROM channel_sessions WHERE user_id=? AND conversation_id=?",
                (user_id, conversation_id),
            )
            if sessions.rowcount:
                deleted["channel_sessions"] = sessions.rowcount
            conversation = conn.execute(
                "DELETE FROM conversations WHERE id=? AND user_id=?",
                (conversation_id, user_id),
            )
            deleted["conversations"] = conversation.rowcount
        return {"existed": True, "conversation_id": conversation_id, "deleted": deleted}

    def set_conversation_mode(self, conversation_id: str, user_id: str, mode: str) -> dict[str, Any] | None:
        normalized_mode = normalize_conversation_mode(mode)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET mode=?, updated_at=? WHERE id=? AND user_id=?",
                (normalized_mode, utc_now(), conversation_id, user_id),
            )
        if cursor.rowcount == 0:
            return None
        return self.get_conversation(conversation_id, user_id)

    def store_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        if not self.get_conversation(conversation_id, user_id):
            raise ValueError("Conversation does not belong to user")
        message_id = new_id("msg")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO messages(id, conversation_id, user_id, role, content,
                   metadata_json, reply_to, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    conversation_id,
                    user_id,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    reply_to,
                    now,
                ),
            )
            conn.execute(
                "UPDATE conversations SET last_message=?, updated_at=? WHERE id=? AND user_id=?",
                (content[:200], now, conversation_id, user_id),
            )
        row = self.execute(
            "SELECT * FROM messages WHERE id=? AND user_id=?", (message_id, user_id)
        ).fetchone()
        return dict(row) if row else {}

    def get_conversation_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        # Select newest N in a subquery, then restore chronological order.
        rows = self.execute(
            """SELECT * FROM (
                   SELECT * FROM messages WHERE conversation_id=? AND user_id=?
                   ORDER BY created_at DESC LIMIT ?
               ) ORDER BY created_at ASC""",
            (conversation_id, user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM messages WHERE id=? AND user_id=?",
            (message_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def count_conversations(self, user_id: str, *, include_archived: bool = False) -> int:
        """Total, so a truncated page can say it is truncated."""
        archived_clause = "" if include_archived else " AND is_archived=0"
        # ``archived_clause`` is selected solely by the boolean argument above.
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM conversations WHERE user_id=?{archived_clause}",  # nosec B608
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_conversations(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One page of conversations, newest first.

        ``offset`` exists because there was none: the listing clamped at 1000 rows
        and `count` was `len(items)`, so a longer history was simply unreachable and
        the response said the total was 1000. Conversations hold the transient
        record of what was actually said, and the oldest are the ones a person goes
        looking for.
        """
        archived_clause = "" if include_archived else " AND is_archived=0"
        # ``archived_clause`` is selected solely by the boolean argument above.
        rows = self.execute(
            f"SELECT * FROM conversations WHERE user_id=?{archived_clause} "  # nosec B608
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, max(1, min(limit, 1000)), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_channel_conversation(self, user_id: str, channel: str, channel_id: str) -> str | None:
        row = self.execute(
            """SELECT s.conversation_id FROM channel_sessions s
               JOIN conversations c ON c.id=s.conversation_id
               WHERE s.user_id=? AND s.channel=? AND s.channel_id=? AND c.is_archived=0""",
            (user_id, channel, channel_id),
        ).fetchone()
        return row["conversation_id"] if row else None

    def get_channel_session(self, user_id: str, channel: str, channel_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT s.*, c.title, c.is_archived
               FROM channel_sessions s
               JOIN conversations c ON c.id=s.conversation_id AND c.user_id=s.user_id
               WHERE s.user_id=? AND s.channel=? AND s.channel_id=?""",
            (user_id, channel, channel_id),
        ).fetchone()
        return dict(row) if row else None

    def set_channel_conversation(
        self,
        user_id: str,
        channel: str,
        channel_id: str,
        conversation_id: str,
        *,
        mode: str | None = None,
    ) -> None:
        conversation = self.get_conversation(conversation_id, user_id)
        if not conversation:
            raise ValueError("Conversation does not belong to user")
        normalized_mode = normalize_conversation_mode(mode or str(conversation.get("mode") or "dialogue"))
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO channel_sessions(user_id, channel, channel_id, conversation_id, mode, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, channel, channel_id) DO UPDATE SET
                     conversation_id=excluded.conversation_id,
                     mode=excluded.mode,
                     updated_at=excluded.updated_at""",
                (user_id, channel, channel_id, conversation_id, normalized_mode, utc_now()),
            )

    def set_channel_mode(
        self, user_id: str, channel: str, channel_id: str, mode: str
    ) -> dict[str, Any] | None:
        normalized_mode = normalize_conversation_mode(mode)
        session = self.get_channel_session(user_id, channel, channel_id)
        if not session:
            return None
        with self.transaction() as conn:
            conn.execute(
                """UPDATE channel_sessions SET mode=?, updated_at=?
                   WHERE user_id=? AND channel=? AND channel_id=?""",
                (normalized_mode, utc_now(), user_id, channel, channel_id),
            )
            conn.execute(
                "UPDATE conversations SET mode=?, updated_at=? WHERE id=? AND user_id=?",
                (normalized_mode, utc_now(), session["conversation_id"], user_id),
            )
        return self.get_channel_session(user_id, channel, channel_id)

    def clear_channel_conversation(self, user_id: str, channel: str, channel_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM channel_sessions WHERE user_id=? AND channel=? AND channel_id=?",
                (user_id, channel, channel_id),
            )
        return cursor.rowcount > 0
