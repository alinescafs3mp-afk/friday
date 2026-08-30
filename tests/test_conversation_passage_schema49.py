from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from friday.account_deletion import (
    _BLOCKING_CHAT_SCOPES,
    _mark_account_deletion_history_clean,
    preflight_account_deletion,
)
from friday.conversation_passages.schema import (
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
    CONVERSATION_PASSAGE_INDEX_REVISION,
    conversation_passage_anchor_locator_sha256,
    conversation_passage_content_sha256,
    conversation_passage_fts_schema_fingerprint,
    conversation_passage_message_revision_sha256,
    conversation_passage_prefix_sha256,
    conversation_passage_schema_fingerprint,
    conversation_passage_set_extend_sha256,
    conversation_passage_set_sha256,
    install_conversation_passage_schema,
    validate_conversation_passage_schema,
)
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import SCHEMA_VERSION, FridayStorage

SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
_PROJECTED_AT = "2026-08-29T12:00:00Z"


def _unpack_schema_48(tmp_path: Path, name: str) -> Path:
    database = tmp_path / name
    with gzip.open(SCHEMA_FIXTURES / "schema-48.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def _insert_source_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    created_at: str,
) -> None:
    conn.execute(
        """INSERT INTO messages(
               id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
           ) VALUES(?,?,?,?,?,'{}',NULL,?)""",
        (message_id, conversation_id, user_id, role, content, created_at),
    )


def _anchor_material(
    message: dict[str, object],
    *,
    anchor_ordinal: int,
    previous_prefix: str | None = None,
) -> tuple[str, ...]:
    message_id = str(message["id"])
    conversation_id = str(message["conversation_id"])
    principal_id = str(message["user_id"])
    role = str(message["role"])
    content = str(message["content"])
    created_at = str(message["created_at"])
    revision = conversation_passage_message_revision_sha256(
        message_id=message_id,
        conversation_id=conversation_id,
        principal_id=principal_id,
        role=role,
        content=content,
        created_at=created_at,
    )
    content_digest = conversation_passage_content_sha256(content)
    locator = conversation_passage_anchor_locator_sha256(
        conversation_id=conversation_id,
        anchor_message_id=message_id,
        anchor_ordinal=anchor_ordinal,
    )
    prefix = conversation_passage_prefix_sha256(previous_prefix, anchor_ordinal, revision)
    return revision, content_digest, locator, prefix


