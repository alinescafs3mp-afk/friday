from __future__ import annotations

import pytest

from friday.orchestration.web_claim_support import (
    WebClaimSupportError,
    WebClaimSupportReason,
    WebClaimSupportState,
    WebClaimSupportV1,
    build_web_claim_support,
)
from friday.orchestration.web_evidence_bundle import WebEvidenceClaimV1


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
) -> WebClaimSupportV1:
    return build_web_claim_support(
        "support-1",
        "turn-1",
        claims=claims,
        admitted_source_ids=source_ids,
    )


def test_supported_claims_are_complete_and_frozen() -> None:
    result = build((claim("claim-1", ("source-1",)),))
    assert result.support is WebClaimSupportState.COMPLETE
    assert result.supported_claim_count == 1
    assert result.claim_count == 1
    assert result.reason is WebClaimSupportReason.ALL_CLAIMS_SUPPORTED
    with pytest.raises(AttributeError):
        result.claim_count = 2  # type: ignore[misc]


def test_contradicting_only_claim_is_not_support() -> None:
    result = build((claim("claim-1", contradicting=("source-1",)),))
    assert result.support is WebClaimSupportState.UNSUPPORTED
    assert result.supported_claim_count == 0
    assert result.reason is WebClaimSupportReason.NO_CLAIM_SUPPORT


def test_partial_support_counts_only_admitted_supporting_ids() -> None:
    result = build(
        (
            claim("claim-1", ("source-1",)),
            claim("claim-2"),
        )
    )
    assert result.support is WebClaimSupportState.PARTIAL
    assert result.supported_claim_count == 1
    assert result.claim_count == 2


def test_empty_claims_are_empty_not_complete() -> None:
    result = build(())
    assert result.support is WebClaimSupportState.EMPTY
    assert result.supported_claim_count == 0
    assert result.claim_count == 0
    assert result.reason is WebClaimSupportReason.NO_CLAIMS


def test_unknown_source_id_is_blocked() -> None:
    result = build((claim("claim-1", ("unknown-source",)),))
    assert result.support is WebClaimSupportState.BLOCKED
    assert result.reason is WebClaimSupportReason.UNKNOWN_SOURCE_ID
    assert result.supported_claim_count == 0
    assert result.claim_count == 0


def test_private_source_url_is_blocked_without_becoming_support() -> None:
    result = build_web_claim_support(
        "support-1",
        "turn-1",
        claims=(claim("claim-1", ("source-1",)),),
        admitted_source_ids=("source-1",),
        admitted_source_urls=("http://127.0.0.1:8000/source",),
    )
    assert result.support is WebClaimSupportState.BLOCKED
    assert result.reason is WebClaimSupportReason.PRIVATE_SOURCE_URL


def test_credential_bearing_url_and_invalid_bundle_are_blocked() -> None:
    credential = build_web_claim_support(
        "support-1",
        "turn-1",
        claims=(),
        admitted_source_ids=(),
        admitted_source_urls=("https://example.com/source?api_key=secret",),
    )
    assert credential.support is WebClaimSupportState.BLOCKED
    assert credential.reason is WebClaimSupportReason.PRIVATE_SOURCE_URL
    invalid = build_web_claim_support("support-1", "turn-1", evidence_bundle={"sources": ()})
    assert invalid.support is WebClaimSupportState.BLOCKED
    assert invalid.reason is WebClaimSupportReason.INVALID_BUNDLE


def test_claim_mapping_and_equivalent_source_facts_are_supported() -> None:
    result = build_web_claim_support(
        "support-1",
        "turn-1",
        claims=(
            {
                "claim_id": "claim-1",
                "normalized_claim": "public claim",
                "supporting_source_ids": ("source-1",),
                "contradicting_source_ids": (),
                "evidence_state": "supported",
                "current_sensitive": False,
            },
        ),
        admitted_source_facts=({"source_id": "source-1", "canonical_url": "https://docs.python.org/3/"},),
    )
    assert result.support is WebClaimSupportState.COMPLETE


def test_unknown_evidence_state_is_not_support() -> None:
    result = build((claim("claim-1", ("source-1",), state="unknown"),))
    assert result.support is WebClaimSupportState.UNSUPPORTED


def test_identity_and_closed_result_fields_are_validated() -> None:
    with pytest.raises(WebClaimSupportError):
        build_web_claim_support("/private", "turn-1", claims=(), admitted_source_ids=())
    with pytest.raises(WebClaimSupportError):
        WebClaimSupportV1(
            "support-1",
            "turn-1",
            WebClaimSupportState.COMPLETE,
            0,
            1,
            WebClaimSupportReason.ALL_CLAIMS_SUPPORTED,
        )
