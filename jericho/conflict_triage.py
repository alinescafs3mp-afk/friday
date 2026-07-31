"""Hints for the near-duplicate review queue — label only, never auto-decide.

The owner's queue is ~200 cosine near-duplicates at threshold 0.95. A probe on
that queue (criteria declared before the run) split it as:

* 68.0% true duplicates — Jaccard ≥0.95 on content stems, lengths within ±5%
* 15.5% form blanks — same template, different people/numbers in the fields
* 14.0% false positives — not actually similar
* 2.5% similar with formatting-only differences

Mass-confirming the queue would deprecate real distinct records on the blank
pairs. The labels below are a review aid so the owner sees where a decision is
cheap and where it needs a careful read. ``conflict_decide`` stays human-only;
the cosine threshold is not touched here.
"""

from __future__ import annotations

from typing import Any

from jericho.eval_bootstrap import content_tokens
from jericho.morphology import stem
from jericho.retrieval import _STOPWORDS, knowledge_search_text, tokens_of

HINT_LIKELY_DUPLICATE = "likely_duplicate"
HINT_LIKELY_DIFFERENT = "likely_different_records"
HINT_UNCERTAIN = "uncertain"

# Measured cut for "true duplicate" on the live queue (see module docstring).
_JACCARD_DUPLICATE = 0.95
_LENGTH_RATIO_DUPLICATE = 0.95  # min/max ≥ 0.95 ↔ lengths within ±5%
# Differing stems whose surface form is a proper name or contains a digit are
# "data fields". When they dominate the diff and the pair still shares a
# template, the pair is a form blank, not a re-save.
_DATA_DIFF_SHARE = 0.5
# Below this Jaccard the pair does not share enough of a template to call a
# blank — random pairs can share a capitalised word by chance.
_MIN_TEMPLATE_JACCARD = 0.5

_HINT_LABELS_RU = {
    HINT_LIKELY_DUPLICATE: "быстро: похоже на дубликат",
    HINT_LIKELY_DIFFERENT: "внимание: разные записи?",
    HINT_UNCERTAIN: "неясно — вчитаться",
}


def hint_label_ru(hint: str) -> str:
    """Short Russian badge for Telegram / chat."""
    return _HINT_LABELS_RU.get(str(hint or ""), _HINT_LABELS_RU[HINT_UNCERTAIN])


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def _length_ratio(left: str, right: str) -> float:
    la, lb = len(left or ""), len(right or "")
    if la == 0 and lb == 0:
        return 1.0
    if la == 0 or lb == 0:
        return 0.0
    return min(la, lb) / max(la, lb)


def _is_data_word(token: str) -> bool:
    """Field-like value: starts with a capital letter, or carries a digit."""
    if not token:
        return False
    if any(ch.isdigit() for ch in token):
        return True
    return token[0].isupper()


def _surface_by_stem(text: str) -> dict[str, str]:
    """Map content stem → one surface form (prefer capitalised for data check)."""
    mapping: dict[str, str] = {}
    for token in tokens_of(text or ""):
        if len(token) <= 2:
            continue
        folded = token.casefold()
        if folded in _STOPWORDS:
            continue
        key = stem(folded)
        if key in _STOPWORDS:
            continue
        prev = mapping.get(key)
        if prev is None or (_is_data_word(token) and not _is_data_word(prev)):
            mapping[key] = token
    return mapping


def classify_near_duplicate_pair(text_a: str, text_b: str) -> dict[str, Any]:
    """Label one near-duplicate candidate. Pure function — no I/O, no side effects.

    Returns ``hint`` plus the three measured features so a reviewer (or test)
    can see why the label was chosen. Does not write, deprecate, or decide.
    """
    left = text_a or ""
    right = text_b or ""
    stems_a = content_tokens(left)
    stems_b = content_tokens(right)
    jaccard = _jaccard(stems_a, stems_b)
    length_ratio = _length_ratio(left, right)

    surf_a = _surface_by_stem(left)
    surf_b = _surface_by_stem(right)
    only_a = set(surf_a) - set(surf_b)
    only_b = set(surf_b) - set(surf_a)
    surfaces = [surf_a[s] for s in only_a] + [surf_b[s] for s in only_b]
    data_count = sum(1 for word in surfaces if _is_data_word(word))
    data_diff_share = (data_count / len(surfaces)) if surfaces else 0.0

    # Data-field diffs first: a form blank can still clear the Jaccard cut when
    # only a short name changes, and that is exactly the dangerous case for
    # keep_a/keep_b (it would deprecate a real distinct record).
    if surfaces and data_diff_share >= _DATA_DIFF_SHARE and jaccard >= _MIN_TEMPLATE_JACCARD:
        hint = HINT_LIKELY_DIFFERENT
    elif jaccard >= _JACCARD_DUPLICATE and length_ratio >= _LENGTH_RATIO_DUPLICATE:
        hint = HINT_LIKELY_DUPLICATE
    else:
        hint = HINT_UNCERTAIN

    return {
        "hint": hint,
        "jaccard": round(jaccard, 4),
        "length_ratio": round(length_ratio, 4),
        "data_diff_share": round(data_diff_share, 4),
        "label_ru": hint_label_ru(hint),
    }


def texts_for_conflict_pair(
    storage: Any,
    user_id: str,
    item: dict[str, Any],
) -> tuple[str, str]:
    """Full indexed text of both sides; fall back to list projection fields."""
    a_id = str(item.get("knowledge_a_id") or item.get("a", {}).get("id") or "")
    b_id = str(item.get("knowledge_b_id") or item.get("b", {}).get("id") or "")
    a_row = storage.get_knowledge_object(a_id, user_id) if a_id else None
    b_row = storage.get_knowledge_object(b_id, user_id) if b_id else None
    if a_row:
        text_a = knowledge_search_text(a_row)
    else:
        text_a = " ".join(str(item.get(k) or "") for k in ("knowledge_a_title", "knowledge_a_summary"))
    if b_row:
        text_b = knowledge_search_text(b_row)
    else:
        text_b = " ".join(str(item.get(k) or "") for k in ("knowledge_b_title", "knowledge_b_summary"))
    return text_a, text_b


def attach_conflict_hint(
    storage: Any,
    user_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Copy of ``item`` with triage features under ``triage``."""
    text_a, text_b = texts_for_conflict_pair(storage, user_id, item)
    triage = classify_near_duplicate_pair(text_a, text_b)
    enriched = dict(item)
    enriched["triage"] = triage
    return enriched
