from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.memory import MemoryVault
from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage.models import KnowledgeObject, RawObject, new_id


def make_knowledge(
    storage, user_id: str, text: str, *, importance: float = 0.5, updated_at: str | None = None
):
    storage.ensure_user(user_id)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        title=text[:50],
        summary=text,
        importance=importance,
        updated_at=updated_at or raw.created_at,
    )
    storage.store_knowledge_object(ko)
    return raw, ko


def test_provenance_tenant_isolation_versions_and_soft_delete(storage):
    raw, ko = make_knowledge(storage, "alice", "Project Alpha launches in September")
    storage.ensure_user("bob")

    assert storage.get_raw_object(raw.id, "bob") is None
    assert storage.get_knowledge_object(ko.id, "bob") is None
    assert storage.search_knowledge("bob", "Alpha") == []

    with pytest.raises(ValueError, match="RawObject"):
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="bob",
                raw_object_id=raw.id,
                content="cross tenant",
            )
        )

    updated = storage.update_knowledge_fields(ko.id, "alice", title="Alpha launch plan")
    assert updated and updated["version"] == 2
    assert len(storage.list_knowledge_versions(ko.id, "alice")) == 2

    assert storage.soft_delete_knowledge_object(ko.id, "alice") is True
    deleted = storage.get_knowledge_object(ko.id, "alice")
    assert deleted and deleted["lifecycle_stage"] == "deleted"
    assert deleted["version"] == 3
    assert len(storage.list_knowledge_versions(ko.id, "alice")) == 3
    assert storage.count_knowledge_objects("alice") == 0


def test_stale_lifecycle_transition_is_versioned(storage):
    """Rewritten, not deleted: it pinned the UNPROTECTED mass archive.

    `deprecate_stale_knowledge` swept every active object under `importance < 0.3`
    older than the threshold — no selection, and none of the protections
    `list_lifecycle_candidates` applies. DATA_LIFECYCLE §5 says lifecycle changes
    apply only to explicitly selected objects, so the archive now takes ids. What
    this test was really about — the transition is versioned — still holds.
    """
    _, ko = make_knowledge(
        storage,
        "alice",
        "Old low-value note",
        importance=0.1,
        updated_at="2020-01-01T00:00:00+00:00",
    )
    result = storage.archive_selected_knowledge("alice", [ko.id], days_threshold=90)
    archived = storage.get_knowledge_object(ko.id, "alice")
    assert result["archived"] == [ko.id]
    assert archived and archived["lifecycle_stage"] == "archived"
    assert archived["version"] == 2
    assert len(storage.list_knowledge_versions(ko.id, "alice")) == 2


def test_a_protected_object_is_reported_not_archived(storage):
    """The reviewer selected it, so "nothing happened" is not an acceptable answer."""
    raw, ko = make_knowledge(
        storage,
        "alice",
        "Скан договора",
        importance=0.1,
        updated_at="2020-01-01T00:00:00+00:00",
    )
    # File-derived knowledge is protected from automated archiving.
    storage.update_knowledge_fields(ko.id, "alice", content_type="file")

    result = storage.archive_selected_knowledge("alice", [ko.id], days_threshold=90)

    assert result["archived"] == []
    assert result["skipped"] and result["skipped"][0]["id"] == ko.id
    assert "file-derived" in result["skipped"][0]["reason"]
    assert storage.get_knowledge_object(ko.id, "alice")["lifecycle_stage"] == "active"
    del raw


def test_online_backup_and_manifest_are_verifiable(storage):
    make_knowledge(storage, "alice", "Backup me")
    result = storage.create_backup(label="pytest")
    assert result["integrity_check"] == "ok"
    assert len(result["sha256"]) == 64
    assert result["scope"]["sqlite_database"] == "included"
    assert result["scope"]["raw_files"] == "external"
    assert result["scope"]["obsidian_profiles_and_vaults"] == "external"
    assert result["scope"]["engineer_command_ledger"] == "external"
    verification = storage.verify_backup(result["database"])
    assert verification["ok"] is True
    assert verification["sha256"] == result["sha256"]
    assert verification["manifest_present"] is True
    assert verification["hash_matches_manifest"] is True
    listed = storage.list_backups()
    assert listed and listed[0]["database"] == result["database"]


def test_memory_vault_is_windows_safe_and_round_trips(settings):
    vault = MemoryVault(settings.memory_vault_dir)
    record = {
        "id": "ko:unsafe/id",
        "user_id": "telegram:telegram:123456",
        "title": "Title: with newline\nand quote ' safely encoded",
        "summary": "summary",
        "content": "knowledge body",
        "tags_json": json.dumps(["alpha", "beta"]),
        "importance": 0.7,
        "lifecycle_stage": "active",
        "version": 2,
        "raw_object_id": "raw_1",
    }
    path = vault.sync_object(record)
    assert path is not None and path.is_file()
    assert ":" not in path.parent.name
    assert "/" not in path.name
    notes = vault.read_vault(record["user_id"])
    assert len(notes) == 1
    assert notes[0]["user_id"] == record["user_id"]
    assert notes[0]["title"] == record["title"]
    vault.delete_object(record["id"], record["user_id"])
    assert not path.exists()


