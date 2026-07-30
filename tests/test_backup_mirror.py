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


def _mounted(path: Path) -> Path:
    """A mirror directory that exists, as a real mount would.

    `mirror_backups` no longer creates it: `mkdir(parents=True)` on an unmounted
    external disk silently produced a same-disk "offsite" copy and reported success.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_backup(settings) -> dict:
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        return storage.create_backup(label="test")
    finally:
        storage.close()


def test_plain_mirror_copies_and_is_idempotent(settings, tmp_path):
    mirrored = replace(settings, backup_mirror_dir=_mounted(tmp_path / "mirror"))
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
        backup_mirror_dir=_mounted(tmp_path / "mirror"),
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
        backup_mirror_dir=_mounted(tmp_path / "mirror"),
        backup_encryption_key_file=tmp_path / "no-such.key",
    )
    _make_backup(mirrored)
    report = mirror_backups(mirrored)
    assert report["failed"] == 1
    assert report["copied"] == 0
    assert list((tmp_path / "mirror").glob("*.enc")) == []


def test_mirror_disabled_reports_disabled(settings):
    assert mirror_backups(settings) == {"enabled": False}


def test_backups_are_pruned_to_the_retention_window(storage, settings):
    """A daily backup that is never removed is a disk that fills.

    The schedule adds a full copy of the database every 24 hours and nothing in
    the codebase ever took one away — `create_backup` always mints a new
    timestamped stem, and the only unlink calls in that module are error cleanup
    for the copy being made. Retention was the missing half of the backup story,
    and the failure mode takes the live instance down along with the backups.
    """
    import json as _json

    for index in range(6):
        result = storage.create_backup(label=f"t{index}")
        # Same-second stems would collide; make the ordering unambiguous.
        stem = Path(result["path"]).with_suffix("")
        renamed = stem.with_name(f"jericho-2026010{index}T000000Z-t{index}").with_suffix(".sqlite3")
        Path(result["path"]).rename(renamed)
        manifest = _json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        manifest["database"] = renamed.name
        Path(result["manifest_path"]).unlink()
        renamed.with_suffix(".manifest.json").write_text(
            _json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

    assert len(storage.list_backups()) == 6
    report = storage.prune_backups(keep=3)
    assert report == {"enabled": True, "removed": 3, "kept": 3}

    survivors = [entry["database"] for entry in storage.list_backups()]
    assert len(survivors) == 3
    assert survivors == sorted(survivors, reverse=True)  # the three NEWEST survive
    assert "jericho-20260105T000000Z-t5.sqlite3" in survivors
    assert "jericho-20260100T000000Z-t0.sqlite3" not in survivors

    # Pruning is opt-out, and a second run is a no-op.
    assert storage.prune_backups(keep=0) == {"enabled": False, "removed": 0, "kept": 0}
    assert storage.prune_backups(keep=3)["removed"] == 0


def test_a_database_without_a_manifest_is_not_pruned(storage, settings):
    """That shape is an interrupted write, not slack — deleting it destroys evidence."""
    result = storage.create_backup(label="orphan")
    Path(result["manifest_path"]).unlink()
    assert storage.prune_backups(keep=1)["removed"] == 0
    assert Path(result["path"]).exists()


def test_an_unmounted_mirror_is_refused_not_created(settings, tmp_path):
    """`mkdir(parents=True)` on an unplugged disk makes a same-disk copy silently.

    The mirror exists because a backup on the same disk does not survive that disk
    dying. Creating the mount point produced exactly the thing the module was
    written to prevent, reported it as a successful mirror, and kept doing so for
    as long as the drive stayed unplugged.
    """
    absent = tmp_path / "not-mounted"
    mirrored = replace(settings, backup_mirror_dir=absent)
    _make_backup(mirrored)

    report = mirror_backups(mirrored)

    assert report["enabled"] is True
    assert report["error"] == "mirror_dir_missing"
    assert report["copied"] == 0
    assert not absent.exists(), "the mount point was created, so the copy is same-disk"


def test_a_manifest_less_mirror_copy_is_repaired_not_skipped_forever(settings, tmp_path):
    """A database without its manifest cannot be verified or restored.

    Idempotency was decided on the database file alone while the manifest was
    copied *after* `os.replace`. An interruption in that window left the
    manifest-less database as the durable state, and every later run saw the file,
    counted it as skipped, and moved on — leaving an offsite copy that
    `verify_backup` refuses. The `except` branch could not clean it up either: it
    unlinks `tmp`, which `os.replace` has already consumed.
    """
    mirror_dir = _mounted(tmp_path / "mirror")
    mirrored = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(mirrored)

    assert mirror_backups(mirrored)["copied"] == 1
    mirror_manifest = mirror_dir / f"{Path(manifest['database']).stem}.manifest.json"
    assert mirror_manifest.is_file()

    # Recreate the torn state: the database is there, the manifest is not.
    mirror_manifest.unlink()

    report = mirror_backups(mirrored)
    assert report["repaired"] == 1
    assert report["skipped_existing"] == 0
    assert mirror_manifest.is_file()

    # And a complete pair is still a no-op.
    assert mirror_backups(mirrored)["skipped_existing"] == 1


# --- дерево оригиналов файлов едет в то же зеркало ----------------------------


def _files_tree(settings) -> None:
    tree = settings.backups_dir / "files" / "u1"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "doc.pdf").write_bytes(b"%PDF-original-bytes")


def test_files_tree_mirrors_plain_and_is_idempotent(settings, tmp_path):
    """mirror_backups ходит по манифестам БД — оригиналы документов оставались
    без offsite-копии даже у того, кто зеркало включил."""
    from jericho.backup_mirror import mirror_files_tree

    mirrored = replace(settings, backup_mirror_dir=_mounted(tmp_path / "mirror"))
    _files_tree(mirrored)

    first = mirror_files_tree(mirrored)
    assert first["enabled"] is True and first["copied"] == 1 and first["complete"] is True
    copy = tmp_path / "mirror" / "files" / "u1" / "doc.pdf"
    assert copy.read_bytes() == b"%PDF-original-bytes"

    again = mirror_files_tree(mirrored)
    assert again["copied"] == 0 and again["skipped_existing"] == 1

    # Удаление источника копию не трогает: зеркало, повторяющее удаление, — не бэкап.
    (mirrored.backups_dir / "files" / "u1" / "doc.pdf").unlink()
    third = mirror_files_tree(mirrored)
    assert copy.is_file()
    assert third["complete"] is True


def test_files_tree_mirrors_encrypted_with_roundtrip_verification(settings, tmp_path):
    from jericho.backup_mirror import decrypt_file, mirror_files_tree

    key_file = tmp_path / "backup.key"
    key_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    mirrored = replace(
        settings,
        backup_mirror_dir=_mounted(tmp_path / "mirror"),
        backup_encryption_key_file=key_file,
    )
    _files_tree(mirrored)

    report = mirror_files_tree(mirrored)
    assert report["copied"] == 1 and report["complete"] is True

    encrypted = tmp_path / "mirror" / "files" / "u1" / "doc.pdf.enc"
    assert encrypted.is_file()
    assert encrypted.read_bytes() != b"%PDF-original-bytes"
    restored = tmp_path / "restored.pdf"
    decrypt_file(encrypted, restored, key_file)
    assert restored.read_bytes() == b"%PDF-original-bytes"


def test_files_tree_never_creates_the_mirror_mountpoint(settings, tmp_path):
    """mkdir на неподключённом диске создаёт каталог В ТОЧКЕ МОНТИРОВАНИЯ и
    рапортует успех — ту же ловушку уже ловили у баз."""
    from jericho.backup_mirror import mirror_files_tree

    mirrored = replace(settings, backup_mirror_dir=tmp_path / "unmounted" / "mirror")
    _files_tree(mirrored)

    report = mirror_files_tree(mirrored)
    assert report["error"] == "mirror_dir_missing"
    assert not (tmp_path / "unmounted").exists(), "зеркало создано на месте отсутствующего диска"


def test_no_files_backup_yet_is_not_an_error(settings, tmp_path):
    from jericho.backup_mirror import mirror_files_tree

    mirrored = replace(settings, backup_mirror_dir=_mounted(tmp_path / "mirror"))
    report = mirror_files_tree(mirrored)
    assert report["state"] == "no_files_backup_yet" and report["complete"] is True
