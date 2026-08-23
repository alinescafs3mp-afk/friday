"""Schema-38 migration and fail-closed DDL contract for durable Work Items."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.storage import SCHEMA_VERSION, FridayStorage

SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


def _schema_37_copy(tmp_path: Path) -> Path:
    database = tmp_path / "schema-37.sqlite3"
    with gzip.open(SCHEMA_FIXTURES / "schema-37.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def test_schema_38_installs_the_exact_work_item_projection(storage) -> None:
    assert SCHEMA_VERSION == 38
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "38"
    objects = {
        (str(row[0]), str(row[1]))
        for row in storage.execute(
            """SELECT type,name FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND (name='work_items' OR tbl_name='work_items')"""
        )
    }
    assert objects == {
        ("table", "work_items"),
        ("index", "uq_work_items_active_conversation"),
        ("index", "idx_work_items_owner_state_updated"),
        ("index", "idx_work_items_conversation_updated"),
        ("index", "idx_work_items_expiry"),
    }
    foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in storage.execute("PRAGMA foreign_key_list(work_items)")
    }
    assert foreign_keys == {
        ("user_id", "users", "id"),
        ("conversation_id", "conversations", "id"),
        ("anchor_user_message_id", "messages", "id"),
        ("anchor_assistant_message_id", "messages", "id"),
    }


def test_released_schema_37_migrates_to_38_without_losing_seed_data(settings, tmp_path) -> None:
    database = _schema_37_copy(tmp_path)
    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "38"
        )
        assert (
            migrated.execute("SELECT COUNT(*) FROM raw_objects WHERE user_id='fixture-owner'").fetchone()[0]
            == 3
        )
        assert migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'"
        ).fetchone()
        migrated_ddl = "".join(
            str(
                migrated.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_items'"
                ).fetchone()[0]
            ).split()
        )
        assert "unixepoch(expires_at)-unixepoch(updated_at)<=43200" in migrated_ddl
        assert "stateIN('cancelled','expired')ANDclosed_at=updated_at" in migrated_ddl
        assert "transition<>'created'ANDrevision>=2" in migrated_ddl
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_exact_interrupted_37_to_38_attempt_is_recoverable(settings, tmp_path) -> None:
    database = tmp_path / "interrupted-exact.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE schema_meta SET value='37' WHERE key='schema_version'")

    recovered = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            recovered.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "38"
        )
        assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        recovered.close()


@pytest.mark.parametrize("published_marker", ["37", "38"])
def test_missing_partial_unique_index_is_rejected_before_if_not_exists_can_hide_it(
    settings,
    tmp_path,
    published_marker: str,
) -> None:
    database = tmp_path / f"counterfeit-work-items-{published_marker}.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP INDEX uq_work_items_active_conversation")
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (published_marker,),
        )

    rejected = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="work item DDL"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()
