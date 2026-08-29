"""Schema-47 reader-first document-passage storage invariants."""

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
    _DELETE_SCOPES,
    _mark_account_deletion_history_clean,
    _unknown_user_scopes,
    preflight_account_deletion,
)
from friday.diagnostics.runtime_lease import ProcessLease
from friday.document_catalog.passage_projection import DocumentPassageProjection
from friday.document_catalog.passage_schema import (
    DOCUMENT_PASSAGE_INDEX_REVISION,
    _canonical_document_passage_schema_objects,
    document_passage_schema_fingerprint,
    document_passage_set_sha256,
    validate_document_passage_schema,
)
from friday.document_catalog.schema import validate_document_catalog_schema
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


def _receipt(body: str) -> dict[str, object]:
    normalized = " ".join(body.split())
    return {
        "extraction_receipt_version": 1,
        "extraction_success": True,
        "extraction_error": "",
        "text_extraction_success": bool(body.strip()),
        "text_sha256": hashlib.sha256(normalized.encode()).hexdigest() if normalized else "",
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
    }


def _file(
    storage: FridayStorage,
    index: int,
    *,
    body: str,
    owner: str = "passage-owner",
) -> RawObject:
    storage.ensure_user(owner)
    raw = RawObject(
        id=f"raw_passage_{index:08x}",
        user_id=owner,
        source="upload",
        source_ref=f"passage-schema-test:{index}",
        raw_content=body,
        content_type="file",
        metadata_json=_receipt(body),
        content_hash=hashlib.sha256(f"passage-source-{index}".encode()).hexdigest(),
        version=1,
    )
    return storage.store_raw_object(raw)


def _projection(raw: RawObject) -> DocumentPassageProjection:
    return DocumentPassageProjection.from_complete_text(
        raw_object_id=raw.id,
        source_version=raw.version,
        source_content_sha256=raw.content_hash,
        extracted_text=raw.raw_content,
    )


