"""Exact schema-37 projection for pre-commit interaction failures."""

from __future__ import annotations

import re
import sqlite3
from enum import StrEnum
from functools import lru_cache

from friday.interaction_control_plane.turn_trace import FailureReason, FailureStage

INTERACTION_FAILURE_SCHEMA_VERSION = 37


class FailureEntrypoint(StrEnum):
    API_CHAT = "api_chat"
    TELEGRAM_CHAT = "telegram_chat"
    REGENERATE = "regenerate"


class FailureRoute(StrEnum):
    ADMISSION = "admission"
    LEGACY = "legacy"
    FILE_READ = "file_read"
    ARCHIVE_READ = "archive_read"
    WEB_READ = "web_read"
    SMALL_TALK = "small_talk"
    ORDINARY_DIALOGUE = "ordinary_dialogue"
    EFFECT = "effect"
    UNKNOWN = "unknown"

    @classmethod
    def from_route_value(cls, value: object) -> FailureRoute:
        try:
            return cls(value) if isinstance(value, str) else cls.UNKNOWN
        except ValueError:
            return cls.UNKNOWN


_ENTRYPOINT_SQL = ", ".join(f"'{value.value}'" for value in FailureEntrypoint)
_ROUTE_SQL = ", ".join(f"'{value.value}'" for value in FailureRoute)
_FAILURE_STAGE_SQL = ", ".join(f"'{value.value}'" for value in FailureStage if value is not FailureStage.NONE)
_FAILURE_REASON_SQL = ", ".join(
    f"'{value.value}'" for value in FailureReason if value is not FailureReason.NONE
)

INTERACTION_FAILURE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS interaction_failure_traces (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT REFERENCES conversations(id),
    turn_digest TEXT NOT NULL,
    conversation_digest TEXT NOT NULL,
    entrypoint TEXT NOT NULL CHECK(entrypoint IN ({_ENTRYPOINT_SQL})),
    route TEXT NOT NULL CHECK(route IN ({_ROUTE_SQL})),
    failure_stage TEXT NOT NULL CHECK(failure_stage IN ({_FAILURE_STAGE_SQL})),
    failure_reason TEXT NOT NULL CHECK(failure_reason IN ({_FAILURE_REASON_SQL})),
    trace_json TEXT NOT NULL CHECK(
        length(trace_json)<=16384
        AND json_valid(trace_json)
        AND json_type(trace_json)='object'
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(user_id, turn_digest),
    CHECK(length(turn_digest)=64 AND turn_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(conversation_digest)=64 AND conversation_digest NOT GLOB '*[^0-9a-f]*')
);

CREATE INDEX IF NOT EXISTS idx_interaction_failure_user_created
    ON interaction_failure_traces(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_failure_conversation
    ON interaction_failure_traces(user_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_failure_expiry
    ON interaction_failure_traces(expires_at);
"""


def _normalize_schema_sql(value: str) -> str:
    """Ignore formatting while preserving every token in the authoritative DDL."""

    return re.sub(r"\s+", "", value)


@lru_cache(maxsize=1)
def _canonical_failure_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(INTERACTION_FAILURE_SCHEMA)
        return {
            (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
            for row in conn.execute(
                """SELECT type,name,sql FROM sqlite_master
                    WHERE sql IS NOT NULL
                      AND (name='interaction_failure_traces'
                           OR tbl_name='interaction_failure_traces')
                    ORDER BY type,name"""
            )
        }
    finally:
        conn.close()


def validate_interaction_failure_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
) -> None:
    """Fail closed when a schema-37 failure store is missing or weakened."""

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_failure_traces'"
    ).fetchone()
    if row is None:
        if required:
            raise sqlite3.DatabaseError("Schema 37 interaction failure store is missing")
        return
    expected_objects = _canonical_failure_schema_objects()
    installed_objects = {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND (name='interaction_failure_traces'
                       OR tbl_name='interaction_failure_traces')
                ORDER BY type,name"""
        )
    }
    if installed_objects != expected_objects:
        raise sqlite3.DatabaseError("Schema 37 interaction failure DDL is incomplete or altered")
    named_index_columns = {
        str(index_name): tuple(
            str(column[2]) for column in conn.execute(f'PRAGMA index_info("{index_name}")')
        )
        for (object_type, index_name) in expected_objects
        if object_type == "index"
    }
    if named_index_columns != {
        "idx_interaction_failure_user_created": ("user_id", "created_at", "id"),
        "idx_interaction_failure_conversation": ("user_id", "conversation_id", "created_at"),
        "idx_interaction_failure_expiry": ("expires_at",),
    }:
        raise sqlite3.DatabaseError("Schema 37 interaction failure indexes are invalid")
    columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(interaction_failure_traces)")
    }
    expected = {
        "id": ("TEXT", 0, 1),
        "user_id": ("TEXT", 1, 0),
        "conversation_id": ("TEXT", 0, 0),
        "turn_digest": ("TEXT", 1, 0),
        "conversation_digest": ("TEXT", 1, 0),
        "entrypoint": ("TEXT", 1, 0),
        "route": ("TEXT", 1, 0),
        "failure_stage": ("TEXT", 1, 0),
        "failure_reason": ("TEXT", 1, 0),
        "trace_json": ("TEXT", 1, 0),
        "created_at": ("TEXT", 1, 0),
        "expires_at": ("TEXT", 1, 0),
    }
    if columns != expected:
        raise sqlite3.DatabaseError("Schema 37 interaction failure store shape is invalid")
    foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]))
        for item in conn.execute("PRAGMA foreign_key_list(interaction_failure_traces)")
    }
    if foreign_keys != {
        ("user_id", "users", "id"),
        ("conversation_id", "conversations", "id"),
    }:
        raise sqlite3.DatabaseError("Schema 37 interaction failure ownership is invalid")
    unique_indexes = [
        item for item in conn.execute("PRAGMA index_list(interaction_failure_traces)") if int(item[2]) == 1
    ]
    unique_columns = {
        tuple(str(column[2]) for column in conn.execute(f'PRAGMA index_info("{item[1]}")'))
        for item in unique_indexes
    }
    if ("user_id", "turn_digest") not in unique_columns:
        raise sqlite3.DatabaseError("Schema 37 interaction failure uniqueness is invalid")


__all__ = [
    "FailureEntrypoint",
    "FailureRoute",
    "INTERACTION_FAILURE_SCHEMA",
    "INTERACTION_FAILURE_SCHEMA_VERSION",
    "validate_interaction_failure_schema",
]
