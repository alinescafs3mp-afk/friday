"""Schema-50 incremental conversation-passage guard migration."""

from __future__ import annotations

import gzip
import json
import re
import shutil
import sqlite3
import subprocess  # nosec B404 - fixed local crash probe
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import friday.storage._core as storage_core
from friday.conversation_passages.schema import (
    _SCHEMA_49_PUBLICATION_GUARDS,
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
    CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES,
    _canonical_released_schema_v49_objects,
    _canonical_schema_objects,
    _fts_objects,
    _ordinary_objects,
    _schema_objects,
    conversation_passage_anchor_locator_sha256,
    conversation_passage_content_sha256,
    conversation_passage_message_revision_sha256,
    conversation_passage_prefix_sha256,
    conversation_passage_schema_fingerprint,
    conversation_passage_set_extend_sha256,
    register_conversation_passage_connection_functions,
    validate_conversation_passage_schema,
)
from friday.storage import SCHEMA_VERSION, FridayStorage, UnsupportedSchemaVersionError

SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
FIXTURE_TITLE = "Synthetic migration conversation"
_CHILD_INSERT_SQL = """INSERT INTO conversation_passages(
       conversation_id,anchor_message_id,anchor_ordinal,
       anchor_message_revision_sha256,anchor_content_sha256,
       anchor_locator_sha256,conversation_prefix_sha256
   ) VALUES(?,?,?,?,?,?,?)"""


def _canonical_first_child(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> tuple[tuple[object, ...], str]:
    source = conn.execute(
        """SELECT id,conversation_id,user_id,role,content,created_at
             FROM messages WHERE conversation_id=?
              AND role IN ('user','assistant') ORDER BY rowid ASC LIMIT 1""",
        (conversation_id,),
    ).fetchone()
    assert source is not None
    revision = conversation_passage_message_revision_sha256(
        message_id=source[0],
        conversation_id=source[1],
        principal_id=source[2],
        role=source[3],
        content=source[4],
        created_at=source[5],
    )
    content_digest = conversation_passage_content_sha256(source[4])
    locator = conversation_passage_anchor_locator_sha256(
        conversation_id=source[1],
        anchor_message_id=source[0],
        anchor_ordinal=0,
    )
    prefix = conversation_passage_prefix_sha256(None, 0, revision)
    child = (
        source[1],
        source[0],
        0,
        revision,
        content_digest,
        locator,
        prefix,
    )
    passage_set = conversation_passage_set_extend_sha256(
        CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
        (0, source[0], revision, content_digest, locator, prefix),
    )
    return child, passage_set


def _unpack_schema_49(tmp_path: Path, name: str) -> Path:
    database = tmp_path / name
    with gzip.open(SCHEMA_FIXTURES / "schema-49.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def _sidecar_rows(conn: sqlite3.Connection) -> tuple[tuple[tuple[object, ...], ...], ...]:
    parents = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT rowid,* FROM conversation_passage_projections ORDER BY conversation_id"
        )
    )
    children = tuple(
        tuple(row) for row in conn.execute("SELECT * FROM conversation_passages ORDER BY passage_rowid")
    )
    return parents, children


