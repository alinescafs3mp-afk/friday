from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage import _core as storage_core
from friday.storage._obsidian import (
    _OBSIDIAN_OPERATIONS_TABLE_SCHEMA_35,
    _OBSIDIAN_SCHEMA_36_TABLES,
    _canonical_schema_objects,
    upgrade_obsidian_schema_35_to_36,
)


def _bundle(storage: FridayStorage, owner: str) -> dict[str, dict]:
    storage.ensure_user(owner)
    return storage.create_obsidian_bundle(
        owner,
        config_root=f"/private/profiles/{owner}",
        database_root=f"/private/data/{owner}",
        api_endpoint=f"unix:///private/run/{owner}.sock",
        api_key_ref=f"secret:obsidian:{owner}",
        server_path=f"/private/vaults/{owner}",
        folder_id=f"friday-{owner}",
        setup_token_hash=hashlib.sha256(f"token:{owner}".encode()).hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )


def _age_current_database_to_released_schema_35(database: Path) -> None:
    canonical_35 = _canonical_schema_objects(35)
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "obsidian_active_frames",
            "obsidian_candidate_set_items",
            "obsidian_candidate_sets",
            "obsidian_note_links",
            "obsidian_note_index",
            "obsidian_note_bindings",
        ):
            conn.execute(f'DROP TABLE "{table}"')  # nosec B608 - fixed schema-36 list
        conn.execute("DROP INDEX idx_obsidian_operations_user_time")
        conn.execute("DROP INDEX idx_obsidian_operations_delivery")
        conn.execute("ALTER TABLE obsidian_operations RENAME TO obsidian_operations_schema36")
        conn.execute(_OBSIDIAN_OPERATIONS_TABLE_SCHEMA_35)
        conn.execute(
            """INSERT INTO obsidian_operations(
                   id, user_id, work_item_id, vault_id, method, arguments_digest,
                   expected_revision, status, result_json, delivery_json,
                   created_at, updated_at
               )
               SELECT id, user_id, work_item_id, vault_id, method, arguments_digest,
                      expected_revision, status, result_json, delivery_json,
                      created_at, updated_at
                 FROM obsidian_operations_schema36"""
        )
        conn.execute("DROP TABLE obsidian_operations_schema36")
        conn.execute(canonical_35[("index", "idx_obsidian_operations_user_time")])
        conn.execute(canonical_35[("index", "idx_obsidian_operations_delivery")])
        conn.execute("UPDATE schema_meta SET value='35' WHERE key IN ('schema_version', 'fts_build')")


def test_schema_36_installs_the_revision_graph_and_extended_operation_contract(storage) -> None:
    assert SCHEMA_VERSION == 39
    marker = storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    assert marker[0] == "39"
    tables = {
        str(row[0])
        for row in storage.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'obsidian_%'"
        )
    }
    assert tables >= _OBSIDIAN_SCHEMA_36_TABLES
    operation_sql = str(
        storage.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='obsidian_operations'"
        ).fetchone()[0]
    )
    for method in (
        "prepend",
        "replace",
        "move",
        "delete",
        "template",
        "task",
        "base",
        "conflict_merge",
    ):
        assert f"'{method}'" in operation_sql


def test_released_schema_35_migrates_atomically_and_preserves_operation_rows(settings, tmp_path) -> None:
    database = tmp_path / "obsidian-schema35.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    aggregate = _bundle(initial, "alice")
    digest = hashlib.sha256(b"released operation").hexdigest()
    initial.prepare_obsidian_operation(
        "alice",
        operation_id="released-schema35-operation",
        vault_id=aggregate["vault"]["id"],
        method="append",
        arguments_digest=digest,
        expected_revision="a" * 64,
    )
    initial.close()
    _age_current_database_to_released_schema_35(database)

    upgraded = FridayStorage(replace(settings, database_path=database))
    try:
        assert (
            upgraded.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "39"
        )
        preserved = upgraded.get_obsidian_operation("alice", "released-schema35-operation")
        assert preserved is not None
        assert preserved["method"] == "append"
        for method in (
            "prepend",
            "replace",
            "move",
            "delete",
            "template",
            "task",
            "base",
            "conflict_merge",
        ):
            row, created = upgraded.prepare_obsidian_operation(
                "alice",
                operation_id=f"schema36-{method}",
                vault_id=aggregate["vault"]["id"],
                method=method,
                arguments_digest=hashlib.sha256(method.encode()).hexdigest(),
            )
            assert created is True and row["method"] == method
    finally:
        upgraded.close()


