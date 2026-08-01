"""Deterministic check that a cited Knowledge Object actually supports the claim.

The answer verifier is an LLM judge: it reads well but it is non-deterministic and
costs a call. This is the cheap, repeatable complement — pure lexical overlap between
the sentence carrying a ``[K#]`` marker and the object that marker points at, using
the same tokenizer retrieval ranks with. It cannot understand paraphrase, so it is
reported as a rate and never as a gate: a low score means "look at this", not "wrong".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from friday.retrieval import lexical_vector, sparse_cosine

CITATION_MARKER_RE = re.compile(r"\[(K\d{1,2})\]", re.IGNORECASE)
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
# Below this the claim and the object share almost no vocabulary. Calibrated against
# the measured gap between a supported claim (~0.25-0.51) and an unrelated one
# (~0.006-0.06); a module constant, and the raw overlaps are reported so it can be
# re-calibrated without changing the metric.
CITATION_OVERLAP_MIN = 0.12
# A fragment this short carries too little vocabulary for overlap to mean anything.
_MIN_WORDS = 3
# Every sentence of the object is scored, and the best one wins. Two cheaper-looking
# variants were tried and both made the verdict depend on WHERE the support sits
# rather than on whether it exists: keeping only the first N sentences hid support
# deeper in the document, and scoring a single query-aware window let an earlier
# decoy region (one that merely contains the claim's token substrings) capture the
# window so the genuinely supporting sentence was never scored at all. Scanning every
# sentence also measured FASTER than the window search on a large object.
_UNIT_CHARS = 400
_MAX_WEAK = 3


def _units(text: str) -> list[str]:
    """Sentence-ish units. An over-long one is SLID over, never truncated: cutting it
    would drop exactly the words that support the claim."""
    units: list[str] = []
    for raw in _CLAIM_SPLIT_RE.split(text or ""):
        part = raw.strip()
        if not part:
            continue
        if len(part) <= _UNIT_CHARS:
            units.append(part)
            continue
        step = max(1, _UNIT_CHARS // 2)
        for start in range(0, len(part), step):
            window = part[start : start + _UNIT_CHARS].strip()
            if window:
                units.append(window)
            if start + _UNIT_CHARS >= len(part):
                break
    return units


def _word_keys(vector: Mapping[str, float]) -> int:
    return sum(1 for key in vector if key.startswith("w:"))


def citation_overlap(
    answer: str,
    citations: Mapping[str, str],
    texts: Mapping[str, str],
    *,
    threshold: float = CITATION_OVERLAP_MIN,
) -> dict[str, Any]:
    """Score every ``[K#]``-carrying sentence against the object it cites.

    ``citations`` maps a marker label to a Knowledge Object id; ``texts`` maps that id
    to the object's text. Markers are read from the ANSWER, not from the legend, so a
    label the model never actually used is not counted as checked.
    """
    checked = supported = unresolved = skipped_short = 0
    overlaps: list[float] = []
    weakest: list[dict[str, Any]] = []

    for unit in _units(answer):
        labels = {match.group(1).upper() for match in CITATION_MARKER_RE.finditer(unit)}
        if not labels:
            continue
        # The marker itself is not evidence: strip it before vectorising, or an object
        # that happens to contain the literal "K1" would look like support.
        claim = CITATION_MARKER_RE.sub(" ", unit)
        claim_vector = lexical_vector(claim)
        if _word_keys(claim_vector) < _MIN_WORDS:
            skipped_short += len(labels)
            continue
        for label in sorted(labels):
            knowledge_id = citations.get(label)
            body = texts.get(knowledge_id or "", "")
            if not knowledge_id or not body:
                unresolved += 1
                continue
            score = max(
                (sparse_cosine(claim_vector, lexical_vector(part)) for part in _units(body)),
                default=0.0,
            )
            checked += 1
            overlaps.append(score)
            if score >= threshold:
                supported += 1
            else:
                weakest.append(
                    {
                        "label": label,
                        "knowledge_object_id": knowledge_id,
                        "overlap": round(score, 4),
                        "claim": claim.strip()[:200],
                    }
                )

    weakest.sort(key=lambda row: row["overlap"])
    weak = len(weakest)
    return {
        "status": "skipped" if checked == 0 else ("weak" if weak else "ok"),
        "threshold": threshold,
        "checked": checked,
        "supported": supported,
        "weak": weak,
        "unresolved": unresolved,
        "skipped_short": skipped_short,
        "min_overlap": round(min(overlaps), 4) if overlaps else None,
        "mean_overlap": round(sum(overlaps) / len(overlaps), 4) if overlaps else None,
        "weakest": weakest[:_MAX_WEAK],
    }