def _publish_released_fixture_prefix(database: Path) -> None:
    with sqlite3.connect(database) as predecessor:
        predecessor.row_factory = sqlite3.Row
        register_conversation_passage_connection_functions(predecessor)
        conversation = predecessor.execute(
            "SELECT id,user_id FROM conversations WHERE title=?",
            (FIXTURE_TITLE,),
        ).fetchone()
        assert conversation is not None
        sources = predecessor.execute(
            """SELECT id,conversation_id,user_id,role,content,created_at
                 FROM messages WHERE conversation_id=?
                  AND role IN ('user','assistant') ORDER BY rowid""",
            (conversation["id"],),
        ).fetchall()
        assert len(sources) == 2

        prefix: str | None = None
        passage_set = CONVERSATION_PASSAGE_EMPTY_SET_SHA256
        for ordinal, source in enumerate(sources):
            revision = conversation_passage_message_revision_sha256(
                message_id=source["id"],
                conversation_id=source["conversation_id"],
                principal_id=source["user_id"],
                role=source["role"],
                content=source["content"],
                created_at=source["created_at"],
            )
            content_digest = conversation_passage_content_sha256(source["content"])
            locator = conversation_passage_anchor_locator_sha256(
                conversation_id=source["conversation_id"],
                anchor_message_id=source["id"],
                anchor_ordinal=ordinal,
            )
            prefix = conversation_passage_prefix_sha256(prefix, ordinal, revision)
            row = (ordinal, source["id"], revision, content_digest, locator, prefix)
            passage_set = conversation_passage_set_extend_sha256(passage_set, row)
            predecessor.execute(
                """INSERT INTO conversation_passages(
                       conversation_id,anchor_message_id,anchor_ordinal,
                       anchor_message_revision_sha256,anchor_content_sha256,
                       anchor_locator_sha256,conversation_prefix_sha256
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    source["conversation_id"],
                    source["id"],
                    ordinal,
                    revision,
                    content_digest,
                    locator,
                    prefix,
                ),
            )
            final = ordinal == len(sources) - 1
            predecessor.execute(
                """UPDATE conversation_passage_projections
                      SET indexed_message_count=?,indexed_through_message_id=?,
                          indexed_conversation_revision_sha256=?,passage_set_sha256=?,
                          projection_status=?,incomplete_reason=?,passage_count=?
                    WHERE conversation_id=?""",
                (
                    ordinal + 1,
                    source["id"],
                    prefix,
                    passage_set,
                    "current" if final else "incomplete",
                    None if final else "backfill_pending",
                    ordinal + 1,
                    source["conversation_id"],
                ),
            )


def _seed_released_source_unavailable(
    database: Path,
    *,
    content: object,
    suffix: str,
    created_at: str = "2026-08-29T10:00:01+00:00",
    terminalize: bool = True,
) -> str:
    conversation_id = f"conv_{suffix:0>16}"
    message_id = f"msg_{suffix:0>16}"
    with sqlite3.connect(database) as predecessor:
        register_conversation_passage_connection_functions(predecessor)
        predecessor.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES(?, 'fixture-owner', 'Source unavailable predecessor', ?, ?)""",
            (
                conversation_id,
                "2026-08-29T10:00:00+00:00",
                "2026-08-29T10:00:00+00:00",
            ),
        )
        predecessor.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(?,?,'fixture-owner','user',?,'{}',NULL,?)""",
            (message_id, conversation_id, content, created_at),
        )
        if terminalize:
            predecessor.execute(
                """UPDATE conversation_passage_projections
                      SET incomplete_reason='source_unavailable'
                    WHERE conversation_id=?""",
                (conversation_id,),
            )
    return conversation_id


def test_schema50_changes_only_incremental_guards_and_bounded_indexes() -> None:
    assert SCHEMA_VERSION == 50
    released = _ordinary_objects(_canonical_released_schema_v49_objects(include_fts=False))
    current = _ordinary_objects(_canonical_schema_objects(include_fts=False))

    assert set(current) - set(released) == {
        ("index", "idx_conversation_passage_conversation_owner_keyset"),
        ("index", "idx_conversation_passage_message_source_order"),
        ("trigger", "conversation_passage_ai_parent_cas"),
        ("trigger", "conversation_passage_bd_validate"),
        ("trigger", "conversation_passage_conversation_bi_identity"),
        ("trigger", "conversation_passage_projection_au_reset_children"),
        ("trigger", "conversation_passage_projection_bd_validate"),
    }
    assert set(released) - set(current) == set()
    assert {key for key in released.keys() & current.keys() if released[key] != current[key]} == {
        ("trigger", "conversation_passage_projection_bi_validate"),
        ("trigger", "conversation_passage_projection_bu_validate"),
        ("trigger", "conversation_passage_bi_validate"),
        ("trigger", "conversation_passage_bu_validate"),
        ("trigger", "conversation_passage_conversation_bu_reset"),
        ("trigger", "conversation_passage_message_ai_invalidate"),
        ("trigger", "conversation_passage_message_au_reset"),
        ("trigger", "conversation_passage_message_bi_identity_immutable"),
    }
    assert set(_SCHEMA_49_PUBLICATION_GUARDS) == {
        name
        for (kind, name) in released.keys() & current.keys()
        if kind == "trigger" and released[(kind, name)] != current[(kind, name)]
    }
    assert (
        current[("table", "conversation_passage_projections")]
        == released[("table", "conversation_passage_projections")]
    )
    assert current[("table", "conversation_passages")] == released[("table", "conversation_passages")]
    assert _fts_objects(_canonical_schema_objects(include_fts=True)) == _fts_objects(
        _canonical_released_schema_v49_objects(include_fts=True)
    )

    source_index = current[("index", "idx_conversation_passage_message_source_order")]
    assert "ONmessages(user_id,conversation_id)WHEREroleIN('user','assistant')" in source_index
    keyset_index = current[("index", "idx_conversation_passage_conversation_owner_keyset")]
    assert "ONconversations(user_id,id)" in keyset_index
    parent_guard = current[("trigger", "conversation_passage_projection_bu_validate")]
    assert "COUNT(" not in parent_guard.upper()
    assert "friday_conversation_passage_set_sha256(" not in parent_guard
    assert "friday_conversation_passage_set_extend_sha256(" in parent_guard
    assert parent_guard.count("length(CAST(first_source.contentASBLOB))") == 1
    assert parent_guard.count("friday_conversation_passage_utf8_valid(CAST(first_source.contentASBLOB))") == 1
    assert "octet_length(" not in parent_guard
    assert "friday_conversation_passage_anchor_valid(" not in parent_guard
    assert "friday_conversation_passage_source_descriptor_valid(" in parent_guard
    parent_guard_without_terminal_body_proofs = parent_guard.replace(
        "length(CAST(first_source.contentASBLOB))",
        "",
    ).replace(
        "friday_conversation_passage_utf8_valid(CAST(first_source.contentASBLOB))",
        "",
    )
    assert (
        re.search(
            r"(?<![0-9A-Za-z_])source\.content(?![0-9A-Za-z_])",
            parent_guard_without_terminal_body_proofs,
        )
        is None
    )
    atomic_cas = current[("trigger", "conversation_passage_ai_parent_cas")]
    assert "COUNT(" not in atomic_cas.upper()
    assert "friday_conversation_passage_set_sha256(" not in atomic_cas
    assert "friday_conversation_passage_set_extend_sha256(" in atomic_cas
    assert "idx_conversation_passage_message_source_order" in atomic_cas
    assert re.search(r"(?<![0-9A-Za-z_])source\.content(?![0-9A-Za-z_])", atomic_cas) is None
    reset_guard = current[("trigger", "conversation_passage_projection_au_reset_children")]
    assert "DELETEFROMconversation_passages" in reset_guard
    assert "UPDATEconversation_passage_projections" in reset_guard
    assert "friday_conversation_passage_utf8_valid(CAST(first_source.contentASBLOB))" in reset_guard
    assert reset_guard.index("DELETEFROMconversation_passages") < reset_guard.index(
        "UPDATEconversation_passage_projections"
    )


def test_exact_schema49_current_rows_migrate_without_identity_or_fts_layout_change(
    settings,
    tmp_path: Path,
) -> None:
    database = _unpack_schema_49(tmp_path, "schema49-current-prefix-to-50.sqlite3")
    _publish_released_fixture_prefix(database)
    with sqlite3.connect(database) as predecessor:
        before_rows = _sidecar_rows(predecessor)
        before_tables = {
            str(row[0]): str(row[1])
            for row in predecessor.execute(
                """SELECT name,sql FROM sqlite_master
                     WHERE type='table' AND name IN (
                           'conversation_passage_projections','conversation_passages')"""
            )
        }
        before_fts = _fts_objects(_schema_objects(predecessor))

    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "50"
        )
        assert _sidecar_rows(migrated.conn) == before_rows
        after_tables = {
            str(row[0]): str(row[1])
            for row in migrated.execute(
                """SELECT name,sql FROM sqlite_master
                     WHERE type='table' AND name IN (
                           'conversation_passage_projections','conversation_passages')"""
            )
        }
        assert after_tables == before_tables
        assert _fts_objects(_schema_objects(migrated.conn)) == before_fts
        assert (
            migrated.execute(
                """SELECT COUNT(*) FROM conversation_passages_fts
                WHERE conversation_passages_fts MATCH 'Synthetic'"""
            ).fetchone()[0]
            == 2
        )
        index = migrated.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='index'
                   AND name='idx_conversation_passage_message_source_order'"""
        ).fetchone()
        assert index is not None and "WHERE role IN ('user','assistant')" in str(index[0])
        validate_conversation_passage_schema(migrated.conn)
        first_fingerprint = conversation_passage_schema_fingerprint(migrated.conn)
    finally:
        migrated.close(final=True)

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert _sidecar_rows(reopened.conn) == before_rows
        assert conversation_passage_schema_fingerprint(reopened.conn) == first_fingerprint
    finally:
        reopened.close(final=True)


