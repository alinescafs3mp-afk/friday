"""The durable update queue: what Telegram sent, until the backend has taken it.

Moved verbatim out of the single 1670-line module. It was always a separate class;
only its address changed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import suppress
from functools import wraps
from typing import TypeVar, cast

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

_OUTBOUND_REPLY_CONTEXT_TTL_SEC = 30 * 24 * 3600.0
_OUTBOUND_REPLY_CONTEXT_MAX_ROWS = 20_000
_TELEGRAM_STATUS_TERMINAL_TTL_SEC = 7 * 24 * 3600.0
_TELEGRAM_STATUS_RUNNING_TTL_SEC = 30 * 24 * 3600.0
_UPDATE_EFFECT_ATTEMPT_SCHEMA = "friday.telegram-update-effect-attempt.v1"
_UPDATE_EFFECT_ATTEMPT_KINDS = frozenset(
    {"callback", "command", "edited-message", "edit-reply", "local-reply"}
)
_DEAD_LETTER_NOTICE_PREFIX = "friday.telegram-dead-letter-notice.v1:"

_F = TypeVar("_F", bound=Callable[..., Any])


def _rollback_or_quarantine(connection: Any) -> None:
    """Discard one failed SQLite mutation before another write can inherit it."""

    try:
        connection.rollback()
    except BaseException:
        # An I/O failure can make rollback itself unavailable.  A closed
        # connection is noisy but safe: the process can no longer observe or
        # accidentally commit tentative state and must recover by reopening.
        with suppress(BaseException):
            connection.close()


def _rollback_failed_write(method: _F) -> _F:
    """Make an existing execute/commit mutator fail back to proven state."""

    @wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        connection = self._conn
        try:
            # Old builds could leave a transaction open after SQLITE_FULL or
            # SQLITE_IOERR.  Never let this write become the unrelated commit
            # that publishes it.
            if connection.in_transaction:
                connection.rollback()
            return method(self, *args, **kwargs)
        except BaseException:
            _rollback_or_quarantine(connection)
            raise

    return cast(_F, guarded)


def _close_failed_initialization(method: _F) -> _F:
    """Do not leave a partially opened/migrated inbox connection alive."""

    @wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                _rollback_or_quarantine(connection)
                with suppress(BaseException):
                    connection.close()
            raise

    return cast(_F, guarded)


def _safe_backend_message_id(value: object) -> str:
    candidate = str(value or "")
    if not candidate or len(candidate) > 128 or not candidate.isascii():
        return ""
    return candidate if all(char.isalnum() or char in "._:-" for char in candidate) else ""


def _update_effect_attempt_witness(update_id: int, kind: str) -> str:
    if (
        isinstance(update_id, bool)
        or not isinstance(update_id, int)
        or not 0 <= update_id <= (2**63 - 1)
        or kind not in _UPDATE_EFFECT_ATTEMPT_KINDS
    ):
        raise ValueError("Telegram update effect identity is invalid")
    fingerprint = hashlib.sha256(
        f"{_UPDATE_EFFECT_ATTEMPT_SCHEMA}:{update_id}:{kind}".encode("ascii", errors="strict")
    ).hexdigest()
    return json.dumps(
        {
            "fingerprint_sha256": fingerprint,
            "kind": kind,
            "schema": _UPDATE_EFFECT_ATTEMPT_SCHEMA,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dead_letter_notice_marker(
    error: str,
    *,
    permanent: bool,
    resume_base: int,
) -> str:
    disposition = "permanent" if permanent else "exhausted"
    safe_error = str(error).split(":", 1)[0][:128]
    return f"{_DEAD_LETTER_NOTICE_PREFIX}{disposition}:{max(0, int(resume_base))}:{safe_error}"


def _parse_dead_letter_notice_marker(value: object) -> tuple[bool, str, int] | None:
    raw = str(value or "")
    if not raw.startswith(_DEAD_LETTER_NOTICE_PREFIX):
        return None
    parts = raw[len(_DEAD_LETTER_NOTICE_PREFIX) :].split(":", 2)
    if len(parts) != 3:
        return None
    disposition, raw_resume_base, error = parts
    if disposition not in {"permanent", "exhausted"} or not error:
        return None
    try:
        resume_base = int(raw_resume_base)
    except ValueError:
        return None
    if resume_base < 0:
        return None
    return disposition == "permanent", error, resume_base


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

    @_close_failed_initialization
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
            CREATE TABLE IF NOT EXISTS notification_delivery_parts (
                notification_id TEXT NOT NULL,
                part_key TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('uncertain', 'confirmed')),
                updated_at REAL NOT NULL,
                PRIMARY KEY(notification_id, part_key)
            );
            CREATE TABLE IF NOT EXISTS notification_delivery_outcomes (
                notification_id TEXT PRIMARY KEY,
                outcome TEXT NOT NULL CHECK(outcome IN ('sent', 'uncertain')),
                status_reconciled INTEGER NOT NULL DEFAULT 0 CHECK(status_reconciled IN (0, 1)),
                updated_at REAL NOT NULL
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
            CREATE TABLE IF NOT EXISTS outbound_reply_context (
                chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                backend_message_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY(chat_id, telegram_message_id)
            );
            CREATE TABLE IF NOT EXISTS telegram_status_messages (
                chat_id INTEGER NOT NULL,
                operation_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
                updated_at REAL NOT NULL,
                PRIMARY KEY(chat_id, operation_id)
            );
            CREATE TABLE IF NOT EXISTS telegram_status_send_fences (
                chat_id INTEGER NOT NULL,
                operation_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(chat_id, operation_id)
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
            "delivery_uncertainty": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE updates ADD COLUMN {name} {definition}")
        outcome_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(notification_delivery_outcomes)").fetchall()
        }
        if "status_reconciled" not in outcome_columns:
            self._conn.execute(
                """ALTER TABLE notification_delivery_outcomes
                   ADD COLUMN status_reconciled INTEGER NOT NULL DEFAULT 0
                   CHECK(status_reconciled IN (0, 1))"""
            )
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
        self._conn.execute(
            "DELETE FROM outbound_reply_context WHERE expires_at < ?",
            (time.time(),),
        )
        status_now = time.time()
        self._conn.execute(
            """DELETE FROM telegram_status_messages
               WHERE (terminal=1 AND updated_at < ?)
                  OR (terminal=0 AND updated_at < ?)""",
            (
                status_now - _TELEGRAM_STATUS_TERMINAL_TTL_SEC,
                status_now - _TELEGRAM_STATUS_RUNNING_TTL_SEC,
            ),
        )
        # A process can stop after persisting the accepted Telegram message id
        # but before removing its pre-send ambiguity fence.  The coordinate is
        # stronger evidence: only those proven-complete fences may be retired.
        # Unresolved fences deliberately have no TTL; expiring one would permit
        # a blind duplicate after an old accepted response was lost.
        self._conn.execute(
            """DELETE FROM telegram_status_send_fences
               WHERE EXISTS (
                   SELECT 1 FROM telegram_status_messages AS messages
                   WHERE messages.chat_id=telegram_status_send_fences.chat_id
                     AND messages.operation_id=telegram_status_send_fences.operation_id
                     AND messages.revision>=telegram_status_send_fences.revision
               )"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_offset(self) -> int:
        row = self._conn.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row["value"]) if row else 0

    def update_requires_retry(self, update_id: int) -> bool:
        """Whether one durable row can still consume its ephemeral ingress state."""

        row = self._conn.execute(
            """SELECT 1 FROM updates
               WHERE update_id=? AND status='pending' AND attempts < ?
                 AND last_error NOT LIKE ?""",
            (int(update_id), MAX_ATTEMPTS, f"{_DEAD_LETTER_NOTICE_PREFIX}%"),
        ).fetchone()
        return row is not None

    def pending_dead_letter_notice(
        self,
        update_ids: list[int],
    ) -> tuple[bool, str, int] | None:
        """Return one exact durable terminal-notice obligation for owned rows."""

        cleaned = list(dict.fromkeys(int(value) for value in update_ids))
        if not cleaned:
            return None
        placeholders = ",".join("?" for _value in cleaned)
        rows = self._conn.execute(
            f"""SELECT status, last_error FROM updates
                WHERE update_id IN ({placeholders})""",  # nosec B608
            cleaned,
        ).fetchall()
        if len(rows) != len(cleaned) or any(str(row["status"]) != "pending" for row in rows):
            return None
        obligations = {_parse_dead_letter_notice_marker(row["last_error"]) for row in rows}
        if len(obligations) != 1:
            raise RuntimeError("Telegram dead-letter notice ownership conflicts")
        return obligations.pop()

    @_rollback_failed_write
    def prepare_dead_letter_notice_many(
        self,
        update_ids: list[int],
        error: str,
        *,
        permanent: bool,
    ) -> None:
        """Persist terminal intent while the row remains restart-dispatchable."""

        cleaned = list(dict.fromkeys(int(value) for value in update_ids))
        if not cleaned:
            raise ValueError("Telegram dead-letter notice has no owned updates")
        anchor = self._conn.execute(
            "SELECT chunks_sent FROM updates WHERE update_id=? AND status='pending'",
            (cleaned[0],),
        ).fetchone()
        if anchor is None:
            raise RuntimeError("Telegram dead-letter notice anchor is absent")
        marker = _dead_letter_notice_marker(
            error,
            permanent=permanent,
            resume_base=int(anchor["chunks_sent"]),
        )
        failed_at = time.time()
        ready_at = failed_at + RETRY_DELAYS_SEC[0]
        cursor = self._conn.executemany(
            """UPDATE updates
               SET status='pending', last_error=?, last_attempt_at=?,
                   next_attempt_at=?, failed_at=NULL
               WHERE update_id=? AND status='pending'""",
            [(marker, failed_at, ready_at, update_id) for update_id in cleaned],
        )
        if cursor.rowcount != len(cleaned):
            self._conn.rollback()
            raise RuntimeError("Telegram dead-letter notice ownership changed")
        self._conn.commit()

    @_rollback_failed_write
    def finish_dead_letter_notice_many(self, update_ids: list[int]) -> None:
        """Absorb rows only after their resumable terminal notice completed."""

        cleaned = list(dict.fromkeys(int(value) for value in update_ids))
        obligation = self.pending_dead_letter_notice(cleaned)
        if obligation is None:
            raise RuntimeError("Telegram dead-letter notice obligation is absent")
        permanent, error, _resume_base = obligation
        failed_at = time.time()
        placeholders = ",".join("?" for _value in cleaned)
        cursor = self._conn.execute(
            f"""UPDATE updates
                SET status='dead_letter',
                    attempts=CASE WHEN ? THEN attempts ELSE ? END,
                    last_error=?, last_attempt_at=?, next_attempt_at=0, failed_at=?
                WHERE update_id IN ({placeholders}) AND status='pending'""",  # nosec B608
            (
                int(permanent),
                MAX_ATTEMPTS,
                error[:500],
                failed_at,
                failed_at,
                *cleaned,
            ),
        )
        if cursor.rowcount != len(cleaned):
            self._conn.rollback()
            raise RuntimeError("Telegram dead-letter notice ownership changed")
        self._conn.commit()

    def update_effect_attempt_kind(self, update_id: int) -> str | None:
        """Return a validated body-free effect witness, never a cached response."""

        row = self._conn.execute(
            "SELECT backend_response_json FROM updates WHERE update_id=?",
            (int(update_id),),
        ).fetchone()
        raw = str(row["backend_response_json"] or "") if row is not None else ""
        if not raw:
            return None
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        kind = document.get("kind") if isinstance(document, dict) else None
        if not isinstance(kind, str) or kind not in _UPDATE_EFFECT_ATTEMPT_KINDS:
            return None
        return kind if raw == _update_effect_attempt_witness(int(update_id), kind) else None

    @_rollback_failed_write
    def set_offset(self, offset: int) -> None:
        self._conn.execute(
            """INSERT INTO state(key, value) VALUES('offset', ?)
               ON CONFLICT(key) DO UPDATE SET value=CASE
                   WHEN CAST(state.value AS INTEGER) < CAST(excluded.value AS INTEGER)
                   THEN excluded.value
                   ELSE state.value
               END""",
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

    @_rollback_failed_write
    def admit_polled_update(self, update: dict[str, Any]) -> tuple[bool, int]:
        """Atomically admit one Telegram update and advance durable poll authority.

        A row without its matching offset can be processed and removed before a
        restart, after which Telegram would redeliver the same id into an empty
        queue.  Keeping both writes in this transaction also makes the durable
        offset a tombstone for completed update ids without adding a table or a
        schema field.
        """

        update_id = int(update.get("update_id", -1))
        if update_id < 0:
            raise ValueError("Telegram update has no valid identity")
        # Serialize the tombstone recheck with admission.  A stale reader must
        # never insert an update after another owner advanced the offset and
        # removed its completed row.
        self._conn.execute("BEGIN IMMEDIATE")
        durable_offset = self.get_offset()
        if update_id < durable_offset:
            self._conn.rollback()
            return False, durable_offset
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
        next_offset = max(durable_offset, update_id + 1)
        self._conn.execute(
            """INSERT INTO state(key, value) VALUES('offset', ?)
               ON CONFLICT(key) DO UPDATE SET value=CASE
                   WHEN CAST(state.value AS INTEGER) < CAST(excluded.value AS INTEGER)
                   THEN excluded.value
                   ELSE state.value
               END""",
            (str(next_offset),),
        )
        row = self._conn.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        next_offset = int(row["value"]) if row is not None else next_offset
        self._conn.commit()
        return cursor.rowcount > 0, next_offset

    @_rollback_failed_write
    def claim_update_effect_attempt(self, update_id: int, kind: str) -> str:
        """Fence a non-replayable update effect before its first external byte.

        The witness is an opaque digest-only envelope in the existing response
        cache column.  A retry can therefore distinguish “never attempted” from
        “outcome may already exist” without a schema or a second ownership lane.
        """

        witness = _update_effect_attempt_witness(update_id, kind)
        exact_update_id = int(update_id)
        cursor = self._conn.execute(
            """UPDATE updates SET backend_response_json=?
               WHERE update_id=? AND backend_response_json IS NULL""",
            (witness, exact_update_id),
        )
        if cursor.rowcount == 1:
            try:
                self._conn.commit()
            except BaseException:
                # No external awaitable exists yet.  If COMMIT landed but its
                # acknowledgement did not, remove only this exact witness so a
                # proven pre-effect local failure remains retryable.
                _rollback_or_quarantine(self._conn)
                with suppress(BaseException):
                    self._conn.execute(
                        """UPDATE updates SET backend_response_json=NULL
                           WHERE update_id=? AND backend_response_json=?""",
                        (exact_update_id, witness),
                    )
                    self._conn.commit()
                raise
            return "claimed"

        # A zero-row UPDATE still opened a write transaction.  Close it before
        # reading the durable winner and before returning a normal replay state.
        self._conn.rollback()
        row = self._conn.execute(
            "SELECT backend_response_json FROM updates WHERE update_id=?",
            (exact_update_id,),
        ).fetchone()
        if row is not None and str(row["backend_response_json"] or "") == witness:
            return "already_attempted"
        raise RuntimeError("Telegram update effect witness conflicts with cached response")

    @_rollback_failed_write
    def release_update_effect_attempt(self, update_id: int, kind: str) -> bool:
        """Release only this exact witness after proven zero-byte backend failure."""

        witness = _update_effect_attempt_witness(update_id, kind)
        cursor = self._conn.execute(
            """UPDATE updates SET backend_response_json=NULL
               WHERE update_id=? AND backend_response_json=?""",
            (int(update_id), witness),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_rollback_failed_write
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

    @_rollback_failed_write
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

    @_rollback_failed_write
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

    def edit_prompt_target(self, prompt_message_id: int) -> str:
        """Read one edit target without consuming it before the backend effect."""

        row = self._conn.execute(
            "SELECT knowledge_id FROM edit_prompts WHERE prompt_message_id=?",
            (int(prompt_message_id),),
        ).fetchone()
        return str(row["knowledge_id"]) if row is not None else ""

    @_rollback_failed_write
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
        # A password challenge raised while recovering media from an exact
        # Telegram reply must remain a reply recovery on the next update.  The
        # marker and structural identities are bridge-owned: callers cannot put
        # arbitrary paths, filenames or quoted text into this durable record.
        if document.get("_friday_reply_recovery") is True:
            reply_source_ref = str(document.get("_friday_reply_document_source_ref") or "")
            reply_unique_id = str(document.get("_friday_reply_document_file_unique_id") or "")
            raw_reply_message_id = document.get("_friday_reply_document_message_id")
            if isinstance(raw_reply_message_id, bool) or not isinstance(raw_reply_message_id, (int, str)):
                return
            try:
                reply_message_id = int(raw_reply_message_id)
            except (TypeError, ValueError):
                return
            if (
                reply_message_id <= 0
                or reply_source_ref != f"telegram-file:{file_id}"
                or reply_unique_id != file_unique_id
            ):
                return
            descriptor["_friday_reply_recovery"] = True
            descriptor["_friday_reply_document_source_ref"] = reply_source_ref
            descriptor["_friday_reply_document_message_id"] = reply_message_id
            if reply_unique_id:
                descriptor["_friday_reply_document_file_unique_id"] = reply_unique_id
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

    @_rollback_failed_write
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
        reply_recovery = document.pop("_friday_reply_recovery", None) is True
        reply_source_ref = str(document.pop("_friday_reply_document_source_ref", "") or "")
        raw_reply_message_id = document.pop("_friday_reply_document_message_id", 0)
        reply_unique_id = str(document.pop("_friday_reply_document_file_unique_id", "") or "")
        result: dict[str, Any] = {
            "document": document,
            "safe_query": str(row["safe_query"] or ""),
            "original_message_id": int(row["original_message_id"] or 0),
        }
        if reply_recovery:
            file_id = str(document.get("file_id") or "")
            file_unique_id = str(document.get("file_unique_id") or "")
            if isinstance(raw_reply_message_id, bool) or not isinstance(raw_reply_message_id, (int, str)):
                return None
            try:
                reply_message_id = int(raw_reply_message_id)
            except (TypeError, ValueError):
                return None
            if (
                reply_message_id <= 0
                or reply_source_ref != f"telegram-file:{file_id}"
                or reply_unique_id != file_unique_id
            ):
                return None
            result.update(
                {
                    "reply_recovery": True,
                    "reply_document_source_ref": reply_source_ref,
                    "reply_document_message_id": reply_message_id,
                    "reply_document_file_unique_id": reply_unique_id,
                }
            )
        return result

    @_rollback_failed_write
    def clear_archive_password_challenge(self, chat_id: int, user_id: int) -> None:
        self._conn.execute(
            "DELETE FROM pending_archive_passwords WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        self._conn.commit()

    @_rollback_failed_write
    def remember_outbound_reply_context(
        self,
        chat_id: int,
        telegram_message_id: int,
        backend_message_id: str,
        *,
        ttl_sec: float = _OUTBOUND_REPLY_CONTEXT_TTL_SEC,
        max_rows: int = _OUTBOUND_REPLY_CONTEXT_MAX_ROWS,
    ) -> None:
        """Bind one delivered Telegram chunk to one opaque backend message.

        No answer text, filename, Raw id or attachment metadata belongs here.
        The backend message id is only a lookup handle; the backend must still
        re-authorize its owner, conversation and current attachment verdict.
        """

        safe_backend_id = _safe_backend_message_id(backend_message_id)
        chat = int(chat_id)
        telegram_id = int(telegram_message_id)
        if not chat or telegram_id <= 0 or not safe_backend_id:
            return
        now = time.time()
        expires_at = now + max(60.0, min(float(ttl_sec), 180 * 24 * 3600.0))
        self._conn.execute(
            """INSERT INTO outbound_reply_context(
                   chat_id, telegram_message_id, backend_message_id, created_at, expires_at
               ) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, telegram_message_id) DO UPDATE SET
                   backend_message_id=excluded.backend_message_id,
                   created_at=excluded.created_at,
                   expires_at=excluded.expires_at""",
            (chat, telegram_id, safe_backend_id, now, expires_at),
        )
        self._conn.execute(
            "DELETE FROM outbound_reply_context WHERE expires_at < ?",
            (now,),
        )
        self._conn.execute(
            """DELETE FROM outbound_reply_context
               WHERE rowid IN (
                   SELECT rowid FROM outbound_reply_context
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?
               )""",
            (max(1, min(int(max_rows), _OUTBOUND_REPLY_CONTEXT_MAX_ROWS)),),
        )
        self._conn.commit()

    @_rollback_failed_write
    def outbound_reply_source_message_id(
        self,
        chat_id: int,
        telegram_message_id: int,
    ) -> str:
        """Resolve a non-expired handle only inside the originating chat."""

        now = time.time()
        self._conn.execute(
            "DELETE FROM outbound_reply_context WHERE expires_at < ?",
            (now,),
        )
        row = self._conn.execute(
            """SELECT backend_message_id FROM outbound_reply_context
               WHERE chat_id=? AND telegram_message_id=? AND expires_at>=?""",
            (int(chat_id), int(telegram_message_id), now),
        ).fetchone()
        self._conn.commit()
        return _safe_backend_message_id(row["backend_message_id"]) if row is not None else ""

    def telegram_status_message(
        self,
        chat_id: int,
        operation_id: str,
    ) -> dict[str, Any] | None:
        """Return only Telegram delivery coordinates, never status content."""

        row = self._conn.execute(
            """SELECT message_id, revision, terminal
               FROM telegram_status_messages
               WHERE chat_id=? AND operation_id=?""",
            (int(chat_id), str(operation_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": int(row["message_id"]),
            "revision": int(row["revision"]),
            "terminal": bool(row["terminal"]),
        }

    def telegram_status_send_fence(
        self,
        chat_id: int,
        operation_id: str,
    ) -> dict[str, int] | None:
        """Return a content-free fence for a send whose outcome is not proven."""

        row = self._conn.execute(
            """SELECT revision FROM telegram_status_send_fences
               WHERE chat_id=? AND operation_id=?""",
            (int(chat_id), str(operation_id)),
        ).fetchone()
        return {"revision": int(row["revision"])} if row is not None else None

    @_rollback_failed_write
    def begin_telegram_status_send(
        self,
        chat_id: int,
        operation_id: str,
        revision: int,
    ) -> str:
        """Arm one durable pre-send fence; only its creator may call Telegram."""

        chat = int(chat_id)
        operation = str(operation_id)
        current_revision = int(revision)
        max_integer = (1 << 63) - 1
        if (
            not chat
            or abs(chat) > max_integer
            or not operation
            or len(operation) > 128
            or not operation.isascii()
            or any(not (character.isalnum() or character in "._:-") for character in operation)
            or current_revision <= 0
            or current_revision > max_integer
        ):
            raise ValueError("invalid Telegram status send fence")
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO telegram_status_send_fences(
                   chat_id, operation_id, revision, updated_at)
               VALUES(?, ?, ?, ?)""",
            (chat, operation, current_revision, time.time()),
        )
        self._conn.commit()
        return "armed" if cursor.rowcount > 0 else "uncertain"

    @_rollback_failed_write
    def clear_telegram_status_send_fence(
        self,
        chat_id: int,
        operation_id: str,
        revision: int,
    ) -> bool:
        """Disarm only the exact send attempt after rejection or durable CAS."""

        cursor = self._conn.execute(
            """DELETE FROM telegram_status_send_fences
               WHERE chat_id=? AND operation_id=? AND revision=?""",
            (int(chat_id), str(operation_id), int(revision)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_rollback_failed_write
    def record_telegram_status_message(
        self,
        chat_id: int,
        operation_id: str,
        message_id: int,
        revision: int,
        terminal: bool,
        *,
        expected_revision: int | None,
    ) -> bool:
        """CAS one status coordinate; a stored terminal state is absorbing."""

        chat = int(chat_id)
        operation = str(operation_id)
        telegram_message_id = int(message_id)
        current_revision = int(revision)
        max_integer = (1 << 63) - 1
        if (
            not chat
            or abs(chat) > max_integer
            or not operation
            or len(operation) > 128
            or not operation.isascii()
            or any(not (character.isalnum() or character in "._:-") for character in operation)
            or telegram_message_id <= 0
            or telegram_message_id > max_integer
            or current_revision <= 0
            or current_revision > max_integer
            or type(terminal) is not bool
        ):
            raise ValueError("invalid Telegram status coordinate")
        if expected_revision is None:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO telegram_status_messages(
                       chat_id, operation_id, message_id, revision, terminal, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    chat,
                    operation,
                    telegram_message_id,
                    current_revision,
                    int(terminal),
                    time.time(),
                ),
            )
        else:
            expected = int(expected_revision)
            if expected <= 0 or expected > max_integer or current_revision <= expected:
                raise ValueError("Telegram status revision is not monotonic")
            cursor = self._conn.execute(
                """UPDATE telegram_status_messages
                   SET message_id=?, revision=?, terminal=?, updated_at=?
                   WHERE chat_id=? AND operation_id=? AND revision=? AND terminal=0""",
                (
                    telegram_message_id,
                    current_revision,
                    int(terminal),
                    time.time(),
                    chat,
                    operation,
                    expected,
                ),
            )
        self._conn.commit()
        return cursor.rowcount > 0

    @_rollback_failed_write
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

    @_rollback_failed_write
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

    def notification_delivery_part_states(self, notification_id: str) -> dict[str, str]:
        """Durable pre-write states for one strict outbound notification.

        An ``uncertain`` row is deliberately stronger than a success cursor: it
        is committed before the Telegram request starts, so a process death at
        any later instruction can never cause the same part to be written again.
        Only the narrow rejection paths delete it.
        """

        rows = self._conn.execute(
            """SELECT part_key, state FROM notification_delivery_parts
               WHERE notification_id=?""",
            (str(notification_id),),
        ).fetchall()
        return {str(row["part_key"]): str(row["state"]) for row in rows}

    def notification_delivery_ids(self) -> set[str]:
        """Notification ids with strict local delivery state awaiting reconciliation."""

        rows = self._conn.execute(
            """SELECT notification_id FROM notification_delivery_parts
               UNION SELECT notification_id FROM notification_delivery_outcomes"""
        ).fetchall()
        return {str(row["notification_id"]) for row in rows}

    def notification_delivery_outcomes(self, *, limit: int = 100) -> dict[str, str]:
        """Strict terminal outcomes still awaiting an exact backend ACK."""

        rows = self._conn.execute(
            """SELECT notification_id, outcome FROM notification_delivery_outcomes
               ORDER BY updated_at ASC, notification_id ASC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return {str(row["notification_id"]): str(row["outcome"]) for row in rows}

    def notification_delivery_reconciled_outcomes(self, *, limit: int = 100) -> dict[str, str]:
        """Outcomes whose separately delivered terminal status is also durable."""

        rows = self._conn.execute(
            """SELECT notification_id, outcome FROM notification_delivery_outcomes
               WHERE status_reconciled=1
               ORDER BY updated_at ASC, notification_id ASC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return {str(row["notification_id"]): str(row["outcome"]) for row in rows}

    def notification_delivery_outcome(self, notification_id: str) -> str | None:
        """Return one exact strict outcome without relying on a bounded scan."""

        row = self._conn.execute(
            """SELECT outcome FROM notification_delivery_outcomes
               WHERE notification_id=?""",
            (str(notification_id),),
        ).fetchone()
        return str(row["outcome"]) if row is not None else None

    def notification_delivery_orphan_outcomes(self, *, limit: int = 100) -> dict[str, str]:
        """Infer bounded ACK outcomes for fences whose post-send write was interrupted."""

        rows = self._conn.execute(
            """SELECT p.notification_id,
                      CASE WHEN MIN(p.state)='confirmed' AND MAX(p.state)='confirmed'
                           THEN 'sent' ELSE 'uncertain' END AS outcome
                 FROM notification_delivery_parts AS p
                 LEFT JOIN notification_delivery_outcomes AS o
                   ON o.notification_id=p.notification_id
                WHERE o.notification_id IS NULL
                GROUP BY p.notification_id
                ORDER BY MIN(p.updated_at) ASC, p.notification_id ASC
                LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return {str(row["notification_id"]): str(row["outcome"]) for row in rows}

    @_rollback_failed_write
    def remember_notification_delivery_outcome(self, notification_id: str, outcome: str) -> None:
        """Persist a terminal local outcome before its fallible backend ACK."""

        notification_id = str(notification_id or "").strip()
        outcome = str(outcome or "").strip()
        if not notification_id or outcome not in {"sent", "uncertain"}:
            raise ValueError("invalid notification delivery outcome")
        self._conn.execute(
            """INSERT INTO notification_delivery_outcomes(
                   notification_id, outcome, status_reconciled, updated_at)
               VALUES(?, ?, 0, ?)
               ON CONFLICT(notification_id) DO UPDATE SET
                   status_reconciled=CASE
                       WHEN notification_delivery_outcomes.outcome='sent'
                            AND excluded.outcome='uncertain' THEN 0
                       ELSE notification_delivery_outcomes.status_reconciled
                   END,
                   outcome=CASE
                       WHEN notification_delivery_outcomes.outcome='uncertain'
                            OR excluded.outcome='uncertain' THEN 'uncertain'
                       ELSE 'sent'
                   END,
                   updated_at=excluded.updated_at""",
            (notification_id, outcome, time.time()),
        )
        self._conn.commit()
        row = self._conn.execute(
            """SELECT outcome FROM notification_delivery_outcomes
               WHERE notification_id=?""",
            (notification_id,),
        ).fetchone()
        expected = (
            "uncertain"
            if row is not None and (str(row["outcome"]) == "uncertain" or outcome == "uncertain")
            else outcome
        )
        if row is None or str(row["outcome"]) != expected:
            raise RuntimeError("notification delivery outcome changed")

    @_rollback_failed_write
    def remember_notification_delivery_reconciled_outcome(
        self,
        notification_id: str,
        outcome: str,
    ) -> None:
        """Atomically persist a terminal outcome that needs no status side effect.

        Reminder delivery has no separate Telegram status carrier.  Writing its
        outcome unreconciled and marking it reconciled in a second commit leaves
        a process-death window whose local fence can never become ACK-eligible.
        This single transaction either leaves no row or one complete proof.
        """

        notification_id = str(notification_id or "").strip()
        outcome = str(outcome or "").strip()
        if not notification_id or outcome not in {"sent", "uncertain"}:
            raise ValueError("invalid notification delivery outcome")
        self._conn.execute(
            """INSERT INTO notification_delivery_outcomes(
                   notification_id, outcome, status_reconciled, updated_at)
               VALUES(?, ?, 1, ?)
               ON CONFLICT(notification_id) DO UPDATE SET
                   outcome=CASE
                       WHEN notification_delivery_outcomes.outcome='uncertain'
                            OR excluded.outcome='uncertain' THEN 'uncertain'
                       ELSE 'sent'
                   END,
                   status_reconciled=1,
                   updated_at=excluded.updated_at""",
            (notification_id, outcome, time.time()),
        )
        self._conn.commit()
        row = self._conn.execute(
            """SELECT outcome, status_reconciled
                 FROM notification_delivery_outcomes
                WHERE notification_id=?""",
            (notification_id,),
        ).fetchone()
        expected = "uncertain" if outcome == "uncertain" else outcome
        if row is not None and str(row["outcome"]) == "uncertain":
            expected = "uncertain"
        if row is None or str(row["outcome"]) != expected or int(row["status_reconciled"]) != 1:
            raise RuntimeError("reconciled notification delivery outcome changed")

    @_rollback_failed_write
    def confirm_notification_status_reconciled(self, notification_id: str) -> None:
        """Prove the visible carrier's terminal status completed before ACK."""

        notification_id = str(notification_id or "").strip()
        if not notification_id:
            raise ValueError("invalid notification delivery identity")
        self._conn.execute(
            """UPDATE notification_delivery_outcomes
               SET status_reconciled=1, updated_at=?
               WHERE notification_id=?""",
            (time.time(), notification_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            """SELECT status_reconciled FROM notification_delivery_outcomes
               WHERE notification_id=?""",
            (notification_id,),
        ).fetchone()
        if row is None or int(row["status_reconciled"]) != 1:
            raise RuntimeError("notification status reconciliation was not persisted")

    @_rollback_failed_write
    def begin_notification_part_delivery(self, notification_id: str, part_key: str) -> str:
        """Atomically arm a pre-write fence; only its creator receives ``armed``."""

        notification_id = str(notification_id or "").strip()
        part_key = str(part_key or "").strip()
        if not notification_id or not part_key:
            raise ValueError("notification delivery identity is incomplete")
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO notification_delivery_parts(
                   notification_id, part_key, state, updated_at)
               VALUES(?, ?, 'uncertain', ?)""",
            (notification_id, part_key, time.time()),
        )
        self._conn.commit()
        row = self._conn.execute(
            """SELECT state FROM notification_delivery_parts
               WHERE notification_id=? AND part_key=?""",
            (notification_id, part_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("notification delivery fence could not be committed")
        return "armed" if cursor.rowcount > 0 else str(row["state"])

    @_rollback_failed_write
    def confirm_notification_part_delivery(self, notification_id: str, part_key: str) -> bool:
        """Close an armed fence only after Telegram returned a successful response."""

        notification_id = str(notification_id or "").strip()
        part_key = str(part_key or "").strip()
        self._conn.execute(
            """UPDATE notification_delivery_parts
               SET state='confirmed', updated_at=?
               WHERE notification_id=? AND part_key=? AND state='uncertain'""",
            (time.time(), notification_id, part_key),
        )
        self._conn.commit()
        row = self._conn.execute(
            """SELECT state FROM notification_delivery_parts
               WHERE notification_id=? AND part_key=?""",
            (notification_id, part_key),
        ).fetchone()
        return row is not None and str(row["state"]) == "confirmed"

    @_rollback_failed_write
    def reject_notification_part_delivery(self, notification_id: str, part_key: str) -> bool:
        """Disarm only after proven non-acceptance (connect failure or HTTP rejection)."""

        cursor = self._conn.execute(
            """DELETE FROM notification_delivery_parts
               WHERE notification_id=? AND part_key=? AND state='uncertain'""",
            (str(notification_id), str(part_key)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    @_rollback_failed_write
    def forget_notification_delivery_parts(self, notification_ids: list[str]) -> None:
        """Retire local fences after the backend durably accepted their terminal ack."""

        cleaned = [str(value) for value in notification_ids if str(value)]
        if not cleaned:
            return
        self._conn.executemany(
            "DELETE FROM notification_delivery_parts WHERE notification_id=?",
            [(value,) for value in cleaned],
        )
        self._conn.executemany(
            "DELETE FROM notification_delivery_outcomes WHERE notification_id=?",
            [(value,) for value in cleaned],
        )
        self._conn.commit()

    def generated_file_was_delivered(self, delivery_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM delivered_generated_files WHERE delivery_key=?",
            (str(delivery_key),),
        ).fetchone()
        return row is not None

    @_rollback_failed_write
    def remember_generated_file_delivery(self, delivery_key: str) -> None:
        """Checkpoint one successful sendDocument before the update can retry."""

        self._conn.execute(
            """INSERT OR IGNORE INTO delivered_generated_files(delivery_key, delivered_at)
               VALUES(?, ?)""",
            (str(delivery_key), time.time()),
        )
        self._conn.commit()

    @_rollback_failed_write
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

    def contiguous_pending_rows(
        self,
        ordering_key: str,
        anchor_update_id: int,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return the pending FIFO suffix beginning at one exact update.

        Telegram media groups have no explicit terminal item.  The transport
        therefore observes the short, contiguous suffix for the same chat and
        decides where the group ends from the durable payloads.  This method
        deliberately does not skip a row: a normal chat message is a hard
        boundary, so a reused/malformed ``media_group_id`` can never gather
        unrelated later uploads into one turn.
        """

        rows = self._conn.execute(
            """SELECT * FROM updates
               WHERE status='pending' AND ordering_key=? AND update_id>=?
               ORDER BY update_id ASC LIMIT ?""",
            (
                str(ordering_key),
                int(anchor_update_id),
                max(1, min(int(limit), BATCH_SIZE * 2)),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    @_rollback_failed_write
    def mark_failure(self, update_id: int, error: str) -> bool:
        row = self._conn.execute(
            "SELECT attempts FROM updates WHERE update_id=? AND status='pending'",
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
               WHERE update_id=? AND status='pending'""",
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

    @_rollback_failed_write
    def mark_failure_many(self, update_ids: list[int], error: str) -> bool:
        """Charge one failed owned album attempt to every durable part atomically."""

        cleaned = list(dict.fromkeys(int(value) for value in update_ids))
        if not cleaned:
            return False
        placeholders = ",".join("?" for _value in cleaned)
        rows = self._conn.execute(
            f"""SELECT update_id, attempts, chunks_sent FROM updates
                WHERE update_id IN ({placeholders}) AND status='pending'""",  # nosec B608
            cleaned,
        ).fetchall()
        if len(rows) != len(cleaned):
            # Ownership was resolved from these exact durable rows. A missing
            # sibling means concurrent/corrupt queue mutation; do not advance a
            # prefix into a state where it can later dispatch alone.
            return False
        attempted_at = time.time()
        group_attempts = max(int(row["attempts"]) for row in rows) + 1
        dead_lettered = group_attempts >= MAX_ATTEMPTS
        chunks_by_update = {int(row["update_id"]): int(row["chunks_sent"]) for row in rows}
        notice_resume_base = chunks_by_update[cleaned[0]]
        updates: list[tuple[int, float, str, str, float, float | None, int]] = []
        for row in rows:
            delay = RETRY_DELAYS_SEC[min(group_attempts - 1, len(RETRY_DELAYS_SEC) - 1)]
            stored_attempts = MAX_ATTEMPTS - 1 if dead_lettered else group_attempts
            stored_error = (
                _dead_letter_notice_marker(
                    error,
                    permanent=False,
                    resume_base=notice_resume_base,
                )
                if dead_lettered
                else error[:500]
            )
            updates.append(
                (
                    stored_attempts,
                    attempted_at,
                    stored_error,
                    "pending",
                    attempted_at + delay,
                    None,
                    int(row["update_id"]),
                )
            )
        self._conn.executemany(
            """UPDATE updates
               SET attempts=?, last_attempt_at=?, last_error=?, status=?,
                   next_attempt_at=?, failed_at=?
               WHERE update_id=? AND status='pending'""",
            updates,
        )
        self._conn.commit()
        return dead_lettered

    @_rollback_failed_write
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

    @_rollback_failed_write
    def mark_dead_letter_many(self, update_ids: list[int], error: str) -> None:
        cleaned = list(dict.fromkeys(int(value) for value in update_ids))
        if not cleaned:
            return
        failed_at = time.time()
        self._conn.executemany(
            """UPDATE updates
               SET status='dead_letter', last_error=?, last_attempt_at=?,
                   next_attempt_at=0, failed_at=?
               WHERE update_id=?""",
            [(error[:500], failed_at, failed_at, update_id) for update_id in cleaned],
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

    @_rollback_failed_write
    def record_answer_chunks_sent(self, update_id: int, count: int) -> None:
        """Отметить кусок ушедшим — СРАЗУ, а не в конце отправки.

        Счётчик живёт на строке обновления и исчезает вместе с ней: успешно
        отвеченное обновление удаляется целиком, поэтому обнулять его отдельно
        не нужно и нечего забыть.
        """
        self._conn.execute(
            "UPDATE updates SET chunks_sent=?, delivery_uncertainty=0 WHERE update_id=?",
            (max(0, int(count)), int(update_id)),
        )
        self._conn.commit()

    @_rollback_failed_write
    def begin_answer_chunk_delivery(self, update_id: int, count: int) -> tuple[int, int] | None:
        """Fence one chunk *before* its first network byte can be written.

        Advancing the cursor before I/O intentionally chooses at-most-once over
        silent duplication.  A process death before the write may lose this one
        chunk, but the same durable state produces the bounded uncertainty
        notice on restart.  A confirmed pre-accept failure can atomically restore
        the snapshot through :meth:`reject_answer_chunk_delivery`.
        """

        exact_update_id = int(update_id)
        exact_count = max(0, int(count))
        row = self._conn.execute(
            "SELECT chunks_sent, delivery_uncertainty FROM updates WHERE update_id=?",
            (exact_update_id,),
        ).fetchone()
        if row is None:
            return None
        previous_count = int(row["chunks_sent"])
        previous_uncertainty = int(row["delivery_uncertainty"])
        if exact_count != previous_count + 1 or previous_uncertainty not in {0, 1, 2}:
            return None
        cursor = self._conn.execute(
            """UPDATE updates SET chunks_sent=?, delivery_uncertainty=1
                 WHERE update_id=? AND chunks_sent=? AND delivery_uncertainty=?""",
            (
                exact_count,
                exact_update_id,
                previous_count,
                previous_uncertainty,
            ),
        )
        if cursor.rowcount != 1:
            # Even a zero-row UPDATE opens a deferred write transaction.  This
            # is a normal compare-and-swap miss, so close it explicitly rather
            # than waiting for another mutator's entry guard to clean it up.
            self._conn.rollback()
            return None
        try:
            self._conn.commit()
        except BaseException:
            # No Telegram awaitable exists yet.  Even when SQLite committed but
            # lost the local success acknowledgement, restoring this exact CAS
            # snapshot is safe and keeps a proven pre-effect failure retryable.
            _rollback_or_quarantine(self._conn)
            with suppress(BaseException):
                self.reject_answer_chunk_delivery(
                    exact_update_id,
                    exact_count,
                    previous_count=previous_count,
                    previous_uncertainty=previous_uncertainty,
                )
            raise
        return previous_count, previous_uncertainty

    @_rollback_failed_write
    def reject_answer_chunk_delivery(
        self,
        update_id: int,
        count: int,
        *,
        previous_count: int,
        previous_uncertainty: int,
    ) -> bool:
        """Roll back only the exact pre-write fence proven not to be accepted."""

        exact_count = max(0, int(count))
        prior_count = max(0, int(previous_count))
        prior_uncertainty = int(previous_uncertainty)
        if exact_count != prior_count + 1 or prior_uncertainty not in {0, 1, 2}:
            return False
        cursor = self._conn.execute(
            """UPDATE updates SET chunks_sent=?, delivery_uncertainty=?
                 WHERE update_id=? AND chunks_sent=? AND delivery_uncertainty=1""",
            (
                prior_count,
                prior_uncertainty,
                int(update_id),
                exact_count,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_rollback_failed_write
    def confirm_answer_chunk_delivery(self, update_id: int, count: int) -> bool:
        """Turn the exact pre-write fence into one confirmed delivered cursor."""

        exact_count = max(0, int(count))
        cursor = self._conn.execute(
            """UPDATE updates SET delivery_uncertainty=0
                 WHERE update_id=? AND chunks_sent=? AND delivery_uncertainty=1""",
            (int(update_id), exact_count),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_rollback_failed_write
    def record_uncertain_answer_chunk(self, update_id: int, count: int) -> None:
        """Never resend a chunk whose Telegram acceptance is unknowable.

        ``ReadTimeout`` happens after the request has been written and may mean
        that Telegram accepted the message but its response was lost.  Advance
        the durable cursor and arm one code-owned warning in the same commit.
        """

        self._conn.execute(
            """UPDATE updates
               SET chunks_sent=MAX(chunks_sent, ?), delivery_uncertainty=1
               WHERE update_id=?""",
            (max(0, int(count)), int(update_id)),
        )
        self._conn.commit()

    def answer_delivery_uncertainty_pending(self, update_id: int) -> bool:
        row = self._conn.execute(
            "SELECT delivery_uncertainty FROM updates WHERE update_id=?",
            (int(update_id),),
        ).fetchone()
        return bool(row is not None and int(row["delivery_uncertainty"]) == 1)

    @_rollback_failed_write
    def begin_answer_delivery_uncertainty_notice(self, update_id: int) -> bool:
        """Fence the warning before its network write, making it at-most-once."""

        cursor = self._conn.execute(
            """UPDATE updates SET delivery_uncertainty=2
               WHERE update_id=? AND delivery_uncertainty=1""",
            (int(update_id),),
        )
        try:
            self._conn.commit()
        except BaseException:
            # As above, the warning has not reached the network yet.  Restore a
            # commit whose return was ambiguous so restart may still send it.
            _rollback_or_quarantine(self._conn)
            with suppress(BaseException):
                self.retry_answer_delivery_uncertainty_notice(update_id)
            raise
        return cursor.rowcount == 1

    @_rollback_failed_write
    def retry_answer_delivery_uncertainty_notice(self, update_id: int) -> None:
        """A pre-accept connection failure may retry the fenced warning."""

        self._conn.execute(
            """UPDATE updates SET delivery_uncertainty=1
               WHERE update_id=? AND delivery_uncertainty=2""",
            (int(update_id),),
        )
        self._conn.commit()

    @_rollback_failed_write
    def cache_backend_response(self, update_id: int, response: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE updates SET backend_response_json=? WHERE update_id=?",
            (json.dumps(response, ensure_ascii=False, sort_keys=True), update_id),
        )
        self._conn.commit()

    @_rollback_failed_write
    def remove(self, update_id: int) -> None:
        self._conn.execute("DELETE FROM updates WHERE update_id=?", (update_id,))
        self._conn.commit()

    @_rollback_failed_write
    def remove_many(self, update_ids: list[int]) -> None:
        cleaned = list(dict.fromkeys(int(value) for value in update_ids))
        if not cleaned:
            return
        self._conn.executemany(
            "DELETE FROM updates WHERE update_id=?",
            [(update_id,) for update_id in cleaned],
        )
        self._conn.commit()
