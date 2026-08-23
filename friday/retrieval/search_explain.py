"""Privacy-safe projection for the read-only retrieval explain endpoint.

The ranker trace contains object ids and titles and is therefore an authorised
admin payload, not something that may be copied into telemetry.  This module
builds the small, content-free part operators can retain: scope, channel and cap
state, index coverage, and closed exclusion/completeness reasons.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

SEARCH_EXPLAIN_CORPORA = ("knowledge", "documents", "messages")
SEARCH_EXPLAIN_DATE_ROLES = ("none", "document_or_mentioned_date")
_EXECUTABLE_CORPORA = frozenset({"knowledge"})
_RECALL_CHANNELS = frozenset({"fts", "recent_pool", "dense", "graph"})
_EMBEDDING_FRESHNESS = {
    "disabled": "not_applicable",
    "not_inspected": "not_inspected",
    "complete": "measured_from_source_version_and_chunk_scheme",
    "incomplete": "measured_from_source_version_and_chunk_scheme",
}
_EXCLUSION_REASONS = frozenset(
    {
        "deleted",
        "deprecated_weak",
        "identifier_mismatch",
        "insufficient_evidence",
        "rerank_below_threshold",
    }
)


def parse_search_explain_corpora(raw: str) -> tuple[str, ...]:
    """Parse a bounded, closed corpus selector without silently widening scope."""

    selected = tuple(dict.fromkeys(part.strip().casefold() for part in raw.split(",") if part.strip()))
    if not selected:
        raise ValueError("corpora_empty")
    unknown = set(selected) - set(SEARCH_EXPLAIN_CORPORA)
    if unknown:
        raise ValueError("corpus_unknown")
    return selected


def validate_search_explain_date_range(
    date_role: str,
    since: str | None,
    until: str | None,
) -> tuple[str, str | None, str | None]:
    """Validate the one date role the current Knowledge contour can honour."""

    if not isinstance(date_role, str) or date_role not in SEARCH_EXPLAIN_DATE_ROLES:
        raise ValueError("date_role_unknown")
    normalized: list[str | None] = []
    for value in (since, until):
        if value is None:
            normalized.append(None)
            continue
        if not isinstance(value, str) or value != value.strip():
            raise ValueError("date_range_invalid")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date_range_invalid") from exc
        # ``fromisoformat`` accepts compact/basic forms; the storage predicate is
        # lexical, so one canonical extended representation is part of the contract.
        if parsed.isoformat() != value:
            raise ValueError("date_range_invalid")
        normalized.append(value)
    normalized_since, normalized_until = normalized
    if (normalized_since or normalized_until) and date_role == "none":
        raise ValueError("date_role_required_for_range")
    if normalized_since and normalized_until and normalized_since > normalized_until:
        raise ValueError("date_range_reversed")
    return date_role, normalized_since, normalized_until


def build_search_explain_projection(
    result: Mapping[str, Any],
    *,
    selected_corpora: Sequence[str],
    authorized_knowledge_objects: int | None,
    date_role: str,
    since: str | None,
    until: str | None,
    graph_selected: bool,
    fts_available: bool,
    embedding_index: Mapping[str, Any],
    result_limit: int,
    dense_object_cap: int,
) -> dict[str, Any]:
    """Return a content/identifier-free operational explanation of one search."""

    if not isinstance(result, Mapping):
        raise ValueError("result_invalid")
    if not isinstance(selected_corpora, Sequence) or isinstance(selected_corpora, (str, bytes)):
        raise ValueError("corpora_invalid")
    if any(not isinstance(name, str) for name in selected_corpora):
        raise ValueError("corpora_invalid")
    validated_corpora = parse_search_explain_corpora(",".join(selected_corpora))
    date_role, since, until = validate_search_explain_date_range(date_role, since, until)
    if not isinstance(result_limit, int) or isinstance(result_limit, bool) or not 1 <= result_limit <= 50:
        raise ValueError("result_limit_invalid")
    if not isinstance(dense_object_cap, int) or isinstance(dense_object_cap, bool) or dense_object_cap < 0:
        raise ValueError("dense_object_cap_invalid")
    if not isinstance(graph_selected, bool) or not isinstance(fts_available, bool):
        raise ValueError("channel_state_invalid")
    if (
        not isinstance(embedding_index, Mapping)
        or any(not isinstance(key, str) for key in embedding_index)
        or set(embedding_index) - {"status", "missing_objects", "stale_objects", "freshness"}
    ):
        raise ValueError("embedding_index_invalid")
    embedding_status = embedding_index.get("status")
    if not isinstance(embedding_status, str):
        raise ValueError("embedding_index_invalid")
    expected_freshness = _EMBEDDING_FRESHNESS.get(embedding_status)
    if (
        expected_freshness is None
        or embedding_index.get("freshness", expected_freshness) != expected_freshness
    ):
        raise ValueError("embedding_index_invalid")
    for field in ("missing_objects", "stale_objects"):
        value = embedding_index.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError("embedding_index_invalid")
        if embedding_status in {"complete", "incomplete"} and value is None:
            raise ValueError("embedding_index_invalid")
        if embedding_status in {"disabled", "not_inspected"} and value is not None:
            raise ValueError("embedding_index_invalid")
    if authorized_knowledge_objects is not None and (
        not isinstance(authorized_knowledge_objects, int)
        or isinstance(authorized_knowledge_objects, bool)
        or authorized_knowledge_objects < 0
    ):
        raise ValueError("authorized_object_count_invalid")

    selected = frozenset(validated_corpora)
    trace = result.get("trace")
    if not isinstance(trace, list) or any(not isinstance(row, Mapping) for row in trace):
        raise ValueError("trace_invalid")
    rows = trace
    strategy_value = result.get("strategy")
    if not isinstance(strategy_value, Mapping):
        raise ValueError("strategy_invalid")
    strategy = strategy_value

    corpus_rows: list[dict[str, Any]] = []
    incomplete: set[str] = set()
    for name in SEARCH_EXPLAIN_CORPORA:
        is_selected = name in selected
        row: dict[str, Any] = {"name": name, "selected": is_selected}
        if not is_selected:
            row["status"] = "not_selected"
        elif name in _EXECUTABLE_CORPORA:
            row.update(
                status="searched",
                authorized_objects=max(0, int(authorized_knowledge_objects or 0)),
                authorization_basis="tenant_live_non_private",
            )
        else:
            reason = f"{name}_not_available_in_current_contour"
            row.update(status="unavailable", authorized_objects=None, reason=reason)
            incomplete.add(reason)
        corpus_rows.append(row)

    source_counts: Counter[str] = Counter()
    exclusion_counts: Counter[str] = Counter()
    for raw_row in rows:
        recalled_by = raw_row.get("recalled_by")
        if isinstance(recalled_by, list):
            if any(not isinstance(name, str) or name not in _RECALL_CHANNELS for name in recalled_by):
                raise ValueError("recall_channel_invalid")
            source_counts.update(recalled_by)
        elif recalled_by is not None:
            raise ValueError("recall_channel_invalid")
        if raw_row.get("status") == "discarded":
            reason = str(raw_row.get("reason") or "other")
            exclusion_counts[reason if reason in _EXCLUSION_REASONS else "other"] += 1

    knowledge_selected = "knowledge" in selected
    dense_configured = str(embedding_index.get("status") or "disabled") in {
        "complete",
        "incomplete",
    }
    channel_rows = [
        {
            "name": "fts",
            "status": (
                "not_selected"
                if not knowledge_selected
                else "unavailable"
                if not fts_available
                else ("used" if source_counts["fts"] else "searched_no_hit")
            ),
            "recalled_candidates": source_counts["fts"],
        },
        {
            "name": "recent_pool",
            "status": (
                "not_selected"
                if not knowledge_selected
                else ("used" if source_counts["recent_pool"] else "searched_no_hit")
            ),
            "recalled_candidates": source_counts["recent_pool"],
        },
        {
            "name": "dense",
            "status": (
                "not_selected"
                if not knowledge_selected
                else "disabled"
                if not dense_configured
                else ("used" if source_counts["dense"] else "searched_no_hit")
            ),
            "recalled_candidates": source_counts["dense"],
        },
        {
            "name": "graph",
            "status": (
                "not_selected"
                if not graph_selected
                else ("used" if source_counts["graph"] else "searched_no_hit")
            ),
            "recalled_candidates": source_counts["graph"],
        },
    ]

    raw_returned = result.get("count", 0)
    raw_matched = result.get("matched_at_least", raw_returned)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (raw_returned, raw_matched)
    ):
        raise ValueError("result_count_invalid")
    returned = raw_returned
    matched = raw_matched
    lexical_pool_limit = strategy.get("lexical_pool_scanned")
    if lexical_pool_limit is not None and (
        not isinstance(lexical_pool_limit, int)
        or isinstance(lexical_pool_limit, bool)
        or lexical_pool_limit < 0
    ):
        raise ValueError("strategy_invalid")
    trace_cap = max(result_limit * 3, 30)
    caps: dict[str, dict[str, Any]] = {
        "result_page": {
            "limit": result_limit,
            "applied": matched > returned,
            "matched_before_page": matched,
        },
        "trace": {
            "limit": trace_cap,
            # The ranker exposes no total candidate count. Equality is therefore
            # explicitly a possible truncation, never asserted as an exact one.
            "possibly_applied": len(rows) >= trace_cap,
            "observed": len(rows),
        },
        "lexical_pool": {
            "limit": lexical_pool_limit,
            "applied": strategy.get("lexical_pool_capped") is True,
        },
        "dense_objects": {
            "limit": dense_object_cap or None,
            "applied": strategy.get("embeddings_capped") is True,
        },
        "dense_chunks": {
            "limit": None,
            "applied": strategy.get("embeddings_chunks_capped") is True,
        },
    }
    if caps["trace"]["possibly_applied"]:
        incomplete.add("trace_cap_reached")
    if caps["lexical_pool"]["applied"]:
        incomplete.add("lexical_pool_capped")
    if caps["dense_objects"]["applied"]:
        incomplete.add("dense_scan_capped")
    if caps["dense_chunks"]["applied"]:
        incomplete.add("dense_chunk_scan_capped")
    if strategy.get("date_window") is True and strategy.get("date_window_applied") is False:
        incomplete.add("date_window_cap_exceeded")
    if knowledge_selected and embedding_index.get("status") == "incomplete":
        incomplete.add("embedding_index_incomplete")
    if knowledge_selected and not fts_available:
        incomplete.add("fts_index_unavailable")

    active_date = bool(since or until)
    return {
        "$schema": "friday.search_explain.v1",
        "privacy": {
            "contains_query": False,
            "contains_object_ids": False,
            "contains_titles_or_passages": False,
        },
        "corpora": corpus_rows,
        "date": {
            "active": active_date,
            "role": date_role if active_date else "none",
            "range": {"since": since, "until": until},
            "hard_prefilter": active_date,
        },
        "authorization": {
            "status": "authorized",
            "scope": "tenant",
            "candidate_reauthorization": True,
            "counts_are_scope_filtered": True,
        },
        "channels": channel_rows,
        "caps": caps,
        "exclusions": {
            "total": sum(exclusion_counts.values()),
            "by_reason": dict(sorted(exclusion_counts.items())),
        },
        "indexes": {
            "knowledge_fts": {
                "status": (
                    "not_inspected"
                    if not knowledge_selected
                    else ("available" if fts_available else "unavailable")
                )
            },
            "knowledge_embeddings": {
                "status": embedding_status,
                "missing_objects": embedding_index.get("missing_objects"),
                "stale_objects": embedding_index.get("stale_objects"),
                "freshness": expected_freshness,
            },
        },
        "completeness": {
            "status": "complete" if not incomplete else "incomplete",
            "reasons": sorted(incomplete),
        },
    }
