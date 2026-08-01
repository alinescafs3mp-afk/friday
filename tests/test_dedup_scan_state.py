"""The incremental scan must be able to finish history — and to reopen it.

Two ways the cursor blob defeated itself, both in `_merge_concurrent`.

* The run merged its result against the snapshot it had STARTED from, so «declare
  history finished only if both agree» meant «agree with myself as I was». One
  tick ending with `backfill_done=False` — which any corpus larger than a single
  tick's budget produces — made it impossible for any later run to record the
  backfill as complete, forever.

* «Start history again from the top» was expressed as `backfill=None`, and the
  merge drops falsy cursors: it handed back the stored deep cursor instead. The
  next tick then asked for rows below the bottom of the corpus, got an empty
  page, and declared history swept — with the row that triggered the reopening
  still unexamined.
"""

from __future__ import annotations

import json

from friday.dedup import ScanState, _decode_scan_state, _merge_concurrent, save_scan_state


def _blob(**fields) -> str:
    state = ScanState(**fields)
    encoded: dict[str, object] = {
        "version": 1,
        "model": state.model,
        "threshold": state.threshold,
        "watermark": list(state.watermark) if state.watermark else None,
        "backfill": list(state.backfill) if state.backfill else None,
        "backfill_done": state.backfill_done,
        "swept_below": state.swept_below,
    }
    return json.dumps(encoded)


def test_history_can_be_declared_finished():
    """The state stored NOW is the other party — not this run's own past."""
    # What a previous unfinished tick left behind, and what this run achieved.
    stored = _blob(backfill=("2026-01-01", "ko_5"), backfill_done=False)
    finished = ScanState(backfill=None, backfill_done=True, swept_below=12)

    merged = _merge_concurrent(stored, finished)
    assert merged.backfill_done is False, "a concurrent unfinished run still wins, as designed"

    # …but once nothing else is in flight, completion must stick.
    idle = _blob(backfill=None, backfill_done=True, swept_below=12)
    assert _merge_concurrent(idle, finished).backfill_done is True


def test_reopening_the_backfill_is_not_swallowed():
    stored = _blob(backfill=("2020-01-01", "ko_bottom"), backfill_done=True, swept_below=3)
    reopened = ScanState(backfill=None, backfill_done=False, swept_below=None)

    without_flag = _merge_concurrent(stored, reopened)
    assert without_flag.backfill == ("2020-01-01", "ko_bottom"), "the premise: min() keeps the deep one"

    merged = _merge_concurrent(stored, reopened, restart_backfill=True)
    assert merged.backfill is None, "the restart was swallowed; the scan resumes at the bottom"
    assert merged.backfill_done is False
    assert merged.swept_below is None


def test_the_watermark_never_goes_backwards():
    """The half that was already right: coverage may cost extra work, never gaps."""
    stored = _blob(watermark=("2026-05-05", "ko_9"), backfill_done=False)
    older = ScanState(watermark=("2026-01-01", "ko_1"))
    assert _merge_concurrent(stored, older).watermark == ("2026-05-05", "ko_9")


def test_state_survives_a_round_trip(storage):
    storage.ensure_user("alice")
    state = ScanState(
        model="test-embed",
        threshold=0.95,
        watermark=("2026-05-05", "ko_9"),
        backfill=("2026-01-01", "ko_1"),
        backfill_done=False,
        swept_below=None,
    )
    save_scan_state(storage, "alice", state)
    raw = storage.kv_get("dedup:scan:alice")
    assert raw
    assert _decode_scan_state(raw) == state
