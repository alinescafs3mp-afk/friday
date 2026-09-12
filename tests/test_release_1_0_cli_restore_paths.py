"""Real isolated CLI database restoration, safety snapshot and refusal boundaries.

Proves selected users rows and two external file canaries, not installation-wide recovery.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import sqlite3
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import SCHEMA_VERSION, FridayStorage, init_storage

CONFIRM = "Восстановление заменит активную SQLite-базу. Повторите команду с --yes после остановки backend и Telegram bridge.\n"
NOTICE = "Важно: восстановлена только SQLite-база; raw files, memory vault, веса модели и локальная конфигурация не изменялись.\n"
SCOPE = {
    "sqlite_database": "restored",
    "raw_files": "unchanged",
    "memory_vault": "unchanged",
    "obsidian_profiles_and_vaults": "unchanged",
    "telegram_queue": "unchanged",
    "engineer_command_ledger": "unchanged",
    "model_weights": "unchanged",
    "configuration_and_secrets": "unchanged",
}


@pytest.fixture(autouse=True)
def isolate_cli_logger():
    """Admit real friday.cli ERROR under inherited logger state; restore after.

    These tests no-op cli.configure_logging, so they inherit disabled/propagate/
    level and logging.disable from a prior test in the same process.
    Restore only state changed here; pytest owns its capture handlers.
    Do not stub Logger.error.
    """

    tracked = ("friday.cli", "friday")
    snapshot = []
    for name in tracked:
        log = logging.getLogger(name)
        snapshot.append((name, log.disabled, log.propagate, log.level))
    disable_level = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for name in tracked:
        log = logging.getLogger(name)
        log.disabled = False
        log.propagate = True
        log.setLevel(logging.NOTSET)
    try:
        yield
    finally:
        logging.disable(disable_level)
        for name, disabled, propagate, level in snapshot:
            log = logging.getLogger(name)
            log.disabled = disabled
            log.propagate = propagate
            log.setLevel(level)


@pytest.fixture
def restore_cli(settings, storage, monkeypatch):
    current = replace(settings, backup_mirror_dir=None, backup_encryption_key_file=None)
    monkeypatch.setattr(config, "load_settings", lambda: current)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    storage.ensure_user("cli089-restore-owner", source="fixture")
    storage.ensure_user("cli089-restore-foreign", source="fixture")
    return current, storage


def _run(monkeypatch, capsys, *arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert type(stopped.value.code) is int
    output = capsys.readouterr()
    return stopped.value.code, output.out, output.err


def _same(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _same(left, right)
    else:
        assert actual == expected


def _rows(storage):
    return [dict(r) for r in storage.execute("SELECT * FROM users ORDER BY id").fetchall()]


def _fresh_rows(settings):
    handle = init_storage(settings)
    try:
        return _rows(handle)
    finally:
        handle.close()


def _backup_rows(path):
    # This completed, hash-pinned backup has no writer; immutable avoids observer WAL/SHM files.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT * FROM users ORDER BY id")]
    finally:
        connection.close()


def _files(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def _locked(path):
    if not path.exists():
        return False
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def _lease(settings, role):
    filename = "telegram-inbox.sqlite3.lock" if role == "telegram-bridge" else f"{role}.lock"
    return ProcessLease(settings.state_dir / filename, protocol=f"friday.{role}.v1")


@pytest.mark.parametrize("latest", [False, True])
def test_cli_restore_selects_explicit_or_latest_distinct_snapshot_and_keeps_safety_copy(
    restore_cli, monkeypatch, capsys, latest
):
    settings, storage = restore_cli
    old_rows = _rows(storage)
    old = storage.create_backup(label="001-old")
    storage.ensure_user("cli089-in-new-snapshot", source="fixture")
    new_rows = _rows(storage)
    new = storage.create_backup(label="999-new")
    assert sorted([old["database"], new["database"]])[-1] == new["database"]
    assert old_rows != new_rows
    storage.ensure_user("cli089-after-both-snapshots", source="fixture")
    active_rows = _rows(storage)
    external = [
        settings.files_dir / "restore-file-canary.txt",
        settings.memory_vault_dir / "restore-vault-canary.md",
    ]
    for path in external:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"external content written after both database snapshots")
    protected = {path: path.read_bytes() for path in external}
    source_files = _files(settings.backups_dir)
    chosen = new if latest else old
    expected_rows = new_rows if latest else old_rows
    original = FridayStorage.restore_backup
    calls = []

    def restore(instance, filename, *, safety_label):
        assert _locked(settings.state_dir / "account-deletion.lock")
        assert _locked(settings.state_dir / "backend.lock")
        calls.append((filename, safety_label))
        return original(instance, filename, safety_label=safety_label)

    monkeypatch.setattr(FridayStorage, "restore_backup", restore)
    storage.close()
    code, output, error = _run(
        monkeypatch, capsys, "restore-backup", *([] if latest else [old["database"]]), "--yes"
    )
    assert code == 0 and error == NOTICE
    body = json.loads(output)
    assert calls == [(chosen["database"], f"pre-restore-{Path(chosen['database']).stem}")]
    safety = body["safety_backup"]
    assert isinstance(safety, dict)
    safety_path = Path(safety["path"])
    assert safety_path.parent == settings.backups_dir and safety_path.name not in source_files
    assert safety["database"] == safety_path.name
    assert safety["label"] == f"pre-restore-{Path(chosen['database']).stem}"
    assert safety["sha256"] == hashlib.sha256(safety_path.read_bytes()).hexdigest()
    assert safety["size_bytes"] == safety_path.stat().st_size
    manifest_path = Path(safety["manifest_path"])
    assert manifest_path == safety_path.with_suffix(".manifest.json")
    _same(
        json.loads(manifest_path.read_text()),
        {k: v for k, v in safety.items() if k not in {"path", "manifest_path"}},
    )
    assert stat.S_IMODE(safety_path.stat().st_mode) == stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    _same(_backup_rows(safety_path), active_rows)
    _same(
        body,
        {
            "ok": True,
            "restored_from": chosen["database"],
            "restored_sha256": chosen["sha256"],
            "source_schema_version": SCHEMA_VERSION,
            "active_schema_version": SCHEMA_VERSION,
            "database_path": str(settings.database_path),
            "safety_backup": safety,
            "recovery_snapshot": None,
            "scope": SCOPE,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "invalidated_deletion_eligibility": 0,
        },
    )
    _same(_fresh_rows(settings), expected_rows)
    assert all(path.read_bytes() == data for path, data in protected.items())
    after_files = _files(settings.backups_dir)
    assert {name: after_files[name] for name in source_files} == source_files
    assert set(after_files) - set(source_files) == {safety_path.name, manifest_path.name}
    assert not (settings.state_dir / "database-restore.intent.json").exists()


def test_cli_restore_without_yes_refuses_a_valid_target_before_storage_restore(
    restore_cli, monkeypatch, capsys
):
    settings, storage = restore_cli
    ready = storage.create_backup(label="confirmation-target")
    before = _rows(storage)
    files_before = _files(settings.backups_dir)
    calls = []
    original = FridayStorage.restore_backup

    def restore(instance, *args, **kwargs):
        calls.append((args, kwargs.copy()))
        return original(instance, *args, **kwargs)

    monkeypatch.setattr(FridayStorage, "restore_backup", restore)
    storage.close()
    assert _run(monkeypatch, capsys, "restore-backup", ready["database"]) == (2, "", CONFIRM)
    assert calls == [] and _files(settings.backups_dir) == files_before
    _same(_fresh_rows(settings), before)


@pytest.mark.parametrize("role", ["account-deletion", "backend", "telegram-bridge"])
def test_cli_restore_refuses_each_active_role_before_mutating_restore_stage(
    restore_cli, monkeypatch, capsys, caplog, role
):
    settings, storage = restore_cli
    ready = storage.create_backup(label="lease-target")
    before = _rows(storage)
    files_before = _files(settings.backups_dir)
    calls = []
    original = FridayStorage._restore_backup_with_stopped_bridge

    def restore_stage(instance, *args, **kwargs):
        calls.append((args, kwargs.copy()))
        return original(instance, *args, **kwargs)

    monkeypatch.setattr(FridayStorage, "_restore_backup_with_stopped_bridge", restore_stage)
    storage.close()
    caplog.clear()
    caplog.set_level(logging.ERROR, logger="friday.cli")
    with _lease(settings, role):
        code, output, error = _run(monkeypatch, capsys, "restore-backup", ready["database"], "--yes")
    assert code == 2 and output == error == "" and calls == []
    reason = "RuntimeError" if role == "telegram-bridge" else "RuntimeLeaseError"
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
        ("ERROR", f"CLI command failed ({reason})")
    ]
    assert _files(settings.backups_dir) == files_before
    _same(_fresh_rows(settings), before)
    assert not (settings.state_dir / "database-restore.intent.json").exists()


def test_cli_restore_rejects_corrupt_manifest_and_keeps_current_rows_and_archive(
    restore_cli, monkeypatch, capsys, caplog
):
    settings, storage = restore_cli
    ready = storage.create_backup(label="corrupt-target")
    storage.ensure_user("cli089-current-must-survive", source="fixture")
    before = _rows(storage)
    manifest = Path(ready["manifest_path"])
    body = json.loads(manifest.read_text())
    body["sha256"] = "0" * 64
    manifest.write_text(json.dumps(body))
    files_before = _files(settings.backups_dir)
    storage.close()
    caplog.clear()
    caplog.set_level(logging.ERROR, logger="friday.cli")
    code, output, error = _run(monkeypatch, capsys, "restore-backup", ready["database"], "--yes")
    assert code == 2 and output == error == ""
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
        ("ERROR", "CLI command failed (RuntimeError)")
    ]
    assert _files(settings.backups_dir) == files_before
    _same(_fresh_rows(settings), before)
    assert not (settings.state_dir / "database-restore.intent.json").exists()