def test_pre_release_database_is_migrated_without_losing_tenant_data(settings, tmp_path):
    database = tmp_path / "pre-release.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE raw_objects (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source TEXT NOT NULL,
            source_ref TEXT NOT NULL, raw_content TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT 'text', metadata_json TEXT NOT NULL DEFAULT '{}',
            received_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE knowledge_objects (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, raw_object_id TEXT,
            entity_id TEXT, content TEXT NOT NULL DEFAULT '', content_type TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]', importance REAL NOT NULL DEFAULT 0.5,
            lifecycle_stage TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
            superseded_by_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE knowledge_object_versions (
            id TEXT PRIMARY KEY, knowledge_object_id TEXT NOT NULL, version INTEGER NOT NULL,
            content TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
            importance REAL NOT NULL DEFAULT 0.5, created_at TEXT NOT NULL
        );
        CREATE TABLE inbox (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, raw_object_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', suggested_entity_id TEXT,
            suggested_tags_json TEXT NOT NULL DEFAULT '[]', classification_notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, reviewed_at TEXT, reviewed_by TEXT
        );
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'other', aliases_json TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
            canonical INTEGER NOT NULL DEFAULT 1, merged_into_id TEXT,
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE entity_resolution_candidates (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, entity_a_id TEXT NOT NULL,
            entity_b_id TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
            resolution_method TEXT NOT NULL DEFAULT 'exact_name', status TEXT NOT NULL DEFAULT 'suggested',
            resolved_by TEXT, created_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE TABLE audit_log (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, action TEXT NOT NULL,
            target_type TEXT NOT NULL, target_id TEXT, before_json TEXT, after_json TEXT,
            ip_address TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO raw_objects VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("raw-old", "legacy-user", "test", "source-old", "Legacy body", "text", "{}", now, now),
    )
    conn.execute(
        "INSERT INTO entities VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ent-a", "legacy-user", "Project Alpha", "project", "[]", "", "{}", 1, None, 1, now, now, None),
    )
    conn.execute(
        "INSERT INTO entities VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ent-b", "legacy-user", "Alpha Project", "project", "[]", "", "{}", 1, None, 1, now, now, None),
    )
    conn.execute(
        "INSERT INTO knowledge_objects VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ko-old",
            "legacy-user",
            "raw-old",
            "ent-a",
            "Legacy body",
            "text",
            "Legacy",
            "Legacy summary",
            "[]",
            0.5,
            "active",
            1,
            None,
            now,
            now,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO knowledge_object_versions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("kov-old", "ko-old", 1, "Legacy body", "Legacy", "Legacy summary", "[]", 0.5, now),
    )
    conn.execute(
        "INSERT INTO inbox VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("inbox-old", "legacy-user", "raw-old", "pending", None, "[]", "", now, None, None),
    )
    conn.execute(
        "INSERT INTO entity_resolution_candidates VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("erc-old", "legacy-user", "ent-a", "ent-b", 0.8, "name", "suggested", None, now, None),
    )
    conn.execute(
        "INSERT INTO audit_log VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("audit-old", "legacy-user", "legacy", "object", "ko-old", None, None, "", now),
    )
    conn.commit()
    conn.close()

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            int(migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            == SCHEMA_VERSION
        )
        assert migrated.get_user("legacy-user") is not None
        assert migrated.get_raw_object("raw-old", "legacy-user")["content_hash"]
        assert migrated.get_entity("ent-a", "legacy-user")["normalized_name"] == "project alpha"
        assert migrated.get_inbox_item("inbox-old", "legacy-user")["knowledge_object_id"] == "ko-old"
        assert (
            migrated.list_knowledge_entity_links("legacy-user", knowledge_object_id="ko-old")[0]["entity_id"]
            == "ent-a"
        )
        candidate = migrated.list_resolution_candidates("legacy-user")[0]
        assert candidate["pair_key"] == "ent-a|ent-b"
        versions = migrated.list_knowledge_versions("ko-old", "legacy-user")
        assert len(versions) == 1 and json.loads(versions[0]["snapshot_json"])["user_id"] == "legacy-user"
        updated = migrated.update_knowledge_fields("ko-old", "legacy-user", importance=0.8)
        assert updated and updated["version"] == 2
        assert len(migrated.list_knowledge_versions("ko-old", "legacy-user")) == 2
    finally:
        migrated.close()


def test_verified_backup_restore_is_atomic_and_creates_safety_copy(storage):
    from friday.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("before-restore")
    created = storage.create_backup(label="restore-source")
    storage.ensure_user("after-backup")
    assert storage.get_user("after-backup") is not None

    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        restored = storage.restore_backup(created["database"], safety_label="pytest-pre-restore")

    assert restored["ok"] is True
    assert restored["integrity_check"] == "ok"
    assert restored["foreign_key_violations"] == 0
    assert restored["scope"]["sqlite_database"] == "restored"
    assert restored["scope"]["raw_files"] == "unchanged"
    assert restored["scope"]["obsidian_profiles_and_vaults"] == "unchanged"
    assert restored["scope"]["engineer_command_ledger"] == "unchanged"
    assert storage.get_user("before-restore") is not None
    assert storage.get_user("after-backup") is None
    safety = restored["safety_backup"]
    assert safety and storage.verify_backup(safety["database"])["ok"] is True


def test_restore_requires_exclusive_backend_process_lease(storage):
    created = storage.create_backup(label="lease-required")

    with pytest.raises(RuntimeError, match="exclusive backend process lease"):
        storage.restore_backup(created["database"])


def test_restore_refuses_tampered_backup_without_touching_active_database(storage):
    from friday.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("active-user")
    created = storage.create_backup(label="tampered-restore")
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="Refusing to restore unverified backup"),
    ):
        storage.restore_backup(created["database"])

    assert storage.get_user("active-user") is not None
    assert storage.diagnostics()["ok"] is True


def test_verify_backup_rejects_path_components(storage):
    created = storage.create_backup(label="strict-filename")

    with pytest.raises(FileNotFoundError, match="path components"):
        storage.verify_backup(f"../{created['database']}")
    with pytest.raises(FileNotFoundError, match="path components"):
        storage.verify_backup(f"subdir\\{created['database']}")


def test_restore_refuses_symlink_database_path_before_safety_backup(storage):
    from friday.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("symlink-victim")
    created = storage.create_backup(label="symlink-restore")
    active_path = storage.settings.database_path
    victim_path = active_path.with_name("victim.sqlite3")
    storage.close()
    os.replace(active_path, victim_path)
    victim_before = victim_path.read_bytes()
    active_path.symlink_to(victim_path)
    try:
        with (
            ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
            pytest.raises(RuntimeError, match="must not be symlinks"),
        ):
            storage.restore_backup(created["database"])
        assert active_path.is_symlink()
        assert victim_path.read_bytes() == victim_before
    finally:
        active_path.unlink(missing_ok=True)
        os.replace(victim_path, active_path)
        assert storage.diagnostics()["ok"] is True


