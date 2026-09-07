from __future__ import annotations

import pytest

from friday.execution_kernel.web_consumption import consume_kernel_web_answer
from friday.execution_kernel.web_research_gates import observe_web_research_gates
from friday.orchestration.web_answer_evidence_consumption import (
    WEB_ANSWER_EVIDENCE_CONSUMPTION_SCHEMA,
    WebAnswerEvidenceConsumptionError,
    WebAnswerEvidencePublication,
    WebAnswerEvidenceReason,
    claims_from_web_answer,
    consume_web_answer_evidence,
    published_web_answer,
)
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_evidence_bundle import WebEvidenceClaimV1
from friday.orchestration.web_provider_policy import WebProviderDecision, WebProviderSelection


def _selection(
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


def _claim(
    claim_id: str,
    supporting: tuple[str, ...] = ("s1",),
    *,
    current_sensitive: bool = False,
    contradicting: tuple[str, ...] = (),
    state: str = "supported",
) -> WebEvidenceClaimV1:
    return WebEvidenceClaimV1(
        claim_id=claim_id,
        normalized_claim=f"public claim {claim_id}",
        supporting_source_ids=supporting,
        contradicting_source_ids=contradicting,
        evidence_state=state,
        current_sensitive=current_sensitive,
    )


def test_supported_answer_consumes_the_admitted_bundle_and_is_frozen() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="Python 3.14 is documented at https://docs.python.org/3/.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
        current_sensitive=True,
        provider_selection=_selection(WebProviderDecision.PRIMARY_OK),
    )
    assert result.publication is WebAnswerEvidencePublication.ADMITTED
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_SUPPORTED
    assert result.claim_support == "complete"
    assert result.claim_currentness == "admitted"
    assert result.claim_count == 1
    assert result.supported_claim_count == 1
    assert result.to_mapping()["schema"] == WEB_ANSWER_EVIDENCE_CONSUMPTION_SCHEMA
    with pytest.raises(AttributeError):
        result.claim_count = 2  # type: ignore[misc]
    published = published_web_answer(
        "Python 3.14 is documented at https://docs.python.org/3/.",
        result,
    )
    assert published.startswith("Python 3.14")


def test_ledger_citations_consume_the_bundle_when_the_answer_has_no_url() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="The language reference describes the 3.14 release.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.ADMITTED
    assert result.claim_support == "complete"
    assert result.citation_coverage == "complete"


def test_unsupported_claims_hold_publication() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="A public fact with no admitted source.",
        admitted_source_urls=(),
        currentness=WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_UNSUPPORTED
    assert result.claim_support == "unsupported"
    assert "не подтверждают" in published_web_answer("A public fact with no admitted source.", result)


def test_empty_answer_holds_instead_of_treating_empty_observers_as_complete() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="   ",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_EMPTY
    assert result.claim_support == "empty"


def test_unadmitted_public_url_holds_as_partial_unsupported_claim() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="See https://docs.python.org/3/ and also https://www.python.org/.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_PARTIAL
    assert result.claim_support == "partial"


def test_private_url_in_the_answer_blocks_publication() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="See http://127.0.0.1:8000/secret.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.BLOCKED
    assert result.reason is WebAnswerEvidenceReason.PRIVATE_OR_INVALID
    assert result.claim_count == 0
    assert published_web_answer("See http://127.0.0.1:8000/secret.", result).startswith(
        "Публикация остановлена"
    )


def test_private_path_in_the_answer_blocks_publication() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="The current status of /home/user/report.pdf is green.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.BLOCKED
    assert result.reason is WebAnswerEvidenceReason.PRIVATE_OR_INVALID


def test_bare_filename_in_the_answer_does_not_block_publication() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="The language reference describes the 3.14 release in report.pdf.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.ADMITTED
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_SUPPORTED
    assert "report.pdf" in published_web_answer(
        "The language reference describes the 3.14 release in report.pdf.",
        result,
    )


def test_current_sensitive_claims_hold_without_required_search() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        admitted_source_urls=("https://docs.python.org/3/",),
        claims=(_claim("claim-1", current_sensitive=True),),
        currentness=WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.CURRENTNESS_HOLD
    assert result.claim_currentness == "hold"
    assert "актуальности" in published_web_answer("public claim claim-1", result)


def test_blocked_private_currentness_cannot_publish() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="Latest news about the attached file.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE,
    )
    assert result.publication is WebAnswerEvidencePublication.BLOCKED
    assert result.reason is WebAnswerEvidenceReason.CURRENTNESS_BLOCKED
    assert result.claim_count == 0


def test_fallback_provider_reaches_the_answer_consumer_as_degraded() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="The language reference describes the 3.14 release.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
        provider_selection=_selection(WebProviderDecision.FALLBACK_USED, "brave", used_fallback=True),
    )
    assert result.publication is WebAnswerEvidencePublication.ADMITTED_DEGRADED
    assert result.reason is WebAnswerEvidenceReason.PROVIDER_FALLBACK
    assert result.claim_support == "complete"


