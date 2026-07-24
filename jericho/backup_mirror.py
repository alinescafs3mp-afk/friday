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


def mirror_backups(settings: Any) -> dict[str, Any]:
    """Copy every verified backup that is missing from the mirror. Idempotent."""
    mirror_dir: Path | None = settings.backup_mirror_dir
    if mirror_dir is None:
        return {"enabled": False}
    key_file: Path | None = settings.backup_encryption_key_file
    mirror_dir.mkdir(parents=True, exist_ok=True)

    copied = skipped = failed = 0
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
        if target.exists():
            skipped += 1
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
            os.replace(tmp, target)
            with_mode = target
            with_mode.chmod(0o600)
            # The manifest rides along in plain form: it carries hashes and
            # metadata, not content, and is needed to verify/restore offsite.
            shutil.copy2(manifest_path, mirror_dir / manifest_path.name)
            (mirror_dir / manifest_path.name).chmod(0o600)
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
        "failed": failed,
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
