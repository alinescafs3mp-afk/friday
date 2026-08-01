"""Storage methods for conversations, messages and channel sessions.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from friday.storage._base import (
    Any,
    StorageShared,
    json,
    new_id,
    normalize_conversation_mode,
    sqlite3,
    utc_now,
)
from friday.storage._knowledge import _fts_terms


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
        """Убрать разговор из списка, сохранив всё сказанное.

        Раньше здесь стоял `DELETE FROM messages` и `DELETE FROM conversations`.
        По требованию владельца (2026-08-01) сказанное в чате неудаляемо: попало
        в чат один раз — и всё. Запрет стоит триггерами в самой базе, так что
        прежний код теперь просто не выполнился бы; вместо него — архивирование.

        Что действительно уходит: привязка канала к разговору. Она не история, а
        указатель «куда писать дальше», и очистить его нужно, иначе следующее
        сообщение из Telegram продолжит убранный разговор.

        Имя метода сохранено: его зовёт маршрут `DELETE /api/conversations/{id}`
        и обе панели. Снаружи смысл прежний — «убрать из списка», — а история
        остаётся и находится поиском по переписке.
        """
        current = self.get_conversation(conversation_id, user_id)
        if not current:
            return {"existed": False, "conversation_id": conversation_id}
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET is_archived=1, updated_at=? WHERE id=? AND user_id=?",
                (utc_now(), conversation_id, user_id),
            )
            sessions = conn.execute(
                "DELETE FROM channel_sessions WHERE user_id=? AND conversation_id=?",
                (user_id, conversation_id),
            )
            kept = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND user_id=?",
                (conversation_id, user_id),
            ).fetchone()[0]
        return {
            "existed": True,
            "conversation_id": conversation_id,
            "archived": True,
            "messages_kept": int(kept),
            "deleted": {"channel_sessions": sessions.rowcount} if sessions.rowcount else {},
        }

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

    def set_conversation_title(self, conversation_id: str, user_id: str, title: str) -> dict[str, Any] | None:
        """Rename a conversation the caller owns; foreign ids are a silent miss."""
        clean = " ".join((title or "").split()).strip()[:200]
        if not clean:
            return None
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
                (clean, utc_now(), conversation_id, user_id),
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

    def count_messages(self, conversation_id: str, *, user_id: str) -> int:
        """How many messages the conversation holds — both conditions from the listing."""
        row = self.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conversation_id=? AND user_id=?",
            (conversation_id, user_id),
        ).fetchone()
        return int(row["count"] if row else 0)

    def get_conversation_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        limit: int = 50,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """A window of the conversation, chronological, defaulting to the tail.

        The old shape — newest N in a subquery, then `ORDER BY created_at ASC` around
        it — did not restore chronological order at all when timestamps tie, and they
        tie constantly: `created_at` is written to second precision, so a question and
        its answer usually share one. An outer sort on an equal key preserves the inner
        DESC order, so each pair came back ANSWER FIRST — measured as
        `A09, A10, Q10, A11, Q11`, and with forty messages inside one second the whole
        conversation came back reversed. That history also feeds the agent's prompt.

        `rowid` is the tiebreaker, not `id`: ids are `uuid4().hex[:16]`, so ordering by
        one is deterministic and chronologically meaningless. The neighbouring
        `list_conversations` uses `, id DESC` because there the order inside a second
        carries no meaning; here it does.

        `offset` counts from the START of the conversation, like every other paged list,
        so the same pager arithmetic applies. Omitting it keeps today's behaviour — the
        last `limit` messages.
        """
        window = max(1, min(limit, 1000))
        if offset is None:
            total = self.count_messages(conversation_id, user_id=user_id)
            offset = max(0, total - window)
        rows = self.execute(
            """SELECT * FROM messages WHERE conversation_id=? AND user_id=?
               ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?""",
            (conversation_id, user_id, window, max(0, offset)),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM messages WHERE id=? AND user_id=?",
            (message_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def search_messages(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search over the caller's own chat history.

        Tenant boundary is ``user_id`` alone: another user's messages are never
        visible, even to an owner actor. Conversation scoping is optional and
        still requires the row to belong to ``user_id`` — filtering by a foreign
        conversation id simply returns nothing.
        """
        text = " ".join((query or "").split()).strip()
        if not text:
            return []
        window = max(1, min(int(limit), 200))
        conv = " ".join(str(conversation_id or "").split()).strip() or None
        rows: list[sqlite3.Row] = []
        terms = _fts_terms(text)
        if self._fts_available and terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            try:
                if conv is not None:
                    rows = self.execute(
                        """SELECT m.*, bm25(messages_fts) AS _rank
                           FROM messages_fts
                           JOIN messages m ON m.rowid=messages_fts.rowid
                           WHERE m.user_id=? AND m.conversation_id=? AND messages_fts MATCH ?
                           ORDER BY _rank ASC, m.created_at DESC LIMIT ?""",
                        (user_id, conv, match_query, window),
                    ).fetchall()
                else:
                    rows = self.execute(
                        """SELECT m.*, bm25(messages_fts) AS _rank
                           FROM messages_fts
                           JOIN messages m ON m.rowid=messages_fts.rowid
                           WHERE m.user_id=? AND messages_fts MATCH ?
                           ORDER BY _rank ASC, m.created_at DESC LIMIT ?""",
                        (user_id, match_query, window),
                    ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            like = f"%{escaped}%"
            if conv is not None:
                rows = self.execute(
                    """SELECT * FROM messages
                       WHERE user_id=? AND conversation_id=? AND content LIKE ? ESCAPE '\\'
                       ORDER BY created_at DESC LIMIT ?""",
                    (user_id, conv, like, window),
                ).fetchall()
            else:
                rows = self.execute(
                    """SELECT * FROM messages
                       WHERE user_id=? AND content LIKE ? ESCAPE '\\'
                       ORDER BY created_at DESC LIMIT ?""",
                    (user_id, like, window),
                ).fetchall()
        return [dict(row) for row in rows]

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

    def what_happened(
        self,
        user_id: str,
        *,
        since: str,
        until: str,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        """Что происходило в промежутке времени — одной лентой, по часам.

        Требование владельца (2026-08-01): «вся информация в чате, все файлы
        фиксируются по времени и дате», и на вопрос «что было 26 июля в 15 часов»
        Пятница обязана ответить уверенно.

        Обычный поиск на такой вопрос не отвечает: он ищет СЛОВА, а спрашивают о
        МОМЕНТЕ. Ключевые слова там — «26 июля» и «15 часов», и по ним найдутся
        документы, где эти даты УПОМЯНУТЫ, а не те, что появились тогда.

        Лента собирается из двух источников, потому что «происходило» — это и
        разговор, и поступления:

        * сообщения чата — что говорили;
        * объекты знания — что появилось в архиве (файл, страница из интернета,
          сохранённая заметка), с пометкой, откуда пришло.

        Границы — строки ISO; сравнение строковое, что для ISO-времени совпадает
        с хронологическим. Хранится всё в UTC, поэтому вызывающий обязан привести
        границы к UTC сам — иначе «15 часов» будет чужим часом.
        """
        window = max(1, min(int(limit), 200))
        # Из базы берётся с запасом, а прореживание идёт уже по собранному: иначе
        # «равномерная выборка» получалась из первых N строк каждого источника,
        # то есть снова из начала промежутка. Потолок — чтобы день с тысячами
        # событий не поднимался в память целиком.
        fetch_window = min(1000, max(window * 8, 200))
        events: list[dict[str, Any]] = []
        for row in self.execute(
            """SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, c.title
               FROM messages m LEFT JOIN conversations c ON c.id = m.conversation_id
               WHERE m.user_id=? AND m.created_at >= ? AND m.created_at <= ?
               ORDER BY m.created_at ASC LIMIT ?""",
            (user_id, since, until, fetch_window),
        ):
            events.append(
                {
                    "kind": "message",
                    "at": str(row["created_at"]),
                    "role": str(row["role"] or ""),
                    "text": str(row["content"] or "")[:600],
                    "conversation_id": str(row["conversation_id"] or ""),
                    "conversation": str(row["title"] or ""),
                }
            )
        for row in self.execute(
            """SELECT k.id, k.title, k.content_type, k.created_at, k.summary, r.source, r.source_ref
               FROM knowledge_objects k LEFT JOIN raw_objects r ON r.id = k.raw_object_id
               WHERE k.user_id=? AND k.deleted_at IS NULL
                 AND k.created_at >= ? AND k.created_at <= ?
               ORDER BY k.created_at ASC LIMIT ?""",
            (user_id, since, until, fetch_window),
        ):
            events.append(
                {
                    "kind": "document",
                    "at": str(row["created_at"]),
                    "id": str(row["id"]),
                    "title": str(row["title"] or ""),
                    "content_type": str(row["content_type"] or ""),
                    "summary": str(row["summary"] or "")[:300],
                    "source": str(row["source"] or ""),
                    "source_ref": str(row["source_ref"] or "")[:300],
                }
            )
        events.sort(key=lambda item: (str(item["at"]), str(item.get("kind"))))
        if len(events) <= window:
            return events
        # Прежде возвращались первые N — то есть на дне с 541 событием человек
        # видел утро и не знал, что было дальше. Берём равномерную выборку по
        # всему промежутку: начало, середина и конец обязаны быть представлены,
        # иначе «что было вчера» отвечает про первый час.
        step = len(events) / float(window)
        picked = [events[min(len(events) - 1, int(index * step))] for index in range(window)]
        # Последнее событие промежутка показываем всегда: чем всё кончилось —
        # обычно самое важное в вопросе «что было».
        if picked[-1] is not events[-1]:
            picked[-1] = events[-1]
        return picked

    def count_what_happened(self, user_id: str, *, since: str, until: str) -> dict[str, int]:
        """Сколько всего событий в промежутке — отдельным счётом, без потолка.

        Длина показанной страницы — не факт о промежутке: сказать «за этот час
        было 60 событий», показав ровно свои 60, значит выдать размер запроса за
        свойство архива.
        """
        messages = self.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE user_id=? AND created_at >= ? AND created_at <= ?",
            (user_id, since, until),
        ).fetchone()["c"]
        documents = self.execute(
            """SELECT COUNT(*) AS c FROM knowledge_objects
               WHERE user_id=? AND deleted_at IS NULL AND created_at >= ? AND created_at <= ?""",
            (user_id, since, until),
        ).fetchone()["c"]
        return {"messages": int(messages), "documents": int(documents), "total": int(messages) + int(documents)}
