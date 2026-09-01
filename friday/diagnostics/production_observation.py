"""Effect-free, body-free observation of durable scheduled work.

This module is deliberately not exported from :mod:`friday.diagnostics`.  A
later owner-only runtime adapter can import it directly after authenticating a
release challenge.  The collector itself neither authenticates callers nor
opens storage: it observes only the already-open backend connection owned by
the process which holds the backend lease.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from friday.config import FridaySettings
from friday.diagnostics.runtime_lease import process_owns_lease
from friday.secondary_product_witness import secondary_product_process_epoch_sha256
from friday.storage import FridayStorage
from friday.storage._core import read_only_storage_snapshot

PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA = "friday.production-read-only-observation.v1"

_BACKEND_LEASE_PROTOCOL = "friday.backend.v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_SCHEMA_VERSION = 50
_WORKER_KEYS = (
    "workers:health:mission_runner",
    "workers:health:reminders_scan",
)
_WORKER_STATES = ("scheduled", "running", "ok", "error", "timeout", "skipped", "unknown")
_MISSION_STATES = (
    "proposed",
    "ready",
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "cancelled",
)
_TASK_STATES = ("pending", "running", "done", "failed", "skipped", "uncertain", "compensated")
_REMINDER_STATES = ("pending", "uncertain", "sent", "failed", "dismissed")
_MAX_SQL_COUNT = (1 << 63) - 1

# PRAGMA table_xinfo rows: cid, name, declared type, not-null, default, pk, hidden.
# Keeping this closed and local prevents a drifted live table from defining its
# own evidence grammar.  In particular, table_info would hide generated columns.
_TABLE_XINFO: dict[str, tuple[tuple[object, ...], ...]] = {
    "schema_meta": (
        (0, "key", "TEXT", 0, None, 1, 0),
        (1, "value", "TEXT", 1, None, 0, 0),
        (2, "updated_at", "TEXT", 1, None, 0, 0),
    ),
    "runtime_kv": (
        (0, "key", "TEXT", 0, None, 1, 0),
        (1, "value", "TEXT", 1, None, 0, 0),
        (2, "updated_at", "TEXT", 1, None, 0, 0),
    ),
    "missions": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "user_id", "TEXT", 1, None, 0, 0),
        (2, "goal", "TEXT", 1, None, 0, 0),
        (3, "title", "TEXT", 1, "''", 0, 0),
        (4, "status", "TEXT", 1, "'ready'", 0, 0),
        (5, "origin", "TEXT", 1, "'user'", 0, 0),
        (6, "plan_summary", "TEXT", 1, "''", 0, 0),
        (7, "created_by", "TEXT", 1, "''", 0, 0),
        (8, "error", "TEXT", 1, "''", 0, 0),
        (9, "task_count", "INTEGER", 1, "0", 0, 0),
        (10, "done_count", "INTEGER", 1, "0", 0, 0),
        (11, "metadata_json", "TEXT", 1, "'{}'", 0, 0),
        (12, "version", "INTEGER", 1, "1", 0, 0),
        (13, "budget_seconds", "INTEGER", 1, "0", 0, 0),
        (14, "budget_tool_calls", "INTEGER", 1, "0", 0, 0),
        (15, "budget_retries", "INTEGER", 1, "0", 0, 0),
        (16, "spent_seconds", "INTEGER", 1, "0", 0, 0),
        (17, "spent_tool_calls", "INTEGER", 1, "0", 0, 0),
        (18, "spent_retries", "INTEGER", 1, "0", 0, 0),
        (19, "deadline_at", "TEXT", 0, None, 0, 0),
        (20, "created_at", "TEXT", 1, None, 0, 0),
        (21, "updated_at", "TEXT", 1, None, 0, 0),
        (22, "started_at", "TEXT", 0, None, 0, 0),
        (23, "completed_at", "TEXT", 0, None, 0, 0),
    ),
    "mission_tasks": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "mission_id", "TEXT", 1, None, 0, 0),
        (2, "user_id", "TEXT", 1, None, 0, 0),
        (3, "seq", "INTEGER", 1, None, 0, 0),
        (4, "kind", "TEXT", 1, "'gather'", 0, 0),
        (5, "title", "TEXT", 1, "''", 0, 0),
        (6, "instruction", "TEXT", 1, None, 0, 0),
        (7, "depends_on_json", "TEXT", 1, "'[]'", 0, 0),
        (8, "status", "TEXT", 1, "'pending'", 0, 0),
        (9, "attempts", "INTEGER", 1, "0", 0, 0),
        (10, "side_effect", "INTEGER", 1, "0", 0, 0),
        (11, "checkpoint_json", "TEXT", 1, "'{}'", 0, 0),
        (12, "compensation", "TEXT", 1, "''", 0, 0),
        (13, "result", "TEXT", 1, "''", 0, 0),
        (14, "inbox_id", "TEXT", 0, None, 0, 0),
        (15, "tools_used_json", "TEXT", 1, "'[]'", 0, 0),
        (16, "error", "TEXT", 1, "''", 0, 0),
        (17, "created_at", "TEXT", 1, None, 0, 0),
        (18, "updated_at", "TEXT", 1, None, 0, 0),
        (19, "started_at", "TEXT", 0, None, 0, 0),
        (20, "completed_at", "TEXT", 0, None, 0, 0),
    ),
    "outbound_notifications": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "user_id", "TEXT", 1, None, 0, 0),
        (2, "chat_id", "TEXT", 1, None, 0, 0),
        (3, "kind", "TEXT", 1, "''", 0, 0),
        (4, "dedup_key", "TEXT", 1, "''", 0, 0),
        (5, "body", "TEXT", 1, None, 0, 0),
        (6, "status", "TEXT", 1, "'pending'", 0, 0),
        (7, "attempts", "INTEGER", 1, "0", 0, 0),
        (8, "created_at", "TEXT", 1, None, 0, 0),
        (9, "sent_at", "TEXT", 0, None, 0, 0),
    ),
}

# Schemas 13--23 reached schema 50 by appending the mission budget columns.
# SQLite deliberately preserves that column order, while fresh schema 24+
# databases have the same closed fields in their declaration order above.
_MIGRATED_MISSIONS_XINFO = (
    *_TABLE_XINFO["missions"][:13],
    (13, "created_at", "TEXT", 1, None, 0, 0),
    (14, "updated_at", "TEXT", 1, None, 0, 0),
    (15, "started_at", "TEXT", 0, None, 0, 0),
    (16, "completed_at", "TEXT", 0, None, 0, 0),
    (17, "budget_seconds", "INTEGER", 1, "0", 0, 0),
    (18, "budget_tool_calls", "INTEGER", 1, "0", 0, 0),
    (19, "budget_retries", "INTEGER", 1, "0", 0, 0),
    (20, "spent_seconds", "INTEGER", 1, "0", 0, 0),
    (21, "spent_tool_calls", "INTEGER", 1, "0", 0, 0),
    (22, "spent_retries", "INTEGER", 1, "0", 0, 0),
    (23, "deadline_at", "TEXT", 0, None, 0, 0),
)
_TABLE_XINFO_LAYOUTS = {
    name: ((rows, _MIGRATED_MISSIONS_XINFO) if name == "missions" else (rows,))
    for name, rows in _TABLE_XINFO.items()
}

_FOREIGN_KEYS: dict[str, frozenset[tuple[str, str, str, str, str, str]]] = {
    "schema_meta": frozenset(),
    "runtime_kv": frozenset(),
    "missions": frozenset({("users", "user_id", "id", "NO ACTION", "NO ACTION", "NONE")}),
    "mission_tasks": frozenset(
        {
            ("users", "user_id", "id", "NO ACTION", "NO ACTION", "NONE"),
            ("missions", "mission_id", "id", "NO ACTION", "NO ACTION", "NONE"),
        }
    ),
    "outbound_notifications": frozenset({("users", "user_id", "id", "NO ACTION", "NO ACTION", "NONE")}),
}

_DEDUP_INDEX_SQL = (
    "CREATE UNIQUE INDEX uq_outbound_dedup "
    "ON outbound_notifications(chat_id, dedup_key) WHERE dedup_key <> ''"
)
_REQUIRED_TABLE_SQL_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "schema_meta": (),
    "runtime_kv": (),
    "missions": (
        "check(statusin('proposed','ready','running','paused','blocked','completed','failed','cancelled'))",
        "check(originin('user','agent','worker'))",
    ),
    "mission_tasks": (
        "check(kindin('gather','produce'))",
        "check(statusin('pending','running','done','failed','skipped','uncertain','compensated'))",
        "unique(mission_id,seq)",
    ),
    "outbound_notifications": (),
}
_EXPECTED_TABLE_CHECK_COUNTS = {
    "schema_meta": 0,
    "runtime_kv": 0,
    "missions": 2,
    "mission_tasks": 2,
    "outbound_notifications": 0,
}
_EXPECTED_TABLE_SQL_COUNTS = {
    "schema_meta": {"primarykey": 1, "unique(": 0, "references": 0},
    "runtime_kv": {"primarykey": 1, "unique(": 0, "references": 0},
    "missions": {"primarykey": 1, "unique(": 0, "references": 1},
    "mission_tasks": {"primarykey": 1, "unique(": 1, "references": 2},
    "outbound_notifications": {"primarykey": 1, "unique(": 0, "references": 1},
}
_FORBIDDEN_TABLE_SQL_FRAGMENTS = (
    "autoincrement",
    "collate",
    "constraint",
    "deferrable",
    "foreignkey(",
    "generated",
    "match",
    "onconflict",
    "strict",
    "withoutrowid",
)
_PROTECTED_TRIGGER_SHA256 = {
    "relation_history_floor_immutable_update": (
        "schema_meta",
        "76086473e51c08ecf7a066d0855dccc30472b194dea903acb9917e62164165f4",
    ),
    "relation_history_floor_immutable_delete": (
        "schema_meta",
        "2be6ca1d13d394e9f01ef5c6f224f22d597ec638dda98b81307b50b5b0ed0545",
    ),
    "relation_history_floor_immutable_insert": (
        "schema_meta",
        "80c9c54a0300f5070812fb2d5574d3921646f369e686d986c92223d0daf5fa3c",
    ),
}
_EXPECTED_UNIQUE_INDEXES = {
    "schema_meta": (("pk", 0, ("key",), ""),),
    "runtime_kv": (("pk", 0, ("key",), ""),),
    "missions": (("pk", 0, ("id",), ""),),
    "mission_tasks": (
        ("pk", 0, ("id",), ""),
        ("u", 0, ("mission_id", "seq"), ""),
    ),
    "outbound_notifications": (
        ("pk", 0, ("id",), ""),
        (
            "c",
            1,
            ("chat_id", "dedup_key"),
            "createuniqueindexuq_outbound_deduponoutbound_notifications(chat_id,dedup_key)wherededup_key<>''",
        ),
    ),
}
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\r\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


class ProductionObservationError(RuntimeError):
    """The live state cannot support a closed read-only observation."""


def _nonnegative(value: object, *, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SQL_COUNT:
        raise ProductionObservationError(f"{label} must be a bounded non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class MissionStatusCounts:
    proposed: int
    ready: int
    running: int
    paused: int
    blocked: int
    completed: int
    failed: int
    cancelled: int

    def __post_init__(self) -> None:
        for name in _MISSION_STATES:
            _nonnegative(getattr(self, name), label=f"missions.{name}")

    def to_payload(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _MISSION_STATES}


@dataclass(frozen=True, slots=True)
class MissionTaskStatusCounts:
    pending: int
    running: int
    done: int
    failed: int
    skipped: int
    uncertain: int
    compensated: int

    def __post_init__(self) -> None:
        for name in _TASK_STATES:
            _nonnegative(getattr(self, name), label=f"mission_tasks.{name}")

    def to_payload(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _TASK_STATES}


@dataclass(frozen=True, slots=True)
class ReminderStatusCounts:
    pending: int
    uncertain: int
    sent: int
    failed: int
    dismissed: int

    def __post_init__(self) -> None:
        for name in _REMINDER_STATES:
            _nonnegative(getattr(self, name), label=f"reminders.{name}")

    def to_payload(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _REMINDER_STATES}


@dataclass(frozen=True, slots=True)
class WorkerHealthCounts:
    present: int
    missing: int
    scheduled: int
    running: int
    ok: int
    error: int
    timeout: int
    skipped: int
    unknown: int

    def __post_init__(self) -> None:
        for name in ("present", "missing", *_WORKER_STATES):
            _nonnegative(getattr(self, name), label=f"workers.{name}")
        if self.present + self.missing != len(_WORKER_KEYS):
            raise ProductionObservationError("worker health cardinality is not closed")
        if sum(getattr(self, state) for state in _WORKER_STATES) != self.present:
            raise ProductionObservationError("worker health states do not match present records")

    def to_payload(self) -> dict[str, object]:
        return {
            "present": self.present,
            "missing": self.missing,
            "health_states": {state: getattr(self, state) for state in _WORKER_STATES},
        }


@dataclass(frozen=True, slots=True)
class ScheduledWorkObservation:
    missions: MissionStatusCounts
    mission_tasks: MissionTaskStatusCounts
    reminders: ReminderStatusCounts
    workers: WorkerHealthCounts

    def __post_init__(self) -> None:
        if type(self.missions) is not MissionStatusCounts:
            raise ProductionObservationError("mission counts contract is invalid")
        if type(self.mission_tasks) is not MissionTaskStatusCounts:
            raise ProductionObservationError("mission task counts contract is invalid")
        if type(self.reminders) is not ReminderStatusCounts:
            raise ProductionObservationError("reminder counts contract is invalid")
        if type(self.workers) is not WorkerHealthCounts:
            raise ProductionObservationError("worker health counts contract is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "missions": self.missions.to_payload(),
            "mission_tasks": self.mission_tasks.to_payload(),
            "reminders": self.reminders.to_payload(),
            "workers": self.workers.to_payload(),
        }


def _schema_attestation_sha256() -> str:
    payload = {
        "table_xinfo_layouts": {
            name: [[list(row) for row in layout] for layout in layouts]
            for name, layouts in sorted(_TABLE_XINFO_LAYOUTS.items())
        },
        "foreign_keys": {
            name: [list(row) for row in sorted(rows)] for name, rows in sorted(_FOREIGN_KEYS.items())
        },
        "mission_sequence": ["mission_id", "seq"],
        "notification_dedup": _DEDUP_INDEX_SQL,
        "required_table_sql_fragments": _REQUIRED_TABLE_SQL_FRAGMENTS,
        "expected_table_check_counts": _EXPECTED_TABLE_CHECK_COUNTS,
        "expected_table_sql_counts": _EXPECTED_TABLE_SQL_COUNTS,
        "forbidden_table_sql_fragments": _FORBIDDEN_TABLE_SQL_FRAGMENTS,
        "protected_temp_objects": "absent",
        "protected_trigger_sha256": _PROTECTED_TRIGGER_SHA256,
        "protected_unique_indexes": _EXPECTED_UNIQUE_INDEXES,
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256 = _schema_attestation_sha256()


@dataclass(frozen=True, slots=True)
class ProductionReadOnlyObservation:
    challenge_sha256: str
    backend_process_epoch_sha256: str
    schema_attestation_sha256: str
    scheduled_work: ScheduledWorkObservation
    schema_version: int = _SCHEMA_VERSION
    database_integrity: str = "ok"
    foreign_key_violations: int = 0
    backend_lease_owned: bool = True
    hard_contradictions: int = 0
    schema: str = PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        _digest(self.challenge_sha256, label="challenge_sha256")
        _digest(self.backend_process_epoch_sha256, label="backend_process_epoch_sha256")
        if self.schema_attestation_sha256 != PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256:
            raise ProductionObservationError("schema attestation does not match the closed release")
        if type(self.scheduled_work) is not ScheduledWorkObservation:
            raise ProductionObservationError("scheduled work observation is invalid")
        if (
            self.schema != PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA
            or type(self.schema_version) is not int
            or self.schema_version != _SCHEMA_VERSION
            or type(self.database_integrity) is not str
            or self.database_integrity != "ok"
            or type(self.foreign_key_violations) is not int
            or self.foreign_key_violations != 0
            or self.backend_lease_owned is not True
            or type(self.hard_contradictions) is not int
            or self.hard_contradictions != 0
        ):
            raise ProductionObservationError("production observation claims an unsuccessful state")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "challenge_sha256": self.challenge_sha256,
            "backend_process_epoch_sha256": self.backend_process_epoch_sha256,
            "backend_lease_owned": self.backend_lease_owned,
            "database": {
                "schema_version": self.schema_version,
                "schema_attestation_sha256": self.schema_attestation_sha256,
                "integrity": self.database_integrity,
                "foreign_key_violations": self.foreign_key_violations,
            },
            "scheduled_work": self.scheduled_work.to_payload(),
            "hard_contradictions": self.hard_contradictions,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None or set(value) == {"0"}:
        raise ProductionObservationError(f"{label} must be an exact lowercase SHA-256 digest")
    return value


def _normalized_sql(value: object) -> str:
    text = _SQL_BLOCK_COMMENT_RE.sub("", str(value or ""))
    text = _SQL_LINE_COMMENT_RE.sub("", text)
    return "".join(text.casefold().split()).replace("ifnotexists", "")


def _normalized_table_sql(name: str, value: object) -> str:
    normalized = _normalized_sql(value)
    return normalized.replace(f'"{name}"', name).replace(f"`{name}`", name).replace(f"[{name}]", name)


def _require_open_connection(settings: FridaySettings, storage: FridayStorage) -> sqlite3.Connection:
    if type(settings) is not FridaySettings or type(storage) is not FridayStorage:
        raise ProductionObservationError("collector requires exact Friday settings and storage")
    if storage.settings is not settings:
        raise ProductionObservationError("settings are not bound to this storage owner")
    local = storage._local  # noqa: SLF001 - proving that observation cannot lazily open SQLite
    conn = getattr(local, "conn", None)
    if type(conn) is not sqlite3.Connection or getattr(local, "generation", None) != storage._generation:  # noqa: SLF001
        raise ProductionObservationError("collector requires an already-open storage connection")
    if conn.in_transaction:
        raise ProductionObservationError("collector requires a committed storage boundary")
    return conn


def _require_database_binding(conn: sqlite3.Connection, settings: FridaySettings) -> None:
    rows = tuple(tuple(row) for row in conn.execute("PRAGMA database_list"))
    names = {str(row[1]) for row in rows}
    main = tuple(row for row in rows if str(row[1]) == "main")
    if len(main) != 1 or not names.issubset({"main", "temp"}):
        raise ProductionObservationError("storage connection has an unexpected database binding")
    try:
        observed = Path(str(main[0][2])).resolve(strict=True)
        configured = settings.database_path.resolve(strict=True)
    except OSError as exc:
        raise ProductionObservationError("configured database binding is unavailable") from exc
    if observed != configured:
        raise ProductionObservationError("storage connection is not the configured database")


def _require_protected_schema_sql(conn: sqlite3.Connection) -> None:
    protected = tuple(_TABLE_XINFO)
    placeholders = ",".join("?" for _ in protected)
    temp = conn.execute(
        f"""SELECT 1 FROM sqlite_temp_master
             WHERE name IN ({placeholders})
                OR (type='trigger' AND tbl_name IN ({placeholders}))
             LIMIT 1""",  # nosec B608
        (*protected, *protected),
    ).fetchone()
    if temp is not None:
        raise ProductionObservationError("protected scheduled-work name is shadowed in temp")
    trigger_rows = conn.execute(
        f"""SELECT name, tbl_name, sql FROM main.sqlite_schema
             WHERE type='trigger' AND tbl_name IN ({placeholders})""",  # nosec B608
        protected,
    ).fetchall()
    triggers = {
        str(row[0]): (str(row[1]), hashlib.sha256(str(row[2] or "").encode("utf-8")).hexdigest())
        for row in trigger_rows
    }
    if triggers != _PROTECTED_TRIGGER_SHA256:
        raise ProductionObservationError("protected scheduled-work trigger surface has drifted")
    rows = conn.execute(
        f"""SELECT name, sql FROM main.sqlite_schema
             WHERE type='table' AND name IN ({placeholders})""",  # nosec B608
        protected,
    ).fetchall()
    observed = {str(row[0]): _normalized_table_sql(str(row[0]), row[1]) for row in rows}
    if set(observed) != set(protected):
        raise ProductionObservationError("required scheduled-work table SQL is unavailable")
    for table, required in _REQUIRED_TABLE_SQL_FRAGMENTS.items():
        sql = observed[table]
        if (
            not sql.startswith(f"createtable{table}(")
            or not sql.endswith(")")
            or sql.count("check(") != _EXPECTED_TABLE_CHECK_COUNTS[table]
            or any(
                sql.count(fragment) != count for fragment, count in _EXPECTED_TABLE_SQL_COUNTS[table].items()
            )
            or any(fragment not in sql for fragment in required)
            or any(fragment in sql for fragment in _FORBIDDEN_TABLE_SQL_FRAGMENTS)
        ):
            raise ProductionObservationError("required scheduled-work table SQL has drifted")


def _require_table_shapes(conn: sqlite3.Connection) -> None:
    for table, expected_layouts in _TABLE_XINFO_LAYOUTS.items():
        rows = tuple(tuple(row) for row in conn.execute(f'PRAGMA main.table_xinfo("{table}")'))
        if any(len(row) != 7 or row[6] != 0 for row in rows) or rows not in expected_layouts:
            raise ProductionObservationError("required scheduled-work table shape has drifted")


def _require_foreign_keys(conn: sqlite3.Connection) -> None:
    for table, expected in _FOREIGN_KEYS.items():
        observed = frozenset(
            (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]), str(row[7]))
            for row in conn.execute(f'PRAGMA main.foreign_key_list("{table}")')
        )
        if observed != expected:
            raise ProductionObservationError("required scheduled-work foreign keys have drifted")


def _index_columns(conn: sqlite3.Connection, name: str) -> tuple[str, ...]:
    return tuple(str(row[2]) for row in conn.execute(f'PRAGMA main.index_info("{name}")'))


def _require_indexes(conn: sqlite3.Connection) -> None:
    for table, expected_unique in _EXPECTED_UNIQUE_INDEXES.items():
        observed: set[tuple[str, int, tuple[str, ...], str]] = set()
        unique_count = 0
        for row in conn.execute(f'PRAGMA main.index_list("{table}")'):
            if int(row[2]) != 1:
                continue
            unique_count += 1
            name = str(row[1])
            origin = str(row[3])
            partial = int(row[4])
            sql_row = conn.execute(
                "SELECT sql FROM main.sqlite_schema WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            sql = "" if sql_row is None or sql_row[0] is None else _normalized_sql(sql_row[0])
            if origin != "c" and sql:
                raise ProductionObservationError("protected unique index SQL has drifted")
            observed.add((origin, partial, _index_columns(conn, name), sql))
        if unique_count != len(expected_unique) or observed != set(expected_unique):
            raise ProductionObservationError("protected unique index surface has drifted")


def _require_integrity(conn: sqlite3.Connection) -> None:
    integrity = tuple(str(row[0]) for row in conn.execute("PRAGMA main.integrity_check(1)"))
    if integrity != ("ok",):
        raise ProductionObservationError("database integrity check failed")
    if conn.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        raise ProductionObservationError("database foreign key check failed")


def _status_counts(
    conn: sqlite3.Connection,
    *,
    table: str,
    allowed: tuple[str, ...],
    where: str = "",
) -> dict[str, int]:
    # Table and predicate are selected only by code-owned call sites below.
    rows = conn.execute(
        f'SELECT status, COUNT(*) FROM main."{table}" {where} GROUP BY status ORDER BY status'  # nosec B608
    ).fetchall()
    counts = {status: 0 for status in allowed}
    for row in rows:
        status = str(row[0])
        if status not in counts or type(row[1]) is not int or row[1] < 0:
            raise ProductionObservationError("scheduled-work state vocabulary has drifted")
        counts[status] = int(row[1])
    return counts


def _worker_counts(conn: sqlite3.Connection) -> WorkerHealthCounts:
    placeholders = ",".join("?" for _ in _WORKER_KEYS)
    malformed = conn.execute(
        f"""SELECT 1 FROM main.runtime_kv
             WHERE key IN ({placeholders})
               AND CASE
                     WHEN length(CAST(value AS BLOB))>16384 THEN 1
                     WHEN NOT json_valid(value) THEN 1
                     WHEN json_type(value) IS NOT 'object' THEN 1
                     WHEN json_type(value,'$.status') IS NOT 'text' THEN 1
                     ELSE 0
                   END
             LIMIT 1""",  # nosec B608 - fixed placeholder cardinality
        _WORKER_KEYS,
    ).fetchone()
    if malformed is not None:
        raise ProductionObservationError("scheduled worker health record is malformed")
    duplicate = conn.execute(
        f"""SELECT 1
               FROM main.runtime_kv AS state, json_tree(state.value) AS member
              WHERE state.key IN ({placeholders}) AND member.key IS NOT NULL
              GROUP BY state.key, member.parent, CAST(member.key AS TEXT)
             HAVING COUNT(*)>1 LIMIT 1""",  # nosec B608 - fixed placeholder cardinality
        _WORKER_KEYS,
    ).fetchone()
    if duplicate is not None:
        raise ProductionObservationError("scheduled worker health record has duplicate JSON keys")
    rows = conn.execute(
        f"""SELECT json_extract(value,'$.status') AS status, COUNT(*)
               FROM main.runtime_kv WHERE key IN ({placeholders})
              GROUP BY status ORDER BY status""",  # nosec B608 - fixed placeholder cardinality
        _WORKER_KEYS,
    ).fetchall()
    counts = {state: 0 for state in _WORKER_STATES}
    present = 0
    for row in rows:
        state = str(row[0])
        count = row[1]
        if state not in counts or type(count) is not int or count < 0:
            raise ProductionObservationError("scheduled worker health state is not closed")
        counts[state] = int(count)
        present += int(count)
    if present > len(_WORKER_KEYS):
        raise ProductionObservationError("scheduled worker health cardinality is invalid")
    return WorkerHealthCounts(
        present=present,
        missing=len(_WORKER_KEYS) - present,
        **counts,
    )


def _require_no_hard_contradictions(conn: sqlite3.Connection) -> None:
    contradiction = conn.execute(
        """SELECT 1
             FROM main.mission_tasks AS task
             LEFT JOIN main.missions AS mission ON mission.id=task.mission_id
            WHERE mission.id IS NULL
               OR task.user_id<>mission.user_id
               OR (task.status='pending' AND (
                       task.side_effect<>0
                       OR trim(COALESCE(task.checkpoint_json,'')) NOT IN ('','{}')
                  ))
               OR (task.status='uncertain' AND task.side_effect=0
                   AND trim(COALESCE(task.checkpoint_json,'')) IN ('','{}'))
               OR (task.status='running' AND trim(COALESCE(task.started_at,''))='')
               OR task.attempts<0
               OR task.side_effect NOT IN (0,1)
               OR task.seq<0
            LIMIT 1"""
    ).fetchone()
    if contradiction is not None:
        raise ProductionObservationError("durable scheduled work contains a hard contradiction")
    malformed_reminder = conn.execute(
        """SELECT 1 FROM main.outbound_notifications
            WHERE kind='reminder'
              AND (status NOT IN ('pending','uncertain','sent','failed','dismissed')
                   OR attempts<0
                   OR (status IN ('pending','uncertain','dismissed') AND dedup_key='')
                   OR (status='sent' AND trim(COALESCE(sent_at,''))=''))
            LIMIT 1"""
    ).fetchone()
    if malformed_reminder is not None:
        raise ProductionObservationError("durable reminder state contains a hard contradiction")


def _collect(
    conn: sqlite3.Connection,
    settings: FridaySettings,
) -> ScheduledWorkObservation:
    _require_database_binding(conn, settings)
    _require_protected_schema_sql(conn)
    _require_table_shapes(conn)
    _require_foreign_keys(conn)
    _require_indexes(conn)
    marker = conn.execute("SELECT value FROM main.schema_meta WHERE key='schema_version'").fetchone()
    if marker is None or str(marker[0]) != str(_SCHEMA_VERSION):
        raise ProductionObservationError("database is not exact schema 50")
    _require_integrity(conn)
    _require_no_hard_contradictions(conn)
    missions = _status_counts(conn, table="missions", allowed=_MISSION_STATES)
    tasks = _status_counts(conn, table="mission_tasks", allowed=_TASK_STATES)
    reminders = _status_counts(
        conn,
        table="outbound_notifications",
        allowed=_REMINDER_STATES,
        where="WHERE kind='reminder'",
    )
    return ScheduledWorkObservation(
        missions=MissionStatusCounts(**missions),
        mission_tasks=MissionTaskStatusCounts(**tasks),
        reminders=ReminderStatusCounts(**reminders),
        workers=_worker_counts(conn),
    )


def collect_production_read_only_observation(
    settings: FridaySettings,
    storage: FridayStorage,
    *,
    challenge_sha256: str,
) -> ProductionReadOnlyObservation:
    """Observe exact scheduled-work aggregates without acquiring an effect owner.

    The challenge is issued and authenticated by the future runtime/operator
    adapter.  The release binding stays outside this projection.  This pure
    collector validates and binds the challenge but manufactures no release
    authority.
    """

    challenge = _digest(challenge_sha256, label="challenge_sha256")
    conn = _require_open_connection(settings, storage)
    lease_path = settings.state_dir / "backend.lock"
    if not process_owns_lease(lease_path, protocol=_BACKEND_LEASE_PROTOCOL):
        raise ProductionObservationError("collector requires this process to own the backend lease")
    try:
        process_epoch = secondary_product_process_epoch_sha256(os.getpid())
    except RuntimeError as exc:
        raise ProductionObservationError("backend process epoch is unavailable") from exc

    with read_only_storage_snapshot(storage) as snapshot:
        if snapshot is not conn:
            raise ProductionObservationError("collector connection binding changed")
        prior_row = conn.execute("PRAGMA query_only").fetchone()
        if prior_row is None or int(prior_row[0]) not in {0, 1}:
            raise ProductionObservationError("SQLite query-only state is invalid")
        prior_query_only = int(prior_row[0])
        conn.execute("PRAGMA query_only=ON")
        try:
            if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise ProductionObservationError("SQLite query-only boundary was not established")
            scheduled_work = _collect(conn, settings)
        finally:
            conn.execute(f"PRAGMA query_only={prior_query_only}")  # nosec B608 - prior bit

    if not process_owns_lease(lease_path, protocol=_BACKEND_LEASE_PROTOCOL):
        raise ProductionObservationError("backend lease ownership changed during observation")
    try:
        observed_process_epoch = secondary_product_process_epoch_sha256(os.getpid())
    except RuntimeError as exc:
        raise ProductionObservationError("backend process epoch is unavailable") from exc
    if observed_process_epoch != process_epoch:
        raise ProductionObservationError("backend process epoch changed during observation")
    return ProductionReadOnlyObservation(
        challenge_sha256=challenge,
        backend_process_epoch_sha256=process_epoch,
        schema_attestation_sha256=PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256,
        scheduled_work=scheduled_work,
    )


__all__ = [
    "PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA",
    "PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256",
    "ProductionObservationError",
    "ProductionReadOnlyObservation",
    "ScheduledWorkObservation",
    "collect_production_read_only_observation",
]