def test_restore_intent_symlink_blocks_database_creation(settings, tmp_path):
    from friday.storage._restore_barrier import DatabaseRestorePendingError

    state_dir = tmp_path / "restore-intent-symlink-state"
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "foreign-restore-intent.json"
    target.write_text("{}", encoding="utf-8")
    (state_dir / "database-restore.intent.json").symlink_to(target)
    configured = replace(
        settings,
        state_dir=state_dir,
        database_path=state_dir / "must-not-be-created.sqlite3",
        database_must_exist=False,
    )
    unopened = FridayStorage(configured)
    try:
        with pytest.raises(DatabaseRestorePendingError, match="recovery is pending"):
            unopened.ensure_user("must-not-exist")
    finally:
        unopened.close()
    assert not configured.database_path.exists()


def test_ambiguous_restore_intent_lstat_blocks_database_creation(
    settings,
    tmp_path,
    monkeypatch,
):
    from friday.storage import _restore_barrier as barrier

    state_dir = tmp_path / "restore-intent-ambiguous-state"
    state_dir.mkdir(mode=0o700)
    configured = replace(
        settings,
        state_dir=state_dir,
        database_path=state_dir / "must-not-be-created.sqlite3",
        database_must_exist=False,
    )

    def ambiguous_lstat(_path):  # noqa: ANN001, ANN202
        raise barrier.DatabaseRestorePendingError("injected ambiguous restore intent")

    monkeypatch.setattr(barrier, "database_restore_intent_lstat", ambiguous_lstat)
    unopened = FridayStorage(configured)
    try:
        with pytest.raises(barrier.DatabaseRestorePendingError, match="ambiguous"):
            unopened.ensure_user("must-not-exist")
    finally:
        unopened.close()
    assert not configured.database_path.exists()


def test_restore_detects_backup_change_during_staging_and_rolls_back(storage, monkeypatch):
    # Patched where the name is LOOKED UP: restore lives in the maintenance mixin and
    # binds the helper at import time, so patching friday.storage would not reach it.
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("stable-before-staging-race")
    created = storage.create_backup(label="staging-race")
    storage.ensure_user("active-after-backup")
    original_stage = storage_module._stage_private_copy
    injected = False

    def tampered_stage(source, destination):
        nonlocal injected
        staged = original_stage(source, destination)
        if not injected and source.name == created["database"]:
            with staged.open("ab") as handle:
                handle.write(b"changed-after-verification")
                handle.flush()
                os.fsync(handle.fileno())
            injected = True
        return staged

    monkeypatch.setattr(storage_module, "_stage_private_copy", tampered_stage)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="Backup changed while it was staged"),
    ):
        storage.restore_backup(created["database"], safety_label="staging-race-safety")

    assert injected is True
    assert storage.get_user("stable-before-staging-race") is not None
    assert storage.get_user("active-after-backup") is not None
    assert storage.diagnostics()["ok"] is True


def test_partial_pre_restore_staging_never_mutates_any_original(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("stable-before-partial-stage")
    created = storage.create_backup(label="partial-stage")
    storage.ensure_user("must-survive-partial-stage")
    database_path = storage.settings.database_path.absolute()
    wal_path = Path(f"{database_path}-wal")
    active_paths = (database_path, wal_path, Path(f"{database_path}-shm"))
    backup_path = Path(created["path"]).resolve()
    original_stage = storage_module._stage_private_copy
    originals: dict[Path, bytes] = {}
    recovery_before = set(storage.settings.backups_dir.glob("recovery-*"))

    def fail_after_first_original(source, destination):  # noqa: ANN001, ANN202
        source_path = Path(source)
        if source_path.resolve() == backup_path:
            prepared = original_stage(source, destination)
            wal_path.write_bytes(b"exact-sidecar-before-partial-staging")
            wal_path.chmod(0o600)
            originals.update({path: path.read_bytes() for path in active_paths if path.is_file()})
            return prepared
        if source_path == wal_path:
            raise OSError("fault after the first rollback member")
        return original_stage(source, destination)

    monkeypatch.setattr(storage_module, "_stage_private_copy", fail_after_first_original)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="active database was left untouched"),
    ):
        storage.restore_backup(created["database"])

    assert originals
    assert {path: path.read_bytes() for path in active_paths if path.is_file()} == originals
    assert not (storage.settings.state_dir / "database-restore.intent.json").exists()
    assert set(storage.settings.backups_dir.glob("recovery-*")) == recovery_before
    assert not list(database_path.parent.glob("*.restore-*.tmp"))
    assert storage.get_user("must-survive-partial-stage") is not None


def test_pre_intent_failure_discards_only_its_unreferenced_recovery_bundle(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-pre-intent-cleanup")
    created = storage.create_backup(label="pre-intent-cleanup")
    storage.ensure_user("must-survive-pre-intent-cleanup")
    database_path = storage.settings.database_path.absolute()
    active_paths = (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    )
    originals = {path: path.read_bytes() for path in active_paths if path.is_file()}
    recovery_before = set(storage.settings.backups_dir.glob("recovery-*"))

    def fail_before_intent(_path, _payload):  # noqa: ANN001, ANN202
        raise OSError("injected intent write failure")

    monkeypatch.setattr(storage_module, "_write_restore_intent", fail_before_intent)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="active database was left untouched"),
    ):
        storage.restore_backup(created["database"])

    assert {path: path.read_bytes() for path in active_paths if path.is_file()} == originals
    assert not (storage.settings.state_dir / "database-restore.intent.json").exists()
    assert set(storage.settings.backups_dir.glob("recovery-*")) == recovery_before
    assert not list(database_path.parent.glob("*.restore-*.tmp"))
    assert storage.get_user("must-survive-pre-intent-cleanup") is not None


def test_durable_restore_intent_precedes_close_and_recovers_exact_bytes(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("before-close-boundary")
    created = storage.create_backup(label="close-boundary")
    storage.ensure_user("must-survive-close-boundary")
    database_path = storage.settings.database_path.absolute()
    active_paths = (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    )
    originals = {path: path.read_bytes() for path in active_paths if path.is_file()}
    recovery_before = set(storage.settings.backups_dir.glob("recovery-*"))
    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    original_close = storage.close
    close_calls = 0
    intent_preceded_close = False

    def fail_after_first_close(*, final=False):  # noqa: ANN001, ANN202
        nonlocal close_calls, intent_preceded_close
        close_calls += 1
        original_close(final=final)
        if close_calls == 1:
            intent_preceded_close = intent_path.is_file()
            raise OSError("injected failure after SQLite close")

    monkeypatch.setattr(storage, "close", fail_after_first_close)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="exact previous database files were restored"),
    ):
        storage.restore_backup(created["database"])

    assert intent_preceded_close is True
    assert close_calls >= 2
    assert {path: path.read_bytes() for path in active_paths if path.is_file()} == originals
    assert not intent_path.exists()
    assert set(storage.settings.backups_dir.glob("recovery-*")) == recovery_before
    assert not list(database_path.parent.glob("*.restore-*.tmp"))
    assert storage.get_user("must-survive-close-boundary") is not None


