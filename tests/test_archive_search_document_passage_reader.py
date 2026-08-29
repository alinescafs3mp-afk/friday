from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import friday.storage._archive_search_documents as archive_documents
from friday.document_catalog.passage_projection import (
    DOCUMENT_PASSAGE_INDEX_REVISION,
    DocumentPassageProjection,
)
from friday.document_catalog.passage_schema import (
    document_passage_set_sha256,
    register_document_passage_connection_functions,
)
from friday.retrieval.archive_search_contract import (
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ReviewScope,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    SearchCorpus,
    SearchExecutionBinding,
    SearchLane,
)
from friday.storage._archive_search_documents import search_archive_document_lane
from friday.storage.models import InboxItem, InboxStatus, RawObject

TENANT = "passage-reader-tenant"
OWNER = "passage-reader-owner"
SNAPSHOT = "passage-reader-snapshot"
ROOT = Path(__file__).resolve().parents[1]


def _raw_id(number: int) -> str:
    return f"raw_{number:016x}"


def _seed(
    storage: Any,
    number: int,
    *,
    body: str,
    tenant: str = TENANT,
    uploader: str = OWNER,
    status: InboxStatus = InboxStatus.CLASSIFIED,
) -> str:
    storage.ensure_user(tenant)
    storage.ensure_user(uploader)
    raw_id = _raw_id(number)
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=tenant,
            source="upload",
            source_ref=f"passage-reader:{number}",
            raw_content=body,
            content_type="file",
            metadata_json={
                "filename": f"passage-{number}.pdf",
                "media_kind": "document",
                "mime_type": "application/pdf",
                "uploaded_by": uploader,
                "extraction_success": True,
                "text_extraction_success": True,
            },
            content_hash=hashlib.sha256(f"source-{number}".encode()).hexdigest(),
            received_at=f"2026-08-29T10:{number % 60:02d}:00+00:00",
            created_at=f"2026-08-29T10:{number % 60:02d}:00+00:00",
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id=f"inbox_{number:016x}",
            user_id=tenant,
            raw_object_id=raw_id,
            knowledge_object_id=None,
            status=status,
            created_at=f"2026-08-29T11:{number % 60:02d}:00+00:00",
            reviewed_at=(
                None if status is InboxStatus.PENDING else f"2026-08-29T12:{number % 60:02d}:00+00:00"
            ),
            reviewed_by=None if status is InboxStatus.PENDING else uploader,
        )
    )
    return raw_id


def _request(query: str = "Needle") -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        review_scope=ReviewScope.DISCOVERABLE,
        limit=20,
    )


def _binding(request: ArchiveSearchRequest) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=TENANT,
        principal_id=OWNER,
        requested_targets=((SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),),
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="passage-reader-run",
        privacy_key=b"p" * 32,
    )


def _search(storage: Any, request: ArchiveSearchRequest):
    binding = _binding(request)
    storage.conn.execute("BEGIN")
    try:
        page = search_archive_document_lane(
            storage.conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
            execution_binding=binding,
            snapshot_discriminator=SNAPSHOT,
            snapshot_current=True,
        )
        coverage = page.to_coverage(
            execution_binding=binding,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
        )
        return page, coverage
    finally:
        storage.conn.rollback()


