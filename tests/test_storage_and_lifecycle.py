from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from jericho.memory import MemoryVault
from jericho.storage import SCHEMA_VERSION, JerichoStorage
from jericho.storage.models import KnowledgeObject, RawObject, new_id


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

    migrated = JerichoStorage(replace(settings, database_path=database))
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
    from jericho.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("before-restore")
    created = storage.create_backup(label="restore-source")
    storage.ensure_user("after-backup")
    assert storage.get_user("after-backup") is not None

    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="jericho.backend.v1"):
        restored = storage.restore_backup(created["database"], safety_label="pytest-pre-restore")

    assert restored["ok"] is True
    assert restored["integrity_check"] == "ok"
    assert restored["foreign_key_violations"] == 0
    assert restored["scope"]["sqlite_database"] == "restored"
    assert restored["scope"]["raw_files"] == "unchanged"
    assert storage.get_user("before-restore") is not None
    assert storage.get_user("after-backup") is None
    safety = restored["safety_backup"]
    assert safety and storage.verify_backup(safety["database"])["ok"] is True


def test_restore_requires_exclusive_backend_process_lease(storage):
    created = storage.create_backup(label="lease-required")

    with pytest.raises(RuntimeError, match="exclusive backend process lease"):
        storage.restore_backup(created["database"])


def test_restore_refuses_tampered_backup_without_touching_active_database(storage):
    from jericho.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("active-user")
    created = storage.create_backup(label="tampered-restore")
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="jericho.backend.v1"),
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
    from jericho.diagnostics.runtime_lease import ProcessLease

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
            ProcessLease(storage.settings.state_dir / "backend.lock", protocol="jericho.backend.v1"),
            pytest.raises(RuntimeError, match="must not be symlinks"),
        ):
            storage.restore_backup(created["database"])
        assert active_path.is_symlink()
        assert victim_path.read_bytes() == victim_before
    finally:
        active_path.unlink(missing_ok=True)
        os.replace(victim_path, active_path)
        assert storage.diagnostics()["ok"] is True


def test_restore_detects_backup_change_during_staging_and_rolls_back(storage, monkeypatch):
    # Patched where the name is LOOKED UP: restore lives in the maintenance mixin and
    # binds the helper at import time, so patching jericho.storage would not reach it.
    from jericho.diagnostics.runtime_lease import ProcessLease
    from jericho.storage import _maintenance as storage_module

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
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="jericho.backend.v1"),
        pytest.raises(RuntimeError, match="Backup changed while it was staged"),
    ):
        storage.restore_backup(created["database"], safety_label="staging-race-safety")

    assert injected is True
    assert storage.get_user("stable-before-staging-race") is not None
    assert storage.get_user("active-after-backup") is not None
    assert storage.diagnostics()["ok"] is True


def test_restore_recovers_over_corrupt_active_database_and_preserves_raw_snapshot(storage):
    from jericho.diagnostics.runtime_lease import ProcessLease

    storage.ensure_user("known-good-backup-user")
    created = storage.create_backup(label="before-corruption")
    storage.close()
    storage.settings.database_path.write_bytes(b"not-a-sqlite-database")

    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="jericho.backend.v1"):
        restored = storage.restore_backup(created["database"], safety_label="corrupt-active")

    assert restored["ok"] is True
    assert restored["safety_backup"] is None
    recovery = restored["recovery_snapshot"]
    assert recovery and recovery["verified"] is False
    recovery_dir = Path(recovery["path"])
    assert (recovery_dir / "jericho.sqlite3").read_bytes() == b"not-a-sqlite-database"
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
    from jericho.workers import WorkersManager

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
    from jericho.storage.models import Entity, EntityType
    from jericho.workers import WorkersManager

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

    from jericho.server import create_app

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
        assert "ids is required" in refused.json()["detail"]
        assert storage.get_knowledge_object(ko.id, "owner")["lifecycle_stage"] == "active"

        accepted = client.post(
            "/api/admin/lifecycle/deprecate",
            json={"user_id": "owner", "ids": [ko.id]},
            headers=owner,
        )
        assert accepted.status_code == 200
        assert accepted.json()["archived"] == 1
        assert storage.get_knowledge_object(ko.id, "owner")["lifecycle_stage"] == "archived"