def _publish_single_anchor(
    storage: FridayStorage,
    *,
    conversation_id: str,
    message_id: str,
) -> dict[str, object]:
    source_row = storage.execute(
        "SELECT id,conversation_id,user_id,role,content,created_at FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    assert source_row is not None
    source = dict(source_row)
    revision, content_digest, locator, prefix = _anchor_material(source, anchor_ordinal=0)
    set_digest = conversation_passage_set_sha256(
        ((0, message_id, revision, content_digest, locator, prefix),)
    )

    with storage.transaction() as conn:
        passage_rowid = int(
            conn.execute("SELECT COALESCE(MAX(passage_rowid),0)+1 FROM conversation_passages").fetchone()[0]
        )
        conn.execute(
            """INSERT INTO conversation_passages(
                   passage_rowid,conversation_id,anchor_message_id,anchor_ordinal,
                   anchor_message_revision_sha256,anchor_content_sha256,
                   anchor_locator_sha256,conversation_prefix_sha256
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                passage_rowid,
                conversation_id,
                message_id,
                0,
                revision,
                content_digest,
                locator,
                prefix,
            ),
        )
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=1,
                      indexed_through_message_id=?,
                      indexed_conversation_revision_sha256=?,
                      passage_set_sha256=?,
                      projection_status='current',
                      incomplete_reason=NULL,
                      passage_count=1,
                      projected_at=?
                WHERE conversation_id=?""",
            (message_id, prefix, set_digest, _PROJECTED_AT, conversation_id),
        )
    return {
        "revision": revision,
        "content_digest": content_digest,
        "locator": locator,
        "prefix": prefix,
        "set_digest": set_digest,
    }


def _append_anchor(
    storage: FridayStorage,
    *,
    conversation_id: str,
    message_id: str,
) -> dict[str, object]:
    source_row = storage.execute(
        "SELECT id,conversation_id,user_id,role,content,created_at FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    parent_row = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert source_row is not None and parent_row is not None
    source = dict(source_row)
    parent = dict(parent_row)
    ordinal = int(parent["passage_count"])
    previous_prefix = str(parent["indexed_conversation_revision_sha256"])
    revision, content_digest, locator, prefix = _anchor_material(
        source,
        anchor_ordinal=ordinal,
        previous_prefix=previous_prefix,
    )
    set_digest = conversation_passage_set_extend_sha256(
        str(parent["passage_set_sha256"]),
        (ordinal, message_id, revision, content_digest, locator, prefix),
    )

    with storage.transaction() as conn:
        passage_rowid = int(
            conn.execute("SELECT COALESCE(MAX(passage_rowid),0)+1 FROM conversation_passages").fetchone()[0]
        )
        conn.execute(
            """INSERT INTO conversation_passages(
                   passage_rowid,conversation_id,anchor_message_id,anchor_ordinal,
                   anchor_message_revision_sha256,anchor_content_sha256,
                   anchor_locator_sha256,conversation_prefix_sha256
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                passage_rowid,
                conversation_id,
                message_id,
                ordinal,
                revision,
                content_digest,
                locator,
                prefix,
            ),
        )
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=?,
                      indexed_through_message_id=?,
                      indexed_conversation_revision_sha256=?,
                      passage_set_sha256=?,
                      projection_status='current',
                      incomplete_reason=NULL,
                      passage_count=?,
                      projected_at=?
                WHERE conversation_id=?""",
            (
                ordinal + 1,
                message_id,
                prefix,
                set_digest,
                ordinal + 1,
                _PROJECTED_AT,
                conversation_id,
            ),
        )
    return {"prefix": prefix, "set_digest": set_digest}


def _execute_without_projection_update_guard(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> None:
    trigger = conn.execute(
        """SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='conversation_passage_projection_bu_validate'"""
    ).fetchone()
    assert trigger is not None and isinstance(trigger["sql"], str)
    conn.execute("DROP TRIGGER conversation_passage_projection_bu_validate")
    try:
        conn.execute(sql, parameters)
    finally:
        conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL


def _delete_projection_without_delete_guard(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> None:
    """Forge one missing parent while restoring the exact canonical fence."""

    trigger = conn.execute(
        """SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='conversation_passage_projection_bd_validate'"""
    ).fetchone()
    assert trigger is not None and isinstance(trigger[0], str)
    canonical_sql = str(trigger[0])
    conn.execute("DROP TRIGGER conversation_passage_projection_bd_validate")
    try:
        conn.execute(
            "DELETE FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation_id,),
        )
    finally:
        conn.execute(canonical_sql)  # nosec B608 - exact SQLite-owned canonical DDL
    restored = conn.execute(
        """SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='conversation_passage_projection_bd_validate'"""
    ).fetchone()
    assert restored is not None and restored[0] == canonical_sql


def _rewrite_backup_manifest(database: Path, manifest_path: Path) -> None:
    blob = database.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["size_bytes"] = len(blob)
    manifest["sha256"] = hashlib.sha256(blob).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_fresh_schema49_is_exact_body_free_and_reader_first(storage: FridayStorage) -> None:
    assert SCHEMA_VERSION == 50
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "50"
    validate_conversation_passage_schema(storage.conn, require_fts=True)
    assert len(conversation_passage_schema_fingerprint(storage.conn)) == 64
    assert len(conversation_passage_fts_schema_fingerprint(storage.conn)) == 64

    projection_columns = {
        str(row[1]) for row in storage.execute("PRAGMA table_info(conversation_passage_projections)")
    }
    passage_columns = {str(row[1]) for row in storage.execute("PRAGMA table_info(conversation_passages)")}
    assert projection_columns == {
        "conversation_id",
        "indexed_message_count",
        "indexed_through_message_id",
        "indexed_conversation_revision_sha256",
        "passage_set_sha256",
        "passage_index_revision",
        "projection_status",
        "incomplete_reason",
        "passage_count",
        "projected_at",
    }
    assert passage_columns == {
        "passage_rowid",
        "conversation_id",
        "anchor_message_id",
        "anchor_ordinal",
        "anchor_message_revision_sha256",
        "anchor_content_sha256",
        "anchor_locator_sha256",
        "conversation_prefix_sha256",
    }
    forbidden = {
        "actor_id",
        "body",
        "content",
        "excerpt",
        "metadata_json",
        "path",
        "person_id",
        "reply_to",
        "tenant_id",
        "text",
        "user_id",
    }
    assert projection_columns.isdisjoint(forbidden)
    assert passage_columns.isdisjoint(forbidden)

    canary = "SCHEMA49-PRIVATE-MESSAGE-BODY"
    conversation = storage.create_conversation("schema49-owner", title="Reader first")
    storage.store_message(conversation["id"], "schema49-owner", "user", canary)
    projection_row = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    assert projection_row is not None
    projection = dict(projection_row)
    assert projection["projection_status"] == "incomplete"
    assert projection["incomplete_reason"] == "backfill_pending"
    assert projection["passage_index_revision"] == CONVERSATION_PASSAGE_INDEX_REVISION
    assert projection["indexed_message_count"] == projection["passage_count"] == 0
    assert projection["indexed_through_message_id"] is None
    assert projection["indexed_conversation_revision_sha256"] is None
    assert projection["passage_set_sha256"] is None
    assert canary not in repr(projection)
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 0
    )
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages_fts").fetchone()[0] == 0


def test_schema48_migration_seeds_historical_conversations_parent_only(
    settings,
    tmp_path: Path,
) -> None:
    database = _unpack_schema_48(tmp_path, "schema48-to-reader-first-49.sqlite3")
    canary = "HISTORICAL-SCHEMA48-CONVERSATION-BODY"
    historical_conversation_id = "conv_0000000000004948"
    historical_user_message_id = "msg_0000000000004941"
    historical_assistant_message_id = "msg_0000000000004942"
    with sqlite3.connect(database) as predecessor:
        assert predecessor.execute("SELECT 1 FROM users WHERE id='fixture-owner'").fetchone() is not None
        predecessor.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES(?,'fixture-owner','Historical',?,?)""",
            (
                historical_conversation_id,
                "2026-08-29T10:00:00+00:00",
                "2026-08-29T10:01:00+00:00",
            ),
        )
        _insert_source_message(
            predecessor,
            message_id=historical_user_message_id,
            conversation_id=historical_conversation_id,
            user_id="fixture-owner",
            role="user",
            content=canary,
            created_at="2026-08-29T10:00:01+00:00",
        )
        _insert_source_message(
            predecessor,
            message_id=historical_assistant_message_id,
            conversation_id=historical_conversation_id,
            user_id="fixture-owner",
            role="assistant",
            content="Historical assistant reply",
            created_at="2026-08-29T10:00:02+00:00",
        )

    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "50"
        )
        projection_row = migrated.execute(
            "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
            (historical_conversation_id,),
        ).fetchone()
        assert projection_row is not None
        projection = dict(projection_row)
        assert projection["projection_status"] == "incomplete"
        assert projection["incomplete_reason"] == "backfill_pending"
        assert projection["passage_count"] == projection["indexed_message_count"] == 0
        assert canary not in repr(projection)
        assert (
            migrated.execute(
                "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
                (historical_conversation_id,),
            ).fetchone()[0]
            == 0
        )
        assert migrated.execute("SELECT COUNT(*) FROM conversation_passages_fts").fetchone()[0] == 0
        validate_conversation_passage_schema(migrated.conn, require_fts=True)
        with (
            migrated.transaction() as conn,
            pytest.raises(
                sqlite3.IntegrityError,
                match="conversation_passage_message_rowid_immutable",
            ),
        ):
            conn.execute(
                "UPDATE messages SET rowid=rowid+1000000 WHERE id=?",
                (historical_user_message_id,),
            )
    finally:
        migrated.close(final=True)


def test_schema49_installer_resumes_missing_parent_without_identity_conflict(
    storage: FridayStorage,
) -> None:
    conversation = storage.create_conversation("schema49-resume-owner")
    with storage.transaction() as conn:
        _delete_projection_without_delete_guard(conn, str(conversation["id"]))
        install_conversation_passage_schema(conn)

    parent = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None
    assert parent["projection_status"] == "incomplete"
    assert parent["incomplete_reason"] == "backfill_pending"
    install_count = storage.execute(
        "SELECT COUNT(*) FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()[0]
    assert install_count == 1
    with storage.transaction() as conn:
        install_conversation_passage_schema(conn)


def test_schema49_seals_the_message_rowid_boundary_but_allows_metadata_updates(
    storage: FridayStorage,
) -> None:
    conversation = storage.create_conversation("schema49-rowid-owner")
    seed_message_id = "msg_00000000000049a1"
    backinsert_message_id = "msg_00000000000049a2"
    previous_max = int(storage.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0])
    high_rowid = previous_max + 100
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO messages(
                   rowid,id,conversation_id,user_id,role,content,
                   metadata_json,reply_to,created_at
               ) VALUES(?,?,?,'schema49-rowid-owner','user','high admitted row',
                        '{}',NULL,'2026-08-29T09:00:00+00:00')""",
            (high_rowid, seed_message_id, conversation["id"]),
        )
    boundary = storage.store_message(
        str(conversation["id"]),
        "schema49-rowid-owner",
        "user",
        "accepted boundary",
    )
    future = storage.store_message(
        str(conversation["id"]),
        "schema49-rowid-owner",
        "assistant",
        "future source",
    )
    future_rowid = int(
        storage.execute("SELECT rowid FROM messages WHERE id=?", (future["id"],)).fetchone()[0]
    )
    boundary_rowid = int(
        storage.execute("SELECT rowid FROM messages WHERE id=?", (boundary["id"],)).fetchone()[0]
    )
    future_content = str(future["content"])

    with storage.transaction() as conn:
        for message_id, row_identity in (
            (seed_message_id, "rowid"),
            (str(boundary["id"]), "_rowid_"),
            (str(future["id"]), "oid"),
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="conversation_passage_message_rowid_immutable",
            ):
                conn.execute(
                    f"UPDATE messages SET {row_identity}={row_identity}+1000000 WHERE id=?",  # nosec B608
                    (message_id,),
                )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_rowid_nonmonotonic",
        ):
            conn.execute(
                """INSERT INTO messages(
                       rowid,id,conversation_id,user_id,role,content,
                       metadata_json,reply_to,created_at
                   ) VALUES(?,?,?,'schema49-rowid-owner','assistant',
                            'forged post-boundary back-insert','{}',NULL,
                            '2026-08-29T12:00:00+00:00')""",
                (
                    previous_max + 50,
                    backinsert_message_id,
                    conversation["id"],
                ),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_identity_immutable",
        ):
            conn.execute(
                """INSERT OR REPLACE INTO messages(
                       id,conversation_id,user_id,role,content,
                       metadata_json,reply_to,created_at
                   ) VALUES(?,?,?,'assistant','replacement bypass','{}',NULL,
                            '2026-08-29T12:00:00+00:00')""",
                (future["id"], conversation["id"], "schema49-rowid-owner"),
            )
        for row_alias, collided_rowid, replacement_id in (
            ("rowid", future_rowid, "msg_fffffffffffffff0"),
            ("_rowid_", boundary_rowid, "msg_fffffffffffffff1"),
            ("oid", high_rowid, "msg_fffffffffffffff2"),
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="conversation_passage_message_identity_immutable",
            ):
                conn.execute(
                    f"""INSERT OR REPLACE INTO messages(
                           {row_alias},id,conversation_id,user_id,role,content,
                           metadata_json,reply_to,created_at
                       ) VALUES(?,?,?,?,'assistant','rowid replacement bypass','{{}}',NULL,
                                '2026-08-29T12:00:00+00:00')""",  # nosec B608 - closed aliases
                    (
                        collided_rowid,
                        replacement_id,
                        conversation["id"],
                        "schema49-rowid-owner",
                    ),
                )
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?",
            ('{"schema49_metadata_compatible":true}', future["id"]),
        )

    ordinary = storage.store_message(
        str(conversation["id"]),
        "schema49-rowid-owner",
        "assistant",
        "ordinary automatic admission remains compatible",
    )
    assert (
        storage.execute("SELECT metadata_json FROM messages WHERE id=?", (future["id"],)).fetchone()[0]
        == '{"schema49_metadata_compatible":true}'
    )
    assert int(
        storage.execute("SELECT rowid FROM messages WHERE id=?", (ordinary["id"],)).fetchone()[0]
    ) > int(storage.execute("SELECT rowid FROM messages WHERE id=?", (future["id"],)).fetchone()[0])
    persisted_future = storage.execute(
        "SELECT rowid,content FROM messages WHERE id=?",
        (future["id"],),
    ).fetchone()
    assert persisted_future is not None
    assert int(persisted_future["rowid"]) == future_rowid
    assert str(persisted_future["content"]) == future_content
    assert all(
        storage.execute("SELECT 1 FROM messages WHERE id=?", (replacement_id,)).fetchone() is None
        for replacement_id in (
            "msg_fffffffffffffff0",
            "msg_fffffffffffffff1",
            "msg_fffffffffffffff2",
        )
    )
    assert storage.execute("SELECT 1 FROM messages WHERE id=?", (backinsert_message_id,)).fetchone() is None
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_schema49_rejects_message_identity_updates_and_replace_conflicts(
    storage: FridayStorage,
) -> None:
    owner = "schema49-message-identity-owner"
    foreign = "schema49-message-identity-foreign"
    first_conversation = storage.create_conversation(owner)
    second_conversation = storage.create_conversation(owner)
    storage.ensure_user(foreign)
    first = storage.store_message(
        str(first_conversation["id"]),
        owner,
        "user",
        "first immutable identity",
    )
    second = storage.store_message(
        str(second_conversation["id"]),
        owner,
        "assistant",
        "second immutable identity",
    )

    with storage.transaction() as conn:
        for statement, parameters in (
            (
                "UPDATE messages SET id='msg_eeeeeeeeeeeeeeee' WHERE id=?",
                (first["id"],),
            ),
            (
                "UPDATE messages SET conversation_id=? WHERE id=?",
                (second_conversation["id"], first["id"]),
            ),
            (
                "UPDATE messages SET user_id=? WHERE id=?",
                (foreign, first["id"]),
            ),
            (
                "UPDATE OR REPLACE messages SET id=? WHERE id=?",
                (second["id"], first["id"]),
            ),
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="conversation_passage_message_identity_immutable",
            ):
                conn.execute(statement, parameters)

    persisted = storage.execute(
        "SELECT id,conversation_id,user_id,content FROM messages WHERE id IN (?,?) ORDER BY id",
        (first["id"], second["id"]),
    ).fetchall()
    assert len(persisted) == 2
    assert {str(row["id"]) for row in persisted} == {str(first["id"]), str(second["id"])}
    assert {str(row["user_id"]) for row in persisted} == {owner}
    assert {str(row["conversation_id"]) for row in persisted} == {
        str(first_conversation["id"]),
        str(second_conversation["id"]),
    }
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_authoritative_validation_rejects_a_forged_skipped_prefix(
    storage: FridayStorage,
) -> None:
    conversation = storage.create_conversation("schema49-skipped-prefix-owner")
    conversation_id = str(conversation["id"])
    with storage.transaction() as conn:
        _insert_source_message(
            conn,
            message_id="msg_0000000000004911",
            conversation_id=conversation_id,
            user_id="schema49-skipped-prefix-owner",
            role="user",
            content="unmapped first source",
            created_at="2026-08-29T10:00:00+00:00",
        )
        _insert_source_message(
            conn,
            message_id="msg_0000000000004912",
            conversation_id=conversation_id,
            user_id="schema49-skipped-prefix-owner",
            role="assistant",
            content="forged ordinal zero",
            created_at="2026-08-29T10:00:01+00:00",
        )
        source = dict(
            conn.execute(
                """SELECT id,conversation_id,user_id,role,content,created_at
                     FROM messages WHERE id='msg_0000000000004912'"""
            ).fetchone()
        )
        revision, content_digest, locator, prefix = _anchor_material(source, anchor_ordinal=0)
        set_digest = conversation_passage_set_sha256(
            ((0, str(source["id"]), revision, content_digest, locator, prefix),)
        )
        triggers = {
            str(row["name"]): str(row["sql"])
            for row in conn.execute(
                """SELECT name,sql FROM sqlite_master
                    WHERE type='trigger' AND name IN (
                        'conversation_passage_bi_validate',
                        'conversation_passage_ai_parent_cas'
                    ) ORDER BY name"""
            ).fetchall()
        }
        assert set(triggers) == {
            "conversation_passage_ai_parent_cas",
            "conversation_passage_bi_validate",
        }
        conn.execute("DROP TRIGGER conversation_passage_ai_parent_cas")
        conn.execute("DROP TRIGGER conversation_passage_bi_validate")
        try:
            conn.execute(
                """INSERT INTO conversation_passages(
                       passage_rowid,conversation_id,anchor_message_id,anchor_ordinal,
                       anchor_message_revision_sha256,anchor_content_sha256,
                       anchor_locator_sha256,conversation_prefix_sha256
                   ) VALUES(1,?,?,0,?,?,?,?)""",
                (
                    conversation_id,
                    source["id"],
                    revision,
                    content_digest,
                    locator,
                    prefix,
                ),
            )
        finally:
            for trigger_sql in triggers.values():
                conn.execute(trigger_sql)  # nosec B608 - exact SQLite-owned canonical DDL
        _execute_without_projection_update_guard(
            conn,
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=1,
                      indexed_through_message_id=?,
                      indexed_conversation_revision_sha256=?,
                      passage_set_sha256=?,
                      projection_status='incomplete',
                      incomplete_reason='source_changed',
                      passage_count=1,
                      projected_at=?
                WHERE conversation_id=?""",
            (source["id"], prefix, set_digest, _PROJECTED_AT, conversation_id),
        )

    with pytest.raises(sqlite3.DatabaseError, match="conversation passage data is invalid"):
        validate_conversation_passage_schema(storage.conn, require_fts=True)


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    (
        ("ddl", "passage DDL is incomplete or altered"),
        ("data", "passage data is invalid"),
    ),
)
def test_exact_schema_or_data_tamper_fails_closed(
    storage: FridayStorage,
    tamper: str,
    expected_error: str,
) -> None:
    conversation = storage.create_conversation("schema49-tamper-owner")
    with storage.transaction() as conn:
        if tamper == "ddl":
            conn.execute("DROP INDEX idx_conversation_passage_anchor_revision")
        else:
            _delete_projection_without_delete_guard(conn, str(conversation["id"]))

    with pytest.raises(sqlite3.DatabaseError, match=expected_error):
        validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_anchor_guard_rejects_cross_conversation_and_cross_owner_sources(
    storage: FridayStorage,
) -> None:
    owner = "schema49-anchor-owner"
    foreign = "schema49-foreign-owner"
    first = storage.create_conversation("schema49-anchor-owner")
    second = storage.create_conversation("schema49-anchor-owner")
    foreign_conversation = storage.create_conversation(foreign)
    other_conversation_message_id = "msg_00000000000049b1"
    foreign_owner_message_id = "msg_00000000000049b2"
    with storage.transaction() as conn:
        _insert_source_message(
            conn,
            message_id=other_conversation_message_id,
            conversation_id=str(second["id"]),
            user_id=owner,
            role="user",
            content="Other conversation",
            created_at="2026-08-29T11:00:00+00:00",
        )
        _insert_source_message(
            conn,
            message_id=foreign_owner_message_id,
            conversation_id=str(foreign_conversation["id"]),
            user_id=foreign,
            role="user",
            content="Foreign owner",
            created_at="2026-08-29T11:00:01+00:00",
        )

    for passage_rowid, message_id in (
        (9_001, other_conversation_message_id),
        (9_002, foreign_owner_message_id),
    ):
        with (
            storage.transaction() as conn,
            pytest.raises(sqlite3.IntegrityError, match="conversation_passage_anchor_invalid"),
        ):
            conn.execute(
                """INSERT INTO conversation_passages(
                       passage_rowid,conversation_id,anchor_message_id,anchor_ordinal,
                       anchor_message_revision_sha256,anchor_content_sha256,
                       anchor_locator_sha256,conversation_prefix_sha256
                   ) VALUES(?,?,?,0,?,?,?,?)""",
                (
                    passage_rowid,
                    first["id"],
                    message_id,
                    "0" * 64,
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                ),
            )
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages").fetchone()[0] == 0


def test_source_admission_rejects_noncanonical_identity_before_anchor_staging(
    storage: FridayStorage,
) -> None:
    owner = "schema49-anchor-identity-owner"
    conversation = storage.create_conversation(owner)
    conversation_id = str(conversation["id"])
    message_id = "private/path/secret.txt"
    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_message_identity_immutable",
        ),
    ):
        _insert_source_message(
            conn,
            message_id=message_id,
            conversation_id=conversation_id,
            user_id=owner,
            role="user",
            content="source body",
            created_at="2026-08-29T11:30:00+00:00",
        )

    assert storage.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone() is None
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages").fetchone()[0] == 0


def test_later_rowid_insert_preserves_prefix_when_backdated_but_source_update_resets(
    storage: FridayStorage,
) -> None:
    conversation = storage.create_conversation("schema49-prefix-owner")
    conversation_id = str(conversation["id"])
    with storage.transaction() as conn:
        _insert_source_message(
            conn,
            message_id="msg_0000000000004901",
            conversation_id=conversation_id,
            user_id="schema49-prefix-owner",
            role="user",
            content="Anchor body must remain outside the sidecar",
            created_at="2026-08-29T12:00:00+00:00",
        )
    material = _publish_single_anchor(
        storage,
        conversation_id=conversation_id,
        message_id="msg_0000000000004901",
    )
    validate_conversation_passage_schema(storage.conn, require_fts=True)

    with storage.transaction() as conn:
        _insert_source_message(
            conn,
            message_id="msg_0000000000004902",
            conversation_id=conversation_id,
            user_id="schema49-prefix-owner",
            role="assistant",
            content="Later accepted row with a backdated source timestamp",
            created_at="2026-08-29T11:59:59+00:00",
        )
    preserved_row = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert preserved_row is not None
    preserved = dict(preserved_row)
    assert preserved["projection_status"] == "incomplete"
    assert preserved["incomplete_reason"] == "source_changed"
    assert preserved["passage_count"] == preserved["indexed_message_count"] == 1
    assert preserved["indexed_through_message_id"] == "msg_0000000000004901"
    assert preserved["indexed_conversation_revision_sha256"] == material["prefix"]
    assert preserved["passage_set_sha256"] == material["set_digest"]
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        == 1
    )
    validate_conversation_passage_schema(storage.conn, require_fts=True)

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET created_at=? WHERE id=?",
            ("2026-08-29T12:00:02+00:00", "msg_0000000000004901"),
        )
    reset_row = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert reset_row is not None
    reset = dict(reset_row)
    assert reset["projection_status"] == "incomplete"
    assert reset["incomplete_reason"] == "source_changed"
    assert reset["passage_count"] == reset["indexed_message_count"] == 0
    assert reset["indexed_through_message_id"] is None
    assert reset["indexed_conversation_revision_sha256"] is None
    assert reset["passage_set_sha256"] is None
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        == 0
    )
    assert storage.execute("SELECT COUNT(*) FROM conversation_passages_fts").fetchone()[0] == 0
    validate_conversation_passage_schema(storage.conn, require_fts=True)


@pytest.mark.parametrize("tamper", ("arbitrary_path", "foreign_identity", "wrong_owned_tail"))
def test_projection_parent_guard_rejects_unbound_tail_identity(
    storage: FridayStorage,
    tamper: str,
) -> None:
    owner = "schema49-parent-tail-owner"
    foreign = "schema49-parent-tail-foreign"
    conversation = storage.create_conversation(owner)
    foreign_conversation = storage.create_conversation(foreign)
    first = storage.store_message(str(conversation["id"]), owner, "user", "first owned source")
    _publish_single_anchor(
        storage,
        conversation_id=str(conversation["id"]),
        message_id=str(first["id"]),
    )
    second = storage.store_message(
        str(conversation["id"]),
        owner,
        "assistant",
        "exact last owned source",
    )
    _append_anchor(
        storage,
        conversation_id=str(conversation["id"]),
        message_id=str(second["id"]),
    )
    foreign_message = storage.store_message(
        str(foreign_conversation["id"]),
        foreign,
        "user",
        "foreign source",
    )
    forged_tail = {
        "arbitrary_path": "private/path/secret.txt",
        "foreign_identity": str(foreign_message["id"]),
        "wrong_owned_tail": str(first["id"]),
    }[tamper]

    with (
        storage.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError, match="conversation_passage_projection_invalid"),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_through_message_id=?
                WHERE conversation_id=?""",
            (forged_tail, conversation["id"]),
        )

    persisted = storage.execute(
        "SELECT indexed_through_message_id FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    assert persisted is not None and persisted["indexed_through_message_id"] == second["id"]
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_projection_parent_guard_rejects_current_strict_prefix(
    storage: FridayStorage,
) -> None:
    owner = "schema49-current-prefix-owner"
    conversation = storage.create_conversation(owner)
    first = storage.store_message(str(conversation["id"]), owner, "user", "first source")
    _publish_single_anchor(
        storage,
        conversation_id=str(conversation["id"]),
        message_id=str(first["id"]),
    )
    storage.store_message(
        str(conversation["id"]),
        owner,
        "assistant",
        "unprojected second source",
    )

    with (
        storage.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError, match="conversation_passage_projection_invalid"),
    ):
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET projection_status='current',incomplete_reason=NULL
                WHERE conversation_id=?""",
            (conversation["id"],),
        )

    parent = storage.execute(
        "SELECT projection_status,incomplete_reason,passage_count FROM conversation_passage_projections "
        "WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None
    assert tuple(parent) == ("incomplete", "source_changed", 1)
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_projection_identity_fence_rejects_replace_without_cascading_children(
    storage: FridayStorage,
) -> None:
    owner = "schema49-projection-replace-owner"
    conversation = storage.create_conversation(owner)
    empty_conversation = storage.create_conversation(owner)
    message = storage.store_message(str(conversation["id"]), owner, "user", "owned source")
    _publish_single_anchor(
        storage,
        conversation_id=str(conversation["id"]),
        message_id=str(message["id"]),
    )
    parent = storage.execute(
        "SELECT rowid AS projection_rowid,* FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    empty_parent = storage.execute(
        "SELECT rowid AS projection_rowid FROM conversation_passage_projections WHERE conversation_id=?",
        (empty_conversation["id"],),
    ).fetchone()
    assert parent is not None and empty_parent is not None

    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_identity_immutable",
        ),
    ):
        conn.execute(
            """INSERT OR REPLACE INTO conversation_passage_projections(
                   conversation_id,indexed_message_count,indexed_through_message_id,
                   indexed_conversation_revision_sha256,passage_set_sha256,
                   passage_index_revision,projection_status,incomplete_reason,
                   passage_count,projected_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            tuple(parent)[1:],
        )

    projection_rowid = int(parent["projection_rowid"])
    with storage.transaction() as conn:
        for row_alias in ("rowid", "_rowid_", "oid"):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="conversation_passage_projection_identity_immutable",
            ):
                conn.execute(
                    f"""UPDATE OR REPLACE conversation_passage_projections
                            SET {row_alias}=?
                          WHERE conversation_id=?""",  # nosec B608 - closed aliases
                    (projection_rowid, empty_conversation["id"]),
                )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_projection_delete_invalid",
        ):
            conn.execute(
                "DELETE FROM conversation_passage_projections WHERE conversation_id=?",
                (empty_conversation["id"],),
            )
        history_guard = conn.execute(
            """SELECT sql FROM sqlite_master
                WHERE type='trigger' AND name='conversations_are_never_deleted'"""
        ).fetchone()
        assert history_guard is not None and isinstance(history_guard["sql"], str)
        history_guard_sql = str(history_guard["sql"])
        conn.execute("DROP TRIGGER conversations_are_never_deleted")
        try:
            conn.execute(
                "DELETE FROM conversations WHERE id=?",
                (empty_conversation["id"],),
            )
        finally:
            conn.execute(history_guard_sql)  # nosec B608 - exact SQLite-owned canonical DDL
        restored_history_guard = conn.execute(
            """SELECT sql FROM sqlite_master
                WHERE type='trigger' AND name='conversations_are_never_deleted'"""
        ).fetchone()
        assert restored_history_guard is not None
        assert restored_history_guard["sql"] == history_guard_sql

    assert (
        storage.execute(
            "SELECT 1 FROM conversation_passage_projections WHERE conversation_id=?",
            (empty_conversation["id"],),
        ).fetchone()
        is None
    )
    replacement_conversation = storage.create_conversation(owner)
    replacement_parent = storage.execute(
        "SELECT rowid AS projection_rowid FROM conversation_passage_projections WHERE conversation_id=?",
        (replacement_conversation["id"],),
    ).fetchone()
    assert replacement_parent is not None
    assert int(replacement_parent["projection_rowid"]) == int(empty_parent["projection_rowid"])

    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0]
        == 1
    )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passage_projections WHERE conversation_id=?",
            (replacement_conversation["id"],),
        ).fetchone()[0]
        == 1
    )
    validate_conversation_passage_schema(storage.conn, require_fts=True)