def _project_current(storage: Any, raw_id: str) -> None:
    with storage.transaction() as conn:
        source = conn.execute(
            "SELECT version,content_hash,raw_content FROM raw_objects WHERE id=?",
            (raw_id,),
        ).fetchone()
        assert source is not None
        projection = DocumentPassageProjection.from_complete_text(
            raw_object_id=raw_id,
            source_version=int(source["version"]),
            source_content_sha256=str(source["content_hash"]),
            extracted_text=str(source["raw_content"]),
        )
        passage_rows = tuple(
            (
                passage.chunk_index,
                passage.start_char,
                passage.end_char,
                passage.content_sha256,
            )
            for passage in projection.passages
        )
        conn.execute(
            """UPDATE document_passage_projections
                  SET source_version=?,source_content_sha256=?,
                      extracted_text_sha256=?,source_char_count=?,
                      passage_set_sha256=?,
                      passage_index_revision=?,projection_status='current',
                      incomplete_reason=NULL,passage_count=?,
                      projected_at='2026-08-29T12:00:00Z'
                WHERE raw_object_id=?""",
            (
                projection.source_version,
                projection.source_content_sha256,
                projection.extracted_text_sha256,
                projection.source_char_count,
                document_passage_set_sha256(passage_rows),
                projection.passage_index_revision,
                len(projection.passages),
                projection.raw_object_id,
            ),
        )
        conn.executemany(
            """INSERT INTO document_passages(
                   raw_object_id,chunk_index,start_char,end_char,content_sha256
               ) VALUES(?,?,?,?,?)""",
            (
                (
                    raw_id,
                    passage.chunk_index,
                    passage.start_char,
                    passage.end_char,
                    passage.content_sha256,
                )
                for passage in projection.passages
            ),
        )


def test_projection_contract_can_import_before_storage_reader_without_a_cycle() -> None:
    probe = """
import friday.document_catalog.passage_projection as projection
import friday.storage._archive_search_documents as reader
assert projection.DOCUMENT_PASSAGE_INDEX_REVISION
assert reader.PASSAGE_INDEX_VERSION == 'archive-storage-char-v1'
"""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and local import probe
        [sys.executable, "-c", probe],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_reader_contract_requires_exact_schema_fingerprint_and_policy(storage, monkeypatch) -> None:
    real_import = archive_documents.importlib.import_module
    schema = SimpleNamespace(
        DOCUMENT_PASSAGE_INDEX_REVISION=DOCUMENT_PASSAGE_INDEX_REVISION,
        document_passage_schema_fingerprint=lambda _conn: "a" * 64,
    )

    def imported(name: str):
        return schema if name == "friday.document_catalog.passage_schema" else real_import(name)

    monkeypatch.setattr(archive_documents.importlib, "import_module", imported)
    assert archive_documents._document_passage_contract(storage.conn) is True  # noqa: SLF001

    schema.DOCUMENT_PASSAGE_INDEX_REVISION = "foreign-policy-v1"
    assert archive_documents._document_passage_contract(storage.conn) is False  # noqa: SLF001
    schema.DOCUMENT_PASSAGE_INDEX_REVISION = DOCUMENT_PASSAGE_INDEX_REVISION
    schema.document_passage_schema_fingerprint = lambda _conn: "not-a-digest"
    assert archive_documents._document_passage_contract(storage.conn) is False  # noqa: SLF001


def test_current_projection_changes_only_coverage_not_legacy_candidate(storage) -> None:
    raw_id = _seed(storage, 1, body="Exact Needle passage body")
    request = _request()
    before, before_coverage = _search(storage, request)
    before_candidate = before.candidates[0].to_private_json()
    before_passage = before.candidates[0].passages[0].passage_ref

    _project_current(storage, raw_id)
    changes = storage.conn.total_changes
    after, after_coverage = _search(storage, request)

    assert storage.conn.total_changes == changes
    assert after.candidates[0].to_private_json() == before_candidate
    assert after.candidates[0].passages[0].passage_ref == before_passage
    assert before.derivative_current is False
    assert before_coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert after.derivative_current is True
    assert after_coverage.states == (CoverageState.COMPLETE,)


def test_missing_projection_keeps_hits_but_never_confirms_a_miss(storage) -> None:
    projected = _seed(storage, 2, body="Needle remains usable")
    _seed(storage, 3, body="A different authorized body")
    _project_current(storage, projected)
    with storage.transaction() as conn:
        conn.execute(
            "DELETE FROM document_passage_projections WHERE raw_object_id=?",
            (_raw_id(3),),
        )

    hit, hit_coverage = _search(storage, _request())
    miss, miss_coverage = _search(storage, _request("Absent phrase"))

    assert hit.matched == hit.returned == 1
    assert hit.candidates[0].passages[0].excerpt == "Needle remains usable"
    assert hit_coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert hit_coverage.absence_decision().value == "evidence_found"
    assert miss.matched == miss.returned == 0
    assert miss_coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert miss_coverage.absence_decision().value == "not_established"


