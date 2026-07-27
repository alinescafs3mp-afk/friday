"""Concurrency stress tests for the connection-per-thread storage.

These directly exercise the rearchitecture that replaced a single shared
``sqlite3.Connection`` (guarded by an RLock held only around ``conn.execute()``,
with ``.fetchone()/.fetchall()`` running after the lock had already been
released) with one connection per thread over WAL. Under the old model,
background workers on threadpool threads stepped cursors on the *same*
connection concurrently with request handlers — a documented "recursive use of
cursors" / half-written-read hazard that surfaced as ``None`` in NOT NULL
columns (e.g. ``quality_score``). The tests below hammer reads and writes from
many threads on one shared :class:`JerichoStorage` and assert no exceptions and
internally consistent reads.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import replace

from jericho.storage import JerichoStorage
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make_pair(user_id: str, n: int) -> tuple[RawObject, KnowledgeObject]:
    content = f"note {n} about project alpha and person ivan"
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="stress",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
    )
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=f"note {n}",
        summary=content,
        tags_json=["stress", "alpha"],
    )
    return raw, ko


def _run_threads(targets: list[threading.Thread]) -> None:
    for thread in targets:
        thread.start()
    for thread in targets:
        thread.join()


def test_parallel_writers_and_readers_never_corrupt(storage):
    """Many concurrent writers + readers must not raise or read torn rows."""

    user_id = "alice"
    storage.ensure_user(user_id)

    writers, per_writer = 8, 20
    readers, read_rounds = 8, 30
    total = writers * per_writer

    barrier = threading.Barrier(writers + readers)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def record(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    def writer(offset: int) -> None:
        try:
            barrier.wait()
            for i in range(per_writer):
                raw, ko = _make_pair(user_id, offset * per_writer + i)
                storage.store_raw_object(raw)
                storage.store_knowledge_object(ko)
        except BaseException as exc:  # noqa: BLE001 — surface any thread failure
            record(exc)

    def reader() -> None:
        try:
            barrier.wait()
            for _ in range(read_rounds):
                # Each fetch pulls a full result set on THIS thread's own
                # connection; under the old shared-connection model a concurrent
                # writer stepping the shared cursor could corrupt these reads.
                count = storage.count_knowledge_objects(user_id)
                assert 0 <= count <= total
                rows = storage.list_knowledge_objects(user_id, limit=total)
                assert len({row["id"] for row in rows}) == len(rows)
                for row in rows:
                    # A half-written read used to surface as None in NOT NULL cols.
                    assert row["id"]
                    assert row["quality_score"] is not None
                    assert row["promotion_score"] is not None
                storage.search_knowledge(user_id, "alpha", limit=5)
                # The exact execute()+fetchall() shape that raced across threads.
                storage.execute(
                    "SELECT COUNT(*) FROM knowledge_objects WHERE user_id=?",
                    (user_id,),
                ).fetchall()
        except BaseException as exc:  # noqa: BLE001 — surface any thread failure
            record(exc)

    threads = [threading.Thread(target=writer, args=(w,), name=f"w{w}") for w in range(writers)]
    threads += [threading.Thread(target=reader, name=f"r{r}") for r in range(readers)]
    _run_threads(threads)

    assert not errors, f"concurrent access raised: {errors[:3]}"
    assert storage.count_knowledge_objects(user_id) == total
    rows = storage.list_knowledge_objects(user_id, limit=total)
    assert len({row["id"] for row in rows}) == total


def test_wal_reader_sees_committed_writes_across_connections(storage):
    """A write committed on one thread is visible to a read on another thread.

    Production reads-after-writes cross the request-handler / worker-thread
    boundary; WAL makes committed writes visible on every other connection, but
    only once committed. This guards against a regression where connection
    isolation would hide committed data across threads.
    """

    user_id = "bob"
    storage.ensure_user(user_id)
    raw, ko = _make_pair(user_id, 0)

    def writer() -> None:
        storage.store_raw_object(raw)
        storage.store_knowledge_object(ko)

    thread = threading.Thread(target=writer)
    thread.start()
    thread.join()

    # Read back on the main thread's own (separate) connection.
    fetched = storage.get_knowledge_object(ko.id, user_id)
    assert fetched is not None
    assert fetched["id"] == ko.id
    assert storage.count_knowledge_objects(user_id) == 1


def test_optimize_serialises_with_writers(storage):
    """optimize() (PRAGMA optimize runs ANALYZE, a write) must take the write lock.

    The background maintenance worker calls storage.optimize() on a threadpool
    thread; it writes sqlite_stat*, so it must serialise with transaction() writers
    rather than contending at the SQLite level. This guards the write path against a
    regression to the lockless execute("PRAGMA optimize") that bypassed the lock.
    """

    user_id = "frank"
    storage.ensure_user(user_id)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(3)

    def writer(offset: int) -> None:
        try:
            barrier.wait()
            for i in range(20):
                raw, ko = _make_pair(user_id, offset * 100 + i)
                storage.store_raw_object(raw)
                storage.store_knowledge_object(ko)
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    def optimizer() -> None:
        try:
            barrier.wait()
            for _ in range(15):
                storage.optimize()
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(1,)),
        threading.Thread(target=optimizer),
    ]
    _run_threads(threads)

    assert not errors, f"optimize()/writers raised under contention: {errors[:3]}"
    assert storage.count_knowledge_objects(user_id) == 40


def test_close_shuts_every_thread_connection_and_reopens(settings, tmp_path):
    """close() must shut connections opened on other threads, then reopen cleanly.

    restore_backup() relies on close() releasing every WAL lock (from whatever
    thread opened the connection) before it swaps the database file, and on the
    same storage object transparently reopening afterwards.
    """

    database = tmp_path / "concurrency.sqlite3"
    store = JerichoStorage(replace(settings, database_path=database))
    try:
        store.ensure_user("carol")

        # Open a connection on a *different* thread and register it.
        def touch() -> None:
            store.execute("SELECT 1").fetchone()

        worker = threading.Thread(target=touch)
        worker.start()
        worker.join()

        # More than one connection is now registered (main thread + worker).
        assert len(store._connections) >= 2  # noqa: SLF001 — asserting internal invariant

        store.close()
        # All connections closed and the registry cleared.
        assert store._connections == []  # noqa: SLF001

        # Reopen-after-close: the next use transparently rebuilds a connection.
        store.ensure_user("dave")
        assert store.count_knowledge_objects("dave") == 0
        assert len(store._connections) >= 1  # noqa: SLF001
    finally:
        store.close()


def test_close_waits_for_in_flight_write_transaction(settings, tmp_path):
    """close() must drain an in-flight write transaction, not truncate it.

    Regression guard for the shutdown race: a background worker's asyncio task can
    be cancelled while its ``asyncio.to_thread(storage.<write>)`` call is still
    running on a threadpool thread; the server then calls storage.close(). close()
    must wait for that write transaction to commit before closing the connection —
    otherwise it either commits a half-written transaction or closes the
    connection mid-statement (raising in the worker thread and losing the write).
    """

    database = tmp_path / "drain.sqlite3"
    store = JerichoStorage(replace(settings, database_path=database))
    try:
        store.ensure_user("erin")
        raw, ko = _make_pair("erin", 0)
        store.store_raw_object(raw)

        inside_tx = threading.Event()
        hold_for = 0.4
        worker_errors: list[BaseException] = []

        def writer() -> None:
            try:
                # Hold one write transaction open across a real KO write (nested
                # store_knowledge_object shares this outer transaction on this
                # thread's connection) so close() must drain it before closing.
                with store.transaction():
                    store.store_knowledge_object(ko)
                    # Signal that the transaction is open, then keep it open long
                    # enough for the main thread to attempt close() and block.
                    inside_tx.set()
                    time.sleep(hold_for)
            except BaseException as exc:  # noqa: BLE001 — surface any thread failure
                worker_errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        assert inside_tx.wait(timeout=5.0)

        started = time.monotonic()
        store.close()  # must block until the writer commits and releases the write lock
        waited = time.monotonic() - started
        thread.join(timeout=5.0)

        assert not worker_errors, f"writer failed (connection closed mid-transaction?): {worker_errors}"
        # close() drained the in-flight transaction rather than truncating it.
        assert waited >= hold_for - 0.1, f"close() did not wait for the writer (waited {waited:.2f}s)"

        # The write is durable after reopen-after-close.
        assert store.get_knowledge_object(ko.id, "erin") is not None
        assert store.count_knowledge_objects("erin") == 1
    finally:
        store.close()


def test_an_interrupted_transaction_is_not_committed_by_close(settings, tmp_path):
    """Ctrl-C mid-write must abort the unit of work, not persist half of it.

    ``transaction()`` caught only ``Exception``, so ``KeyboardInterrupt``,
    ``SystemExit`` and ``asyncio.CancelledError`` unwound past the rollback and
    left ``BEGIN IMMEDIATE`` open on the connection. ``close()`` then called
    ``connection.commit()`` on the way out — turning an interrupted `jericho
    import` or a cancelled worker tick into a durable partial write. Both halves
    are fixed here: rollback on BaseException, and close() rolls an open
    transaction back instead of committing it.
    """
    import pytest

    from jericho.storage.models import utc_now

    database = tmp_path / "interrupted.sqlite3"
    store = JerichoStorage(replace(settings, database_path=database))
    try:
        store.ensure_user("owner")
        raw, ko = _make_pair("owner", 1)
        with pytest.raises(KeyboardInterrupt), store.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_kv(key, value, updated_at) VALUES('half-written','1',?)",
                (utc_now(),),
            )
            raise KeyboardInterrupt
        # The connection must be usable again, not stuck inside an open BEGIN.
        assert not store.conn.in_transaction
    finally:
        store.close()

    reopened = JerichoStorage(replace(settings, database_path=database))
    try:
        row = reopened.execute("SELECT value FROM runtime_kv WHERE key='half-written'").fetchone()
        assert row is None, "an interrupted transaction survived close() as a durable write"
    finally:
        reopened.close()
    del raw, ko


def _one_knowledge_object(storage, user_id: str = "owner"):
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user(user_id)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content="исходный текст",
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content="исходный текст",
        title="Заметка",
        summary="исходный текст",
    )
    storage.store_knowledge_object(ko)
    return ko


def test_concurrent_edits_to_one_object_all_survive(storage):
    """A lost edit is worse than a failed one: nothing says it happened.

    `update_knowledge_fields` read the row, merged the change and wrote — with the
    write lock held only for the last step. Two editors both read version 1, both
    computed 2, and the second UPDATE overwrote the first. The version snapshot went
    with it, because `_store_ko_version` is INSERT OR IGNORE on
    `(knowledge_object_id, version)`, so the duplicate is dropped in silence.

    Reproduced before the fix, three runs out of three: six concurrent edits ended
    at **version 3 instead of 7** with three snapshots instead of seven. No
    exception anywhere — four edits and four pieces of history simply gone.
    """
    writers = 6
    ko = _one_knowledge_object(storage)
    barrier = threading.Barrier(writers)
    failures: list[BaseException] = []

    def edit(index: int) -> None:
        try:
            barrier.wait()
            storage.update_knowledge_fields(ko.id, "owner", title=f"правка-{index}")
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            failures.append(exc)

    threads = [threading.Thread(target=edit, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, f"writers failed: {failures}"
    final = storage.get_knowledge_object(ko.id, "owner")
    assert int(final["version"]) == writers + 1, "an edit was overwritten"

    versions = [
        int(row["version"])
        for row in storage.execute(
            "SELECT version FROM knowledge_object_versions WHERE knowledge_object_id=? ORDER BY version",
            (ko.id,),
        ).fetchall()
    ]
    assert versions == list(range(1, writers + 2)), f"version history has holes: {versions}"
    # The surviving title belongs to one of the writers, not to a half-applied merge.
    assert final["title"] in {f"правка-{index}" for index in range(writers)}


def test_concurrent_entity_edits_all_survive(storage):
    """`update_entity` had the identical shape, and so does its snapshot table."""
    from jericho.storage.models import Entity, EntityType, new_id

    writers = 6
    storage.ensure_user("owner")
    entity = Entity(
        id=new_id("ent"),
        user_id="owner",
        name="Проект Орион",
        entity_type=EntityType.PROJECT.value,
    )
    storage.create_entity(entity)
    barrier = threading.Barrier(writers)
    failures: list[BaseException] = []

    def edit(index: int) -> None:
        try:
            current = storage.get_entity(entity.id, "owner")
            updated = Entity(
                id=str(current["id"]),
                user_id="owner",
                name=f"Орион {index}",
                entity_type=str(current["entity_type"]),
                version=int(current["version"]),
            )
            barrier.wait()
            storage.update_entity(updated)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=edit, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, f"writers failed: {failures}"
    assert int(storage.get_entity(entity.id, "owner")["version"]) == writers + 1
    versions = [int(row["version"]) for row in storage.list_entity_versions(entity.id, "owner")]
    assert sorted(versions) == list(range(1, writers + 2)), f"version history has holes: {versions}"
