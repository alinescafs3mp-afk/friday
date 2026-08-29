"""Focused acceptance for the bounded schema-47 document-passage writer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

import friday.storage._document_catalog as document_catalog_storage
from friday.document_catalog.passage_projection import DocumentPassageProjection
from friday.document_catalog.passage_schema import (
    document_passage_schema_fingerprint,
    document_passage_set_sha256,
    validate_document_passage_schema,
)
from friday.retrieval._contract_utils import RetrievalContractError
from friday.storage import FridayStorage
from friday.storage.models import RawObject

OWNER = "passage-writer-owner"


def _receipt(body: str) -> dict[str, object]:
    normalized = " ".join(body.split())
    return {
        "extraction_receipt_version": 1,
        "extraction_success": True,
        "extraction_error": "",
        "text_extraction_success": True,
        "text_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
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
    raw_object_id: str,
    *,
    body: str,
    source_ref: str | None = None,
) -> RawObject:
    storage.ensure_user(OWNER)
    raw = RawObject(
        id=raw_object_id,
        user_id=OWNER,
        source="upload",
        source_ref=source_ref or f"passage-writer:{raw_object_id}",
        raw_content=body,
        content_type="file",
        metadata_json=_receipt(body),
        content_hash=hashlib.sha256(f"source:{source_ref or raw_object_id}".encode()).hexdigest(),
        version=1,
    )
    return storage.store_raw_object(raw)


def _backfill(
    storage: FridayStorage,
    *,
    cursor: str | None = None,
    limit: int = 64,
) -> dict[str, object]:
    return storage.backfill_document_catalog(
        OWNER,
        after_raw_object_id=cursor,
        limit=limit,
        include_document_passages=True,
    )


def _expected_projection(raw: RawObject, *, body: str | None = None) -> DocumentPassageProjection:
    return DocumentPassageProjection.from_complete_text(
        raw_object_id=raw.id,
        source_version=raw.version,
        source_content_sha256=raw.content_hash,
        extracted_text=raw.raw_content if body is None else body,
    )


def _parent(storage: FridayStorage, raw_object_id: str) -> sqlite3.Row:
    row = storage.execute(
        "SELECT * FROM document_passage_projections WHERE raw_object_id=?",
        (raw_object_id,),
    ).fetchone()
    assert row is not None
    return row


def _children(storage: FridayStorage, raw_object_id: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in storage.execute(
            """SELECT chunk_index,start_char,end_char,content_sha256
                 FROM document_passages
                WHERE raw_object_id=? ORDER BY chunk_index""",
            (raw_object_id,),
        ).fetchall()
    )


def test_backfill_atomically_publishes_exact_body_free_parent_and_children(
    storage: FridayStorage,
) -> None:
    secret = "SECRET-PASSAGE-BODY-9917 " + ("alpha beta gamma. " * 180)
    raw = _file(storage, "raw_passage_writer_atomic", body=secret)
    expected = _expected_projection(raw)
    expected_rows = tuple(
        (item.chunk_index, item.start_char, item.end_char, item.content_sha256) for item in expected.passages
    )
    fingerprint = document_passage_schema_fingerprint(storage.conn)

    report = _backfill(storage, limit=1)

    assert report["examined"] == 1
    assert report["processed"] == 0, "passage work must not redefine legacy catalog counts"
    assert report["passage_processed"] == report["passage_changed"] == 1
    parent = _parent(storage, raw.id)
    assert parent["source_version"] == raw.version
    assert parent["source_content_sha256"] == raw.content_hash
    assert parent["extracted_text_sha256"] == expected.extracted_text_sha256
    assert parent["source_char_count"] == len(secret)
    assert parent["passage_set_sha256"] == document_passage_set_sha256(expected_rows)
    assert parent["projection_status"] == "current"
    assert parent["incomplete_reason"] is None
    assert parent["passage_count"] == len(expected_rows)
    assert _children(storage, raw.id) == expected_rows
    assert document_passage_schema_fingerprint(storage.conn) == fingerprint
    validate_document_passage_schema(storage.conn)

    sidecar_material = repr(dict(parent)) + repr(_children(storage, raw.id))
    assert secret not in sidecar_material
    forbidden = {"body", "text", "excerpt", "path", "filename", "metadata_json"}
    parent_columns = {
        str(row[1]) for row in storage.execute("PRAGMA table_info(document_passage_projections)")
    }
    child_columns = {str(row[1]) for row in storage.execute("PRAGMA table_info(document_passages)")}
    assert parent_columns.isdisjoint(forbidden)
    assert child_columns.isdisjoint(forbidden)


def test_backfill_cursor_resumes_after_reopen_and_replay_is_idempotent(settings) -> None:
    first = FridayStorage(settings)
    raw_ids = [f"raw_passage_restart_{index:02d}" for index in range(3)]
    try:
        for index, raw_id in enumerate(raw_ids):
            _file(first, raw_id, body=f"# Restart {index}\n" + ("bounded body. " * 120))
        page = _backfill(first, limit=1)
        assert page["passage_changed"] == 1
        assert page["has_more"] is True
        assert page["next_after_raw_object_id"] == raw_ids[0]
        first_parent = tuple(_parent(first, raw_ids[0]))
        cursor = str(page["next_after_raw_object_id"])
    finally:
        first.close(final=True)

    reopened = FridayStorage(replace(settings, database_must_exist=True))
    try:
        resumed = _backfill(reopened, cursor=cursor, limit=1)
        assert resumed["passage_changed"] == 1
        assert resumed["next_after_raw_object_id"] == raw_ids[1]
        assert tuple(_parent(reopened, raw_ids[0])) == first_parent
        assert len(_children(reopened, raw_ids[0])) == int(_parent(reopened, raw_ids[0])["passage_count"])

        replay = _backfill(reopened, cursor=None, limit=1)
        assert replay["passage_processed"] == replay["passage_changed"] == 0
        assert tuple(_parent(reopened, raw_ids[0])) == first_parent
        assert len(_children(reopened, raw_ids[0])) == int(_parent(reopened, raw_ids[0])["passage_count"])
    finally:
        reopened.close(final=True)


def test_source_change_resets_children_and_republishes_exact_replacement(
    storage: FridayStorage,
) -> None:
    original = "# Original\n" + ("old passage. " * 180)
    raw = _file(storage, "raw_passage_source_change", body=original)
    assert _backfill(storage, limit=1)["passage_changed"] == 1
    original_rows = _children(storage, raw.id)

    replacement = "# Replacement\n" + ("new exact passage. " * 210)
    replacement_digest = hashlib.sha256(b"replacement-source-bytes").hexdigest()
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE raw_objects
                  SET raw_content=?,metadata_json=?,content_hash=?,version=2
                WHERE id=?""",
            (replacement, json.dumps(_receipt(replacement)), replacement_digest, raw.id),
        )

    reset = _parent(storage, raw.id)
    assert reset["projection_status"] == "incomplete"
    assert reset["incomplete_reason"] == "source_changed"
    assert reset["source_version"] == 2
    assert reset["source_content_sha256"] == replacement_digest
    assert reset["passage_count"] == 0
    assert _children(storage, raw.id) == ()

    report = _backfill(storage, limit=1)
    assert report["passage_changed"] == 1
    source = storage.execute(
        "SELECT id,version,content_hash,raw_content FROM raw_objects WHERE id=?",
        (raw.id,),
    ).fetchone()
    assert source is not None
    expected = DocumentPassageProjection.from_complete_text(
        raw_object_id=raw.id,
        source_version=2,
        source_content_sha256=replacement_digest,
        extracted_text=replacement,
    )
    expected_rows = tuple(
        (item.chunk_index, item.start_char, item.end_char, item.content_sha256) for item in expected.passages
    )
    assert _children(storage, raw.id) == expected_rows
    assert _children(storage, raw.id) != original_rows
    assert _parent(storage, raw.id)["extracted_text_sha256"] == expected.extracted_text_sha256
    validate_document_passage_schema(storage.conn)