def test_failed_rollback_keeps_durable_copies_and_next_attempt_resumes(storage, monkeypatch):
    from fastapi.testclient import TestClient

    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.server import create_app
    from friday.storage import _maintenance as storage_module
    from friday.storage import init_storage
    from friday.storage._restore_barrier import DatabaseRestorePendingError

    storage.ensure_user("before-durable-rollback")
    created = storage.create_backup(label="durable-rollback")
    storage.ensure_user("must-return-after-durable-rollback")
    original_recovery_stage = storage_module._stage_verified_recovery_copy
    original_diagnostics = storage.diagnostics

    def fail_recovery_copy(_snapshot, _destination):  # noqa: ANN001, ANN202
        raise OSError("injected rollback staging failure")

    def fail_restored_health():  # noqa: ANN202
        raise RuntimeError("injected post-replacement health failure")

    monkeypatch.setattr(storage_module, "_stage_verified_recovery_copy", fail_recovery_copy)
    monkeypatch.setattr(storage, "diagnostics", fail_restored_health)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable rollback is pending"),
    ):
        storage.restore_backup(created["database"])

    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    recovery_path = Path(intent["recovery_path"])
    assert intent["phase"] == "prepared"
    assert recovery_path.is_dir()
    assert (recovery_path / "recovery.json").is_file()
    assert all((recovery_path / name).is_file() for name in intent["original_files"])

    # A normal backend/storage restart must fail before SQLite can open, migrate,
    # or create anything while the prepared marker exists.
    protected_paths = [
        intent_path,
        recovery_path / "recovery.json",
        *(recovery_path / name for name in intent["original_files"]),
        *(
            path
            for path in (
                storage.settings.database_path,
                Path(f"{storage.settings.database_path}-wal"),
                Path(f"{storage.settings.database_path}-shm"),
            )
            if path.is_file()
        ),
    ]
    protected_before = {path: path.read_bytes() for path in protected_paths}
    with pytest.raises(DatabaseRestorePendingError, match="recovery is pending"):
        init_storage(storage.settings)
    assert {path: path.read_bytes() for path in protected_paths} == protected_before
    recovery_handle = init_storage(storage.settings, allow_pending_restore=True)
    recovery_handle.close()
    assert {path: path.read_bytes() for path in protected_paths} == protected_before
    with (
        pytest.raises(DatabaseRestorePendingError, match="recovery is pending"),
        TestClient(create_app(storage.settings)),
    ):
        pass
    assert {path: path.read_bytes() for path in protected_paths} == protected_before
    restarted = FridayStorage(storage.settings)
    try:
        with pytest.raises(DatabaseRestorePendingError, match="recovery is pending"):
            restarted.ensure_user("must-not-open-during-restore-recovery")
    finally:
        restarted.close()
    assert {path: path.read_bytes() for path in protected_paths} == protected_before

    # A recovery source that changes while being copied must not make the staged
    # bytes authoritative or touch any active/recovery member.
    def corrupt_recovery_stage(snapshot, destination):  # noqa: ANN001, ANN202
        prepared = original_recovery_stage(snapshot, destination)
        with prepared.open("ab") as handle:
            handle.write(b"corrupt-staged-rollback")
            handle.flush()
            os.fsync(handle.fileno())
        return prepared

    monkeypatch.setattr(
        storage_module,
        "_stage_verified_recovery_copy",
        corrupt_recovery_stage,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="staged copy is invalid"),
    ):
        storage_module._recover_interrupted_restore(  # noqa: SLF001
            storage.settings,
            storage.settings.database_path.absolute(),
        )
    assert {path: path.read_bytes() for path in protected_paths} == protected_before
    assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))

    recovery_source = recovery_path / str(intent["original_files"][0])
    displaced_source = recovery_source.with_name(recovery_source.name + ".displaced")
    drift_injected = False

    def replace_recovery_source_with_symlink(snapshot, destination):  # noqa: ANN001, ANN202
        nonlocal drift_injected
        source_path = Path(snapshot.path)
        if not drift_injected and source_path == recovery_source:
            os.replace(recovery_source, displaced_source)
            recovery_source.symlink_to(displaced_source.name)
            drift_injected = True
        return original_recovery_stage(snapshot, destination)

    monkeypatch.setattr(
        storage_module,
        "_stage_verified_recovery_copy",
        replace_recovery_source_with_symlink,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="recovery source changed"),
    ):
        storage_module._recover_interrupted_restore(  # noqa: SLF001
            storage.settings,
            storage.settings.database_path.absolute(),
        )
    assert drift_injected is True
    assert recovery_source.is_symlink()
    recovery_source.unlink()
    os.replace(displaced_source, recovery_source)
    assert {path: path.read_bytes() for path in protected_paths} == protected_before
    assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))

    # A pathname replacement after all copies were staged must still be caught
    # before the first active unlink/replacement.  Identical bytes on a new inode
    # do not preserve the durable recovery authority named by the marker.
    original_staged_verify = storage_module._verify_staged_recovery_copy
    displaced_after_stage = recovery_source.with_name(recovery_source.name + ".after-stage")
    active_paths = [
        path
        for path in (
            storage.settings.database_path,
            Path(f"{storage.settings.database_path}-wal"),
            Path(f"{storage.settings.database_path}-shm"),
        )
        if path.is_file()
    ]
    active_before_drift = {path: path.read_bytes() for path in active_paths}
    after_stage_drift_injected = False

    def replace_source_after_staged_verification(path, snapshot):  # noqa: ANN001, ANN202
        nonlocal after_stage_drift_injected
        identity = original_staged_verify(path, snapshot)
        if not after_stage_drift_injected:
            os.replace(recovery_source, displaced_after_stage)
            recovery_source.write_bytes(displaced_after_stage.read_bytes())
            recovery_source.chmod(0o600)
            after_stage_drift_injected = True
        return identity

    monkeypatch.setattr(
        storage_module,
        "_verify_staged_recovery_copy",
        replace_source_after_staged_verification,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="recovery source changed"),
    ):
        storage_module._recover_interrupted_restore(  # noqa: SLF001
            storage.settings,
            storage.settings.database_path.absolute(),
        )
    assert after_stage_drift_injected is True
    assert {path: path.read_bytes() for path in active_paths} == active_before_drift
    recovery_source.unlink()
    os.replace(displaced_after_stage, recovery_source)
    assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))

    # Corruption after the intent/source revalidation but before mutation is
    # caught by the final full hash pass, not by inode/size bookkeeping alone.
    original_reload = storage_module._reload_exact_restore_intent
    monkeypatch.setattr(
        storage_module,
        "_verify_staged_recovery_copy",
        original_staged_verify,
    )
    late_stage_corruption_injected = False

    def corrupt_stage_after_intent_reload(settings, database_path, expected):  # noqa: ANN001, ANN202
        nonlocal late_stage_corruption_injected
        observed = original_reload(settings, database_path, expected)
        staged_paths = list(database_path.parent.glob("*.restore-*.tmp"))
        assert staged_paths
        with staged_paths[0].open("ab") as handle:
            handle.write(b"late-staged-corruption")
            handle.flush()
            os.fsync(handle.fileno())
        late_stage_corruption_injected = True
        return observed

    monkeypatch.setattr(
        storage_module,
        "_reload_exact_restore_intent",
        corrupt_stage_after_intent_reload,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="staged copy is invalid"),
    ):
        storage_module._recover_interrupted_restore(  # noqa: SLF001
            storage.settings,
            storage.settings.database_path.absolute(),
        )
    assert late_stage_corruption_injected is True
    assert {path: path.read_bytes() for path in active_paths} == active_before_drift
    assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))
    monkeypatch.setattr(storage_module, "_reload_exact_restore_intent", original_reload)

    monkeypatch.setattr(
        storage_module,
        "_stage_verified_recovery_copy",
        original_recovery_stage,
    )
    monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
            )
            == "rolled_back"
        )
    assert not intent_path.exists()
    assert storage.get_user("must-return-after-durable-rollback") is not None
    assert storage.diagnostics()["ok"] is True


