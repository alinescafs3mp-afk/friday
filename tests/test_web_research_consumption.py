from __future__ import annotations

import pytest

from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_provider_policy import WebProviderDecision, WebProviderSelection
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionError,
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
    build_web_research_consumption,
)


def selection(
    decision: WebProviderDecision,
    provider_id: str | None = "yandex",
    source_count: int = 1,
    *,
    used_fallback: bool = False,
) -> WebProviderSelection:
    return WebProviderSelection(
        decision=decision,
        selected_provider_id=provider_id,
        source_count=source_count,
        direct_source_count=0,
        requested_sources=source_count,
        completed_sources=source_count,
        failed_sources=0,
        timed_out_sources=0,
        used_fallback=used_fallback,
    )


def build(provider: WebProviderSelection | None, **kwargs: object) -> WebResearchConsumptionV1:
    return build_web_research_consumption(
        "consumption-1",
        "turn-1",
        WebCurrentnessDecision.SEARCH_REQUIRED,
        provider,
        **kwargs,
    )


def test_primary_ok_is_consumable_and_frozen() -> None:
    result = build(selection(WebProviderDecision.PRIMARY_OK))
    assert result.usability is WebResearchConsumptionState.CONSUMABLE
    assert result.reason is WebResearchConsumptionReason.PRIMARY_SOURCES
    assert result.selected_provider_id == "yandex"
    assert result.admitted_source_count == 1
    with pytest.raises(AttributeError):
        result.admitted_source_count = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("decision", "reason"),
    (
        (WebProviderDecision.FALLBACK_USED, WebResearchConsumptionReason.FALLBACK_SOURCES),
        (WebProviderDecision.DEGRADED_PARTIAL, WebResearchConsumptionReason.PARTIAL_SOURCES),
    ),
)
def test_fallback_and_partial_are_degraded_at_most(
    decision: WebProviderDecision, reason: WebResearchConsumptionReason
) -> None:
    result = build(
        selection(decision, "wikipedia", used_fallback=decision is WebProviderDecision.FALLBACK_USED)
    )
    assert result.usability is WebResearchConsumptionState.CONSUMABLE_DEGRADED
    assert result.reason is reason


def test_blocked_currentness_cannot_become_consumable() -> None:
    result = build_web_research_consumption(
        "consumption-1",
        "turn-1",
        WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE,
        selection(WebProviderDecision.PRIMARY_OK),
    )
    assert result.usability is WebResearchConsumptionState.BLOCKED_PRIVATE
    assert result.selected_provider_id is None
    assert result.admitted_source_count == 0


def test_unavailable_provider_and_zero_sources_are_never_empty_success() -> None:
    assert build(None).usability is WebResearchConsumptionState.UNAVAILABLE
    result = build(selection(WebProviderDecision.UNAVAILABLE, None, 0))
    assert result.usability is WebResearchConsumptionState.UNAVAILABLE
    assert result.reason is WebResearchConsumptionReason.NO_ADMITTED_SOURCES
    assert result.selected_provider_id is None


@pytest.mark.parametrize(
    "topic",
    (
        "/home/user/report.txt",
        "Compare report.pdf with the latest public page",
        "Use private_id_abc123 for the current result",
        "What is the current answer in this attached document?",
        "Check https://localhost/private",
    ),
)
def test_private_topic_facts_block_without_leaking_a_provider(topic: str) -> None:
    result = build(selection(WebProviderDecision.PRIMARY_OK), topic=topic)
    assert result.usability is WebResearchConsumptionState.BLOCKED_PRIVATE
    assert result.reason is WebResearchConsumptionReason.TOPIC_PRIVATE
    assert result.selected_provider_id is None


@pytest.mark.parametrize(
    "urls",
    (
        ("http://127.0.0.1:8000/source",),
        ("https://example.com/source?api_key=secret",),
        ("https://user:password@example.com/source",),
        ("https://docs.example.test/source",),
    ),
)
def test_private_or_credential_source_urls_block(urls: tuple[str, ...]) -> None:
    result = build(selection(WebProviderDecision.PRIMARY_OK), source_urls=urls)
    assert result.usability is WebResearchConsumptionState.BLOCKED_PRIVATE
    assert result.reason is WebResearchConsumptionReason.SOURCE_FACT_PRIVATE


