#!/usr/bin/env python
"""Build one database per historical schema version, for the migration test to carry forward.

``verify_backup`` checks that a backup's schema version is a number in range. It does
not check that the migration chain actually runs on that database — and a backup you
cannot open is not a backup. The existing migration tests hand-build a few tables with
a handful of rows, which tests the migration the author was thinking about.

These fixtures are real instead. Versions 14, 15 and 16 are produced by checking out
the commit that introduced them and letting THAT code create its own database, so the
DDL is the historical DDL rather than my reconstruction of it. Version 13 predates this
repository, so its structure is lifted from a real backup — schema only, never rows.

Nothing here contains personal data: every row is written by this script.

    python tools/build_schema_fixtures.py --schema-13-from ~/.jericho/data/backups/old.sqlite3
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import subprocess  # nosec B404 - fixed git invocations, no shell
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "schemas"

# The commit that first shipped each schema version, found by walking SCHEMA_VERSION
# through history. Pinned rather than rediscovered: a fixture that silently rebuilds
# from a different commit is no longer the artefact its name claims.
COMMITS = {
    14: "c47b159",
    15: "5481a34",
    16: "bf10276",
}

# Rows every fixture carries, so the migration test can assert that data SURVIVES the
# chain and not merely that the version number moved. Written against the oldest API
# that all versions share.
SEED_USER = "fixture-owner"


STAMP = "2026-01-01T00:00:00+00:00"


def _insert(db: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """Insert a row, filling every NOT NULL column the caller did not name.

    The historical schemas differ in which columns exist and which are mandatory, and
    hardcoding one version's column list produces fixtures for a schema that never
    shipped. Timestamps get a fixed stamp, other required columns a benign default.
    """
    columns = list(db.execute(f"PRAGMA table_info({table})"))  # nosec B608 - fixed names
    row = dict(values)
    for _cid, name, declared_type, not_null, default, _pk in columns:
        if name in row or default is not None or not not_null:
            continue
        lowered = str(name).lower()
        if lowered.endswith("_at") or lowered in {"created", "updated"}:
            row[name] = STAMP
        elif "INT" in str(declared_type).upper():
            row[name] = 1
        elif "REAL" in str(declared_type).upper() or "FLOA" in str(declared_type).upper():
            row[name] = 0.0
        else:
            row[name] = ""
    known = {str(name) for _cid, name, *_rest in columns}
    row = {key: value for key, value in row.items() if key in known}
    placeholders = ", ".join("?" for _ in row)
    names = ", ".join(row)
    db.execute(
        f"INSERT INTO {table}({names}) VALUES({placeholders})",  # nosec B608 - names from PRAGMA
        tuple(row.values()),
    )


def _set_marker(db: sqlite3.Connection, version: int) -> None:
    _insert(db, "schema_meta", {"key": "schema_version", "value": str(version)})


def _run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        args, cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _seed_script(home: Path) -> str:
    return f"""
import sys
sys.path.insert(0, ".")
from jericho.config import ensure_runtime_dirs, load_settings
from jericho.storage import init_storage, SCHEMA_VERSION
from jericho.storage.models import RawObject, new_id
from datetime import UTC, datetime

settings = load_settings()
ensure_runtime_dirs(settings)
storage = init_storage(settings)
storage.ensure_user({SEED_USER!r}, source="upload")
now = datetime.now(UTC).isoformat()
for index in range(3):
    storage.store_raw_object(
        RawObject(
            id=f"raw-fixture-{{index}}",
            user_id={SEED_USER!r},
            source="upload",
            source_ref=f"sha256:fixture{{index:058d}}",
            raw_content=f"Фикстурная запись номер {{index}} для проверки миграций.",
            content_type="text/plain",
            content_hash=f"{{index:064d}}",
            received_at=now,
        )
    )
