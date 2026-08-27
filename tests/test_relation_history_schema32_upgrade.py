"""The deployed schema-31 relation authority upgrades to schema 32 atomically.

The fixture is synthetic, but its relation-history DDL is the exact early schema-31
contract observed in the verified pre-migration backup.  These tests keep that one
known predecessor openable without weakening fail-closed handling for any other
same-number shape.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.interaction_control_plane.work_item_schema import _WORK_ITEM_TABLES
from friday.storage import SCHEMA_VERSION, FridayStorage, UnsupportedSchemaVersionError
from friday.storage import _core as storage_core
from friday.storage.models import normalize_known_at

FIXTURE = Path(__file__).parent / "fixtures" / "schemas" / "schema-31.sqlite3.gz"
LEGACY_GUARD = "relations_revision_ai"


def _unpack_schema_31(tmp_path: Path, name: str) -> Path:
    database = tmp_path / f"{name}.sqlite3"
    with gzip.open(FIXTURE, "rb") as source, database.open("wb") as destination:
        shutil.copyfileobj(source, destination)
    return database


def _make_schema_32(settings: Any, tmp_path: Path, name: str) -> Path:
    """Build the exact schema-32 predecessor of the file-alias migration.

    A fresh ``FridayStorage`` now creates schema 36.  Tests which labelled that
    database "v32" were no longer exercising the predecessor at all and then
    expected the wrong version in fail-closed diagnostics.  Schema 33 adds only
    the immutable transport-alias table/index, so remove those two artifacts
    and restore both completion markers to obtain the released v32 shape.
    """

    database = tmp_path / f"{name}.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    made.execute("SELECT 1")
    made.close(final=True)
    with sqlite3.connect(database) as predecessor:
        predecessor.execute("DROP TRIGGER document_catalog_raw_ai_seed")
        predecessor.execute("DROP TRIGGER document_catalog_raw_au_reconcile")
        predecessor.execute("DROP TRIGGER document_catalog_raw_au_extraction_state")
        predecessor.execute("DROP TABLE document_catalog")
        # The catalog's owner keyset index lives on raw_objects, so dropping the
        # sidecar table cannot remove it.  A synthetic v32 predecessor must not
        # retain that schema-41 authority artifact.
        predecessor.execute("DROP INDEX idx_document_catalog_source_owner_id")
        # Work Items arrived in schema 38. Removing their tables also removes
        # schema-42 triggers that reference the schema-33 alias authority.
        for table in _WORK_ITEM_TABLES:
            predecessor.execute(f'DROP TABLE "{table}"')
        predecessor.execute("DROP TABLE file_source_aliases")
        predecessor.execute("UPDATE schema_meta SET value='32' WHERE key IN ('schema_version','fts_build')")
    return database


def _weaken_relation_projection(database: Path) -> None:
    with sqlite3.connect(database) as db:
        table_sql = str(
            db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='relations'").fetchone()[0]
        )
        dependencies = [
            str(row[0])
            for row in db.execute(
                """SELECT sql FROM sqlite_master
                     WHERE tbl_name='relations'
                       AND type IN ('index', 'trigger')
                       AND sql IS NOT NULL
                     ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name"""
            ).fetchall()
        ]
        columns = ", ".join(str(row[1]) for row in db.execute("PRAGMA table_info(relations)"))
        weakened_sql = table_sql.replace(
            "CREATE TABLE relations",
            "CREATE TABLE relations_weakened",
            1,
        ).replace("CHECK(source_entity_id <> target_entity_id)", "CHECK(1)")
        assert weakened_sql != table_sql and "CHECK(1)" in weakened_sql
        db.execute(weakened_sql)
        db.execute(
            f"INSERT INTO relations_weakened({columns}) SELECT {columns} FROM relations"  # nosec B608 - PRAGMA-derived fixed schema columns
        )
        db.execute("DROP TABLE relations")
        db.execute("ALTER TABLE relations_weakened RENAME TO relations")
        for statement in dependencies:
            db.execute(statement)


def _seed_all_observed_boundary_sources(database: Path) -> str:
    """Exercise every temporal-authority arm with synthetic, non-private rows."""

    version_at = "2026-01-02T00:00:00.000000Z"
    merge_at = "2026-01-03T00:00:00.000000Z"
    undone_at = "2026-01-04T00:00:00.000000Z"
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        source = dict(db.execute("SELECT * FROM entities WHERE id='entity-fixture-source'").fetchone())
        target = dict(db.execute("SELECT * FROM entities WHERE id='entity-fixture-target'").fetchone())
        db.execute(
            """INSERT INTO entity_versions(
                   id, user_id, entity_id, version, snapshot_json, created_at
               ) VALUES(?, ?, ?, ?, ?, ?)""",
            (
                "entity-version-fixture-boundary",
                "fixture-owner",
                source["id"],
                1,
                json.dumps(source, ensure_ascii=False, separators=(",", ":")),
                version_at,
            ),
        )
        db.execute(
            """INSERT INTO entity_merge_history(
                   id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json,
                   merged_by, created_at, transfer_json, undone_at, undone_by
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "merge-fixture-boundary",
                "fixture-owner",
                source["id"],
                target["id"],
                json.dumps(source, ensure_ascii=False, separators=(",", ":")),
                json.dumps(target, ensure_ascii=False, separators=(",", ":")),
                json.dumps(target, ensure_ascii=False, separators=(",", ":")),
                "fixture-owner",
                merge_at,
                "{}",
                undone_at,
                "fixture-owner",
            ),
        )
    return undone_at


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def _digest(rows: list[tuple[Any, ...]]) -> str:
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _relation_evidence(conn: sqlite3.Connection) -> dict[str, Any]:
    revisions = _rows(conn, "SELECT * FROM relation_revisions ORDER BY event_seq")
    relations = _rows(conn, "SELECT * FROM relations ORDER BY id")
    floor = _rows(
        conn,
        """SELECT value, updated_at FROM schema_meta
             WHERE key='relation_history_complete_from'""",
    )
    sequence = _rows(
        conn,
        "SELECT seq FROM sqlite_sequence WHERE name='relation_revisions'",
    )
    return {
        "revision_count": len(revisions),
        "revision_digest": _digest(revisions),
        "relation_count": len(relations),
        "relation_digest": _digest(relations),
        "floor": floor,
        "event_sequence": sequence,
    }


