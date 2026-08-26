"""Schema 31 makes relation transaction-time evidence append-only and honest."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from friday.storage import SCHEMA_VERSION, FridayStorage, UnsupportedSchemaVersionError
from friday.storage.models import (
    Entity,
    EntityType,
    Relation,
    RelationType,
    normalize_known_at,
)

CAPTURE_TRIGGERS = {
    "relations_revision_ai",
    "relations_revision_au",
    "relations_revision_bd",
}
PROTECTION_TRIGGERS = {
    "relations_revision_identity_immutable",
    "relations_revision_insert_guard",
    "relations_revision_update_conflict_guard",
    "relation_revisions_append_only_update",
    "relation_revisions_append_only_delete",
    "relation_revisions_append_only_replace",
    "relation_revision_context_monotonic_update",
    "relation_revision_context_immutable_delete",
    "relation_revision_context_singleton_insert",
    "relation_history_floor_immutable_update",
    "relation_history_floor_immutable_delete",
    "relation_history_floor_immutable_insert",
}

RELATION_INSERT_OR_IGNORE_SQL = """INSERT OR IGNORE INTO relations(
    id, user_id, source_entity_id, target_entity_id, relation_type, weight,
    metadata_json, created_at, deleted_at, valid_from, valid_to,
    invalidated_at, superseded_by
) VALUES(
    :id, :user_id, :source_entity_id, :target_entity_id, :relation_type, :weight,
    :metadata_json, :created_at, :deleted_at, :valid_from, :valid_to,
    :invalidated_at, :superseded_by
)"""

RELATION_INSERT_OR_REPLACE_SQL = RELATION_INSERT_OR_IGNORE_SQL.replace(
    "INSERT OR IGNORE", "INSERT OR REPLACE", 1
)


def _seed_endpoints(storage: FridayStorage, user_id: str = "alice") -> tuple[str, str]:
    source_id = f"entity-{user_id}-source"
    target_id = f"entity-{user_id}-target"
    storage.create_entity(Entity(source_id, user_id, "Иван Иванов", EntityType.PERSON))
    storage.create_entity(Entity(target_id, user_id, "Проект Альфа", EntityType.PROJECT))
    return source_id, target_id


def _relation(
    relation_id: str,
    source_id: str,
    target_id: str,
    *,
    user_id: str = "alice",
    relation_type: RelationType = RelationType.MEMBER_OF,
) -> Relation:
    return Relation(
        id=relation_id,
        user_id=user_id,
        source_entity_id=source_id,
        target_entity_id=target_id,
        relation_type=relation_type,
        weight=0.75,
        metadata_json={"evidence": "рапорт"},
        created_at="1999-01-01T00:00:00+00:00",
        valid_from="2020-01-01",
    )


def _age_current_database_to_schema_30(database) -> None:
    """Remove only schema-31 artefacts from a database built by current code."""

    with sqlite3.connect(database) as conn:
        for trigger in sorted(CAPTURE_TRIGGERS | PROTECTION_TRIGGERS):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")  # nosec B608 - fixed test allowlist
        conn.execute("DROP TABLE relation_revisions")
        conn.execute("DROP TABLE relation_revision_context")
        conn.execute("DELETE FROM schema_meta WHERE key='relation_history_complete_from'")
        conn.execute("UPDATE schema_meta SET value='30' WHERE key IN ('schema_version', 'fts_build')")


def _revision_rows(storage: FridayStorage, relation_id: str) -> list[dict]:
    return [
        dict(row)
        for row in storage.execute(
            "SELECT * FROM relation_revisions WHERE relation_id=? ORDER BY event_seq",
            (relation_id,),
        ).fetchall()
    ]


def test_known_at_normalization_requires_a_real_offset_aware_instant() -> None:
    assert normalize_known_at("2024-03-05T12:34:56.12+03:00") == "2024-03-05T09:34:56.120000Z"
    assert normalize_known_at("2099-01-01T00:00:00Z", reject_future=False) == ("2099-01-01T00:00:00.000000Z")

    for invalid in (
        "2024-03-05",
        "2024-03-05T12:34:56",
        "2024-03-05 12:34:56+00:00",
        "not-a-timestamp",
        "2024-13-05T12:34:56Z",
    ):
        with pytest.raises(ValueError):
            normalize_known_at(invalid)

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        normalize_known_at(future)


def test_schema_32_installs_the_full_snapshot_context_indexes_and_guards(storage) -> None:
    assert SCHEMA_VERSION == 44
    marker = storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    assert marker and marker[0] == "44"

    columns = {row[1]: row[2] for row in storage.execute("PRAGMA table_info(relation_revisions)").fetchall()}
    assert tuple(columns) == (
        "event_seq",
        "relation_id",
        "revision",
        "present",
        "operation",
        "recorded_at",
        "batch_id",
        "history_quality",
        "user_id",
        "source_entity_id",
        "target_entity_id",
        "relation_type",
        "weight",
        "metadata_json",
        "created_at",
        "deleted_at",
        "valid_from",
        "valid_to",
        "invalidated_at",
        "superseded_by",
    )
    assert columns["event_seq"] == "INTEGER"
    assert columns["weight"] == "REAL"

    indexes = {
        row[0]
        for row in storage.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='relation_revisions'"
        ).fetchall()
    }
    assert {
        "idx_relation_revisions_user_time",
        "idx_relation_revisions_source_time",
        "idx_relation_revisions_target_time",
    } <= indexes
    triggers = {
        row[0] for row in storage.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    assert triggers >= CAPTURE_TRIGGERS | PROTECTION_TRIGGERS

    context = dict(storage.execute("SELECT * FROM relation_revision_context").fetchone())
    floor = storage.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()
    assert floor and normalize_known_at(str(floor[0]), reject_future=False) == floor[0]
    assert context == {
        "singleton": 1,
        "batch_id": "",
        "recorded_at": "",
        "observed_at": context["observed_at"],
    }
    assert normalize_known_at(context["observed_at"], reject_future=False) == context["observed_at"]
    assert context["observed_at"] >= floor[0]


def test_update_trigger_names_every_current_projection_column(storage) -> None:
    trigger = storage.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='relations_revision_au'"
    ).fetchone()[0]
    relation_columns = {
        str(row[1]).casefold() for row in storage.execute("PRAGMA table_info(relations)").fetchall()
    }
    normalized = " ".join(str(trigger).casefold().split())
    for column in relation_columns:
        assert f"old.{column} is not new.{column}" in normalized, (
            f"content-changing UPDATE of relations.{column} bypasses transaction history"
        )


def test_observed_boundary_is_persistent_monotonic_and_replace_delete_safe(storage) -> None:
    before = dict(storage.execute("SELECT * FROM relation_revision_context").fetchone())
    observed_time = datetime.fromisoformat(before["observed_at"].replace("Z", "+00:00"))
    lower = (
        (observed_time - timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )

    with pytest.raises(sqlite3.IntegrityError, match="observed boundary is immutable"):
        storage.execute(
            "UPDATE relation_revision_context SET observed_at=? WHERE singleton=1",
            (lower,),
        )
    storage.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError, match="observed boundary is immutable"):
        storage.execute("DELETE FROM relation_revision_context WHERE singleton=1")
    storage.execute("ROLLBACK")
    with pytest.raises(sqlite3.IntegrityError, match="observed boundary is immutable"):
        storage.execute(
            """INSERT OR REPLACE INTO relation_revision_context(
                   singleton, batch_id, recorded_at, observed_at
               ) VALUES(1, '', '', ?)""",
            (before["observed_at"],),
        )
    storage.execute("ROLLBACK")

    assert dict(storage.execute("SELECT * FROM relation_revision_context").fetchone()) == before


def test_served_observed_boundary_survives_reopen(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-observed-boundary.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    cutoff = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert made.relation_history_status("alice", cutoff)["known_at"] == cutoff
    assert (
        made.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
        == cutoff
    )
    made.close()

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        assert (
            reopened.execute(
                "SELECT observed_at FROM relation_revision_context WHERE singleton=1"
            ).fetchone()[0]
            == cutoff
        )
        assert reopened.relation_history_status("alice", cutoff)["known_at"] == cutoff
    finally:
        reopened.close()


def test_migration_baselines_the_current_projection_at_the_floor_not_created_at(settings, tmp_path) -> None:
    database = tmp_path / "schema-30-with-relation.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    made.create_relation(_relation("relation-before-v31", source_id, target_id))
    made.close()
    _age_current_database_to_schema_30(database)

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        floor = migrated.execute(
            "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
        ).fetchone()[0]
        rows = _revision_rows(migrated, "relation-before-v31")
        assert len(rows) == 1
        baseline = rows[0]
        assert baseline["revision"] == 1
        assert baseline["present"] == 1
        assert baseline["operation"] == "migration_baseline"
        assert baseline["history_quality"] == "migration_baseline"
        assert baseline["batch_id"] == "migration:v31"
        assert baseline["recorded_at"] == floor
        assert baseline["recorded_at"] != baseline["created_at"]
        assert baseline["source_entity_id"] == source_id
        assert baseline["target_entity_id"] == target_id
        assert baseline["metadata_json"] == '{"evidence": "рапорт"}'
    finally:
        migrated.close()

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        assert len(_revision_rows(reopened, "relation-before-v31")) == 1
        assert (
            reopened.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()[0]
            == floor
        )
    finally:
        reopened.close()


def test_insert_real_updates_direct_sql_and_delete_are_captured_without_noop_noise(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    relation_id = "relation-capture"
    storage.create_relation(_relation(relation_id, source_id, target_id))

    with storage.transaction() as conn:
        conn.execute("UPDATE relations SET weight=weight WHERE id=?", (relation_id,))
    assert len(_revision_rows(storage, relation_id)) == 1, "a no-op UPDATE invented a revision"

    with storage.transaction() as conn:
        batch_context = dict(
            conn.execute(
                "SELECT batch_id, recorded_at FROM relation_revision_context WHERE singleton=1"
            ).fetchone()
        )
        conn.execute("UPDATE relations SET weight=0.9 WHERE id=?", (relation_id,))
        conn.execute(
            "UPDATE relations SET metadata_json=? WHERE id=?",
            ('{"evidence":"приказ"}', relation_id),
        )

    # Deliberately outside FridayStorage.transaction(): the trigger must still
    # capture future/direct SQL and clearly label its fallback batch.
    storage.execute(
        "UPDATE relations SET valid_to='2023-03-03', invalidated_at='2024-04-04T00:00:00Z' WHERE id=?",
        (relation_id,),
    )
    storage.commit()

    with storage.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE id=?", (relation_id,))

    rows = _revision_rows(storage, relation_id)
    assert [row["revision"] for row in rows] == [1, 2, 3, 4, 5]
    assert [row["operation"] for row in rows] == ["insert", "update", "update", "update", "delete"]
    assert [row["present"] for row in rows] == [1, 1, 1, 1, 0]
    assert rows[1]["batch_id"] == rows[2]["batch_id"] == batch_context["batch_id"]
    assert rows[1]["recorded_at"] == rows[2]["recorded_at"] == batch_context["recorded_at"]
    assert rows[1]["event_seq"] < rows[2]["event_seq"]
    assert rows[3]["batch_id"].startswith("external:")
    assert normalize_known_at(rows[3]["recorded_at"], reject_future=False) == rows[3]["recorded_at"]
    assert rows[-1]["metadata_json"] == '{"evidence":"приказ"}'
    assert rows[-1]["valid_to"] == "2023-03-03"


def test_managed_relation_time_never_moves_backwards_with_the_wall_clock(storage, monkeypatch) -> None:
    source_id, target_id = _seed_endpoints(storage)
    storage.relation_history_status("alice")
    # Entity versions historically used second-granularity timestamps.  Age
    # these synthetic endpoints so identity validation can prove they predate
    # both sub-second relation boundaries under test.
    storage.execute(
        "UPDATE entity_versions SET created_at=? WHERE user_id=? AND entity_id IN (?, ?)",
        ("2000-01-01T00:00:00Z", "alice", source_id, target_id),
    )
    storage.commit()
    prior_clock = str(
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
    )
    prior_time = datetime.fromisoformat(prior_clock.replace("Z", "+00:00"))
    first_wall = prior_time + timedelta(microseconds=200)
    rewound_wall = prior_time + timedelta(microseconds=100)
    supplied = iter((first_wall, rewound_wall))

    class RewindingDateTime:
        @classmethod
        def now(cls, tz=None):
            value = next(supplied)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr("friday.storage._core.datetime", RewindingDateTime)
    relation = storage.create_relation(_relation("relation-clock-rewind", source_id, target_id))
    storage.invalidate_relation("alice", relation.id, valid_to="2024-01-01")

    revisions = _revision_rows(storage, relation.id)
    first_boundary = first_wall.isoformat(timespec="microseconds").replace("+00:00", "Z")
    rewound_boundary = rewound_wall.isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert revisions[0]["recorded_at"] == first_boundary
    assert revisions[1]["recorded_at"] > first_boundary
    assert storage.get_entity_relations(source_id, "alice", known_at=rewound_boundary) == []
    at_original_boundary = storage.get_entity_relations(source_id, "alice", known_at=first_boundary)
    assert [(row["id"], row["weight"], row["valid_to"]) for row in at_original_boundary] == [
        (relation.id, 0.75, None)
    ]


@pytest.mark.parametrize("external_operation", ["insert", "update", "delete"])
def test_external_relation_trigger_fallback_clamps_a_rewound_sqlite_clock(
    storage, monkeypatch, external_operation
) -> None:
    source_id, target_id = _seed_endpoints(storage)
    synthetic_future = datetime.now(UTC) + timedelta(days=1)

    class FutureDateTime:
        @classmethod
        def now(cls, tz=None):
            return (
                synthetic_future.astimezone(tz) if tz is not None else synthetic_future.replace(tzinfo=None)
            )

    with monkeypatch.context() as scoped:
        scoped.setattr("friday.storage._core.datetime", FutureDateTime)
        relation = storage.create_relation(_relation("relation-external-clock-rewind", source_id, target_id))

    # Deliberately outside FridayStorage.transaction(): SQLite's wall clock is
    # now earlier than the last managed boundary, and every capture trigger's
    # fallback independently owns the monotonicity guarantee.
    expected_operation = external_operation
    if external_operation == "insert":
        direct = _relation("relation-external-direct-insert", source_id, target_id)
        direct.relation_type = RelationType.RELATED_TO
        storage.execute(RELATION_INSERT_OR_IGNORE_SQL, direct.to_row())
        observed_relation_id = direct.id
    elif external_operation == "update":
        storage.execute("UPDATE relations SET weight=0.5 WHERE id=?", (relation.id,))
        observed_relation_id = relation.id
    else:
        storage.execute("DELETE FROM relations WHERE id=?", (relation.id,))
        observed_relation_id = relation.id
    storage.commit()
    anchor_recorded_at = _revision_rows(storage, relation.id)[0]["recorded_at"]
    observed = _revision_rows(storage, observed_relation_id)[-1]
    assert observed["operation"] == expected_operation
    assert observed["recorded_at"] > anchor_recorded_at
    old_snapshot = [
        row
        for row in _revision_rows(storage, observed_relation_id)
        if row["recorded_at"] <= anchor_recorded_at
    ]
    if external_operation == "insert":
        assert old_snapshot == []
    else:
        assert len(old_snapshot) == 1
        assert old_snapshot[0]["operation"] == "insert"
        assert old_snapshot[0]["present"] == 1
        assert old_snapshot[0]["weight"] == 0.75


def test_current_schema_refuses_decreasing_relation_recorded_at(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-decreasing-recorded-at.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-decreasing-clock-lineage", source_id, target_id))
    with made.transaction() as conn:
        conn.execute("UPDATE relations SET weight=0.5 WHERE id=?", (relation.id,))
    made.close()

    counterfeit_recorded_at = "2000-01-01T00:00:00.000000Z"
    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='relation_revisions_append_only_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revisions_append_only_update")
        corrupt.execute(
            "UPDATE relation_revisions SET recorded_at=? WHERE relation_id=? AND revision=2",
            (counterfeit_recorded_at, relation.id),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="recorded_at decreases across event order",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)
    assert counterfeit_recorded_at not in str(caught.value)


def test_current_schema_refuses_a_transaction_boundary_reused_by_another_batch(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-reused-transaction-boundary.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-reused-boundary", source_id, target_id))
    made.invalidate_relation("alice", relation.id, valid_to="2024-01-01")
    first_boundary = _revision_rows(made, relation.id)[0]["recorded_at"]
    made.close()

    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='relation_revisions_append_only_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revisions_append_only_update")
        corrupt.execute(
            "UPDATE relation_revisions SET recorded_at=? WHERE relation_id=? AND revision=2",
            (first_boundary, relation.id),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="reuses a transaction boundary across batches",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)
    assert first_boundary not in str(caught.value)


def test_current_schema_refuses_relation_history_before_its_completeness_floor(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-revision-before-floor.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(
        _relation("private-revision-before-completeness-floor", source_id, target_id)
    )
    made.close()

    counterfeit_recorded_at = "2000-01-01T00:00:00.000000Z"
    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='relation_revisions_append_only_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revisions_append_only_update")
        corrupt.execute(
            "UPDATE relation_revisions SET recorded_at=? WHERE relation_id=?",
            (counterfeit_recorded_at, relation.id),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="relation history violates its completeness floor",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)
    assert counterfeit_recorded_at not in str(caught.value)


def test_current_schema_refuses_a_captured_revision_exactly_on_the_migration_floor(
    settings, tmp_path
) -> None:
    database = tmp_path / "schema-31-captured-revision-on-floor.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-revision-on-floor", source_id, target_id))
    floor = str(made.relation_history_status("alice")["known_at_floor"])
    made.close()

    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='relation_revisions_append_only_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revisions_append_only_update")
        corrupt.execute(
            "UPDATE relation_revisions SET recorded_at=? WHERE relation_id=?",
            (floor, relation.id),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="relation history violates its completeness floor",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)
    assert floor not in str(caught.value)


def test_one_outer_transaction_has_one_precise_batch_and_rollback_leaves_nothing(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    first = _relation("relation-batch-one", source_id, target_id)
    second = _relation(
        "relation-batch-two",
        source_id,
        target_id,
        relation_type=RelationType.WORKS_ON,
    )

    with storage.transaction() as conn:
        context = dict(conn.execute("SELECT * FROM relation_revision_context").fetchone())
        conn.execute(
            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                   relation_type, weight, metadata_json, created_at, deleted_at,
                   valid_from, valid_to, invalidated_at, superseded_by)
               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                   :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                   :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
            first.to_row(),
        )
        conn.execute(
            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                   relation_type, weight, metadata_json, created_at, deleted_at,
                   valid_from, valid_to, invalidated_at, superseded_by)
               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                   :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                   :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
            second.to_row(),
        )

    rows = [
        dict(row)
        for row in storage.execute(
            "SELECT * FROM relation_revisions WHERE relation_id IN (?, ?) ORDER BY event_seq",
            (first.id, second.id),
        ).fetchall()
    ]
    assert len(rows) == 2
    assert {row["batch_id"] for row in rows} == {context["batch_id"]}
    assert {row["recorded_at"] for row in rows} == {context["recorded_at"]}
    assert rows[0]["event_seq"] < rows[1]["event_seq"]
    assert context["recorded_at"].endswith("Z") and len(context["recorded_at"].split(".")[1]) == 7
    assert context["observed_at"] == context["recorded_at"]

    committed_context = dict(storage.execute("SELECT * FROM relation_revision_context").fetchone())

    doomed = _relation(
        "relation-rolled-back",
        source_id,
        target_id,
        relation_type=RelationType.DEPENDS_ON,
    )
    with pytest.raises(RuntimeError, match="interrupt"), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                   relation_type, weight, metadata_json, created_at, deleted_at,
                   valid_from, valid_to, invalidated_at, superseded_by)
               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                   :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                   :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
            doomed.to_row(),
        )
        raise RuntimeError("interrupt")

    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (doomed.id,)).fetchone() is None
    assert _revision_rows(storage, doomed.id) == []
    assert dict(storage.execute("SELECT * FROM relation_revision_context").fetchone()) == committed_context


def test_transaction_supplies_a_batch_inside_a_preexisting_implicit_sql_transaction(storage) -> None:
    """A direct execute() may open sqlite's outer transaction before our context manager."""

    source_id, target_id = _seed_endpoints(storage)
    storage.execute(
        "UPDATE entities SET description='uncommitted prelude' WHERE id=?",
        (source_id,),
    )
    relation = storage.create_relation(_relation("relation-after-direct-execute", source_id, target_id))
    revision = _revision_rows(storage, relation.id)[0]
    assert revision["batch_id"].startswith("relation_batch_")
    assert not revision["batch_id"].startswith("external:")
    assert normalize_known_at(revision["recorded_at"], reject_future=False) == revision["recorded_at"]
    assert dict(storage.execute("SELECT * FROM relation_revision_context").fetchone()) == {
        "singleton": 1,
        "batch_id": "",
        "recorded_at": "",
        "observed_at": revision["recorded_at"],
    }
    storage.commit()


