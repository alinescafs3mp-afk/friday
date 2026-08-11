"""The durable update queue: what Telegram sent, until the backend has taken it.

Moved verbatim out of the single 1670-line module. It was always a separate class;
only its address changed.
"""

from __future__ import annotations

from friday.private_fs import prepare_private_sqlite, restrict_sqlite_files
from friday.telegram_bridge._base import (
    _EDIT_TARGET_MEMORY,
    BATCH_SIZE,
    DELIVERED_NOTIFICATION_TTL_SEC,
    MAX_ATTEMPTS,
    RETRY_DELAYS_SEC,
    Any,
    Path,
    json,
    sqlite3,
    time,
)


def _ordering_key(update: dict[str, Any], update_id: int) -> str:
    """Stable FIFO partition for one Telegram conversation.

    Chat order is authoritative for ordinary and edited messages. Callback
    queries normally carry the originating message; inline callbacks do not, so
    their sender is the narrowest durable partition available. An unrecognised or
    malformed update gets its own key: it may fail independently, but can never
    become a global head-of-line blocker.
    """

    def _telegram_id(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed or None

    for field in ("message", "edited_message"):
        message = update.get(field)
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = _telegram_id(chat.get("id")) if isinstance(chat, dict) else None
        if chat_id is not None:
            return f"chat:{chat_id}"

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = _telegram_id(chat.get("id")) if isinstance(chat, dict) else None
        if chat_id is not None:
            return f"chat:{chat_id}"
        sender = callback.get("from")
        sender_id = _telegram_id(sender.get("id")) if isinstance(sender, dict) else None
        if sender_id is not None:
            return f"user:{sender_id}"

    return f"update:{update_id}"


class _UpdateInbox:
    """SQLite queue: Telegram offsets advance only after an update is durable."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        prepare_private_sqlite(path)
        self._conn = sqlite3.connect(str(path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS updates (
                update_id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                backend_response_json TEXT,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                next_attempt_at REAL NOT NULL DEFAULT 0,
                failed_at REAL,
                ordering_key TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registered_chats (
                chat_id INTEGER PRIMARY KEY,
                registered_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivered_notifications (
                notification_id TEXT PRIMARY KEY,
                delivered_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivered_generated_files (
                delivery_key TEXT PRIMARY KEY,
                delivered_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edit_prompts (
                prompt_message_id INTEGER PRIMARY KEY,
                knowledge_id TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_archive_passwords (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                media_json TEXT NOT NULL,
                safe_query TEXT NOT NULL DEFAULT '',
                original_message_id INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );
            """
        )
        restrict_sqlite_files(path)
        # Idempotent upgrade from the original durable-inbox schema.  Add
        # columns before recreating the index so an existing database can never
        # fail halfway through startup because an index references new fields.
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(updates)").fetchall()}
        additions = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "next_attempt_at": "REAL NOT NULL DEFAULT 0",
            "failed_at": "REAL",
            "ordering_key": "TEXT NOT NULL DEFAULT ''",
            "chunks_sent": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE updates ADD COLUMN {name} {definition}")
        # Old rows predate the FIFO partition. Backfill from their durable payload
        # before the index is created; malformed legacy JSON is isolated under its
        # own update id rather than sharing one empty key with every other chat.
        for row in self._conn.execute(
            "SELECT update_id, payload_json FROM updates WHERE ordering_key=''"
        ).fetchall():
            update_id = int(row["update_id"])
            try:
                payload = json.loads(str(row["payload_json"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            key = _ordering_key(payload if isinstance(payload, dict) else {}, update_id)
            self._conn.execute(
                "UPDATE updates SET ordering_key=? WHERE update_id=?",
                (key, update_id),
            )
        self._conn.execute(
            """UPDATE updates
               SET status='dead_letter', failed_at=COALESCE(failed_at, last_attempt_at)
               WHERE attempts>=? AND status='pending'""",
            (MAX_ATTEMPTS,),
        )
        self._conn.execute("DROP INDEX IF EXISTS idx_updates_pending")
        self._conn.execute(
            """CREATE INDEX idx_updates_pending
               ON updates(status, next_attempt_at, update_id)"""
        )
        self._conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_updates_ordering
               ON updates(status, ordering_key, update_id)"""
        )
        # Строка о доставленном уведомлении живёт до подтверждения бэкенду, то
        # есть считанные секунды. Пережить неделю она может только в одном
        # случае: подтверждение прошло, а снятие строки не успело (падение между
        # двумя действиями). Тогда бэкенд этот номер больше не предложит никогда,
        # и без уборки строка осталась бы здесь навсегда.
        self._conn.execute(
            "DELETE FROM delivered_notifications WHERE delivered_at < ?",
            (time.time() - DELIVERED_NOTIFICATION_TTL_SEC,),
        )
        self._conn.execute(
            "DELETE FROM delivered_generated_files WHERE delivered_at < ?",
            (time.time() - DELIVERED_NOTIFICATION_TTL_SEC,),
        )
        self._conn.execute(
            "DELETE FROM pending_archive_passwords WHERE expires_at < ?",
            (time.time(),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_offset(self) -> int:
        row = self._conn.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row["value"]) if row else 0

    def set_offset(self, offset: int) -> None:
        self._conn.execute(
            """INSERT INTO state(key, value) VALUES('offset', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(max(0, int(offset))),),
        )
        # Своя фиксация, а не чужая. Запись висела в открытой транзакции до
        # ближайшего `store()` — то есть до СЛЕДУЮЩЕГО обновления. Если его не
        # случалось (тихий чат, остановка), смещение не доезжало на диск вовсе, и
        # мост после подъёма перечитывал уже разобранное. Повторной обработки это
        # не давало (`INSERT OR IGNORE` + удаление отвеченной строки), но
        # состояние на диске не сходилось с состоянием в памяти — а расхождение,
        # которое ничего не ломает сегодня, ломает завтра.
        self._conn.commit()

    def remember_registered_chat(self, chat_id: int) -> None:
        """A private chat admitted through open registration, so later gate
        checks (callbacks, outbound push) recognise it without re-deriving
        'private' from a payload they do not have. Durable across restarts —
        losing this on restart would silently re-lock out an already-registered
        person until their next message."""
        self._conn.execute(
            "INSERT OR IGNORE INTO registered_chats(chat_id, registered_at) VALUES(?, ?)",
            (int(chat_id), time.time()),
        )
        self._conn.commit()

    def is_registered_chat(self, chat_id: int) -> bool:
        row = self._conn.execute("SELECT 1 FROM registered_chats WHERE chat_id=?", (int(chat_id),)).fetchone()
        return row is not None

    def remember_edit_prompt(self, prompt_message_id: int, knowledge_id: str) -> None:
        """«Ответьте на ЭТО сообщение новым текстом» — запомнить, о какой записи речь.

        Приглашение жило в словаре процесса, и перезапуск моста разрывал связь
        молча: человек отвечал репликой на приглашение, ответ не узнавался как
        правка и уходил к модели обычным вопросом. Ждать ответа человек может
        сколько угодно, а мост между тем перезапускается — окно не редкое.

        Держится не больше `_EDIT_TARGET_MEMORY` приглашений: человек либо
        отвечает вскоре, либо передумал. Лишнее вытесняется по старшинству, как
        и в прежнем словаре.
        """
        self._conn.execute(
            """INSERT INTO edit_prompts(prompt_message_id, knowledge_id, created_at)
               VALUES(?, ?, ?)
               ON CONFLICT(prompt_message_id) DO UPDATE
                   SET knowledge_id=excluded.knowledge_id, created_at=excluded.created_at""",
            (int(prompt_message_id), str(knowledge_id), time.time()),
        )
        self._conn.execute(
            """DELETE FROM edit_prompts WHERE prompt_message_id NOT IN (
                   SELECT prompt_message_id FROM edit_prompts
                    ORDER BY created_at DESC LIMIT ?
               )""",
            (_EDIT_TARGET_MEMORY,),
        )
        self._conn.commit()

    def take_edit_prompt(self, prompt_message_id: int) -> str:
        """Забрать запись, к которой относится ответ, и снять приглашение.

        Забрать, а не прочитать: приглашение одноразовое, и второй ответ на то же
        сообщение не должен править запись ещё раз.
        """
        row = self._conn.execute(
            "SELECT knowledge_id FROM edit_prompts WHERE prompt_message_id=?",
            (int(prompt_message_id),),
        ).fetchone()
        if row is None:
            return ""
        self._conn.execute(
            "DELETE FROM edit_prompts WHERE prompt_message_id=?",
            (int(prompt_message_id),),
        )
        self._conn.commit()
        return str(row["knowledge_id"])

    def remember_archive_password_challenge(
        self,
        chat_id: int,
        user_id: int,
        document: dict[str, Any],
        *,
        safe_query: str = "",
        original_message_id: int = 0,
        ttl_sec: float = 3600.0,
    ) -> None:
        """Keep only the non-secret Telegram handle needed to retry an archive.

        The archive bytes are deliberately not stored here: Telegram's stable
        ``file_id`` authorizes a fresh bounded download.  This closed descriptor
        also prevents arbitrary update fields (captions, entities, paths or
        credentials) from leaking into the challenge table.
        """

        file_id = str(document.get("file_id") or "")[:512]
        if not file_id:
            return
        descriptor: dict[str, Any] = {"file_id": file_id}
        file_unique_id = str(document.get("file_unique_id") or "")[:512]
        if file_unique_id:
            descriptor["file_unique_id"] = file_unique_id
        filename = Path(str(document.get("file_name") or "archive.bin")).name[:255]
        descriptor["file_name"] = filename or "archive.bin"
        mime_type = str(document.get("mime_type") or "application/octet-stream")[:160]
        descriptor["mime_type"] = mime_type
        for key in ("file_size", "duration"):
            value = document.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                descriptor[key] = value
        now = time.time()
        expires_at = now + max(60.0, min(float(ttl_sec), 24 * 3600.0))
        self._conn.execute(
            """INSERT INTO pending_archive_passwords(
                   chat_id, user_id, media_json, safe_query,
                   original_message_id, created_at, expires_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET
                   media_json=excluded.media_json,
                   safe_query=excluded.safe_query,
                   original_message_id=excluded.original_message_id,
                   created_at=excluded.created_at,
                   expires_at=excluded.expires_at""",
            (
                int(chat_id),
                int(user_id),
                json.dumps(descriptor, ensure_ascii=False, sort_keys=True),
                str(safe_query or "")[:4000],
                max(0, int(original_message_id or 0)),
                now,
                expires_at,
            ),
        )
        self._conn.commit()

    def archive_password_challenge(self, chat_id: int, user_id: int) -> dict[str, Any] | None:
        now = time.time()
        self._conn.execute(
            "DELETE FROM pending_archive_passwords WHERE expires_at < ?",
            (now,),
        )
        row = self._conn.execute(
            """SELECT media_json, safe_query, original_message_id
               FROM pending_archive_passwords
               WHERE chat_id=? AND user_id=? AND expires_at>=?""",
            (int(chat_id), int(user_id), now),
        ).fetchone()
        self._conn.commit()
        if row is None:
            return None
        try:
            document = json.loads(str(row["media_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(document, dict) or not str(document.get("file_id") or ""):
            return None
        return {
            "document": document,
            "safe_query": str(row["safe_query"] or ""),
            "original_message_id": int(row["original_message_id"] or 0),
        }

    def clear_archive_password_challenge(self, chat_id: int, user_id: int) -> None:
        self._conn.execute(
            "DELETE FROM pending_archive_passwords WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        self._conn.commit()

    def remember_delivered_notification(self, notification_id: str) -> None:
        """Уведомление ушло человеку — записать это ТАМ, ГДЕ ЭТО ПРОИЗОШЛО.

        Признак доставки жил только в списке в памяти процесса до общего
        подтверждения в конце пачки. Провалилось подтверждение — на бэкенде все
        двадцать сообщений по-прежнему `pending`, и следующий оборот доставлял
        их человеку заново, и так каждые пятнадцать секунд.

        Подтверждать каждое отдельным сетевым вызовом нельзя (бюджет частоты
        владельца), а вот записать строку в свою же очередь — можно: это
        локально, стоит одну вставку и переживает перезапуск моста.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO delivered_notifications(notification_id, delivered_at) VALUES(?, ?)",
            (str(notification_id), time.time()),
        )
        self._conn.commit()

    def delivered_notification_ids(self) -> set[str]:
        """Номера, уже ушедшие человеку и ещё не подтверждённые бэкенду."""
        rows = self._conn.execute("SELECT notification_id FROM delivered_notifications").fetchall()
        return {str(row["notification_id"]) for row in rows}

    def forget_delivered_notifications(self, notification_ids: list[str]) -> None:
        """Подтверждение дошло — бэкенд помнит сам, локальная строка больше не нужна."""
        cleaned = [str(value) for value in notification_ids if str(value)]
        if not cleaned:
            return
        self._conn.executemany(
            "DELETE FROM delivered_notifications WHERE notification_id=?",
            [(value,) for value in cleaned],
        )
        self._conn.commit()

    def generated_file_was_delivered(self, delivery_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM delivered_generated_files WHERE delivery_key=?",
            (str(delivery_key),),
        ).fetchone()
        return row is not None

    def remember_generated_file_delivery(self, delivery_key: str) -> None:
        """Checkpoint one successful sendDocument before the update can retry."""

        self._conn.execute(
            """INSERT OR IGNORE INTO delivered_generated_files(delivery_key, delivered_at)
               VALUES(?, ?)""",
            (str(delivery_key), time.time()),
        )
        self._conn.commit()

    def store(self, update: dict[str, Any]) -> bool:
        update_id = int(update.get("update_id", -1))
        if update_id < 0:
            return False
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO updates(update_id, payload_json, created_at, ordering_key)
               VALUES(?, ?, ?, ?)""",
            (
                update_id,
                json.dumps(update, ensure_ascii=False, sort_keys=True),
                time.time(),
                _ordering_key(update, update_id),
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def pending(self, *, now: float | None = None, limit: int = BATCH_SIZE) -> list[dict[str, Any]]:
        ready_at = time.time() if now is None else float(now)
        row_limit = max(1, min(int(limit), BATCH_SIZE * 2))
        rows = self._conn.execute(
            """SELECT current.* FROM updates AS current
               WHERE current.status='pending'
                 AND current.attempts < ?
                 AND current.next_attempt_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM updates AS earlier
                     WHERE earlier.status='pending'
                       AND earlier.attempts < ?
                       AND earlier.ordering_key=current.ordering_key
                       AND earlier.update_id < current.update_id
                 )
               ORDER BY current.update_id ASC LIMIT ?""",
            (MAX_ATTEMPTS, ready_at, MAX_ATTEMPTS, row_limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_failure(self, update_id: int, error: str) -> bool:
        row = self._conn.execute(
            "SELECT attempts FROM updates WHERE update_id=?",
            (update_id,),
        ).fetchone()
        if row is None:
            return False
        attempts = int(row["attempts"]) + 1
        attempted_at = time.time()
        dead_lettered = attempts >= MAX_ATTEMPTS
        delay = RETRY_DELAYS_SEC[min(attempts - 1, len(RETRY_DELAYS_SEC) - 1)]
        self._conn.execute(
            """UPDATE updates
               SET attempts=?, last_attempt_at=?, last_error=?, status=?,
                   next_attempt_at=?, failed_at=?
               WHERE update_id=?""",
            (
                attempts,
                attempted_at,
                error[:500],
                "dead_letter" if dead_lettered else "pending",
                0 if dead_lettered else attempted_at + delay,
                attempted_at if dead_lettered else None,
                update_id,
            ),
        )
        self._conn.commit()
        return dead_lettered

    def mark_dead_letter(self, update_id: int, error: str) -> None:
        failed_at = time.time()
        self._conn.execute(
            """UPDATE updates
               SET status='dead_letter', last_error=?, last_attempt_at=?,
                   next_attempt_at=0, failed_at=?
               WHERE update_id=?""",
            (error[:500], failed_at, failed_at, update_id),
        )
        self._conn.commit()

    def dead_letters(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM updates WHERE status='dead_letter'
               ORDER BY failed_at DESC, update_id ASC LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS count FROM updates GROUP BY status").fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "pending": counts.get("pending", 0),
            "dead_letter": counts.get("dead_letter", 0),
        }

    def answer_chunks_sent(self, update_id: int) -> int:
        """Сколько кусков ответа на это обновление человек уже получил.

        Длинный ответ уходит в Telegram несколькими сообщениями. Обрыв сети на
        третьем куске из пяти — не потеря ответа: строка остаётся в очереди и
        повторяется, а повтор до сих пор слал ВСЕ куски заново, и первые два
        приходили человеку дважды.
        """
        row = self._conn.execute(
            "SELECT chunks_sent FROM updates WHERE update_id=?",
            (int(update_id),),
        ).fetchone()
        return int(row["chunks_sent"]) if row else 0

    def record_answer_chunks_sent(self, update_id: int, count: int) -> None:
        """Отметить кусок ушедшим — СРАЗУ, а не в конце отправки.

        Счётчик живёт на строке обновления и исчезает вместе с ней: успешно
        отвеченное обновление удаляется целиком, поэтому обнулять его отдельно
        не нужно и нечего забыть.
        """
        self._conn.execute(
            "UPDATE updates SET chunks_sent=? WHERE update_id=?",
            (max(0, int(count)), int(update_id)),
        )
        self._conn.commit()

    def cache_backend_response(self, update_id: int, response: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE updates SET backend_response_json=? WHERE update_id=?",
            (json.dumps(response, ensure_ascii=False, sort_keys=True), update_id),
        )
        self._conn.commit()

    def remove(self, update_id: int) -> None:
        self._conn.execute("DELETE FROM updates WHERE update_id=?", (update_id,))
        self._conn.commit()
