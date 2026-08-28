"""Authenticated lifecycle barrier for the external Engineer command ledger.

The SQLite file is restorable data; the anchor is deliberately kept in the
live state directory and authenticated with the Engineer command key.  A
runtime open therefore cannot mistake a missing, replaced, or rolled-back
ledger for a fresh store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any, Final, Literal

from .contracts import CommandError, canonical_json_bytes

CommandStoreOpenMode = Literal["provision", "runtime"]

COMMAND_STORE_SCHEMA_VERSION: Final = 1
_ANCHOR_SCHEMA: Final = "friday.engineer-command-store-anchor.v1"
_BOOTSTRAP_SCHEMA: Final = "friday.engineer-command-store-bootstrap.v1"
_PENDING_SCHEMA: Final = "friday.engineer-command-store-pending.v1"
_COMMITTED_SCHEMA: Final = "friday.engineer-command-store-committed.v1"
_BACKUP_AUTHORITY_SCHEMA: Final = "friday.engineer-command-backup-authority.v1"
_STORE_ID_RE = re.compile(r"[0-9a-f]{32}")
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_AUTHORITY_SEQUENCE: Final = 9_223_372_036_854_775_806
_MAX_ANCHOR_BYTES: Final = 4_096
_MAC_DOMAIN: Final = b"friday.engineer-command-store-anchor.v1\x00"
_BOOTSTRAP_MAC_DOMAIN: Final = b"friday.engineer-command-store-bootstrap.v1\x00"
_PENDING_MAC_DOMAIN: Final = b"friday.engineer-command-store-pending.v1\x00"
_COMMITTED_MAC_DOMAIN: Final = b"friday.engineer-command-store-committed.v1\x00"
_BACKUP_AUTHORITY_MAC_DOMAIN: Final = b"friday.engineer-command-backup-authority.v1\x00"

_META_TABLE_SQL = """
CREATE TABLE command_store_lifecycle_meta (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton=1),
    store_id TEXT NOT NULL
        CHECK(typeof(store_id)='text' AND length(store_id)=32
              AND store_id NOT GLOB '*[^0-9a-f]*'),
    schema_version INTEGER NOT NULL
        CHECK(typeof(schema_version)='integer' AND schema_version=1),
    authority_sequence INTEGER NOT NULL
        CHECK(typeof(authority_sequence)='integer'
              AND authority_sequence BETWEEN 0 AND 9223372036854775806)
) WITHOUT ROWID;
"""
_META_INSERT_GUARD_SQL = """
CREATE TRIGGER command_store_lifecycle_meta_insert_guard
BEFORE INSERT ON command_store_lifecycle_meta
BEGIN
    SELECT RAISE(ABORT, 'command_store_lifecycle_meta_immutable');
END;
"""
_META_UPDATE_GUARD_SQL = """
CREATE TRIGGER command_store_lifecycle_meta_update_guard
BEFORE UPDATE ON command_store_lifecycle_meta
WHEN NEW.singleton IS NOT OLD.singleton
  OR NEW.store_id IS NOT OLD.store_id
  OR NEW.schema_version IS NOT OLD.schema_version
  OR typeof(NEW.authority_sequence)<>'integer'
  OR NEW.authority_sequence<>OLD.authority_sequence+1
BEGIN
    SELECT RAISE(ABORT, 'command_store_lifecycle_meta_immutable');
END;
"""
_META_DELETE_GUARD_SQL = """
CREATE TRIGGER command_store_lifecycle_meta_delete_guard
BEFORE DELETE ON command_store_lifecycle_meta
BEGIN
    SELECT RAISE(ABORT, 'command_store_lifecycle_meta_immutable');
