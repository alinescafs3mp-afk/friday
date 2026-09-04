from __future__ import annotations

import pytest

from friday.orchestration.web_evidence_bundle import WebEvidenceSourceV1
from friday.orchestration.web_source_date_coverage import (
    WebSourceDateCoverageError,
    WebSourceDateCoverageReason,
    WebSourceDateCoverageState,
    WebSourceDateCoverageV1,
    build_web_source_date_coverage,
)


def source(
    source_id: str,
    publication_or_update_date: str | None = "2026-09-01",
    retrieved_at: str = "2026-09-04T14:00:00Z",
) -> WebEvidenceSourceV1:
    return WebEvidenceSourceV1(
        source_id=source_id,
        canonical_url=f"https://docs.python.org/{source_id}/",
        title="Python documentation",
        publisher_domain="docs.python.org",
        publication_or_update_date=publication_or_update_date,
        retrieved_at=retrieved_at,
        source_class="public",
        content_digest="a" * 64,
        relevant_passage_references=("intro",),
    )


def build(
    sources: tuple[WebEvidenceSourceV1, ...],
) -> WebSourceDateCoverageV1:
    return build_web_source_date_coverage("coverage-1", "turn-1", sources=sources)


def test_all_sources_with_publication_dates_are_dated_and_frozen() -> None:
    result = build((source("source-1"),))
    assert result.coverage is WebSourceDateCoverageState.DATED
    assert result.dated_source_count == 1
    assert result.source_count == 1
    assert result.reason is WebSourceDateCoverageReason.ALL_SOURCES_DATED
    with pytest.raises(AttributeError):
        result.source_count = 2  # type: ignore[misc]


def test_retrieved_at_alone_does_not_make_a_source_dated() -> None:
    result = build((source("source-1", publication_or_update_date=None),))
    assert result.coverage is WebSourceDateCoverageState.UNDATED
    assert result.dated_source_count == 0
    assert result.source_count == 1


def test_partial_date_coverage_counts_only_present_publication_dates() -> None:
    result = build(
        (
            source("source-1", publication_or_update_date="2026-09-01"),
            source("source-2", publication_or_update_date=None),
        )
    )
    assert result.coverage is WebSourceDateCoverageState.PARTIAL
    assert result.dated_source_count == 1
    assert result.source_count == 2
    assert result.reason is WebSourceDateCoverageReason.SOME_SOURCES_DATED


def test_empty_sources_are_empty_not_dated() -> None:
    result = build(())
    assert result.coverage is WebSourceDateCoverageState.EMPTY
    assert result.dated_source_count == 0
    assert result.source_count == 0
    assert result.reason is WebSourceDateCoverageReason.NO_SOURCES


def test_mapping_source_facts_support_valid_dates_and_missing_dates() -> None:
    result = build_web_source_date_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "publication_or_update_date": "2026-09-01",
            },
            {
                "source_id": "source-2",
                "canonical_url": "https://www.python.org/",
            },
        ),
    )
    assert result.coverage is WebSourceDateCoverageState.PARTIAL
    assert result.dated_source_count == 1


def test_invalid_date_is_blocked_without_exposing_source_counts() -> None:
    result = build_web_source_date_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "publication_or_update_date": "not-a-date",
            },
        ),
    )
    assert result.coverage is WebSourceDateCoverageState.BLOCKED
    assert result.reason is WebSourceDateCoverageReason.INVALID_DATE
    assert result.dated_source_count == 0
    assert result.source_count == 0


def test_private_url_is_blocked_without_exposing_source_counts() -> None:
    result = build_web_source_date_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "http://127.0.0.1:8000/source",
                "publication_or_update_date": "2026-09-01",
            },
        ),
    )
    assert result.coverage is WebSourceDateCoverageState.BLOCKED
    assert result.reason is WebSourceDateCoverageReason.PRIVATE_URL
    assert result.dated_source_count == 0
    assert result.source_count == 0


def test_invalid_source_shape_and_duplicate_ids_are_blocked() -> None:
    invalid = build_web_source_date_coverage(
        "coverage-1",
        "turn-1",
        source_facts=({"source_id": "source-1", "canonical_url": "https://example.com/"},),
    )
    assert invalid.coverage is WebSourceDateCoverageState.UNDATED
    duplicate = build_web_source_date_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {"source_id": "source-1", "canonical_url": "https://example.com/"},
            {"source_id": "source-1", "canonical_url": "https://www.python.org/"},
        ),
    )
    assert duplicate.coverage is WebSourceDateCoverageState.BLOCKED
    assert duplicate.reason is WebSourceDateCoverageReason.INVALID_SOURCE_FACTS


def test_invalid_retrieved_at_is_blocked_even_when_publication_date_exists() -> None:
    result = build_web_source_date_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "publication_or_update_date": "2026-09-01",
                "retrieved_at": "2026-09-04T14:00:00",
            },
        ),
    )
    assert result.coverage is WebSourceDateCoverageState.BLOCKED
    assert result.reason is WebSourceDateCoverageReason.INVALID_SOURCE_FACTS


def test_identity_and_closed_result_fields_are_validated() -> None:
    with pytest.raises(WebSourceDateCoverageError):
        build_web_source_date_coverage("/private", "turn-1", sources=())
    with pytest.raises(WebSourceDateCoverageError):
        WebSourceDateCoverageV1(
            "coverage-1",
            "turn-1",
            WebSourceDateCoverageState.DATED,
            0,
            1,
            WebSourceDateCoverageReason.ALL_SOURCES_DATED,
        )