def test_target_replace_fault_uses_complete_durable_rollback_set(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-target-replace-fault")
    created = storage.create_backup(label="target-replace-fault")
    storage.ensure_user("must-survive-target-replace-fault")
    database_path = storage.settings.database_path.absolute()
    original_replace = storage_module.os.replace
    injected = False

    def fail_target_replace(source, destination, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal injected
        if (
            not injected
            and Path(destination).name == database_path.name
            and ".restore-" in Path(source).name
            and (storage.settings.state_dir / "database-restore.intent.json").is_file()
        ):
            injected = True
            raise OSError("injected target replace failure")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "replace", fail_target_replace)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="restored automatically"),
    ):
        storage.restore_backup(created["database"])

    assert injected is True
    assert not (storage.settings.state_dir / "database-restore.intent.json").exists()
    assert storage.get_user("must-survive-target-replace-fault") is not None
    assert storage.diagnostics()["ok"] is True


def test_target_replace_rejects_late_displaced_hardlink(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-late-displaced-hardlink")
    created = storage.create_backup(label="late-displaced-hardlink")
    storage.ensure_user("must-survive-late-displaced-hardlink")
    database_path = storage.settings.database_path.absolute()
    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    alias = database_path.with_name("late-displaced-database.alias")
    original_replace = storage_module.os.replace
    injected = False

    def hardlink_displaced_target(source, destination, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal injected
        if (
            not injected
            and kwargs.get("dst_dir_fd") is not None
            and Path(destination).name == database_path.name
            and ".restore-" in Path(source).name
            and intent_path.is_file()
        ):
            os.link(database_path, alias)
            injected = True
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "replace", hardlink_displaced_target)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="restored automatically"),
    ):
        storage.restore_backup(created["database"])

    assert injected is True
    assert alias.is_file() and alias.stat().st_nlink == 1
    assert not os.path.samefile(alias, database_path)
    assert not intent_path.exists()
    assert storage.get_user("must-survive-late-displaced-hardlink") is not None
    assert storage.diagnostics()["ok"] is True


def test_committed_restore_marker_resumes_without_rewinding_target(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("present-in-target")
    created = storage.create_backup(label="committed-marker")
    storage.ensure_user("absent-from-target")
    original_remove = storage_module._remove_restore_intent

    def power_loss_before_marker_cleanup(_path, _expected_identity):  # noqa: ANN001, ANN202
        raise OSError("injected power loss after committed marker")

    monkeypatch.setattr(
        storage_module,
        "_remove_restore_intent",
        power_loss_before_marker_cleanup,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable commit cleanup is pending"),
    ):
        storage.restore_backup(created["database"])

    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    assert json.loads(intent_path.read_text(encoding="utf-8"))["phase"] == "committed"
    monkeypatch.setattr(storage_module, "_remove_restore_intent", original_remove)
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
            )
            == "committed"
        )
    assert storage.get_user("present-in-target") is not None
    assert storage.get_user("absent-from-target") is None


