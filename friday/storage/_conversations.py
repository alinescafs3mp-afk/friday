"""Storage methods for conversations, messages and channel sessions.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from friday.storage._base import (
    Any,
    StorageShared,
    json,
    new_id,
    normalize_conversation_mode,
    sqlite3,
    utc_now,
)
from friday.storage._knowledge import _fts_term_groups
from friday.storage._privacy import (
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
)

_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")


def _validated_reply_parent(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    user_id: str,
    child_role: str,
    reply_to: str | None,
) -> str | None:
    """Keep only one existing, same-scope, opposite-role message edge."""

    candidate = reply_to if isinstance(reply_to, str) else ""
    expected_parent_role = {"user": "assistant", "assistant": "user"}.get(child_role)
    if expected_parent_role is None or _MESSAGE_ID_RE.fullmatch(candidate) is None:
        return None
    parent = conn.execute(
        "SELECT role FROM messages WHERE id=? AND conversation_id=? AND user_id=?",
        (candidate, conversation_id, user_id),
    ).fetchone()
    if parent is None or str(parent["role"] or "") != expected_parent_role:
        return None
    return candidate


def create_conversation_in_transaction(
    conn: sqlite3.Connection,
    user_id: str,
    title: str = "",
    mode: str = "dialogue",
) -> dict[str, Any]:
    """Create one conversation using a transaction owned by the caller."""

    conversation_id = new_id("conv")
    now = utc_now()
    normalized_mode = normalize_conversation_mode(mode)
    conn.execute(
        """INSERT INTO conversations(id, user_id, title, last_message, unread_count,
           is_pinned, is_archived, mode, created_at, updated_at)
           VALUES(?, ?, ?, '', 0, 0, 0, ?, ?, ?)""",
        (conversation_id, user_id, title[:200], normalized_mode, now, now),
    )
    row = conn.execute(
        "SELECT * FROM conversations WHERE id=? AND user_id=?",
        (conversation_id, user_id),
    ).fetchone()
    return dict(row) if row else {}


def store_message_in_transaction(
    conn: sqlite3.Connection,
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Store one message using a transaction owned by the caller."""

    conversation = conn.execute(
        "SELECT id FROM conversations WHERE id=? AND user_id=?",
        (conversation_id, user_id),
    ).fetchone()
    if conversation is None:
        raise ValueError("Conversation does not belong to user")

    validated_reply_to = _validated_reply_parent(
        conn,
        conversation_id=conversation_id,
        user_id=user_id,
        child_role=role,
        reply_to=reply_to,
    )

    message_id = new_id("msg")
    now = utc_now()
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
            validated_reply_to,
            now,
        ),
    )
    conn.execute(
        "UPDATE conversations SET last_message=?, updated_at=? WHERE id=? AND user_id=?",
        (content[:200], now, conversation_id, user_id),
    )
    row = conn.execute("SELECT * FROM messages WHERE id=? AND user_id=?", (message_id, user_id)).fetchone()
    return dict(row) if row else {}


