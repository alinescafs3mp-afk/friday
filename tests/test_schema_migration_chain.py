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

from friday.storage import SCHEMA_VERSION, FridayStorage

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

    storage = FridayStorage(replace(settings, database_path=database))
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

    first = FridayStorage(replace(settings, database_path=database))
    try:
        snapshot = sorted(
            (row["type"], row["name"])
            for row in first.execute("SELECT type, name FROM sqlite_master").fetchall()
        )
    finally:
        first.close()

    second = FridayStorage(replace(settings, database_path=database))
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
    storage = FridayStorage(replace(settings, database_path=database))
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

REAL_BACKUPS = os.environ.get("FRIDAY_TEST_BACKUPS_DIR", "")


@pytest.mark.skipif(not REAL_BACKUPS, reason="set FRIDAY_TEST_BACKUPS_DIR to check real backups")
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
        storage = FridayStorage(replace(settings, database_path=working))
        try:
            marker = storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            assert int(marker[0]) == SCHEMA_VERSION, f"{backup.name}: schema {origin} did not migrate"
            assert storage.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert storage.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            storage.close()
        checked.append((backup.name, origin))

    print(f"\n  migrated {len(checked)} real backups: {sorted({v for _, v in checked})} -> {SCHEMA_VERSION}")


def _seed_ignored_verdict(storage, user_id: str = "owner") -> tuple[str, str]:
    """Raw object + KO + inbox row, then the IGNORED verdict applied by hand.

    Mirrors what `ingestion/_review.py` does for InboxStatus.IGNORED: soft-delete
    the attached Knowledge Object and clear the Inbox link. Done at the storage
    layer so the test pins the *migration*, not the review service.
    """
    from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject, new_id

    storage.ensure_user(user_id)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content="черновик, который владелец отверг",
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=raw.raw_content,
        title="черновик",
        summary=raw.raw_content,
    )
    storage.store_knowledge_object(ko)
    item = storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=user_id,
            raw_object_id=raw.id,
            knowledge_object_id=ko.id,
        )
    )
    storage.soft_delete_knowledge_object(ko.id, user_id)
    storage.update_inbox_status(
        item.id,
        InboxStatus.IGNORED,
        "owner",
        user_id=user_id,
        clear_knowledge_object_id=True,
    )
    row = storage.get_inbox_item(item.id, user_id)
    assert not row["knowledge_object_id"], "seed failed: the verdict did not clear the link"
    return item.id, ko.id


def test_reopening_does_not_resurrect_an_ignored_verdict(settings, tmp_path):
    """ "Игнорировать" is a verdict, and a migration must not overrule it.

    DATA_LIFECYCLE §3: IGNORED soft-deletes the attached Knowledge Object and
    clears the Inbox link, so the material leaves retrieval. The legacy-link
    reconstruction in `_migrate_legacy_schema` then re-pointed that Inbox row at
    the very object the human had just rejected — it matched on raw_object_id
    with no `deleted_at` filter, and it ran on *every* process start rather than
    only on an actual upgrade. Restart the backend and the rejected item is back
    in the Inbox wearing its old KO.
    """
    database = tmp_path / "verdict.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    try:
        inbox_id, ko_id = _seed_ignored_verdict(first)
    finally:
        first.close()

    second = FridayStorage(replace(settings, database_path=database))
    try:
        row = second.get_inbox_item(inbox_id, "owner")
        assert row is not None
        assert not row["knowledge_object_id"], f"reopening re-linked the ignored item to soft-deleted {ko_id}"
    finally:
        second.close()