def test_next_eligible_source_plan_is_partial_index_ordered(storage: FridayStorage) -> None:
    owner = "schema50-plan-owner"
    conversation = storage.create_conversation(owner)
    for index in range(4):
        storage.store_message(
            conversation["id"],
            owner,
            "user" if index % 2 == 0 else "assistant",
            f"bounded plan message {index}",
        )
    detail = " ".join(
        str(row[3])
        for row in storage.execute(
            """EXPLAIN QUERY PLAN
               SELECT rowid,id FROM messages
                    INDEXED BY idx_conversation_passage_message_source_order
                WHERE user_id=? AND conversation_id=?
                  AND role IN ('user','assistant') AND rowid>?
                ORDER BY rowid ASC LIMIT 1""",
            (owner, conversation["id"], 0),
        ).fetchall()
    )
    assert "idx_conversation_passage_message_source_order" in detail
    assert "rowid>?" in detail
    assert "USE TEMP B-TREE" not in detail
    index_row = next(
        row
        for row in storage.execute("PRAGMA index_list(messages)").fetchall()
        if row[1] == "idx_conversation_passage_message_source_order"
    )
    assert index_row[4] == 1
    keyset_detail = " ".join(
        str(row[3])
        for row in storage.execute(
            """EXPLAIN QUERY PLAN
               SELECT id FROM conversations
                    INDEXED BY idx_conversation_passage_conversation_owner_keyset
                WHERE user_id=? AND id>?
                ORDER BY id ASC LIMIT 3""",
            (owner, ""),
        ).fetchall()
    )
    assert "idx_conversation_passage_conversation_owner_keyset" in keyset_detail
    assert "user_id=? AND id>?" in keyset_detail
    assert "USE TEMP B-TREE" not in keyset_detail


def test_canonical_conversation_and_eligible_message_ids_are_enforced_at_admission(
    storage: FridayStorage,
) -> None:
    owner = "schema50-canonical-admission-owner"
    storage.ensure_user(owner)
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_conversation_identity_invalid",
        ),
    ):
        conn.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES('not-canonical',?,'invalid',?,?)""",
            (owner, "2026-08-29T10:00:00+00:00", "2026-08-29T10:00:00+00:00"),
        )

    conversation = storage.create_conversation(owner)
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_identity_immutable",
        ),
    ):
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES('not-canonical',?,?,'user','body','{}',NULL,?)""",
            (conversation["id"], owner, "2026-08-29T10:00:01+00:00"),
        )

    with storage.transaction() as conn:
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_message_bi_identity_immutable'"""
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        conn.execute("DROP TRIGGER conversation_passage_message_bi_identity_immutable")
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES('not-canonical',?,?,'user','body','{}',NULL,?)""",
            (conversation["id"], owner, "2026-08-29T10:00:01+00:00"),
        )
        conn.execute(str(trigger[0]))  # nosec B608 - exact authenticated SQLite DDL
    with pytest.raises(sqlite3.DatabaseError, match="Schema 50 conversation passage data"):
        validate_conversation_passage_schema(storage.conn)


def test_malformed_utf8_text_is_rejected_at_admission_and_by_full_validation(
    storage: FridayStorage,
) -> None:
    owner = "schema50-malformed-utf8-owner"
    conversation = storage.create_conversation(owner)
    timestamp = "2026-08-30T00:00:00+00:00"
    statement = """INSERT INTO messages(
           id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
       ) VALUES(
           'msg_dddddddddddddddd',?,?,'user',CAST(x'80' AS TEXT),'{}',NULL,?
       )"""
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_identity_immutable",
        ),
    ):
        conn.execute(statement, (conversation["id"], owner, timestamp))

    with storage.transaction() as conn:
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_message_bi_identity_immutable'"""
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        conn.execute("DROP TRIGGER conversation_passage_message_bi_identity_immutable")
        conn.execute(statement, (conversation["id"], owner, timestamp))
        conn.execute(str(trigger[0]))  # nosec B608 - exact authenticated SQLite DDL

    with pytest.raises(sqlite3.DatabaseError, match="Schema 50 conversation passage data"):
        validate_conversation_passage_schema(storage.conn)