def _relation_authority(conn: sqlite3.Connection) -> dict[str, Any]:
    names = sorted(
        storage_core._RELATION_HISTORY_OWNED_TABLES
        | set(storage_core._RELATION_HISTORY_TRIGGER_TABLES)
        | {"relations"}
    )
    protected_tables = set(storage_core._RELATION_HISTORY_TRIGGER_TABLES.values())
    schema = [
        row
        for row in _rows(
            conn,
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name",
        )
        if str(row[1]) in names or (str(row[0]) in {"index", "trigger"} and str(row[2]) in protected_tables)
    ]
    return {
        "marker": _rows(
            conn,
            "SELECT value FROM schema_meta WHERE key='schema_version'",
        ),
        "schema_digest": _digest(schema),
        "schema_count": len(schema),
        "context": _rows(conn, "SELECT * FROM relation_revision_context"),
        "evidence": _relation_evidence(conn),
    }


def _legacy_observed_boundary(conn: sqlite3.Connection) -> str:
    raw_boundaries = _rows(
        conn,
        """SELECT value FROM schema_meta
             WHERE key='relation_history_complete_from'
           UNION ALL
           SELECT recorded_at FROM relation_revisions
           UNION ALL
           SELECT created_at FROM entity_versions
           UNION ALL
           SELECT created_at FROM entity_merge_history
           UNION ALL
           SELECT undone_at FROM entity_merge_history WHERE undone_at IS NOT NULL""",
    )
    boundaries = [normalize_known_at(str(row[0]), reject_future=False) for row in raw_boundaries if row[0]]
    assert boundaries, "synthetic schema-31 fixture has no temporal authority"
    return max(boundaries)


