"""Schema-37 fallback contract for the private pre-commit failure store."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from friday.interaction_control_plane.failure_schema import INTERACTION_FAILURE_SCHEMA
from friday.storage import SCHEMA_VERSION, FridayStorage


def test_schema_37_installs_a_separate_exact_failure_store(storage) -> None:
    assert SCHEMA_VERSION == 39
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "39"
    objects = {
        (str(row[0]), str(row[1]))
        for row in storage.execute(
            """SELECT type,name FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND (name='interaction_failure_traces'
                       OR tbl_name='interaction_failure_traces')"""
        )
    }
    assert objects == {
        ("table", "interaction_failure_traces"),
        ("index", "idx_interaction_failure_user_created"),
        ("index", "idx_interaction_failure_conversation"),
        ("index", "idx_interaction_failure_expiry"),
    }


def test_schema_37_reopens_without_treating_37_as_an_obsidian_schema(settings, tmp_path) -> None:
    database = tmp_path / "schema-37-reopen.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    first.execute("SELECT 1")
    first.close()

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            reopened.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "39"
        )
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def _replace_failure_schema(database, *, table_sql: str, index_sql: list[str]) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE interaction_failure_traces")
        conn.execute(table_sql)
        for statement in index_sql:
            conn.execute(statement)


def test_current_schema_rejects_a_failure_table_without_owner_uniqueness(settings, tmp_path) -> None:
    database = tmp_path / "counterfeit-failure-store.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    statements = [part.strip() for part in INTERACTION_FAILURE_SCHEMA.split(";") if part.strip()]
    weakened = statements[0].replace("    UNIQUE(user_id, turn_digest),\n", "")
    _replace_failure_schema(database, table_sql=weakened, index_sql=statements[1:])

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="failure DDL"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()


def test_current_schema_rejects_a_named_index_with_counterfeit_columns(settings, tmp_path) -> None:
    database = tmp_path / "counterfeit-failure-index.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    statements = [part.strip() for part in INTERACTION_FAILURE_SCHEMA.split(";") if part.strip()]
    counterfeit_indexes = [
        statement.replace(
            "ON interaction_failure_traces(user_id, created_at DESC, id DESC)",
            "ON interaction_failure_traces(user_id, id DESC)",
        )
        if "idx_interaction_failure_user_created" in statement
        else statement
        for statement in statements[1:]
    ]
    _replace_failure_schema(
        database,
        table_sql=statements[0],
        index_sql=counterfeit_indexes,
    )

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="failure DDL"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()
