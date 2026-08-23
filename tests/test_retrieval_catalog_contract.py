from __future__ import annotations

from datetime import date

import pytest

from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CatalogIndexLane,
    CatalogIndexState,
    CatalogIndexStatus,
    CatalogIngestState,
    CatalogItem,
    CatalogReviewState,
    IndexIncompleteReason,
    LifecycleRef,
    LifecycleState,
    RepresentationKind,
    ResolvedSource,
    RetrievalContractError,
    RevalidationTarget,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TemporalFact,
    TemporalOrigin,
    TemporalPrecision,
    TemporalRole,
)


def _snapshot() -> tuple[ResolvedSource, SourceRevision, SourceRevision]:
    source_ref = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        "tenant-main",
        "person-42",
        CanonicalObjectKind.RAW_OBJECT,
        "raw_0123456789abcdef",
    )
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, "raw_0123456789abcdef")
    inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, "inbox_0123456789abcdef")
    knowledge = SourceRepresentation(
        RepresentationKind.KNOWLEDGE_OBJECT,
        "ko_0123456789abcdef",
    )
    raw_revision = SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, "a" * 64)
    knowledge_revision = SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "2")
    return (
        ResolvedSource.create(
            source_ref=source_ref,
            representations=[raw, inbox, knowledge],
            lifecycle=[
                LifecycleRef(raw, LifecycleState.ACTIVE),
                LifecycleRef(inbox, LifecycleState.CLASSIFIED),
                LifecycleRef(knowledge, LifecycleState.ACTIVE),
            ],
            revisions=[raw_revision, knowledge_revision],
            revalidation_targets=[
                RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL),
                RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL),
                RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
            ],
        ),
        raw_revision,
        knowledge_revision,
    )


def _index_states() -> tuple[CatalogIndexState, ...]:
    return (
        CatalogIndexState(
            CatalogIndexLane.APPROXIMATE_IDENTITY,
            CatalogIndexStatus.CURRENT,
            None,
        ),
        CatalogIndexState(CatalogIndexLane.CATALOG, CatalogIndexStatus.CURRENT, None),
        CatalogIndexState(
            CatalogIndexLane.DENSE,
            CatalogIndexStatus.INCOMPATIBLE,
            IndexIncompleteReason.EMBEDDING_INCOMPATIBLE,
        ),
        CatalogIndexState(CatalogIndexLane.LEXICAL, CatalogIndexStatus.CURRENT, None),
        CatalogIndexState(
            CatalogIndexLane.PASSAGES,
            CatalogIndexStatus.PARTIAL,
            IndexIncompleteReason.BACKFILL_PENDING,
        ),
    )


