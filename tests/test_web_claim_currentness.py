from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.web_claim_currentness import (
    WEB_CLAIM_CURRENTNESS_SCHEMA,
    WebClaimCurrentnessError,
    WebClaimCurrentnessReason,
    WebClaimCurrentnessState,
    WebClaimCurrentnessV1,
    build_web_claim_currentness,
    validate_web_claim_currentness,
)
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_evidence_bundle import WebEvidenceClaimV1


def claim(claim_id: str, *, current_sensitive: bool = False) -> WebEvidenceClaimV1:
    return WebEvidenceClaimV1(
        claim_id=claim_id,
        normalized_claim=f"claim {claim_id}",
        supporting_source_ids=(),
        contradicting_source_ids=(),
        evidence_state="supported",
        current_sensitive=current_sensitive,
    )


def build(
    currentness: WebCurrentnessDecision | str,
    claims: tuple[WebEvidenceClaimV1, ...] = (),
) -> WebClaimCurrentnessV1:
    return build_web_claim_currentness("currentness:1", "turn:1", currentness, claims)


def test_empty_claims_are_empty_not_admitted() -> None:
    result = build(WebCurrentnessDecision.SEARCH_NOT_REQUIRED)

    assert result.admission is WebClaimCurrentnessState.EMPTY
    assert result.reason is WebClaimCurrentnessReason.NO_CLAIMS
    assert result.current_sensitive_claim_count == 0
    assert result.claim_count == 0


def test_private_currentness_blocks_without_exposing_claim_counts() -> None:
    result = build(WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE, (claim("claim:1"),))

    assert result.admission is WebClaimCurrentnessState.BLOCKED
    assert result.reason is WebClaimCurrentnessReason.CURRENTNESS_PRIVATE
    assert result.current_sensitive_claim_count == 0
    assert result.claim_count == 0


def test_non_sensitive_claims_are_admitted_without_currentness_search() -> None:
    result = build(WebCurrentnessDecision.SEARCH_NOT_REQUIRED, (claim("claim:1"),))

    assert result.admission is WebClaimCurrentnessState.ADMITTED
    assert result.reason is WebClaimCurrentnessReason.NO_CURRENT_SENSITIVE_CLAIMS
    assert result.current_sensitive_claim_count == 0
    assert result.claim_count == 1


def test_current_sensitive_claims_are_admitted_when_search_is_required() -> None:
    result = build(
        WebCurrentnessDecision.SEARCH_REQUIRED,
        (claim("claim:1", current_sensitive=True), claim("claim:2")),
    )

    assert result.admission is WebClaimCurrentnessState.ADMITTED
    assert result.reason is WebClaimCurrentnessReason.CURRENTNESS_REQUIRED
    assert result.current_sensitive_claim_count == 1
    assert result.claim_count == 2


def test_current_sensitive_claims_hold_when_search_is_not_required() -> None:
    result = build(
        WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
        (claim("claim:1", current_sensitive=True),),
    )

    assert result.admission is WebClaimCurrentnessState.HOLD
    assert result.reason is WebClaimCurrentnessReason.CURRENTNESS_NOT_REQUIRED
    assert result.current_sensitive_claim_count == 1
    assert result.claim_count == 1


def test_invalid_currentness_facts_fail_closed() -> None:
    result = build("invented-currentness", (claim("claim:1"),))

    assert result.admission is WebClaimCurrentnessState.BLOCKED
    assert result.reason is WebClaimCurrentnessReason.FACTS_INVALID
    assert result.current_sensitive_claim_count == 0
    assert result.claim_count == 0


def test_invalid_claim_facts_fail_closed_without_partial_counts() -> None:
    result = build(
        WebCurrentnessDecision.SEARCH_REQUIRED,
        (  # type: ignore[arg-type]
            {
                "claim_id": "claim:1",
                "normalized_claim": "public claim",
                "supporting_source_ids": (),
                "contradicting_source_ids": (),
                "evidence_state": "supported",
                "current_sensitive": "yes",
            },
        ),
    )

    assert result.admission is WebClaimCurrentnessState.BLOCKED
    assert result.reason is WebClaimCurrentnessReason.FACTS_INVALID
    assert result.current_sensitive_claim_count == 0
    assert result.claim_count == 0


def test_duplicate_claim_ids_are_blocked() -> None:
    result = build(
        WebCurrentnessDecision.SEARCH_REQUIRED,
        (claim("claim:1"), claim("claim:1", current_sensitive=True)),
    )

    assert result.admission is WebClaimCurrentnessState.BLOCKED
    assert result.reason is WebClaimCurrentnessReason.FACTS_INVALID
    assert result.claim_count == 0


def test_mapping_claims_and_result_round_trip() -> None:
    result = build_web_claim_currentness(
        {
            "schema": WEB_CLAIM_CURRENTNESS_SCHEMA,
            "currentness_id": "currentness:mapping",
            "authenticated_turn_id": "turn:mapping",
            "currentness": "search_required",
            "claims": [claim("claim:1", current_sensitive=True).to_mapping()],
        }
    )

    assert result.admission is WebClaimCurrentnessState.ADMITTED
    encoded = result.to_mapping()
    assert build_web_claim_currentness(encoded) == result
    assert validate_web_claim_currentness(encoded) is True


def test_frozen_result_and_closed_constructor_fields() -> None:
    result = build(WebCurrentnessDecision.SEARCH_NOT_REQUIRED, (claim("claim:1"),))

    with pytest.raises(FrozenInstanceError):
        result.claim_count = 2  # type: ignore[misc]
    with pytest.raises(WebClaimCurrentnessError):
        WebClaimCurrentnessV1(
            "currentness:bad",
            "turn:bad",
            WebClaimCurrentnessState.ADMITTED,
            0,
            0,
            WebClaimCurrentnessReason.NO_CLAIMS,
        )


def test_validator_rejects_wrong_schema_unknown_fields_and_bad_closed_values() -> None:
    result = build(WebCurrentnessDecision.SEARCH_NOT_REQUIRED, (claim("claim:1"),))
    encoded = result.to_mapping()

    wrong_schema = {**encoded, "schema": "invented"}
    unknown_field = {**encoded, "extra": "nope"}
    bad_reason = {**encoded, "reason": "invented"}
    assert validate_web_claim_currentness(wrong_schema) is False
    assert validate_web_claim_currentness(unknown_field) is False
    assert validate_web_claim_currentness(bad_reason) is False


def test_validator_requires_the_serialized_v1_shape() -> None:
    result = build(WebCurrentnessDecision.SEARCH_NOT_REQUIRED)
    encoded = result.to_mapping()

    assert (
        validate_web_claim_currentness({key: value for key, value in encoded.items() if key != "schema"})
        is False
    )
    assert validate_web_claim_currentness({**encoded, "state": encoded["admission"]}) is False


def test_builder_does_not_reclassify_currentness(monkeypatch: pytest.MonkeyPatch) -> None:
    import friday.orchestration.web_currentness_policy as policy

    monkeypatch.setattr(
        policy, "classify_web_currentness", lambda *_args, **_kwargs: pytest.fail("reclassified")
    )

    result = build(WebCurrentnessDecision.SEARCH_REQUIRED, (claim("claim:1", current_sensitive=True),))

    assert result.admission is WebClaimCurrentnessState.ADMITTED


def test_serialized_output_uses_the_frozen_schema() -> None:
    result = build(WebCurrentnessDecision.SEARCH_NOT_REQUIRED)

    assert result.to_mapping()["schema"] == WEB_CLAIM_CURRENTNESS_SCHEMA