def test_schema_35_tamper_is_rejected_before_any_schema_36_ddl(settings, tmp_path) -> None:
    database = tmp_path / "obsidian-schema35-tamper.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    _bundle(initial, "alice")
    initial.close()
    _age_current_database_to_released_schema_35(database)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP INDEX uq_obsidian_profile_user")

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 35 Obsidian state"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()
    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "35"
        installed = {
            str(row[0])
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'obsidian_%'"
            )
        }
        assert installed.isdisjoint(_OBSIDIAN_SCHEMA_36_TABLES)


def test_schema_35_to_36_failure_rolls_back_rebuild_rows_tables_and_marker(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "obsidian-schema35-rollback.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    aggregate = _bundle(initial, "alice")
    initial.prepare_obsidian_operation(
        "alice",
        operation_id="must-survive-rollback",
        vault_id=aggregate["vault"]["id"],
        method="append",
        arguments_digest=hashlib.sha256(b"must-survive-rollback").hexdigest(),
    )
    initial.close()
    _age_current_database_to_released_schema_35(database)

    def fail_after_rebuild(conn: sqlite3.Connection) -> None:
        upgrade_obsidian_schema_35_to_36(conn)
        raise RuntimeError("injected schema-36 failure")

    monkeypatch.setattr(storage_core, "upgrade_obsidian_schema_35_to_36", fail_after_rebuild)
    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(RuntimeError, match="injected schema-36 failure"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()

    with sqlite3.connect(database) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "35"
        assert probe.execute(
            "SELECT method FROM obsidian_operations WHERE id='must-survive-rollback'"
        ).fetchone() == ("append",)
        installed = {
            str(row[0])
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'obsidian_%'"
            )
        }
        assert installed.isdisjoint(_OBSIDIAN_SCHEMA_36_TABLES)
        operation_sql = str(
            probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='obsidian_operations'"
            ).fetchone()[0]
        )
        assert operation_sql == _OBSIDIAN_OPERATIONS_TABLE_SCHEMA_35


def test_current_schema_rejects_a_missing_schema_36_selection_guard(settings, tmp_path) -> None:
    database = tmp_path / "obsidian-schema36-tamper.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    initial.execute("SELECT 1")
    initial.close()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TRIGGER obsidian_candidate_selection_update_guard")

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 36 Obsidian state"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()


def test_current_schema_rejects_an_offline_cross_owner_note_projection(settings, tmp_path) -> None:
    database = tmp_path / "obsidian-schema36-owner-tamper.sqlite3"
    initial = FridayStorage(replace(settings, database_path=database))
    alice = _bundle(initial, "alice")
    bob = _bundle(initial, "bob")
    bob_binding = initial.upsert_obsidian_note_binding(
        "bob",
        vault_id=bob["vault"]["id"],
        integration_id="obnote-cross-owner",
        current_path="Cross Owner.md",
        current_revision="9" * 64,
        origin="user",
    )
    initial.close()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """INSERT INTO obsidian_note_index(
                   user_id, binding_id, vault_id, revision, path, title,
                   metadata_json, metadata_coverage, body_text, body_coverage,
                   source_size_bytes, state, indexed_at, updated_at
               ) VALUES('alice', ?, ?, ?, 'Cross Owner.md', '', '{}', 'none',
                        '', 'none', 0, 'ready', ?, ?)""",
            (
                bob_binding["id"],
                alice["vault"]["id"],
                "9" * 64,
                "2026-08-22T10:00:00+00:00",
                "2026-08-22T10:00:00+00:00",
            ),
        )

    rejected = FridayStorage(replace(settings, database_path=database))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="violates owner foreign keys"):
            rejected.execute("SELECT 1")
    finally:
        rejected.close()


