"""Retrieval quality evaluation — measure whether search actually helps.

The feedback loop nudges ranking, but nothing tells you if retrieval got better
or worse. This runs a stored gold set (query → the Knowledge Objects a good
search must surface) through the real hybrid searcher and reports recall@k,
precision@k and MRR. Each run is stored so the next one can flag a regression —
quality stops being a feeling.

The searcher is deterministic without an LLM (FTS + lexical + graph + optional
dense recall), so this measures the retrieval layer in isolation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

_DEFAULT_K = 10
_LAST_RUN_KEY = "eval:last_run:"


def recall_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    top = set(retrieved[:k])
    return len(top & expected) / len(expected)


def precision_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    if k <= 0 or not retrieved:
        return 0.0
    top = retrieved[:k]
    hits = sum(1 for item in top if item in expected)
    return hits / min(k, len(top))


def reciprocal_rank(retrieved: list[str], expected: set[str]) -> float:
    for position, document_id in enumerate(retrieved, start=1):
        if document_id in expected:
            return 1.0 / position
    return 0.0


async def run_eval(
    storage: Any,
    embeddings: Any,
    settings: Any,
    user_id: str,
    *,
    k: int = _DEFAULT_K,
) -> dict[str, Any]:
    """Run the gold set through the real searcher; return an aggregate report."""
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.retrieval import HybridSearcher

    cases = storage.list_eval_cases(user_id)
    if not cases:
        return {"cases": 0, "recall_at_k": None, "reason": "no gold cases"}

    searcher = HybridSearcher(storage, embeddings, graph_max_depth=settings.graph_max_depth)
    kg = KnowledgeGraph(storage)

    per_case: list[dict[str, Any]] = []
    recall_sum = precision_sum = rr_sum = 0.0
    for case in cases:
        expected = {str(item) for item in case.get("expected_ids", [])}
        if not expected:
            continue
        result = await searcher.search(user_id, str(case["query"]), limit=max(k, 20), kg=kg)
        retrieved = [str(hit.get("id")) for hit in result.get("results", []) if hit.get("id")]
        case_recall = recall_at_k(retrieved, expected, k)
        case_precision = precision_at_k(retrieved, expected, k)
        case_rr = reciprocal_rank(retrieved, expected)
        recall_sum += case_recall
        precision_sum += case_precision
        rr_sum += case_rr
        per_case.append(
            {
                "id": case["id"],
                "query": case["query"],
                "expected": len(expected),
                "found": len(set(retrieved[:k]) & expected),
                "recall_at_k": round(case_recall, 4),
                "reciprocal_rank": round(case_rr, 4),
            }
        )

    scored = len(per_case) or 1
    report = {
        "cases": len(per_case),
        "k": k,
        "recall_at_k": round(recall_sum / scored, 4),
        "precision_at_k": round(precision_sum / scored, 4),
        "mrr": round(rr_sum / scored, 4),
        "per_case": sorted(per_case, key=lambda item: item["recall_at_k"]),
    }
    report["regression"] = _compare_and_store(storage, user_id, report)
    return report


def _compare_and_store(storage: Any, user_id: str, report: dict[str, Any]) -> dict[str, Any]:
    """Compare against the last stored run, then persist this one as the baseline."""
    previous_raw = storage.kv_get(f"{_LAST_RUN_KEY}{user_id}")
    regression: dict[str, Any] = {"previous_recall": None, "delta": None, "regressed": False}
    if previous_raw:
        try:
            previous = json.loads(previous_raw)
            prev_recall = float(previous.get("recall_at_k"))
            delta = round(report["recall_at_k"] - prev_recall, 4)
            regression = {
                "previous_recall": prev_recall,
                "delta": delta,
                # A meaningful drop, not measurement noise.
                "regressed": delta <= -0.05,
            }
            if regression["regressed"]:
                LOGGER.warning(
                    "Retrieval quality regressed for %s: recall@%d %.3f -> %.3f (Δ%.3f)",
                    user_id,
                    report["k"],
                    prev_recall,
                    report["recall_at_k"],
                    delta,
                )
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    storage.kv_set(
        f"{_LAST_RUN_KEY}{user_id}",
        json.dumps(
            {"recall_at_k": report["recall_at_k"], "mrr": report["mrr"], "cases": report["cases"]},
            ensure_ascii=False,
        ),
    )
    return regression
