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
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    SearchCorpus,
    SearchExecutionBinding,
    SearchLane,
)
from friday.storage._archive_search_documents import (
    PASSAGE_INDEX_VERSION,
    search_archive_document_lane,
)
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
    extraction_metadata: dict[str, object] | None = None,
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
            metadata_json=(
                {
                    "filename": f"passage-{number}.pdf",
                    "media_kind": "document",
                    "mime_type": "application/pdf",
                    "uploaded_by": uploader,
                    "extraction_success": True,
                    "text_extraction_success": True,
                }
                if extraction_metadata is None
                else extraction_metadata
            ),
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
    assert PASSAGE_INDEX_VERSION == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    probe = """
import friday.document_catalog.passage_projection as projection
import friday.storage._archive_search_documents as reader
assert projection.DOCUMENT_PASSAGE_INDEX_REVISION
assert reader.PASSAGE_INDEX_VERSION == 'archive-storage-char-v1'
assert reader.DOCUMENT_STORED_PASSAGE_INDEX_VERSION == (
    'archive-storage-char-v2:document-chunk-spans-v3'
)
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
        document_passage_set_sha256=document_passage_set_sha256,
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


def test_current_projection_changes_only_passage_identity_and_coverage(storage) -> None:
    raw_id = _seed(storage, 1, body="Exact Needle passage body")
    request = _request()
    before, before_coverage = _search(storage, request)
    before_miss, before_miss_coverage = _search(storage, _request("Absent phrase"))
    before_candidate = before.candidates[0]
    before_passage = before.candidates[0].passages[0].passage_ref

    _project_current(storage, raw_id)
    changes = storage.conn.total_changes
    after, after_coverage = _search(storage, request)
    after_miss, after_miss_coverage = _search(storage, _request("Absent phrase"))

    assert storage.conn.total_changes == changes
    after_candidate = after.candidates[0]
    after_passage = after_candidate.passages[0].passage_ref
    assert after_candidate.resolved_source == before_candidate.resolved_source
    assert after_candidate.matches == before_candidate.matches
    assert after_candidate.review_state is before_candidate.review_state
    assert after_candidate.lifecycle_state is before_candidate.lifecycle_state
    assert after_candidate.evidence_authority is before_candidate.evidence_authority
    assert after_candidate.passages[0].excerpt == before_candidate.passages[0].excerpt
    assert before_passage.passage_index_version == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    assert after_passage.passage_index_version == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    assert after_passage != before_passage
    assert before.derivative_current is False
    assert before_coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert after.derivative_current is True
    assert after_coverage.states == (CoverageState.COMPLETE,)
    assert before_miss.matched == after_miss.matched == 0
    assert before_miss_coverage.absence_decision().value == "not_established"
    assert after_miss_coverage.states == (CoverageState.COMPLETE,)
    assert after_miss_coverage.absence_decision().value == "authorized_absence_confirmed"


def test_current_children_do_not_change_candidate_membership_or_lane_rank(storage) -> None:
    raw_ids = (
        _seed(storage, 63, body="Needle first ranked evidence"),
        _seed(storage, 64, body="Needle second ranked evidence"),
    )
    request = _request()
    before, _before_coverage = _search(storage, request)

    for raw_id in reversed(raw_ids):
        _project_current(storage, raw_id)
    after, after_coverage = _search(storage, request)

    def identity_and_rank(page):  # type: ignore[no-untyped-def]
        return tuple(
            (candidate.resolved_source.source_ref.canonical_object_id, candidate.matches)
            for candidate in page.candidates
        )

    assert identity_and_rank(after) == identity_and_rank(before)
    assert (after.total, after.examined, after.matched, after.returned) == (
        before.total,
        before.examined,
        before.matched,
        before.returned,
    )
    assert all(
        candidate.passages[0].passage_ref.passage_index_version == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
        for candidate in after.candidates
    )
    assert after_coverage.states == (CoverageState.COMPLETE,)


def test_current_projection_uses_lowest_stored_child_containing_exact_match(storage) -> None:
    marker = "NeedleOverlap"
    body = ("a" * 1_050) + marker + ("b" * 2_000)
    raw_id = _seed(storage, 60, body=body)
    _project_current(storage, raw_id)

    rows = storage.execute(
        """SELECT chunk_index,start_char,end_char
             FROM document_passages
            WHERE raw_object_id=? ORDER BY chunk_index""",
        (raw_id,),
    )
    match_start = body.index(marker)
    match_end = match_start + len(marker)
    containing = [
        tuple(row)
        for row in rows
        if int(row["start_char"]) <= match_start < match_end <= int(row["end_char"])
    ]
    assert len(containing) >= 2

    page, coverage = _search(storage, _request(marker))
    passage = page.candidates[0].passages[0]
    locator = passage.passage_ref.locator
    selected = containing[0]

    assert passage.passage_ref.passage_index_version == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    assert locator.chunk_index == selected[0]  # type: ignore[union-attr]
    assert selected[1] <= locator.start_char <= match_start  # type: ignore[union-attr]
    assert match_end <= locator.end_char <= selected[2]  # type: ignore[union-attr]
    assert body[locator.start_char : locator.end_char] == passage.excerpt  # type: ignore[union-attr]
    assert coverage.states == (CoverageState.COMPLETE,)


def test_match_crossing_every_stored_child_keeps_legacy_locator(storage) -> None:
    marker = "Q" * 600
    body = ("a" * 700) + marker + ("b" * 1_800)
    raw_id = _seed(storage, 61, body=body)
    _project_current(storage, raw_id)
    match_start = body.index(marker)
    match_end = match_start + len(marker)
    rows = storage.execute(
        """SELECT start_char,end_char FROM document_passages
            WHERE raw_object_id=? ORDER BY chunk_index""",
        (raw_id,),
    )
    assert not any(int(row["start_char"]) <= match_start < match_end <= int(row["end_char"]) for row in rows)

    page, coverage = _search(storage, _request(marker))

    assert page.matched == page.returned == 1
    assert (
        page.candidates[0].passages[0].passage_ref.passage_index_version
        == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    )
    assert page.candidates[0].passages[0].passage_ref.locator.chunk_index == 0  # type: ignore[union-attr]
    assert page.derivative_current is True
    assert coverage.states == (CoverageState.COMPLETE,)


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
    assert (
        hit.candidates[0].passages[0].passage_ref.passage_index_version
        == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    )
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
    assert (
        page.candidates[0].passages[0].passage_ref.passage_index_version
        == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    )
    assert page.derivative_current is False
    assert coverage.states == (
        (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
        if drift == "child_count"
        else (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    )


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
    assert (
        page.candidates[0].passages[0].passage_ref.passage_index_version
        == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    )
    assert coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)


def test_forged_consistent_child_set_cannot_mint_a_stored_locator(storage) -> None:
    raw_id = _seed(storage, 62, body="Needle " + ("alpha beta gamma. " * 100))
    _project_current(storage, raw_id)
    with storage.transaction() as conn:
        conn.create_function(
            "friday_document_passage_span_valid",
            6,
            lambda *_args: 1,
            deterministic=True,
        )
        conn.create_function(
            "friday_document_passage_projection_valid",
            14,
            lambda *_args: 1,
            deterministic=True,
        )
        conn.execute(
            """UPDATE document_passages
                  SET content_sha256=?
                WHERE raw_object_id=? AND chunk_index=0""",
            ("f" * 64, raw_id),
        )
        forged_rows = tuple(
            (int(row[0]), int(row[1]), int(row[2]), str(row[3]))
            for row in conn.execute(
                """SELECT chunk_index,start_char,end_char,content_sha256
                     FROM document_passages
                    WHERE raw_object_id=? ORDER BY chunk_index""",
                (raw_id,),
            )
        )
        conn.execute(
            """UPDATE document_passage_projections
                  SET passage_set_sha256=? WHERE raw_object_id=?""",
            (document_passage_set_sha256(forged_rows), raw_id),
        )
        register_document_passage_connection_functions(conn)

    page, coverage = _search(storage, _request())

    assert page.matched == page.returned == 1
    assert page.derivative_current is False
    assert page.derivative_unavailable is True
    assert (
        page.candidates[0].passages[0].passage_ref.passage_index_version
        == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    )
    assert coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)


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
    assert (
        page.candidates[0].passages[0].passage_ref.passage_index_version
        == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    )
    assert coverage.states == (CoverageState.COMPLETE,)


def test_terminal_incomplete_source_is_unavailable_and_never_confirms_a_miss(storage) -> None:
    current = _seed(storage, 10, body="Needle current evidence")
    _seed(
        storage,
        11,
        body="Different terminal evidence",
        extraction_metadata={
            "filename": "passage-11.pdf",
            "media_kind": "document",
            "mime_type": "application/pdf",
            "uploaded_by": OWNER,
            "extraction_success": False,
            "text_extraction_success": False,
            "extraction_error": "terminal",
        },
    )
    _project_current(storage, current)

    hit, hit_coverage = _search(storage, _request())
    miss, miss_coverage = _search(storage, _request("Absent phrase"))

    assert hit.matched == hit.returned == 1
    assert hit.derivative_unavailable is True
    assert hit_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert miss.matched == miss.returned == 0
    assert miss_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert miss_coverage.absence_decision().value == "not_established"


def test_source_change_is_pending_until_bounded_writer_republishes(storage) -> None:
    raw_id = _seed(storage, 12, body="Needle original evidence")
    initial = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=1,
        include_document_passages=True,
    )
    assert initial["passage_changed"] == 1
    assert _search(storage, _request())[1].states == (CoverageState.COMPLETE,)

    with storage.transaction() as conn:
        conn.execute("DELETE FROM document_passages WHERE raw_object_id=?", (raw_id,))
        conn.execute(
            """UPDATE document_passage_projections
                  SET extracted_text_sha256=NULL,source_char_count=NULL,
                      passage_set_sha256=NULL,projection_status='incomplete',
                      incomplete_reason='source_changed',passage_count=0
                WHERE raw_object_id=?""",
            (raw_id,),
        )

    pending, pending_coverage = _search(storage, _request())
    assert pending.matched == pending.returned == 1
    assert pending_coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    repaired = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=1,
        include_document_passages=True,
    )
    assert repaired["passage_processed"] == repaired["passage_changed"] == 1
    current, current_coverage = _search(storage, _request())
    assert current.matched == current.returned == 1
    assert current.derivative_current is True
    assert current_coverage.states == (CoverageState.COMPLETE,)


def test_all_current_capped_lane_reports_only_capped_partial(storage) -> None:
    for number in range(20, 41):
        raw_id = _seed(storage, number, body=f"Needle capped evidence {number}")
        _project_current(storage, raw_id)

    page, coverage = _search(storage, _request())

    assert page.total == page.examined == page.matched == 21
    assert page.returned == 20 and page.has_more is True
    assert page.derivative_current is True
    assert coverage.states == (CoverageState.CAPPED, CoverageState.PARTIAL)
    assert coverage.next_cursor_available is False


def test_exact_64_span_tail_remains_current_and_searchable(storage) -> None:
    body = ("bounded section. " * 5_200) + "NeedleTailToken"
    raw_id = _seed(storage, 50, body=body)
    _project_current(storage, raw_id)

    parent = storage.execute(
        "SELECT passage_count,source_char_count FROM document_passage_projections WHERE raw_object_id=?",
        (raw_id,),
    ).fetchone()
    tail = storage.execute(
        """SELECT chunk_index,end_char FROM document_passages
            WHERE raw_object_id=? ORDER BY chunk_index DESC LIMIT 1""",
        (raw_id,),
    ).fetchone()
    page, coverage = _search(storage, _request("NeedleTailToken"))

    assert parent is not None and tuple(parent) == (64, len(body))
    assert tail is not None and tuple(tail) == (63, len(body))
    assert page.matched == page.returned == 1
    assert (
        page.candidates[0].passages[0].passage_ref.passage_index_version
        == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    )
    assert page.candidates[0].passages[0].passage_ref.locator.chunk_index == 63  # type: ignore[union-attr]
    assert page.derivative_current is True
    assert coverage.states == (CoverageState.COMPLETE,)


def test_embedded_nul_uses_exact_python_character_count_for_readiness(storage) -> None:
    body = "Needle\x00Unicode café tail"
    raw_id = _seed(storage, 51, body=body)
    _project_current(storage, raw_id)

    parent = storage.execute(
        "SELECT source_char_count FROM document_passage_projections WHERE raw_object_id=?",
        (raw_id,),
    ).fetchone()
    page, coverage = _search(storage, _request())

    assert parent is not None and int(parent[0]) == len(body)
    assert page.matched == page.returned == 1
    assert page.derivative_current is True
    assert coverage.states == (CoverageState.COMPLETE,)
