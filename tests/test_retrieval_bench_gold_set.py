"""The bench's gold set must not quietly become easy.

A retrieval benchmark is only worth its labels. Four cases in this gold set were
labelled ``cross-script`` — the category that exists to show what dense retrieval buys
— while sharing up to three content words with their target document. They scored
1.00, which said nothing except that lexical search can match repeated words.

That is not a mistake anyone notices by reading: the queries look plausibly hard. So
the gold set audits itself, and this runs it in CI. When the bench eventually reports
that embeddings lifted ``cross-script`` from 0.25 to 0.90, that number has to mean
something.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from retrieval_bench import (  # noqa: E402
    DOCUMENTS,
    GOLD,
    SEMANTIC_KINDS,
    audit_gold_set,
)


def test_no_semantic_case_can_be_answered_by_word_overlap() -> None:
    complaints = audit_gold_set()
    assert not complaints, "gold set inflated:\n" + "\n".join(f"  - {c}" for c in complaints)


def test_every_case_points_at_a_document_that_exists() -> None:
    known = {doc_id for doc_id, _, _, _ in DOCUMENTS}
    dangling = sorted({expected for _, expected, _ in GOLD} - known)
    assert not dangling, f"gold cases reference missing documents: {dangling}"


def test_the_hard_categories_are_actually_represented() -> None:
    """A gold set that quietly loses its hard cases still reports a fine average."""
    counts: dict[str, int] = {}
    for _, _, kind in GOLD:
        counts[kind] = counts.get(kind, 0) + 1
    for kind in SEMANTIC_KINDS:
        assert counts.get(kind, 0) >= 4, f"{kind}: only {counts.get(kind, 0)} cases, too few to read"


@pytest.mark.parametrize("doc_id,title,body,category", DOCUMENTS)
def test_documents_are_substantial_enough_to_retrieve(doc_id, title, body, category) -> None:
    """A one-line document is found by accident, not by ranking."""
    assert len(body) >= 80, f"{doc_id} is too short to be a meaningful retrieval target"
    assert title.strip() and category.strip()