def test_public_source_urls_are_optional_and_count_attested() -> None:
    result = build(
        selection(WebProviderDecision.PRIMARY_OK, source_count=2),
        source_urls=("https://docs.python.org/3/", "https://www.python.org/"),
    )
    assert result.usability is WebResearchConsumptionState.CONSUMABLE
    mismatched = build(
        selection(WebProviderDecision.PRIMARY_OK, source_count=2),
        source_urls=("https://docs.python.org/3/",),
    )
    assert mismatched.usability is WebResearchConsumptionState.UNAVAILABLE
    assert mismatched.reason is WebResearchConsumptionReason.EVIDENCE_MISMATCH


def test_evidence_bundle_is_optional_but_must_match_observed_sources() -> None:
    bundle = {
        "schema": "friday.web-evidence-bundle.v1",
        "research_id": "research-1",
        "authenticated_turn_id": "turn-1",
        "task_topic": "Python public documentation",
        "freshness_requirement": "current",
        "query_plan": ("Python 3.14 official docs", "Python 3.14 reference"),
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
        "provider_outcomes": ({"provider_id": "yandex", "status": "completed", "source_count": 1},),
    }
    result = build(selection(WebProviderDecision.PRIMARY_OK), evidence_bundle=bundle)
    assert result.usability is WebResearchConsumptionState.CONSUMABLE
    mismatch = build(selection(WebProviderDecision.PRIMARY_OK, source_count=2), evidence_bundle=bundle)
    assert mismatch.usability is WebResearchConsumptionState.UNAVAILABLE
    assert mismatch.reason is WebResearchConsumptionReason.EVIDENCE_MISMATCH


def test_private_evidence_topic_blocks_before_invalid_provider_can_succeed() -> None:
    result = build_web_research_consumption(
        "consumption-1",
        "turn-1",
        WebCurrentnessDecision.SEARCH_REQUIRED,
        selection(WebProviderDecision.PRIMARY_OK),
        evidence_bundle={"task_topic": "мой документ report.pdf", "sources": ()},
    )
    assert result.usability is WebResearchConsumptionState.BLOCKED_PRIVATE
    assert result.reason is WebResearchConsumptionReason.SOURCE_FACT_PRIVATE


def test_invalid_evidence_is_unavailable_and_unknown_provider_facts_fail_closed() -> None:
    invalid_evidence = build(selection(WebProviderDecision.PRIMARY_OK), evidence_bundle={"sources": ()})
    assert invalid_evidence.usability is WebResearchConsumptionState.UNAVAILABLE
    assert invalid_evidence.reason is WebResearchConsumptionReason.EVIDENCE_INVALID
    invalid_provider = build({"decision": "primary_ok", "selected_provider_id": "unknown", "source_count": 1})
    assert invalid_provider.usability is WebResearchConsumptionState.UNAVAILABLE
    assert invalid_provider.reason is WebResearchConsumptionReason.PROVIDER_FACTS_INVALID


def test_not_required_can_consume_already_observed_public_evidence() -> None:
    result = build_web_research_consumption(
        "consumption-1",
        "turn-1",
        WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
        selection(WebProviderDecision.PRIMARY_OK),
    )
    assert result.usability is WebResearchConsumptionState.CONSUMABLE


def test_identity_and_closed_fields_are_validated() -> None:
    with pytest.raises(WebResearchConsumptionError):
        build_web_research_consumption("/private/id", "turn-1", "search_required", None)
    with pytest.raises(WebResearchConsumptionError):
        build_web_research_consumption("consumption-1", "turn-1", "maybe", None)
    with pytest.raises(WebResearchConsumptionError):
        WebResearchConsumptionV1(
            "consumption-1",
            "turn-1",
            WebResearchConsumptionState.CONSUMABLE,
            "yandex",
            0,
            WebResearchConsumptionReason.PRIMARY_SOURCES,
        )
