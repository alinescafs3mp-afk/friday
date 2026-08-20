"""Nested and legacy private state stays owner-only under a common umask."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

from friday.backup_files import backup_files_incremental
from friday.backup_mirror import mirror_backups, mirror_files_tree
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import init_storage
from friday.supervisor import ChildSpec, Supervisor


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _legacy_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    path.write_bytes(content)
    path.chmod(0o644)


def test_storage_startup_repairs_owned_trees_without_touching_disabled_vault(
    settings,
    tmp_path: Path,
) -> None:
    home = tmp_path / "legacy-private-home"
    secured = replace(
        settings,
        home=home,
        state_dir=home / "state",
        database_path=home / "state" / "friday.sqlite3",
        files_dir=home / "files",
        backups_dir=home / "backups",
        exports_dir=home / "exports",
        memory_vault_dir=home / "vault",
        memory_vault_mode="disabled",
        log_dir=home / "logs",
        cache_dir=home / "cache",
    )
    runtime_roots = (
        secured.state_dir,
        secured.files_dir,
        secured.backups_dir,
        secured.exports_dir,
        secured.log_dir,
        secured.cache_dir,
    )
    roots = (*runtime_roots, secured.memory_vault_dir)
    for index, root in enumerate(roots):
        nested = root / "legacy" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        root.chmod(0o755)
        (root / "legacy").chmod(0o755)
        nested.chmod(0o755)
        payload = nested / f"private-{index}.bin"
        payload.write_bytes(f"private-{index}".encode())
        payload.chmod(0o644)

    previous_umask = os.umask(0o022)
    storage = None
    try:
        storage = init_storage(secured)
        for root in runtime_roots:
            for current, directories, filenames in os.walk(root):
                assert _mode(Path(current)) == 0o700
                assert all(_mode(Path(current) / name) == 0o700 for name in directories)
                assert all(_mode(Path(current) / name) == 0o600 for name in filenames)
        # Body-free mode may inventory a legacy projection, but startup must not
        # create, chmod, delete, or otherwise mutate it implicitly.
        assert _mode(secured.memory_vault_dir) == 0o755
        assert _mode(secured.memory_vault_dir / "legacy") == 0o755
        assert _mode(secured.memory_vault_dir / "legacy" / "nested") == 0o755
        assert _mode(secured.memory_vault_dir / "legacy" / "nested" / "private-6.bin") == 0o644
    finally:
        if storage is not None:
            storage.close()
        os.umask(previous_umask)


def test_runtime_lease_repairs_legacy_parent_and_file_under_umask_022(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "nested" / "backend.lock"
    _legacy_file(path, b"{}\n")
    path.parent.chmod(0o755)
    previous_umask = os.umask(0o022)
    lease = ProcessLease(path, protocol="friday.test.private-mode.v1")
    try:
        lease.acquire()
        assert _mode(path.parent) == 0o700
        assert _mode(path) == 0o600
    finally:
        lease.release()
        os.umask(previous_umask)


def test_supervisor_repairs_live_and_rotated_logs_under_umask_022(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o755)
    log_path = log_dir / "backend.log"
    log_path.write_text("legacy\n", encoding="utf-8")
    log_path.chmod(0o644)
    spec = ChildSpec(
        name="private-log",
        argv=[sys.executable, "-c", "print('private child output')"],
        log_path=log_path,
    )
    previous_umask = os.umask(0o022)
    try:
        supervisor = Supervisor(
            [spec],
            max_rapid_crashes=1,
            poll_interval_sec=0.01,
            log_backups=2,
        )
        supervisor.run()
        assert _mode(log_dir) == 0o700
        assert _mode(log_path) == 0o600

        older = log_path.with_name("backend.log.1")
        older.write_text("older", encoding="utf-8")
        older.chmod(0o644)
        supervisor._rotate(log_path)  # noqa: SLF001 - exact rotation boundary
        assert _mode(log_path) == 0o600
        assert _mode(log_path.with_name("backend.log.1")) == 0o600
        assert _mode(log_path.with_name("backend.log.2")) == 0o600
    finally:
        os.umask(previous_umask)


def test_ingestion_nested_directories_and_replayed_file_are_private(
    settings,
    storage,
) -> None:
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    body = b"private uploaded bytes"
    digest = hashlib.sha256(body).hexdigest()
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    previous_umask = os.umask(0o022)
    try:
        target = pipeline._store_file("alice", body, digest, "private.bin")  # noqa: SLF001
        assert _mode(target.parent) == 0o700
        assert _mode(target.parent.parent) == 0o700
        assert _mode(target) == 0o600

        target.chmod(0o644)
        replayed = pipeline._store_file("alice", body, digest, "private.bin")  # noqa: SLF001
        assert replayed == target
        assert _mode(target) == 0o600
    finally:
        os.umask(previous_umask)


def test_skipped_local_and_mirrored_backups_repair_legacy_modes(
    settings,
    tmp_path: Path,
) -> None:
    previous_umask = os.umask(0o022)
    try:
        source_root = tmp_path / "originals"
        target_root = tmp_path / "local-backup"
        source = source_root / "alice" / "document.bin"
        destination = target_root / "alice" / "document.bin"
        _legacy_file(source, b"same private document")
        _legacy_file(destination, b"same private document")

        report = backup_files_incremental(source_root, target_root)
        assert report["copied"] == 0
        assert _mode(target_root) == 0o700
        assert _mode(destination.parent) == 0o700
        assert _mode(destination) == 0o600

        backups = tmp_path / "database-backups"
        mirror = tmp_path / "mirror"
        database = backups / "jericho-test.sqlite3"
        manifest = backups / "jericho-test.manifest.json"
        _legacy_file(database, b"synthetic sqlite backup")
        _legacy_file(
            manifest,
            json.dumps(
                {"database": database.name, "sha256": hashlib.sha256(database.read_bytes()).hexdigest()}
            ).encode(),
        )
        mirrored_database = mirror / database.name
        mirrored_manifest = mirror / manifest.name
        _legacy_file(mirrored_database, database.read_bytes())
        _legacy_file(mirrored_manifest, manifest.read_bytes())
        mirrored = replace(settings, backups_dir=backups, backup_mirror_dir=mirror)

        mirror_report = mirror_backups(mirrored)
        assert mirror_report["skipped_existing"] == 1
        assert _mode(mirror) == 0o700
        assert _mode(mirrored_database) == 0o600
        assert _mode(mirrored_manifest) == 0o600
        assert _mode(database) == 0o600
        assert _mode(manifest) == 0o600

        source_tree = backups / "files" / "alice"
        mirror_tree = mirror / "files" / "alice"
        _legacy_file(source_tree / "scan.pdf", b"private scan")
        _legacy_file(mirror_tree / "scan.pdf", b"private scan")
        files_report = mirror_files_tree(mirrored)
        assert files_report["skipped_existing"] == 1
        assert _mode(mirror / "files") == 0o700
        assert _mode(mirror_tree) == 0o700
        assert _mode(mirror_tree / "scan.pdf") == 0o600
    finally:
        os.umask(previous_umask)
