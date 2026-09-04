from __future__ import annotations

import pytest

from friday.orchestration.web_provider_policy import (
    ProviderObservation,
    WebProviderDecision,
    WebProviderPolicyError,
    WebProviderStatus,
    select_web_provider,
    validate_public_web_url,
)
from friday.web_research_contract import (
    MAX_DIRECT_RESEARCH_SOURCES,
    MAX_RESEARCH_ATTEMPTS,
    MAX_RESEARCH_SOURCES,
    research_attempt_counters_are_conserved,
)


def test_primary_ok_requires_completed_primary_and_admitted_public_source() -> None:
    result = select_web_provider(
        ProviderObservation(
            provider_id="yandex",
            status=WebProviderStatus.COMPLETED,
            source_count=2,
            requested_sources=2,
            completed_sources=2,
            source_urls=("https://docs.python.org/3/", "https://www.python.org/"),
        )
    )
    assert result.decision is WebProviderDecision.PRIMARY_OK
    assert result.selected_provider_id == "yandex"
    assert result.admitted_source_count == 2
    assert result.used_fallback is False


@pytest.mark.parametrize(
    "primary",
    (
        ProviderObservation(provider_id="yandex", status="refused", requested_sources=1, failed_sources=1),
        ProviderObservation(provider_id="yandex", status="unavailable"),
        ProviderObservation(provider_id="yandex", status="empty"),
        ProviderObservation(provider_id="yandex", status="completed"),
        ProviderObservation(
            provider_id="yandex",
            status="completed",
            source_count=1,
            required_filter_refused=True,
        ),
    ),
)
def test_primary_is_not_ok_when_refused_empty_or_filter_refused(primary: ProviderObservation) -> None:
    result = select_web_provider(primary)
    assert result.decision is WebProviderDecision.UNAVAILABLE
    assert result.selected_provider_id is None
    assert result.admitted_source_count == 0


def test_named_fallback_is_used_only_after_primary_refusal() -> None:
    result = select_web_provider(
        ProviderObservation(provider_id="yandex", status="refused", requested_sources=1, failed_sources=1),
        ProviderObservation(provider_id="wikipedia", status="completed", source_count=1),
    )
    assert result.decision is WebProviderDecision.FALLBACK_USED
    assert result.selected_provider_id == "wikipedia"
    assert result.used_fallback is True


@pytest.mark.parametrize(
    "primary",
    (
        ProviderObservation(
            provider_id="yandex",
            status="completed",
            source_count=2,
            requested_sources=3,
            completed_sources=2,
            failed_sources=1,
        ),
        ProviderObservation(
            provider_id="yandex",
            status="timed_out",
            source_count=1,
            requested_sources=2,
            completed_sources=1,
            timed_out_sources=1,
        ),
    ),
)
def test_remaining_admitted_sources_are_degraded_partial(primary: ProviderObservation) -> None:
    result = select_web_provider(primary)
    assert result.decision is WebProviderDecision.DEGRADED_PARTIAL
    assert result.selected_provider_id == "yandex"
    assert result.admitted_source_count > 0


def test_partial_named_fallback_is_honest_degraded_result() -> None:
    result = select_web_provider(
        {"provider": "yandex", "status": "refused", "requested": 1, "failed": 1},
        {
            "provider": "wikipedia",
            "status": "partial",
            "sources": 1,
            "requested": 3,
            "completed": 1,
            "failed": 1,
            "timed_out": 1,
        },
    )
    assert result.decision is WebProviderDecision.DEGRADED_PARTIAL
    assert result.selected_provider_id == "wikipedia"
    assert result.used_fallback is True


def test_no_provider_is_an_unavailable_nonempty_failure_not_empty_success() -> None:
    result = select_web_provider(
        ProviderObservation(provider_id="yandex", status="refused", requested_sources=2, failed_sources=2),
        ProviderObservation(provider_id="wikipedia", status="empty"),
    )
    assert result.decision is WebProviderDecision.UNAVAILABLE
    assert result.selected_provider_id is None
    assert result.source_count == 0


def test_attempt_and_source_bounds_use_shared_contract() -> None:
    observation = ProviderObservation(
        provider_id="yandex",
        status="partial",
        source_count=MAX_RESEARCH_SOURCES,
        direct_source_count=MAX_DIRECT_RESEARCH_SOURCES,
        requested_sources=MAX_RESEARCH_ATTEMPTS,
        completed_sources=MAX_RESEARCH_SOURCES + MAX_DIRECT_RESEARCH_SOURCES,
        failed_sources=MAX_RESEARCH_ATTEMPTS - MAX_RESEARCH_SOURCES - MAX_DIRECT_RESEARCH_SOURCES,
    )
    assert research_attempt_counters_are_conserved(observation.counters_mapping())
    assert observation.admitted_source_count == MAX_RESEARCH_SOURCES + MAX_DIRECT_RESEARCH_SOURCES
    with pytest.raises(WebProviderPolicyError):
        ProviderObservation(provider_id="yandex", status="completed", source_count=MAX_RESEARCH_SOURCES + 1)
    with pytest.raises(WebProviderPolicyError):
        ProviderObservation(
            provider_id="yandex", status="completed", direct_source_count=MAX_DIRECT_RESEARCH_SOURCES + 1
        )
    with pytest.raises(WebProviderPolicyError):
        ProviderObservation(
            provider_id="yandex",
            status="refused",
            requested_sources=MAX_RESEARCH_ATTEMPTS + 1,
            failed_sources=MAX_RESEARCH_ATTEMPTS + 1,
        )


@pytest.mark.parametrize("provider_id", ("unknown", "google", "", "private-provider"))
def test_unknown_provider_ids_fail_closed(provider_id: str) -> None:
    with pytest.raises(WebProviderPolicyError):
        ProviderObservation(provider_id=provider_id, status="unavailable")


def test_unknown_status_and_duplicate_fallback_fail_closed() -> None:
    with pytest.raises(WebProviderPolicyError):
        ProviderObservation(provider_id="yandex", status="maybe")
    with pytest.raises(WebProviderPolicyError):
        select_web_provider(
            ProviderObservation(provider_id="yandex", status="unavailable"),
            ProviderObservation(provider_id="yandex", status="completed", source_count=1),
        )


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1:8080/search",
        "https://localhost/private",
        "https://10.0.0.4/results",
        "https://example.com/results?api_key=secret",
        "https://user:password@example.com/results",
        "https://example.com/token/secret",
        "https://docs.example.test/results",
    ),
)
def test_private_endpoints_and_credential_urls_fail_closed(url: str) -> None:
    with pytest.raises(WebProviderPolicyError):
        validate_public_web_url(url)


def test_public_source_urls_are_accepted_without_network_access() -> None:
    observation = ProviderObservation(
        provider_id="brave-html",
        status="completed",
        source_count=1,
        source_urls=("https://docs.python.org/3/library/urllib.parse.html",),
        endpoint_url="https://search.example.com/api",
    )
    assert observation.source_urls[0].startswith("https://docs.python.org/")
    assert observation.endpoint_url == "https://search.example.com/api"
