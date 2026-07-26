"""A backup you cannot open is not a backup.

``verify_backup`` checks integrity, foreign keys, the manifest digest — and that the
schema version is a number in range. It does not check that the migration chain runs
on that database, which is the one thing the restore path actually needs. Between them
these are not the same claim: 0 ≤ 13 ≤ 16 is arithmetic, not a migration.

The existing migration tests hand-build a few tables with a handful of rows, so they
test the migration their author had in mind. These fixtures are real databases: 14, 15
and 16 were created by checking out the commit that introduced them and letting THAT
code build its own schema; 13 predates this repository, so its structure was lifted
from a real backup. They contain no personal data — every row is written by
``tools/build_schema_fixtures.py``.

What is asserted is survival, not arithmetic. A migration that silently drops rows
would move the version marker and pass any check that only reads the marker.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from jericho.storage import SCHEMA_VERSION, JerichoStorage

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
FIXTURE_USER = "fixture-owner"
# Rows every fixture carries, seeded by the builder.
FIXTURE_RAW_IDS = {f"raw-fixture-{index}" for index in range(3)}


def _fixture_versions() -> list[int]:
    return sorted(int(path.name.split("-")[1].split(".")[0]) for path in FIXTURES.glob("schema-*.sqlite3.gz"))


def _unpack(version: int, destination: Path) -> Path:
    archive = FIXTURES / f"schema-{version}.sqlite3.gz"
    database = destination / f"schema-{version}.sqlite3"
    with gzip.open(archive, "rb") as packed, open(database, "wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def test_the_fixture_set_covers_every_schema_back_to_the_oldest_backup() -> None:
    """A gap here means a restore path nobody exercises."""
    versions = _fixture_versions()
    assert versions, "no schema fixtures found; run tools/build_schema_fixtures.py"
    assert max(versions) == SCHEMA_VERSION, (
        f"fixtures stop at schema {max(versions)} but the code is at {SCHEMA_VERSION}. "
        "Add a fixture for the new version — see tools/build_schema_fixtures.py."
    )
    assert versions == list(range(min(versions), max(versions) + 1)), f"gap in {versions}"


@pytest.mark.parametrize("version", _fixture_versions())
def test_a_database_at_this_schema_migrates_forward_and_keeps_its_data(version, settings, tmp_path):
    database = _unpack(version, tmp_path)

    before = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        assert (
            int(before.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
            == version
        )
    finally:
        before.close()

    storage = JerichoStorage(replace(settings, database_path=database))
    try:
        marker = storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert int(marker[0]) == SCHEMA_VERSION, f"schema {version} did not migrate to {SCHEMA_VERSION}"

        assert storage.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert storage.execute("PRAGMA foreign_key_check").fetchall() == []

        # Survival, not arithmetic: a migration that dropped the rows would still have
        # moved the marker above.
        assert storage.get_user(FIXTURE_USER) is not None, "the migration lost the account"
        surviving = {
            row["id"]
            for row in storage.execute(
                "SELECT id FROM raw_objects WHERE user_id=?", (FIXTURE_USER,)
            ).fetchall()
        }
        assert surviving == FIXTURE_RAW_IDS, f"raw objects lost: {sorted(FIXTURE_RAW_IDS - surviving)}"
        assert storage.kv_get("fixture:marker") == f"schema-{version}"
    finally:
        storage.close()


@pytest.mark.parametrize("version", _fixture_versions())
def test_migrating_twice_changes_nothing(version, settings, tmp_path):
    """Restores get retried, and a migration that is not idempotent corrupts on the
    second open rather than the first."""
    database = _unpack(version, tmp_path)

    first = JerichoStorage(replace(settings, database_path=database))
    try:
        snapshot = sorted(
            (row["type"], row["name"])
            for row in first.execute("SELECT type, name FROM sqlite_master").fetchall()
        )
    finally:
        first.close()

    second = JerichoStorage(replace(settings, database_path=database))
    try:
        again = sorted(
            (row["type"], row["name"])
            for row in second.execute("SELECT type, name FROM sqlite_master").fetchall()
        )
        assert again == snapshot
        assert second.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        second.close()


@pytest.mark.parametrize("version", _fixture_versions())
def test_a_migrated_database_can_be_written_to(version, settings, tmp_path):
    """Opening is not enough: the restored database has to accept the next write."""
    database = _unpack(version, tmp_path)
    storage = JerichoStorage(replace(settings, database_path=database))
    try:
        storage.record_event("migration.smoke", {"from_schema": version})
        assert storage.list_events(event_type="migration.smoke")[0]["payload"]["from_schema"] == version
    finally:
        storage.close()


def test_the_fixtures_carry_no_personal_data() -> None:
    """These files are committed. Nothing from the owner's database may be in them."""
    for version in _fixture_versions():
        archive = FIXTURES / f"schema-{version}.sqlite3.gz"
        with gzip.open(archive, "rb") as packed:
            blob = packed.read()
        # The builder seeds exactly one account, and it is not a real one.
        assert FIXTURE_USER.encode() in blob
        for forbidden in (b"telegram_user", b"@gmail", b"Bearer "):
            assert forbidden not in blob, f"schema-{version} fixture contains {forbidden!r}"


# --- the owner's real backups, when they are present ----------------------

REAL_BACKUPS = os.environ.get("JERICHO_TEST_BACKUPS_DIR", "")


@pytest.mark.skipif(not REAL_BACKUPS, reason="set JERICHO_TEST_BACKUPS_DIR to check real backups")
def test_every_real_backup_migrates_and_opens(settings, tmp_path):
    """Point this at a live backups directory to rehearse the actual restore.

    The committed fixtures prove the chain runs on databases this project built. They
    cannot prove it runs on the databases sitting on THIS machine, which are the ones a
    restore would use. Opt-in because those files are personal data and their contents
    never belong in a test run that ships.
    """
    backups = sorted(Path(REAL_BACKUPS).expanduser().glob("*.sqlite3"))
    assert backups, f"no backups found in {REAL_BACKUPS}"

    checked: list[tuple[str, int]] = []
    for backup in backups:
        probe = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            row = probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        finally:
            probe.close()
        assert row is not None, f"{backup.name} has no schema_version marker"
        origin = int(row[0])

        # Work on a copy: a test must never migrate the owner's actual backup.
        working = tmp_path / backup.name
        shutil.copy2(backup, working)
        storage = JerichoStorage(replace(settings, database_path=working))
        try:
            marker = storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            assert int(marker[0]) == SCHEMA_VERSION, f"{backup.name}: schema {origin} did not migrate"
            assert storage.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert storage.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            storage.close()
        checked.append((backup.name, origin))

    print(f"\n  migrated {len(checked)} real backups: {sorted({v for _, v in checked})} -> {SCHEMA_VERSION}")