def _publish_current(storage: FridayStorage, raw: RawObject) -> DocumentPassageProjection:
    projection = _projection(raw)
    passage_set_sha256 = document_passage_set_sha256(
        tuple(
            (item.chunk_index, item.start_char, item.end_char, item.content_sha256)
            for item in projection.passages
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE document_passage_projections
                  SET extracted_text_sha256=?,source_char_count=?,
                      passage_set_sha256=?,
                      projection_status='current',incomplete_reason=NULL,
                      passage_count=?,projected_at='2026-08-29T12:00:00Z'
                WHERE raw_object_id=?""",
            (
                projection.extracted_text_sha256,
                projection.source_char_count,
                passage_set_sha256,
                len(projection.passages),
                raw.id,
            ),
        )
        conn.executemany(
            """INSERT INTO document_passages(
                   raw_object_id,chunk_index,start_char,end_char,content_sha256
               ) VALUES(?,?,?,?,?)""",
            (
                (
                    raw.id,
                    passage.chunk_index,
                    passage.start_char,
                    passage.end_char,
                    passage.content_sha256,
                )
                for passage in projection.passages
            ),
        )
    return projection


def _unpack_schema_46(tmp_path: Path, name: str) -> Path:
    database = tmp_path / name
    with gzip.open(SCHEMA_FIXTURES / "schema-46.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def test_schema_47_is_exact_body_free_raw_bound_and_fingerprinted(
    storage: FridayStorage,
) -> None:
    assert SCHEMA_VERSION == 47
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "47"
    observed = {
        (str(row[0]), str(row[1])): "".join(str(row[2]).split())
        for row in storage.execute(
            """SELECT type,name,sql FROM sqlite_master
                 WHERE sql IS NOT NULL
                   AND (name IN ('document_passage_projections','document_passages')
                        OR name LIKE 'document_passage_%'
                        OR name LIKE 'idx_document_passage_%')"""
        )
    }
    assert observed == _canonical_document_passage_schema_objects()
    assert len(document_passage_schema_fingerprint(storage.conn)) == 64
    validate_document_passage_schema(storage.conn)
    # Schema 47 must not expand schema 41's broad SQL-object fingerprint.
    validate_document_catalog_schema(storage.conn)

    projection_columns = {
        str(row[1]) for row in storage.execute("PRAGMA table_info(document_passage_projections)")
    }
    assert projection_columns == {
        "raw_object_id",
        "source_version",
        "source_content_sha256",
        "extracted_text_sha256",
        "source_char_count",
        "passage_set_sha256",
        "passage_index_revision",
        "projection_status",
        "incomplete_reason",
        "passage_count",
        "projected_at",
    }
    passage_columns = {str(row[1]) for row in storage.execute("PRAGMA table_info(document_passages)")}
    assert passage_columns == {
        "raw_object_id",
        "chunk_index",
        "start_char",
        "end_char",
        "content_sha256",
    }
    forbidden = {
        "user_id",
        "tenant_id",
        "owner_id",
        "body",
        "text",
        "excerpt",
        "filename",
        "path",
        "metadata_json",
        "model",
        "embedding",
    }
    assert projection_columns.isdisjoint(forbidden)
    assert passage_columns.isdisjoint(forbidden)

    projection_fks = {
        (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
        for row in storage.execute("PRAGMA foreign_key_list(document_passage_projections)")
    }
    passage_fks = {
        (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
        for row in storage.execute("PRAGMA foreign_key_list(document_passages)")
    }
    assert projection_fks == {("raw_object_id", "raw_objects", "id", "CASCADE")}
    assert passage_fks == {("raw_object_id", "document_passage_projections", "raw_object_id", "CASCADE")}


def test_live_file_seed_and_raw_lifecycle_reset_are_explicit_and_body_free(
    storage: FridayStorage,
) -> None:
    body = "# Passage source\n" + ("alpha beta gamma. " * 140)
    raw = _file(storage, 1, body=body)
    seeded = storage.execute(
        "SELECT * FROM document_passage_projections WHERE raw_object_id=?", (raw.id,)
    ).fetchone()
    assert seeded is not None
    assert seeded["source_version"] == 1
    assert seeded["source_content_sha256"] == raw.content_hash
    assert seeded["passage_index_revision"] == DOCUMENT_PASSAGE_INDEX_REVISION
    assert seeded["projection_status"] == "incomplete"
    assert seeded["incomplete_reason"] == "backfill_pending"
    assert seeded["passage_count"] == 0
    assert body not in repr(dict(seeded))

    projection = _publish_current(storage, raw)
    assert len(projection.passages) > 1
    assert storage.execute(
        "SELECT COUNT(*) FROM document_passages WHERE raw_object_id=?", (raw.id,)
    ).fetchone()[0] == len(projection.passages)

    replacement = "# Replacement\n" + ("delta epsilon. " * 120)
    replacement_hash = hashlib.sha256(b"replacement-source").hexdigest()
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE raw_objects
                  SET raw_content=?,metadata_json=?,content_hash=?,version=2
                WHERE id=?""",
            (replacement, json.dumps(_receipt(replacement)), replacement_hash, raw.id),
        )
    reset = storage.execute(
        "SELECT * FROM document_passage_projections WHERE raw_object_id=?", (raw.id,)
    ).fetchone()
    assert reset is not None
    assert reset["source_version"] == 2
    assert reset["source_content_sha256"] == replacement_hash
    assert reset["projection_status"] == "incomplete"
    assert reset["incomplete_reason"] == "source_changed"
    assert reset["passage_count"] == 0
    assert (
        storage.execute("SELECT COUNT(*) FROM document_passages WHERE raw_object_id=?", (raw.id,)).fetchone()[
            0
        ]
        == 0
    )

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-29T12:01:00Z' WHERE id=?",
            (raw.id,),
        )
    assert (
        storage.execute(
            "SELECT 1 FROM document_passage_projections WHERE raw_object_id=?", (raw.id,)
        ).fetchone()
        is None
    )

    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET deleted_at=NULL WHERE id=?", (raw.id,))
    assert (
        storage.execute(
            "SELECT incomplete_reason FROM document_passage_projections WHERE raw_object_id=?",
            (raw.id,),
        ).fetchone()[0]
        == "source_changed"
    )

    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET content_type='text/plain' WHERE id=?", (raw.id,))
    assert (
        storage.execute(
            "SELECT 1 FROM document_passage_projections WHERE raw_object_id=?", (raw.id,)
        ).fetchone()
        is None
    )
    validate_document_passage_schema(storage.conn)


