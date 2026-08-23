"""Exact schema-39 projection for the two bounded durable recall Work Items."""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache

from friday.interaction_control_plane.work_item_contract import (
    RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON,
    WORK_ITEM_ACTIVE_FRAME_MAX_BYTES,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
)

WORK_ITEM_SCHEMA_VERSION = 39
WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES = 4_096
WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES = 65_536
WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT = 8

_WORK_ITEM_TABLES = ("work_item_selected_evidence", "work_items")
_WORK_ITEM_INDEXES = (
    "idx_work_item_selected_evidence_origin_boundary",
    "idx_work_items_conversation_updated",
    "idx_work_items_expiry",
    "idx_work_items_owner_state_updated",
    "uq_work_items_active_conversation",
)
_SCHEMA_38_INDEXES = (
    "idx_work_items_conversation_updated",
    "idx_work_items_expiry",
    "idx_work_items_owner_state_updated",
    "uq_work_items_active_conversation",
)


def _sql_values(values: object) -> str:
    return ", ".join(f"'{value.value}'" for value in values)  # type: ignore[attr-defined]


_KIND_SQL = _sql_values(WorkKind)
_GOAL_SQL = _sql_values(WorkGoal)
_STATE_SQL = _sql_values(WorkState)
_PLAYBOOK_SQL = _sql_values(WorkPlaybook)
_COMPLETION_SQL = _sql_values(WorkCompletionContract)
_TRANSITION_SQL = _sql_values(WorkTransition)

# Retain the exact released projection so a marker-38 database is authenticated
# before its table is rebuilt. The old DDL must never be relaxed merely to make
# a migration succeed.
_WORK_ITEM_SCHEMA_38 = f"""
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ('recall_conversation')),
    goal TEXT NOT NULL CHECK(goal IN ('exact_current_conversation_recall')),
    state TEXT NOT NULL CHECK(state IN ('active', 'suspended', 'cancelled', 'expired')),
    playbook TEXT NOT NULL CHECK(playbook IN ('recall_conversation')),
    completion_contract TEXT NOT NULL CHECK(completion_contract IN ('accepted_exact_owned_message_window')),
    active_frame_json TEXT NOT NULL CHECK(
        typeof(active_frame_json)='text'
        AND length(CAST(active_frame_json AS BLOB))<={WORK_ITEM_ACTIVE_FRAME_MAX_BYTES}
        AND json_valid(active_frame_json)
        AND json_type(active_frame_json)='object'
    ),
    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    anchor_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    accepted_plan_sha256 TEXT NOT NULL,
    accepted_outcome_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
    transition TEXT NOT NULL CHECK(transition IN ('created', 'constraint_updated', 'suspended', 'cancelled', 'expired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK(length(id)=21 AND substr(id,1,5)='work_'
          AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_assistant_message_id)=20
          AND substr(anchor_assistant_message_id,1,4)='msg_'
          AND substr(anchor_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND anchor_assistant_message_id<>anchor_user_message_id),
    CHECK(length(accepted_plan_sha256)=64
          AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(accepted_outcome_sha256)=64
          AND accepted_outcome_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(created_at) BETWEEN 20 AND 64
          AND length(updated_at) BETWEEN 20 AND 64
          AND length(expires_at) BETWEEN 20 AND 64
          AND (closed_at IS NULL OR length(closed_at) BETWEEN 20 AND 64)),
    CHECK(updated_at>=created_at),
    CHECK(unixepoch(updated_at) IS NOT NULL
          AND unixepoch(expires_at) IS NOT NULL
          AND unixepoch(expires_at)-unixepoch(updated_at)<={WORK_ITEM_TTL_HOURS * 60 * 60}),
    CHECK((state IN ('active','suspended') AND closed_at IS NULL)
          OR (state IN ('cancelled','expired') AND closed_at=updated_at)),
    CHECK((state IN ('active','suspended') AND expires_at>updated_at)
          OR state='cancelled'
          OR (state='expired' AND expires_at<=updated_at)),
    CHECK((state='active' AND transition IN ('created','constraint_updated'))
          OR (state='suspended' AND transition='suspended')
          OR (state='cancelled' AND transition='cancelled')
          OR (state='expired' AND transition='expired')),
    CHECK((transition='created' AND revision=1)
          OR (transition<>'created' AND revision>=2))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_items_active_conversation
    ON work_items(user_id, conversation_id) WHERE state='active';
CREATE INDEX IF NOT EXISTS idx_work_items_owner_state_updated
    ON work_items(user_id, state, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_conversation_updated
    ON work_items(user_id, conversation_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_expiry
    ON work_items(state, expires_at);
"""

