"""A crash between the schema commit and the FTS build must not blind search.

The core migration commits the schema marker, and only then does the FTS phase
run — `executescript` makes the FTS DDL durable by itself, while the rebuild that
fills the index commits later. A process killed in that window leaves a database
whose schema marker is CURRENT and whose FTS tables EXIST but are empty. Every
later open then sees "marker current, tables present", skips the rebuild, and
every pre-crash document is unfindable by search — silently, permanently, and
with no error anywhere. `search_knowledge`'s LIKE fallback only fires when FTS
returns nothing at all, so one post-crash row hides the whole loss.

`integrity-check` cannot substitute for the marker: measured on SQLite, an
external-content index that is entirely empty PASSES it — the check compares the
index against itself, not against the content table it shadows.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace

from friday.storage import SCHEMA_VERSION, FridayStorage, init_storage
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _seed(storage, title: str, content: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("source"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def test_a_crash_before_the_fts_build_heals_on_the_next_open(settings, tmp_path):
    database = tmp_path / "crashed.sqlite3"
    tuned = replace(settings, database_path=database)

    storage = init_storage(tuned)
    storage.ensure_user("alice")
    target = _seed(storage, "Ведомость", "Тело документа про дежурства караула.")
    assert [row["id"] for row in storage.search_knowledge("alice", "дежурства")] == [target]
    storage.close(final=True)

    # Reconstruct the post-crash state exactly: FTS tables present (their DDL
    # auto-committed) but empty (the rebuild never committed), schema marker
    # untouched and current.
    raw_connection = sqlite3.connect(database)
    raw_connection.executescript(
        "DROP TABLE IF EXISTS knowledge_fts;\n"
        "DROP TABLE IF EXISTS raw_fts;\n"
        "DROP TABLE IF EXISTS messages_fts;\n"
    )
    from friday.storage._base import FTS_SCHEMA

    raw_connection.executescript(FTS_SCHEMA)
    raw_connection.execute("DELETE FROM schema_meta WHERE key='fts_build'")
    marker = raw_connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    assert marker and str(marker[0]) == str(SCHEMA_VERSION), "the premise: the schema looks current"
    empty = raw_connection.execute(
        "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'дежурства'"
    ).fetchone()[0]
    assert empty == 0, "the premise: the index is empty"
    raw_connection.commit()
    raw_connection.close()

    # Reopening must notice and rebuild.
    healed = FridayStorage(tuned)
    try:
        # A document written AFTER the crash is indexed by the triggers, so FTS
        # returns something — which switches off `search_knowledge`'s LIKE
        # fallback. Without this the fallback answers on behalf of the broken
        # index and the test passes on the broken code; the first version of this
        # test did exactly that, and the mutation showed it.
        fresh = _seed(healed, "Свежая", "Ещё одно тело про дежурства смены.")
        found = {row["id"] for row in healed.search_knowledge("alice", "дежурства")}
        assert fresh in found, "the premise: the fresh document is indexed"
        assert target in found, "search stayed blind to the pre-crash document"
    finally:
        healed.close(final=True)


def test_a_healthy_database_does_not_rebuild_on_every_open(settings, tmp_path):
    """The marker's other half: once recorded, reopening costs nothing extra."""
    database = tmp_path / "healthy.sqlite3"
    tuned = replace(settings, database_path=database)

    storage = init_storage(tuned)
    storage.ensure_user("alice")
    _seed(storage, "Приказ", "Ещё одно тело про отпуск.")
    storage.close(final=True)

    probe = sqlite3.connect(database)
    marker = probe.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone()
    probe.close()
    assert marker and str(marker[0]) == str(SCHEMA_VERSION)

    reopened = FridayStorage(tuned)
    try:
        assert reopened.search_knowledge("alice", "отпуск")
    finally:
        reopened.close(final=True)
