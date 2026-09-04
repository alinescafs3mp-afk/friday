from __future__ import annotations

import pytest

from friday.orchestration.web_citation_coverage import (
    WebCitationCoverageError,
    WebCitationCoverageReason,
    WebCitationCoverageState,
    WebCitationCoverageV1,
    build_web_citation_coverage,
)


def test_host_identity_is_casefolded_and_not_public_suffix_collapsed() -> None:
    result = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        ("https://Example.COM/a", "https://docs.python.org/3/", "https://docs.python.org/3/library/"),
        ("https://example.com/cited", "https://DOCS.python.org/reference"),
    )
    assert result.coverage is WebCitationCoverageState.COMPLETE
    assert result.cited_host_count == 2
    assert result.admitted_host_count == 2
    assert result.reason is WebCitationCoverageReason.ALL_ADMITTED_HOSTS_CITED


def test_partial_when_only_some_admitted_hosts_are_cited() -> None:
    result = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        ("https://one.example.com", "https://two.example.com"),
        ("https://one.example.com/source",),
    )
    assert result.coverage is WebCitationCoverageState.PARTIAL
    assert result.cited_host_count == 1
    assert result.admitted_host_count == 2
    assert result.reason is WebCitationCoverageReason.SOME_ADMITTED_HOSTS_CITED


@pytest.mark.parametrize(
    "admitted,cited,reason",
    (
        ((), (), WebCitationCoverageReason.NO_ADMITTED_HOSTS),
        (("https://one.example.com",), (), WebCitationCoverageReason.NO_CITED_PUBLIC_HOSTS),
        ((), ("https://one.example.com",), WebCitationCoverageReason.NO_ADMITTED_HOSTS),
    ),
)
def test_empty_is_not_complete(admitted: tuple[str, ...], cited: tuple[str, ...], reason: object) -> None:
    result = build_web_citation_coverage("coverage-1", "turn-1", admitted, cited)
    assert result.coverage is WebCitationCoverageState.EMPTY
    assert result.cited_host_count == 0
    assert result.admitted_host_count == 0
    assert result.reason is reason


@pytest.mark.parametrize(
    "url",
    (
        "https://service.local/source",
        "http://127.0.0.1:8080/source",
        "https://10.0.0.7/source",
        "https://user:password@example.com/source",
        "https://example.com/source?access_token=secret",
        "report.pdf",
    ),
)
def test_private_suffix_non_global_ip_and_credentials_block(url: str) -> None:
    result = build_web_citation_coverage("coverage-1", "turn-1", (url,), (url,))
    assert result.coverage is WebCitationCoverageState.BLOCKED_PRIVATE
    assert result.reason is WebCitationCoverageReason.PRIVATE_URL
    assert result.cited_host_count == 0
    assert result.admitted_host_count == 0


def test_mapping_source_urls_and_optional_evidence_bundle_are_supported() -> None:
    result = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        ({"url": "https://docs.python.org/3/"},),
        ({"canonical_url": "https://docs.python.org/3/library/"},),
    )
    assert result.coverage is WebCitationCoverageState.COMPLETE


def test_invalid_evidence_bundle_fails_closed_and_mismatched_hosts_are_partial() -> None:
    invalid = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        evidence_bundle={"sources": ()},
        cited_source_urls=("https://docs.python.org/3/",),
    )
    assert invalid.coverage is WebCitationCoverageState.BLOCKED_PRIVATE
    mismatch = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        ("https://one.example.com",),
        ("https://two.example.com",),
    )
    assert mismatch.coverage is WebCitationCoverageState.PARTIAL
    assert mismatch.reason is WebCitationCoverageReason.HOST_SET_MISMATCH


def test_evidence_bundle_sources_can_supply_the_admitted_set() -> None:
    bundle = {
        "schema": "friday.web-evidence-bundle.v1",
        "research_id": "research-1",
        "authenticated_turn_id": "turn-1",
        "task_topic": "Python public documentation",
        "freshness_requirement": "current",
        "query_plan": ("Python official docs", "Python reference"),
        "sources": (
            {
                "source_id": "source-1",
                "canonical_url": "https://docs.python.org/3/",
                "title": "Python documentation",
                "publisher_domain": "docs.python.org",
                "publication_or_update_date": None,
                "retrieved_at": "2026-09-04T14:00:00Z",
                "source_class": "public",
                "content_digest": "a" * 64,
                "relevant_passage_references": ("section 1",),
            },
        ),
        "claims": (),
        "contradictions": (),
        "missing_evidence": (),
        "coverage": 1.0,
        "retrieved_at": "2026-09-04T14:00:00Z",
        "provider_outcomes": (),
    }
    result = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        evidence_bundle=bundle,
        cited_source_urls=("https://docs.python.org/3/library/",),
    )
    assert result.coverage is WebCitationCoverageState.COMPLETE


def test_coverage_type_is_frozen_and_closed() -> None:
    result = build_web_citation_coverage(
        "coverage-1",
        "turn-1",
        ("https://example.com/source",),
        ("https://example.com/citation",),
    )
    assert isinstance(result, WebCitationCoverageV1)
    with pytest.raises(AttributeError):
        result.coverage = WebCitationCoverageState.EMPTY  # type: ignore[misc]
    with pytest.raises(WebCitationCoverageError):
        WebCitationCoverageV1(
            "coverage-1",
            "turn-1",
            WebCitationCoverageState.COMPLETE,
            0,
            1,
            WebCitationCoverageReason.ALL_ADMITTED_HOSTS_CITED,
        )