END;
"""
_META_SCHEMA_OBJECTS = {
    "command_store_lifecycle_meta": (
        "table",
        "command_store_lifecycle_meta",
        _META_TABLE_SQL,
    ),
    "command_store_lifecycle_meta_insert_guard": (
        "trigger",
        "command_store_lifecycle_meta",
        _META_INSERT_GUARD_SQL,
    ),
    "command_store_lifecycle_meta_update_guard": (
        "trigger",
        "command_store_lifecycle_meta",
        _META_UPDATE_GUARD_SQL,
    ),
    "command_store_lifecycle_meta_delete_guard": (
        "trigger",
        "command_store_lifecycle_meta",
        _META_DELETE_GUARD_SQL,
    ),
}


def _canonical_schema_sql(value: str) -> str:
    return " ".join(str(value or "").rstrip().rstrip(";").split())


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _secure_directory(path: Path, *, create: bool) -> None:
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        observed = path.lstat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_uid != os.geteuid()
        ):
            raise CommandError("command_store_state_dir_invalid")
        if create:
            os.chmod(path, 0o700, follow_symlinks=False)
        elif observed.st_mode & 0o077:
            raise CommandError("command_store_state_dir_invalid")
    except CommandError:
        raise
    except OSError as exc:
        raise CommandError("command_store_state_dir_invalid") from exc


def _read_private_file(path: Path, *, maximum: int, error: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(str(path), flags)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or observed.st_mode & 0o077
            or observed.st_size <= 0
            or observed.st_size > maximum
        ):
            raise CommandError(error)
        payload = os.read(descriptor, maximum + 1)
        if not payload or len(payload) > maximum or os.read(descriptor, 1):
            raise CommandError(error)
        return payload
    except CommandError:
        raise
    except OSError as exc:
        raise CommandError(error) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    directory = path.parent
    directory_fd = -1
    descriptor = -1
    temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        directory_fd = os.open(
            str(directory),
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short anchor write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        if directory_fd >= 0:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory_fd)
        raise CommandError("command_store_anchor_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)


def _database_identity(path: Path) -> tuple[int, int]:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise CommandError("command_store_database_missing") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_nlink != 1
    ):
        raise CommandError("command_store_database_invalid")
    if observed.st_size <= 0:
        raise CommandError("command_store_database_empty")
    return int(observed.st_dev), int(observed.st_ino)


def command_store_backup_is_quiescent(connection: sqlite3.Connection) -> bool:
    """Prove no command/delivery saga can cross a main-database backup."""

    try:
        row = connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM jobs
                    WHERE status IN ('planned','admitted','running')
                       OR (status='unknown' AND NOT EXISTS (
                               SELECT 1 FROM command_job_publications AS resolved
                                WHERE resolved.job_id=jobs.job_id
                                  AND resolved.state IN ('sent','blocked')
                           ))
                   UNION ALL
                   SELECT 1 FROM command_job_publications
                    WHERE state IN ('pending','staged','uncertain')
                       OR (state='blocked'
                           AND last_error_code='no_generated_files'
                           AND carrier_retired_at IS NULL)
               )"""
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise CommandError("command_store_backup_quiescence_unavailable") from exc
    if row is None or type(row[0]) is not int or row[0] not in {0, 1}:
        raise CommandError("command_store_backup_quiescence_invalid")
    return row[0] == 0


