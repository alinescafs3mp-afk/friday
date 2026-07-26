"""The durable update queue: what Telegram sent, until the backend has taken it.

Moved verbatim out of the single 1670-line module. It was always a separate class;
only its address changed.
"""

from __future__ import annotations

from jericho.telegram_bridge._base import (
    BATCH_SIZE,
    MAX_ATTEMPTS,
    RETRY_DELAYS_SEC,
    Any,
    Path,
    json,
    sqlite3,
    time,
)


class _UpdateInbox:
    """SQLite queue: Telegram offsets advance only after an update is durable."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
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
                failed_at REAL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Idempotent upgrade from the original durable-inbox schema.  Add
        # columns before recreating the index so an existing database can never
        # fail halfway through startup because an index references new fields.
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(updates)").fetchall()}
        additions = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "next_attempt_at": "REAL NOT NULL DEFAULT 0",
            "failed_at": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE updates ADD COLUMN {name} {definition}")
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
        self._conn.commit()

    def store(self, update: dict[str, Any]) -> bool:
        update_id = int(update.get("update_id", -1))
        if update_id < 0:
            return False
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO updates(update_id, payload_json, created_at)
               VALUES(?, ?, ?)""",
            (update_id, json.dumps(update, ensure_ascii=False, sort_keys=True), time.time()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        ready_at = time.time() if now is None else float(now)
        rows = self._conn.execute(
            """SELECT * FROM updates
               WHERE status='pending' AND attempts < ? AND next_attempt_at <= ?
               ORDER BY update_id ASC LIMIT ?""",
            (MAX_ATTEMPTS, ready_at, BATCH_SIZE),
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

    def cache_backend_response(self, update_id: int, response: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE updates SET backend_response_json=? WHERE update_id=?",
            (json.dumps(response, ensure_ascii=False, sort_keys=True), update_id),
        )
        self._conn.commit()

    def remove(self, update_id: int) -> None:
        self._conn.execute("DELETE FROM updates WHERE update_id=?", (update_id,))
        self._conn.commit()