def test_legacy_reconstruction_is_skipped_once_the_schema_is_current(settings, tmp_path):
    """The backfill is an upgrade step, not a startup chore.

    It was invoked unconditionally by every process's first connection, which is
    both how a fixed backfill keeps re-firing on already-correct data and a scan
    of every entity row on every single start.
    """
    database = tmp_path / "current.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    try:
        first.ensure_user("owner")
    finally:
        first.close()

    from friday.storage._core import CoreMixin

    calls: list[int] = []
    original = CoreMixin._migrate_legacy_schema

    def counting(self, conn):
        calls.append(1)
        return original(self, conn)

    # Patch the class that DEFINES the method. Assigning to FridayStorage would
    # add a shadowing entry to the subclass __dict__ that reassignment cannot
    # remove, and `test_no_method_is_defined_twice_across_the_class_hierarchy`
    # would then fail in whichever test file happens to run next.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(CoreMixin, "_migrate_legacy_schema", counting)
    try:
        second = FridayStorage(replace(settings, database_path=database))
        try:
            second.execute("SELECT 1").fetchone()
        finally:
            second.close()
    finally:
        monkeypatch.undo()
    assert calls == [], "legacy reconstruction ran again on an already-current database"


def test_a_new_column_needs_a_new_schema_number(settings, tmp_path):
    """Столбец, добавленный без роста номера схемы, до живой базы НЕ доедет.

    Случилось 2026-08-04 и стоило пятиминутной поломки живого маршрута. Столбец
    `monitors.created_by` был добавлен и в `CREATE TABLE`, и в список миграции —
    а номер схемы остался прежним. `_migrate_legacy_schema` вызывается ТОЛЬКО
    когда отметка в базе меньше текущего числа, поэтому на существующей базе
    столбец не появился, код его уже читал, и `/api/me/monitors` отдавал 500.

    Весь набор тестов при этом был зелёным: там база каждый раз создаётся с нуля
    по актуальному `CREATE TABLE`, где столбец есть. Ровно тот случай, когда
    проверять надо ПОСТАВЛЯЕМОЕ — обновление существующей базы, а не создание
    новой.

    Этот тест ставит базу в состояние «схема на единицу младше» и требует, чтобы
    открытие её обновило: так забытый номер краснеет здесь, а не у человека.
    """
    from friday.storage._base import SCHEMA_VERSION

    database = tmp_path / "aged.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    try:
        made.ensure_user("owner")
    finally:
        made.close()

    # Состаривание: отметка младше, столбец снят — как на базе, собранной прошлой
    # версией кода.
    aged = sqlite3.connect(database)
    try:
        aged.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        aged.execute("ALTER TABLE monitors DROP COLUMN created_by")
        aged.commit()
    finally:
        aged.close()

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        columns = {row[1] for row in reopened.execute("PRAGMA table_info(monitors)").fetchall()}
        version = reopened.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        reopened.close()

    assert "created_by" in columns, "миграция не добралась до существующей базы"
    assert str(version) == str(SCHEMA_VERSION)


def test_schema_29_rebuilds_the_active_relation_index(settings, tmp_path):
    """`IF NOT EXISTS` cannot update a partial index whose WHERE clause changed."""
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage.models import EntityType, RelationType

    database = tmp_path / "schema-29-relation-index.sqlite3"
    made = FridayStorage(replace(settings, database_path=database))
    try:
        graph = KnowledgeGraph(made)
        person = graph.create_entity("alice", "Иван Иванов", EntityType.PERSON)
        unit = graph.create_entity("alice", "Проект Альфа", EntityType.PROJECT)
        first = graph.create_relation(
            "alice",
            str(person["id"]),
            str(unit["id"]),
            RelationType.MEMBER_OF,
            valid_from="2020-01-01",
        )
        graph.invalidate_relation("alice", first.id, valid_to="2023-01-01")
    finally:
        made.close()

    aged = sqlite3.connect(database)
    try:
        aged.execute("DROP INDEX uq_active_relation")
        aged.execute(
            """CREATE UNIQUE INDEX uq_active_relation
               ON relations(user_id, source_entity_id, target_entity_id, relation_type)
               WHERE deleted_at IS NULL"""
        )
        aged.execute("UPDATE schema_meta SET value='29' WHERE key='schema_version'")
        aged.commit()
    finally:
        aged.close()

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        sql = str(
            reopened.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_active_relation'"
            ).fetchone()[0]
        )
        assert "valid_to IS NULL" in sql
        second = KnowledgeGraph(reopened).create_relation(
            "alice",
            str(person["id"]),
            str(unit["id"]),
            RelationType.MEMBER_OF,
            valid_from="2024-01-01",
        )
        assert second.id != first.id
    finally:
        reopened.close()
