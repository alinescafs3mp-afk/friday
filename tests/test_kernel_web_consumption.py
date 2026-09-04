from __future__ import annotations

from friday.execution_kernel.web_consumption import (
    _web_requested_url_blocked_private,
    _web_research_empty_source_refusal,
    _web_research_private_source_refusal,
    _web_research_provider_consumption_refusal,
    _web_search_consumption_failure,
    _web_search_result_urls,
)
from friday.web_surfer import SEARCH_FILTER_ATTESTATION_KEY


def test_private_fetch_url_is_blocked_before_outbound() -> None:
    assert _web_requested_url_blocked_private("https://ok.example/1") is True
    assert _web_requested_url_blocked_private("https://127.0.0.1/secret") is True
    assert _web_requested_url_blocked_private("https://example.com/1") is False
    assert _web_requested_url_blocked_private("") is False


def test_search_result_urls_ignore_non_mapping_rows() -> None:
    assert _web_search_result_urls(
        [{"url": "https://example.com/a"}, "nope", {"url": "  "}, {"href": "https://example.com/b"}]
    ) == ("https://example.com/a",)


def test_private_research_sources_refuse_after_observation() -> None:
    report = {
        "query": "needle",
        "sources": [{"url": "https://docs.example.test/a"}],
        "outbound_attempted": True,
        "freshness": "week",
    }
    refusal = _web_research_private_source_refusal(report, "needle")
    assert refusal is not None
    assert refusal["error"] == "source_fact_private"
    assert refusal["sources"] == []
    assert refusal["search_failed"] is True
    assert refusal["freshness"] == "week"
    assert (
        _web_research_private_source_refusal(
            {"sources": [{"url": "https://docs.python.org/3/"}], "outbound_attempted": True},
            "needle",
        )
        is None
    )


def test_empty_sources_after_outbound_are_not_completeness() -> None:
    empty = {"sources": [], "outbound_attempted": True, "source_class": "foreign"}
    refusal = _web_research_empty_source_refusal(empty, "needle")
    assert refusal is not None
    assert refusal["error"] == "no_admitted_sources"
    assert refusal["source_class"] == "foreign"
    assert _web_research_empty_source_refusal({"sources": [], "outbound_attempted": False}, "needle") is None
    assert (
        _web_research_empty_source_refusal(
            {"sources": [{"url": "https://example.com/a"}], "outbound_attempted": True},
            "needle",
        )
        is None
    )


def test_invalid_selected_provider_is_refused_without_inventing_one() -> None:
    public = {
        "sources": [{"url": "https://example.com/a"}],
        "outbound_attempted": True,
    }
    assert _web_research_provider_consumption_refusal(public, "needle") is None
    invalid = {**public, "selected_provider_id": "not-a-closed-provider"}
    refusal = _web_research_provider_consumption_refusal(invalid, "needle")
    assert refusal is not None
    assert refusal["error"] == "provider_facts_invalid"
    assert refusal["sources"] == []


def test_search_consumption_failure_keeps_results_empty_and_freshness() -> None:
    refusal = _web_search_consumption_failure(
        "needle",
        {
            "freshness": "day",
            SEARCH_FILTER_ATTESTATION_KEY: {"freshness": "day"},
            "results": [{"url": "https://intranet.local/x"}],
        },
        error="source_fact_private",
    )
    assert refusal["results"] == []
    assert refusal["search_failed"] is True
    assert refusal["freshness"] == "day"
    assert refusal[SEARCH_FILTER_ATTESTATION_KEY] == {"freshness": "day"}
    assert "sources" not in refusal