storage.kv_set("fixture:marker", f"schema-{{SCHEMA_VERSION}}")
print(SCHEMA_VERSION)
storage.close()
"""


def build_from_commit(version: int, commit: str, workspace: Path) -> Path:
    """Let the historical code create its own database."""
    worktree = workspace / f"wt{version}"
    home = workspace / f"home{version}"
    _run(["git", "worktree", "add", "-q", "--detach", str(worktree), commit], cwd=REPO)
    try:
        script = worktree / "_seed_fixture.py"
        script.write_text(_seed_script(home), encoding="utf-8")
        env_line = f"JERICHO_HOME={home}"
        reported = _run(
            ["env", env_line, sys.executable, str(script)],
            cwd=worktree,
        ).strip()
        if reported != str(version):
            raise SystemExit(f"commit {commit} reports schema {reported}, expected {version}")
        source = home / "data" / "state" / "jericho.sqlite3"
        return _finalise(source, version)
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO)


def build_from_backup(version: int, backup: Path, workspace: Path) -> Path:
    """Rebuild an out-of-history schema from a real backup's DDL, carrying no rows."""
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as source_db:
        marker = source_db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if marker is None or int(marker[0]) != version:
            raise SystemExit(f"{backup} is schema {marker and marker[0]}, expected {version}")
        # Tables before anything that references them: rootpage order happily puts an
        # index ahead of its table. Shadow tables of an FTS virtual table are skipped —
        # the CREATE VIRTUAL TABLE statement builds them itself, and replaying them
        # separately collides.
        virtual = {
            str(row[0])
            for row in source_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'"
            )
        }
        statements = [
            str(row[1])
            for row in source_db.execute(
                """SELECT name, sql FROM sqlite_master
                   WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
                   ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1
                                      WHEN 'index' THEN 2 ELSE 3 END, rootpage"""
            )
            if not any(str(row[0]).startswith(f"{base}_") for base in virtual)
        ]
    target = workspace / f"schema-{version}.sqlite3"
    target.unlink(missing_ok=True)
    with sqlite3.connect(target) as db:
        for statement in statements:
            # Structure only. Not one row of the source database is read.
            db.execute(statement)
        _set_marker(db, version)
        # Columns are read from the table rather than assumed: this schema predates the
        # repository, so guessing its shape is how a fixture ends up testing a schema
        # that never existed.
        _insert(db, "users", {"id": SEED_USER, "display_name": "Fixture Owner"})
        for index in range(3):
            _insert(
                db,
                "raw_objects",
                {
                    "id": f"raw-fixture-{index}",
                    "user_id": SEED_USER,
                    "source": "upload",
                    "source_ref": f"sha256:fixture{index:058d}",
                    "raw_content": f"Фикстурная запись номер {index} для проверки миграций.",
                    "content_type": "text/plain",
                    "content_hash": f"{index:064d}",
                },
            )
        _insert(db, "runtime_kv", {"key": "fixture:marker", "value": f"schema-{version}"})
        db.commit()
    return _finalise(target, version)


def _finalise(source: Path, version: int) -> Path:
    """Fold in the WAL, shrink, and store compressed."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    staged = source.parent / f"staged-{version}.sqlite3"
    shutil.copy2(source, staged)
    with sqlite3.connect(staged) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("VACUUM")
    for leftover in (f"{staged}-wal", f"{staged}-shm"):
        Path(leftover).unlink(missing_ok=True)
    # An almost-empty database with forty tables is half a megabyte of page overhead
    # and compresses to two percent of that. Four of them belong in a repository at
    # 36 KB, not at 2 MB.
    destination = FIXTURES / f"schema-{version}.sqlite3.gz"
    with open(staged, "rb") as raw, gzip.open(destination, "wb", compresslevel=9) as packed:
        shutil.copyfileobj(raw, packed)
    staged.unlink(missing_ok=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-13-from",
        type=Path,
        help="A real schema-13 backup to lift the DDL from (structure only, no rows)",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="jericho-fixtures-") as temporary:
        workspace = Path(temporary)
        for version, commit in sorted(COMMITS.items()):
            path = build_from_commit(version, commit, workspace)
            print(f"  schema {version:>2}  {path.name}  {path.stat().st_size:>8,} bytes  (from {commit})")
        if args.schema_13_from:
            path = build_from_backup(13, args.schema_13_from.expanduser(), workspace)
            print(f"  schema 13  {path.name}  {path.stat().st_size:>8,} bytes  (DDL from a real backup)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
