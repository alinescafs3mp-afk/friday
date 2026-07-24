"""Offsite backup mirroring + encryption — a same-disk backup is not a backup.

Pins: plain mirror copies verified by manifest sha256 and idempotent on re-run;
encrypted mirror (.enc via system openssl) verified by decrypt-and-compare and
round-trippable; a missing key file is a counted failure, not a silent skip.
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path

from jericho.backup_mirror import decrypt_file, mirror_backups
from jericho.storage import init_storage


def _make_backup(settings) -> dict:
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        return storage.create_backup(label="test")
    finally:
        storage.close()


def test_plain_mirror_copies_and_is_idempotent(settings, tmp_path):
    mirrored = replace(settings, backup_mirror_dir=tmp_path / "mirror")
    manifest = _make_backup(mirrored)

    first = mirror_backups(mirrored)
    assert first["enabled"] is True
    assert first["copied"] == 1
    assert first["failed"] == 0

    copy = tmp_path / "mirror" / manifest["database"]
    assert copy.is_file()
    # The manifest travels with the copy so offsite verification is possible.
    assert (tmp_path / "mirror" / f"{Path(manifest['database']).stem}.manifest.json").is_file()
    assert copy.read_bytes() == (mirrored.backups_dir / manifest["database"]).read_bytes()

    again = mirror_backups(mirrored)
    assert again["copied"] == 0
    assert again["skipped_existing"] == 1


def test_encrypted_mirror_roundtrip(settings, tmp_path):
    key_file = tmp_path / "backup.key"
    key_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    mirrored = replace(
        settings,
        backup_mirror_dir=tmp_path / "mirror",
        backup_encryption_key_file=key_file,
    )
    manifest = _make_backup(mirrored)

    report = mirror_backups(mirrored)
    assert report["copied"] == 1
    assert report["encrypted"] is True

    encrypted = tmp_path / "mirror" / f"{manifest['database']}.enc"
    assert encrypted.is_file()
    # No plaintext database escaped to the mirror.
    assert not (tmp_path / "mirror" / manifest["database"]).exists()
    original = (mirrored.backups_dir / manifest["database"]).read_bytes()
    assert original not in encrypted.read_bytes()  # actually encrypted, not renamed

    restored = tmp_path / "restored.sqlite3"
    decrypt_file(encrypted, restored, key_file)
    assert restored.read_bytes() == original


def test_missing_key_file_is_a_counted_failure(settings, tmp_path):
    mirrored = replace(
        settings,
        backup_mirror_dir=tmp_path / "mirror",
        backup_encryption_key_file=tmp_path / "no-such.key",
    )
    _make_backup(mirrored)
    report = mirror_backups(mirrored)
    assert report["failed"] == 1
    assert report["copied"] == 0
    assert list((tmp_path / "mirror").glob("*.enc")) == []


def test_mirror_disabled_reports_disabled(settings):
    assert mirror_backups(settings) == {"enabled": False}