def test_restore_intent_read_stays_bound_to_lstat_parent_during_aba(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("present-in-intent-aba-target")
    created = storage.create_backup(label="intent-parent-aba")
    storage.ensure_user("absent-from-intent-aba-target")
    original_remove = storage_module._remove_restore_intent

    def preserve_committed_marker(_path, _expected_identity):  # noqa: ANN001, ANN202
        raise OSError("injected power loss before committed marker cleanup")

    monkeypatch.setattr(storage_module, "_remove_restore_intent", preserve_committed_marker)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable commit cleanup is pending"),
    ):
        storage.restore_backup(created["database"])

    state_dir = storage.settings.state_dir
    intent_path = state_dir / "database-restore.intent.json"
    committed_payload = json.loads(intent_path.read_text(encoding="utf-8"))
    assert committed_payload["phase"] == "committed"
    storage.close()
    monkeypatch.setattr(storage_module, "_remove_restore_intent", original_remove)

    displaced_state_dir = state_dir.with_name(f"{state_dir.name}-aba-original")
    decoy_state_dir = state_dir.with_name(f"{state_dir.name}-aba-decoy")
    decoy_state_dir.mkdir(mode=0o700)
    decoy_intent = decoy_state_dir / intent_path.name
    decoy_payload = {**committed_payload, "phase": "prepared"}
    decoy_intent.write_text(
        json.dumps(decoy_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    decoy_intent.chmod(0o600)

    original_open = storage_module.os.open
    injected = False

    def open_intent_during_parent_aba(path, flags, mode=0o777, *, dir_fd=None):  # noqa: ANN001, ANN202
        nonlocal injected
        if not injected and Path(path).name == intent_path.name:
            os.replace(state_dir, displaced_state_dir)
            os.replace(decoy_state_dir, state_dir)
            try:
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                injected = True
                return descriptor
            finally:
                os.replace(state_dir, decoy_state_dir)
                os.replace(displaced_state_dir, state_dir)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(storage_module.os, "open", open_intent_during_parent_aba)
    loaded = storage_module._load_restore_intent(  # noqa: SLF001
        storage.settings,
        storage.settings.database_path.absolute(),
    )

    assert injected is True
    assert loaded.intent == committed_payload
    assert loaded.intent["phase"] == "committed"
    assert json.loads(decoy_intent.read_text(encoding="utf-8")) == decoy_payload

    monkeypatch.setattr(storage_module.os, "open", original_open)
    decoy_intent.unlink()
    decoy_state_dir.rmdir()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
            )
            == "committed"
        )
    assert storage.get_user("present-in-intent-aba-target") is not None
    assert storage.get_user("absent-from-intent-aba-target") is None


def test_commit_marker_unlink_then_fsync_failure_reports_committed_truthfully(
    storage,
    monkeypatch,
):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("present-in-unlink-fsync-target")
    created = storage.create_backup(label="unlink-fsync-commit")
    storage.ensure_user("absent-from-unlink-fsync-target")
    original_remove = storage_module._remove_restore_intent

    def unlink_then_fail(path, _expected_identity):  # noqa: ANN001, ANN202
        path.unlink(missing_ok=True)
        raise OSError("injected directory fsync failure after intent unlink")

    monkeypatch.setattr(storage_module, "_remove_restore_intent", unlink_then_fail)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(
            RuntimeError,
            match="restore committed, but restore-intent cleanup durability is uncertain",
        ) as captured,
    ):
        storage.restore_backup(created["database"])

    assert "previous database files were restored" not in str(captured.value)
    assert not (storage.settings.state_dir / "database-restore.intent.json").exists()
    monkeypatch.setattr(storage_module, "_remove_restore_intent", original_remove)
    assert storage.get_user("present-in-unlink-fsync-target") is not None
    assert storage.get_user("absent-from-unlink-fsync-target") is None


def test_commit_marker_cleanup_noop_never_claims_success_or_rolls_back(
    storage,
    monkeypatch,
):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("present-in-noop-cleanup-target")
    created = storage.create_backup(label="noop-commit-cleanup")
    storage.ensure_user("absent-from-noop-cleanup-target")
    original_remove = storage_module._remove_restore_intent

    monkeypatch.setattr(
        storage_module,
        "_remove_restore_intent",
        lambda _path, _expected_identity: None,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable commit cleanup is pending") as captured,
    ):
        storage.restore_backup(created["database"])

    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    assert "previous database files were restored" not in str(captured.value)
    assert json.loads(intent_path.read_text(encoding="utf-8"))["phase"] == "committed"
    monkeypatch.setattr(storage_module, "_remove_restore_intent", original_remove)
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
            )
            == "committed"
        )
    assert storage.get_user("present-in-noop-cleanup-target") is not None
    assert storage.get_user("absent-from-noop-cleanup-target") is None


def test_commit_marker_hardlink_race_never_reports_cleanup_success(storage, monkeypatch):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("present-in-marker-hardlink-target")
    created = storage.create_backup(label="marker-hardlink-race")
    storage.ensure_user("absent-from-marker-hardlink-target")
    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    alias = intent_path.with_name("database-restore.intent.alias")
    original_unlink = storage_module.os.unlink
    injected = False

    def hardlink_immediately_before_unlink(path, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal injected
        if not injected and kwargs.get("dir_fd") is not None and Path(path).name == intent_path.name:
            os.link(intent_path, alias)
            injected = True
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "unlink", hardlink_immediately_before_unlink)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(
            RuntimeError,
            match="restore committed, but restore-intent cleanup durability is uncertain",
        ) as captured,
    ):
        storage.restore_backup(created["database"])

    assert injected is True
    assert "previous database files were restored" not in str(captured.value)
    assert not intent_path.exists()
    assert alias.is_file() and alias.stat().st_nlink == 1

    monkeypatch.setattr(storage_module.os, "unlink", original_unlink)
    os.link(alias, intent_path)
    alias.unlink()
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
            )
            == "committed"
        )
    assert storage.get_user("present-in-marker-hardlink-target") is not None
    assert storage.get_user("absent-from-marker-hardlink-target") is None


def test_rollback_marker_unlink_then_fsync_failure_reports_rolled_back_truthfully(
    storage,
    monkeypatch,
):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-rollback-cleanup-fault")
    created = storage.create_backup(label="rollback-cleanup-fault")
    storage.ensure_user("must-return-after-rollback-cleanup-fault")
    original_remove = storage_module._remove_restore_intent
    original_diagnostics = storage.diagnostics

    def fail_target_health():  # noqa: ANN202
        raise RuntimeError("injected target health failure")

    def unlink_then_fail(path, _expected_identity):  # noqa: ANN001, ANN202
        path.unlink()
        raise OSError("injected directory fsync failure after rollback intent unlink")

    monkeypatch.setattr(storage, "diagnostics", fail_target_health)
    monkeypatch.setattr(storage_module, "_remove_restore_intent", unlink_then_fail)
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(
            RuntimeError,
            match="exact previous database files were restored.*cleanup durability is uncertain",
        ) as captured,
    ):
        storage.restore_backup(created["database"])

    assert "restore committed" not in str(captured.value).casefold()
    assert not (storage.settings.state_dir / "database-restore.intent.json").exists()
    monkeypatch.setattr(storage_module, "_remove_restore_intent", original_remove)
    monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
    assert storage.get_user("must-return-after-rollback-cleanup-fault") is not None
    assert storage.diagnostics()["ok"] is True


