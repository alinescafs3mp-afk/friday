from __future__ import annotations

import pytest

from friday.orchestration.web_contradiction_coverage import (
    WebContradictionCoverageError,
    WebContradictionCoverageReason,
    WebContradictionCoverageState,
    WebContradictionCoverageV1,
    build_web_contradiction_coverage,
)
from friday.orchestration.web_evidence_bundle import (
    WebEvidenceBundleV1,
    WebEvidenceClaimV1,
    WebEvidenceSourceV1,
    WebProviderOutcomeV1,
)


def claim(
    claim_id: str,
    supporting: tuple[str, ...] = (),
    contradicting: tuple[str, ...] = (),
    state: str = "supported",
) -> WebEvidenceClaimV1:
    return WebEvidenceClaimV1(
        claim_id=claim_id,
        normalized_claim=f"claim {claim_id}",
        supporting_source_ids=supporting,
        contradicting_source_ids=contradicting,
        evidence_state=state,
        current_sensitive=False,
    )


def build(
    claims: tuple[WebEvidenceClaimV1, ...], source_ids: tuple[str, ...] = ("source-1",)
) -> WebContradictionCoverageV1:
    return build_web_contradiction_coverage(
        "coverage-1",
        "turn-1",
        claims=claims,
        admitted_source_ids=source_ids,
    )


def evidence_bundle() -> WebEvidenceBundleV1:
    source = WebEvidenceSourceV1(
        source_id="source-1",
        canonical_url="https://docs.python.org/3/",
        title="Python documentation",
        publisher_domain="docs.python.org",
        publication_or_update_date=None,
        retrieved_at="2026-09-04T14:00:00Z",
        source_class="public",
        content_digest="a" * 64,
        relevant_passage_references=("intro",),
    )
    return WebEvidenceBundleV1(
        research_id="research-1",
        authenticated_turn_id="turn-1",
        task_topic="Python public documentation",
        freshness_requirement="current",
        query_plan=("Python documentation", "Python reference"),
        sources=(source,),
        claims=(claim("claim-1", contradicting=("source-1",), state="contradicted"),),
        contradictions=(),
        missing_evidence=(),
        coverage=1.0,
        retrieved_at="2026-09-04T14:00:00Z",
        provider_outcomes=(WebProviderOutcomeV1("yandex", "completed", 1),),
    )


def test_all_claims_contradicted_are_universal_and_frozen() -> None:
    result = build((claim("claim-1", contradicting=("source-1",)),))
    assert result.coverage is WebContradictionCoverageState.UNIVERSAL
    assert result.contradicted_claim_count == 1
    assert result.claim_count == 1
    assert result.reason is WebContradictionCoverageReason.ALL_CLAIMS_CONTRADICTED
    with pytest.raises(AttributeError):
        result.claim_count = 2  # type: ignore[misc]


def test_built_evidence_bundle_supplies_admitted_sources_and_claims() -> None:
    result = build_web_contradiction_coverage("coverage-1", "turn-1", evidence_bundle=evidence_bundle())
    assert result.coverage is WebContradictionCoverageState.UNIVERSAL
    assert result.contradicted_claim_count == 1


def test_supporting_only_claim_is_not_contradicted() -> None:
    result = build((claim("claim-1", supporting=("source-1",)),))
    assert result.coverage is WebContradictionCoverageState.NONE
    assert result.contradicted_claim_count == 0
    assert result.reason is WebContradictionCoverageReason.NO_CONTRADICTED_CLAIMS


def test_present_counts_only_admitted_contradicting_ids() -> None:
    result = build(
        (
            claim("claim-1", contradicting=("source-1",)),
            claim("claim-2"),
        )
    )
    assert result.coverage is WebContradictionCoverageState.PRESENT
    assert result.contradicted_claim_count == 1
    assert result.claim_count == 2


def test_empty_claims_are_empty_not_none() -> None:
    result = build(())
    assert result.coverage is WebContradictionCoverageState.EMPTY
    assert result.contradicted_claim_count == 0
    assert result.claim_count == 0
    assert result.reason is WebContradictionCoverageReason.NO_CLAIMS


def test_unknown_source_id_is_blocked() -> None:
    result = build((claim("claim-1", contradicting=("unknown-source",)),))
    assert result.coverage is WebContradictionCoverageState.BLOCKED
    assert result.reason is WebContradictionCoverageReason.UNKNOWN_SOURCE_ID
    assert result.contradicted_claim_count == 0
    assert result.claim_count == 0


def test_private_source_url_is_blocked_without_becoming_coverage() -> None:
    result = build_web_contradiction_coverage(
        "coverage-1",
        "turn-1",
        claims=(claim("claim-1", contradicting=("source-1",)),),
        admitted_source_ids=("source-1",),
        admitted_source_urls=("http://127.0.0.1:8000/source",),
    )
    assert result.coverage is WebContradictionCoverageState.BLOCKED
    assert result.reason is WebContradictionCoverageReason.PRIVATE_SOURCE_URL


def test_credential_url_and_invalid_bundle_are_blocked() -> None:
    credential = build_web_contradiction_coverage(
        "coverage-1",
        "turn-1",
        claims=(),
        admitted_source_ids=(),
        admitted_source_urls=("https://example.com/source?api_key=secret",),
    )
    assert credential.coverage is WebContradictionCoverageState.BLOCKED
    assert credential.reason is WebContradictionCoverageReason.PRIVATE_SOURCE_URL
    invalid = build_web_contradiction_coverage("coverage-1", "turn-1", evidence_bundle={"sources": ()})
    assert invalid.coverage is WebContradictionCoverageState.BLOCKED
    assert invalid.reason is WebContradictionCoverageReason.INVALID_BUNDLE


def test_claim_mapping_and_equivalent_source_facts_are_contradicted() -> None:
    result = build_web_contradiction_coverage(
        "coverage-1",
        "turn-1",
        claims=(
            {
                "claim_id": "claim-1",
                "normalized_claim": "public claim",
                "supporting_source_ids": (),
                "contradicting_source_ids": ("source-1",),
                "evidence_state": "contradicted",
                "current_sensitive": False,
            },
        ),
        admitted_source_facts=({"source_id": "source-1", "canonical_url": "https://docs.python.org/3/"},),
    )
    assert result.coverage is WebContradictionCoverageState.UNIVERSAL


def test_admitted_contradicting_source_counts_as_contradiction() -> None:
    result = build(
        (claim("claim-1", contradicting=("source-2",)),),
        source_ids=("source-1", "source-2"),
    )
    assert result.coverage is WebContradictionCoverageState.UNIVERSAL


def test_identity_and_closed_result_fields_are_validated() -> None:
    with pytest.raises(WebContradictionCoverageError):
        build_web_contradiction_coverage("/private", "turn-1", claims=(), admitted_source_ids=())
    with pytest.raises(WebContradictionCoverageError):
        WebContradictionCoverageV1(
            "coverage-1",
            "turn-1",
            WebContradictionCoverageState.UNIVERSAL,
            0,
            1,
            WebContradictionCoverageReason.ALL_CLAIMS_CONTRADICTED,
        )