def test_unavailable_provider_holds_even_when_claims_look_supported() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        answer="The language reference describes the 3.14 release.",
        admitted_source_urls=("https://docs.python.org/3/",),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
        provider_selection=_selection(WebProviderDecision.UNAVAILABLE, None, 0),
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.PROVIDER_UNAVAILABLE
    assert "канал недоступен" in published_web_answer("ignored", result)


def test_universal_contradiction_holds_publication() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        admitted_source_urls=("https://docs.python.org/3/",),
        claims=(_claim("claim-1", supporting=(), contradicting=("s1",), state="contradicted"),),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.CONTRADICTION_UNIVERSAL
    assert result.contradiction == "universal"


def test_unknown_source_id_blocks_without_exposing_counts() -> None:
    result = consume_web_answer_evidence(
        "answer-1",
        "turn-1",
        admitted_source_urls=("https://docs.python.org/3/",),
        claims=(_claim("claim-1", supporting=("unknown-source",)),),
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.BLOCKED
    assert result.reason is WebAnswerEvidenceReason.UNKNOWN_SOURCE
    assert result.claim_count == 0


def test_claims_from_web_answer_bind_cited_host_and_flag_extra_urls() -> None:
    facts = ({"source_id": "s1", "canonical_url": "https://docs.python.org/3/"},)
    claims = claims_from_web_answer(
        "See https://docs.python.org/3/ and https://www.python.org/.",
        facts,
        current_sensitive=True,
    )
    assert len(claims) == 2
    assert claims[0].supporting_source_ids == ("s1",)
    assert claims[0].current_sensitive is True
    assert claims[1].supporting_source_ids == ()
    assert claims[1].evidence_state == "unverified"


def test_identity_is_validated() -> None:
    with pytest.raises(WebAnswerEvidenceConsumptionError):
        consume_web_answer_evidence("/private", "turn-1", answer="x")
    with pytest.raises(WebAnswerEvidenceConsumptionError):
        consume_web_answer_evidence("answer-1", "bad id", answer="x")


def _kernel_report(
    *,
    selected: str | None = "yandex",
    primary: str | None = "yandex",
    used_fallback: bool = False,
    sources: tuple[str, ...] = ("https://docs.python.org/3/",),
) -> dict[str, object]:
    report: dict[str, object] = {
        "sources": [{"url": url, "title": "Python docs"} for url in sources],
        "provider_used_fallback": used_fallback,
    }
    if selected is not None:
        report["selected_provider_id"] = selected
    if primary is not None:
        report["provider_primary_id"] = primary
    return report


def test_kernel_answer_consumes_an_admitted_research_report() -> None:
    result = consume_kernel_web_answer(
        "What does the language reference say about 3.14?",
        _kernel_report(),
        "The language reference describes the 3.14 release.",
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.ADMITTED
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_SUPPORTED
    assert result.claim_support == "complete"


def test_kernel_answer_marks_configured_fallback_as_degraded() -> None:
    result = consume_kernel_web_answer(
        "What does the language reference say about 3.14?",
        _kernel_report(selected="brave", primary="yandex", used_fallback=True),
        "The language reference describes the 3.14 release.",
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.ADMITTED_DEGRADED
    assert result.reason is WebAnswerEvidenceReason.PROVIDER_FALLBACK


def test_kernel_answer_holds_when_the_report_has_no_admitted_sources() -> None:
    result = consume_kernel_web_answer(
        "What does the language reference say about 3.14?",
        _kernel_report(selected=None, primary=None, sources=()),
        "The language reference describes the 3.14 release.",
        currentness=WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
    )
    assert result.publication is WebAnswerEvidencePublication.HOLD
    assert result.reason is WebAnswerEvidenceReason.CLAIMS_UNSUPPORTED


def test_research_io_observers_keep_empty_claims_after_answer_consumption() -> None:
    report = {
        "query": "needle",
        "sources": [
            {
                "url": "https://docs.python.org/3/",
                "title": "Python docs",
                "text": "Python 3.14 is documented here.",
            }
        ],
        "selected_provider_id": "yandex",
        "provider_primary_id": "yandex",
        "provider_used_fallback": False,
        "requested_sources": 1,
        "completed_sources": 1,
        "failed_sources": 0,
        "timed_out_sources": 0,
        "search_timed_out": False,
        "outbound_attempted": True,
    }
    consume_kernel_web_answer(
        "What does the language reference say about 3.14?",
        report,
        "The language reference describes the 3.14 release.",
        currentness=WebCurrentnessDecision.SEARCH_REQUIRED,
    )
    observed = observe_web_research_gates(
        "needle",
        report,
        executed_queries=("needle",),
        mission=None,
    )
    assert observed["research_claim_support"] == "empty"
    assert observed["research_grounding"] == "empty"
    assert observed["research_contradiction"] == "empty"
