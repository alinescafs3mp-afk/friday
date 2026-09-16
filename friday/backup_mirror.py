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
import hmac
import json
import logging
import os
import secrets
import sqlite3
import stat
import subprocess  # nosec B404 - fixed openssl argv, no shell
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from friday.backup_files import _digest_from_name
from friday.private_fs import (
    ensure_private_directory,
    prepare_private_file,
    restrict_private_file,
)
from friday.storage.backup_contract import validate_backup_manifest

LOGGER = logging.getLogger(__name__)

_OPENSSL_ARGS = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt"]
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_MAX_MANIFEST_BYTES = 1024 * 1024


class BackupMirrorError(RuntimeError):
    """Mirroring or crypto failed in a way the operator must see."""


class BackupMirrorConflict(BackupMirrorError):
    """A different, already verified recovery pair owns the immutable name."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_openssl(args: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["openssl", *args],
            capture_output=True,
            timeout=600,
            check=False,
            pass_fds=pass_fds,
        )
    except FileNotFoundError as exc:
        raise BackupMirrorError("openssl не найден — шифрование бэкапов требует системный openssl") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:300]
        raise BackupMirrorError(f"openssl завершился с ошибкой: {detail}")


def encrypt_file(source: Path, destination: Path, key_file: Path) -> None:
    if not key_file.is_file():
        raise BackupMirrorError(f"Файл ключа шифрования не найден: {key_file}")
    prepare_private_file(destination)
    _run_openssl([*_OPENSSL_ARGS, "-in", str(source), "-out", str(destination), "-pass", f"file:{key_file}"])
    restrict_private_file(destination)


def decrypt_file(source: Path, destination: Path, key_file: Path, *, overwrite: bool = True) -> None:
    """Publish plaintext only after the complete decryption succeeds."""

    if not key_file.is_file():
        raise BackupMirrorError(f"Файл ключа шифрования не найден: {key_file}")
    parent = destination.parent
    if parent.is_symlink() or destination.is_symlink():
        raise BackupMirrorError("Путь расшифровки не должен быть символической ссылкой")
    if not parent.exists():
        ensure_private_directory(parent)
    try:
        with tempfile.TemporaryDirectory(prefix=".friday-decrypt-", dir=parent) as temporary:
            pending = Path(temporary) / "plaintext"
            prepare_private_file(pending)
            _run_openssl(
                [
                    *_OPENSSL_ARGS,
                    "-d",
                    "-in",
                    str(source),
                    "-out",
                    str(pending),
                    "-pass",
                    f"file:{key_file}",
                ]
            )
            with pending.open("rb") as handle:
                os.fsync(handle.fileno())
            if overwrite:
                if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                    raise BackupMirrorError("Файл назначения расшифровки не является обычным файлом")
                os.replace(pending, destination)
            else:
                # A racing creator, including a dangling symlink, must win:
                # the CLI promises never to overwrite an existing destination.
                os.link(pending, destination, follow_symlinks=False)
    except OSError as exc:
        raise BackupMirrorError("Не удалось опубликовать расшифрованный файл") from exc


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags() -> int:
    # Opening a FIFO read-only normally waits forever for a writer.  Type
    # validation necessarily happens after open, so make every candidate open
    # non-blocking; regular-file semantics are unchanged by O_NONBLOCK.
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode) == (right.st_dev, right.st_ino, right.st_mode)


def _guard_identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_mode),
        int(status.st_nlink),
        int(status.st_uid),
        int(status.st_size),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _validate_owned_directory(status: os.stat_result) -> None:
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        raise BackupMirrorError("Каталог зеркала не является приватным каталогом владельца")


def _validate_owned_regular(status: os.stat_result) -> None:
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != os.geteuid():
        raise BackupMirrorError("Файл зеркала не является независимым приватным файлом владельца")


def _open_absolute_directory(path: Path, *, create_leaf: bool = False) -> int:
    """Pin an absolute directory without following any configured component."""

    candidate = Path(os.path.abspath(path))
    if not candidate.anchor or candidate == Path(candidate.anchor):
        raise BackupMirrorError("Корневой каталог нельзя использовать как зеркало")
    descriptor = os.open(candidate.anchor, _directory_flags())
    try:
        for index, component in enumerate(candidate.parts[1:]):
            final = index == len(candidate.parts[1:]) - 1
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not (create_leaf and final):
                    raise
                os.mkdir(component, mode=_PRIVATE_DIRECTORY_MODE, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            opened = os.fstat(child)
            lexical = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(opened.st_mode) or not _same_identity(opened, lexical):
                os.close(child)
                raise BackupMirrorError("Каталог изменился во время безопасного открытия")
            os.close(descriptor)
            descriptor = child
        status = os.fstat(descriptor)
        _validate_owned_directory(status)
        os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _relative_parts(relative: Path) -> tuple[str, ...]:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts:
        raise BackupMirrorError("Недопустимый относительный путь зеркала")
    parts = tuple(candidate.parts)
    if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in parts):
        raise BackupMirrorError("Недопустимый относительный путь зеркала")
    return parts


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, mode=_PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
        os.fsync(parent_fd)
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    lexical = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_identity(opened, lexical):
        os.close(descriptor)
        raise BackupMirrorError("Каталог зеркала изменился во время безопасного открытия")
    try:
        _validate_owned_directory(opened)
        os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _open_relative_directory(root_fd: int, relative: Path, *, create: bool) -> Iterator[int]:
    descriptor = os.dup(root_fd)
    try:
        if str(relative) not in {"", "."}:
            for component in _relative_parts(relative):
                child = _open_child_directory(descriptor, component, create=create)
                os.close(descriptor)
                descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_regular_at(parent_fd: int, name: str) -> Iterator[tuple[int, os.stat_result]]:
    descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        lexical = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(opened, lexical):
            raise BackupMirrorError("Файл изменился во время безопасного открытия")
        _validate_owned_regular(opened)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        yield descriptor, os.fstat(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _open_regular_relative(root_fd: int, relative: Path) -> Iterator[tuple[int, os.stat_result]]:
    parts = _relative_parts(relative)
    parent = Path(*parts[:-1]) if len(parts) > 1 else Path()
    with (
        _open_relative_directory(root_fd, parent, create=False) as parent_fd,
        _open_regular_at(parent_fd, parts[-1]) as opened,
    ):
        yield opened


def _read_fd(descriptor: int, *, max_bytes: int | None = None) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise BackupMirrorError("Манифест превышает допустимый размер")
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - operating-system contract guard
            raise OSError("short write")
        view = view[written:]


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _verify_database_fd(descriptor: int, expected_schema: int) -> None:
    path = f"/proc/self/fd/{descriptor}"
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise BackupMirrorError("Локальный backup не является проверяемой SQLite-базой") from exc
    if integrity != [("ok",)] or foreign or row is None:
        raise BackupMirrorError("Локальный backup не прошёл SQLite-проверку")
    try:
        observed_schema = int(row[0])
    except (TypeError, ValueError) as exc:
        raise BackupMirrorError("Локальный backup содержит некорректную версию схемы") from exc
    if observed_schema != expected_schema:
        raise BackupMirrorError("Версия схемы backup не совпала с манифестом")


def _capture_manifest(
    backups_fd: int,
    manifest_name: str,
) -> tuple[bytes, str, str, int]:
    with _open_regular_at(backups_fd, manifest_name) as (manifest_fd, _manifest_status):
        raw = _read_fd(manifest_fd, max_bytes=_MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_json_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupMirrorError("Манифест backup не является корректным JSON") from exc
    if not isinstance(manifest, dict):
        raise BackupMirrorError("Корень манифеста backup должен быть объектом")
    try:
        contract = validate_backup_manifest(manifest, manifest_name=manifest_name)
    except ValueError as exc:
        raise BackupMirrorError("Манифест backup не прошёл канонический контракт") from exc
    with _open_regular_at(backups_fd, contract.database) as (database_fd, database_status):
        _verify_database_fd(database_fd, contract.schema_version)
        actual_sha = _sha256_fd(database_fd)
        try:
            validate_backup_manifest(
                manifest,
                manifest_name=manifest_name,
                actual_size_bytes=database_status.st_size,
                actual_sha256=actual_sha,
                actual_schema_version=contract.schema_version,
            )
        except ValueError as exc:
            raise BackupMirrorError("Локальный backup не совпал с манифестом") from exc
    return raw, contract.database, contract.sha256, contract.size_bytes


def _replacement_guard(parent_fd: int, name: str) -> tuple[int, ...] | None:
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _validate_owned_regular(status)
    return _guard_identity(status)


def _assert_replacement_guard(parent_fd: int, name: str, expected: tuple[int, ...] | None) -> None:
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if expected is None:
            return
        raise BackupMirrorError("Файл назначения исчез во время публикации") from None
    if expected is None or _guard_identity(status) != expected:
        raise BackupMirrorError("Файл назначения изменился во время публикации")
    _validate_owned_regular(status)


def _temporary_name(kind: str) -> str:
    return f".friday-{kind}-{secrets.token_hex(16)}.tmp"


def _stage_bytes(parent_fd: int, data: bytes, *, kind: str) -> tuple[str, tuple[int, ...]]:
    name = _temporary_name(kind)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, _PRIVATE_FILE_MODE, dir_fd=parent_fd)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        _validate_owned_regular(status)
        guard = _guard_identity(status)
    except BaseException:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent_fd)
        raise
    os.close(descriptor)
    return name, guard


def _copy_fd_to_stage(source_fd: int, parent_fd: int, name: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_fd = os.open(name, flags, _PRIVATE_FILE_MODE, dir_fd=parent_fd)
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            _write_all(destination_fd, block)
        os.fsync(destination_fd)
        _validate_owned_regular(os.fstat(destination_fd))
    finally:
        os.lseek(source_fd, 0, os.SEEK_SET)
        os.close(destination_fd)


def _verify_encrypted_fd(
    encrypted_fd: int,
    key_file: Path,
    expected_sha256: str,
    *,
    expected_schema: int | None = None,
) -> int:
    """Verify decoded bytes and return their size for the manifest contract."""
    if not key_file.is_file():
        raise BackupMirrorError(f"Файл ключа шифрования не найден: {key_file}")
    descriptor, name = tempfile.mkstemp(prefix=".jericho-verify-", suffix=".sqlite3")
    os.fchmod(descriptor, _PRIVATE_FILE_MODE)
    try:
        _run_openssl(
            [
                *_OPENSSL_ARGS,
                "-d",
                "-in",
                f"/proc/self/fd/{encrypted_fd}",
                "-out",
                f"/proc/self/fd/{descriptor}",
                "-pass",
                f"file:{key_file}",
            ],
            pass_fds=(encrypted_fd, descriptor),
        )
        os.fsync(descriptor)
        decoded_status = os.fstat(descriptor)
        _validate_owned_regular(decoded_status)
        if not hmac.compare_digest(_sha256_fd(descriptor), expected_sha256):
            raise BackupMirrorError("Расшифрованная копия не совпала с ожидаемым sha256")
        if expected_schema is not None:
            _verify_database_fd(descriptor, expected_schema)
        return decoded_status.st_size
    finally:
        os.close(descriptor)
        Path(name).unlink(missing_ok=True)


def _stage_payload(
    source_fd: int,
    parent_fd: int,
    key_file: Path | None,
    expected_sha: str,
) -> tuple[str, tuple[int, ...]]:
    name = _temporary_name("mirror")
    try:
        if key_file is None:
            _copy_fd_to_stage(source_fd, parent_fd, name)
        else:
            if not key_file.is_file():
                raise BackupMirrorError(f"Файл ключа шифрования не найден: {key_file}")
            flags = (
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            pending_fd = os.open(name, flags, _PRIVATE_FILE_MODE, dir_fd=parent_fd)
            try:
                _run_openssl(
                    [
                        *_OPENSSL_ARGS,
                        "-in",
                        f"/proc/self/fd/{source_fd}",
                        "-out",
                        f"/proc/self/fd/{pending_fd}",
                        "-pass",
                        f"file:{key_file}",
                    ],
                    pass_fds=(source_fd, pending_fd),
                )
                os.fsync(pending_fd)
                _validate_owned_regular(os.fstat(pending_fd))
            finally:
                os.close(pending_fd)
        with _open_regular_at(parent_fd, name) as (pending_fd, _status):
            if key_file is None:
                if not hmac.compare_digest(_sha256_fd(pending_fd), expected_sha):
                    raise BackupMirrorError("Копия не совпала с ожидаемым sha256")
            else:
                _verify_encrypted_fd(pending_fd, key_file, expected_sha)
            guard = _guard_identity(os.fstat(pending_fd))
        return name, guard
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent_fd)
        raise


def _assert_stage_guard(
    parent_fd: int,
    staged_name: str,
    expected_stage: tuple[int, ...],
) -> None:
    staged = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
    _validate_owned_regular(staged)
    if _guard_identity(staged) != expected_stage:
        raise BackupMirrorError("Временный файл изменился до публикации")


def _replace_stage(
    parent_fd: int,
    staged_name: str,
    target_name: str,
    expected_stage: tuple[int, ...],
) -> None:
    _assert_stage_guard(parent_fd, staged_name, expected_stage)
    os.replace(staged_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _publish_bytes(parent_fd: int, target_name: str, data: bytes) -> None:
    guard = _replacement_guard(parent_fd, target_name)
    staged_name, staged_guard = _stage_bytes(parent_fd, data, kind="manifest")
    try:
        _assert_replacement_guard(parent_fd, target_name, guard)
        _replace_stage(parent_fd, staged_name, target_name, staged_guard)
        os.fsync(parent_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(staged_name, dir_fd=parent_fd)


def _publish_pair(
    parent_fd: int,
    *,
    target_name: str,
    manifest_name: str,
    source_fd: int,
    manifest_bytes: bytes,
    key_file: Path | None,
    expected_sha: str,
) -> None:
    target_guard = _replacement_guard(parent_fd, target_name)
    manifest_guard = _replacement_guard(parent_fd, manifest_name)
    payload_stage, payload_stage_guard = _stage_payload(
        source_fd,
        parent_fd,
        key_file,
        expected_sha,
    )
    manifest_stage = ""
    manifest_stage_guard: tuple[int, ...] | None = None
    try:
        manifest_stage, manifest_stage_guard = _stage_bytes(
            parent_fd,
            manifest_bytes,
            kind="manifest",
        )
        _assert_replacement_guard(parent_fd, target_name, target_guard)
        _assert_replacement_guard(parent_fd, manifest_name, manifest_guard)
        # Validate the complete candidate pair before either public name moves.
        # In particular, a staged payload swapped to a FIFO must not leave a
        # newly published manifest behind when its own publication is refused.
        _assert_stage_guard(parent_fd, payload_stage, payload_stage_guard)
        _assert_stage_guard(parent_fd, manifest_stage, manifest_stage_guard)
        # A recovery manifest must be durable before bytes carrying its digest.
        _replace_stage(parent_fd, manifest_stage, manifest_name, manifest_stage_guard)
        manifest_stage = ""
        _replace_stage(parent_fd, payload_stage, target_name, payload_stage_guard)
        payload_stage = ""
        os.fsync(parent_fd)
    finally:
        for staged_name in (manifest_stage, payload_stage):
            if staged_name:
                with suppress(FileNotFoundError):
                    os.unlink(staged_name, dir_fd=parent_fd)


def _publish_payload(
    parent_fd: int,
    target_name: str,
    source_fd: int,
    key_file: Path | None,
    expected_sha: str,
) -> None:
    guard = _replacement_guard(parent_fd, target_name)
    staged_name, staged_guard = _stage_payload(
        source_fd,
        parent_fd,
        key_file,
        expected_sha,
    )
    try:
        _assert_replacement_guard(parent_fd, target_name, guard)
        _replace_stage(parent_fd, staged_name, target_name, staged_guard)
        os.fsync(parent_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(staged_name, dir_fd=parent_fd)


def _existing_matches(
    parent_fd: int,
    name: str,
    key_file: Path | None,
    expected_sha: str,
) -> tuple[bool, bool]:
    try:
        with _open_regular_at(parent_fd, name) as (descriptor, _status):
            if key_file is None:
                matches = hmac.compare_digest(_sha256_fd(descriptor), expected_sha)
            else:
                try:
                    _verify_encrypted_fd(descriptor, key_file, expected_sha)
                    matches = True
                except BackupMirrorError:
                    matches = False
            return True, matches
    except FileNotFoundError:
        return False, False


def _existing_bytes(parent_fd: int, name: str) -> bytes | None:
    try:
        with _open_regular_at(parent_fd, name) as (descriptor, _status):
            return _read_fd(descriptor, max_bytes=_MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        return None


def _existing_recovery_pair_sha(
    parent_fd: int,
    *,
    target_name: str,
    manifest_name: str,
    key_file: Path | None,
) -> str | None:
    """Return the digest of an existing coherent pair, or ``None`` if torn.

    The pair is deliberately validated independently of the incoming source.
    A verified immutable backup already published under the canonical name must
    not be destroyed merely because a later source reuses that name.
    """

    raw = _existing_bytes(parent_fd, manifest_name)
    if raw is None:
        return None
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_json_object)
        if not isinstance(manifest, dict):
            return None
        contract = validate_backup_manifest(manifest, manifest_name=manifest_name)
    except (UnicodeDecodeError, ValueError):
        return None
    paired_target = f"{contract.database}.enc" if key_file else contract.database
    if paired_target != target_name:
        return None
    try:
        with _open_regular_at(parent_fd, target_name) as (descriptor, status):
            if key_file is None:
                if not hmac.compare_digest(_sha256_fd(descriptor), contract.sha256):
                    return None
                _verify_database_fd(descriptor, contract.schema_version)
                decoded_size = status.st_size
            else:
                decoded_size = _verify_encrypted_fd(
                    descriptor,
                    key_file,
                    contract.sha256,
                    expected_schema=contract.schema_version,
                )
            validate_backup_manifest(
                manifest,
                manifest_name=manifest_name,
                actual_size_bytes=decoded_size,
            )
    except FileNotFoundError:
        return None
    except (BackupMirrorError, ValueError):
        return None
    return contract.sha256


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _list_regular_files(root_fd: int) -> list[Path]:
    """Inventory a pinned source tree without following a lexical symlink."""

    found: list[Path] = []

    def visit(directory_fd: int, prefix: Path) -> None:
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = prefix / name
            if stat.S_ISLNK(status.st_mode):
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd = _open_child_directory(directory_fd, name, create=False)
                try:
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(status.st_mode):
                found.append(relative)

    visit(root_fd, Path())
    return found


def mirror_backups(settings: Any) -> dict[str, Any]:
    """Copy every verified backup that is missing from the mirror. Idempotent."""
    mirror_dir: Path | None = settings.backup_mirror_dir
    if mirror_dir is None:
        return {"enabled": False}
    key_file: Path | None = settings.backup_encryption_key_file
    try:
        mirror_fd = _open_absolute_directory(mirror_dir)
    except (OSError, BackupMirrorError):
        # Never create it. Doing so at an unmounted external-disk path silently
        # creates a same-disk directory and calls it an offsite copy.
        LOGGER.error("Каталог зеркала не смонтирован или отсутствует")
        return {
            "enabled": True,
            "mirror_dir": str(mirror_dir),
            "error": "mirror_dir_missing",
            "copied": 0,
            "skipped_existing": 0,
            "repaired": 0,
            "failed": 0,
        }
    backups_fd = -1
    try:
        try:
            backups_fd = _open_absolute_directory(settings.backups_dir, create_leaf=True)
        except (OSError, BackupMirrorError):
            return {
                "enabled": True,
                "mirror_dir": str(mirror_dir),
                "error": "backups_dir_invalid",
                "copied": 0,
                "skipped_existing": 0,
                "repaired": 0,
                "failed": 1,
            }
        same_device = os.fstat(mirror_fd).st_dev == os.fstat(backups_fd).st_dev
        if same_device:
            LOGGER.warning("Зеркало лежит на том же устройстве, что и бэкапы — это не offsite-копия")

        copied = skipped = failed = repaired = conflicts = 0
        leftovers: list[str] = []
        manifest_names = sorted(
            entry.name for entry in os.scandir(backups_fd) if entry.name.endswith(".manifest.json")
        )
        for manifest_name in manifest_names:
            try:
                manifest_bytes, database, expected_sha, _size = _capture_manifest(
                    backups_fd,
                    manifest_name,
                )
                target_name = f"{database}.enc" if key_file else database
                if key_file is not None and _entry_exists(mirror_fd, database):
                    leftovers.append(database)
                    LOGGER.warning("В зеркале осталась НЕзашифрованная копия — удалите её вручную")
                target_existed, target_matches = _existing_matches(
                    mirror_fd,
                    target_name,
                    key_file,
                    expected_sha,
                )
                if target_existed and target_matches:
                    mirrored_manifest = _existing_bytes(mirror_fd, manifest_name)
                    if mirrored_manifest == manifest_bytes:
                        skipped += 1
                        continue
                    _publish_bytes(mirror_fd, manifest_name, manifest_bytes)
                    repaired += 1
                    LOGGER.info("Восстановлен манифест зеркальной копии")
                    continue
                if target_existed:
                    existing_pair_sha = _existing_recovery_pair_sha(
                        mirror_fd,
                        target_name=target_name,
                        manifest_name=manifest_name,
                        key_file=key_file,
                    )
                    if existing_pair_sha is not None:
                        raise BackupMirrorConflict("Проверенная immutable-копия уже занимает это имя")
                with _open_regular_at(backups_fd, database) as (source_fd, source_status):
                    # Reconfirm the exact source after target inspection and before
                    # publication; only captured validated bytes may reach recovery.
                    if source_status.st_size != _size or not hmac.compare_digest(
                        _sha256_fd(source_fd), expected_sha
                    ):
                        raise BackupMirrorError("Локальный backup изменился после проверки")
                    _publish_pair(
                        mirror_fd,
                        target_name=target_name,
                        manifest_name=manifest_name,
                        source_fd=source_fd,
                        manifest_bytes=manifest_bytes,
                        key_file=key_file,
                        expected_sha=expected_sha,
                    )
                copied += 1
                repaired += int(target_existed)
            except BackupMirrorConflict:
                conflicts += 1
                failed += 1
                LOGGER.warning("Имя backup уже занято другой проверенной immutable-копией")
            except Exception as exc:
                failed += 1
                LOGGER.warning("Не удалось отзеркалировать бэкап (%s)", type(exc).__name__)
        report = {
            "enabled": True,
            "mirror_dir": str(mirror_dir),
            "encrypted": key_file is not None,
            "copied": copied,
            "skipped_existing": skipped,
            "repaired": repaired,
            "failed": failed,
            "plaintext_leftovers": sorted(leftovers),
            "same_device": same_device,
        }
        if conflicts:
            report["conflicts"] = conflicts
        if copied or failed:
            LOGGER.info(
                "Зеркалирование бэкапов: скопировано %d, пропущено %d, ошибок %d",
                copied,
                skipped,
                failed,
            )
        return report
    finally:
        if backups_fd >= 0:
            os.close(backups_fd)
        os.close(mirror_fd)


def mirror_files_tree(settings: Any, *, budget_sec: float = 300.0) -> dict[str, Any]:
    """Зеркалирование дерева оригиналов файлов (``backups/files/``). Идемпотентно.

    `mirror_backups` ходит по манифестам БД, и дерево файлов в зеркало не ехало:
    оригиналы документов — то, ради чего provenance строился, — оставались без
    offsite-копии даже у того, кто зеркало включил. Файлы content-addressed и
    неизменяемы, поэтому инкремент — «чего в зеркале нет»; удаления НЕ
    распространяются. С ключом каждый файл шифруется той же схемой, что базы, и
    проверяется расшифровкой; без ключа — копией и сверкой sha256.
    """
    mirror_dir: Path | None = settings.backup_mirror_dir
    if mirror_dir is None:
        return {"enabled": False}
    source_root: Path = settings.backups_dir / "files"
    try:
        source_fd = _open_absolute_directory(source_root)
    except FileNotFoundError:
        return {
            "enabled": True,
            "state": "no_files_backup_yet",
            "copied": 0,
            "failed": 0,
            "complete": True,
        }
    except (OSError, BackupMirrorError):
        return {
            "enabled": True,
            "error": "files_backup_invalid",
            "copied": 0,
            "failed": 1,
            "complete": False,
        }
    try:
        mirror_fd = _open_absolute_directory(mirror_dir)
    except (OSError, BackupMirrorError):
        os.close(source_fd)
        return {
            "enabled": True,
            "error": "mirror_dir_missing",
            "mirror_dir": str(mirror_dir),
            "copied": 0,
            "failed": 0,
            "complete": False,
        }
    key_file: Path | None = settings.backup_encryption_key_file
    import time

    target_root_fd = -1
    try:
        try:
            target_root_fd = _open_child_directory(mirror_fd, "files", create=True)
        except (OSError, BackupMirrorError):
            return {
                "enabled": True,
                "error": "mirror_files_root_invalid",
                "mirror_dir": str(mirror_dir),
                "copied": 0,
                "failed": 1,
                "complete": False,
            }
        started = time.monotonic()
        total = copied = skipped = failed = pending = repaired = 0
        try:
            candidates = _list_regular_files(source_fd)
        except (OSError, BackupMirrorError):
            return {
                "enabled": True,
                "error": "files_backup_inventory_invalid",
                "mirror_dir": str(mirror_dir),
                "copied": 0,
                "failed": 1,
                "complete": False,
            }
        for relative in candidates:
            total += 1
            if time.monotonic() - started > budget_sec:
                pending += 1
                continue
            try:
                with _open_regular_relative(source_fd, relative) as (opened_source, _source_status):
                    actual_sha = _sha256_fd(opened_source)
                    addressed_sha = _digest_from_name(relative.name)
                    expected_sha = addressed_sha or actual_sha
                    if addressed_sha is not None and not hmac.compare_digest(actual_sha, addressed_sha):
                        raise BackupMirrorError("Оригинал файла не совпал с content-addressed sha256")
                    target_name = f"{relative.name}.enc" if key_file else relative.name
                    with _open_relative_directory(
                        target_root_fd,
                        relative.parent,
                        create=True,
                    ) as target_parent_fd:
                        target_existed, target_matches = _existing_matches(
                            target_parent_fd,
                            target_name,
                            key_file,
                            expected_sha,
                        )
                        if target_existed and target_matches:
                            skipped += 1
                            continue
                        if target_existed and addressed_sha is None:
                            raise BackupMirrorError(
                                "Нельзя заменить отличающуюся legacy-копию без доверенного digest"
                            )
                        if time.monotonic() - started > budget_sec:
                            pending += 1
                            continue
                        _publish_payload(
                            target_parent_fd,
                            target_name,
                            opened_source,
                            key_file,
                            expected_sha,
                        )
                        copied += 1
                        repaired += int(target_existed)
            except Exception as exc:
                failed += 1
                LOGGER.warning("Не удалось отзеркалировать файл (%s)", type(exc).__name__)
        report = {
            "enabled": True,
            "mirror_dir": str(mirror_dir),
            "encrypted": key_file is not None,
            "total": total,
            "copied": copied,
            "skipped_existing": skipped,
            "repaired": repaired,
            "failed": failed,
            "pending": pending,
            "complete": pending == 0 and failed == 0,
        }
        if copied or failed or pending:
            LOGGER.info(
                "Зеркалирование файлов: скопировано %d, пропущено %d, отложено %d, ошибок %d",
                copied,
                skipped,
                pending,
                failed,
            )
        return report
    finally:
        if target_root_fd >= 0:
            os.close(target_root_fd)
        os.close(mirror_fd)
        os.close(source_fd)