def test_managed_block_rolls_back_inside_a_preexisting_implicit_sql_transaction(storage) -> None:
    """Its SAVEPOINT must not lose the caller's prelude or leak failed relation DML."""

    source_id, target_id = _seed_endpoints(storage)
    prior_observed = str(
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[0]
    )
    storage.execute(
        "UPDATE entities SET description='caller prelude survives' WHERE id=?",
        (source_id,),
    )
    relation = _relation("relation-failed-after-direct-execute", source_id, target_id)

    with pytest.raises(RuntimeError, match="managed failure"), storage.transaction():
        # This is a genuine nested Friday transaction and must keep sharing the
        # managed block's batch. The outer managed block alone owns the SAVEPOINT.
        storage.create_relation(relation)
        raise RuntimeError("managed failure")

    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (relation.id,)).fetchone() is None
    assert _revision_rows(storage, relation.id) == []
    assert storage.execute("SELECT description FROM entities WHERE id=?", (source_id,)).fetchone()[0] == (
        "caller prelude survives"
    )
    assert dict(storage.execute("SELECT * FROM relation_revision_context").fetchone()) == {
        "singleton": 1,
        "batch_id": "",
        "recorded_at": "",
        "observed_at": prior_observed,
    }

    # A later unrelated commit may persist the caller's prelude, never the failed
    # managed relation or its append-only evidence.
    storage.commit()
    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (relation.id,)).fetchone() is None
    assert _revision_rows(storage, relation.id) == []