@pytest.mark.parametrize(
    "collision",
    ("passage_rowid", "anchor_message_id", "anchor_ordinal", "foreign_passage_rowid"),
)
def test_child_identity_fence_rejects_replace_collisions(
    storage: FridayStorage,
    collision: str,
) -> None:
    owner = "schema49-child-replace-owner"
    foreign = "schema49-child-replace-foreign"
    conversation = storage.create_conversation(owner)
    first = storage.store_message(str(conversation["id"]), owner, "user", "first source")
    _publish_single_anchor(
        storage,
        conversation_id=str(conversation["id"]),
        message_id=str(first["id"]),
    )
    second = storage.store_message(str(conversation["id"]), owner, "assistant", "second source")
    parent = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    source = storage.execute(
        "SELECT id,conversation_id,user_id,role,content,created_at FROM messages WHERE id=?",
        (second["id"],),
    ).fetchone()
    first_child = storage.execute(
        "SELECT * FROM conversation_passages WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchone()
    assert parent is not None and source is not None and first_child is not None
    revision, content_digest, locator, prefix = _anchor_material(
        dict(source),
        anchor_ordinal=1,
        previous_prefix=str(parent["indexed_conversation_revision_sha256"]),
    )
    passage_rowid = int(first_child["passage_rowid"])
    anchor_message_id = str(second["id"])
    anchor_ordinal = 1
    foreign_child_id: str | None = None
    if collision == "anchor_message_id":
        passage_rowid += 10_000
        anchor_message_id = str(first["id"])
    elif collision == "anchor_ordinal":
        passage_rowid += 10_000
        anchor_ordinal = 0
    elif collision == "foreign_passage_rowid":
        foreign_conversation = storage.create_conversation(foreign)
        foreign_message = storage.store_message(
            str(foreign_conversation["id"]),
            foreign,
            "user",
            "foreign source",
        )
        _publish_single_anchor(
            storage,
            conversation_id=str(foreign_conversation["id"]),
            message_id=str(foreign_message["id"]),
        )
        foreign_child = storage.execute(
            "SELECT passage_rowid,anchor_message_id FROM conversation_passages WHERE conversation_id=?",
            (foreign_conversation["id"],),
        ).fetchone()
        assert foreign_child is not None
        passage_rowid = int(foreign_child["passage_rowid"])
        foreign_child_id = str(foreign_child["anchor_message_id"])

    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="conversation_passage_anchor_identity_immutable",
        ),
    ):
        conn.execute(
            """INSERT OR REPLACE INTO conversation_passages(
                   passage_rowid,conversation_id,anchor_message_id,anchor_ordinal,
                   anchor_message_revision_sha256,anchor_content_sha256,
                   anchor_locator_sha256,conversation_prefix_sha256
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                passage_rowid,
                conversation["id"],
                anchor_message_id,
                anchor_ordinal,
                revision,
                content_digest,
                locator,
                prefix,
            ),
        )

    persisted_first = storage.execute(
        "SELECT anchor_message_id FROM conversation_passages WHERE conversation_id=? AND anchor_ordinal=0",
        (conversation["id"],),
    ).fetchone()
    assert persisted_first is not None and persisted_first["anchor_message_id"] == first["id"]
    if foreign_child_id is not None:
        assert (
            storage.execute(
                "SELECT 1 FROM conversation_passages WHERE anchor_message_id=?",
                (foreign_child_id,),
            ).fetchone()
            is not None
        )
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_account_deletion_classifies_owned_passage_cascade_without_foreign_blocker(
    storage: FridayStorage,
) -> None:
    target = "local:conversation-passage-delete-target"
    neighbour = "local:conversation-passage-delete-neighbour"
    target_conversation = storage.create_conversation(target, title="Target retained chat")
    neighbour_conversation = storage.create_conversation(neighbour, title="Neighbour retained chat")
    target_message = storage.store_message(
        str(target_conversation["id"]),
        target,
        "user",
        "target retained message body",
    )
    neighbour_message = storage.store_message(
        str(neighbour_conversation["id"]),
        neighbour,
        "user",
        "neighbour retained message body",
    )
    _publish_single_anchor(
        storage,
        conversation_id=str(target_conversation["id"]),
        message_id=str(target_message["id"]),
    )
    _publish_single_anchor(
        storage,
        conversation_id=str(neighbour_conversation["id"]),
        message_id=str(neighbour_message["id"]),
    )

    scopes = {scope.key: scope for scope in _BLOCKING_CHAT_SCOPES}
    expected_scope = "conversation_id IN (SELECT id FROM conversations WHERE user_id=?)"
    assert scopes["conversation_passages"].predicate == expected_scope
    assert scopes["conversation_passage_projections"].predicate == expected_scope
    assert _mark_account_deletion_history_clean(storage, target)
    storage.update_user(target, status="disabled")

    plan = preflight_account_deletion(storage, target, quiescence_available=True)

    assert plan["counts"]["conversation_passage_projections"] == 1
    assert plan["counts"]["conversation_passages"] == 1
    assert plan["counts"]["conversations"] == 1
    assert plan["counts"]["messages"] == 1
    assert plan["cross_account_object_references"]["foreign_keys"] == {}
    assert {item["code"] for item in plan["blockers"]} == {"chat_history"}
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (neighbour_conversation["id"],),
        ).fetchone()[0]
        == 1
    )


def test_owner_export_and_backup_restore_round_trip_body_free_passage_rows(
    storage: FridayStorage,
) -> None:
    owner = "conversation-passage-export-owner"
    foreign = "conversation-passage-export-foreign"
    owner_body = "OWNER-CONVERSATION-PASSAGE-PRIVATE-BODY"
    foreign_body = "FOREIGN-CONVERSATION-PASSAGE-PRIVATE-BODY"
    owner_conversation = storage.create_conversation(owner, title="Owner export")
    foreign_conversation = storage.create_conversation(foreign, title="Foreign export")
    owner_message = storage.store_message(
        str(owner_conversation["id"]),
        owner,
        "user",
        owner_body,
    )
    foreign_message = storage.store_message(
        str(foreign_conversation["id"]),
        foreign,
        "user",
        foreign_body,
    )
    _publish_single_anchor(
        storage,
        conversation_id=str(owner_conversation["id"]),
        message_id=str(owner_message["id"]),
    )
    _publish_single_anchor(
        storage,
        conversation_id=str(foreign_conversation["id"]),
        message_id=str(foreign_message["id"]),
    )
    expected_parent = dict(
        storage.execute(
            "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
            (owner_conversation["id"],),
        ).fetchone()
    )
    expected_stored_child = dict(
        storage.execute(
            "SELECT * FROM conversation_passages WHERE conversation_id=?",
            (owner_conversation["id"],),
        ).fetchone()
    )
    expected_export_child = {
        key: value for key, value in expected_stored_child.items() if key != "passage_rowid"
    }

    exported = storage.export_user(owner)
    payload = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))
    exported_parents = payload["conversation_passage_projections"]
    exported_children = payload["conversation_passages"]
    sidecar_json = json.dumps(
        {"parents": exported_parents, "children": exported_children},
        ensure_ascii=False,
        sort_keys=True,
    )

    assert exported_parents == [expected_parent]
    assert exported_children == [expected_export_child]
    assert all("passage_rowid" not in row for row in exported_children)
    assert str(foreign_conversation["id"]) not in sidecar_json
    assert str(foreign_message["id"]) not in sidecar_json
    assert owner_body not in sidecar_json
    assert foreign_body not in sidecar_json
    assert all(
        forbidden not in row
        for row in (*exported_parents, *exported_children)
        for forbidden in ("body", "content", "text", "path", "metadata_json", "user_id")
    )

    backup = storage.create_backup(label="conversation-passage-export-round-trip")
    after_backup = storage.create_conversation(owner, title="Must disappear after restore")
    assert (
        storage.execute(
            "SELECT 1 FROM conversation_passage_projections WHERE conversation_id=?",
            (after_backup["id"],),
        ).fetchone()
        is not None
    )
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        restored = storage.restore_backup(
            str(backup["database"]),
            safety_label="conversation-passage-export-round-trip-safety",
        )

    assert restored["ok"] is True
    assert (
        storage.execute(
            "SELECT 1 FROM conversation_passage_projections WHERE conversation_id=?",
            (after_backup["id"],),
        ).fetchone()
        is None
    )
    restored_parent = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (owner_conversation["id"],),
    ).fetchone()
    restored_child = storage.execute(
        "SELECT * FROM conversation_passages WHERE conversation_id=?",
        (owner_conversation["id"],),
    ).fetchone()
    assert restored_parent is not None and dict(restored_parent) == expected_parent
    assert restored_child is not None and dict(restored_child) == expected_stored_child
    assert owner_body not in repr(dict(restored_parent))
    assert owner_body not in repr(dict(restored_child))
    validate_conversation_passage_schema(storage.conn, require_fts=True)


def test_owner_export_rejects_a_foreign_parent_tail_without_partial_file(
    storage: FridayStorage,
) -> None:
    owner = "conversation-passage-export-tamper-owner"
    foreign = "conversation-passage-export-tamper-foreign"
    owner_conversation = storage.create_conversation(owner)
    foreign_conversation = storage.create_conversation(foreign)
    owner_message = storage.store_message(
        str(owner_conversation["id"]),
        owner,
        "user",
        "owner source body",
    )
    foreign_message = storage.store_message(
        str(foreign_conversation["id"]),
        foreign,
        "user",
        "foreign identity must not enter owner export",
    )
    _publish_single_anchor(
        storage,
        conversation_id=str(owner_conversation["id"]),
        message_id=str(owner_message["id"]),
    )
    with storage.transaction() as conn:
        _execute_without_projection_update_guard(
            conn,
            """UPDATE conversation_passage_projections
                  SET indexed_through_message_id=?
                WHERE conversation_id=?""",
            (foreign_message["id"], owner_conversation["id"]),
        )

    exports_dir = Path(storage.settings.exports_dir)
    before = set(exports_dir.glob("jericho-export-*.json")) if exports_dir.exists() else set()
    with pytest.raises(sqlite3.DatabaseError, match="conversation passage data is invalid"):
        storage.export_user(owner)
    after = set(exports_dir.glob("jericho-export-*.json")) if exports_dir.exists() else set()

    assert after == before
    assert all(str(foreign_message["id"]) not in path.read_text(encoding="utf-8") for path in after)


def test_canonical_conversation_digest_chain_is_deterministic_and_drift_sensitive() -> None:
    source = {
        "message_id": "msg_schema49_digest",
        "conversation_id": "conv_schema49_digest",
        "principal_id": "schema49-digest-owner",
        "role": "user",
        "content": "Canonical текст",
        "created_at": "2026-08-29T13:00:00+00:00",
    }
    first = conversation_passage_message_revision_sha256(**source)
    second = conversation_passage_message_revision_sha256(**dict(reversed(tuple(source.items()))))
    assert first == second
    assert len(first) == 64
    assert first != conversation_passage_message_revision_sha256(
        **{**source, "conversation_id": "conv_schema49_digest_drift"}
    )

    content_digest = conversation_passage_content_sha256(str(source["content"]))
    locator = conversation_passage_anchor_locator_sha256(
        conversation_id=str(source["conversation_id"]),
        anchor_message_id=str(source["message_id"]),
        anchor_ordinal=0,
    )
    prefix = conversation_passage_prefix_sha256(None, 0, first)
    row = (0, str(source["message_id"]), first, content_digest, locator, prefix)
    expected_set = conversation_passage_set_sha256((row,))
    assert expected_set == conversation_passage_set_sha256((row,))
    assert expected_set == conversation_passage_set_extend_sha256(
        CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
        row,
    )
    assert len({first, content_digest, locator, prefix, expected_set}) == 5


def test_backup_and_restore_preflight_rejects_conversation_projection_tamper(
    storage: FridayStorage,
) -> None:
    conversation = storage.create_conversation("schema49-backup-owner")
    backup = storage.create_backup(label="conversation-passage-tamper")
    database = Path(str(backup["path"]))
    manifest_path = Path(str(backup["manifest_path"]))
    assert storage.verify_backup(database.name)["ok"] is True

    with sqlite3.connect(database) as forged:
        _delete_projection_without_delete_guard(forged, str(conversation["id"]))
        forged.commit()
    _rewrite_backup_manifest(database, manifest_path)

    verification = storage.verify_backup(database.name)
    assert verification["ok"] is False
    assert verification["hash_matches_manifest"] is True
    assert "conversation passage data is invalid" in str(verification["database_error"])

    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="Refusing to restore unverified backup"),
    ):
        storage.restore_backup(database.name)
    assert (
        storage.execute(
            "SELECT 1 FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()
        is not None
    )
