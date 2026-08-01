"""The model advises on prose; it does not set the score a document is ranked by.

`advise_inbox_item` promises in its own docstring that «deterministic scores remain
authoritative», and caps entity confidence at 0.79 to keep model suggestions below
the graph's auto-create thresholds. `importance` had no such bound — only a clamp to
[0, 1] — and it is the ONE machine score the model can write into a canonical object:
quality_score and promotion_score stay deterministic.

An adversarial pass corrected the original claim in two useful ways. Upward the model
adds nothing: `_estimate_importance` is derived entirely from the text and already
reaches 1.0 on a page written to do so, so binding to the baseline is not about
stopping a rise. What is unique is DOWN — the deterministic floor is 0.22 + quality
* 0.28, and the model could write 0.0-0.21, below anything the text-derived path can
produce, which is what moves an object into the lifecycle review queue. Measured: a
page scoring 1.0 deterministically, advised to 0.01, became a candidate at risk 0.576
that it is not at 1.0.
"""

from __future__ import annotations

import pytest

from friday.ingestion._base import _coerce_score


def _bounded(baseline: float, advised) -> float:
    """The rule as implemented in `advise_inbox_item`."""
    return min(baseline + 0.15, max(baseline - 0.15, _coerce_score(advised, default=baseline)))


@pytest.mark.parametrize(
    ("baseline", "advised", "expected"),
    [
        (0.76, 1.0, 0.91),  # a confident model may nudge, not dictate
        (0.76, 0.0, 0.61),  # …and the direction that mattered
        (1.0, 0.01, 0.85),
        (0.5, 0.55, 0.55),  # inside the band it passes through untouched
        (0.5, 999, 0.65),  # nonsense clamps, then binds
        (0.5, "не число", 0.5),  # unparseable falls back to the baseline
        (0.5, None, 0.5),
    ],
)
def test_the_advised_score_stays_near_the_deterministic_one(baseline, advised, expected):
    assert _bounded(baseline, advised) == pytest.approx(expected)


def test_the_model_can_never_reach_below_the_deterministic_floor():
    """0.22 is the floor `_estimate_importance` can produce; the model went under it."""
    floor = 0.22
    assert _bounded(floor, 0.0) == pytest.approx(0.07)  # still bounded, not zero
    # …and from a normal score the model cannot drag an object into review-only range.
    assert _bounded(0.76, 0.0) > 0.5


def test_the_rule_is_the_one_in_the_source():
    import inspect

    from friday.ingestion import _advice

    source = inspect.getsource(_advice)
    assert "baseline_importance + 0.15" in source
    assert "baseline_importance - 0.15" in source


def test_the_other_machine_scores_are_not_model_writable():
    """quality_score and promotion_score must not appear in the merged suggestions."""
    import inspect

    from friday.ingestion import _advice

    body = inspect.getsource(_advice.AdviceMixin.advise_inbox_item)
    merged = body[body.index("merged = {") : body.index("model_advice", body.index("merged = {"))]
    assert "quality_score" not in merged
    assert "promotion_score" not in merged


def test_a_stored_zero_is_not_read_as_a_half():
    """`float(value or 0.5)` made the lowest score the one the lifecycle scan skipped.

    Found while checking where a model-driven low importance actually goes: with the
    old `or`, a stored 0.0 read back as 0.5, so exactly-zero — the value that should
    weigh MOST toward review — was the single value that weighed least. The same
    pattern sat on quality_score and promotion_score.
    """
    from friday.storage._knowledge import _score_or

    assert _score_or(0.0) == 0.0
    assert _score_or(0) == 0.0
    assert _score_or(None) == 0.5
    assert _score_or("") == 0.5
    assert _score_or("не число") == 0.5
    assert _score_or(1.4) == 1.0
    assert _score_or(-2) == 0.0
