"""Crash-safe operation receipts kept strictly outside an Obsidian vault."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .contracts import IdempotencyConflictError, VaultPathError, validate_revision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS note_operation_receipts (
    operation_digest TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    note_path TEXT NOT NULL,
    base_revision TEXT,
    target_revision TEXT NOT NULL,
    created INTEGER NOT NULL CHECK(created IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'committed')),
    CHECK(length(operation_digest)=64 AND operation_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(arguments_digest)=64 AND arguments_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(base_revision IS NULL OR
          (length(base_revision)=64 AND base_revision NOT GLOB '*[^0-9a-f]*')),
    CHECK(length(target_revision)=64 AND target_revision NOT GLOB '*[^0-9a-f]*')
)
"""


@dataclass(frozen=True, slots=True)
class NoteOperationReceipt:
    operation_digest: str
    method: str
    arguments_digest: str
    note_path: str
    base_revision: str | None
    target_revision: str
    created: bool
    state: str


class NoteOperationReceiptStore:
    """One small SQLite fence outside the synchronized folder."""

    def __init__(self, root: Path, *, vault_root: Path) -> None:
        configured = root.absolute()
        vault = vault_root.resolve(strict=True)
        if configured.is_symlink():
            raise VaultPathError("operation receipt root must not be a symbolic link")
        if configured == vault or configured.is_relative_to(vault):
            raise VaultPathError("operation receipt root must be outside the Obsidian vault")
        configured.mkdir(parents=True, exist_ok=True, mode=0o700)
        if configured.is_symlink():
            raise VaultPathError("operation receipt root must not be a symbolic link")
        configured.chmod(0o700)
        resolved = configured.resolve(strict=True)
        if resolved == vault or resolved.is_relative_to(vault):
            raise VaultPathError("operation receipt root must be outside the Obsidian vault")
        self._database = resolved / "receipts.sqlite3"
        if self._database.is_symlink() or (self._database.exists() and not self._database.is_file()):
            raise VaultPathError("operation receipt database must be a regular file")
        self._lock = threading.RLock()
        with closing(self._connect()) as conn:
            conn.execute(_SCHEMA)
        if self._database.is_symlink() or not self._database.is_file():
            raise VaultPathError("operation receipt database must be a regular file")
        self._database.chmod(0o600)

    def prepare(
        self,
        *,
        operation_digest: str,
        method: str,
        arguments_digest: str,
        note_path: str,
        base_revision: str | None,
        target_revision: str,
        created: bool,
    ) -> tuple[NoteOperationReceipt, bool]:
        if base_revision is not None:
            validate_revision(base_revision)
        validate_revision(target_revision)
        identity = (method, arguments_digest, note_path)
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM note_operation_receipts WHERE operation_digest=?",
                (operation_digest,),
            ).fetchone()
            if row is not None:
                actual = (str(row["method"]), str(row["arguments_digest"]), str(row["note_path"]))
                if actual != identity:
                    conn.rollback()
                    raise IdempotencyConflictError("operation ID was reused with different note arguments")
                conn.commit()
                return _receipt(row), False
            conn.execute(
                """INSERT INTO note_operation_receipts(
                       operation_digest, method, arguments_digest, note_path,
                       base_revision, target_revision, created, state
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'prepared')""",
                (
                    operation_digest,
                    method,
                    arguments_digest,
                    note_path,
                    base_revision,
                    target_revision,
                    int(created),
                ),
            )
            row = conn.execute(
                "SELECT * FROM note_operation_receipts WHERE operation_digest=?",
                (operation_digest,),
            ).fetchone()
            assert row is not None
            conn.commit()
            return _receipt(row), True

    def lookup(
        self,
        *,
        operation_digest: str,
        method: str,
        arguments_digest: str,
        note_path: str,
    ) -> NoteOperationReceipt | None:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM note_operation_receipts WHERE operation_digest=?",
                (operation_digest,),
            ).fetchone()
        if row is None:
            return None
        actual = (str(row["method"]), str(row["arguments_digest"]), str(row["note_path"]))
        if actual != (method, arguments_digest, note_path):
            raise IdempotencyConflictError("operation ID was reused with different note arguments")
        return _receipt(row)

    def commit(self, operation_digest: str) -> NoteOperationReceipt:
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM note_operation_receipts WHERE operation_digest=?",
                (operation_digest,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RuntimeError("note operation receipt disappeared")
            conn.execute(
                "UPDATE note_operation_receipts SET state='committed' WHERE operation_digest=?",
                (operation_digest,),
            )
            row = conn.execute(
                "SELECT * FROM note_operation_receipts WHERE operation_digest=?",
                (operation_digest,),
            ).fetchone()
            assert row is not None
            conn.commit()
            return _receipt(row)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn


def _receipt(row: sqlite3.Row) -> NoteOperationReceipt:
    return NoteOperationReceipt(
        operation_digest=str(row["operation_digest"]),
        method=str(row["method"]),
        arguments_digest=str(row["arguments_digest"]),
        note_path=str(row["note_path"]),
        base_revision=str(row["base_revision"]) if row["base_revision"] is not None else None,
        target_revision=str(row["target_revision"]),
        created=bool(row["created"]),
        state=str(row["state"]),
    )


__all__ = ["NoteOperationReceipt", "NoteOperationReceiptStore"]