@pytest.mark.parametrize("drift", ("source", "incomplete", "child_count"))
def test_stale_incomplete_or_short_projection_is_partial_fallback(
    storage,
    drift: str,
) -> None:
    raw_id = _seed(storage, 7, body="Needle exact fallback")
    _project_current(storage, raw_id)
    with storage.transaction() as conn:
        if drift == "source":
            conn.create_function(
                "friday_document_passage_projection_valid",
                14,
                lambda *_args: 1,
                deterministic=True,
            )
            conn.execute(
                "UPDATE document_passage_projections SET source_content_sha256=? WHERE raw_object_id=?",
                ("f" * 64, raw_id),
            )
            register_document_passage_connection_functions(conn)
        elif drift == "incomplete":
            conn.execute("DELETE FROM document_passages WHERE raw_object_id=?", (raw_id,))
            conn.execute(
                """UPDATE document_passage_projections
                      SET extracted_text_sha256=NULL,source_char_count=NULL,
                          passage_set_sha256=NULL,
                          projection_status='incomplete',
                          incomplete_reason='backfill_pending',passage_count=0
                    WHERE raw_object_id=?""",
                (raw_id,),
            )
        else:
            conn.execute("DELETE FROM document_passages WHERE raw_object_id=?", (raw_id,))

    page, coverage = _search(storage, _request())

    assert page.matched == page.returned == 1
    assert page.candidates[0].passages[0].excerpt == "Needle exact fallback"
    assert page.derivative_current is False
    assert coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)


def test_foreign_and_ignored_projection_gaps_do_not_degrade_owner_coverage(
    storage,
) -> None:
    projected = _seed(storage, 4, body="Needle owner body")
    _seed(
        storage,
        5,
        body="Needle ignored body",
        status=InboxStatus.IGNORED,
    )
    _seed(
        storage,
        6,
        body="Needle foreign body",
        tenant="passage-reader-foreign-tenant",
        uploader="passage-reader-foreign-owner",
    )
    _project_current(storage, projected)

    page, coverage = _search(storage, _request())

    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    assert page.derivative_current is True
    assert coverage.states == (CoverageState.COMPLETE,)


def test_tampered_child_set_keeps_legacy_hit_but_fails_readiness_closed(storage) -> None:
    raw_id = _seed(storage, 8, body="Needle " + ("alpha beta gamma. " * 500))
    _project_current(storage, raw_id)
    with storage.transaction() as conn:
        conn.create_function(
            "friday_document_passage_span_valid",
            6,
            lambda *_args: 1,
            deterministic=True,
        )
        conn.execute(
            """UPDATE document_passages
                  SET content_sha256=?
                WHERE raw_object_id=? AND chunk_index=0""",
            ("f" * 64, raw_id),
        )
        register_document_passage_connection_functions(conn)

    page, coverage = _search(storage, _request())

    assert page.matched == page.returned == 1
    assert page.derivative_current is False
    assert coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)


def test_current_reader_never_rechunks_source_per_child(storage, monkeypatch) -> None:
    raw_id = _seed(storage, 9, body="Needle " + ("alpha beta gamma. " * 30_000))
    _project_current(storage, raw_id)
    calls = 0

    def counted_projection(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("reader must not rechunk an already published passage set")

    monkeypatch.setattr(
        DocumentPassageProjection,
        "from_complete_text",
        classmethod(counted_projection),
    )

    page, coverage = _search(storage, _request())

    assert calls == 0
    assert page.derivative_current is True
    assert coverage.states == (CoverageState.COMPLETE,)
