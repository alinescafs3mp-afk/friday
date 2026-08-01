"""Storage methods for connection lifecycle, schema creation and migration.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import re
from datetime import date

from friday.storage._base import (
    CORE_INDEX_SCHEMA,
    CORE_TABLE_SCHEMA,
    FTS_SCHEMA,
    FTS_VOCAB_SCHEMA,
    LOGGER,
    MISSION_TASKS_SCHEMA,
    SCHEMA_VERSION,
    Any,
    FridaySettings,
    Iterator,
    StorageClosedError,
    StorageShared,
    UnsupportedSchemaVersionError,
    _snapshot,
    contextmanager,
    hashlib,
    json,
    new_id,
    normalize_entity_name,
    sqlite3,
    suppress,
    threading,
    time,
    utc_now,
)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_DATE_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$")


def iso_date(value: Any) -> str | None:
    """Привести дату документа к `ГГГГ-ММ-ДД` или вернуть None.

    Строки берутся из бумаги как есть, поэтому здесь три работы сразу: узнать форму,
    отбросить не-даты и отбросить невозможные даты.

    Замерено на архиве владельца (3180 значений у 630 объектов): 2537 в форме
    дд.мм.гггг, 345 уже в ISO, 223 — это ВРЕМЯ («1:25»), остальное мусор, включая
    «00.00.0000». Время и мусор обязаны отсеиваться здесь, а не попадать в диапазон
    случайно: иначе фильтр «за март 2023» вернёт документы, в которых нет ни одной
    мартовской даты, и человек перестанет ему верить.

    Год ограничен снизу: «01.01.0001» — это не дата документа, а артефакт разбора.
    """
    if value is None:
        return None
    text = str(value).strip()
    match = _ISO_DATE_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = _DMY_DATE_RE.match(text)
        if not match:
            return None
        day, month, year = (int(part) for part in match.groups())
    if not (1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        date(year, month, day)
    except ValueError:
        # 31 февраля и подобное: строка выглядит датой, датой не являясь.
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


class CoreMixin(StorageShared):
    def __init__(self, settings: FridaySettings) -> None:
        self.settings = settings
        self._db_path = settings.database_path
        # Connection-per-thread: no two threads ever share one sqlite3.Connection,
        # so cursors are never stepped concurrently on a single connection (the
        # half-written-read hazard). WAL then gives many concurrent readers plus a
        # single writer across the separate connections. ``threading.local`` holds
        # each thread's connection; the registry lets close() shut every thread's
        # connection down (e.g. before a restore swaps the database file), and the
        # generation counter invalidates the per-thread caches after close() so the
        # next use transparently reopens.
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._generation = 0
        # Writers serialise through this process-wide lock, held for the whole
        # transaction() block. Reads never take it, so the WAL many-reader win is
        # preserved; writers, however, keep the single-writer invariant in Python
        # (no two BEGIN IMMEDIATE ever contend at the SQLite level, so a writer can
        # never surface "database is locked" from internal contention). close()
        # also takes it, so a shutdown cannot close a connection out from under an
        # in-flight write transaction on a background-worker thread — restoring the
        # drain-before-close guarantee the old shared RLock provided. Reentrant so
        # nested transaction() on one thread does not self-deadlock.
        self._write_lock = threading.RLock()
        # Schema creation/migration runs exactly once, on the first connection,
        # behind this lock; every other connection only opens and configures itself.
        self._init_lock = threading.Lock()
        self._schema_ready = False
        self._fts_available = True
        # Set by close(final=True). Distinct from a plain close(), which must stay
        # reopenable — restore_backup swaps the database file and then carries on.
        self._shut_down = False

    @property
    def conn(self) -> sqlite3.Connection:
        local = self._local
        cached = getattr(local, "conn", None)
        if cached is not None and getattr(local, "generation", None) == self._generation:
            return cached
        if self._shut_down:
            # A thread that outlived shutdown asking for a connection is not a
            # request to reopen the database; it is work that should have finished.
            # Silently handing it a fresh connection is how writes escaped past the
            # released process lease.
            raise StorageClosedError("Storage is shut down; this connection will not be reopened")
        connection = self._open()
        with self._registry_lock:
            self._connections.append(connection)
            local.conn = connection
            local.generation = self._generation
        return connection

    @staticmethod
    def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _open(self) -> sqlite3.Connection:
        """Open and migrate SQLite, tolerating concurrent first-start workers.

        SQLite's connection timeout does not consistently cover
        ``PRAGMA journal_mode=WAL`` or schema initialization.  A second process
        starting at the same instant can therefore receive ``database is
        locked`` before normal busy handling applies.  Retrying the complete,
        idempotent initialization avoids exposing a half-configured connection.
        """

        deadline = time.monotonic() + 15.0
        delay = 0.025
        while True:
            try:
                return self._open_once()
            except sqlite3.OperationalError as exc:
                if not self._is_sqlite_busy(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.8, 0.5)

    def _open_once(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so close() can shut every thread's connection
        # down from the shutdown/restore thread; cross-thread *use* is prevented
        # structurally (the conn property only ever hands back the caller thread's
        # own connection via threading.local), not by the sqlite thread check.
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            # Per-connection configuration — applied to EVERY thread's connection.
            # busy_timeout must precede WAL negotiation; sqlite3's connect timeout
            # alone does not reliably protect that PRAGMA on every platform. WAL is
            # persistent in the database header (idempotent to re-issue), but
            # foreign_keys and synchronous are per-connection and non-persistent, so
            # every connection must set them or FK enforcement silently disappears.
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            # SQLite's lower()/NOCASE only fold ASCII; tags and entity names are
            # frequently Cyrillic, so Unicode-correct case-insensitive queries
            # (browse-by-tag) go through Python's casefold instead. User functions
            # are registered per connection.
            conn.create_function(
                "jericho_casefold",
                1,
                lambda value: str(value).casefold() if value is not None else None,
                deterministic=True,
            )
            # Даты из документов извлечены и лежат в метаданных СЫРЫМИ строками — так,
            # как они написаны в бумаге. Замерено на архиве владельца: 3180 значений у
            # 630 объектов, из них 2537 в форме дд.мм.гггг, 345 в ISO, 223 — вообще
            # время («1:25»), остальное мусор вроде «00.00.0000». Фильтровать по такому
            # можно только приведя к одной форме, а приводить надо В SQL: иначе условие
            # не построить, и работа остаётся лежать в JSON-блобе без применения.
            conn.create_function(
                "jericho_iso_date",
                1,
                iso_date,
                deterministic=True,
            )
            # Schema creation/migration/FTS is applied exactly once, by the first
            # connection; later connections open against the already-migrated file.
            self._ensure_schema(conn)
            # The core migration transiently lowers busy_timeout (a PRAGMA embedded
            # in CORE_SCHEMA); restore the uniform value on the migrating connection.
            conn.execute("PRAGMA busy_timeout=10000")
            return conn
        except BaseException:
            conn.close()
            raise

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create/upgrade the schema exactly once, behind a barrier.

        The first connection runs the (idempotent) migration; no connection may
        run application queries until it has committed, so the double-checked
        ``_init_lock`` both serialises the single migration and makes later
        openers wait for it. A busy failure propagates so ``_open``'s retry loop
        re-runs the still-idempotent migration on a fresh connection.
        """

        if self._schema_ready:
            return
        with self._init_lock:
            if self._schema_ready:
                return
            self._migrate_schema(conn)
            self._schema_ready = True

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        # Fail closed before executing any DDL. Opening a database produced by a
        # newer Friday build must never rewrite its schema marker or attempt a
        # best-effort downgrade. A malformed explicit marker is treated the same
        # way; databases without a marker remain valid pre-release migrations.
        schema_meta_preexisting = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
            is not None
        )
        previous_schema_version: str | None = None
        # Separate from the schema marker ON PURPOSE. The core migration commits
        # first, then the FTS phase runs and commits second — and `executescript`
        # makes the FTS DDL durable on its own. A process that died in that window
        # left a database whose schema marker was current and whose FTS tables
        # existed but were EMPTY, and the next open saw "marker current, tables
        # present" and skipped the rebuild forever: every pre-crash document became
        # unfindable by search, silently and permanently.
        #
        # This marker is written only after the FTS phase itself commits, so the
        # crash window reopens as "index not built by this version" and heals.
        fts_build_marker: str | None = None
        if schema_meta_preexisting:
            row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            previous_schema_version = str(row[0]).strip() if row else None
            marker_row = conn.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone()
            fts_build_marker = str(marker_row[0]).strip() if marker_row else None
            if previous_schema_version:
                try:
                    parsed_version = int(previous_schema_version)
                except ValueError as exc:
                    raise UnsupportedSchemaVersionError(
                        f"Invalid database schema version: {previous_schema_version!r}"
                    ) from exc
                if parsed_version < 0 or parsed_version > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Database schema version {parsed_version} is not supported by "
                        f"this Friday build (maximum {SCHEMA_VERSION})"
                    )

        fts_preexisting = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'").fetchone()
            is not None
        )
        # Probed BEFORE the FTS DDL runs, like the line above. Asking afterwards
        # always answers "yes" — the table has just been created — and the rebuild
        # that populates an external-content index over pre-existing rows is then
        # skipped, leaving an index that reports rows and matches nothing.
        raw_fts_preexisting = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_fts'").fetchone()
            is not None
        )
        messages_fts_preexisting = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'").fetchone()
            is not None
        )
        # Create/recognize tables first, then add legacy columns, and only then
        # create indexes that reference those columns. Keep this core migration in
        # one explicit transaction: sqlite3's executescript() commits an existing
        # transaction implicitly, which otherwise leaves half-applied DDL/backfills
        # after a migration failure.
        # The legacy reconstruction is an UPGRADE step, and it ran on every
        # process's first connection instead. That is how a corrected backfill keeps
        # re-firing on data that is already right — and it re-scanned every entity
        # row on every start for nothing. The marker is only written after the whole
        # migration transaction commits, so version == SCHEMA_VERSION already proves
        # the backfill ran; an absent or lower marker still runs it.
        already_current = previous_schema_version is not None and (
            previous_schema_version.strip() == str(SCHEMA_VERSION)
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._execute_statements(conn, CORE_TABLE_SCHEMA)
            if not already_current:
                self._migrate_legacy_schema(conn)
            self._execute_statements(conn, CORE_INDEX_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?)",
                (str(SCHEMA_VERSION), utc_now()),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

        # FTS is an optional, self-healing derivative index. Build it only after
        # the authoritative core schema/data transaction has committed, so an
        # unavailable FTS5 module can never roll back or partially expose personal
        # knowledge migration.
        # True whenever this build has not recorded a finished FTS build. Covers
        # the crash window above and every database written before the marker
        # existed. `integrity-check` cannot stand in for it: measured on SQLite,
        # an external-content index that is entirely EMPTY passes integrity-check
        # and matches nothing — the check verifies what the index claims against
        # itself, not against the content table it shadows.
        fts_unverified = fts_build_marker != str(SCHEMA_VERSION)
        try:
            conn.executescript(FTS_SCHEMA)
            knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0]
            if knowledge_count and (not fts_preexisting or fts_unverified):
                # An external-content FTS table created after rows already exist
                # starts with an empty index. Rebuild before update triggers can
                # attempt to delete missing index entries.
                conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
            elif fts_preexisting and previous_schema_version != str(SCHEMA_VERSION):
                try:
                    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('integrity-check')")
                except sqlite3.DatabaseError:
                    LOGGER.warning("Rebuilding an inconsistent knowledge FTS index")
                    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
            # Same contract for the source index: an external-content FTS table
            # created over existing rows starts EMPTY, and the update triggers would
            # then try to delete index entries that were never written.
            raw_count = conn.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0]
            if raw_count and (not raw_fts_preexisting or fts_unverified):
                conn.execute("INSERT INTO raw_fts(raw_fts) VALUES('rebuild')")
            # Same contract for chat history: messages predate messages_fts on any
            # schema < 20, so an empty external-content index must rebuild once.
            messages_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if messages_count and (not messages_fts_preexisting or fts_unverified):
                conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            elif messages_fts_preexisting and previous_schema_version != str(SCHEMA_VERSION):
                try:
                    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
                except sqlite3.DatabaseError:
                    LOGGER.warning("Rebuilding an inconsistent messages FTS index")
                    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            # The vocabulary view, in its own try: losing spelling repair on an
            # SQLite without `fts5vocab` is a degradation, losing full-text search
            # with it would be an outage.
            try:
                conn.executescript(FTS_VOCAB_SCHEMA)
            except sqlite3.OperationalError as exc:
                LOGGER.info("fts5vocab is unavailable; spelling repair disabled: %s", exc)
            # Last, and inside the same commit as the rebuilds it certifies.
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES('fts_build', ?, ?)",
                (str(SCHEMA_VERSION), utc_now()),
            )
        except sqlite3.OperationalError as exc:
            if self._is_sqlite_busy(exc):
                raise
            self._fts_available = False
            LOGGER.warning("SQLite FTS5 is unavailable; using LIKE search: %s", exc)
        conn.commit()

    @staticmethod
    def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
        """Execute a plain SQL script without sqlite3's implicit commit.

        ``sqlite3.complete_statement`` handles multiline indexes, comments and
        trigger bodies (it only reports completion at the trigger's final
        ``END;``) while preserving the caller's explicit transaction.
        """

        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if not sqlite3.complete_statement(pending):
                continue
            statement = pending.strip()
            pending = ""
            if statement:
                conn.execute(statement)
        if pending.strip():
            raise sqlite3.OperationalError("Incomplete SQL statement in core schema")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _widen_mission_task_states(conn: sqlite3.Connection, table_names: set[str]) -> None:
        """Разрешить шагам миссии состояния восстановления.

        `uncertain` («неизвестно, случился ли побочный эффект») и `compensated`
        появились в схеме 24 вместе с чекпойнтами. SQLite не умеет менять CHECK
        на месте, а `ALTER TABLE ADD COLUMN` его не трогает — поэтому база,
        созданная прежней версией, молча отвергала бы новое состояние ровно в тот
        момент, когда оно нужнее всего: при разборе сбоя.

        Таблица пересоздаётся со строками: шаги миссии — рабочие записи, терять
        их нельзя. Идёт только если старое ограничение действительно узкое.
        """
        if "mission_tasks" not in table_names:
            return
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mission_tasks'"
        ).fetchone()
        definition = str((row[0] if row else "") or "")
        if "uncertain" in definition:
            return
        columns = [item[1] for item in conn.execute("PRAGMA table_info(mission_tasks)")]
        shared = ", ".join(columns)
        conn.execute("ALTER TABLE mission_tasks RENAME TO mission_tasks_pre24")
        # `executescript` неявно коммитит открытую транзакцию — на середине
        # миграции это означает половину применённой схемы при любой ошибке
        # дальше. Здесь ровно одно выражение, поэтому обычный execute.
        conn.execute(MISSION_TASKS_SCHEMA.strip().rstrip(";"))
        conn.execute(f"INSERT INTO mission_tasks({shared}) SELECT {shared} FROM mission_tasks_pre24")  # nosec B608
        conn.execute("DROP TABLE mission_tasks_pre24")

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        """Upgrade pre-release databases without discarding personal data.

        Early Friday builds shipped before the users table, provenance hashes,
        normalized entity names and JSON snapshots existed. SQLite cannot add
        foreign keys or table constraints in place, so the migration adds the
        columns needed by current code and backfills equivalent records. New
        installations take the same idempotent path with no data to transform.
        """
        table_names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        additions: dict[str, dict[str, str]] = {
            "raw_objects": {
                "content_hash": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1",
                "deleted_at": "TEXT",
            },
            "knowledge_object_versions": {
                "user_id": "TEXT NOT NULL DEFAULT ''",
                "snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "knowledge_objects": {
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "knowledge_kind": "TEXT NOT NULL DEFAULT 'note'",
                "quality_score": "REAL NOT NULL DEFAULT 0.5",
                "promotion_score": "REAL NOT NULL DEFAULT 0.5",
            },
            "inbox": {
                "knowledge_object_id": "TEXT",
                "suggestions_json": "TEXT NOT NULL DEFAULT '{}'",
                "suggested_action": "TEXT NOT NULL DEFAULT 'review'",
                "promotion_score": "REAL NOT NULL DEFAULT 0.0",
                "quality_score": "REAL NOT NULL DEFAULT 0.0",
            },
            "entities": {"normalized_name": "TEXT NOT NULL DEFAULT ''"},
            # Бюджеты, срок и следы восстановления миссий (спека v3 §5, схема 24).
            "missions": {
                "budget_seconds": "INTEGER NOT NULL DEFAULT 0",
                "budget_tool_calls": "INTEGER NOT NULL DEFAULT 0",
                "budget_retries": "INTEGER NOT NULL DEFAULT 0",
                "spent_seconds": "INTEGER NOT NULL DEFAULT 0",
                "spent_tool_calls": "INTEGER NOT NULL DEFAULT 0",
                "spent_retries": "INTEGER NOT NULL DEFAULT 0",
                "deadline_at": "TEXT",
            },
            "mission_tasks": {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "side_effect": "INTEGER NOT NULL DEFAULT 0",
                "checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
                "compensation": "TEXT NOT NULL DEFAULT ''",
            },
            "entity_merge_history": {
                "transfer_json": "TEXT NOT NULL DEFAULT '{}'",
                "undone_at": "TEXT",
                "undone_by": "TEXT",
            },
            "entity_resolution_candidates": {
                "pair_key": "TEXT NOT NULL DEFAULT ''",
                "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "audit_log": {"request_id": "TEXT NOT NULL DEFAULT ''"},
            "request_idempotency": {
                "request_hash": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'complete'",
                "lease_token": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            "conversations": {"mode": "TEXT NOT NULL DEFAULT 'dialogue'"},
            "channel_sessions": {"mode": "TEXT NOT NULL DEFAULT 'dialogue'"},
            # NULL expires_at = a non-expiring token (all legacy tokens stay valid).
            "api_tokens": {"expires_at": "TEXT"},
            # Fingerprint of the chunking config an object's chunk rows were built
            # with. '' means "not chunked" -- exactly what every pre-0.41 row already
            # stores, so turning chunking off re-indexes nothing.
            "knowledge_embeddings": {"chunk_scheme": "TEXT NOT NULL DEFAULT ''"},
        }
        for table, columns in additions.items():
            if table not in table_names:
                continue
            existing = self._table_columns(conn, table)
            for column, declaration in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        self._widen_mission_task_states(conn, table_names)

        now = utc_now()

        # Register every tenant already referenced by legacy data. This makes
        # old knowledge immediately visible to workers and the Admin UI.
        tenant_tables = (
            "raw_objects",
            "knowledge_objects",
            "inbox",
            "entities",
            "relations",
            "feedback",
            "conversations",
            "messages",
            "audit_log",
        )
        tenant_ids: set[str] = set()
        for table in tenant_tables:
            if table not in table_names or "user_id" not in self._table_columns(conn, table):
                continue
            # ``table`` comes from the fixed legacy tenant-table allowlist above.
            rows = conn.execute(
                f"SELECT DISTINCT user_id FROM {table} WHERE user_id IS NOT NULL AND user_id<>''"  # nosec B608
            ).fetchall()
            tenant_ids.update(str(row[0]) for row in rows if row[0])
        for user_id in sorted(tenant_ids):
            conn.execute(
                """INSERT OR IGNORE INTO users(
                       id, source, external_id, display_name, username, preset_key, status,
                       metadata_json, created_at, updated_at, last_seen_at
                   ) VALUES(?, 'legacy', '', '', '', 'user', 'active', ?, ?, ?, ?)""",
                (
                    user_id,
                    json.dumps({"migrated_from_pre_release": True}, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )

        if "raw_objects" in table_names:
            rows = conn.execute(
                "SELECT id, raw_content FROM raw_objects WHERE content_hash='' OR content_hash IS NULL"
            ).fetchall()
            for row in rows:
                digest = hashlib.sha256(
                    str(row["raw_content"] or "").encode("utf-8", errors="replace")
                ).hexdigest()
                conn.execute(
                    "UPDATE raw_objects SET content_hash=? WHERE id=?",
                    (digest, row["id"]),
                )

        if "knowledge_objects" in table_names:
            # Existing records predate explicit promotion/quality scoring. Keep
            # them searchable, but mark the scores as migrated estimates so the
            # safe cleanup scanner can review them without destructive guesses.
            conn.execute(
                """UPDATE knowledge_objects
                   SET metadata_json=CASE
                         WHEN metadata_json IS NULL OR metadata_json='' THEN '{}'
                         ELSE metadata_json
                       END,
                       knowledge_kind=CASE
                         WHEN knowledge_kind IS NULL OR knowledge_kind='' THEN 'note'
                         ELSE knowledge_kind
                       END,
                       quality_score=CASE
                         WHEN quality_score IS NULL THEN 0.5
                         ELSE MIN(1.0, MAX(0.0, quality_score))
                       END,
                       promotion_score=CASE
                         WHEN promotion_score IS NULL THEN 0.5
                         ELSE MIN(1.0, MAX(0.0, promotion_score))
                       END"""
            )

        if "inbox" in table_names:
            conn.execute(
                """UPDATE inbox
                   SET suggestions_json=CASE
                         WHEN suggestions_json IS NULL OR suggestions_json='' THEN '{}'
                         ELSE suggestions_json
                       END,
                       suggested_action=CASE
                         WHEN suggested_action IS NULL OR suggested_action='' THEN 'review'
                         ELSE suggested_action
                       END,
                       promotion_score=MIN(1.0, MAX(0.0, COALESCE(promotion_score, 0.0))),
                       quality_score=MIN(1.0, MAX(0.0, COALESCE(quality_score, 0.0)))"""
            )

        if "request_idempotency" in table_names:
            conn.execute(
                """UPDATE request_idempotency
                   SET request_hash=COALESCE(request_hash, ''),
                       state=CASE WHEN state='pending' THEN 'pending' ELSE 'complete' END,
                       lease_token=COALESCE(lease_token, ''),
                       response_json=COALESCE(NULLIF(response_json, ''), '{}'),
                       updated_at=CASE
                         WHEN updated_at IS NULL OR updated_at='' THEN created_at
                         ELSE updated_at
                       END"""
            )

        if "conversations" in table_names:
            conn.execute(
                """UPDATE conversations SET mode=CASE
                       WHEN mode IN ('dialogue', 'knowledge_work', 'research') THEN mode
                       ELSE 'dialogue'
                   END"""
            )

        if "channel_sessions" in table_names:
            conn.execute(
                """UPDATE channel_sessions SET mode=CASE
                       WHEN mode IN ('dialogue', 'knowledge_work', 'research') THEN mode
                       ELSE 'dialogue'
                   END"""
            )

        # Schema v8 keeps the append-only feedback history and reconstructs a
        # single current signal per target/type.  Later changes update this
        # projection without deleting the audit trail.
        if {"feedback", "feedback_state"} <= table_names:
            rows = conn.execute(
                """SELECT f.* FROM feedback f
                   JOIN (
                     SELECT user_id, target_type, target_id, feedback_type, MAX(created_at) AS newest
                     FROM feedback
                     GROUP BY user_id, target_type, target_id, feedback_type
                   ) latest
                     ON latest.user_id=f.user_id
                    AND latest.target_type=f.target_type
                    AND latest.target_id=f.target_id
                    AND latest.feedback_type=f.feedback_type
                    AND latest.newest=f.created_at
                   ORDER BY f.id DESC"""
            ).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO feedback_state(
                           user_id, target_type, target_id, feedback_type, score,
                           comment, context_json, feedback_id, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["user_id"],
                        row["target_type"],
                        row["target_id"],
                        row["feedback_type"],
                        row["score"],
                        row["comment"],
                        row["context_json"],
                        row["id"],
                        row["created_at"],
                    ),
                )

        if {"knowledge_objects", "knowledge_usage"} <= table_names:
            conn.execute(
                """INSERT OR IGNORE INTO knowledge_usage(
                       user_id, knowledge_object_id, updated_at
                   )
                   SELECT user_id, id, COALESCE(NULLIF(updated_at, ''), created_at)
                   FROM knowledge_objects"""
            )

        if "entities" in table_names:
            # Recompute all values because schema v5 preserves punctuation in compact identifiers.
            # This prevents distinct symbols such as ``ABC.A`` and ``ABC/B`` from collapsing.
            #
            # v18 recomputes them again: `normalize_entity_name` now folds Russian
            # inflection and `ё`, so «CIDR-ПОДПИСКА» and «CIDR-ПОДПИСКУ» resolve to
            # one node instead of accumulating one node per grammatical case. The
            # rows are NOT merged here — folding changes what a lookup finds and
            # makes the pair visible as a duplicate candidate, while merging two
            # existing nodes stays the owner's decision.
            rows = conn.execute("SELECT id, name FROM entities").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE entities SET normalized_name=? WHERE id=?",
                    (normalize_entity_name(row["name"]), row["id"]),
                )

        if "entity_resolution_candidates" in table_names:
            rows = conn.execute(
                "SELECT id, entity_a_id, entity_b_id FROM entity_resolution_candidates "
                "WHERE pair_key='' OR pair_key IS NULL"
            ).fetchall()
            for row in rows:
                pair_key = "|".join(sorted((row["entity_a_id"], row["entity_b_id"])))
                conn.execute(
                    "UPDATE entity_resolution_candidates SET pair_key=? WHERE id=?",
                    (pair_key, row["id"]),
                )

        # Convert old per-field versions to provenance-rich JSON snapshots.
        if "knowledge_object_versions" in table_names:
            version_columns = self._table_columns(conn, "knowledge_object_versions")
            rows = conn.execute(
                """SELECT v.*, k.user_id AS owner_user_id
                   FROM knowledge_object_versions v
                   JOIN knowledge_objects k ON k.id=v.knowledge_object_id
                   WHERE v.user_id='' OR v.user_id IS NULL
                      OR v.snapshot_json='{}' OR v.snapshot_json IS NULL"""
            ).fetchall()
            for row in rows:
                values = dict(row)
                owner = str(values.pop("owner_user_id") or "")
                snapshot: dict[str, Any] = {
                    key: values.get(key)
                    for key in (
                        "knowledge_object_id",
                        "version",
                        "content",
                        "title",
                        "summary",
                        "tags_json",
                        "importance",
                        "created_at",
                    )
                    if key in version_columns
                }
                snapshot["user_id"] = owner
                conn.execute(
                    "UPDATE knowledge_object_versions SET user_id=?, snapshot_json=? WHERE id=?",
                    (owner, _snapshot(snapshot), row["id"]),
                )

        # Ensure each current object/entity has at least one baseline version.
        if "knowledge_objects" in table_names:
            for row in conn.execute("SELECT * FROM knowledge_objects").fetchall():
                existing = conn.execute(
                    """SELECT 1 FROM knowledge_object_versions
                       WHERE knowledge_object_id=? AND version=? LIMIT 1""",
                    (row["id"], int(row["version"] or 1)),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO knowledge_object_versions
                           (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
                           VALUES(?, ?, ?, ?, ?, ?)""",
                        (
                            new_id("kov"),
                            row["user_id"],
                            row["id"],
                            int(row["version"] or 1),
                            _snapshot(dict(row)),
                            now,
                        ),
                    )

        if "entities" in table_names:
            for row in conn.execute("SELECT * FROM entities").fetchall():
                conn.execute(
                    """INSERT OR IGNORE INTO entity_versions
                       (id, user_id, entity_id, version, snapshot_json, created_at)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("entv"),
                        row["user_id"],
                        row["id"],
                        int(row["version"] or 1),
                        _snapshot(dict(row)),
                        now,
                    ),
                )

        # Reconstruct links represented by legacy convenience columns.
        #
        # `k.deleted_at IS NULL` is load-bearing, not defensive. DATA_LIFECYCLE §3
        # makes IGNORED a verdict: the attached Knowledge Object is soft-deleted and
        # the Inbox link cleared, so the material leaves retrieval. Matching on
        # raw_object_id alone re-pointed that Inbox row straight back at the object
        # the human had just rejected — the reconstruction quietly overruled a
        # review decision it knows nothing about.
        if "inbox" in table_names and "knowledge_objects" in table_names:
            conn.execute(
                """UPDATE inbox
                   SET knowledge_object_id=(
                       SELECT k.id FROM knowledge_objects k
                       WHERE k.user_id=inbox.user_id AND k.raw_object_id=inbox.raw_object_id
                         AND k.deleted_at IS NULL
                       ORDER BY k.version DESC LIMIT 1
                   )
                   WHERE (knowledge_object_id IS NULL OR knowledge_object_id='')
                     AND reviewed_at IS NULL"""
            )

        if {"knowledge_objects", "entities", "knowledge_entity_links"} <= table_names:
            rows = conn.execute(
                """SELECT k.user_id, k.id AS knowledge_object_id, k.entity_id
                   FROM knowledge_objects k
                   JOIN entities e ON e.id=k.entity_id AND e.user_id=k.user_id
                   WHERE k.entity_id IS NOT NULL AND k.entity_id<>''"""
            ).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links(
                           id, user_id, knowledge_object_id, entity_id, status, confidence,
                           evidence_json, created_at, reviewed_at, reviewed_by
                       ) VALUES(?, ?, ?, ?, 'accepted', 1.0, ?, ?, ?, 'migration')""",
                    (
                        new_id("kel"),
                        row["user_id"],
                        row["knowledge_object_id"],
                        row["entity_id"],
                        json.dumps({"source": "legacy_entity_id"}, sort_keys=True),
                        now,
                        now,
                    ),
                )

    def close(self, *, final: bool = False) -> None:
        """Close every thread's connection. ``final`` makes the closure permanent.

        ``final=False`` keeps the reopen-after-close contract that ``restore_backup``
        depends on. ``final=True`` is process shutdown: any later access raises
        ``StorageClosedError`` instead of quietly opening a new connection behind the
        released ``backend.lock``.
        """
        if final:
            self._shut_down = True
        # Shut down every thread's connection (not just the caller's), commit any
        # pending work, then invalidate the per-thread caches by bumping the
        # generation and re-arming the one-time schema init. This makes close()
        # release all WAL locks before a restore swaps the database file, and lets
        # the next use on any thread transparently reopen (and re-migrate) — the
        # reopen-after-close contract restore_backup() depends on.
        #
        # The write lock is taken first (same order as transaction()) so a shutdown
        # drains any in-flight write transaction — e.g. a background worker whose
        # asyncio task was cancelled but whose to_thread() DB call is still running
        # — before its connection is closed, instead of committing a half-written
        # transaction or closing the connection mid-statement.
        with self._write_lock:
            with self._registry_lock:
                connections = list(self._connections)
                self._connections.clear()
                self._generation += 1
                self._schema_ready = False
            for connection in connections:
                with suppress(sqlite3.Error):
                    # Roll back, never commit. A connection still inside a
                    # transaction at shutdown is holding an *unfinished* unit of
                    # work — committing it here is how an aborted write became
                    # durable. Nothing legitimate depends on this commit: every DML
                    # statement in the storage layer runs inside transaction(),
                    # which commits on its own successful exit.
                    if connection.in_transaction:
                        connection.rollback()
                with suppress(sqlite3.Error):
                    connection.close()

    def execute(self, sql: str, params: tuple | dict | None = None) -> sqlite3.Cursor:
        # No lock: this thread's own connection, and the caller fetches the cursor
        # on this same thread, so no cursor is ever stepped across threads.
        return self.conn.execute(sql, params or ())

    def commit(self) -> None:
        self.conn.commit()

    def optimize(self) -> None:
        # PRAGMA optimize runs ANALYZE, which writes sqlite_stat*, so it is a writer
        # and must take the write lock like transaction() — otherwise it contends
        # with a concurrent BEGIN IMMEDIATE at the SQLite level (risking "database is
        # locked") and close() cannot drain it before shutting the connection down.
        with self._write_lock:
            self.conn.execute("PRAGMA optimize")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Serialise writers in Python (single-writer invariant), so two threads'
        # BEGIN IMMEDIATE never contend at the SQLite level and close() can drain
        # the in-flight writer before shutting connections down. Reads never take
        # this lock, so concurrent readers still run in parallel over WAL. The lock
        # is reentrant, so a nested transaction() on the same thread — detected via
        # this thread's own connection's in_transaction — does not self-deadlock.
        with self._write_lock:
            conn = self.conn
            nested = conn.in_transaction
            if not nested:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if not nested:
                    conn.commit()
            except BaseException:
                # BaseException, not Exception: KeyboardInterrupt, SystemExit and
                # asyncio.CancelledError all unwind through here, and all three are
                # ways a *real* transaction gets abandoned — Ctrl-C during `jericho
                # import`, a cancelled worker tick. Catching only Exception left the
                # BEGIN IMMEDIATE open on this connection, and close() then committed
                # it: an interrupted unit of work became a durable partial write.
                if not nested:
                    conn.rollback()
                raise