class ConversationsMixin(StorageShared):
    def create_conversation(
        self,
        user_id: str,
        title: str = "",
        *,
        mode: str = "dialogue",
    ) -> dict[str, Any]:
        self.ensure_user(user_id)
        with self.transaction() as conn:
            return create_conversation_in_transaction(conn, user_id, title, mode)

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
        with self.transaction() as conn:
            return store_message_in_transaction(
                conn,
                conversation_id,
                user_id,
                role,
                content,
                metadata,
                reply_to,
            )

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
        role: str | None = None,
        before_message_id: str | None = None,
        match_all_terms: bool = False,
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
        selected_role = str(role or "").strip().casefold() or None
        if selected_role not in {None, "user", "assistant"}:
            raise ValueError("invalid message role")
        before = " ".join(str(before_message_id or "").split()).strip() or None
        if before is not None and len(before) > 200:
            raise ValueError("invalid message boundary identity")
        clauses = ["m.user_id=?"]
        params: list[Any] = [user_id]
        if conv is not None:
            clauses.append("m.conversation_id=?")
            params.append(conv)
        if selected_role is not None:
            clauses.append("m.role=?")
            params.append(selected_role)
        if before is not None:
            clauses.append(
                "m.rowid < (SELECT boundary.rowid FROM messages boundary "
                "WHERE boundary.id=? AND boundary.user_id=?)"
            )
            params.extend((before, user_id))
        where = " AND ".join(clauses)
        rows: list[sqlite3.Row] = []
        term_groups = _fts_term_groups(text)
        if self._fts_available and term_groups:

            def atom(term: str) -> str:
                lexical = term[:-1] if term.endswith("*") else term
                return f'"{lexical.replace(chr(34), chr(34) * 2)}"*'

            if match_all_terms is True:
                grouped = [
                    "(" + " OR ".join(atom(term) for term in group) + ")"
                    if len(group) > 1
                    else atom(group[0])
                    for group in term_groups
                    if group
                ]
                match_query = " AND ".join(grouped)
            else:
                match_query = " OR ".join(atom(term) for group in term_groups for term in group)
            try:
                rows = self.execute(
                    f"""SELECT m.*, bm25(messages_fts) AS _rank
                           FROM messages_fts
                           JOIN messages m ON m.rowid=messages_fts.rowid
                          WHERE {where} AND messages_fts MATCH ?
                          ORDER BY _rank ASC, m.created_at DESC, m.rowid DESC
                          LIMIT ?""",  # nosec B608 - clauses are selected only by fixed branches
                    (*params, match_query, window),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            like = f"%{escaped}%"
            rows = self.execute(
                f"""SELECT m.* FROM messages m
                      WHERE {where} AND m.content LIKE ? ESCAPE '\\'
                      ORDER BY m.created_at DESC, m.rowid DESC
                      LIMIT ?""",  # nosec B608 - clauses are selected only by fixed branches
                (*params, like, window),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages_window(
        self,
        user_id: str,
        since: str,
        until: str,
        *,
        role: str | None = None,
        conversation_id: str | None = None,
        before_message_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one chronological, tenant-scoped half-open message window.

        Unlike FTS search this selector does not require the user's date/time
        words to occur in message bodies.  ``total`` is computed in the same
        SQLite statement as the page, so ``complete`` can never be inferred
        from a top-k result.  Bounds are normalized to UTC and use
        ``[since, until)``; adjacent minute/day windows therefore cannot
        duplicate a boundary row.
        """

        tenant = str(user_id or "").strip()
        if not tenant:
            raise ValueError("user_id is required")

        def utc_boundary(value: str) -> str:
            clean = str(value or "").strip()
            if not clean or len(clean) > 64:
                raise ValueError("invalid message time boundary")
            try:
                parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("invalid message time boundary") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("message time boundary must include an offset")
            return parsed.astimezone(UTC).isoformat()

        start = utc_boundary(since)
        end = utc_boundary(until)
        if start >= end:
            raise ValueError("message time window must be non-empty")
        selected_role = str(role or "").strip().casefold() or None
        if selected_role not in {None, "user", "assistant"}:
            raise ValueError("invalid message role")
        conv = " ".join(str(conversation_id or "").split()).strip() or None
        before = " ".join(str(before_message_id or "").split()).strip() or None
        if before is not None and len(before) > 200:
            raise ValueError("invalid message boundary identity")
        page_size = max(1, min(int(limit), 100))
        page_offset = max(0, min(int(offset), 1_000_000))

        clauses = ["m.user_id=?", "m.created_at>=?", "m.created_at<?"]
        params: list[Any] = [tenant, start, end]
        if selected_role is not None:
            clauses.append("m.role=?")
            params.append(selected_role)
        if conv is not None:
            clauses.append("m.conversation_id=?")
            params.append(conv)
        if before is not None:
            clauses.append(
                "m.rowid < (SELECT boundary.rowid FROM messages boundary "
                "WHERE boundary.id=? AND boundary.user_id=?)"
            )
            params.extend((before, tenant))
        where = " AND ".join(clauses)
        rows = self.execute(
            f"""WITH scoped AS (
                       SELECT m.*, m.rowid AS rowid
                         FROM messages m
                        WHERE {where}
                   ),
                   totals AS (
                       SELECT COUNT(*) AS total FROM scoped
                   ),
                   page AS (
                       SELECT * FROM scoped
                        ORDER BY created_at ASC, rowid ASC
                        LIMIT ? OFFSET ?
                   )
                   SELECT page.*, totals.total AS _total
                     FROM totals LEFT JOIN page ON 1=1
                    ORDER BY page.created_at ASC, page.rowid ASC""",  # nosec B608
            (*params, page_size, page_offset),
        ).fetchall()
        total = int(rows[0]["_total"] if rows else 0)
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("_total", None)
            item.pop("rowid", None)
            if item.get("id") is not None:
                results.append(item)
        shown = len(results)
        complete = page_offset + shown >= total
        return {
            "results": results,
            "total": total,
            "shown": shown,
            "complete": complete,
            "limit": page_size,
            "offset": page_offset,
            "next_offset": None if complete else page_offset + shown,
            "since": start,
            "until": end,
            "role": selected_role,
        }

    def count_conversations(self, user_id: str, *, include_archived: bool = False) -> int:
        """Total, so a truncated page can say it is truncated."""
        archived_clause = "" if include_archived else " AND is_archived=0"
        # ``archived_clause`` is selected solely by the boolean argument above.
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM conversations WHERE user_id=?{archived_clause}",  # nosec B608
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_chat_feed(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Кто писал Пятнице — по одной строке на человека, свежие сверху.

        Владелец попросил «видеть мир глазами Пятницы»: кто и что ей скидывал и
        писал, в виде списка чатов. Сводки по людям в системе не было вовсе —
        были разговоры (по одному человеку за раз) и активность (по одному
        человеку за раз), то есть чтобы узнать, кто вообще писал за день,
        приходилось перебирать учётки руками.

        Одним запросом: последнее сообщение каждого человека, сколько всего
        сообщений и сколько файлов он прислал.

        «Сколько файлов он прислал» считается по ПОМЕТКЕ АВТОРА, а не по учётке в
        строке материала. В общем архиве `user_id` у документа — арендатор, один
        на всех: та же формула приписывала владельцу все загрузки установки
        (1695 файлов на живой базе), а каждому участнику — ноль. Число было
        посчитано верно и отвечало на другой вопрос.

        Пометка пишется с 2026-08-04, и у всего, что принято раньше, автора нет.
        Догадаться о нём нельзя, а приписать кому-нибудь — значит показать
        человеку чужие документы как его. Поэтому такие строки не попадают
        никому и считаются отдельно: `files_without_an_author` — единственный
        честный ответ «не знаю, чьи», без которого «у Ивана 0 файлов» читается
        как «Иван ничего не присылал».
        """
        rows = self.execute(
            """
            WITH ranked_messages AS (
                SELECT m.user_id,
                       m.content,
                       m.role,
                       m.created_at,
                       m.conversation_id,
                       ROW_NUMBER() OVER (PARTITION BY m.user_id ORDER BY m.created_at DESC) AS rn
                FROM messages m
            ),
            last_message AS (
                SELECT user_id, content, role, created_at, conversation_id
                  FROM ranked_messages
                 WHERE rn=1
            ),
            message_counts AS (
                SELECT user_id, COUNT(*) AS message_count
                  FROM messages
                 GROUP BY user_id
            ),
            raw_counts AS (
                SELECT CASE
                         WHEN json_valid(metadata_json)
                          AND json_type(metadata_json,'$.uploaded_by')='text'
                         THEN json_extract(metadata_json,'$.uploaded_by')
                         ELSE ''
                       END AS uploaded_by,
                       COUNT(*) AS arrival_count,
                       SUM(CASE
                             WHEN content_type='file' AND deleted_at IS NULL THEN 1
                             ELSE 0
                           END) AS file_count
                  FROM raw_objects
                 GROUP BY uploaded_by
            )
            SELECT u.id AS user_id,
                   u.display_name,
                   u.username,
                   u.preset_key,
                   u.status,
                   u.metadata_json,
                   lm.content AS last_content,
                   lm.role AS last_role,
                   lm.created_at AS last_at,
                   lm.conversation_id AS last_conversation_id,
                   COALESCE(mc.message_count, 0) AS message_count,
                   COALESCE(rc.file_count, 0) AS file_count
            FROM users u
            LEFT JOIN last_message lm ON lm.user_id=u.id
            LEFT JOIN message_counts mc ON mc.user_id=u.id
            LEFT JOIN raw_counts rc ON rc.uploaded_by=u.id
            WHERE COALESCE(mc.message_count, 0)>0 OR COALESCE(rc.arrival_count, 0)>0
            ORDER BY COALESCE(lm.created_at, u.created_at) DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def files_without_an_author(self) -> int:
        """Сколько файлов в архиве не знают, кто их принёс.

        Ровно тот же вопрос, на который отвечает `arrivals_without_an_author` в
        надзоре, и по той же причине: пока признак автора не писался, приписать
        эти файлы некому. Число стоит рядом с лентой, чтобы «у всех по нулю»
        читалось как «раньше не записывали», а не как «никто ничего не присылал».
        """
        row = self.execute(
            "SELECT COUNT(*) AS n FROM raw_objects "
            "WHERE content_type='file' AND deleted_at IS NULL "
            "AND COALESCE(json_extract(metadata_json,'$.uploaded_by'),'') = ''"
        ).fetchone()
        return int((row["n"] if row else 0) or 0)

    def count_chat_feed(self) -> int:
        """Сколько всего людей в ленте — независимо от размера страницы.

        `len(items)` при `limit` отвечает «сколько я попросил», а не «сколько
        есть». Условие повторяет `list_chat_feed` дословно, иначе два числа
        разойдутся молча.
        """
        row = self.execute(
            """
            WITH message_users AS (
                SELECT user_id FROM messages GROUP BY user_id
            ),
            raw_users AS (
                SELECT CASE
                         WHEN json_valid(metadata_json)
                          AND json_type(metadata_json,'$.uploaded_by')='text'
                         THEN json_extract(metadata_json,'$.uploaded_by')
                         ELSE ''
                       END AS uploaded_by
                  FROM raw_objects
                 GROUP BY uploaded_by
            )
            SELECT COUNT(*) AS n FROM users u
            LEFT JOIN message_users mu ON mu.user_id=u.id
            LEFT JOIN raw_users ru ON ru.uploaded_by=u.id
            WHERE mu.user_id IS NOT NULL OR ru.uploaded_by IS NOT NULL
            """
        ).fetchone()
        return int((row["n"] if row else 0) or 0)

    def list_chat_thread(self, user_id: str, *, limit: int = 500) -> dict[str, Any]:
        """One bounded chronological tail across all of a person's conversations.

        The admin messenger is a person-level view.  Building it by first listing
        conversations and then issuing one request per conversation made a click a
        serialized HTTP waterfall and silently omitted every conversation after the
        fifth.  This query chooses the newest messages globally in one snapshot,
        reports the full total honestly, and only then restores chronological order.
        Internal rowids and the window counter never leave storage.
        """

        window = max(1, min(int(limit), 1000))
        rows = self.execute(
            """
            WITH newest AS (
                SELECT m.*,
                       m.rowid AS _message_rowid,
                       COUNT(*) OVER () AS _thread_total
                  FROM messages m
                 WHERE m.user_id=?
                 ORDER BY m.created_at DESC, m.rowid DESC
                 LIMIT ?
            )
            SELECT * FROM newest
             ORDER BY created_at ASC, _message_rowid ASC
            """,
            (user_id, window),
        ).fetchall()
        total = int(rows[0]["_thread_total"] or 0) if rows else 0
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("_message_rowid", None)
            item.pop("_thread_total", None)
            items.append(item)
        return {"items": items, "total": total, "limit": window}

    def chat_feed_cursor(self) -> dict[str, Any]:
        """Отпечаток ленты переписки: изменилось ли что-нибудь.

        Панель обновляется сама, как обычный мессенджер, и для этого спрашивает
        не всю ленту, а эту метку. `list_chat_feed` — четыре подзапроса и
        оконная функция по всем сообщениям; дёргать её раз в несколько секунд
        ради «а вдруг что-то новое» значило бы держать базу занятой на пустом
        месте.

        Здесь два дешёвых агрегата по индексированным столбцам: время последнего
        сообщения и их общее число. Первого мало само по себе — удаление
        диалога время не двигает, а лента меняется; второго мало тоже — правка
        существующего сообщения меняет содержимое, но не количество. Вместе они
        ловят всё, что видно в ленте.
        """
        row = self.execute(
            "SELECT COUNT(*) AS total, COALESCE(MAX(created_at), '') AS last_at FROM messages"
        ).fetchone()
        return {
            "total": int(row["total"] if row else 0),
            "last_at": str((row["last_at"] if row else "") or ""),
        }

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
               JOIN conversations c ON c.id=s.conversation_id AND c.user_id=s.user_id
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
        person_id: str = "",
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
        # Каждый источник прореживается ВНУТРИ SQLite по всему интервалу. Простое
        # ``ORDER BY ... ASC LIMIT`` здесь ложно: на дне с 1500 событиями оно
        # видит только раннее утро, а затем называет row N «последним событием».
        # NTILE читает весь индексированный диапазон, но возвращает bounded
        # representative rows плюс настоящий хвост; тела тысяч строк в Python не
        # поднимаются.
        fetch_window = min(1000, max(window * 8, 200))
        events: list[dict[str, Any]] = []
        for row in self.execute(
            """WITH ranked AS (
                   SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, c.title,
                          ROW_NUMBER() OVER (ORDER BY m.created_at, m.rowid) AS source_row,
                          COUNT(*) OVER () AS source_total,
                          NTILE(?) OVER (ORDER BY m.created_at, m.rowid) AS sample_bucket
                   FROM messages m LEFT JOIN conversations c
                     ON c.id = m.conversation_id AND c.user_id = m.user_id
                   WHERE m.user_id=? AND m.created_at >= ? AND m.created_at <= ?
               ), sampled AS (
                   SELECT ranked.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY sample_bucket ORDER BY source_row
                          ) AS bucket_row
                   FROM ranked
               )
               SELECT id, conversation_id, role, content, created_at, title
               FROM sampled
               WHERE bucket_row=1 OR source_row=source_total
               ORDER BY created_at, source_row""",
            # Переписка — по ЧЕЛОВЕКУ. Знания ниже — по арендатору: они общие по
            # прямой просьбе владельца. Один параметр на обе границы означал, что
            # участник видел реплики владельца вместо своих (воспроизведено).
            (fetch_window, person_id or user_id, since, until),
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
            f"""WITH source_rows AS (
                   SELECT k.id, k.title, k.content_type, k.created_at, k.summary,
                          r.source, r.source_ref, k.rowid AS storage_row
                   FROM knowledge_objects k
                   JOIN raw_objects r ON r.id = k.raw_object_id AND r.user_id=k.user_id
                        AND {_not_private_raw_dependency("r")}
                   WHERE k.user_id=? AND k.deleted_at IS NULL
                     AND {_not_private_knowledge_dependency("k")}
                     AND k.created_at >= ? AND k.created_at <= ?
               ), ranked AS (
                   SELECT source_rows.*,
                          ROW_NUMBER() OVER (ORDER BY created_at, storage_row) AS source_row,
                          COUNT(*) OVER () AS source_total,
                          NTILE(?) OVER (ORDER BY created_at, storage_row) AS sample_bucket
                   FROM source_rows
               ), sampled AS (
                   SELECT ranked.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY sample_bucket ORDER BY source_row
                          ) AS bucket_row
                   FROM ranked
               )
               SELECT id, title, content_type, created_at, summary, source, source_ref
               FROM sampled
               WHERE bucket_row=1 OR source_row=source_total
               ORDER BY created_at, source_row""",  # nosec B608
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

    def count_what_happened(
        self, user_id: str, *, person_id: str = "", since: str, until: str
    ) -> dict[str, int]:
        """Сколько всего событий в промежутке — отдельным счётом, без потолка.

        Длина показанной страницы — не факт о промежутке: сказать «за этот час
        было 60 событий», показав ровно свои 60, значит выдать размер запроса за
        свойство архива.

        В общем архиве это ДВЕ РАЗНЫЕ границы, и до правки их обслуживал один
        параметр. Переписка личная — она лежит под `own_id` человека; документы и
        знания общие по прямой просьбе владельца — они лежат под арендатором. Один
        `user_id` на обе означал, что участник, спросивший «что было вчера»,
        получал реплики ВЛАДЕЛЬЦА дословно, а своих не видел вовсе.

        Воспроизведено на изолированном стенде: участник получил чужую фразу вместе
        с ролью и заголовком разговора. На живой базе под арендатором лежит 2862
        сообщения владельца против 92/84/42/14/2 у участников.

        Поэтому `person_id` — отдельный параметр, и подставлять в него арендатора
        нельзя. Пустой означает «те же границы, что и раньше» — обычная настройка,
        где человек и арендатор совпадают.
        """
        messages = self.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE user_id=? AND created_at >= ? AND created_at <= ?",
            (person_id or user_id, since, until),
        ).fetchone()["c"]
        documents = self.execute(
            f"""SELECT COUNT(*) AS c FROM knowledge_objects k
               JOIN raw_objects r ON r.id=k.raw_object_id AND r.user_id=k.user_id
                    AND {_not_private_raw_dependency("r")}
               WHERE k.user_id=? AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
                 AND k.created_at >= ? AND k.created_at <= ?""",  # nosec B608
            (user_id, since, until),
        ).fetchone()["c"]
        return {
            "messages": int(messages),
            "documents": int(documents),
            "total": int(messages) + int(documents),
        }
