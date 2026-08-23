"""Schema-39 migration and fail-closed DDL contract for durable Work Items."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.interaction_control_plane.work_item_contract import RecallConversationActiveFrame
from friday.interaction_control_plane.work_item_schema import _WORK_ITEM_SCHEMA_38
from friday.storage import SCHEMA_VERSION, FridayStorage

SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


def _schema_37_copy(tmp_path: Path) -> Path:
    database = tmp_path / "schema-37.sqlite3"
    with gzip.open(SCHEMA_FIXTURES / "schema-37.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def _install_released_work_item_schema_38(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE work_item_selected_evidence")
    conn.execute("DROP TRIGGER trg_work_items_workflow_identity_immutable")
    for index in (
        "uq_work_items_active_conversation",
        "idx_work_items_owner_state_updated",
        "idx_work_items_conversation_updated",
        "idx_work_items_expiry",
    ):
        conn.execute(f'DROP INDEX "{index}"')
    conn.execute("DROP TABLE work_items")
    conn.executescript(_WORK_ITEM_SCHEMA_38)
    conn.execute("UPDATE schema_meta SET value='38' WHERE key='schema_version'")


def test_schema_39_installs_the_exact_work_item_projection(storage) -> None:
    assert SCHEMA_VERSION == 39
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "39"
    objects = {
        (str(row[0]), str(row[1]))
        for row in storage.execute(
            """SELECT type,name FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND (name IN ('work_items','work_item_selected_evidence')
                       OR tbl_name IN ('work_items','work_item_selected_evidence'))"""
        )
    }
    assert objects == {
        ("table", "work_items"),
        ("index", "uq_work_items_active_conversation"),
        ("index", "idx_work_items_owner_state_updated"),
        ("index", "idx_work_items_conversation_updated"),
        ("index", "idx_work_items_expiry"),
        ("table", "work_item_selected_evidence"),
        ("index", "idx_work_item_selected_evidence_origin_boundary"),
        ("trigger", "trg_work_item_selected_evidence_scope_insert"),
        ("trigger", "trg_work_item_selected_evidence_immutable"),
        ("trigger", "trg_work_items_workflow_identity_immutable"),
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


def test_released_schema_37_migrates_to_39_without_losing_seed_data(settings, tmp_path) -> None:
    database = _schema_37_copy(tmp_path)
    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "39"
        )
        assert (
            migrated.execute("SELECT COUNT(*) FROM raw_objects WHERE user_id='fixture-owner'").fetchone()[0]
            == 3
        )
        assert migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_item_selected_evidence'"
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


def test_exact_interrupted_37_to_39_attempt_is_recoverable(settings, tmp_path) -> None:
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
            == "39"
        )
        assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        recovered.close()


def test_released_schema_38_rebuild_preserves_every_recall_row(settings, tmp_path) -> None:
    database = tmp_path / "released-schema-38.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    owner = "schema-38-owner"
    initial.ensure_user(owner, source="local")
    conversation = initial.create_conversation(owner, "Recall migration")
    boundary = initial.store_message(conversation["id"], owner, "user", "Что было вчера?")
    assistant = initial.store_message(
        conversation["id"],
        owner,
        "assistant",
        "Принятый ответ",
        reply_to=boundary["id"],
    )
    initial.close()
    frame = RecallConversationActiveFrame.create(
        timezone_name="Europe/Moscow",
        since_utc="2026-08-21T21:00:00+00:00",
        until_utc="2026-08-22T21:00:00+00:00",
    )
    row = (
        "work_0123456789abcdef",
        owner,
        conversation["id"],
        "recall_conversation",
        "exact_current_conversation_recall",
        "active",
        "recall_conversation",
        "accepted_exact_owned_message_window",
        frame.to_json(),
        boundary["id"],
        assistant["id"],
        "a" * 64,
        "b" * 64,
        1,
        "created",
        "2026-08-23T08:00:00+00:00",
        "2026-08-23T08:00:00+00:00",
        "2026-08-23T20:00:00+00:00",
        None,
    )
    with sqlite3.connect(database) as conn:
        _install_released_work_item_schema_38(conn)
        conn.execute(
            """INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )

    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert tuple(migrated.execute("SELECT * FROM work_items").fetchone()) == row
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "39"
        )
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_counterfeit_schema_38_is_rejected_before_rebuild(settings, tmp_path) -> None:
    database = tmp_path / "counterfeit-schema-38.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    with sqlite3.connect(database) as conn:
        _install_released_work_item_schema_38(conn)
        conn.execute("DROP INDEX idx_work_items_expiry")

    rejected = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 38 work item DDL"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()


@pytest.mark.parametrize("published_marker", ["37", "38", "39"])
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