def test_current_spans_are_exact_and_data_tamper_fails_closed(storage: FridayStorage) -> None:
    body = "# Exact spans\n" + ("one two three four five. " * 180)
    raw = _file(storage, 2, body=body)
    projection = _publish_current(storage, raw)
    validate_document_passage_schema(storage.conn)

    observed = storage.execute(
        """SELECT chunk_index,start_char,end_char,content_sha256
             FROM document_passages WHERE raw_object_id=? ORDER BY chunk_index""",
        (raw.id,),
    ).fetchall()
    assert [tuple(row) for row in observed] == [
        (item.chunk_index, item.start_char, item.end_char, item.content_sha256)
        for item in projection.passages
    ]

    with pytest.raises(sqlite3.DatabaseError, match="document_passage_span_invalid"):
        storage.execute(
            """UPDATE document_passages SET start_char=start_char+1
                 WHERE raw_object_id=? AND chunk_index=0""",
            (raw.id,),
        )
    storage.conn.rollback()

    removed = projection.passages[-1]
    with storage.transaction() as conn:
        conn.execute(
            "DELETE FROM document_passages WHERE raw_object_id=? AND chunk_index=?",
            (raw.id, removed.chunk_index),
        )
    with pytest.raises(sqlite3.DatabaseError, match="passage data is invalid"):
        validate_document_passage_schema(storage.conn)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO document_passages(
                   raw_object_id,chunk_index,start_char,end_char,content_sha256
               ) VALUES(?,?,?,?,?)""",
            (
                raw.id,
                removed.chunk_index,
                removed.start_char,
                removed.end_char,
                removed.content_sha256,
            ),
        )
    validate_document_passage_schema(storage.conn)

    with pytest.raises(sqlite3.DatabaseError, match="document_passage_projection_invalid"):
        storage.execute(
            """UPDATE document_passage_projections SET passage_count=passage_count-1
                 WHERE raw_object_id=?""",
            (raw.id,),
        )
    storage.conn.rollback()


def test_exact_46_migration_survives_and_reopen_is_idempotent(settings, tmp_path: Path) -> None:
    database = _unpack_schema_46(tmp_path, "schema46-to-47.sqlite3")
    first = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert first.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "47"
        live_files = first.execute(
            """SELECT COUNT(*) FROM raw_objects
                 WHERE content_type='file' AND deleted_at IS NULL"""
        ).fetchone()[0]
        assert first.execute("SELECT COUNT(*) FROM document_passage_projections").fetchone()[0] == live_files
        assert first.execute("SELECT COUNT(*) FROM document_passages").fetchone()[0] == 0
        assert (
            first.execute(
                """SELECT COUNT(*) FROM document_passage_projections
                 WHERE projection_status<>'incomplete'
                    OR incomplete_reason IS NULL
                    OR extracted_text_sha256 IS NOT NULL
                    OR source_char_count IS NOT NULL
                    OR passage_set_sha256 IS NOT NULL
                    OR passage_count<>0"""
            ).fetchone()[0]
            == 0
        )
        fixture = first.execute(
            """SELECT projection_status,incomplete_reason,passage_count,projected_at
                 FROM document_passage_projections WHERE raw_object_id='raw-fixture-0'"""
        ).fetchone()
        assert fixture is not None
        assert tuple(fixture[:3]) == ("incomplete", "backfill_pending", 0)
        projected_at = str(fixture[3])
        assert first.kv_get("fixture:marker") == "schema-46"
        fingerprint = document_passage_schema_fingerprint(first.conn)
    finally:
        first.close(final=True)

    second = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert document_passage_schema_fingerprint(second.conn) == fingerprint
        assert (
            second.execute(
                """SELECT projected_at FROM document_passage_projections
                 WHERE raw_object_id='raw-fixture-0'"""
            ).fetchone()[0]
            == projected_at
        )
        assert second.execute("SELECT COUNT(*) FROM document_passages").fetchone()[0] == 0
        validate_document_passage_schema(second.conn)
    finally:
        second.close(final=True)


def test_partial_interrupted_schema_47_fails_without_marker_publication(
    settings,
    tmp_path: Path,
) -> None:
    database = _unpack_schema_46(tmp_path, "schema46-partial-passage.sqlite3")
    with sqlite3.connect(database) as interrupted:
        interrupted.execute("CREATE TABLE document_passage_projections(raw_object_id TEXT PRIMARY KEY)")
        interrupted.commit()

    broken = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="passage DDL is incomplete or altered"):
            broken.execute("SELECT 1").fetchone()
    finally:
        broken.close(final=True)
    with sqlite3.connect(database) as unchanged:
        assert (
            unchanged.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "46"
        )
        assert {
            str(row[1]) for row in unchanged.execute("PRAGMA table_info(document_passage_projections)")
        } == {"raw_object_id"}


def test_exact_ddl_tamper_is_rejected(storage: FridayStorage) -> None:
    storage.execute("DROP INDEX idx_document_passage_content")
    with pytest.raises(sqlite3.DatabaseError, match="passage DDL is incomplete or altered"):
        validate_document_passage_schema(storage.conn)


@pytest.mark.parametrize(
    "extra_ddl",
    (
        """CREATE INDEX rogue_projection_lookup
               ON document_passage_projections(raw_object_id)""",
        """CREATE TRIGGER rogue_child_trigger
               BEFORE INSERT ON document_passages
               BEGIN SELECT 1; END""",
    ),
)
def test_arbitrary_named_objects_on_passage_tables_fail_exact_ddl_validation(
    storage: FridayStorage,
    extra_ddl: str,
) -> None:
    storage.execute(extra_ddl)
    with pytest.raises(sqlite3.DatabaseError, match="passage DDL is incomplete or altered"):
        validate_document_passage_schema(storage.conn)


def test_offline_orphan_child_fails_data_validation(storage: FridayStorage) -> None:
    database = storage._db_path
    storage.conn.commit()
    with sqlite3.connect(database) as offline:
        offline.execute("PRAGMA foreign_keys=OFF")
        trigger_row = offline.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='trigger' AND name='document_passage_bi_validate'"""
        ).fetchone()
        assert trigger_row is not None and isinstance(trigger_row[0], str)
        trigger_sql = trigger_row[0]
        offline.execute("DROP TRIGGER document_passage_bi_validate")
        offline.execute(
            """INSERT INTO document_passages(
                   raw_object_id,chunk_index,start_char,end_char,content_sha256
               ) VALUES('raw_offline_orphan',0,0,1,?)""",
            ("a" * 64,),
        )
        offline.execute(trigger_sql)
        offline.commit()

        # The attacker restored byte-exact DDL, so only the data validator can
        # reject this child whose parent was bypassed with FK enforcement off.
        assert len(document_passage_schema_fingerprint(offline)) == 64
        with pytest.raises(sqlite3.DatabaseError, match="passage data is invalid"):
            validate_document_passage_schema(offline)


