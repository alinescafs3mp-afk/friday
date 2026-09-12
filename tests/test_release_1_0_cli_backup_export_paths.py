"""Real isolated CLI database backup, verification and selected tenant export.

Database-only scope is explicit. Binary files, full installation restore,
encrypted/off-device mirrors and complete export privacy have separate cases.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
import stat
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import SCHEMA_VERSION
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

OWN = "cli089-backup-own"
FOREIGN = "cli089-backup-foreign"
STAMP = "2026-01-02T03:04:05+00:00"
TABLES = ("users", "raw_objects", "inbox")
SCOPE = {
    "sqlite_database": "included",
    "raw_files": "external",
    "memory_vault": "external",
    "obsidian_profiles_and_vaults": "external",
    "engineer_command_ledger": "external",
    "model_weights": "external",
    "configuration_and_secrets": "external",
}
NO_MIRROR = "⚠ Зеркалирование выключено: копия лежит на том же диске. Настройте FRIDAY_BACKUP_MIRROR_DIR (внешний диск/синхронизируемый каталог).\n"


def _same(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected, strict=True):
            _same(a, b)
    else:
        assert actual == expected


def _rows(storage):
    return {
        name: [dict(row) for row in storage.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()]
        for name in TABLES
    }


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


def _lease(settings, name):
    return ProcessLease(
        settings.state_dir / f"{name}.lock",
        protocol="friday.account-deletion.v1" if name == "account-deletion" else "friday.backend.v1",
    )


@pytest.fixture
def backup_cli(settings, storage, monkeypatch):
    current = replace(settings, backup_mirror_dir=None, backup_encryption_key_file=None)
    monkeypatch.setattr(config, "load_settings", lambda: current)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    for person, text in [(OWN, "own-cli-backup-canary"), (FOREIGN, "foreign-cli-backup-canary")]:
        storage.ensure_user(person, source="fixture", display_name=person)
        raw = RawObject(
            id=new_id("raw"),
            user_id=person,
            source="upload",
            source_ref=f"cli-backup:{person}",
            raw_content=text,
            content_type="text",
            received_at=STAMP,
            created_at=STAMP,
        )
        storage.store_raw_object(raw)
        storage.store_inbox_item(
            InboxItem(id=new_id("inbox"), user_id=person, raw_object_id=raw.id, status=InboxStatus.PENDING)
        )
    return current, storage


def _run(monkeypatch, capsys, *arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert type(stop.value.code) is int
    captured = capsys.readouterr()
    return stop.value.code, captured.out, captured.err


def _verify_expected(path):
    return {
        "database": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "integrity_check": "ok",
        "database_schema_version": SCHEMA_VERSION,
        "database_schema_supported": True,
        "foreign_key_violations": 0,
        "database_error": None,
        "manifest_present": True,
        "hash_matches_manifest": True,
        "manifest_database_matches": True,
        "manifest_size_matches": True,
        "manifest_schema_supported": True,
        "manifest_schema_matches_database": True,
        "manifest_scope_matches": True,
        "engineer_command_authority_matches": None,
        "engineer_command_store_id": None,
        "engineer_command_authority_sequence": None,
        "manifest_error": None,
        "ok": True,
    }


@pytest.mark.parametrize(
    "label,mirror", [(None, False), ("cli-own-fixture", False), ("cli-mirror-fixture", True)]
)
def test_cli_backup_creates_verified_private_database_pair_and_truthful_scope(
    backup_cli, monkeypatch, capsys, tmp_path, label, mirror
):
    settings, storage = backup_cli
    target = tmp_path / "owned-mirror"
    if mirror:
        target.mkdir(mode=0o700)
        current = replace(settings, backup_mirror_dir=target)
        monkeypatch.setattr(config, "load_settings", lambda: current)
    before = _rows(storage)
    original = type(storage).create_backup
    calls = []

    def create(observed, *, label):
        # The outer backend lease establishes compatibility; this pins dispatcher ownership.
        assert _locked(settings.state_dir / "account-deletion.lock")
        calls.append(label)
        return original(observed, label=label)

    monkeypatch.setattr(type(storage), "create_backup", create)
    started = datetime.now(UTC).replace(microsecond=0)
    with _lease(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, "backup", *(["--label", label] if label else []))
    ended = datetime.now(UTC).replace(microsecond=0)
    assert code == 0 and error == ("" if mirror else NO_MIRROR)
    body = json.loads(output)
    expected_label = label or "cli"
    assert calls == [expected_label]
    database = Path(body["path"])
    manifest_path = Path(body["manifest_path"])
    assert database.parent == settings.backups_dir and manifest_path == database.with_suffix(".manifest.json")
    assert re.fullmatch(r"jericho-\d{8}T\d{6}Z-" + re.escape(expected_label) + r"\.sqlite3", database.name)
    assert set(_files(settings.backups_dir)) == {database.name, manifest_path.name}
    manifest = json.loads(manifest_path.read_text())
    assert started <= datetime.fromisoformat(manifest["created_at"]) <= ended
    expected_manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": manifest["created_at"],
        "label": expected_label,
        "database": database.name,
        "size_bytes": database.stat().st_size,
        "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "scope": SCOPE,
    }
    _same(manifest, expected_manifest)
    expected_mirror = {"enabled": False}
    if mirror:
        expected_mirror = {
            "enabled": True,
            "mirror_dir": str(target),
            "encrypted": False,
            "copied": 1,
            "skipped_existing": 0,
            "repaired": 0,
            "failed": 0,
            "plaintext_leftovers": [],
            "same_device": True,
        }
        assert _files(target) == _files(settings.backups_dir)
    _same(
        body,
        {
            **expected_manifest,
            "path": str(database),
            "manifest_path": str(manifest_path),
            "mirror": expected_mirror,
        },
    )
    for path in (database, manifest_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        copied = {
            name: [dict(r) for r in connection.execute(f"SELECT * FROM {name} ORDER BY id")]
            for name in TABLES
        }
    finally:
        connection.close()
    _same(copied, before)
    _same(_rows(storage), before)


def test_cli_verify_explicit_and_latest_backup_then_detects_corrupted_manifest(
    backup_cli, monkeypatch, capsys
):
    settings, storage = backup_cli
    first = storage.create_backup(label="001")
    second = storage.create_backup(label="999")
    names = [first["database"], second["database"]]
    assert sorted(names)[-1] == second["database"]
    before = _rows(storage)
    original = type(storage).verify_backup
    calls = []

    def verify(observed, filename):
        # The outer backend lease establishes compatibility; this pins dispatcher ownership.
        assert _locked(settings.state_dir / "account-deletion.lock")
        calls.append(filename)
        return original(observed, filename)

    monkeypatch.setattr(type(storage), "verify_backup", verify)
    with _lease(settings, "backend"):
        for args, name in [((first["database"],), first["database"]), ((), second["database"])]:
            files_before = _files(settings.backups_dir)
            code, output, error = _run(monkeypatch, capsys, "verify-backup", *args)
            assert code == 0 and error == ""
            _same(json.loads(output), _verify_expected(settings.backups_dir / name))
            assert _files(settings.backups_dir) == files_before
            _same(_rows(storage), before)
        path = Path(second["manifest_path"])
        damaged = json.loads(path.read_text())
        damaged["sha256"] = "0" * 64
        path.write_text(json.dumps(damaged))
        files_before = _files(settings.backups_dir)
        code, output, error = _run(monkeypatch, capsys, "verify-backup", second["database"])
        assert code == 1 and error == ""
        _same(
            json.loads(output),
            {**_verify_expected(Path(second["path"])), "hash_matches_manifest": False, "ok": False},
        )
        assert _files(settings.backups_dir) == files_before
    assert calls == [first["database"], second["database"], second["database"]]
    _same(_rows(storage), before)


def test_cli_verify_empty_store_and_invalid_path_are_visible_refusals(
    backup_cli, monkeypatch, capsys, caplog
):
    settings, storage = backup_cli
    before = _rows(storage)
    assert _files(settings.backups_dir) == {}
    assert _run(monkeypatch, capsys, "verify-backup") == (2, "", "Резервных копий не найдено.\n")
    # A viable existing outside archive distinguishes containment from missing-file refusal.
    ready = storage.create_backup(label="outside-path-fixture")
    outside = settings.backups_dir.parent / "outside.sqlite3"
    outside_manifest = outside.with_suffix(".manifest.json")
    Path(ready["path"]).rename(outside)
    Path(ready["manifest_path"]).rename(outside_manifest)
    manifest = json.loads(outside_manifest.read_text())
    manifest["database"] = outside.name
    outside_manifest.write_text(json.dumps(manifest))
    assert outside.stat().st_size > 0
    assert manifest["sha256"] == hashlib.sha256(outside.read_bytes()).hexdigest()
    assert manifest["integrity_check"] == "ok" and manifest["foreign_key_violations"] == 0
    protected = {path: path.read_bytes() for path in (outside, outside_manifest)}
    assert _files(settings.backups_dir) == {}
    for filename in ("missing.sqlite3", "../outside.sqlite3"):
        caplog.clear()
        code, output, error = _run(monkeypatch, capsys, "verify-backup", filename)
        assert code == 2 and output == error == ""
        assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
            ("ERROR", "CLI command failed (FileNotFoundError)")
        ]
        assert _files(settings.backups_dir) == {}
        assert all(path.read_bytes() == data for path, data in protected.items())
        _same(_rows(storage), before)


def test_cli_export_writes_one_private_tenant_artifact_with_selected_rows(backup_cli, monkeypatch, capsys):
    settings, storage = backup_cli
    before = _rows(storage)
    original = type(storage).export_user
    calls = []

    def export(observed, person):
        # The outer backend lease establishes compatibility; this pins dispatcher ownership.
        assert _locked(settings.state_dir / "account-deletion.lock")
        calls.append(person)
        return original(observed, person)

    monkeypatch.setattr(type(storage), "export_user", export)
    start = datetime.now(UTC).replace(microsecond=0)
    with _lease(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, "export-user", OWN)
    end = datetime.now(UTC).replace(microsecond=0)
    assert code == 0 and error == "" and calls == [OWN]
    result = json.loads(output)
    path = Path(result["path"])
    assert path.parent == settings.exports_dir and set(_files(settings.exports_dir)) == {path.name}
    assert re.fullmatch(
        r"jericho-export-"
        + re.escape(OWN)
        + "--"
        + hashlib.sha256(OWN.encode()).hexdigest()[:12]
        + r"-\d{8}T\d{6}\.\d{6}Z\.json",
        path.name,
    )
    _same(result, {"path": str(path), "filename": path.name, "size_bytes": path.stat().st_size})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert payload["format"] == "jericho-user-export-v3"
    _same(payload["scope"], {"main_database_rows": "included", "engineer_command_ledger": "external"})
    assert start <= datetime.fromisoformat(payload["exported_at"]) <= end
    _same(payload["user"], next(r for r in before["users"] if r["id"] == OWN))
    for name in ("raw_objects", "inbox"):
        _same(payload[name], [r for r in before[name] if r["user_id"] == OWN])
    assert FOREIGN not in path.read_text() and "foreign-cli-backup-canary" not in path.read_text()
    _same(_rows(storage), before)


def test_cli_export_unknown_user_creates_no_artifact(backup_cli, monkeypatch, capsys, caplog):
    settings, storage = backup_cli
    before = _rows(storage)
    caplog.clear()
    code, output, error = _run(monkeypatch, capsys, "export-user", "missing-cli-fixture")
    assert code == 2 and output == error == ""
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
        ("ERROR", "CLI command failed (ValueError)")
    ]
    assert _files(settings.exports_dir) == {}
    _same(_rows(storage), before)


@pytest.mark.parametrize("command", ["backup", "verify-backup", "export-user"])
def test_cli_backup_export_refuse_held_deletion_lease_before_real_handlers(
    backup_cli, monkeypatch, capsys, caplog, command
):
    settings, storage = backup_cli
    ready = storage.create_backup(label="valid-lease-target")
    before = _rows(storage)
    files_before = _files(settings.backups_dir)
    exports_before = _files(settings.exports_dir)
    method = {"backup": "create_backup", "verify-backup": "verify_backup", "export-user": "export_user"}[
        command
    ]
    original = getattr(type(storage), method)
    calls = []

    def observe(instance, *args, **kwargs):
        calls.append((args, kwargs.copy()))
        return original(instance, *args, **kwargs)

    monkeypatch.setattr(type(storage), method, observe)
    args = [ready["database"]] if command == "verify-backup" else [OWN] if command == "export-user" else []
    caplog.clear()
    with _lease(settings, "account-deletion"):
        code, output, error = _run(monkeypatch, capsys, command, *args)
    assert code == 2 and output == error == "" and calls == []
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
        ("ERROR", "CLI command failed (RuntimeLeaseError)")
    ]
    assert _files(settings.backups_dir) == files_before and _files(settings.exports_dir) == exports_before
    _same(_rows(storage), before)
