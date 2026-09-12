"""Real isolated backup-keygen/decrypt CLI, including failed output publication.

Uses an owned synthetic OpenSSL fixture; no remote mirror or live data.
"""

from __future__ import annotations

import hashlib
import re
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease

KEEP_KEY = "Храните копию ключа ОТДЕЛЬНО от зеркала: без ключа .enc-копии невосстановимы.\n"
NEXT_STEP = "Дальше: `jericho verify-backup` рядом с манифестом или `jericho restore-backup`.\n"


@pytest.fixture
def crypto_cli(settings, storage, monkeypatch):
    current = replace(settings, backup_encryption_key_file=None, backup_mirror_dir=None)
    monkeypatch.setattr(config, "load_settings", lambda: current)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    storage.ensure_user("cli089-crypto-owner", source="fixture")
    return current, storage


def _run(monkeypatch, capsys, *arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert type(stopped.value.code) is int
    output = capsys.readouterr()
    return stopped.value.code, output.out, output.err


def _rows(storage):
    return [dict(r) for r in storage.execute("SELECT * FROM users ORDER BY id").fetchall()]


def _lease(settings, role):
    return ProcessLease(settings.state_dir / f"{role}.lock", protocol=f"friday.{role}.v1")


def _forbid_database(monkeypatch):
    import friday.storage

    def forbidden(*args, **kwargs):
        raise AssertionError("account-free crypto command opened storage")

    monkeypatch.setattr(friday.storage, "init_storage", forbidden)


def _encrypted_fixture(tmp_path):
    key = tmp_path / "fixture.key"
    key.write_text("07" * 32 + "\n")
    key.chmod(0o600)
    plain = tmp_path / "fixture.input"
    payload = b"cli089 exact decrypted bytes\x00\xff\n" + bytes(range(256)) * 3
    plain.write_bytes(payload)
    encrypted = tmp_path / "fixture.sqlite3.enc"
    # Independent fixture production: actual fixed OpenSSL argv, no application encrypt helper.
    result = subprocess.run(
        [
            "/usr/bin/openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-salt",
            "-in",
            str(plain),
            "-out",
            str(encrypted),
            "-pass",
            f"file:{key}",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    assert encrypted.read_bytes().startswith(b"Salted__")
    assert len(encrypted.read_bytes()) > len(payload)
    assert encrypted.read_bytes() != payload
    plain.unlink()
    return key, encrypted, payload


@pytest.mark.parametrize("configured", [False, True])
def test_cli_keygen_creates_private_key_without_account_access_or_secret_output(
    crypto_cli, monkeypatch, capsys, caplog, tmp_path, configured
):
    settings, storage = crypto_cli
    target = tmp_path / "nested" / "generated.key"
    if configured:
        monkeypatch.setattr(
            config, "load_settings", lambda: replace(settings, backup_encryption_key_file=target)
        )
    before = _rows(storage)
    _forbid_database(monkeypatch)
    caplog.clear()
    with _lease(settings, "account-deletion"), _lease(settings, "backend"):
        code, output, error = _run(
            monkeypatch, capsys, "backup-keygen", *([] if configured else ["--out", str(target)])
        )
    assert code == 0 and error == ""
    secret = target.read_text()
    assert re.fullmatch(r"[0-9a-f]{64}\n", secret)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert output == f"Ключ шифрования бэкапов записан: {target} (права 600)\n" + KEEP_KEY
    assert secret.strip() not in output + error + caplog.text
    assert _rows(storage) == before


def test_cli_keygen_requires_target_and_explicit_force_to_replace_existing_key(
    crypto_cli, monkeypatch, capsys, caplog, tmp_path
):
    settings, storage = crypto_cli
    target = tmp_path / "existing.key"
    previous = "00" * 32 + "\n"
    target.write_text(previous)
    target.chmod(0o600)
    before = _rows(storage)
    _forbid_database(monkeypatch)
    assert _run(monkeypatch, capsys, "backup-keygen") == (
        2,
        "",
        "Укажите --out или задайте FRIDAY_BACKUP_ENCRYPTION_KEY_FILE в .env.local\n",
    )
    assert _run(monkeypatch, capsys, "backup-keygen", "--out", str(target)) == (
        2,
        f"Не перезаписываю существующий ключ: {target} (используйте --force)\n",
        "",
    )
    assert target.read_text() == previous
    code, output, error = _run(monkeypatch, capsys, "backup-keygen", "--out", str(target), "--force")
    current = target.read_text()
    assert code == 0 and error == "" and re.fullmatch(r"[0-9a-f]{64}\n", current)
    assert current != previous and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert output == f"Ключ шифрования бэкапов записан: {target} (права 600)\n" + KEEP_KEY
    assert current.strip() not in output + error + caplog.text and previous.strip() not in output + error
    assert _rows(storage) == before


@pytest.mark.parametrize("configured", [False, True])
def test_cli_decrypt_restores_exact_bytes_with_explicit_or_default_paths(
    crypto_cli, monkeypatch, capsys, caplog, tmp_path, configured
):
    settings, storage = crypto_cli
    key, encrypted, payload = _encrypted_fixture(tmp_path)
    before = _rows(storage)
    protected = {p: p.read_bytes() for p in (key, encrypted)}
    destination = encrypted.with_suffix("") if configured else tmp_path / "chosen.output"
    if configured:
        monkeypatch.setattr(
            config, "load_settings", lambda: replace(settings, backup_encryption_key_file=key)
        )
    arguments = [] if configured else ["--key", str(key), "--out", str(destination)]
    _forbid_database(monkeypatch)
    caplog.clear()
    with _lease(settings, "account-deletion"), _lease(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, "decrypt-backup", str(encrypted), *arguments)
    assert code == 0 and error == ""
    assert output == f"Расшифровано: {destination}\n" + NEXT_STEP
    assert destination.read_bytes() == payload
    assert hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(payload).digest()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert all(p.read_bytes() == data for p, data in protected.items())
    assert key.read_text().strip() not in output + error + caplog.text
    assert _rows(storage) == before


def test_cli_decrypt_missing_key_configuration_is_visible_and_preserves_fixture(
    crypto_cli, monkeypatch, capsys, caplog, tmp_path
):
    _, storage = crypto_cli
    key, encrypted, _ = _encrypted_fixture(tmp_path)
    before = _rows(storage)
    original = encrypted.read_bytes()
    destination = encrypted.with_suffix("")
    _forbid_database(monkeypatch)
    assert _run(monkeypatch, capsys, "decrypt-backup", str(encrypted)) == (
        2,
        "",
        "Укажите --key или задайте FRIDAY_BACKUP_ENCRYPTION_KEY_FILE\n",
    )
    caplog.clear()
    code, output, error = _run(
        monkeypatch, capsys, "decrypt-backup", str(encrypted), "--key", str(tmp_path / "missing.key")
    )
    assert code == 2 and output == error == ""
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
        ("ERROR", "CLI command failed (BackupMirrorError)")
    ]
    assert not destination.exists() and encrypted.read_bytes() == original and key.is_file()
    assert _rows(storage) == before


def test_cli_decrypt_existing_destination_is_never_overwritten(crypto_cli, monkeypatch, capsys, tmp_path):
    _, storage = crypto_cli
    key, encrypted, _ = _encrypted_fixture(tmp_path)
    destination = tmp_path / "keep.output"
    destination.write_bytes(b"existing destination must survive")
    protected = {p: p.read_bytes() for p in (destination, key, encrypted)}
    before = _rows(storage)
    _forbid_database(monkeypatch)
    assert _run(
        monkeypatch, capsys, "decrypt-backup", str(encrypted), "--key", str(key), "--out", str(destination)
    ) == (2, "", f"Не перезаписываю существующий файл: {destination}\n")
    assert all(p.read_bytes() == data for p, data in protected.items())
    assert _rows(storage) == before


def test_cli_decrypt_corrupt_ciphertext_does_not_publish_a_failed_destination(
    crypto_cli, monkeypatch, capsys, caplog, tmp_path
):
    _, storage = crypto_cli
    key, encrypted, _ = _encrypted_fixture(tmp_path)
    # Salt header plus one ciphertext byte cannot be a complete AES-CBC message.
    encrypted.write_bytes(encrypted.read_bytes()[:17])
    destination = tmp_path / "failed.output"
    protected = {p: p.read_bytes() for p in (key, encrypted)}
    before = _rows(storage)
    _forbid_database(monkeypatch)
    caplog.clear()
    code, output, error = _run(
        monkeypatch, capsys, "decrypt-backup", str(encrypted), "--key", str(key), "--out", str(destination)
    )
    assert code == 2 and output == error == ""
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "friday.cli"] == [
        ("ERROR", "CLI command failed (BackupMirrorError)")
    ]
    assert all(p.read_bytes() == data for p, data in protected.items())
    assert _rows(storage) == before
    assert not destination.exists(), "failed decryption published a destination that blocks a corrected retry"


def test_decrypt_failure_preserves_existing_destination_and_cleans_private_staging(monkeypatch, tmp_path):
    from friday import backup_mirror

    key, encrypted, _payload = _encrypted_fixture(tmp_path)
    destination = tmp_path / "preserved.output"
    destination.write_bytes(b"existing valid backup")
    destination.chmod(0o640)
    before = set(tmp_path.iterdir())

    def fail_after_partial_output(arguments):
        pending = Path(arguments[arguments.index("-out") + 1])
        assert pending != destination
        assert stat.S_IMODE(pending.stat().st_mode) == 0o600
        assert stat.S_IMODE(pending.parent.stat().st_mode) == 0o700
        pending.write_bytes(b"partial private plaintext")
        assert destination.read_bytes() == b"existing valid backup"
        raise backup_mirror.BackupMirrorError("injected decryption failure")

    monkeypatch.setattr(backup_mirror, "_run_openssl", fail_after_partial_output)
    with pytest.raises(backup_mirror.BackupMirrorError):
        backup_mirror.decrypt_file(encrypted, destination, key)
    assert destination.read_bytes() == b"existing valid backup"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_cli_decrypt_preserves_a_destination_created_during_decryption(
    crypto_cli, monkeypatch, capsys, tmp_path, kind
):
    from friday import backup_mirror

    _settings, _storage = crypto_cli
    key, encrypted, _payload = _encrypted_fixture(tmp_path)
    destination = tmp_path / "racing.output"
    canary = tmp_path / "racing.canary"
    canary.write_bytes(b"concurrent destination")
    original = backup_mirror._run_openssl
    before = set(tmp_path.iterdir())

    def create_destination_after_decryption(arguments):
        original(arguments)
        assert not destination.exists()
        if kind == "file":
            destination.write_bytes(canary.read_bytes())
        else:
            destination.symlink_to(canary)

    monkeypatch.setattr(backup_mirror, "_run_openssl", create_destination_after_decryption)
    code, output, _error = _run(
        monkeypatch, capsys, "decrypt-backup", str(encrypted), "--key", str(key), "--out", str(destination)
    )
    assert code == 2 and output == ""
    assert destination.read_bytes() == canary.read_bytes() == b"concurrent destination"
    assert destination.is_symlink() is (kind == "symlink")
    assert set(tmp_path.iterdir()) == before | {destination}


def test_decrypt_success_replaces_existing_low_level_destination_with_complete_private_bytes(tmp_path):
    from friday.backup_mirror import decrypt_file

    key, encrypted, payload = _encrypted_fixture(tmp_path)
    destination = tmp_path / "replace.output"
    destination.write_bytes(b"old bytes")
    before = set(tmp_path.iterdir())
    decrypt_file(encrypted, destination, key)
    assert destination.read_bytes() == payload
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert set(tmp_path.iterdir()) == before
