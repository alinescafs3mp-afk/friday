from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
import sqlite3
from dataclasses import asdict, is_dataclass
from typing import Any

import pytest

import friday.storage._archive_search_documents as archive_document_storage
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveLifecycleConstraint,
    ArchiveReviewState,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
    ReviewScope,
)
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    LifecycleState,
    RepresentationKind,
    RevisionKind,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceKind,
    SourceRepresentation,
    SourceRevision,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
)
from friday.storage._archive_search_documents import (
    ArchiveDocumentLanePage,
    ArchiveDocumentStorageError,
    search_archive_document_lane,
    select_authorized_archive_document_replay_source_in_transaction,
)
from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject

TENANT = "archive-tenant"
OWNER = "archive-owner"
OTHER_OWNER = "archive-other-owner"
FOREIGN_TENANT = "archive-foreign-tenant"
SECRET = "Needle private body 9917"
SNAPSHOT = "archive-doc-snapshot"


def _opaque(prefix: str, number: int) -> str:
    return f"{prefix}_{number:016x}"


def _seed(
    storage: Any,
    number: int,
    *,
    tenant: str = TENANT,
    owner: str = OWNER,
    filename: str | None = None,
    body: str = SECRET,
    received_at: str | None = None,
    inbox_status: InboxStatus | None = None,
    knowledge_content: str | None = None,
    knowledge_title: str = "",
    knowledge_lifecycle: str = "active",
    content_hash: str | None = None,
    text_extraction_success: bool | None = None,
) -> tuple[str, str | None]:
    storage.ensure_user(tenant)
    storage.ensure_user(owner)
    raw_id = _opaque("raw", number)
    metadata: dict[str, object] = {
        "filename": filename or f"document-{number}.pdf",
        "mime_type": "application/pdf",
        "media_kind": "document",
        "uploaded_by": owner,
    }
    if text_extraction_success is not None:
        metadata["extraction_success"] = True
        metadata["extraction_error"] = ""
        metadata["text_extraction_success"] = text_extraction_success
    raw = RawObject(
        id=raw_id,
        user_id=tenant,
        source="upload",
        source_ref=f"telegram-file:{number}",
        raw_content=body,
        content_type="file",
        metadata_json=metadata,
        content_hash=(
            hashlib.sha256(f"source-bytes-{number}".encode()).hexdigest()
            if content_hash is None
            else content_hash
        ),
        received_at=received_at or f"2026-08-23T10:{number % 60:02d}:00+00:00",
        created_at=received_at or f"2026-08-23T10:{number % 60:02d}:00+00:00",
    )
    storage.store_raw_object(raw)
    knowledge_id: str | None = None
    if knowledge_content is not None:
        knowledge_id = _opaque("ko", number)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=knowledge_id,
                user_id=tenant,
                raw_object_id=raw_id,
                content=knowledge_content,
                content_type="document",
                title=knowledge_title,
                summary=f"Summary {number}",
                lifecycle_stage=knowledge_lifecycle,
                version=number + 1,
                created_at=f"2026-08-23T11:{number % 60:02d}:00+00:00",
                updated_at=f"2026-08-23T11:{number % 60:02d}:00+00:00",
            )
        )
    if inbox_status is not None:
        storage.store_inbox_item(
            InboxItem(
                id=_opaque("inbox", number),
                user_id=tenant,
                raw_object_id=raw_id,
                knowledge_object_id=knowledge_id,
                status=inbox_status,
                created_at=f"2026-08-23T12:{number % 60:02d}:00+00:00",
                reviewed_at=(
                    None
                    if inbox_status is InboxStatus.PENDING
                    else f"2026-08-23T13:{number % 60:02d}:00+00:00"
                ),
                reviewed_by=owner if inbox_status is not InboxStatus.PENDING else None,
            )
        )
    return raw_id, knowledge_id


def _request(
    *,
    corpora: tuple[ArchiveSearchCorpus, ...] = (
        ArchiveSearchCorpus.DOCUMENTS,
        ArchiveSearchCorpus.KNOWLEDGE,
    ),
    query: str = "Needle",
    review_scope: ReviewScope = ReviewScope.DISCOVERABLE,
    lifecycle_constraints: tuple[ArchiveLifecycleConstraint, ...] = (),
    temporal_constraints: tuple[ArchiveTemporalConstraint, ...] = (),
    limit: int = 20,
    focus: str = "",
) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=query,
        corpora=corpora,
        review_scope=review_scope,
        lifecycle_constraints=lifecycle_constraints,
        temporal_constraints=temporal_constraints,
        limit=limit,
        focus=focus,
    )


def _binding(
    request: ArchiveSearchRequest,
    target: tuple[SearchCorpus, SearchLane],
    *,
    tenant: str = TENANT,
    owner: str = OWNER,
    snapshot: str = SNAPSHOT,
    run: str = "archive-doc-run",
) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=tenant,
        principal_id=owner,
        requested_targets=(target,),
        snapshot_discriminator=snapshot,
        run_discriminator=run,
        privacy_key=b"z" * 32,
    )


def _search(
    storage: Any,
    *,
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    limit: int | None = None,
) -> ArchiveDocumentLanePage:
    conn = storage.conn
    conn.execute("BEGIN")
    try:
        return search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=corpus,
            lane=lane,
            execution_binding=_binding(
                request,
                (
                    {
                        ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
                        ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
                    }[corpus],
                    lane,
                ),
            ),
            snapshot_discriminator=SNAPSHOT,
            snapshot_current=True,
            limit=limit,
        )
    finally:
        conn.rollback()


def _coverage(
    page: ArchiveDocumentLanePage,
    request: ArchiveSearchRequest,
    *,
    tenant: str = TENANT,
    owner: str = OWNER,
    snapshot: str = SNAPSHOT,
) -> SearchCoverage:
    target = (
        {
            ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
            ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
        }[page.corpus],
        page.lane,
    )
    return page.to_coverage(
        execution_binding=_binding(
            request,
            target,
            tenant=tenant,
            owner=owner,
            snapshot=snapshot,
        ),
        tenant_id=tenant,
        owner_id=owner,
        request=request,
        snapshot_discriminator=snapshot,
    )


