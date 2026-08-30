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
    python tools/build_schema_fixtures.py --schema-31-from /path/to/verified-schema-31.sqlite3
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
import hashlib
import sys
sys.path.insert(0, ".")
from friday.config import ensure_runtime_dirs, load_settings
from friday.storage import init_storage, SCHEMA_VERSION
from friday.storage.models import RawObject, new_id
from datetime import UTC, datetime

settings = load_settings()
ensure_runtime_dirs(settings)
storage = init_storage(settings)
storage.ensure_user({SEED_USER!r}, source="upload")
now = datetime.now(UTC).isoformat()
for index in range(3):
    body = f"Фикстурная запись номер {{index}} для проверки миграций."
    normalized = " ".join(body.split())
    receipt = (
        {{
            "extraction_receipt_version": 1,
            "extraction_success": True,
            "extraction_error": "",
            "text_extraction_success": True,
            "text_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            "extraction_chars": len(body),
            "text_truncated": False,
            "archive_truncated": False,
            "source_truncated_for_parse": False,
            "parse_deadline_reached": False,
            "parse_pages_read": 0,
            "parse_pages_truncated": False,
            "parse_total_pages": 0,
            "vision_pages_total": 0,
            "vision_pages_read": 0,
            "archive_files": 0,
            "archive_files_read": 0,
            "vision_used": False,
            "vision_review_required": False,
            "unsupported_format": False,
        }}
        if SCHEMA_VERSION >= 41 and index == 0
        else {{}}
    )
    storage.store_raw_object(
        RawObject(
            id=f"raw-fixture-{{index}}",
            user_id={SEED_USER!r},
            source="upload",
            source_ref=f"sha256:fixture{{index:058d}}",
            raw_content=body,
            content_type="file" if SCHEMA_VERSION >= 41 and index == 0 else "text/plain",
            metadata_json=receipt,
            content_hash=f"{{index:064d}}",
            received_at=now,
        )
    )
if SCHEMA_VERSION >= 48:
    # Exercise the released document-passage child-row contract, not only its
    # explicit-incomplete seed.  The synthetic corpus has exactly one live file.
    passage_report = storage.backfill_document_catalog(
        {SEED_USER!r},
        after_raw_object_id=None,
        limit=64,
        include_document_passages=True,
    )
    if passage_report["passage_changed"] != 1:
        raise RuntimeError(f"schema {{SCHEMA_VERSION}} fixture did not publish one passage set")
