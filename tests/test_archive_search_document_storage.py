from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
import sqlite3
from typing import Any

import pytest

from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveLifecycleConstraint,
    ArchiveReviewState,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
    ReviewScope,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    LifecycleState,
    RepresentationKind,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceKind,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
)
from friday.storage._archive_search_documents import (
    ArchiveDocumentLanePage,
    ArchiveDocumentStorageError,
    search_archive_document_lane,
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
) -> tuple[str, str | None]:
    storage.ensure_user(tenant)
    storage.ensure_user(owner)
    raw_id = _opaque("raw", number)
    raw = RawObject(
        id=raw_id,
        user_id=tenant,
        source="upload",
        source_ref=f"telegram-file:{number}",
        raw_content=body,
        content_type="file",
        metadata_json={
            "filename": filename or f"document-{number}.pdf",
            "mime_type": "application/pdf",
            "media_kind": "document",
            "uploaded_by": owner,
        },
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
) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=query,
        corpora=corpora,
        review_scope=review_scope,
        lifecycle_constraints=lifecycle_constraints,
        temporal_constraints=temporal_constraints,
        limit=limit,
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
            execution_binding=_binding(request, ({
                ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
                ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
            }[corpus], lane)),
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
    target = ({
        ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
        ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
    }[page.corpus], page.lane)
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
    assert documents.derivative_current is True
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
        assert passage.passage_ref.source_revision.value == hashlib.sha256(
            f"source-bytes-{source_number}".encode()
        ).hexdigest()

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
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", sql, re.I) for sql in statements)
    assert ("raw_objects", "raw_content") not in body_reads
    assert ("knowledge_objects", "content") not in body_reads

    source_ids = tuple(
        item.resolved_source.source_ref.canonical_object_id for item in first.candidates
    )
    assert len(source_ids) == len(set(source_ids)) == 2
    assert set(source_ids) <= {raw_exact, raw_prefix, raw_substring, raw_alias}

    coverage = _coverage(first, request)
    assert coverage.states == (CoverageState.CAPPED, CoverageState.PARTIAL)
    assert coverage.eligible_authorized == coverage.examined == 4
    assert coverage.matched_at_least == 4 and coverage.returned == 2
    assert coverage.next_cursor_available is False


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
    by_raw = {
        item.resolved_source.source_ref.canonical_object_id: item
        for item in page.candidates
    }
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
    by_raw = {
        item.resolved_source.source_ref.canonical_object_id: item
        for item in page.candidates
    }
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
    assert "Точная Ёлка в извлечённом тексте"[
        locator.start_char : locator.end_char  # type: ignore[union-attr]
    ] == passage.excerpt


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
    assert coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )
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
        assert page.derivative_current is True
        assert "Café" in page.candidates[0].passages[0].excerpt
        assert _coverage(page, request).absence_decision().value == "evidence_found"


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
    assert coverage.states == (CoverageState.COMPLETE,)
    assert coverage.absence_decision().value == "authorized_absence_confirmed"


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