def test_account_deletion_counts_only_target_passage_rows_and_keeps_neighbour(
    storage: FridayStorage,
) -> None:
    target = "local:passage-delete-target"
    neighbour = "local:passage-delete-neighbour"
    body = "# Account lifecycle\n" + ("passage account boundary. " * 160)
    target_raw = _file(storage, 401, body=body, owner=target)
    neighbour_raw = _file(storage, 402, body=body, owner=neighbour)
    target_projection = _publish_current(storage, target_raw)
    neighbour_projection = _publish_current(storage, neighbour_raw)

    scopes = {scope.key: scope for scope in _DELETE_SCOPES}
    expected_scope = "raw_object_id IN (SELECT id FROM raw_objects WHERE user_id=?)"
    assert scopes["document_passages"].predicate == expected_scope
    assert scopes["document_passage_projections"].predicate == expected_scope
    assert _unknown_user_scopes(storage.conn) == []
    assert _mark_account_deletion_history_clean(storage, target)
    storage.update_user(target, status="disabled")

    plan = preflight_account_deletion(storage, target, quiescence_available=True)

    assert plan["counts"]["document_passage_projections"] == 1
    assert plan["counts"]["document_passages"] == len(target_projection.passages)
    assert plan["counts"]["raw_objects"] == 1
    assert plan["cross_account_object_references"]["foreign_keys"] == {}
    assert {item["code"] for item in plan["blockers"]} == {"stored_files"}
    assert storage.execute(
        "SELECT COUNT(*) FROM document_passages WHERE raw_object_id=?",
        (target_raw.id,),
    ).fetchone()[0] == len(target_projection.passages)
    assert storage.execute(
        "SELECT COUNT(*) FROM document_passages WHERE raw_object_id=?",
        (neighbour_raw.id,),
    ).fetchone()[0] == len(neighbour_projection.passages)