def test_real_nested_transactions_keep_the_outer_relation_batch(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    nested_relation = _relation("relation-real-nested", source_id, target_id)

    with storage.transaction() as outer:
        outer_context = dict(outer.execute("SELECT * FROM relation_revision_context").fetchone())
        with storage.transaction() as inner:
            assert dict(inner.execute("SELECT * FROM relation_revision_context").fetchone()) == outer_context
            storage.create_relation(nested_relation)
        assert dict(outer.execute("SELECT * FROM relation_revision_context").fetchone()) == outer_context

    revision = _revision_rows(storage, nested_relation.id)[0]
    assert revision["batch_id"] == outer_context["batch_id"]
    assert revision["recorded_at"] == outer_context["recorded_at"]


def test_caught_inner_transaction_rolls_back_only_its_dml_and_revisions(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    survivor = _relation("relation-outer-survivor", source_id, target_id)
    inner_only = _relation(
        "relation-inner-rolled-back",
        source_id,
        target_id,
        relation_type=RelationType.WORKS_ON,
    )
    after_inner = _relation(
        "relation-outer-after-catch",
        source_id,
        target_id,
        relation_type=RelationType.DEPENDS_ON,
    )

    with storage.transaction() as outer:
        outer_context = dict(outer.execute("SELECT * FROM relation_revision_context").fetchone())
        storage.create_relation(survivor)

        with pytest.raises(RuntimeError, match="inner failure"), storage.transaction() as inner:
            assert dict(inner.execute("SELECT * FROM relation_revision_context").fetchone()) == (
                outer_context
            )
            inner.execute("UPDATE relations SET weight=0.2 WHERE id=?", (survivor.id,))
            storage.create_relation(inner_only)
            raise RuntimeError("inner failure")

        # The caught exception did not poison the outer unit, while both the
        # inner current rows and the revisions produced for them disappeared.
        assert outer.execute("SELECT weight FROM relations WHERE id=?", (survivor.id,)).fetchone()[0] == 0.75
        assert len(_revision_rows(storage, survivor.id)) == 1
        assert outer.execute("SELECT 1 FROM relations WHERE id=?", (inner_only.id,)).fetchone() is None
        assert _revision_rows(storage, inner_only.id) == []
        assert dict(outer.execute("SELECT * FROM relation_revision_context").fetchone()) == outer_context
        storage.create_relation(after_inner)

    surviving_revisions = _revision_rows(storage, survivor.id) + _revision_rows(storage, after_inner.id)
    assert len(surviving_revisions) == 2
    assert {row["batch_id"] for row in surviving_revisions} == {outer_context["batch_id"]}
    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (inner_only.id,)).fetchone() is None
    assert _revision_rows(storage, inner_only.id) == []


def test_relation_replace_guards_preserve_lineage_with_recursive_triggers_off(storage) -> None:
    alice_source, alice_target = _seed_endpoints(storage)
    bob_source, bob_target = _seed_endpoints(storage, "bob")
    original = storage.create_relation(_relation("relation-replace-protected", alice_source, alice_target))
    original_snapshot = dict(storage.execute("SELECT * FROM relations WHERE id=?", (original.id,)).fetchone())

    # A primary-key REPLACE would otherwise move the same relation lineage to a
    # different tenant without firing the displaced row's DELETE capture.
    tenant_move = _relation(
        original.id,
        bob_source,
        bob_target,
        user_id="bob",
    )
    with (
        pytest.raises(sqlite3.IntegrityError, match="replace current state or move identity"),
        storage.transaction() as conn,
    ):
        conn.execute("PRAGMA recursive_triggers=OFF")
        conn.execute(RELATION_INSERT_OR_REPLACE_SQL, tenant_move.to_row())

    assert dict(storage.execute("SELECT * FROM relations WHERE id=?", (original.id,)).fetchone()) == (
        original_snapshot
    )
    assert [row["user_id"] for row in _revision_rows(storage, original.id)] == ["alice"]

    # A different primary key can conflict with the active partial unique index.
    # REPLACE used to erase `original` and leave its latest present snapshot as a
    # ghost whenever recursive DELETE triggers were disabled.
    active_conflict = _relation("relation-active-conflict", alice_source, alice_target)
    with (
        pytest.raises(sqlite3.IntegrityError, match="replace current state or move identity"),
        storage.transaction() as conn,
    ):
        conn.execute("PRAGMA recursive_triggers=OFF")
        conn.execute(RELATION_INSERT_OR_REPLACE_SQL, active_conflict.to_row())

    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (original.id,)).fetchone()
    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (active_conflict.id,)).fetchone() is None
    assert len(_revision_rows(storage, original.id)) == 1
    assert _revision_rows(storage, active_conflict.id) == []


