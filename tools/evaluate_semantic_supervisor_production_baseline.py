#!/usr/bin/env python3
"""Emit a body-free candidate baseline from committed Friday traces."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.orchestration.supervisor_contracts import canonical_dumps  # noqa: E402
from friday.orchestration.supervisor_production_baseline import (  # noqa: E402
    SupervisorBaselineError,
    build_production_baseline,
)


def _open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise OSError("database path is not a regular file")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate only committed interaction traces and joined semantic-shadow events; "
            "the result requires independent operator acceptance."
        )
    )
    parser.add_argument("--database", required=True, type=Path, help="Friday SQLite database")
    parser.add_argument("--limit", type=int, default=10_000, help="newest rows per source (1..100000)")
    args = parser.parse_args(argv)
    try:
        with _open_read_only(args.database) as connection:
            report = build_production_baseline(connection, limit=args.limit)
    except (OSError, sqlite3.Error, SupervisorBaselineError) as error:
        parser.error(type(error).__name__)
    sys.stdout.write(canonical_dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