if SCHEMA_VERSION >= 49:
    # Schema 49 is deliberately reader-first; schema 50 activates the bounded
    # incremental writer without changing either ordinary table or the FTS view.
    conversation = storage.create_conversation(
        {SEED_USER!r},
        title="Synthetic migration conversation",
    )
    storage.store_message(
        conversation["id"],
        {SEED_USER!r},
        "user",
        "Synthetic schema fixture user message",
    )
    storage.store_message(
        conversation["id"],
        {SEED_USER!r},
        "assistant",
        "Synthetic schema fixture assistant message",
    )
    if SCHEMA_VERSION >= 50:
        passage_report = storage.backfill_conversation_passages({SEED_USER!r}, limit=8)
        if passage_report["anchors_written"] != 2 or passage_report["has_more"]:
            raise RuntimeError(
                f"schema {{SCHEMA_VERSION}} fixture did not publish its bounded conversation prefix"
            )
    projection = storage.execute(
        "SELECT projection_status,incomplete_reason,indexed_message_count,passage_count "
        "FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    child_count = storage.execute(
        "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()[0]
    expected_projection = (
        ("incomplete", "backfill_pending", 0, 0)
        if SCHEMA_VERSION == 49
        else ("current", None, 2, 2)
    )
    expected_children = 0 if SCHEMA_VERSION == 49 else 2
    if (
        projection is None
        or tuple(projection) != expected_projection
        or child_count != expected_children
    ):
        raise RuntimeError(
            f"schema {{SCHEMA_VERSION}} fixture has the wrong conversation-passage contour"
        )
if SCHEMA_VERSION >= 31:
    # Schema 31's defining authoritative data is relation history. Seed one
    # completely synthetic lineage so its committed fixture proves both the
    # current projection and captured revision survive reopen. Older historical
    # worktrees never import model shapes they did not ship.
    from friday.storage.models import Entity, EntityType, Relation, RelationType

    source = Entity(
        id="entity-fixture-source",
        user_id={SEED_USER!r},
        name="Fixture Person",
        entity_type=EntityType.PERSON,
    )
    target = Entity(
        id="entity-fixture-target",
        user_id={SEED_USER!r},
        name="Fixture Project",
        entity_type=EntityType.PROJECT,
    )
    storage.create_entity(source)
    storage.create_entity(target)
    storage.create_relation(
        Relation(
            id="relation-fixture-history",
            user_id={SEED_USER!r},
            source_entity_id=source.id,
            target_entity_id=target.id,
            relation_type=RelationType.WORKS_ON,
            weight=0.75,
            metadata_json={{"evidence": "synthetic fixture"}},
            created_at=now,
            valid_from="2026-01-01",
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
        env_line = f"FRIDAY_HOME={home}"
        reported = _run(
            ["env", env_line, sys.executable, str(script)],
            cwd=worktree,
        ).strip()
        if reported != str(version):
            raise SystemExit(f"commit {commit} reports schema {reported}, expected {version}")
        source = home / "data" / "state" / "friday.sqlite3"
        return _finalise(source, version)
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO)


def build_from_working_tree(workspace: Path) -> Path:
    """Let the CODE AS IT STANDS create its own database, whatever version that is.

    The historical fixtures come from historical commits, but the newest one
    cannot: the version that needs a fixture is the one being added right now,
    and it has no commit yet. Without this the step was manual, and a manual step
    in a chain the migration test walks is a gap waiting to happen — the test
    fails with "fixtures stop at 17 but the code is at 18" and the only
    documented way forward is a tool that cannot make 18.
    """
    home = workspace / "home-current"
    script = REPO / "_seed_fixture.py"
    script.write_text(_seed_script(home), encoding="utf-8")
    try:
        reported = _run(["env", f"FRIDAY_HOME={home}", sys.executable, str(script)], cwd=REPO).strip()
    finally:
        script.unlink(missing_ok=True)
    version = int(reported)
    return _finalise(home / "data" / "state" / "friday.sqlite3", version)


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
        if version == 31:
            # Schema 31 existed on deployed WIP installations before its source
            # checkpoint was committed. Lift only sqlite_master DDL above, then
            # build a wholly synthetic authoritative lineage: no row from the
            # operator's backup is ever selected or copied.
            floor = "2025-12-31T23:59:59.999999Z"
            recorded_at = "2026-01-01T00:00:00.000000Z"
            _insert(
                db,
                "schema_meta",
                {
                    "key": "relation_history_complete_from",
                    "value": floor,
                    "updated_at": floor,
                },
            )
            db.execute(
                """INSERT INTO relation_revision_context(singleton, batch_id, recorded_at)
                   VALUES(1, 'relation_batch_fixture', ?)""",
                (recorded_at,),
            )
            _insert(
                db,
                "entities",
                {
                    "id": "entity-fixture-source",
                    "user_id": SEED_USER,
                    "name": "Fixture Person",
                    "normalized_name": "fixture person",
                    "entity_type": "person",
                },
            )
            _insert(
                db,
                "entities",
                {
                    "id": "entity-fixture-target",
                    "user_id": SEED_USER,
                    "name": "Fixture Project",
                    "normalized_name": "fixture project",
                    "entity_type": "project",
                },
            )
            _insert(
                db,
                "relations",
                {
                    "id": "relation-fixture-history",
                    "user_id": SEED_USER,
                    "source_entity_id": "entity-fixture-source",
                    "target_entity_id": "entity-fixture-target",
                    "relation_type": "works_on",
                    "weight": 0.75,
                    "metadata_json": '{"evidence":"synthetic fixture"}',
                    "created_at": recorded_at,
                    "valid_from": "2026-01-01",
                },
            )
            db.execute("UPDATE relation_revision_context SET batch_id='', recorded_at='' WHERE singleton=1")
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
    parser.add_argument(
        "--schema-31-from",
        type=Path,
        help="A verified schema-31 backup to lift deployed WIP DDL from (structure only, no rows)",
    )
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="Build only the fixture for the schema this working tree produces",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="jericho-fixtures-") as temporary:
        workspace = Path(temporary)
        if args.current_only:
            path = build_from_working_tree(workspace)
            print(f"  current    {path.name}  {path.stat().st_size:>8,} bytes  (from the working tree)")
            return 0
        path = build_from_working_tree(workspace)
        print(f"  current    {path.name}  {path.stat().st_size:>8,} bytes  (from the working tree)")
        for version, commit in sorted(COMMITS.items()):
            path = build_from_commit(version, commit, workspace)
            print(f"  schema {version:>2}  {path.name}  {path.stat().st_size:>8,} bytes  (from {commit})")
        if args.schema_13_from:
            path = build_from_backup(13, args.schema_13_from.expanduser(), workspace)
            print(f"  schema 13  {path.name}  {path.stat().st_size:>8,} bytes  (DDL from a real backup)")
        if args.schema_31_from:
            path = build_from_backup(31, args.schema_31_from.expanduser(), workspace)
            print(
                f"  schema 31  {path.name}  {path.stat().st_size:>8,} bytes  (deployed DDL, synthetic rows)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
