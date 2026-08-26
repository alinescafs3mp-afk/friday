"""Schema-41 durable DocumentCatalog invariants and bounded convergence."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import friday.storage._document_catalog as document_catalog_storage
from friday.account_deletion import (
    _DELETE_SCOPES,
    _mark_account_deletion_history_clean,
    _unknown_user_scopes,
    preflight_account_deletion,
)
from friday.document_catalog.schema import (
    DOCUMENT_CATALOG_ENRICHMENT_REVISION,
    _canonical_document_catalog_schema_objects,
    document_catalog_schema_fingerprint,
    validate_document_catalog_schema,
)
from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage.models import KnowledgeObject, RawObject, new_id

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


def _unpack_schema_40(tmp_path: Path, name: str) -> Path:
    database = tmp_path / name
    with gzip.open(SCHEMA_FIXTURES / "schema-40.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)
    return database


def _file(
    storage: FridayStorage,
    index: int,
    *,
    owner: str = "alice",
    body: object = "# Explicit heading\nBody",
    metadata: object = None,
    content_hash: object | None = None,
    version: object = 1,
) -> RawObject:
    storage.ensure_user(owner)
    raw = RawObject(
        id=f"raw_catalog_{index:08x}",
        user_id=owner,
        source="upload",
        source_ref=f"catalog-test:{index}",
        raw_content=body,  # type: ignore[arg-type]
        content_type="file",
        metadata_json=(
            _receipt(body)
            if metadata is None and type(body) is str
            else ({} if metadata is None else metadata)
        ),  # type: ignore[arg-type]
        content_hash=(
            hashlib.sha256(f"file-{index}".encode()).hexdigest() if content_hash is None else content_hash
        ),  # type: ignore[arg-type]
        version=version,  # type: ignore[arg-type]
    )
    return storage.store_raw_object(raw)


def test_schema_41_is_exact_body_free_and_fingerprinted(storage: FridayStorage) -> None:
    assert SCHEMA_VERSION == 45
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "45"
    observed = {
        (str(row[0]), str(row[1])): "".join(str(row[2]).split())
        for row in storage.execute(
            """SELECT type,name,sql FROM sqlite_master
                WHERE sql IS NOT NULL AND (
                    name='document_catalog' OR tbl_name='document_catalog'
                    OR name LIKE 'document_catalog_%'
                    OR name LIKE 'idx_document_catalog_%'
                )"""
        )
    }
    assert observed == _canonical_document_catalog_schema_objects()
    assert len(document_catalog_schema_fingerprint(storage.conn)) == 64
    columns = {str(row[1]) for row in storage.execute("PRAGMA table_info(document_catalog)")}
    assert columns == {
        "raw_object_id",
        "source_version",
        "source_content_sha256",
        "extracted_text_sha256",
        "semantic_title",
        "title_authority",
        "enrichment_revision",
        "enrichment_status",
        "incomplete_reason",
        "enriched_at",
    }
    assert not columns.intersection(
        {"user_id", "tenant_id", "uploader_id", "body", "summary", "tags", "metadata_json"}
    )


def test_exact_schema_40_file_migrates_to_backfill_pending_then_current(
    settings,
    tmp_path: Path,
) -> None:
    database = _unpack_schema_40(tmp_path, "schema40-to-41.sqlite3")
    body = "# Migrated heading\nHistorical extracted body"
    with sqlite3.connect(database) as legacy:
        legacy.execute(
            """INSERT INTO raw_objects(
                   id,user_id,source,source_ref,raw_content,content_type,
                   metadata_json,content_hash,version,received_at,created_at
               ) VALUES(?,?,?,?,?,'file',?,?,1,?,?)""",
            (
                "raw-catalog-schema40-migration",
                "fixture-owner",
                "upload",
                "catalog-schema40-migration",
                body,
                json.dumps(_receipt(body), sort_keys=True),
                hashlib.sha256(b"schema40 migration source").hexdigest(),
                "2026-08-25T00:00:00Z",
                "2026-08-25T00:00:00Z",
            ),
        )
        legacy.commit()

    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "45"
        )
        pending = migrated.get_document_catalog_entry("fixture-owner", "raw-catalog-schema40-migration")
        assert pending is not None and pending["incomplete_reason"] == "backfill_pending"
        assert (
            migrated.backfill_document_catalog("fixture-owner", after_raw_object_id=None, limit=10)[
                "processed"
            ]
            == 1
        )
        current = migrated.get_document_catalog_entry("fixture-owner", "raw-catalog-schema40-migration")
        assert current is not None and current["enrichment_status"] == "current"
    finally:
        migrated.close(final=True)


def test_interrupted_counterfeit_schema_41_fails_closed_without_marker_publication(
    settings,
    tmp_path: Path,
) -> None:
    database = _unpack_schema_40(tmp_path, "schema40-counterfeit.sqlite3")
    with sqlite3.connect(database) as interrupted:
        interrupted.execute("CREATE TABLE document_catalog(raw_object_id TEXT PRIMARY KEY)")
        interrupted.commit()

    broken = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="DocumentCatalog DDL"):
            broken.execute("SELECT 1").fetchone()
    finally:
        broken.close(final=True)
    with sqlite3.connect(database) as unchanged:
        assert (
            unchanged.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "40"
        )


def test_safe_ingestion_is_current_in_the_same_write_and_title_is_navigation_only(
    storage: FridayStorage,
) -> None:
    raw = _file(storage, 1)
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None
    assert row["source_version"] == 1
    assert row["source_content_sha256"] == raw.content_hash
    assert row["extracted_text_sha256"] == hashlib.sha256(raw.raw_content.encode()).hexdigest()
    assert row["semantic_title"] == "Explicit heading"
    assert row["title_authority"] == "navigation_only"
    assert row["enrichment_revision"] == DOCUMENT_CATALOG_ENRICHMENT_REVISION
    assert row["enrichment_status"] == "current" and row["incomplete_reason"] is None
    assert storage.get_document_catalog_entry("bob", raw.id) is None


def test_cas_upsert_is_idempotent_and_rejects_a_stale_source_revision(
    storage: FridayStorage,
) -> None:
    raw = _file(storage, 8)
    original = storage.get_document_catalog_entry("alice", raw.id)
    assert original is not None
    repeated = storage.upsert_document_catalog_entry(
        "alice",
        raw.id,
        expected_source_version=raw.version,
        expected_source_content_sha256=raw.content_hash,
        enriched_at="2026-08-25T01:02:03Z",
    )
    assert repeated == original

    replacement_hash = hashlib.sha256(b"replacement revision").hexdigest()
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET version=version+1,content_hash=? WHERE id=?",
            (replacement_hash, raw.id),
        )
    assert (
        storage.upsert_document_catalog_entry(
            "alice",
            raw.id,
            expected_source_version=raw.version,
            expected_source_content_sha256=raw.content_hash,
        )
        is None
    )
    current = storage.upsert_document_catalog_entry(
        "alice",
        raw.id,
        expected_source_version=raw.version + 1,
        expected_source_content_sha256=replacement_hash,
    )
    assert current is not None and current["enrichment_status"] == "current"
    assert current["source_version"] == raw.version + 1


def test_ordinary_first_line_is_not_duplicated_as_a_semantic_title(storage: FridayStorage) -> None:
    body = "A short sentence from the private body.\nMore body."
    raw = _file(storage, 2, body=body)
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None and row["enrichment_status"] == "current"
    assert row["semantic_title"] is None
    assert body not in str(row)


def test_compatible_extraction_receipt_requires_two_success_attestations(
    storage: FridayStorage,
) -> None:
    compatible = _file(
        storage,
        9,
        metadata={"extraction_success": True, "text_extraction_success": True},
    )
    compatible_row = storage.get_document_catalog_entry("alice", compatible.id)
    assert compatible_row is not None and compatible_row["enrichment_status"] == "current"

    weak = _file(storage, 10, metadata={"text_extraction_success": True})
    weak_row = storage.get_document_catalog_entry("alice", weak.id)
    assert weak_row is not None
    assert weak_row["incomplete_reason"] == "extraction_incomplete"

    malformed_flag = _file(
        storage,
        11,
        metadata={
            "extraction_success": True,
            "text_extraction_success": True,
            "text_truncated": "false",
        },
    )
    malformed_row = storage.get_document_catalog_entry("alice", malformed_flag.id)
    assert malformed_row is not None
    assert malformed_row["incomplete_reason"] == "extraction_incomplete"


@pytest.mark.parametrize(
    "missing_field",
    (
        "parse_total_pages",
        "archive_files_read",
        "extraction_chars",
        "text_truncated",
    ),
)
def test_v1_receipt_requires_every_field_and_complete_counter_pairs(
    storage: FridayStorage,
    missing_field: str,
) -> None:
    body = "# Strict v1 receipt\nComplete extracted body"
    metadata = _receipt(body)
    metadata.pop(missing_field)
    raw = _file(storage, 200 + len(missing_field), body=body, metadata=metadata)
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None
    assert row["enrichment_status"] == "incomplete"
    assert row["incomplete_reason"] == "extraction_incomplete"

    with pytest.raises(sqlite3.DatabaseError, match="source binding"):
        storage.execute(
            """UPDATE document_catalog
                  SET enrichment_status='current',incomplete_reason=NULL,
                      extracted_text_sha256=?,semantic_title='Strict v1 receipt'
                WHERE raw_object_id=?""",
            (hashlib.sha256(body.encode()).hexdigest(), raw.id),
        )
    storage.conn.rollback()


@pytest.mark.parametrize(
    "mutation",
    (
        "digest_mismatch",
        "length_mismatch",
        "non_false_flag",
        "non_integer_counter",
        "partial_pages",
        "partial_archive",
    ),
)
def test_v1_receipt_requires_exact_digest_length_flags_and_counters(
    storage: FridayStorage,
    mutation: str,
) -> None:
    body = "# Exact v1 receipt\nComplete extracted body"
    metadata = _receipt(body)
    if mutation == "digest_mismatch":
        metadata["text_sha256"] = "0" * 64
    elif mutation == "length_mismatch":
        metadata["extraction_chars"] = len(body) + 1
    elif mutation == "non_false_flag":
        metadata["text_truncated"] = "false"
    elif mutation == "non_integer_counter":
        metadata["parse_pages_read"] = 0.0
    elif mutation == "partial_pages":
        metadata.update(parse_pages_read=1, parse_total_pages=2)
    else:
        metadata.update(archive_files=2, archive_files_read=1)

    raw = _file(storage, 260 + len(mutation), body=body, metadata=metadata)
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None
    assert row["enrichment_status"] == "incomplete"
    assert row["incomplete_reason"] == "extraction_incomplete"


@pytest.mark.parametrize(
    ("assignment", "params"),
    (
        ("source_version=0", ()),
        ("source_content_sha256=?", ("A" * 64,)),
        ("source_content_sha256=?", (sqlite3.Binary(b"a" * 64),)),
        ("extracted_text_sha256=?", (sqlite3.Binary(b"b" * 64),)),
        ("semantic_title=?", ("x" * 241,)),
        ("semantic_title=?", ("forged model prose",)),
        ("semantic_title=?", ("forged\ttitle",)),
        ("incomplete_reason='arbitrary'", ()),
        ("enriched_at='2026-08-23T24:00:00Z'", ()),
        ("enriched_at='2026-02-30T00:00:00Z'", ()),
        ("enrichment_status='current',incomplete_reason='no_text'", ()),
        ("enrichment_status='incomplete',incomplete_reason=NULL", ()),
    ),
)
def test_direct_mutations_cannot_forge_hash_version_title_reason_time_or_matrix(
    storage: FridayStorage,
    assignment: str,
    params: tuple[object, ...],
) -> None:
    raw = _file(storage, 3)
    with pytest.raises(sqlite3.DatabaseError):
        storage.execute(
            f"UPDATE document_catalog SET {assignment} WHERE raw_object_id=?",  # nosec B608
            (*params, raw.id),
        )
    storage.conn.rollback()
    validate_document_catalog_schema(storage.conn)


def test_wrong_exact_body_hash_and_non_source_title_are_rejected(storage: FridayStorage) -> None:
    raw = _file(storage, 4)
    with pytest.raises(sqlite3.DatabaseError):
        storage.execute(
            """UPDATE document_catalog
                  SET extracted_text_sha256=?,semantic_title='Different heading'
                WHERE raw_object_id=?""",
            ("f" * 64, raw.id),
        )
    storage.conn.rollback()
    validate_document_catalog_schema(storage.conn)


@pytest.mark.parametrize(
    ("body", "metadata"),
    (
        ("", {"text_extraction_success": False}),
        ("Legacy body without a durable extraction receipt.", {}),
    ),
)
def test_direct_sql_cannot_promote_unready_extraction_to_current(
    storage: FridayStorage,
    body: str,
    metadata: dict[str, object],
) -> None:
    raw = _file(storage, 41 + len(body), body=body, metadata=metadata)
    row = storage.execute(
        "SELECT enrichment_status,incomplete_reason FROM document_catalog WHERE raw_object_id=?",
        (raw.id,),
    ).fetchone()
    assert row["enrichment_status"] == "incomplete"
    with pytest.raises(sqlite3.DatabaseError, match="source binding"):
        storage.execute(
            """UPDATE document_catalog
                  SET enrichment_status='current',incomplete_reason=NULL,
                      extracted_text_sha256=?,semantic_title=NULL
                WHERE raw_object_id=?""",
            (hashlib.sha256(body.encode()).hexdigest(), raw.id),
        )
    storage.conn.rollback()
    validate_document_catalog_schema(storage.conn)


def test_duplicate_extraction_receipt_keys_fail_explicitly(storage: FridayStorage) -> None:
    raw = _file(storage, 92)
    storage.execute(
        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
        ('{"text_extraction_success":true,"text_extraction_success":false}', raw.id),
    )
    result = storage.backfill_document_catalog("alice", after_raw_object_id=None, limit=10)
    assert result["processed"] == 1
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None
    assert row["enrichment_status"] == "incomplete"
    assert row["incomplete_reason"] == "extraction_failed"


@pytest.mark.parametrize(
    "forged_reason",
    ("source_unavailable", "extraction_failed", "extraction_incomplete", "no_text", "unsupported_content"),
)
def test_direct_sql_cannot_forge_a_closed_but_false_incomplete_reason(
    storage: FridayStorage,
    forged_reason: str,
) -> None:
    raw = _file(storage, 100 + len(forged_reason))
    with pytest.raises(sqlite3.DatabaseError, match="source binding"):
        storage.execute(
            """UPDATE document_catalog
                  SET enrichment_status='incomplete',incomplete_reason=?,
                      extracted_text_sha256=NULL,semantic_title=NULL
                WHERE raw_object_id=?""",
            (forged_reason, raw.id),
        )
    storage.conn.rollback()
    validate_document_catalog_schema(storage.conn)


def test_upsert_rejects_a_false_closed_incomplete_reason(storage: FridayStorage) -> None:
    raw = _file(storage, 130)
    with pytest.raises(ValueError, match="does not match"):
        storage.upsert_document_catalog_entry(
            "alice",
            raw.id,
            expected_source_version=raw.version,
            expected_source_content_sha256=raw.content_hash,
            enrichment_status="incomplete",
            incomplete_reason="no_text",
        )


def test_raw_revision_and_metadata_mutation_clear_stale_title_without_json_parsing(
    storage: FridayStorage,
) -> None:
    raw = _file(storage, 5)
    replacement = "# Replacement\nNew body"
    storage.execute(
        "UPDATE raw_objects SET raw_content=?,version=version+1 WHERE id=?",
        (replacement, raw.id),
    )
    row = storage.execute("SELECT * FROM document_catalog WHERE raw_object_id=?", (raw.id,)).fetchone()
    assert row["enrichment_status"] == "incomplete"
    assert row["incomplete_reason"] == "source_changed"
    assert row["semantic_title"] is None and row["extracted_text_sha256"] is None

    storage.execute("UPDATE raw_objects SET metadata_json='malformed{' WHERE id=?", (raw.id,))
    row = storage.execute("SELECT * FROM document_catalog WHERE raw_object_id=?", (raw.id,)).fetchone()
    assert row["incomplete_reason"] == "source_changed"
    result = storage.backfill_document_catalog("alice", after_raw_object_id=None, limit=10)
    assert result["processed"] == 1
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None and row["incomplete_reason"] == "extraction_failed"


def test_same_write_source_and_metadata_replacement_rebinds_without_blocking_raw(
    storage: FridayStorage,
) -> None:
    raw = _file(storage, 151)
    replacement = "# Replacement\nThe registered source changed."
    replacement_hash = hashlib.sha256(b"replacement file bytes").hexdigest()

    storage.execute(
        """UPDATE raw_objects
              SET raw_content=?,content_hash=?,metadata_json=?
            WHERE id=?""",
        (replacement, replacement_hash, "malformed{", raw.id),
    )

    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None
    assert row["source_version"] == raw.version
    assert row["source_content_sha256"] == replacement_hash
    assert row["enrichment_status"] == "incomplete"
    assert row["incomplete_reason"] == "source_changed"


def test_invalid_legacy_revision_is_explicit_incomplete_not_a_raw_write_failure(
    storage: FridayStorage,
) -> None:
    raw = _file(storage, 6, content_hash="invalid", version=0)
    row = storage.get_document_catalog_entry("alice", raw.id)
    assert row is not None
    assert row["source_version"] is None and row["source_content_sha256"] is None
    assert row["enrichment_status"] == "incomplete"
    assert row["incomplete_reason"] == "source_unavailable"
    coverage = storage.document_catalog_coverage("alice")
    assert coverage["catalogued"] == 1 and coverage["stale"] == 0
    storage.execute("UPDATE raw_objects SET metadata_json='malformed{' WHERE id=?", (raw.id,))
    after_metadata_update = storage.get_document_catalog_entry("alice", raw.id)
    assert after_metadata_update is not None
    assert after_metadata_update["incomplete_reason"] == "source_unavailable"
    assert storage.reconcile_document_catalog("alice", after_raw_object_id=None) == {
        "examined": 1,
        "inserted": 0,
        "reset": 0,
        "removed": 0,
        "has_more": False,
        "next_after_raw_object_id": None,
    }


def test_soft_delete_prunes_and_restore_reseeds_explicitly(storage: FridayStorage) -> None:
    raw = _file(storage, 7)
    storage.execute("UPDATE raw_objects SET deleted_at='2026-08-25T00:00:00Z' WHERE id=?", (raw.id,))
    assert (
        storage.execute("SELECT 1 FROM document_catalog WHERE raw_object_id=?", (raw.id,)).fetchone() is None
    )
    storage.execute("UPDATE raw_objects SET deleted_at=NULL WHERE id=?", (raw.id,))
    row = storage.execute("SELECT * FROM document_catalog WHERE raw_object_id=?", (raw.id,)).fetchone()
    assert row is not None and row["incomplete_reason"] == "backfill_pending"


def test_backfill_converges_across_pages_and_restart_without_cursor_starvation(
    settings,
) -> None:
    first = FridayStorage(settings)
    try:
        raw_ids: list[str] = []
        for index in range(20, 27):
            raw = _file(first, index, metadata={})
            raw_ids.append(raw.id)
        with first.transaction() as conn:
            conn.executemany(
                """UPDATE document_catalog
                      SET enrichment_status='incomplete',incomplete_reason='backfill_pending',
                          extracted_text_sha256=NULL,semantic_title=NULL
                    WHERE raw_object_id=?""",
                ((raw_id,) for raw_id in raw_ids),
            )
        one = first.backfill_document_catalog("alice", after_raw_object_id=None, limit=2)
        assert one["processed"] == 2 and one["has_more"] is True
        first.kv_set("test:document-catalog-cursor", str(one["next_after_raw_object_id"]))
    finally:
        first.close(final=True)

    reopened = FridayStorage(settings)
    try:
        cursor = str(reopened.kv_get("test:document-catalog-cursor") or "")
        second = reopened.backfill_document_catalog("alice", after_raw_object_id=cursor, limit=2)
        assert second["processed"] == 2 and second["has_more"] is True
        third = reopened.backfill_document_catalog(
            "alice",
            after_raw_object_id=str(second["next_after_raw_object_id"]),
            limit=2,
        )
        assert third["processed"] == 2 and third["has_more"] is True
        final = reopened.backfill_document_catalog(
            "alice",
            after_raw_object_id=str(third["next_after_raw_object_id"]),
            limit=2,
        )
        assert final["processed"] == 1 and final["has_more"] is False
        empty = reopened.backfill_document_catalog("alice", after_raw_object_id=raw_ids[-1], limit=2)
        assert empty["processed"] == 0 and empty["has_more"] is False
        coverage = reopened.document_catalog_coverage("alice")
        assert coverage["coverage_complete"] is True
        assert coverage["explicit_incomplete"] == 7
        assert coverage["incomplete_reasons"]["extraction_incomplete"] == 7
    finally:
        reopened.close(final=True)


def test_keyset_checkpoint_round_trips_every_schema_valid_raw_text_id(
    storage: FridayStorage,
) -> None:
    storage.ensure_user("alice")
    raw_ids = ["", " a", " b", "a", "a" * 201, "z"]
    for index, raw_id in enumerate(raw_ids):
        body = f"# Opaque cursor {index}\nBody"
        storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id="alice",
                source="upload",
                source_ref=f"opaque-cursor:{index}",
                raw_content=body,
                content_type="file",
                metadata_json=_receipt(body),
                content_hash=hashlib.sha256(f"opaque-{index}".encode()).hexdigest(),
            )
        )
    with storage.transaction() as conn:
        conn.executemany(
            """UPDATE document_catalog
                  SET enrichment_status='incomplete',incomplete_reason='backfill_pending',
                      extracted_text_sha256=NULL,semantic_title=NULL
                WHERE raw_object_id=?""",
            ((raw_id,) for raw_id in raw_ids),
        )

    cursor: str | None = None
    for index, raw_id in enumerate(raw_ids):
        report = storage.backfill_document_catalog(
            "alice",
            after_raw_object_id=cursor,
            limit=1,
        )
        assert report["examined"] == report["processed"] == 1
        if index < len(raw_ids) - 1:
            assert report["has_more"] is True
            assert report["next_after_raw_object_id"] == raw_id
            cursor = raw_id
        else:
            assert report["has_more"] is False
            assert report["next_after_raw_object_id"] is None
    for raw_id in raw_ids:
        entry = storage.get_document_catalog_entry("alice", raw_id)
        assert entry is not None and entry["raw_object_id"] == raw_id

    storage.execute(
        """UPDATE document_catalog
              SET enrichment_status='incomplete',incomplete_reason='backfill_pending',
                  extracted_text_sha256=NULL,semantic_title=NULL
            WHERE raw_object_id=' a'"""
    )
    source = storage.execute("SELECT version,content_hash FROM raw_objects WHERE id=' a'").fetchone()
    assert source is not None
    updated = storage.upsert_document_catalog_entry(
        "alice",
        " a",
        expected_source_version=int(source["version"]),
        expected_source_content_sha256=str(source["content_hash"]),
    )
    assert updated is not None and updated["raw_object_id"] == " a"
    assert storage.get_document_catalog_entry("alice", "a") is not None
    with pytest.raises(ValueError, match="exact TEXT or None"):
        storage.backfill_document_catalog("alice", after_raw_object_id=1, limit=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact TEXT"):
        storage.get_document_catalog_entry("alice", None)  # type: ignore[arg-type]


def test_backfill_streams_bodies_under_a_strict_byte_budget_with_oversized_progress(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Oversized\n" + ("x" * 40)
    raw_ids = [_file(storage, 230 + index, body=body).id for index in range(3)]
    with storage.transaction() as conn:
        conn.executemany(
            """UPDATE document_catalog
                  SET enrichment_status='incomplete',incomplete_reason='backfill_pending',
                      extracted_text_sha256=NULL,semantic_title=NULL
                WHERE raw_object_id=?""",
            ((raw_id,) for raw_id in raw_ids),
        )
    monkeypatch.setattr(
        document_catalog_storage,
        "DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES",
        16,
    )
    original_source = document_catalog_storage._raw_projection_source  # noqa: SLF001
    body_reads: list[str] = []

    def observed_source(
        conn: sqlite3.Connection,
        *,
        owner: str,
        raw_object_id: str,
    ) -> sqlite3.Row | None:
        body_reads.append(raw_object_id)
        return original_source(conn, owner=owner, raw_object_id=raw_object_id)

    monkeypatch.setattr(document_catalog_storage, "_raw_projection_source", observed_source)

    first = storage.backfill_document_catalog("alice", after_raw_object_id=None, limit=10)
    assert first["processed"] == 1
    assert first["has_more"] is True
    assert first["byte_budget_reached"] is True
    assert first["raw_text_bytes_examined"] == len(body.encode()) > 16
    assert body_reads == [raw_ids[0]]
    second = storage.backfill_document_catalog(
        "alice",
        after_raw_object_id=str(first["next_after_raw_object_id"]),
        limit=10,
    )
    assert second["processed"] == 1 and second["has_more"] is True
    assert body_reads == raw_ids[:2]
    final = storage.backfill_document_catalog(
        "alice",
        after_raw_object_id=str(second["next_after_raw_object_id"]),
        limit=10,
    )
    assert final["processed"] == 1 and final["has_more"] is False
    assert body_reads == raw_ids


def test_backfill_reads_a_body_only_after_the_indexed_page_marks_it_retryable(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_ids = [_file(storage, 320 + index).id for index in range(4)]
    storage.execute(
        """UPDATE document_catalog
              SET enrichment_status='incomplete',incomplete_reason='backfill_pending',
                  extracted_text_sha256=NULL,semantic_title=NULL
            WHERE raw_object_id=?""",
        (raw_ids[-1],),
    )
    original = document_catalog_storage._raw_projection_source  # noqa: SLF001
    body_reads: list[str] = []

    def observed_source(
        conn: sqlite3.Connection,
        *,
        owner: str,
        raw_object_id: str,
    ) -> sqlite3.Row | None:
        body_reads.append(raw_object_id)
        return original(conn, owner=owner, raw_object_id=raw_object_id)

    monkeypatch.setattr(document_catalog_storage, "_raw_projection_source", observed_source)
    report = storage.backfill_document_catalog("alice", after_raw_object_id=None, limit=4)

    assert report["examined"] == 4
    assert report["processed"] == 1
    assert body_reads == [raw_ids[-1]]


def test_rebuild_byte_budget_cursor_and_reconcile_has_more_are_bounded(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Rebuild\n" + ("y" * 40)
    raw_ids = [_file(storage, 240 + index, body=body).id for index in range(3)]
    monkeypatch.setattr(
        document_catalog_storage,
        "DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES",
        16,
    )
    original_source = document_catalog_storage._raw_projection_source  # noqa: SLF001
    body_reads: list[str] = []

    def observed_source(
        conn: sqlite3.Connection,
        *,
        owner: str,
        raw_object_id: str,
    ) -> sqlite3.Row | None:
        body_reads.append(raw_object_id)
        return original_source(conn, owner=owner, raw_object_id=raw_object_id)

    monkeypatch.setattr(document_catalog_storage, "_raw_projection_source", observed_source)
    first = storage.rebuild_document_catalog("alice", limit=10)
    assert first["processed"] == 1
    assert first["has_more"] is True
    assert first["byte_budget_reached"] is True
    assert first["next_after_raw_object_id"] == raw_ids[0]
    assert body_reads == [raw_ids[0]]
    second = storage.rebuild_document_catalog(
        "alice",
        after_raw_object_id=str(first["next_after_raw_object_id"]),
        limit=10,
    )
    assert second["processed"] == 1
    assert second["next_after_raw_object_id"] == raw_ids[1]
    assert body_reads == raw_ids[:2]

    with storage.transaction() as conn:
        conn.executemany(
            "DELETE FROM document_catalog WHERE raw_object_id=?",
            ((raw_id,) for raw_id in raw_ids),
        )
    reconciled = storage.reconcile_document_catalog("alice", after_raw_object_id=None, limit=1)
    assert reconciled == {
        "examined": 1,
        "inserted": 1,
        "reset": 0,
        "removed": 0,
        "has_more": True,
        "next_after_raw_object_id": raw_ids[0],
    }
    assert (
        storage.reconcile_document_catalog(
            "alice",
            after_raw_object_id=str(reconciled["next_after_raw_object_id"]),
            limit=256,
        )["has_more"]
        is False
    )
    with pytest.raises(ValueError, match="between 1 and 256"):
        storage.backfill_document_catalog("alice", after_raw_object_id=None, limit=257)


def test_convergence_pages_use_the_owner_keyset_without_a_temp_sort(
    storage: FridayStorage,
) -> None:
    _file(storage, 305)
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        storage.rebuild_document_catalog("alice", after_raw_object_id="", limit=1)
        storage.backfill_document_catalog("alice", after_raw_object_id="", limit=1)
        storage.reconcile_document_catalog("alice", after_raw_object_id="", limit=1)
    finally:
        storage.conn.set_trace_callback(None)

    page_queries = [
        statement
        for statement in statements
        if "INDEXED BY idx_document_catalog_source_owner_id" in statement
    ]
    assert len(page_queries) == 3
    for query in page_queries:
        plan = storage.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        details = "\n".join(str(row[3]) for row in plan)
        assert "idx_document_catalog_source_owner_id" in details
        assert "TEMP B-TREE" not in details
        assert "source.id>" in query.replace(" ", "")


def test_exact_schema_validator_rejects_missing_and_extra_objects(storage: FridayStorage) -> None:
    storage.execute("CREATE INDEX idx_document_catalog_counterfeit ON document_catalog(raw_object_id)")
    with pytest.raises(sqlite3.DatabaseError, match="DDL"):
        document_catalog_schema_fingerprint(storage.conn)
    storage.execute("DROP INDEX idx_document_catalog_counterfeit")
    storage.execute(
        """CREATE TRIGGER unrelated_sidecar_reader AFTER INSERT ON users
           BEGIN SELECT COUNT(*) FROM document_catalog; END"""
    )
    with pytest.raises(sqlite3.DatabaseError, match="DDL"):
        validate_document_catalog_schema(storage.conn)
    storage.execute("DROP TRIGGER unrelated_sidecar_reader")
    storage.execute("DROP TRIGGER document_catalog_raw_au_extraction_state")
    with pytest.raises(sqlite3.DatabaseError, match="DDL"):
        validate_document_catalog_schema(storage.conn)


@pytest.mark.parametrize(
    ("version", "content_hash", "counterfeit_assignment"),
    (
        (1, "invalid", "source_version=NULL"),
        (0, "a" * 64, "source_content_sha256=NULL"),
    ),
)
def test_schema_validator_rejects_null_mixed_source_binding_corruption(
    storage: FridayStorage,
    version: int,
    content_hash: str,
    counterfeit_assignment: str,
) -> None:
    raw = _file(storage, 310 + version, version=version, content_hash=content_hash)
    trigger_sql = storage.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='document_catalog_bu_validate'"
    ).fetchone()[0]
    storage.execute("DROP TRIGGER document_catalog_bu_validate")
    storage.execute(
        f"UPDATE document_catalog SET {counterfeit_assignment} WHERE raw_object_id=?",  # nosec B608
        (raw.id,),
    )
    storage.execute(str(trigger_sql))

    with pytest.raises(sqlite3.DatabaseError, match="source binding"):
        validate_document_catalog_schema(storage.conn)


def test_account_deletion_inventory_derives_catalog_owner_through_raw(storage: FridayStorage) -> None:
    scope = next(item for item in _DELETE_SCOPES if item.key == "document_catalog")
    assert scope.predicate == "raw_object_id IN (SELECT id FROM raw_objects WHERE user_id=?)"
    assert _unknown_user_scopes(storage.conn) == []


def test_account_deletion_preflight_counts_exact_scope_without_touching_neighbour(
    storage: FridayStorage,
) -> None:
    target = "local:catalog-delete"
    neighbour = "local:catalog-neighbour"
    target_raw = _file(storage, 140, owner=target)
    neighbour_raw = _file(storage, 141, owner=neighbour)
    assert _mark_account_deletion_history_clean(storage, target)
    storage.update_user(target, status="disabled")

    plan = preflight_account_deletion(storage, target, quiescence_available=True)
    assert plan["counts"]["document_catalog"] == 1
    assert plan["counts"]["raw_objects"] == 1
    assert plan["cross_account_object_references"]["foreign_keys"] == {}
    assert {item["code"] for item in plan["blockers"]} == {"stored_files"}
    assert storage.get_document_catalog_entry(target, target_raw.id) is not None
    assert storage.get_document_catalog_entry(neighbour, neighbour_raw.id) is not None


def test_purge_counts_and_removes_the_sidecar_before_raw(storage: FridayStorage) -> None:
    raw = _file(storage, 40)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        title="Catalog purge",
        content=raw.raw_content,
        summary="",
    )
    storage.store_knowledge_object(knowledge)
    storage.soft_delete_knowledge_object(knowledge.id, "alice")
    result = storage.purge_knowledge_object(knowledge.id, "alice")
    assert result["raw_removed"] is True
    assert result["deleted"]["document_catalog"] == 1
    assert (
        storage.execute("SELECT 1 FROM document_catalog WHERE raw_object_id=?", (raw.id,)).fetchone() is None
    )


def test_purge_retains_catalog_until_the_authoritative_raw_is_removed(storage: FridayStorage) -> None:
    raw = _file(storage, 142)
    first = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        title="First",
        content=raw.raw_content,
        summary="",
    )
    second = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        title="Second",
        content=raw.raw_content,
        summary="",
    )
    storage.store_knowledge_object(first)
    storage.store_knowledge_object(second)
    storage.soft_delete_knowledge_object(first.id, "alice")
    retained = storage.purge_knowledge_object(first.id, "alice")
    assert retained["raw_removed"] is False
    assert "document_catalog" not in retained["deleted"]
    assert storage.get_document_catalog_entry("alice", raw.id) is not None

    storage.soft_delete_knowledge_object(second.id, "alice")
    removed = storage.purge_knowledge_object(second.id, "alice")
    assert removed["raw_removed"] is True
    assert removed["deleted"]["document_catalog"] == 1


def test_backup_authenticates_document_catalog_schema_and_data(storage: FridayStorage) -> None:
    raw = _file(storage, 143)
    backup = storage.create_backup(label="document-catalog-schema41")
    verification = storage.verify_backup(Path(str(backup["path"])).name)
    assert verification["ok"] is True
    with sqlite3.connect(str(backup["path"])) as copied:
        copied.row_factory = sqlite3.Row
        validate_document_catalog_schema(copied)
        row = copied.execute("SELECT * FROM document_catalog WHERE raw_object_id=?", (raw.id,)).fetchone()
        assert row is not None and row["enrichment_status"] == "current"
