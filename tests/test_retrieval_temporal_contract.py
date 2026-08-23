from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from friday.retrieval.contracts import (
    RepresentationKind,
    RetrievalContractError,
    RevisionKind,
    SourceRepresentation,
    SourceRevision,
    TemporalFact,
    TemporalOrigin,
    TemporalPrecision,
    TemporalRole,
    temporal_facts_for_role,
)


def _raw_revision(digest: str = "a" * 64) -> SourceRevision:
    return SourceRevision(
        SourceRepresentation(RepresentationKind.RAW_OBJECT, "raw_0123456789abcdef"),
        RevisionKind.RAW_CONTENT_SHA256,
        digest,
    )


def _ko_revision(version: str = "2") -> SourceRevision:
    return SourceRevision(
        SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, "ko_0123456789abcdef"),
        RevisionKind.KNOWLEDGE_VERSION,
        version,
    )


def _conversation_revision() -> SourceRevision:
    return SourceRevision(
        SourceRepresentation(RepresentationKind.CONVERSATION, "conv_0123456789abcdef"),
        RevisionKind.MESSAGE_LEDGER_SHA256,
        "b" * 64,
    )


def test_legacy_document_date_is_explicit_and_never_substituted() -> None:
    legacy = TemporalFact.for_date(
        role=TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE,
        value=date(2024, 5, 7),
        precision=TemporalPrecision.DAY,
        origin=TemporalOrigin.LEGACY_COLLAPSED,
        source_revision=_ko_revision(),
    )

    assert legacy.start == "2024-05-07"
    assert legacy.end == "2024-05-08"
    assert temporal_facts_for_role([legacy], TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE) == (legacy,)
    assert temporal_facts_for_role([legacy], TemporalRole.DOCUMENT_CREATED_AT) == ()
    assert temporal_facts_for_role([legacy], TemporalRole.EVENT_DATE) == ()


def test_temporal_fact_retains_exact_source_revision() -> None:
    fact = TemporalFact.for_date(
        role=TemporalRole.DOCUMENT_CREATED_AT,
        value=date(2023, 1, 1),
        precision=TemporalPrecision.YEAR,
        origin=TemporalOrigin.SOURCE_METADATA,
        source_revision=_raw_revision(),
    )

    assert TemporalFact.parse_private(fact.to_private_json()) == fact
    assert fact.source_revision == _raw_revision()
    assert _raw_revision("c" * 64) != fact.source_revision
    assert "raw_0123456789abcdef" not in repr(fact)


def test_document_metadata_dates_allow_bounded_precision_or_instant() -> None:
    month = TemporalFact.for_date(
        role=TemporalRole.DOCUMENT_MODIFIED_AT,
        value=date(2025, 8, 1),
        precision=TemporalPrecision.MONTH,
        origin=TemporalOrigin.SOURCE_METADATA,
        source_revision=_raw_revision(),
    )
    instant = TemporalFact.for_instant(
        role=TemporalRole.DOCUMENT_MODIFIED_AT,
        value=datetime(2025, 8, 2, 12, 30, tzinfo=UTC),
        origin=TemporalOrigin.SOURCE_METADATA,
        source_revision=_raw_revision(),
    )

    assert (month.start, month.end) == ("2025-08-01", "2025-09-01")
    assert instant.start == "2025-08-02T12:30:00+00:00"


def test_ko_projection_dates_have_projection_roles_not_document_roles() -> None:
    projection = TemporalFact.for_instant(
        role=TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT,
        value=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        origin=TemporalOrigin.KNOWLEDGE_PROJECTION,
        source_revision=_ko_revision(),
    )

    assert projection.matches_role(TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT)
    assert not projection.matches_role(TemporalRole.DOCUMENT_CREATED_AT)
    with pytest.raises(RetrievalContractError, match="projection"):
        TemporalFact.for_instant(
            role=TemporalRole.DOCUMENT_CREATED_AT,
            value=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            origin=TemporalOrigin.KNOWLEDGE_PROJECTION,
            source_revision=_ko_revision(),
        )


def test_provenance_roles_are_closed_and_semantic() -> None:
    extracted = TemporalFact.for_date(
        role=TemporalRole.MENTIONED_DATE,
        value=date(2026, 8, 23),
        precision=TemporalPrecision.DAY,
        origin=TemporalOrigin.PARSER_EXTRACTED,
        source_revision=_raw_revision(),
    )
    asserted = TemporalFact.for_date(
        role=TemporalRole.EVENT_DATE,
        value=date(2026, 8, 24),
        precision=TemporalPrecision.DAY,
        origin=TemporalOrigin.USER_ASSERTED,
        source_revision=_raw_revision(),
    )
    assert extracted.origin is TemporalOrigin.PARSER_EXTRACTED
    assert asserted.origin is TemporalOrigin.USER_ASSERTED

    with pytest.raises(RetrievalContractError, match="content date role"):
        TemporalFact.for_instant(
            role=TemporalRole.UPLOADED_AT,
            value=datetime(2026, 8, 23, tzinfo=UTC),
            origin=TemporalOrigin.PARSER_EXTRACTED,
            source_revision=_raw_revision(),
        )


def test_conversation_time_anchors_message_ledger_and_normalizes_zone() -> None:
    fact = TemporalFact.for_instant(
        role=TemporalRole.CONVERSATION_TIME,
        value=datetime(2026, 8, 23, 15, 0, tzinfo=timezone(timedelta(hours=3))),
        origin=TemporalOrigin.STORAGE_COLUMN,
        source_revision=_conversation_revision(),
    )
    assert fact.start == "2026-08-23T12:00:00+00:00"

    with pytest.raises(RetrievalContractError, match="message-ledger"):
        TemporalFact.for_instant(
            role=TemporalRole.CONVERSATION_TIME,
            value=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            origin=TemporalOrigin.STORAGE_COLUMN,
            source_revision=_raw_revision(),
        )
    with pytest.raises(RetrievalContractError, match="offset"):
        TemporalFact.for_instant(
            role=TemporalRole.CONVERSATION_TIME,
            value=datetime(2026, 8, 23, 12, 0),
            origin=TemporalOrigin.STORAGE_COLUMN,
            source_revision=_conversation_revision(),
        )


def test_instant_only_storage_role_rejects_date_interval() -> None:
    with pytest.raises(RetrievalContractError, match="exact instant"):
        TemporalFact.for_date(
            role=TemporalRole.RECEIVED_AT,
            value=date(2026, 8, 23),
            precision=TemporalPrecision.DAY,
            origin=TemporalOrigin.STORAGE_COLUMN,
            source_revision=_raw_revision(),
        )


def test_temporal_json_is_closed_and_canonical() -> None:
    fact = TemporalFact.for_date(
        role=TemporalRole.EVENT_DATE,
        value=date(2026, 8, 23),
        precision=TemporalPrecision.DAY,
        origin=TemporalOrigin.EXTERNAL_AUTHORITY,
        source_revision=_raw_revision(),
    )
    payload = fact.to_private_payload()
    payload["timezone_guess"] = "Europe/Moscow"
    with pytest.raises(RetrievalContractError, match="closed contract"):
        TemporalFact.parse_private(json.dumps(payload, sort_keys=True, separators=(",", ":")))