def test_late_target_hardlink_alias_blocks_restore_commit_and_keeps_recovery(
    storage,
    monkeypatch,
):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-late-target-hardlink")
    created = storage.create_backup(label="late-target-hardlink")
    storage.ensure_user("must-return-after-target-hardlink")
    database_path = storage.settings.database_path.absolute()
    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    alias = database_path.with_name("late-target-hardlink.alias")
    original_verify = storage_module._verify_exact_restore_copy
    injected = False

    def hardlink_after_target_verification(path, **kwargs):  # noqa: ANN001, ANN202
        nonlocal injected
        identity = original_verify(path, **kwargs)
        if not injected and Path(path) == database_path and intent_path.is_file():
            os.link(path, alias)
            injected = True
        return identity

    monkeypatch.setattr(
        storage_module,
        "_verify_exact_restore_copy",
        hardlink_after_target_verification,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable rollback is pending"),
    ):
        storage.restore_backup(created["database"])

    assert injected is True
    assert alias.is_file() and os.path.samefile(alias, database_path)
    assert json.loads(intent_path.read_text(encoding="utf-8"))["phase"] == "prepared"

    monkeypatch.setattr(storage_module, "_verify_exact_restore_copy", original_verify)
    alias.unlink()
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                database_path,
            )
            == "rolled_back"
        )
    assert storage.get_user("must-return-after-target-hardlink") is not None


def test_late_recovery_hardlink_alias_blocks_rollback_completion(
    storage,
    monkeypatch,
):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-late-recovery-hardlink")
    created = storage.create_backup(label="late-recovery-hardlink")
    storage.ensure_user("must-return-after-recovery-hardlink")
    database_path = storage.settings.database_path.absolute()
    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    alias = database_path.with_name("late-recovery-hardlink.alias")
    original_diagnostics = storage.diagnostics
    original_staged_verify = storage_module._verify_staged_recovery_copy
    injected = False

    def fail_target_health():  # noqa: ANN202
        raise RuntimeError("injected target health failure before rollback")

    def hardlink_after_recovery_verification(path, snapshot):  # noqa: ANN001, ANN202
        nonlocal injected
        identity = original_staged_verify(path, snapshot)
        if not injected and Path(path) == database_path and intent_path.is_file():
            os.link(path, alias)
            injected = True
        return identity

    monkeypatch.setattr(storage, "diagnostics", fail_target_health)
    monkeypatch.setattr(
        storage_module,
        "_verify_staged_recovery_copy",
        hardlink_after_recovery_verification,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable rollback is pending"),
    ):
        storage.restore_backup(created["database"])

    assert injected is True
    assert alias.is_file() and os.path.samefile(alias, database_path)
    assert json.loads(intent_path.read_text(encoding="utf-8"))["phase"] == "prepared"

    monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
    monkeypatch.setattr(
        storage_module,
        "_verify_staged_recovery_copy",
        original_staged_verify,
    )
    alias.unlink()
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                database_path,
            )
            == "rolled_back"
        )
    assert storage.get_user("must-return-after-recovery-hardlink") is not None


def test_late_recovery_single_link_replacement_blocks_rollback_completion(
    storage,
    monkeypatch,
):
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.storage import _maintenance as storage_module

    storage.ensure_user("before-late-recovery-replacement")
    created = storage.create_backup(label="late-recovery-replacement")
    storage.ensure_user("must-return-after-recovery-replacement")
    database_path = storage.settings.database_path.absolute()
    intent_path = storage.settings.state_dir / "database-restore.intent.json"
    displaced = database_path.with_name("late-recovery-replacement.displaced")
    original_diagnostics = storage.diagnostics
    original_staged_verify = storage_module._verify_staged_recovery_copy
    injected = False

    def fail_target_health():  # noqa: ANN202
        raise RuntimeError("injected target health failure before rollback")

    def replace_after_recovery_verification(path, snapshot):  # noqa: ANN001, ANN202
        nonlocal injected
        identity = original_staged_verify(path, snapshot)
        if not injected and Path(path) == database_path and intent_path.is_file():
            verified_bytes = Path(path).read_bytes()
            os.replace(path, displaced)
            Path(path).write_bytes(verified_bytes)
            Path(path).chmod(0o600)
            injected = True
        return identity

    monkeypatch.setattr(storage, "diagnostics", fail_target_health)
    monkeypatch.setattr(
        storage_module,
        "_verify_staged_recovery_copy",
        replace_after_recovery_verification,
    )
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="durable rollback is pending"),
    ):
        storage.restore_backup(created["database"])

    assert injected is True
    assert database_path.is_file() and displaced.is_file()
    assert database_path.read_bytes() == displaced.read_bytes()
    assert not os.path.samefile(database_path, displaced)
    assert database_path.stat().st_nlink == displaced.stat().st_nlink == 1
    assert json.loads(intent_path.read_text(encoding="utf-8"))["phase"] == "prepared"

    monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
    monkeypatch.setattr(
        storage_module,
        "_verify_staged_recovery_copy",
        original_staged_verify,
    )
    displaced.unlink()
    storage.close()
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        assert (
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                database_path,
            )
            == "rolled_back"
        )
    assert storage.get_user("must-return-after-recovery-replacement") is not None


def test_backup_verification_requires_exact_closed_scope(storage):
    from friday.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("scope-owner")
    created = storage.create_backup(label="closed-scope")
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scope"]["engineer_command_ledger"] = "included"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verification = storage.verify_backup(created["database"])
    assert verification["manifest_scope_matches"] is False
    assert verification["ok"] is False
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="unverified backup"),
    ):
        storage.restore_backup(created["database"])


def test_restore_recovers_over_corrupt_active_database_and_preserves_raw_snapshot(storage):
    from friday.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("known-good-backup-user")
    created = storage.create_backup(label="before-corruption")
    storage.close()
    storage.settings.database_path.write_bytes(b"not-a-sqlite-database")

    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        restored = storage.restore_backup(created["database"], safety_label="corrupt-active")

    assert restored["ok"] is True
    assert restored["safety_backup"] is None
    recovery = restored["recovery_snapshot"]
    assert recovery and recovery["verified"] is False
    recovery_dir = Path(recovery["path"])
    assert (recovery_dir / "friday.sqlite3").read_bytes() == b"not-a-sqlite-database"
    assert storage.get_user("known-good-backup-user") is not None
    assert storage.diagnostics()["ok"] is True