def _catalog(filename: str = "report.pdf") -> CatalogItem:
    snapshot, raw_revision, knowledge_revision = _snapshot()
    facts = [
        TemporalFact.for_date(
            role=TemporalRole.DOCUMENT_CREATED_AT,
            value=date(2025, 8, 1),
            precision=TemporalPrecision.MONTH,
            origin=TemporalOrigin.SOURCE_METADATA,
            source_revision=raw_revision,
        ),
        TemporalFact.for_date(
            role=TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE,
            value=date(2025, 8, 12),
            precision=TemporalPrecision.DAY,
            origin=TemporalOrigin.LEGACY_COLLAPSED,
            source_revision=knowledge_revision,
        ),
    ]
    return CatalogItem.create(
        source_ref=snapshot.source_ref,
        resolved_source=snapshot,
        canonical_title="Quarterly report",
        visible_title="Q3 report",
        filename=filename,
        aliases=["Q3", "quarterly"],
        review_state=CatalogReviewState.CLASSIFIED,
        ingest_state=CatalogIngestState.EXTRACTED,
        index_states=_index_states(),
        temporal_facts=facts,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


def test_catalog_is_body_free_private_and_non_authoritative() -> None:
    catalog = _catalog()
    payload = catalog.to_private_payload()

    assert CatalogItem.parse_private(catalog.to_private_json()) == catalog
    assert not (_all_keys(payload) & {"body", "content", "raw_text", "path", "query"})
    assert catalog.filename == "report.pdf"
    assert not hasattr(catalog, "authorize")
    assert "Quarterly report" not in repr(catalog)
    assert "person-42" not in repr(catalog)


def test_catalog_names_are_navigation_projection_not_source_identity() -> None:
    first = _catalog("report.pdf")
    renamed = _catalog("renamed.pdf")

    assert first.source_ref == renamed.source_ref
    assert first.source_ref.logical_digest(b"k" * 32) == renamed.source_ref.logical_digest(b"k" * 32)
    assert first.filename != renamed.filename
    with pytest.raises(RetrievalContractError, match="display path"):
        _catalog("Projects/report.pdf")


def test_catalog_requires_every_index_lane_with_explicit_incompleteness() -> None:
    catalog = _catalog()
    assert {item.lane for item in catalog.index_states} == set(CatalogIndexLane)

    with pytest.raises(RetrievalContractError, match="every index lane"):
        CatalogItem(
            source_ref=catalog.source_ref,
            resolved_source=catalog.resolved_source,
            canonical_title=catalog.canonical_title,
            visible_title=catalog.visible_title,
            filename=catalog.filename,
            aliases=catalog.aliases,
            review_state=catalog.review_state,
            ingest_state=catalog.ingest_state,
            index_states=catalog.index_states[:-1],
            temporal_facts=catalog.temporal_facts,
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CatalogIndexStatus.FAILED, IndexIncompleteReason.BACKFILL_PENDING),
        (CatalogIndexStatus.PENDING, IndexIncompleteReason.EXTRACTION_FAILED),
        (CatalogIndexStatus.STALE, IndexIncompleteReason.NO_TEXT),
        (CatalogIndexStatus.CURRENT, IndexIncompleteReason.SOURCE_CHANGED),
    ],
)
def test_index_status_reason_matrix_is_closed(
    status: CatalogIndexStatus, reason: IndexIncompleteReason
) -> None:
    with pytest.raises(RetrievalContractError, match="disagree"):
        CatalogIndexState(CatalogIndexLane.PASSAGES, status, reason)


def test_not_applicable_lane_still_explains_why() -> None:
    state = CatalogIndexState(
        CatalogIndexLane.DENSE,
        CatalogIndexStatus.NOT_APPLICABLE,
        IndexIncompleteReason.NO_TEXT,
    )
    assert state.incomplete_reason is IndexIncompleteReason.NO_TEXT


def test_catalog_create_rejects_untrusted_mixed_aliases_cleanly() -> None:
    snapshot, _raw_revision, _knowledge_revision = _snapshot()
    with pytest.raises(RetrievalContractError, match="aliases"):
        CatalogItem.create(
            source_ref=snapshot.source_ref,
            resolved_source=snapshot,
            canonical_title=None,
            visible_title=None,
            filename=None,
            aliases=["valid", 3],  # type: ignore[list-item]
            review_state=CatalogReviewState.CLASSIFIED,
            ingest_state=CatalogIngestState.EXTRACTED,
            index_states=_index_states(),
            temporal_facts=[],
        )


def test_catalog_temporal_fact_must_belong_to_snapshot() -> None:
    catalog = _catalog()
    foreign_fact = TemporalFact.for_date(
        role=TemporalRole.EVENT_DATE,
        value=date(2026, 8, 23),
        precision=TemporalPrecision.DAY,
        origin=TemporalOrigin.USER_ASSERTED,
        source_revision=SourceRevision(
            SourceRepresentation(RepresentationKind.RAW_OBJECT, "raw_0123456789abcdef"),
            RevisionKind.RAW_CONTENT_SHA256,
            "f" * 64,
        ),
    )
    with pytest.raises(RetrievalContractError, match="resolved snapshot"):
        CatalogItem.create(
            source_ref=catalog.source_ref,
            resolved_source=catalog.resolved_source,
            canonical_title=catalog.canonical_title,
            visible_title=catalog.visible_title,
            filename=catalog.filename,
            aliases=catalog.aliases,
            review_state=catalog.review_state,
            ingest_state=catalog.ingest_state,
            index_states=catalog.index_states,
            temporal_facts=[foreign_fact],
        )
