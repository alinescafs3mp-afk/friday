"""Kernel consumption of already-observed public-web source facts.

This module refuses private URLs and invalid provider facts.  It does not
search, fetch, or choose a provider.  Callers keep quota and adapter I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_provider_policy import (
    ProviderObservation,
    WebProviderPolicyError,
    WebProviderStatus,
    select_web_provider,
)
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionState,
    build_web_research_consumption,
)
from friday.web_research_contract import MAX_RESEARCH_SOURCES
from friday.web_surfer import SEARCH_FILTER_ATTESTATION_KEY

_KERNEL_WEB_RESEARCH_CONSUMPTION_ID = "kernel.web_research"
_KERNEL_WEB_RESEARCH_TURN_ID = "kernel.web_research"
_CONSUMPTION_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _kernel_web_research_turn_id() -> str:
    context = current_primary_authenticated_turn_context()
    token = str(getattr(context, "turn_id", "") or "") if context is not None else ""
    if _CONSUMPTION_IDENTITY_RE.fullmatch(token):
        return token
    return _KERNEL_WEB_RESEARCH_TURN_ID


def _web_research_source_urls(report: Mapping[str, Any]) -> tuple[str, ...]:
    raw_sources = report.get("sources")
    if not isinstance(raw_sources, list):
        return ()
    urls: list[str] = []
    for item in raw_sources:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url)
    return tuple(urls)


def _web_search_result_urls(raw_results: object) -> tuple[str, ...]:
    if not isinstance(raw_results, list):
        return ()
    urls: list[str] = []
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url)
    return tuple(urls)


def _web_requested_url_blocked_private(url: str) -> bool:
    """Refuse a requested fetch URL that is not a public web source."""

    if not url.strip():
        return False
    consumption = build_web_research_consumption(
        _KERNEL_WEB_RESEARCH_CONSUMPTION_ID,
        _kernel_web_research_turn_id(),
        WebCurrentnessDecision.SEARCH_REQUIRED,
        None,
        source_urls=(url,),
        topic="",
    )
    return consumption.usability is WebResearchConsumptionState.BLOCKED_PRIVATE


def _web_search_consumption_failure(
    query: str,
    response: Mapping[str, Any],
    *,
    error: str,
) -> dict[str, Any]:
    refusal: dict[str, Any] = {
        "query": query,
        "results": [],
        "outbound_attempted": True,
        "search_failed": True,
        "error": error,
    }
    freshness = response.get("freshness")
    if isinstance(freshness, str) and freshness:
        refusal["freshness"] = freshness
        attestation = response.get(SEARCH_FILTER_ATTESTATION_KEY)
        if isinstance(attestation, Mapping):
            refusal[SEARCH_FILTER_ATTESTATION_KEY] = dict(attestation)
    return refusal


def _web_research_consumption_failure(
    query: str,
    report: Mapping[str, Any],
    *,
    error: str,
) -> dict[str, Any]:
    refusal: dict[str, Any] = {
        "query": query,
        "sources": [],
        "requested_sources": 0,
        "completed_sources": 0,
        "timed_out_sources": 0,
        "failed_sources": 0,
        "search_timed_out": False,
        "outbound_attempted": True,
        "search_failed": True,
        "error": error,
    }
    for field in ("freshness", "source_class", "topic_class"):
        value = report.get(field)
        if isinstance(value, str) and value:
            refusal[field] = value
    return refusal


def _web_research_private_source_refusal(report: Mapping[str, Any], query: str) -> dict[str, Any] | None:
    """Refuse a live research report whose observed source URLs are private."""

    urls = _web_research_source_urls(report)
    if not urls:
        return None
    consumption = build_web_research_consumption(
        _KERNEL_WEB_RESEARCH_CONSUMPTION_ID,
        _kernel_web_research_turn_id(),
        WebCurrentnessDecision.SEARCH_REQUIRED,
        None,
        source_urls=urls,
        topic="",
    )
    if consumption.usability is not WebResearchConsumptionState.BLOCKED_PRIVATE:
        return None
    return _web_research_consumption_failure(query, report, error="source_fact_private")


def _web_research_empty_source_refusal(report: Mapping[str, Any], query: str) -> dict[str, Any] | None:
    """Empty observed sources after outbound are a failed search, not completeness."""

    if _web_research_source_urls(report):
        return None
    if report.get("outbound_attempted") is not True:
        return None
    return _web_research_consumption_failure(query, report, error="no_admitted_sources")


def _web_research_provider_consumption_refusal(
    report: Mapping[str, Any], query: str
) -> dict[str, Any] | None:
    """Consume observed provider facts when the adapter named a selected provider.

    A missing ``selected_provider_id`` is a legacy adapter: do not invent a
    provider and do not refuse public sources.  ``SearchResult.source`` is not
    a provider id.
    """

    selected_id = report.get("selected_provider_id")
    if not isinstance(selected_id, str) or not selected_id.strip():
        return None
    urls = _web_research_source_urls(report)
    try:
        admitted = len(urls)
        search_count = min(admitted, MAX_RESEARCH_SOURCES)
        selected = ProviderObservation(
            provider_id=selected_id,
            status=WebProviderStatus.COMPLETED,
            source_count=search_count,
            direct_source_count=admitted - search_count,
            source_urls=urls,
        )
        primary_id = report.get("provider_primary_id")
        used_fallback = report.get("provider_used_fallback") is True
        if (
            used_fallback
            and isinstance(primary_id, str)
            and primary_id.strip()
            and primary_id.strip().casefold() != selected_id.strip().casefold()
        ):
            primary = ProviderObservation(
                provider_id=primary_id,
                status=WebProviderStatus.REFUSED,
            )
            selection = select_web_provider(primary, selected)
        else:
            selection = select_web_provider(selected)
    except (TypeError, ValueError, WebProviderPolicyError):
        return _web_research_consumption_failure(query, report, error="provider_facts_invalid")
    consumption = build_web_research_consumption(
        _KERNEL_WEB_RESEARCH_CONSUMPTION_ID,
        _kernel_web_research_turn_id(),
        WebCurrentnessDecision.SEARCH_REQUIRED,
        selection,
        source_urls=urls,
        topic="",
    )
    if consumption.usability in {
        WebResearchConsumptionState.CONSUMABLE,
        WebResearchConsumptionState.CONSUMABLE_DEGRADED,
    }:
        return None
    if consumption.usability is WebResearchConsumptionState.BLOCKED_PRIVATE:
        return _web_research_consumption_failure(query, report, error="source_fact_private")
    return _web_research_consumption_failure(query, report, error=consumption.reason.value)