def test_update_or_replace_cannot_conflict_delete_an_active_relation(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    first = storage.create_relation(_relation("relation-update-replace-first", source_id, target_id))
    second = storage.create_relation(
        _relation(
            "relation-update-replace-second",
            source_id,
            target_id,
            relation_type=RelationType.WORKS_ON,
        )
    )

    with (
        pytest.raises(sqlite3.IntegrityError, match="replace an active relation"),
        storage.transaction() as conn,
    ):
        conn.execute("PRAGMA recursive_triggers=OFF")
        conn.execute(
            "UPDATE OR REPLACE relations SET relation_type=? WHERE id=?",
            (RelationType.WORKS_ON.value, first.id),
        )

    assert storage.execute("SELECT relation_type FROM relations WHERE id=?", (first.id,)).fetchone()[0] == (
        RelationType.MEMBER_OF.value
    )
    assert storage.execute("SELECT relation_type FROM relations WHERE id=?", (second.id,)).fetchone()[0] == (
        RelationType.WORKS_ON.value
    )
    assert len(_revision_rows(storage, first.id)) == 1
    assert len(_revision_rows(storage, second.id)) == 1


def test_captured_tombstone_allows_same_tenant_unmerge_resurrection_and_duplicate_create(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    bob_source, bob_target = _seed_endpoints(storage, "bob")
    relation = storage.create_relation(_relation("relation-unmerge-resurrection", source_id, target_id))

    with storage.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE id=?", (relation.id,))
    assert [row["present"] for row in _revision_rows(storage, relation.id)] == [1, 0]

    moved_owner = _relation(
        relation.id,
        bob_source,
        bob_target,
        user_id="bob",
    )
    with (
        pytest.raises(sqlite3.IntegrityError, match="move identity"),
        storage.transaction() as conn,
    ):
        conn.execute(RELATION_INSERT_OR_IGNORE_SQL, moved_owner.to_row())

    # This is the exact INSERT conflict mode used by unmerge_entities.  A captured
    # tombstone with the same owner is a continuation, not a destructive REPLACE.
    with storage.transaction() as conn:
        cursor = conn.execute(RELATION_INSERT_OR_IGNORE_SQL, relation.to_row())
        assert cursor.rowcount == 1
    assert [row["operation"] for row in _revision_rows(storage, relation.id)] == [
        "insert",
        "delete",
        "insert",
    ]

    # Ordinary idempotent creation still resolves to the active row and invents
    # neither a replacement nor another revision.
    duplicate = storage.create_relation(_relation("different-caller-id", source_id, target_id))
    assert duplicate.id == relation.id
    assert len(_revision_rows(storage, relation.id)) == 3
    assert _revision_rows(storage, "different-caller-id") == []

    # A later active duplicate is the reason unmerge uses OR IGNORE.  The guard
    # must keep that safe no-op while also making OR REPLACE unable to evict the
    # later decision when recursive DELETE triggers are off.
    with storage.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE id=?", (relation.id,))
    later = storage.create_relation(_relation("relation-created-after-merge", source_id, target_id))
    assert later.id == "relation-created-after-merge"

    with storage.transaction() as conn:
        ignored = conn.execute(RELATION_INSERT_OR_IGNORE_SQL, relation.to_row())
        assert ignored.rowcount == 0
    with storage.transaction() as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        ignored_replace = conn.execute(RELATION_INSERT_OR_REPLACE_SQL, relation.to_row())
        assert ignored_replace.rowcount == 0

    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (relation.id,)).fetchone() is None
    assert storage.execute("SELECT 1 FROM relations WHERE id=?", (later.id,)).fetchone()
    assert [row["operation"] for row in _revision_rows(storage, relation.id)] == [
        "insert",
        "delete",
        "insert",
        "delete",
    ]
    assert len(_revision_rows(storage, later.id)) == 1


def test_history_and_its_completeness_floor_are_database_immutable(storage) -> None:
    source_id, target_id = _seed_endpoints(storage)
    relation = storage.create_relation(_relation("relation-protected", source_id, target_id))
    revision = _revision_rows(storage, relation.id)[0]
    storage.ensure_user("bob")

    with (
        pytest.raises(sqlite3.IntegrityError, match="id and user_id are immutable"),
        storage.transaction() as conn,
    ):
        conn.execute(
            "UPDATE relations SET id='relation-renamed' WHERE id=?",
            (relation.id,),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="id and user_id are immutable"),
        storage.transaction() as conn,
    ):
        conn.execute(
            "UPDATE relations SET user_id='bob' WHERE id=?",
            (relation.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.transaction() as conn:
        conn.execute(
            "UPDATE relation_revisions SET weight=0.1 WHERE event_seq=?",
            (revision["event_seq"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.transaction() as conn:
        conn.execute("DELETE FROM relation_revisions WHERE event_seq=?", (revision["event_seq"],))
    columns = tuple(revision)
    placeholders = ", ".join(f":{column}" for column in columns)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), storage.transaction() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO relation_revisions({', '.join(columns)}) "  # nosec B608 - PRAGMA row keys
            f"VALUES({placeholders})",  # nosec B608 - one placeholder per fixed schema key
            revision,
        )

    floor = storage.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), storage.transaction() as conn:
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='relation_history_complete_from'",
            ("2000-01-01T00:00:00.000000Z",),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), storage.transaction() as conn:
        conn.execute("DELETE FROM schema_meta WHERE key='relation_history_complete_from'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), storage.transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO schema_meta(key, value, updated_at)
               VALUES('relation_history_complete_from', ?, ?)""",
            ("2000-01-01T00:00:00.000000Z", "2000-01-01T00:00:00.000000Z"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), storage.transaction() as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        conn.execute(
            """INSERT INTO schema_meta(key, value, updated_at)
               VALUES('synthetic-floor-carrier', ?, ?)""",
            ("2000-01-01T00:00:00.000000Z", "2000-01-01T00:00:00.000000Z"),
        )
        conn.execute(
            """UPDATE OR REPLACE schema_meta
                  SET key='relation_history_complete_from'
                WHERE key='synthetic-floor-carrier'"""
        )

    assert len(_revision_rows(storage, relation.id)) == 1
    assert storage.execute("SELECT id FROM relations WHERE id=?", (relation.id,)).fetchone()
    assert storage.execute("SELECT id FROM relations WHERE id='relation-renamed'").fetchone() is None
    assert (
        storage.execute(
            "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
        ).fetchone()[0]
        == floor
    )


def test_schema_31_migration_failure_rolls_back_tables_floor_baseline_and_marker(
    settings, tmp_path, monkeypatch
) -> None:
    database = tmp_path / "schema-30-failure.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    made.create_relation(_relation("relation-before-failure", source_id, target_id))
    made.close()
    _age_current_database_to_schema_30(database)

    from friday.storage._core import CoreMixin

    def fail_after_baseline(self, conn):
        del self, conn
        raise RuntimeError("injected schema-31 failure")

    monkeypatch.setattr(CoreMixin, "_retire_outdated_indexes", fail_after_baseline)
    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(RuntimeError, match="injected schema-31 failure"):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        marker = probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        floor = probe.execute(
            "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
        ).fetchone()
        tables = {
            row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert marker == "30"
    assert floor is None
    assert "relation_revisions" not in tables
    assert "relation_revision_context" not in tables


def test_current_schema_refuses_a_missing_capture_trigger_after_a_revision_was_lost(
    settings, tmp_path
) -> None:
    database = tmp_path / "schema-31-lost-captured-update.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("relation-with-lost-update", source_id, target_id))
    floor = made.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()[0]
    made.close()

    # Synthetic corruption only: once UPDATE capture is absent, current state can
    # advance without its authoritative revision. Recreating the trigger on reopen
    # cannot recover that already-lost transaction-time boundary.
    with sqlite3.connect(database) as corrupt:
        corrupt.execute("DROP TRIGGER relations_revision_au")
        corrupt.execute("UPDATE relations SET weight=0.25 WHERE id=?", (relation.id,))
        current_weight = corrupt.execute(
            "SELECT weight FROM relations WHERE id=?", (relation.id,)
        ).fetchone()[0]
        history_weight = corrupt.execute(
            """SELECT weight FROM relation_revisions
               WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1""",
            (relation.id,),
        ).fetchone()[0]
    assert current_weight == 0.25
    assert history_weight == 0.75

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*relations_revision_au",
    ):
        broken.execute("SELECT 1")
    broken.close()

    # Diagnostic reopen must not claim completeness by restoring the trigger, by
    # moving the floor, or by inventing a baseline for today's projection.
    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='relations_revision_au'"
            ).fetchone()
            is None
        )
        assert (
            probe.execute(
                "SELECT COUNT(*) FROM relation_revisions WHERE relation_id=?", (relation.id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            probe.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()[0]
            == floor
        )


def test_current_schema_refuses_current_projection_that_lags_its_restored_capture_trigger(
    settings, tmp_path
) -> None:
    database = tmp_path / "schema-31-stale-current-lineage.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("relation-with-stale-lineage", source_id, target_id))
    made.close()

    with sqlite3.connect(database) as corrupt:
        trigger_sql = corrupt.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='relations_revision_au'"
        ).fetchone()[0]
        corrupt.execute("DROP TRIGGER relations_revision_au")
        corrupt.execute("UPDATE relations SET weight=0.25 WHERE id=?", (relation.id,))
        corrupt.execute(str(trigger_sql))

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*current relation projection",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT COUNT(*) FROM relation_revisions WHERE relation_id=?", (relation.id,)
            ).fetchone()[0]
            == 1
        )


def test_current_schema_refuses_a_present_history_ghost_after_delete_capture_was_lost(
    settings, tmp_path
) -> None:
    database = tmp_path / "schema-31-present-history-ghost.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-relation-id-must-not-leak", source_id, target_id))
    made.close()

    with sqlite3.connect(database) as corrupt:
        trigger_sql = corrupt.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='relations_revision_bd'"
        ).fetchone()[0]
        corrupt.execute("DROP TRIGGER relations_revision_bd")
        corrupt.execute("DELETE FROM relations WHERE id=?", (relation.id,))
        corrupt.execute(str(trigger_sql))

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="latest present relation history is absent from current projection",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)


def test_current_schema_refuses_a_current_row_after_its_insert_capture_was_lost(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-current-after-tombstone.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-tombstone-id-must-not-leak", source_id, target_id))
    with made.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE id=?", (relation.id,))
    made.close()

    with sqlite3.connect(database) as corrupt:
        trigger_sql = corrupt.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='relations_revision_ai'"
        ).fetchone()[0]
        corrupt.execute("DROP TRIGGER relations_revision_ai")
        corrupt.execute(RELATION_INSERT_OR_IGNORE_SQL, relation.to_row())
        corrupt.execute(str(trigger_sql))

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="latest relation tombstone still has a current projection",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)


@pytest.mark.parametrize(
    ("corruption", "diagnostic"),
    [
        (
            "UPDATE relation_revisions SET user_id='other-tenant' "
            "WHERE relation_id='private-sequence-id' AND revision=1",
            "relation history owner continuity is broken",
        ),
        (
            "UPDATE relation_revisions SET revision=3 WHERE relation_id='private-sequence-id' AND revision=2",
            "relation revision sequence has gaps or reordered events",
        ),
    ],
)
def test_current_schema_audits_owner_continuity_and_revision_sequence_without_identifiers(
    settings, tmp_path, corruption, diagnostic
) -> None:
    database = tmp_path / "schema-31-broken-revision-sequence.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-sequence-id", source_id, target_id))
    with made.transaction() as conn:
        conn.execute("UPDATE relations SET weight=0.5 WHERE id=?", (relation.id,))
    made.close()

    with sqlite3.connect(database) as corrupt:
        trigger_sql = corrupt.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='trigger' AND name='relation_revisions_append_only_update'"""
        ).fetchone()[0]
        corrupt.execute("DROP TRIGGER relation_revisions_append_only_update")
        corrupt.execute(corruption)
        corrupt.execute(str(trigger_sql))

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(UnsupportedSchemaVersionError, match=diagnostic) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert "private-sequence-id" not in str(caught.value)
    assert "other-tenant" not in str(caught.value)


@pytest.mark.parametrize("missing_trigger", sorted(CAPTURE_TRIGGERS | PROTECTION_TRIGGERS))
def test_current_schema_refuses_every_missing_relation_history_trigger(
    settings, tmp_path, missing_trigger
) -> None:
    database = tmp_path / f"schema-31-missing-{missing_trigger}.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    with sqlite3.connect(database) as corrupt:
        corrupt.execute(f"DROP TRIGGER {missing_trigger}")  # nosec B608 - fixed test allowlist

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=rf"relation history is incomplete.*{missing_trigger}",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (missing_trigger,),
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("altered_trigger", sorted(CAPTURE_TRIGGERS | PROTECTION_TRIGGERS))
def test_current_schema_refuses_every_same_name_noop_relation_history_trigger(
    settings, tmp_path, altered_trigger
) -> None:
    database = tmp_path / f"schema-31-altered-{altered_trigger}.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    # A trigger's name and table do not prove that it still captures or protects
    # anything. Keep the original event header, but replace its whole body with a
    # valid no-op so CREATE IF NOT EXISTS would otherwise accept the counterfeit.
    with sqlite3.connect(database) as corrupt:
        trigger_sql = str(
            corrupt.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (altered_trigger,),
            ).fetchone()[0]
        )
        header, separator, _body = trigger_sql.partition("\nBEGIN\n")
        assert separator
        corrupt.execute(f"DROP TRIGGER {altered_trigger}")  # nosec B608 - fixed test allowlist
        corrupt.execute(f"{header}\nBEGIN\n    SELECT 1;\nEND")  # nosec B608 - synthetic DDL

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=rf"relation history is incomplete.*altered triggers: {altered_trigger}",
    ):
        broken.execute("SELECT 1")
    broken.close()

    # Validation runs before idempotent DDL, so diagnostics must leave the no-op
    # in place rather than silently replacing it and claiming the gap is healed.
    with sqlite3.connect(database) as probe:
        persisted = str(
            probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (altered_trigger,),
            ).fetchone()[0]
        )
    assert "SELECT 1" in persisted


