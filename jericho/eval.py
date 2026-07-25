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
_CHUNK_AB_KEY = "eval:chunk_ab:"


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
    report = await _score_cases(searcher, KnowledgeGraph(storage), user_id, cases, k)
    report["regression"] = _compare_and_store(storage, user_id, report)
    return report


async def _score_cases(
    searcher: Any, kg: Any, user_id: str, cases: list[dict[str, Any]], k: int
) -> dict[str, Any]:
    """Score a gold set with one searcher. Extracted so an A/B measures both arms
    with byte-identical code — a difference in the numbers can only come from the
    searcher, never from the harness."""
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
                "precision_at_k": round(case_precision, 4),
                "reciprocal_rank": round(case_rr, 4),
            }
        )

    scored = len(per_case) or 1
    return {
        "cases": len(per_case),
        "k": k,
        "recall_at_k": round(recall_sum / scored, 4),
        "precision_at_k": round(precision_sum / scored, 4),
        "mrr": round(rr_sum / scored, 4),
        "per_case": sorted(per_case, key=lambda item: item["recall_at_k"]),
    }


async def compare_chunk_recall(
    storage: Any,
    embeddings: Any,
    settings: Any,
    user_id: str,
    *,
    k: int = _DEFAULT_K,
) -> dict[str, Any]:
    """Run the gold set twice — without and with passage-level recall — and report the
    difference.

    Passage-level recall is a ranking change, and a ranking change is only an
    improvement if it is measured on the operator's OWN corpus. Ship it when
    ``delta.recall_at_k >= +0.05`` and ``delta.precision_at_k >= -0.02``; otherwise set
    ``JERICHO_EMBEDDINGS_CHUNK_CHARS=0``. Advisory only: nothing here feeds ranking.
    """
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.retrieval import HybridSearcher

    cases = storage.list_eval_cases(user_id)
    if not cases:
        return {"cases": 0, "reason": "no gold cases"}
    if settings.embeddings_chunk_chars <= 0:
        return {"cases": len(cases), "reason": "chunking disabled"}

    kg = KnowledgeGraph(storage)
    arms: dict[str, dict[str, Any]] = {}
    for name, chunk_recall in (("baseline", False), ("chunked", True)):
        searcher = HybridSearcher(
            storage,
            embeddings,
            graph_max_depth=settings.graph_max_depth,
            chunk_recall=chunk_recall,
        )
        arms[name] = await _score_cases(searcher, kg, user_id, cases, k)

    baseline_cases = {row["id"]: row for row in arms["baseline"]["per_case"]}
    per_case = [
        {
            "id": row["id"],
            "query": row["query"],
            "baseline_recall": baseline_cases.get(row["id"], {}).get("recall_at_k", 0.0),
            "chunked_recall": row["recall_at_k"],
            "delta": round(row["recall_at_k"] - baseline_cases.get(row["id"], {}).get("recall_at_k", 0.0), 4),
        }
        for row in arms["chunked"]["per_case"]
    ]
    report = {
        "k": k,
        "cases": arms["chunked"]["cases"],
        "baseline": arms["baseline"],
        "chunked": arms["chunked"],
        "delta": {
            metric: round(arms["chunked"][metric] - arms["baseline"][metric], 4)
            for metric in ("recall_at_k", "precision_at_k", "mrr")
        },
        # Regressions first: the point of the report is to find what got worse.
        "per_case": sorted(per_case, key=lambda item: item["delta"]),
    }
    storage.kv_set(f"{_CHUNK_AB_KEY}{user_id}", json.dumps(report["delta"], ensure_ascii=False))
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
