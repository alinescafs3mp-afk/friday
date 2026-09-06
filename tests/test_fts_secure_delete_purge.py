"""FTS5 secure-delete plus external-content DELETE must leave a sound index.

The secondary-product witness purge enables FTS5 ``secure-delete`` and then
deletes the content row. On SQLite 3.45.1 that sequence corrupts the inverted
index unless the remaining ``raw_objects`` reconstruct it in the same
transaction. This is a disposable-schema regression, not a production-DB repair.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE raw_objects(
  id INTEGER PRIMARY KEY,
  raw_content TEXT NOT NULL
);
CREATE VIRTUAL TABLE raw_fts USING fts5(
  raw_content,
  content=raw_objects,
  content_rowid=rowid,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER raw_objects_ai AFTER INSERT ON raw_objects BEGIN
  INSERT INTO raw_fts(rowid, raw_content) VALUES (new.rowid, new.raw_content);
END;
CREATE TRIGGER raw_objects_ad AFTER DELETE ON raw_objects BEGIN
  INSERT INTO raw_fts(raw_fts, rowid, raw_content) VALUES ('delete', old.rowid, old.raw_content);
END;
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute("INSERT INTO raw_objects(id, raw_content) VALUES (1, 'secret payload a')")
    conn.execute("INSERT INTO raw_objects(id, raw_content) VALUES (2, 'kept payload b')")
    conn.commit()
    return conn


def _integrity(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("PRAGMA integrity_check")]


def test_secure_delete_content_delete_rebuild_keeps_integrity_and_remaining_hits(
    tmp_path: Path,
) -> None:
    conn = _connect(tmp_path / "purge.sqlite3")
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO raw_fts(raw_fts, rank) VALUES('secure-delete', 1)")
        enabled = conn.execute("SELECT v AS value FROM raw_fts_config WHERE k='secure-delete'").fetchone()
        assert enabled is not None and int(enabled["value"]) == 1
        conn.execute("DELETE FROM raw_objects WHERE id=1")
        conn.execute("INSERT INTO raw_fts(raw_fts) VALUES('rebuild')")
        conn.execute("COMMIT")
        assert _integrity(conn) == ["ok"]
        assert conn.execute("SELECT rowid FROM raw_fts WHERE raw_fts MATCH 'secret'").fetchall() == []
        kept = conn.execute("SELECT rowid FROM raw_fts WHERE raw_fts MATCH 'kept'").fetchall()
        assert [row[0] for row in kept] == [2]
        assert [row[0] for row in conn.execute("SELECT id FROM raw_objects ORDER BY id")] == [2]
    finally:
        conn.close()


def test_secure_delete_content_delete_rebuild_rolls_back_to_both_rows(
    tmp_path: Path,
) -> None:
    conn = _connect(tmp_path / "rollback.sqlite3")
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO raw_fts(raw_fts, rank) VALUES('secure-delete', 1)")
        conn.execute("DELETE FROM raw_objects WHERE id=1")
        conn.execute("INSERT INTO raw_fts(raw_fts) VALUES('rebuild')")
        conn.execute("ROLLBACK")
        assert _integrity(conn) == ["ok"]
        secret = conn.execute("SELECT rowid FROM raw_fts WHERE raw_fts MATCH 'secret'").fetchall()
        kept = conn.execute("SELECT rowid FROM raw_fts WHERE raw_fts MATCH 'kept'").fetchall()
        assert [row[0] for row in secret] == [1]
        assert [row[0] for row in kept] == [2]
        assert [row[0] for row in conn.execute("SELECT id FROM raw_objects ORDER BY id")] == [1, 2]
    finally:
        conn.close()