def test_shared_raw_ko_purge_retains_then_exactly_counts_passage_cascade(
    storage: FridayStorage,
) -> None:
    owner = "passage-purge-owner"
    raw = _file(
        storage,
        410,
        body="# Shared Raw\n" + ("shared passage projection. " * 170),
        owner=owner,
    )
    projection = _publish_current(storage, raw)
    first = KnowledgeObject(
        id=new_id("ko"),
        user_id=owner,
        raw_object_id=raw.id,
        title="First shared projection owner",
        content=raw.raw_content,
        summary="",
    )
    second = KnowledgeObject(
        id=new_id("ko"),
        user_id=owner,
        raw_object_id=raw.id,
        title="Second shared projection owner",
        content=raw.raw_content,
        summary="",
    )
    storage.store_knowledge_object(first)
    storage.store_knowledge_object(second)

    assert storage.soft_delete_knowledge_object(first.id, owner)
    retained = storage.purge_knowledge_object(first.id, owner)
    assert retained["raw_removed"] is False
    assert "document_passages" not in retained["deleted"]
    assert "document_passage_projections" not in retained["deleted"]
    assert storage.execute(
        "SELECT COUNT(*) FROM document_passages WHERE raw_object_id=?",
        (raw.id,),
    ).fetchone()[0] == len(projection.passages)

    assert storage.soft_delete_knowledge_object(second.id, owner)
    removed = storage.purge_knowledge_object(second.id, owner)
    assert removed["raw_removed"] is True
    assert removed["deleted"]["document_passages"] == len(projection.passages)
    assert removed["deleted"]["document_passage_projections"] == 1
    assert removed["deleted"]["raw_objects"] == 1
    assert (
        storage.execute(
            "SELECT 1 FROM document_passage_projections WHERE raw_object_id=?",
            (raw.id,),
        ).fetchone()
        is None
    )
    assert (
        storage.execute(
            "SELECT 1 FROM document_passages WHERE raw_object_id=?",
            (raw.id,),
        ).fetchone()
        is None
    )