def test_passage_backfill_obeys_keyset_page_limit(storage: FridayStorage) -> None:
    raw_ids = [f"raw_passage_page_{index:02d}" for index in range(5)]
    for index, raw_id in enumerate(raw_ids):
        _file(storage, raw_id, body=f"# Page {index}\n" + ("small body. " * 30))

    report = _backfill(storage, limit=2)

    assert report["examined"] == 2
    assert report["passage_processed"] == report["passage_changed"] == 2
    assert report["has_more"] is True
    assert report["next_after_raw_object_id"] == raw_ids[1]
    assert [
        str(row[0])
        for row in storage.execute(
            """SELECT raw_object_id FROM document_passage_projections
                WHERE projection_status='current' ORDER BY raw_object_id"""
        ).fetchall()
    ] == raw_ids[:2]


def test_passage_backfill_obeys_byte_budget_with_one_oversized_progress_item(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_ids = [f"raw_passage_bytes_{index:02d}" for index in range(3)]
    body = "# Oversized\n" + ("x" * 512)
    for raw_id in raw_ids:
        _file(storage, raw_id, body=body)
    monkeypatch.setattr(document_catalog_storage, "DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES", 32)

    report = _backfill(storage, limit=10)

    assert report["passage_processed"] == report["passage_changed"] == 1
    assert report["raw_text_bytes_examined"] == len(body.encode()) > 32
    assert report["byte_budget_reached"] is True
    assert report["has_more"] is True
    assert report["next_after_raw_object_id"] == raw_ids[0]
    assert _parent(storage, raw_ids[0])["projection_status"] == "current"
    assert all(_parent(storage, raw_id)["projection_status"] == "incomplete" for raw_id in raw_ids[1:])


def test_schema_legal_legacy_id_stays_explicit_incomplete_and_cursor_advances(
    storage: FridayStorage,
) -> None:
    legacy = _file(
        storage,
        "",
        body="# Legacy empty id\nExact body",
        source_ref="passage-writer:legacy-empty-id",
    )
    valid = _file(storage, "raw_passage_valid_after_legacy", body="# Valid\nExact body")

    legacy_page = _backfill(storage, limit=1)

    assert legacy_page["examined"] == 1
    assert legacy_page["processed"] == 0
    assert legacy_page["passage_processed"] == 1
    assert legacy_page["passage_changed"] == 0
    assert legacy_page["has_more"] is True
    assert legacy_page["next_after_raw_object_id"] == ""
    assert _parent(storage, legacy.id)["raw_object_id"] == ""
    assert _parent(storage, legacy.id)["projection_status"] == "incomplete"
    assert _parent(storage, legacy.id)["incomplete_reason"] == "backfill_pending"
    assert _children(storage, legacy.id) == ()
    validate_document_passage_schema(storage.conn)

    resumed = _backfill(storage, cursor="", limit=1)
    assert resumed["passage_processed"] == resumed["passage_changed"] == 1
    assert _parent(storage, valid.id)["projection_status"] == "current"


def test_child_insert_failure_rolls_back_parent_and_partial_children(
    storage: FridayStorage,
) -> None:
    body = "# Rollback\n" + ("alpha beta gamma delta. " * 320)
    raw = _file(storage, "raw_passage_forced_rollback", body=body)
    assert len(_expected_projection(raw).passages) > 1
    fingerprint = document_passage_schema_fingerprint(storage.conn)
    with storage.transaction() as conn:
        conn.execute(
            """CREATE TEMP TRIGGER force_document_passage_child_failure
               BEFORE INSERT ON main.document_passages
               WHEN NEW.chunk_index=1
               BEGIN
                   SELECT RAISE(ABORT,'forced_document_passage_child_failure');
               END"""
        )

    with pytest.raises(sqlite3.DatabaseError, match="forced_document_passage_child_failure"):
        _backfill(storage, limit=1)

    rolled_back = _parent(storage, raw.id)
    assert rolled_back["projection_status"] == "incomplete"
    assert rolled_back["incomplete_reason"] == "backfill_pending"
    assert rolled_back["passage_count"] == 0
    assert _children(storage, raw.id) == ()
    assert document_passage_schema_fingerprint(storage.conn) == fingerprint

    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER temp.force_document_passage_child_failure")
    assert _backfill(storage, limit=1)["passage_changed"] == 1
    validate_document_passage_schema(storage.conn)


def test_supported_identity_projection_failure_is_visible_and_keeps_pending(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _file(storage, "raw_passage_projection_failure", body="# Failure\nExact body")

    def rejected_projection(*_args: object, **_kwargs: object) -> DocumentPassageProjection:
        raise RetrievalContractError("forced projection contract failure")

    monkeypatch.setattr(
        DocumentPassageProjection,
        "from_complete_text",
        classmethod(rejected_projection),
    )

    with pytest.raises(RetrievalContractError, match="forced projection contract failure"):
        _backfill(storage, limit=1)

    assert _parent(storage, raw.id)["projection_status"] == "incomplete"
    assert _parent(storage, raw.id)["incomplete_reason"] == "backfill_pending"
    assert _children(storage, raw.id) == ()


def test_v3_writer_repairs_released_nonprogress_topology_in_one_bounded_item(
    storage: FridayStorage,
) -> None:
    body = "Prelude sentence. " + ("x" * 1_400)
    raw = _file(storage, "raw_passage_v2_nonprogress", body=body)
    expected = _expected_projection(raw)

    report = _backfill(storage, limit=1)

    assert report["examined"] == report["passage_processed"] == 1
    assert report["passage_changed"] == 1
    parent = _parent(storage, raw.id)
    assert parent["projection_status"] == "current"
    assert parent["incomplete_reason"] is None
    assert parent["passage_count"] == 3
    assert _children(storage, raw.id) == tuple(
        (item.chunk_index, item.start_char, item.end_char, item.content_sha256) for item in expected.passages
    )
    assert [(row[1], row[2]) for row in _children(storage, raw.id)] == [
        (0, 18),
        (18, 1_218),
        (1_018, len(body)),
    ]
    validate_document_passage_schema(storage.conn)
