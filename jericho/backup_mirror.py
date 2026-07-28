"""Offsite mirroring (and optional encryption) of verified SQLite backups.

A backup on the same disk protects only against application bugs, not against
the disk dying — so every verified backup is copied to a configured mirror
directory (external drive, NFS mount, synced folder). When an encryption key
file is configured, the MIRROR copy is AES-256 encrypted via the system
``openssl`` binary (no new Python dependency); local copies stay plain so an
emergency restore is one file rename away.

Every mirrored copy is verified: a plain copy by SHA-256 against the backup's
manifest, an encrypted copy by decrypting it back and comparing the digest —
an unverified offsite copy is worse than none, it is false confidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - fixed openssl argv, no shell
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

_OPENSSL_ARGS = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt"]


class BackupMirrorError(RuntimeError):
    """Mirroring or crypto failed in a way the operator must see."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_openssl(args: list[str]) -> None:
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["openssl", *args],
            capture_output=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupMirrorError("openssl не найден — шифрование бэкапов требует системный openssl") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:300]
        raise BackupMirrorError(f"openssl завершился с ошибкой: {detail}")


def encrypt_file(source: Path, destination: Path, key_file: Path) -> None:
    if not key_file.is_file():
        raise BackupMirrorError(f"Файл ключа шифрования не найден: {key_file}")
    _run_openssl([*_OPENSSL_ARGS, "-in", str(source), "-out", str(destination), "-pass", f"file:{key_file}"])


def decrypt_file(source: Path, destination: Path, key_file: Path) -> None:
    if not key_file.is_file():
        raise BackupMirrorError(f"Файл ключа шифрования не найден: {key_file}")
    _run_openssl(
        [
            *_OPENSSL_ARGS,
            "-d",
            "-in",
            str(source),
            "-out",
            str(destination),
            "-pass",
            f"file:{key_file}",
        ]
    )


def _verify_encrypted_copy(encrypted: Path, key_file: Path, expected_sha256: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".jericho-verify-", suffix=".sqlite3")
    os.close(descriptor)
    probe = Path(name)
    try:
        decrypt_file(encrypted, probe, key_file)
        actual = _sha256(probe)
        if actual != expected_sha256:
            raise BackupMirrorError("Расшифрованная копия не совпала с манифестом (sha256)")
    finally:
        probe.unlink(missing_ok=True)


def _publish_manifest(source: Path, target: Path) -> None:
    """Copy a manifest into place atomically, so a torn write is never visible."""
    tmp = target.with_name(target.name + ".tmp")
    try:
        shutil.copy2(source, tmp)
        tmp.chmod(0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def mirror_backups(settings: Any) -> dict[str, Any]:
    """Copy every verified backup that is missing from the mirror. Idempotent."""
    mirror_dir: Path | None = settings.backup_mirror_dir
    if mirror_dir is None:
        return {"enabled": False}
    key_file: Path | None = settings.backup_encryption_key_file
    if not mirror_dir.is_dir():
        # Never create it. `mkdir(parents=True)` on an unmounted external disk
        # silently makes a directory AT the mount point — on the same physical
        # disk — and then reports a successful mirror. "A copy on the same disk is
        # not a backup" is this module's whole premise, and that is precisely what
        # it produced, quietly, for as long as the disk stayed unplugged.
        LOGGER.error("Каталог зеркала не смонтирован или отсутствует: %s", mirror_dir)
        return {
            "enabled": True,
            "mirror_dir": str(mirror_dir),
            "error": "mirror_dir_missing",
            "copied": 0,
            "skipped_existing": 0,
            "repaired": 0,
            "failed": 0,
        }
    same_device = False
    with suppress(OSError):
        same_device = mirror_dir.stat().st_dev == settings.backups_dir.stat().st_dev
    if same_device:
        LOGGER.warning(
            "Зеркало %s лежит на том же устройстве, что и бэкапы — это не offsite-копия",
            mirror_dir,
        )

    copied = skipped = failed = repaired = 0
    leftovers: list[str] = []
    for manifest_path in sorted(settings.backups_dir.glob("*.manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed += 1
            continue
        database = str(manifest.get("database") or "")
        expected_sha = str(manifest.get("sha256") or "")
        source = settings.backups_dir / database
        if not database or not expected_sha or not source.is_file():
            continue
        target = mirror_dir / (f"{database}.enc" if key_file else database)
        mirrored_manifest = mirror_dir / manifest_path.name
        # Turning encryption ON does not remove what was mirrored before it. The
        # target's NAME depends on the key, so the .enc copy lands BESIDE the old
        # plaintext one rather than replacing it — and nothing ever deletes it: this
        # module unlinks only its own temp file, and `prune_backups` walks
        # `backups_dir`, never the mirror. The mirror is by design an external or
        # synced volume that is not trusted, which is the whole reason to encrypt, so
        # a readable database sitting there indefinitely is the thing encryption was
        # turned on to prevent. Counted BEFORE the skip below, or the first encrypted
        # run would report it once and never again.
        if key_file is not None and (mirror_dir / database).exists():
            leftovers.append(database)
            LOGGER.warning("В зеркале осталась НЕзашифрованная копия %s — удалите её вручную", database)
        # A database and its manifest are ONE unit. Skipping on the database alone
        # meant that an interruption between `os.replace` and the manifest copy left
        # a manifest-less database as the durable state — and every later run saw
        # the file, skipped it, and moved on. That copy cannot be verified or
        # restored: `verify_backup` needs the manifest. The `except` branch below
        # could not clean it up either, since it only unlinks `tmp`, which
        # `os.replace` has already consumed.
        if target.exists() and mirrored_manifest.exists():
            skipped += 1
            continue
        if target.exists() and not mirrored_manifest.exists():
            try:
                _publish_manifest(manifest_path, mirrored_manifest)
                repaired += 1
                LOGGER.info("Восстановлен манифест зеркальной копии %s", database)
            except Exception:
                failed += 1
                LOGGER.warning("Не удалось восстановить манифест для %s", database, exc_info=True)
            continue
        tmp = target.with_name(target.name + ".tmp")
        try:
            if key_file:
                encrypt_file(source, tmp, key_file)
                _verify_encrypted_copy(tmp, key_file, expected_sha)
            else:
                shutil.copy2(source, tmp)
                if _sha256(tmp) != expected_sha:
                    raise BackupMirrorError("Копия не совпала с манифестом (sha256)")
            # Manifest FIRST. A manifest without its database is the harmless
            # half of the pair — `verify_backup` reports a missing file and the
            # next run completes the copy — while a database without its manifest
            # is unrestorable and, before this, permanently skipped.
            _publish_manifest(manifest_path, mirrored_manifest)
            os.replace(tmp, target)
            target.chmod(0o600)
            copied += 1
        except Exception:
            failed += 1
            tmp.unlink(missing_ok=True)
            LOGGER.warning("Не удалось отзеркалировать бэкап %s", database, exc_info=True)
    report = {
        "enabled": True,
        "mirror_dir": str(mirror_dir),
        "encrypted": key_file is not None,
        "copied": copied,
        "skipped_existing": skipped,
        "repaired": repaired,
        "failed": failed,
        # Named for what an operator has to DO about it. `encrypted` is an echo of the
        # configuration and stays true — this run did encrypt; it says nothing about
        # what is already lying in the directory.
        "plaintext_leftovers": sorted(leftovers),
        "same_device": same_device,
    }
    if copied or failed:
        LOGGER.info(
            "Зеркалирование бэкапов: скопировано %d, пропущено %d, ошибок %d (%s)",
            copied,
            skipped,
            failed,
            mirror_dir,
        )
    return report