def test_backup_does_not_hold_lock_during_verification(storage, monkeypatch):
    # The integrity/foreign-key scan runs against the backup copy on its own
    # connection, so the live storage lock must be free while it runs — otherwise
    # every request stalls for the whole (potentially minutes-long) scan.
    import threading
    import time

    reached_verify = threading.Event()
    release = threading.Event()
    original = storage._verify_backup_conn

    def slow_verify(conn):
        reached_verify.set()
        assert release.wait(3), "test stalled waiting to release verification"
        return original(conn)

    monkeypatch.setattr(storage, "_verify_backup_conn", slow_verify)

    outcome: dict = {}
    errors: list = []

    def run_backup():
        try:
            outcome.update(storage.create_backup(label="concurrency"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run_backup)
    worker.start()
    assert reached_verify.wait(3), "backup never reached verification"

    # Verification is in flight on the backup connection; a lock-taking read on
    # this thread must return promptly rather than block until release.
    start = time.monotonic()
    storage.kv_get("backup-concurrency-probe")
    elapsed = time.monotonic() - start
    release.set()
    worker.join(5)

    assert not errors, errors
    assert elapsed < 1.5, f"read blocked {elapsed:.2f}s — lock held during verification"
    assert outcome.get("integrity_check") == "ok"


@pytest.mark.asyncio
async def test_the_vault_stops_keeping_plaintext_of_deleted_knowledge(settings, storage):
    """ "Deleted" has to mean deleted on disk too, not only in the database.

    `_vault_sync` iterated `list_knowledge_objects`, which filters
    `deleted_at IS NULL`, and never reconciled removals; `MemoryVault.delete_object`
    had exactly one production caller — the hard-purge path. So a soft-deleted
    object, or one the reviewer marked IGNORED, kept a **plaintext Markdown copy of
    its full content** in the vault forever, while the user was told it was deleted
    and search agreed with them. Backups then carried that copy onward.
    """
    from friday.workers import WorkersManager

    kept_raw, kept = make_knowledge(storage, "alice", "Рабочая заметка, которая остаётся")
    _, doomed = make_knowledge(storage, "alice", "Пароль от роутера: 12345, удалить это")
    vault = MemoryVault(settings.memory_vault_dir)
    manager = WorkersManager(settings, storage, None, None, memory_vault=vault)

    def notes() -> list[Path]:
        # README.md is the vault's own signpost, not a projection of a KO.
        return [
            path for path in (settings.memory_vault_dir / "users").rglob("*.md") if path.name != "README.md"
        ]

    await manager._vault_sync("alice")  # noqa: SLF001
    assert len(notes()) == 2

    storage.soft_delete_knowledge_object(doomed.id, "alice")
    await manager._vault_sync("alice")  # noqa: SLF001

    remaining = notes()
    assert len(remaining) == 1
    body = remaining[0].read_text(encoding="utf-8")
    assert "Пароль от роутера" not in body
    assert "Рабочая заметка" in body
    del kept_raw, kept


@pytest.mark.asyncio
async def test_the_vault_worker_carries_entity_links_into_the_notes(settings, storage):
    """The `[[wikilinks]]` are only useful if the sync pass actually supplies them.

    `MemoryVault` renders whatever entity names it is handed; the wiring that
    fetches them lives in the worker, one batched query per page. A rename of that
    storage method, or a page loop that forgets to pass it, would leave every note
    linkless and every test in `test_memory_vault_notes.py` still green.
    """
    from friday.storage.models import Entity, EntityType
    from friday.workers import WorkersManager

    _, ko = make_knowledge(storage, "alice", "Аренда квартиры на Мира")
    entity = storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name="Квартира на Мира", entity_type=EntityType.OTHER)
    )
    storage.link_knowledge_entity("alice", ko.id, entity.id, status="accepted")
    rejected = storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name="Ошибочная сущность", entity_type=EntityType.OTHER)
    )
    storage.link_knowledge_entity("alice", ko.id, rejected.id, status="rejected")

    vault = MemoryVault(settings.memory_vault_dir)
    manager = WorkersManager(settings, storage, None, None, memory_vault=vault)
    await manager._vault_sync("alice")  # noqa: SLF001

    note = next(
        path for path in (settings.memory_vault_dir / "users").rglob("*.md") if path.name != "README.md"
    )
    body = note.read_text(encoding="utf-8")
    assert "[[Квартира на Мира]]" in body
    # A link the reviewer rejected is a wrong edge in the graph, not a weak one.
    assert "Ошибочная сущность" not in body


def test_mass_archive_without_selection_is_refused(settings):
    """DATA_LIFECYCLE §5: lifecycle changes apply only to explicitly selected objects.

    `POST /api/admin/lifecycle/deprecate` swept every active object under
    `importance < 0.3` older than the threshold — no ids, and none of the
    protections `list_lifecycle_candidates` applies. A file the owner uploaded, a
    note they explicitly saved, something used in an answer last week: archived in
    one unreviewed call. Same shape as the review-gate bypasses already closed in
    `bulk_classify_inbox` and the disk importer.
    """
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("owner")
        _, ko = make_knowledge(
            storage, "owner", "Старая заметка", importance=0.1, updated_at="2020-01-01T00:00:00+00:00"
        )
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        refused = client.post("/api/admin/lifecycle/deprecate", json={"user_id": "owner"}, headers=owner)
        assert refused.status_code == 400
        assert "Нужны ids" in refused.json()["detail"]
        assert storage.get_knowledge_object(ko.id, "owner")["lifecycle_stage"] == "active"

        accepted = client.post(
            "/api/admin/lifecycle/deprecate",
            json={"user_id": "owner", "ids": [ko.id]},
            headers=owner,
        )
        assert accepted.status_code == 200
        assert accepted.json()["archived"] == 1
        assert storage.get_knowledge_object(ko.id, "owner")["lifecycle_stage"] == "archived"
