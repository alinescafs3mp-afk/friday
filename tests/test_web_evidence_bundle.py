"""Closed, body-free WebEvidenceBundleV1 contract tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.web_evidence_bundle import (
    WEB_EVIDENCE_BUNDLE_SCHEMA,
    WebEvidenceBundleError,
    WebEvidenceBundleV1,
    WebEvidenceState,
    build_web_evidence_bundle,
    validate_web_evidence_bundle,
)


def _source(index: int = 1, *, publication_or_update_date: str | None = "2026-09-03") -> dict[str, object]:
    return {
        "source_id": f"S{index}",
        "canonical_url": f"https://example{index}.com/reference",
        "title": f"Public reference {index}",
        "publisher_domain": f"example{index}.com",
        "publication_or_update_date": publication_or_update_date,
        "retrieved_at": "2026-09-04T13:00:00Z",
        "source_class": "official",
        "content_digest": f"{index:064x}",
        "relevant_passage_references": [f"paragraph:{index}"],
    }


def _payload() -> dict[str, object]:
    return {
        "schema": WEB_EVIDENCE_BUNDLE_SCHEMA,
        "research_id": "research:2026-09-04:1",
        "authenticated_turn_id": "turn:2026-09-04:1",
        "task_topic": "current public documentation changes",
        "freshness_requirement": "current",
        "query_plan": [
            "official documentation changes in 2026",
            "independent release notes and migration guidance in 2026",
        ],
        "sources": [_source(1), _source(2)],
        "claims": [
            {
                "claim_id": "C1",
                "normalized_claim": "The documented behavior changed in the current release.",
                "supporting_source_ids": ["S1", "S2"],
                "contradicting_source_ids": [],
                "evidence_state": WebEvidenceState.SUPPORTED.value,
                "current_sensitive": True,
            }
        ],
        "contradictions": [],
        "missing_evidence": ["Independent production confirmation is missing."],
        "coverage": 0.75,
        "retrieved_at": "2026-09-04T13:05:00+00:00",
        "provider_outcomes": [{"provider_id": "primary", "status": "success", "source_count": 2}],
    }


def test_builder_returns_frozen_body_free_bundle_with_all_preferred_fields() -> None:
    bundle = build_web_evidence_bundle(_payload())

    assert isinstance(bundle, WebEvidenceBundleV1)
    assert bundle.research_id == "research:2026-09-04:1"
    assert bundle.authenticated_turn_id == "turn:2026-09-04:1"
    assert bundle.query_plan[0].startswith("official")
    assert bundle.sources[0].canonical_url == "https://example1.com/reference"
    assert bundle.sources[0].publication_or_update_date == "2026-09-03"
    assert bundle.sources[0].relevant_passage_references == ("paragraph:1",)
    assert bundle.claims[0].supporting_source_ids == ("S1", "S2")
    assert bundle.claims[0].current_sensitive is True
    assert bundle.provider_outcomes[0].source_count == 2
    assert isinstance(bundle.sources, tuple)
    assert isinstance(bundle.claims, tuple)
    assert "body" not in bundle.to_mapping()

    with pytest.raises(FrozenInstanceError):
        bundle.coverage = 1.0  # type: ignore[misc]


def test_mapping_round_trip_is_stable_and_nested_values_are_immutable() -> None:
    bundle = build_web_evidence_bundle(_payload())
    again = build_web_evidence_bundle(bundle.to_mapping())

    assert again == bundle
    assert again.to_mapping()["schema"] == WEB_EVIDENCE_BUNDLE_SCHEMA
    assert isinstance(again.claims[0].supporting_source_ids, tuple)
    assert isinstance(again.provider_outcomes, tuple)


@pytest.mark.parametrize(
    "query_plan",
    [
        [],
        ["only one query"],
        ["same query", "same   query"],
        [f"public query {index}" for index in range(9)],
    ],
)
def test_query_plan_is_bounded_and_complementary(query_plan: list[str]) -> None:
    payload = _payload()
    payload["query_plan"] = query_plan

    with pytest.raises(WebEvidenceBundleError, match="query_plan"):
        build_web_evidence_bundle(payload)


def test_sources_are_bounded_by_the_existing_research_limit() -> None:
    payload = _payload()
    payload["sources"] = [_source(index) for index in range(1, 10)]

    with pytest.raises(WebEvidenceBundleError, match="sources_bound"):
        build_web_evidence_bundle(payload)


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("summarize /home/owner/report.pdf", "query_path"),
        ("compare private report.pdf with release notes", "query_private_filename"),
        ("inspect https://127.0.0.1/admin", "query_url_non_public_host"),
        ("inspect https://research.example.test/notes", "query_url_non_public_host"),
        ("inspect //127.0.0.1/admin", "query_url_non_public_host"),
    ],
)
def test_queries_reject_private_paths_filenames_and_non_public_urls(query: str, error: str) -> None:
    payload = _payload()
    payload["query_plan"] = [query, "independent public source comparison"]

    with pytest.raises(WebEvidenceBundleError, match=error):
        build_web_evidence_bundle(payload)


@pytest.mark.parametrize(
    "url",
    [
        "file:///home/owner/report.pdf",
        "http://127.0.0.1:8080/private",
        "https://localhost/admin",
        "https://service.internal/report",
        "https://example.test/report",
        "https://user:password@example.com/private",
    ],
)
def test_sources_reject_non_public_or_credential_bearing_urls(url: str) -> None:
    payload = _payload()
    source = _source()
    source["canonical_url"] = url
    payload["sources"] = [source]

    with pytest.raises(WebEvidenceBundleError, match="canonical_url"):
        build_web_evidence_bundle(payload)


def test_public_source_url_and_unknown_public_query_url_are_accepted() -> None:
    payload = _payload()
    payload["query_plan"] = [
        "read https://docs.example.com/release-notes",
        "compare independent public migration guidance",
    ]
    payload["sources"] = [_source(publication_or_update_date=None)]
    payload["claims"] = []

    bundle = build_web_evidence_bundle(payload)

    assert bundle.sources[0].publication_or_update_date is None
    assert validate_web_evidence_bundle(bundle) is True


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "claims",
            [
                {
                    "claim_id": "C1",
                    "normalized_claim": "claim",
                    "supporting_source_ids": ["S9"],
                    "contradicting_source_ids": [],
                    "evidence_state": "unknown",
                    "current_sensitive": False,
                }
            ],
            "claim_source_ids_unknown",
        ),
        ("coverage", 1.1, "coverage_range"),
        ("retrieved_at", "2026-09-04T13:05:00", "retrieved_at_timezone"),
    ],
)
def test_cross_references_and_closed_metadata_fail_closed(field: str, value: object, error: str) -> None:
    payload = deepcopy(_payload())
    payload[field] = value

    with pytest.raises(WebEvidenceBundleError, match=error):
        build_web_evidence_bundle(payload)


def test_validator_returns_false_without_network_or_file_access() -> None:
    payload = _payload()
    assert validate_web_evidence_bundle(payload) is True

    malformed = deepcopy(payload)
    malformed["query_plan"] = ["private secret.pdf", "another public query"]
    assert validate_web_evidence_bundle(malformed) is False
