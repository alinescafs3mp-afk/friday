"""Fail-closed process boundary for an interrupted main-database restore.

This module is deliberately independent from the storage mixins.  ``_core`` may
therefore consult it before SQLite is opened while ``_maintenance`` owns the
only explicit recovery path, without creating an import cycle between them.
"""

from __future__ import annotations

import os
from pathlib import Path

DATABASE_RESTORE_INTENT_FILENAME = "database-restore.intent.json"


class DatabaseRestorePendingError(RuntimeError):
    """Ordinary database access encountered durable restore state."""


def database_restore_intent_path(state_dir: Path) -> Path:
    return Path(state_dir) / DATABASE_RESTORE_INTENT_FILENAME


def database_restore_intent_lstat(path: Path) -> os.stat_result | None:
    """Return exact marker metadata; only ``ENOENT`` proves absence.

    ``Path.exists()`` intentionally suppresses several filesystem errors.  That
    behaviour is unsuitable for a recovery authority: inaccessible or ambiguous
    state must block database creation/migration instead of being interpreted as
    an absent transaction.
    """

    try:
        return Path(path).lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseRestorePendingError(
            "Database restore intent cannot be inspected; explicit recovery is required"
        ) from exc


def assert_no_pending_database_restore(state_dir: Path) -> None:
    """Block every ordinary SQLite open while any restore marker is present."""

    path = database_restore_intent_path(state_dir)
    if database_restore_intent_lstat(path) is not None:
        raise DatabaseRestorePendingError(
            "Database restore recovery is pending; stop Friday and run restore-backup"
        )


__all__ = [
    "DATABASE_RESTORE_INTENT_FILENAME",
    "DatabaseRestorePendingError",
    "assert_no_pending_database_restore",
    "database_restore_intent_lstat",
    "database_restore_intent_path",
]
