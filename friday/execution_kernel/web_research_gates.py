"""Observe remaining N2 contracts on an already-admitted research report.

This module does not search or fetch.  Callers keep adapter I/O and quota.
Claims are not invented: research I/O has no answer, so claim-support,
grounding, contradiction, citation, and answer-gate observe empty claims.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from friday.execution_kernel.web_consumption import (
    _KERNEL_WEB_RESEARCH_CONSUMPTION_ID,
    _kernel_web_research_turn_id,
    _web_research_source_urls,
)
from friday.orchestration.web_citation_coverage import build_web_citation_coverage
from friday.orchestration.web_claim_currentness import build_web_claim_currentness
from friday.orchestration.web_claim_support import build_web_claim_support
from friday.orchestration.web_contradiction_coverage import build_web_contradiction_coverage
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_evidence_grounding import build_web_evidence_grounding
from friday.orchestration.web_mission_coverage import (
    WebMissionCoverageState,
    build_web_mission_coverage,
)
from friday.orchestration.web_passage_reference_coverage import build_web_passage_reference_coverage
from friday.orchestration.web_provider_policy import (
    ProviderObservation,
    WebProviderPolicyError,
    WebProviderSelection,
    WebProviderStatus,
    select_web_provider,
    validate_public_web_url,
)
from friday.orchestration.web_research_answer_gate import build_web_research_answer_gate
from friday.orchestration.web_research_consumption import build_web_research_consumption
from friday.orchestration.web_research_mission import (
    WebResearchMissionError,
    WebResearchMissionV1,
    plan_web_research_mission,
)
from friday.orchestration.web_research_readiness import build_web_research_readiness
from friday.orchestration.web_source_date_coverage import build_web_source_date_coverage
from friday.orchestration.web_source_diversity import build_web_source_diversity
from friday.web_research_contract import MAX_RESEARCH_SOURCES

_MISSION_ID = "kernel.web_research.mission"
_DIVERSITY_ID = "kernel.web_research.diversity"
_READINESS_ID = "kernel.web_research.readiness"
_CITATION_ID = "kernel.web_research.citation"
_GATE_ID = "kernel.web_research.answer"
_SUPPORT_ID = "kernel.web_research.support"
_GROUNDING_ID = "kernel.web_research.grounding"
_CONTRADICTION_ID = "kernel.web_research.contradiction"
_DATES_ID = "kernel.web_research.dates"
_PASSAGES_ID = "kernel.web_research.passages"
_CURRENTNESS_ID = "kernel.web_research.claims.currentness"
_COVERAGE_ID = "kernel.web_research.mission.coverage"


def _closed(value: object) -> str:
    enum = getattr(value, "value", value)
    return str(enum)


def _public_urls(urls: Sequence[str]) -> tuple[str, ...]:
    admitted: list[str] = []
    seen: set[str] = set()
    for url in urls:
        try:
            canonical = validate_public_web_url(url, field="source_url")
        except (TypeError, ValueError, WebProviderPolicyError):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        admitted.append(canonical)
    return tuple(admitted)


def _source_facts(report: Mapping[str, Any]) -> list[dict[str, object]]:
    raw_sources = report.get("sources")
    if not isinstance(raw_sources, list):
        return []
    facts: list[dict[str, object]] = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        try:
            canonical = validate_public_web_url(url, field="source_url")
        except (TypeError, ValueError, WebProviderPolicyError):
            continue
        fact: dict[str, object] = {"source_id": f"s{index}", "canonical_url": canonical}
        for key in ("publication_or_update_date", "publication_date", "updated_at"):
            raw_date = item.get(key)
            if isinstance(raw_date, str) and raw_date.strip():
                fact[key] = raw_date
                break
        for key in ("relevant_passage_references", "passage_references", "passages"):
            raw_refs = item.get(key)
            if raw_refs:
                fact[key] = raw_refs
                break
        facts.append(fact)
        if len(facts) >= MAX_RESEARCH_SOURCES:
            break
    return facts


def _provider_selection(report: Mapping[str, Any], urls: tuple[str, ...]) -> WebProviderSelection | None:
    selected_id = report.get("selected_provider_id")
    if not isinstance(selected_id, str) or not selected_id.strip():
        return None
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
            return select_web_provider(primary, selected)
        return select_web_provider(selected)
    except (TypeError, ValueError, WebProviderPolicyError):
        return None


def _fact_bearing_source_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_sources = report.get("sources")
    if not isinstance(raw_sources, list):
        return []
    return [
        item
        for item in raw_sources
        if isinstance(item, Mapping)
        and not str(item.get("error") or "").strip()
        and str(item.get("text") or "").strip()
    ]


def report_has_admitted_fact_sources(report: Mapping[str, Any]) -> bool:
    """Return whether the report already carries readable public source bodies."""

    return bool(_fact_bearing_source_rows(report))


def plan_kernel_web_research_mission(
    query: str,
    *,
    freshness: str = "",
    turn_id: str | None = None,
) -> WebResearchMissionV1 | None:
    """Plan complementary public queries.  Private topics yield no plan."""

    token = turn_id or _kernel_web_research_turn_id()
    try:
        return plan_web_research_mission(
            mission_id=_MISSION_ID,
            authenticated_turn_id=token,
            public_topic=query,
            freshness_requirement=freshness or "unspecified",
        )
    except (TypeError, ValueError, WebResearchMissionError):
        return None


def complementary_mission_queries(
    query: str,
    mission: WebResearchMissionV1 | None,
) -> tuple[str, ...]:
    """Return planned queries that are not the original outbound query."""

    if mission is None:
        return ()
    original = " ".join(query.casefold().split())
    return tuple(item for item in mission.query_plan if " ".join(item.casefold().split()) != original)


def merge_unique_research_sources(
    report: dict[str, Any],
    extra_sources: Sequence[object],
    *,
    bound: int,
) -> None:
    """Append unique public source rows without exceeding the caller bound."""

    raw = report.get("sources")
    if not isinstance(raw, list):
        return
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            host = (urlsplit(url).hostname or "").rstrip(".").casefold()
            seen.add(f"{host}|{urlsplit(url).path}")
    for item in extra_sources:
        if len(raw) >= bound:
            break
        if not isinstance(item, Mapping):
            continue
        if str(item.get("error") or "").strip() or not str(item.get("text") or "").strip():
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        try:
            validate_public_web_url(url, field="source_url")
        except (TypeError, ValueError, WebProviderPolicyError):
            continue
        identity = f"{(urlsplit(url).hostname or '').rstrip('.').casefold()}|{urlsplit(url).path}"
        if identity in seen:
            continue
        seen.add(identity)
        raw.append(dict(item))
    report["sources"] = raw
    if "completed_sources" in report and isinstance(report.get("completed_sources"), int):
        report["completed_sources"] = len(raw)
        failed = report.get("failed_sources")
        timed_out = report.get("timed_out_sources")
        if (
            isinstance(failed, int)
            and not isinstance(failed, bool)
            and isinstance(timed_out, int)
            and not isinstance(timed_out, bool)
        ):
            report["requested_sources"] = len(raw) + failed + timed_out


def observe_web_research_gates(
    query: str,
    report: Mapping[str, Any],
    *,
    executed_queries: Sequence[str],
    mission: WebResearchMissionV1 | None,
    freshness: str = "",
) -> dict[str, Any]:
    """Attach compact, honest N2 observations.  Do not invent claims or dates."""

    try:
        return _observe_web_research_gates(
            query,
            report,
            executed_queries=executed_queries,
            mission=mission,
            freshness=freshness,
        )
    except (TypeError, ValueError):
        return {
            "research_mission_executed": False,
            "research_mission_query_count": 0,
            "research_executed_query_count": len(tuple(executed_queries)),
            "research_mission_coverage": "blocked",
            "research_diversity": "empty",
            "research_readiness": "not_ready",
            "research_consumption": "unavailable",
            "research_answer_admission": "hold",
            "research_source_dates": "blocked",
            "research_passages": "blocked",
            "research_claim_support": "blocked",
            "research_grounding": "blocked",
            "research_contradiction": "blocked",
            "research_claim_currentness": "blocked",
        }


def _observe_web_research_gates(
    query: str,
    report: Mapping[str, Any],
    *,
    executed_queries: Sequence[str],
    mission: WebResearchMissionV1 | None,
    freshness: str = "",
) -> dict[str, Any]:
    del query
    turn_id = _kernel_web_research_turn_id()
    if mission is not None:
        turn_id = mission.authenticated_turn_id
    urls = _public_urls(_web_research_source_urls(report))
    facts = _source_facts(report)
    consumption = build_web_research_consumption(
        _KERNEL_WEB_RESEARCH_CONSUMPTION_ID,
        turn_id,
        WebCurrentnessDecision.SEARCH_REQUIRED,
        _provider_selection(report, urls),
        source_urls=urls,
        topic="",
    )
    try:
        diversity = build_web_source_diversity(
            diversity_id=_DIVERSITY_ID,
            authenticated_turn_id=turn_id,
            source_urls=urls,
        )
    except (TypeError, ValueError):
        diversity = None
    readiness = build_web_research_readiness(
        _READINESS_ID,
        mission,
        diversity,
        consumption,
        authenticated_turn_id=turn_id,
    )
    planned = mission.query_plan if mission is not None else ()
    coverage = build_web_mission_coverage(
        _COVERAGE_ID,
        mission,
        tuple(item for item in executed_queries if item in planned),
        authenticated_turn_id=turn_id,
    )
    citation = build_web_citation_coverage(
        _CITATION_ID,
        turn_id,
        urls,
        (),
    )
    answer_gate = build_web_research_answer_gate(
        _GATE_ID,
        readiness,
        citation,
        authenticated_turn_id=turn_id,
    )
    dates = build_web_source_date_coverage(_DATES_ID, turn_id, facts)
    passages = build_web_passage_reference_coverage(_PASSAGES_ID, turn_id, facts)
    support = build_web_claim_support(
        _SUPPORT_ID,
        turn_id,
        claims=(),
        admitted_source_urls=urls,
    )
    grounding = build_web_evidence_grounding(
        _GROUNDING_ID,
        turn_id,
        claims=(),
        admitted_source_urls=urls,
    )
    contradiction = build_web_contradiction_coverage(
        _CONTRADICTION_ID,
        turn_id,
        claims=(),
        admitted_source_urls=urls,
    )
    claim_currentness = build_web_claim_currentness(
        _CURRENTNESS_ID,
        turn_id,
        WebCurrentnessDecision.SEARCH_REQUIRED if freshness else WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
        (),
    )
    executed = coverage.coverage is WebMissionCoverageState.COMPLETE
    compact = {
        "research_mission_executed": executed,
        "research_mission_query_count": coverage.planned_query_count,
        "research_executed_query_count": len(tuple(executed_queries)),
        "research_mission_coverage": _closed(coverage.coverage),
        "research_diversity": _closed(diversity.diversity_note) if diversity is not None else "empty",
        "research_readiness": _closed(readiness.readiness),
        "research_consumption": _closed(consumption.usability),
        "research_answer_admission": _closed(answer_gate.admission),
        "research_source_dates": _closed(dates.coverage),
        "research_passages": _closed(passages.coverage),
        "research_claim_support": _closed(support.support),
        "research_grounding": _closed(grounding.grounding),
        "research_contradiction": _closed(contradiction.coverage),
        "research_claim_currentness": _closed(claim_currentness.admission),
    }
    return compact
