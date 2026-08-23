"""Exact schema-38 projection for the first durable Work Item slice."""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache

from friday.interaction_control_plane.work_item_contract import (
    WORK_ITEM_ACTIVE_FRAME_MAX_BYTES,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
)

WORK_ITEM_SCHEMA_VERSION = 38


def _sql_values(values: object) -> str:
    return ", ".join(f"'{value.value}'" for value in values)  # type: ignore[attr-defined]


_KIND_SQL = _sql_values(WorkKind)
_GOAL_SQL = _sql_values(WorkGoal)
_STATE_SQL = _sql_values(WorkState)
_PLAYBOOK_SQL = _sql_values(WorkPlaybook)
_COMPLETION_SQL = _sql_values(WorkCompletionContract)
_TRANSITION_SQL = _sql_values(WorkTransition)

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


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


@lru_cache(maxsize=1)
def _canonical_work_item_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(WORK_ITEM_SCHEMA)
        return {
            (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
            for row in conn.execute(
                """SELECT type,name,sql FROM sqlite_master
                    WHERE sql IS NOT NULL
                      AND (name='work_items' OR tbl_name='work_items')
                    ORDER BY type,name"""
            )
        }
    finally:
        conn.close()


def validate_work_item_schema(conn: sqlite3.Connection, *, required: bool = True) -> None:
    """Fail closed when the schema-38 Work Item projection is absent or weakened."""

    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'").fetchone()
    if row is None:
        if required:
            raise sqlite3.DatabaseError("Schema 38 work item store is missing")
        return
    expected_objects = _canonical_work_item_schema_objects()
    installed_objects = {
        (str(item[0]), str(item[1])): _normalize_schema_sql(str(item[2]))
        for item in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND (name='work_items' OR tbl_name='work_items')
                ORDER BY type,name"""
        )
    }
    if installed_objects != expected_objects:
        raise sqlite3.DatabaseError("Schema 38 work item DDL is incomplete or altered")

    named_index_columns = {
        str(index_name): tuple(
            str(column[2]) for column in conn.execute(f'PRAGMA index_info("{index_name}")')
        )
        for object_type, index_name in expected_objects
        if object_type == "index"
    }
    if named_index_columns != {
        "uq_work_items_active_conversation": ("user_id", "conversation_id"),
        "idx_work_items_owner_state_updated": ("user_id", "state", "updated_at", "id"),
        "idx_work_items_conversation_updated": (
            "user_id",
            "conversation_id",
            "updated_at",
            "id",
        ),
        "idx_work_items_expiry": ("state", "expires_at"),
    }:
        raise sqlite3.DatabaseError("Schema 38 work item indexes are invalid")

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
        raise sqlite3.DatabaseError("Schema 38 work item store shape is invalid")

    foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]))
        for item in conn.execute("PRAGMA foreign_key_list(work_items)")
    }
    if foreign_keys != {
        ("user_id", "users", "id"),
        ("conversation_id", "conversations", "id"),
        ("anchor_user_message_id", "messages", "id"),
        ("anchor_assistant_message_id", "messages", "id"),
    }:
        raise sqlite3.DatabaseError("Schema 38 work item ownership anchors are invalid")

    unique_indexes = [item for item in conn.execute("PRAGMA index_list(work_items)") if int(item[2]) == 1]
    unique_columns = {
        tuple(str(column[2]) for column in conn.execute(f'PRAGMA index_info("{item[1]}")'))
        for item in unique_indexes
    }
    if ("user_id", "conversation_id") not in unique_columns:
        raise sqlite3.DatabaseError("Schema 38 active work item uniqueness is invalid")


__all__ = [
    "WORK_ITEM_SCHEMA",
    "WORK_ITEM_SCHEMA_VERSION",
    "validate_work_item_schema",
]