def _install_test_document_catalog(storage: Any) -> None:
    """Install the future sidecar shape without changing production schema code."""

    with storage.transaction() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS document_catalog(
                   raw_object_id TEXT NOT NULL PRIMARY KEY
                       REFERENCES raw_objects(id) ON DELETE CASCADE,
                   source_version INTEGER,
                   source_content_sha256 TEXT,
                   extracted_text_sha256 TEXT,
                   semantic_title TEXT,
                   title_authority TEXT NOT NULL DEFAULT 'navigation_only',
                   enrichment_revision INTEGER NOT NULL DEFAULT 1,
                   enrichment_status TEXT NOT NULL,
                   incomplete_reason TEXT,
                   enriched_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_document_catalog_status
                 ON document_catalog(enrichment_status,raw_object_id)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_document_catalog_reason
                 ON document_catalog(incomplete_reason,raw_object_id)
              WHERE enrichment_status='incomplete'"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_document_catalog_text
                 ON document_catalog(extracted_text_sha256,raw_object_id)
              WHERE enrichment_status='current'"""
        )


def _replace_with_loose_test_document_catalog(storage: Any) -> None:
    """Explicitly bypass schema guards for impossible stale-row defense tests."""

    with storage.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS document_catalog")
    _install_test_document_catalog(storage)


def _delete_test_document_catalog_row(storage: Any, raw_id: str) -> None:
    with storage.transaction() as conn:
        conn.execute("DELETE FROM document_catalog WHERE raw_object_id=?", (raw_id,))


def _store_test_document_catalog_row(
    storage: Any,
    raw_id: str,
    *,
    semantic_title: str | None,
    status: str = "current",
    stale_version: bool = False,
    stale_hash: bool = False,
) -> None:
    raw = storage.conn.execute(
        "SELECT version,content_hash,raw_content FROM raw_objects WHERE id=?",
        (raw_id,),
    ).fetchone()
    assert raw is not None
    source_version = int(raw[0]) + (1 if stale_version else 0)
    source_hash = "0" * 64 if stale_hash else str(raw[1])
    extracted_text_hash = hashlib.sha256(str(raw[2]).encode()).hexdigest()
    incomplete = status == "incomplete"
    with storage.transaction() as conn:
        existing = conn.execute(
            """SELECT enrichment_status,semantic_title
                 FROM document_catalog WHERE raw_object_id=?""",
            (raw_id,),
        ).fetchone()
        if existing is not None and not stale_version and not stale_hash:
            if incomplete:
                conn.execute(
                    """UPDATE document_catalog
                          SET extracted_text_sha256=NULL, semantic_title=NULL,
                              enrichment_status='incomplete',
                              incomplete_reason='backfill_pending', enriched_at=?
                        WHERE raw_object_id=?""",
                    ("2026-08-25T09:00:00Z", raw_id),
                )
                return
            assert tuple(existing) == ("current", semantic_title)
            return
        assert existing is None
        conn.execute(
            """INSERT INTO document_catalog(
                   raw_object_id,source_version,source_content_sha256,
                   extracted_text_sha256,semantic_title,title_authority,
                   enrichment_revision,enrichment_status,incomplete_reason,enriched_at
               ) VALUES(?,?,?,?,?,'navigation_only',1,?,?,?)""",
            (
                raw_id,
                source_version,
                source_hash,
                None if incomplete else extracted_text_hash,
                None if incomplete else semantic_title,
                status,
                "backfill_pending" if incomplete else None,
                "2026-08-25T09:00:00Z",
            ),
        )


def test_lexical_lanes_authorize_before_counts_and_return_exact_revision_passages(storage) -> None:
    confirmed_raw, confirmed_ko = _seed(
        storage,
        1,
        filename="confirmed.pdf",
        body=f"Raw exact {SECRET} confirmed",
        knowledge_content=f"Knowledge exact {SECRET} canonical",
        knowledge_title="Confirmed knowledge",
    )
    pending_raw, _ = _seed(
        storage,
        2,
        filename="pending.pdf",
        body=f"Pending exact {SECRET}",
        inbox_status=InboxStatus.PENDING,
    )
    _seed(storage, 3, owner=OTHER_OWNER, body=f"Other owner {SECRET}")
    _seed(storage, 4, tenant=FOREIGN_TENANT, body=f"Foreign tenant {SECRET}")
    _seed(storage, 5, body=f"Ignored {SECRET}", inbox_status=InboxStatus.IGNORED)
    request = _request()

    before_changes = storage.conn.total_changes
    documents = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    knowledge = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert storage.conn.total_changes == before_changes

    assert (documents.total, documents.examined, documents.matched, documents.returned) == (2, 2, 2, 2)
    assert documents.derivative_current is False
    assert CoverageState.UNAVAILABLE in _coverage(documents, request).states
    assert documents.has_more is False and documents.available is True
    states = {item.review_state: item for item in documents.candidates}
    assert set(states) == {ArchiveReviewState.CONFIRMED, ArchiveReviewState.PENDING}
    assert states[ArchiveReviewState.CONFIRMED].evidence_authority is ArchiveEvidenceAuthority.CANONICAL
    assert states[ArchiveReviewState.PENDING].evidence_authority is ArchiveEvidenceAuthority.NONCANONICAL
    assert states[ArchiveReviewState.PENDING].lifecycle_state is LifecycleState.PENDING
    for candidate in documents.candidates:
        passage = candidate.passages[0]
        locator = passage.passage_ref.locator
        source_id = candidate.resolved_source.source_ref.canonical_object_id
        source_body = {
            confirmed_raw: f"Raw exact {SECRET} confirmed",
            pending_raw: f"Pending exact {SECRET}",
        }[source_id]
        assert source_body[locator.start_char : locator.end_char] == passage.excerpt  # type: ignore[union-attr]
        assert passage.passage_ref.source_revision.representation.kind is RepresentationKind.RAW_OBJECT
        source_number = 1 if source_id == confirmed_raw else 2
        assert (
            passage.passage_ref.source_revision.value
            == hashlib.sha256(f"source-bytes-{source_number}".encode()).hexdigest()
        )

    assert (knowledge.total, knowledge.examined, knowledge.matched, knowledge.returned) == (1, 1, 1, 1)
    assert knowledge.derivative_current is True
    candidate = knowledge.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == confirmed_raw
    assert candidate.passages[0].passage_ref.source_revision.representation.object_id == confirmed_ko
    assert candidate.passages[0].excerpt in f"Knowledge exact {SECRET} canonical"
    raw_revision = next(
        item
        for item in candidate.resolved_source.revisions
        if item.representation.kind is RepresentationKind.RAW_OBJECT
    )
    assert raw_revision.value == hashlib.sha256(b"source-bytes-1").hexdigest()
    assert candidate.review_state is ArchiveReviewState.CONFIRMED
    assert candidate.evidence_authority is ArchiveEvidenceAuthority.CANONICAL

    publicish = json.dumps([documents, knowledge], default=str, ensure_ascii=False)
    for private in (SECRET, confirmed_raw, pending_raw, str(confirmed_ko)):
        assert private not in publicish


def test_focused_lexical_lead_reaches_target_beyond_the_anchor_cap(storage) -> None:
    target_body = "Иванов\nДолжность: ведущий инженер"
    target, _ = _seed(
        storage,
        5000,
        body=target_body,
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for index in range(100):
        _seed(
            storage,
            5100 + index,
            body=f"Иванов — специалист технического отдела {index:03d}",
            received_at=f"2026-08-24T12:{index % 60:02d}:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
        limit=10,
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    passage = page.candidates[0].passages[0]
    locator = passage.passage_ref.locator
    assert passage.excerpt == target_body
    assert target_body[locator.start_char : locator.end_char] == passage.excerpt  # type: ignore[union-attr]
    assert page.has_more is True
    coverage = _coverage(page, request)
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.PARTIAL in coverage.states


def test_focused_lexical_focus_pool_requires_anchor_and_detail(storage) -> None:
    target_body = "Иванов\nДолжность: ведущий инженер"
    target, _ = _seed(
        storage,
        6000,
        body=target_body,
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for index in range(110):
        _seed(
            storage,
            6100 + index,
            body=f"Петров\nДолжность: директор {index:03d}",
            received_at=f"2026-08-26T15:{index % 60:02d}:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.total, page.examined, page.matched, page.returned) == (111, 111, 1, 1)
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert page.has_more is False
    coverage = _coverage(page, request)
    assert CoverageState.CAPPED not in coverage.states


@pytest.mark.parametrize(
    ("anchor", "detail", "value"),
    (
        ("张伟", "职位", "首席工程师"),
        ("Νίκος", "θέση", "μηχανικός"),
        ("أحمد", "المنصب", "مهندس"),
    ),
)
def test_focused_lexical_live_fts_admits_all_script_source_tokens(
    storage,
    anchor: str,
    detail: str,
    value: str,
) -> None:
    body = f"{anchor}\n{detail}: {value}"
    target, _ = _seed(
        storage,
        6050,
        body=body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query=anchor,
        focus=f"{anchor} {detail}",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.matched == page.returned == 1
    candidate = page.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == target
    assert candidate.passages[0].excerpt == body


@pytest.mark.parametrize("detail", ("X", "7"))
def test_focused_lexical_live_fts_keeps_one_character_anchor_and_detail_exact(
    storage,
    detail: str,
) -> None:
    target_body = f"李\n{detail}: инженер"
    target, _ = _seed(
        storage,
        6052,
        body=target_body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        6053,
        body="李\nY: отсутствующий detail",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        6054,
        body=f"王\n{detail}: отсутствующий anchor",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="李",
        focus=detail,
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.matched == page.returned == 1
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert page.candidates[0].passages[0].excerpt == target_body


@pytest.mark.parametrize("stored_anchor", ("ＡＢ", "ⒶⒷ"))
def test_focused_lexical_conservative_lead_recovers_unicode_compatibility_anchor(
    storage,
    stored_anchor: str,
) -> None:
    body = f"{stored_anchor}\nRole: engineer"
    target, _ = _seed(
        storage,
        6051,
        body=body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="AB",
        focus="AB role",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.matched == page.returned == 1
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert page.candidates[0].passages[0].excerpt == body


def test_focused_lexical_requires_every_anchor_term_before_the_cap(storage) -> None:
    target_body = "Иванов проект Альфа\nДолжность: ведущий инженер"
    target, _ = _seed(
        storage,
        6300,
        body=target_body,
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for index in range(110):
        _seed(
            storage,
            6400 + index,
            body=f"Иванов\nДолжность: директор {index:03d}",
            received_at=f"2026-08-27T16:{index % 60:02d}:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов проект Альфа",
        focus="Иванов проект Альфа должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.total, page.examined, page.matched, page.returned) == (111, 111, 1, 1)
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert page.candidates[0].passages[0].excerpt == target_body
    assert page.has_more is False
    assert CoverageState.CAPPED not in _coverage(page, request).states


def test_focused_lexical_keeps_yo_spellings_inside_one_anchor_group(storage) -> None:
    body = "Иванов черных\nДолжность: ведущий инженер"
    target, _ = _seed(
        storage,
        7000,
        body=body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов чёрных",
        focus="Иванов чёрных должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.matched == page.returned == 1
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert page.candidates[0].passages[0].excerpt == body


def test_focused_lexical_ninth_anchor_cannot_join_an_adjacent_record(storage) -> None:
    anchors = tuple(f"anchor{index:02d}" for index in range(1, 10))
    _seed(
        storage,
        7100,
        body=(f"{' '.join(anchors[:8])}\nДолжность: ведущий инженер\n{anchors[8]}"),
        inbox_status=InboxStatus.CLASSIFIED,
    )
    query = " ".join(anchors)
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query=query,
        focus=f"{query} должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 0, 0)
    assert page.candidates == ()


def test_focused_lexical_foreign_fts_rows_cannot_change_authorized_leads(storage) -> None:
    newer, _ = _seed(
        storage,
        6600,
        body="Иванов\nДолжность: ведущий инженер",
        received_at="2026-01-02T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    older, _ = _seed(
        storage,
        6601,
        body="Иванов\nДолжность: системный архитектор",
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    def search_ids() -> tuple[tuple[str, ...], bool]:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )
        return (
            tuple(item.resolved_source.source_ref.canonical_object_id for item in page.candidates),
            CoverageState.CAPPED in _coverage(page, request).states,
        )

    before = search_ids()
    for index in range(100):
        _seed(
            storage,
            6700 + index,
            tenant=FOREIGN_TENANT,
            owner=OTHER_OWNER,
            body=("Иванов " * (1 + index % 7)) + "\nДолжность: чужой источник",
            received_at=f"2026-08-28T17:{index % 60:02d}:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        after = search_ids()
    finally:
        storage.conn.set_trace_callback(None)

    assert before == after == ((newer, older), False)
    query = next(item for item in statements if "focus_pool AS MATERIALIZED" in item)
    assert "bm25(raw_fts)" not in query


@pytest.mark.parametrize("drift", ("delete-all", "stale-row"))
def test_focused_lexical_unattested_fts_miss_cannot_establish_absence(
    drift: str,
    storage,
) -> None:
    target, _ = _seed(
        storage,
        7200,
        body="Иванов\nДолжность: ведущий инженер",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    expected_total = 1
    if drift == "stale-row":
        _seed(
            storage,
            7201,
            body="Иванов",
            inbox_status=InboxStatus.CLASSIFIED,
            text_extraction_success=True,
        )
        expected_total = 2
    report = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=expected_total,
        include_document_passages=True,
    )
    assert report["passage_changed"] == expected_total
    with storage.transaction() as conn:
        if drift == "delete-all":
            conn.execute("INSERT INTO raw_fts(raw_fts) VALUES('delete-all')")
        else:
            row = conn.execute(
                "SELECT rowid, raw_content FROM raw_objects WHERE id=? AND user_id=?",
                (target, TENANT),
            ).fetchone()
            assert row is not None
            conn.execute(
                "INSERT INTO raw_fts(raw_fts,rowid,raw_content) VALUES('delete',?,?)",
                (row["rowid"], row["raw_content"]),
            )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.total, page.examined, page.matched, page.returned) == (
        expected_total,
        expected_total,
        0,
        0,
    )
    assert page.derivative_current is False
    assert page.derivative_unavailable is True
    coverage = _coverage(page, request)
    assert CoverageState.UNAVAILABLE in coverage.states
    assert coverage.absence_decision().value == "not_established"


def test_focused_lexical_surviving_hit_cannot_claim_stale_fts_complete(storage) -> None:
    removed, _ = _seed(
        storage,
        7202,
        body="Иванов\nДолжность: ведущий инженер",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    surviving, _ = _seed(
        storage,
        7203,
        body="Иванов\nДолжность: системный архитектор",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    report = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=2,
        include_document_passages=True,
    )
    assert report["passage_changed"] == 2
    with storage.transaction() as conn:
        row = conn.execute(
            "SELECT rowid, raw_content FROM raw_objects WHERE id=? AND user_id=?",
            (removed, TENANT),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO raw_fts(raw_fts,rowid,raw_content) VALUES('delete',?,?)",
            (row["rowid"], row["raw_content"]),
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.total, page.examined, page.matched, page.returned) == (2, 2, 1, 1)
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == surviving
    assert page.derivative_current is False
    assert page.derivative_unavailable is True
    coverage = _coverage(page, request)
    assert CoverageState.UNAVAILABLE in coverage.states
    assert coverage.absence_decision().value == "evidence_found"


def test_focused_lexical_projection_rejects_predicate_only_and_far_join(storage) -> None:
    _seed(
        storage,
        5300,
        body="Должность: посторонний предикат без искомой фамилии",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        5301,
        body=(
            "Иванов\n"
            + ("нейтральный раздел без кадровых сведений\n" * 30)
            + "Петров\nДолжность: генеральный директор"
        ),
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        5302,
        body="Иванов\nПетров Должность: генеральный директор",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        5303,
        body="Петров Должность: генеральный директор\nИванов",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.examined == 4
    assert page.matched == page.returned == 0
    assert page.candidates == ()


def test_focused_lexical_passage_uses_the_projector_exact_span(storage) -> None:
    body = "Служебная преамбула\n\nИванов\nДолжность: ведущий инженер\n\nПетров\nДолжность: директор"
    raw_id, _ = _seed(
        storage,
        5400,
        body=body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.matched == page.returned == 1
    candidate = page.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == raw_id
    passage = candidate.passages[0]
    locator = passage.passage_ref.locator
    assert passage.excerpt == "Иванов\nДолжность: ведущий инженер"
    assert body[locator.start_char : locator.end_char] == passage.excerpt  # type: ignore[union-attr]
    assert passage.passage_ref.passage_index_version == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    assert "Петров" not in passage.excerpt


def test_focused_lexical_passage_uses_v2_only_for_a_containing_authenticated_child(
    storage,
) -> None:
    body = "Иванов\nДолжность: ведущий инженер"
    _seed(
        storage,
        5450,
        body=body,
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    report = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=1,
        include_document_passages=True,
    )
    assert report["passage_changed"] == 1
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    passage = page.candidates[0].passages[0]
    locator = passage.passage_ref.locator
    assert passage.passage_ref.passage_index_version == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    assert body[locator.start_char : locator.end_char] == passage.excerpt  # type: ignore[union-attr]


def test_no_focus_keeps_the_ordinary_lexical_sql_path(storage) -> None:
    _seed(
        storage,
        5500,
        body="Needle ordinary lexical source",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,), query="Needle")
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )
    finally:
        storage.conn.set_trace_callback(None)

    query = next(item for item in statements if "lexical_needles AS MATERIALIZED" in item)
    assert "focus_ranked AS MATERIALIZED" not in query
    assert page.matched == page.returned == 1


def test_focused_lexical_fts_authorizes_before_the_sentinel_and_body_projection(
    storage,
) -> None:
    target_body = "Иванов\nДолжность: ведущий инженер"
    target, _ = _seed(
        storage,
        5600,
        body=target_body,
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for index in range(110):
        if index % 2:
            _seed(
                storage,
                5700 + index,
                tenant=FOREIGN_TENANT,
                owner=OTHER_OWNER,
                body="Иванов\nДолжность: чужой источник",
                received_at=f"2026-08-25T14:{index % 60:02d}:00+00:00",
                inbox_status=InboxStatus.CLASSIFIED,
            )
        else:
            _seed(
                storage,
                5700 + index,
                body="Иванов\nДолжность: отклонённый источник",
                received_at=f"2026-08-25T14:{index % 60:02d}:00+00:00",
                inbox_status=InboxStatus.IGNORED,
            )
    fold_calls: list[object] = []

    def monitored_fold(value: object) -> str:
        fold_calls.append(value)
        return archive_document_storage._archive_search_fold(value)  # noqa: SLF001

    storage.conn.create_function("friday_archive_fold", 1, monitored_fold, deterministic=True)
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )
    finally:
        storage.conn.set_trace_callback(None)

    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert fold_calls == ["Ёж-Archive-Probe"]
    query = next(item for item in statements if "focus_pool AS MATERIALIZED" in item)
    sentinel = query.index("LIMIT 101")
    authorized = query.index("JOIN authorized_sources s ON s.raw_rowid=f.raw_rowid")
    assert sentinel < authorized
    assert "raw_content AS passage_body" not in query
    body_query = next(item for item in statements if "passage_body_blob" in item)
    assert statements.index(query) < statements.index(body_query)
    assert "substr(CAST(raw_content AS BLOB),1," in body_query
    assert "version=" in body_query and "content_hash=" in body_query
    assert "friday_archive_fold(s.passage_body)" not in query


def test_focused_lexical_body_reads_obey_aggregate_and_per_source_budgets(
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "Иванов\nДолжность: ведущий инженер\n\n" + ("нейтральный раздел\n" * 10)
    body_bytes = len(body.encode())
    aggregate_budget = body_bytes * 2 + 10
    oversized_body = "Иванов\nДолжность: ведущий инженер\n\n" + ("слишком большой раздел\n" * 20)
    assert len(oversized_body.encode()) > 500
    _seed(
        storage,
        7399,
        body=oversized_body,
        received_at="2026-08-29T17:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for index in range(12):
        _seed(
            storage,
            7400 + index,
            body=body,
            received_at=f"2026-08-29T18:{index:02d}:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    monkeypatch.setattr(archive_document_storage, "_FOCUSED_DOCUMENT_BODY_MAX_BYTES", 500)
    monkeypatch.setattr(
        archive_document_storage,
        "_FOCUSED_DOCUMENT_BODY_BUDGET_BYTES",
        aggregate_budget,
    )
    projected_body_bytes: list[int] = []
    original_projector = archive_document_storage.project_source_focus

    def monitored_projector(body: str, *args: object, **kwargs: object):
        projected_body_bytes.append(len(body.encode()))
        return original_projector(body, *args, **kwargs)

    monkeypatch.setattr(archive_document_storage, "project_source_focus", monitored_projector)
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )
    finally:
        storage.conn.set_trace_callback(None)

    assert projected_body_bytes == [body_bytes, body_bytes]
    assert sum(projected_body_bytes) <= aggregate_budget
    assert all(item <= 500 for item in projected_body_bytes)
    assert (page.matched, page.returned) == (2, 2)
    assert page.has_more is True
    coverage = _coverage(page, request)
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.UNAVAILABLE in coverage.states
    assert coverage.limit == request.limit == 20
    lead_query = next(item for item in statements if "focus_pool AS MATERIALIZED" in item)
    assert "raw_content AS passage_body" not in lead_query
    body_queries = [item for item in statements if "passage_body_blob" in item]
    assert len(body_queries) == 3
    assert all("length(CAST(raw_content AS BLOB)) BETWEEN 1 AND" in item for item in body_queries)


def test_focused_lexical_failed_body_reads_consume_the_attempt_budget(
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(12):
        _seed(
            storage,
            7450 + index,
            body="Иванов\nДолжность: ведущий инженер\n\n" + ("oversized\n" * 80),
            received_at=f"2026-08-29T20:{index:02d}:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    monkeypatch.setattr(archive_document_storage, "_FOCUSED_DOCUMENT_BODY_MAX_BYTES", 100)
    monkeypatch.setattr(archive_document_storage, "_FOCUSED_DOCUMENT_BODY_BUDGET_BYTES", 400)
    projector_calls = 0

    def forbidden_projector(*_args: object, **_kwargs: object) -> None:
        nonlocal projector_calls
        projector_calls += 1
        raise AssertionError("oversized body reached the exact projector")

    monkeypatch.setattr(archive_document_storage, "project_source_focus", forbidden_projector)
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )
    finally:
        storage.conn.set_trace_callback(None)

    body_queries = [item for item in statements if "passage_body_blob" in item]
    assert projector_calls == 0
    assert len(body_queries) == 4
    assert all("length(CAST(raw_content AS BLOB)) BETWEEN 1 AND 100" in item for item in body_queries)
    assert page.candidates == ()
    assert page.has_more is True
    coverage = _coverage(page, request)
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.UNAVAILABLE in coverage.states


def test_focused_lexical_oversize_zero_return_keeps_the_applied_request_limit(
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(
        storage,
        7500,
        body="Иванов\nДолжность: ведущий инженер\n\n" + ("крупный раздел\n" * 20),
        inbox_status=InboxStatus.CLASSIFIED,
    )
    monkeypatch.setattr(archive_document_storage, "_FOCUSED_DOCUMENT_BODY_MAX_BYTES", 100)
    monkeypatch.setattr(archive_document_storage, "_FOCUSED_DOCUMENT_BODY_BUDGET_BYTES", 200)
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.matched, page.returned) == (0, 0)
    assert page.has_more is True
    assert page.applied_limit == request.limit == 20
    coverage = _coverage(page, request)
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.UNAVAILABLE in coverage.states
    assert coverage.limit == 20
    assert coverage.absence_decision().value == "not_established"


def test_focused_lexical_missing_raw_fts_fails_closed_without_a_body_scan(storage) -> None:
    _seed(
        storage,
        5900,
        body="Иванов\nДолжность: ведущий инженер",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Иванов",
        focus="Иванов должность",
    )
    with storage.transaction() as conn:
        conn.execute("DROP TABLE raw_fts")

    with pytest.raises(ArchiveDocumentStorageError, match="lexical index is unavailable"):
        _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )


def test_foreign_corpus_cannot_change_authorized_membership_or_lane_ranks(storage) -> None:
    _seed(
        storage,
        10,
        body="Needle first",
        received_at="2026-08-23T10:10:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        11,
        body="Needle second",
        received_at="2026-08-23T10:11:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    before = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    for number in range(100, 112):
        _seed(
            storage,
            number,
            tenant=FOREIGN_TENANT,
            owner=OTHER_OWNER,
            body="Needle Needle Needle foreign corpus mutation",
        )
    for number in range(112, 118):
        _seed(
            storage,
            number,
            owner=OTHER_OWNER,
            body="Needle same tenant but foreign principal mutation",
        )
    after = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert after == before
    assert tuple(item.matches[0].rank for item in after.candidates) == (1, 2)


def test_body_matched_filename_terms_break_a_capped_tie_without_bypassing_the_cap(
    storage,
) -> None:
    target, _ = _seed(
        storage,
        1200,
        filename="nebula-budget.md",
        body="Orchid nebula budget reconciliation uses a reserve ledger.",
        received_at="2026-01-12T09:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for index in (*range(13, 26), *range(1, 13)):
        _seed(
            storage,
            1200 + index,
            filename="nebula-only.txt" if index == 1 else f"decoy-{index:02d}.txt",
            body=f"Nebula budget decoy {index:02d} contains no relevant evidence.",
            received_at=f"2026-06-{index:02d}T10:00:00+00:00",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="nebula budget",
    )

    first = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    second = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    permuted = _search(
        storage,
        request=_request(
            corpora=(ArchiveSearchCorpus.DOCUMENTS,),
            query="budget nebula",
        ),
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (first.total, first.examined, first.matched, first.returned) == (26, 26, 26, 20)
    assert first.has_more is True
    assert first.candidates[0].resolved_source.source_ref.canonical_object_id == target
    assert tuple(item.resolved_source.source_ref.canonical_object_id for item in first.candidates) == tuple(
        item.resolved_source.source_ref.canonical_object_id for item in second.candidates
    )
    assert tuple(item.resolved_source.source_ref.canonical_object_id for item in first.candidates) == tuple(
        item.resolved_source.source_ref.canonical_object_id for item in permuted.candidates
    )
    coverage = _coverage(first, request)
    assert coverage.states == (
        CoverageState.CAPPED,
        CoverageState.PARTIAL,
        CoverageState.UNAVAILABLE,
    )
    assert coverage.next_cursor_available is False
    assert coverage.absence_decision().value == "evidence_found"


def test_filename_terms_never_create_lexical_evidence_or_cross_authority_scope(
    storage,
) -> None:
    filename_only, _ = _seed(
        storage,
        1230,
        filename="nebula-budget.md",
        body="Unrelated owner body.",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    owner_match, _ = _seed(
        storage,
        1231,
        filename="owner-document.txt",
        body="Owner nebula budget evidence.",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        1232,
        tenant=FOREIGN_TENANT,
        owner=OTHER_OWNER,
        filename="nebula-budget.md",
        body="Foreign nebula budget evidence.",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        1233,
        owner=OTHER_OWNER,
        filename="nebula-budget.md",
        body="Other principal nebula budget evidence.",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        1234,
        filename="nebula-budget.md",
        body="Ignored nebula budget evidence.",
        inbox_status=InboxStatus.IGNORED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="nebula budget",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    source_ids = tuple(item.resolved_source.source_ref.canonical_object_id for item in page.candidates)
    assert source_ids == (owner_match,)
    assert filename_only not in source_ids
    assert (page.total, page.examined, page.matched, page.returned) == (2, 2, 1, 1)


def test_duplicate_filename_metadata_cannot_win_a_lexical_format_tie(storage) -> None:
    malformed, _ = _seed(
        storage,
        1240,
        filename="placeholder.pdf",
        body="Nebula budget malformed metadata evidence.",
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    clean, _ = _seed(
        storage,
        1241,
        filename="clean-document.pdf",
        body="Nebula budget clean evidence.",
        received_at="2026-02-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    duplicate_metadata = (
        '{"filename":"nebula-budget.md","filename":"ignored.pdf",'
        f'"mime_type":"application/pdf","media_kind":"document","uploaded_by":"{OWNER}"}}'
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (duplicate_metadata, malformed),
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="nebula budget",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == clean
    assert page.candidates[1].resolved_source.source_ref.canonical_object_id == malformed
    coverage = _coverage(page, request)
    assert CoverageState.BACKFILL_PENDING in coverage.states
    assert CoverageState.PARTIAL in coverage.states
    assert coverage.absence_decision().value == "evidence_found"


@pytest.mark.parametrize(
    "unsafe_filename",
    (
        " nebula-budget.md",
        "nebula-budget.md\nprivate",
        "nebula-budget.md\x00private",
        "n" * 261,
        7,
    ),
    ids=("leading-space", "newline", "nul", "oversized", "wrong-type"),
)
def test_unsafe_filename_cannot_win_a_lexical_format_tie(
    storage,
    unsafe_filename: object,
) -> None:
    unsafe, _ = _seed(
        storage,
        1250,
        filename="placeholder.pdf",
        body="Nebula budget unsafe filename evidence.",
        received_at="2026-01-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    clean, _ = _seed(
        storage,
        1251,
        filename="clean-document.pdf",
        body="Nebula budget clean filename evidence.",
        received_at="2026-02-01T00:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    metadata = json.dumps(
        {
            "filename": unsafe_filename,
            "media_kind": "document",
            "mime_type": "application/pdf",
            "uploaded_by": OWNER,
        }
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (metadata, unsafe),
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="nebula budget",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert tuple(item.resolved_source.source_ref.canonical_object_id for item in page.candidates) == (
        clean,
        unsafe,
    )
    coverage = _coverage(page, request)
    assert CoverageState.UNAVAILABLE in coverage.states
    assert CoverageState.PARTIAL in coverage.states


def test_filename_boost_sql_requires_a_nonempty_canonical_lexical_term(storage) -> None:
    _seed(
        storage,
        1260,
        filename="---",
        body="---",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="---",
    )

    sql, _parameters = archive_document_storage._lexical_sql(  # noqa: SLF001
        ArchiveSearchCorpus.DOCUMENTS,
        request,
        derivative_available=False,
    )
    assert "WHEN EXISTS (SELECT 1 FROM lexical_needles)" in sql
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.matched == page.returned == 0
    assert page.candidates == ()


def test_catalog_navigation_is_body_free_stably_ordered_and_honestly_capped(storage) -> None:
    raw_exact, _ = _seed(
        storage,
        20,
        filename="report",
        body="body exact without query",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    raw_prefix, _ = _seed(
        storage,
        21,
        filename="report alpha.pdf",
        body="body prefix",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    raw_substring, _ = _seed(
        storage,
        22,
        filename="annual report.pdf",
        body="body substring",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    raw_alias, _ = _seed(
        storage,
        23,
        filename="opaque-name.bin",
        body="body alias",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                TENANT,
                OWNER,
                "telegram-file:alias-23",
                raw_alias,
                "report alias.pdf",
                "2026-08-23T14:00:00+00:00",
            ),
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="report",
        limit=2,
    )

    statements: list[str] = []
    body_reads: list[tuple[str, str]] = []

    def authorizer(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table is not None and column is not None:
            body_reads.append((table, column))
        return sqlite3.SQLITE_OK

    storage.conn.set_trace_callback(statements.append)
    storage.conn.set_authorizer(authorizer)
    storage.conn.execute("BEGIN")
    try:
        first = search_archive_document_lane(
            storage.conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
            execution_binding=_binding(
                request,
                (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG),
            ),
            snapshot_discriminator=SNAPSHOT,
            snapshot_current=True,
            limit=2,
        )
        assert storage.conn.in_transaction is True
    finally:
        storage.conn.rollback()
        storage.conn.set_trace_callback(None)
        storage.conn.set_authorizer(None)

    assert (first.total, first.examined, first.matched, first.returned) == (4, 4, 4, 2)
    assert first.has_more
    assert all(item.navigation_only and not item.passages for item in first.candidates)
    assert first.candidates[0].filename == "report"
    assert raw_exact not in repr(first)
    assert not any(
        re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", sql, re.I) for sql in statements
    )
    assert ("raw_objects", "raw_content") not in body_reads
    assert ("knowledge_objects", "content") not in body_reads

    source_ids = tuple(item.resolved_source.source_ref.canonical_object_id for item in first.candidates)
    assert len(source_ids) == len(set(source_ids)) == 2
    assert set(source_ids) <= {raw_exact, raw_prefix, raw_substring, raw_alias}

    coverage = _coverage(first, request)
    assert coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.CAPPED,
        CoverageState.PARTIAL,
    )
    assert coverage.eligible_authorized == coverage.examined == 4
    assert coverage.matched_at_least == 4 and coverage.returned == 2
    assert coverage.next_cursor_available is False


def test_current_exact_semantic_title_is_body_free_navigation_not_evidence(
    storage,
) -> None:
    raw_id, _ = _seed(
        storage,
        24,
        filename="opaque-24.bin",
        body="# Quarterly Solstice Ledger\nAuthoritative raw body phrase 5521",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    _install_test_document_catalog(storage)
    _store_test_document_catalog_row(
        storage,
        raw_id,
        semantic_title="Quarterly Solstice Ledger",
    )
    catalog_request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Quarterly Solstice Ledger",
    )
    reads: list[tuple[str, str]] = []

    def authorizer(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table is not None and column is not None:
            reads.append((table, column))
        return sqlite3.SQLITE_OK

    storage.conn.set_authorizer(authorizer)
    try:
        catalog = _search(
            storage,
            request=catalog_request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
        )
    finally:
        storage.conn.set_authorizer(None)

    assert (catalog.total, catalog.examined, catalog.matched, catalog.returned) == (1, 1, 1, 1)
    assert catalog.catalog_projection_current is True
    candidate = catalog.candidates[0]
    assert candidate.title == "Quarterly Solstice Ledger"
    assert candidate.evidence_authority is ArchiveEvidenceAuthority.NAVIGATION_ONLY
    assert candidate.navigation_only is True
    assert candidate.passages == ()
    assert _coverage(catalog, catalog_request).states == (CoverageState.COMPLETE,)
    assert ("document_catalog", "semantic_title") in reads
    assert ("raw_objects", "raw_content") not in reads
    assert ("knowledge_objects", "content") not in reads

    reads.clear()
    storage.conn.set_authorizer(authorizer)
    try:
        semantic_lexical = _search(
            storage,
            request=catalog_request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
        )
    finally:
        storage.conn.set_authorizer(None)
    assert semantic_lexical.matched == semantic_lexical.returned == 1
    assert semantic_lexical.candidates[0].evidence_authority is ArchiveEvidenceAuthority.CANONICAL
    assert semantic_lexical.candidates[0].passages
    assert not any(table == "document_catalog" for table, _column in reads)

    body_request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Authoritative raw body phrase 5521",
    )
    body_hit = _search(
        storage,
        request=body_request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    ).candidates[0]
    assert body_hit.evidence_authority is ArchiveEvidenceAuthority.CANONICAL
    assert "Authoritative raw body phrase 5521" in body_hit.passages[0].excerpt


@pytest.mark.parametrize(
    "projection_state",
    ["missing", "stale_version", "stale_hash", "incomplete", "invalid_title"],
)
def test_noncurrent_document_catalog_projection_is_distinct_backfill_coverage(
    storage,
    monkeypatch: pytest.MonkeyPatch,
    projection_state: str,
) -> None:
    raw_id, _ = _seed(
        storage,
        25,
        filename="opaque-25.bin",
        body="# Projected private navigation label\nUnrelated authoritative body",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    _install_test_document_catalog(storage)
    if projection_state == "missing":
        _delete_test_document_catalog_row(storage, raw_id)
    else:
        if projection_state in {"stale_version", "stale_hash", "invalid_title"}:
            _replace_with_loose_test_document_catalog(storage)
        _store_test_document_catalog_row(
            storage,
            raw_id,
            semantic_title=(
                "Projected private navigation label\t"
                if projection_state == "invalid_title"
                else "Projected private navigation label"
            ),
            status="incomplete" if projection_state == "incomplete" else "current",
            stale_version=projection_state == "stale_version",
            stale_hash=projection_state == "stale_hash",
        )
    if projection_state in {"stale_version", "stale_hash", "invalid_title"}:
        monkeypatch.setattr(
            archive_document_storage,
            "_document_catalog_contract",
            lambda _conn: (True, 1),
        )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Projected private navigation label",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    coverage = _coverage(page, request)

    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 0, 0)
    assert page.authority_scope_complete is True
    assert page.catalog_projection_current is False
    assert coverage.eligible_authorized == 1
    assert coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert coverage.absence_decision().value == "not_established"


@pytest.mark.parametrize("schema_state", ["missing", "counterfeit"])
def test_missing_or_counterfeit_catalog_schema_keeps_raw_navigation_but_not_absence_authority(
    storage,
    schema_state: str,
) -> None:
    raw_id, _ = _seed(
        storage,
        26,
        filename="authoritative-filename-26.pdf",
        body="Authoritative body 26",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    with storage.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS document_catalog")
        if schema_state == "counterfeit":
            conn.execute("CREATE TABLE document_catalog(opaque_future_shape TEXT)")
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="authoritative-filename-26.pdf",
    )
    reads: list[tuple[str, str]] = []

    def authorizer(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table is not None and column is not None:
            reads.append((table, column))
        return sqlite3.SQLITE_OK

    storage.conn.set_authorizer(authorizer)
    try:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
        )
    finally:
        storage.conn.set_authorizer(None)
    coverage = _coverage(page, request)

    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == raw_id
    assert page.authority_scope_complete is True
    assert page.catalog_projection_current is False
    assert coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert coverage.absence_decision().value == "evidence_found"
    assert not any(table == "document_catalog" for table, _column in reads)

    absent_request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="not present in authoritative filename",
    )
    absent = _search(
        storage,
        request=absent_request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    assert absent.matched == absent.returned == 0
    assert _coverage(absent, absent_request).absence_decision().value == "not_established"


def test_catalog_projection_join_is_authorized_first_indexed_and_bounded(
    storage,
) -> None:
    raw_id, _ = _seed(
        storage,
        27,
        filename="bounded-27.pdf",
        body="# Bounded catalog title\nBounded body",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    _install_test_document_catalog(storage)
    _store_test_document_catalog_row(storage, raw_id, semantic_title="Bounded catalog title")
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Bounded catalog title",
        limit=2,
    )
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        page = _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
        )
    finally:
        storage.conn.set_trace_callback(None)
    query = next(item for item in statements if "document_catalog_joined AS MATERIALIZED" in item)
    plan = tuple(str(row[3]) for row in storage.conn.execute("EXPLAIN QUERY PLAN " + query).fetchall())

    assert page.returned == 1
    assert query.index("authorized_sources AS MATERIALIZED") < query.index(
        "document_catalog_joined AS MATERIALIZED"
    )
    assert query.index("document_catalog_joined AS MATERIALIZED") < query.index(
        "LEFT JOIN document_catalog dc"
    )
    assert "LIMIT 3" in query
    assert any("SEARCH dc USING INDEX" in detail and "raw_object_id=?" in detail for detail in plan), plan
    assert not any("SCAN dc" in detail for detail in plan), plan


def test_foreign_and_ignored_catalog_rows_cannot_match_or_degrade_owner_scope(
    storage,
) -> None:
    visible_id, _ = _seed(
        storage,
        28,
        filename="visible-owner.pdf",
        body="# Visible owner title\nVisible owner body",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    foreign_id, _ = _seed(
        storage,
        29,
        tenant=TENANT,
        owner=OTHER_OWNER,
        filename="foreign-owner.pdf",
        body="# Foreign projected secret 7721\nForeign owner body",
        inbox_status=InboxStatus.CLASSIFIED,
        text_extraction_success=True,
    )
    _seed(
        storage,
        30,
        filename="ignored-owner.pdf",
        body="Ignored owner body",
        inbox_status=InboxStatus.IGNORED,
    )
    _install_test_document_catalog(storage)
    _store_test_document_catalog_row(storage, visible_id, semantic_title="Visible owner title")
    _store_test_document_catalog_row(
        storage,
        foreign_id,
        semantic_title="Foreign projected secret 7721",
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Foreign projected secret 7721",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    coverage = _coverage(page, request)

    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 0, 0)
    assert page.catalog_projection_current is True
    assert coverage.states == (CoverageState.COMPLETE,)
    assert coverage.absence_decision().value == "authorized_absence_confirmed"


def test_internal_document_materialization_can_exceed_public_request_limit(storage) -> None:
    for number in range(30, 33):
        _seed(
            storage,
            number,
            filename=f"Needle {number}.md",
            body=f"Needle body {number}",
            inbox_status=InboxStatus.CLASSIFIED,
        )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,), limit=1)

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
        limit=3,
    )

    assert request.limit == 1
    assert page.returned == len(page.candidates) == 3
    assert page.applied_limit == 3
    with pytest.raises(ArchiveDocumentStorageError, match="page limit"):
        _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.LEXICAL,
            limit=21,
        )


def test_review_and_lifecycle_filters_are_applied_to_the_authorized_total(storage) -> None:
    _seed(
        storage,
        30,
        filename="needle-confirmed.pdf",
        body="Needle confirmed",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        31,
        filename="needle-pending.pdf",
        body="Needle pending",
        inbox_status=InboxStatus.PENDING,
    )
    _seed(
        storage,
        32,
        filename="needle-archived.pdf",
        body="Needle archived raw",
        knowledge_content="Needle archived knowledge",
        knowledge_title="Needle archive",
        knowledge_lifecycle="archived",
    )
    _seed(
        storage,
        33,
        filename="needle-unreviewed.pdf",
        body="Needle unreviewed",
    )
    discoverable = _request()
    confirmed = _request(review_scope=ReviewScope.CONFIRMED_ONLY)
    pending_only = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        lifecycle_constraints=(
            ArchiveLifecycleConstraint.create(
                ArchiveSearchCorpus.DOCUMENTS,
                (LifecycleState.PENDING,),
            ),
        ),
    )

    document_confirmed = _search(
        storage,
        request=confirmed,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    assert (document_confirmed.total, document_confirmed.examined) == (None, 1)
    assert all(item.review_state is ArchiveReviewState.CONFIRMED for item in document_confirmed.candidates)
    confirmed_coverage = _coverage(document_confirmed, confirmed)
    assert confirmed_coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )

    document_pending = _search(
        storage,
        request=pending_only,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert (document_pending.total, document_pending.examined, document_pending.matched) == (1, 1, 1)
    assert document_pending.candidates[0].review_state is ArchiveReviewState.PENDING
    assert document_pending.candidates[0].lifecycle_state is LifecycleState.PENDING

    knowledge_discoverable = _search(
        storage,
        request=discoverable,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert knowledge_discoverable.total == 1
    assert knowledge_discoverable.candidates[0].review_state is ArchiveReviewState.ARCHIVED
    assert knowledge_discoverable.candidates[0].lifecycle_state is LifecycleState.ARCHIVED

    knowledge_confirmed = _search(
        storage,
        request=confirmed,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert knowledge_confirmed.total == knowledge_confirmed.matched == 0
    assert knowledge_confirmed.candidates == ()


def test_latest_inbox_state_wins_over_an_older_review_timestamp(storage) -> None:
    raw_id, _ = _seed(
        storage,
        34,
        filename="needle-review.pdf",
        body="Needle review body",
        received_at="2026-08-23T08:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    storage.store_inbox_item(
        InboxItem(
            id=_opaque("inbox", 340),
            user_id=TENANT,
            raw_object_id=raw_id,
            status=InboxStatus.PENDING,
            created_at="2026-08-23T14:00:00+00:00",
        )
    )
    revived_raw, _ = _seed(
        storage,
        35,
        filename="needle-revived.pdf",
        body="Needle revived body",
        received_at="2026-08-23T08:30:00+00:00",
        inbox_status=InboxStatus.IGNORED,
    )
    storage.store_inbox_item(
        InboxItem(
            id=_opaque("inbox", 350),
            user_id=TENANT,
            raw_object_id=revived_raw,
            status=InboxStatus.CLASSIFIED,
            created_at="2026-08-23T15:00:00+00:00",
            reviewed_at="2026-08-23T15:01:00+00:00",
            reviewed_by=OWNER,
        )
    )

    page = _search(
        storage,
        request=_request(corpora=(ArchiveSearchCorpus.DOCUMENTS,)),
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    by_raw = {item.resolved_source.source_ref.canonical_object_id: item for item in page.candidates}
    candidate = by_raw[raw_id]
    assert candidate.review_state is ArchiveReviewState.PENDING
    assert candidate.lifecycle_state is LifecycleState.PENDING
    assert candidate.evidence_authority is ArchiveEvidenceAuthority.NONCANONICAL
    assert by_raw[revived_raw].review_state is ArchiveReviewState.CONFIRMED


def test_unknown_current_lifecycle_is_backfill_and_never_false_absence(storage) -> None:
    inbox_raw, _ = _seed(
        storage,
        36,
        body="Needle with an unknown current review state",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _knowledge_raw, knowledge_id = _seed(
        storage,
        37,
        body="source",
        knowledge_content="Needle with an unknown Knowledge lifecycle",
    )
    assert knowledge_id is not None
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE inbox SET status='future-review-state' WHERE raw_object_id=?",
            (inbox_raw,),
        )
        conn.execute(
            "UPDATE knowledge_objects SET lifecycle_stage='future-lifecycle' WHERE id=?",
            (knowledge_id,),
        )

    document_request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    documents = _search(
        storage,
        request=document_request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert (documents.total, documents.examined, documents.matched) == (None, 0, 0)
    assert _coverage(documents, document_request).absence_decision().value == "not_established"

    knowledge_request = _request(corpora=(ArchiveSearchCorpus.KNOWLEDGE,))
    knowledge = _search(
        storage,
        request=knowledge_request,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert (knowledge.total, knowledge.examined, knowledge.matched) == (None, 0, 0)
    knowledge_coverage = _coverage(knowledge, knowledge_request)
    assert knowledge_coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )
    assert knowledge_coverage.absence_decision().value == "not_established"


def test_superseded_active_ko_cannot_publish_stale_factual_evidence(storage) -> None:
    raw_id, stale_ko = _seed(
        storage,
        38,
        body="source",
        knowledge_content="Needle only in the stale projection",
    )
    assert stale_ko is not None
    replacement = "ko_current_replacement_projection"
    storage.store_knowledge_object(
        KnowledgeObject(
            id=replacement,
            user_id=TENANT,
            raw_object_id=raw_id,
            content="current unrelated projection",
            content_type="document",
            title="Current projection",
            lifecycle_stage="active",
            version=2,
            created_at="2026-08-23T19:00:00+00:00",
            updated_at="2026-08-23T19:00:00+00:00",
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET superseded_by_id=? WHERE id=?",
            (replacement, stale_ko),
        )

    request = _request(corpora=(ArchiveSearchCorpus.KNOWLEDGE,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (None, 1, 0, 0)
    coverage = _coverage(page, request)
    assert coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )
    assert coverage.absence_decision().value == "not_established"


def test_knowledge_lane_covers_non_file_sources_and_does_not_collapse_independent_kos(storage) -> None:
    first_raw, matching_ko = _seed(
        storage,
        60,
        filename="multi-ko.pdf",
        body="source registration",
        knowledge_content="Needle in the lower-version independent KO",
        knowledge_title="Matching projection",
    )
    assert matching_ko is not None
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko_unrelated_higher_version",
            user_id=TENANT,
            raw_object_id=first_raw,
            content="unrelated projection",
            content_type="document",
            title="Unrelated projection",
            lifecycle_stage="active",
            version=999,
            created_at="2026-08-23T15:00:00+00:00",
            updated_at="2026-08-23T15:00:00+00:00",
        )
    )
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko_deprecated_matching_projection",
            user_id=TENANT,
            raw_object_id=first_raw,
            content="Needle in a deprecated projection",
            content_type="document",
            title="Needle",
            lifecycle_stage="deprecated",
            version=1000,
            created_at="2026-08-23T15:10:00+00:00",
            updated_at="2026-08-23T15:10:00+00:00",
        )
    )
    second_raw = _opaque("raw", 61)
    second_body = "Needle in ordinary captured web knowledge"
    storage.store_raw_object(
        RawObject(
            id=second_raw,
            user_id=TENANT,
            source="web",
            source_ref="https://example.invalid/audit",
            raw_content=second_body,
            content_type="text",
            metadata_json={"uploaded_by": OWNER},
            content_hash=hashlib.sha256(second_body.encode("utf-8")).hexdigest(),
            received_at="2026-08-23T16:00:00+00:00",
            created_at="2026-08-23T16:00:00+00:00",
        )
    )
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko_non_file_web_capture",
            user_id=TENANT,
            raw_object_id=second_raw,
            content=second_body,
            content_type="web",
            title="Captured page",
            lifecycle_stage="active",
            version=1,
            created_at="2026-08-23T16:01:00+00:00",
            updated_at="2026-08-23T16:01:00+00:00",
        )
    )
    foreign_raw = _opaque("raw", 62)
    storage.store_raw_object(
        RawObject(
            id=foreign_raw,
            user_id=TENANT,
            source="web",
            source_ref="https://example.invalid/foreign",
            raw_content="Needle belongs to another principal",
            content_type="text",
            metadata_json={"uploaded_by": OTHER_OWNER},
            content_hash=hashlib.sha256(b"foreign-knowledge").hexdigest(),
            received_at="2026-08-23T17:00:00+00:00",
            created_at="2026-08-23T17:00:00+00:00",
        )
    )
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko_foreign_web_capture",
            user_id=TENANT,
            raw_object_id=foreign_raw,
            content="Needle belongs to another principal",
            content_type="web",
            title="Foreign captured page",
            lifecycle_stage="active",
            version=1,
            created_at="2026-08-23T17:01:00+00:00",
            updated_at="2026-08-23T17:01:00+00:00",
        )
    )
    unattributed_raw = _opaque("raw", 63)
    storage.store_raw_object(
        RawObject(
            id=unattributed_raw,
            user_id=TENANT,
            source="api",
            source_ref="legacy-unattributed",
            raw_content="Needle legacy knowledge",
            content_type="text",
            metadata_json={},
            content_hash=hashlib.sha256(b"legacy-unattributed").hexdigest(),
            received_at="2026-08-23T18:00:00+00:00",
            created_at="2026-08-23T18:00:00+00:00",
        )
    )
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko_legacy_unattributed",
            user_id=TENANT,
            raw_object_id=unattributed_raw,
            content="Needle legacy knowledge",
            content_type="note",
            title="Legacy unattributed",
            lifecycle_stage="active",
            version=1,
            created_at="2026-08-23T18:01:00+00:00",
            updated_at="2026-08-23T18:01:00+00:00",
        )
    )

    request = _request(corpora=(ArchiveSearchCorpus.KNOWLEDGE,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (None, 2, 2, 2)
    by_raw = {item.resolved_source.source_ref.canonical_object_id: item for item in page.candidates}
    first = by_raw[first_raw]
    assert first.passages[0].passage_ref.source_revision.representation.object_id == matching_ko
    assert by_raw[second_raw].resolved_source.source_ref.source_kind is SourceKind.WEB_CAPTURE
    assert page.authority_scope_complete is False
    assert _coverage(page, request).states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )


def test_temporal_constraints_filter_before_counts_and_emit_exact_facts(storage) -> None:
    _seed(
        storage,
        70,
        body="Needle too early",
        received_at="2026-08-23T10:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    expected_raw, _ = _seed(
        storage,
        71,
        body="Needle in range",
        received_at="2026-08-23T12:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    received = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.DOCUMENTS,
        TemporalRole.RECEIVED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-23T11:00:00+00:00",
        "2026-08-23T13:00:00+00:00",
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        temporal_constraints=(received,),
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    candidate = page.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == expected_raw
    assert len(candidate.temporal_facts) == 1
    fact = candidate.temporal_facts[0]
    assert fact.role is TemporalRole.RECEIVED_AT
    assert fact.start == "2026-08-23T12:00:00+00:00"

    # Receipt is not silently substituted for a separately attested upload
    # instant: this valid document role is unsupported by the current schema.
    unsupported = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.DOCUMENTS,
        TemporalRole.UPLOADED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-23T00:00:00+00:00",
        "2026-08-24T00:00:00+00:00",
    )
    unsupported_request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        temporal_constraints=(unsupported,),
    )
    unavailable = _search(
        storage,
        request=unsupported_request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert unavailable.available is False and unavailable.total is None
    assert _coverage(unavailable, unsupported_request).states == (
        CoverageState.PARTIAL,
        CoverageState.UNAVAILABLE,
    )


def test_unknown_temporal_value_is_backfill_not_false_absence(storage) -> None:
    _seed(
        storage,
        72,
        body="Needle with legacy time",
        received_at="legacy-invalid-time",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    constraint = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.DOCUMENTS,
        TemporalRole.RECEIVED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-23T11:00:00+00:00",
        "2026-08-23T13:00:00+00:00",
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        temporal_constraints=(constraint,),
    )
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (None, 0, 0, 0)
    coverage = _coverage(page, request)
    assert coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )
    assert coverage.absence_decision().value == "not_established"


def test_temporal_filter_preserves_exact_microsecond_boundaries(storage) -> None:
    _seed(
        storage,
        74,
        body="Needle one microsecond too early",
        received_at="2026-08-23T12:00:00.000001+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    expected, _ = _seed(
        storage,
        75,
        body="Needle exactly at the lower bound",
        received_at="2026-08-23T12:00:00.000002+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    constraint = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.DOCUMENTS,
        TemporalRole.RECEIVED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-23T12:00:00.000002+00:00",
        "2026-08-23T12:00:00.000003+00:00",
    )
    page = _search(
        storage,
        request=_request(
            corpora=(ArchiveSearchCorpus.DOCUMENTS,),
            temporal_constraints=(constraint,),
        ),
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == expected


def test_one_invalid_ko_time_cannot_hide_behind_an_examined_sibling(storage) -> None:
    raw_id, matching_ko = _seed(
        storage,
        76,
        body="source",
        knowledge_content="Needle only in the projection with unknown time",
    )
    assert matching_ko is not None
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko_valid_time_unrelated_projection",
            user_id=TENANT,
            raw_object_id=raw_id,
            content="unrelated current projection",
            content_type="document",
            title="Unrelated",
            lifecycle_stage="active",
            version=2,
            created_at="2026-08-23T11:30:00+00:00",
            updated_at="2026-08-23T11:30:00+00:00",
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET created_at='legacy-invalid-time' WHERE id=?",
            (matching_ko,),
        )
    constraint = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.KNOWLEDGE,
        TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-23T11:00:00+00:00",
        "2026-08-23T12:00:00+00:00",
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
        temporal_constraints=(constraint,),
    )
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (None, 1, 0, 0)
    coverage = _coverage(page, request)
    assert coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )
    assert coverage.absence_decision().value == "not_established"


def test_empty_fts_derivative_cannot_create_false_absence(storage) -> None:
    _seed(
        storage,
        41,
        body="Needle survives an empty derivative",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    with storage.transaction() as conn:
        conn.execute("INSERT INTO raw_fts(raw_fts) VALUES('delete-all')")
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    assert page.derivative_current is False
    assert _coverage(page, request).absence_decision().value == "evidence_found"


def test_raw_derivative_cannot_borrow_ignored_or_cross_tenant_hits(storage) -> None:
    target_body = "Needle exact authorized target"
    target_raw, _ = _seed(
        storage,
        42,
        body=target_body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        43,
        body="Needle IGNORED-DERIVATIVE-CANARY",
        inbox_status=InboxStatus.IGNORED,
    )
    _seed(
        storage,
        44,
        tenant=FOREIGN_TENANT,
        owner=OTHER_OWNER,
        body="Needle FOREIGN-DERIVATIVE-CANARY",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    _seed(
        storage,
        45,
        owner=OTHER_OWNER,
        body="Needle OTHER-OWNER-DERIVATIVE-CANARY",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    with storage.transaction() as conn:
        row = conn.execute(
            "SELECT rowid, raw_content FROM raw_objects WHERE id=? AND user_id=?",
            (target_raw, TENANT),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO raw_fts(raw_fts,rowid,raw_content) VALUES('delete',?,?)",
            (row["rowid"], row["raw_content"]),
        )

    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    assert page.derivative_current is False
    assert page.candidates[0].resolved_source.source_ref.canonical_object_id == target_raw
    rendered = json.dumps(page.candidates[0].to_private_json(), ensure_ascii=False)
    assert "exact authorized target" in rendered
    assert "IGNORED-DERIVATIVE-CANARY" not in rendered
    assert "FOREIGN-DERIVATIVE-CANARY" not in rendered
    assert "OTHER-OWNER-DERIVATIVE-CANARY" not in rendered


def test_unicode_folded_match_keeps_an_exact_raw_text_locator(storage) -> None:
    raw_id, _ = _seed(
        storage,
        73,
        body="Точная Ёлка в извлечённом тексте",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="елка",
    )
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.matched == page.returned == 1
    candidate = page.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == raw_id
    passage = candidate.passages[0]
    assert "Ёлка" in passage.excerpt
    locator = passage.passage_ref.locator
    assert (
        "Точная Ёлка в извлечённом тексте"[
            locator.start_char : locator.end_char  # type: ignore[union-attr]
        ]
        == passage.excerpt
    )


def test_lane_page_is_exact_process_private_and_snapshot_attestation_is_explicit(storage) -> None:
    _seed(storage, 42, filename="needle-page.pdf", inbox_status=InboxStatus.CLASSIFIED)
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    for copier in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(ArchiveDocumentStorageError):
            copier(page)

    storage.conn.execute("BEGIN")
    try:
        stale_snapshot = search_archive_document_lane(
            storage.conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
            execution_binding=_binding(
                request,
                (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG),
            ),
            snapshot_discriminator=SNAPSHOT,
            snapshot_current=False,
        )
    finally:
        storage.conn.rollback()
    stale_coverage = _coverage(stale_snapshot, request)
    assert stale_coverage.snapshot_current is False
    assert stale_coverage.absence_decision().value == "not_established"

    target = (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG)
    storage.conn.execute("BEGIN")
    try:
        with pytest.raises(ArchiveDocumentStorageError):
            search_archive_document_lane(
                storage.conn,
                tenant_id=TENANT,
                owner_id=OWNER,
                request=request,
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                lane=SearchLane.CATALOG,
                execution_binding=_binding(request, target, owner=OTHER_OWNER),
                snapshot_discriminator=SNAPSHOT,
                snapshot_current=True,
            )
        with pytest.raises(ArchiveDocumentStorageError):
            search_archive_document_lane(
                storage.conn,
                tenant_id=TENANT,
                owner_id=OWNER,
                request=request,
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                lane=SearchLane.CATALOG,
                execution_binding=_binding(request, target),
                snapshot_discriminator="different-snapshot",
                snapshot_current=True,
            )
    finally:
        storage.conn.rollback()

    with pytest.raises(ArchiveDocumentStorageError):
        _coverage(page, request, owner=OTHER_OWNER)
    with pytest.raises(ArchiveDocumentStorageError):
        _coverage(page, request, snapshot="different-snapshot")

    opaque_handle = page._execution_handle  # noqa: SLF001
    assert opaque_handle not in repr(page)
    object.__setattr__(
        page,
        "_execution_handle",
        _binding(request, target, run="different-run").opaque_handle,
    )
    with pytest.raises(ArchiveDocumentStorageError):
        _coverage(page, request)
    object.__setattr__(page, "_execution_handle", opaque_handle)

    object.__setattr__(page, "_request_handle", b"0" * 32)
    with pytest.raises(ArchiveDocumentStorageError):
        _coverage(page, request)
    assert "{}" not in repr(page)


def test_document_replay_source_is_immutable_and_not_dataclass_serializable(storage) -> None:
    raw_id, _knowledge_id = _seed(
        storage,
        43,
        filename="needle-replay.pdf",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    conversation = storage.create_conversation(OWNER, "Replay boundary")
    boundary = storage.store_message(
        conversation["id"],
        OWNER,
        "user",
        "replay selected evidence",
    )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    candidate = next(
        item for item in page.candidates if item.resolved_source.source_ref.canonical_object_id == raw_id
    )

    storage.conn.execute("BEGIN")
    try:
        source = select_authorized_archive_document_replay_source_in_transaction(
            storage.conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            origin_boundary_user_message_id=boundary["id"],
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            source_ref=candidate.resolved_source.source_ref,
        )
        assert source is not None
        assert source.body == SECRET
        assert not is_dataclass(source)
        with pytest.raises(TypeError):
            asdict(source)  # type: ignore[call-overload]
        with pytest.raises(TypeError, match="immutable"):
            source.body = "changed"
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(ArchiveDocumentStorageError):
                operation(source)
    finally:
        storage.conn.rollback()


def test_document_replay_authorizes_body_free_then_reads_one_exact_large_source(storage) -> None:
    large_body = "Needle exact large replay\n" + "x" * 1_100_000
    raw_id, _knowledge_id = _seed(
        storage,
        44,
        filename="needle-large-replay.pdf",
        body=large_body,
        inbox_status=InboxStatus.CLASSIFIED,
    )
    for ordinal in range(45, 48):
        _seed(
            storage,
            ordinal,
            filename=f"peer-{ordinal}.pdf",
            body="large peer\n" + "y" * 1_100_000,
            inbox_status=InboxStatus.CLASSIFIED,
        )
    conversation = storage.create_conversation(OWNER, "Large replay boundary")
    boundary = storage.store_message(conversation["id"], OWNER, "user", "replay large source")
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    candidate = next(
        item for item in page.candidates if item.resolved_source.source_ref.canonical_object_id == raw_id
    )
    raw_hash = str(
        storage.conn.execute("SELECT content_hash FROM raw_objects WHERE id=?", (raw_id,)).fetchone()[0]
    )
    revision = SourceRevision(
        SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id),
        RevisionKind.RAW_CONTENT_SHA256,
        raw_hash,
    )

    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    storage.conn.execute("BEGIN")
    try:
        source = select_authorized_archive_document_replay_source_in_transaction(
            storage.conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            origin_boundary_user_message_id=boundary["id"],
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            source_ref=candidate.resolved_source.source_ref,
            source_revision=revision,
        )
        assert source is not None
        assert source.body == large_body
    finally:
        storage.conn.rollback()
        storage.conn.set_trace_callback(None)
    body_reads = [item for item in statements if "AS replay_body" in item]
    assert len(body_reads) == 1
    lead = next(item for item in statements if "replay_boundary AS MATERIALIZED" in item)
    assert "live_raw.raw_content" not in lead

    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET content_hash=? WHERE id=?", ("f" * 64, raw_id))
    drift_statements: list[str] = []
    storage.conn.set_trace_callback(drift_statements.append)
    storage.conn.execute("BEGIN")
    try:
        assert (
            select_authorized_archive_document_replay_source_in_transaction(
                storage.conn,
                tenant_id=TENANT,
                owner_id=OWNER,
                origin_boundary_user_message_id=boundary["id"],
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                source_ref=candidate.resolved_source.source_ref,
                source_revision=revision,
            )
            is None
        )
    finally:
        storage.conn.rollback()
        storage.conn.set_trace_callback(None)
    assert not any("AS replay_body" in item for item in drift_statements)


def test_inbound_continuation_is_explicitly_unavailable_without_a_local_cursor(storage) -> None:
    _seed(storage, 43, filename="needle-continuation.pdf", inbox_status=InboxStatus.CLASSIFIED)
    request = ArchiveSearchRequest.create(
        query="Needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        continuation="opaque_authority_owned_cursor",
    )
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    assert page.available is False
    assert (page.total, page.examined, page.matched, page.returned) == (None, 0, 0, 0)
    coverage = _coverage(page, request)
    assert coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert coverage.next_cursor_available is False


def test_inactive_authority_is_unavailable_and_cannot_confirm_absence(storage) -> None:
    _seed(storage, 44, body="Needle behind inactive authority", inbox_status=InboxStatus.CLASSIFIED)
    with storage.transaction() as conn:
        conn.execute("UPDATE users SET status='disabled' WHERE id=?", (OWNER,))
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.available is False
    assert (page.total, page.examined, page.matched, page.returned) == (None, 0, 0, 0)
    coverage = _coverage(page, request)
    assert coverage.authority_rechecked is False
    assert coverage.absence_decision().value == "not_established"


def test_missing_fts_uses_authorized_exact_fallback_and_never_loses_evidence(storage) -> None:
    _seed(storage, 40, body="Needle still registered", inbox_status=InboxStatus.CLASSIFIED)
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    with storage.transaction() as conn:
        conn.execute("DROP TABLE raw_fts")

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.available is True
    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    coverage = _coverage(page, request)
    assert coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert page.derivative_current is False
    assert coverage.absence_decision().value == "evidence_found"


def test_diacritic_fold_matches_fts_and_keeps_coverage_honest(storage) -> None:
    _seed(
        storage,
        80,
        body="Un Café document",
        inbox_status=InboxStatus.CLASSIFIED,
        knowledge_content="Knowledge Café projection",
    )
    request = _request(query="Cafe")

    documents = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    knowledge = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
    )

    for page in (documents, knowledge):
        assert page.matched == page.returned == 1
        assert "Café" in page.candidates[0].passages[0].excerpt
        assert _coverage(page, request).absence_decision().value == "evidence_found"
    assert documents.derivative_current is False
    assert CoverageState.UNAVAILABLE in _coverage(documents, request).states
    assert knowledge.derivative_current is True


def test_missing_fts_cannot_confirm_absence(storage) -> None:
    _seed(storage, 81, body="Different body", inbox_status=InboxStatus.CLASSIFIED)
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,), query="Missing phrase")
    with storage.transaction() as conn:
        conn.execute("DROP TABLE raw_fts")

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.matched == page.returned == 0
    assert page.derivative_current is False
    assert _coverage(page, request).absence_decision().value == "not_established"


def test_noncanonical_24_hour_timestamp_cannot_confirm_temporal_absence(storage) -> None:
    _seed(
        storage,
        82,
        body="Temporal needle",
        received_at="2026-08-23T24:00:00+00:00",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    constraint = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.DOCUMENTS,
        TemporalRole.RECEIVED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-24T00:00:00+00:00",
        "2026-08-24T01:00:00+00:00",
    )
    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Temporal needle",
        temporal_constraints=(constraint,),
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.total is None
    assert _coverage(page, request).absence_decision().value == "not_established"


def test_malformed_inbox_ordering_timestamp_is_backfill_not_current_authority(storage) -> None:
    raw_id, _ = _seed(
        storage,
        83,
        body="Review needle",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    storage.store_inbox_item(
        InboxItem(
            id=_opaque("inbox", 830),
            user_id=TENANT,
            raw_object_id=raw_id,
            status=InboxStatus.IGNORED,
            created_at="zzzz",
        )
    )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,), query="Missing phrase")

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )
    assert page.total is None
    assert _coverage(page, request).absence_decision().value == "not_established"


def test_raw_revision_identity_is_stable_across_catalog_and_lexical(storage) -> None:
    _seed(
        storage,
        84,
        filename="stable-revision.pdf",
        body="Stable revision body",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    catalog_request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="stable-revision.pdf",
    )
    lexical_request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Stable revision body",
    )
    catalog = _search(
        storage,
        request=catalog_request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    lexical = _search(
        storage,
        request=lexical_request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    def raw_revision(page: ArchiveDocumentLanePage) -> str:
        return next(
            item.value
            for item in page.candidates[0].resolved_source.revisions
            if item.representation.kind is RepresentationKind.RAW_OBJECT
        )

    assert raw_revision(catalog) == raw_revision(lexical)


@pytest.mark.parametrize("first_filename", ["ignored.pdf", "voice.ogg"])
def test_duplicate_filename_metadata_cannot_confirm_catalog_absence(
    storage,
    first_filename: str,
) -> None:
    raw_id, _ = _seed(
        storage,
        85,
        filename="placeholder.pdf",
        body="Catalog body",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    duplicate_metadata = (
        f'{{"filename":"{first_filename}","filename":"Needle.pdf",'
        f'"mime_type":"application/pdf","media_kind":"document","uploaded_by":"{OWNER}"}}'
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (duplicate_metadata, raw_id),
        )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,), query="Needle.pdf")

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    assert page.total is None
    assert page.matched == page.returned == 0
    assert _coverage(page, request).absence_decision().value == "not_established"


def test_oversized_audio_metadata_fails_closed_before_document_recall(storage) -> None:
    raw_id, _ = _seed(
        storage,
        851,
        filename="oversized-audio.ogg",
        body="Oversized audio marker",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    oversized_audio_metadata = json.dumps(
        {
            "filename": "oversized-audio.ogg",
            "mime_type": "audio/ogg",
            "media_kind": "voice",
            "uploaded_by": OWNER,
            "padding": "x" * 200_000,
        },
        separators=(",", ":"),
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (oversized_audio_metadata, raw_id),
        )
    ordinary_id, _ = _seed(
        storage,
        852,
        filename="ordinary-document.pdf",
        body="Ordinary document marker",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    searchable = storage.get_searchable_file_sources(
        TENANT,
        [raw_id, ordinary_id],
    )
    assert [item["id"] for item in searchable] == [ordinary_id]

    request = _request(
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        query="Oversized audio marker",
    )

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
    )

    assert all(
        candidate.resolved_source.source_ref.canonical_object_id != raw_id for candidate in page.candidates
    )


def test_foreign_principal_catalog_corruption_does_not_degrade_owner_coverage(storage) -> None:
    _seed(
        storage,
        86,
        filename="owner-document.pdf",
        body="Owner body",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    foreign_id, _ = _seed(
        storage,
        87,
        tenant=TENANT,
        owner=OTHER_OWNER,
        filename="foreign-placeholder.pdf",
        body="Foreign body",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    foreign_metadata = (
        '{"filename":"voice.ogg","filename":"Needle.pdf",'
        f'"mime_type":"application/pdf","media_kind":"document","uploaded_by":"{OTHER_OWNER}"}}'
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (foreign_metadata, foreign_id),
        )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,), query="Missing phrase")

    page = _search(
        storage,
        request=request,
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.CATALOG,
    )
    coverage = _coverage(page, request)
    assert (page.total, page.examined, page.matched, page.returned) == (1, 1, 0, 0)
    assert page.authority_scope_complete is True
    assert page.catalog_projection_current is False
    assert coverage.states == (CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL)
    assert coverage.absence_decision().value == "not_established"


def test_invalid_revision_and_private_factory_fail_closed_without_body_or_identifiers(storage) -> None:
    raw_id, _ = _seed(
        storage,
        50,
        filename="needle-corrupt.pdf",
        body=SECRET,
        content_hash="invalid-private-revision",
        inbox_status=InboxStatus.CLASSIFIED,
    )
    request = _request(corpora=(ArchiveSearchCorpus.DOCUMENTS,))
    with pytest.raises(ArchiveDocumentStorageError) as snapshot_failure:
        search_archive_document_lane(
            storage.conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
            execution_binding=_binding(
                request,
                (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG),
            ),
            snapshot_discriminator=SNAPSHOT,
            snapshot_current=True,
        )
    assert SECRET not in str(snapshot_failure.value) and raw_id not in str(snapshot_failure.value)

    with pytest.raises(ArchiveDocumentStorageError) as failure:
        _search(
            storage,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.CATALOG,
        )
    rendered = repr(failure.value) + str(failure.value)
    assert SECRET not in rendered and raw_id not in rendered

    with pytest.raises(ArchiveDocumentStorageError) as page_failure:
        ArchiveDocumentLanePage(
            ArchiveSearchCorpus.DOCUMENTS,
            SearchLane.CATALOG,
            [],
            0,
            0,
            0,
            0,
            False,
            True,
        )
    assert SECRET not in str(page_failure.value) and raw_id not in str(page_failure.value)