def test_bindings_index_links_and_continuation_state_fail_closed_when_revision_moves(
    storage: FridayStorage,
) -> None:
    alice = _bundle(storage, "alice")
    bob = _bundle(storage, "bob")
    vault_id = alice["vault"]["id"]
    with pytest.raises(ValueError, match="not found for expected_current_revision"):
        storage.upsert_obsidian_note_binding(
            "alice",
            vault_id=vault_id,
            integration_id="obnote-missing-cas",
            current_path="Missing.md",
            current_revision="0" * 64,
            origin="user",
            expected_current_revision="1" * 64,
        )
    assert storage.get_obsidian_note_binding("alice", "obnote-missing-cas") is None
    source = storage.upsert_obsidian_note_binding(
        "alice",
        vault_id=vault_id,
        integration_id="obnote-source",
        current_path="Notes/Source.md",
        current_revision="a" * 64,
        ownership_mode="linked",
        origin="friday",
        projection_kind="linked",
        projection={"managed_regions": ["summary"]},
        friday_object_kind="knowledge_object",
        friday_object_id="ko-source",
    )
    target = storage.upsert_obsidian_note_binding(
        "alice",
        vault_id=vault_id,
        integration_id="obnote-target",
        current_path="Projects/Friday.md",
        current_revision="b" * 64,
        origin="android",
    )
    storage.upsert_obsidian_note_index(
        "alice",
        binding_id=target["id"],
        revision="b" * 64,
        metadata={"tags": ["friday"]},
        body_text="Friday retrieval",
        title="Friday",
        source_modified_at="2026-08-22T10:00:00+00:00",
    )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        storage.invalidate_obsidian_note_index("alice", target["id"], expected_revision="")
    links = storage.replace_obsidian_note_links(
        "alice",
        binding_id=source["id"],
        revision="a" * 64,
        links=[
            {
                "kind": "wikilink",
                "target_text": "Projects/Friday",
                "target_path": "Projects/Friday.md",
                "resolved_binding_id": target["id"],
                "metadata": {"embed": False},
            }
        ],
    )
    assert links[0]["resolution_state"] == "resolved"
    candidate_set = storage.create_obsidian_candidate_set(
        "alice",
        vault_id=vault_id,
        query={"text": "Friday retrieval"},
        candidates=[
            {
                "binding_id": target["id"],
                "revision": "b" * 64,
                "path": "Projects/Friday.md",
                "title": "Friday",
                "score": 0.9,
                "match_channels": ["lexical", "graph"],
                "excerpt": "Friday retrieval",
            },
            {
                "binding_id": source["id"],
                "revision": "a" * 64,
                "path": "Notes/Source.md",
                "title": "Source",
                "score": 0.1,
                "match_channels": ["graph"],
            },
        ],
        coverage={"body": "complete"},
        work_item_id="work-1",
        expires_at="2030-01-01T00:00:00+00:00",
    )
    operation, _ = storage.prepare_obsidian_operation(
        "alice",
        operation_id="frame-operation",
        work_item_id="work-1",
        vault_id=vault_id,
        method="append",
        arguments_digest=hashlib.sha256(b"frame-operation").hexdigest(),
    )
    frame = storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=vault_id,
        frame_id="frame-1",
        work_item_id="work-1",
        active_binding_id=source["id"],
        candidate_set_id=candidate_set["id"],
        last_operation_id=operation["id"],
        frame={"active_heading": "Retrieval"},
        expires_at="2030-01-01T00:00:00+00:00",
    )
    assert frame["selected_binding_id"] is None
    selected = storage.select_obsidian_candidate("alice", candidate_set["id"], 1)
    assert selected["binding_id"] == target["id"]
    assert storage.get_obsidian_active_frame("alice", "frame-1")["selected_binding_id"] == target["id"]
    with pytest.raises(ValueError, match="does not match the candidate set selection"):
        storage.upsert_obsidian_active_frame(
            "alice",
            vault_id=vault_id,
            frame_id="mismatched-selection-frame",
            candidate_set_id=candidate_set["id"],
            selected_binding_id=source["id"],
            expires_at="2030-01-01T00:00:00+00:00",
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
        storage.execute(
            """INSERT INTO obsidian_candidate_set_items(
                   user_id, candidate_set_id, vault_id, ordinal, binding_id,
                   observed_revision, observed_path, title, score,
                   match_channels_json, candidate_json, created_at
               ) SELECT user_id, candidate_set_id, vault_id, 3, binding_id,
                        observed_revision, observed_path, title, score,
                        match_channels_json, candidate_json, created_at
                   FROM obsidian_candidate_set_items
                  WHERE user_id=? AND candidate_set_id=? AND ordinal=1""",
            ("alice", candidate_set["id"]),
        )

    moved = storage.upsert_obsidian_note_binding(
        "alice",
        vault_id=vault_id,
        integration_id="obnote-target",
        current_path="Architecture/Friday.md",
        current_revision="c" * 64,
        origin="android",
        expected_current_revision="b" * 64,
    )
    assert moved["id"] == target["id"], "move changed the stable integration identity"
    assert storage.get_obsidian_note_index("alice", target["id"]) is None
    assert storage.get_obsidian_note_index("alice", target["id"], include_stale=True)["state"] == "stale"
    assert storage.get_obsidian_candidate_set("alice", candidate_set["id"]) is None
    assert (
        storage.get_obsidian_candidate_set("alice", candidate_set["id"], include_inactive=True)["status"]
        == "invalidated"
    )
    assert storage.get_obsidian_active_frame("alice", "frame-1") is None
    assert (
        storage.get_obsidian_active_frame("alice", "frame-1", include_inactive=True)["state"] == "invalidated"
    )

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        storage.tombstone_obsidian_note_binding(
            "alice",
            "obnote-target",
            expected_revision="",
        )
    tombstone = storage.tombstone_obsidian_note_binding(
        "alice",
        "obnote-target",
        expected_revision="c" * 64,
    )
    assert tombstone["deleted_at"] is not None
    unresolved = storage.list_obsidian_note_links("alice", resolution_state="unresolved")
    assert unresolved[0]["resolved_binding_id"] is None
    assert storage.get_obsidian_note_binding("alice", "obnote-target") is None
    assert storage.get_obsidian_note_binding("bob", "obnote-target") is None
    bob_binding = storage.upsert_obsidian_note_binding(
        "bob",
        vault_id=bob["vault"]["id"],
        integration_id="obnote-target",
        current_path="Projects/Friday.md",
        current_revision="d" * 64,
        origin="user",
    )
    assert bob_binding["id"] != target["id"]
    assert storage.list_obsidian_note_bindings("alice", limit=5000) == [source]
    with pytest.raises(ValueError, match="between 1 and 5000"):
        storage.list_obsidian_note_bindings("alice", limit=5001)
    assert storage.execute("PRAGMA foreign_key_check").fetchall() == []
    assert storage.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_candidate_and_frame_ttl_are_durable_and_json_fk_guards_reject_tampering(
    storage: FridayStorage,
) -> None:
    alice = _bundle(storage, "alice")
    bob = _bundle(storage, "bob")
    binding = storage.upsert_obsidian_note_binding(
        "alice",
        vault_id=alice["vault"]["id"],
        integration_id="obnote-expiring",
        current_path="Expiring.md",
        current_revision="e" * 64,
        origin="user",
    )
    candidate_set = storage.create_obsidian_candidate_set(
        "alice",
        vault_id=alice["vault"]["id"],
        query={"text": "expiring"},
        candidates=[{"binding_id": binding["id"], "match_channels": []}],
        now="2026-08-22T10:00:00+00:00",
        expires_at="2026-08-22T10:01:00+00:00",
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=alice["vault"]["id"],
        frame_id="expiring-frame",
        candidate_set_id=candidate_set["id"],
        now="2026-08-22T10:00:00+00:00",
        expires_at="2026-08-22T10:01:00+00:00",
    )
    with pytest.raises(ValueError, match="candidate set expired"):
        storage.select_obsidian_candidate(
            "alice",
            candidate_set["id"],
            1,
            now="2026-08-22T10:02:00+00:00",
        )
    assert (
        storage.execute(
            "SELECT status FROM obsidian_candidate_sets WHERE user_id=? AND id=?",
            ("alice", candidate_set["id"]),
        ).fetchone()[0]
        == "expired"
    )
    assert (
        storage.execute(
            "SELECT state FROM obsidian_active_frames WHERE user_id=? AND id=?",
            ("alice", "expiring-frame"),
        ).fetchone()[0]
        == "invalidated"
    )
    assert (
        storage.get_obsidian_active_frame("alice", "expiring-frame", now="2026-08-22T10:02:00+00:00") is None
    )

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        storage.execute(
            "UPDATE obsidian_note_bindings SET projection_json='[]' WHERE user_id=? AND id=?",
            ("alice", binding["id"]),
        )
    bob_binding = storage.upsert_obsidian_note_binding(
        "bob",
        vault_id=bob["vault"]["id"],
        integration_id="obnote-bob",
        current_path="Bob.md",
        current_revision="f" * 64,
        origin="user",
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        storage.execute(
            """INSERT INTO obsidian_note_index(
                   user_id, binding_id, vault_id, revision, path, title,
                   metadata_json, metadata_coverage, body_text, body_coverage,
                   source_size_bytes, state, indexed_at, updated_at
               ) VALUES(?, ?, ?, ?, 'Cross.md', '', '{}', 'none', '', 'none',
                        0, 'ready', ?, ?)""",
            (
                "alice",
                bob_binding["id"],
                alice["vault"]["id"],
                "f" * 64,
                "2026-08-22T10:00:00+00:00",
                "2026-08-22T10:00:00+00:00",
            ),
        )
    with pytest.raises(ValueError, match="cannot exceed 100"):
        storage.create_obsidian_candidate_set(
            "alice",
            vault_id=alice["vault"]["id"],
            query={},
            candidates=[{"binding_id": binding["id"]}] * 101,
        )


def test_user_export_includes_only_the_owners_schema_36_obsidian_projection(storage) -> None:
    alice = _bundle(storage, "alice")
    _bundle(storage, "bob")
    binding = storage.upsert_obsidian_note_binding(
        "alice",
        vault_id=alice["vault"]["id"],
        integration_id="obnote-export",
        current_path="Export.md",
        current_revision="1" * 64,
        origin="user",
    )
    storage.upsert_obsidian_note_index(
        "alice",
        binding_id=binding["id"],
        revision="1" * 64,
        metadata={},
        body_text="bounded export body",
    )
    candidate_set = storage.create_obsidian_candidate_set(
        "alice",
        vault_id=alice["vault"]["id"],
        query={"text": "export"},
        candidates=[{"binding_id": binding["id"]}],
        expires_at="2030-01-01T00:00:00+00:00",
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=alice["vault"]["id"],
        frame_id="export-frame",
        candidate_set_id=candidate_set["id"],
        expires_at="2030-01-01T00:00:00+00:00",
    )

    exported = storage.export_user("alice")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    for table in (
        "obsidian_note_bindings",
        "obsidian_note_index",
        "obsidian_candidate_sets",
        "obsidian_candidate_set_items",
        "obsidian_active_frames",
    ):
        assert payload[table]
        assert {row["user_id"] for row in payload[table]} == {"alice"}
