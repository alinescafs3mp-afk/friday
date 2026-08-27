#!/usr/bin/env python3
"""Print one private, deployment-local semantic-supervisor canary actor binding."""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.orchestration.supervisor_actor_binding import (  # noqa: E402
    SupervisorCanaryActorBindingError,
    parse_supervisor_canary_actor_projection,
    supervisor_canary_actor_binding_from_transaction,
)

_MAX_STDIN_BYTES = 4_096


def _open_private_read_only_database(path: Path) -> sqlite3.Connection:
    if not isinstance(path, Path) or not path.is_absolute():
        raise OSError("database is unavailable")
    lexical = Path(os.path.abspath(path))
    if lexical != path or lexical.resolve(strict=True) != lexical:
        raise OSError("database is unavailable")
    status = os.stat(lexical, follow_symlinks=False)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise OSError("database is unavailable")
    connection = sqlite3.connect(
        f"{lexical.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=5.0,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise OSError("database is unavailable")
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read an exact ActorContext JSON projection from stdin and print only its "
            "deployment-local canary binding digest."
        )
    )
    parser.add_argument(
        "--database", required=True, type=Path, help="canonical private Friday SQLite database"
    )
    args = parser.parse_args(argv)
    connection: sqlite3.Connection | None = None
    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        actor = parse_supervisor_canary_actor_projection(raw)
        connection = _open_private_read_only_database(args.database)
        connection.execute("BEGIN")
        digest = supervisor_canary_actor_binding_from_transaction(connection, actor)
        connection.rollback()
    except (OSError, sqlite3.Error, SupervisorCanaryActorBindingError, TypeError, ValueError):
        if connection is not None:
            connection.rollback()
        sys.stderr.write("semantic supervisor canary actor binding unavailable\n")
        return 2
    finally:
        if connection is not None:
            connection.close()
    sys.stdout.write(digest + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
