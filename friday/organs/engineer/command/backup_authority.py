"""Lock-coordinated read observer for the external command-store authority."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .contracts import CommandError
from .store import validate_command_store_runtime_schema
from .store_lifecycle import (
    CommandStoreLifecycle,
    command_store_backup_is_quiescent,
    validate_runtime_database,
)

_T = TypeVar("_T")


def command_store_backup_authority_required(settings: Any) -> bool:
    """Treat any configured/provisioned ledger residue as authority evidence.

    Feature disablement is not deletion.  Once any part of the external store,
    key, or authenticated lifecycle exists, an unbound main-database backup is
    unsafe.  Ambiguous filesystem errors are evidence too; absence must be
    positively established for every path.
    """

    root = Path(settings.engineer_command_store_dir)
    state_dir = Path(settings.state_dir)
    candidates = (
        root,
        Path(settings.engineer_command_key_file),
        root / "kernel.sqlite",
        root / "kernel.lock",
        root / "kernel.lease",
        root / "jobs",
        root / "workbenches",
        state_dir / "engineer-command-store.anchor.json",
        state_dir / "engineer-command-store.bootstrap.json",
        state_dir / "engineer-command-store.pending.json",
        state_dir / "engineer-command-store.committed.json",
        state_dir / ".engineer-command-store.test.key",
    )
    if bool(getattr(settings, "engineer_command_enabled", False)):
        return True
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


class CommandStoreBackupAuthorityObserver:
    """Observe a live store without competing for its singleton runtime lease.

    Every observation takes the same exclusive ``kernel.lock`` used by command
    transactions, then validates the SQLite lifecycle row against its keyed
    non-database anchor.  This is the safe seam for the online CLI backup: it
    can coexist with the backend, but it cannot see a half-committed authority
    transition or create a second command kernel.
    """

    def __init__(self, root: Path, *, lifecycle_key: bytes, state_dir: Path) -> None:
        self.root = Path(root)
        self.database_path = self.root / "kernel.sqlite"
        self.state_dir = Path(state_dir)
        try:
            root_status = self.root.lstat()
            lock_path = self.root / "kernel.lock"
            lock_status = lock_path.lstat()
        except OSError as exc:
            raise CommandError("command_store_backup_authority_unavailable") from exc
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or stat.S_ISLNK(root_status.st_mode)
            or root_status.st_uid != os.geteuid()
            or root_status.st_mode & 0o077
            or not stat.S_ISREG(lock_status.st_mode)
            or stat.S_ISLNK(lock_status.st_mode)
            or lock_status.st_uid != os.geteuid()
            or lock_status.st_mode & 0o077
        ):
            raise CommandError("command_store_backup_authority_unavailable")
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._lock_fd = os.open(str(lock_path), flags)
        except OSError as exc:
            raise CommandError("command_store_backup_authority_unavailable") from exc
        self._lock_path = lock_path
        self._lock_device = int(os.fstat(self._lock_fd).st_dev)
        self._lock_inode = int(os.fstat(self._lock_fd).st_ino)
        self._key = lifecycle_key
        self._local = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._local:
            if self._closed:
                return
            self._closed = True
            os.close(self._lock_fd)

    def _assert_lock_identity(self) -> None:
        """Prove the pathname still names the inode whose flock we hold."""

        try:
            descriptor_status = os.fstat(self._lock_fd)
            path_status = self._lock_path.lstat()
        except OSError as exc:
            raise CommandError("command_store_backup_authority_unavailable") from exc
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or not stat.S_ISREG(path_status.st_mode)
            or stat.S_ISLNK(path_status.st_mode)
            or descriptor_status.st_uid != os.geteuid()
            or path_status.st_uid != os.geteuid()
            or descriptor_status.st_mode & 0o077
            or path_status.st_mode & 0o077
            or int(descriptor_status.st_dev) != self._lock_device
            or int(descriptor_status.st_ino) != self._lock_inode
            or int(path_status.st_dev) != self._lock_device
            or int(path_status.st_ino) != self._lock_inode
        ):
            raise CommandError("command_store_backup_authority_unavailable")

    def _observe(self, callback: Callable[[CommandStoreLifecycle, sqlite3.Connection], _T]) -> _T:
        import fcntl

        with self._local:
            if self._closed:
                raise CommandError("command_store_backup_authority_unavailable")
            self._assert_lock_identity()
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            connection: sqlite3.Connection | None = None
            try:
                self._assert_lock_identity()
                lifecycle = CommandStoreLifecycle(
                    database_path=self.database_path,
                    state_dir=self.state_dir,
                    mode="runtime",
                    key=self._key,
                )
                lifecycle.preflight_runtime_database()
                connection = sqlite3.connect(
                    f"{self.database_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
                connection.row_factory = sqlite3.Row
                validate_runtime_database(connection)
                lifecycle.open_runtime(connection)
                validate_command_store_runtime_schema(connection)
                result = callback(lifecycle, connection)
                self._assert_lock_identity()
                return result
            finally:
                if connection is not None:
                    connection.close()
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def backup_authority_snapshot(self) -> tuple[str, int, bool]:
        def snapshot(
            lifecycle: CommandStoreLifecycle,
            connection: sqlite3.Connection,
        ) -> tuple[str, int, bool]:
            store_id, sequence = lifecycle.authenticated_identity(connection)
            return store_id, sequence, command_store_backup_is_quiescent(connection)

        return self._observe(snapshot)

    def attest_main_database_backup(
        self,
        database_sha256: str,
    ) -> dict[str, str | int | bool]:
        return self._observe(
            lambda lifecycle, connection: lifecycle.attest_main_database_backup(
                connection,
                database_sha256=database_sha256,
            )
        )

    def verify_main_database_backup_authority(
        self,
        evidence: object,
        database_sha256: str,
    ) -> tuple[str, int, bool]:
        def verify(
            lifecycle: CommandStoreLifecycle,
            connection: sqlite3.Connection,
        ) -> tuple[str, int, bool]:
            store_id, sequence = lifecycle.verify_main_database_backup_authority(
                connection,
                evidence,
                database_sha256=database_sha256,
            )
            return store_id, sequence, True

        return self._observe(verify)


__all__ = [
    "CommandStoreBackupAuthorityObserver",
    "command_store_backup_authority_required",
]
