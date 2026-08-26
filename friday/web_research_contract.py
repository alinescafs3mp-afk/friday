"""Closed counter contract for target-attested web research reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

MAX_RESEARCH_SOURCES = 8
MAX_DIRECT_RESEARCH_SOURCES = 3
MAX_RESEARCH_SOURCE_ROWS = MAX_RESEARCH_SOURCES + MAX_DIRECT_RESEARCH_SOURCES
MAX_RESEARCH_ATTEMPTS = MAX_RESEARCH_SOURCES * 2
MAX_RESEARCH_SOURCE_TEXT_CHARS = 20_000
MAX_RESEARCH_DECLARED_TEXT_CHARS = 1_000_000_000
MAX_OUTBOUND_WEB_QUERY_CHARS = 200


def normalize_outbound_web_query(value: Any) -> str:
    """Return the exact bounded query string which may cross the web boundary."""

    if not isinstance(value, str):
        return ""
    return value[: MAX_OUTBOUND_WEB_QUERY_CHARS + 2].strip()[:MAX_OUTBOUND_WEB_QUERY_CHARS]


def target_research_report_is_valid(
    report: Any,
    *,
    configured_max_sources: int | None = None,
    allow_source_subset: bool = False,
) -> bool:
    """Validate producer target/counter invariants; legacy reports stay targetless."""

    if not isinstance(report, Mapping):
        return False
    if configured_max_sources is not None and (
        not isinstance(configured_max_sources, int)
        or isinstance(configured_max_sources, bool)
        or not 1 <= configured_max_sources <= MAX_RESEARCH_SOURCES
    ):
        return False
    has_target = "target_sources" in report
    sources = report.get("sources")
    if not isinstance(sources, list):
        return False
    if len(sources) > MAX_RESEARCH_SOURCE_ROWS:
        return False
    counter_names = (
        "requested_sources",
        "completed_sources",
        "failed_sources",
        "timed_out_sources",
    )
    has_complete_counters = all(name in report for name in counter_names)
    if has_target and not has_complete_counters:
        return False
    if not has_complete_counters:
        return True
    values = {key: report.get(key) for key in counter_names}
    limits = {
        "requested_sources": MAX_RESEARCH_ATTEMPTS,
        "completed_sources": MAX_RESEARCH_SOURCE_ROWS,
        "failed_sources": MAX_RESEARCH_ATTEMPTS,
        "timed_out_sources": MAX_RESEARCH_ATTEMPTS,
    }
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > limits[key]
        for key, value in values.items()
    ) or not isinstance(report.get("search_timed_out"), bool):
        return False
    requested = cast(int, values["requested_sources"])
    completed = cast(int, values["completed_sources"])
    failed = cast(int, values["failed_sources"])
    timed_out = cast(int, values["timed_out_sources"])
    if (
        (completed < len(sources) if allow_source_subset else completed != len(sources))
        or bool(sources)
        and requested == 0
        or failed + timed_out > requested
        or requested > completed + failed + timed_out
        or completed + failed + timed_out > requested + MAX_DIRECT_RESEARCH_SOURCES
    ):
        return False
    if not has_target:
        return True
    target_value = report.get("target_sources")
    if (
        not isinstance(target_value, int)
        or isinstance(target_value, bool)
        or not 0 <= target_value <= MAX_RESEARCH_SOURCES
        or configured_max_sources is not None
        and target_value > configured_max_sources
    ):
        return False
    target = target_value
    if target == 0:
        return bool(not sources and requested == completed == failed == timed_out == 0)
    if target > requested:
        return False
    if completed > target + MAX_DIRECT_RESEARCH_SOURCES:
        return False
    if configured_max_sources is None:
        return requested <= 2 * target
    if target < configured_max_sources:
        # Search returned fewer rows than requested, so there is no refill tail.
        return requested == target
    return target <= requested <= 2 * target


__all__ = (
    "MAX_DIRECT_RESEARCH_SOURCES",
    "MAX_OUTBOUND_WEB_QUERY_CHARS",
    "MAX_RESEARCH_ATTEMPTS",
    "MAX_RESEARCH_DECLARED_TEXT_CHARS",
    "MAX_RESEARCH_SOURCE_ROWS",
    "MAX_RESEARCH_SOURCE_TEXT_CHARS",
    "MAX_RESEARCH_SOURCES",
    "normalize_outbound_web_query",
    "target_research_report_is_valid",
)
