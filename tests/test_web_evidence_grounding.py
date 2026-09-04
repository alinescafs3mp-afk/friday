from __future__ import annotations

import pytest

from friday.orchestration.web_evidence_bundle import (
    WebEvidenceBundleV1,
    WebEvidenceClaimV1,
    WebEvidenceSourceV1,
    WebProviderOutcomeV1,
)
from friday.orchestration.web_evidence_grounding import (
    WebEvidenceGroundingError,
    WebEvidenceGroundingReason,
    WebEvidenceGroundingState,
    WebEvidenceGroundingV1,
    build_web_evidence_grounding,
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
) -> WebEvidenceGroundingV1:
    return build_web_evidence_grounding(
        "grounding-1",
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
        claims=(claim("claim-1", contradicting=("source-1",)),),
        contradictions=(),
        missing_evidence=(),
        coverage=1.0,
        retrieved_at="2026-09-04T14:00:00Z",
        provider_outcomes=(WebProviderOutcomeV1("yandex", "completed", 1),),
    )


def test_all_claims_grounded_are_grounded_and_frozen() -> None:
    result = build((claim("claim-1", supporting=("source-1",)),))
    assert result.grounding is WebEvidenceGroundingState.GROUNDED
    assert result.grounded_claim_count == 1
    assert result.claim_count == 1
    assert result.reason is WebEvidenceGroundingReason.ALL_CLAIMS_GROUNDED
    with pytest.raises(AttributeError):
        result.claim_count = 2  # type: ignore[misc]


def test_contradiction_only_claim_is_grounded() -> None:
    result = build((claim("claim-1", contradicting=("source-1",)),))
    assert result.grounding is WebEvidenceGroundingState.GROUNDED
    assert result.grounded_claim_count == 1


def test_supporting_only_claim_is_grounded_without_being_contradiction() -> None:
    result = build((claim("claim-1", supporting=("source-1",)),))
    assert result.grounding is WebEvidenceGroundingState.GROUNDED


def test_partial_counts_only_claims_with_an_admitted_binding() -> None:
    result = build(
        (
            claim("claim-1", supporting=("source-1",)),
            claim("claim-2"),
        )
    )
    assert result.grounding is WebEvidenceGroundingState.PARTIAL
    assert result.grounded_claim_count == 1
    assert result.claim_count == 2


def test_empty_claims_are_empty_not_grounded() -> None:
    result = build(())
    assert result.grounding is WebEvidenceGroundingState.EMPTY
    assert result.grounded_claim_count == 0
    assert result.claim_count == 0
    assert result.reason is WebEvidenceGroundingReason.NO_CLAIMS


def test_claims_without_admitted_bindings_are_ungrounded() -> None:
    result = build((claim("claim-1"),))
    assert result.grounding is WebEvidenceGroundingState.UNGROUNDED
    assert result.grounded_claim_count == 0
    assert result.reason is WebEvidenceGroundingReason.NO_GROUNDED_CLAIMS


def test_unknown_source_id_is_blocked() -> None:
    result = build((claim("claim-1", supporting=("unknown-source",)),))
    assert result.grounding is WebEvidenceGroundingState.BLOCKED
    assert result.reason is WebEvidenceGroundingReason.UNKNOWN_SOURCE_ID
    assert result.grounded_claim_count == 0
    assert result.claim_count == 0


def test_private_source_url_is_blocked_without_becoming_grounding() -> None:
    result = build_web_evidence_grounding(
        "grounding-1",
        "turn-1",
        claims=(claim("claim-1", supporting=("source-1",)),),
        admitted_source_ids=("source-1",),
        admitted_source_urls=("http://127.0.0.1:8000/source",),
    )
    assert result.grounding is WebEvidenceGroundingState.BLOCKED
    assert result.reason is WebEvidenceGroundingReason.PRIVATE_SOURCE_URL


def test_credential_url_and_invalid_bundle_are_blocked() -> None:
    credential = build_web_evidence_grounding(
        "grounding-1",
        "turn-1",
        claims=(),
        admitted_source_ids=(),
        admitted_source_urls=("https://example.com/source?api_key=secret",),
    )
    assert credential.grounding is WebEvidenceGroundingState.BLOCKED
    assert credential.reason is WebEvidenceGroundingReason.PRIVATE_SOURCE_URL
    invalid = build_web_evidence_grounding("grounding-1", "turn-1", evidence_bundle={"sources": ()})
    assert invalid.grounding is WebEvidenceGroundingState.BLOCKED
    assert invalid.reason is WebEvidenceGroundingReason.INVALID_BUNDLE


def test_claim_mapping_and_equivalent_source_facts_are_grounded() -> None:
    result = build_web_evidence_grounding(
        "grounding-1",
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
    assert result.grounding is WebEvidenceGroundingState.GROUNDED


def test_built_evidence_bundle_supplies_admitted_sources_and_claims() -> None:
    result = build_web_evidence_grounding("grounding-1", "turn-1", evidence_bundle=evidence_bundle())
    assert result.grounding is WebEvidenceGroundingState.GROUNDED
    assert result.grounded_claim_count == 1


def test_identity_and_closed_result_fields_are_validated() -> None:
    with pytest.raises(WebEvidenceGroundingError):
        build_web_evidence_grounding("/private", "turn-1", claims=(), admitted_source_ids=())
    with pytest.raises(WebEvidenceGroundingError):
        WebEvidenceGroundingV1(
            "grounding-1",
            "turn-1",
            WebEvidenceGroundingState.GROUNDED,
            0,
            1,
            WebEvidenceGroundingReason.ALL_CLAIMS_GROUNDED,
        )
