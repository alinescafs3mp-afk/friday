"""Offsite backup mirroring + encryption — a same-disk backup is not a backup.

Pins: plain mirror copies verified by manifest sha256 and idempotent on re-run;
encrypted mirror (.enc via system openssl) verified by decrypt-and-compare and
round-trippable; a missing key file is a counted failure, not a silent skip.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from friday.backup_mirror import decrypt_file, mirror_backups
from friday.storage import init_storage


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
    # `unverified` — новое поле: сколько копий не прошли проверку. Здесь все
    # исправны, и счёт `keep` ведётся именно по ним.
    assert report == {"enabled": True, "removed": 3, "kept": 3, "unverified": 0}

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
    from friday.backup_mirror import mirror_files_tree

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
    from friday.backup_mirror import decrypt_file, mirror_files_tree

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
    from friday.backup_mirror import mirror_files_tree

    mirrored = replace(settings, backup_mirror_dir=tmp_path / "unmounted" / "mirror")
    _files_tree(mirrored)

    report = mirror_files_tree(mirrored)
    assert report["error"] == "mirror_dir_missing"
    assert not (tmp_path / "unmounted").exists(), "зеркало создано на месте отсутствующего диска"


def test_no_files_backup_yet_is_not_an_error(settings, tmp_path):
    from friday.backup_mirror import mirror_files_tree

    mirrored = replace(settings, backup_mirror_dir=_mounted(tmp_path / "mirror"))
    report = mirror_files_tree(mirrored)
    assert report["state"] == "no_files_backup_yet" and report["complete"] is True


def _mirror_case(settings, tmp_path, kind, encrypted):
    """Real product backups and OpenSSL; no outcome or filesystem mocks."""
    from friday.backup_mirror import mirror_files_tree

    key = tmp_path / "fixture.key" if encrypted else None
    if key is not None:
        key.write_text(secrets.token_hex(32) + "\n")
    configured = replace(
        settings, backup_mirror_dir=_mounted(tmp_path / "mirror"), backup_encryption_key_file=key
    )
    if kind == "database":
        manifest = _make_backup(configured)
        source = Path(manifest["path"])
        relative = Path(source.name)
        invoke = partial(mirror_backups, configured)
        manifest_copy = configured.backup_mirror_dir / Path(manifest["manifest_path"]).name
    else:
        raw = b"owned document fixture, exact durable bytes\n"
        source = configured.backups_dir / "files" / "u1" / (hashlib.sha256(raw).hexdigest() + ".txt")
        source.parent.mkdir(parents=True)
        source.write_bytes(raw)
        relative = Path("files/u1") / source.name
        invoke = partial(mirror_files_tree, configured)
        manifest_copy = None
    target = configured.backup_mirror_dir / relative.with_name(relative.name + (".enc" if encrypted else ""))
    return configured, source, target, manifest_copy, invoke


def _recovered_bytes(target, key, tmp_path):
    if key is None:
        return target.read_bytes()
    recovered = tmp_path / "recovered"
    decrypt_file(target, recovered, key)
    try:
        return recovered.read_bytes()
    finally:
        recovered.unlink()


@pytest.mark.parametrize("kind", ["database", "files"])
@pytest.mark.parametrize("encrypted", [False, True])
def test_existing_corrupt_mirror_is_repaired_before_success(settings, tmp_path, kind, encrypted):
    configured, source, target, _manifest, run = _mirror_case(settings, tmp_path, kind, encrypted)
    original = source.read_bytes()
    assert run()["copied"] == 1
    raw = target.read_bytes()
    target.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))

    result = run()

    assert result["copied"] == result["repaired"] == 1
    assert result["failed"] == result["skipped_existing"] == 0
    assert _recovered_bytes(target, configured.backup_encryption_key_file, tmp_path) == original
    assert source.read_bytes() == original
    again = run()
    assert again["skipped_existing"] == 1 and again["copied"] == again["failed"] == 0


@pytest.mark.parametrize("kind", ["database", "files"])
@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_corrupt_source_cannot_create_or_replace_recovery_bytes(
    settings, tmp_path, kind, encrypted, existing
):
    configured, source, target, _manifest, run = _mirror_case(settings, tmp_path, kind, encrypted)
    if existing:
        assert run()["copied"] == 1
        before = target.read_bytes()
    original = source.read_bytes()
    source.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))

    result = run()

    assert result["failed"] == 1
    assert result["copied"] == result["skipped_existing"] == 0
    if kind == "files":
        assert result["complete"] is False
    if existing:
        assert target.read_bytes() == before
        assert _recovered_bytes(target, configured.backup_encryption_key_file, tmp_path) == original
    else:
        assert not target.exists()


@pytest.mark.parametrize("encrypted", [False, True])
def test_corrupt_mirror_manifest_is_repaired_with_exact_matching_database(settings, tmp_path, encrypted):
    configured, source, target, manifest, run = _mirror_case(settings, tmp_path, "database", encrypted)
    assert run()["copied"] == 1
    database_before = target.read_bytes()
    manifest.write_text('{"sha256":"incorrect"}\n')

    result = run()

    assert result["repaired"] == 1 and result["failed"] == result["skipped_existing"] == 0
    assert target.read_bytes() == database_before
    published = json.loads(manifest.read_text())
    assert published["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("kind", ["database", "files"])
def test_missing_encryption_key_preserves_existing_recovery_copy(settings, tmp_path, kind):
    configured, _source, target, _manifest, run = _mirror_case(settings, tmp_path, kind, True)
    assert run()["copied"] == 1
    before = target.read_bytes()
    configured.backup_encryption_key_file.unlink()

    result = run()

    assert result["failed"] == 1 and result["skipped_existing"] == result["copied"] == 0
    assert target.read_bytes() == before
    assert not list(target.parent.glob(".friday-mirror-*"))


@pytest.mark.parametrize("kind", ["database", "files"])
def test_mirror_target_symlink_never_changes_foreign_file(settings, tmp_path, kind):
    _configured, _source, target, _manifest, run = _mirror_case(settings, tmp_path, kind, False)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"must be retained")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(foreign)

    result = run()

    assert result["failed"] == 1 and result["copied"] == result["skipped_existing"] == 0
    assert target.is_symlink() and foreign.read_bytes() == b"must be retained"


def test_files_mirror_expired_budget_does_not_skip_unverified_existing_copy(settings, tmp_path):
    from friday.backup_mirror import mirror_files_tree

    configured, _source, target, _manifest, run = _mirror_case(settings, tmp_path, "files", False)
    assert run()["copied"] == 1
    target.write_bytes(b"broken")

    result = mirror_files_tree(configured, budget_sec=0)

    assert result["complete"] is False and result["pending"] == 1
    assert result["skipped_existing"] == 0 and target.read_bytes() == b"broken"


@pytest.mark.parametrize("invalid", ["parent-path", "absolute-path", "digest", "not-object", "symlink"])
def test_malformed_manifest_cannot_mirror_foreign_bytes(settings, tmp_path, invalid):
    configured, source, target, _manifest, run = _mirror_case(settings, tmp_path, "database", False)
    original_manifest = source.with_suffix(".manifest.json")
    body = json.loads(original_manifest.read_text())
    foreign = tmp_path / "foreign.sqlite3"
    foreign.write_bytes(b"unrelated data")
    before_mode = foreign.stat().st_mode
    if invalid in {"parent-path", "absolute-path"}:
        body["database"] = "../foreign.sqlite3" if invalid == "parent-path" else str(foreign)
    elif invalid == "digest":
        body["sha256"] = "invalid"
    elif invalid == "not-object":
        body = []
    if invalid == "symlink":
        original_manifest.unlink()
        original_manifest.symlink_to(foreign)
    else:
        original_manifest.write_text(json.dumps(body))

    result = run()

    assert result["failed"] == 1 and result["copied"] == result["skipped_existing"] == 0
    assert not target.exists()
    assert foreign.read_bytes() == b"unrelated data" and foreign.stat().st_mode == before_mode


@pytest.mark.parametrize(
    "invalid",
    ["size", "schema", "integrity", "foreign-keys", "scope", "authority", "unknown-field"],
)
def test_invalid_local_manifest_contract_preserves_good_recovery_pair(settings, tmp_path, invalid):
    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    assert mirror_backups(configured)["copied"] == 1
    local_manifest = Path(manifest["manifest_path"])
    mirrored_manifest = mirror_dir / local_manifest.name
    mirrored_database = mirror_dir / manifest["database"]
    good_manifest = mirrored_manifest.read_bytes()
    good_database = mirrored_database.read_bytes()
    body = json.loads(local_manifest.read_text(encoding="utf-8"))
    if invalid == "size":
        body["size_bytes"] += 1
    elif invalid == "schema":
        body["schema_version"] -= 1
    elif invalid == "integrity":
        body["integrity_check"] = "corrupt"
    elif invalid == "foreign-keys":
        body["foreign_key_violations"] = 1
    elif invalid == "scope":
        body["scope"] = {"sqlite_database": "included"}
    elif invalid == "authority":
        body["engineer_command_ledger_authority"] = {}
    else:
        body["untrusted"] = True
    local_manifest.write_text(json.dumps(body), encoding="utf-8")

    result = mirror_backups(configured)

    assert result["failed"] == 1
    assert result["copied"] == result["skipped_existing"] == result["repaired"] == 0
    assert mirrored_manifest.read_bytes() == good_manifest
    assert mirrored_database.read_bytes() == good_database


def test_self_consistent_manifest_cannot_promote_non_sqlite_source_bytes(settings, tmp_path):
    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    assert mirror_backups(configured)["copied"] == 1
    local_database = Path(manifest["path"])
    local_manifest = Path(manifest["manifest_path"])
    mirrored_database = mirror_dir / local_database.name
    mirrored_manifest = mirror_dir / local_manifest.name
    good_database = mirrored_database.read_bytes()
    good_manifest = mirrored_manifest.read_bytes()
    fake_database = b"not a sqlite backup despite a matching digest\n"
    local_database.write_bytes(fake_database)
    body = json.loads(local_manifest.read_text(encoding="utf-8"))
    body["size_bytes"] = len(fake_database)
    body["sha256"] = hashlib.sha256(fake_database).hexdigest()
    local_manifest.write_text(json.dumps(body), encoding="utf-8")

    result = mirror_backups(configured)

    assert result["failed"] == 1 and result["copied"] == result["repaired"] == 0
    assert mirrored_database.read_bytes() == good_database
    assert mirrored_manifest.read_bytes() == good_manifest


def test_unpaired_manifest_filename_is_refused_before_recovery_publication(settings, tmp_path):
    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    paired = Path(manifest["manifest_path"])
    unpaired = paired.with_name("unpaired.manifest.json")
    paired.replace(unpaired)

    result = mirror_backups(configured)

    assert result["failed"] == 1 and result["copied"] == result["repaired"] == 0
    assert not (mirror_dir / manifest["database"]).exists()
    assert not (mirror_dir / unpaired.name).exists()
    assert not (mirror_dir / paired.name).exists()


def test_changed_legacy_source_cannot_replace_different_existing_recovery(settings, tmp_path):
    from friday.backup_mirror import mirror_files_tree

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    source = configured.backups_dir / "files" / "u1" / "legacy.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"known-good legacy bytes\n")
    assert mirror_files_tree(configured)["copied"] == 1
    target = mirror_dir / "files" / "u1" / "legacy.txt"
    good_recovery = target.read_bytes()
    source.write_bytes(b"changed untrusted bytes\n")

    result = mirror_files_tree(configured)

    assert result["failed"] == 1 and result["complete"] is False
    assert result["copied"] == result["skipped_existing"] == result["repaired"] == 0
    assert target.read_bytes() == good_recovery


def test_intermediate_target_symlink_cannot_escape_mirror_root(settings, tmp_path):
    from friday.backup_mirror import mirror_files_tree

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    raw = b"path-confined source bytes\n"
    source = configured.backups_dir / "files" / "a" / "b" / (hashlib.sha256(raw).hexdigest() + ".txt")
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o755)
    foreign_mode = stat.S_IMODE(foreign.stat().st_mode)
    target_root = mirror_dir / "files"
    target_root.mkdir()
    (target_root / "a").symlink_to(foreign, target_is_directory=True)

    result = mirror_files_tree(configured)

    assert result["failed"] == 1 and result["copied"] == 0 and result["complete"] is False
    assert (target_root / "a").is_symlink()
    assert list(foreign.rglob("*")) == []
    assert stat.S_IMODE(foreign.stat().st_mode) == foreign_mode


def test_hardlinked_target_is_refused_without_chmod_or_trust(settings, tmp_path):
    from friday.backup_mirror import mirror_files_tree

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    raw = b"hardlink recovery bytes\n"
    source = configured.backups_dir / "files" / "u1" / (hashlib.sha256(raw).hexdigest() + ".txt")
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    target = mirror_dir / "files" / "u1" / source.name
    target.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign-target"
    foreign.write_bytes(raw)
    foreign.chmod(0o644)
    os.link(foreign, target)
    before = foreign.read_bytes()

    result = mirror_files_tree(configured)

    assert result["failed"] == 1 and result["copied"] == result["skipped_existing"] == 0
    assert target.stat().st_ino == foreign.stat().st_ino and foreign.stat().st_nlink == 2
    assert foreign.read_bytes() == before
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o644


def test_hardlinked_mirror_manifest_is_refused_before_database_publication(settings, tmp_path):
    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    foreign = tmp_path / "foreign-manifest"
    foreign.write_bytes(b"foreign manifest bytes\n")
    foreign.chmod(0o644)
    mirrored_manifest = mirror_dir / Path(manifest["manifest_path"]).name
    os.link(foreign, mirrored_manifest)
    before = foreign.read_bytes()

    result = mirror_backups(configured)

    assert result["failed"] == 1 and result["copied"] == result["repaired"] == 0
    assert not (mirror_dir / manifest["database"]).exists()
    assert mirrored_manifest.stat().st_ino == foreign.stat().st_ino
    assert foreign.read_bytes() == before and stat.S_IMODE(foreign.stat().st_mode) == 0o644


def test_hardlinked_source_is_refused_before_chmod_or_read(settings, tmp_path):
    from friday.backup_mirror import mirror_files_tree

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    raw = b"source custody bytes\n"
    foreign = tmp_path / "foreign-source"
    foreign.write_bytes(raw)
    foreign.chmod(0o644)
    source = configured.backups_dir / "files" / "u1" / (hashlib.sha256(raw).hexdigest() + ".txt")
    source.parent.mkdir(parents=True)
    os.link(foreign, source)

    result = mirror_files_tree(configured)

    assert result["failed"] == 1 and result["copied"] == 0
    assert foreign.read_bytes() == raw and stat.S_IMODE(foreign.stat().st_mode) == 0o644
    assert foreign.stat().st_nlink == 2


def test_hardlinked_local_manifest_is_refused_before_chmod_or_publication(settings, tmp_path):
    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    local_manifest = Path(manifest["manifest_path"])
    raw = local_manifest.read_bytes()
    local_manifest.unlink()
    foreign = tmp_path / "foreign-local-manifest"
    foreign.write_bytes(raw)
    foreign.chmod(0o644)
    os.link(foreign, local_manifest)

    result = mirror_backups(configured)

    assert result["failed"] == 1 and result["copied"] == 0
    assert not (mirror_dir / manifest["database"]).exists()
    assert foreign.read_bytes() == raw and stat.S_IMODE(foreign.stat().st_mode) == 0o644


def test_duplicate_manifest_key_preserves_existing_recovery_pair(settings, tmp_path):
    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    assert mirror_backups(configured)["copied"] == 1
    local_manifest = Path(manifest["manifest_path"])
    mirrored_manifest = mirror_dir / local_manifest.name
    mirrored_database = mirror_dir / manifest["database"]
    good_manifest = mirrored_manifest.read_bytes()
    good_database = mirrored_database.read_bytes()
    body = json.loads(local_manifest.read_text(encoding="utf-8"))
    duplicate = json.dumps(body)[:-1] + f',"sha256":"{body["sha256"]}"}}'
    local_manifest.write_text(duplicate, encoding="utf-8")

    result = mirror_backups(configured)

    assert result["failed"] == 1 and result["repaired"] == result["copied"] == 0
    assert mirrored_manifest.read_bytes() == good_manifest
    assert mirrored_database.read_bytes() == good_database


def test_target_swap_during_staging_fails_closed_and_cleans_temporary(settings, tmp_path, monkeypatch):
    import friday.backup_mirror as backup_mirror

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    raw = b"race-bound recovery bytes\n"
    source = configured.backups_dir / "files" / "u1" / (hashlib.sha256(raw).hexdigest() + ".txt")
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    target = mirror_dir / "files" / "u1" / source.name
    foreign = tmp_path / "foreign-race-target"
    foreign.write_bytes(b"must remain foreign\n")
    foreign.chmod(0o644)
    original_stage = backup_mirror._stage_payload

    def swap_after_stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        os.link(foreign, target)
        return staged

    monkeypatch.setattr(backup_mirror, "_stage_payload", swap_after_stage)

    result = backup_mirror.mirror_files_tree(configured)

    assert result["failed"] == 1 and result["copied"] == 0
    assert target.stat().st_ino == foreign.stat().st_ino
    assert foreign.read_bytes() == b"must remain foreign\n"
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o644
    assert not list(mirror_dir.rglob(".friday-mirror-*.tmp"))


def test_published_manifest_is_the_validated_captured_bytes(settings, tmp_path, monkeypatch):
    import friday.backup_mirror as backup_mirror

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    local_manifest = Path(manifest["manifest_path"])
    captured = local_manifest.read_bytes()
    original_publish = backup_mirror._publish_pair

    def mutate_after_capture(*args, **kwargs):
        local_manifest.write_text('{"corrupt":true}', encoding="utf-8")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(backup_mirror, "_publish_pair", mutate_after_capture)

    result = backup_mirror.mirror_backups(configured)

    assert result["copied"] == 1 and result["failed"] == 0
    assert (mirror_dir / local_manifest.name).read_bytes() == captured


@pytest.mark.parametrize("candidate", ["files-target", "database-target", "local-manifest"])
def test_fifo_candidates_are_rejected_without_waiting_for_a_writer(settings, tmp_path, candidate):
    """A type check after a blocking FIFO open is not a usable type check."""

    from friday.backup_mirror import mirror_files_tree

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    if candidate == "files-target":
        raw = b"nonblocking FIFO target\n"
        source = configured.backups_dir / "files" / "u1" / (hashlib.sha256(raw).hexdigest() + ".txt")
        source.parent.mkdir(parents=True)
        source.write_bytes(raw)
        fifo = mirror_dir / "files" / "u1" / source.name
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo, mode=0o600)
        invoke = partial(mirror_files_tree, configured, budget_sec=0.05)
    elif candidate == "database-target":
        manifest = _make_backup(configured)
        fifo = mirror_dir / manifest["database"]
        os.mkfifo(fifo, mode=0o600)
        invoke = partial(mirror_backups, configured)
    else:
        fifo = configured.backups_dir / "owned.manifest.json"
        fifo.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo, mode=0o600)
        invoke = partial(mirror_backups, configured)

    started = time.monotonic()
    result = invoke()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result["failed"] == 1 and result["copied"] == 0
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert not list(mirror_dir.rglob(".friday-*.tmp"))


def test_regular_source_swapped_to_fifo_after_inventory_fails_promptly(settings, tmp_path, monkeypatch):
    import friday.backup_mirror as backup_mirror

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    raw = b"inventory race source\n"
    source = configured.backups_dir / "files" / "u1" / (hashlib.sha256(raw).hexdigest() + ".txt")
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    original_inventory = backup_mirror._list_regular_files

    def swap_after_inventory(root_fd):
        candidates = original_inventory(root_fd)
        source.unlink()
        os.mkfifo(source, mode=0o600)
        return candidates

    monkeypatch.setattr(backup_mirror, "_list_regular_files", swap_after_inventory)
    started = time.monotonic()
    result = backup_mirror.mirror_files_tree(configured)

    assert time.monotonic() - started < 1.0
    assert result["failed"] == 1 and result["copied"] == 0
    assert stat.S_ISFIFO(source.lstat().st_mode)
    assert not list((mirror_dir / "files").rglob("*.*"))


@pytest.mark.parametrize("stage_kind", ["payload", "manifest"])
def test_stage_swapped_to_fifo_fails_closed_and_cleans_up(settings, tmp_path, monkeypatch, stage_kind):
    import friday.backup_mirror as backup_mirror

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    if stage_kind == "payload":
        original_stage = backup_mirror._stage_payload

        def swap_payload(*args, **kwargs):
            name, guard = original_stage(*args, **kwargs)
            parent_fd = args[1]
            os.unlink(name, dir_fd=parent_fd)
            os.mkfifo(name, mode=0o600, dir_fd=parent_fd)
            return name, guard

        monkeypatch.setattr(backup_mirror, "_stage_payload", swap_payload)
    else:
        original_stage_bytes = backup_mirror._stage_bytes

        def swap_manifest(parent_fd, data, *, kind):
            name, guard = original_stage_bytes(parent_fd, data, kind=kind)
            if kind == "manifest":
                os.unlink(name, dir_fd=parent_fd)
                os.mkfifo(name, mode=0o600, dir_fd=parent_fd)
            return name, guard

        monkeypatch.setattr(backup_mirror, "_stage_bytes", swap_manifest)

    started = time.monotonic()
    result = backup_mirror.mirror_backups(configured)

    assert time.monotonic() - started < 1.0
    assert result["failed"] == 1 and result["copied"] == 0
    assert not (mirror_dir / manifest["database"]).exists()
    assert not (mirror_dir / Path(manifest["manifest_path"]).name).exists()
    assert not list(mirror_dir.glob(".friday-*.tmp"))


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("fault_target", ["manifest", "payload"])
def test_changed_valid_backup_conflicts_before_pair_publication_fault(
    settings, tmp_path, monkeypatch, encrypted, fault_target
):
    """Immutable-name conflict removes both two-file replacement fault windows."""

    import friday.backup_mirror as backup_mirror

    mirror_dir = _mounted(tmp_path / "mirror")
    key_file = tmp_path / "backup.key" if encrypted else None
    if key_file is not None:
        key_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    configured = replace(
        settings,
        backup_mirror_dir=mirror_dir,
        backup_encryption_key_file=key_file,
    )
    manifest = _make_backup(configured)
    assert backup_mirror.mirror_backups(configured)["copied"] == 1
    source = Path(manifest["path"])
    local_manifest = Path(manifest["manifest_path"])
    target = mirror_dir / (source.name + ".enc" if encrypted else source.name)
    mirrored_manifest = mirror_dir / local_manifest.name
    before = {
        "payload": target.read_bytes(),
        "manifest": mirrored_manifest.read_bytes(),
        "payload_mode": stat.S_IMODE(target.stat().st_mode),
        "manifest_mode": stat.S_IMODE(mirrored_manifest.stat().st_mode),
    }

    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE immutable_name_conflict (value TEXT)")
        connection.execute("INSERT INTO immutable_name_conflict VALUES ('changed')")
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    body = json.loads(local_manifest.read_text(encoding="utf-8"))
    body["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    body["size_bytes"] = source.stat().st_size
    local_manifest.write_text(json.dumps(body) + "\n", encoding="utf-8")

    original_replace = backup_mirror.os.replace
    hits = []
    rejected_name = target.name if fault_target == "payload" else mirrored_manifest.name

    def fail_if_publication_is_attempted(src, dst, *args, **kwargs):
        if str(dst) == rejected_name:
            hits.append(str(dst))
            raise OSError("owned publication fault")
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup_mirror.os, "replace", fail_if_publication_is_attempted)
    result = backup_mirror.mirror_backups(configured)

    assert result["failed"] == result["conflicts"] == 1
    assert result["copied"] == result["repaired"] == 0
    assert hits == [], "a verified immutable pair must be refused before either replace"
    assert target.read_bytes() == before["payload"]
    assert mirrored_manifest.read_bytes() == before["manifest"]
    assert stat.S_IMODE(target.stat().st_mode) == before["payload_mode"] == 0o600
    assert stat.S_IMODE(mirrored_manifest.stat().st_mode) == before["manifest_mode"] == 0o600
    if encrypted:
        decoded = tmp_path / f"decoded-{fault_target}.sqlite3"
        backup_mirror.decrypt_file(target, decoded, key_file)
        surviving_digest = hashlib.sha256(decoded.read_bytes()).hexdigest()
    else:
        surviving_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert json.loads(mirrored_manifest.read_text(encoding="utf-8"))["sha256"] == (surviving_digest)
    assert not list(mirror_dir.glob(".friday-*.tmp"))


def test_self_consistent_non_sqlite_remote_pair_is_repaired(settings, tmp_path):
    """A matching digest alone does not make an old recovery pair valid."""

    mirror_dir = _mounted(tmp_path / "mirror")
    configured = replace(settings, backup_mirror_dir=mirror_dir)
    manifest = _make_backup(configured)
    assert mirror_backups(configured)["copied"] == 1
    target = mirror_dir / manifest["database"]
    mirrored_manifest = mirror_dir / Path(manifest["manifest_path"]).name
    corrupt = b"self-consistent but not SQLite\n"
    target.write_bytes(corrupt)
    body = json.loads(mirrored_manifest.read_text(encoding="utf-8"))
    body["sha256"] = hashlib.sha256(corrupt).hexdigest()
    body["size_bytes"] = len(corrupt)
    mirrored_manifest.write_text(json.dumps(body) + "\n", encoding="utf-8")

    result = mirror_backups(configured)

    assert result["copied"] == result["repaired"] == 1
    assert result["failed"] == 0 and result.get("conflicts", 0) == 0
    assert target.read_bytes() == Path(manifest["path"]).read_bytes()
    assert mirrored_manifest.read_bytes() == Path(manifest["manifest_path"]).read_bytes()


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("size_case", ["exact", "smaller", "larger", "zero"])
def test_existing_pair_conflict_requires_actual_decoded_size(settings, tmp_path, encrypted, size_case):
    """An unrestorable size-mismatched pair must not block a valid repair."""

    mirror_dir = _mounted(tmp_path / "mirror")
    key_file = tmp_path / "backup.key" if encrypted else None
    if key_file is not None:
        key_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    configured = replace(
        settings,
        backup_mirror_dir=mirror_dir,
        backup_encryption_key_file=key_file,
    )
    backup = _make_backup(configured)
    assert mirror_backups(configured)["copied"] == 1
    source = Path(backup["path"])
    local_manifest = Path(backup["manifest_path"])
    target = mirror_dir / (source.name + ".enc" if encrypted else source.name)
    remote_manifest = mirror_dir / local_manifest.name
    original_size = source.stat().st_size
    remote_body = json.loads(remote_manifest.read_text(encoding="utf-8"))
    remote_body["size_bytes"] = {
        "exact": original_size,
        "smaller": original_size - 1,
        "larger": original_size + 1,
        "zero": 0,
    }[size_case]
    remote_manifest.write_text(json.dumps(remote_body) + "\n", encoding="utf-8")
    before_payload = target.read_bytes()
    before_manifest = remote_manifest.read_bytes()

    def verify_recovery(label):
        recovery = _mounted(tmp_path / label)
        recovered = recovery / source.name
        if encrypted:
            decrypt_file(target, recovered, key_file)
        else:
            recovered.write_bytes(target.read_bytes())
        (recovery / remote_manifest.name).write_bytes(remote_manifest.read_bytes())
        storage = init_storage(replace(configured, backups_dir=recovery))
        try:
            return storage.verify_backup(recovered.name)
        finally:
            storage.close()

    initial_verification = verify_recovery("recovery-before")
    assert initial_verification["hash_matches_manifest"] is True
    assert initial_verification["integrity_check"] == "ok"
    assert initial_verification["ok"] is (size_case == "exact")
    assert initial_verification["manifest_size_matches"] is (size_case == "exact")

    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE changed_valid_backup (value TEXT)")
        connection.execute("INSERT INTO changed_valid_backup VALUES ('new snapshot')")
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        connection.close()
    body = json.loads(local_manifest.read_text(encoding="utf-8"))
    body["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    body["size_bytes"] = source.stat().st_size
    assert body["sha256"] != backup["sha256"]
    local_manifest.write_text(json.dumps(body) + "\n", encoding="utf-8")

    result = mirror_backups(configured)

    if size_case == "exact":
        assert result["failed"] == result["conflicts"] == 1
        assert result["copied"] == result["repaired"] == 0
        assert target.read_bytes() == before_payload
        assert remote_manifest.read_bytes() == before_manifest
        expected_digest = backup["sha256"]
    else:
        assert result["failed"] == 0
        assert result.get("conflicts", 0) == 0
        assert result["copied"] == result["repaired"] == 1
        assert remote_manifest.read_bytes() == local_manifest.read_bytes()
        expected_digest = body["sha256"]
    final_verification = verify_recovery("recovery-after")
    assert final_verification["ok"] is True
    assert final_verification["sha256"] == expected_digest
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(remote_manifest.stat().st_mode) == 0o600
    assert not list(mirror_dir.glob(".friday-*.tmp"))