def test_current_schema_compares_trigger_literals_case_sensitively(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-case-altered-floor-trigger.sqlite3"
    trigger_name = "relation_history_floor_immutable_insert"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    # SQL keywords are case-insensitive, but the floor key literal is not. A
    # whole-definition casefold would accept this trigger even though it no
    # longer protects the real lower-case completeness promise.
    with sqlite3.connect(database) as corrupt:
        trigger_sql = str(
            corrupt.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()[0]
        )
        altered_sql = trigger_sql.replace(
            "'relation_history_complete_from'",
            "'RELATION_HISTORY_COMPLETE_FROM'",
        )
        assert altered_sql != trigger_sql
        corrupt.execute(f"DROP TRIGGER {trigger_name}")
        corrupt.execute(altered_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=rf"relation history is incomplete.*altered triggers: {trigger_name}",
    ):
        broken.execute("SELECT 1")
    broken.close()


@pytest.mark.parametrize(
    "counterfeit_context_sql",
    [
        """CREATE TABLE relation_revision_context (
               singleton INTEGER CHECK(singleton = 1),
               batch_id TEXT NOT NULL DEFAULT '',
               recorded_at TEXT NOT NULL DEFAULT '',
               CHECK((batch_id = '' AND recorded_at = '')
                     OR (batch_id <> '' AND recorded_at <> ''))
           )""",
        """CREATE TABLE relation_revision_context (
               singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
               batch_id TEXT NOT NULL DEFAULT '',
               recorded_at TEXT NOT NULL DEFAULT ''
           )""",
    ],
    ids=["missing-primary-key", "missing-coupled-empty-check"],
)
def test_current_schema_refuses_a_counterfeit_relation_revision_context_table(
    settings, tmp_path, counterfeit_context_sql
) -> None:
    database = tmp_path / "schema-31-counterfeit-context.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    with sqlite3.connect(database) as corrupt:
        corrupt.execute("DROP TABLE relation_revision_context")
        corrupt.execute(counterfeit_context_sql)
        corrupt.execute(
            "INSERT INTO relation_revision_context(singleton, batch_id, recorded_at) VALUES(1, '', '')"
        )
        altered_sql = corrupt.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='relation_revision_context'"
        ).fetchone()[0]

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*altered tables: relation_revision_context",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='relation_revision_context'"
            ).fetchone()[0]
            == altered_sql
        )


