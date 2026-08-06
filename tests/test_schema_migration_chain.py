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
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from friday.storage import SCHEMA_VERSION, FridayStorage

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
FIXTURE_USER = "fixture-owner"
# Rows every fixture carries, seeded by the builder.
FIXTURE_RAW_IDS = {f"raw-fixture-{index}" for index in range(3)}
FIXTURE_RELATION_ID = "relation-fixture-history"


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


def test_schema_31_fixture_carries_current_relation_and_captured_history(tmp_path) -> None:
    """The fixture must exercise schema 31's authoritative feature, not just DDL."""

    database = _unpack(31, tmp_path)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as fixture:
        current = fixture.execute(
            "SELECT user_id, source_entity_id, target_entity_id FROM relations WHERE id=?",
            (FIXTURE_RELATION_ID,),
        ).fetchone()
        revision = fixture.execute(
            """SELECT user_id, source_entity_id, target_entity_id, revision, present,
                      operation, history_quality, batch_id
               FROM relation_revisions WHERE relation_id=?""",
            (FIXTURE_RELATION_ID,),
        ).fetchone()

    assert current == (FIXTURE_USER, "entity-fixture-source", "entity-fixture-target")
    assert revision is not None
    assert revision[:-1] == (
        FIXTURE_USER,
        "entity-fixture-source",
        "entity-fixture-target",
        1,
        1,
        "insert",
        "captured",
    )
    assert str(revision[-1]).startswith("relation_batch_")


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


def test_concurrent_schema_open_refreshes_marker_after_acquiring_write_lock(
    settings, tmp_path, monkeypatch
) -> None:
    """A waiter must see the migration committed while it waited for BEGIN."""

    database = _unpack(30, tmp_path)
    real_connect = sqlite3.connect
    reader_at_begin = threading.Event()
    writer_finished = threading.Event()
    reader_errors: list[BaseException] = []

    class DelayedBeginConnection(sqlite3.Connection):
        _delayed = False

        def execute(self, sql, parameters=(), /):
            if str(sql).strip().upper() == "BEGIN IMMEDIATE" and not self._delayed:
                self._delayed = True
                reader_at_begin.set()
                if not writer_finished.wait(timeout=10):
                    raise AssertionError("concurrent schema writer did not finish")
            return super().execute(sql, parameters)

    def controlled_connect(database_arg, *args, **kwargs):
        if str(database_arg) == str(database) and threading.current_thread().name == "schema-stale-reader":
            kwargs = {**kwargs, "factory": DelayedBeginConnection}
        return real_connect(database_arg, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", controlled_connect)

    def open_waiting_reader() -> None:
        reader = FridayStorage(replace(settings, database_path=database))
        try:
            reader.execute("SELECT 1").fetchone()
        except BaseException as exc:  # reported in the asserting test thread
            reader_errors.append(exc)
        finally:
            reader.close()

    waiting_thread = threading.Thread(
        target=open_waiting_reader,
        name="schema-stale-reader",
        daemon=True,
    )
    waiting_thread.start()
    assert reader_at_begin.wait(timeout=10), "reader never reached the migration lock"

    writer = FridayStorage(replace(settings, database_path=database))
    try:
        assert writer.execute("SELECT 1").fetchone() is not None
    finally:
        writer.close()
        writer_finished.set()
        waiting_thread.join(timeout=10)

    assert not waiting_thread.is_alive()
    assert reader_errors == []
    with real_connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == (
            str(SCHEMA_VERSION),
        )


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

    # Start from the database the previous build actually produced. Artificially
    # relabelling a current schema is no longer equivalent: schema 31 contains an
    # authoritative history whose own marker/floor must fail closed if rewound.
    database = _unpack(SCHEMA_VERSION - 1, tmp_path)

    # Состаривание: отметка младше, столбец снят — как на базе, собранной прошлой
    # версией кода.
    aged = sqlite3.connect(database)
    try:
        aged.execute("ALTER TABLE monitors DROP COLUMN created_by")
        aged.commit()
    finally:
        aged.close()

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        columns = {row[1] for row in reopened.execute("PRAGMA table_info(monitors)").fetchall()}
        version = reopened.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
    finally:
        reopened.close()

    assert "created_by" in columns, "миграция не добралась до существующей базы"
    assert str(version) == str(SCHEMA_VERSION)


def test_schema_29_rebuilds_the_active_relation_index(settings, tmp_path):
    """`IF NOT EXISTS` cannot update a partial index whose WHERE clause changed."""
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage.models import RelationType

    # Use the real synthetic schema-29 fixture. Rewinding a schema-31 database to
    # marker 29 would correctly be rejected because its append-only revisions and
    # immutable floor cannot honestly be made legacy again.
    database = _unpack(29, tmp_path)
    source_id = "entity-schema29-source"
    target_id = "entity-schema29-target"
    first_id = "relation-schema29-finished"
    with sqlite3.connect(database) as aged:
        now = "2026-01-01T00:00:00Z"
        aged.executemany(
            """INSERT INTO entities(
                   id, user_id, name, normalized_name, entity_type,
                   created_at, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
            [
                (source_id, FIXTURE_USER, "Fixture Person", "fixture person", "person", now, now),
                (target_id, FIXTURE_USER, "Fixture Project", "fixture project", "project", now, now),
            ],
        )
        aged.execute(
            """INSERT INTO relations(
                   id, user_id, source_entity_id, target_entity_id, relation_type,
                   created_at, valid_from, valid_to
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                first_id,
                FIXTURE_USER,
                source_id,
                target_id,
                RelationType.MEMBER_OF.value,
                now,
                "2020-01-01",
                "2023-01-01",
            ),
        )

    reopened = FridayStorage(replace(settings, database_path=database))
    try:
        sql = str(
            reopened.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_active_relation'"
            ).fetchone()[0]
        )
        assert "valid_to IS NULL" in sql
        second = KnowledgeGraph(reopened).create_relation(
            FIXTURE_USER,
            source_id,
            target_id,
            RelationType.MEMBER_OF,
            valid_from="2024-01-01",
        )
        assert second.id != first_id
    finally:
        reopened.close()
