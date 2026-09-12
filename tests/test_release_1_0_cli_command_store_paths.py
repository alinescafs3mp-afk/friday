"""Real CLI provisioning of a disposable external command ledger; no command execution."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import stat
import sys
from dataclasses import replace

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease
from friday.organs.engineer import command_tools
from friday.organs.engineer.command.contracts import CommandError
from friday.organs.engineer.command.store import CommandJobStore

MASTER = b"M" * 32
LIFECYCLE_KEY = hmac.new(MASTER, b"friday-engineer-command-v1\x00store-lifecycle", hashlib.sha256).digest()


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


@pytest.fixture
def command_context(settings, storage, monkeypatch):
    external = settings.home.parent / "external-command-authority"
    external.mkdir(mode=0o700)
    key = external / "command.key"
    key.write_bytes(MASTER)
    key.chmod(0o600)
    current = replace(settings, engineer_command_store_dir=external / "ledger", engineer_command_key_file=key)
    storage.ensure_user("command-store-archive-canary", source="test", display_name="private archive canary")
    storage.close()
    monkeypatch.setattr(config, "load_settings", lambda: current)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    original, reached = command_tools.provision_engineer_command_store, []

    def observed(loaded):
        assert loaded == current
        assert all(_locked(current.state_dir / (role + ".lock")) for role in ("account-deletion", "backend"))
        reached.append(True)
        return original(loaded)

    monkeypatch.setattr(command_tools, "provision_engineer_command_store", observed)
    return current, reached


def _run(settings, monkeypatch, capsys, *, held_backend=False):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", "engineer-command-store-provision"])
    # main() logs safe exception classes. Exercise its real secure formatter and
    # capture that stream independently of pytest's own logging handler.
    logger = logging.getLogger()
    prior_level = logger.level
    prior_formatters = [(handler, handler.formatter) for handler in logger.handlers]
    log_text = io.StringIO()
    handler = logging.StreamHandler(log_text)
    logger.addHandler(handler)
    try:
        with pytest.raises(SystemExit) as exited:
            cli.main()
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(prior_level)
        for previous, formatter in prior_formatters:
            previous.setFormatter(formatter)
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    assert not _locked(settings.state_dir / "account-deletion.lock")
    assert _locked(settings.state_dir / "backend.lock") is held_backend
    return exited.value.code, captured.out, captured.err + log_text.getvalue()


def _archive(settings):
    # Provisioning must leave existing archive database bytes alone. Locks and the
    # new authenticated lifecycle anchor are operational files, not archive data.
    return {
        str(p.relative_to(settings.home)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in settings.home.rglob("*")
        if p.is_file() and p.suffix in {".sqlite3", ".sqlite", ".db"}
    }


def _ledger(settings):
    path = settings.engineer_command_store_dir / "kernel.sqlite"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        meta = connection.execute(
            "SELECT store_id,schema_version,authority_sequence FROM command_store_lifecycle_meta WHERE singleton=1"
        ).fetchone()
        nonces = connection.execute("SELECT nonce,kind,exp FROM grant_nonces ORDER BY nonce").fetchall()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return meta, nonces
    finally:
        connection.close()


def _runtime(settings):
    return CommandJobStore.open_runtime(
        settings.engineer_command_store_dir,
        lifecycle_key=LIFECYCLE_KEY,
        lifecycle_state_dir=settings.state_dir,
    )


def test_command_store_cli_provisions_authenticated_private_ledger_and_repeat_preserves_authority(
    command_context, monkeypatch, capsys
):
    settings, reached = command_context
    archive = _archive(settings)
    assert archive and not settings.engineer_command_store_dir.exists()
    code, output, error = _run(settings, monkeypatch, capsys)
    assert code == 0 and not error and reached and json.loads(output) == {"status": "provisioned"}
    assert str(settings.home.parent) not in output and MASTER.hex() not in output
    root = settings.engineer_command_store_dir
    assert all(stat.S_IMODE((root / name).stat().st_mode) == 0o700 for name in (".", "jobs", "workbenches"))
    assert all(
        stat.S_IMODE((root / name).stat().st_mode) == 0o600
        for name in ("kernel.sqlite", "kernel.lock", "kernel.lease")
    )
    with_key = _runtime(settings)
    try:
        with_key.assert_lifecycle_ready()
        with with_key.transaction():
            with_key.consume_nonce("preserved-replay-canary", exp=2**31, now=1)
        identity = with_key.backup_authority_snapshot()
    finally:
        with_key.close()
    before = _ledger(settings)
    assert before[0][0] and before[0][1] > 0 and before[1] == [("preserved-replay-canary", "used", 2**31)]
    code, output, error = _run(settings, monkeypatch, capsys)
    assert code == 0 and not error and json.loads(output) == {"status": "provisioned"}
    assert _ledger(settings) == before and _archive(settings) == archive
    with_key = _runtime(settings)
    try:
        with_key.assert_lifecycle_ready()
        assert with_key.backup_authority_snapshot() == identity
        with pytest.raises(CommandError, match="grant_replay"), with_key.transaction():
            with_key.consume_nonce("preserved-replay-canary", exp=2**31, now=1)
    finally:
        with_key.close()
    assert _ledger(settings) == before and not _locked(root / "kernel.lease")


@pytest.mark.parametrize("kind", ["missing", "short", "public-mode", "symlink"])
def test_command_store_cli_invalid_key_refuses_without_creating_ledger(
    command_context, monkeypatch, capsys, kind
):
    settings, _reached = command_context
    key = settings.engineer_command_key_file
    canary = key.with_name("key-canary")
    canary.write_bytes(MASTER)
    canary.chmod(0o600)
    if kind == "missing":
        key.unlink()
    elif kind == "short":
        key.write_bytes(b"short")
    elif kind == "public-mode":
        key.chmod(0o644)
    else:
        key.unlink()
        key.symlink_to(canary)
    archive = _archive(settings)
    code, _output, error = _run(settings, monkeypatch, capsys)
    assert code == 2 and error
    assert not settings.engineer_command_store_dir.exists() and _archive(settings) == archive
    assert canary.read_bytes() == MASTER and MASTER.hex() not in error


def test_command_store_cli_store_symlink_refuses_without_writing_target(command_context, monkeypatch, capsys):
    settings, _reached = command_context
    target = settings.home.parent / "untouched-target"
    target.mkdir(mode=0o700)
    canary = target / "canary"
    canary.write_text("keep target bytes")
    settings.engineer_command_store_dir.symlink_to(target, target_is_directory=True)
    archive = _archive(settings)
    code, _output, error = _run(settings, monkeypatch, capsys)
    assert code == 2 and "CLI command failed (CommandError)" in error
    assert list(target.iterdir()) == [canary] and canary.read_text() == "keep target bytes"
    assert _archive(settings) == archive


def test_command_store_cli_changed_master_cannot_rebind_existing_authority(
    command_context, monkeypatch, capsys
):
    settings, _reached = command_context
    code, _output, _error = _run(settings, monkeypatch, capsys)
    assert code == 0
    before, archive = _ledger(settings), _archive(settings)
    key = settings.engineer_command_key_file
    key.write_bytes(b"D" * 32)
    code, _output, error = _run(settings, monkeypatch, capsys)
    assert code == 2 and error and _ledger(settings) == before and _archive(settings) == archive
    key.write_bytes(MASTER)
    runtime = _runtime(settings)
    try:
        runtime.assert_lifecycle_ready()
    finally:
        runtime.close()


@pytest.mark.parametrize("busy", ["backend", "ledger"])
def test_command_store_cli_live_leases_prevent_second_writer(command_context, monkeypatch, capsys, busy):
    settings, reached = command_context
    archive = _archive(settings)
    if busy == "backend":
        with ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
            code, _output, error = _run(settings, monkeypatch, capsys, held_backend=True)
            assert code == 2 and error and not reached and not settings.engineer_command_store_dir.exists()
        assert not _locked(settings.state_dir / "backend.lock")
    else:
        code, _output, _error = _run(settings, monkeypatch, capsys)
        assert code == 0
        before = _ledger(settings)
        runtime = _runtime(settings)
        try:
            code, _output, error = _run(settings, monkeypatch, capsys)
            assert code == 2 and "CLI command failed (CommandError)" in error
            assert _ledger(settings) == before
            runtime.assert_lifecycle_ready()
        finally:
            runtime.close()
        assert not _locked(settings.engineer_command_store_dir / "kernel.lease")
    assert _archive(settings) == archive


def test_command_store_main_file_is_private_before_sqlite_with_permissive_umask(
    command_context, monkeypatch, capsys
):
    settings, _reached = command_context
    archive = _archive(settings)
    database = settings.engineer_command_store_dir / "kernel.sqlite"
    original_connect = sqlite3.connect
    observed = []

    def observe_connect(target, *arguments, **options):
        if str(target) == str(database):
            assert database.is_file()
            observed.append(stat.S_IMODE(database.stat().st_mode))
            assert observed[-1] == 0o600
        return original_connect(target, *arguments, **options)

    monkeypatch.setattr(sqlite3, "connect", observe_connect)
    previous_umask = os.umask(0)
    try:
        code, output, error = _run(settings, monkeypatch, capsys)
    finally:
        os.umask(previous_umask)
    assert code == 0 and not error and json.loads(output) == {"status": "provisioned"}
    assert observed == [0o600]
    assert _archive(settings) == archive
    for name in ("kernel.sqlite", "kernel.sqlite-wal", "kernel.sqlite-shm", "kernel.lock", "kernel.lease"):
        path = database.parent / name
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