def test_schema_31_to_32_preserves_evidence_and_installs_the_exact_current_contract(
    settings, tmp_path
) -> None:
    database = _unpack_schema_31(tmp_path, "preserves-evidence")
    latest_seeded_boundary = _seed_all_observed_boundary_sources(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as before_conn:
        assert _rows(
            before_conn,
            "SELECT value FROM schema_meta WHERE key='schema_version'",
        ) == [("31",)]
        before = _relation_evidence(before_conn)
        expected_observed_at = _legacy_observed_boundary(before_conn)
        assert expected_observed_at == latest_seeded_boundary

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        assert migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(SCHEMA_VERSION)
        assert SCHEMA_VERSION == 46
        assert tuple(
            migrated.execute(
                """SELECT singleton, batch_id, recorded_at, observed_at
                     FROM relation_revision_context"""
            ).fetchone()
        ) == (1, "", "", expected_observed_at)

        canonical_tables, canonical_triggers = storage_core._canonical_relation_history_schema_sql()
        installed_context = migrated.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='table' AND name='relation_revision_context'"""
        ).fetchone()[0]
        assert installed_context == canonical_tables["relation_revision_context"]
        installed_triggers = {
            str(row[0]): str(row[1])
            for row in migrated.execute(
                """SELECT name, sql FROM sqlite_master
                     WHERE type='trigger' ORDER BY name"""
            ).fetchall()
            if str(row[0]) in storage_core._RELATION_HISTORY_TRIGGER_TABLES
        }
        assert installed_triggers == canonical_triggers
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _relation_evidence(migrated.conn) == before
    finally:
        migrated.close(final=True)


@pytest.mark.parametrize("corruption", ["missing", "altered", "extra"])
def test_schema_31_rejects_a_missing_altered_or_extra_guard_without_mutation(
    settings, tmp_path, corruption
) -> None:
    database = _unpack_schema_31(tmp_path, f"rejects-{corruption}-guard")
    with sqlite3.connect(database) as corrupt:
        expected_name = LEGACY_GUARD
        if corruption == "extra":
            expected_name = "synthetic_extra_relation_history_writer"
            corrupt.execute(
                f"""CREATE TRIGGER {expected_name}
                    AFTER INSERT ON relation_revisions
                    BEGIN
                        SELECT 1;
                    END"""  # nosec B608 - fixed synthetic trigger name
            )
        else:
            trigger_sql = str(
                corrupt.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (LEGACY_GUARD,),
                ).fetchone()[0]
            )
            corrupt.execute(f'DROP TRIGGER "{LEGACY_GUARD}"')  # nosec B608 - fixed test name
            if corruption == "altered":
                header, separator, _body = trigger_sql.partition("\nBEGIN\n")
                assert separator, "legacy fixture trigger has an unexpected shape"
                corrupt.execute(f"{header}\nBEGIN\n    SELECT 1;\nEND")  # nosec B608 - synthetic DDL

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        before = _relation_authority(probe)

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        expected_pattern = (
            r"Schema 31 relation history is incomplete.*unexpected triggers: 1"
            if corruption == "extra"
            else rf"Schema 31 relation history is incomplete.*{expected_name}"
        )
        with pytest.raises(
            UnsupportedSchemaVersionError,
            match=expected_pattern,
        ) as caught:
            rejected.execute("SELECT 1")
        if corruption == "extra":
            assert expected_name not in str(caught.value)
    finally:
        rejected.close(final=True)

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert _relation_authority(probe) == before


def test_schema_31_upgrade_failure_rolls_back_marker_schema_and_evidence(
    settings, tmp_path, monkeypatch
) -> None:
    database = _unpack_schema_31(tmp_path, "rollback")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        before = _relation_authority(probe)

    real_upgrade = storage_core._upgrade_relation_history_31_to_32
    called = False

    def fail_after_upgrade(conn: sqlite3.Connection) -> None:
        nonlocal called
        real_upgrade(conn)
        called = True
        raise RuntimeError("injected schema-32 upgrade failure")

    monkeypatch.setattr(storage_core, "_upgrade_relation_history_31_to_32", fail_after_upgrade)
    failed = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(RuntimeError, match="injected schema-32 upgrade failure"):
            failed.execute("SELECT 1")
    finally:
        failed.close(final=True)
    assert called

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert _relation_authority(probe) == before

    monkeypatch.setattr(storage_core, "_upgrade_relation_history_31_to_32", real_upgrade)
    recovered = FridayStorage(replace(settings, database_path=database))
    try:
        assert recovered.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(SCHEMA_VERSION)
        assert _relation_evidence(recovered.conn) == before["evidence"]
    finally:
        recovered.close(final=True)


@pytest.mark.parametrize("source_schema", [31, 32])
def test_relation_projection_with_weakened_constraints_is_rejected_without_mutation(
    settings, tmp_path, source_schema
) -> None:
    if source_schema == 31:
        database = _unpack_schema_31(tmp_path, "weakened-relation-projection-v31")
    else:
        database = _make_schema_32(settings, tmp_path, "weakened-relation-projection-v32")
    _weaken_relation_projection(database)

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        before = _relation_authority(probe)
    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(
            UnsupportedSchemaVersionError,
            match=rf"Schema {source_schema} relation history is incomplete.*altered tables: relations",
        ):
            rejected.execute("SELECT 1")
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert _relation_authority(probe) == before


@pytest.mark.parametrize("source_schema", [31, 32])
@pytest.mark.parametrize(
    ("table", "index_definition"),
    [
        ("relations", "(weight)"),
        ("relation_revisions", "(weight)"),
        (
            "schema_meta",
            "(value) WHERE key IN ('relation_history_complete_from', 'synthetic-other')",
        ),
        ("relation_revision_context", "(recorded_at)"),
    ],
)
def test_unknown_unique_index_on_guarded_authority_is_rejected_without_mutation_or_disclosure(
    settings, tmp_path, source_schema, table, index_definition
) -> None:
    if source_schema == 31:
        database = _unpack_schema_31(tmp_path, f"extra-unique-{table}-v31")
    else:
        database = _make_schema_32(settings, tmp_path, f"extra-unique-{table}-v32")
    private_index_name = f"synthetic_private_{table}_unique"
    with sqlite3.connect(database) as corrupt:
        corrupt.execute(
            f'CREATE UNIQUE INDEX "{private_index_name}" ON "{table}"{index_definition}'  # nosec B608 - fixed synthetic parameter matrix
        )

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        before = _relation_authority(probe)
    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(
            UnsupportedSchemaVersionError,
            match=rf"Schema {source_schema} relation history is incomplete.*unexpected unique indexes: 1",
        ) as caught:
            rejected.execute("SELECT 1")
        assert private_index_name not in str(caught.value)
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert _relation_authority(probe) == before


@pytest.mark.parametrize("source_schema", [31, 32])
@pytest.mark.parametrize("corruption", ["missing", "altered"])
def test_active_relation_unique_contract_is_exact_and_fail_closed(
    settings, tmp_path, source_schema, corruption
) -> None:
    if source_schema == 31:
        database = _unpack_schema_31(tmp_path, f"{corruption}-active-unique-v31")
    else:
        database = _make_schema_32(settings, tmp_path, f"{corruption}-active-unique-v32")
    with sqlite3.connect(database) as corrupt:
        corrupt.execute("DROP INDEX uq_active_relation")
        if corruption == "altered":
            corrupt.execute("CREATE UNIQUE INDEX uq_active_relation ON relations(weight)")

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        before = _relation_authority(probe)
    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(
            UnsupportedSchemaVersionError,
            match=rf"Schema {source_schema} relation history is incomplete.*{corruption} unique indexes: uq_active_relation",
        ):
            rejected.execute("SELECT 1")
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert _relation_authority(probe) == before


def test_schema_32_rejects_an_unknown_relation_authority_trigger_without_mutation(settings, tmp_path) -> None:
    database = _make_schema_32(settings, tmp_path, "schema-32-extra-trigger")

    trigger_name = "synthetic_extra_relation_history_writer"
    with sqlite3.connect(database) as corrupt:
        corrupt.execute(
            f"""CREATE TRIGGER {trigger_name}
                AFTER INSERT ON relation_revisions
                BEGIN
                    SELECT 1;
                END"""  # nosec B608 - fixed synthetic trigger name
        )
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        before = _relation_authority(probe)

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(
            UnsupportedSchemaVersionError,
            match=r"Schema 32 relation history is incomplete.*unexpected triggers: 1",
        ) as caught:
            rejected.execute("SELECT 1")
        assert trigger_name not in str(caught.value)
    finally:
        rejected.close(final=True)

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert _relation_authority(probe) == before


def test_schema_32_to_33_adds_file_alias_authority_without_rewriting_raw_objects(settings, tmp_path) -> None:
    database = _make_schema_32(settings, tmp_path, "schema-32-file-alias-upgrade")
    with sqlite3.connect(database) as predecessor:
        predecessor.execute(
            """INSERT INTO users(id, display_name, created_at, updated_at, last_seen_at)
               VALUES(
                   'schema32-owner','Schema owner','2026-01-01','2026-01-01','2026-01-01'
               )"""
        )
        predecessor.execute(
            """INSERT INTO raw_objects(
                   id,user_id,content_type,source,source_ref,raw_content,metadata_json,
                   content_hash,received_at,created_at,deleted_at
               ) VALUES(
                   'raw_schema32_file','schema32-owner','file','upload',
                   'telegram-file:legacy','[File: legacy.odt]',
                   '{"filename":"legacy.odt","uploaded_by":"schema32-owner"}',
                   'schema32-hash','2026-01-01','2026-01-01',NULL
               )"""
        )
        before = tuple(
            predecessor.execute(
                "SELECT id,source_ref,raw_content,metadata_json,content_hash FROM raw_objects"
            ).fetchone()
        )

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        assert migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(SCHEMA_VERSION)
        assert (
            migrated.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_source_aliases'"
            ).fetchone()
            is not None
        )
        after = tuple(
            migrated.execute(
                "SELECT id,source_ref,raw_content,metadata_json,content_hash FROM raw_objects"
            ).fetchone()
        )
        assert after == before
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close(final=True)
