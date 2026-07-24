"""Near-duplicate Knowledge Object detection over persisted embeddings.

Entity resolution deduplicates entities; nothing deduplicates knowledge itself,
so a bulk import (bookmarks, mail) inevitably accumulates near-identical
records. This reuses the vectors already indexed for dense recall: pairwise
cosine above a high threshold flags a likely duplicate, stored as a review-gated
``near_duplicate`` conflict — so the existing "keep A / keep B" resolution
(§5) IS the deduplication (the loser becomes ``deprecated``). Nothing is merged
or deleted automatically.

Pure pairwise cosine is O(n²·d); a personal knowledge base is small and this
runs in the background, but the corpus is capped and any truncation is logged
so a large base never silently under-reports.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from jericho.retrieval import unpack_vector

LOGGER = logging.getLogger(__name__)

NEAR_DUPLICATE_TYPE = "near_duplicate"


def _unit(vector: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return None
    return [value / norm for value in vector]


def find_near_duplicate_pairs(
    vectors: list[tuple[str, list[float]]],
    *,
    threshold: float,
    max_pairs: int = 200,
) -> list[tuple[str, str, float]]:
    """Return (id_a, id_b, cosine) pairs at or above ``threshold``, strongest first.

    ``id_a`` < ``id_b`` canonically so a pair is reported once regardless of
    order. Zero and mismatched-length vectors are skipped rather than compared.
    """
    prepared: list[tuple[str, list[float], int]] = []
    for object_id, vector in vectors:
        unit = _unit(vector)
        if unit is not None:
            prepared.append((object_id, unit, len(unit)))

    pairs: list[tuple[str, str, float]] = []
    for i in range(len(prepared)):
        id_i, vec_i, dim_i = prepared[i]
        for j in range(i + 1, len(prepared)):
            id_j, vec_j, dim_j = prepared[j]
            if dim_i != dim_j:
                continue
            score = sum(a * b for a, b in zip(vec_i, vec_j, strict=False))
            if score >= threshold:
                low, high = (id_i, id_j) if id_i < id_j else (id_j, id_i)
                pairs.append((low, high, score))
    pairs.sort(key=lambda item: item[2], reverse=True)
    return pairs[:max_pairs]


def detect_near_duplicates(
    storage: Any,
    settings: Any,
    user_id: str,
    *,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Scan a user's vectors and store review-gated near-duplicate conflicts."""
    model = settings.embeddings_model
    if not model:
        return {"detected": 0, "reason": "embeddings model not configured"}
    max_objects = int(getattr(settings, "dedup_max_objects", 1500))
    rows = storage.get_user_vectors(user_id, model, limit=max_objects)
    truncated = len(rows) >= max_objects
    vectors: list[tuple[str, list[float]]] = [
        (object_id, list(unpack_vector(blob))) for object_id, blob in rows
    ]
    effective_threshold = float(threshold if threshold is not None else settings.dedup_threshold)
    pairs = find_near_duplicate_pairs(vectors, threshold=effective_threshold)

    stored = 0
    for id_a, id_b, score in pairs:
        try:
            storage.store_knowledge_conflict(
                user_id,
                id_a,
                id_b,
                conflict_type=NEAR_DUPLICATE_TYPE,
                confidence=min(1.0, max(0.0, score)),
                evidence={"similarity": round(score, 4), "detector": "embedding_cosine"},
            )
            stored += 1
        except ValueError:
            # A deleted side or a self-pair is skipped, not fatal to the batch.
            continue
    if truncated:
        LOGGER.info(
            "Near-duplicate scan for %s capped at %d objects; older vectors not compared this run",
            user_id,
            max_objects,
        )
    return {
        "detected": stored,
        "candidate_pairs": len(pairs),
        "objects_scanned": len(vectors),
        "truncated": truncated,
    }
