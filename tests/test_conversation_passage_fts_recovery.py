from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from typing import Any

import pytest

import friday.conversation_passages.schema as passage_schema
import friday.storage._core as storage_core
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import FridayStorage

_CONVERSATION_FTS_RECEIPT = "conversation_passage_fts_build"


def _simulate_missing_conversation_fts5(monkeypatch: pytest.MonkeyPatch) -> None:
    canonical = passage_schema._canonical_schema_objects

    def unavailable(*, include_fts: bool) -> dict[tuple[str, str], str]:
        if include_fts:
            raise sqlite3.OperationalError("no such module: fts5")
        return canonical(include_fts=False)

    monkeypatch.setattr(passage_schema, "_canonical_schema_objects", unavailable)


def test_concurrent_current_schema_repairs_one_missing_fts_artifact_once(
    settings: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """The derivative probe, install, rebuild and marker share one file lock."""

    database = tmp_path / "concurrent-missing-message-fts.sqlite3"
    configured = replace(
        settings,
        database_path=database,
        database_must_exist=False,
    )
    initial = FridayStorage(configured)
    initial.ensure_user("conversation-fts-recovery-owner")
    conversation = initial.create_conversation("conversation-fts-recovery-owner")
    initial.store_message(
        str(conversation["id"]),
        "conversation-fts-recovery-owner",
        "user",
        "Synthetic row requiring exactly one derivative rebuild",
    )
    initial.close(final=True)

    real_connect = sqlite3.connect
    with real_connect(database) as damaged:
        assert damaged.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone() == ("49",)
        damaged.execute("DROP TABLE messages_fts")

    first_phase_committed = threading.Barrier(2, timeout=10)
    count_lock = threading.Lock()
    rebuild_statements: list[str] = []

    class CoordinatedConnection(sqlite3.Connection):
        _first_explicit_commit = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.set_trace_callback(self._trace)

        @staticmethod
        def _trace(statement: str) -> None:
            normalized = "".join(statement.casefold().split())
            if "insertintomessages_fts(messages_fts)values('rebuild')" in normalized:
                with count_lock:
                    rebuild_statements.append(statement)

        def commit(self) -> None:
            super().commit()
            if self._first_explicit_commit:
                self._first_explicit_commit = False
                # Both openers have completed the authoritative phase.  Older
                # code had already cached the missing-table observation here;
                # both would subsequently rebuild from that stale decision.
                first_phase_committed.wait()

    worker_names = {"fts-recovery-opener-1", "fts-recovery-opener-2"}

    def controlled_connect(database_arg: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if threading.current_thread().name in worker_names:
            kwargs = {**kwargs, "factory": CoordinatedConnection}
        return real_connect(database_arg, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", controlled_connect)
    reopened_settings = replace(configured, database_must_exist=True)
    errors: list[BaseException] = []

    def open_storage() -> None:
        reopened = FridayStorage(reopened_settings)
        try:
            reopened.execute("SELECT 1").fetchone()
        except BaseException as exc:  # surfaced in the asserting test thread
            errors.append(exc)
        finally:
            reopened.close(final=True)

    workers = tuple(
        threading.Thread(target=open_storage, name=name, daemon=True) for name in sorted(worker_names)
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(rebuild_statements) == 1
    with real_connect(f"file:{database}?mode=ro", uri=True) as repaired:
        assert repaired.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone() == ("49",)
        assert repaired.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'synthetic'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "lane_failure",
    (
        sqlite3.OperationalError("synthetic conversation FTS operational failure"),
        sqlite3.DatabaseError("synthetic conversation FTS derivative failure"),
    ),
)
def test_conversation_fts_failure_preserves_released_message_search(
    settings: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    lane_failure: sqlite3.DatabaseError,
) -> None:
    database = tmp_path / "conversation-fts-lane-failure.sqlite3"
    configured = replace(settings, database_path=database)
    owner = "conversation-fts-lane-owner"
    initial = FridayStorage(configured)
    initial.ensure_user(owner)
    conversation = initial.create_conversation(owner)
    message = initial.store_message(
        str(conversation["id"]),
        owner,
        "user",
        "Legacy lane survives isolatedconversationfault",
    )
    initial.close(final=True)

    with sqlite3.connect(database) as damaged:
        assert damaged.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone() == ("49",)
        assert damaged.execute(
            "SELECT value FROM schema_meta WHERE key=?",
            (_CONVERSATION_FTS_RECEIPT,),
        ).fetchone() == ("49",)
        damaged.execute(
            "DELETE FROM schema_meta WHERE key=?",
            (_CONVERSATION_FTS_RECEIPT,),
        )
        # Exact post-authoritative-migration crash shape: schema 49 is durable,
        # while the released legacy FTS receipt still names its predecessor.
        damaged.execute("UPDATE schema_meta SET value='48' WHERE key='fts_build'")

    def fail_conversation_fts_install(
        _conn: sqlite3.Connection,
        *,
        _register_functions: bool = True,
        _validate_authoritative_data: bool = True,
    ) -> None:
        raise lane_failure

    monkeypatch.setattr(
        storage_core,
        "install_conversation_passage_fts_schema",
        fail_conversation_fts_install,
    )
    reopened = FridayStorage(replace(configured, database_must_exist=True))
    try:
        assert reopened._fts_available is True
        assert tuple(reopened.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone()) == (
            "49",
        )
        assert (
            reopened.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (_CONVERSATION_FTS_RECEIPT,),
            ).fetchone()
            is None
        )
        assert (
            reopened.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'isolatedconversationfault'"
            ).fetchone()[0]
            == 1
        )
        found = reopened.search_messages(owner, "isolatedconversationfault")
        assert [str(item["id"]) for item in found] == [str(message["id"])]
    finally:
        reopened.close(final=True)


def test_fts_created_backup_verifies_and_restores_on_no_fts5_fallback(
    settings: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "no-fts5-backup-restore.sqlite3"
    storage = FridayStorage(replace(settings, database_path=database))
    retained_owner = "conversation-no-fts5-retained"
    later_owner = "conversation-no-fts5-later"
    try:
        storage.ensure_user(retained_owner)
        conversation = storage.create_conversation(retained_owner)
        storage.store_message(
            str(conversation["id"]),
            retained_owner,
            "user",
            "Created while FTS5 was available",
        )
        source = storage.create_backup(label="fts5-created-schema49")
        storage.ensure_user(later_owner)

        _simulate_missing_conversation_fts5(monkeypatch)
        assert storage.verify_backup(str(source["database"]))["ok"] is True
        fallback_copy = storage.create_backup(label="no-fts5-schema49-copy")
        assert storage.verify_backup(str(fallback_copy["database"]))["ok"] is True

        with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
            restored = storage.restore_backup(
                str(source["database"]),
                safety_label="no-fts5-schema49-safety",
            )

        assert restored["ok"] is True
        assert storage.get_user(retained_owner) is not None
        assert storage.get_user(later_owner) is None
        assert storage.verify_backup(str(source["database"]))["ok"] is True
    finally:
        storage.close(final=True)