def test_exact_oversized_utf8_poison_cannot_be_admitted_or_terminalized(
    storage: FridayStorage,
) -> None:
    owner = "schema50-oversized-utf8-poison-owner"
    conversation = storage.create_conversation(owner)
    statement = """INSERT INTO messages(
           id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
       ) VALUES(
           'msg_eeeeeeeeeeeeeeee',?,?,'user',
           CAST(x'80'||zeroblob(4194304) AS TEXT),'{}',NULL,?
       )"""
    parameters = (conversation["id"], owner, "2026-08-30T00:00:00+00:00")
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_identity_immutable",
        ),
    ):
        conn.execute(statement, parameters)

    with storage.transaction() as conn:
        guard_rows = conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE type='trigger' AND name IN (
                       'conversation_passage_message_bi_identity_immutable',
                       'conversation_passage_message_ai_invalidate'
                 ) ORDER BY name"""
        ).fetchall()
        guards = {str(row["name"]): str(row["sql"]) for row in guard_rows}
        assert set(guards) == {
            "conversation_passage_message_ai_invalidate",
            "conversation_passage_message_bi_identity_immutable",
        }
        for name in guards:
            conn.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - authenticated names
        conn.execute(statement, parameters)
        for sql in guards.values():
            conn.execute(sql)  # nosec B608 - exact authenticated SQLite DDL

    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_invalid",
        ),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_unavailable'
                WHERE conversation_id=?""",
            (conversation["id"],),
        )
    with pytest.raises(sqlite3.DatabaseError, match="Schema 50 conversation passage data"):
        validate_conversation_passage_schema(storage.conn)


def test_full_validator_rejects_a_forged_noncanonical_conversation_identity(
    storage: FridayStorage,
) -> None:
    owner = "schema50-forged-conversation-owner"
    storage.ensure_user(owner)
    with storage.transaction() as conn:
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_conversation_bi_identity'"""
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        conn.execute("DROP TRIGGER conversation_passage_conversation_bi_identity")
        conn.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES('aaa',?,'forged invalid identity',?,?)""",
            (owner, "2026-08-29T10:00:00+00:00", "2026-08-29T10:00:00+00:00"),
        )
        conn.execute(str(trigger[0]))  # nosec B608 - exact authenticated SQLite DDL
    with pytest.raises(sqlite3.DatabaseError, match="Schema 50 conversation passage data"):
        validate_conversation_passage_schema(storage.conn)


@pytest.mark.parametrize("poison", ("conversation_id", "message_id"))
def test_schema49_migration_rejects_noncanonical_authoritative_identities(
    settings,
    tmp_path: Path,
    poison: str,
) -> None:
    database = _unpack_schema_49(tmp_path, f"schema49-{poison}-poison.sqlite3")
    with sqlite3.connect(database) as predecessor:
        register_conversation_passage_connection_functions(predecessor)
        if poison == "conversation_id":
            predecessor.execute(
                """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
                   VALUES('aaa','fixture-owner','invalid identity',?,?)""",
                ("2026-08-29T10:00:00+00:00", "2026-08-29T10:00:00+00:00"),
            )
        else:
            conversation = predecessor.execute(
                "SELECT id FROM conversations WHERE title=?",
                (FIXTURE_TITLE,),
            ).fetchone()
            assert conversation is not None
            predecessor.execute(
                """INSERT INTO messages(
                       id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                   ) VALUES('aaa',?,'fixture-owner','user','invalid identity','{}',NULL,?)""",
                (conversation[0], "2026-08-29T10:00:01+00:00"),
            )

    rejected = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 49 conversation passage data"):
            rejected.execute("SELECT 1").fetchone()
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("49",)


@pytest.mark.parametrize("oversized", (False, True))
def test_schema49_migration_rejects_malformed_utf8_text_before_activation(
    settings,
    tmp_path: Path,
    oversized: bool,
) -> None:
    label = "oversized" if oversized else "small"
    database = _unpack_schema_49(tmp_path, f"schema49-malformed-utf8-{label}.sqlite3")
    suffix = "eeeeeeeeeeeeeeee" if oversized else "dddddddddddddddd"
    conversation_id = f"conv_{suffix}"
    message_id = f"msg_{suffix}"
    with sqlite3.connect(database) as predecessor:
        register_conversation_passage_connection_functions(predecessor)
        predecessor.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES(?,'fixture-owner','malformed utf8',?,?)""",
            (
                conversation_id,
                "2026-08-30T00:00:00+00:00",
                "2026-08-30T00:00:00+00:00",
            ),
        )
        predecessor.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(
                   ?,?,'fixture-owner','user',
                   CASE WHEN ?=1
                        THEN CAST(x'80'||zeroblob(4194304) AS TEXT)
                        ELSE CAST(x'80' AS TEXT) END,
                   '{}',NULL,?
               )""",
            (message_id, conversation_id, int(oversized), "2026-08-30T00:00:00+00:00"),
        )

    rejected = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 49 conversation passage data"):
            rejected.execute("SELECT 1").fetchone()
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("49",)
        assert probe.execute("SELECT id FROM messages WHERE id=?", (message_id,)).fetchone() == (message_id,)


@pytest.mark.parametrize("malformation", ("blob_content", "invalid_timestamp"))
def test_malformed_oversized_first_source_cannot_terminalize_or_pass_validation(
    storage: FridayStorage,
    malformation: str,
) -> None:
    owner = f"schema50-terminal-{malformation}-owner"
    conversation = storage.create_conversation(owner)
    content: object = (
        sqlite3.Binary(b"x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1))
        if malformation == "blob_content"
        else "x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1)
    )
    created_at = (
        "2026-08-29T10:00:01+00:00" if malformation == "blob_content" else "not-a-canonical-timestamp"
    )
    message_id = "msg_0000000000005b10" if malformation == "blob_content" else "msg_0000000000005a10"
    message_parameters = (message_id, conversation["id"], owner, content, created_at)
    message_sql = """INSERT INTO messages(
           id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
       ) VALUES(?,?,?,'user',?,'{}',NULL,?)"""
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_identity_immutable",
        ),
    ):
        conn.execute(message_sql, message_parameters)

    with storage.transaction() as conn:
        guard_rows = conn.execute(
            """SELECT name,sql FROM sqlite_master WHERE type='trigger'
                 AND name IN (
                     'conversation_passage_message_bi_identity_immutable',
                     'conversation_passage_message_ai_invalidate'
                 ) ORDER BY name"""
        ).fetchall()
        guards = {str(row["name"]): str(row["sql"]) for row in guard_rows}
        assert set(guards) == {
            "conversation_passage_message_ai_invalidate",
            "conversation_passage_message_bi_identity_immutable",
        }
        for name in guards:
            conn.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - authenticated names
        conn.execute(message_sql, message_parameters)
        for sql in guards.values():
            conn.execute(sql)  # nosec B608 - exact authenticated SQLite DDL

    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_invalid",
        ),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_unavailable'
                WHERE conversation_id=?""",
            (conversation["id"],),
        )

    with storage.transaction() as conn:
        parent_guard = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_projection_bu_validate'"""
        ).fetchone()
        assert parent_guard is not None and isinstance(parent_guard[0], str)
        conn.execute("DROP TRIGGER conversation_passage_projection_bu_validate")
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_unavailable'
                WHERE conversation_id=?""",
            (conversation["id"],),
        )
        conn.execute(str(parent_guard[0]))  # nosec B608 - exact authenticated SQLite DDL
    with pytest.raises(sqlite3.DatabaseError, match="Schema 50 conversation passage data"):
        validate_conversation_passage_schema(storage.conn)


def test_direct_child_insert_atomically_advances_parent_and_replay_is_closed(
    storage: FridayStorage,
) -> None:
    owner = "schema50-atomic-child-owner"
    conversation = storage.create_conversation(owner)
    storage.store_message(conversation["id"], owner, "user", "atomic child source")
    child, expected_set = _canonical_first_child(storage.conn, conversation["id"])

    with storage.transaction() as conn:
        inserted = conn.execute(_CHILD_INSERT_SQL, child)
        assert inserted.rowcount == 1

    parent = storage.execute(
        """SELECT indexed_message_count,indexed_through_message_id,
                  indexed_conversation_revision_sha256,passage_set_sha256,
                  projection_status,incomplete_reason,passage_count
             FROM conversation_passage_projections WHERE conversation_id=?""",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None
    assert tuple(parent) == (
        1,
        child[1],
        child[6],
        expected_set,
        "current",
        None,
        1,
    )
    before = _sidecar_rows(storage.conn)
    with (
        storage.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute(_CHILD_INSERT_SQL, child)
    assert _sidecar_rows(storage.conn) == before
    validate_conversation_passage_schema(storage.conn)


def test_direct_parent_only_advance_aborts_without_a_child(storage: FridayStorage) -> None:
    owner = "schema50-parent-only-owner"
    conversation = storage.create_conversation(owner)
    storage.store_message(conversation["id"], owner, "user", "parent-only source")
    child, expected_set = _canonical_first_child(storage.conn, conversation["id"])
    before = _sidecar_rows(storage.conn)

    with (
        storage.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError, match="conversation_passage_projection_invalid"),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=1,indexed_through_message_id=?,
                      indexed_conversation_revision_sha256=?,passage_set_sha256=?,
                      projection_status='current',incomplete_reason=NULL,passage_count=1
                WHERE conversation_id=?""",
            (child[1], child[6], expected_set, conversation["id"]),
        )

    assert _sidecar_rows(storage.conn) == before
    validate_conversation_passage_schema(storage.conn)


