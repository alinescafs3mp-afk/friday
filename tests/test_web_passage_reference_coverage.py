from __future__ import annotations

import pytest

from friday.orchestration.web_evidence_bundle import WebEvidenceSourceV1
from friday.orchestration.web_passage_reference_coverage import (
    WebPassageReferenceCoverageError,
    WebPassageReferenceCoverageReason,
    WebPassageReferenceCoverageState,
    WebPassageReferenceCoverageV1,
    build_web_passage_reference_coverage,
)


def source(
    source_id: str,
    references: tuple[str, ...] = ("intro",),
    publication_or_update_date: str | None = None,
) -> WebEvidenceSourceV1:
    return WebEvidenceSourceV1(
        source_id=source_id,
        canonical_url=f"https://docs.python.org/{source_id}/",
        title="Python documentation",
        publisher_domain="docs.python.org",
        publication_or_update_date=publication_or_update_date,
        retrieved_at="2026-09-04T14:00:00Z",
        source_class="public",
        content_digest="a" * 64,
        relevant_passage_references=references,
    )


def build(sources: tuple[WebEvidenceSourceV1, ...]) -> WebPassageReferenceCoverageV1:
    return build_web_passage_reference_coverage("coverage-1", "turn-1", sources=sources)


def test_all_sources_with_references_are_referenced_and_frozen() -> None:
    result = build((source("source-1"),))
    assert result.coverage is WebPassageReferenceCoverageState.REFERENCED
    assert result.referenced_source_count == 1
    assert result.source_count == 1
    assert result.reason is WebPassageReferenceCoverageReason.ALL_SOURCES_REFERENCED
    with pytest.raises(AttributeError):
        result.source_count = 2  # type: ignore[misc]


def test_title_digest_retrieved_at_and_date_are_not_references() -> None:
    result = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "title": "A title",
                "content_digest": "a" * 64,
                "retrieved_at": "2026-09-04T14:00:00Z",
                "publication_or_update_date": "2026-09-01",
            },
        ),
    )
    assert result.coverage is WebPassageReferenceCoverageState.BARE
    assert result.referenced_source_count == 0
    assert result.source_count == 1


def test_partial_reference_coverage_counts_only_nonempty_references() -> None:
    result = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "relevant_passage_references": ("intro",),
            },
            {
                "source_id": "source-2",
                "canonical_url": "https://www.python.org/",
                "relevant_passage_references": (),
            },
        ),
    )
    assert result.coverage is WebPassageReferenceCoverageState.PARTIAL
    assert result.referenced_source_count == 1
    assert result.source_count == 2


def test_empty_sources_are_empty_not_referenced() -> None:
    result = build(())
    assert result.coverage is WebPassageReferenceCoverageState.EMPTY
    assert result.referenced_source_count == 0
    assert result.source_count == 0
    assert result.reason is WebPassageReferenceCoverageReason.NO_SOURCES


def test_sources_without_references_are_bare() -> None:
    result = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=({"source_id": "source-1", "canonical_url": "https://docs.python.org/3/"},),
    )
    assert result.coverage is WebPassageReferenceCoverageState.BARE
    assert result.reason is WebPassageReferenceCoverageReason.NO_SOURCES_REFERENCED


def test_empty_string_reference_is_blocked_without_source_counts() -> None:
    result = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "relevant_passage_references": ("",),
            },
        ),
    )
    assert result.coverage is WebPassageReferenceCoverageState.BLOCKED
    assert result.reason is WebPassageReferenceCoverageReason.EMPTY_REFERENCE
    assert result.referenced_source_count == 0
    assert result.source_count == 0


def test_private_url_is_blocked_without_source_counts() -> None:
    result = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {
                "source_id": "source-1",
                "canonical_url": "http://127.0.0.1:8000/source",
                "relevant_passage_references": ("intro",),
            },
        ),
    )
    assert result.coverage is WebPassageReferenceCoverageState.BLOCKED
    assert result.reason is WebPassageReferenceCoverageReason.PRIVATE_URL
    assert result.referenced_source_count == 0
    assert result.source_count == 0


def test_invalid_source_shape_and_duplicate_ids_are_blocked() -> None:
    invalid = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=({"source_id": "source-1"},),
    )
    assert invalid.coverage is WebPassageReferenceCoverageState.BLOCKED
    duplicate = build_web_passage_reference_coverage(
        "coverage-1",
        "turn-1",
        source_facts=(
            {"source_id": "source-1", "canonical_url": "https://example.com/"},
            {"source_id": "source-1", "canonical_url": "https://www.python.org/"},
        ),
    )
    assert duplicate.coverage is WebPassageReferenceCoverageState.BLOCKED
    assert duplicate.reason is WebPassageReferenceCoverageReason.INVALID_SOURCE_FACTS


def test_identity_and_closed_result_fields_are_validated() -> None:
    with pytest.raises(WebPassageReferenceCoverageError):
        build_web_passage_reference_coverage("/private", "turn-1", sources=())
    with pytest.raises(WebPassageReferenceCoverageError):
        WebPassageReferenceCoverageV1(
            "coverage-1",
            "turn-1",
            WebPassageReferenceCoverageState.REFERENCED,
            0,
            1,
            WebPassageReferenceCoverageReason.ALL_SOURCES_REFERENCED,
        )