class CommandStoreLifecycle:
    """Own the SQLite generation and its authenticated non-database anchor."""

    def __init__(
        self,
        *,
        database_path: Path,
        state_dir: Path,
        mode: CommandStoreOpenMode,
        key: bytes | None,
    ) -> None:
        if mode not in {"provision", "runtime"}:
            raise CommandError("command_store_open_mode_invalid")
        self.database_path = Path(database_path)
        self.state_dir = Path(state_dir)
        self.anchor_path = self.state_dir / "engineer-command-store.anchor.json"
        self.bootstrap_path = self.state_dir / "engineer-command-store.bootstrap.json"
        self.pending_path = self.state_dir / "engineer-command-store.pending.json"
        self.committed_path = self.state_dir / "engineer-command-store.committed.json"
        self._test_key_path = self.state_dir / ".engineer-command-store.test.key"
        _secure_directory(self.state_dir, create=mode == "provision")
        self._key = self._resolve_key(mode=mode, key=key)
        self._store_id = ""
        self._authority_sequence = -1
        self._database_device = -1
        self._database_inode = -1
        self._ready = False
        self._poisoned = False

    def preflight_runtime_database(self) -> None:
        """Reject a missing/empty/non-regular database before SQLite can create it."""

        _database_identity(self.database_path)

    def _resolve_key(self, *, mode: CommandStoreOpenMode, key: bytes | None) -> bytes:
        if key is not None:
            if type(key) is not bytes or len(key) != 32:
                raise CommandError("command_store_lifecycle_key_invalid")
            return key
        if mode == "runtime":
            raise CommandError("command_store_lifecycle_key_missing")
        if self._test_key_path.exists() or self._test_key_path.is_symlink():
            payload = _read_private_file(
                self._test_key_path,
                maximum=32,
                error="command_store_lifecycle_key_invalid",
            )
            if len(payload) != 32:
                raise CommandError("command_store_lifecycle_key_invalid")
            return payload
        if (
            self.anchor_path.exists()
            or self.anchor_path.is_symlink()
            or self.bootstrap_path.exists()
            or self.bootstrap_path.is_symlink()
            or self.pending_path.exists()
            or self.pending_path.is_symlink()
            or self.committed_path.exists()
            or self.committed_path.is_symlink()
        ):
            raise CommandError("command_store_lifecycle_key_missing")
        payload = secrets.token_bytes(32)
        _atomic_write_private(self._test_key_path, payload)
        return payload

    def preflight_provision(self, connection: sqlite3.Connection) -> bool:
        """Validate an anchored store before maintenance; return True for legacy/new."""

        objects = self._schema_objects(connection)
        if not objects:
            has_bootstrap = self.bootstrap_path.exists() or self.bootstrap_path.is_symlink()
            has_non_bootstrap_evidence = (
                self.anchor_path.exists()
                or self.anchor_path.is_symlink()
                or self.pending_path.exists()
                or self.pending_path.is_symlink()
                or self.committed_path.exists()
                or self.committed_path.is_symlink()
            )
            if has_non_bootstrap_evidence:
                raise CommandError("command_store_lifecycle_mismatch")
            # An authenticated bootstrap is the only lifecycle evidence allowed
            # here.  It may outlive a crash after its fsync but before the
            # lifecycle-meta transaction commits.  Its exact database identity
            # and MAC are checked before any ordinary store migration, then
            # rechecked by finish_provision immediately before it is consumed.
            # A pre-lifecycle production ledger is deliberately adopted only by
            # this explicit maintenance path.  Prove its SQLite image before
            # schema migration; runtime open never reaches this branch.
            validate_runtime_database(connection)
            if has_bootstrap:
                self._database_device, self._database_inode = _database_identity(self.database_path)
                self._validated_bootstrap_store_id()
            return True
        self._validate_meta_schema(connection, objects=objects)
        self._open_existing(connection)
        return False

    def finish_provision(self, connection: sqlite3.Connection, *, legacy: bool) -> None:
        if legacy:
            if self._schema_objects(connection):
                raise CommandError("command_store_lifecycle_schema_invalid")
            self._database_device, self._database_inode = _database_identity(self.database_path)
            if self.bootstrap_path.exists() or self.bootstrap_path.is_symlink():
                store_id = self._validated_bootstrap_store_id()
            else:
                store_id = secrets.token_hex(16)
                self._store_id = store_id
                self._authority_sequence = 0
                self._write_bootstrap()
                self._validate_bootstrap()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_META_TABLE_SQL)
                connection.execute(
                    """INSERT INTO command_store_lifecycle_meta(
                           singleton,store_id,schema_version,authority_sequence)
                       VALUES(1,?,?,0)""",
                    (store_id, COMMAND_STORE_SCHEMA_VERSION),
                )
                connection.execute(_META_INSERT_GUARD_SQL)
                connection.execute(_META_UPDATE_GUARD_SQL)
                connection.execute(_META_DELETE_GUARD_SQL)
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        self._validate_meta_schema(connection)
        row = self._meta_row(connection)
        self._store_id, self._authority_sequence = self._validated_meta(row)
        self._database_device, self._database_inode = _database_identity(self.database_path)
        if legacy:
            if self._authority_sequence != 0:
                raise CommandError("command_store_lifecycle_mismatch")
            self._finish_bootstrap_anchor()
        else:
            self._recover_or_validate_anchor(connection)
        self._ready = True

    def open_runtime(self, connection: sqlite3.Connection) -> None:
        self._validate_meta_schema(connection)
        self._open_existing(connection)

    def _open_existing(self, connection: sqlite3.Connection) -> None:
        self._store_id, self._authority_sequence = self._validated_meta(self._meta_row(connection))
        self._database_device, self._database_inode = _database_identity(self.database_path)
        self._recover_or_validate_anchor(connection)
        self._ready = True

    def assert_ready(self, connection: sqlite3.Connection) -> None:
        if not self._ready or self._poisoned:
            raise CommandError("command_store_lifecycle_unavailable")
        if _database_identity(self.database_path) != (
            self._database_device,
            self._database_inode,
        ):
            self._poisoned = True
            raise CommandError("command_store_database_replaced")
        try:
            store_id, sequence = self._validated_meta(self._meta_row(connection))
        except CommandError:
            self._poisoned = True
            raise
        if store_id != self._store_id or sequence != self._authority_sequence:
            self._poisoned = True
            raise CommandError("command_store_lifecycle_mismatch")
        try:
            self._validate_anchor(sequence)
            if (
                self.bootstrap_path.exists()
                or self.bootstrap_path.is_symlink()
                or self.pending_path.exists()
                or self.pending_path.is_symlink()
                or self.committed_path.exists()
                or self.committed_path.is_symlink()
            ):
                raise CommandError("command_store_lifecycle_mismatch")
        except CommandError:
            self._poisoned = True
            raise

    def authenticated_identity(self, connection: sqlite3.Connection) -> tuple[str, int]:
        """Return the exact live generation only after rechecking its authority."""

        self.assert_ready(connection)
        return self._store_id, self._authority_sequence

    def attest_main_database_backup(
        self,
        connection: sqlite3.Connection,
        *,
        database_sha256: str,
    ) -> dict[str, str | int | bool]:
        """Bind one main-database image to this exact authenticated authority.

        The main SQLite manifest is not itself a trust root.  The keyed proof
        therefore covers both the external ledger generation and the copied
        database digest, preventing an operator (or damaged manifest) from
        transplanting a current generation number onto an older database.
        """

        store_id, sequence = self.authenticated_identity(connection)
        if type(database_sha256) is not str or _LOWER_SHA256_RE.fullmatch(database_sha256) is None:
            raise CommandError("command_store_backup_digest_invalid")
        if not command_store_backup_is_quiescent(connection):
            raise CommandError("command_store_backup_not_quiescent")
        payload: dict[str, str | int | bool] = {
            "authority_sequence": sequence,
            "database_sha256": database_sha256,
            "quiescent": True,
            "schema": _BACKUP_AUTHORITY_SCHEMA,
            "store_id": store_id,
        }
        return {
            **payload,
            "mac": hmac.new(
                self._key,
                _BACKUP_AUTHORITY_MAC_DOMAIN + canonical_json_bytes(payload),
                hashlib.sha256,
            ).hexdigest(),
        }

    def verify_main_database_backup_authority(
        self,
        connection: sqlite3.Connection,
        evidence: object,
        *,
        database_sha256: str,
    ) -> tuple[str, int]:
        """Verify a backup proof and require the ledger to remain at that point."""

        store_id, sequence = self.authenticated_identity(connection)
        if (
            type(database_sha256) is not str
            or _LOWER_SHA256_RE.fullmatch(database_sha256) is None
            or not isinstance(evidence, dict)
            or set(evidence)
            != {
                "authority_sequence",
                "database_sha256",
                "mac",
                "quiescent",
                "schema",
                "store_id",
            }
        ):
            raise CommandError("command_store_backup_authority_invalid")
        observed_store_id = evidence.get("store_id")
        observed_sequence = evidence.get("authority_sequence")
        observed_digest = evidence.get("database_sha256")
        observed_mac = evidence.get("mac")
        if (
            type(observed_store_id) is not str
            or _STORE_ID_RE.fullmatch(observed_store_id) is None
            or type(observed_sequence) is not int
            or not 0 <= observed_sequence <= _MAX_AUTHORITY_SEQUENCE
            or type(observed_digest) is not str
            or _LOWER_SHA256_RE.fullmatch(observed_digest) is None
            or type(observed_mac) is not str
            or _LOWER_SHA256_RE.fullmatch(observed_mac) is None
            or evidence.get("schema") != _BACKUP_AUTHORITY_SCHEMA
            or evidence.get("quiescent") is not True
            or observed_store_id != store_id
            or observed_sequence != sequence
            or not hmac.compare_digest(observed_digest, database_sha256)
        ):
            raise CommandError("command_store_backup_authority_mismatch")
        if not command_store_backup_is_quiescent(connection):
            raise CommandError("command_store_backup_not_quiescent")
        payload: dict[str, str | int | bool] = {
            "authority_sequence": observed_sequence,
            "database_sha256": observed_digest,
            "quiescent": True,
            "schema": _BACKUP_AUTHORITY_SCHEMA,
            "store_id": observed_store_id,
        }
        expected_mac = hmac.new(
            self._key,
            _BACKUP_AUTHORITY_MAC_DOMAIN + canonical_json_bytes(payload),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(observed_mac, expected_mac):
            raise CommandError("command_store_backup_authority_mismatch")
        return store_id, sequence

    def begin_barrier(self, connection: sqlite3.Connection) -> int:
        """Durably publish an authenticated one-step intent before SQLite begins."""

        self.assert_ready(connection)
        if self._authority_sequence >= _MAX_AUTHORITY_SEQUENCE:
            raise CommandError("command_store_authority_sequence_exhausted")
        next_sequence = self._authority_sequence + 1
        self._write_pending(self._authority_sequence, next_sequence)
        self._validate_pending(self._authority_sequence, next_sequence)
        return next_sequence

    def advance_in_transaction(self, connection: sqlite3.Connection, sequence: int) -> None:
        if sequence != self._authority_sequence + 1:
            self._poisoned = True
            raise CommandError("command_store_lifecycle_mismatch")
        self._validate_pending(self._authority_sequence, sequence)
        cursor = connection.execute(
            """UPDATE command_store_lifecycle_meta
                  SET authority_sequence=?
                WHERE singleton=1 AND store_id=? AND schema_version=?
                  AND authority_sequence=?""",
            (
                sequence,
                self._store_id,
                COMMAND_STORE_SCHEMA_VERSION,
                self._authority_sequence,
            ),
        )
        if cursor.rowcount != 1:
            self._poisoned = True
            raise CommandError("command_store_lifecycle_mismatch")

    def abort_barrier(self, connection: sqlite3.Connection, sequence: int) -> None:
        """Retire a pre-commit intent after SQLite has certainly rolled back."""

        try:
            store_id, observed = self._validated_meta(self._meta_row(connection))
            if (
                store_id != self._store_id
                or observed != self._authority_sequence
                or sequence != self._authority_sequence + 1
            ):
                raise CommandError("command_store_lifecycle_mismatch")
            self._validate_pending(self._authority_sequence, sequence)
            if self.committed_path.exists() or self.committed_path.is_symlink():
                raise CommandError("command_store_lifecycle_mismatch")
            self._remove_pending()
            self._validate_anchor(self._authority_sequence)
        except Exception as exc:
            self._poisoned = True
            if isinstance(exc, CommandError):
                raise
            raise CommandError("command_store_anchor_write_failed") from exc

    def mark_committed(self, connection: sqlite3.Connection, sequence: int) -> None:
        """Fsync an authenticated post-COMMIT proof before publishing authority."""

        try:
            store_id, observed = self._validated_meta(self._meta_row(connection))
            if store_id != self._store_id or observed != sequence:
                raise CommandError("command_store_lifecycle_mismatch")
            self._validate_pending(self._authority_sequence, sequence)
            self._write_committed(self._authority_sequence, sequence)
            self._validate_committed(self._authority_sequence, sequence)
        except Exception as exc:
            self._poisoned = True
            if isinstance(exc, CommandError):
                raise
            raise CommandError("command_store_anchor_write_failed") from exc

    def finish_commit(self, connection: sqlite3.Connection, sequence: int) -> None:
        """Publish and verify the anchor before authority returns to its caller."""

        try:
            store_id, observed = self._validated_meta(self._meta_row(connection))
            if store_id != self._store_id or observed != sequence:
                raise CommandError("command_store_lifecycle_mismatch")
            self._validate_pending(self._authority_sequence, sequence)
            self._validate_committed(self._authority_sequence, sequence)
            self._write_anchor(sequence)
            self._validate_anchor(sequence)
            self._remove_private(self.committed_path)
            self._remove_pending()
        except Exception as exc:
            self._poisoned = True
            if isinstance(exc, CommandError):
                raise
            raise CommandError("command_store_anchor_write_failed") from exc
        self._authority_sequence = sequence

    def poison(self) -> None:
        self._poisoned = True

    def _anchor_payload(self, sequence: int) -> dict[str, Any]:
        return {
            "authority_sequence": sequence,
            "database_device": self._database_device,
            "database_inode": self._database_inode,
            "schema": _ANCHOR_SCHEMA,
            "schema_version": COMMAND_STORE_SCHEMA_VERSION,
            "store_id": self._store_id,
        }

    def _anchor_mac(self, payload: dict[str, Any]) -> str:
        return hmac.new(
            self._key,
            _MAC_DOMAIN + canonical_json_bytes(payload),
            hashlib.sha256,
        ).hexdigest()

    def _bootstrap_payload(self, store_id: str) -> dict[str, Any]:
        return {
            "authority_sequence": 0,
            "database_device": self._database_device,
            "database_inode": self._database_inode,
            "schema": _BOOTSTRAP_SCHEMA,
            "schema_version": COMMAND_STORE_SCHEMA_VERSION,
            "store_id": store_id,
        }

    def _bootstrap_mac(self, payload: dict[str, Any]) -> str:
        return hmac.new(
            self._key,
            _BOOTSTRAP_MAC_DOMAIN + canonical_json_bytes(payload),
            hashlib.sha256,
        ).hexdigest()

    def _pending_payload(self, previous: int, sequence: int) -> dict[str, Any]:
        return {
            "database_device": self._database_device,
            "database_inode": self._database_inode,
            "from_authority_sequence": previous,
            "schema": _PENDING_SCHEMA,
            "schema_version": COMMAND_STORE_SCHEMA_VERSION,
            "store_id": self._store_id,
            "to_authority_sequence": sequence,
        }

    def _pending_mac(self, payload: dict[str, Any]) -> str:
        return hmac.new(
            self._key,
            _PENDING_MAC_DOMAIN + canonical_json_bytes(payload),
            hashlib.sha256,
        ).hexdigest()

    def _committed_payload(self, previous: int, sequence: int) -> dict[str, Any]:
        payload = self._pending_payload(previous, sequence)
        payload["schema"] = _COMMITTED_SCHEMA
        return payload

    def _committed_mac(self, payload: dict[str, Any]) -> str:
        return hmac.new(
            self._key,
            _COMMITTED_MAC_DOMAIN + canonical_json_bytes(payload),
            hashlib.sha256,
        ).hexdigest()

    def _write_anchor(self, sequence: int) -> None:
        payload = self._anchor_payload(sequence)
        envelope = {**payload, "mac": self._anchor_mac(payload)}
        _atomic_write_private(self.anchor_path, canonical_json_bytes(envelope) + b"\n")

    def _write_bootstrap(self) -> None:
        payload = self._bootstrap_payload(self._store_id)
        envelope = {**payload, "mac": self._bootstrap_mac(payload)}
        _atomic_write_private(self.bootstrap_path, canonical_json_bytes(envelope) + b"\n")

    def _write_pending(self, previous: int, sequence: int) -> None:
        payload = self._pending_payload(previous, sequence)
        envelope = {**payload, "mac": self._pending_mac(payload)}
        _atomic_write_private(self.pending_path, canonical_json_bytes(envelope) + b"\n")

    def _write_committed(self, previous: int, sequence: int) -> None:
        payload = self._committed_payload(previous, sequence)
        envelope = {**payload, "mac": self._committed_mac(payload)}
        _atomic_write_private(self.committed_path, canonical_json_bytes(envelope) + b"\n")

    def _remove_pending(self) -> None:
        self._remove_private(self.pending_path)

    def _remove_private(self, path: Path) -> None:
        directory_fd = -1
        try:
            directory_fd = os.open(
                str(self.state_dir),
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            raise CommandError("command_store_anchor_write_failed") from exc
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)

    def _validate_anchor(self, sequence: int) -> None:
        raw = _read_private_file(
            self.anchor_path,
            maximum=_MAX_ANCHOR_BYTES,
            error="command_store_anchor_invalid",
        )
        try:
            envelope = json.loads(raw, object_pairs_hook=_closed_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError("command_store_anchor_invalid") from exc
        if not isinstance(envelope, dict) or set(envelope) != {
            "authority_sequence",
            "database_device",
            "database_inode",
            "mac",
            "schema",
            "schema_version",
            "store_id",
        }:
            raise CommandError("command_store_anchor_invalid")
        mac = envelope.pop("mac", None)
        expected = self._anchor_payload(sequence)
        if (
            not isinstance(mac, str)
            or _LOWER_SHA256_RE.fullmatch(mac) is None
            or any(
                type(envelope.get(field)) is not int
                for field in (
                    "authority_sequence",
                    "database_device",
                    "database_inode",
                    "schema_version",
                )
            )
            or envelope != expected
            or not hmac.compare_digest(mac, self._anchor_mac(expected))
        ):
            raise CommandError("command_store_lifecycle_mismatch")

    def _validated_bootstrap_store_id(self) -> str:
        raw = _read_private_file(
            self.bootstrap_path,
            maximum=_MAX_ANCHOR_BYTES,
            error="command_store_bootstrap_invalid",
        )
        try:
            envelope = json.loads(raw, object_pairs_hook=_closed_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError("command_store_bootstrap_invalid") from exc
        if not isinstance(envelope, dict) or set(envelope) != {
            "authority_sequence",
            "database_device",
            "database_inode",
            "mac",
            "schema",
            "schema_version",
            "store_id",
        }:
            raise CommandError("command_store_bootstrap_invalid")
        mac = envelope.pop("mac", None)
        store_id = envelope.get("store_id")
        if type(store_id) is not str or _STORE_ID_RE.fullmatch(store_id) is None:
            raise CommandError("command_store_lifecycle_mismatch")
        expected = self._bootstrap_payload(store_id)
        if (
            not isinstance(mac, str)
            or _LOWER_SHA256_RE.fullmatch(mac) is None
            or any(
                type(envelope.get(field)) is not int
                for field in (
                    "authority_sequence",
                    "database_device",
                    "database_inode",
                    "schema_version",
                )
            )
            or envelope != expected
            or not hmac.compare_digest(mac, self._bootstrap_mac(expected))
        ):
            raise CommandError("command_store_lifecycle_mismatch")
        return store_id

    def _validate_bootstrap(self) -> None:
        if self._validated_bootstrap_store_id() != self._store_id:
            raise CommandError("command_store_lifecycle_mismatch")

    def _finish_bootstrap_anchor(self) -> None:
        self._validate_bootstrap()
        if self.pending_path.exists() or self.pending_path.is_symlink():
            raise CommandError("command_store_lifecycle_mismatch")
        if self.committed_path.exists() or self.committed_path.is_symlink():
            raise CommandError("command_store_lifecycle_mismatch")
        if self.anchor_path.exists() or self.anchor_path.is_symlink():
            self._validate_anchor(0)
        else:
            self._write_anchor(0)
            self._validate_anchor(0)
        self._remove_private(self.bootstrap_path)

    def _validate_pending(self, previous: int, sequence: int) -> None:
        self._validate_transition_file(
            self.pending_path,
            previous=previous,
            sequence=sequence,
            schema=_PENDING_SCHEMA,
            mac_function=self._pending_mac,
            error="command_store_pending_invalid",
        )

    def _validate_committed(self, previous: int, sequence: int) -> None:
        self._validate_transition_file(
            self.committed_path,
            previous=previous,
            sequence=sequence,
            schema=_COMMITTED_SCHEMA,
            mac_function=self._committed_mac,
            error="command_store_committed_invalid",
        )

    def _validate_transition_file(
        self,
        path: Path,
        *,
        previous: int,
        sequence: int,
        schema: str,
        mac_function: Any,
        error: str,
    ) -> None:
        raw = _read_private_file(
            path,
            maximum=_MAX_ANCHOR_BYTES,
            error=error,
        )
        try:
            envelope = json.loads(raw, object_pairs_hook=_closed_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError(error) from exc
        if not isinstance(envelope, dict) or set(envelope) != {
            "database_device",
            "database_inode",
            "from_authority_sequence",
            "mac",
            "schema",
            "schema_version",
            "store_id",
            "to_authority_sequence",
        }:
            raise CommandError(error)
        mac = envelope.pop("mac", None)
        expected = self._pending_payload(previous, sequence)
        expected["schema"] = schema
        if (
            not isinstance(mac, str)
            or _LOWER_SHA256_RE.fullmatch(mac) is None
            or any(
                type(envelope.get(field)) is not int
                for field in (
                    "database_device",
                    "database_inode",
                    "from_authority_sequence",
                    "schema_version",
                    "to_authority_sequence",
                )
            )
            or envelope != expected
            or not hmac.compare_digest(mac, mac_function(expected))
        ):
            raise CommandError("command_store_lifecycle_mismatch")

    def _recover_or_validate_anchor(self, connection: sqlite3.Connection) -> None:
        """Resolve only the two authenticated crash windows; reject every rollback."""

        if self.bootstrap_path.exists() or self.bootstrap_path.is_symlink():
            if self._authority_sequence != 0:
                raise CommandError("command_store_lifecycle_mismatch")
            self._finish_bootstrap_anchor()
            return

        if not (self.pending_path.exists() or self.pending_path.is_symlink()):
            if self.committed_path.exists() or self.committed_path.is_symlink():
                raise CommandError("command_store_lifecycle_mismatch")
            self._validate_anchor(self._authority_sequence)
            return
        if self._authority_sequence <= 0:
            # A valid pending transition may be 0 -> 1 with an uncommitted DB.
            previous = 0
            sequence = 1
        else:
            # The DB can be on either side of the single pending transition.
            candidates = (
                (self._authority_sequence, self._authority_sequence + 1),
                (self._authority_sequence - 1, self._authority_sequence),
            )
            previous = sequence = -1
            for candidate_previous, candidate_sequence in candidates:
                try:
                    self._validate_pending(candidate_previous, candidate_sequence)
                except CommandError:
                    continue
                previous, sequence = candidate_previous, candidate_sequence
                break
            if previous < 0:
                raise CommandError("command_store_lifecycle_mismatch")
        self._validate_pending(previous, sequence)
        has_committed = self.committed_path.exists() or self.committed_path.is_symlink()
        if has_committed:
            self._validate_committed(previous, sequence)
        if self._authority_sequence == previous:
            # SQLite did not commit. A prematurely advanced stable anchor would
            # instead prove rollback/loss and is never repaired automatically.
            if has_committed:
                raise CommandError("command_store_lifecycle_mismatch")
            self._validate_anchor(previous)
            self._remove_pending()
            return
        if self._authority_sequence != sequence:
            raise CommandError("command_store_lifecycle_mismatch")
        if not has_committed:
            self._write_committed(previous, sequence)
            self._validate_committed(previous, sequence)
        try:
            self._validate_anchor(sequence)
        except CommandError:
            self._validate_anchor(previous)
            self._write_anchor(sequence)
            self._validate_anchor(sequence)
        self._remove_private(self.committed_path)
        self._remove_pending()

    @staticmethod
    def _schema_objects(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        rows = connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
                WHERE name='command_store_lifecycle_meta'
                   OR name GLOB 'command_store_lifecycle_meta_*'
                   OR tbl_name='command_store_lifecycle_meta'"""
        ).fetchall()
        return {str(row["name"]): row for row in rows}

    def _validate_meta_schema(
        self,
        connection: sqlite3.Connection,
        *,
        objects: dict[str, sqlite3.Row] | None = None,
    ) -> None:
        observed = self._schema_objects(connection) if objects is None else objects
        if set(observed) != set(_META_SCHEMA_OBJECTS):
            raise CommandError("command_store_lifecycle_schema_invalid")
        for name, (expected_type, expected_table, expected_sql) in _META_SCHEMA_OBJECTS.items():
            row = observed[name]
            if (
                str(row["type"]) != expected_type
                or str(row["tbl_name"]) != expected_table
                or _canonical_schema_sql(str(row["sql"] or "")) != _canonical_schema_sql(expected_sql)
            ):
                raise CommandError("command_store_lifecycle_schema_invalid")
        rows = connection.execute("SELECT * FROM command_store_lifecycle_meta").fetchall()
        if len(rows) != 1:
            raise CommandError("command_store_lifecycle_schema_invalid")

    @staticmethod
    def _meta_row(connection: sqlite3.Connection) -> sqlite3.Row:
        try:
            row = connection.execute(
                """SELECT singleton,store_id,schema_version,authority_sequence
                     FROM command_store_lifecycle_meta WHERE singleton=1"""
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise CommandError("command_store_lifecycle_schema_invalid") from exc
        if row is None:
            raise CommandError("command_store_lifecycle_schema_invalid")
        return row

    @staticmethod
    def _validated_meta(row: sqlite3.Row) -> tuple[str, int]:
        try:
            if type(row["singleton"]) is not int or row["singleton"] != 1:
                raise ValueError("invalid singleton")
            store_id = row["store_id"]
            version = row["schema_version"]
            sequence = row["authority_sequence"]
            if type(store_id) is not str or _STORE_ID_RE.fullmatch(store_id) is None:
                raise ValueError("invalid store id")
            if type(version) is not int or version != COMMAND_STORE_SCHEMA_VERSION:
                raise ValueError("invalid schema version")
            if type(sequence) is not int or not 0 <= sequence <= _MAX_AUTHORITY_SEQUENCE:
                raise ValueError("invalid authority sequence")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise CommandError("command_store_lifecycle_schema_invalid") from exc
        return store_id, sequence


def validate_runtime_database(connection: sqlite3.Connection) -> None:
    """Fail closed on an unreadable/corrupt SQLite image before runtime use."""

    try:
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise CommandError("command_store_database_corrupt") from exc
    if [tuple(row) for row in integrity] != [("ok",)] or foreign_keys:
        raise CommandError("command_store_database_corrupt")