def test_current_schema_refuses_a_counterfeit_relation_revisions_table(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-counterfeit-revisions.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    made.create_relation(_relation("private-counterfeit-table-lineage", source_id, target_id))
    made.close()

    columns = (
        "event_seq",
        "relation_id",
        "revision",
        "present",
        "operation",
        "recorded_at",
        "batch_id",
        "history_quality",
        "user_id",
        "source_entity_id",
        "target_entity_id",
        "relation_type",
        "weight",
        "metadata_json",
        "created_at",
        "deleted_at",
        "valid_from",
        "valid_to",
        "invalidated_at",
        "superseded_by",
    )
    with sqlite3.connect(database) as corrupt:
        rows = corrupt.execute(
            f"SELECT {', '.join(columns)} FROM relation_revisions ORDER BY event_seq"  # nosec B608
        ).fetchall()
        append_triggers = [
            str(row[0])
            for row in corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND tbl_name='relation_revisions'
                   ORDER BY name"""
            ).fetchall()
        ]
        corrupt.execute("DROP TABLE relation_revisions")
        corrupt.execute(
            """CREATE TABLE relation_revisions (
                   event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                   relation_id TEXT NOT NULL,
                   revision INTEGER NOT NULL,
                   present INTEGER NOT NULL,
                   operation TEXT NOT NULL,
                   recorded_at TEXT NOT NULL,
                   batch_id TEXT NOT NULL,
                   history_quality TEXT NOT NULL,
                   user_id TEXT NOT NULL,
                   source_entity_id TEXT NOT NULL,
                   target_entity_id TEXT NOT NULL,
                   relation_type TEXT NOT NULL,
                   weight REAL NOT NULL,
                   metadata_json TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   deleted_at TEXT,
                   valid_from TEXT NOT NULL,
                   valid_to TEXT,
                   invalidated_at TEXT,
                   superseded_by TEXT
               )"""
        )
        placeholders = ", ".join("?" for _ in columns)
        corrupt.executemany(
            f"INSERT INTO relation_revisions({', '.join(columns)}) VALUES({placeholders})",  # nosec B608
            rows,
        )
        for trigger_sql in append_triggers:
            corrupt.execute(trigger_sql)
        altered_sql = corrupt.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='relation_revisions'"
        ).fetchone()[0]
        revision_count = len(rows)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*altered tables: relation_revisions",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert "private-counterfeit-table-lineage" not in str(caught.value)

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='relation_revisions'"
            ).fetchone()[0]
            == altered_sql
        )
        assert probe.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0] == revision_count


@pytest.mark.parametrize(
    ("corruption", "expected_marker"),
    [
        ("DELETE FROM schema_meta WHERE key='schema_version'", None),
        ("UPDATE schema_meta SET value='' WHERE key='schema_version'", ""),
        ("UPDATE schema_meta SET value='30' WHERE key='schema_version'", "30"),
    ],
)
def test_relation_history_artifacts_refuse_a_missing_or_stale_schema_marker(
    settings, tmp_path, corruption, expected_marker
) -> None:
    database = tmp_path / "schema-31-stale-marker.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    made.create_relation(_relation("private-marker-lineage", source_id, target_id))
    floor = made.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()[0]
    made.close()

    with sqlite3.connect(database) as corrupt:
        corrupt.execute(corruption)
        revision_count = corrupt.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0]

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="schema marker predates installed relation-history artifacts",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        marker_row = probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert (marker_row[0] if marker_row else None) == expected_marker
        assert (
            probe.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()[0]
            == floor
        )
        assert probe.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0] == revision_count


def test_relation_history_artifacts_refuse_a_dropped_schema_meta_table(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-dropped-schema-meta.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    made.create_relation(_relation("private-dropped-marker-lineage", source_id, target_id))
    made.close()

    with sqlite3.connect(database) as corrupt:
        corrupt.execute("DROP TABLE schema_meta")
        revision_count = corrupt.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0]
        schema_before = corrupt.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="schema marker predates installed relation-history artifacts",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
            is None
        )
        assert probe.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0] == revision_count
        assert (
            probe.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            == schema_before
        )


@pytest.mark.parametrize(
    ("counterfeit_floor", "counterfeit_updated_at", "diagnostic"),
    [
        ("not-a-timestamp", None, "invalid relation_history_complete_from floor"),
        (
            "2000-01-01T03:00:00+03:00",
            None,
            "non-canonical relation_history_complete_from floor",
        ),
        (
            " 2000-01-01T00:00:00.000000Z ",
            " 2000-01-01T00:00:00.000000Z ",
            "non-canonical relation_history_complete_from floor",
        ),
        (
            "2000-01-01T00:00:00.000000Z",
            None,
            "relation_history_complete_from floor provenance is inconsistent",
        ),
    ],
)
def test_current_schema_refuses_an_invalid_or_noncanonical_history_floor(
    settings, tmp_path, counterfeit_floor, counterfeit_updated_at, diagnostic
) -> None:
    database = tmp_path / "schema-31-counterfeit-floor.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='relation_history_floor_immutable_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_history_floor_immutable_update")
        corrupt.execute(
            """UPDATE schema_meta
                  SET value=?, updated_at=COALESCE(?, updated_at)
                WHERE key='relation_history_complete_from'""",
            (counterfeit_floor, counterfeit_updated_at),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(UnsupportedSchemaVersionError, match=diagnostic) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert counterfeit_floor not in str(caught.value)

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()[0]
            == counterfeit_floor
        )


def test_current_schema_refuses_a_migration_baseline_detached_from_its_floor(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-detached-baseline.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    made.create_relation(_relation("private-detached-baseline", source_id, target_id))
    made.close()
    _age_current_database_to_schema_30(database)

    migrated = FridayStorage(replace(settings, database_path=database))
    floor = migrated.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()[0]
    migrated.close()

    counterfeit_recorded_at = "2000-01-01T00:00:00.000000Z"
    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='relation_revisions_append_only_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revisions_append_only_update")
        corrupt.execute(
            """UPDATE relation_revisions SET recorded_at=?
               WHERE operation='migration_baseline'""",
            (counterfeit_recorded_at,),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="migration baseline does not match relation history floor",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert "private-detached-baseline" not in str(caught.value)
    assert counterfeit_recorded_at not in str(caught.value)

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()[0]
            == floor
        )
        assert (
            probe.execute(
                "SELECT recorded_at FROM relation_revisions WHERE operation='migration_baseline'"
            ).fetchone()[0]
            == counterfeit_recorded_at
        )


def test_current_schema_refuses_a_missing_relation_revision_context_table(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-missing-revision-context-table.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    with sqlite3.connect(database) as corrupt:
        corrupt.execute("DROP TABLE relation_revision_context")

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*relation_revision_context",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_revision_context'"
            ).fetchone()
            is None
        )


def test_current_schema_refuses_a_missing_relation_revision_context_row(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-missing-revision-context-row.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master WHERE type='trigger'
                   AND name='relation_revision_context_immutable_delete'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revision_context_immutable_delete")
        corrupt.execute("DELETE FROM relation_revision_context WHERE singleton=1")
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*context singleton",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT 1 FROM relation_revision_context").fetchone() is None


def test_current_schema_refuses_an_observed_boundary_behind_relation_evidence(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-observed-behind-evidence.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("private-observed-tail", source_id, target_id))
    floor = str(made.relation_history_status("alice")["known_at_floor"])
    made.close()

    with sqlite3.connect(database) as corrupt:
        guard_sql = str(
            corrupt.execute(
                """SELECT sql FROM sqlite_master WHERE type='trigger'
                   AND name='relation_revision_context_monotonic_update'"""
            ).fetchone()[0]
        )
        corrupt.execute("DROP TRIGGER relation_revision_context_monotonic_update")
        corrupt.execute(
            "UPDATE relation_revision_context SET observed_at=? WHERE singleton=1",
            (floor,),
        )
        corrupt.execute(guard_sql)

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="relation history exceeds its observed boundary",
    ) as caught:
        broken.execute("SELECT 1")
    broken.close()
    assert relation.id not in str(caught.value)
    assert floor not in str(caught.value)


def test_current_schema_refuses_a_missing_floor_even_when_its_guards_are_present(settings, tmp_path) -> None:
    database = tmp_path / "schema-31-missing-relation-history-floor.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close()

    with sqlite3.connect(database) as corrupt:
        guard_sql = corrupt.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='trigger' AND name='relation_history_floor_immutable_delete'"""
        ).fetchone()[0]
        corrupt.execute("DROP TRIGGER relation_history_floor_immutable_delete")
        corrupt.execute("DELETE FROM schema_meta WHERE key='relation_history_complete_from'")
        corrupt.execute(str(guard_sql))

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"relation history is incomplete.*relation_history_complete_from floor",
    ):
        broken.execute("SELECT 1")
    broken.close()

    with sqlite3.connect(database) as probe:
        assert (
            probe.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("recreate_history_table", [False, True])
def test_current_schema_refuses_lost_or_empty_recreated_history_instead_of_inventing_it(
    settings, tmp_path, recreate_history_table
) -> None:
    database = tmp_path / "schema-31-lost-relation-history.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    source_id, target_id = _seed_endpoints(made)
    relation = made.create_relation(_relation("relation-whose-history-was-lost", source_id, target_id))
    floor = made.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()[0]
    made.close()

    # Synthetic corruption only: recreate the authoritative table and its guards
    # exactly, but with no rows. The marker/floor/current projection remain, which
    # was the fail-open state found by read-only audit.
    with sqlite3.connect(database) as corrupt:
        table_sql = corrupt.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='relation_revisions'"
        ).fetchone()[0]
        guard_sql = [
            row[0]
            for row in corrupt.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND tbl_name='relation_revisions'
                   ORDER BY name"""
            ).fetchall()
        ]
        corrupt.execute("DROP TABLE relation_revisions")
        if recreate_history_table:
            corrupt.execute(str(table_sql))
            for statement in guard_sql:
                corrupt.execute(str(statement))

    broken = FridayStorage(replace(settings, database_path=database))
    with pytest.raises(UnsupportedSchemaVersionError, match="relation history is incomplete"):
        broken.execute("SELECT 1")
    broken.close()

    # Reopen is diagnostic only: it must neither move the immutable floor nor
    # synthesize a present-day baseline under that older completeness promise.
    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(
            SCHEMA_VERSION
        )
        assert (
            probe.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()[0]
            == floor
        )
        history_table = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_revisions'"
        ).fetchone()
        assert (history_table is not None) is recreate_history_table
        if recreate_history_table:
            assert probe.execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0] == 0
        assert probe.execute("SELECT COUNT(*) FROM relations WHERE id=?", (relation.id,)).fetchone()[0] == 1