def test_atomic_child_parent_cas_rolls_back_across_abort_and_hard_crash_then_writer_replays(
    settings,
    tmp_path: Path,
) -> None:
    database = tmp_path / "schema50-atomic-crash.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    owner = "schema50-atomic-crash-owner"
    conversation = first.create_conversation(owner)
    first.store_message(conversation["id"], owner, "user", "crash replay source")
    child, _expected_set = _canonical_first_child(first.conn, conversation["id"])

    class RollbackProbe(RuntimeError):
        pass

    with (
        pytest.raises(RollbackProbe, match="rollback atomic child"),
        first.transaction() as conn,
    ):
        conn.execute(_CHILD_INSERT_SQL, child)
        raise RollbackProbe("rollback atomic child")
    assert (
        first.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    assert (
        first.execute(
            "SELECT passage_count FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    first.close(final=True)

    crash_program = f"""
import json
import os
import sqlite3
import sys
from friday.conversation_passages.schema import register_conversation_passage_connection_functions

conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA foreign_keys=ON')
register_conversation_passage_connection_functions(conn)
conn.execute('BEGIN IMMEDIATE')
conn.execute({_CHILD_INSERT_SQL!r}, tuple(json.loads(sys.argv[2])))
os._exit(0)
"""
    subprocess.run(  # nosec B603 - fixed interpreter and local crash program
        [sys.executable, "-c", crash_program, str(database), json.dumps(child)],
        cwd=Path(__file__).parent.parent,
        check=True,
    )

    replay = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            replay.execute(
                "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            replay.execute(
                "SELECT passage_count FROM conversation_passage_projections WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()[0]
            == 0
        )
        report = replay.backfill_conversation_passages(owner, limit=1)
        assert report["anchors_written"] == 1
        assert report["current"] == 1
        assert (
            replay.execute(
                "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            replay.execute(
                "SELECT passage_count FROM conversation_passage_projections WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()[0]
            == 1
        )
        validate_conversation_passage_schema(replay.conn)
    finally:
        replay.close(final=True)


def test_parent_delete_is_fenced_and_exact_reset_cleans_fts_then_rebuilds(
    storage: FridayStorage,
) -> None:
    owner = "schema50-parent-reset-owner"
    conversation = storage.create_conversation(owner)
    for body in ("reset first", "reset second"):
        storage.store_message(conversation["id"], owner, "user", body)
    assert storage.backfill_conversation_passages(owner, limit=2)["anchors_written"] == 2
    before = _sidecar_rows(storage.conn)

    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_delete_invalid",
        ),
    ):
        conn.execute(
            "DELETE FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation["id"],),
        )
    assert _sidecar_rows(storage.conn) == before

    with storage.transaction() as conn:
        changed = conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=0,indexed_through_message_id=NULL,
                      indexed_conversation_revision_sha256=NULL,passage_set_sha256=NULL,
                      projection_status='incomplete',incomplete_reason='source_changed',
                      passage_count=0
                WHERE conversation_id=?""",
            (conversation["id"],),
        )
        assert changed.rowcount == 1
    parent = storage.execute(
        """SELECT projection_status,incomplete_reason,indexed_message_count,passage_count
             FROM conversation_passage_projections WHERE conversation_id=?""",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None and tuple(parent) == ("incomplete", "source_changed", 0, 0)
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages_fts").fetchone()[0] == 0
    validate_conversation_passage_schema(storage.conn, require_fts=True)

    rebuilt = storage.backfill_conversation_passages(owner, limit=2)
    assert rebuilt["anchors_written"] == 2
    assert rebuilt["current"] == 1
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_created_at_reset_is_atomic_and_conversation_identity_is_immutable(
    storage: FridayStorage,
) -> None:
    owner = "schema50-source-reset-owner"
    conversation = storage.create_conversation(owner)
    message = storage.store_message(conversation["id"], owner, "user", "reset timestamp")
    assert storage.backfill_conversation_passages(owner, limit=1)["current"] == 1

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET created_at=? WHERE id=?",
            ("2026-08-29T15:00:00+00:00", message["id"]),
        )
    parent = storage.execute(
        """SELECT projection_status,incomplete_reason,passage_count
             FROM conversation_passage_projections WHERE conversation_id=?""",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None and tuple(parent) == ("incomplete", "source_changed", 0)
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages_fts").fetchone()[0] == 0
    validate_conversation_passage_schema(storage.conn, require_fts=True)

    storage.ensure_user("schema50-source-reset-other-owner")
    for sql, parameters in (
        (
            "UPDATE conversations SET id='conv_ffffffffffffffff' WHERE id=?",
            (conversation["id"],),
        ),
        (
            "UPDATE conversations SET user_id=? WHERE id=?",
            ("schema50-source-reset-other-owner", conversation["id"]),
        ),
    ):
        with (
            storage.transaction() as conn,
            pytest.raises(
                sqlite3.IntegrityError,
                match="conversation_passage_conversation_identity_immutable",
            ),
        ):
            conn.execute(sql, parameters)


def test_authorized_conversation_delete_cascade_removes_parent_child_and_fts(
    storage: FridayStorage,
) -> None:
    owner = "schema50-conversation-cascade-owner"
    conversation = storage.create_conversation(owner)
    storage.store_message(conversation["id"], owner, "user", "cascade source")
    assert storage.backfill_conversation_passages(owner, limit=1)["current"] == 1

    with storage.transaction() as conn:
        trigger_rows = conn.execute(
            """SELECT name,sql FROM sqlite_master
                WHERE type='trigger' AND name IN (
                    'conversations_are_never_deleted','messages_are_never_deleted'
                ) ORDER BY name"""
        ).fetchall()
        trigger_sql = {str(row["name"]): str(row["sql"]) for row in trigger_rows}
        assert set(trigger_sql) == {
            "conversations_are_never_deleted",
            "messages_are_never_deleted",
        }
        for trigger_name in trigger_sql:
            conn.execute(f'DROP TRIGGER "{trigger_name}"')  # nosec B608 - SQLite-owned names
        conn.execute("PRAGMA defer_foreign_keys=ON")
        conn.execute("DELETE FROM conversations WHERE id=?", (conversation["id"],))
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation["id"],))
        for sql in trigger_sql.values():
            conn.execute(sql)  # nosec B608 - exact SQLite-owned canonical DDL

    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages_fts").fetchone()[0] == 0


@pytest.mark.parametrize("trigger_name", _SCHEMA_49_PUBLICATION_GUARDS)
def test_altered_schema49_predecessor_fails_before_guard_replacement(
    settings,
    tmp_path: Path,
    trigger_name: str,
) -> None:
    database = _unpack_schema_49(tmp_path, f"schema49-altered-{trigger_name}.sqlite3")
    with sqlite3.connect(database) as altered:
        altered.execute(f'DROP TRIGGER "{trigger_name}"')  # nosec B608 - fixed allowlist
    with sqlite3.connect(database) as probe:
        before_rows = _sidecar_rows(probe)
        before_schema = tuple(
            tuple(row)
            for row in probe.execute(
                """SELECT type,name,sql FROM sqlite_master
                     WHERE sql IS NOT NULL ORDER BY type,name"""
            )
        )

    rejected = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 49 conversation passage DDL"):
            rejected.execute("SELECT 1").fetchone()
    finally:
        rejected.close(final=True)

    with sqlite3.connect(database) as probe:
        assert _sidecar_rows(probe) == before_rows
        assert (
            tuple(
                tuple(row)
                for row in probe.execute(
                    """SELECT type,name,sql FROM sqlite_master
                     WHERE sql IS NOT NULL ORDER BY type,name"""
                )
            )
            == before_schema
        )
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("49",)


def test_schema49_source_unavailable_migrates_only_for_exact_first_oversized_source(
    settings,
    tmp_path: Path,
) -> None:
    valid_database = _unpack_schema_49(tmp_path, "schema49-valid-source-unavailable.sqlite3")
    valid_conversation = _seed_released_source_unavailable(
        valid_database,
        content="x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
        suffix="51",
    )
    migrated = FridayStorage(replace(settings, database_path=valid_database, database_must_exist=True))
    try:
        row = migrated.execute(
            """SELECT projection_status,incomplete_reason,passage_count
                 FROM conversation_passage_projections WHERE conversation_id=?""",
            (valid_conversation,),
        ).fetchone()
        assert row is not None and tuple(row) == ("incomplete", "source_unavailable", 0)
        validate_conversation_passage_schema(migrated.conn)
    finally:
        migrated.close(final=True)

    invalid_database = _unpack_schema_49(tmp_path, "schema49-false-source-unavailable.sqlite3")
    invalid_conversation = _seed_released_source_unavailable(
        invalid_database,
        content="ordinary valid first source",
        suffix="52",
    )
    rejected = FridayStorage(replace(settings, database_path=invalid_database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 49 conversation passage data"):
            rejected.execute("SELECT 1").fetchone()
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{invalid_database}?mode=ro", uri=True) as probe:
        assert probe.execute(
            "SELECT incomplete_reason FROM conversation_passage_projections WHERE conversation_id=?",
            (invalid_conversation,),
        ).fetchone() == ("source_unavailable",)
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("49",)


def test_schema49_migration_terminalizes_valid_retryable_first_oversize(
    settings,
    tmp_path: Path,
) -> None:
    database = _unpack_schema_49(tmp_path, "schema49-retryable-first-oversize.sqlite3")
    conversation_id = _seed_released_source_unavailable(
        database,
        content="x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
        suffix="55",
        terminalize=False,
    )

    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        row = migrated.execute(
            """SELECT projection_status,incomplete_reason,passage_count
                 FROM conversation_passage_projections WHERE conversation_id=?""",
            (conversation_id,),
        ).fetchone()
        assert row is not None and tuple(row) == ("incomplete", "source_unavailable", 0)
        validate_conversation_passage_schema(migrated.conn)
    finally:
        migrated.close(final=True)


@pytest.mark.parametrize("malformation", ("blob_content", "invalid_timestamp"))
def test_schema49_migration_rejects_malformed_oversized_source_unavailable(
    settings,
    tmp_path: Path,
    malformation: str,
) -> None:
    database = _unpack_schema_49(tmp_path, f"schema49-{malformation}-terminal.sqlite3")
    content: object = (
        sqlite3.Binary(b"x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1))
        if malformation == "blob_content"
        else "x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1)
    )
    created_at = (
        "2026-08-29T10:00:01+00:00" if malformation == "blob_content" else "not-a-canonical-timestamp"
    )
    _seed_released_source_unavailable(
        database,
        content=content,
        created_at=created_at,
        suffix="53" if malformation == "blob_content" else "54",
    )

    rejected = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="Schema 49 conversation passage data"):
            rejected.execute("SELECT 1").fetchone()
    finally:
        rejected.close(final=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("49",)


def test_source_unavailable_update_insert_and_full_validation_are_exact(
    storage: FridayStorage,
) -> None:
    ordinary_owner = "schema50-ordinary-unavailable-owner"
    ordinary = storage.create_conversation(ordinary_owner)
    storage.store_message(ordinary["id"], ordinary_owner, "user", "ordinary valid first source")
    with (
        storage.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError, match="conversation_passage_projection_invalid"),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_unavailable'
                WHERE conversation_id=?""",
            (ordinary["id"],),
        )

    parent = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (ordinary["id"],),
    ).fetchone()
    assert parent is not None
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_delete_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            "DELETE FROM conversation_passage_projections WHERE conversation_id=?",
            (ordinary["id"],),
        )
        conn.execute(
            """INSERT INTO conversation_passage_projections(
                   conversation_id,indexed_message_count,indexed_through_message_id,
                   indexed_conversation_revision_sha256,passage_set_sha256,
                   passage_index_revision,projection_status,incomplete_reason,
                   passage_count,projected_at
               ) VALUES(?,0,NULL,NULL,NULL,?,'incomplete','source_unavailable',0,?)""",
            (ordinary["id"], parent[5], parent[9]),
        )

    preceded_owner = "schema50-preceded-unavailable-owner"
    preceded = storage.create_conversation(preceded_owner)
    storage.store_message(preceded["id"], preceded_owner, "user", "small predecessor")
    storage.store_message(
        preceded["id"],
        preceded_owner,
        "user",
        "x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
    )
    with (
        storage.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError, match="conversation_passage_projection_invalid"),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_unavailable'
                WHERE conversation_id=?""",
            (preceded["id"],),
        )

    oversized_owner = "schema50-oversized-unavailable-owner"
    oversized = storage.create_conversation(oversized_owner)
    storage.store_message(
        oversized["id"],
        oversized_owner,
        "user",
        "x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
    )
    admitted = storage.execute(
        """SELECT projection_status,incomplete_reason,passage_count
             FROM conversation_passage_projections WHERE conversation_id=?""",
        (oversized["id"],),
    ).fetchone()
    assert admitted is not None and tuple(admitted) == ("incomplete", "source_unavailable", 0)
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_invalid",
        ),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_changed'
                WHERE conversation_id=?""",
            (oversized["id"],),
        )
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE messages SET created_at='2026-08-30T01:00:00+00:00'
                WHERE conversation_id=? AND role='user'""",
            (oversized["id"],),
        )
    preserved = storage.execute(
        """SELECT incomplete_reason FROM conversation_passage_projections
            WHERE conversation_id=?""",
        (oversized["id"],),
    ).fetchone()
    assert preserved is not None and tuple(preserved) == ("source_unavailable",)
    validate_conversation_passage_schema(storage.conn)

    oversized_parent = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (oversized["id"],),
    ).fetchone()
    assert oversized_parent is not None
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_delete_invalid",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            "DELETE FROM conversation_passage_projections WHERE conversation_id=?",
            (oversized["id"],),
        )
        conn.execute(
            """INSERT INTO conversation_passage_projections(
                   conversation_id,indexed_message_count,indexed_through_message_id,
                   indexed_conversation_revision_sha256,passage_set_sha256,
                   passage_index_revision,projection_status,incomplete_reason,
                   passage_count,projected_at
               ) VALUES(?,0,NULL,NULL,NULL,?,'incomplete','source_unavailable',0,?)""",
            (oversized["id"], oversized_parent[5], oversized_parent[9]),
        )

    with storage.transaction() as conn:
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='trigger'
                   AND name='conversation_passage_projection_bu_validate'"""
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        conn.execute("DROP TRIGGER conversation_passage_projection_bu_validate")
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET incomplete_reason='source_unavailable'
                WHERE conversation_id=?""",
            (ordinary["id"],),
        )
        conn.execute(str(trigger[0]))  # nosec B608 - exact authenticated SQLite DDL
    with pytest.raises(sqlite3.DatabaseError, match="Schema 50 conversation passage data"):
        validate_conversation_passage_schema(storage.conn)


@pytest.mark.parametrize("reset_kind", ("direct_projection", "later_created_at"))
def test_every_reset_terminalizes_a_historical_current_first_oversize(
    storage: FridayStorage,
    reset_kind: str,
) -> None:
    suffix = "1" if reset_kind == "direct_projection" else "2"
    owner = f"schema50-historical-oversize-reset-{suffix}-owner"
    first_message_id = f"msg_00000000000050f{suffix}"
    conversation = storage.create_conversation(owner)
    with storage.transaction() as conn:
        invalidation = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_message_ai_invalidate'"""
        ).fetchone()
        assert invalidation is not None and isinstance(invalidation[0], str)
        conn.execute("DROP TRIGGER conversation_passage_message_ai_invalidate")
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(
                   ?,?,?,'user',?,'{}',NULL,
                   '2026-08-30T02:00:00+00:00'
               )""",
            (
                first_message_id,
                conversation["id"],
                owner,
                "x" * (CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
            ),
        )
        conn.execute(str(invalidation[0]))  # nosec B608 - exact authenticated SQLite DDL

    child, _passage_set = _canonical_first_child(storage.conn, str(conversation["id"]))
    with storage.transaction() as conn:
        conn.execute(_CHILD_INSERT_SQL, child)
    later = storage.store_message(conversation["id"], owner, "assistant", "later small source")
    report = storage.backfill_conversation_passages(owner, limit=1)
    assert report["anchors_written"] == report["current"] == 1
    current = storage.execute(
        """SELECT projection_status,passage_count
             FROM conversation_passage_projections WHERE conversation_id=?""",
        (conversation["id"],),
    ).fetchone()
    assert current is not None and tuple(current) == ("current", 2)

    with storage.transaction() as conn:
        if reset_kind == "later_created_at":
            conn.execute(
                """UPDATE messages SET created_at='2026-08-30T03:00:00+00:00'
                    WHERE id=?""",
                (later["id"],),
            )
        else:
            conn.execute(
                """UPDATE conversation_passage_projections
                      SET indexed_message_count=0,indexed_through_message_id=NULL,
                          indexed_conversation_revision_sha256=NULL,
                          passage_set_sha256=NULL,projection_status='incomplete',
                          incomplete_reason='source_changed',passage_count=0
                    WHERE conversation_id=?""",
                (conversation["id"],),
            )

    parent = storage.execute(
        """SELECT projection_status,incomplete_reason,passage_count
             FROM conversation_passage_projections WHERE conversation_id=?""",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None and tuple(parent) == ("incomplete", "source_unavailable", 0)
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    validate_conversation_passage_schema(storage.conn)


def test_nonzero_prefix_cannot_be_terminalized_or_directly_delete_a_child(
    storage: FridayStorage,
) -> None:
    owner = "schema50-terminal-owner"
    conversation = storage.create_conversation(owner)
    for body in ("accepted prefix", "later invalid source"):
        storage.store_message(conversation["id"], owner, "user", body)
    report = storage.backfill_conversation_passages(owner, limit=1)
    assert report["anchors_written"] == 1
    before_parent = tuple(
        storage.execute(
            "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()
    )
    before_child = tuple(
        storage.execute(
            "SELECT * FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()
    )

    with storage.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """UPDATE conversation_passage_projections
                      SET incomplete_reason='source_unavailable'
                    WHERE conversation_id=?""",
                (conversation["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="conversation_passage_anchor_invalid"):
            conn.execute(
                "DELETE FROM conversation_passages WHERE conversation_id=?",
                (conversation["id"],),
            )

    assert (
        tuple(
            storage.execute(
                "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()
        )
        == before_parent
    )
    assert (
        tuple(
            storage.execute(
                "SELECT * FROM conversation_passages WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()
        )
        == before_child
    )


def test_schema49_predecessor_rejects_schema50_marker_without_mutation(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "schema50-rejected-by-49.sqlite3"
    current = FridayStorage(replace(settings, database_path=database))
    current.ensure_user("schema50-predecessor-sentinel")
    current.close(final=True)
    before = database.read_bytes()

    monkeypatch.setattr(storage_core, "SCHEMA_VERSION", 49)
    predecessor = FridayStorage(replace(settings, database_path=database, database_must_exist=False))
    try:
        with pytest.raises(UnsupportedSchemaVersionError, match="maximum 49"):
            predecessor.execute("SELECT 1").fetchone()
    finally:
        predecessor.close(final=True)

    assert database.read_bytes() == before
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as probe:
        assert probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() == ("50",)
        assert probe.execute("SELECT id FROM users WHERE id='schema50-predecessor-sentinel'").fetchone() == (
            "schema50-predecessor-sentinel",
        )