WORK_ITEM_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ({_KIND_SQL})),
    goal TEXT NOT NULL CHECK(goal IN ({_GOAL_SQL})),
    state TEXT NOT NULL CHECK(state IN ({_STATE_SQL})),
    playbook TEXT NOT NULL CHECK(playbook IN ({_PLAYBOOK_SQL})),
    completion_contract TEXT NOT NULL CHECK(completion_contract IN ({_COMPLETION_SQL})),
    active_frame_json TEXT NOT NULL CHECK(
        typeof(active_frame_json)='text'
        AND length(CAST(active_frame_json AS BLOB))<={WORK_ITEM_ACTIVE_FRAME_MAX_BYTES}
        AND json_valid(active_frame_json)
        AND json_type(active_frame_json)='object'
    ),
    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    anchor_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    accepted_plan_sha256 TEXT NOT NULL,
    accepted_outcome_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
    transition TEXT NOT NULL CHECK(transition IN ({_TRANSITION_SQL})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK(length(id)=21 AND substr(id,1,5)='work_'
          AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_assistant_message_id)=20
          AND substr(anchor_assistant_message_id,1,4)='msg_'
          AND substr(anchor_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND anchor_assistant_message_id<>anchor_user_message_id),
    CHECK(length(accepted_plan_sha256)=64
          AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(accepted_outcome_sha256)=64
          AND accepted_outcome_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(created_at) BETWEEN 20 AND 64
          AND length(updated_at) BETWEEN 20 AND 64
          AND length(expires_at) BETWEEN 20 AND 64
          AND (closed_at IS NULL OR length(closed_at) BETWEEN 20 AND 64)),
    CHECK(updated_at>=created_at),
    CHECK(unixepoch(updated_at) IS NOT NULL
          AND unixepoch(expires_at) IS NOT NULL
          AND unixepoch(expires_at)-unixepoch(updated_at)<={WORK_ITEM_TTL_HOURS * 60 * 60}),
    CHECK((state IN ('active','suspended') AND closed_at IS NULL)
          OR (state IN ('cancelled','expired') AND closed_at=updated_at)),
    CHECK((state IN ('active','suspended') AND expires_at>updated_at)
          OR state='cancelled'
          OR (state='expired' AND expires_at<=updated_at)),
    CHECK((state='active' AND transition IN ('created','constraint_updated','evidence_replayed'))
          OR (state='suspended' AND transition='suspended')
          OR (state='cancelled' AND transition='cancelled')
          OR (state='expired' AND transition='expired')),
    CHECK((transition='created' AND revision=1)
          OR (transition<>'created' AND revision>=2)),
    CHECK(
        (kind='recall_conversation'
         AND goal='exact_current_conversation_recall'
         AND playbook='recall_conversation'
         AND completion_contract='accepted_exact_owned_message_window'
         AND transition<>'evidence_replayed')
        OR
        (kind='recall_selected_archive_evidence'
         AND goal='exact_selected_archive_evidence_recall'
         AND playbook='recall_selected_archive_evidence'
         AND completion_contract='accepted_exact_selected_archive_evidence'
         AND transition<>'constraint_updated')
    ),
    CHECK(kind<>'recall_selected_archive_evidence'
          OR active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_items_active_conversation
    ON work_items(user_id, conversation_id) WHERE state='active';
CREATE INDEX IF NOT EXISTS idx_work_items_owner_state_updated
    ON work_items(user_id, state, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_conversation_updated
    ON work_items(user_id, conversation_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_expiry
    ON work_items(state, expires_at);

CREATE TABLE IF NOT EXISTS work_item_selected_evidence (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    corpus TEXT NOT NULL CHECK(corpus IN ('documents','knowledge','messages')),
    source_ref_json TEXT NOT NULL CHECK(
        typeof(source_ref_json)='text'
        AND length(CAST(source_ref_json AS BLOB)) BETWEEN 2 AND {WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES}
        AND json_valid(source_ref_json)
        AND json_type(source_ref_json)='object'
    ),
    passage_refs_json TEXT NOT NULL CHECK(
        typeof(passage_refs_json)='text'
        AND length(CAST(passage_refs_json AS BLOB)) BETWEEN 3 AND {WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES}
        AND json_valid(passage_refs_json)
        AND json_type(passage_refs_json)='array'
        AND json_array_length(passage_refs_json) BETWEEN 1 AND {WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT}
    ),
    source_snapshot_sha256 TEXT NOT NULL CHECK(
        length(source_snapshot_sha256)=64
        AND source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_sha256 TEXT NOT NULL CHECK(
        length(coverage_sha256)=64
        AND coverage_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_grade TEXT NOT NULL CHECK(coverage_grade IN ('complete','partial')),
    origin_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(origin_boundary_user_message_id)=20
          AND substr(origin_boundary_user_message_id,1,4)='msg_'
          AND substr(origin_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(
        (corpus='documents'
         AND json_extract(source_ref_json,'$.source_kind')='document')
        OR
        (corpus='knowledge'
         AND json_extract(source_ref_json,'$.source_kind')
             IN ('document','web_capture','generated_artifact'))
        OR
        (corpus='messages'
         AND json_extract(source_ref_json,'$.source_kind')='conversation')
    )
);

CREATE INDEX IF NOT EXISTS idx_work_item_selected_evidence_origin_boundary
    ON work_item_selected_evidence(origin_boundary_user_message_id, work_item_id);

CREATE TRIGGER IF NOT EXISTS trg_work_item_selected_evidence_scope_insert
BEFORE INSERT ON work_item_selected_evidence
WHEN NOT EXISTS (
        SELECT 1
          FROM work_items work
          JOIN messages boundary
            ON boundary.id=NEW.origin_boundary_user_message_id
           AND boundary.user_id=work.user_id
           AND boundary.conversation_id=work.conversation_id
           AND boundary.role='user'
         WHERE work.id=NEW.work_item_id
           AND work.kind='recall_selected_archive_evidence'
           AND work.active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}'
           AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.passage_refs_json) passage
         WHERE passage.type<>'object'
            OR json_extract(passage.value,'$.schema')<>'friday.passage-ref.private.v1'
            OR json(json_extract(passage.value,'$.source_ref'))<>json(NEW.source_ref_json)
    )
BEGIN
    SELECT RAISE(ABORT, 'selected archive evidence scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_selected_evidence_immutable
BEFORE UPDATE ON work_item_selected_evidence
BEGIN
    SELECT RAISE(ABORT, 'selected archive evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_workflow_identity_immutable
BEFORE UPDATE OF kind,goal,playbook,completion_contract ON work_items
WHEN NEW.kind<>OLD.kind
  OR NEW.goal<>OLD.goal
  OR NEW.playbook<>OLD.playbook
  OR NEW.completion_contract<>OLD.completion_contract
BEGIN
    SELECT RAISE(ABORT, 'work item workflow identity is immutable');
END;
"""


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _execute_schema(conn: sqlite3.Connection, schema: str) -> None:
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():  # pragma: no cover - constants are complete SQL
        raise sqlite3.DatabaseError("Work Item schema contains an incomplete statement")


def _schema_objects(conn: sqlite3.Connection, *, current: bool) -> dict[tuple[str, str], str]:
    tables = _WORK_ITEM_TABLES if current else ("work_items",)
    placeholders = ",".join("?" for _item in tables)
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            f"""SELECT type,name,sql FROM sqlite_master
                  WHERE sql IS NOT NULL
                    AND (name IN ({placeholders}) OR tbl_name IN ({placeholders}))
                  ORDER BY type,name""",  # nosec B608 - generated placeholders only
            (*tables, *tables),
        )
    }


@lru_cache(maxsize=1)
def _canonical_work_item_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, WORK_ITEM_SCHEMA)
        return _schema_objects(conn, current=True)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _canonical_schema_38_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, _WORK_ITEM_SCHEMA_38)
        return _schema_objects(conn, current=False)
    finally:
        conn.close()


def _related_schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    names = (*_WORK_ITEM_TABLES, *_WORK_ITEM_INDEXES)
    placeholders = ",".join("?" for _item in names)
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            f"""SELECT type,name,sql FROM sqlite_master
                  WHERE sql IS NOT NULL
                    AND (name IN ({placeholders}) OR tbl_name IN (?,?))
                  ORDER BY type,name""",  # nosec B608 - generated placeholders only
            (*names, *_WORK_ITEM_TABLES),
        )
    }


def _validate_current_data(conn: sqlite3.Connection) -> None:
    # Retrieval contracts import storage-backed package exports. Delay that
    # dependency until schema bootstrap has completed module initialization.
    from friday.interaction_control_plane.archive_evidence_work_item import (
        RecallSelectedArchiveEvidenceWorkItem,
    )
    from friday.interaction_control_plane.selected_archive_evidence import (
        SelectedArchiveEvidence,
        SelectedArchiveEvidenceError,
    )
    from friday.interaction_control_plane.work_item_contract import WorkItemContractError

    mismatch = conn.execute(
        """SELECT 1
             FROM work_items work
             LEFT JOIN work_item_selected_evidence evidence
               ON evidence.work_item_id=work.id
            WHERE (work.kind='recall_selected_archive_evidence' AND evidence.work_item_id IS NULL)
               OR (work.kind<>'recall_selected_archive_evidence' AND evidence.work_item_id IS NOT NULL)
            LIMIT 1"""
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.DatabaseError("Schema 39 selected evidence cardinality is invalid")
    cursor = conn.execute("SELECT * FROM work_item_selected_evidence ORDER BY work_item_id")
    columns = tuple(str(item[0]) for item in cursor.description or ())
    for raw in cursor.fetchall():
        row = dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(columns, raw, strict=True))
        try:
            evidence = SelectedArchiveEvidence.from_storage_row(row)
        except SelectedArchiveEvidenceError as exc:
            raise sqlite3.DatabaseError("Schema 39 selected evidence identity is invalid") from exc
        work_cursor = conn.execute("SELECT * FROM work_items WHERE id=?", (evidence.work_item_id,))
        raw_work = work_cursor.fetchone()
        if raw_work is None:  # pragma: no cover - the sidecar FK already proves this
            raise sqlite3.DatabaseError("Schema 39 selected evidence owner is missing")
        work_columns = tuple(str(item[0]) for item in work_cursor.description or ())
        work = (
            dict(raw_work)
            if isinstance(raw_work, sqlite3.Row)
            else dict(zip(work_columns, raw_work, strict=True))
        )
        if evidence.source_ref.principal_id != work.get("user_id"):
            raise sqlite3.DatabaseError("Schema 39 selected evidence owner is invalid")
        try:
            RecallSelectedArchiveEvidenceWorkItem.from_storage_rows(work, evidence)
        except WorkItemContractError as exc:
            raise sqlite3.DatabaseError("Schema 39 archive Work Item data is invalid") from exc
        boundary = conn.execute(
            """SELECT 1
                 FROM work_items work
                 JOIN messages message
                   ON message.id=?
                  AND message.user_id=work.user_id
                  AND message.conversation_id=work.conversation_id
                  AND message.role='user'
                WHERE work.id=? AND work.kind='recall_selected_archive_evidence'""",
            (evidence.origin_boundary_user_message_id, evidence.work_item_id),
        ).fetchone()
        if boundary is None:
            raise sqlite3.DatabaseError("Schema 39 selected evidence boundary is invalid")


def validate_work_item_schema(conn: sqlite3.Connection, *, required: bool = True) -> None:
    """Fail closed when the exact schema-39 Work Item projection is weakened."""

    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'").fetchone()
    if row is None:
        if required:
            raise sqlite3.DatabaseError("Schema 39 work item store is missing")
        if _related_schema_objects(conn):
            raise sqlite3.DatabaseError("Schema 39 work item DDL is incomplete or altered")
        return
    if _schema_objects(conn, current=True) != _canonical_work_item_schema_objects():
        raise sqlite3.DatabaseError("Schema 39 work item DDL is incomplete or altered")

    expected_index_columns = {
        "uq_work_items_active_conversation": ("user_id", "conversation_id"),
        "idx_work_items_owner_state_updated": ("user_id", "state", "updated_at", "id"),
        "idx_work_items_conversation_updated": ("user_id", "conversation_id", "updated_at", "id"),
        "idx_work_items_expiry": ("state", "expires_at"),
        "idx_work_item_selected_evidence_origin_boundary": (
            "origin_boundary_user_message_id",
            "work_item_id",
        ),
    }
    named_index_columns = {
        name: tuple(str(column[2]) for column in conn.execute(f'PRAGMA index_info("{name}")'))
        for name in expected_index_columns
    }
    if named_index_columns != expected_index_columns:
        raise sqlite3.DatabaseError("Schema 39 work item indexes are invalid")

    columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_items)")
    }
    if columns != {
        "id": ("TEXT", 0, 1),
        "user_id": ("TEXT", 1, 0),
        "conversation_id": ("TEXT", 1, 0),
        "kind": ("TEXT", 1, 0),
        "goal": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "playbook": ("TEXT", 1, 0),
        "completion_contract": ("TEXT", 1, 0),
        "active_frame_json": ("TEXT", 1, 0),
        "anchor_user_message_id": ("TEXT", 1, 0),
        "anchor_assistant_message_id": ("TEXT", 1, 0),
        "accepted_plan_sha256": ("TEXT", 1, 0),
        "accepted_outcome_sha256": ("TEXT", 1, 0),
        "revision": ("INTEGER", 1, 0),
        "transition": ("TEXT", 1, 0),
        "created_at": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
        "expires_at": ("TEXT", 1, 0),
        "closed_at": ("TEXT", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 39 work item store shape is invalid")
    evidence_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_selected_evidence)")
    }
    if evidence_columns != {
        "work_item_id": ("TEXT", 0, 1),
        "corpus": ("TEXT", 1, 0),
        "source_ref_json": ("TEXT", 1, 0),
        "passage_refs_json": ("TEXT", 1, 0),
        "source_snapshot_sha256": ("TEXT", 1, 0),
        "coverage_sha256": ("TEXT", 1, 0),
        "coverage_grade": ("TEXT", 1, 0),
        "origin_boundary_user_message_id": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError("Schema 39 selected evidence store shape is invalid")

    work_item_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_items)")
    }
    if work_item_foreign_keys != {
        ("user_id", "users", "id", "NO ACTION", "NO ACTION"),
        ("conversation_id", "conversations", "id", "NO ACTION", "NO ACTION"),
        ("anchor_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
        ("anchor_assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 39 work item ownership anchors are invalid")
    evidence_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_selected_evidence)")
    }
    if evidence_foreign_keys != {
        ("work_item_id", "work_items", "id", "NO ACTION", "CASCADE"),
        ("origin_boundary_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 39 selected evidence foreign keys are invalid")
    _validate_current_data(conn)


def upgrade_work_item_schema_38_to_39(
    conn: sqlite3.Connection,
    *,
    required: bool,
) -> None:
    """Authenticate and atomically rebuild only the exact released schema 38."""

    related = _related_schema_objects(conn)
    if not related:
        if required:
            raise sqlite3.DatabaseError("Schema 38 work item store is missing")
        return
    if _schema_objects(conn, current=True) == _canonical_work_item_schema_objects():
        validate_work_item_schema(conn)
        return
    installed_legacy = _schema_objects(conn, current=False)
    if installed_legacy != _canonical_schema_38_objects() or any(
        name.startswith("work_item_selected_evidence") for _kind, name in related
    ):
        raise sqlite3.DatabaseError("Schema 38 work item DDL is incomplete or altered")
    conn.execute("ALTER TABLE work_items RENAME TO work_items_schema38")
    for index in _SCHEMA_38_INDEXES:
        conn.execute(f'DROP INDEX "{index}"')
    _execute_schema(conn, WORK_ITEM_SCHEMA)
    conn.execute(
        """INSERT INTO work_items(
               id,user_id,conversation_id,kind,goal,state,playbook,
               completion_contract,active_frame_json,anchor_user_message_id,
               anchor_assistant_message_id,accepted_plan_sha256,
               accepted_outcome_sha256,revision,transition,created_at,
               updated_at,expires_at,closed_at
           )
           SELECT id,user_id,conversation_id,kind,goal,state,playbook,
                  completion_contract,active_frame_json,anchor_user_message_id,
                  anchor_assistant_message_id,accepted_plan_sha256,
                  accepted_outcome_sha256,revision,transition,created_at,
                  updated_at,expires_at,closed_at
             FROM work_items_schema38"""
    )
    conn.execute("DROP TABLE work_items_schema38")
    validate_work_item_schema(conn)


__all__ = [
    "WORK_ITEM_SCHEMA",
    "WORK_ITEM_SCHEMA_VERSION",
    "upgrade_work_item_schema_38_to_39",
    "validate_work_item_schema",
]