def _rewrite_backup_manifest(database: Path, manifest_path: Path) -> None:
    blob = database.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["size_bytes"] = len(blob)
    manifest["sha256"] = hashlib.sha256(blob).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    (
        ("schema", "passage DDL is incomplete or altered"),
        ("data", "passage data is invalid"),
    ),
)
def test_backup_and_restore_reject_passage_schema_or_data_tamper_without_live_mutation(
    storage: FridayStorage,
    tamper: str,
    expected_error: str,
) -> None:
    raw = _file(
        storage,
        420,
        body="# Backup passage\n" + ("authenticated child row. " * 160),
    )
    projection = _publish_current(storage, raw)
    backup = storage.create_backup(label=f"passage-{tamper}-tamper")
    database = Path(str(backup["path"]))
    manifest_path = Path(str(backup["manifest_path"]))
    assert storage.verify_backup(database.name)["ok"] is True

    with sqlite3.connect(database) as forged:
        if tamper == "schema":
            forged.execute("DROP INDEX idx_document_passage_content")
        else:
            forged.execute(
                "DELETE FROM document_passages WHERE raw_object_id=? AND chunk_index=?",
                (raw.id, projection.passages[-1].chunk_index),
            )
        forged.commit()
    _rewrite_backup_manifest(database, manifest_path)

    verification = storage.verify_backup(database.name)
    assert verification["ok"] is False
    assert verification["hash_matches_manifest"] is True
    assert expected_error in str(verification["database_error"])

    live_marker = f"passage-live-after-{tamper}-tamper"
    storage.ensure_user(live_marker)
    live_fingerprint = document_passage_schema_fingerprint(storage.conn)
    live_child_count = storage.execute("SELECT COUNT(*) FROM document_passages").fetchone()[0]
    with (
        ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(RuntimeError, match="Refusing to restore unverified backup"),
    ):
        storage.restore_backup(database.name)

    assert storage.get_user(live_marker) is not None
    assert document_passage_schema_fingerprint(storage.conn) == live_fingerprint
    assert storage.execute("SELECT COUNT(*) FROM document_passages").fetchone()[0] == live_child_count


def _mark_foreign_private_reminder(
    storage: FridayStorage,
    *,
    entity_id: str,
    tenant_id: str,
    person_id: str,
) -> None:
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id,user_id,occurred_at,precision,source,updated_at)
               VALUES(?,?,'2026-08-29T09:00:00Z','day',?,'2026-08-29T09:00:00Z')""",
            (entity_id, tenant_id, f"reminder:{person_id}"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id,person_id,privacy_kind,created_at)
               VALUES(?,?,'reminder','2026-08-29T09:00:00Z')""",
            (entity_id, person_id),
        )


def test_export_privacy_fixed_point_removes_hidden_raw_passage_derivatives(
    storage: FridayStorage,
) -> None:
    owner = LEGACY_OWNER_USER_ID
    foreign_person = "passage-private-person"
    storage.ensure_user(owner, preset_key="owner")
    storage.ensure_user(foreign_person)
    private_entity = Entity(
        new_id("ent"),
        owner,
        "Private passage export authority",
        EntityType.EVENT,
    )
    storage.create_entity(private_entity)
    private_raw = _file(
        storage,
        430,
        body="# Private derivative\n" + ("private passage body 430. " * 150),
        owner=owner,
    )
    private_projection = _publish_current(storage, private_raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=owner,
            raw_object_id=private_raw.id,
            entity_id=private_entity.id,
            title="Private passage knowledge",
            content=private_raw.raw_content,
            summary="",
        )
    )
    public_raw = _file(
        storage,
        431,
        body="# Public derivative\n" + ("public passage body 431. " * 150),
        owner=owner,
    )
    public_projection = _publish_current(storage, public_raw)
    _mark_foreign_private_reminder(
        storage,
        entity_id=private_entity.id,
        tenant_id=owner,
        person_id=foreign_person,
    )

    exported = storage.export_user(owner)
    payload = json.loads(Path(str(exported["path"])).read_text(encoding="utf-8"))
    projection_ids = {str(row["raw_object_id"]) for row in payload["document_passage_projections"]}
    passage_ids = {str(row["raw_object_id"]) for row in payload["document_passages"]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert projection_ids == {public_raw.id}
    assert passage_ids == {public_raw.id}
    assert len(payload["document_passages"]) == len(public_projection.passages)
    assert private_raw.id not in encoded
    assert private_projection.extracted_text_sha256 not in encoded
    assert "private passage body 430" not in encoded
